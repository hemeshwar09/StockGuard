# StockGuard

An inventory reservation and order service that **never oversells**, even when hundreds of shoppers race for the last units. Built with FastAPI, PostgreSQL and SQLAlchemy (async), containerised and ready to deploy.

The problem it solves is the one every e-commerce backend hits on launch day: *N buyers, M < N units, and nobody may buy a unit that isn't there.*

## How it works

```mermaid
sequenceDiagram
    participant C as Client
    participant A as API (FastAPI)
    participant P as PostgreSQL
    C->>A: POST /reservations {product, qty}
    A->>P: UPDATE products SET reserved = reserved + qty<br/>WHERE stock - reserved >= qty
    P-->>A: 1 row (held) / 0 rows (sold out)
    A-->>C: 201 reservation (expires in TTL) / 409 insufficient_stock
    C->>A: POST /orders {reservation_id}  + Idempotency-Key
    A->>P: insert order (PENDING_PAYMENT) + idempotency record, one transaction
    C->>A: POST /orders/{id}/pay  + Idempotency-Key
    A->>P: reservation ACTIVE -> CONFIRMED, stock -= qty, reserved -= qty
    A-->>C: 200 order PAID
    Note over A,P: Background sweeper expires unpaid reservations<br/>and returns the units to stock
```

### Guarantees and how they're enforced

| Guarantee | Mechanism |
|---|---|
| No overselling | Claim stock with a single conditional `UPDATE ... WHERE stock - reserved >= qty` (check and write are one atomic step) |
| Safety net if app code is wrong | `CHECK (reserved <= stock)`, `CHECK (stock >= 0)` constraints: the DB refuses to commit an oversold state |
| Retries never double-charge | `Idempotency-Key` on order creation and payment; the key row and the business write commit in the same transaction |
| Abandoned carts free their stock | Reservations carry a TTL; a background sweeper expires them with guarded state transitions |
| Cancel / expire / pay can't double-release | Every state change is `UPDATE ... WHERE status = 'ACTIVE'`, so exactly one transition wins |
| No deadlocks between those flows | One global lock order everywhere: order row, then reservation row, then product row |
| Safe with multiple replicas | All sweeper work is idempotent and guarded; schema creation is serialised with an advisory lock |

### Four reservation strategies (switch with `INVENTORY_STRATEGY`)

| Strategy | Idea | Trade-off |
|---|---|---|
| `atomic` (default) | One conditional UPDATE | Fewest round trips, no retry loop. Hot rows still serialise on the row lock |
| `pessimistic` | `SELECT ... FOR UPDATE`, check, write | Easy to reason about; holds the lock across two statements |
| `optimistic` | Version column + retry with jittered backoff | No lock held while reading; collapses into retries under heavy contention (returns 503 + `Retry-After` after `OPTIMISTIC_MAX_RETRIES`) |
| `naive` | Read, check, write an absolute value | **Intentionally broken** (lost update). Exists so the load test can show what overselling looks like. Never use in production |

## Quick start

Requires Docker.

```bash
docker compose up --build -d        # API on http://localhost:8000, Postgres on :5432
open http://localhost:8000/docs     # interactive API docs
```

Try the flow:

```bash
ADMIN='X-Admin-Key: dev-admin-key'

# 1. create a product with 3 units (admin)
curl -s -X POST localhost:8000/products -H "$ADMIN" -H 'Content-Type: application/json' \
  -d '{"sku":"MUG-001","name":"Coffee mug","price_cents":1299,"stock":3}'

# 2. a shopper reserves 1 unit
curl -s -X POST localhost:8000/reservations -H 'X-User-Id: alice' \
  -H 'Content-Type: application/json' -d '{"product_id":1,"quantity":1}'

# 3. turn it into an order (Idempotency-Key is required)
curl -s -X POST localhost:8000/orders -H 'X-User-Id: alice' -H 'Idempotency-Key: order-0001-abc' \
  -H 'Content-Type: application/json' -d '{"reservation_id":"<id from step 2>"}'

# 4. pay (mock gateway: "success" or "failure")
curl -s -X POST localhost:8000/orders/<order id>/pay -H 'X-User-Id: alice' \
  -H 'Idempotency-Key: pay-0001-abc' -H 'Content-Type: application/json' -d '{"outcome":"success"}'
```

## API

| Method & path | Auth | Purpose |
|---|---|---|
| `POST /products` | admin | Create a product with initial stock |
| `GET /products`, `GET /products/{id}` | none | List / read (`available = stock - reserved`) |
| `POST /products/{id}/restock` | admin | Add stock |
| `GET /products/{id}/audit` | admin | Invariant check: does `reserved` equal the sum of ACTIVE reservations? Any oversell? |
| `POST /reservations` | user | Hold stock for the TTL. `409 insufficient_stock` when sold out |
| `GET /reservations/{id}`, `POST /reservations/{id}/release` | user | Inspect / give units back |
| `POST /orders` | user + `Idempotency-Key` | Create a PENDING_PAYMENT order from a reservation |
| `GET /orders/{id}` | user | Read own order |
| `POST /orders/{id}/pay` | user + `Idempotency-Key` | Settle: `success` decrements stock, `failure` releases it |
| `POST /orders/{id}/cancel` | user | Cancel pending (release) or paid (mock refund + restock) |
| `GET /health`, `GET /health/live` | none | Readiness (checks DB) / liveness |

Auth is deliberately minimal: users are identified by an `X-User-Id` header and admins by `X-Admin-Key`. **Replace `current_user` in `app/api/deps.py` with real JWT/OIDC verification before exposing this publicly.**

Errors are always `{"error": "<code>", "detail": "<message>"}`: `insufficient_stock`, `reservation_expired`, `invalid_state`, `idempotency_key_reuse`, `contention`, ...

## Tests

The tests exercise real Postgres row locking, so they need a database (SQLite would hide the races):

```bash
docker compose up -d db
pip install -r requirements-dev.txt
pytest -v          # uses postgresql+asyncpg://stockguard:stockguard@localhost:5432/stockguard_test
```

What's covered: 100 concurrent buyers vs 20 units for each strategy (exactly 20 succeed, books balance), multi-unit reservations, concurrent duplicate requests with one idempotency key, key reuse with a different body, payment failure, concurrent double-pay with different keys, cancel/refund, reservation expiry + sweeper idempotency, ownership and auth checks. CI (`.github/workflows/ci.yml`) runs lint + tests against a Postgres service container.

## Load test and benchmarking

```bash
python scripts/loadtest.py --stock 100 --buyers 500 --concurrency 100 --output results-atomic.json
```

Each buyer runs reserve, then order, then pay; ~10% of order/pay calls are fired twice at once with the same `Idempotency-Key`. Afterwards the script calls `/audit` and checks:

* no oversell, and books consistent
* units paid for == stock decrease, and never more than the initial stock
* duplicate requests all replayed the same response

Run it once per strategy (restart the API in between):

```bash
INVENTORY_STRATEGY=pessimistic docker compose up -d --force-recreate api
python scripts/loadtest.py --output results-pessimistic.json

INVENTORY_STRATEGY=naive docker compose up -d --force-recreate api   # expect FAIL: this is the demo of the bug
python scripts/loadtest.py --output results-naive.json
```

Record **your own measured numbers** here (machine, Postgres version, params), then use them in your resume bullets:

| Strategy | Throughput (req/s) | reserve p95 (ms) | pay p95 (ms) | Oversold? |
|---|---|---|---|---|
| atomic | | | | |
| pessimistic | | | | |
| optimistic | | | | |
| naive | | | | |

## Deploy

### Option A: Render (fastest)

1. Push this repo to GitHub.
2. Render dashboard: **New, Blueprint**, select the repo. `render.yaml` provisions the web service and a Postgres database and wires `DATABASE_URL`; `ADMIN_API_KEY` is generated for you (read it in the service's Environment tab).
3. When live, open `https://<your-service>.onrender.com/docs`, and point the load test at it:
   `python scripts/loadtest.py --base-url https://<your-service>.onrender.com --admin-key <key>`

Free-tier notes: instances sleep when idle (first request is slow) and free Postgres databases expire; check Render's current terms and upgrade the plan if you need it to stay up for recruiters.

### Option B: AWS

The image is a standard container that reads `PORT` and `DATABASE_URL`. For example: push to **ECR**, run on **ECS Fargate** (or App Runner) behind an ALB with `/health` as the target health check, use **RDS PostgreSQL**, and store `ADMIN_API_KEY` / `DATABASE_URL` in **Secrets Manager** or SSM Parameter Store.

### Production checklist

* Set a strong `ADMIN_API_KEY`; replace header-based user auth with real JWT/OIDC.
* Keep `INVENTORY_STRATEGY=atomic` (never `naive`).
* Introduce Alembic migrations and set `CREATE_TABLES_ON_STARTUP=false` (tables are auto-created for convenience).
* Put TLS in front (the platform/ALB does this), and consider rate limiting at the edge.

## Configuration

| Env var | Default | Meaning |
|---|---|---|
| `DATABASE_URL` | local Postgres | `postgres://`, `postgresql://` and `?sslmode=` forms are normalised for asyncpg |
| `ADMIN_API_KEY` | `change-me` | Admin secret (a warning is logged if left default) |
| `INVENTORY_STRATEGY` | `atomic` | `atomic` / `pessimistic` / `optimistic` / `naive` |
| `RESERVATION_TTL_SECONDS` | `600` | How long a hold lasts |
| `SWEEPER_ENABLED`, `SWEEP_INTERVAL_SECONDS` | `true`, `5` | Expiry sweeper |
| `OPTIMISTIC_MAX_RETRIES` | `8` | Retries before `503 contention` |
| `DB_POOL_SIZE`, `DB_MAX_OVERFLOW` | `20`, `20` | Connection pool |
| `IDEMPOTENCY_TTL_HOURS` | `24` | How long replay keys are kept |
| `CREATE_TABLES_ON_STARTUP` | `true` | Auto-create schema (use migrations in production) |

## Project layout

```
app/
  main.py            app, lifespan (schema, sweeper), routers
  config.py db.py    settings, async engine/session
  models.py          Product, Reservation, Order, IdempotencyKey (+ CHECK constraints)
  services/
    inventory.py     reservation strategies, confirm/release/restock
    orders.py        order lifecycle (create, pay, cancel)
    idempotency.py   Idempotency-Key execution wrapper
    sweeper.py       reservation expiry + housekeeping
  api/               HTTP layer (products, reservations, orders, health)
tests/               pytest suite (needs Postgres)
scripts/loadtest.py  concurrent-buyer load test + invariant checker
```

## Known limitations / next steps

* Payment is mocked (`outcome` field). A real integration would call a gateway and confirm via webhook.
* Auth is a header stand-in (see above).
* No metrics endpoint yet; Prometheus counters for reservations, sold-out rejections and sweeper activity would be the natural next addition.
* Multi-item carts: today an order is one product line; a cart would reserve several products in one transaction, locking products in a consistent order to stay deadlock-free.
* Domain events (order paid, stock low) via a transactional outbox.
