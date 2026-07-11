"""Notifications list / mark-read and device (push token) registry (spec §8)."""
import uuid

from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import Page, get_current_user
from app.core.errors import not_found
from app.core.security import utcnow
from app.db.base import get_db
from app.db.models import Device, Notification, User
from app.services.notify import serialize_notification

router = APIRouter(tags=["notifications"])


@router.get("/notifications")
async def list_notifications(unread_only: bool = Query(False), page: Page = Depends(),
                             db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    base = select(Notification).where(Notification.user_id == user.id)
    count_base = select(func.count()).select_from(Notification).where(Notification.user_id == user.id)
    if unread_only:
        base = base.where(Notification.read_at.is_(None))
        count_base = count_base.where(Notification.read_at.is_(None))

    total = (await db.execute(count_base)).scalar_one()
    unread_count = (
        await db.execute(select(func.count()).select_from(Notification)
                         .where(Notification.user_id == user.id, Notification.read_at.is_(None)))
    ).scalar_one()
    rows = (
        await db.execute(base.order_by(Notification.created_at.desc())
                         .offset(page.offset).limit(page.limit))
    ).scalars().all()

    envelope = page.envelope([serialize_notification(n) for n in rows], total)
    envelope["unread_count"] = unread_count
    return envelope


@router.post("/notifications/{notification_id}/read")
async def mark_read(notification_id: uuid.UUID, db: AsyncSession = Depends(get_db),
                    user: User = Depends(get_current_user)):
    notif = await db.get(Notification, notification_id)
    if notif is None or notif.user_id != user.id:
        raise not_found("NOTIFICATION_NOT_FOUND", "Notification not found.")
    if notif.read_at is None:
        notif.read_at = utcnow()
        await db.commit()
    return {"id": str(notif.id), "read": True}


@router.post("/notifications/read-all")
async def mark_all_read(db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    await db.execute(
        update(Notification).where(Notification.user_id == user.id, Notification.read_at.is_(None))
        .values(read_at=utcnow())
    )
    await db.commit()
    from app.realtime import manager
    await manager.emit(f"user:{user.id}:notifications", "read_all", {})
    return {"read_all": True}


# --------------------------------------------------------------------------- devices
class DeviceIn(BaseModel):
    fcm_token: str = Field(min_length=8)
    platform: str = Field(pattern="^(android|ios|web)$")
    app_version: str | None = None


@router.post("/devices", status_code=201)
async def register_device(body: DeviceIn, db: AsyncSession = Depends(get_db),
                          user: User = Depends(get_current_user)):
    device = (
        await db.execute(select(Device).where(Device.fcm_token == body.fcm_token))
    ).scalar_one_or_none()
    if device is None:
        device = Device(user_id=user.id, fcm_token=body.fcm_token,
                        platform=body.platform, app_version=body.app_version)
        db.add(device)
    else:  # token re-registered (possibly by a different account on the same device)
        device.user_id = user.id
        device.platform = body.platform
        device.app_version = body.app_version
    device.last_seen_at = utcnow()
    await db.commit()
    return {"id": str(device.id), "registered": True}


@router.delete("/devices/{fcm_token}", status_code=204)
async def delete_device(fcm_token: str, db: AsyncSession = Depends(get_db),
                        user: User = Depends(get_current_user)):
    device = (
        await db.execute(select(Device).where(Device.fcm_token == fcm_token,
                                              Device.user_id == user.id))
    ).scalar_one_or_none()
    if device is not None:
        await db.delete(device)
        await db.commit()
    return Response(status_code=204)
