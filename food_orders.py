"""Kitchen order wait times.

The Orders tab is manual. Staff tap New order when food is rung up and Done when
it is handed over. That pair is the wait. Till receipts are optional: they land
too late to drive the screen, and a held phone sale is missing until it is paid.
The ingest stores those receipts for later matching. It does not open a card.

Times are Australia/Brisbane. Nothing here logs secrets, keys, or the database URL.
"""
import json
import re
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP

FMT = "%Y-%m-%d %H:%M:%S"
UNDO_MINUTES = 5
REPORT_CAP = 5000
INGEST_MAX = 1_000_000
RATE_MAX = 60
RATE_WINDOW = 60
_hits = {}

SCHEMA = """
CREATE TABLE IF NOT EXISTS food_orders(
  id INTEGER PRIMARY KEY,
  ticket_no INTEGER NOT NULL,
  trading_date TEXT NOT NULL,
  started_at TEXT NOT NULL,
  source TEXT NOT NULL,
  customer TEXT,
  done_at TEXT,
  done_by TEXT,
  created_by TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_food_orders_ticket ON food_orders(trading_date, ticket_no);
CREATE INDEX IF NOT EXISTS ix_food_orders_open ON food_orders(done_at, started_at);
CREATE INDEX IF NOT EXISTS ix_food_orders_date ON food_orders(trading_date);
CREATE TABLE IF NOT EXISTS food_order_undo(
  id INTEGER PRIMARY KEY CHECK (id = 1),
  order_id INTEGER NOT NULL,
  kind TEXT NOT NULL,
  at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS food_order_receipts(
  id INTEGER PRIMARY KEY,
  order_key TEXT NOT NULL UNIQUE,
  receipt TEXT NOT NULL UNIQUE,
  held_receipt TEXT,
  rung_up TEXT NOT NULL,
  trading_date TEXT NOT NULL,
  source TEXT NOT NULL,
  status TEXT NOT NULL,
  customer TEXT,
  items TEXT NOT NULL,
  paid_at TEXT,
  received_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_food_order_receipts_date ON food_order_receipts(trading_date);
CREATE INDEX IF NOT EXISTS ix_food_order_receipts_held ON food_order_receipts(held_receipt);
"""


def ensure(c):
    c.executescript(SCHEMA)


def one(c, sql, params=()):
    row = c.execute(sql, params).fetchone()
    return dict(row) if row else None


def many(c, sql, params=()):
    return [dict(row) for row in c.execute(sql, params).fetchall()]


def _now():
    import server
    return server.now()


def stamp(when=None):
    return (when or _now()).strftime(FMT)


def _unique(ex):
    name = type(ex).__name__
    if name in ("IntegrityError", "UniqueViolation"):
        return True
    text = str(ex).lower()
    return "unique" in text or "duplicate" in text


def clean_customer(value, required=False, label="customer", limit=60):
    import portal
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            raise ValueError(f"{label} is required")
        return None
    if not isinstance(value, str):
        raise ValueError(f"{label} must be text")
    text = " ".join(value.split())
    if not text:
        if required:
            raise ValueError(f"{label} is required")
        return None
    if len(text) > limit:
        raise ValueError(f"{label} is too long")
    if portal.has_card_number(text):
        raise ValueError(f"{label} must not include a card number")
    return text


def manual_source(value):
    if value is None or value == "":
        return "counter"
    if not isinstance(value, str):
        raise ValueError("source must be counter or phone")
    key = value.strip().casefold()
    if key == "counter":
        return "counter"
    if key == "phone":
        return "phone"
    raise ValueError("source must be counter or phone")


def parse_id(value):
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError("id is required")
    return value


def _set_undo(c, order_id, kind, at):
    c.execute(
        """INSERT INTO food_order_undo(id, order_id, kind, at) VALUES(1,?,?,?)
           ON CONFLICT(id) DO UPDATE SET order_id=excluded.order_id, kind=excluded.kind, at=excluded.at""",
        (order_id, kind, at),
    )


def _slot(c):
    return one(c, "SELECT order_id, kind, at FROM food_order_undo WHERE id=1")


def _fresh(at, now_stamp):
    try:
        delta = datetime.strptime(now_stamp, FMT) - datetime.strptime(at, FMT)
    except (TypeError, ValueError):
        return False
    return timedelta(0) <= delta <= timedelta(minutes=UNDO_MINUTES)


def create_manual(c, staff, source, customer, when=None):
    """Open one card. The number restarts at 1 each Brisbane day."""
    when = when or _now()
    started = when.strftime(FMT)
    day = when.date().isoformat()
    source = manual_source(source)
    customer = clean_customer(customer, limit=40)
    new_id = None
    ticket = None
    for _ in range(5):
        current = one(c, "SELECT COALESCE(MAX(ticket_no), 0) AS n FROM food_orders WHERE trading_date=?", (day,))
        ticket = int(current["n"]) + 1
        c.execute("SAVEPOINT fo_ticket")
        try:
            cur = c.execute(
                """INSERT INTO food_orders(ticket_no, trading_date, started_at, source, customer, created_by)
                   VALUES(?,?,?,?,?,?) RETURNING id""",
                (ticket, day, started, source, customer, staff),
            )
            new_id = dict(cur.fetchone())["id"]
            c.execute("RELEASE SAVEPOINT fo_ticket")
            break
        except Exception as ex:
            c.execute("ROLLBACK TO SAVEPOINT fo_ticket")
            c.execute("RELEASE SAVEPOINT fo_ticket")
            if not _unique(ex):
                raise
            new_id = None
    if new_id is None:
        raise ValueError("could not number that order")
    _set_undo(c, new_id, "create", started)
    return None


def mark_done(c, staff, order_id, when=None):
    order_id = parse_id(order_id)
    when = when or _now()
    done_at = when.strftime(FMT)
    row = one(c, "SELECT id, done_at FROM food_orders WHERE id=?", (order_id,))
    if not row or row["done_at"]:
        return "not waiting", 404
    cur = c.execute(
        "UPDATE food_orders SET done_at=?, done_by=? WHERE id=? AND done_at IS NULL",
        (done_at, staff, order_id),
    )
    if cur.rowcount != 1:
        return "not waiting", 404
    _set_undo(c, order_id, "done", done_at)
    return None, 200


def undo(c, order_id, when=None):
    """Revert only the last New order or Done, and only within a few minutes."""
    order_id = parse_id(order_id)
    now_stamp = stamp(when)
    slot = _slot(c)
    if not slot or not _fresh(slot["at"], now_stamp):
        return "nothing to undo", 404
    if slot["order_id"] != order_id:
        return "that was not the last tap", 409
    if slot["kind"] == "create":
        cur = c.execute("DELETE FROM food_orders WHERE id=? AND done_at IS NULL", (order_id,))
        if cur.rowcount != 1:
            return "nothing to undo", 404
    elif slot["kind"] == "done":
        cur = c.execute(
            "UPDATE food_orders SET done_at=NULL, done_by=NULL WHERE id=? AND done_at IS NOT NULL",
            (order_id,),
        )
        if cur.rowcount != 1:
            return "nothing to undo", 404
    else:
        return "nothing to undo", 404
    c.execute("DELETE FROM food_order_undo WHERE id=1")
    return None, 200


def open_orders(c):
    out = []
    for row in many(c, "SELECT * FROM food_orders WHERE done_at IS NULL ORDER BY started_at, id"):
        out.append({
            "id": row["id"],
            "ticket": row["ticket_no"],
            "source": row["source"],
            "customer": row["customer"] or "",
            "started_at": row["started_at"],
            "trading_date": row["trading_date"],
        })
    return out


def undo_view(c, when=None):
    slot = _slot(c)
    if not slot or not _fresh(slot["at"], stamp(when)):
        return None
    order = one(c, "SELECT id, ticket_no FROM food_orders WHERE id=?", (slot["order_id"],))
    if not order:
        return None
    return {"id": order["id"], "ticket": order["ticket_no"], "kind": slot["kind"]}


def attach(ex, c):
    ex["food_orders"] = open_orders(c)
    ex["food_order_undo"] = undo_view(c)


def wait_seconds(start, done):
    try:
        delta = datetime.strptime(done, FMT) - datetime.strptime(start, FMT)
    except (TypeError, ValueError):
        return 0
    return max(0, int(delta.total_seconds()))


def over_15(start, done):
    return wait_seconds(start, done) > 15 * 60


def _round_min(seconds):
    if seconds is None:
        return None
    minutes = (Decimal(str(seconds)) / Decimal(60)).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
    return float(minutes)


def _median(values):
    ordered = sorted(values)
    count = len(ordered)
    mid = count // 2
    if count % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


def _stats(records):
    done = [row for row in records if row["seconds"] is not None]
    waits = [row["seconds"] for row in done]
    return {
        "orders": len(records),
        "done": len(done),
        "open": len(records) - len(done),
        "over15": sum(1 for row in done if row["seconds"] > 15 * 60),
        "avgMin": _round_min(sum(waits) / len(waits)) if waits else None,
        "medianMin": _round_min(_median(waits)) if waits else None,
        "maxMin": _round_min(max(waits)) if waits else None,
    }


def _public_manual(row):
    seconds = wait_seconds(row["started_at"], row["done_at"]) if row["done_at"] else None
    return {
        "id": row["id"],
        "ticket": row["ticket_no"],
        "date": row["trading_date"],
        "source": row["source"],
        "customer": row["customer"] or "",
        "startedAt": row["started_at"],
        "doneAt": row["done_at"],
        "doneBy": row["done_by"] or "",
        "waitMin": _round_min(seconds) if seconds is not None else None,
        "over15": bool(seconds is not None and seconds > 15 * 60),
        "open": row["done_at"] is None,
        "seconds": seconds,
    }


def _load_items(raw):
    try:
        items = json.loads(raw or "[]")
    except json.JSONDecodeError:
        return []
    return items if isinstance(items, list) else []


def _public_receipt(row):
    return {
        "receipt": row["receipt"],
        "orderId": row["order_key"],
        "heldReceipt": row["held_receipt"],
        "status": row["status"],
        "source": row["source"],
        "customer": row["customer"] or "",
        "startedAt": row["rung_up"],
        "paidAt": row["paid_at"],
        "date": row["trading_date"],
        "items": _load_items(row["items"]),
    }


def report_payload(c, start, end):
    manual = many(
        c,
        """SELECT * FROM food_orders WHERE trading_date>=? AND trading_date<=?
           ORDER BY started_at, id LIMIT ?""",
        (start.isoformat(), end.isoformat(), REPORT_CAP + 1),
    )
    receipts = many(
        c,
        """SELECT * FROM food_order_receipts WHERE trading_date>=? AND trading_date<=?
           ORDER BY rung_up, id LIMIT ?""",
        (start.isoformat(), end.isoformat(), REPORT_CAP + 1),
    )
    truncated = len(manual) > REPORT_CAP or len(receipts) > REPORT_CAP
    manual = manual[:REPORT_CAP]
    receipts = receipts[:REPORT_CAP]
    orders = [_public_manual(row) for row in manual]
    by_day = {}
    by_hour = {}
    for order in orders:
        by_day.setdefault(order["date"], []).append(order)
        hour = int(order["startedAt"][11:13])
        by_hour.setdefault((order["date"], hour), []).append(order)
    days = [{"date": day, **_stats(by_day[day])} for day in sorted(by_day)]
    hours = [
        {"date": day, "hour": hour, **_stats(by_hour[(day, hour)])}
        for day, hour in sorted(by_hour)
    ]
    public_orders = []
    for order in orders:
        item = dict(order)
        item.pop("seconds", None)
        public_orders.append(item)
    return {
        "from": start.isoformat(),
        "to": end.isoformat(),
        "timezone": "Australia/Brisbane",
        "truncated": truncated,
        "summary": _stats(orders),
        "days": days,
        "hours": hours,
        "orders": public_orders,
        "receipts": [_public_receipt(row) for row in receipts],
    }


def _fmt_min(value):
    if value is None:
        return "—"
    return f"{value:.1f}"


def _hour_label(hour):
    suffix = "am" if hour < 12 else "pm"
    clock = hour % 12 or 12
    return f"{clock} {suffix}"


def _source_label(source):
    return {"counter": "Counter", "phone": "Phone", "bopple": "Bopple"}.get(source, source)


def _short_time(value):
    if not value:
        return "—"
    try:
        parsed = datetime.strptime(value, FMT)
    except ValueError:
        return value
    hour = parsed.hour % 12 or 12
    suffix = "am" if parsed.hour < 12 else "pm"
    return f"{hour}:{parsed.minute:02d} {suffix}"


def _items_text(items):
    parts = []
    for item in items:
        qty = item.get("qty")
        if isinstance(qty, float) and qty.is_integer():
            qty = int(qty)
        name = item.get("name") or ""
        parts.append(f"{qty} × {name}")
    return ", ".join(parts)


def _stat_card(card_id, label, value, note):
    import portal
    return (
        f'<div class="card stat" id="{card_id}"><div class="lbl">{portal.h(label)}</div>'
        f"<b>{portal.h(value)}</b><span>{portal.h(note)}</span></div>"
    )


def _stats_row(cells):
    import portal
    return "<tr>" + "".join(f"<td{cls}>{portal.h(text)}</td>" for cls, text in cells) + "</tr>"


def page(c, query):
    import portal
    anchor = portal.today()
    period = (query or {}).get("period")
    if not period and (query or {}).get("from") and (query or {}).get("to"):
        period = "custom"
    try:
        active, start, end = portal.period_bounds(period or "last7", (query or {}).get("from"), (query or {}).get("to"), anchor)
        error = ""
    except ValueError as ex:
        active, start, end = portal.period_bounds("last7", None, None, anchor)
        error = f'<p class="alert">{portal.h(str(ex))}</p>'
    payload = report_payload(c, start, end)
    summary = payload["summary"]
    cards = "".join((
        _stat_card("order-times-count", "Orders", str(summary["orders"]), "in this range"),
        _stat_card("order-times-avg", "Average", _fmt_min(summary["avgMin"]), "minutes"),
        _stat_card("order-times-median", "Median", _fmt_min(summary["medianMin"]), "minutes"),
        _stat_card("order-times-max", "Longest", _fmt_min(summary["maxMin"]), "minutes"),
        _stat_card("order-times-over", "Over 15 min", str(summary["over15"]), "handed over"),
        _stat_card("order-times-open", "Not marked done", str(summary["open"]), "still open or never tapped"),
    ))
    day_rows = []
    for row in payload["days"]:
        day_rows.append(_stats_row([
            ("", portal.fmt_date(row["date"])),
            (' class="num"', str(row["orders"])),
            (' class="num"', str(row["done"])),
            (' class="num"', str(row["open"])),
            (' class="num"', str(row["over15"])),
            (' class="num"', _fmt_min(row["avgMin"])),
            (' class="num"', _fmt_min(row["medianMin"])),
            (' class="num"', _fmt_min(row["maxMin"])),
        ]))
    if day_rows:
        day_table = (
            '<div class="tw" id="order-times-days"><table><thead><tr>'
            "<th>Day</th><th>Orders</th><th>Done</th><th>Not marked done</th><th>Over 15 min</th>"
            "<th>Average</th><th>Median</th><th>Longest</th></tr></thead><tbody>"
            + "".join(day_rows) + "</tbody></table></div>"
        )
    else:
        day_table = '<p class="empty" id="order-times-days">No kitchen orders in this range.</p>'
    hour_rows = []
    for row in payload["hours"]:
        hour_rows.append(_stats_row([
            ("", portal.fmt_date(row["date"])),
            ("", _hour_label(row["hour"])),
            (' class="num"', str(row["orders"])),
            (' class="num"', str(row["done"])),
            (' class="num"', str(row["open"])),
            (' class="num"', str(row["over15"])),
            (' class="num"', _fmt_min(row["avgMin"])),
            (' class="num"', _fmt_min(row["medianMin"])),
            (' class="num"', _fmt_min(row["maxMin"])),
        ]))
    if hour_rows:
        hour_table = (
            '<div class="tw" id="order-times-hours"><table><thead><tr>'
            "<th>Day</th><th>Hour</th><th>Orders</th><th>Done</th><th>Not marked done</th>"
            "<th>Over 15 min</th><th>Average</th><th>Median</th><th>Longest</th>"
            "</tr></thead><tbody>" + "".join(hour_rows) + "</tbody></table></div>"
        )
    else:
        hour_table = '<p class="empty" id="order-times-hours">No kitchen orders in this range.</p>'
    raw_rows = []
    for order in payload["orders"]:
        raw_rows.append(_stats_row([
            ("", f"#{order['ticket']}"),
            ("", portal.fmt_date(order["date"])),
            ("", _source_label(order["source"])),
            ("", order["customer"] or "—"),
            ("", _short_time(order["startedAt"])),
            ("", _short_time(order["doneAt"])),
            (' class="num"', _fmt_min(order["waitMin"])),
            ("", "Yes" if order["over15"] else "—"),
            ("", order["doneBy"] or "—"),
        ]))
    if raw_rows:
        raw_table = (
            '<div class="tw" id="order-times-rows"><table><thead><tr>'
            "<th>#</th><th>Day</th><th>Source</th><th>Name</th><th>Started</th><th>Handed over</th>"
            "<th>Wait min</th><th>Over 15</th><th>Done by</th></tr></thead><tbody>"
            + "".join(raw_rows) + "</tbody></table></div>"
        )
    else:
        raw_table = '<p class="empty" id="order-times-rows">No kitchen orders in this range.</p>'
    receipt_rows = []
    for row in payload["receipts"]:
        receipt_rows.append(_stats_row([
            ("", row["receipt"]),
            ("", row["orderId"]),
            ("", "Held" if row["status"] == "held" else "Paid"),
            ("", _source_label(row["source"])),
            ("", row["customer"] or "—"),
            ("", _short_time(row["startedAt"])),
            ("", _short_time(row["paidAt"])),
            ("", _items_text(row["items"]) or "—"),
        ]))
    if receipt_rows:
        receipt_table = (
            '<div class="tw" id="order-times-receipts"><table><thead><tr>'
            "<th>Receipt</th><th>Order id</th><th>Status</th><th>Source</th><th>Name</th>"
            "<th>Till time</th><th>Paid at</th><th>Items</th></tr></thead><tbody>"
            + "".join(receipt_rows) + "</tbody></table></div>"
        )
    else:
        receipt_table = '<p class="empty" id="order-times-receipts">No till receipts stored for this range.</p>'
    truncated = ""
    if payload["truncated"]:
        truncated = '<p class="alert">This range is too long to show in full. Narrow the dates.</p>'
    return f"""<div class="ph"><h1>Order times</h1>
<p>How long food waits, from the kitchen tap on New order until Done. Compare a range before and after a menu change.</p>
<p class="help">Average, median and longest use handed-over orders only. Not marked done were never tapped Done. Over 15 min means the wait was longer than 15 minutes. Phone and counter use the same clock: it starts at the New order tap.</p></div>
{error}{portal.period_controls('/portal/order-times', active, start, end)}
{truncated}
<div class="grid g3" id="order-times-summary">{cards}</div>
<section class="card"><h2>By day</h2>
<p class="help">Oldest day first. A day with no orders is left out.</p>
{day_table}</section>
<section class="card"><h2>By hour</h2>
<p class="help">The hour the kitchen tapped New order, Australia/Brisbane.</p>
{hour_table}</section>
<section class="card"><h2>Every kitchen order</h2>
<p class="help">These rows stay, so the before and after can be checked again. Wait is blank until Done is tapped.</p>
{raw_table}</section>
<section class="card"><h2>Till receipts</h2>
<p class="help">Optional copy from the till. Receipts often arrive 20 minutes or more later, and a held phone sale is missing until it is paid. They are kept for later matching. They are not the wait times above, and they do not open a card on the kitchen screen.</p>
{receipt_table}</section>"""


def _bounds(query):
    import portal
    period = (query or {}).get("period")
    if not period and (query or {}).get("from") and (query or {}).get("to"):
        period = "custom"
    return portal.period_bounds(period or "last7", (query or {}).get("from"), (query or {}).get("to"), portal.today())


def handle_report(handler, query):
    import portal
    user = portal.session_user(handler)
    key = portal.ingest_key()
    if not user and not (key and portal.secret_eq(portal.bearer_token(handler), key)):
        handler.send(401, {"error": "login required"})
        return
    try:
        _active, start, end = _bounds(query)
    except ValueError as ex:
        handler.send(400, {"error": str(ex)[:180]})
        return
    c = portal.db()
    try:
        payload = report_payload(c, start, end)
    finally:
        c.close()
    handler.send(200, payload, extra=[("X-Robots-Tag", "noindex, nofollow")])


def _rate_ok(ip):
    import time
    now_ts = time.time()
    hits = [t for t in _hits.get(ip, []) if now_ts - t < RATE_WINDOW]
    if len(hits) >= RATE_MAX:
        _hits[ip] = hits
        return False
    hits.append(now_ts)
    _hits[ip] = hits
    return True


def _read_json(handler):
    import portal
    raw = portal.read_limited(handler, INGEST_MAX)
    encoding = (handler.headers.get("Content-Encoding") or "").strip().lower()
    if encoding == "gzip":
        try:
            raw = portal._gunzip_limited(raw, INGEST_MAX)
        except OverflowError:
            raise
        except ValueError:
            raise ValueError("body must be gzip-compressed JSON")
    elif encoding and encoding != "identity":
        raise ValueError("content encoding must be gzip or identity")
    try:
        body = json.loads(raw.decode("utf-8") or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise ValueError("body must be JSON")
    if not isinstance(body, dict):
        raise ValueError("body must be a JSON object")
    return body


def _parse_token(value, label):
    import portal
    if isinstance(value, bool) or isinstance(value, float):
        raise ValueError(f"{label} is invalid")
    if isinstance(value, int):
        if value < 0:
            raise ValueError(f"{label} is invalid")
        value = str(value)
    if not isinstance(value, str):
        raise ValueError(f"{label} is required")
    text = value.strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,31}", text) or portal.has_card_number(text):
        raise ValueError(f"{label} is invalid")
    return text


def _parse_time(value, label):
    import server
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a time")
    text = value.strip()
    if not text:
        return None
    if len(text) > 40:
        raise ValueError(f"{label} must be a time")
    try:
        if text.endswith("Z"):
            parsed = datetime.fromisoformat(text[:-1] + "+00:00")
        else:
            parsed = datetime.fromisoformat(text)
    except ValueError:
        raise ValueError(f"{label} must be a time like 2026-11-22T12:04:00+10:00")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=server.TZ)
    return parsed.astimezone(server.TZ).replace(microsecond=0).strftime(FMT)


def _till_source(value):
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ValueError("source must be counter or bopple")
    key = value.strip().casefold()
    aliases = {
        "counter": "counter",
        "till": "counter",
        "pos": "counter",
        "swiftpos": "counter",
        "bopple": "bopple",
        "online": "bopple",
    }
    if key not in aliases:
        raise ValueError("source must be counter or bopple")
    return aliases[key]


def _parse_status(value):
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ValueError("status must be held or paid")
    key = value.strip().casefold()
    if key not in ("held", "paid"):
        raise ValueError("status must be held or paid")
    return key


def _parse_qty(value, label):
    if isinstance(value, bool) or value is None:
        raise ValueError(f"{label} must be a number")
    if isinstance(value, str):
        text = value.strip()
        if not re.fullmatch(r"\d+(\.\d{1,2})?", text):
            raise ValueError(f"{label} must be a number")
        value = float(text) if "." in text else int(text)
    if isinstance(value, int):
        number = value
    elif isinstance(value, float):
        number = round(value, 2)
        if number == int(number):
            number = int(number)
    else:
        raise ValueError(f"{label} must be a number")
    if isinstance(number, float) and number <= 0:
        raise ValueError(f"{label} must be from 1 to 99")
    if not isinstance(number, (int, float)) or number <= 0 or number > 99:
        raise ValueError(f"{label} must be from 1 to 99")
    return number


def _parse_items(value, label):
    import portal
    if value is None:
        return False, None
    if not isinstance(value, list):
        raise ValueError(f"{label} must be a list")
    if not value:
        raise ValueError(f"{label} needs at least one item")
    if len(value) > 40:
        raise ValueError(f"{label} has too many items")
    items = []
    for index, row in enumerate(value):
        item_label = f"{label}[{index}]"
        if not isinstance(row, dict):
            raise ValueError(f"{item_label} must be an object")
        portal.reject_sensitive(row, item_label)
        if "name" in row:
            raw_name = row.get("name")
        elif "description" in row:
            raw_name = row.get("description")
        else:
            raise ValueError(f"{item_label} name is required")
        if not isinstance(raw_name, str):
            raise ValueError(f"{item_label} name is required")
        name = " ".join(raw_name.split())
        if not name or len(name) > 80:
            raise ValueError(f"{item_label} name must be 1 to 80 characters")
        if portal.has_card_number(name):
            raise ValueError(f"{item_label} name must not include a card number")
        if "qty" in row:
            raw_qty = row.get("qty")
        elif "quantity" in row:
            raw_qty = row.get("quantity")
        else:
            raise ValueError(f"{item_label} qty is required")
        items.append({"name": name, "qty": _parse_qty(raw_qty, f"{item_label} qty")})
    return True, items


def _present(row, *keys):
    for key in keys:
        if key in row:
            return True, row.get(key)
    return False, None


def parse_order(row, label):
    import portal
    if not isinstance(row, dict):
        raise ValueError(f"{label} must be an object")
    portal.reject_sensitive(row, label)
    if "receipt" not in row:
        raise ValueError(f"{label} receipt is required")
    receipt = _parse_token(row.get("receipt"), f"{label} receipt")
    order_id = None
    if "orderId" in row or "order_id" in row:
        order_id = _parse_token(row.get("orderId", row.get("order_id")), f"{label} orderId")
    link = None
    if "link" in row or "heldReceipt" in row or "held_receipt" in row:
        raw_link = row.get("link", row.get("heldReceipt", row.get("held_receipt")))
        if raw_link not in (None, ""):
            link = _parse_token(raw_link, f"{label} link")
    customer_present, raw_customer = _present(row, "customer", "customerName", "customer_name")
    customer = clean_customer(raw_customer, label=f"{label} customer") if customer_present else None
    items_present, items = _parse_items(row.get("items") if "items" in row else None, f"{label} items")
    return {
        "label": label,
        "receipt": receipt,
        "order_id": order_id,
        "link": link,
        "status": _parse_status(row.get("status")),
        "source": _till_source(row.get("source")),
        "customer_present": customer_present and bool(customer),
        "customer": customer,
        "held_at": _parse_time(row.get("heldAt", row.get("held_at")), f"{label} heldAt"),
        "rung_up": _parse_time(row.get("rungUp", row.get("rung_up")), f"{label} rungUp"),
        "paid_at": _parse_time(row.get("paidAt", row.get("paid_at")), f"{label} paidAt"),
        "items_present": items_present,
        "items": items,
    }


def parse_payload(body):
    import portal
    portal.reject_sensitive(body, "food-orders")
    if "orders" in body:
        orders = body.get("orders")
        if not isinstance(orders, list):
            raise ValueError("orders must be a list")
        labels = [f"orders[{index}]" for index in range(len(orders))]
    elif "receipt" in body:
        orders = [body]
        labels = ["order"]
    else:
        raise ValueError("body must be {orders: [...]}")
    if len(orders) > 200:
        raise ValueError("too many orders")
    return [parse_order(row, label) for row, label in zip(orders, labels)]


def _lookup_keys(order):
    keys = []
    for key in (order["link"], order["order_id"], order["receipt"]):
        if key and key not in keys:
            keys.append(key)
    return keys


def find_receipt(c, order):
    found = {}
    for key in _lookup_keys(order):
        for row in many(
            c,
            """SELECT * FROM food_order_receipts
               WHERE order_key=? OR receipt=? OR held_receipt=?""",
            (key, key, key),
        ):
            found[row["id"]] = row
    if len(found) > 1:
        raise ValueError(f"{order['label']} matches more than one till order")
    if not found:
        return None
    return next(iter(found.values()))


def _dump_items(items):
    return json.dumps(items, separators=(",", ":"), ensure_ascii=False)


def _insert_receipt(c, order, now_stamp):
    label = order["label"]
    status = order["status"] or "paid"
    if not order["source"]:
        raise ValueError(f"{label} source must be counter or bopple")
    if not order["items_present"] or not order["items"]:
        raise ValueError(f"{label} items is required")
    if status == "held":
        if not order["held_at"]:
            raise ValueError(f"{label} heldAt is required")
        if not order["customer"]:
            raise ValueError(f"{label} customer is required")
    times = [item for item in (order["held_at"], order["rung_up"]) if item]
    if not times:
        raise ValueError(f"{label} rungUp or heldAt is required")
    start = min(times)
    order_key = order["order_id"] or order["link"] or order["receipt"]
    held_receipt = None
    if order["link"] and order["link"] != order["receipt"]:
        held_receipt = order["link"]
    elif order["order_id"] and order["order_id"] != order["receipt"]:
        held_receipt = order["order_id"]
    elif status == "held":
        held_receipt = order["receipt"]
    c.execute("SAVEPOINT fo_receipt")
    try:
        c.execute(
            """INSERT INTO food_order_receipts(
                 order_key, receipt, held_receipt, rung_up, trading_date, source, status,
                 customer, items, paid_at, received_at, updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                order_key, order["receipt"], held_receipt, start, start[:10], order["source"], status,
                order["customer"], _dump_items(order["items"]), order["paid_at"], now_stamp, now_stamp,
            ),
        )
        c.execute("RELEASE SAVEPOINT fo_receipt")
    except Exception as ex:
        c.execute("ROLLBACK TO SAVEPOINT fo_receipt")
        c.execute("RELEASE SAVEPOINT fo_receipt")
        if not _unique(ex):
            raise
        existing = find_receipt(c, order)
        if not existing:
            raise ValueError(f"{label} receipt is already used")
        return apply_update(c, existing, order, now_stamp)
    return "created"


def apply_update(c, existing, order, now_stamp):
    """Keep the earliest start. A later paid update must not move that clock or add a second row."""
    label = order["label"]
    if existing["status"] == "paid" and order["status"] == "held":
        return "unchanged"
    status = existing["status"]
    if order["status"] == "paid":
        status = "paid"
    start = existing["rung_up"]
    for candidate in (order["held_at"], order["rung_up"]):
        if candidate and candidate < start:
            start = candidate
    source = order["source"] or existing["source"]
    customer = existing["customer"]
    if order["customer_present"] and order["customer"]:
        customer = order["customer"]
    items = _load_items(existing["items"])
    if order["items_present"]:
        items = order["items"]
    paid_at = existing["paid_at"]
    if order["paid_at"]:
        paid_at = order["paid_at"]
    receipt = order["receipt"]
    if receipt != existing["receipt"]:
        clash = one(
            c,
            "SELECT id FROM food_order_receipts WHERE receipt=? AND id<>?",
            (receipt, existing["id"]),
        )
        if clash:
            raise ValueError(f"{label} receipt is already used by another order")
    held_receipt = existing["held_receipt"]
    if receipt != existing["receipt"] and not held_receipt:
        held_receipt = existing["receipt"]
    dumped = _dump_items(items)
    changed = any((
        status != existing["status"],
        start != existing["rung_up"],
        source != existing["source"],
        (customer or None) != (existing["customer"] or None),
        dumped != (existing["items"] or "[]"),
        (paid_at or None) != (existing["paid_at"] or None),
        receipt != existing["receipt"],
        (held_receipt or None) != (existing["held_receipt"] or None),
        start[:10] != existing["trading_date"],
    ))
    if not changed:
        return "unchanged"
    c.execute(
        """UPDATE food_order_receipts
           SET receipt=?, held_receipt=?, rung_up=?, trading_date=?, source=?, status=?,
               customer=?, items=?, paid_at=?, updated_at=?
           WHERE id=?""",
        (
            receipt, held_receipt, start, start[:10], source, status,
            customer, dumped, paid_at, now_stamp, existing["id"],
        ),
    )
    return "updated"


def store_orders(c, orders, now_stamp):
    created = updated = unchanged = 0
    for order in orders:
        existing = find_receipt(c, order)
        if existing:
            result = apply_update(c, existing, order, now_stamp)
        else:
            result = _insert_receipt(c, order, now_stamp)
        if result == "created":
            created += 1
        elif result == "updated":
            updated += 1
        else:
            unchanged += 1
    return {"received": len(orders), "created": created, "updated": updated, "unchanged": unchanged}


def handle_ingest(handler):
    import portal
    if not portal.ingest_authorized(handler):
        return
    if not _rate_ok(portal.client_ip(handler)):
        handler.send(429, {"error": "too many ingest requests"})
        return
    try:
        orders = parse_payload(_read_json(handler))
    except OverflowError:
        handler.send(413, {"error": "payload too large"})
        return
    except ValueError as ex:
        portal.log("food-orders rejected")
        handler.send(400, {"error": str(ex)[:180]})
        return
    c = portal.db()
    try:
        try:
            result = store_orders(c, orders, stamp())
        except ValueError as ex:
            portal.log("food-orders rejected")
            handler.send(400, {"error": str(ex)[:180]})
            return
        except Exception as ex:
            portal.log("food-orders failed: " + type(ex).__name__)
            handler.send(500, {"error": "could not store that payload"})
            return
        c.commit()
    finally:
        c.close()
    portal.log(
        f"food-orders created={result['created']} updated={result['updated']} unchanged={result['unchanged']}"
    )
    handler.send(200, {"ok": True, **result})
