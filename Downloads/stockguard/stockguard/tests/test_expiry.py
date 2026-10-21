import uuid

from sqlalchemy import text

from app.config import settings
from app.db import SessionLocal
from app.services.sweeper import sweep_once
from tests.helpers import get_product, idem, make_product, reserve, user


async def _force_expire(reservation_id: str) -> None:
    async with SessionLocal() as session, session.begin():
        await session.execute(
            text("UPDATE reservations SET expires_at = now() - interval '1 minute' WHERE id = :id"),
            {"id": uuid.UUID(reservation_id)},
        )


async def test_expired_reservation_cannot_be_paid_and_sweeper_restores_stock(client):
    product_id = await make_product(client, stock=3)
    held = (await reserve(client, product_id, 2)).json()
    placed = await client.post(
        "/orders", json={"reservation_id": held["id"]}, headers=idem("u1")
    )
    assert placed.status_code == 201
    order_id = placed.json()["id"]
    assert (await get_product(client, product_id))["available"] == 1

    await _force_expire(held["id"])

    pay = await client.post(f"/orders/{order_id}/pay", json={}, headers=idem("u1"))
    assert pay.status_code == 409 and pay.json()["error"] == "reservation_expired"

    expired, cancelled = await sweep_once()
    assert expired >= 1 and cancelled >= 1

    product = await get_product(client, product_id)
    assert (product["stock"], product["reserved"], product["available"]) == (3, 0, 3)
    order = await client.get(f"/orders/{order_id}", headers=user("u1"))
    assert order.json()["status"] == "CANCELLED"


async def test_sweeper_is_idempotent(client):
    product_id = await make_product(client, stock=2)
    held = (await reserve(client, product_id, 2)).json()
    await _force_expire(held["id"])

    await sweep_once()
    await sweep_once()  # second pass must not double-release

    product = await get_product(client, product_id)
    assert (product["stock"], product["reserved"], product["available"]) == (2, 0, 2)


async def test_order_cannot_be_created_from_expired_reservation(client, monkeypatch):
    monkeypatch.setattr(settings, "reservation_ttl_seconds", -5)
    product_id = await make_product(client, stock=2)
    held = (await reserve(client, product_id)).json()

    placed = await client.post(
        "/orders", json={"reservation_id": held["id"]}, headers=idem("u1")
    )
    assert placed.status_code == 409 and placed.json()["error"] == "reservation_expired"
