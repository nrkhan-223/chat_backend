"""
Redis Pub/Sub module for multi-worker WebSocket support.
When deploying with multiple Uvicorn workers, each worker has its own
in-memory ConnectionManager. Redis Pub/Sub ensures events are broadcast
across all workers.
"""

import json
import asyncio
import logging
from typing import Optional, Callable

import redis.asyncio as aioredis

from config import settings

logger = logging.getLogger(__name__)


class RedisPubSub:
    """Redis Pub/Sub manager for cross-worker event broadcasting."""

    def __init__(self):
        self.redis: Optional[aioredis.Redis] = None
        self.pubsub: Optional[aioredis.client.PubSub] = None
        self._listeners: dict[str, list[Callable]] = {}
        self._listen_task: Optional[asyncio.Task] = None
        self._connected = False

    async def connect(self):
        """Connect to Redis."""
        try:
            self.redis = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
            self.pubsub = self.redis.pubsub()
            self._connected = True
            self._listen_task = asyncio.create_task(self._listen())
            logger.info("Connected to Redis Pub/Sub")
        except Exception as e:
            logger.warning(f"Redis connection failed: {e}. Running in single-worker mode.")
            self._connected = False

    async def disconnect(self):
        """Disconnect from Redis."""
        if self._listen_task:
            self._listen_task.cancel()
        if self.pubsub:
            await self.pubsub.unsubscribe()
            await self.pubsub.close()
        if self.redis:
            await self.redis.close()
        self._connected = False

    async def subscribe(self, channel: str, callback: Callable):
        """Subscribe to a Redis channel with a callback."""
        if not self._connected:
            return
        if channel not in self._listeners:
            self._listeners[channel] = []
            await self.pubsub.subscribe(channel)
        self._listeners[channel].append(callback)

    async def unsubscribe(self, channel: str, callback: Optional[Callable] = None):
        """Unsubscribe from a Redis channel."""
        if not self._connected:
            return
        if callback:
            if channel in self._listeners:
                self._listeners[channel] = [cb for cb in self._listeners[channel] if cb != callback]
                if not self._listeners[channel]:
                    del self._listeners[channel]
                    await self.pubsub.unsubscribe(channel)
        else:
            if channel in self._listeners:
                del self._listeners[channel]
                await self.pubsub.unsubscribe(channel)

    async def publish(self, channel: str, data: dict):
        """Publish a message to a Redis channel."""
        if not self._connected:
            return
        try:
            await self.redis.publish(channel, json.dumps(data))
        except Exception as e:
            logger.error(f"Error publishing to Redis channel {channel}: {e}")

    async def _listen(self):
        """Listen for messages on subscribed channels."""
        try:
            async for message in self.pubsub.listen():
                if message["type"] == "message":
                    channel = message["channel"]
                    if isinstance(channel, bytes):
                        channel = channel.decode()
                    try:
                        data = json.loads(message["data"])
                    except (json.JSONDecodeError, TypeError):
                        continue
                    callbacks = self._listeners.get(channel, [])
                    for callback in callbacks:
                        try:
                            if asyncio.iscoroutinefunction(callback):
                                await callback(data)
                            else:
                                callback(data)
                        except Exception as e:
                            logger.error(f"Error in Redis callback for {channel}: {e}")
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Redis listener error: {e}")

    @property
    def is_connected(self) -> bool:
        return self._connected


# Global instance
redis_pubsub = RedisPubSub()
