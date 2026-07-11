"""Third-party provider abstractions.

Development defaults are dependency-free (console/local-disk).  Production
implementations (MSG91/Twilio, SES/SMTP, FCM, S3) plug in behind the same
interfaces — selected via settings, wired in `get_*_provider()`.
"""
import io
import logging
import shutil
import uuid
from pathlib import Path

from app.core.config import settings
from app.core.security import sign_storage_key

log = logging.getLogger("sportyqo.providers")


# --- SMS -------------------------------------------------------------------
class SmsProvider:
    async def send(self, phone: str, message: str) -> None:
        raise NotImplementedError


class ConsoleSms(SmsProvider):
    async def send(self, phone: str, message: str) -> None:
        log.info("[SMS → %s] %s", phone, message)


class Msg91Sms(SmsProvider):
    """MSG91 (India). Requires MSG91_AUTH_KEY; HTTP call left as a thin wrapper."""
    async def send(self, phone: str, message: str) -> None:
        import httpx
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(
                "https://control.msg91.com/api/v5/flow/",
                headers={"authkey": settings.msg91_auth_key},
                json={"recipients": [{"mobiles": phone.lstrip("+"), "message": message}]},
            )


def get_sms_provider() -> SmsProvider:
    if settings.sms_provider == "msg91" and settings.msg91_auth_key:
        return Msg91Sms()
    return ConsoleSms()


# --- Email -------------------------------------------------------------------
class EmailProvider:
    async def send(self, to: str, subject: str, body: str) -> None:
        raise NotImplementedError


class ConsoleEmail(EmailProvider):
    async def send(self, to: str, subject: str, body: str) -> None:
        log.info("[EMAIL → %s] %s\n%s", to, subject, body)


class SmtpEmail(EmailProvider):
    async def send(self, to: str, subject: str, body: str) -> None:
        import smtplib
        from email.message import EmailMessage

        msg = EmailMessage()
        msg["From"], msg["To"], msg["Subject"] = settings.email_from, to, subject
        msg.set_content(body)
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port) as server:
            server.starttls()
            if settings.smtp_user:
                server.login(settings.smtp_user, settings.smtp_password)
            server.send_message(msg)


def get_email_provider() -> EmailProvider:
    if settings.email_provider == "smtp" and settings.smtp_host:
        return SmtpEmail()
    return ConsoleEmail()


# --- Push (FCM/APNs) ----------------------------------------------------------
class PushProvider:
    async def send(self, tokens: list[str], title: str, body: str, data: dict | None = None) -> None:
        raise NotImplementedError


class ConsolePush(PushProvider):
    async def send(self, tokens: list[str], title: str, body: str, data: dict | None = None) -> None:
        if tokens:
            log.info("[PUSH → %d device(s)] %s — %s %s", len(tokens), title, body, data or "")


def get_push_provider() -> PushProvider:
    # FCM implementation slots in here once a service-account JSON is configured.
    return ConsolePush()


# --- Storage -------------------------------------------------------------------
class StorageResult:
    def __init__(self, storage_key: str, url: str | None):
        self.storage_key = storage_key
        self.url = url


class StorageProvider:
    async def save(self, data: bytes, filename: str, mime: str, acl: str = "public") -> StorageResult: ...
    async def open_path(self, storage_key: str) -> Path: ...
    def public_url(self, storage_key: str) -> str: ...
    def signed_url(self, storage_key: str) -> str: ...
    async def delete(self, storage_key: str) -> None: ...


class LocalStorage(StorageProvider):
    """Local-disk storage. Public files are served from /static; private files
    only via HMAC-signed URLs (see /v1/files/signed)."""

    def __init__(self) -> None:
        self.root = Path(settings.storage_dir)
        (self.root / "public").mkdir(parents=True, exist_ok=True)
        (self.root / "private").mkdir(parents=True, exist_ok=True)

    async def save(self, data: bytes, filename: str, mime: str, acl: str = "public") -> StorageResult:
        safe = filename.replace("/", "_").replace("\\", "_")
        key = f"{acl}/{uuid.uuid4().hex}_{safe}"
        path = self.root / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        url = self.public_url(key) if acl == "public" else None
        return StorageResult(key, url)

    async def open_path(self, storage_key: str) -> Path:
        return self.root / storage_key

    def public_url(self, storage_key: str) -> str:
        return f"{settings.base_url}/static/{storage_key.removeprefix('public/')}"

    def signed_url(self, storage_key: str) -> str:
        exp, sig = sign_storage_key(storage_key)
        return f"{settings.base_url}/v1/files/signed?key={storage_key}&exp={exp}&sig={sig}"

    async def delete(self, storage_key: str) -> None:
        path = self.root / storage_key
        if path.exists():
            path.unlink()


_storage: StorageProvider | None = None


def get_storage() -> StorageProvider:
    global _storage
    if _storage is None:
        # S3Storage (boto3 presigned) slots in here when settings.storage_provider == "s3".
        _storage = LocalStorage()
    return _storage


# --- image hygiene ----------------------------------------------------------------
def strip_image_metadata(data: bytes, mime: str) -> tuple[bytes, int | None, int | None]:
    """Re-encode images to drop EXIF (incl. GPS). Returns (bytes, width, height)."""
    try:
        from PIL import Image

        img = Image.open(io.BytesIO(data))
        width, height = img.size
        out = io.BytesIO()
        fmt = {"image/jpeg": "JPEG", "image/png": "PNG", "image/webp": "WEBP"}.get(mime, img.format or "PNG")
        if fmt == "JPEG" and img.mode in ("RGBA", "P"):
            img = img.convert("RGB")
        # Re-encode without carrying EXIF (drops GPS and other metadata).
        save_kwargs = {"exif": b""} if fmt in ("JPEG", "WEBP") else {}
        img.save(out, format=fmt, **save_kwargs)
        return out.getvalue(), width, height
    except Exception:  # non-image or unreadable — return as-is
        return data, None, None


def virus_scan(data: bytes) -> bool:
    """Hook for ClamAV / VirusTotal in production. Always clean in dev."""
    return True
