import asyncio
import hashlib
import json
import os
import re
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import HTTPException

BASE_DIR = Path(__file__).resolve().parents[2]
UPLOAD_DIR = BASE_DIR / "uploads"
METADATA_DIR = BASE_DIR / "metadata"
METADATA_FILE = METADATA_DIR / "files.json"
MAX_FILE_SIZE = 10 * 1024 * 1024
RETENTION = timedelta(hours=48)
ALLOWED_EXTENSIONS = {".txt", ".md", ".csv", ".json", ".py", ".js", ".html", ".css"}
SAFE_FILENAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,254}$")
storage_lock = asyncio.Lock()


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def ensure_storage() -> None:
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    METADATA_DIR.mkdir(parents=True, exist_ok=True)
    if not METADATA_FILE.exists():
        METADATA_FILE.write_text("{}", encoding="utf-8")


def load_metadata() -> dict:
    ensure_storage()
    try:
        data = json.loads(METADATA_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        raise HTTPException(status_code=500, detail="File metadata is unavailable.")


def save_metadata(data: dict) -> None:
    ensure_storage()
    temp_file = METADATA_FILE.with_suffix(".tmp")
    temp_file.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(temp_file, METADATA_FILE)


def validate_filename(filename: str) -> str:
    # FileStorage.filename is client-controlled; only accept a single safe basename.
    if not filename or Path(filename).name != filename or "/" in filename or "\\" in filename:
        raise HTTPException(status_code=400, detail="Invalid filename.")
    if not SAFE_FILENAME.fullmatch(filename):
        raise HTTPException(status_code=400, detail="Filename may contain only letters, numbers, dot, underscore, and hyphen.")
    if Path(filename).suffix.lower() not in ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=415, detail="Unsupported file type. Allowed extensions: .txt, .md, .csv, .json, .py, .js, .html, .css.")
    return filename


def expired(record: dict, now: datetime | None = None) -> bool:
    expiry = datetime.fromisoformat(record["expires_at"])
    return (now or now_utc()) >= expiry


def remove_record(filename: str, data: dict) -> None:
    data.pop(filename, None)
    try:
        (UPLOAD_DIR / filename).unlink(missing_ok=True)
    finally:
        save_metadata(data)


async def cleanup_expired() -> None:
    async with storage_lock:
        data = load_metadata()
        now = now_utc()
        removed = False
        for filename, record in list(data.items()):
            if expired(record, now):
                (UPLOAD_DIR / filename).unlink(missing_ok=True)
                data.pop(filename, None)
                removed = True
        if removed:
            save_metadata(data)


async def cleanup_loop() -> None:
    while True:
        await asyncio.sleep(30)
        try:
            await cleanup_expired()
        except Exception:
            # A failed sweep is retried on the next interval; reads also enforce expiry.
            continue


async def store_upload(filename: str, contents: bytes) -> dict:
    async with storage_lock:
        data = load_metadata()
        now = now_utc()
        current = data.get(filename)
        if current and not expired(current, now):
            raise HTTPException(status_code=409, detail="A file with this name already exists.")
        if current:
            (UPLOAD_DIR / filename).unlink(missing_ok=True)
            data.pop(filename, None)

        token = secrets.token_urlsafe(32)
        expires_at = now + RETENTION
        record = {
            "filename": filename,
            "size": len(contents),
            "uploaded_at": now.isoformat(),
            "expires_at": expires_at.isoformat(),
            "delete_token_hash": hashlib.sha256(token.encode("utf-8")).hexdigest(),
        }
        # Exclusive creation prevents overwriting files even if the metadata is stale.
        destination = UPLOAD_DIR / filename
        try:
            with destination.open("xb") as output:
                output.write(contents)
        except FileExistsError:
            raise HTTPException(status_code=409, detail="A file with this name already exists.")
        data[filename] = record
        try:
            save_metadata(data)
        except Exception:
            destination.unlink(missing_ok=True)
            raise
        return {**record, "delete_token": token}


async def get_active_record(filename: str) -> dict:
    validate_filename(filename)
    async with storage_lock:
        data = load_metadata()
        record = data.get(filename)
        if not record:
            raise HTTPException(status_code=404, detail="File not found.")
        if expired(record):
            remove_record(filename, data)
            raise HTTPException(status_code=410, detail="File has expired.")
        if not (UPLOAD_DIR / filename).is_file():
            data.pop(filename, None)
            save_metadata(data)
            raise HTTPException(status_code=404, detail="File not found.")
        return record


async def delete_by_token(token: str) -> str:
    candidate = hashlib.sha256(token.encode("utf-8")).hexdigest()
    async with storage_lock:
        data = load_metadata()
        for filename, record in list(data.items()):
            if secrets.compare_digest(candidate, record["delete_token_hash"]):
                if expired(record):
                    remove_record(filename, data)
                    raise HTTPException(status_code=410, detail="File has expired.")
                remove_record(filename, data)
                return filename
    raise HTTPException(status_code=404, detail="Invalid deletion token.")
