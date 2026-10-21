import uuid

import httpx

ADMIN = {"X-Admin-Key": "test-admin-key"}


def user(user_id: str) -> dict[str, str]:
    return {"X-User-Id": user_id}


def idem(user_id: str, key: str | None = None) -> dict[str, str]:
    return {"X-User-Id": user_id, "Idempotency-Key": key or f"key-{uuid.uuid4().hex}"}


async def make_product(client: httpx.AsyncClient, *, stock: int, price_cents: int = 1000) -> int:
    response = await client.post(
        "/products",
        json={
            "sku": f"sku-{uuid.uuid4().hex[:12]}",
            "name": "Test product",
            "price_cents": price_cents,
            "stock": stock,
        },
        headers=ADMIN,
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def reserve(
    client: httpx.AsyncClient, product_id: int, quantity: int = 1, user_id: str = "u1"
) -> httpx.Response:
    return await client.post(
        "/reservations",
        json={"product_id": product_id, "quantity": quantity},
        headers=user(user_id),
    )


async def get_product(client: httpx.AsyncClient, product_id: int) -> dict:
    response = await client.get(f"/products/{product_id}")
    assert response.status_code == 200, response.text
    return response.json()


async def get_audit(client: httpx.AsyncClient, product_id: int) -> dict:
    response = await client.get(f"/products/{product_id}/audit", headers=ADMIN)
    assert response.status_code == 200, response.text
    return response.json()
