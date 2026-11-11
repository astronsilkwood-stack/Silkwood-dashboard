"""Till exceptions: price overrides, fuel drive-offs, and account charges.

The nightly file is one Brisbane trade date. Posting that date again replaces every
stored row for the date, so a repaid IOU or drive-off can change from unpaid to paid.
Rows for other dates stay. The stable key is type, date, receipt, time, and item.
"""
import json
import re
from decimal import Decimal, ROUND_HALF_UP
from urllib.parse import urlencode

import portal

TYPES = ("price_override", "drive_off", "account")
TYPE_LABEL = {
    "price_override": "Price override",
    "drive_off": "Drive-off",
    "account": "Account",
}
SUBTYPES = {"shop", "surcharge_default_zero"}
ACCOUNT_TYPES = {"Drive Off": "drive_off", "IOU": "account", "On account": "account"}
STATUSES = {"paid", "unpaid", "partial"}
BASIS = {"allocated_payments", "fifo_estimate_vs_account_balance"}
MAX_DATES = 800
MAX_ROWS = 20000
MAX_LINES = 80
_DIGIT_RUN = re.compile(r"(?<!\d)(?:\d[ -]?){11,}\d(?!\d)")
_COLUMNS = (
    "row_key", "trade_date", "till_time", "exc_type", "subtype", "receipt", "staff", "item",
    "quantity", "normal_price", "charged_price", "difference", "amount", "customer", "status",
    "account_type", "outstanding", "paid_on_account", "status_basis", "plu", "reason", "rego",
    "lines", "source_as_of", "updated_at",
)


def _has_long_digits(text):
    for match in _DIGIT_RUN.finditer(text or ""):
        if len(re.sub(r"\D", "", match.group())) >= 12:
            return True
    return False


def _mask_secrets(value):
    """Backstop so a card or bank number is not shown even if one was stored."""
    def repl(match):
        digits = re.sub(r"\D", "", match.group())
        return "••••" if len(digits) >= 12 else match.group()
    return _DIGIT_RUN.sub(repl, "" if value is None else str(value))


def show(value, blank="—"):
    if value is None or value == "":
        return blank
    return portal.h(_mask_secrets(value))


def _reject_long_numbers(node):
    if isinstance(node, dict):
        for value in node.values():
            _reject_long_numbers(value)
    elif isinstance(node, list):
        for value in node:
            _reject_long_numbers(value)
    elif isinstance(node, str):
        if _has_long_digits(node):
            raise ValueError("shift-exceptions must not include a card or bank number")
    elif isinstance(node, bool):
        return
    elif isinstance(node, int):
        if len(str(abs(node))) >= 12:
            raise ValueError("shift-exceptions must not include a card or bank number")
    elif isinstance(node, float):
        if node == node and abs(node) < 1e15 and float(node).is_integer() and len(str(int(abs(node)))) >= 12:
            raise ValueError("shift-exceptions must not include a card or bank number")


def _blank(value):
    return value is None or value == "" or value == []


def _plain(value, label, required=False, limit=200):
    if _blank(value):
        if required:
            raise ValueError(f"{label} is required")
        return None
    if not isinstance(value, str):
        raise ValueError(f"{label} must be text")
    text = " ".join(value.split())
    if len(text) > limit:
        raise ValueError(f"{label} is too long")
    if _has_long_digits(text) or portal.has_card_number(text):
        raise ValueError(f"{label} must not include a card or bank number")
    return text


def _money(value, label, required=False):
    if _blank(value):
        if required:
            raise ValueError(f"{label} is required")
        return None
    return portal.num(value, label)


def _must_blank(row, key, label):
    if not _blank(row.get(key)):
        raise ValueError(f"{label} must be empty")


def _code(value, label, limit, required=False):
    if _blank(value):
        if required:
            raise ValueError(f"{label} is required")
        return None
    if isinstance(value, bool):
        raise ValueError(f"{label} must be digits")
    if isinstance(value, int):
        value = str(value)
    elif isinstance(value, float) and value.is_integer():
        value = str(int(value))
    if not isinstance(value, str) or not re.fullmatch(r"\d{1," + str(limit) + r"}", value.strip()):
        raise ValueError(f"{label} must be digits")
    if _has_long_digits(value) or portal.has_card_number(value):
        raise ValueError(f"{label} must not include a card or bank number")
    return value.strip()


def _time(value, label):
    if not isinstance(value, str) or not re.fullmatch(r"\d{2}:\d{2}:\d{2}", value):
        raise ValueError(f"{label} must be HH:MM:SS")
    hour, minute, second = (int(part) for part in value.split(":"))
    if hour > 23 or minute > 59 or second > 59:
        raise ValueError(f"{label} must be HH:MM:SS")
    return value


def _flag(value, label):
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    raise ValueError(f"{label} must be true or false")


def _lines(raw, label):
    if _blank(raw):
        return []
    if not isinstance(raw, list):
        raise ValueError(f"{label} lines must be a list")
    if len(raw) > MAX_LINES:
        raise ValueError(f"{label} has too many lines")
    lines = []
    for i, line in enumerate(raw):
        if not isinstance(line, dict):
            raise ValueError(f"{label} lines[{i}] must be an object")
        lines.append({
            "item": _plain(line.get("item"), f"{label} lines[{i}] item", required=True, limit=240),
            "plu": _code(line.get("plu"), f"{label} lines[{i}] plu", 10),
            "quantity": _money(line.get("quantity"), f"{label} lines[{i}] quantity"),
            "amount": _money(line.get("amount"), f"{label} lines[{i}] amount", required=True),
            "adjustment": _flag(line.get("adjustment"), f"{label} lines[{i}] adjustment"),
        })
    return lines


def _row_key(typ, day, receipt, till_time, item):
    return "\n".join((typ, day, receipt, till_time, item.casefold()))


def _parse_row(raw, label, expected):
    if not isinstance(raw, dict):
        raise ValueError(f"{label} must be an object")
    typ = raw.get("type")
    if typ not in TYPES:
        raise ValueError(f"{label} type must be price_override, drive_off or account")
    if _blank(raw.get("date")):
        if not expected:
            raise ValueError(f"{label} date is required")
        day = expected
    else:
        day = portal.need_date(raw.get("date"), f"{label} date")
        if expected and day != expected:
            raise ValueError(f"{label} date must be {expected}")
    till_time = _time(raw.get("time"), f"{label} time")
    receipt = _code(raw.get("receipt"), f"{label} receipt", 12, required=True)
    item = _plain(raw.get("item"), f"{label} item", required=True, limit=400)
    staff = _plain(raw.get("staff"), f"{label} staff", required=True, limit=80)
    customer = _plain(raw.get("customer"), f"{label} customer", limit=80)
    rego = _plain(raw.get("rego"), f"{label} rego", limit=16)
    if typ == "price_override":
        for key in ("account_type", "outstanding", "paid_on_account", "status", "status_basis", "rego"):
            _must_blank(raw, key, f"{label} {key}")
        if not _blank(raw.get("lines")):
            raise ValueError(f"{label} lines must be empty")
        subtype = raw.get("subtype")
        if subtype not in SUBTYPES:
            raise ValueError(f"{label} subtype must be shop or surcharge_default_zero")
        normal = _money(raw.get("normal_price"), f"{label} normal_price", required=True)
        charged = _money(raw.get("charged_price"), f"{label} charged_price", required=True)
        difference = _money(raw.get("difference"), f"{label} difference", required=True)
        account_type = status = basis = None
        outstanding = paid = None
        lines = []
        plu = _code(raw.get("plu"), f"{label} plu", 10)
        reason = _plain(raw.get("reason"), f"{label} reason", limit=200)
    else:
        for key in ("normal_price", "charged_price", "difference", "subtype", "plu", "reason"):
            _must_blank(raw, key, f"{label} {key}")
        account_type = raw.get("account_type")
        if account_type not in ACCOUNT_TYPES or ACCOUNT_TYPES[account_type] != typ:
            raise ValueError(f"{label} account_type does not match type")
        status = raw.get("status")
        if status not in STATUSES:
            raise ValueError(f"{label} status must be paid, unpaid or partial")
        basis = raw.get("status_basis")
        if basis not in BASIS:
            raise ValueError(f"{label} status_basis is not recognised")
        outstanding = _money(raw.get("outstanding"), f"{label} outstanding", required=True)
        paid = _money(raw.get("paid_on_account"), f"{label} paid_on_account")
        lines = _lines(raw.get("lines"), label)
        subtype = normal = charged = difference = plu = reason = None
    amount = _money(raw.get("amount"), f"{label} amount", required=True)
    quantity = _money(raw.get("quantity"), f"{label} quantity")
    return {
        "row_key": _row_key(typ, day, receipt, till_time, item),
        "trade_date": day,
        "till_time": till_time,
        "exc_type": typ,
        "subtype": subtype,
        "receipt": receipt,
        "staff": staff,
        "item": item,
        "quantity": quantity,
        "normal_price": normal,
        "charged_price": charged,
        "difference": difference,
        "amount": amount,
        "customer": customer,
        "status": status,
        "account_type": account_type,
        "outstanding": outstanding,
        "paid_on_account": paid,
        "status_basis": basis,
        "plu": plu,
        "reason": reason,
        "rego": rego,
        "lines": lines,
    }


def _batches(data):
    has_days = data.get("days") is not None
    has_date = not _blank(data.get("date"))
    has_rows = data.get("rows") is not None
    has_dates = data.get("dates") is not None
    if has_days and (has_date or has_rows or has_dates):
        raise ValueError("shift-exceptions days cannot be combined with date, dates or rows")
    if has_date and has_dates:
        raise ValueError("shift-exceptions uses date or dates, not both")
    if has_days:
        days = data["days"]
        if not isinstance(days, list) or not days:
            raise ValueError("shift-exceptions days must be a list of dates")
        if len(days) > MAX_DATES:
            raise ValueError("shift-exceptions has too many dates")
        out = []
        seen = set()
        for i, day in enumerate(days):
            if not isinstance(day, dict):
                raise ValueError(f"days[{i}] must be an object")
            iso = portal.need_date(day.get("date"), f"days[{i}] date")
            if iso in seen:
                raise ValueError(f"days[{i}] repeats {iso}")
            seen.add(iso)
            rows = day.get("rows") if day.get("rows") is not None else []
            if not isinstance(rows, list):
                raise ValueError(f"days[{i}] rows must be a list")
            out.append((iso, rows))
        return out
    if has_date:
        iso = portal.need_date(data.get("date"), "date")
        rows = data.get("rows") if data.get("rows") is not None else []
        if not isinstance(rows, list):
            raise ValueError("shift-exceptions rows must be a list")
        return [(iso, rows)]
    rows = data.get("rows") if has_rows else []
    if not isinstance(rows, list):
        raise ValueError("shift-exceptions rows must be a list")
    grouped = {}
    order = []
    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ValueError(f"rows[{i}] must be an object")
        iso = portal.need_date(row.get("date"), f"rows[{i}] date")
        if iso not in grouped:
            grouped[iso] = []
            order.append(iso)
        grouped[iso].append(row)
    if has_dates:
        dates = data["dates"]
        if not isinstance(dates, list):
            raise ValueError("shift-exceptions dates must be a list")
        for i, iso in enumerate(dates):
            iso = portal.need_date(iso, f"dates[{i}]")
            if iso not in grouped:
                grouped[iso] = []
                order.append(iso)
    if not order:
        raise ValueError("shift-exceptions needs a date")
    if len(order) > MAX_DATES:
        raise ValueError("shift-exceptions has too many dates")
    return [(iso, grouped[iso]) for iso in order]


def _meta_text(data, key, limit):
    if _blank(data.get(key)):
        return ""
    return _plain(data.get(key), key, limit=limit) or ""


def apply_shift_exceptions(c, data):
    """Replace each trade date in the payload. Other dates stay."""
    if not isinstance(data, dict):
        raise ValueError("shift-exceptions data must be an object")
    portal.reject_sensitive(data, "shift-exceptions")
    _reject_long_numbers(data)
    if not _blank(data.get("timezone")) and data.get("timezone") != "Australia/Brisbane":
        raise ValueError("shift-exceptions timezone must be Australia/Brisbane")
    generated = _meta_text(data, "generated_at", 40)
    if generated and not re.fullmatch(r"[0-9T:Z+\-. ]{10,40}", generated):
        raise ValueError("generated_at must be a timestamp")
    source = _meta_text(data, "source", 200)
    location = _meta_text(data, "location", 80)
    specs = _batches(data)
    parsed = []
    total = 0
    for day, raw_rows in specs:
        total += len(raw_rows)
        if total > MAX_ROWS:
            raise ValueError("shift-exceptions payload is too large")
        items = {}
        for i, raw in enumerate(raw_rows):
            item = _parse_row(raw, f"{day} rows[{i}]", day)
            items[item["row_key"]] = item
        parsed.append((day, items))
    dates = [day for day, _items in parsed]
    prev = portal.meta_get(c, "shift-exceptions")
    candidate = portal.need_date(data.get("asOf"), "asOf") if not _blank(data.get("asOf")) else max(dates)
    previous = prev.get("asOf")
    as_of = max(candidate, previous) if previous else candidate
    source = source or prev.get("source") or ""
    location = location or prev.get("location") or ""
    generated = generated or prev.get("generatedAt") or ""
    stored = 0
    when = portal.stamp()
    for day, items in parsed:
        c.execute("DELETE FROM portal_shift_exceptions WHERE trade_date=?", (day,))
        for item in items.values():
            lines = json.dumps(item["lines"], separators=(",", ":"), ensure_ascii=False) if item["lines"] else None
            values = tuple(item[name] for name in _COLUMNS if name not in ("lines", "source_as_of", "updated_at"))
            c.execute(
                f"""INSERT INTO portal_shift_exceptions({",".join(_COLUMNS)})
                    VALUES({",".join("?" * len(_COLUMNS))})""",
                values + (lines, candidate, when),
            )
            stored += 1
    portal.meta_set(c, "shift-exceptions", {
        "asOf": as_of,
        "source": source,
        "location": location,
        "generatedAt": generated,
        "timezone": "Australia/Brisbane",
        "dates": len(parsed),
        "rows": stored,
    })
    portal.save_snapshot(c, "shift-exceptions", max(dates), {
        "asOf": as_of,
        "source": source,
        "location": location,
        "generatedAt": generated,
        "dates": dates,
        "rows": stored,
    })
    return {"asOf": as_of, "dates": len(parsed), "rows": stored, "replaced": dates}


def _load(c, start, end):
    found = portal.rows(c.execute(
        "SELECT * FROM portal_shift_exceptions WHERE trade_date>=? AND trade_date<=?",
        (start.isoformat(), end.isoformat()),
    ))
    found.sort(key=lambda row: (row["trade_date"], row["till_time"], row["receipt"], row["item"] or ""), reverse=True)
    return found


def _n(count, singular):
    word = singular if count == 1 else singular + "s"
    return f"{count} {word}"


def _qty(value):
    if value is None:
        return "—"
    q = Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if q == q.to_integral():
        return f"{int(q):,}"
    return f"{q:,.2f}"


def _sum_rows(found, typ):
    picked = [row for row in found if row["exc_type"] == typ]
    return {
        "n": len(picked),
        "amount": sum(row["amount"] or 0 for row in picked),
        "difference": sum(row["difference"] or 0 for row in picked),
        "outstanding": sum(row["outstanding"] or 0 for row in picked),
        "unpaid": sum(1 for row in picked if row["status"] == "unpaid"),
        "partial": sum(1 for row in picked if row["status"] == "partial"),
    }


def _owed_phrase(summary):
    detail = []
    if summary["unpaid"]:
        detail.append(f"{summary['unpaid']} unpaid")
    if summary["partial"]:
        detail.append(f"{summary['partial']} partial")
    tail = ", ".join(detail) if detail else "none unpaid"
    return f"Outstanding {portal.money(summary['outstanding'])} · {tail}"


def overview_card(c):
    meta = portal.meta_get(c, "shift-exceptions")
    if not meta.get("asOf"):
        return (
            '<div class="grid g4"><section class="card stat" id="overview-exceptions"><div class="lbl">Till exceptions</div>'
            '<b>—</b><span>not loaded yet</span>'
            '<a href="/portal/exceptions">View exceptions</a></section></div>'
        )
    end = portal.today()
    start = end.replace(day=1)
    found = _load(c, start, end)
    counts = {typ: 0 for typ in TYPES}
    outstanding = 0.0
    for row in found:
        counts[row["exc_type"]] += 1
        if row["exc_type"] in ("drive_off", "account"):
            outstanding += row["outstanding"] or 0
    total = sum(counts.values())
    span = (
        f"this month · {_n(counts['price_override'], 'price override')} · "
        f"{_n(counts['drive_off'], 'drive-off')} · {_n(counts['account'], 'account charge')} · "
        f"{portal.money(outstanding)} outstanding"
    )
    return (
        f'<div class="grid g4"><section class="card stat" id="overview-exceptions"><div class="lbl">Till exceptions</div>'
        f"<b>{total}</b><span>{portal.h(span)}</span>"
        '<p class="mini">Paid status for named accounts is an estimate.</p>'
        '<a href="/portal/exceptions">View exceptions</a></section></div>'
    )


def _query(active, start, end, typ=""):
    params = {"period": active}
    if active == "custom":
        params["from"] = start.isoformat()
        params["to"] = end.isoformat()
    if typ:
        params["type"] = typ
    return "/portal/exceptions?" + urlencode(params)


def _type_chips(active, start, end, typ):
    labels = (("", "All"), ("price_override", "Price overrides"), ("drive_off", "Drive-offs"), ("account", "Accounts"))
    chips = []
    for key, label in labels:
        cls = " on" if key == typ else ""
        chips.append(f'<a class="chip{cls}" href="{_query(active, start, end, key)}">{label}</a>')
    return f'<div class="chips" id="exception-types">{"".join(chips)}</div>'


def _type_card(typ, summary):
    title = {"price_override": "Price overrides", "drive_off": "Drive-offs", "account": "Account charges"}[typ]
    spans = [f"<span>Amount charged {portal.money(summary['amount'])}</span>"]
    if typ == "price_override":
        spans.append(f"<span>Price difference {portal.money(summary['difference'])}</span>")
    else:
        spans.append(f"<span>{portal.h(_owed_phrase(summary))}</span>")
    return (
        f'<div class="card stat" id="exception-{typ}"><div class="lbl">{title}</div>'
        f"<b>{summary['n']}</b>{''.join(spans)}</div>"
    )


def _status_cell(row):
    status = row["status"]
    if not status:
        return "<td>—</td>"
    cls = {"paid": "ok", "unpaid": "bad", "partial": "warn"}.get(status, "")
    label = {"paid": "Paid", "unpaid": "Unpaid", "partial": "Partial"}.get(status, status)
    html = f'<span class="pill {cls}">{label}</span>'
    if row["status_basis"] == "fifo_estimate_vs_account_balance":
        html += ' <span class="pill warn">Estimate</span>'
    return f"<td>{html}</td>"


def _item_cell(row):
    bits = [show(row["item"], blank="")]
    if row["exc_type"] == "price_override" and row["normal_price"] is not None:
        bits.append(
            f'<div class="mini">Normal {portal.money(row["normal_price"])} · charged {portal.money(row["charged_price"])}</div>'
        )
    if row["reason"]:
        bits.append(f'<div class="mini">{show(row["reason"], blank="")}</div>')
    lines = []
    if row.get("lines"):
        try:
            parsed = json.loads(row["lines"])
        except json.JSONDecodeError:
            parsed = []
        if isinstance(parsed, list):
            lines = parsed
    if len(lines) > 1 or any(isinstance(line, dict) and line.get("adjustment") for line in lines):
        items = []
        for line in lines:
            if not isinstance(line, dict):
                continue
            flag = " · adjustment" if line.get("adjustment") else ""
            items.append(
                f'<li>{show(line.get("item"), blank="")} · {_qty(line.get("quantity"))} · {portal.money(line.get("amount"))}{flag}</li>'
            )
        if items:
            bits.append("<ul>" + "".join(items) + "</ul>")
    return "<td>" + "".join(bits) + "</td>"


def _customer_cell(row):
    parts = []
    if row["customer"]:
        parts.append(show(row["customer"], blank=""))
    if row["rego"]:
        parts.append(f'<div class="mini">Rego {show(row["rego"], blank="")}</div>')
    return "<td>" + ("".join(parts) or "—") + "</td>"


def _staff_section(found):
    buckets = {}
    for row in found:
        if row["exc_type"] != "price_override":
            continue
        bucket = buckets.setdefault(row["staff"], {"n": 0, "amount": 0.0, "difference": 0.0})
        bucket["n"] += 1
        bucket["amount"] += row["amount"] or 0
        bucket["difference"] += row["difference"] or 0
    if not buckets:
        body = '<p class="empty">No price overrides in this range.</p>'
    else:
        lines = []
        for name, bucket in sorted(buckets.items(), key=lambda kv: (-kv[1]["n"], kv[0].casefold())):
            lines.append(
                f"<tr><td>{show(name, blank='')}</td><td class=\"num\">{bucket['n']}</td>"
                f"<td class=\"num\">{portal.money(bucket['amount'])}</td>"
                f"<td class=\"num\">{portal.money(bucket['difference'])}</td></tr>"
            )
        body = (
            '<div class="tw"><table><thead><tr><th>Staff</th><th>Overrides</th><th>Amount charged</th>'
            "<th>Price difference</th></tr></thead><tbody>" + "".join(lines) + "</tbody></table></div>"
        )
    return (
        '<section class="card" id="exception-staff"><h2>Price overrides by staff</h2>'
        '<p class="help">Count of price overrides in this date range, for each name on the till.</p>'
        f"{body}</section>"
    )


def _table(rows):
    if not rows:
        return '<p class="empty" id="exception-rows">No till exceptions in this range.</p>'
    body = []
    for row in rows:
        cls = {"unpaid": ' class="negative"', "partial": ' class="low"', "paid": ' class="ok"'}.get(row["status"] or "", "")
        kind = TYPE_LABEL.get(row["exc_type"], row["exc_type"])
        extra = ""
        if row["exc_type"] == "price_override":
            sub = "Surcharge" if row["subtype"] == "surcharge_default_zero" else "Shop"
            extra = f'<div class="mini">{sub}</div>'
        elif row["account_type"]:
            extra = f'<div class="mini">{show(row["account_type"], blank="")}</div>'
        diff = portal.money(row["difference"]) if row["difference"] is not None else "—"
        owed = portal.money(row["outstanding"]) if row["outstanding"] is not None else "—"
        body.append(
            f"<tr{cls}><td>{portal.h(portal.fmt_date(row['trade_date']))}</td><td>{show(row['till_time'], blank='')}</td>"
            f"<td>{portal.h(kind)}{extra}</td><td>{show(row['staff'], blank='')}</td><td>{show(row['receipt'], blank='')}</td>"
            f"{_item_cell(row)}<td class=\"num\">{_qty(row['quantity'])}</td>"
            f"<td class=\"num\">{portal.money(row['amount'])}</td><td class=\"num\">{diff}</td>"
            f"{_customer_cell(row)}{_status_cell(row)}<td class=\"num\">{owed}</td></tr>"
        )
    return (
        '<div class="tw" id="exception-rows"><table><thead><tr><th>Date</th><th>Time</th><th>Type</th><th>Staff</th>'
        "<th>Receipt</th><th>Item</th><th>Qty</th><th>Amount</th><th>Difference</th><th>Customer</th>"
        "<th>Status</th><th>Outstanding</th></tr></thead><tbody>"
        + "".join(body) + "</tbody></table></div>"
        '<p class="help">Newest first. Amount is what the till charged. For a price override, difference is charged minus the normal price, times the quantity.</p>'
    )


def page(c, query):
    meta = portal.meta_get(c, "shift-exceptions")
    if not meta.get("asOf"):
        return """<div class="ph"><h1>Till exceptions</h1>
<p>Price overrides, fuel drive-offs, and account charges (IOU and on account) from the till.</p></div>
<section class="card"><p class="empty" id="exceptions-empty">No till exceptions yet. The nightly refresh sends them as dataset shift-exceptions.</p></section>"""
    anchor = portal.today()
    try:
        active, start, end = portal.period_bounds(query.get("period"), query.get("from"), query.get("to"), anchor)
        error = ""
    except ValueError as ex:
        active, start, end = portal.period_bounds("month", None, None, anchor)
        error = f'<p class="alert">{portal.h(str(ex))}</p>'
    typ = query.get("type") if query.get("type") in TYPES else ""
    found = _load(c, start, end)
    shown = [row for row in found if not typ or row["exc_type"] == typ]
    cards = "".join(_type_card(key, _sum_rows(found, key)) for key in TYPES)
    bits = []
    if meta.get("source"):
        bits.append("Source: " + show(meta.get("source"), blank=""))
    if meta.get("location"):
        bits.append(show(meta.get("location"), blank=""))
    if meta.get("generatedAt"):
        bits.append("Exported " + show(meta.get("generatedAt"), blank=""))
    source = f'<p class="help">{" · ".join(bits)}</p>' if bits else ""
    return f"""<div class="ph"><h1>Till exceptions</h1>{portal.through_line(meta.get("asOf"))}
<p>Price overrides, fuel drive-offs, and account charges (IOU and on account) rung on the till. Showing {portal.h(portal.fmt_date(start.isoformat()))} – {portal.h(portal.fmt_date(end.isoformat()))}. Australia/Brisbane.</p>
<p class="help">A positive price difference was charged above the normal price. Surcharges whose normal price is $0, such as a fleet card surcharge, are listed with the overrides.</p>
{source}</div>
{error}{portal.period_controls("/portal/exceptions", active, start, end, {"type": typ} if typ else None)}
{_type_chips(active, start, end, typ)}
<p class="callout" id="account-estimate">Paid status for named accounts is an estimate. Payments on those accounts are not allocated to a receipt, so the current balance is applied to the newest charges first. Drive-off and IOU status uses the payments SwiftPOS allocated to that charge.</p>
<section class="grid g3" id="exception-totals">{cards}</section>
{_staff_section(found)}
<section class="card"><h2>Exceptions</h2>
<p class="help">{len(shown)} in this list. Staff and receipt are the till clerk and the till receipt number.</p>
{_table(shown)}</section>"""
