from tests.helpers import ADMIN, idem, make_product, reserve


async def test_missing_user_header_is_401(client):
    response = await client.post("/reservations", json={"product_id": 1, "quantity": 1})
    assert response.status_code == 401


async def test_admin_endpoints_require_key(client):
    body = {"sku": "nope", "name": "x", "price_cents": 1, "stock": 1}
    assert (await client.post("/products", json=body)).status_code == 403
    assert (
        await client.post("/products", json=body, headers={"X-Admin-Key": "wrong"})
    ).status_code == 403
    product_id = await make_product(client, stock=1)
    assert (await client.get(f"/products/{product_id}/audit")).status_code == 403
    assert (await client.get(f"/products/{product_id}/audit", headers=ADMIN)).status_code == 200


async def test_duplicate_sku_is_409(client):
    body = {"sku": "dup-sku-test", "name": "x", "price_cents": 1, "stock": 1}
    first = await client.post("/products", json=body, headers=ADMIN)
    second = await client.post("/products", json=body, headers=ADMIN)
    assert first.status_code in (201, 409)  # 409 if a previous run left it behind
    assert second.status_code == 409


async def test_users_cannot_touch_each_others_resources(client):
    product_id = await make_product(client, stock=2)
    held = (await reserve(client, product_id, 1, user_id="alice")).json()

    assert (
        await client.get(f"/reservations/{held['id']}", headers={"X-User-Id": "mallory"})
    ).status_code == 404

    stolen = await client.post(
        "/orders", json={"reservation_id": held["id"]}, headers=idem("mallory")
    )
    assert stolen.status_code == 404

    mine = await client.post("/orders", json={"reservation_id": held["id"]}, headers=idem("alice"))
    assert mine.status_code == 201
    assert (
        await client.get(f"/orders/{mine.json()['id']}", headers={"X-User-Id": "mallory"})
    ).status_code == 404


async def test_health_endpoints(client):
    assert (await client.get("/health/live")).status_code == 200
    ready = await client.get("/health")
    assert ready.status_code == 200 and ready.json()["status"] == "ok"
