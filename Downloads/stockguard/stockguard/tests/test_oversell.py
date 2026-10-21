import asyncio
from collections import Counter

import pytest

from app.config import settings
from tests.helpers import ADMIN, get_audit, get_product, make_product, reserve


@pytest.mark.parametrize("strategy", ["atomic", "pessimistic", "optimistic"])
async def test_never_oversells_under_concurrency(client, monkeypatch, strategy):
    monkeypatch.setattr(settings, "inventory_strategy", strategy)
    monkeypatch.setattr(settings, "optimistic_max_retries", 200)

    stock, buyers = 20, 100
    product_id = await make_product(client, stock=stock)

    responses = await asyncio.gather(
        *(reserve(client, product_id, 1, user_id=f"buyer-{i}") for i in range(buyers))
    )
    codes = Counter(r.status_code for r in responses)

    assert codes[201] == stock, codes
    assert codes[409] == buyers - stock, codes
    assert set(codes) <= {201, 409}, codes

    audit = await get_audit(client, product_id)
    assert audit["consistent"] and not audit["oversold"]
    assert audit["reserved"] == stock
    assert audit["active_reservation_qty"] == stock
    assert audit["available"] == 0


async def test_multi_unit_reservations_respect_stock(client):
    product_id = await make_product(client, stock=10)
    responses = await asyncio.gather(
        *(reserve(client, product_id, 3, user_id=f"b{i}") for i in range(8))
    )
    successes = sum(1 for r in responses if r.status_code == 201)
    assert successes == 3  # 3 * 3 = 9 <= 10, a fourth would need 12

    audit = await get_audit(client, product_id)
    assert audit["reserved"] == 9 and audit["consistent"] and not audit["oversold"]


async def test_unknown_product_is_404_and_bad_quantity_is_422(client):
    assert (await reserve(client, 99_999_999)).status_code == 404
    product_id = await make_product(client, stock=1)
    assert (await reserve(client, product_id, 0)).status_code == 422


async def test_release_returns_units(client):
    product_id = await make_product(client, stock=2)
    created = await reserve(client, product_id, 2)
    assert created.status_code == 201
    assert (await get_product(client, product_id))["available"] == 0

    released = await client.post(
        f"/reservations/{created.json()['id']}/release", headers={"X-User-Id": "u1"}
    )
    assert released.status_code == 200 and released.json()["status"] == "RELEASED"
    assert (await get_product(client, product_id))["available"] == 2

    again = await client.post(
        f"/reservations/{created.json()['id']}/release", headers={"X-User-Id": "u1"}
    )
    assert again.status_code == 409


async def test_restock_makes_units_available(client):
    product_id = await make_product(client, stock=1)
    assert (await reserve(client, product_id)).status_code == 201
    assert (await reserve(client, product_id)).status_code == 409

    restocked = await client.post(
        f"/products/{product_id}/restock", json={"quantity": 5}, headers=ADMIN
    )
    assert restocked.status_code == 200 and restocked.json()["available"] == 5
