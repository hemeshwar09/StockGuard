"""
Background housekeeping:

1. Expire ACTIVE reservations past their TTL and return the units to stock.
2. Cancel PENDING_PAYMENT orders whose reservation is gone.
3. Delete old idempotency keys.

Every transition is a guarded UPDATE, so running several sweepers (one per
API worker / replica) is safe: at most one wins each row.
"""

import asyncio
import logging
from datetime import timedelta

from sqlalchemy import delete, select, update

from app.config import settings
from app.db import SessionLocal
from app.models import (
    IdempotencyKey,
    Order,
    OrderStatus,
    Reservation,
    ReservationStatus,
    utcnow,
)
from app.services.inventory import release_reservation

logger = logging.getLogger(__name__)


async def sweep_once(batch_size: int = 200) -> tuple[int, int]:
    """One sweep. Returns (reservations_expired, orders_cancelled)."""
    now = utcnow()

    async with SessionLocal() as session:
        ids = (
            (
                await session.execute(
                    select(Reservation.id)
                    .where(
                        Reservation.status == ReservationStatus.ACTIVE,
                        Reservation.expires_at <= now,
                    )
                    .order_by(Reservation.expires_at)
                    .limit(batch_size)
                )
            )
            .scalars()
            .all()
        )

    expired = 0
    for reservation_id in ids:
        # One small transaction per reservation keeps lock hold times tiny.
        async with SessionLocal() as session, session.begin():
            if await release_reservation(
                session, reservation_id, new_status=ReservationStatus.EXPIRED
            ):
                expired += 1

    async with SessionLocal() as session, session.begin():
        dead_reservations = select(Reservation.id).where(
            Reservation.status.in_([ReservationStatus.EXPIRED, ReservationStatus.RELEASED])
        )
        result = await session.execute(
            update(Order)
            .where(
                Order.status == OrderStatus.PENDING_PAYMENT,
                Order.reservation_id.in_(dead_reservations),
            )
            .values(status=OrderStatus.CANCELLED, updated_at=utcnow())
            .execution_options(synchronize_session=False)
        )
        cancelled = result.rowcount

        cutoff = utcnow() - timedelta(hours=settings.idempotency_ttl_hours)
        await session.execute(
            delete(IdempotencyKey)
            .where(IdempotencyKey.created_at < cutoff)
            .execution_options(synchronize_session=False)
        )

    return expired, cancelled


async def run_sweeper() -> None:
    logger.info("reservation sweeper started (every %ss)", settings.sweep_interval_seconds)
    while True:
        try:
            expired, cancelled = await sweep_once()
            if expired or cancelled:
                logger.info(
                    "sweep: expired %d reservations, cancelled %d orders", expired, cancelled
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("sweeper iteration failed")
        await asyncio.sleep(settings.sweep_interval_seconds)
