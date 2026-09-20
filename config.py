

from pydantic_settings import BaseSettings
from typing import Optional, List


class Settings(BaseSettings):
    # Application
    APP_NAME: str = "Chat Application"
    DEBUG: bool = False
    API_PREFIX: str = "/api"

    # Database
    DATABASE_URL: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/chatapp"
    DATABASE_URL_SYNC: str = "postgresql://postgres:postgres@localhost:5432/chatapp"

    # JWT Authentication
    SECRET_KEY: str = "your-super-secret-key-change-in-production-min-32-chars"
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 10080  # 7 days

    # Redis (for multi-worker WebSocket pub/sub)
    REDIS_URL: str = "redis://localhost:6379/0"

    # File Storage - S3
    AWS_ACCESS_KEY_ID: Optional[str] = None
    AWS_SECRET_ACCESS_KEY: Optional[str] = None
    AWS_S3_BUCKET_NAME: Optional[str] = None
    AWS_REGION: str = "us-east-1"

    # File Storage - GCS (alternative to S3)
    GCS_BUCKET_NAME: Optional[str] = None
    GCS_CREDENTIALS_FILE: Optional[str] = None

    # Storage provider: "s3", "gcs", or "local"
    STORAGE_PROVIDER: str = "local"
    LOCAL_UPLOAD_DIR: str = "./uploads"

    # File upload limits
    MAX_IMAGE_SIZE_MB: int = 10
    MAX_FILE_SIZE_MB: int = 50
    ALLOWED_IMAGE_TYPES: List[str] = ["image/jpeg", "image/png", "image/gif", "image/webp"]

    # Presence
    PRESENCE_OFFLINE_DELAY_SECONDS: int = 30

    # CORS
    CORS_ORIGINS: List[str] = ["http://localhost:3000", "http://localhost:5173"]

    class Config:
        env_file = ".env"
        case_sensitive = True


settings = Settings()
