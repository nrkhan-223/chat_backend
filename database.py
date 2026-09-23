from typing import AsyncGenerator
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlmodel import SQLModel
from config import settings

# Create async engine with robust connection pooling
engine: AsyncEngine = create_async_engine(
    settings.DATABASE_URL,
    echo=settings.DEBUG,
    pool_pre_ping=True,
    pool_size=settings.DB_POOL_SIZE if hasattr(settings, "DB_POOL_SIZE") else 20,
    max_overflow=settings.DB_MAX_OVERFLOW if hasattr(settings, "DB_MAX_OVERFLOW") else 10,
    pool_recycle=1800,  # Recycle connections every 30 minutes to prevent stale/dropped connections
    connect_args={
        "ssl": "require",  # Preferred string format for asyncpg / postgres drivers
    },
)

# Use async_sessionmaker instead of legacy sessionmaker(class_=AsyncSession)
async_session_factory = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,  # Prevents unnecessary premature flushes before commit
)


async def get_session() -> AsyncGenerator[AsyncSession, None]:

    async with async_session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def create_tables() -> None:
    """Create all database tables on application startup."""
    async with engine.begin() as conn:
        await conn.run_sync(lambda sync_conn: SQLModel.metadata.create_all(bind=sync_conn))


async def close_db() -> None:
    """Properly dispose of engine connections on application shutdown."""
    await engine.dispose()
