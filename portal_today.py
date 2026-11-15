"""Hourly till sales for the trading day still in progress.

Posted as dataset sales-today. Each post replaces that date. The nightly sales
dataset stays the completed day: once that date is in portal_sales_days, this
card stops using the hourly figures and waits for the next day.
"""
import json
from datetime import datetime, timedelta
from decimal import Decimal

import portal

HOUR_LIMIT = 24
ITEM_LIMIT = 40
TENDER_LIMIT = 20


def _parse_as_of(value):
    if not isinstance(value, str):
        raise ValueError("asOf must be a time")
    text = value.strip()
    if not text or len(text) > 40:
        raise ValueError("asOf must be a time")
    parsed = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        when = datetime.fromisoformat(parsed)
    except ValueError:
        raise ValueError("asOf must be a time")
    if when.tzinfo is None:
        when = when.replace(tzinfo=portal._srv().TZ)
    else:
        when = when.astimezone(portal._srv().TZ)
    return text, when


def _whole(value, label):
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        value = int(value.strip())
    number = portal.whole(value, label, required=True)
    if number < 0:
        raise ValueError(f"{label} must be zero or more")
    return number


def _money(value, label):
    number = portal.num(value, label)
    return float(portal._cents(number))


def _clean_hours(raw):
    if not isinstance(raw, list):
        raise ValueError("sales-today needs byHour")
    if len(raw) > HOUR_LIMIT * 2:
        raise ValueError("sales-today has too many hours")
    found = {}
    for index, row in enumerate(raw):
        if not isinstance(row, dict):
            raise ValueError(f"byHour[{index}] must be an object")
        hour = _whole(row.get("hour"), f"byHour[{index}] hour")
        if hour > 23:
            raise ValueError(f"byHour[{index}] hour must be 0 to 23")
        found[hour] = {
            "hour": hour,
            "receipts": _whole(row.get("receipts"), f"byHour[{index}] receipts"),
            "totalIncGst": _money(row.get("totalIncGst"), f"byHour[{index}] totalIncGst"),
        }
    return [found[hour] for hour in sorted(found)]


def _clean_items(raw):
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError("topItems must be a list")
    if len(raw) > ITEM_LIMIT:
        raise ValueError("sales-today has too many items")
    items = []
    for index, row in enumerate(raw):
        if not isinstance(row, dict):
            raise ValueError(f"topItems[{index}] must be an object")
        code = " ".join(str(row.get("code") or "").split())[:40]
        description = " ".join(str(row.get("description") or "").split())[:160]
        if not code and not description:
            raise ValueError(f"topItems[{index}] needs a code or description")
        portal.reject_sensitive({"code": code, "description": description}, "sales-today")
        qty = portal.num(row.get("qty"), f"topItems[{index}] qty")
        if qty < 0:
            raise ValueError(f"topItems[{index}] qty must be zero or more")
        items.append({
            "code": code,
            "description": description,
            "qty": qty,
            "incGst": _money(row.get("incGst"), f"topItems[{index}] incGst"),
        })
    return items


def _clean_tenders(raw):
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError("tenders must be a list")
    if len(raw) > TENDER_LIMIT:
        raise ValueError("sales-today has too many tenders")
    tenders = []
    for index, row in enumerate(raw):
        if not isinstance(row, dict):
            raise ValueError(f"tenders[{index}] must be an object")
        name = " ".join(str(row.get("name") or "").split())[:80]
        if not name:
            raise ValueError(f"tenders[{index}] needs a name")
        portal.reject_sensitive(name, "sales-today")
        tenders.append({
            "name": name,
            "count": _whole(row.get("count"), f"tenders[{index}] count"),
            "amount": _money(row.get("amount"), f"tenders[{index}] amount"),
        })
    return tenders


def apply_sales_today(c, data):
    """Replace one trading day's hourly snapshot. Other days stay."""
    if not isinstance(data, dict):
        raise ValueError("sales-today must be an object")
    trading = portal.need_date(data.get("date"), "date")
    day = datetime.strptime(trading, "%Y-%m-%d").date()
    if day < datetime(2024, 1, 1).date() or day > portal.today() + timedelta(days=1):
        raise ValueError("date is out of range")
    as_of, _when = _parse_as_of(data.get("asOf"))
    receipts = _whole(data.get("receipts"), "receipts")
    total = _money(data.get("totalIncGst"), "totalIncGst")
    shop = _money(data.get("shopIncGst"), "shopIncGst")
    fuel = _money(data.get("fuelIncGst"), "fuelIncGst")
    hours = _clean_hours(data.get("byHour"))
    items = _clean_items(data.get("topItems"))
    tenders = _clean_tenders(data.get("tenders"))
    stored = {
        "date": trading,
        "asOf": as_of,
        "receipts": receipts,
        "totalIncGst": total,
        "shopIncGst": shop,
        "fuelIncGst": fuel,
        "byHour": hours,
        "topItems": items,
        "tenders": tenders,
    }
    c.execute(
        """INSERT INTO portal_sales_today(date, as_of, receipts, total_inc, shop_inc, fuel_inc, by_hour, top_items, tenders, received_at)
           VALUES(?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(date) DO UPDATE SET as_of=excluded.as_of, receipts=excluded.receipts,
             total_inc=excluded.total_inc, shop_inc=excluded.shop_inc, fuel_inc=excluded.fuel_inc,
             by_hour=excluded.by_hour, top_items=excluded.top_items, tenders=excluded.tenders,
             received_at=excluded.received_at""",
        (
            trading, as_of, receipts, total, shop, fuel,
            json.dumps(hours, separators=(",", ":")),
            json.dumps(items, separators=(",", ":")),
            json.dumps(tenders, separators=(",", ":")),
            portal.stamp(),
        ),
    )
    portal.save_snapshot(c, "sales-today", trading, stored)
    portal.meta_set(c, "sales-today", {"asOf": as_of, "date": trading, "receipts": receipts})
    return {"asOf": as_of, "date": trading, "receipts": receipts}


def _loads(raw):
    try:
        data = json.loads(raw or "[]")
    except json.JSONDecodeError:
        return []
    return data if isinstance(data, list) else []


def _hour_label(hour):
    shown = hour % 12 or 12
    suffix = "am" if hour < 12 else "pm"
    return f"{shown}{suffix}"


def _updated(as_of):
    _text, when = _parse_as_of(as_of)
    return when.strftime("%H:%M")


def _open_row(c):
    rows = portal.rows(c.execute("SELECT * FROM portal_sales_today ORDER BY date"))
    if not rows:
        return None, "empty"
    closed = {
        row["date"] for row in portal.rows(c.execute("SELECT date FROM portal_sales_days"))
    }
    open_rows = [row for row in rows if row["date"] not in closed]
    if open_rows:
        return open_rows[-1], "open"
    return rows[-1], "closed"


def _compare(c, trading):
    day = datetime.strptime(trading, "%Y-%m-%d").date()
    last = day - timedelta(days=7)
    row = portal.one(c.execute("SELECT total, shop, fuel FROM portal_sales_days WHERE date=?", (last.isoformat(),)))
    label = f"{last.strftime('%A')} {portal.fmt_date(last.isoformat())}"
    if not row:
        return f'<p class="help" id="today-compare">No completed sales for {portal.h(label)} yet.</p>'
    return (
        f'<p class="help" id="today-compare">Same weekday last week, {portal.h(label)}, was '
        f'{portal.money(row["total"])} inc GST'
        f' (shop {portal.money(row["shop"])}, fuel {portal.money(row["fuel"])}).</p>'
    )


def _move(today_total, last_total):
    if last_total in (None, 0):
        return ""
    gap = float(today_total) - float(last_total)
    word = "above" if gap >= 0 else "below"
    return f'<span class="mini" id="today-gap">{portal.money(abs(gap))} {word} that day so far.</span>'


def card(c):
    row, state = _open_row(c)
    if state == "empty":
        return (
            '<section class="card" id="today-so-far"><h2>Today so far</h2>'
            '<p class="empty">No hourly till total yet. Completed days still come from the nightly sales file.</p>'
            "</section>"
        )
    if state == "closed":
        return (
            f'<section class="card" id="today-so-far" data-closed="{portal.h(row["date"])}"><h2>Today so far</h2>'
            f'<p>The nightly sales file now has {portal.h(portal.fmt_date(row["date"]))}, so that hourly total has rolled off. '
            f'The next hourly post starts the new day. '
            f'<a href="/portal/day?date={portal.h(row["date"])}">Open that day</a>.</p></section>'
        )
    trading = row["date"]
    day = datetime.strptime(trading, "%Y-%m-%d").date()
    title = "Today so far" if day == portal.today() else "So far"
    last = day - timedelta(days=7)
    last_row = portal.one(c.execute("SELECT total FROM portal_sales_days WHERE date=?", (last.isoformat(),)))
    gap = _move(row["total_inc"], last_row["total"] if last_row else None)
    hours = []
    for item in _loads(row["by_hour"]):
        if not isinstance(item, dict):
            continue
        hours.append(
            f'<div><b>{portal.h(_hour_label(int(item.get("hour") or 0)))}</b>'
            f'<span>{portal.money(item.get("totalIncGst"))}</span>'
            f'<span class="mini">{portal.h(item.get("receipts") or 0)} receipts</span></div>'
        )
    hour_html = f'<div class="hour-grid" id="today-hours">{"".join(hours)}</div>' if hours else ""
    items = []
    for item in _loads(row["top_items"]):
        if not isinstance(item, dict):
            continue
        name = item.get("description") or item.get("code") or "—"
        code = f' <span class="mini">{portal.h(item.get("code"))}</span>' if item.get("description") and item.get("code") else ""
        qty = item.get("qty")
        if isinstance(qty, float) and qty.is_integer():
            qty = int(qty)
        items.append(
            f"<tr><td>{portal.h(name)}{code}</td><td class=\"num\">{portal.h(qty)}</td>"
            f"<td class=\"num\">{portal.money(item.get('incGst'))}</td></tr>"
        )
    item_html = ""
    if items:
        item_html = (
            '<h3>Top items</h3><div class="tw"><table><thead><tr><th>Item</th><th>Qty</th><th>Inc GST</th></tr></thead><tbody>'
            + "".join(items) + "</tbody></table></div>"
        )
    tenders = []
    for item in _loads(row["tenders"]):
        if not isinstance(item, dict):
            continue
        tenders.append(
            f"<tr><td>{portal.h(item.get('name') or '—')}</td><td class=\"num\">{portal.h(item.get('count') or 0)}</td>"
            f"<td class=\"num\">{portal.money(item.get('amount'))}</td></tr>"
        )
    tender_html = ""
    if tenders:
        tender_html = (
            '<h3>Tenders</h3><div class="tw"><table><thead><tr><th>Tender</th><th>Count</th><th>Amount</th></tr></thead><tbody>'
            + "".join(tenders) + "</tbody></table></div>"
        )
    return f"""<section class="card" id="today-so-far" data-date="{portal.h(trading)}" data-total="{portal.h(format(portal._cents(row['total_inc']), 'f'))}">
<h2>{portal.h(title)}</h2>
<p id="today-updated">{portal.h(portal.fmt_date(trading))} · Updated {_updated(row['as_of'])}</p>
<div class="grid g3">
<div class="stat tone-sales"><div class="lbl">Total</div><b>{portal.money(row['total_inc'])}</b><span>{portal.h(row['receipts'])} receipts, inc GST</span>{gap}</div>
<div class="stat tone-shop"><div class="lbl">Shop</div><b>{portal.money(row['shop_inc'])}</b><span>inc GST</span></div>
<div class="stat tone-fuel"><div class="lbl">Fuel</div><b>{portal.money(row['fuel_inc'])}</b><span>inc GST</span></div>
</div>
{_compare(c, trading)}
{hour_html}
{item_html}
{tender_html}
<p class="help">Hourly till total for this trading day. It is not added into the completed-day totals. The nightly sales file replaces this day when it arrives, and the card rolls on to the next day.</p>
</section>"""
