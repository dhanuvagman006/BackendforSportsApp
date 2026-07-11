"""Seed the database.

Usage:
    python -m scripts.seed            # card tiers only (idempotent)
    python -m scripts.seed --demo     # + a demo coach, player, league and match
"""
import asyncio
import sys

from sqlalchemy import select

from app.core.security import hash_password
from app.db.base import SessionLocal
from app.db.models import League, LeagueMember, Standing, Team, User, UserProfile, UserSettings
from app.services.identifiers import allocate_player_id, mint_league_code
from app.services.scoring import recompute_score, seed_tiers


async def main(demo: bool) -> None:
    async with SessionLocal() as db:
        await seed_tiers(db)
        await db.commit()
        print("✓ card tiers seeded")

        if not demo:
            return

        existing = (await db.execute(select(User).where(User.email == "coach@demo.sportyqo"))).scalar_one_or_none()
        if existing:
            print("✓ demo data already present")
            return

        coach = User(email="coach@demo.sportyqo", password_hash=hash_password("Demo1234"),
                     full_name="Coach Suneeth", role="coach", onboarding_stage="complete")
        db.add(coach)
        await db.flush()
        db.add(UserSettings(user_id=coach.id))

        player = User(email="player@demo.sportyqo", password_hash=hash_password("Demo1234"),
                      full_name="Aarav Mehta", role="player", onboarding_stage="complete")
        db.add(player)
        await db.flush()
        db.add(UserSettings(user_id=player.id))
        player.player_id = await allocate_player_id(db)
        db.add(UserProfile(user_id=player.id, sport="Cricket", sub_role="Batter",
                           age_group="U16", location="Mumbai, India"))
        await recompute_score(db, player.id)

        code = await mint_league_code(db, "Falcons U16 Premier League")
        league = League(owner_id=coach.id, name="Falcons U16 Premier League", league_code=code,
                        cricket_type="professional", gender="mens", location="Bangalore, Karnataka",
                        teams_count=2, status="active")
        db.add(league)
        await db.flush()
        teams = [Team(league_id=league.id, name=n, position=i)
                 for i, n in enumerate(["Falcons FC", "Thunder Strikers"])]
        db.add_all(teams)
        await db.flush()
        db.add_all([Standing(league_id=league.id, team_id=t.id) for t in teams])
        db.add(LeagueMember(league_id=league.id, team_id=teams[0].id, user_id=player.id))
        await db.commit()

        print(f"✓ demo data ready — coach@demo.sportyqo / player@demo.sportyqo (password Demo1234)")
        print(f"  league code: {code}")


if __name__ == "__main__":
    asyncio.run(main(demo="--demo" in sys.argv))
