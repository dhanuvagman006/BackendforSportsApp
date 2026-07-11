"""Media uploads (spec §7): direct multipart, presigned flow for big videos,
and signed serving for private files."""
import uuid

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, Query, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.deps import get_current_user
from app.core.errors import bad_request, forbidden, not_found, too_large, unsupported_media
from app.core.security import verify_storage_signature
from app.db.base import SessionLocal, get_db
from app.db.models import Media, User
from app.services.media_ops import PURPOSES, store_upload
from app.services.providers import get_storage
from app.services.serializers import serialize_media_item

router = APIRouter(tags=["media"])


@router.post("/media", status_code=201)
async def upload_media(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
    file: UploadFile = File(...),
    purpose: str = Form(...),
    title: str | None = Form(default=None),
    subtitle: str | None = Form(default=None),
):
    media = await store_upload(db, user, file, purpose=purpose, title=title, subtitle=subtitle)
    await db.commit()
    return {
        "id": str(media.id),
        "url": media.url or get_storage().signed_url(media.storage_key),
        "mime": media.mime,
        "bytes": media.bytes,
        "width": media.width,
        "height": media.height,
        "acl": media.acl,
        "status": media.status,
    }


# --------------------------------------------------------------------------- presigned flow
class PresignIn(BaseModel):
    filename: str = Field(min_length=1, max_length=200)
    mime: str
    bytes: int = Field(gt=0)
    purpose: str = "post_video"


@router.post("/media/presign")
async def presign(body: PresignIn, db: AsyncSession = Depends(get_db),
                  user: User = Depends(get_current_user)):
    if body.purpose not in PURPOSES:
        raise bad_request("VALIDATION_ERROR", f"Unknown purpose '{body.purpose}'.", field="purpose")
    max_bytes, allowed, acl = PURPOSES[body.purpose]
    if body.bytes > max_bytes:
        raise too_large(f"Max size for {body.purpose} is {max_bytes // (1024 * 1024)} MB.")
    mime = body.mime.lower()
    if not any(mime == a or (a.endswith("/") and mime.startswith(a)) for a in allowed):
        raise unsupported_media(f"'{mime}' is not allowed for {body.purpose}.")

    media = Media(id=uuid.uuid4(), owner_id=user.id, purpose=body.purpose,
                  storage_key=f"{acl}/pending_{uuid.uuid4().hex}_{body.filename.replace('/', '_')}",
                  mime=mime, bytes=body.bytes, status="pending", acl=acl)
    db.add(media)
    await db.commit()

    # Local-storage dev flow: the client PUTs to our own upload endpoint.
    # With S3 configured, this returns a real presigned S3 URL instead.
    return {
        "media_id": str(media.id),
        "upload_url": f"{settings.base_url}/v1/media/{media.id}/upload",
        "fields": {},
        "expires_in": 900,
    }


@router.put("/media/{media_id}/upload", status_code=204)
async def presigned_upload(media_id: uuid.UUID, request: Request,
                           db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    media = await db.get(Media, media_id)
    if media is None or media.owner_id != user.id:
        raise not_found("MEDIA_NOT_FOUND", "Upload target not found.")
    if media.status != "pending":
        raise bad_request("VALIDATION_ERROR", "This upload was already completed.")
    data = await request.body()
    max_bytes, _, _ = PURPOSES[media.purpose]
    if len(data) > max_bytes:
        raise too_large()
    stored = await get_storage().save(data, media.storage_key.split("_", 2)[-1], media.mime or "", acl=media.acl)
    media.storage_key = stored.storage_key
    media.url = stored.url
    media.bytes = len(data)
    await db.commit()
    return None


async def _finish_processing(media_id: uuid.UUID) -> None:
    """Background 'transcode' — in production this is the MediaConvert/Mux
    webhook target; in dev we mark ready immediately and emit over WS."""
    async with SessionLocal() as db:
        media = await db.get(Media, media_id)
        if media is None:
            return
        media.status = "ready"
        await db.commit()
    from app.realtime import manager
    await manager.emit(f"media:{media_id}", "ready",
                       {"media_id": str(media_id), "status": "ready"})


@router.post("/media/{media_id}/complete")
async def complete_upload(media_id: uuid.UUID, background: BackgroundTasks,
                          db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    media = await db.get(Media, media_id)
    if media is None or media.owner_id != user.id:
        raise not_found("MEDIA_NOT_FOUND", "Upload not found.")
    media.status = "processing"
    await db.commit()
    background.add_task(_finish_processing, media_id)
    return {"media_id": str(media_id), "status": "processing"}


@router.get("/media/{media_id}")
async def get_media(media_id: uuid.UUID, db: AsyncSession = Depends(get_db),
                    user: User = Depends(get_current_user)):
    media = await db.get(Media, media_id)
    if media is None:
        raise not_found("MEDIA_NOT_FOUND", "Media not found.")
    if media.acl == "private" and media.owner_id != user.id:
        raise forbidden("NOT_ALLOWED", "This file is private.")
    return serialize_media_item(media)


# --------------------------------------------------------------------------- signed private files
@router.get("/files/signed")
async def serve_signed(key: str = Query(...), exp: int = Query(...), sig: str = Query(...)):
    if not verify_storage_signature(key, exp, sig):
        raise forbidden("NOT_ALLOWED", "This link is invalid or has expired.")
    path = await get_storage().open_path(key)
    if not path.exists():
        raise not_found("MEDIA_NOT_FOUND", "File not found.")
    return FileResponse(path)
