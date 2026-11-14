"""Monthly profit and loss for the owner portal.

Income is the portal's own till sales plus Bopple, ex GST. Cost of sales and
operating expenses are the Xero P&L for that month, already ex GST. Xero does
not hold sales. Wages and super stay on the Xero lines when that report has
them. Contract hours are typed on Staff hours and are not in Xero, so they are
an extra line and are not added on top of the Xero wage accounts. Daily rent is typed on this page and is not entered in Xero. United Petroleum
direct debits coded to Purchases - Fuel already include that rent, so the page
shows the rent as an operating expense and removes the same ex-GST amount from
fuel purchases. The two lines offset. A Xero rent account means both lines are
left out.
"""
import re
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP

import portal

_WAGE_ACCOUNT = re.compile(r"wages?|salar|superannuation|(?<![a-z])super(?![a-z])", re.I)
_ACCOUNT_ID = re.compile(r"[A-Za-z0-9_.:-]{1,80}")
NOTE = (
    "Xero purchases are by bill date, not stock used. "
    "Income is ex GST. Till shop and fuel are stored including GST, so each is divided by 1.1. "
    "Bopple online gross is added on top of till sales and divided by 1.1 the same way. "
    "Cost of sales and operating expenses are the Xero amounts, already ex GST. "
    "Wages and super are the Xero accounts when the report includes them. "
    "Contract hours are typed on Staff hours, are not in Xero, and are added as their own line. "
    "Employee wages already in Xero are not added again. "
    "Change is this month minus the previous month. "
    "The current month is month to date. The previous column is the whole previous month. "
    "Portal rent is typed on this page and is not entered in Xero. "
    "The typed amount includes GST and each day is that amount divided by 1.1. "
    "A finished month counts every day. This month counts the days through the same date as sales. "
    "United Petroleum direct debits that Xero codes to Purchases - Fuel already include this daily rent. "
    "The page removes that same ex-GST amount from fuel purchases and shows it as rent. "
    "Those two lines offset, so total costs and net profit do not change. "
    "If that month's Xero report already has a Rent or Rent & Lease account, both portal lines are left out."
)

FUEL_RENT_LABEL = "Less rent included in United fuel debits"
FUEL_RENT_LEAD = (
    "United Petroleum direct debits that Xero codes to Purchases - Fuel already include this daily rent. "
    "This removes the same ex-GST amount from fuel purchases for the same days the rent line counts. "
    "The operating-expense rent line shows that amount as rent. "
    "The two lines offset, so total costs and net profit do not change."
)
RENT_OFFSET_LEAD = (
    "The same amount is removed from Purchases - Fuel, because those United direct debits already include this rent."
)

# Owner Astron's daily rent, GST inclusive. The page shows it ex GST.
# A saved row with the same effective date replaces that step. Later dates do not.
DEFAULT_RENT_INC = Decimal("321.13")
DEFAULT_RENT_FROM = date(2026, 7, 1)
_RENT_WORD = re.compile(r"(?<![a-z])rent(?![a-z])")


def _month_key(value, label):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}", value):
        raise ValueError(f"{label} must be YYYY-MM")
    year, mon = int(value[:4]), int(value[5:])
    if year < 1990 or year > 2100 or not 1 <= mon <= 12:
        raise ValueError(f"{label} must be YYYY-MM")
    return f"{year:04d}-{mon:02d}"


def _text(value, label, limit):
    if not isinstance(value, str):
        raise ValueError(f"{label} must be text")
    text = " ".join(value.split())
    if not text or len(text) > limit:
        raise ValueError(f"{label} is required")
    return text


def apply_xero_pnl(c, data):
    """Replace one month of Xero P&L account rows. Other months stay."""
    if not isinstance(data, dict):
        raise ValueError("xero-pnl must be an object")
    portal.reject_sensitive(data, "xero-pnl")
    month = _month_key(data.get("month"), "month")
    accounts = data.get("accounts")
    if not isinstance(accounts, list) or not accounts:
        raise ValueError("xero-pnl needs account rows")
    if len(accounts) > 400:
        raise ValueError("xero-pnl has too many accounts")
    if data.get("asOf"):
        as_of = portal.need_date(data.get("asOf"), "asOf")
    else:
        year, mon = int(month[:4]), int(month[5:])
        as_of = portal.month_last_day(date(year, mon, 1)).isoformat()
    source = ""
    if data.get("source") not in (None, ""):
        if not isinstance(data.get("source"), str):
            raise ValueError("source must be text")
        source = " ".join(data.get("source").split())[:160]
    clean = []
    seen = set()
    for i, row in enumerate(accounts):
        if not isinstance(row, dict):
            raise ValueError(f"accounts[{i}] must be an object")
        section = _text(row.get("section"), f"accounts[{i}] section", 120)
        name = row.get("accountName", row.get("name"))
        account_name = _text(name if isinstance(name, str) else "", f"accounts[{i}] account name", 160)
        account_id = str(row.get("accountId") or row.get("account_id") or "").strip()
        if not _ACCOUNT_ID.fullmatch(account_id):
            raise ValueError(f"accounts[{i}] needs an account id")
        if account_id in seen:
            raise ValueError(f"accounts[{i}] repeats an account id")
        seen.add(account_id)
        amount = portal._cents(portal.num(row.get("amount"), "amount"))
        clean.append((section, account_name, account_id, float(amount)))
    c.execute("DELETE FROM portal_xero_pnl WHERE month=?", (month,))
    for index, (section, account_name, account_id, amount) in enumerate(clean):
        c.execute(
            """INSERT INTO portal_xero_pnl(month,account_id,section,account_name,amount,sort_order,source_as_of)
               VALUES(?,?,?,?,?,?,?)""",
            (month, account_id, section, account_name, amount, index, as_of),
        )
    months = portal.one(c.execute("SELECT COUNT(DISTINCT month) AS n FROM portal_xero_pnl"))["n"]
    portal.meta_set(c, "xero-pnl", {"asOf": as_of, "month": month, "accounts": len(clean), "months": months, "source": source})
    portal.save_snapshot(c, "xero-pnl", as_of, {"month": month, "asOf": as_of, "source": source, "accounts": [
        {"section": section, "accountName": name, "accountId": account_id, "amount": amount}
        for section, name, account_id, amount in clean
    ]})
    return {"asOf": as_of, "month": month, "accounts": len(clean)}


def section_kind(title):
    text = " ".join((title or "").casefold().split())
    if "cost of sales" in text:
        return "cost"
    if "operating expense" in text:
        return "expense"
    if "income" in text or "revenue" in text or "turnover" in text:
        return "income"
    return "other"


def is_wage_account(name):
    return bool(_WAGE_ACCOUNT.search(" ".join((name or "").casefold().split())))


def _rows(c, month):
    return portal.rows(c.execute(
        """SELECT section, account_name, account_id, amount, sort_order
           FROM portal_xero_pnl WHERE month=? ORDER BY sort_order, account_name""",
        (month,),
    ))


def _by_kind(rows):
    groups = {"cost": [], "expense": [], "income": [], "other": []}
    for row in rows:
        groups[section_kind(row["section"])].append(row)
    return groups


def _has_wages(rows):
    return any(is_wage_account(row["account_name"]) for row in rows)


def _cents_amount(row):
    return portal._cents(row["amount"])


def _sum_rows(rows):
    return sum((_cents_amount(row) for row in rows), Decimal("0.00"))


def _ex_gst(amount):
    return portal._cents(Decimal(amount) / portal.GST_DIVISOR)


def income_for(c, start, end):
    """Fuel, shop, and online ex GST. A missing feed stays missing, not zero."""
    sales = portal.till_sales_ex_gst(portal.sales_between(c, start, end))
    online_rows = portal.online_days_between(c, start, end)
    if online_rows is None:
        online_ex = None
    else:
        gross = sum((Decimal(str(row["total"] or 0)) for row in online_rows), Decimal("0"))
        online_ex = _ex_gst(gross)
    fuel = sales["fuel_ex"] if sales else None
    shop = sales["shop_ex"] if sales else None
    if sales is None and online_ex is None:
        total = None
    elif sales is None:
        total = online_ex if online_ex else None
    elif online_ex is None:
        total = sales["total_ex"]
    else:
        total = sales["total_ex"] + online_ex
    return {
        "fuel": fuel,
        "shop": shop,
        "online": online_ex,
        "total": total,
        "till_loaded": sales is not None,
        "online_loaded": online_rows is not None,
    }


def _contract(c, start, end):
    return sum((portal._cents(row["cost"]) for row in portal.contract_between(c, start, end)), Decimal("0.00"))


def _default_rent():
    return {"daily_inc": DEFAULT_RENT_INC, "effective_from": DEFAULT_RENT_FROM, "saved": False}


def load_rent_rates(c):
    """Saved daily rates, oldest first, with the 1 Jul 2026 default unless that date is saved."""
    found = portal.rows(c.execute(
        "SELECT effective_from, daily_inc FROM portal_rent_rates ORDER BY effective_from"
    ))
    saved = []
    for row in found:
        saved.append({
            "daily_inc": portal._cents(row["daily_inc"]),
            "effective_from": date.fromisoformat(row["effective_from"]),
            "saved": True,
        })
    if not any(row["effective_from"] == DEFAULT_RENT_FROM for row in saved):
        saved.append(_default_rent())
        saved.sort(key=lambda row: row["effective_from"])
    return saved


def rate_for_day(rates, day):
    """Latest rate whose effective date is on or before day, or None."""
    chosen = None
    for row in rates:
        if row["effective_from"] <= day:
            chosen = row
        else:
            break
    return chosen


def daily_ex_gst(daily_inc):
    """One day's rent ex GST. The stored rate includes GST, so divide by 1.1 and round to cents."""
    return portal._cents(portal._cents(daily_inc) / portal.GST_DIVISOR)


def rent_coverage(rates, start, end):
    """Ex-GST rent for each inclusive stretch that shares one rate. Days with no rate are left out."""
    stretches = []
    day = start
    while day <= end:
        rate = rate_for_day(rates, day)
        if stretches and stretches[-1]["rate"] is rate:
            stretches[-1]["end"] = day
        else:
            stretches.append({"rate": rate, "start": day, "end": day})
        day += timedelta(days=1)
    segments = []
    total = Decimal("0.00")
    days = 0
    for stretch in stretches:
        if stretch["rate"] is None:
            continue
        count = (stretch["end"] - stretch["start"]).days + 1
        ex = daily_ex_gst(stretch["rate"]["daily_inc"])
        amount = portal._cents(ex * count)
        segments.append({
            "daily_inc": stretch["rate"]["daily_inc"],
            "daily_ex": ex,
            "start": stretch["start"],
            "end": stretch["end"],
            "days": count,
            "amount": amount,
        })
        total += amount
        days += count
    return {"segments": segments, "amount": total, "days": days}


def xero_rent_names(rows):
    """Account names that already are Rent or Rent & Lease, so the portal line must not be added."""
    names = []
    for row in rows:
        text = " ".join((row["account_name"] or "").casefold().replace("&", " and ").replace("/", " ").split())
        if _RENT_WORD.search(text):
            names.append(row["account_name"])
    return names


def _plain_money(amount):
    return format(portal._cents(amount), "f")


def _segment_note(seg):
    inc = _plain_money(seg["daily_inc"])
    ex = portal.money(seg["daily_ex"])
    start = portal.fmt_date(seg["start"].isoformat())
    finish = portal.fmt_date(seg["end"].isoformat())
    unit = "day" if seg["days"] == 1 else "days"
    return (
        f"${inc} a day includes GST. This line is ex GST, {inc} / 1.1 = {ex} a day. "
        f"Counted {start} to {finish} ({seg['days']} {unit})."
    )


def _coverage_note(coverage, month_start, end, mtd):
    if not coverage["segments"]:
        return ""
    text = " ".join(_segment_note(seg) for seg in coverage["segments"])
    if mtd:
        return text + " The end date is the same month-to-date date this page uses for sales."
    last = portal.month_last_day(month_start)
    span = (last - month_start).days + 1
    if coverage["days"] == span and end == last:
        text += " Every day of the month is counted."
    return text


def _rent_side(rates, month_start, end, rows, loaded, mtd):
    empty = {
        "loaded": False, "skipped": False, "names": [], "show": False,
        "amount": None, "text": None, "note": "", "label_rate": None,
        "included": Decimal("0.00"), "days": 0,
    }
    if not loaded:
        return empty
    names = xero_rent_names(rows)
    if names:
        return {
            "loaded": True, "skipped": True, "names": names, "show": False,
            "amount": None, "text": "In Xero", "note": "", "label_rate": None,
            "included": Decimal("0.00"), "days": 0,
        }
    coverage = rent_coverage(rates, month_start, end)
    show = coverage["days"] > 0
    single = {seg["daily_inc"] for seg in coverage["segments"]}
    return {
        "loaded": True, "skipped": False, "names": [], "show": show,
        "amount": coverage["amount"] if show else Decimal("0.00"),
        "text": None,
        "note": _coverage_note(coverage, month_start, end, mtd) if show else "",
        "label_rate": next(iter(single)) if len(single) == 1 else None,
        "included": coverage["amount"],
        "days": coverage["days"],
    }


def _rent_label(rate):
    return f"Rent (portal, {portal.money(rate)}/day inc GST)"


def _rent_warning(month_label, names):
    if len(names) == 2:
        shown = " and ".join(names)
    else:
        shown = ", ".join(names)
    return (
        f"{month_label} already has {shown} in the Xero report, "
        "so the portal daily rent line is not added and Purchases - Fuel is left as Xero recorded it."
    )


def rent_warnings(current, previous, prev_loaded, month_label, prev_label):
    warnings = []
    if current["rent"]["skipped"]:
        warnings.append(_rent_warning(month_label, current["rent"]["names"]))
    if prev_loaded and previous["rent"]["skipped"]:
        warnings.append(_rent_warning(prev_label, previous["rent"]["names"]))
    return warnings


def _rent_visible(current, previous, prev_loaded):
    return current["rent"]["show"] or (prev_loaded and previous["rent"]["show"])


def _day_notes(current, previous, prev_loaded):
    notes = []
    if current["rent"]["note"]:
        notes.append(current["rent"]["note"])
    if prev_loaded and previous["rent"]["note"]:
        notes.append("Previous month: " + previous["rent"]["note"])
    return " ".join(notes)


def _signed_rent_cells(current, previous, prev_loaded, sign):
    """sign is 1 for the rent expense and -1 for the fuel-purchase reduction."""
    def cell(side):
        if side["skipped"]:
            return None, "In Xero" if sign > 0 else "Left in fuel"
        amount = side["amount"] if side["amount"] is not None else Decimal("0.00")
        if sign < 0:
            amount = -amount
        return amount, None

    this_amount, this_text = cell(current["rent"])
    if not prev_loaded:
        return this_amount, this_text, None, None
    prev_amount, prev_text = cell(previous["rent"])
    return this_amount, this_text, prev_amount, prev_text


def _append_fuel_rent_line(rows, current, previous, prev_loaded):
    if not _rent_visible(current, previous, prev_loaded):
        return
    hint = FUEL_RENT_LEAD
    days = _day_notes(current, previous, prev_loaded)
    if days:
        hint = hint + " " + days
    this_amount, this_text, prev_amount, prev_text = _signed_rent_cells(current, previous, prev_loaded, -1)
    rows.append(_line(
        FUEL_RENT_LABEL, this_amount, prev_amount, row_id="pnl-rent-fuel", hint=hint, title=hint,
        this_text=this_text, prev_text=prev_text,
    ))


def _append_rent_line(rows, current, previous, prev_loaded):
    if not _rent_visible(current, previous, prev_loaded):
        return
    cur, prev = current["rent"], previous["rent"]
    show_prev = prev_loaded and prev["show"]
    if cur["show"] and cur["label_rate"] is not None:
        label = _rent_label(cur["label_rate"])
    elif show_prev and prev["label_rate"] is not None and not cur["show"]:
        label = _rent_label(prev["label_rate"])
    else:
        label = "Rent (portal)"
    hint = RENT_OFFSET_LEAD
    days = _day_notes(current, previous, prev_loaded)
    if days:
        hint = hint + " " + days
    this_amount, this_text, prev_amount, prev_text = _signed_rent_cells(current, previous, prev_loaded, 1)
    rows.append(_line(
        label, this_amount, prev_amount, row_id="pnl-rent", hint=hint, title=hint,
        this_text=this_text, prev_text=prev_text,
    ))


def _rates_help(rates):
    saved = [row for row in rates if row["saved"]]
    default_saved = any(row["saved"] and row["effective_from"] == DEFAULT_RENT_FROM for row in rates)
    bits = []
    if saved:
        shown = "; ".join(
            f"{portal.money(row['daily_inc'])} from {portal.fmt_date(row['effective_from'].isoformat())}"
            for row in saved
        )
        bits.append(f"Saved rates: {shown}.")
    if not default_saved:
        bits.append(
            f"Unless a rate is saved for {portal.fmt_date(DEFAULT_RENT_FROM.isoformat())}, "
            f"the page uses {portal.money(DEFAULT_RENT_INC)} a day including GST from that date."
        )
    bits.append(
        "Saving the same effective date replaces that rate. "
        "A new date leaves earlier months on the rate that applied then. Nothing is sent to Xero."
    )
    return " ".join(bits)


def _rate_to_edit(rates, today_d):
    chosen = rate_for_day(rates, today_d)
    return chosen or rates[0]


def save_rent_rate(c, actor, form):
    """Insert or replace one daily rent rate. Other effective dates stay."""
    amount = portal._money_input(
        form.get("daily_inc"), "daily rent including GST", True, Decimal("0.01"), Decimal("100000"),
    )
    try:
        effective = date.fromisoformat(portal.need_date(form.get("effective_from"), "effective from"))
    except ValueError:
        raise ValueError("Effective from must be a date.")
    if effective < date(2024, 1, 1) or effective > date(2100, 12, 31):
        raise ValueError("Choose an effective date from 2024 through 2100.")
    c.execute(
        """INSERT INTO portal_rent_rates(effective_from, daily_inc, updated_at, updated_by)
           VALUES(?,?,?,?)
           ON CONFLICT(effective_from) DO UPDATE SET daily_inc=excluded.daily_inc,
             updated_at=excluded.updated_at, updated_by=excluded.updated_by""",
        (effective.isoformat(), float(amount), portal.stamp(), actor),
    )


def _rent_settings(c, user, month):
    rates = load_rent_rates(c)
    current = _rate_to_edit(rates, portal.today())
    return f'''<section class="card" id="rent-setting"><h2>Portal rent</h2>
<p class="help">Daily rent for this Profit &amp; Loss page only. It is not entered in Xero. The amount includes GST. The line divides it by 1.1, then multiplies by the days covered. A finished month counts every day. This month counts through the same date the page uses for sales. United Petroleum direct debits coded to Purchases - Fuel already include this rent, so the page also removes the same ex-GST amount from fuel purchases. The two lines offset.</p>
<p class="help" id="rent-rates">{portal.h(_rates_help(rates))}</p>
<form method="post" action="/portal/pnl" id="rent-form">
<input type="hidden" name="csrf" value="{portal.h(user["csrf"])}">
<input type="hidden" name="action" value="save_rent_rate">
<input type="hidden" name="month" value="{month.strftime("%Y-%m")}">
<div class="row">
<label class="f">Daily rent, including GST<input name="daily_inc" id="rent-daily" inputmode="decimal" required value="{portal.h(portal._amount_value(current["daily_inc"]))}"></label>
<label class="f">Effective from<input type="date" name="effective_from" id="rent-effective" required value="{current["effective_from"].isoformat()}" min="2024-01-01" max="2100-12-31"></label>
</div>
<button class="btn" type="submit">Save rent rate</button>
</form></section>'''


def _portal_wages(c, start, end, rows, loaded):
    """Portal employee wages only when this month's Xero report has no wage or super account."""
    if not loaded:
        return "skip", None
    if _has_wages(rows):
        return "xero", None
    bill = portal.wages_for_range(c, start, end)["bill"]
    if bill["missing"]:
        return "incomplete", None
    if bill["employee_incl"] is None:
        return "none", Decimal("0.00")
    return "amount", bill["employee_incl"]


def _merge(current_rows, previous_rows, prev_loaded):
    prev_by = {row["account_id"]: row for row in previous_rows}
    lines = []
    seen = set()
    for row in current_rows:
        seen.add(row["account_id"])
        other = prev_by.get(row["account_id"])
        if not prev_loaded:
            prev = None
        elif other is None:
            prev = Decimal("0.00")
        else:
            prev = _cents_amount(other)
        lines.append({"label": row["account_name"], "this": _cents_amount(row), "prev": prev, "hint": ""})
    if prev_loaded:
        for row in previous_rows:
            if row["account_id"] in seen:
                continue
            lines.append({
                "label": row["account_name"],
                "this": Decimal("0.00"),
                "prev": _cents_amount(row),
                "hint": "",
            })
    return lines


def _delta(this, prev, places="0.01"):
    if this is None or prev is None:
        return None
    return (Decimal(this) - Decimal(prev)).quantize(Decimal(places), rounding=ROUND_HALF_UP)


def _line(label, this, prev, kind="line", hint="", row_id="", percent=False, this_text=None, prev_text=None, title=""):
    places = "0.1" if percent else "0.01"
    return {
        "kind": kind,
        "label": label,
        "hint": hint,
        "title": title,
        "this": this,
        "prev": prev,
        "change": None if this_text or prev_text else _delta(this, prev, places),
        "id": row_id,
        "percent": percent,
        "this_text": this_text,
        "prev_text": prev_text,
    }


def _section(label):
    return {"kind": "section", "label": label, "hint": "", "this": None, "prev": None, "change": None,
            "id": "", "percent": False, "this_text": None, "prev_text": None}


def _wage_cell(mode, amount):
    if mode == "xero":
        return None, "Included in Xero"
    if mode == "incomplete":
        return None, "No rate yet"
    if mode in ("skip",):
        return None, None
    return amount, None


def _expense_total(rows, contract, wage_mode, wage_amount, rent_included, loaded):
    if not loaded:
        return None
    total = _sum_rows(rows) + contract + rent_included
    if wage_mode == "amount" and wage_amount is not None:
        total += wage_amount
    return total


def _side_figures(c, month_start, end, rows, loaded, rates, mtd):
    income = income_for(c, month_start, end)
    kinds = _by_kind(rows) if loaded else {"cost": [], "expense": [], "income": [], "other": []}
    cost = _sum_rows(kinds["cost"]) if loaded else None
    other = _sum_rows(kinds["other"]) if loaded else None
    contract = _contract(c, month_start, end) if loaded else None
    wage_mode, wage_amount = _portal_wages(c, month_start, end, rows, loaded)
    rent = _rent_side(rates, month_start, end, rows, loaded, mtd)
    if cost is not None:
        cost = cost - rent["included"]
    expense = _expense_total(
        kinds["expense"], contract or Decimal("0.00"), wage_mode, wage_amount, rent["included"], loaded,
    )
    if not loaded or income["total"] is None or cost is None:
        gross = None
        gp_pct = None
    else:
        gross = income["total"] - cost
        gp_pct = portal.wage_ratio(gross, income["total"])
    if gross is None or expense is None:
        net = None
        margin = None
    else:
        net = gross - expense - (other or Decimal("0.00"))
        margin = portal.wage_ratio(net, income["total"])
    return {
        "income": income,
        "kinds": kinds,
        "cost": cost,
        "other": other,
        "contract": contract,
        "wage_mode": wage_mode,
        "wage_amount": wage_amount,
        "rent": rent,
        "expense": expense,
        "gross": gross,
        "gp_pct": gp_pct,
        "net": net,
        "margin": margin,
    }


def build(c, month_start, today_d):
    """Figures for one calendar month and the previous month. Decimals, or None when missing."""
    mtd = month_start.year == today_d.year and month_start.month == today_d.month
    end = today_d if mtd else portal.month_last_day(month_start)
    prev_start = (month_start - timedelta(days=1)).replace(day=1)
    prev_end = portal.month_last_day(prev_start)
    month = f"{month_start.year:04d}-{month_start.month:02d}"
    prev_month = f"{prev_start.year:04d}-{prev_start.month:02d}"
    current_rows = _rows(c, month)
    previous_rows = _rows(c, prev_month)
    loaded = bool(current_rows)
    prev_loaded = bool(previous_rows)
    rates = load_rent_rates(c)
    current = _side_figures(c, month_start, end, current_rows, loaded, rates, mtd)
    previous = _side_figures(c, prev_start, prev_end, previous_rows, prev_loaded, rates, False)
    rows = _statement_rows(current, previous, loaded, prev_loaded)
    month_label = _month_name(month_start)
    prev_label = _month_name(prev_start)
    return {
        "month": month,
        "prev_month": prev_month,
        "start": month_start,
        "end": end,
        "mtd": mtd,
        "loaded": loaded,
        "prev_loaded": prev_loaded,
        "fuel": current["income"]["fuel"],
        "shop": current["income"]["shop"],
        "online": current["income"]["online"],
        "income": current["income"]["total"],
        "till_loaded": current["income"]["till_loaded"],
        "online_loaded": current["income"]["online_loaded"],
        "cost": current["cost"],
        "gross": current["gross"],
        "gp_pct": current["gp_pct"],
        "contract": current["contract"],
        "portal_wages": current["wage_amount"] if current["wage_mode"] == "amount" else None,
        "wage_mode": current["wage_mode"],
        "rent": current["rent"]["included"] if loaded else None,
        "rent_days": current["rent"]["days"] if loaded else 0,
        "rent_skipped": current["rent"]["skipped"] if loaded else False,
        "expenses": current["expense"],
        "net": current["net"],
        "margin": current["margin"],
        "prev_income": previous["income"]["total"],
        "prev_net": previous["net"],
        "prev_rent": previous["rent"]["included"] if prev_loaded else None,
        "prev_expenses": previous["expense"],
        "rows": rows,
        "notes": _notes(current["income"]),
        "rent_warnings": rent_warnings(current, previous, prev_loaded, month_label, prev_label),
    }


def _show_wages(current, previous, prev_loaded):
    if current["wage_mode"] in ("amount", "incomplete", "none"):
        return True
    return prev_loaded and previous["wage_mode"] in ("amount", "incomplete")


def _account_lines(current_rows, previous_rows, prev_loaded):
    return [
        _line(line["label"], line["this"], line["prev"])
        for line in _merge(current_rows, previous_rows if prev_loaded else [], prev_loaded)
    ]


def _statement_rows(current, previous, loaded, prev_loaded):
    cur_in, prev_in = current["income"], previous["income"]
    rows = [
        _section("Income"),
        _line("Fuel", cur_in["fuel"], prev_in["fuel"], row_id="pnl-fuel"),
        _line("Shop", cur_in["shop"], prev_in["shop"], row_id="pnl-shop"),
        _line("Online (Bopple)", cur_in["online"], prev_in["online"], row_id="pnl-online"),
        _line("Total income", cur_in["total"], prev_in["total"], kind="total", row_id="pnl-income-row"),
    ]
    if not loaded:
        return rows
    if current["kinds"]["income"] or (prev_loaded and previous["kinds"]["income"]):
        rows.append(_section("Xero income (not included)"))
        rows.extend(_account_lines(current["kinds"]["income"], previous["kinds"]["income"], prev_loaded))
    rows.append(_section("Cost of sales"))
    rows.extend(_account_lines(current["kinds"]["cost"], previous["kinds"]["cost"], prev_loaded))
    _append_fuel_rent_line(rows, current, previous, prev_loaded)
    rows.append(_line("Total cost of sales", current["cost"], previous["cost"] if prev_loaded else None, kind="total", row_id="pnl-cos"))
    rows.append(_line("Gross profit", current["gross"], previous["gross"] if prev_loaded else None, kind="total", row_id="pnl-gross-row"))
    rows.append(_line(
        "Gross profit %", current["gp_pct"], previous["gp_pct"] if prev_loaded else None,
        kind="pct", row_id="pnl-gp-pct", percent=True,
    ))
    rows.append(_section("Operating expenses"))
    for line in _merge(current["kinds"]["expense"], previous["kinds"]["expense"] if prev_loaded else [], prev_loaded):
        rows.append(_line(line["label"], line["this"], line["prev"]))
    if _show_wages(current, previous, prev_loaded):
        this_amount, this_text = _wage_cell(current["wage_mode"], current["wage_amount"])
        if prev_loaded:
            prev_amount, prev_text = _wage_cell(previous["wage_mode"], previous["wage_amount"])
        else:
            prev_amount, prev_text = None, None
        rows.append(_line(
            "Staff wages and super", this_amount, prev_amount, row_id="pnl-portal-wages",
            hint="From Staff hours only when that month's Xero report has no wages or super accounts",
            this_text=this_text, prev_text=prev_text,
        ))
    rows.append(_line(
        "Contract hours", current["contract"], previous["contract"] if prev_loaded else None,
        row_id="pnl-contract", hint="Not in Xero",
    ))
    _append_rent_line(rows, current, previous, prev_loaded)
    rows.append(_line(
        "Total operating expenses", current["expense"], previous["expense"] if prev_loaded else None,
        kind="total", row_id="pnl-opex",
    ))
    if current["kinds"]["other"] or (prev_loaded and previous["kinds"]["other"]):
        rows.append(_section("Other"))
        for line in _merge(current["kinds"]["other"], previous["kinds"]["other"] if prev_loaded else [], prev_loaded):
            rows.append(_line(line["label"], line["this"], line["prev"]))
    rows.append(_line("Net profit", current["net"], previous["net"] if prev_loaded else None, kind="total", row_id="pnl-net-row"))
    rows.append(_line(
        "Net margin", current["margin"], previous["margin"] if prev_loaded else None,
        kind="pct", row_id="pnl-margin", percent=True,
    ))
    return rows


def _notes(income):
    notes = []
    if not income["till_loaded"]:
        notes.append("Till sales are not loaded for this month.")
    elif not income["online_loaded"]:
        notes.append("Online orders are not loaded, so the total is till only.")
    return notes


def _selected_month(query, today_d):
    current = today_d.replace(day=1)
    raw = (query or {}).get("month")
    if not raw:
        return current, ""
    if isinstance(raw, list):
        raw = raw[0]
    try:
        if not isinstance(raw, str) or not re.fullmatch(r"\d{4}-\d{2}", raw):
            raise ValueError
        year, mon = int(raw[:4]), int(raw[5:])
        chosen = date(year, mon, 1)
    except ValueError:
        return current, "Month must be YYYY-MM."
    if chosen.year < 2024 or chosen > current:
        return current, "Choose a month from 2024 through this month."
    return chosen, ""


def _month_name(day):
    return day.strftime("%B %Y")


def _shift_month(day, delta):
    year = day.year + (day.month - 1 + delta) // 12
    month = (day.month - 1 + delta) % 12 + 1
    return date(year, month, 1)


def _show(row, side):
    text = row.get(side + "_text")
    if text:
        return portal.h(text)
    value = row.get(side)
    if row.get("percent"):
        return portal.h(portal.pct_text(value))
    return portal.h(portal.money(value))


def _table(rows):
    body = []
    for row in rows:
        if row["kind"] == "section":
            body.append(f'<tr class="pnl-section"><td colspan="4">{portal.h(row["label"])}</td></tr>')
            continue
        hint = f'<small class="mini">{portal.h(row["hint"])}</small>' if row.get("hint") else ""
        ident = f' id="{row["id"]}"' if row.get("id") else ""
        cls = "pnl-total" if row["kind"] in ("total", "pct") else ""
        negative = row.get("this") is not None and not row.get("percent") and row["this"] < 0 and row["kind"] == "total"
        if negative:
            cls = (cls + " negative").strip()
        klass = f' class="{cls}"' if cls else ""
        label = portal.h(row["label"])
        if row.get("title"):
            label = f'<span title="{portal.h(row["title"])}">{label}</span>'
        body.append(
            f'<tr{klass}{ident}><td>{label}{hint}</td>'
            f'<td class="num">{_show(row, "this")}</td>'
            f'<td class="num">{_show(row, "prev")}</td>'
            f'<td class="num">{_show(row, "change")}</td></tr>'
        )
    return "".join(body)


def _cards(rows, this_head, prev_head):
    parts = []
    for row in rows:
        if row["kind"] == "section":
            parts.append(f'<h3 class="pnl-kicker">{portal.h(row["label"])}</h3>')
            continue
        hint = f'<span class="mini">{portal.h(row["hint"])}</span>' if row.get("hint") else ""
        cls = "pnl-card total" if row["kind"] in ("total", "pct") else "pnl-card"
        title = f' title="{portal.h(row["title"])}"' if row.get("title") else ""
        parts.append(
            f'<article class="{cls}"><h3{title}>{portal.h(row["label"])}{hint}</h3><dl>'
            f'<div><dt>{portal.h(this_head)}</dt><dd><b>{_show(row, "this")}</b></dd></div>'
            f'<div><dt>{portal.h(prev_head)}</dt><dd><b>{_show(row, "prev")}</b></dd></div>'
            f'<div><dt>Change</dt><dd><b>{_show(row, "change")}</b></dd></div>'
            f'</dl></article>'
        )
    return "".join(parts)


def _stat(ident, label, value, span, percent=False):
    shown = portal.pct_text(value) if percent else portal.money(value)
    return (
        f'<div class="card stat" id="{ident}"><div class="lbl">{portal.h(label)}</div>'
        f'<b>{portal.h(shown)}</b><span>{portal.h(span)}</span></div>'
    )


def _form(month, today_d):
    current = today_d.replace(day=1)
    prev = _shift_month(month, -1)
    chips = [
        f'<a class="chip" href="/portal/pnl?month={prev.strftime("%Y-%m")}">{portal.h(_month_name(prev))}</a>',
        f'<a class="chip on" href="/portal/pnl?month={month.strftime("%Y-%m")}" aria-current="page">{portal.h(_month_name(month))}</a>',
    ]
    nxt = _shift_month(month, 1)
    if nxt <= current:
        chips.append(f'<a class="chip" href="/portal/pnl?month={nxt.strftime("%Y-%m")}">{portal.h(_month_name(nxt))}</a>')
    value = month.strftime("%Y-%m")
    return f'''<form class="period" id="pnl-month" method="get" action="/portal/pnl">
<div class="chips">{''.join(chips)}</div>
<div class="row">
<label class="f">Month<input type="month" name="month" value="{value}" min="2024-01" max="{current.strftime("%Y-%m")}" required></label>
<button class="btn" type="submit">Show</button>
</div></form>'''


def page(c, query, user=None, error=""):
    today_d = portal.today()
    month, month_error = _selected_month(query, today_d)
    statement = build(c, month, today_d)
    mtd = " month to date" if statement["mtd"] else ""
    period = _month_name(month) + mtd
    if statement["mtd"]:
        period += " through " + portal.fmt_date(statement["end"].isoformat())
    else:
        period += ", " + portal.fmt_date(statement["start"].isoformat()) + " to " + portal.fmt_date(statement["end"].isoformat())
    prev_name = _month_name(date(int(statement["prev_month"][:4]), int(statement["prev_month"][5:]), 1))
    this_head = _month_name(month) + (" month to date" if statement["mtd"] else "")
    alert = f'<p class="alert" id="pnl-error">{portal.h(month_error)}</p>' if month_error else ""
    if error:
        alert += f'<p class="alert" id="pnl-rent-error">{portal.h(error)}</p>'
    ok_text = ""
    if (query or {}).get("ok") == "rent":
        ok_text = '<p class="oknote" id="pnl-rent-saved">Saved. The rent rate is stored on this portal only. Nothing was sent to Xero.</p>'
    warnings = ""
    if statement["rent_warnings"]:
        items = "".join(f"<p>{portal.h(text)}</p>" for text in statement["rent_warnings"])
        warnings = f'<div class="alert" id="pnl-rent-xero">{items}</div>'
    notes = "".join(f'<p class="help">{portal.h(note)}</p>' for note in statement["notes"])
    settings = _rent_settings(c, user, month) if user else ""
    if statement["loaded"]:
        gp_span = portal.pct_text(statement["gp_pct"]) + " of income" if statement["gp_pct"] is not None else "Share of income"
        margin_span = portal.pct_text(statement["margin"]) + " of income" if statement["margin"] is not None else "Share of income"
        stats = (
            '<div class="grid g3">'
            + _stat("pnl-income", "Income ex GST", statement["income"], "Fuel, shop, and online")
            + _stat("pnl-gross", "Gross profit", statement["gross"], gp_span)
            + _stat("pnl-net", "Net profit", statement["net"], margin_span)
            + "</div>"
        )
        missing = ""
        if not statement["prev_loaded"]:
            missing = f'<p class="help" id="pnl-prev-missing">{portal.h(prev_name)}: Xero P&L not loaded yet.</p>'
    else:
        stats = '<div class="grid g3">' + _stat("pnl-income", "Income ex GST", statement["income"], "Fuel, shop, and online") + "</div>"
        missing = '<p class="empty" id="xero-pnl-empty">Xero P&L not loaded yet</p>'
    table = f'''<div class="pnl-wrap"><table class="pnl-table">
<thead><tr><th>Account</th><th class="num">{portal.h(this_head)}</th><th class="num">{portal.h(prev_name)}</th><th class="num">Change</th></tr></thead>
<tbody>{_table(statement["rows"])}</tbody></table></div>
<div class="pnl-cards">{_cards(statement["rows"], this_head, prev_name)}</div>'''
    return f'''<div class="ph"><h1>Profit &amp; Loss</h1>
<p class="through" id="pnl-period">{portal.h(period)}. Compared with {portal.h(prev_name)}.</p></div>
{alert}{ok_text}{_form(month, today_d)}
{stats}
<section class="card" id="pnl-statement"><h2>{portal.h(_month_name(month))}</h2>
{notes}{warnings}{missing}{table}
<p class="help" id="pnl-note">{portal.h(NOTE)}</p></section>
{settings}'''
