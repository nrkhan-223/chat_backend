"""
Main FastAPI application entry point.
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from config import settings
from database import init_db
from routers import auth_router, messages_router, channels_router, users_router, upload_router
from services.websocket import router as ws_router
from services.pubsub import redis_pubsub
from middleware.error_handler import ErrorHandlerMiddleware

# Configure logging
logging.basicConfig(
    level=logging.DEBUG if settings.DEBUG else logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan handler."""
    logger.info("🚀 Starting Chat Application Backend...")
    # logger.info(f"📦 Database: {settings.DATABASE_URL}")
    logger.info(f"💾 Storage Provider: {settings.STORAGE_PROVIDER}")

    # Initialize database
    try:
        await init_db()
        logger.info("✅ Database tables created/verified")
    except Exception as e:
        logger.error(f"❌ Database initialization failed: {e}")

    # Connect to Redis
    await redis_pubsub.connect()

    yield

    # Shutdown
    await redis_pubsub.disconnect()
    logger.info("👋 Shutting down Chat Application Backend...")


# Create FastAPI application
app = FastAPI(
    title=settings.APP_NAME,
    description="Real-time Chat Application API with WebSocket support",
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
)

# Add error handler middleware
app.add_middleware(ErrorHandlerMiddleware)

# CORS Middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount static files for local uploads
if settings.STORAGE_PROVIDER == "local":
    import os
    os.makedirs(settings.LOCAL_UPLOAD_DIR, exist_ok=True)
    app.mount("/uploads", StaticFiles(directory=settings.LOCAL_UPLOAD_DIR), name="uploads")

# Include API routers
app.include_router(auth_router, prefix="/api")
app.include_router(messages_router, prefix="/api")
app.include_router(channels_router, prefix="/api")
app.include_router(users_router, prefix="/api")
app.include_router(upload_router, prefix="/api")

# Include WebSocket router
app.include_router(ws_router)


@app.get("/health", tags=["Health"])
async def health_check():
    """Health check endpoint."""
    return {"status": "healthy", "app": settings.APP_NAME, "version": "1.0.0"}


@app.get("/", tags=["Root"])
async def root():
    """Root endpoint."""
    return {"message": "Chat Application API", "docs": "/docs", "websocket": "/ws?token=<JWT_TOKEN>", "version": "1.0.0"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=settings.DEBUG, log_level="info")
