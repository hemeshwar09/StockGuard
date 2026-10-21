import asyncio

from tests.helpers import idem, make_product, reserve


async def test_concurrent_duplicates_create_one_order(client):
    product_id = await make_product(client, stock=3)
    held = (await reserve(client, product_id)).json()
    headers = idem("u1", "same-key-12345")
    body = {"reservation_id": held["id"]}

    first, second = await asyncio.gather(
        client.post("/orders", json=body, headers=headers),
        client.post("/orders", json=body, headers=headers),
    )
    assert first.status_code == second.status_code == 201
    assert first.json()["id"] == second.json()["id"]
    assert {first.headers.get("idempotent-replayed"), second.headers.get("idempotent-replayed")} == {
        None,
        "true",
    }


async def test_sequential_replay_returns_stored_response(client):
    product_id = await make_product(client, stock=3)
    held = (await reserve(client, product_id)).json()
    headers = idem("u1", "replay-key-12345")
    body = {"reservation_id": held["id"]}

    first = await client.post("/orders", json=body, headers=headers)
    replay = await client.post("/orders", json=body, headers=headers)
    assert first.status_code == replay.status_code == 201
    assert first.json() == replay.json()
    assert replay.headers.get("idempotent-replayed") == "true"


async def test_same_key_different_request_is_rejected(client):
    product_id = await make_product(client, stock=3)
    first_hold = (await reserve(client, product_id)).json()
    second_hold = (await reserve(client, product_id)).json()
    headers = idem("u1", "reused-key-12345")

    ok = await client.post("/orders", json={"reservation_id": first_hold["id"]}, headers=headers)
    assert ok.status_code == 201
    clash = await client.post(
        "/orders", json={"reservation_id": second_hold["id"]}, headers=headers
    )
    assert clash.status_code == 422
    assert clash.json()["error"] == "idempotency_key_reuse"


async def test_failed_attempt_can_be_retried_with_same_key(client):
    product_id = await make_product(client, stock=3)
    headers = idem("u1", "retry-after-fail-1")

    missing = await client.post(
        "/orders", json={"reservation_id": "00000000-0000-0000-0000-000000000000"}, headers=headers
    )
    assert missing.status_code == 404  # failure is not cached

    held = (await reserve(client, product_id)).json()
    # different body now, so this is a *new* request under a reused key -> must be allowed
    # because the failed attempt rolled back and stored nothing.
    retry = await client.post("/orders", json={"reservation_id": held["id"]}, headers=headers)
    assert retry.status_code == 201


async def test_idempotency_key_is_required(client):
    product_id = await make_product(client, stock=1)
    held = (await reserve(client, product_id)).json()
    response = await client.post(
        "/orders", json={"reservation_id": held["id"]}, headers={"X-User-Id": "u1"}
    )
    assert response.status_code == 422
