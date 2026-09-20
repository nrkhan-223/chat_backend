"""
WebSocket Gateway for real-time chat.
Handles all client-to-server and server-to-client events.
Manages connections, typing indicators, and presence lifecycle.
"""

import json
import uuid
import asyncio
import secrets
import logging
from datetime import datetime
from typing import Dict, Set, Optional
from collections import defaultdict

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, and_, desc, or_

from database import get_session
from models import (
    User, Channel, ChannelMember, Message,
    Attachment, Reaction, ChatType, UserStatus
)
from auth import verify_token_for_ws
from config import settings

logger = logging.getLogger(__name__)

router = APIRouter(tags=["WebSocket"])


# ==================== CONNECTION MANAGER ====================

class ConnectionManager:
    """Manages all active WebSocket connections."""

    def __init__(self):
        self.active_connections: Dict[uuid.UUID, WebSocket] = {}
        self.chat_subscriptions: Dict[str, Set[uuid.UUID]] = defaultdict(set)
        self.typing_users: Dict[str, Dict[uuid.UUID, datetime]] = defaultdict(dict)
        self.offline_tasks: Dict[uuid.UUID, asyncio.Task] = {}

    async def connect(self, user_id: uuid.UUID, websocket: WebSocket):
        await websocket.accept()
        self.active_connections[user_id] = websocket
        if user_id in self.offline_tasks:
            self.offline_tasks[user_id].cancel()
            del self.offline_tasks[user_id]
        await self._subscribe_user_to_chats(user_id)
        logger.info(f"User {user_id} connected. Total: {len(self.active_connections)}")

    def disconnect(self, user_id: uuid.UUID):
        if user_id in self.active_connections:
            del self.active_connections[user_id]
        for chat_id in list(self.chat_subscriptions.keys()):
            self.chat_subscriptions[chat_id].discard(user_id)
        for chat_id in list(self.typing_users.keys()):
            self.typing_users[chat_id].pop(user_id, None)
        task = asyncio.create_task(
            self._schedule_offline(user_id, settings.PRESENCE_OFFLINE_DELAY_SECONDS)
        )
        self.offline_tasks[user_id] = task
        logger.info(f"User {user_id} disconnected. Total: {len(self.active_connections)}")

    async def _schedule_offline(self, user_id: uuid.UUID, delay_seconds: int):
        try:
            await asyncio.sleep(delay_seconds)
            if user_id not in self.active_connections:
                async with get_session() as session:
                    result = await session.execute(select(User).where(User.id == user_id))
                    user = result.scalar_one_or_none()
                    if user and user.status != UserStatus.OFFLINE:
                        user.status = UserStatus.OFFLINE
                        session.add(user)
                        await session.commit()
                        await self.broadcast_user_update(user_id, {
                            "id": str(user.id),
                            "status": "offline",
                        })
                        logger.info(f"User {user_id} set to offline")
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Error scheduling offline for {user_id}: {e}")

    async def _subscribe_user_to_chats(self, user_id: uuid.UUID):
        async with get_session() as session:
            result = await session.execute(
                select(ChannelMember.channel_id).where(ChannelMember.user_id == user_id)
            )
            for channel_id in result.scalars().all():
                self.chat_subscriptions[str(channel_id)].add(user_id)
            public_result = await session.execute(
                select(Channel.id).where(Channel.is_private == False)
            )
            for channel_id in public_result.scalars().all():
                self.chat_subscriptions[str(channel_id)].add(user_id)

    def subscribe_to_chat(self, user_id: uuid.UUID, chat_id: str):
        self.chat_subscriptions[chat_id].add(user_id)

    def unsubscribe_from_chat(self, user_id: uuid.UUID, chat_id: str):
        self.chat_subscriptions[chat_id].discard(user_id)

    async def send_to_user(self, user_id: uuid.UUID, data: dict):
        if user_id in self.active_connections:
            try:
                await self.active_connections[user_id].send_json(data)
            except Exception as e:
                logger.error(f"Error sending to user {user_id}: {e}")

    async def broadcast_to_chat(self, chat_id: str, data: dict, exclude_user: Optional[uuid.UUID] = None):
        subscribers = self.chat_subscriptions.get(chat_id, set())
        for user_id in subscribers:
            if user_id != exclude_user and user_id in self.active_connections:
                try:
                    await self.active_connections[user_id].send_json(data)
                except Exception as e:
                    logger.error(f"Error broadcasting to {user_id} in {chat_id}: {e}")

    async def broadcast_user_update(self, user_id: uuid.UUID, user_data: dict):
        for chat_id, subscribers in self.chat_subscriptions.items():
            if user_id in subscribers:
                await self.broadcast_to_chat(chat_id, {
                    "type": "user:updated",
                    "payload": user_data,
                })

    def update_typing(self, chat_id: str, user_id: uuid.UUID, is_typing: bool):
        if is_typing:
            self.typing_users[chat_id][user_id] = datetime.utcnow()
        else:
            self.typing_users[chat_id].pop(user_id, None)

    def get_typing_users(self, chat_id: str) -> Dict[uuid.UUID, datetime]:
        now = datetime.utcnow()
        stale = [uid for uid, ts in self.typing_users[chat_id].items() if (now - ts).total_seconds() > 5]
        for uid in stale:
            del self.typing_users[chat_id][uid]
        return dict(self.typing_users[chat_id])


manager = ConnectionManager()


# ==================== WEBSOCKET ENDPOINT ====================

@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket, token: str = Query(...)):
    """Main WebSocket endpoint for real-time chat."""
    user_id = verify_token_for_ws(token)
    if not user_id:
        await websocket.close(code=4001, reason="Invalid or expired token")
        return

    async with get_session() as session:
        result = await session.execute(select(User).where(User.id == user_id))
        user = result.scalar_one_or_none()
        if not user:
            await websocket.close(code=4001, reason="User not found")
            return

    await manager.connect(user_id, websocket)

    async with get_session() as session:
        user.status = UserStatus.ONLINE
        session.add(user)
        await session.commit()

    init_payload = await _build_init_payload(user_id)
    await websocket.send_json({"type": "init", "payload": init_payload})

    await manager.broadcast_user_update(user_id, {
        "id": str(user.id),
        "name": user.name,
        "status": "online",
        "avatar_url": user.avatar_url,
    })

    try:
        while True:
            raw_data = await websocket.receive_text()
            try:
                data = json.loads(raw_data)
            except json.JSONDecodeError:
                await websocket.send_json({"type": "error", "payload": {"detail": "Invalid JSON"}})
                continue

            event_type = data.get("type")
            payload = data.get("payload", {})
            await _handle_event(websocket, user_id, event_type, payload)

    except WebSocketDisconnect:
        manager.disconnect(user_id)
    except Exception as e:
        logger.error(f"WebSocket error for {user_id}: {e}")
        manager.disconnect(user_id)


# ==================== EVENT ROUTER ====================

async def _handle_event(websocket: WebSocket, user_id: uuid.UUID, event_type: str, payload: dict):
    """Route incoming WebSocket events to the appropriate handler."""
    handlers = {
        "message:send": _handle_message_send,
        "message:edit": _handle_message_edit,
        "message:delete": _handle_message_delete,
        "message:react": _handle_message_react,
        "message:pin": _handle_message_pin,
        "channel:create": _handle_channel_create,
        "channel:join": _handle_channel_join,
        "channel:leave": _handle_channel_leave,
        "channel:addMember": _handle_channel_add_member,
        "channel:removeMember": _handle_channel_remove_member,
        "channel:promoteAdmin": _handle_channel_promote_admin,
        "channel:demoteAdmin": _handle_channel_demote_admin,
        "channel:updateSettings": _handle_channel_update_settings,
        "typing:start": _handle_typing_start,
        "typing:stop": _handle_typing_stop,
        "status:update": _handle_status_update,
    }
    handler = handlers.get(event_type)
    if handler:
        await handler(websocket, user_id, payload)
    else:
        await websocket.send_json({"type": "error", "payload": {"detail": f"Unknown event: {event_type}"}})


# ==================== MESSAGE HANDLERS ====================

async def _handle_message_send(websocket: WebSocket, user_id: uuid.UUID, payload: dict):
    """Handle sending a new message."""
    chat_id = payload.get("chat_id")
    content = payload.get("content", "").strip()
    attachments_data = payload.get("attachments", [])
    reply_to_id = payload.get("reply_to_id")

    if not chat_id or not content:
        await websocket.send_json({"type": "channel:error", "payload": {"detail": "chat_id and content required"}})
        return

    async with get_session() as session:
        chat_type, channel = await _validate_chat_access(session, user_id, chat_id)
        if not chat_type:
            await websocket.send_json({"type": "channel:error", "payload": {"detail": "No access to this chat"}})
            return

        # Check posting restrictions
        if chat_type == ChatType.CHANNEL and channel and channel.only_admins_can_post:
            membership = await session.execute(
                select(ChannelMember).where(and_(
                    ChannelMember.channel_id == channel.id,
                    ChannelMember.user_id == user_id,
                    ChannelMember.is_admin == True,
                ))
            )
            if not membership.scalar_one_or_none():
                await websocket.send_json({"type": "channel:error", "payload": {"detail": "Only admins can post"}})
                return

        user_result = await session.execute(select(User).where(User.id == user_id))
        sender = user_result.scalar_one()

        message = Message(
            chat_id=chat_id,
            chat_type=chat_type,
            sender_id=user_id,
            content=content,
            reply_to_id=uuid.UUID(reply_to_id) if reply_to_id else None,
        )
        session.add(message)
        await session.flush()

        for att_data in attachments_data:
            attachment = Attachment(
                message_id=message.id,
                name=att_data.get("name", "file"),
                type=att_data.get("type", "file"),
                url=att_data.get("url", ""),
                size_bytes=att_data.get("size_bytes", 0),
            )
            session.add(attachment)

        await session.commit()
        await session.refresh(message)

        reply_count_result = await session.execute(
            select(func.count(Message.id)).where(Message.reply_to_id == message.id)
        )
        reply_count = reply_count_result.scalar() or 0

        message_data = {
            "id": str(message.id),
            "chat_id": message.chat_id,
            "chat_type": message.chat_type.value,
            "sender_id": str(message.sender_id),
            "sender_name": sender.name,
            "sender_avatar": sender.avatar_url,
            "content": message.content,
            "timestamp": message.timestamp.isoformat(),
            "edited_at": None,
            "is_pinned": message.is_pinned,
            "reply_to_id": str(message.reply_to_id) if message.reply_to_id else None,
            "reply_to_content": None,
            "reply_to_sender_name": None,
            "attachments": [
                {"id": str(a.id), "name": a.name, "type": a.type.value if hasattr(a.type, 'value') else a.type, "url": a.url, "size_bytes": a.size_bytes}
                for a in (message.attachments or [])
            ],
            "reactions": [],
            "reply_count": reply_count,
        }

        if message.reply_to_id:
            parent_result = await session.execute(select(Message).where(Message.id == message.reply_to_id))
            parent = parent_result.scalar_one_or_none()
            if parent:
                ps = await session.execute(select(User).where(User.id == parent.sender_id))
                parent_sender = ps.scalar_one_or_none()
                message_data["reply_to_content"] = parent.content[:100]
                message_data["reply_to_sender_name"] = parent_sender.name if parent_sender else "Unknown"

    manager.update_typing(chat_id, user_id, False)
    await manager.broadcast_to_chat(chat_id, {"type": "message:new", "payload": message_data})


async def _handle_message_edit(websocket: WebSocket, user_id: uuid.UUID, payload: dict):
    """Handle editing a message."""
    message_id = payload.get("message_id")
    content = payload.get("content", "").strip()
    if not message_id or not content:
        return

    async with get_session() as session:
        result = await session.execute(select(Message).where(Message.id == uuid.UUID(message_id)))
        message = result.scalar_one_or_none()
        if not message:
            await websocket.send_json({"type": "channel:error", "payload": {"detail": "Message not found"}})
            return
        if message.sender_id != user_id:
            await websocket.send_json({"type": "channel:error", "payload": {"detail": "Can only edit own messages"}})
            return
        message.content = content
        message.edited_at = datetime.utcnow()
        session.add(message)
        await session.commit()

    await manager.broadcast_to_chat(message.chat_id, {
        "type": "message:updated",
        "payload": {"id": str(message.id), "chat_id": message.chat_id, "content": content, "edited_at": message.edited_at.isoformat()},
    })


async def _handle_message_delete(websocket: WebSocket, user_id: uuid.UUID, payload: dict):
    """Handle deleting a message."""
    message_id = payload.get("message_id")
    if not message_id:
        return

    async with get_session() as session:
        result = await session.execute(select(Message).where(Message.id == uuid.UUID(message_id)))
        message = result.scalar_one_or_none()
        if not message:
            return
        if message.sender_id != user_id:
            await websocket.send_json({"type": "channel:error", "payload": {"detail": "Can only delete own messages"}})
            return
        chat_id = message.chat_id
        await session.delete(message)
        await session.commit()

    await manager.broadcast_to_chat(chat_id, {"type": "message:deleted", "payload": {"id": message_id, "chat_id": chat_id}})


async def _handle_message_react(websocket: WebSocket, user_id: uuid.UUID, payload: dict):
    """Handle adding/removing a reaction."""
    message_id = payload.get("message_id")
    emoji = payload.get("emoji", "")
    if not message_id or not emoji:
        return

    async with get_session() as session:
        msg_uuid = uuid.UUID(message_id)
        existing = await session.execute(
            select(Reaction).where(and_(Reaction.message_id == msg_uuid, Reaction.user_id == user_id, Reaction.emoji == emoji))
        )
        existing_reaction = existing.scalar_one_or_none()

        if existing_reaction:
            await session.delete(existing_reaction)
            action = "removed"
        else:
            session.add(Reaction(message_id=msg_uuid, user_id=user_id, emoji=emoji))
            action = "added"

        await session.commit()

        reactions_result = await session.execute(select(Reaction).where(Reaction.message_id == msg_uuid))
        all_reactions = reactions_result.scalars().all()
        reaction_map = {}
        for r in all_reactions:
            if r.emoji not in reaction_map:
                reaction_map[r.emoji] = {"emoji": r.emoji, "count": 0, "user_ids": []}
            reaction_map[r.emoji]["count"] += 1
            reaction_map[r.emoji]["user_ids"].append(str(r.user_id))

        msg_result = await session.execute(select(Message).where(Message.id == msg_uuid))
        message = msg_result.scalar_one_or_none()
        chat_id = message.chat_id if message else ""

    await manager.broadcast_to_chat(chat_id, {
        "type": "message:reaction",
        "payload": {"message_id": message_id, "emoji": emoji, "action": action, "user_id": str(user_id), "reactions": list(reaction_map.values())},
    })


async def _handle_message_pin(websocket: WebSocket, user_id: uuid.UUID, payload: dict):
    """Handle pinning/unpinning a message."""
    message_id = payload.get("message_id")
    if not message_id:
        return

    async with get_session() as session:
        result = await session.execute(select(Message).where(Message.id == uuid.UUID(message_id)))
        message = result.scalar_one_or_none()
        if not message:
            return

        if message.chat_type == ChatType.CHANNEL:
            admin_check = await session.execute(
                select(ChannelMember).where(and_(
                    ChannelMember.channel_id == uuid.UUID(message.chat_id),
                    ChannelMember.user_id == user_id,
                    ChannelMember.is_admin == True,
                ))
            )
            if not admin_check.scalar_one_or_none():
                await websocket.send_json({"type": "channel:error", "payload": {"detail": "Only admins can pin"}})
                return

        message.is_pinned = not message.is_pinned
        session.add(message)
        await session.commit()
        chat_id = message.chat_id

    await manager.broadcast_to_chat(chat_id, {
        "type": "message:pinned",
        "payload": {"message_id": message_id, "is_pinned": message.is_pinned, "chat_id": chat_id},
    })


# ==================== CHANNEL HANDLERS ====================

async def _handle_channel_create(websocket: WebSocket, user_id: uuid.UUID, payload: dict):
    """Handle creating a new channel."""
    name = payload.get("name", "").strip()
    if not name:
        await websocket.send_json({"type": "channel:error", "payload": {"detail": "Channel name required"}})
        return

    async with get_session() as session:
        is_private = payload.get("is_private", False)
        invite_code = secrets.token_urlsafe(12) if is_private else None

        channel = Channel(
            name=name,
            description=payload.get("description"),
            is_private=is_private,
            topic=payload.get("topic"),
            created_by=user_id,
            invite_code=invite_code,
        )
        session.add(channel)
        await session.flush()
        session.add(ChannelMember(channel_id=channel.id, user_id=user_id, is_admin=True))
        await session.commit()
        await session.refresh(channel)

        manager.subscribe_to_chat(user_id, str(channel.id))
        channel_data = {
            "id": str(channel.id), "name": channel.name, "description": channel.description,
            "is_private": channel.is_private, "topic": channel.topic,
            "created_at": channel.created_at.isoformat(), "created_by": str(channel.created_by),
            "invite_code": channel.invite_code, "only_admins_can_post": channel.only_admins_can_post,
            "member_count": 1,
        }

    if not is_private:
        for uid in manager.active_connections:
            await manager.send_to_user(uid, {"type": "channel:created", "payload": channel_data})
    else:
        await manager.send_to_user(user_id, {"type": "channel:created", "payload": channel_data})


async def _handle_channel_join(websocket: WebSocket, user_id: uuid.UUID, payload: dict):
    """Handle joining a channel."""
    channel_id = payload.get("channel_id")
    invite_code = payload.get("invite_code")
    if not channel_id:
        return

    async with get_session() as session:
        ch_uuid = uuid.UUID(channel_id)
        result = await session.execute(select(Channel).where(Channel.id == ch_uuid))
        channel = result.scalar_one_or_none()
        if not channel:
            await websocket.send_json({"type": "channel:error", "payload": {"detail": "Channel not found"}})
            return

        existing = await session.execute(
            select(ChannelMember).where(and_(ChannelMember.channel_id == ch_uuid, ChannelMember.user_id == user_id))
        )
        if existing.scalar_one_or_none():
            await websocket.send_json({"type": "channel:error", "payload": {"detail": "Already a member"}})
            return

        if channel.is_private:
            if not invite_code or invite_code != channel.invite_code:
                await websocket.send_json({"type": "channel:error", "payload": {"detail": "Invalid invite code"}})
                return

        session.add(ChannelMember(channel_id=ch_uuid, user_id=user_id, is_admin=False))
        await session.commit()
        manager.subscribe_to_chat(user_id, channel_id)

        user_result = await session.execute(select(User).where(User.id == user_id))
        user = user_result.scalar_one()

    await manager.broadcast_to_chat(channel_id, {
        "type": "user:joined",
        "payload": {"chat_id": channel_id, "user_id": str(user_id), "user_name": user.name},
    })
    await manager.send_to_user(user_id, {"type": "channel:updated", "payload": {"id": channel_id, "action": "joined"}})


async def _handle_channel_leave(websocket: WebSocket, user_id: uuid.UUID, payload: dict):
    """Handle leaving a channel."""
    channel_id = payload.get("channel_id")
    if not channel_id:
        return

    async with get_session() as session:
        ch_uuid = uuid.UUID(channel_id)
        result = await session.execute(
            select(ChannelMember).where(and_(ChannelMember.channel_id == ch_uuid, ChannelMember.user_id == user_id))
        )
        member = result.scalar_one_or_none()
        if not member:
            return

        await session.delete(member)
        channel_result = await session.execute(select(Channel).where(Channel.id == ch_uuid))
        channel = channel_result.scalar_one_or_none()
        new_admin_id = None
        if channel and channel.created_by == user_id:
            oldest = await session.execute(
                select(ChannelMember).where(ChannelMember.channel_id == ch_uuid).order_by(ChannelMember.joined_at.asc()).limit(1)
            )
            oldest_member = oldest.scalar_one_or_none()
            if oldest_member:
                oldest_member.is_admin = True
                session.add(oldest_member)
                new_admin_id = str(oldest_member.user_id)

        await session.commit()
        manager.unsubscribe_from_chat(user_id, channel_id)

        user_result = await session.execute(select(User).where(User.id == user_id))
        user = user_result.scalar_one()

    await manager.broadcast_to_chat(channel_id, {
        "type": "user:left",
        "payload": {"chat_id": channel_id, "user_id": str(user_id), "user_name": user.name},
    })
    if new_admin_id:
        await manager.broadcast_to_chat(channel_id, {
            "type": "channel:updated",
            "payload": {"id": channel_id, "action": "admin_promoted", "new_admin_id": new_admin_id},
        })


async def _handle_channel_add_member(websocket: WebSocket, user_id: uuid.UUID, payload: dict):
    """Handle adding a member to a channel (admin only)."""
    channel_id = payload.get("channel_id")
    target_user_id = payload.get("user_id")
    if not channel_id or not target_user_id:
        return

    async with get_session() as session:
        ch_uuid = uuid.UUID(channel_id)
        target_uuid = uuid.UUID(target_user_id)

        admin_check = await session.execute(
            select(ChannelMember).where(and_(ChannelMember.channel_id == ch_uuid, ChannelMember.user_id == user_id, ChannelMember.is_admin == True))
        )
        if not admin_check.scalar_one_or_none():
            await websocket.send_json({"type": "channel:error", "payload": {"detail": "Only admins can add members"}})
            return

        user_result = await session.execute(select(User).where(User.id == target_uuid))
        target_user = user_result.scalar_one_or_none()
        if not target_user:
            await websocket.send_json({"type": "channel:error", "payload": {"detail": "User not found"}})
            return

        existing = await session.execute(
            select(ChannelMember).where(and_(ChannelMember.channel_id == ch_uuid, ChannelMember.user_id == target_uuid))
        )
        if existing.scalar_one_or_none():
            await websocket.send_json({"type": "channel:error", "payload": {"detail": "Already a member"}})
            return

        session.add(ChannelMember(channel_id=ch_uuid, user_id=target_uuid, is_admin=False))
        await session.commit()
        manager.subscribe_to_chat(target_uuid, channel_id)

    await manager.send_to_user(target_uuid, {"type": "channel:updated", "payload": {"id": channel_id, "action": "added"}})
    await manager.broadcast_to_chat(channel_id, {
        "type": "user:joined",
        "payload": {"chat_id": channel_id, "user_id": target_user_id, "user_name": target_user.name},
    })


async def _handle_channel_remove_member(websocket: WebSocket, user_id: uuid.UUID, payload: dict):
    """Handle removing a member from a channel (admin only)."""
    channel_id = payload.get("channel_id")
    target_user_id = payload.get("user_id")
    if not channel_id or not target_user_id:
        return

    async with get_session() as session:
        ch_uuid = uuid.UUID(channel_id)
        target_uuid = uuid.UUID(target_user_id)

        admin_check = await session.execute(
            select(ChannelMember).where(and_(ChannelMember.channel_id == ch_uuid, ChannelMember.user_id == user_id, ChannelMember.is_admin == True))
        )
        if not admin_check.scalar_one_or_none():
            await websocket.send_json({"type": "channel:error", "payload": {"detail": "Only admins can remove members"}})
            return

        result = await session.execute(
            select(ChannelMember).where(and_(ChannelMember.channel_id == ch_uuid, ChannelMember.user_id == target_uuid))
        )
        member = result.scalar_one_or_none()
        if not member:
            return

        await session.delete(member)
        await session.commit()
        manager.unsubscribe_from_chat(target_uuid, channel_id)

        user_result = await session.execute(select(User).where(User.id == target_uuid))
        target_user = user_result.scalar_one_or_none()

    await manager.send_to_user(target_uuid, {"type": "channel:updated", "payload": {"id": channel_id, "action": "removed"}})
    await manager.broadcast_to_chat(channel_id, {
        "type": "user:left",
        "payload": {"chat_id": channel_id, "user_id": target_user_id, "user_name": target_user.name if target_user else "Unknown"},
    })


async def _handle_channel_promote_admin(websocket: WebSocket, user_id: uuid.UUID, payload: dict):
    """Handle promoting a member to admin."""
    channel_id = payload.get("channel_id")
    target_user_id = payload.get("user_id")
    if not channel_id or not target_user_id:
        return

    async with get_session() as session:
        ch_uuid = uuid.UUID(channel_id)
        target_uuid = uuid.UUID(target_user_id)

        admin_check = await session.execute(
            select(ChannelMember).where(and_(ChannelMember.channel_id == ch_uuid, ChannelMember.user_id == user_id, ChannelMember.is_admin == True))
        )
        if not admin_check.scalar_one_or_none():
            await websocket.send_json({"type": "channel:error", "payload": {"detail": "Only admins can promote"}})
            return

        result = await session.execute(
            select(ChannelMember).where(and_(ChannelMember.channel_id == ch_uuid, ChannelMember.user_id == target_uuid))
        )
        member = result.scalar_one_or_none()
        if not member:
            await websocket.send_json({"type": "channel:error", "payload": {"detail": "Member not found"}})
            return

        member.is_admin = True
        session.add(member)
        await session.commit()

    await manager.broadcast_to_chat(channel_id, {
        "type": "channel:updated",
        "payload": {"id": channel_id, "action": "admin_promoted", "user_id": target_user_id},
    })


async def _handle_channel_demote_admin(websocket: WebSocket, user_id: uuid.UUID, payload: dict):
    """Handle demoting an admin to regular member."""
    channel_id = payload.get("channel_id")
    target_user_id = payload.get("user_id")
    if not channel_id or not target_user_id:
        return

    async with get_session() as session:
        ch_uuid = uuid.UUID(channel_id)
        target_uuid = uuid.UUID(target_user_id)

        admin_check = await session.execute(
            select(ChannelMember).where(and_(ChannelMember.channel_id == ch_uuid, ChannelMember.user_id == user_id, ChannelMember.is_admin == True))
        )
        if not admin_check.scalar_one_or_none():
            await websocket.send_json({"type": "channel:error", "payload": {"detail": "Only admins can demote"}})
            return

        result = await session.execute(
            select(ChannelMember).where(and_(ChannelMember.channel_id == ch_uuid, ChannelMember.user_id == target_uuid))
        )
        member = result.scalar_one_or_none()
        if not member:
            await websocket.send_json({"type": "channel:error", "payload": {"detail": "Member not found"}})
            return

        member.is_admin = False
        session.add(member)
        await session.commit()

    await manager.broadcast_to_chat(channel_id, {
        "type": "channel:updated",
        "payload": {"id": channel_id, "action": "admin_demoted", "user_id": target_user_id},
    })


async def _handle_channel_update_settings(websocket: WebSocket, user_id: uuid.UUID, payload: dict):
    """Handle updating channel settings (admin only)."""
    channel_id = payload.get("channel_id")
    if not channel_id:
        return

    async with get_session() as session:
        ch_uuid = uuid.UUID(channel_id)
        admin_check = await session.execute(
            select(ChannelMember).where(and_(ChannelMember.channel_id == ch_uuid, ChannelMember.user_id == user_id, ChannelMember.is_admin == True))
        )
        if not admin_check.scalar_one_or_none():
            await websocket.send_json({"type": "channel:error", "payload": {"detail": "Only admins can update settings"}})
            return

        result = await session.execute(select(Channel).where(Channel.id == ch_uuid))
        channel = result.scalar_one_or_none()
        if not channel:
            return

        if "topic" in payload:
            channel.topic = payload["topic"]
        if "description" in payload:
            channel.description = payload["description"]
        if "only_admins_can_post" in payload:
            channel.only_admins_can_post = payload["only_admins_can_post"]

        session.add(channel)
        await session.commit()

    await manager.broadcast_to_chat(channel_id, {
        "type": "channel:updated",
        "payload": {
            "id": channel_id, "action": "settings_updated",
            "topic": channel.topic, "description": channel.description,
            "only_admins_can_post": channel.only_admins_can_post,
        },
    })


# ==================== TYPING & PRESENCE ====================

async def _handle_typing_start(websocket: WebSocket, user_id: uuid.UUID, payload: dict):
    """Handle typing start indicator."""
    chat_id = payload.get("chat_id")
    if not chat_id:
        return
    manager.update_typing(chat_id, user_id, True)
    typing = manager.get_typing_users(chat_id)
    user_ids = [uid for uid in typing.keys() if uid != user_id]
    user_names = []
    async with get_session() as session:
        for uid in user_ids:
            result = await session.execute(select(User.name).where(User.id == uid))
            name = result.scalar_one_or_none()
            if name:
                user_names.append(name)
    if user_names:
        await manager.broadcast_to_chat(chat_id, {
            "type": "typing:update",
            "payload": {"chat_id": chat_id, "user_ids": [str(uid) for uid in user_ids], "user_names": user_names},
        }, exclude_user=user_id)


async def _handle_typing_stop(websocket: WebSocket, user_id: uuid.UUID, payload: dict):
    """Handle typing stop indicator."""
    chat_id = payload.get("chat_id")
    if not chat_id:
        return
    manager.update_typing(chat_id, user_id, False)
    typing = manager.get_typing_users(chat_id)
    user_ids = [uid for uid in typing.keys() if uid != user_id]
    user_names = []
    async with get_session() as session:
        for uid in user_ids:
            result = await session.execute(select(User.name).where(User.id == uid))
            name = result.scalar_one_or_none()
            if name:
                user_names.append(name)
    await manager.broadcast_to_chat(chat_id, {
        "type": "typing:update",
        "payload": {"chat_id": chat_id, "user_ids": [str(uid) for uid in user_ids], "user_names": user_names},
    }, exclude_user=user_id)


async def _handle_status_update(websocket: WebSocket, user_id: uuid.UUID, payload: dict):
    """Handle user status update."""
    new_status = payload.get("status")
    if not new_status or new_status not in ["online", "busy", "away"]:
        return
    async with get_session() as session:
        result = await session.execute(select(User).where(User.id == user_id))
        user = result.scalar_one_or_none()
        if not user:
            return
        user.status = UserStatus(new_status)
        session.add(user)
        await session.commit()
    await manager.broadcast_user_update(user_id, {"id": str(user_id), "status": new_status})


# ==================== HELPERS ====================

async def _validate_chat_access(session: AsyncSession, user_id: uuid.UUID, chat_id: str):
    """Validate that a user has access to a chat."""
    if chat_id.startswith("dm:"):
        parts = chat_id.split(":")
        if len(parts) == 3 and str(user_id) in [parts[1], parts[2]]:
            return ChatType.DM, None
        return None, None
    else:
        try:
            ch_uuid = uuid.UUID(chat_id)
        except ValueError:
            return None, None
        membership = await session.execute(
            select(ChannelMember).where(and_(ChannelMember.channel_id == ch_uuid, ChannelMember.user_id == user_id))
        )
        if membership.scalar_one_or_none():
            channel_result = await session.execute(select(Channel).where(Channel.id == ch_uuid))
            return ChatType.CHANNEL, channel_result.scalar_one_or_none()
        channel_result = await session.execute(select(Channel).where(Channel.id == ch_uuid))
        channel = channel_result.scalar_one_or_none()
        if channel and not channel.is_private:
            return ChatType.CHANNEL, channel
        return None, None


async def _build_init_payload(user_id: uuid.UUID) -> dict:
    """Build the initial payload sent on WebSocket connection."""
    async with get_session() as session:
        users_result = await session.execute(select(User))
        all_users = users_result.scalars().all()
        users_data = [
            {
                "id": str(u.id), "name": u.name, "email": u.email, "avatar_url": u.avatar_url,
                "status": u.status.value if hasattr(u.status, 'value') else u.status,
                "custom_status": u.custom_status, "bio": u.bio, "phone": u.phone, "is_bot": u.is_bot,
            }
            for u in all_users
        ]

        member_channels = await session.execute(
            select(ChannelMember.channel_id).where(ChannelMember.user_id == user_id)
        )
        member_ids = {row[0] for row in member_channels.all()}

        channels_result = await session.execute(select(Channel))
        channels = channels_result.scalars().all()
        channels_data = []
        for ch in channels:
            if not ch.is_private or ch.id in member_ids:
                count_result = await session.execute(
                    select(func.count(ChannelMember.user_id)).where(ChannelMember.channel_id == ch.id)
                )
                member_count = count_result.scalar() or 0
                channels_data.append({
                    "id": str(ch.id), "name": ch.name, "description": ch.description,
                    "is_private": ch.is_private, "topic": ch.topic,
                    "created_at": ch.created_at.isoformat(), "created_by": str(ch.created_by),
                    "invite_code": ch.invite_code if ch.id in member_ids else None,
                    "only_admins_can_post": ch.only_admins_can_post, "member_count": member_count,
                })

        all_chat_ids = [ch["id"] for ch in channels_data]
        messages_data = []
        pinned_ids = []

        for chat_id in all_chat_ids[:20]:
            msgs_result = await session.execute(
                select(Message).where(Message.chat_id == chat_id).order_by(desc(Message.timestamp)).limit(50)
            )
            msgs = msgs_result.scalars().all()
            for msg in reversed(msgs):
                sender_result = await session.execute(select(User).where(User.id == msg.sender_id))
                sender = sender_result.scalar_one_or_none()
                reply_count_result = await session.execute(
                    select(func.count(Message.id)).where(Message.reply_to_id == msg.id)
                )
                reply_count = reply_count_result.scalar() or 0
                msg_data = {
                    "id": str(msg.id), "chat_id": msg.chat_id,
                    "chat_type": msg.chat_type.value if hasattr(msg.chat_type, 'value') else msg.chat_type,
                    "sender_id": str(msg.sender_id), "sender_name": sender.name if sender else "Unknown",
                    "sender_avatar": sender.avatar_url if sender else None,
                    "content": msg.content, "timestamp": msg.timestamp.isoformat(),
                    "edited_at": msg.edited_at.isoformat() if msg.edited_at else None,
                    "is_pinned": msg.is_pinned,
                    "reply_to_id": str(msg.reply_to_id) if msg.reply_to_id else None,
                    "reply_to_content": None, "reply_to_sender_name": None,
                    "attachments": [], "reactions": [], "reply_count": reply_count,
                }
                if msg.is_pinned:
                    pinned_ids.append(str(msg.id))
                messages_data.append(msg_data)

        return {"users": users_data, "channels": channels_data, "messages": messages_data, "pinnedIds": pinned_ids}
