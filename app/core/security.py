"""Passwords (argon2id), JWT access tokens, refresh-token hashing, signed URLs."""
import hashlib
import hmac
import re
import secrets
import time
import uuid
from datetime import datetime, timedelta, timezone

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

from app.core.config import settings

_ph = PasswordHasher()  # argon2id by default

PASSWORD_RULE = re.compile(r"^(?=.*[A-Za-z])(?=.*\d).+$")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# --- passwords -------------------------------------------------------------
def hash_password(password: str) -> str:
    return _ph.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return _ph.verify(password_hash, password)
    except VerifyMismatchError:
        return False
    except Exception:
        return False


def validate_password_strength(password: str) -> bool:
    return len(password) >= settings.password_min_length and bool(PASSWORD_RULE.match(password))


# --- JWT access tokens -----------------------------------------------------
def create_access_token(user_id: uuid.UUID, role: str) -> str:
    now = utcnow()
    payload = {
        "sub": str(user_id),
        "role": role,
        "type": "access",
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(seconds=settings.access_token_ttl_seconds)).timestamp()),
        "jti": uuid.uuid4().hex,
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


class TokenError(Exception):
    def __init__(self, code: str):
        self.code = code


def decode_access_token(token: str) -> dict:
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    except jwt.ExpiredSignatureError as e:
        raise TokenError("TOKEN_EXPIRED") from e
    except jwt.PyJWTError as e:
        raise TokenError("INVALID_TOKEN") from e
    if payload.get("type") != "access":
        raise TokenError("INVALID_TOKEN")
    return payload


# --- refresh tokens ---------------------------------------------------------
def new_refresh_token() -> str:
    return secrets.token_urlsafe(48)


def hash_refresh_token(token: str) -> str:
    """sha256 — deterministic so it can be used as a lookup key."""
    return hashlib.sha256(token.encode()).hexdigest()


def refresh_expiry() -> datetime:
    return utcnow() + timedelta(days=settings.refresh_token_ttl_days)


# --- opaque single-use tokens (email verify / password reset) ----------------
def new_opaque_token() -> str:
    return secrets.token_urlsafe(32)


def hash_opaque_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# --- signed URLs for private media (local storage) ---------------------------
def sign_storage_key(key: str, expires_in: int | None = None) -> tuple[int, str]:
    exp = int(time.time()) + (expires_in or settings.signed_url_ttl_seconds)
    sig = hmac.new(settings.jwt_secret.encode(), f"{key}:{exp}".encode(), hashlib.sha256).hexdigest()
    return exp, sig


def verify_storage_signature(key: str, exp: int, sig: str) -> bool:
    if exp < int(time.time()):
        return False
    expected = hmac.new(settings.jwt_secret.encode(), f"{key}:{exp}".encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, sig)


def mask_phone(phone: str) -> str:
    """'+919876543210' → '+91 98••••3210'"""
    digits = phone.lstrip("+")
    if len(digits) < 8:
        return phone
    cc, rest = digits[:2], digits[2:]
    return f"+{cc} {rest[:2]}••••{rest[-4:]}"
