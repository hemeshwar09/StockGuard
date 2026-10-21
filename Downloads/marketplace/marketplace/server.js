import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';

const PORT = +process.env.PORT || 3000, SECRET = process.env.SECRET || 'dev-secret-change-me';
const DB_FILE = process.env.DB_FILE || 'data/db.json', COMMISSION = 0.10, PAGE = 12;
const uid = () => crypto.randomUUID().replace(/-/g, '').slice(0, 12), now = () => new Date().toISOString();
const E = (status, message) => Object.assign(new Error(message), { status });

// ---------- persistence: in-memory + atomic JSON snapshot (swap for Postgres in prod) ----------
let db; try { db = JSON.parse(fs.readFileSync(DB_FILE, 'utf8')); } catch {}
let timer; const save = () => { clearTimeout(timer); timer = setTimeout(flush, 150); };
function flush() { fs.mkdirSync(path.dirname(DB_FILE), { recursive: true }); fs.writeFileSync(DB_FILE + '.tmp', JSON.stringify(db)); fs.renameSync(DB_FILE + '.tmp', DB_FILE); }
process.on('SIGINT', () => { flush(); process.exit(0); });

// ---------- auth: scrypt passwords + HMAC-signed expiring tokens ----------
const hashPw = (p, s = crypto.randomBytes(16).toString('hex')) => s + ':' + crypto.scryptSync(p, s, 32).toString('hex');
const checkPw = (p, h) => { const [s, k] = h.split(':'); const a = Buffer.from(hashPw(p, s).split(':')[1], 'hex'); return crypto.timingSafeEqual(a, Buffer.from(k, 'hex')); };
const b64 = o => Buffer.from(JSON.stringify(o)).toString('base64url');
const mac = s => crypto.createHmac('sha256', SECRET).update(s).digest('base64url');
const sign = u => { const p = b64({ id: u.id, exp: Date.now() + 7 * 864e5 }); return p + '.' + mac(p); };
function userFrom(req) {
  const [p, s] = (req.headers.authorization || '').replace('Bearer ', '').split('.');
  if (!p || !s || mac(p) !== s) return null;
  try { const o = JSON.parse(Buffer.from(p, 'base64url')); return o.exp > Date.now() ? db.users.find(u => u.id === o.id) || null : null; } catch { return null; }
}
const safe = u => ({ id: u.id, name: u.name, email: u.email, role: u.role, shop: u.shop, status: u.status });

if (!db) {  // ---------- seed ----------
  const mk = (name, email, pw, role, shop, status) => ({ id: uid(), name, email, pass: hashPw(pw), role, shop, status, cart: [], createdAt: now() });
  const admin = mk('Admin', 'admin@shop.test', 'admin123!', 'admin', null, 'approved');
  const v1 = mk('Aria', 'aria@shop.test', 'vendor123!', 'vendor', 'Aurora Audio', 'approved');
  const v2 = mk('Leo', 'leo@shop.test', 'vendor123!', 'vendor', 'Greenleaf Home', 'approved');
  const P = (v, title, price, category, emoji, stock, description) => ({ id: uid(), vendorId: v.id, title, price, category, emoji, stock, description, status: 'active', createdAt: now() });
  db = { users: [admin, v1, v2], reviews: [], orders: [], idem: {},
    products: [P(v1, 'Wireless Noise-Cancelling Headphones', 7900, 'Electronics', '🎧', 40, '30-hour battery, active noise cancelling.'),
      P(v1, 'Bluetooth Speaker', 3900, 'Electronics', '🔊', 60, 'Waterproof, 12-hour playtime.'), P(v1, 'Mechanical Keyboard', 6500, 'Electronics', '⌨️', 25, 'Hot-swappable switches, RGB.'),
      P(v2, 'Ceramic Plant Pot Set', 2400, 'Home', '🪴', 80, 'Set of three with drainage trays.'), P(v2, 'Linen Throw Blanket', 4200, 'Home', '🛋️', 35, 'Soft, breathable, machine washable.'),
      P(v2, 'Pour-Over Coffee Set', 3600, 'Kitchen', '☕', 50, 'Glass dripper, filters and carafe.'), P(v2, 'Cast Iron Skillet', 3100, 'Kitchen', '🍳', 45, 'Pre-seasoned 10-inch skillet.')],
    coupons: [{ code: 'WELCOME10', type: 'percent', value: 10, min: 0, left: 1000 }, { code: 'FLAT50', type: 'fixed', value: 5000, min: 30000, left: 100 }] };
  flush();
}

// ---------- helpers ----------
const rate = new Map(); const limited = (k, max) => { const t = Date.now(), e = rate.get(k); if (!e || t - e.t > 60000) { rate.set(k, { t, n: 1 }); return false; } return ++e.n > max; };
const str = (v, min, max, name) => { if (typeof v !== 'string' || v.trim().length < min || v.length > max) throw E(400, `${name} must be ${min}-${max} characters`); return v.trim(); };
const int = (v, min, max, name) => { v = Math.round(+v); if (!Number.isFinite(v) || v < min || v > max) throw E(400, `${name} must be ${min}-${max}`); return v; };
const rating = id => { const r = db.reviews.filter(x => x.productId === id); return { rating: r.length ? +(r.reduce((a, x) => a + x.rating, 0) / r.length).toFixed(1) : 0, reviews: r.length }; };
const vendorOf = id => db.users.find(u => u.id === id);
const pub = p => ({ ...p, vendor: vendorOf(p.vendorId)?.shop, ...rating(p.id) });
const orderStatus = o => { const s = o.items.map(i => i.status); return s.every(x => x === 'cancelled') ? 'cancelled' : s.every(x => x === 'delivered') ? 'delivered' : s.some(x => x !== 'placed' && x !== 'cancelled') ? 'processing' : 'placed'; };
const view = (o, u) => { const items = u.role === 'vendor' ? o.items.filter(i => i.vendorId === u.id) : o.items; return { ...o, items, status: orderStatus(o) }; };

// ---------- routes ----------
const R = []; const route = (m, p, auth, fn) => R.push([m, new RegExp('^' + p.replace(/:(\w+)/g, '(?<$1>[^/]+)') + '$'), auth, fn]);
route('POST', '/api/auth/register', null, ({ body }) => {
  const email = str(body.email, 5, 100, 'Email').toLowerCase(); if (!/^\S+@\S+\.\S+$/.test(email)) throw E(400, 'Invalid email');
  if (db.users.some(u => u.email === email)) throw E(409, 'Email already registered');
  const role = body.role === 'vendor' ? 'vendor' : 'customer', pw = str(body.password, 8, 100, 'Password');
  const u = { id: uid(), name: str(body.name, 2, 60, 'Name'), email, pass: hashPw(pw), role, shop: role === 'vendor' ? str(body.shop, 2, 60, 'Shop name') : null,
    status: role === 'vendor' ? 'pending' : 'approved', cart: [], createdAt: now() };
  db.users.push(u); save(); return [201, { token: sign(u), user: safe(u) }];
});
route('POST', '/api/auth/login', null, ({ body }) => {
  const u = db.users.find(x => x.email === String(body.email || '').toLowerCase());
  if (!u || !checkPw(String(body.password || ''), u.pass)) throw E(401, 'Invalid email or password');
  return { token: sign(u), user: safe(u) };
});
route('GET', '/api/me', 'any', ({ user }) => ({ user: safe(user), cartCount: user.cart.reduce((a, c) => a + c.qty, 0) }));
route('GET', '/api/categories', null, () => { const m = {}; db.products.filter(p => p.status === 'active').forEach(p => m[p.category] = (m[p.category] || 0) + 1); return Object.entries(m).map(([name, count]) => ({ name, count })); });
route('GET', '/api/products', null, ({ query: q }) => {
  let l = db.products.filter(p => p.status === 'active' && vendorOf(p.vendorId)?.status === 'approved').map(pub);
  if (q.q) { const s = q.q.toLowerCase(); l = l.filter(p => (p.title + ' ' + p.description + ' ' + p.vendor).toLowerCase().includes(s)); }
  if (q.category) l = l.filter(p => p.category === q.category);
  if (q.vendor) l = l.filter(p => p.vendorId === q.vendor);
  if (q.min) l = l.filter(p => p.price >= +q.min * 100); if (q.max) l = l.filter(p => p.price <= +q.max * 100);
  const by = { low: (a, b) => a.price - b.price, high: (a, b) => b.price - a.price, rating: (a, b) => b.rating - a.rating || b.reviews - a.reviews };
  l.sort(by[q.sort] || ((a, b) => b.createdAt.localeCompare(a.createdAt)));
  const page = Math.max(1, +q.page || 1); return { total: l.length, page, pages: Math.max(1, Math.ceil(l.length / PAGE)), items: l.slice((page - 1) * PAGE, page * PAGE) };
});
route('GET', '/api/products/:id', null, ({ params, user }) => {
  const p = db.products.find(x => x.id === params.id && x.status === 'active'); if (!p) throw E(404, 'Product not found');
  const reviews = db.reviews.filter(r => r.productId === p.id).map(r => ({ ...r, user: db.users.find(u => u.id === r.userId)?.name })).reverse();
  const canReview = !!user && !reviews.some(r => r.userId === user.id) && db.orders.some(o => o.userId === user.id && o.items.some(i => i.productId === p.id && i.status === 'delivered'));
  return { ...pub(p), reviewList: reviews, canReview };
});
route('POST', '/api/products/:id/reviews', 'any', ({ params, user, body }) => {
  const p = db.products.find(x => x.id === params.id); if (!p) throw E(404, 'Product not found');
  if (db.reviews.some(r => r.productId === p.id && r.userId === user.id)) throw E(409, 'You already reviewed this product');
  if (!db.orders.some(o => o.userId === user.id && o.items.some(i => i.productId === p.id && i.status === 'delivered'))) throw E(403, 'Only buyers with a delivered order can review');
  db.reviews.push({ id: uid(), productId: p.id, userId: user.id, rating: int(body.rating, 1, 5, 'Rating'), comment: str(body.comment || 'No comment', 1, 500, 'Comment'), createdAt: now() }); save(); return [201, { ok: true }];
});
// vendor product management
const prodFields = (b, p = {}) => ({ title: b.title !== undefined ? str(b.title, 3, 120, 'Title') : p.title, description: b.description !== undefined ? str(b.description, 0 || 1, 2000, 'Description') : p.description,
  price: b.price !== undefined ? int(+b.price * 100, 100, 100000000, 'Price') : p.price, stock: b.stock !== undefined ? int(b.stock, 0, 100000, 'Stock') : p.stock,
  category: b.category !== undefined ? str(b.category, 2, 40, 'Category') : p.category, emoji: b.emoji !== undefined ? str(b.emoji, 1, 8, 'Emoji') : p.emoji });
route('POST', '/api/products', ['vendor'], ({ user, body }) => {
  if (user.status !== 'approved') throw E(403, 'Your vendor account is awaiting admin approval');
  const p = { id: uid(), vendorId: user.id, status: 'active', createdAt: now(), ...prodFields({ emoji: '📦', ...body }) }; db.products.push(p); save(); return [201, p];
});
const own = (user, id) => { const p = db.products.find(x => x.id === id); if (!p) throw E(404, 'Product not found'); if (p.vendorId !== user.id && user.role !== 'admin') throw E(403, 'Not your product'); return p; };
route('PUT', '/api/products/:id', ['vendor', 'admin'], ({ user, params, body }) => { const p = own(user, params.id); Object.assign(p, prodFields(body, p)); save(); return p; });
route('DELETE', '/api/products/:id', ['vendor', 'admin'], ({ user, params }) => { own(user, params.id).status = 'archived'; save(); return { ok: true }; });
// cart
const cartView = u => { const items = u.cart.map(c => { const p = db.products.find(x => x.id === c.productId); return p && p.status === 'active' ? { ...pub(p), qty: c.qty } : null; }).filter(Boolean);
  return { items, subtotal: items.reduce((a, i) => a + i.price * i.qty, 0) }; };
route('GET', '/api/cart', ['customer'], ({ user }) => cartView(user));
route('PUT', '/api/cart', ['customer'], ({ user, body }) => {
  const p = db.products.find(x => x.id === body.productId && x.status === 'active'); if (!p) throw E(404, 'Product not found');
  const qty = int(body.qty, 0, 20, 'Quantity'); user.cart = user.cart.filter(c => c.productId !== p.id);
  if (qty > 0) { if (qty > p.stock) throw E(409, `Only ${p.stock} in stock`); user.cart.push({ productId: p.id, qty }); } save(); return cartView(user);
});
// checkout: the whole validate-reserve-write section is synchronous, so it is atomic (no interleaving, no oversell)
route('POST', '/api/checkout', ['customer'], ({ user, body }) => {
  const key = body.idempotencyKey && `${user.id}:${body.idempotencyKey}`; if (key && db.idem[key]) return [200, db.orders.find(o => o.id === db.idem[key])];
  if (!user.cart.length) throw E(400, 'Cart is empty'); const address = str(body.address, 8, 300, 'Address');
  const lines = user.cart.map(c => { const p = db.products.find(x => x.id === c.productId);
    if (!p || p.status !== 'active') throw E(409, 'An item in your cart is no longer available');
    if (p.stock < c.qty) throw E(409, `Only ${p.stock} left of "${p.title}"`); return { p, qty: c.qty, total: p.price * c.qty }; });
  const subtotal = lines.reduce((a, l) => a + l.total, 0); let discount = 0, coupon = null;
  if (body.coupon) { coupon = db.coupons.find(c => c.code === String(body.coupon).toUpperCase());
    if (!coupon || coupon.left <= 0) throw E(400, 'Invalid or expired coupon'); if (subtotal < coupon.min) throw E(400, `Coupon needs a minimum of $${coupon.min / 100}`);
    discount = Math.min(subtotal, coupon.type === 'percent' ? Math.floor(subtotal * coupon.value / 100) : coupon.value); }
  let left = discount;
  const items = lines.map((l, i) => { const d = i === lines.length - 1 ? left : Math.floor(discount * l.total / subtotal); left -= d; const net = l.total - d, commission = Math.round(net * COMMISSION);
    l.p.stock -= l.qty; return { id: uid(), productId: l.p.id, vendorId: l.p.vendorId, title: l.p.title, emoji: l.p.emoji, qty: l.qty, unitPrice: l.p.price, total: l.total, discount: d, net, commission, vendorNet: net - commission, status: 'placed' }; });
  if (coupon) coupon.left--;
  const order = { id: uid(), userId: user.id, items, subtotal, discount, total: subtotal - discount, coupon: coupon?.code || null, address, createdAt: now() };
  db.orders.push(order); user.cart = []; if (key) db.idem[key] = order.id; save(); return [201, view(order, user)];
});
route('GET', '/api/orders', ['customer', 'vendor', 'admin'], ({ user }) => {
  const l = user.role === 'customer' ? db.orders.filter(o => o.userId === user.id) : user.role === 'vendor' ? db.orders.filter(o => o.items.some(i => i.vendorId === user.id)) : db.orders;
  return l.slice().reverse().map(o => ({ ...view(o, user), customer: user.role === 'customer' ? undefined : db.users.find(u => u.id === o.userId)?.name }));
});
route('POST', '/api/orders/:id/cancel', ['customer'], ({ user, params }) => {
  const o = db.orders.find(x => x.id === params.id && x.userId === user.id); if (!o) throw E(404, 'Order not found');
  if (o.items.some(i => i.status !== 'placed' && i.status !== 'cancelled')) throw E(409, 'Order already shipped, it can no longer be cancelled');
  o.items.forEach(i => { if (i.status === 'placed') { i.status = 'cancelled'; const p = db.products.find(x => x.id === i.productId); if (p) p.stock += i.qty; } }); save(); return view(o, user);
});
route('POST', '/api/orders/:id/advance', ['vendor'], ({ user, params }) => {   // vendor moves ONLY their own items: placed → shipped → delivered
  const o = db.orders.find(x => x.id === params.id); const mine = o?.items.filter(i => i.vendorId === user.id && ['placed', 'shipped'].includes(i.status));
  if (!mine?.length) throw E(404, 'Nothing to advance'); mine.forEach(i => { i.status = i.status === 'placed' ? 'shipped' : 'delivered'; }); save(); return view(o, user);
});
route('GET', '/api/vendor/dashboard', ['vendor'], ({ user }) => {
  const items = db.orders.flatMap(o => o.items).filter(i => i.vendorId === user.id && i.status !== 'cancelled'), sum = (l, k) => l.reduce((a, i) => a + i[k], 0), mine = db.products.filter(p => p.vendorId === user.id && p.status === 'active');
  return { status: user.status, shop: user.shop, products: mine.map(pub), units: sum(items, 'qty'), gross: sum(items, 'net'), commission: sum(items, 'commission'), net: sum(items, 'vendorNet'),
    pendingPayout: sum(items.filter(i => i.status !== 'delivered'), 'vendorNet'), availablePayout: sum(items.filter(i => i.status === 'delivered'), 'vendorNet'), lowStock: mine.filter(p => p.stock <= 3).map(p => p.title) };
});
route('GET', '/api/admin/overview', ['admin'], () => { const items = db.orders.flatMap(o => o.items).filter(i => i.status !== 'cancelled');
  return { gmv: items.reduce((a, i) => a + i.net, 0), commission: items.reduce((a, i) => a + i.commission, 0), orders: db.orders.length, customers: db.users.filter(u => u.role === 'customer').length,
    vendors: db.users.filter(u => u.role === 'vendor').map(v => ({ ...safe(v), products: db.products.filter(p => p.vendorId === v.id && p.status === 'active').length })), coupons: db.coupons }; });
route('POST', '/api/admin/vendors/:id/:action', ['admin'], ({ params }) => { const v = db.users.find(u => u.id === params.id && u.role === 'vendor'); if (!v) throw E(404, 'Vendor not found');
  v.status = params.action === 'approve' ? 'approved' : 'suspended'; save(); return safe(v); });

// ---------- http ----------
const page = fs.readFileSync(new URL('./public/index.html', import.meta.url));
const server = http.createServer(async (req, res) => {
  const send = (code, obj) => { res.writeHead(code, { 'content-type': 'application/json', 'x-content-type-options': 'nosniff' }); res.end(JSON.stringify(obj)); };
  try {
    const url = new URL(req.url, 'http://x'), ip = req.socket.remoteAddress;
    if (!url.pathname.startsWith('/api/')) { res.writeHead(200, { 'content-type': 'text/html; charset=utf-8', 'x-frame-options': 'DENY', 'x-content-type-options': 'nosniff' }); return res.end(page); }
    if (limited('g' + ip, 600) || (url.pathname.startsWith('/api/auth/') && limited('a' + ip, 30))) throw E(429, 'Too many requests, slow down');
    let raw = ''; for await (const c of req) { raw += c; if (raw.length > 1e5) throw E(413, 'Body too large'); }
    let body = {}; try { body = raw ? JSON.parse(raw) : {}; } catch { throw E(400, 'Invalid JSON'); }
    const user = userFrom(req);
    for (const [m, re, auth, fn] of R) { const x = m === req.method && re.exec(url.pathname); if (!x) continue;
      if (auth && !user) throw E(401, 'Please log in'); if (Array.isArray(auth) && !auth.includes(user.role)) throw E(403, 'Not allowed for your account type');
      const out = await fn({ req, user, body, params: x.groups || {}, query: Object.fromEntries(url.searchParams) }); return Array.isArray(out) ? send(out[0], out[1]) : send(200, out); }
    throw E(404, 'Not found');
  } catch (e) { if (!e.status) console.error(e); send(e.status || 500, { error: e.status ? e.message : 'Internal error' }); }
});
server.listen(PORT, () => console.log(`Marketplace on http://localhost:${PORT}`));
