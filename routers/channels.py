"""
Channels API endpoints.
"""

import uuid
import secrets
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, and_, or_

from database import get_session
from models import User, Channel, ChannelMember
from schemas import ChannelPublic, ChannelCreate, ChannelUpdateSettings, ChannelJoinRequest
from auth import get_current_user

router = APIRouter(prefix="/channels", tags=["Channels"])


@router.get("", response_model=list[ChannelPublic])
async def list_channels(
    search: Optional[str] = Query(None),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """List channels (public + user's private channels)."""
    member_query = select(ChannelMember.channel_id).where(ChannelMember.user_id == current_user.id)
    member_result = await session.execute(member_query)
    member_channel_ids = {row[0] for row in member_result.all()}

    query = select(Channel)
    if search:
        query = query.where(Channel.name.ilike(f"%{search}%"))
    query = query.order_by(Channel.created_at.desc())
    result = await session.execute(query)
    channels = result.scalars().all()

    filtered = [ch for ch in channels if not ch.is_private or ch.id in member_channel_ids]

    channels_data = []
    for channel in filtered:
        count_result = await session.execute(
            select(func.count(ChannelMember.user_id)).where(ChannelMember.channel_id == channel.id)
        )
        member_count = count_result.scalar() or 0
        channels_data.append(ChannelPublic(
            id=channel.id, name=channel.name, description=channel.description,
            is_private=channel.is_private, topic=channel.topic,
            created_at=channel.created_at, created_by=channel.created_by,
            invite_code=channel.invite_code if channel.id in member_channel_ids else None,
            only_admins_can_post=channel.only_admins_can_post, member_count=member_count,
        ))
    return channels_data


@router.post("", response_model=ChannelPublic, status_code=status.HTTP_201_CREATED)
async def create_channel(data: ChannelCreate, current_user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    """Create a new channel."""
    invite_code = secrets.token_urlsafe(12) if data.is_private else None
    channel = Channel(name=data.name, description=data.description, is_private=data.is_private, topic=data.topic, created_by=current_user.id, invite_code=invite_code)
    session.add(channel)
    await session.flush()
    session.add(ChannelMember(channel_id=channel.id, user_id=current_user.id, is_admin=True))
    await session.flush()
    await session.refresh(channel)
    return ChannelPublic(id=channel.id, name=channel.name, description=channel.description, is_private=channel.is_private, topic=channel.topic, created_at=channel.created_at, created_by=channel.created_by, invite_code=channel.invite_code, only_admins_can_post=channel.only_admins_can_post, member_count=1)


@router.post("/{channel_id}/join")
async def join_channel(channel_id: uuid.UUID, data: ChannelJoinRequest, current_user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    """Join a channel."""
    result = await session.execute(select(Channel).where(Channel.id == channel_id))
    channel = result.scalar_one_or_none()
    if not channel:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Channel not found")

    existing = await session.execute(select(ChannelMember).where(and_(ChannelMember.channel_id == channel_id, ChannelMember.user_id == current_user.id)))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Already a member")

    if channel.is_private and (not data.invite_code or data.invite_code != channel.invite_code):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid invite code")

    session.add(ChannelMember(channel_id=channel_id, user_id=current_user.id, is_admin=False))
    await session.flush()
    return {"status": "joined", "channel_id": str(channel_id)}


@router.delete("/{channel_id}/leave")
async def leave_channel(channel_id: uuid.UUID, current_user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    """Leave a channel."""
    result = await session.execute(select(ChannelMember).where(and_(ChannelMember.channel_id == channel_id, ChannelMember.user_id == current_user.id)))
    membership = result.scalar_one_or_none()
    if not membership:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not a member")

    channel_result = await session.execute(select(Channel).where(Channel.id == channel_id))
    channel = channel_result.scalar_one_or_none()

    await session.delete(membership)
    await session.flush()

    if channel and channel.created_by == current_user.id:
        oldest = await session.execute(select(ChannelMember).where(ChannelMember.channel_id == channel_id).order_by(ChannelMember.joined_at.asc()).limit(1))
        oldest_member = oldest.scalar_one_or_none()
        if oldest_member:
            oldest_member.is_admin = True
            session.add(oldest_member)
            await session.flush()

    return {"status": "left", "channel_id": str(channel_id)}


@router.post("/{channel_id}/members/{user_id}")
async def add_member(channel_id: uuid.UUID, user_id: uuid.UUID, current_user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    """Add member (admin only)."""
    admin_check = await session.execute(select(ChannelMember).where(and_(ChannelMember.channel_id == channel_id, ChannelMember.user_id == current_user.id, ChannelMember.is_admin == True)))
    if not admin_check.scalar_one_or_none():
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only admins can add members")

    user_result = await session.execute(select(User).where(User.id == user_id))
    if not user_result.scalar_one_or_none():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    existing = await session.execute(select(ChannelMember).where(and_(ChannelMember.channel_id == channel_id, ChannelMember.user_id == user_id)))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Already a member")

    session.add(ChannelMember(channel_id=channel_id, user_id=user_id, is_admin=False))
    await session.flush()
    return {"status": "added", "user_id": str(user_id)}


@router.delete("/{channel_id}/members/{user_id}")
async def remove_member(channel_id: uuid.UUID, user_id: uuid.UUID, current_user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    """Remove member (admin only)."""
    admin_check = await session.execute(select(ChannelMember).where(and_(ChannelMember.channel_id == channel_id, ChannelMember.user_id == current_user.id, ChannelMember.is_admin == True)))
    if not admin_check.scalar_one_or_none():
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only admins can remove members")

    result = await session.execute(select(ChannelMember).where(and_(ChannelMember.channel_id == channel_id, ChannelMember.user_id == user_id)))
    member = result.scalar_one_or_none()
    if not member:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Member not found")

    await session.delete(member)
    await session.flush()
    return {"status": "removed", "user_id": str(user_id)}


@router.post("/{channel_id}/members/{user_id}/promote")
async def promote_admin(channel_id: uuid.UUID, user_id: uuid.UUID, current_user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    """Promote to admin."""
    admin_check = await session.execute(select(ChannelMember).where(and_(ChannelMember.channel_id == channel_id, ChannelMember.user_id == current_user.id, ChannelMember.is_admin == True)))
    if not admin_check.scalar_one_or_none():
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only admins can promote")

    result = await session.execute(select(ChannelMember).where(and_(ChannelMember.channel_id == channel_id, ChannelMember.user_id == user_id)))
    member = result.scalar_one_or_none()
    if not member:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Member not found")

    member.is_admin = True
    session.add(member)
    await session.flush()
    return {"status": "promoted", "user_id": str(user_id)}


@router.post("/{channel_id}/members/{user_id}/demote")
async def demote_admin(channel_id: uuid.UUID, user_id: uuid.UUID, current_user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    """Demote from admin."""
    admin_check = await session.execute(select(ChannelMember).where(and_(ChannelMember.channel_id == channel_id, ChannelMember.user_id == current_user.id, ChannelMember.is_admin == True)))
    if not admin_check.scalar_one_or_none():
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only admins can demote")

    result = await session.execute(select(ChannelMember).where(and_(ChannelMember.channel_id == channel_id, ChannelMember.user_id == user_id)))
    member = result.scalar_one_or_none()
    if not member:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Member not found")

    member.is_admin = False
    session.add(member)
    await session.flush()
    return {"status": "demoted", "user_id": str(user_id)}


@router.put("/{channel_id}/settings", response_model=ChannelPublic)
async def update_channel_settings(channel_id: uuid.UUID, data: ChannelUpdateSettings, current_user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    """Update channel settings (admin only)."""
    admin_check = await session.execute(select(ChannelMember).where(and_(ChannelMember.channel_id == channel_id, ChannelMember.user_id == current_user.id, ChannelMember.is_admin == True)))
    if not admin_check.scalar_one_or_none():
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only admins can update settings")

    result = await session.execute(select(Channel).where(Channel.id == channel_id))
    channel = result.scalar_one_or_none()
    if not channel:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Channel not found")

    update_data = data.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(channel, field, value)

    session.add(channel)
    await session.flush()
    await session.refresh(channel)

    count_result = await session.execute(select(func.count(ChannelMember.user_id)).where(ChannelMember.channel_id == channel_id))
    member_count = count_result.scalar() or 0

    return ChannelPublic(id=channel.id, name=channel.name, description=channel.description, is_private=channel.is_private, topic=channel.topic, created_at=channel.created_at, created_by=channel.created_by, invite_code=channel.invite_code, only_admins_can_post=channel.only_admins_can_post, member_count=member_count)
