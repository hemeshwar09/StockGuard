import uuid

from fastapi import APIRouter

from app.api.deps import CurrentUser, Session
from app.errors import InvalidState
from app.models import Reservation, ReservationStatus
from app.schemas import ReservationCreate, ReservationOut
from app.services.inventory import release_reservation, reserve_stock
from app.services.orders import get_owned_reservation

router = APIRouter(prefix="/reservations", tags=["reservations"])


@router.post("", response_model=ReservationOut, status_code=201)
async def create_reservation(
    body: ReservationCreate, user_id: CurrentUser, session: Session
) -> Reservation:
    """Hold stock for the TTL window. 409 insufficient_stock when sold out."""
    async with session.begin():
        reservation = await reserve_stock(
            session, product_id=body.product_id, user_id=user_id, quantity=body.quantity
        )
    return reservation


@router.get("/{reservation_id}", response_model=ReservationOut)
async def get_reservation(
    reservation_id: uuid.UUID, user_id: CurrentUser, session: Session
) -> Reservation:
    return await get_owned_reservation(session, user_id, reservation_id)


@router.post("/{reservation_id}/release", response_model=ReservationOut)
async def release(
    reservation_id: uuid.UUID, user_id: CurrentUser, session: Session
) -> Reservation:
    """Give the held units back (e.g. the shopper removed the item from the cart)."""
    async with session.begin():
        reservation = await get_owned_reservation(session, user_id, reservation_id)
        released = await release_reservation(
            session, reservation_id, new_status=ReservationStatus.RELEASED
        )
        if not released:
            raise InvalidState(f"reservation is {reservation.status}, not ACTIVE")
        reservation.status = ReservationStatus.RELEASED
    return reservation
