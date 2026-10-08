import io
import sys
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

from app.main import app
from app.services.storage import now_utc, set_supabase_client


class TestApiEndpoints(unittest.TestCase):
    def setUp(self):
        self.mock_client = MagicMock()
        self.mock_table = MagicMock()
        self.mock_storage_bucket = MagicMock()

        self.mock_client.table.return_value = self.mock_table
        self.mock_client.storage.from_.return_value = self.mock_storage_bucket
        self.mock_storage_bucket.get_public_url.side_effect = lambda f: f"https://mock.supabase.co/storage/v1/object/public/uploads/{f}"

        set_supabase_client(self.mock_client)
        self.client = TestClient(app)

    def tearDown(self):
        set_supabase_client(None)

    def test_health_check(self):
        response = self.client.get("/api/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})

    def test_upload_success(self):
        # Table select returns no existing file
        select_mock = MagicMock()
        select_mock.eq.return_value.execute.return_value = MagicMock(data=[])
        self.mock_table.select.return_value = select_mock

        # Table insert succeeds
        self.mock_table.insert.return_value.execute.return_value = MagicMock(data=[{"filename": "hello.txt"}])

        response = self.client.post(
            "/api/upload",
            files={"file": ("hello.txt", io.BytesIO(b"Hello world!"), "text/plain")},
        )
        self.assertEqual(response.status_code, 201)
        data = response.json()
        self.assertEqual(data["filename"], "hello.txt")
        self.assertIn("delete_token", data)
        self.assertIn("public_url", data)
        self.assertIn("download_url", data)
        self.assertEqual(data["size"], 12)

    def test_upload_pasted_text_with_spaces_and_parentheses(self):
        select_mock = MagicMock()
        select_mock.eq.return_value.execute.return_value = MagicMock(data=[])
        self.mock_table.select.return_value = select_mock
        self.mock_table.insert.return_value.execute.return_value = MagicMock(data=[{"filename": "Pasted text(10).txt"}])

        response = self.client.post(
            "/api/upload",
            files={"file": ("Pasted text(10).txt", io.BytesIO(b"Hello Supabase"), "text/plain")},
        )
        self.assertEqual(response.status_code, 201)
        data = response.json()
        self.assertEqual(data["filename"], "Pasted text(10).txt")
        self.assertIn("delete_token", data)
        self.assertIn("public_url", data)
        self.assertIn("download_url", data)
        self.assertEqual(data["size"], 14)

    def test_upload_invalid_utf8(self):
        # Invalid UTF-8 bytes
        invalid_bytes = b"\xff\xfe\x00\x00\xaa\xbb"
        response = self.client.post(
            "/api/upload",
            files={"file": ("bad.txt", io.BytesIO(invalid_bytes), "text/plain")},
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("UTF-8", response.json()["detail"])

    def test_upload_unsupported_extension(self):
        response = self.client.post(
            "/api/upload",
            files={"file": ("danger.exe", io.BytesIO(b"binary"), "application/octet-stream")},
        )
        self.assertEqual(response.status_code, 415)

    def test_upload_file_too_large(self):
        large_bytes = b"x" * (10 * 1024 * 1024 + 10)
        response = self.client.post(
            "/api/upload",
            files={"file": ("big.txt", io.BytesIO(large_bytes), "text/plain")},
        )
        self.assertEqual(response.status_code, 413)
        self.assertIn("10 MB", response.json()["detail"])

    def test_upload_duplicate_file(self):
        active_record = {
            "filename": "existing.txt",
            "size": 10,
            "uploaded_at": now_utc().isoformat(),
            "expires_at": (now_utc() + timedelta(hours=24)).isoformat(),
            "delete_token_hash": "dummyhash",
        }
        select_mock = MagicMock()
        select_mock.eq.return_value.execute.return_value = MagicMock(data=[active_record])
        self.mock_table.select.return_value = select_mock

        response = self.client.post(
            "/api/upload",
            files={"file": ("existing.txt", io.BytesIO(b"Hello Supabase"), "text/plain")},
        )
        self.assertEqual(response.status_code, 409)
        self.assertIn("already exists", response.json()["detail"])

    def test_download_redirect(self):
        record = {
            "filename": "myfile.txt",
            "size": 5,
            "uploaded_at": now_utc().isoformat(),
            "expires_at": (now_utc() + timedelta(hours=24)).isoformat(),
            "delete_token_hash": "hash",
        }
        select_mock = MagicMock()
        select_mock.eq.return_value.execute.return_value = MagicMock(data=[record])
        self.mock_table.select.return_value = select_mock

        response = self.client.get("/download/myfile.txt", follow_redirects=False)
        self.assertEqual(response.status_code, 307)
        self.assertEqual(
            response.headers["location"],
            "https://mock.supabase.co/storage/v1/object/public/uploads/myfile.txt",
        )

    def test_delete_file_by_token(self):
        record = {
            "filename": "trash.txt",
            "size": 5,
            "uploaded_at": now_utc().isoformat(),
            "expires_at": (now_utc() + timedelta(hours=24)).isoformat(),
            "delete_token_hash": "dummyhash",
        }
        # In test, we can mock delete_token_hash query match
        select_mock = MagicMock()
        select_mock.eq.return_value.execute.return_value = MagicMock(data=[record])
        self.mock_table.select.return_value = select_mock

        delete_mock = MagicMock()
        delete_mock.eq.return_value.execute.return_value = MagicMock()
        self.mock_table.delete.return_value = delete_mock

        response = self.client.delete("/api/delete/any_token")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["filename"], "trash.txt")

    def test_admin_list_files(self):
        now = now_utc()
        records = [
            {
                "filename": "f1.txt",
                "size": 100,
                "uploaded_at": now.isoformat(),
                "expires_at": (now + timedelta(hours=20)).isoformat(),
                "delete_token_hash": "h1",
            }
        ]
        order_mock = MagicMock()
        order_mock.execute.return_value = MagicMock(data=records)
        self.mock_table.select.return_value.order.return_value = order_mock

        response = self.client.get("/api/admin/files")
        self.assertEqual(response.status_code, 200)
        files = response.json()
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0]["id"], "f1.txt")
        self.assertEqual(files[0]["filename"], "f1.txt")
        self.assertIn("uploadedAt", files[0])
        self.assertIn("expiresAt", files[0])

    def test_admin_delete_file(self):
        record = {
            "filename": "to_remove.txt",
            "size": 50,
            "uploaded_at": now_utc().isoformat(),
            "expires_at": (now_utc() + timedelta(hours=10)).isoformat(),
            "delete_token_hash": "h",
        }
        select_mock = MagicMock()
        select_mock.eq.return_value.execute.return_value = MagicMock(data=[record])
        self.mock_table.select.return_value = select_mock

        delete_mock = MagicMock()
        delete_mock.eq.return_value.execute.return_value = MagicMock()
        self.mock_table.delete.return_value = delete_mock

        response = self.client.delete("/api/admin/files/to_remove.txt")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["success"])

    def test_cleanup_endpoint(self):
        self.mock_table.select.return_value.execute.return_value = MagicMock(data=[])
        response = self.client.post("/api/cleanup")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["removed_count"], 0)


if __name__ == "__main__":
    unittest.main()
