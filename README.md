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

`dataset` is one of `sales`, `purchases`, `fuel-commission`, `fuel-dips`, `insights`, `invoices`, `product-search`, `retail-pricing`, `till-price-changes`, `shifts`, `last-year`, `cost-overrides`, `stock-movements`, `online-orders`, `shift-exceptions`, `barcode-map`, `last-year-monthly`, `pay-rates`. A `200` body is `{"ok": true, "dataset": "...", "asOf": "YYYY-MM-DD", ...}`. Missing or wrong key is `401`. If `PORTAL_INGEST_KEY` is unset the route is `503` and stores nothing. The same payload posted again updates that date; older dates already stored are kept. `pay-rates` is the exception: re-posting replaces the whole list.

The body limit is 8,000,000 bytes. Send plain JSON, or gzip it and set `Content-Encoding: gzip`. The limit applies to the bytes on the wire and again to the JSON after gzip is decompressed. A barcode map of about 4,400 PLUs is about 400KB of JSON and fits either way.

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

`pay_url` is optional. It must be `http` or `https`, with no username or password in the URL. The Pay button opens that address in a new tab, and only for a bill that is still unpaid. `status` is `unpaid`, `paid`, `void`, `draft`, or `absent`. `paid` and `p` are the paid list. Void, draft, and absent stay off both lists. A due date before today (Brisbane) is shown as overdue on an unpaid bill. `"sync": true` marks invoices missing from this upload as `absent` so they leave the unpaid list; the row is kept. Card numbers, and fields named like card, cvv, bsb, or bank account, are rejected and nothing from that upload is stored.

`amount_due` is what is still owing. On a paid bill it is usually 0, so it is not the amount that was paid. Optional `amount` is the invoice total (inc GST), a number. Optional `paid_date` is `YYYY-MM-DD`. When those two are present, the paid list shows the paid date and the total paid is the sum of `amount`. Without them the paid list still shows who is paid, and the total stays blank.

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

**last-year-monthly** — whole-month totals for the overview comparison. This is separate from `last-year`. Shop and food are inc GST. Fuel is litres. Any figure may be null. Null means that figure has not been supplied. The overview shows “No last-year figure yet” and does not treat it as zero. A later upload of the same `month` replaces that row, including fields you set back to null. Other months already stored are kept. Send one month, or `months`. Optional `asOf` is `YYYY-MM-DD`. If it is omitted, the portal uses the last day of the latest month in the payload.

```json
{"dataset":"last-year-monthly","data":{"months":[
  {"month":"2025-11","shop_sales_inc":182430.50,"food_sales_inc":61220.00,"fuel_litres":115281.95,"source":"SwiftPOS monthly totals","note":"Till only, inc GST. Food is kitchen sales."},
  {"month":"2025-10","shop_sales_inc":null,"food_sales_inc":null,"fuel_litres":108440.12,"source":"Fuel sheet","note":"Shop and food not extracted yet."}
]}}
```

One month can be the data object itself: `{"month":"2025-11","shop_sales_inc":182430.50,"food_sales_inc":null,"fuel_litres":null,"source":"","note":""}`. `month` is `YYYY-MM`. `shop_sales_inc` and `food_sales_inc` are numbers or null (inc GST, till only, not Bopple). `fuel_litres` is a number or null. `source` and `note` are optional text. A `200` body is `{"ok":true,"dataset":"last-year-monthly","asOf":"2025-11-30","months":2}`. `months` is how many rows this upload wrote.

**stock-movements** — units only, for products the owner is tracking. Re-posting the same product, date, and source replaces that row. Other rows are left as they are. `source` is `till-sales` (units sold) or `supplier-invoice` (units received). `sold` and `received` are zero or positive. `reference` is optional (an invoice number). A sample is `tools/fixtures/stock-movements.json`.

```json
{"dataset":"stock-movements","data":{"asOf":"2026-11-10","movements":[
  {"code":"804040","date":"2026-11-10","source":"till-sales","sold":23,"received":0},
  {"code":"804040","date":"2026-11-09","source":"supplier-invoice","sold":0,"received":12,"reference":"TILL8122"}
]}}
```

The opening count is the stock at the **end** of its date (Australia/Brisbane). Movements on that date are not applied again. Stock on hand = opening + received on later dates − sold on later dates. A count entered during the day should use yesterday’s date if that night’s sales should still come off it. Only products with an opening count are tracked. A new count does not delete the earlier one.

The box can read the tracked list with the same ingest key:

```
GET /api/portal/stock
Authorization: Bearer <PORTAL_INGEST_KEY>
```

The JSON has `timezone`, `baselineRule`, and `products[]` (`code`, `name`, `group`, `department`, `barcode`, `baselineDate`, `baselineAt`, `openingQty`, `soldSince`, `receivedSince`, `onHand`, `daysCover`, `lastUpdated`, `note`, `manual`). `manual` is true when the owner added the product on the portal rather than from the till catalog. If they leave the code blank, the portal assigns a stable id such as `M-0001`. Post `supplier-invoice` movements against that code. Editing the code later keeps the counts and movements. Stop tracking hides the product from this list and leaves the history in place. Send movements for tracked codes from the day after `baselineDate`. `barcode` on that payload is still the single catalog or manual barcode. Extra barcodes the owner links from the phone are not in this list.

**Barcodes.** Stock on hand has a Scan button. Chrome uses the browser BarcodeDetector API. Safari loads `/static/zxing.js` (ZXing, vendored, no CDN). A match opens that product so the count can be typed and saved. Search by name, PLU or barcode is unchanged. Keep camera open counts several items without closing the camera. The camera stops when the scanner is closed.

A barcode is matched in this order: a link saved in the portal (`portal_product_barcodes`, many per product), then `product-search`, then the `barcodes` column on `retail-pricing`, then a manual product's barcode, then `barcode-map`. The earlier source wins when the same barcode is in more than one place. Catalog barcodes come from `product-search` product tuples `[code, name, refIndex, barcode, sellCents, costExCents, flag, packedDaily]`. Several barcodes can be separated by whitespace, commas, semicolons or pipes in that same field. A value shorter than 4 characters is ignored. A value shorter than 8 characters that is the same as the PLU is ignored, so fuel rows that copy `"2"`, `"3"` or `"5"` into the barcode slot do not scan as unleaded. UPC-A and the same code with a leading zero (EAN-13) match each other. A checked UPC-E expands to UPC-A. The same rules apply to barcodes in `barcode-map`.

In the current sample, Arnott's Shapes is EAN-13 `9310072028866`, the bacon muffin barcode is empty, and miscellaneous uses a short code `2901`. An unknown scan shows "Barcode not linked". The owner picks the product and the portal stores the link. The next scan works on any device. Linking does not change stock movements or on-hand.

`product-search` and `retail-pricing` only carry products that have sold recently. A scan of anything else needs the full list:

**barcode-map** — every PLU and its shelf barcodes. Each upload replaces the whole map. No new environment variable. A product that appears only here can still take an opening count.

```json
{"dataset":"barcode-map","data":{"asOf":"2026-11-12","products":[
  ["10000","ARNOTTS SHAPES BARBECUE 175G","9310072028866"],
  ["77001","SHELF ONLY SOAP","9305555555551,9305555555558"],
  ["77002","LIST JOINED CHIPS",["9305555555572","9305555555589"]]
]}}
```

`asOf` is optional (`YYYY-MM-DD`). If it is omitted, the portal uses today's date in Australia/Brisbane. `products` is a list of `[code, description, barcodes]`. `code` is the PLU. Send it as a string so a leading zero is kept; a JSON number is accepted and stored as text. `description` is a string. `barcodes` is a comma-separated string or a list of EAN/UPC barcodes. Spaces, commas, semicolons and pipes all separate barcodes in a string. Send each barcode as a string so a leading zero is kept. A 13-digit value is EAN-13, not a card number. An empty `products` list clears the map. A `200` body is `{"ok":true,"dataset":"barcode-map","asOf":"2026-11-12","products":4400,"barcodes":5100}`. `products` is the number of PLUs stored after a repeated code keeps the last row. `barcodes` is how many barcode tokens were stored.

Nightly push: put the SwiftPOS shelf barcode on `barcode-map` for the whole catalog, and also in the existing `barcode` field (and `barcodes` on retail-pricing) for products that have sold. Do not copy the PLU into the barcode slot. Join extra barcodes for the same product with commas. Do not send card numbers.

**online-orders** — Bopple pickup orders paid online (Stripe via Bopple). These are extra sales. They are not rung through the till. The portal adds `daily[].total` (gross) on top of till sales for day, month-to-date, and overview totals, and still shows Till, Online (Bopple), and Total separately. Shop GP is till GP only; online orders have no cost. A sample is `tools/fixtures/online-orders.json`.

```json
{"dataset":"online-orders","data":{
  "source":"Bopple Payments by Order",
  "asOf":"2026-11-11",
  "daily":[
    {"date":"2026-11-11","orders":1,"total":16.0,"fees":0.0,"refunds":0.0,"net":16.0}
  ],
  "orders":[
    {"order_no":"26672951","time":"2026-11-11 11:47:23 AM (Wed)","trading_date":"2026-11-11","wallet":"Link","card":"Visa","total":16.0,"fees":0.0,"refunds":0.0,"net":16.0}
  ]
}}
```

`asOf` is optional. If it is omitted, the latest `daily[].date` or `orders[].trading_date` is used. `source` is optional. `daily` rows are upserted by `date`. `orders` are upserted by `order_no`. Rows for other dates stay. On each daily row, `orders` is a whole number of orders that day. Money fields are numbers. `fees` and `refunds` keep Bopple’s sign (a fee is usually negative). Net payout is gross plus fees plus refunds. `card` is the brand (Visa, Master, Amex), not a card number. A card number, or a field named like `card_number`, `cvv`, or a bank account, is rejected and nothing from that upload is stored. `wallet` is a short name such as Card, Apple pay, Link, or Google pay. `time` is the Bopple timestamp text. If `daily` is omitted and `orders` is present, the portal builds the daily totals from the orders. At least one daily row or one order is required.

Each order may also include the Scheduled Orders export: `customer_name`, `order_type` (such as `Pickup`), `fulfilment_time` (such as `02:21pm`), and `items`. An item is `{category, product, qty, total, modifiers: [{category, name, qty, total}]}`. A modifier belongs to the product row above it. Orders that omit these fields still ingest. A later upload that omits them leaves the stored customer and items in place. `tools/parse_scheduled_orders.py` turns the CSV (UTF-8 with BOM) into that structure keyed by order number.

**shift-exceptions** — price overrides, fuel drive-offs, and account charges (IOU and on account) from the till. These are not extra sales. A sample is `tools/fixtures/shift-exceptions.json`. The usual nightly body is one trade date, the same JSON file the box already writes for that day. `row_count` is ignored. `rows` may be empty, which clears that date.

```json
{"dataset":"shift-exceptions","data":{
  "date":"2026-11-11",
  "location":"Silkwood POS1",
  "source":"SwiftPOS QUICKFUEL, location 1",
  "generated_at":"2026-11-12T07:41:24+10:00",
  "timezone":"Australia/Brisbane",
  "rows":[]
}}
```

`timezone`, when sent, must be `Australia/Brisbane`. `location`, `source`, and `generated_at` are optional. `asOf` is optional. If it is omitted, the latest trade date in the payload is used. A later upload does not move `asOf` backward, so re-sending an older day of the last seven leaves the newer watermark in place.

Each listed date is replaced. Rows for dates not in the payload stay. The row key is `type`, `date`, `receipt`, `time`, and `item` (case and extra spaces ignored). Two rows in one date with the same key keep the later one. A changed item does not leave the old row behind, because the whole date is deleted and written again.

To send several dates in one request, use `days` instead of a top-level `date` and `rows`:

```json
{"dataset":"shift-exceptions","data":{
  "days":[
    {"date":"2026-11-10","rows":[]},
    {"date":"2026-11-11","rows":[]}
  ]
}}
```

A backfill can send a flat `rows` list. Each row needs its own `date`. Add `dates` when a day in the export has no rows and should be cleared. Dates omitted from both `rows` and `dates` are left as they are.

```json
{"dataset":"shift-exceptions","data":{
  "dates":["2026-10-24","2026-11-11"],
  "rows":[]
}}
```

Do not send `days` together with `date`, `dates`, or `rows`. A `200` body includes `asOf`, `dates` (how many dates were replaced), `rows` (how many rows were stored), and `replaced` (those dates).

Every row needs `type`, `time` (`HH:MM:SS`), `receipt`, `staff`, `item`, and `amount`. `receipt` is a digit string (a whole number is accepted). `type` is `price_override`, `drive_off`, or `account`.

A price override also needs `subtype` (`shop` or `surcharge_default_zero`), `normal_price`, `charged_price`, and `difference`. `difference` is `(charged - normal) × quantity`. `plu` and `reason` are optional. `status` is empty.

A drive-off or account row also needs `account_type`, `status` (`paid`, `unpaid`, or `partial`), `status_basis` (`allocated_payments` or `fifo_estimate_vs_account_balance`), and `outstanding`. `account_type` is `Drive Off` for a drive-off, and `IOU` or `On account` for an account charge. `quantity`, `customer`, `rego`, `paid_on_account`, and `lines` are optional. `lines` entries are `item`, `plu`, `quantity`, `amount`, and `adjustment`.

A card number, a run of 12 or more digits, or a field named like `card_number`, `cvv`, `account`, `bsb`, or a bank account is rejected and nothing from that upload is stored. `account_type` is the charge kind, not a bank account. The page does not show card or bank numbers. Paid, unpaid, and partial for named on-account customers is an estimate.

The kitchen page date presets are Today, Yesterday, This week, Last 7 days, This month and Last month, plus From and To. Those dates apply to the kitchen totals, the group list and a single item. The nightly kitchen file is one total per day (`days[]` is `[date, kitchenSalesInc, coveredGp, coveredEx, kitchenSalesEx, shopSalesInc, packedItems]`). There is no hour field in that pack, so the item view says so. If a future day row includes an 8th `hours` list of `[hour, qty]`, that table is shown.

### Help chat

Logged-in pages include a Help panel. The portal does not call a model. Optional env vars, both unset by default:

| Name | Purpose |
|---|---|
| `PORTAL_CHAT_WEBHOOK_URL` | Where a new owner message is relayed. |
| `PORTAL_CHAT_WEBHOOK_KEY` | Bearer token for that POST. Never logged. |

`POST /api/portal/chat` requires the owner session and the CSRF header. Body:

```json
{"message":"What sold yesterday?","page":"/portal/kitchen","context":{"period":"yesterday"}}
```

The row is stored. If `PORTAL_CHAT_WEBHOOK_URL` is set, the portal POSTs this payload with `Authorization: Bearer <PORTAL_CHAT_WEBHOOK_KEY>`:

```json
{"id":1,"message":"What sold yesterday?","page":"/portal/kitchen","context":{"period":"yesterday"},"createdAt":"2026-11-11T15:00:00+10:00"}
```

The response `status` is `offline` when the URL is unset (the panel says “Assistant offline — your message is saved”), `sent` when the webhook accepted it, or `saved` when the webhook could not be reached. `GET /api/portal/chat` returns `{assistant, messages:[{id, message, page, context, reply, createdAt, repliedAt}]}`. `assistant` is `offline` or `online`. It does not include the webhook address. The panel polls that every 5 seconds while it is open and shows “Thinking…” until a reply when a webhook is configured. `POST /api/portal/chat/clear` wipes the thread.

The message context sent to the assistant includes the owner’s page query plus server figures for the month through the latest till day: `tillSales`, `onlineOrders` (`not loaded`, or order count, gross, fees, and net), `totalSales` (till plus online gross), and `shopGp` (till GP). Online orders are omitted from the total when that dataset has not been stored.

The assistant answers with the ingest key, not the owner session:

```
POST /api/portal/chat/reply
Authorization: Bearer <PORTAL_INGEST_KEY>

{"id":1,"reply":"Yesterday’s kitchen sales were **down** on the day before."}
```

Replies are shown as plain text with bold markers only. Raw HTML is escaped. `OPENAI_API_KEY` is not read.

**pay-rates** — hourly rates for employees on Staff hours. Owner-only. The logged-in Staff hours and Overview pages use them. They are not in `GET /api/portal/staff-hours`, the hours CSV, or any page that does not require the owner session. Re-posting replaces the whole list. An employee left out of the new list no longer has a rate. Names match the Staff hours names with case and extra spaces ignored.

Example only, not a real pay rate:

```json
{"dataset":"pay-rates","data":{"asOf":"2026-11-11","employees":[
  {"name":"Alex","ordinary":32.00,"saturday":48.00,"sunday":64.00,"publicHoliday":80.00,"superRate":0.12}
]}}
```

`asOf` is optional (`YYYY-MM-DD`). If it is omitted, the portal uses today in Australia/Brisbane. `employees` may be empty, which clears every rate. Each employee needs `name`, `ordinary` (Mon–Fri, when the day is not a public holiday), `saturday`, `sunday`, `publicHoliday`, and `superRate`. Rates are dollars per hour, from 0 to 1000. `superRate` is a fraction from 0 to 1. `0.12` is the 12% super guarantee. Do not send `12` for 12%. The rate follows the day the shift started. A Queensland public holiday uses `publicHoliday`, including when it falls on a weekend. Christmas Eve uses `publicHoliday` only when the shift starts at or after 6pm. Anyone with hours and no matching name is shown as “No rate yet” and left out of the wage totals. A `200` body is `{"ok":true,"dataset":"pay-rates","asOf":"2026-11-11","employees":1}`.

### Staff hours read

The portal page is `/portal/hours` (Monday–Sunday week, Brisbane). The box can read the same week without a browser session:

```
GET /api/portal/staff-hours?week=2026-11-09
Authorization: Bearer <PORTAL_INGEST_KEY>
```

`week` is any date in the week; the response uses that week's Monday. The JSON has `timezone`, `week`, `weekEnding`, `days` (weekend and Queensland public holiday flags), `people` (daily hours, shift start/finish/break/notes), `grandTotal`, and a note that nothing is sent to Xero. It does not include pay rates, wage costs, or contract entries. CSV for the pay run is downloaded from the page while logged in and is the same employee hours only. Contract hours are entered on the page (a name, a date, hours and an hourly rate, or a flat amount). The hourly rate is saved for that contractor. Contract costs have no super. They are in the on-screen wage bill and in wages vs sales, and they are not in this feed or the CSV. The portal does not create or post a pay run.

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
