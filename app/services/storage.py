import asyncio
import hashlib
import logging
import os
import re
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import HTTPException
from supabase import Client, create_client

logger = logging.getLogger("temptext.storage")

MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MB
RETENTION = timedelta(hours=48)
ALLOWED_EXTENSIONS = {
    ".txt",
    ".md",
    ".csv",
    ".json",
    ".py",
    ".js",
    ".html",
    ".css",
}
# Allow alphanumeric characters, spaces, dots, hyphens, underscores, brackets, and parentheses
SAFE_FILENAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._\-()\[\]]{0,254}$")
BUCKET_NAME = "uploads"

storage_lock = asyncio.Lock()
_supabase_client: Optional[Client] = None


def get_supabase_client() -> Client:
    """
    Lazily initializes and returns the Supabase client.
    Ensures missing environment variables do not crash module import.
    """
    global _supabase_client
    if _supabase_client is not None:
        return _supabase_client

    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    supabase_url = os.environ.get("SUPABASE_URL", "").strip()
    supabase_secret_key = os.environ.get("SUPABASE_SECRET_KEY", "").strip()

    if not supabase_url or not supabase_secret_key:
        logger.error(
            "Supabase configuration missing: SUPABASE_URL or SUPABASE_SECRET_KEY is not set."
        )
        raise HTTPException(
            status_code=500,
            detail=(
                "Supabase configuration is missing. "
                "SUPABASE_URL and SUPABASE_SECRET_KEY environment variables must be configured in Vercel / server settings."
            ),
        )

    try:
        _supabase_client = create_client(supabase_url, supabase_secret_key)
        return _supabase_client
    except Exception as exc:
        logger.error("Failed to initialize Supabase client: %s", exc)
        raise HTTPException(
            status_code=500,
            detail="Failed to initialize Supabase client. Please check connection and credentials.",
        )


def set_supabase_client(client: Optional[Client]) -> None:
    """Helper to set or reset the client (useful for unit testing and mocks)."""
    global _supabase_client
    _supabase_client = client


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def ensure_storage() -> None:
    """
    Storage and metadata are handled persistently by Supabase Storage and PostgreSQL.
    No persistent files or folders are needed on the local/Vercel serverless filesystem.
    """
    pass


def validate_filename(filename: str) -> str:
    """Validate client-supplied filename for path safety and allowed extensions."""
    if not filename or not isinstance(filename, str):
        raise HTTPException(status_code=400, detail="Invalid filename.")

    cleaned_name = filename.strip()

    if (
        not cleaned_name
        or Path(cleaned_name).name != cleaned_name
        or "/" in cleaned_name
        or "\\" in cleaned_name
        or ".." in cleaned_name
    ):
        raise HTTPException(status_code=400, detail="Invalid filename.")

    if not SAFE_FILENAME.fullmatch(cleaned_name):
        raise HTTPException(
            status_code=400,
            detail="Filename may contain only letters, numbers, spaces, dot, underscore, parentheses, brackets, and hyphen.",
        )

    if Path(cleaned_name).suffix.lower() not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=415,
            detail=(
                "Unsupported file type. Allowed extensions: "
                ".txt, .md, .csv, .json, .py, .js, .html, .css."
            ),
        )

    return cleaned_name


def expired(record: dict, now: datetime | None = None) -> bool:
    """Check if a file metadata record has passed its expiration time."""
    expires_at_str = record.get("expires_at", "")
    try:
        expiry = datetime.fromisoformat(expires_at_str.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return True
    return (now or now_utc()) >= expiry


def get_public_url(filename: str) -> str:
    """Retrieve the Supabase public Storage URL for a given filename."""
    supabase = get_supabase_client()
    try:
        return supabase.storage.from_(BUCKET_NAME).get_public_url(filename)
    except Exception as exc:
        logger.error("Failed to generate public URL for '%s': %s", filename, exc)
        return ""


async def remove_record(filename: str, raise_on_error: bool = True) -> None:
    """
    Deletes the file from Supabase Storage and removes its metadata row from PostgreSQL.
    """
    supabase = get_supabase_client()

    # 1. Delete actual file from Supabase Storage
    try:
        supabase.storage.from_(BUCKET_NAME).remove([filename])
    except Exception as exc:
        logger.error("Supabase Storage deletion error for '%s': %s", filename, exc)
        if raise_on_error:
            raise HTTPException(
                status_code=500,
                detail=f"Failed to delete file from storage: {exc}",
            )

    # 2. Delete metadata row from PostgreSQL table
    try:
        supabase.table("uploaded_files").delete().eq("filename", filename).execute()
    except Exception as exc:
        logger.error("Supabase DB delete metadata error for '%s': %s", filename, exc)
        if raise_on_error:
            raise HTTPException(
                status_code=500,
                detail=f"Failed to delete file metadata: {exc}",
            )


async def store_upload(
    filename: str,
    contents: bytes,
    content_type: str = "text/plain",
) -> dict:
    """
    Uploads file content to Supabase Storage and records metadata in PostgreSQL.
    Rolls back Storage upload if metadata insert fails.
    """
    async with storage_lock:
        supabase = get_supabase_client()
        now = now_utc()
        expires_at = now + RETENTION

        # Check existing metadata in Supabase PostgreSQL
        try:
            result = (
                supabase.table("uploaded_files")
                .select("*")
                .eq("filename", filename)
                .execute()
            )
            current = result.data[0] if (result and result.data) else None
        except HTTPException:
            raise
        except Exception as exc:
            logger.error("Failed to query database for '%s': %s", filename, exc)
            raise HTTPException(
                status_code=500,
                detail="Failed to query database for existing file metadata.",
            )

        if current and not expired(current, now):
            raise HTTPException(
                status_code=409,
                detail="A file with this name already exists.",
            )

        # If an expired record with this name exists, remove it before uploading new file
        if current:
            await remove_record(filename, raise_on_error=False)

        # Create deletion token and SHA-256 hash
        token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()

        # Upload actual file bytes to Supabase Storage
        try:
            supabase.storage.from_(BUCKET_NAME).upload(
                path=filename,
                file=contents,
                file_options={
                    "content-type": content_type or "text/plain",
                    "upsert": "true",
                },
            )
        except Exception as exc:
            err_msg = str(exc).lower()
            logger.error("Supabase Storage upload error for '%s': %s", filename, exc)
            if "already exists" in err_msg or "duplicate" in err_msg or "409" in err_msg:
                raise HTTPException(
                    status_code=409,
                    detail="A file with this name already exists.",
                )
            raise HTTPException(
                status_code=500,
                detail="Failed to upload file to storage.",
            )

        record = {
            "filename": filename,
            "size": len(contents),
            "uploaded_at": now.isoformat(),
            "expires_at": expires_at.isoformat(),
            "delete_token_hash": token_hash,
        }

        # Save metadata to Supabase PostgreSQL table
        try:
            supabase.table("uploaded_files").insert(record).execute()
        except Exception as exc:
            logger.error("Database metadata insert error for '%s': %s", filename, exc)
            # Rollback Storage upload to prevent orphan files
            try:
                supabase.storage.from_(BUCKET_NAME).remove([filename])
            except Exception as rollback_err:
                logger.error("Storage rollback failed for '%s': %s", filename, rollback_err)

            err_msg = str(exc).lower()
            if "unique" in err_msg or "duplicate" in err_msg or "already exists" in err_msg:
                raise HTTPException(
                    status_code=409,
                    detail="A file with this name already exists.",
                )
            raise HTTPException(
                status_code=500,
                detail="Failed to save file metadata.",
            )

        public_url = get_public_url(filename)

        return {
            **record,
            "delete_token": token,
            "public_url": public_url,
        }


async def get_active_record(filename: str) -> dict:
    """
    Retrieves active metadata for a file. If expired, automatically cleans it up.
    """
    valid_name = validate_filename(filename)

    async with storage_lock:
        supabase = get_supabase_client()
        try:
            result = (
                supabase.table("uploaded_files")
                .select("*")
                .eq("filename", valid_name)
                .execute()
            )
        except HTTPException:
            raise
        except Exception as exc:
            logger.error("Database query failed while fetching file '%s': %s", valid_name, exc)
            raise HTTPException(
                status_code=500,
                detail="Database query failed while fetching file.",
            )

        if not result or not result.data:
            raise HTTPException(status_code=404, detail="File not found.")

        record = result.data[0]

        if expired(record):
            await remove_record(valid_name, raise_on_error=False)
            raise HTTPException(status_code=410, detail="File has expired.")

        record["public_url"] = get_public_url(valid_name)
        return record


async def delete_by_token(token: str) -> str:
    """
    Validates deletion token and deletes file from Storage and PostgreSQL.
    """
    if not token or not isinstance(token, str):
        raise HTTPException(status_code=404, detail="Invalid deletion token.")

    candidate = hashlib.sha256(token.encode("utf-8")).hexdigest()

    async with storage_lock:
        supabase = get_supabase_client()
        try:
            result = (
                supabase.table("uploaded_files")
                .select("*")
                .eq("delete_token_hash", candidate)
                .execute()
            )
        except HTTPException:
            raise
        except Exception as exc:
            logger.error("Database query failed during token delete: %s", exc)
            raise HTTPException(
                status_code=500,
                detail="Database query failed while validating deletion token.",
            )

        if not result or not result.data:
            raise HTTPException(status_code=404, detail="Invalid deletion token.")

        record = result.data[0]
        filename = record["filename"]

        if expired(record):
            await remove_record(filename, raise_on_error=False)
            raise HTTPException(status_code=410, detail="File has expired.")

        await remove_record(filename, raise_on_error=True)
        return filename


async def list_all_records() -> list[dict]:
    """
    Admin: Lists all active uploaded files, cleaning up any expired files found.
    """
    async with storage_lock:
        supabase = get_supabase_client()
        try:
            result = (
                supabase.table("uploaded_files")
                .select("*")
                .order("uploaded_at", desc=True)
                .execute()
            )
        except HTTPException:
            raise
        except Exception as exc:
            logger.error("Database query failed during admin list: %s", exc)
            raise HTTPException(
                status_code=500,
                detail="Database query failed while fetching files list.",
            )

        records = result.data if (result and result.data) else []
        active = []
        now = now_utc()

        for record in records:
            if expired(record, now):
                await remove_record(record["filename"], raise_on_error=False)
                continue
            record["public_url"] = get_public_url(record["filename"])
            active.append(record)

        return active


async def admin_delete_file(filename: str) -> None:
    """
    Admin: Deletes file from Supabase Storage and PostgreSQL table.
    """
    valid_name = validate_filename(filename)

    async with storage_lock:
        supabase = get_supabase_client()
        try:
            result = (
                supabase.table("uploaded_files")
                .select("*")
                .eq("filename", valid_name)
                .execute()
            )
        except HTTPException:
            raise
        except Exception as exc:
            logger.error("Database query failed during admin delete of '%s': %s", valid_name, exc)
            raise HTTPException(
                status_code=500,
                detail="Database query failed while checking file.",
            )

        if not result or not result.data:
            raise HTTPException(
                status_code=404,
                detail=f"File '{valid_name}' not found.",
            )

        await remove_record(valid_name, raise_on_error=True)


async def cleanup_expired() -> int:
    """
    Scans and removes all expired files from Supabase Storage and PostgreSQL.
    Returns the number of removed files.
    """
    async with storage_lock:
        try:
            supabase = get_supabase_client()
        except Exception:
            return 0

        try:
            result = (
                supabase.table("uploaded_files")
                .select("*")
                .execute()
            )
        except Exception:
            return 0

        records = result.data if (result and result.data) else []
        now = now_utc()
        removed_count = 0

        for record in records:
            if expired(record, now):
                try:
                    await remove_record(record["filename"], raise_on_error=False)
                    removed_count += 1
                except Exception:
                    pass

        return removed_count


async def cleanup_loop() -> None:
    """
    Background worker loop for persistent server environments (e.g. local uvicorn).
    """
    while True:
        await asyncio.sleep(1800)
        try:
            await cleanup_expired()
        except Exception:
            continue