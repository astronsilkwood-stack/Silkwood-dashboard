# Silkwood Daily Ops dashboard (v2)
Python stdlib server (`server.py`) + `index.html`. Storage:
- no DATABASE_URL -> SQLite file `silkwood.db` (local/box use)
- DATABASE_URL=postgresql://... -> Postgres (Neon in the cloud). Tables + starter data are created on first start.

Env vars: PORT (default 8787), DATABASE_URL, STAFF_PIN (shared staff PIN asked once per device and stored there;
empty = no PIN, local only), MANAGER_PIN (reopen a submitted day; default 1945).

Checklist webhooks (server only; the browser never sees the URL or key). Each is a POST URL plus a bearer token
sent as `Authorization: Bearer <key>`. If either value is missing, that checklist’s Submit returns 503.
- Closing Checklist: CLOSING_WEBHOOK_URL, CLOSING_WEBHOOK_KEY
- Kitchen Daily Checklist: KITCHEN_WEBHOOK_URL, KITCHEN_WEBHOOK_KEY
  Payload `type` is `kitchen_daily_checklist`, `site` is `United Silkwood`. `tasks` is id → `done` or `na`
  (bacon, crumbed_fish, fried_topup, kabana, trifle, oil_fryer, chicken_fryer, defrost_fish, chicken_buckets).
  `taskLabels` repeats the staff wording. Also sent: checklistDate, staffName, taskComments, dayComments.

Public pickup orders (no PIN): `/order`. POST `/api/order` recomputes prices from `static/order/menu.json` and saves the order, then forwards it to ORDER_WEBHOOK_URL with `Authorization: Bearer ORDER_WEBHOOK_KEY`. If either value is missing, or the webhook fails, the order is still saved and the customer still gets a confirmation. The key is never logged. Payment is pay at pickup only.

Run on the box: ./run.sh (keep-alive; re-run after a box restart). Local URL http://localhost:8787
Cloud: see DEPLOY.md (Render free web service + Neon free Postgres). Files: requirements.txt, Dockerfile, render.yaml, Procfile.
Temperature rules: fridge <=5, freezer <=-15, hot hold >=60, cooked core >=75 (°C).
Legacy v1 (Apps Script/Drive, ../build.py) untouched; its link error = the Apps Script deploy was never done.

## Owner portal

Private pages for Astron at `/portal`. This is not the staff PIN. Staff routes, `/order`, and the checklist webhooks are unchanged. The portal does not take payment, store card or bank details, or post payroll.

### Env vars (Render → silkwood-ops → Environment)

| Name | Required | Purpose |
|---|---|---|
| `PORTAL_ADMIN_USER` | yes | First owner username. Letters, numbers, `.`, `_`, `-`. Stored in lowercase. |
| `PORTAL_ADMIN_PASSWORD_HASH` | yes | Argon2 or bcrypt hash. Never the plain password. |
| `PORTAL_INGEST_KEY` | yes | Long random secret. Bearer key for the nightly upload and the staff-hours read. |
| `PORTAL_SESSION_HOURS` | no | Absolute session length. Default 12. |
| `PORTAL_INSIGHTS_GENERATE` | no | `1` writes a plain figure summary when that night has no pushed insight. Default off. No LLM and no API key. |
| `PORTAL_COOKIE_SECURE` | no | `1` forces the Secure cookie flag, `0` forces it off. On Render, or when `X-Forwarded-Proto` is `https`, Secure is already on. |

The server never logs these values, the database URL, or session cookies.

### First owner login

On the machine you trust (not in the Render shell history if you can avoid it):

```
python3 tools/hash_portal_password.py
```

It asks for the password twice and prints an argon2 hash. Put that hash in `PORTAL_ADMIN_PASSWORD_HASH`, set `PORTAL_ADMIN_USER` (for example `astron`), set `PORTAL_INGEST_KEY` to a long random string, and redeploy. Open `https://silkwood-ops.onrender.com/portal`.

The account is created on startup when that username does not exist yet. A password changed on the Change password page stays in the database. It is replaced only when you change `PORTAL_ADMIN_PASSWORD_HASH` and restart; that also logs every session out. A bcrypt hash (`$2b$...`) is accepted as well:

```
python3 -c "import bcrypt; print(bcrypt.hashpw(b'your password', bcrypt.gensalt()).decode())"
```

Sessions last 12 hours, then the owner signs in again. The cookie is `HttpOnly` and `SameSite=Lax`, and `Secure` on Render. Five wrong passwords from the same network inside 15 minutes pause further attempts from that network. Log out is a button on the portal, not a GET link.

### Nightly ingest

After the 9:30pm Brisbane refresh, POST one dataset at a time:

```
POST /api/portal/ingest
Authorization: Bearer <PORTAL_INGEST_KEY>
Content-Type: application/json

{"dataset": "<name>", "data": { ... }}
```

`dataset` is one of `sales`, `purchases`, `fuel-commission`, `fuel-dips`, `insights`, `invoices`, `product-search`, `retail-pricing`, `till-price-changes`, `shifts`, `last-year`, `cost-overrides`. A `200` body is `{"ok": true, "dataset": "...", "asOf": "YYYY-MM-DD", ...}`. Missing or wrong key is `401`. If `PORTAL_INGEST_KEY` is unset the route is `503` and stores nothing. The same payload posted again updates that date; older dates already stored are kept.

No new environment variable is required for these pages.

**2026 gross profit.** Leave the GP in `sales` on till cost. Post `cost-overrides` as the full cost table (`items` replaces the table on every upload). The portal applies an override, when `effectiveFrom` is on or before the sale date, anywhere it shows 2026 GP, GP% or cost coverage. Otherwise it uses the till cost. 2025 has no GP. Do not also rebase the sales GP on the box, or the override is counted twice.

`data` shapes:

**sales** — the `dashboard-data.json` object. Rows are upserted by `daily[].date`. Fields read: `asOf`, `latestSalesDate`, `periods[].daily[]` (`date`, `total`, `shop`, `fuel`, `gp`, `gpPct`, `coverage`, `coveredEx`, `missing`, `products`), and `commission` (`month`, `target`, `amount`, `october`, `daily[]` with `date`, `amount`, `status`, `ulpL`, `ulpCpl`, `dieselL`, `dieselCpl`, `pulpL`, `pulpCpl`). `products`, `searchPack`, and `kitchen` are stored with the snapshot and are not shown.

**purchases** — the `purchases-data.json` object. Each bill is `[supplierIndex, date, invoiceNumber, exGst, incGst, status, dueDate, amountDue, shopShare, note]`. `supplierIndex` points at `suppliers`. Upsert key is supplier + invoice number + bill date.

**fuel-commission** — the `fuel-commission-data.json` object. Each `days[]` row is upserted by `date` (`grades`, `totalCommEx`, `litres`, `salesInc`). `mtd`, `tomorrow`, and `basis` are kept from the latest upload.

**fuel-dips** — the `fuel-dips-data.json` object. `tankColumns` and `pumpColumns` describe each row inside `days[]`. Upserted by `date`. There is no `asOf`; the latest dip date is used.

**insights** — written by the shop computer. No model key is required here.

```json
{"dataset":"insights","data":{"date":"2026-11-10","headline":"One line","bullets":["First point","Second point"]}}
```

`headline` is required, at most 200 characters. `bullets` is 1 to 12 strings, each at most 500 characters. The latest date is shown on the portal home.

**invoices** — the unpaid list. Upserted by supplier + invoice number.

```json
{"dataset":"invoices","data":{"date":"2026-11-10","sync":false,"invoices":[
  {"supplier":"Bega Dairy","invoice_number":"577803222","date":"2026-09-08","due_date":"2026-09-15","amount_due":89.72,"status":"unpaid","pay_url":"https://supplier.example/pay/577803222"}
]}}
```

`pay_url` is optional. It must be `http` or `https`, with no username or password in the URL. The Pay button opens that address in a new tab. `status` is `unpaid`, `paid`, `void`, `draft`, or `absent`. A due date before today (Brisbane) is shown as overdue. `"sync": true` marks invoices missing from this upload as `absent` so they leave the unpaid list; the row is kept. Card numbers, and fields named like card, cvv, bsb, or bank account, are rejected and nothing from that upload is stored.

**sales** also keeps `periods[]` `weekdays`, `departments`, `top`, `low`, and `summary`, plus `products` (code to name), `kitchen`, `window`, `olderMonths`, and the commission header (`planDaily`, `neededDaily`, `missingDates`, `october`, …). `searchPack` is ignored. Optional `lastYear` is rejected if it contains cost, GP, coverage, or a missing-cost field.

**purchases** also stores `shop` (`[date, shopSalesInc]`) and `spend` (`[supplierIndex, date, exGst, incGst, bucket]`). Each upload replaces those series.

**cost-overrides** — the full box cost table. Every upload replaces the table.

```json
{"dataset":"cost-overrides","data":{"asOf":"2026-11-10","items":[
  {"code":"804040","costExCents":200,"effectiveFrom":"2026-11-01","source":"invoice"}
]}}
```

`costExCents` is a non-negative integer. `source` is `invoice` or `owner`. The latest row with `effectiveFrom` on or before the sale date replaces the till unit cost for 2026 GP. A known zero is a real cost, not a missing cost.

**product-search** — `dates` (ISO days), `refs` (`[department, group]`), `products` tuples `[code, name, refIndex, barcode, sellCents, costExCents, flag, packedDaily]`. `flag` 0 is GST-free (sales ex GST equals sales inc GST); `flag` 1 divides by 1.1. Packed daily tokens are `dayIndex:qty,salesDelta[,unused],costDelta`, day index in base 36, deltas in base-36 cents, `_` plus a decimal for litres, and `!` for no till cost that day. An empty qty means 1.

**retail-pricing** — `asOf`, `columns`, `rows`, `config` (`default_margin`, `department_margins`, `excluded_departments`, `retail_endings_cents`), `promos`, `overrides`. Calculator inputs are not stored.

**till-price-changes** — `asOf`, `rows` (objects: `id`, `flagged`, `plu`, `name`, `oldCost`, `newCost`, `currentSell`, `currentGpPct`, `reason`, `status`), `noCost` (`columns`, `rows`), `newItems` (`columns`, `rows`). Status is Pending, Done, or Superseded. A reason containing `COST-ONLY` does not change the board price.

**shifts** — till cash-off, not staff hours. `mediaNames`, `mediaColumns`, `otherTenders`, `differenceBasis`, `days[]` with `date`, `txnNo`, `windowFrom`, `windowTo`, `committed`, and `media` tuples `[mediaIndex, expected, counted, difference, count]`. Counted cash of 0 on a committed day is not a shortage. Optional `olderMonths` tuples `[month, mediaName, status, expected, counted, difference, count, days]`.

**last-year** — one month, or `{months: [...]}`. No cost, GP, coverage, or missing-cost field anywhere in the JSON (the upload is rejected).

```json
{"dataset":"last-year","data":{
  "label":"November 2025","year":2025,"month":11,
  "fromDate":"2025-11-01","toDate":"2025-11-30","days":30,
  "source":"SwiftPOS PLU Sales, 1 Nov 2025 to 1 Dec 2025, whole month",
  "fuelLitres":115281.95,"fuelEx":193763.72,
  "fuelGrades":[{"code":"2","name":"UNLEADED","qty":40269.06,"salesEx":65858.07}],
  "groups":[{"name":"A1 Fuel","qty":115281.95,"salesEx":193763.72}],
  "products":[{"code":"2","name":"UNLEADED","group":"A1 Fuel","qty":40269.06,"salesEx":65858.07}]
}}
```

`fuelLitres` may instead be `{"ULP":0,"DIS":0,"P95":0,"total":0}`. Product tuples `[code, name, group, qty, salesEx]` or `[code, qty, salesEx]` are accepted. Dollars are ex GST. The November 2025 fixture in `tools/fixtures/last-year.json` is that month’s PLU report.

### Staff hours read

The portal page is `/portal/hours` (Monday–Sunday week, Brisbane). The box can read the same week without a browser session:

```
GET /api/portal/staff-hours?week=2026-11-09
Authorization: Bearer <PORTAL_INGEST_KEY>
```

`week` is any date in the week; the response uses that week's Monday. The JSON has `timezone`, `week`, `weekEnding`, `days` (weekend and Queensland public holiday flags), `people` (daily hours, shift start/finish/break/notes), `grandTotal`, and a note that nothing is sent to Xero. CSV for the pay run is downloaded from the page while logged in. The portal does not create or post a pay run.

### Local look at the attached data

```
export SILKWOOD_DB=/tmp/silkwood-portal.db
export PORTAL_ADMIN_USER=astron
export PORTAL_ADMIN_PASSWORD_HASH="$(python3 tools/hash_portal_password.py)"
export PORTAL_INGEST_KEY="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
export PORTAL_COOKIE_SECURE=0
python3 tools/seed_portal.py
python3 server.py
```

Then open http://localhost:8787/portal. Sample fixtures live in `tools/fixtures/`. Tests: `python3 -m unittest tools.test_portal`.
