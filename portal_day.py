"""One-day owner summary. Reads data the portal already stores. It does not ingest or pay anything."""
import json
from datetime import date, timedelta
from urllib.parse import urlencode

import portal


def default_day(c):
    """Latest loaded sales day, which is yesterday until the 9:30pm refresh and today after it."""
    through = portal.data_through(c)
    if through:
        return date.fromisoformat(through)
    return portal.today()


def _day_from_query(c, query):
    raw = (query or {}).get("date")
    if not raw:
        return default_day(c), ""
    try:
        day = date.fromisoformat(portal.need_date(raw, "date"))
    except ValueError:
        return default_day(c), "Date must be YYYY-MM-DD."
    if day < date(2024, 1, 1) or day > portal.today():
        return default_day(c), "Choose a date from 2024 through today."
    return day, ""


def _nav(day):
    prev_d = day - timedelta(days=1)
    next_d = day + timedelta(days=1)
    prev_link = (
        f'<a class="btn ghost" href="/portal/day?date={prev_d.isoformat()}">Previous day</a>'
        if prev_d >= date(2024, 1, 1) else '<span class="btn ghost" aria-disabled="true">Previous day</span>'
    )
    next_link = (
        f'<a class="btn ghost" href="/portal/day?date={next_d.isoformat()}">Next day</a>'
        if next_d <= portal.today() else '<span class="btn ghost" aria-disabled="true">Next day</span>'
    )
    return f"""<form class="daybar" method="get" action="/portal/day">
{prev_link}
<label class="f">Date<input type="date" name="date" value="{portal.h(day.isoformat())}" required></label>
<button class="btn" type="submit">Show</button>
{next_link}
</form>"""


def _empty(text):
    return f'<p class="empty">{portal.h(text)}</p>'


def _sales_section(c, day):
    iso = day.isoformat()
    row = portal.one(c.execute("SELECT * FROM portal_sales_days WHERE date=?", (iso,)))
    loaded = portal.online_loaded(c)
    online_row = None
    if loaded:
        online_row = portal.one(c.execute("SELECT * FROM portal_online_days WHERE date=?", (iso,)))
    food = portal.kitchen_sales_by_date(c).get(iso)
    if not row and not (loaded and online_row) and food is None:
        body = _empty("No sales for this day.")
    else:
        days = [row] if row else []
        online_by = {}
        if loaded:
            online_by[iso] = float(online_row["total"] or 0) if online_row else 0.0
        shown = portal.sales_display_rows(days, online_by, loaded)
        body = portal.daily_table(shown)
        bits = []
        if row:
            bits.append(f"Fuel {portal.money(row['fuel'])}")
            bits.append(f"Shop {portal.money(row['shop'])}")
        bits.append("Food " + (portal.money(food) if food is not None else "no kitchen total"))
        body += f'<p class="help" id="day-split">{" · ".join(bits)}. Food is the kitchen total and is already inside shop sales.</p>'
    return f'<section class="card" id="day-sales"><h2>Daily sales</h2>{body}</section>'


def _commission_section(c, day):
    iso = day.isoformat()
    row = portal.one(c.execute("SELECT * FROM portal_commission_days WHERE date=?", (iso,)))
    sales = portal.one(c.execute("SELECT gp, gp_pct, coverage FROM portal_sales_days WHERE date=?", (iso,)))
    if not row and not sales:
        return f'<section class="card" id="day-commission"><h2>Commission and GP</h2>{_empty("No commission or GP for this day.")}</section>'
    parts = []
    if row:
        parts.append(f'<div class="card stat"><div class="lbl">Commission ex GST</div><b>{portal.money(row["amount"])}</b></div>')
        parts.append(f'<div class="card stat"><div class="lbl">Fuel litres</div><b>{portal.litres(row["litres"])}</b></div>')
        grades = []
        try:
            parsed = json.loads(row["detail"]) if row.get("detail") else []
        except json.JSONDecodeError:
            parsed = []
        if isinstance(parsed, list):
            for grade in parsed:
                if isinstance(grade, dict):
                    grades.append(
                        f"<tr><td>{portal.h(grade.get('label') or grade.get('grade') or '')}</td>"
                        f"<td class=\"num\">{portal.litres(grade.get('litres'))}</td></tr>"
                    )
        if grades:
            parts.append(
                '<div class="tw"><table><thead><tr><th>Grade</th><th>Litres</th></tr></thead><tbody>'
                + "".join(grades) + "</tbody></table></div>"
            )
    else:
        parts.append(_empty("No fuel commission for this day."))
    if sales:
        parts.append(
            f'<div class="card stat"><div class="lbl">Shop GP</div><b>{portal.money(sales["gp"])}</b>'
            f'<span>{portal.pct_ratio(sales["gp_pct"])} · coverage {portal.pct_ratio(sales["coverage"])}</span></div>'
        )
    else:
        parts.append(_empty("No shop GP for this day."))
    return f'<section class="card" id="day-commission"><h2>Commission and GP</h2><div class="grid g2">{"".join(parts)}</div></section>'


def _dips_section(c, day):
    iso = day.isoformat()
    row = portal.one(c.execute("SELECT payload FROM portal_dip_days WHERE date=?", (iso,)))
    litres = portal.fuel_litres_by_date(c).get(iso)
    litre_line = f'<p class="help">Fuel litres from commission: {portal.litres(litres)}.</p>' if litres is not None else ""
    if not row:
        body = _empty("No tank dip for this day.") + litre_line
    else:
        payload = json.loads(row["payload"])
        tanks = []
        for tank in payload.get("tanks") or []:
            low = " low" if tank.get("low") else ""
            tanks.append(
                f'<article class="card tank{low}"><div class="lbl">Tank {portal.h(tank.get("tank"))} · {portal.h(tank.get("grade"))}</div>'
                f'<b>{portal.pct_points(tank.get("percentFull"))}</b>'
                f'<p>{portal.litres(tank.get("litres"))} of {portal.litres(tank.get("capacity"))}</p></article>'
            )
        body = f'<div class="grid g3">{"".join(tanks) or _empty("No tanks in this dip.")}</div>' + litre_line
        body += '<p class="help"><a href="/portal/dips?date=' + portal.h(iso) + '">All pump readings</a></p>'
    return f'<section class="card" id="day-dips"><h2>Fuel litres and tank dips</h2>{body}</section>'


def _hours_section(c, day):
    shifts = portal.roster_between(c, day, day)
    if not shifts:
        body = _empty("No staff hours for this day.")
    else:
        lines = []
        for shift in shifts:
            lines.append(
                f"<tr><td>{portal.h(shift['name'])}</td><td>{portal.h(shift['start_time'])}</td>"
                f"<td>{portal.h(shift['finish_time'])}</td><td class=\"num\">{portal.hours_text(shift['hours'])}</td>"
                f"<td>{portal.h(shift['notes'] or '')}</td></tr>"
            )
        body = (
            '<div class="tw"><table><thead><tr><th>Staff</th><th>Start</th><th>Finish</th><th>Hours</th><th>Notes</th></tr></thead><tbody>'
            + "".join(lines) + "</tbody></table></div>"
        )
    return (
        f'<section class="card" id="day-hours"><h2>Staff hours</h2>{body}'
        f'<p class="help"><a href="/portal/hours?week={portal.h(day.isoformat())}">Open the week</a>. Nothing here is sent to Xero.</p></section>'
    )


def _exceptions_section(c, day):
    meta = portal.meta_get(c, "shift-exceptions")
    if not meta.get("asOf"):
        body = _empty("No till exceptions loaded.")
    else:
        found = portal.rows(c.execute(
            "SELECT * FROM portal_shift_exceptions WHERE trade_date=? ORDER BY till_time, receipt",
            (day.isoformat(),),
        ))
        if not found:
            body = _empty("No till exceptions on this day.")
        else:
            labels = {"price_override": "Price override", "drive_off": "Drive-off", "account": "Account"}
            lines = []
            for row in found:
                lines.append(
                    f"<tr><td>{portal.h(row['till_time'] or '')}</td><td>{portal.h(labels.get(row['exc_type'], row['exc_type']))}</td>"
                    f"<td>{portal.h(row['staff'] or '')}</td><td>{portal.h(row['receipt'] or '')}</td>"
                    f"<td>{portal.h(row['item'] or '')}</td><td class=\"num\">{portal.money(row['amount'])}</td></tr>"
                )
            body = (
                '<div class="tw"><table><thead><tr><th>Time</th><th>Type</th><th>Staff</th><th>Receipt</th><th>Item</th><th>Amount</th></tr></thead><tbody>'
                + "".join(lines) + "</tbody></table></div>"
            )
    link = "/portal/exceptions?" + urlencode({"period": "custom", "from": day.isoformat(), "to": day.isoformat()})
    return f'<section class="card" id="day-exceptions"><h2>Shift exceptions</h2>{body}<p class="help"><a href="{portal.h(link)}">Open till exceptions</a></p></section>'


def _activity_section(c, day):
    iso = day.isoformat()
    hot = portal.rows(c.execute(
        "SELECT box, item, qty, time_in, out_at, outcome, staff FROM hotbox_items WHERE date=? ORDER BY time_in, id",
        (iso,),
    ))
    checks = portal.rows(c.execute(
        "SELECT at, staff, action FROM audit WHERE date=? AND (action='closing' OR action='closing-test' OR action='kitchen_daily') ORDER BY at",
        (iso,),
    ))
    if not hot and not checks:
        body = _empty("No closing checklist or food activity for this day.")
    else:
        bits = []
        if checks:
            labels = {"closing": "Closing checklist", "closing-test": "Closing checklist (test)", "kitchen_daily": "Kitchen checklist"}
            lines = []
            for row in checks:
                lines.append(
                    f"<tr><td>{portal.h(labels.get(row['action'], row['action']))}</td>"
                    f"<td>{portal.h(row['staff'] or '')}</td><td>{portal.h(portal.fmt_stamp(row['at']))}</td></tr>"
                )
            bits.append(
                '<div class="tw"><table><thead><tr><th>Checklist</th><th>Staff</th><th>When</th></tr></thead><tbody>'
                + "".join(lines) + "</tbody></table></div>"
            )
        else:
            bits.append(_empty("No closing checklist stored for this day."))
        if hot:
            lines = []
            for row in hot:
                lines.append(
                    f"<tr><td>{portal.h(row['box'] or '')}</td><td>{portal.h(row['item'] or '')}</td>"
                    f"<td>{portal.h(row['qty'] or '')}</td><td>{portal.h((row['time_in'] or '')[11:16])}</td>"
                    f"<td>{portal.h(row['outcome'] or 'in hot box')}</td><td>{portal.h(row['staff'] or '')}</td></tr>"
                )
            bits.append(
                "<h3>Food activity</h3>"
                '<div class="tw"><table><thead><tr><th>Hot box</th><th>Item</th><th>Qty</th><th>In</th><th>Result</th><th>Staff</th></tr></thead><tbody>'
                + "".join(lines) + "</tbody></table></div>"
            )
        else:
            bits.append(_empty("No food activity for this day."))
        body = "".join(bits)
    return f'<section class="card" id="day-activity"><h2>Closing checklist and food activity</h2>{body}</section>'


def _invoice_section(c, day):
    iso = day.isoformat()
    bills = portal.rows(c.execute(
        "SELECT supplier, invoice_no, inc_gst, status, amount_due FROM portal_bills WHERE bill_date=? ORDER BY supplier, invoice_no",
        (iso,),
    ))
    invoices = portal.rows(c.execute(
        "SELECT * FROM portal_invoices WHERE invoice_date=? ORDER BY supplier, invoice_number",
        (iso,),
    ))
    payments = portal.owner_payment_map(c)
    if not bills and not invoices:
        body = _empty("No invoices for this day.")
    else:
        bits = []
        if bills:
            lines = []
            for row in bills:
                lines.append(
                    f"<tr><td>{portal.h(row['supplier'])}</td><td>{portal.h(row['invoice_no'])}</td>"
                    f"<td class=\"num\">{portal.money(row['inc_gst'])}</td><td>{portal.h(row['status'] or '')}</td>"
                    f"<td class=\"num\">{portal.money(row['amount_due'])}</td></tr>"
                )
            bits.append(
                "<h3>Bills dated this day</h3>"
                '<div class="tw"><table><thead><tr><th>Supplier</th><th>Invoice</th><th>Inc GST</th><th>Status</th><th>Due</th></tr></thead><tbody>'
                + "".join(lines) + "</tbody></table></div>"
            )
        else:
            bits.append(_empty("No purchase bills dated this day."))
        if invoices:
            lines = []
            for row in invoices:
                payment = payments.get(row["invoice_key"])
                if payment and portal.payment_is_matched(payment, row):
                    state = "Paid – matched in Xero"
                elif payment:
                    state = "Paid – awaiting Xero match"
                else:
                    state = (row["status"] or "").strip() or "—"
                lines.append(
                    f"<tr><td>{portal.h(row['supplier'])}</td><td>{portal.h(row['invoice_number'])}</td>"
                    f"<td class=\"num\">{portal.money(row['amount_due'])}</td><td>{portal.h(state)}</td></tr>"
                )
            bits.append(
                "<h3>Invoice list dated this day</h3>"
                '<div class="tw"><table><thead><tr><th>Supplier</th><th>Invoice</th><th>Amount due</th><th>Status</th></tr></thead><tbody>'
                + "".join(lines) + "</tbody></table></div>"
            )
        else:
            bits.append(_empty("No invoice-list rows dated this day."))
        body = "".join(bits)
    return (
        f'<section class="card" id="day-invoices"><h2>Invoices received</h2>{body}'
        '<p class="help"><a href="/portal/invoices">Open invoices</a>. This page does not pay anyone.</p></section>'
    )


def page(c, query):
    day, problem = _day_from_query(c, query)
    error = f'<p class="alert">{portal.h(problem)}</p>' if problem else ""
    return f"""<div class="ph"><h1>Daily summary</h1>
<p>{portal.h(portal.fmt_date(day.isoformat()))}, Australia/Brisbane. One day of the figures already stored here. A missing feed stays blank.</p></div>
{error}{_nav(day)}
{_sales_section(c, day)}
{_commission_section(c, day)}
{_dips_section(c, day)}
{_hours_section(c, day)}
{_exceptions_section(c, day)}
{_activity_section(c, day)}
{_invoice_section(c, day)}"""


def _glance_card(c, label, day):
    iso = day.isoformat()
    sales = portal.one(c.execute("SELECT total FROM portal_sales_days WHERE date=?", (iso,)))
    loaded = portal.online_loaded(c)
    online = None
    if loaded:
        row = portal.one(c.execute("SELECT total FROM portal_online_days WHERE date=?", (iso,)))
        online = float(row["total"] or 0) if row else 0.0
    if sales:
        till = float(sales["total"] or 0)
        combined = till + (online or 0)
        figure = portal.money(combined)
        till_txt = portal.money(till)
    elif online:
        figure = portal.money(online)
        till_txt = "No till sales"
    else:
        figure = "—"
        till_txt = "No till sales"
    online_txt = "Online not loaded" if not loaded else portal.money(online or 0)
    href = "/portal/day?" + urlencode({"date": iso})
    return (
        f'<a class="glance" href="{portal.h(href)}"><div class="lbl">{portal.h(label)} · {portal.h(portal.fmt_date(iso))}</div>'
        f"<b>{figure}</b><span>Till {till_txt} · Online {online_txt}</span></a>"
    )


def glance(c):
    today_d = portal.today()
    yesterday = today_d - timedelta(days=1)
    cards = _glance_card(c, "Yesterday", yesterday) + _glance_card(c, "Today", today_d)
    return f"""<section class="card" id="day-glance"><h2>Today and yesterday at a glance</h2>
<p class="help">Opens the day on Daily summary. Today’s till sales wait for the 9:30pm Brisbane refresh.</p>
<div class="grid g2">{cards}</div></section>"""
