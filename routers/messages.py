import uuid
from datetime import datetime
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, and_, desc

from database import get_session
from models import User, Message, Channel, ChannelMember, ChatType
from schemas import MessagePublic, PaginatedMessages, AttachmentPublic, ReactionPublic
from auth import get_current_user

router = APIRouter(prefix="/messages", tags=["Messages"])


async def _serialize_message(session: AsyncSession, message: Message, reply_count: int = 0) -> MessagePublic:
    """Serialize a Message into MessagePublic schema."""
    sender = message.sender
    sender_name = sender.name if sender else "Unknown"
    sender_avatar = sender.avatar_url if sender else None

    reply_to_content = None
    reply_to_sender_name = None
    if message.reply_to and message.reply_to.sender:
        reply_to_content = message.reply_to.content[:100]
        reply_to_sender_name = message.reply_to.sender.name

    attachments = [AttachmentPublic.model_validate(att) for att in (message.attachments or [])]

    reaction_map = {}
    for reaction in (message.reactions or []):
        emoji = reaction.emoji
        if emoji not in reaction_map:
            reaction_map[emoji] = {"emoji": emoji, "count": 0, "user_ids": []}
        reaction_map[emoji]["count"] += 1
        reaction_map[emoji]["user_ids"].append(reaction.user_id)

    reactions = [ReactionPublic(**v) for v in reaction_map.values()]

    return MessagePublic(
        id=message.id, chat_id=message.chat_id, chat_type=message.chat_type.value,
        sender_id=message.sender_id, sender_name=sender_name, sender_avatar=sender_avatar,
        content=message.content, timestamp=message.timestamp, edited_at=message.edited_at,
        is_pinned=message.is_pinned, reply_to_id=message.reply_to_id,
        reply_to_content=reply_to_content, reply_to_sender_name=reply_to_sender_name,
        attachments=attachments, reactions=reactions, reply_count=reply_count,
    )


@router.get("", response_model=PaginatedMessages)
async def get_messages(
    chat_id: str = Query(...),
    before: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=200),
    search: Optional[str] = Query(None),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Fetch paginated message history with cursor-based pagination and search."""
    # Verify access
    if chat_id.startswith("dm:"):
        parts = chat_id.split(":")
        if len(parts) == 3 and str(current_user.id) not in [parts[1], parts[2]]:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not part of this DM")
    else:
        channel_uuid = uuid.UUID(chat_id)
        membership = await session.execute(
            select(ChannelMember).where(and_(ChannelMember.channel_id == channel_uuid, ChannelMember.user_id == current_user.id))
        )
        if not membership.scalar_one_or_none():
            channel_result = await session.execute(select(Channel).where(Channel.id == channel_uuid))
            channel = channel_result.scalar_one_or_none()
            if not channel or channel.is_private:
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="No access to this channel")

    query = select(Message).where(Message.chat_id == chat_id)

    if before:
        try:
            cursor_time = datetime.fromisoformat(before)
            query = query.where(Message.timestamp < cursor_time)
        except ValueError:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid cursor format")

    if search:
        query = query.where(Message.content.ilike(f"%{search}%"))

    query = query.order_by(desc(Message.timestamp)).limit(limit + 1)
    result = await session.execute(query)
    messages = result.scalars().all()

    has_more = len(messages) > limit
    if has_more:
        messages = messages[:limit]

    message_ids = [m.id for m in messages]
    reply_counts = {}
    if message_ids:
        rc_result = await session.execute(
            select(Message.reply_to_id, func.count(Message.id)).where(Message.reply_to_id.in_(message_ids)).group_by(Message.reply_to_id)
        )
        reply_counts = {row[0]: row[1] for row in rc_result.all()}

    serialized = []
    for msg in messages:
        count = reply_counts.get(msg.id, 0)
        serialized.append(await _serialize_message(session, msg, count))

    serialized.reverse()
    next_cursor = messages[-1].timestamp.isoformat() if has_more and messages else None

    return PaginatedMessages(messages=serialized, has_more=has_more, next_cursor=next_cursor)
