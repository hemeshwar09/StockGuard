#!/usr/bin/env python
"""
Concurrent-buyer load test + invariant checker for StockGuard.

Creates a product with --stock units, unleashes --buyers shoppers (at most
--concurrency in flight) that each run reserve -> order -> pay, then asks the
server's audit endpoint whether the books balance.

Run it once per INVENTORY_STRATEGY (restart the API in between) and compare:

    python scripts/loadtest.py --stock 100 --buyers 500 --concurrency 100

Exit code is 0 only if every invariant held.
"""

import argparse
import asyncio
import json
import os
import random
import sys
import time
import uuid
from collections import Counter, defaultdict
from typing import Any

import httpx


def percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(pct / 100 * (len(ordered) - 1))))
    return ordered[index]


class Stats:
    def __init__(self) -> None:
        self.latencies: dict[str, list[float]] = defaultdict(list)
        self.codes: Counter[tuple[str, int]] = Counter()
        self.transport_errors: Counter[str] = Counter()
        self.paid_buyers = 0
        self.idempotency_checked = 0
        self.idempotency_violations = 0


async def timed(stats: Stats, name: str, coro: Any) -> httpx.Response | None:
    started = time.perf_counter()
    try:
        response = await coro
    except httpx.HTTPError:
        stats.transport_errors[name] += 1
        return None
    stats.latencies[name].append((time.perf_counter() - started) * 1000)
    stats.codes[(name, response.status_code)] += 1
    return response


async def buyer(
    index: int,
    client: httpx.AsyncClient,
    sem: asyncio.Semaphore,
    stats: Stats,
    product_id: int,
    quantity: int,
    duplicate_rate: float,
) -> None:
    user = {"X-User-Id": f"loadtest-user-{index}"}
    async with sem:
        reserve = await timed(
            stats,
            "reserve",
            client.post(
                "/reservations",
                json={"product_id": product_id, "quantity": quantity},
                headers=user,
            ),
        )
        if reserve is None or reserve.status_code != 201:
            return  # sold out (409) or error: this buyer is done
        reservation_id = reserve.json()["id"]

        # --- create order (optionally fired twice at once with the same key)
        order_headers = {**user, "Idempotency-Key": str(uuid.uuid4())}
        order_req = lambda: client.post(  # noqa: E731
            "/orders", json={"reservation_id": reservation_id}, headers=order_headers
        )
        if random.random() < duplicate_rate:
            first, second = await asyncio.gather(
                timed(stats, "order", order_req()), timed(stats, "order", order_req())
            )
            order = first
            if first is not None and second is not None:
                stats.idempotency_checked += 1
                same = (
                    first.status_code == second.status_code == 201
                    and first.json()["id"] == second.json()["id"]
                )
                if not same:
                    stats.idempotency_violations += 1
        else:
            order = await timed(stats, "order", order_req())
        if order is None or order.status_code != 201:
            return
        order_id = order.json()["id"]

        # --- pay (also sometimes duplicated)
        pay_headers = {**user, "Idempotency-Key": str(uuid.uuid4())}
        pay_req = lambda: client.post(  # noqa: E731
            f"/orders/{order_id}/pay", json={"outcome": "success"}, headers=pay_headers
        )
        if random.random() < duplicate_rate:
            first, second = await asyncio.gather(
                timed(stats, "pay", pay_req()), timed(stats, "pay", pay_req())
            )
            paid = first
            if first is not None and second is not None:
                stats.idempotency_checked += 1
                same = (
                    first.status_code == second.status_code == 200
                    and first.json()["status"] == second.json()["status"] == "PAID"
                )
                if not same:
                    stats.idempotency_violations += 1
        else:
            paid = await timed(stats, "pay", pay_req())
        if paid is not None and paid.status_code == 200 and paid.json()["status"] == "PAID":
            stats.paid_buyers += 1


async def run(args: argparse.Namespace) -> int:
    limits = httpx.Limits(
        max_connections=args.concurrency * 2, max_keepalive_connections=args.concurrency
    )
    async with httpx.AsyncClient(base_url=args.base_url, timeout=60, limits=limits) as client:
        health = (await client.get("/health")).json()
        strategy = health.get("inventory_strategy", "unknown")
        admin = {"X-Admin-Key": args.admin_key}

        created = await client.post(
            "/products",
            json={
                "sku": f"loadtest-{uuid.uuid4().hex[:10]}",
                "name": "Load test item",
                "price_cents": args.price_cents,
                "stock": args.stock,
            },
            headers=admin,
        )
        created.raise_for_status()
        product_id = created.json()["id"]

        stats = Stats()
        sem = asyncio.Semaphore(args.concurrency)
        started = time.perf_counter()
        await asyncio.gather(
            *(
                buyer(i, client, sem, stats, product_id, args.quantity, args.duplicate_rate)
                for i in range(args.buyers)
            )
        )
        elapsed = time.perf_counter() - started

        audit = (await client.get(f"/products/{product_id}/audit", headers=admin)).json()
        product = (await client.get(f"/products/{product_id}")).json()

    total_requests = sum(stats.codes.values())
    units_sold = stats.paid_buyers * args.quantity
    stock_delta = args.stock - product["stock"]

    checks = {
        "no_oversell (active reservations <= stock)": not audit["oversold"],
        "books_consistent (reserved == sum of active reservations)": audit["consistent"],
        "units_sold_match_stock_decrease": units_sold == stock_delta,
        "never_sold_more_than_stock": units_sold <= args.stock,
        "idempotent_retries_replayed": stats.idempotency_violations == 0,
    }
    passed = all(checks.values())

    print(f"\n=== StockGuard load test  (strategy: {strategy}) ===")
    print(f"stock={args.stock}  buyers={args.buyers}  concurrency={args.concurrency}  "
          f"qty/buyer={args.quantity}  duplicate_rate={args.duplicate_rate}")
    print(f"wall time: {elapsed:.2f}s   requests: {total_requests}   "
          f"throughput: {total_requests / elapsed:.0f} req/s")
    print("\nlatency (ms)        p50     p95     p99     count")
    for name in ("reserve", "order", "pay"):
        values = stats.latencies.get(name, [])
        print(f"  {name:<14}{percentile(values, 50):7.1f}{percentile(values, 95):8.1f}"
              f"{percentile(values, 99):8.1f}{len(values):9d}")
    print("\nstatus codes:")
    for (name, code), count in sorted(stats.codes.items()):
        print(f"  {name:<8}{code}  x{count}")
    if stats.transport_errors:
        print(f"transport errors: {dict(stats.transport_errors)}")
    print(f"\nunits sold (PAID): {units_sold}   stock decrease: {stock_delta}   "
          f"final stock: {product['stock']}   reserved: {product['reserved']}")
    print(f"idempotency duplicate pairs checked: {stats.idempotency_checked}  "
          f"violations: {stats.idempotency_violations}")
    print("\ninvariants:")
    for label, ok in checks.items():
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    print(f"\nRESULT: {'PASS' if passed else 'FAIL'}\n")

    if args.output:
        report = {
            "strategy": strategy,
            "params": {k: v for k, v in vars(args).items() if k != "admin_key"},
            "wall_seconds": round(elapsed, 3),
            "requests": total_requests,
            "req_per_sec": round(total_requests / elapsed, 1),
            "latency_ms": {
                name: {
                    "p50": round(percentile(v, 50), 2),
                    "p95": round(percentile(v, 95), 2),
                    "p99": round(percentile(v, 99), 2),
                    "count": len(v),
                }
                for name, v in stats.latencies.items()
            },
            "status_codes": {f"{n}:{c}": k for (n, c), k in sorted(stats.codes.items())},
            "units_sold": units_sold,
            "stock_decrease": stock_delta,
            "audit": audit,
            "checks": checks,
            "passed": passed,
        }
        with open(args.output, "w") as fh:
            json.dump(report, fh, indent=2)
        print(f"wrote {args.output}")
    return 0 if passed else 1


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--admin-key", default=os.environ.get("ADMIN_API_KEY", "dev-admin-key"))
    parser.add_argument("--stock", type=int, default=100)
    parser.add_argument("--buyers", type=int, default=500)
    parser.add_argument("--concurrency", type=int, default=100)
    parser.add_argument("--quantity", type=int, default=1)
    parser.add_argument("--price-cents", type=int, default=4999)
    parser.add_argument("--duplicate-rate", type=float, default=0.1,
                        help="fraction of order/pay calls sent twice concurrently with one key")
    parser.add_argument("--output", default=None, help="write a JSON report here")
    sys.exit(asyncio.run(run(parser.parse_args())))


if __name__ == "__main__":
    main()
