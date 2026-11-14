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

`dataset` is one of `sales`, `purchases`, `fuel-commission`, `fuel-dips`, `insights`, `invoices`, `product-search`, `retail-pricing`, `till-price-changes`, `shifts`, `last-year`, `cost-overrides`, `stock-movements`, `online-orders`, `shift-exceptions`, `barcode-map`, `last-year-monthly`, `last-year-plu`, `pay-rates`, `xero-pnl`. A `200` body is `{"ok": true, "dataset": "...", "asOf": "YYYY-MM-DD", ...}`. Missing or wrong key is `401`. If `PORTAL_INGEST_KEY` is unset the route is `503` and stores nothing. The same payload posted again updates that date; older dates already stored are kept. `pay-rates` is the exception: re-posting replaces the whole list. `xero-pnl` replaces the account rows for that month and keeps every other month.

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

`amount_due` is what is still owing. On a paid bill it is usually 0, so it is not the amount that was paid. Optional `amount` is the invoice total (inc GST), a number. Optional `paid_date` is `YYYY-MM-DD`. When those two are present, the paid list shows the paid date and the total paid is the sum of `amount`. Without them the paid list still shows who is paid, and the total stays blank. Optional `invoice_id` is the Xero invoice id. A later upload that omits it keeps the id already stored.

The Invoices page can filter by supplier (same checklist as Purchases: search, All suppliers, Clear). The choice is in the query string.

**Owner mark as paid.** On an unpaid row, Mark as paid asks for method (Card, Cash, Bank transfer, Direct debit), date (default today, Australia/Brisbane), amount (defaults to the amount due), and an optional note. It is stored in the portal database, keyed by supplier and invoice number, and is not deleted when the invoices dataset is posted again. The row then reads Paid – awaiting Xero match and can be edited or undone. When a later ingest says the bill is paid, or the match endpoint below is called, it reads Paid – matched in Xero and the owner's method, date, amount, and note stay on the row. Pay still only opens the supplier's own page, and only while the bill is unpaid. The portal does not pay anyone.

```
GET /api/portal/invoice-payments
Authorization: Bearer <PORTAL_INGEST_KEY>
```

`200` body:

```json
{"payments":[{"invoiceId":null,"invoiceNumber":"577803222","supplier":"Bega Dairy","amount":89.72,"method":"Cash","date":"2026-11-13","note":""}]}
```

Only owner records still awaiting a Xero match. `invoiceId` is the Xero id when `invoice_id` was ingested, otherwise `null`. `method` is one of `Card`, `Cash`, `Bank transfer`, `Direct debit`. `date` is `YYYY-MM-DD`. `note` is a string, empty when omitted.

```
POST /api/portal/invoice-payments/match
Authorization: Bearer <PORTAL_INGEST_KEY>
Content-Type: application/json

{"supplier":"Bega Dairy","invoiceNumber":"577803222","xeroPaymentReference":"PAY-100"}
```

Send `invoiceId` instead of `supplier` and `invoiceNumber` when you have the Xero id. `200` body:

```json
{"ok":true,"invoiceId":null,"invoiceNumber":"577803222","supplier":"Bega Dairy","xeroPaymentReference":"PAY-100","matchedAt":"2026-11-13 09:00:00","alreadyMatched":false}
```

`matchedAt` is Australia/Brisbane, `YYYY-MM-DD HH:MM:SS`. Posting again returns the stored reference and `"alreadyMatched": true`. Missing key is `503`. Wrong key is `401`. No matching owner record is `404`. This does not create a payment in Xero.

**Purchases filters.** `/portal/purchases` has This week, This month, Last month, and a custom From/To. The supplier checklist is every supplier in the purchases data, All suppliers by default, with a search box and Clear. Totals, the supplier table, the previous-period table, the bill list, and the weekly chart follow both filters. Shop sales in the weekly chart stay the whole shop. The query string is the bookmark.

**Daily summary.** `/portal/day` is one day (date picker, previous day, next day). It opens on the latest loaded sales day. It shows that day's till, online, and combined sales, fuel, shop, and food, commission and GP, fuel litres and tank dips, staff hours, till exceptions, closing checklist and hot-box activity when those rows exist, and invoices dated that day. A missing feed says there is no data. Overview keeps the month totals, last year comparison, last-year product card, fuel target, and insights, plus a today/yesterday card that links here.

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

**last-year-plu** — the overview Last year comparison. This is separate from `last-year` (product file, ex GST) and from `last-year-monthly` (the shop, food, and fuel-litre lines). One row is one whole calendar month from a SwiftPOS PLU Sales Total. Dollars are inc GST. Fuel litres are the grade quantities. Re-posting a month replaces that month. Other months stay. A null figure is blank on the page. It is not shown as zero. A month with no row says “Last year not loaded yet”.

The comparison follows the month picker on the overview (or the month of the date range when the picker is left alone). This year is the loaded till days for that month: total, fuel, and shop from the daysheet, fuel litres and grade dollars from the fuel-commission file, and shop departments from the nightly sales snapshot when that snapshot covers the same first and last loaded day. Same days last year is the stored month’s daily average times the number of loaded days. It is not a day-by-day file. The full month is shown beside it. Online orders are not included.

November 2025 is seeded from `tools/fixtures/ly-plu-months.json` the first time the server starts, and only when that month is not already stored. A later ingest of November 2025 is kept across restarts.

To add another month, copy one object in that file and POST it. No new environment variable. The numbers in the sample below show the shape. They are not October’s figures.

```json
{"dataset":"last-year-plu","data":{"months":[
  {"month":"2025-10","label":"October 2025","fromDate":"2025-10-01","toDate":"2025-10-31","days":31,
   "tradingWindow":"1/10/2025 6:00:00 AM to 1/11/2025 5:59:59 AM",
   "source":"SwiftPOS PLU Sales Total","note":"Whole month. Same-days figures are the daily average.",
   "totalInc":360000.00,"totalEx":330000.00,"shopInc":160000.00,"shopEx":150000.00,
   "fuelInc":200000.00,"fuelEx":180000.00,"fuelLitres":110000.00,
   "fuelGrades":[
     {"grade":"DIS","name":"Diesel","litres":60000.00,"salesInc":110000.00,"salesEx":100000.00},
     {"grade":"ULP","name":"Unleaded","litres":40000.00,"salesInc":75000.00,"salesEx":68000.00},
     {"grade":"P95","name":"Premium 95","litres":10000.00,"salesInc":15000.00,"salesEx":12000.00}
   ],
   "departments":[
     {"name":"Roadhouse","salesInc":60000.00,"salesEx":59000.00,"qty":10000},
     {"name":"Beverages","salesInc":50000.00,"salesEx":46000.00,"qty":9000},
     {"name":"A Fuel","salesInc":200000.00,"salesEx":180000.00,"qty":110000.00,"fuel":true}
   ]}
]}}
```

`month` is `YYYY-MM`. `days` is how many days the monthly total covers (use the calendar length when the report is the whole month). `totalInc` is the report total inc GST. `shopInc` is total minus fuel. `fuelGrades[].grade` is `DIS`, `ULP`, or `P95`. Department `name` must match this year’s sales file (`Roadhouse`, `Beverages`, `Tobacco`, `Confectionery`, and the other SwiftPOS group names). Set `"fuel": true` on the fuel group so it stays in the fuel block. Any money or litre field may be null. Do not send cost, GP, coverage, or a card number. The upload is rejected and nothing from it is stored.

From the PLU Sales Total PDF, use each group total’s Sales Inc as `salesInc`, Sales Ex as `salesEx`, and the quantity as `qty`. Fuel grade rows are the diesel, unleaded, and premium lines. The printed report total is `totalInc` / `totalEx`.

A `200` body is `{"ok":true,"dataset":"last-year-plu","asOf":"2025-10-31","months":1}`. `months` is how many rows this upload wrote. Optional `asOf` is `YYYY-MM-DD`. If it is omitted, the portal uses the last day of the latest month in the payload.

To keep the month for the next empty database, add the same object to `tools/fixtures/ly-plu-months.json`. The server inserts a seeded month only when that month is not already in the database. It does not replace a month you have already posted.

November 2025 in the seed file is the PLU report for 1 Nov 2025 6:00am to 1 Dec 2025 5:59am: total $397,328.81 inc GST ($368,303.31 ex), fuel $213,140.10 over 115,281.947 L, shop $184,188.71. The main Confectionery group is $5,406.36. A separate SwiftPOS group 7, also named Confectionery on the report ($38.87), is stored as `Confectionery (group 7)` so the two are not added together. Group sales ex GST on that report add to one cent more than the printed report total. The stored total uses the printed report total.

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

The JSON has `timezone`, `baselineRule`, and `products[]` (`code`, `name`, `group`, `department`, `barcode`, `baselineDate`, `baselineAt`, `openingQty`, `soldSince`, `receivedSince`, `onHand`, `daysCover`, `lastUpdated`, `note`, `manual`, `supplier`, `supplierSource`). `manual` is true when the owner added the product on the portal rather than from the till catalog. If they leave the code blank, the portal assigns a stable id such as `M-0001`. Post `supplier-invoice` movements against that code. Editing the code later keeps the counts, movements, and saved supplier. Stop tracking hides the product from this list and leaves the history in place. Send movements for tracked codes from the day after `baselineDate`. `barcode` on that payload is still the single catalog or manual barcode. Extra barcodes the owner links from the phone are not in this list.

`supplier` is who to order that product from, or null. `supplierSource` is `manual` when the owner saved it, `suggested` when a supplier-invoice movement’s `reference` matches one purchase bill or invoice number, or null. The newest unambiguous delivery wins. A manual name replaces the suggestion. Do not send suppliers in the nightly upload. Re-posting `stock-movements`, `purchases`, `invoices`, or `product-search` does not change a saved supplier. The owner sets it on Products and on Stock on hand. The picker lists suppliers already on purchases and invoices, and a new name can be typed. Unassigned is the filter for products with no saved supplier and no suggestion. Sort by Supplier, or Group by supplier, for a per-supplier list to print. The table `portal_product_suppliers` is created on startup. No separate migration.

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

**xero-pnl** — one month of the Xero Profit and Loss, account by account. Xero has purchases and expenses. It does not have till sales. The Profit & Loss page (`/portal/pnl`) uses till fuel and shop, plus Bopple online gross, each divided by 1.1 so income is ex GST. Cost of sales and operating expenses are these Xero amounts, already ex GST. Purchases are by bill date, not stock used. Re-posting a month replaces that month. Other months stay. A month with no rows says “Xero P&L not loaded yet”.

Wages and super on the page are the Xero accounts when the report includes them (names such as Wages and Salaries or Superannuation). Contract hours typed on Staff hours are not in Xero. The page adds them as their own line. It does not also add the portal’s employee wage cost when those Xero accounts are present.

Portal rent is typed on the Profit & Loss page. It is not an ingest dataset and nothing is sent to Xero. The default is $321.13 a day including GST from 1 Jul 2026. The page divides that by 1.1 ($291.94 ex GST a day) and multiplies by the days in the column. A finished month counts every day. The current month counts 1st through the same date the page uses for sales. Saving a later effective date keeps earlier months on the earlier rate. United Petroleum direct debits coded to Purchases - Fuel already include this rent, so the page shows the ex-GST rent as an operating expense and removes the same amount from fuel purchases (`Less rent included in United fuel debits`). The two lines offset: gross profit rises by the rent amount and operating expenses rise by the same amount, so total costs and net profit do not change. If that month’s Xero report already has an account named like Rent or Rent & Lease, both portal lines are left out and Purchases - Fuel stays as Xero recorded it.

Manual adjustment lines are also typed on that page. Each line has a label, a section (cost of sales, operating expenses, income, or other), an optional account such as Purchases - Fuel, an amount ex GST, and an optional note. They are stored in the portal database. The owner can add, edit, and delete them. They are marked on the statement as a manual portal adjustment, not from Xero. A positive amount increases that section, and so the month’s totals, gross profit, and net profit. The previous-month column uses the same lines. Cost of sales is added before the United fuel rent line, so rent inside a fuel debit is still removed once and is not counted twice. The nightly `xero-pnl` upload replaces Xero account rows only. It does not change these lines. October 2026 is seeded with one line, `United fuel debits 1–20 Oct (missing from Xero)`, on Purchases - Fuel, for $175,944.56 ex GST ($193,539.02 inc GST on United advices CSJR765–779, including the 15 Oct debit coded to balance-sheet account 627). Seeding again does not add a second copy. Deleting that line keeps it deleted.

`tools/xero_pnl.py` converts a Xero report into this body. It accepts the row array, the sample file `tools/fixtures/xero-pnl-oct-2026-sample.txt` (a short preamble, then the JSON array), or the official report object (`Reports[0].Rows`, PascalCase or camelCase). Pass the month. A report pulled on 14 November for October is still `--month 2026-10`. One amount column only. Summary rows, gross profit, and net profit are left out. The tool prints JSON. It does not read `PORTAL_INGEST_KEY` and it does not post.

```
python3 tools/xero_pnl.py tools/fixtures/xero-pnl-oct-2026-sample.txt --month 2026-10
```

Post that body with the existing ingest key. Do not put the key in the file or on the command line of the converter.

```json
{"dataset":"xero-pnl","data":{
  "month":"2026-10",
  "asOf":"2026-10-31",
  "source":"Xero Profit and Loss",
  "accounts":[
    {"section":"Less Cost of Sales","accountName":"Purchases - Fuel","accountId":"6db74470-bcc1-4f6d-bb5a-8fb739bea54c","amount":79072.96},
    {"section":"Less Operating Expenses","accountName":"Wages and Salaries","accountId":"9fc02881-ac9b-4703-bf81-f5e933541891","amount":21199.73}
  ]
}}
```

`month` is `YYYY-MM`. `asOf` is optional (`YYYY-MM-DD`). If it is omitted, the portal uses the last day of that month. `source` is optional. `accounts` is required and must not be empty. `section` is the Xero section title, such as `Less Cost of Sales` or `Less Operating Expenses`. `accountName` is the account name. `accountId` is the Xero account id. `amount` is the ex-GST dollars Xero prints, and may be negative. Do not send a field named `account`. That name is reserved for bank details and the upload is rejected. A `200` body is `{"ok":true,"dataset":"xero-pnl","asOf":"2026-10-31","month":"2026-10","accounts":13}`. `accounts` is how many rows this upload stored.

### Staff hours read

The portal page is `/portal/hours` (Monday–Sunday week, Brisbane). The box can read the same week without a browser session:

```
GET /api/portal/staff-hours?week=2026-11-09
Authorization: Bearer <PORTAL_INGEST_KEY>
```

`week` is any date in the week; the response uses that week's Monday. The JSON has `timezone`, `week`, `weekEnding`, `days` (weekend and Queensland public holiday flags), `people` (daily hours, shift start/finish/break/notes), `grandTotal`, and a note that nothing is sent to Xero. It does not include pay rates, wage costs, or contract entries. CSV for the pay run is downloaded from the page while logged in and is the same employee hours only. Contract hours are entered on the page, per day, in a week grid that matches the pay run. The hourly rate is saved for that contractor. Leave a day blank and save to clear that day's hours. A flat amount is still a single entry. Contract costs have no super. They are in the on-screen wage bill and in wages vs sales, and they are not in this feed or the CSV. Tap a pay-run name or a contract name to open that person's dates, hours, rates, and cost for the week on screen. The portal does not create or post a pay run. Nightly ingest does not write contract hours.

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
