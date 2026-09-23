import uuid
import enum
from datetime import datetime, timezone
from typing import Optional, List
from sqlmodel import SQLModel, Field, Relationship, Column
from sqlalchemy import Text, Integer, DateTime, Enum as SAEnum


# ==================== ENUMS ====================

class UserStatus(str, enum.Enum):
    ONLINE = "online"
    BUSY = "busy"
    AWAY = "away"
    OFFLINE = "offline"


class ChatType(str, enum.Enum):
    CHANNEL = "channel"
    DM = "dm"


class AttachmentType(str, enum.Enum):
    IMAGE = "image"
    FILE = "file"


# ==================== MODELS ====================

class User(SQLModel, table=True):
    """User table - represents all users in the workspace."""
    __tablename__ = "users"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    name: str = Field(max_length=100, index=True)
    email: str = Field(max_length=255, unique=True, index=True)
    password_hash: str = Field(max_length=255)
    avatar_url: Optional[str] = Field(default=None, max_length=500)
    status: UserStatus = Field(
        default=UserStatus.OFFLINE,
        sa_column=Column(SAEnum(UserStatus), default=UserStatus.OFFLINE)
    )
    custom_status: Optional[str] = Field(default=None, max_length=200)
    bio: Optional[str] = Field(default=None, sa_column=Column(Text))
    phone: Optional[str] = Field(default=None, max_length=20)
    is_bot: bool = Field(default=False)
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        sa_column=Column(DateTime(timezone=True), index=True)
    )

    # Relationships
    channel_memberships: List["ChannelMember"] = Relationship(back_populates="user")
    sent_messages: List["Message"] = Relationship(back_populates="sender")
    reactions: List["Reaction"] = Relationship(back_populates="user")


class Channel(SQLModel, table=True):
    """Channel table - represents group chat channels."""
    __tablename__ = "channels"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    name: str = Field(max_length=100, index=True)
    description: Optional[str] = Field(default=None, sa_column=Column(Text))
    is_private: bool = Field(default=False)
    topic: Optional[str] = Field(default=None, max_length=200)
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        sa_column=Column(DateTime(timezone=True), index=True)
    )
    created_by: uuid.UUID = Field(foreign_key="users.id", index=True)
    invite_code: Optional[str] = Field(default=None, unique=True, max_length=50)
    only_admins_can_post: bool = Field(default=False)

    # Relationships
    members: List["ChannelMember"] = Relationship(back_populates="channel")
    creator: Optional[User] = Relationship()


class ChannelMember(SQLModel, table=True):
    """ChannelMember table - many-to-many join between User and Channel."""
    __tablename__ = "channel_members"

    channel_id: uuid.UUID = Field(foreign_key="channels.id", primary_key=True)
    user_id: uuid.UUID = Field(foreign_key="users.id", primary_key=True)
    is_admin: bool = Field(default=False)
    joined_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        sa_column=Column(DateTime(timezone=True), index=True)
    )

    # Relationships
    channel: Optional[Channel] = Relationship(back_populates="members")
    user: Optional[User] = Relationship(back_populates="channel_memberships")


class Message(SQLModel, table=True):
    """Message table - stores all messages (channel and DM)."""
    __tablename__ = "messages"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    chat_id: str = Field(max_length=255, index=True)
    chat_type: ChatType = Field(
        sa_column=Column(SAEnum(ChatType), index=True)
    )
    sender_id: uuid.UUID = Field(foreign_key="users.id", index=True)
    content: str = Field(sa_column=Column(Text))
    timestamp: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        sa_column=Column(DateTime(timezone=True), index=True)
    )
    edited_at: Optional[datetime] = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    is_pinned: bool = Field(default=False)
    reply_to_id: Optional[uuid.UUID] = Field(default=None, foreign_key="messages.id")

    # Relationships
    sender: Optional[User] = Relationship(back_populates="sent_messages")
    attachments: List["Attachment"] = Relationship(back_populates="message")
    reactions: List["Reaction"] = Relationship(back_populates="message")
    reply_to: Optional["Message"] = Relationship(
        back_populates="replies",
        sa_relationship_kwargs={"remote_side": "Message.id"}
    )
    replies: List["Message"] = Relationship(
        back_populates="reply_to"
    )


class Attachment(SQLModel, table=True):
    """Attachment table - files/images attached to messages."""
    __tablename__ = "attachments"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    message_id: uuid.UUID = Field(foreign_key="messages.id", index=True)
    name: str = Field(max_length=255)
    type: AttachmentType = Field(
        sa_column=Column(SAEnum(AttachmentType))
    )
    url: str = Field(max_length=1000)
    size_bytes: int = Field(sa_column=Column(Integer))

    # Relationships
    message: Optional[Message] = Relationship(back_populates="attachments")


class Reaction(SQLModel, table=True):
    """Reaction table - emoji reactions on messages."""
    __tablename__ = "reactions"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    message_id: uuid.UUID = Field(foreign_key="messages.id", index=True)
    user_id: uuid.UUID = Field(foreign_key="users.id", index=True)
    emoji: str = Field(max_length=50)

    # Relationships
    message: Optional[Message] = Relationship(back_populates="reactions")
    user: Optional[User] = Relationship(back_populates="reactions")