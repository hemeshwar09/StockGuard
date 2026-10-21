"""
Idempotency-Key support (the Stripe-style contract).

Within ONE transaction we insert the key with ON CONFLICT DO NOTHING, run the
business logic, and store the response. Consequences:

* Two concurrent requests with the same key: Postgres makes the second INSERT
  wait for the first transaction to finish. When it finishes, the second sees
  the conflict, reads the stored response and replays it. The work runs once.
* If the handler fails, the transaction (including the key row) rolls back, so
  a failed attempt can be retried with the same key.
* Same key + different request body/path -> 422 (a client bug, not a retry).
"""

import hashlib
import json
from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import Conflict, IdempotencyMismatch
from app.models import IdempotencyKey

Handler = Callable[[], Awaitable[dict[str, Any]]]


def compute_request_hash(method: str, path: str, body: dict[str, Any]) -> str:
    canonical = json.dumps(
        {"m": method, "p": path, "b": body}, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


async def execute_idempotent(
    session: AsyncSession,
    *,
    user_id: str,
    key: str,
    request_hash: str,
    success_status: int,
    handler: Handler,
) -> tuple[int, dict[str, Any], bool]:
    """Returns (status_code, json_payload, replayed). Opens and commits the transaction."""
    async with session.begin():
        inserted = (
            await session.execute(
                pg_insert(IdempotencyKey)
                .values(user_id=user_id, key=key, request_hash=request_hash)
                .on_conflict_do_nothing(index_elements=["user_id", "key"])
                .returning(IdempotencyKey.key)
            )
        ).first()

        if inserted is None:
            existing = (
                await session.execute(
                    select(IdempotencyKey).where(
                        IdempotencyKey.user_id == user_id, IdempotencyKey.key == key
                    )
                )
            ).scalar_one()
            if existing.request_hash != request_hash:
                raise IdempotencyMismatch(
                    "this Idempotency-Key was already used with a different request"
                )
            if existing.response is None or existing.status_code is None:
                raise Conflict("a request with this Idempotency-Key is still in progress")
            return existing.status_code, existing.response, True

        payload = await handler()
        await session.execute(
            update(IdempotencyKey)
            .where(IdempotencyKey.user_id == user_id, IdempotencyKey.key == key)
            .values(status_code=success_status, response=payload)
            .execution_options(synchronize_session=False)
        )
        return success_status, payload, False
