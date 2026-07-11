"""Recompute rankings for every category — run from cron, e.g.

    */30 * * * *  cd /srv/sportyqo && python -m scripts.recompute_rankings
"""
import asyncio

from sqlalchemy import func, select

from app.db.base import SessionLocal
from app.db.models import User, UserProfile
from app.services.scoring import recompute_rankings


async def main() -> None:
    async with SessionLocal() as db:
        categories = (
            await db.execute(
                select(func.concat(func.coalesce(UserProfile.age_group, "Open"), " ", UserProfile.sport))
                .join(User, User.id == UserProfile.user_id)
                .where(UserProfile.sport.is_not(None), User.deleted_at.is_(None))
                .distinct()
            )
        ).scalars().all()
        for category in categories:
            await recompute_rankings(db, category)
            print(f"✓ {category}")
        await db.commit()
    print(f"done — {len(categories)} categories")


if __name__ == "__main__":
    asyncio.run(main())
