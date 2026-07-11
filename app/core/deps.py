"""Shared FastAPI dependencies."""
import uuid

from fastapi import Depends, Header, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.errors import ApiError, forbidden, unauthorized
from app.core.security import TokenError, decode_access_token
from app.db.base import get_db
from app.db.models import User


async def get_current_user(
    request: Request,
    db: AsyncSession = Depends(get_db),
    authorization: str | None = Header(default=None),
) -> User:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise unauthorized("INVALID_TOKEN", "Missing bearer token.")
    token = authorization.split(" ", 1)[1].strip()
    try:
        payload = decode_access_token(token)
    except TokenError as e:
        raise unauthorized(e.code, "Your session has expired." if e.code == "TOKEN_EXPIRED" else "Invalid token.")
    user = await db.get(User, uuid.UUID(payload["sub"]))
    if user is None or user.deleted_at is not None:
        raise unauthorized("INVALID_TOKEN", "Account not found.")
    return user


async def get_optional_user(
    request: Request,
    db: AsyncSession = Depends(get_db),
    authorization: str | None = Header(default=None),
) -> User | None:
    if not authorization:
        return None
    try:
        return await get_current_user(request, db, authorization)
    except ApiError:
        return None


def require_role(role: str):
    async def _guard(user: User = Depends(get_current_user)) -> User:
        if user.role != role:
            raise forbidden("ROLE_MISMATCH", f"This action requires a {role} account.")
        return user

    return _guard


require_coach = require_role("coach")
require_player = require_role("player")


async def require_admin(x_admin_key: str | None = Header(default=None)) -> None:
    if not x_admin_key or x_admin_key != settings.admin_api_key:
        raise forbidden("ADMIN_ONLY", "Admin credentials required.")


class Page:
    def __init__(self, page: int = Query(1, ge=1), limit: int = Query(20, ge=1, le=100)):
        self.page = page
        self.limit = limit

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.limit

    def envelope(self, items: list, total: int) -> dict:
        return {
            "items": items,
            "page": self.page,
            "limit": self.limit,
            "total": total,
            "has_more": self.offset + len(items) < total,
        }
