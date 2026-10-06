from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, Response

from app.services.storage import (
    MAX_FILE_SIZE,
    UPLOAD_DIR,
    delete_by_token,
    get_active_record,
    store_upload,
    validate_filename,
)

router = APIRouter()


@router.post("/api/upload", status_code=201)
async def upload_file(request: Request, file: UploadFile = File(...)):
    filename = validate_filename(file.filename or "")
    contents = await file.read(MAX_FILE_SIZE + 1)
    if len(contents) > MAX_FILE_SIZE:
        raise HTTPException(status_code=413, detail="File exceeds the 10 MB size limit.")
    try:
        contents.decode("utf-8")
    except UnicodeDecodeError:
        raise HTTPException(status_code=400, detail="Uploaded file must contain valid UTF-8 text.")
    stored = await store_upload(filename, contents)
    public_url = f"{str(request.base_url).rstrip('/')}/{filename}"
    return {
        "filename": filename,
        "url": public_url,
        "public_url": public_url,
        "download_url": f"{str(request.base_url).rstrip('/')}/download/{filename}",
        "delete_token": stored["delete_token"],
        "expires_at": stored["expires_at"],
        "uploaded_at": stored["uploaded_at"],
        "size": stored["size"],
    }


@router.get("/download/{filename}")
async def download_file(filename: str):
    await get_active_record(filename)
    return FileResponse(
        UPLOAD_DIR / filename,
        media_type="application/octet-stream",
        filename=filename,
    )


@router.delete("/api/delete/{secure_token}")
async def delete_file(secure_token: str):
    filename = await delete_by_token(secure_token)
    return {"message": "File deleted successfully.", "filename": filename}


@router.get("/{filename}")
async def get_file(filename: str):
    validate_filename(filename)
    record = await get_active_record(filename)
    contents = (UPLOAD_DIR / filename).read_bytes()
    return Response(
        content=contents,
        media_type="text/plain",
        headers={"Content-Disposition": f'inline; filename="{record["filename"]}"'},
    )
