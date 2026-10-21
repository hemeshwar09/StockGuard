import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError

logger = logging.getLogger(__name__)


class DomainError(Exception):
    status_code = 400
    code = "bad_request"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class Unauthorized(DomainError):
    status_code = 401
    code = "unauthorized"


class Forbidden(DomainError):
    status_code = 403
    code = "forbidden"


class NotFound(DomainError):
    status_code = 404
    code = "not_found"


class Conflict(DomainError):
    status_code = 409
    code = "conflict"


class InsufficientStock(DomainError):
    status_code = 409
    code = "insufficient_stock"


class InvalidState(DomainError):
    status_code = 409
    code = "invalid_state"


class ReservationExpired(DomainError):
    status_code = 409
    code = "reservation_expired"


class IdempotencyMismatch(DomainError):
    status_code = 422
    code = "idempotency_key_reuse"


class Contention(DomainError):
    """Optimistic strategy exhausted its retries; the client should retry."""

    status_code = 503
    code = "contention"


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(DomainError)
    async def _domain_error(_: Request, exc: DomainError) -> JSONResponse:
        headers = {"Retry-After": "1"} if isinstance(exc, Contention) else None
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": exc.code, "detail": exc.message},
            headers=headers,
        )

    @app.exception_handler(IntegrityError)
    async def _integrity_error(_: Request, exc: IntegrityError) -> JSONResponse:
        # A CHECK constraint firing means an invariant was about to break; the
        # transaction is rolled back, so nothing was oversold.
        logger.warning("integrity violation: %s", exc.orig)
        return JSONResponse(
            status_code=409,
            content={
                "error": "integrity_violation",
                "detail": "request conflicts with a database constraint",
            },
        )
