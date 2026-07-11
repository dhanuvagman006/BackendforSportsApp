"""Server-only admin surface (X-Admin-Key header). All actions audit-logged."""
import uuid

from fastapi import APIRouter, BackgroundTasks, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.deps import require_admin
from app.core.errors import bad_request, not_found
from app.core.security import utcnow
from app.db.base import get_db
from app.db.models import (
    AuditLog,
    Certification,
    CertificationDocument,
    CoachProfile,
    User,
    UserProfile,
)
from app.services import scoring
from app.services.notify import create_notification, deliver_notification
from app.services.serializers import serialize_media_item

router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_admin)])


@router.get("/certifications")
async def list_certifications(status: str = Query("under_review"),
                              db: AsyncSession = Depends(get_db)):
    rows = (
        await db.execute(
            select(Certification, User)
            .join(User, User.id == Certification.coach_id)
            .options(selectinload(Certification.documents).selectinload(CertificationDocument.media))
            .where(Certification.status == status)
            .order_by(Certification.created_at)
        )
    ).all()
    return {
        "items": [
            {
                "certification_id": str(c.id),
                "coach": {"id": str(u.id), "name": u.full_name, "email": u.email},
                "certification_level": c.certification_level,
                "issuing_body": c.issuing_body,
                "issued_on": c.issued_on.isoformat() if c.issued_on else None,
                "status": c.status,
                "submitted_at": c.created_at.isoformat(),
                "documents": [serialize_media_item(d.media) for d in c.documents],
            }
            for c, u in rows
        ]
    }


class ReviewIn(BaseModel):
    status: str = Field(pattern="^(approved|rejected)$")
    rejection_reason: str | None = Field(default=None, max_length=500)
    reviewer: str = "admin"


@router.post("/certifications/{certification_id}/review")
async def review_certification(certification_id: uuid.UUID, body: ReviewIn,
                               background: BackgroundTasks, db: AsyncSession = Depends(get_db)):
    cert = await db.get(Certification, certification_id)
    if cert is None:
        raise not_found("CERTIFICATION_NOT_FOUND", "Certification not found.")
    if cert.status != "under_review":
        raise bad_request("VALIDATION_ERROR", f"Certification is already {cert.status}.")
    if body.status == "rejected" and not body.rejection_reason:
        raise bad_request("MISSING_FIELD", "rejection_reason is required when rejecting.",
                          field="rejection_reason")

    cert.status = body.status
    cert.reviewed_by = body.reviewer
    cert.reviewed_at = utcnow()
    cert.rejection_reason = body.rejection_reason

    coach = await db.get(User, cert.coach_id)
    if body.status == "approved":
        coach.verified = True  # blue tick (spec: coach verified set by certification approval)
        cp = (await db.execute(select(CoachProfile).where(CoachProfile.user_id == coach.id))).scalar_one_or_none()
        if cp:
            cp.certification = cert.certification_level
        message = f"Your {cert.certification_level} certification was approved — you're now verified!"
    else:
        message = f"Your {cert.certification_level} certification was rejected: {body.rejection_reason}"

    n = await create_notification(db, coach.id, "certification_update", message)
    db.add(AuditLog(actor=body.reviewer, action=f"certification.{body.status}",
                    entity="certification", entity_id=str(cert.id),
                    detail={"coach_id": str(coach.id), "reason": body.rejection_reason}))
    await db.commit()
    background.add_task(deliver_notification, n.id)
    return {"certification_id": str(cert.id), "status": cert.status}


class CorrectionIn(BaseModel):
    user_id: uuid.UUID
    points: int = Field(ge=-100000, le=100000)
    reason: str = Field(min_length=3, max_length=500)


@router.post("/points/correction", status_code=201)
async def points_correction(body: CorrectionIn, db: AsyncSession = Depends(get_db)):
    """Score corrections go through the ledger too — the append-only invariant
    holds even for admins (no direct writes to qo_scores, ever)."""
    user = await db.get(User, body.user_id)
    if user is None:
        raise not_found("PLAYER_NOT_FOUND", "User not found.")
    event = await scoring.award_points(db, body.user_id, source="correction",
                                       points=body.points, reason=f"Admin correction: {body.reason}")
    profile = (await db.execute(select(UserProfile).where(UserProfile.user_id == body.user_id))).scalar_one_or_none()
    category = scoring.ranking_category(profile)
    if category:
        await scoring.recompute_rankings(db, category)
    db.add(AuditLog(actor="admin", action="points.correction", entity="user",
                    entity_id=str(body.user_id),
                    detail={"points": body.points, "reason": body.reason}))
    await db.commit()
    return {"event_id": str(event.id), "points": body.points}
