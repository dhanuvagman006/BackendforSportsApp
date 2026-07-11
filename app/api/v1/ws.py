"""WebSocket endpoint (spec §9): wss://…/v1/ws?token=<access_token>

Client → server frames: {"action": "subscribe"|"unsubscribe"|"ping", "channel": "..."}
Server → client frames: {"channel": "...", "event": "...", "data": {...}}
"""
import logging
import uuid

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect

from app.core.security import TokenError, decode_access_token
from app.db.base import SessionLocal
from app.realtime import authorize_channel, manager

log = logging.getLogger("sportyqo.ws")
router = APIRouter(tags=["realtime"])


@router.websocket("/ws")
async def websocket_endpoint(ws: WebSocket, token: str = Query(...)):
    try:
        payload = decode_access_token(token)
    except TokenError:
        await ws.close(code=4401, reason="invalid token")
        return
    user_id = uuid.UUID(payload["sub"])
    role = payload.get("role", "player")

    await ws.accept()
    # personal channels are auto-subscribed
    await manager.subscribe(f"user:{user_id}:notifications", ws)
    if role == "player":
        await manager.subscribe(f"player:{user_id}:qo_score", ws)

    try:
        while True:
            frame = await ws.receive_json()
            action = frame.get("action")
            channel = frame.get("channel", "")

            if action == "ping":
                await ws.send_json({"channel": "system", "event": "pong", "data": {}})
                continue
            if action not in ("subscribe", "unsubscribe") or not channel:
                await ws.send_json({"channel": "system", "event": "error",
                                    "data": {"message": "unknown action"}})
                continue

            if action == "unsubscribe":
                await manager.unsubscribe(channel, ws)
                await ws.send_json({"channel": channel, "event": "unsubscribed", "data": {}})
                continue

            async with SessionLocal() as db:
                allowed = await authorize_channel(channel, user_id, role, db)
            if not allowed:
                await ws.send_json({"channel": channel, "event": "error",
                                    "data": {"message": "not authorized for this channel"}})
                continue
            await manager.subscribe(channel, ws)
            await ws.send_json({"channel": channel, "event": "subscribed", "data": {}})
    except WebSocketDisconnect:
        pass
    except Exception:
        log.exception("websocket error")
    finally:
        await manager.drop(ws)
