"""
Storage service for file uploads.
Supports S3, GCS, and local file storage.
"""

import os
import uuid
from typing import Tuple
from pathlib import Path

from config import settings


class StorageService:
    """Handles file storage across different providers."""

    def __init__(self):
        self.provider = settings.STORAGE_PROVIDER

    async def upload_file(self, file_content: bytes, filename: str, content_type: str) -> Tuple[str, str]:
        """Upload a file and return (public_url, file_key)."""
        ext = Path(filename).suffix
        file_key = f"{uuid.uuid4()}{ext}"

        if self.provider == "s3":
            return await self._upload_s3(file_content, file_key, content_type)
        elif self.provider == "gcs":
            return await self._upload_gcs(file_content, file_key, content_type)
        else:
            return await self._upload_local(file_content, file_key, content_type)

    async def _upload_s3(self, file_content: bytes, file_key: str, content_type: str) -> Tuple[str, str]:
        """Upload to AWS S3."""
        import boto3

        s3_client = boto3.client(
            "s3",
            aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
            aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
            region_name=settings.AWS_REGION,
        )

        s3_client.put_object(
            Bucket=settings.AWS_S3_BUCKET_NAME,
            Key=file_key,
            Body=file_content,
            ContentType=content_type,
        )

        url = f"https://{settings.AWS_S3_BUCKET_NAME}.s3.{settings.AWS_REGION}.amazonaws.com/{file_key}"
        return url, file_key

    async def _upload_gcs(self, file_content: bytes, file_key: str, content_type: str) -> Tuple[str, str]:
        """Upload to Google Cloud Storage."""
        from google.cloud import storage

        client = storage.Client.from_service_account_json(settings.GCS_CREDENTIALS_FILE)
        bucket = client.bucket(settings.GCS_BUCKET_NAME)
        blob = bucket.blob(file_key)
        blob.upload_from_string(file_content, content_type=content_type)

        url = f"https://storage.googleapis.com/{settings.GCS_BUCKET_NAME}/{file_key}"
        return url, file_key

    async def _upload_local(self, file_content: bytes, file_key: str, content_type: str) -> Tuple[str, str]:
        """Upload to local filesystem."""
        upload_dir = Path(settings.LOCAL_UPLOAD_DIR)
        upload_dir.mkdir(parents=True, exist_ok=True)

        file_path = upload_dir / file_key
        with open(file_path, "wb") as f:
            f.write(file_content)

        url = f"/uploads/{file_key}"
        return url, file_key

    async def delete_file(self, file_key: str) -> None:
        """Delete a file from storage."""
        if self.provider == "s3":
            import boto3
            s3_client = boto3.client("s3")
            s3_client.delete_object(Bucket=settings.AWS_S3_BUCKET_NAME, Key=file_key)
        elif self.provider == "gcs":
            from google.cloud import storage
            client = storage.Client.from_service_account_json(settings.GCS_CREDENTIALS_FILE)
            bucket = client.bucket(settings.GCS_BUCKET_NAME)
            blob = bucket.blob(file_key)
            blob.delete()
        else:
            file_path = Path(settings.LOCAL_UPLOAD_DIR) / file_key
            if file_path.exists():
                os.remove(file_path)


# Singleton instance
storage_service = StorageService()
