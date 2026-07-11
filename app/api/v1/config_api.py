"""Server-owned config (Appendix A #3): card tiers + cricket types are served
from the backend so tuning never requires an app release."""
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import get_db
from app.db.models import CRICKET_TYPES
from app.services import scoring

router = APIRouter(prefix="/config", tags=["config"])

_TYPE_LABELS = {
    "gully": "Gully Cricket",
    "professional": "Professional Cricket",
    "box": "Box Cricket",
    "tennis_ball": "Tennis Ball Cricket",
    "hard_ball": "Hard Ball Cricket",
    "corporate": "Corporate Cricket",
    "beach": "Beach Cricket",
}


@router.get("/card-tiers")
async def card_tiers(db: AsyncSession = Depends(get_db)):
    tiers = await scoring.load_tiers(db)
    if not tiers:
        await scoring.seed_tiers(db)
        await db.commit()
        tiers = await scoring.load_tiers(db)
    return {
        "tiers": [
            {"level": t.level, "tier": scoring.tier_slug(t.label), "label": t.label,
             "threshold": t.threshold, "hex": t.hex}
            for t in tiers
        ]
    }


@router.get("/cricket-types")
async def cricket_types():
    return {"types": [{"value": v, "label": _TYPE_LABELS[v]} for v in CRICKET_TYPES]}
