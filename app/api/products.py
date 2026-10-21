from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.api.deps import Session, require_admin
from app.errors import Conflict, NotFound
from app.models import Product, Reservation, ReservationStatus
from app.schemas import AuditOut, ProductCreate, ProductOut, RestockRequest
from app.services.inventory import restock

router = APIRouter(prefix="/products", tags=["products"])


@router.post(
    "",
    response_model=ProductOut,
    status_code=201,
    dependencies=[Depends(require_admin)],
)
async def create_product(body: ProductCreate, session: Session) -> Product:
    product = Product(
        sku=body.sku,
        name=body.name,
        price_cents=body.price_cents,
        stock=body.stock,
        reserved=0,
        version=1,
    )
    try:
        async with session.begin():
            session.add(product)
            await session.flush()
    except IntegrityError:
        raise Conflict(f"a product with sku '{body.sku}' already exists") from None
    return product


@router.get("", response_model=list[ProductOut])
async def list_products(
    session: Session,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> list[Product]:
    rows = await session.execute(select(Product).order_by(Product.id).limit(limit).offset(offset))
    return list(rows.scalars().all())


@router.get("/{product_id}", response_model=ProductOut)
async def get_product(product_id: int, session: Session) -> Product:
    product = (
        await session.execute(select(Product).where(Product.id == product_id))
    ).scalar_one_or_none()
    if product is None:
        raise NotFound(f"product {product_id} not found")
    return product


@router.post(
    "/{product_id}/restock",
    response_model=ProductOut,
    dependencies=[Depends(require_admin)],
)
async def restock_product(product_id: int, body: RestockRequest, session: Session) -> Product:
    async with session.begin():
        await restock(session, product_id, body.quantity)
        product = (
            await session.execute(
                select(Product)
                .where(Product.id == product_id)
                .execution_options(populate_existing=True)
            )
        ).scalar_one()
    return product


@router.get(
    "/{product_id}/audit",
    response_model=AuditOut,
    dependencies=[Depends(require_admin)],
)
async def audit_product(product_id: int, session: Session) -> AuditOut:
    """
    Invariant check used by the load test. One statement => one consistent
    snapshot of the product row and its ACTIVE reservations.
    """
    active_qty = (
        select(func.coalesce(func.sum(Reservation.quantity), 0))
        .where(
            Reservation.product_id == product_id,
            Reservation.status == ReservationStatus.ACTIVE,
        )
        .scalar_subquery()
    )
    row = (
        await session.execute(
            select(Product.stock, Product.reserved, active_qty.label("active_qty")).where(
                Product.id == product_id
            )
        )
    ).first()
    if row is None:
        raise NotFound(f"product {product_id} not found")

    stock, reserved, active = int(row.stock), int(row.reserved), int(row.active_qty)
    return AuditOut(
        product_id=product_id,
        stock=stock,
        reserved=reserved,
        available=stock - reserved,
        active_reservation_qty=active,
        consistent=(reserved == active and 0 <= reserved <= stock),
        oversold=active > stock,
    )
