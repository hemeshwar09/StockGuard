"""
Stock reservation: the heart of the service.

Four interchangeable strategies claim stock for a reservation. All run inside
the caller's transaction (READ COMMITTED, Postgres default).

atomic       One conditional UPDATE: `... WHERE stock - reserved >= :qty`.
             The row lock Postgres takes for the UPDATE serialises writers and
             the predicate is re-checked after the lock is acquired, so the
             check and the write are a single atomic step. Default.
pessimistic  SELECT ... FOR UPDATE, check in Python, then write.
optimistic   Read a version, then UPDATE ... WHERE version = :seen; retry with
             jittered backoff if somebody else won.
naive        Read, check, write an absolute value from the stale read. This is
             the classic lost-update bug and exists ONLY so the load test can
             show what overselling looks like.
"""

import asyncio
import logging
import random
import uuid
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import NoReturn

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.errors import Contention, InsufficientStock, NotFound, ReservationExpired
from app.models import Product, Reservation, ReservationStatus, utcnow

logger = logging.getLogger(__name__)

Strategy = Callable[[AsyncSession, int, int], Awaitable[None]]


async def _missing_or_insufficient(session: AsyncSession, product_id: int) -> NoReturn:
    exists = (
        await session.execute(select(Product.id).where(Product.id == product_id))
    ).first()
    if exists is None:
        raise NotFound(f"product {product_id} not found")
    raise InsufficientStock(f"insufficient stock for product {product_id}")


async def _reserve_atomic(session: AsyncSession, product_id: int, quantity: int) -> None:
    stmt = (
        update(Product)
        .where(Product.id == product_id, Product.stock - Product.reserved >= quantity)
        .values(reserved=Product.reserved + quantity, version=Product.version + 1)
        .returning(Product.id)
        .execution_options(synchronize_session=False)
    )
    if (await session.execute(stmt)).first() is None:
        await _missing_or_insufficient(session, product_id)


async def _reserve_pessimistic(session: AsyncSession, product_id: int, quantity: int) -> None:
    product = (
        await session.execute(
            select(Product)
            .where(Product.id == product_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if product is None:
        raise NotFound(f"product {product_id} not found")
    if product.stock - product.reserved < quantity:
        raise InsufficientStock(f"insufficient stock for product {product_id}")
    product.reserved += quantity
    product.version += 1
    await session.flush()


async def _reserve_optimistic(session: AsyncSession, product_id: int, quantity: int) -> None:
    for attempt in range(settings.optimistic_max_retries):
        row = (
            await session.execute(
                select(Product.stock, Product.reserved, Product.version).where(
                    Product.id == product_id
                )
            )
        ).first()
        if row is None:
            raise NotFound(f"product {product_id} not found")
        if row.stock - row.reserved < quantity:
            raise InsufficientStock(f"insufficient stock for product {product_id}")

        result = await session.execute(
            update(Product)
            .where(Product.id == product_id, Product.version == row.version)
            .values(reserved=row.reserved + quantity, version=row.version + 1)
            .execution_options(synchronize_session=False)
        )
        if result.rowcount == 1:
            return
        # Lost the race: back off with jitter, then re-read the fresh row.
        await asyncio.sleep(random.uniform(0, 0.005 * (2 ** min(attempt, 5))))
    raise Contention("too much contention on this product, retry shortly")


async def _reserve_naive(session: AsyncSession, product_id: int, quantity: int) -> None:
    """DELIBERATELY BROKEN. Two buyers read reserved=0, both write reserved=1."""
    row = (
        await session.execute(
            select(Product.stock, Product.reserved).where(Product.id == product_id)
        )
    ).first()
    if row is None:
        raise NotFound(f"product {product_id} not found")
    if row.stock - row.reserved < quantity:
        raise InsufficientStock(f"insufficient stock for product {product_id}")
    await session.execute(
        update(Product)
        .where(Product.id == product_id)
        .values(reserved=row.reserved + quantity)  # absolute value from a stale read
        .execution_options(synchronize_session=False)
    )


STRATEGIES: dict[str, Strategy] = {
    "atomic": _reserve_atomic,
    "pessimistic": _reserve_pessimistic,
    "optimistic": _reserve_optimistic,
    "naive": _reserve_naive,
}


async def reserve_stock(
    session: AsyncSession, *, product_id: int, user_id: str, quantity: int
) -> Reservation:
    """Claim stock and create an ACTIVE reservation. Caller owns the transaction."""
    await STRATEGIES[settings.inventory_strategy](session, product_id, quantity)

    now = utcnow()
    reservation = Reservation(
        id=uuid.uuid4(),
        product_id=product_id,
        user_id=user_id,
        quantity=quantity,
        status=ReservationStatus.ACTIVE,
        expires_at=now + timedelta(seconds=settings.reservation_ttl_seconds),
        created_at=now,
    )
    session.add(reservation)
    await session.flush()
    return reservation


async def release_reservation(
    session: AsyncSession, reservation_id: uuid.UUID, *, new_status: str
) -> bool:
    """
    ACTIVE -> RELEASED/EXPIRED and give the units back. Returns False if the
    reservation was not ACTIVE. The guarded UPDATE makes this safe against
    concurrent cancel / expiry / pay: only one transition can win.
    """
    result = await session.execute(
        update(Reservation)
        .where(Reservation.id == reservation_id, Reservation.status == ReservationStatus.ACTIVE)
        .values(status=new_status)
        .returning(Reservation.product_id, Reservation.quantity)
        .execution_options(synchronize_session=False)
    )
    row = result.first()
    if row is None:
        return False
    await session.execute(
        update(Product)
        .where(Product.id == row.product_id)
        .values(reserved=Product.reserved - row.quantity, version=Product.version + 1)
        .execution_options(synchronize_session=False)
    )
    return True


async def confirm_reservation(session: AsyncSession, reservation_id: uuid.UUID) -> None:
    """ACTIVE (and unexpired) -> CONFIRMED: units leave `reserved` and `stock` together."""
    result = await session.execute(
        update(Reservation)
        .where(
            Reservation.id == reservation_id,
            Reservation.status == ReservationStatus.ACTIVE,
            Reservation.expires_at > utcnow(),
        )
        .values(status=ReservationStatus.CONFIRMED)
        .returning(Reservation.product_id, Reservation.quantity)
        .execution_options(synchronize_session=False)
    )
    row = result.first()
    if row is None:
        raise ReservationExpired("reservation is no longer active (expired or already used)")
    await session.execute(
        update(Product)
        .where(Product.id == row.product_id)
        .values(
            stock=Product.stock - row.quantity,
            reserved=Product.reserved - row.quantity,
            version=Product.version + 1,
        )
        .execution_options(synchronize_session=False)
    )


async def restock(session: AsyncSession, product_id: int, quantity: int) -> None:
    result = await session.execute(
        update(Product)
        .where(Product.id == product_id)
        .values(stock=Product.stock + quantity, version=Product.version + 1)
        .returning(Product.id)
        .execution_options(synchronize_session=False)
    )
    if result.first() is None:
        raise NotFound(f"product {product_id} not found")
