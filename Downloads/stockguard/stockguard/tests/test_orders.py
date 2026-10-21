import asyncio

from tests.helpers import get_product, idem, make_product, reserve, user


async def _order_for(client, product_id, quantity=1, user_id="u1"):
    held = await reserve(client, product_id, quantity, user_id)
    assert held.status_code == 201, held.text
    placed = await client.post(
        "/orders", json={"reservation_id": held.json()["id"]}, headers=idem(user_id)
    )
    assert placed.status_code == 201, placed.text
    return held.json(), placed.json()


async def test_full_purchase_flow(client):
    product_id = await make_product(client, stock=5, price_cents=2500)
    _, order = await _order_for(client, product_id, quantity=2)
    assert order["status"] == "PENDING_PAYMENT"
    assert order["total_cents"] == 5000

    paid = await client.post(
        f"/orders/{order['id']}/pay", json={"outcome": "success"}, headers=idem("u1")
    )
    assert paid.status_code == 200 and paid.json()["status"] == "PAID"

    product = await get_product(client, product_id)
    assert (product["stock"], product["reserved"], product["available"]) == (3, 0, 3)


async def test_payment_failure_releases_stock(client):
    product_id = await make_product(client, stock=5)
    _, order = await _order_for(client, product_id, quantity=2)

    failed = await client.post(
        f"/orders/{order['id']}/pay", json={"outcome": "failure"}, headers=idem("u1")
    )
    assert failed.status_code == 200 and failed.json()["status"] == "PAYMENT_FAILED"

    product = await get_product(client, product_id)
    assert (product["stock"], product["reserved"], product["available"]) == (5, 0, 5)


async def test_concurrent_payments_with_different_keys_charge_once(client):
    product_id = await make_product(client, stock=5)
    _, order = await _order_for(client, product_id, quantity=2)

    first, second = await asyncio.gather(
        client.post(f"/orders/{order['id']}/pay", json={}, headers=idem("u1")),
        client.post(f"/orders/{order['id']}/pay", json={}, headers=idem("u1")),
    )
    assert sorted([first.status_code, second.status_code]) == [200, 409]

    product = await get_product(client, product_id)
    assert (product["stock"], product["reserved"]) == (3, 0)  # decremented exactly once


async def test_cancel_pending_releases_and_cancel_paid_restocks(client):
    product_id = await make_product(client, stock=4)

    _, pending = await _order_for(client, product_id, quantity=1)
    cancelled = await client.post(f"/orders/{pending['id']}/cancel", headers=user("u1"))
    assert cancelled.status_code == 200 and cancelled.json()["status"] == "CANCELLED"
    assert (await get_product(client, product_id))["available"] == 4

    _, paid_order = await _order_for(client, product_id, quantity=2)
    await client.post(f"/orders/{paid_order['id']}/pay", json={}, headers=idem("u1"))
    assert (await get_product(client, product_id))["stock"] == 2

    refunded = await client.post(f"/orders/{paid_order['id']}/cancel", headers=user("u1"))
    assert refunded.status_code == 200
    assert (await get_product(client, product_id))["stock"] == 4

    again = await client.post(f"/orders/{paid_order['id']}/cancel", headers=user("u1"))
    assert again.status_code == 409


async def test_one_reservation_backs_one_order(client):
    product_id = await make_product(client, stock=2)
    held, _ = await _order_for(client, product_id)
    second = await client.post(
        "/orders", json={"reservation_id": held["id"]}, headers=idem("u1")
    )
    assert second.status_code == 409
