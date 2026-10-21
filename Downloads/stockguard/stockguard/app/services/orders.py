"""
Order lifecycle:  reserve -> create order -> pay -> (PAID | PAYMENT_FAILED)

Lock order is always  order row -> reservation row -> product row, in every
code path (pay, cancel, sweeper). A single global ordering is what keeps these
concurrent flows deadlock-free.
"""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import InvalidState, NotFound, ReservationExpired
from app.models import Order, OrderStatus, Product, Reservation, ReservationStatus, utcnow
from app.services.inventory import confirm_reservation, release_reservation, restock


async def create_order(
    session: AsyncSession, *, user_id: str, reservation_id: uuid.UUID
) -> Order:
    reservation = (
        await session.execute(
            select(Reservation)
            .where(Reservation.id == reservation_id, Reservation.user_id == user_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if reservation is None:
        raise NotFound("reservation not found")
    if reservation.status != ReservationStatus.ACTIVE or reservation.expires_at <= utcnow():
        raise ReservationExpired("reservation is no longer active")

    already = (
        await session.execute(select(Order.id).where(Order.reservation_id == reservation_id))
    ).first()
    if already is not None:
        raise InvalidState("an order already exists for this reservation")

    product = (
        await session.execute(select(Product).where(Product.id == reservation.product_id))
    ).scalar_one()

    now = utcnow()
    order = Order(
        id=uuid.uuid4(),
        user_id=user_id,
        reservation_id=reservation.id,
        product_id=reservation.product_id,
        quantity=reservation.quantity,
        unit_price_cents=product.price_cents,
        total_cents=product.price_cents * reservation.quantity,
        status=OrderStatus.PENDING_PAYMENT,
        created_at=now,
        updated_at=now,
    )
    session.add(order)
    await session.flush()
    return order


async def _lock_order(session: AsyncSession, user_id: str, order_id: uuid.UUID) -> Order:
    order = (
        await session.execute(
            select(Order)
            .where(Order.id == order_id, Order.user_id == user_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if order is None:
        raise NotFound("order not found")
    return order


async def pay_order(
    session: AsyncSession, *, user_id: str, order_id: uuid.UUID, outcome: str
) -> Order:
    order = await _lock_order(session, user_id, order_id)
    if order.status != OrderStatus.PENDING_PAYMENT:
        raise InvalidState(f"order is {order.status}, expected {OrderStatus.PENDING_PAYMENT}")

    if outcome == "success":
        await confirm_reservation(session, order.reservation_id)  # raises if expired
        order.status = OrderStatus.PAID
    else:
        order.status = OrderStatus.PAYMENT_FAILED
        await release_reservation(
            session, order.reservation_id, new_status=ReservationStatus.RELEASED
        )
    order.updated_at = utcnow()
    await session.flush()
    return order


async def cancel_order(session: AsyncSession, *, user_id: str, order_id: uuid.UUID) -> Order:
    order = await _lock_order(session, user_id, order_id)
    if order.status == OrderStatus.PENDING_PAYMENT:
        await release_reservation(
            session, order.reservation_id, new_status=ReservationStatus.RELEASED
        )
    elif order.status == OrderStatus.PAID:
        # Refund (mocked): the sold units go back on the shelf.
        await restock(session, order.product_id, order.quantity)
    else:
        raise InvalidState(f"order is {order.status} and cannot be cancelled")
    order.status = OrderStatus.CANCELLED
    order.updated_at = utcnow()
    await session.flush()
    return order


async def get_owned_order(session: AsyncSession, user_id: str, order_id: uuid.UUID) -> Order:
    order = (
        await session.execute(select(Order).where(Order.id == order_id, Order.user_id == user_id))
    ).scalar_one_or_none()
    if order is None:
        raise NotFound("order not found")
    return order


async def get_owned_reservation(
    session: AsyncSession, user_id: str, reservation_id: uuid.UUID
) -> Reservation:
    reservation = (
        await session.execute(
            select(Reservation).where(
                Reservation.id == reservation_id, Reservation.user_id == user_id
            )
        )
    ).scalar_one_or_none()
    if reservation is None:
        raise NotFound("reservation not found")
    return reservation
