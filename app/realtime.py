"""WebSocket channels (spec §9).

Frames:  { "channel": "...", "event": "...", "data": { ... } }
Client actions: {"action": "subscribe"|"unsubscribe", "channel": "..."}

This manager is in-process — correct for a single API instance.  For
horizontal scale, back `emit()` with Redis pub/sub so every instance
sees every event (interface stays identical).
"""
import asyncio
import logging
import uuid
from collections import defaultdict

from fastapi import WebSocket

log = logging.getLogger("sportyqo.ws")


class ConnectionManager:
    def __init__(self) -> None:
        self._channels: dict[str, set[WebSocket]] = defaultdict(set)
        self._lock = asyncio.Lock()

    async def subscribe(self, channel: str, ws: WebSocket) -> None:
        async with self._lock:
            self._channels[channel].add(ws)

    async def unsubscribe(self, channel: str, ws: WebSocket) -> None:
        async with self._lock:
            self._channels[channel].discard(ws)

    async def drop(self, ws: WebSocket) -> None:
        async with self._lock:
            for subscribers in self._channels.values():
                subscribers.discard(ws)

    async def emit(self, channel: str, event: str, data: dict) -> None:
        frame = {"channel": channel, "event": event, "data": data}
        dead: list[WebSocket] = []
        for ws in list(self._channels.get(channel, ())):
            try:
                await ws.send_json(frame)
            except Exception:
                dead.append(ws)
        for ws in dead:
            await self.drop(ws)


manager = ConnectionManager()


async def authorize_channel(channel: str, user_id: uuid.UUID, role: str, db) -> bool:
    """Per-channel authorization (private channels only for their owner/members)."""
    from sqlalchemy import or_, select

    from app.db.models import ConversationParticipant, League, LeagueMember, Media

    parts = channel.split(":")
    if len(parts) < 2:
        return False
    scope, ident = parts[0], parts[1]

    if scope == "user":
        return ident == str(user_id) and parts[-1] == "notifications"
    if scope == "player":
        return ident == str(user_id) and parts[-1] == "qo_score"
    if scope == "post":
        return parts[-1] == "counts"  # public counters for any authenticated viewer
    if scope == "league":
        try:
            league_id = uuid.UUID(ident)
        except ValueError:
            return False
        member = (
            await db.execute(
                select(LeagueMember.id).where(
                    LeagueMember.league_id == league_id,
                    LeagueMember.user_id == user_id,
                    LeagueMember.status == "active",
                )
            )
        ).first()
        if member:
            return True
        owner = (
            await db.execute(select(League.id).where(League.id == league_id, League.owner_id == user_id))
        ).first()
        return owner is not None
    if scope == "conversation":
        try:
            conv_id = uuid.UUID(ident)
        except ValueError:
            return False
        row = (
            await db.execute(
                select(ConversationParticipant.id).where(
                    ConversationParticipant.conversation_id == conv_id,
                    ConversationParticipant.user_id == user_id,
                )
            )
        ).first()
        return row is not None
    if scope == "media":
        try:
            media_id = uuid.UUID(ident)
        except ValueError:
            return False
        row = (await db.execute(select(Media.id).where(Media.id == media_id, Media.owner_id == user_id))).first()
        return row is not None
    return False
