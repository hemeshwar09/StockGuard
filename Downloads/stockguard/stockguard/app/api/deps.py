import secrets
from typing import Annotated

from fastapi import Depends, Header
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import get_session
from app.errors import Forbidden, Unauthorized


def current_user(x_user_id: Annotated[str | None, Header()] = None) -> str:
    """
    Stand-in for real authentication. In production, replace with JWT/OIDC
    verification (e.g. Cognito) and return the token's subject.
    """
    if x_user_id is None or not x_user_id.strip():
        raise Unauthorized("missing X-User-Id header")
    user_id = x_user_id.strip()
    if len(user_id) > 128:
        raise Unauthorized("X-User-Id is too long")
    return user_id


def require_admin(x_admin_key: Annotated[str | None, Header()] = None) -> None:
    if x_admin_key is None or not secrets.compare_digest(
        x_admin_key.encode(), settings.admin_api_key.encode()
    ):
        raise Forbidden("admin key required")


CurrentUser = Annotated[str, Depends(current_user)]
Session = Annotated[AsyncSession, Depends(get_session)]
IdempotencyKeyHeader = Annotated[str, Header(min_length=8, max_length=128)]
