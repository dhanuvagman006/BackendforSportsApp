"""Error envelope — every error returns
{ "error": { "code", "message", "field", "request_id" } }  (spec §11).
"""
import logging
import uuid

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

log = logging.getLogger("sportyqo")


class ApiError(Exception):
    def __init__(self, status_code: int, code: str, message: str, field: str | None = None,
                 headers: dict | None = None):
        self.status_code = status_code
        self.code = code
        self.message = message
        self.field = field
        self.headers = headers or {}
        super().__init__(message)


# convenience constructors -------------------------------------------------
def bad_request(code: str, message: str, field: str | None = None) -> ApiError:
    return ApiError(400, code, message, field)


def unauthorized(code: str = "INVALID_CREDENTIALS", message: str = "Invalid credentials.") -> ApiError:
    return ApiError(401, code, message)


def forbidden(code: str, message: str) -> ApiError:
    return ApiError(403, code, message)


def not_found(code: str, message: str) -> ApiError:
    return ApiError(404, code, message)


def conflict(code: str, message: str, field: str | None = None) -> ApiError:
    return ApiError(409, code, message, field)


def gone(code: str, message: str) -> ApiError:
    return ApiError(410, code, message)


def too_large(message: str = "File too large.") -> ApiError:
    return ApiError(413, "FILE_TOO_LARGE", message)


def unsupported_media(message: str = "Unsupported file type.") -> ApiError:
    return ApiError(415, "UNSUPPORTED_MIME", message)


def unprocessable(code: str, message: str, field: str | None = None) -> ApiError:
    return ApiError(422, code, message, field)


def rate_limited(message: str = "Too many attempts. Try again later.", retry_after: int = 60) -> ApiError:
    return ApiError(429, "TOO_MANY_ATTEMPTS", message, headers={"Retry-After": str(retry_after)})


def _envelope(request: Request, code: str, message: str, field: str | None = None) -> dict:
    return {
        "error": {
            "code": code,
            "message": message,
            "field": field,
            "request_id": getattr(request.state, "request_id", None),
        }
    }


def install_error_handlers(app: FastAPI) -> None:
    @app.middleware("http")
    async def request_id_middleware(request: Request, call_next):
        request.state.request_id = "req_" + uuid.uuid4().hex[:20]
        response = await call_next(request)
        response.headers["X-Request-Id"] = request.state.request_id
        return response

    @app.exception_handler(ApiError)
    async def api_error_handler(request: Request, exc: ApiError):
        return JSONResponse(
            status_code=exc.status_code,
            content=_envelope(request, exc.code, exc.message, exc.field),
            headers=exc.headers,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_handler(request: Request, exc: RequestValidationError):
        first = exc.errors()[0] if exc.errors() else {}
        loc = [str(p) for p in first.get("loc", []) if p not in ("body", "query", "path")]
        field = ".".join(loc) or None
        code = "MISSING_FIELD" if first.get("type") == "missing" else "VALIDATION_ERROR"
        return JSONResponse(status_code=400, content=_envelope(request, code, first.get("msg", "Invalid request."), field))

    @app.exception_handler(Exception)
    async def unhandled_handler(request: Request, exc: Exception):
        log.exception("Unhandled error [%s]", getattr(request.state, "request_id", "?"))
        return JSONResponse(status_code=500, content=_envelope(request, "INTERNAL_ERROR", "Something went wrong on our side."))
