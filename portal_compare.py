"""Month-by-month sales against the same month last year.

Last year is a SwiftPOS PLU Sales Total for the whole month (inc GST, fuel litres,
and department totals). It is not a day-by-day file. Same-days figures are that
month's daily average. More months are added with the last-year-plu ingest, or by
putting them in tools/fixtures/ly-plu-months.json before the next start. A month
that is not stored is left blank. It is not shown as zero.
"""
import json
import re
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

import portal

SEED_PATH = Path(__file__).resolve().parent / "tools" / "fixtures" / "ly-plu-months.json"
MONEY = Decimal("0.01")
LITRES = Decimal("0.001")
GRADE_ORDER = (("DIS", "Diesel"), ("ULP", "Unleaded"), ("P95", "Premium 95"))
FUEL_NAMES = {"a fuel", "fuel"}


def prorate(amount, days_this, days_last, places="0.01"):
    """Share of a monthly total. None when either side has no days or no amount."""
    if amount is None or not days_this or not days_last:
        return None
    share = Decimal(str(amount)) * Decimal(int(days_this)) / Decimal(int(days_last))
    return share.quantize(Decimal(places), rounding=ROUND_HALF_UP)


def _amount(value, label, places):
    number = portal.num(value, label, required=False)
    if number is None:
        return None
    return float(Decimal(str(number)).quantize(places, rounding=ROUND_HALF_UP))


def _text(value, label, limit):
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError(f"{label} must be text")
    return " ".join(value.split())[:limit]


def _norm(name):
    return " ".join(str(name or "").casefold().split())


def _slug(text):
    return re.sub(r"[^a-z0-9]+", "-", _norm(text)).strip("-") or "row"


def _month_key(value, label):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}", value):
        raise ValueError(f"{label} must be YYYY-MM")
    year, month = int(value[:4]), int(value[5:])
    if year < 1990 or year > 2100 or not 1 <= month <= 12:
        raise ValueError(f"{label} must be YYYY-MM")
    return f"{year:04d}-{month:02d}"


def _grade_row(row, label):
    if not isinstance(row, dict):
        raise ValueError(f"{label} must be an object")
    grade = str(row.get("grade") or "").strip().upper()
    if not re.fullmatch(r"[A-Z0-9]{1,8}", grade):
        raise ValueError(f"{label} needs a grade code")
    name = _text(row.get("name"), "name", 40) or grade
    portal.parity()._text_card(name, "last-year-plu")
    return {
        "grade": grade,
        "name": name,
        "litres": _amount(row.get("litres"), "litres", LITRES),
        "salesInc": _amount(row.get("salesInc"), "salesInc", MONEY),
        "salesEx": _amount(row.get("salesEx"), "salesEx", MONEY),
    }


def _department_row(row, label):
    if not isinstance(row, dict):
        raise ValueError(f"{label} must be an object")
    name = _text(row.get("name"), "name", 80)
    if not name:
        raise ValueError(f"{label} needs a name")
    portal.parity()._text_card(name, "last-year-plu")
    fuel = row.get("fuel", False)
    if not isinstance(fuel, bool):
        raise ValueError(f"{label} fuel must be true or false")
    clean = {
        "name": name,
        "salesInc": _amount(row.get("salesInc"), "salesInc", MONEY),
        "salesEx": _amount(row.get("salesEx"), "salesEx", MONEY),
        "qty": _amount(row.get("qty"), "qty", LITRES),
    }
    if fuel:
        clean["fuel"] = True
    return clean


def _clean_month(row, label):
    if not isinstance(row, dict):
        raise ValueError(f"{label} must be an object")
    portal.reject_sensitive(row, "last-year-plu")
    portal.parity().reject_last_year_metrics(row, "last-year-plu")
    key = _month_key(row.get("month"), f"{label} month")
    year, month = int(key[:4]), int(key[5:])
    days = portal.whole(row.get("days"), "days") if row.get("days") is not None else None
    if days is not None and not 1 <= days <= 31:
        raise ValueError("days must be from 1 to 31")
    grades_in = row.get("fuelGrades") or []
    depts_in = row.get("departments") or []
    if not isinstance(grades_in, list) or not isinstance(depts_in, list):
        raise ValueError(f"{label} fuel grades and departments must be lists")
    if len(grades_in) > 12 or len(depts_in) > 80:
        raise ValueError(f"{label} has too many rows")
    start = row.get("fromDate")
    end = row.get("toDate")
    clean = {
        "month": key,
        "label": _text(row.get("label"), "label", 80) or date(year, month, 1).strftime("%B %Y"),
        "fromDate": portal.need_date(start, "fromDate") if start else "",
        "toDate": portal.need_date(end, "toDate") if end else "",
        "days": days,
        "tradingWindow": _text(row.get("tradingWindow"), "tradingWindow", 120),
        "source": _text(row.get("source"), "source", 300),
        "note": _text(row.get("note"), "note", 500),
        "totalInc": _amount(row.get("totalInc"), "totalInc", MONEY),
        "totalEx": _amount(row.get("totalEx"), "totalEx", MONEY),
        "shopInc": _amount(row.get("shopInc"), "shopInc", MONEY),
        "shopEx": _amount(row.get("shopEx"), "shopEx", MONEY),
        "fuelInc": _amount(row.get("fuelInc"), "fuelInc", MONEY),
        "fuelEx": _amount(row.get("fuelEx"), "fuelEx", MONEY),
        "fuelLitres": _amount(row.get("fuelLitres"), "fuelLitres", LITRES),
        "fuelGrades": [_grade_row(item, f"{label} fuelGrades") for item in grades_in],
        "departments": [_department_row(item, f"{label} departments") for item in depts_in],
    }
    return clean


def _has_figure(row):
    if any(row.get(key) is not None for key in ("totalInc", "shopInc", "fuelInc", "fuelLitres")):
        return True
    for grade in row.get("fuelGrades") or []:
        if grade.get("salesInc") is not None or grade.get("litres") is not None:
            return True
    for dept in row.get("departments") or []:
        if dept.get("salesInc") is not None:
            return True
    return False


def months_in(data):
    if not isinstance(data, dict):
        raise ValueError("last-year-plu must be an object")
    portal.reject_sensitive(data, "last-year-plu")
    portal.parity().reject_last_year_metrics(data, "last-year-plu")
    has_one = "month" in data
    has_many = "months" in data
    if has_one and has_many:
        raise ValueError("last-year-plu needs months or one month, not both")
    if has_many:
        rows = data.get("months")
        if not isinstance(rows, list) or not rows:
            raise ValueError("last-year-plu needs a months list")
    elif has_one:
        rows = [data]
    else:
        raise ValueError("last-year-plu needs a month")
    if len(rows) > 240:
        raise ValueError("last-year-plu has too many months")
    ordered = {}
    for i, row in enumerate(rows):
        clean = _clean_month(row, f"months[{i}]")
        ordered[clean["month"]] = clean
    return ordered


def _as_of(data, ordered):
    if data.get("asOf"):
        return portal.need_date(data.get("asOf"), "asOf")
    latest = max(ordered)
    year, month = int(latest[:4]), int(latest[5:])
    return portal.month_last_day(date(year, month, 1)).isoformat()


def _remember(c, as_of):
    total = portal.one(c.execute("SELECT COUNT(*) AS n FROM portal_ly_plu"))["n"]
    portal.meta_set(c, "last-year-plu", {"asOf": as_of, "months": total})


def apply(c, data):
    """Upsert prior-year months. Other months already stored are kept."""
    ordered = months_in(data)
    as_of = _as_of(data, ordered)
    for row in ordered.values():
        raw = json.dumps(row, separators=(",", ":"), ensure_ascii=False)
        c.execute(
            """INSERT INTO portal_ly_plu(month,payload,updated_at) VALUES(?,?,?)
               ON CONFLICT(month) DO UPDATE SET payload=excluded.payload, updated_at=excluded.updated_at""",
            (row["month"], raw, portal.stamp()),
        )
    _remember(c, as_of)
    portal.save_snapshot(c, "last-year-plu", as_of, {"asOf": as_of, "months": list(ordered.values())})
    return {"asOf": as_of, "months": len(ordered)}


def seed(c):
    """Insert seeded months that are not stored yet. A stored month is left as it is."""
    if not SEED_PATH.is_file():
        return 0
    try:
        ordered = months_in(json.loads(SEED_PATH.read_text()))
    except (OSError, ValueError, json.JSONDecodeError):
        portal.log("last-year-plu seed skipped")
        return 0
    found = {row["month"] for row in portal.rows(c.execute("SELECT month FROM portal_ly_plu"))}
    added = 0
    for key, row in ordered.items():
        if key in found:
            continue
        raw = json.dumps(row, separators=(",", ":"), ensure_ascii=False)
        c.execute(
            "INSERT INTO portal_ly_plu(month,payload,updated_at) VALUES(?,?,?)",
            (key, raw, portal.stamp()),
        )
        added += 1
    if added or not portal.meta_get(c, "last-year-plu"):
        latest = portal.one(c.execute("SELECT MAX(month) AS m FROM portal_ly_plu"))
        if latest and latest["m"]:
            year, month = int(latest["m"][:4]), int(latest["m"][5:])
            _remember(c, portal.month_last_day(date(year, month, 1)).isoformat())
    return added


def load_month(c, month_key):
    row = portal.one(c.execute("SELECT payload FROM portal_ly_plu WHERE month=?", (month_key,)))
    if not row:
        return None
    try:
        data = json.loads(row["payload"])
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _sales_month(c, month_start):
    end = portal.month_last_day(month_start)
    found = portal.sales_between(c, month_start, end)
    if not found:
        return {"rows": [], "days": 0, "total": None, "shop": None, "fuel": None, "span": None}

    def total(key):
        amount = Decimal("0")
        for row in found:
            amount += Decimal(str(row.get(key) or 0))
        return amount.quantize(MONEY, rounding=ROUND_HALF_UP)

    dates = [row["date"] for row in found]
    return {
        "rows": found,
        "days": len(found),
        "total": total("total"),
        "shop": total("shop"),
        "fuel": total("fuel"),
        "span": (min(dates), max(dates)),
    }


def _grades_month(c, month_start):
    end = portal.month_last_day(month_start)
    found = {code: {"litres": Decimal("0"), "sales": Decimal("0"), "litres_seen": False, "sales_seen": False}
             for code, _name in GRADE_ORDER}
    columns = {"ULP": "ulp_l", "DIS": "diesel_l", "P95": "pulp_l"}
    for row in portal.commission_between(c, month_start, end):
        seen = set()
        try:
            detail = json.loads(row.get("detail") or "[]")
        except json.JSONDecodeError:
            detail = []
        if isinstance(detail, list):
            for grade in detail:
                if not isinstance(grade, dict):
                    continue
                code = str(grade.get("grade") or "")
                if code not in found:
                    continue
                seen.add(code)
                if grade.get("litres") is not None:
                    found[code]["litres"] += Decimal(str(grade["litres"]))
                    found[code]["litres_seen"] = True
                if grade.get("salesInc") is not None:
                    found[code]["sales"] += Decimal(str(grade["salesInc"]))
                    found[code]["sales_seen"] = True
        for code, column in columns.items():
            if code in seen or row.get(column) is None:
                continue
            found[code]["litres"] += Decimal(str(row[column]))
            found[code]["litres_seen"] = True
    out = {}
    for code, bucket in found.items():
        out[code] = {
            "litres": bucket["litres"].quantize(MONEY, rounding=ROUND_HALF_UP) if bucket["litres_seen"] else None,
            "sales": bucket["sales"].quantize(MONEY, rounding=ROUND_HALF_UP) if bucket["sales_seen"] else None,
        }
    return out


def _departments_for(c, sales):
    span = sales.get("span")
    if not span:
        return None
    start, end = span
    for period in portal.meta_get(c, "sales").get("periods") or []:
        if period.get("fromDate") != start or period.get("toDate") != end:
            continue
        named = {}
        for raw in period.get("departments") or []:
            if not isinstance(raw, list) or len(raw) < 2 or raw[1] is None:
                continue
            label = str(raw[0] or "").strip()
            if not label or _norm(label) in FUEL_NAMES:
                continue
            named[_norm(label)] = (label, Decimal(str(raw[1])).quantize(MONEY, rounding=ROUND_HALF_UP))
        return named or None
    return None


def _row(key, label, unit, kind, group, this, full, days_this, days_last):
    full_q = None if full is None else Decimal(str(full)).quantize(MONEY, rounding=ROUND_HALF_UP)
    return {
        "key": key,
        "label": label,
        "unit": unit,
        "kind": kind,
        "group": group,
        "this": this,
        "same": prorate(full_q, days_this, days_last),
        "full": full_q,
    }


def build(c, month_start):
    """Figures for one calendar month against the same month last year."""
    ly_key = f"{month_start.year - 1:04d}-{month_start.month:02d}"
    ly = load_month(c, ly_key)
    loaded = bool(ly and _has_figure(ly))
    sales = _sales_month(c, month_start)
    grades = _grades_month(c, month_start)
    departments = _departments_for(c, sales)
    days_this = sales["days"]
    days_last = None
    if loaded:
        days_last = ly.get("days") or portal.month_last_day(date(month_start.year - 1, month_start.month, 1)).day
    ly_grades = {}
    ly_depts = {}
    if loaded:
        for grade in ly.get("fuelGrades") or []:
            if isinstance(grade, dict) and grade.get("grade"):
                ly_grades[str(grade["grade"])] = grade
        for dept in ly.get("departments") or []:
            if not isinstance(dept, dict) or dept.get("fuel"):
                continue
            name = dept.get("name") or ""
            if _norm(name) in FUEL_NAMES or dept.get("salesInc") is None:
                continue
            ly_depts[_norm(name)] = dept
    rows = [
        _row("total", "Total", "inc GST", "money", "", sales["total"], ly.get("totalInc") if loaded else None, days_this, days_last),
        _row("fuel", "Fuel", "inc GST", "money", "", sales["fuel"], ly.get("fuelInc") if loaded else None, days_this, days_last),
        _row("litres", "Fuel litres", "", "litres", "", None, ly.get("fuelLitres") if loaded else None, days_this, days_last),
        _row("shop", "Shop", "inc GST", "money", "", sales["shop"], ly.get("shopInc") if loaded else None, days_this, days_last),
    ]
    # Fuel litres this year are the grade sum when the commission file has them, else blank.
    litre_values = [item["litres"] for item in grades.values() if item["litres"] is not None]
    if litre_values:
        rows[2]["this"] = sum(litre_values, Decimal("0")).quantize(MONEY, rounding=ROUND_HALF_UP)
    for code, fallback in GRADE_ORDER:
        last = ly_grades.get(code) or {}
        current = grades.get(code) or {}
        name = last.get("name") or fallback
        if not last and current.get("sales") is None and current.get("litres") is None:
            continue
        rows.append(_row(_slug(code), name, "inc GST", "money", "grades", current.get("sales"), last.get("salesInc"), days_this, days_last))
        rows.append(_row(_slug(code) + "-l", name + " litres", "", "litres", "grades", current.get("litres"), last.get("litres"), days_this, days_last))
    matched = []
    if loaded and departments:
        for key, (label, amount) in departments.items():
            last = ly_depts.get(key)
            if not last:
                continue
            matched.append(_row("dept-" + _slug(label), label, "inc GST", "money", "departments", amount, last.get("salesInc"), days_this, days_last))
        matched.sort(key=lambda item: item["this"] or Decimal("0"), reverse=True)
        rows.extend(matched)
    return {
        "monthKey": f"{month_start.year:04d}-{month_start.month:02d}",
        "label": month_start.strftime("%B %Y"),
        "lyKey": ly_key,
        "lyLabel": (ly or {}).get("label") or date(month_start.year - 1, month_start.month, 1).strftime("%B %Y"),
        "loaded": loaded,
        "daysThis": days_this,
        "daysLast": days_last,
        "source": (ly or {}).get("source") or "",
        "note": (ly or {}).get("note") or "",
        "tradingWindow": (ly or {}).get("tradingWindow") or "",
        "departmentsReady": departments is not None,
        "departmentCount": len(matched),
        "rows": rows,
    }


def _shown(value, kind):
    if kind == "litres":
        return portal.litres(value)
    return portal.money(value)


def _change_html(this, base, kind):
    if this is None or base is None:
        return ""
    diff = (Decimal(str(this)) - Decimal(str(base))).quantize(MONEY, rounding=ROUND_HALF_UP)
    shown = _shown(diff, kind)
    if Decimal(str(base)) == 0:
        cls = "up" if diff >= 0 else "down"
        return f'<span class="trend {cls}">{shown}</span>'
    pct = ((Decimal(str(this)) - Decimal(str(base))) / Decimal(str(base)) * Decimal(100)).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
    if diff > 0:
        word, cls = "above", "up"
    elif diff < 0:
        word, cls = "below", "down"
    else:
        word, cls = "same", "up"
    return f'<span class="trend {cls}">{shown} · {format(abs(pct), "f")}% {word}</span>'


def _cell(value, kind, blank):
    if value is None:
        return f'<b class="ly-missing">{portal.h(blank)}</b>'
    return f"<b>{_shown(value, kind)}</b>"


def _blank_this(report):
    return "No sales loaded" if not report["daysThis"] else "—"


def _unit(row):
    if not row["unit"]:
        return ""
    return f'<p class="mini">{portal.h(row["unit"])}</p>'


def _cards(report):
    blank_this = _blank_this(report)
    cards = []
    for row in report["rows"]:
        if row["group"]:
            continue
        cards.append(
            f'<article class="compare-card" id="yc-{portal.h(row["key"])}">'
            f'<h3>{portal.h(row["label"])}</h3>{_unit(row)}'
            f'<dl><div><dt>This month</dt><dd>{_cell(row["this"], row["kind"], blank_this)}</dd></div>'
            f'<div id="yc-{portal.h(row["key"])}-same"><dt>Same days last year</dt>'
            f'<dd>{_cell(row["same"], row["kind"], "—")}{_change_html(row["this"], row["same"], row["kind"])}</dd></div>'
            f'<div id="yc-{portal.h(row["key"])}-full"><dt>Full {portal.h(report["lyLabel"])}</dt>'
            f'<dd>{_cell(row["full"], row["kind"], "—")}{_change_html(row["this"], row["full"], row["kind"])}</dd></div>'
            f"</dl></article>"
        )
    return "".join(cards)


def _table(report):
    blank_this = _blank_this(report)
    body = []
    group = None
    titles = {"grades": "Fuel grades", "departments": "Shop departments"}
    for row in report["rows"]:
        if row["group"] != group:
            group = row["group"]
            if group in titles:
                body.append(f'<tr class="compare-group"><td colspan="6">{titles[group]}</td></tr>')
        body.append(
            f'<tr id="yc-{portal.h(row["key"])}-row"><td>{portal.h(row["label"])}'
            f'{_unit(row)}</td>'
            f'<td class="num">{_cell(row["this"], row["kind"], blank_this)}</td>'
            f'<td class="num">{_cell(row["same"], row["kind"], "—")}</td>'
            f'<td class="num">{_change_html(row["this"], row["same"], row["kind"]) or "—"}</td>'
            f'<td class="num">{_cell(row["full"], row["kind"], "—")}</td>'
            f'<td class="num">{_change_html(row["this"], row["full"], row["kind"]) or "—"}</td></tr>'
        )
    return (
        '<div class="compare-wrap"><table class="compare-table"><caption class="mini">This month, the same number of days last year as a daily average, and the full month last year.</caption>'
        "<thead><tr><th></th><th>This month</th><th>Same days last year</th><th>Difference</th>"
        f"<th>Full {portal.h(report['lyLabel'])}</th><th>vs full month</th></tr></thead><tbody>"
        + "".join(body) + "</tbody></table></div>"
    )


def _mobile_cards(report):
    blank_this = _blank_this(report)
    blocks = []
    group = None
    titles = {"grades": "Fuel grades", "departments": "Shop departments"}
    for row in report["rows"]:
        if not row["group"]:
            continue
        if row["group"] != group:
            group = row["group"]
            blocks.append(f'<h3 class="compare-kicker">{titles[group]}</h3>')
        blocks.append(
            f'<article class="compare-card" id="yc-{portal.h(row["key"])}">'
            f'<h3>{portal.h(row["label"])}</h3>{_unit(row)}<dl>'
            f'<div><dt>This month</dt><dd>{_cell(row["this"], row["kind"], blank_this)}</dd></div>'
            f'<div><dt>Same days last year</dt><dd>{_cell(row["same"], row["kind"], "—")}'
            f'{_change_html(row["this"], row["same"], row["kind"])}</dd></div>'
            f'<div><dt>Full {portal.h(report["lyLabel"])}</dt><dd>{_cell(row["full"], row["kind"], "—")}'
            f'{_change_html(row["this"], row["full"], row["kind"])}</dd></div>'
            f"</dl></article>"
        )
    return "".join(blocks)


def _basis(report):
    if not report["loaded"]:
        return (
            f'<p class="empty" id="year-compare-empty">Last year not loaded yet for {portal.h(report["lyLabel"])}. '
            "Add that month from the SwiftPOS PLU Sales Total report. It is not shown as zero.</p>"
        )
    days_this = report["daysThis"]
    days_last = report["daysLast"]
    if not days_this:
        text = (
            f"No sales are loaded for {report['label']}, so there is no same-days comparison. "
            f"Full {report['lyLabel']} is the whole month. Same days would be a daily average of that monthly total."
        )
    elif days_this == days_last:
        text = (
            f"Same days last year is a daily average of the monthly total, and it matches the full month "
            f"because all {days_last} days are loaded. {report['lyLabel']} is not a day-by-day file."
        )
    else:
        text = (
            f"Same days last year is a daily average. {report['lyLabel']} is one monthly total, not a day-by-day file. "
            f"{days_this} loaded day{'s' if days_this != 1 else ''} this month, of {days_last} days in {report['lyLabel']}."
        )
    window = f" Trading window {report['tradingWindow']}." if report["tradingWindow"] else ""
    return f'<p class="help" id="year-compare-basis">{portal.h(text)}{portal.h(window)} Till sales, inc GST. Online orders are not included.</p>'


def _department_note(report):
    if not report["loaded"]:
        return ""
    if report["departmentCount"]:
        return (
            '<p class="help" id="year-compare-depts">Shop departments are listed where this month’s sales file uses the same name. '
            "A department that is in only one file is left out. It is not shown as zero.</p>"
        )
    if report["departmentsReady"]:
        return '<p class="help" id="year-compare-depts">No shop department in this month’s sales file uses the same name as last year.</p>'
    if report["daysThis"]:
        return '<p class="help" id="year-compare-depts">Shop departments for this month are not in the loaded sales file, so they are not compared.</p>'
    return ""


def _month_form(active, start, end, month_key):
    hidden = f'<input type="hidden" name="period" value="{portal.h(active)}">'
    if active == "custom":
        hidden += (
            f'<input type="hidden" name="from" value="{portal.h(start.isoformat())}">'
            f'<input type="hidden" name="to" value="{portal.h(end.isoformat())}">'
        )
    return (
        f'<form class="period" method="get" action="/portal" id="year-compare-form">{hidden}'
        f'<div class="row"><label class="f">Month'
        f'<input type="month" name="compare" id="year-compare-month" value="{portal.h(month_key)}" required>'
        f'</label><button class="btn" type="submit">Show month</button></div></form>'
    )


def chosen_month(active_start, query):
    raw = (query.get("compare") or "").strip()
    if re.fullmatch(r"\d{4}-\d{2}", raw):
        year, month = int(raw[:4]), int(raw[5:])
        if 1990 <= year <= 2100 and 1 <= month <= 12:
            return date(year, month, 1)
    return active_start


def section(c, active, start, end, query):
    month_start = chosen_month(portal.viewed_month(start, end), query)
    report = build(c, month_start)
    source = ""
    if report["loaded"] and (report["source"] or report["note"]):
        bits = " ".join(part for part in (report["source"], report["note"]) if part)
        source = f'<p class="help" id="year-compare-source">{portal.h(bits)}</p>'
    return f"""<section class="card" id="year-compare" data-month="{portal.h(report["monthKey"])}" data-loaded="{"1" if report["loaded"] else "0"}">
<div class="lbl">Last year</div>
<h2>{portal.h(report["label"])} vs {portal.h(report["lyLabel"])}</h2>
<p class="help">This block follows the month chosen here. The other cards follow the date range above.</p>
{_month_form(active, start, end, report["monthKey"])}
{_basis(report)}
<div class="compare-cards">{_cards(report)}</div>
{_table(report)}
<div class="compare-cards compare-extra">{_mobile_cards(report)}</div>
{_department_note(report)}
{source}
</section>"""
