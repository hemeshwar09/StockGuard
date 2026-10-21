import uuid
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.api.deps import CurrentUser, IdempotencyKeyHeader, Session
from app.models import Order
from app.schemas import OrderCreate, OrderOut, PayRequest
from app.services import orders as order_service
from app.services.idempotency import compute_request_hash, execute_idempotent

router = APIRouter(prefix="/orders", tags=["orders"])


def _respond(status_code: int, payload: dict[str, Any], replayed: bool) -> JSONResponse:
    headers = {"Idempotent-Replayed": "true"} if replayed else None
    return JSONResponse(status_code=status_code, content=payload, headers=headers)


def _serialize(order: Order) -> dict[str, Any]:
    return OrderOut.model_validate(order).model_dump(mode="json")


@router.post("", response_model=OrderOut, status_code=201)
async def create_order(
    request: Request,
    body: OrderCreate,
    user_id: CurrentUser,
    session: Session,
    idempotency_key: IdempotencyKeyHeader,
) -> JSONResponse:
    """Turn an ACTIVE reservation into a PENDING_PAYMENT order. Requires Idempotency-Key."""

    async def handler() -> dict[str, Any]:
        order = await order_service.create_order(
            session, user_id=user_id, reservation_id=body.reservation_id
        )
        return _serialize(order)

    status_code, payload, replayed = await execute_idempotent(
        session,
        user_id=user_id,
        key=idempotency_key,
        request_hash=compute_request_hash(
            request.method, request.url.path, body.model_dump(mode="json")
        ),
        success_status=201,
        handler=handler,
    )
    return _respond(status_code, payload, replayed)


@router.get("/{order_id}", response_model=OrderOut)
async def get_order(order_id: uuid.UUID, user_id: CurrentUser, session: Session) -> Order:
    return await order_service.get_owned_order(session, user_id, order_id)


@router.post("/{order_id}/pay", response_model=OrderOut)
async def pay_order(
    order_id: uuid.UUID,
    request: Request,
    user_id: CurrentUser,
    session: Session,
    idempotency_key: IdempotencyKeyHeader,
    body: PayRequest | None = None,
) -> JSONResponse:
    """
    Settle the order. `outcome` simulates the payment gateway result.
    success -> stock permanently decremented; failure -> units released.
    """
    outcome = body.outcome if body else "success"

    async def handler() -> dict[str, Any]:
        order = await order_service.pay_order(
            session, user_id=user_id, order_id=order_id, outcome=outcome
        )
        return _serialize(order)

    status_code, payload, replayed = await execute_idempotent(
        session,
        user_id=user_id,
        key=idempotency_key,
        request_hash=compute_request_hash(request.method, request.url.path, {"outcome": outcome}),
        success_status=200,
        handler=handler,
    )
    return _respond(status_code, payload, replayed)


@router.post("/{order_id}/cancel", response_model=OrderOut)
async def cancel_order(order_id: uuid.UUID, user_id: CurrentUser, session: Session) -> Order:
    """Cancel a pending order (releases stock) or a paid one (mock refund + restock)."""
    async with session.begin():
        order = await order_service.cancel_order(session, user_id=user_id, order_id=order_id)
    return order
