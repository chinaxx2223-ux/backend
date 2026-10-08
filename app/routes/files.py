import logging
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse

logger = logging.getLogger("temptext.routes")

try:
    from app.services.storage import (
        MAX_FILE_SIZE,
        admin_delete_file,
        cleanup_expired,
        delete_by_token,
        get_active_record,
        list_all_records,
        store_upload,
        validate_filename,
    )
except ImportError:
    from services.storage import (
        MAX_FILE_SIZE,
        admin_delete_file,
        cleanup_expired,
        delete_by_token,
        get_active_record,
        list_all_records,
        store_upload,
        validate_filename,
    )

router = APIRouter()

MIME_MAP = {
    ".txt": "text/plain; charset=utf-8",
    ".md": "text/markdown; charset=utf-8",
    ".csv": "text/csv; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".py": "text/x-python; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
}


@router.post("/api/upload", status_code=201)
async def upload_file(
    request: Request,
    file: UploadFile = File(...),
):
    """
    Upload a text file, save to Supabase Storage, and store metadata in Supabase PostgreSQL.
    """
    filename = validate_filename(file.filename or "")

    contents = await file.read(MAX_FILE_SIZE + 1)
    if len(contents) > MAX_FILE_SIZE:
        raise HTTPException(
            status_code=413,
            detail="File exceeds the 10 MB size limit.",
        )

    # Validate valid UTF-8 text
    try:
        contents.decode("utf-8")
    except UnicodeDecodeError:
        raise HTTPException(
            status_code=400,
            detail="Uploaded file must contain valid UTF-8 text.",
        )

    # Preserve or determine the correct MIME content type
    ext = Path(filename).suffix.lower()
    content_type = file.content_type
    if not content_type or content_type == "application/octet-stream":
        content_type = MIME_MAP.get(ext, "text/plain; charset=utf-8")

    stored = await store_upload(
        filename=filename,
        contents=contents,
        content_type=content_type,
    )

    public_url = stored["public_url"]

    # Build download URL properly behind Vercel reverse proxy
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("x-forwarded-host", request.url.netloc)
    base_url = f"{proto}://{host}".rstrip("/")
    download_url = f"{base_url}/download/{quote(filename, safe='()')}"

    return {
        "filename": filename,
        "url": public_url,
        "public_url": public_url,
        "download_url": download_url,
        "delete_token": stored["delete_token"],
        "expires_at": stored["expires_at"],
        "uploaded_at": stored["uploaded_at"],
        "size": stored["size"],
    }


@router.get("/download/{filename}")
async def download_file(filename: str):
    """
    Validates expiration from Supabase and redirects to Supabase Storage public URL.
    """
    record = await get_active_record(filename)
    public_url = record.get("public_url")
    if not public_url:
        raise HTTPException(status_code=404, detail="File URL unavailable.")

    return RedirectResponse(url=public_url, status_code=307)


@router.delete("/api/delete/{secure_token}")
async def delete_file(secure_token: str):
    """
    Uploader Early Deletion: Hashes token, verifies metadata, and deletes
    from Supabase Storage and PostgreSQL.
    """
    filename = await delete_by_token(secure_token)
    return {
        "message": "File deleted successfully.",
        "filename": filename,
    }


@router.get("/api/admin/files")
async def get_admin_files():
    """
    Admin: Lists all active uploaded files from Supabase PostgreSQL.
    Provides fields formatted for the admin table interface.
    """
    records = await list_all_records()
    formatted = []
    for r in records:
        public_url = r.get("public_url", "")
        formatted.append({
            "id": r["filename"],
            "filename": r["filename"],
            "size": r["size"],
            "uploaded_at": r["uploaded_at"],
            "uploadedAt": r["uploaded_at"],
            "expires_at": r["expires_at"],
            "expiresAt": r["expires_at"],
            "url": public_url,
            "public_url": public_url,
        })
    return formatted


@router.delete("/api/admin/files/{file_id}")
async def delete_admin_file(file_id: str):
    """
    Admin: Deletes file by ID/filename from Supabase Storage and PostgreSQL.
    """
    await admin_delete_file(file_id)
    return {
        "message": f"File '{file_id}' deleted successfully.",
        "filename": file_id,
        "success": True,
    }


@router.api_route("/api/cleanup", methods=["GET", "POST"])
async def run_cleanup():
    """
    Cleanup endpoint compatible with Vercel Cron or external triggers.
    Removes expired files from Supabase Storage and PostgreSQL.
    """
    removed_count = await cleanup_expired()
    return {
        "message": "Cleanup completed successfully.",
        "removed_count": removed_count,
    }


@router.get("/{filename}")
async def get_file(filename: str):
    """
    Public direct access URL: Validates file and redirects to Supabase Storage public URL.
    """
    record = await get_active_record(filename)
    public_url = record.get("public_url")
    if not public_url:
        raise HTTPException(status_code=404, detail="File URL unavailable.")

    return RedirectResponse(url=public_url, status_code=307)