"""
File upload API endpoints.
"""

from fastapi import APIRouter, Depends, HTTPException, status, UploadFile, File

from models import User
from schemas import UploadResponse
from auth import get_current_user
from services.storage import storage_service
from config import settings

router = APIRouter(prefix="/upload", tags=["Uploads"])


@router.post("/image", response_model=UploadResponse)
async def upload_image(file: UploadFile = File(...), current_user: User = Depends(get_current_user)):
    """Upload an image file."""
    if file.content_type not in settings.ALLOWED_IMAGE_TYPES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Invalid image type. Allowed: {', '.join(settings.ALLOWED_IMAGE_TYPES)}")

    content = await file.read()
    max_size = settings.MAX_IMAGE_SIZE_MB * 1024 * 1024
    if len(content) > max_size:
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail=f"Image exceeds {settings.MAX_IMAGE_SIZE_MB}MB")

    url, _ = await storage_service.upload_file(content, file.filename or "image.jpg", file.content_type)
    return UploadResponse(url=url, name=file.filename or "image.jpg", type="image", size_bytes=len(content))


@router.post("/file", response_model=UploadResponse)
async def upload_file(file: UploadFile = File(...), current_user: User = Depends(get_current_user)):
    """Upload a general file."""
    content = await file.read()
    max_size = settings.MAX_FILE_SIZE_MB * 1024 * 1024
    if len(content) > max_size:
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail=f"File exceeds {settings.MAX_FILE_SIZE_MB}MB")

    file_type = "image" if file.content_type and file.content_type.startswith("image/") else "file"
    url, _ = await storage_service.upload_file(content, file.filename or "file", file.content_type or "application/octet-stream")
    return UploadResponse(url=url, name=file.filename or "file", type=file_type, size_bytes=len(content))
