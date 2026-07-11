"""Server-minted identifiers (spec Appendix A #1, #2).

- Player IDs: `P{YY}{NNN}` from a per-year, row-locked sequence table.
- League codes: `PREF-NN-YY`, unique index + collision retry.
"""
import random
import re
import string
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import League, PlayerIdSequence


async def allocate_player_id(db: AsyncSession) -> str:
    """Allocate the next public Player ID inside the caller's transaction.

    Uses SELECT ... FOR UPDATE on the year row so concurrent allocations
    serialize and never collide (replacing the client's
    `millisecondsSinceEpoch % 999` hack).
    """
    year = datetime.now(timezone.utc).year % 100
    row = (
        await db.execute(
            select(PlayerIdSequence).where(PlayerIdSequence.year == year).with_for_update()
        )
    ).scalar_one_or_none()
    if row is None:
        row = PlayerIdSequence(year=year, last_seq=0)
        db.add(row)
        await db.flush()
        row = (
            await db.execute(
                select(PlayerIdSequence).where(PlayerIdSequence.year == year).with_for_update()
            )
        ).scalar_one()
    row.last_seq += 1
    return f"P{year:02d}{row.last_seq:03d}"


def _league_prefix(name: str) -> str:
    letters = re.sub(r"[^A-Za-z]", "", name).upper()
    return (letters[:4] or "SPTQ").ljust(4, "X")


def _candidate_code(name: str) -> str:
    yy = datetime.now(timezone.utc).year % 100
    mid = "".join(random.choices(string.digits, k=2))
    return f"{_league_prefix(name)}-{mid}-{yy:02d}"


async def mint_league_code(db: AsyncSession, name: str, max_attempts: int = 8) -> str:
    """Generate a unique league code (`FALC-16-24` style)."""
    for attempt in range(max_attempts):
        code = _candidate_code(name)
        if attempt >= max_attempts - 2:  # widen the space if we keep colliding
            code = f"{_league_prefix(name)}-{''.join(random.choices(string.ascii_uppercase + string.digits, k=4))}"
        exists = (await db.execute(select(League.id).where(League.league_code == code))).first()
        if not exists:
            return code
    raise RuntimeError("CODE_COLLISION")


def derive_age_group(dob) -> str:
    """Public-safe age bucket from DOB (dob itself is never exposed)."""
    today = datetime.now(timezone.utc).date()
    age = today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))
    if age < 12:
        return "U12"
    if age < 14:
        return "U14"
    if age < 16:
        return "U16"
    if age < 19:
        return "U19"
    return "Open"
