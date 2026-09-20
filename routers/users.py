import uuid
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, or_

from database import get_session
from models import User
from schemas import UserPublic
from auth import get_current_user

router = APIRouter(prefix="/users", tags=["Users"])


@router.get("", response_model=list[UserPublic])
async def list_users(
    search: Optional[str] = Query(None),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Discover users in the workspace."""
    query = select(User).where(User.id != current_user.id)
    if search:
        pattern = f"%{search}%"
        query = query.where(or_(User.name.ilike(pattern), User.email.ilike(pattern)))
    query = query.order_by(User.name.asc()).limit(100)
    result = await session.execute(query)
    users = result.scalars().all()
    return [UserPublic.model_validate(user) for user in users]


@router.get("/{user_id}", response_model=UserPublic)
async def get_user(user_id: str, current_user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    """Get user public profile."""
    result = await session.execute(select(User).where(User.id == uuid.UUID(user_id)))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    return UserPublic.model_validate(user)
