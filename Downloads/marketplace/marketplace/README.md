# Bazaar: multi-vendor marketplace

Zero dependencies. Node 20+.  `node server.js` then open http://localhost:3000   ·   `node test/smoke.mjs` runs the test suite.

**Demo logins:** admin@shop.test / admin123!  ·  aria@shop.test / vendor123!  ·  leo@shop.test / vendor123!  ·  register to get a customer. Coupons: `WELCOME10`, `FLAT50` (min $300).

## What it does
- **Three roles:** customers shop; vendors run a shop; admin approves/suspends vendors and sees GMV + commission.
- **Catalogue:** full-text search, category, price range, sort (new/price/rating), pagination.
- **Cart and checkout:** atomic stock reservation, per-vendor split orders, coupons (percent/fixed, minimum, usage cap) with discount allocated across lines, idempotency keys.
- **Fulfilment:** each vendor moves only their own items placed → shipped → delivered; customers can cancel before shipment (stock restored).
- **Money:** all amounts are integer cents; 10% platform commission; vendor ledger shows gross, commission, net, pending vs available payout.
- **Trust:** reviews only from verified buyers with a delivered order, one per product.
- **Security:** scrypt password hashing, HMAC-signed expiring tokens, role checks on every route, ownership checks on products, per-IP rate limiting, body size limits, input validation, output escaping.

## Tested (test/smoke.mjs)
20 concurrent buyers for 5 units → exactly 5 succeed, 0 oversell · idempotent checkout · role/ownership enforcement · coupon math · cancel/restock · verified-review rule · vendor ledger · search.

## Interview talking points
- Checkout validate→reserve→write is one synchronous section, so it cannot interleave; in Postgres this is a transaction with `SELECT … FOR UPDATE` or `UPDATE … SET stock = stock - $1 WHERE stock >= $1`.
- Money in cents avoids float errors; discounts are allocated per line with the remainder on the last line so totals always reconcile.
- Persistence is an in-memory store with atomic JSON snapshots; the route layer is storage-agnostic so swapping in Postgres means rewriting the data access only.

## Not included (next steps)
Real payments (Stripe), image uploads, email notifications, Postgres, refunds/returns, shipping and tax rules, WebSocket order updates.
