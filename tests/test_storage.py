import asyncio
import hashlib
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import HTTPException

from app.services.storage import (
    BUCKET_NAME,
    admin_delete_file,
    cleanup_expired,
    delete_by_token,
    expired,
    get_active_record,
    list_all_records,
    now_utc,
    set_supabase_client,
    store_upload,
    validate_filename,
)


class TestValidationAndExpiry(unittest.TestCase):
    def test_validate_filename_valid(self):
        valid_names = [
            "note.txt",
            "data_2026.csv",
            "script.py",
            "readme.md",
            "config.json",
            "app.js",
            "index.html",
            "style.css",
            "Pasted text(10).txt",
            "has spaces.txt",
            "my (notes) [draft].txt",
        ]
        for name in valid_names:
            self.assertEqual(validate_filename(name), name)

    def test_validate_filename_invalid_characters_and_paths(self):
        invalid_names = [
            "",
            "../etc/passwd.txt",
            "..\\windows\\test.txt",
            "/absolute.txt",
            "special$chars.txt",
            ".hidden.txt",
            "bad;cmd.txt",
        ]
        for name in invalid_names:
            with self.assertRaises(HTTPException) as ctx:
                validate_filename(name)
            self.assertEqual(ctx.exception.status_code, 400)

    def test_validate_filename_unsupported_extensions(self):
        unsupported = [
            "malware.exe",
            "image.png",
            "data.zip",
            "video.mp4",
            "archive.tar.gz",
        ]
        for name in unsupported:
            with self.assertRaises(HTTPException) as ctx:
                validate_filename(name)
            self.assertEqual(ctx.exception.status_code, 415)

    def test_expired_logic(self):
        now = now_utc()
        past = now - timedelta(hours=1)
        future = now + timedelta(hours=48)

        self.assertTrue(expired({"expires_at": past.isoformat()}, now))
        self.assertFalse(expired({"expires_at": future.isoformat()}, now))
        self.assertTrue(expired({"expires_at": "invalid-date"}, now))


class TestSupabaseStorageOperations(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # Create a mock Supabase client
        self.mock_client = MagicMock()
        self.mock_table = MagicMock()
        self.mock_storage_bucket = MagicMock()

        self.mock_client.table.return_value = self.mock_table
        self.mock_client.storage.from_.return_value = self.mock_storage_bucket

        self.mock_storage_bucket.get_public_url.side_effect = lambda f: f"https://test.supabase.co/storage/v1/object/public/uploads/{f}"
        set_supabase_client(self.mock_client)

    def tearDown(self):
        set_supabase_client(None)

    async def test_store_upload_success(self):
        # Table select returns no existing file
        select_mock = MagicMock()
        select_mock.eq.return_value.execute.return_value = MagicMock(data=[])
        self.mock_table.select.return_value = select_mock

        # Table insert succeeds
        self.mock_table.insert.return_value.execute.return_value = MagicMock(data=[{"filename": "test.txt"}])

        # Storage upload succeeds
        self.mock_storage_bucket.upload.return_value = MagicMock()

        result = await store_upload("test.txt", b"hello world", "text/plain")
        self.assertEqual(result["filename"], "test.txt")
        self.assertEqual(result["size"], 11)
        self.assertIn("delete_token", result)
        self.assertIn("https://test.supabase.co/storage/v1/object/public/uploads/test.txt", result["public_url"])

        # Check storage upload was called
        self.mock_storage_bucket.upload.assert_called_once()

    async def test_store_upload_duplicate_active_file_raises_409(self):
        # Existing active record
        active_record = {
            "filename": "duplicate.txt",
            "size": 10,
            "uploaded_at": now_utc().isoformat(),
            "expires_at": (now_utc() + timedelta(hours=24)).isoformat(),
            "delete_token_hash": "dummyhash",
        }
        select_mock = MagicMock()
        select_mock.eq.return_value.execute.return_value = MagicMock(data=[active_record])
        self.mock_table.select.return_value = select_mock

        with self.assertRaises(HTTPException) as ctx:
            await store_upload("duplicate.txt", b"data", "text/plain")
        self.assertEqual(ctx.exception.status_code, 409)

    async def test_store_upload_rollback_on_metadata_failure(self):
        # Table select returns no existing file
        select_mock = MagicMock()
        select_mock.eq.return_value.execute.return_value = MagicMock(data=[])
        self.mock_table.select.return_value = select_mock

        # Storage upload succeeds
        self.mock_storage_bucket.upload.return_value = MagicMock()

        # Table insert fails with database exception
        self.mock_table.insert.return_value.execute.side_effect = Exception("DB connection timeout")

        with self.assertRaises(HTTPException) as ctx:
            await store_upload("fail.txt", b"content", "text/plain")
        self.assertEqual(ctx.exception.status_code, 500)

        # Verify storage remove was called to rollback the uploaded file
        self.mock_storage_bucket.remove.assert_called_with(["fail.txt"])

    async def test_get_active_record_success(self):
        record = {
            "filename": "hello.txt",
            "size": 5,
            "uploaded_at": now_utc().isoformat(),
            "expires_at": (now_utc() + timedelta(hours=24)).isoformat(),
            "delete_token_hash": "hash123",
        }
        select_mock = MagicMock()
        select_mock.eq.return_value.execute.return_value = MagicMock(data=[record])
        self.mock_table.select.return_value = select_mock

        result = await get_active_record("hello.txt")
        self.assertEqual(result["filename"], "hello.txt")
        self.assertIn("public_url", result)

    async def test_get_active_record_not_found(self):
        select_mock = MagicMock()
        select_mock.eq.return_value.execute.return_value = MagicMock(data=[])
        self.mock_table.select.return_value = select_mock

        with self.assertRaises(HTTPException) as ctx:
            await get_active_record("missing.txt")
        self.assertEqual(ctx.exception.status_code, 404)

    async def test_get_active_record_expired_raises_410(self):
        expired_record = {
            "filename": "expired.txt",
            "size": 5,
            "uploaded_at": (now_utc() - timedelta(hours=50)).isoformat(),
            "expires_at": (now_utc() - timedelta(hours=2)).isoformat(),
            "delete_token_hash": "hash123",
        }
        select_mock = MagicMock()
        select_mock.eq.return_value.execute.return_value = MagicMock(data=[expired_record])
        self.mock_table.select.return_value = select_mock

        delete_mock = MagicMock()
        delete_mock.eq.return_value.execute.return_value = MagicMock()
        self.mock_table.delete.return_value = delete_mock

        with self.assertRaises(HTTPException) as ctx:
            await get_active_record("expired.txt")
        self.assertEqual(ctx.exception.status_code, 410)

        # Verify removal was triggered
        self.mock_storage_bucket.remove.assert_called_with(["expired.txt"])

    async def test_delete_by_token_success(self):
        raw_token = "valid_token_string_12345"
        token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
        record = {
            "filename": "to_delete.txt",
            "size": 10,
            "uploaded_at": now_utc().isoformat(),
            "expires_at": (now_utc() + timedelta(hours=24)).isoformat(),
            "delete_token_hash": token_hash,
        }

        select_mock = MagicMock()
        select_mock.eq.return_value.execute.return_value = MagicMock(data=[record])
        self.mock_table.select.return_value = select_mock

        delete_mock = MagicMock()
        delete_mock.eq.return_value.execute.return_value = MagicMock()
        self.mock_table.delete.return_value = delete_mock

        filename = await delete_by_token(raw_token)
        self.assertEqual(filename, "to_delete.txt")
        self.mock_storage_bucket.remove.assert_called_with(["to_delete.txt"])

    async def test_delete_by_token_storage_error_raises_500(self):
        raw_token = "valid_token"
        token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
        record = {
            "filename": "fail_delete.txt",
            "size": 10,
            "uploaded_at": now_utc().isoformat(),
            "expires_at": (now_utc() + timedelta(hours=24)).isoformat(),
            "delete_token_hash": token_hash,
        }

        select_mock = MagicMock()
        select_mock.eq.return_value.execute.return_value = MagicMock(data=[record])
        self.mock_table.select.return_value = select_mock

        # Storage deletion fails
        self.mock_storage_bucket.remove.side_effect = Exception("Storage S3 error")

        with self.assertRaises(HTTPException) as ctx:
            await delete_by_token(raw_token)
        self.assertEqual(ctx.exception.status_code, 500)

    async def test_admin_delete_file(self):
        record = {
            "filename": "admin_file.txt",
            "size": 20,
            "uploaded_at": now_utc().isoformat(),
            "expires_at": (now_utc() + timedelta(hours=24)).isoformat(),
            "delete_token_hash": "hash",
        }
        select_mock = MagicMock()
        select_mock.eq.return_value.execute.return_value = MagicMock(data=[record])
        self.mock_table.select.return_value = select_mock

        delete_mock = MagicMock()
        delete_mock.eq.return_value.execute.return_value = MagicMock()
        self.mock_table.delete.return_value = delete_mock

        await admin_delete_file("admin_file.txt")
        self.mock_storage_bucket.remove.assert_called_with(["admin_file.txt"])

    async def test_cleanup_expired(self):
        now = now_utc()
        active = {
            "filename": "active.txt",
            "size": 10,
            "uploaded_at": now.isoformat(),
            "expires_at": (now + timedelta(hours=10)).isoformat(),
            "delete_token_hash": "h1",
        }
        exp1 = {
            "filename": "old1.txt",
            "size": 10,
            "uploaded_at": (now - timedelta(hours=50)).isoformat(),
            "expires_at": (now - timedelta(hours=2)).isoformat(),
            "delete_token_hash": "h2",
        }
        exp2 = {
            "filename": "old2.txt",
            "size": 10,
            "uploaded_at": (now - timedelta(hours=60)).isoformat(),
            "expires_at": (now - timedelta(hours=12)).isoformat(),
            "delete_token_hash": "h3",
        }

        self.mock_table.select.return_value.execute.return_value = MagicMock(data=[active, exp1, exp2])
        delete_mock = MagicMock()
        delete_mock.eq.return_value.execute.return_value = MagicMock()
        self.mock_table.delete.return_value = delete_mock

        removed = await cleanup_expired()
        self.assertEqual(removed, 2)


if __name__ == "__main__":
    unittest.main()
