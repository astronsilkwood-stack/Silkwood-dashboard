"""Sales-dashboard pages and 2026 gross-profit cost overrides.

Cost contract: the nightly `sales` figures stay on till cost. This module
applies dataset `cost-overrides` when a page shows 2026 GP, GP% or cost
coverage. An override replaces the till unit cost on sale dates on or after
`effectiveFrom`. The 2025 comparison never uses cost or GP.

Do not pre-adjust GP in the sales upload. That would apply the override twice.
"""
import json
import re
from datetime import date, timedelta
from urllib.parse import quote
from decimal import Decimal, ROUND_HALF_UP

import portal

SHOP_EXCLUDED = {"A Fuel", "System", "Surcharge"}
FORBIDDEN_LY = {
    "gp", "gppct", "gp_pct", "cost", "costex", "cost_ex", "coverage", "missing",
    "missingcost", "margin", "unitcost", "gppctinc", "zerocost",
}
OVERRIDE_SOURCES = {"invoice", "owner"}


def save_blob(c, dataset, as_of, payload):
    raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    c.execute(
        """INSERT INTO portal_blobs(dataset,as_of,payload,received_at) VALUES(?,?,?,?)
           ON CONFLICT(dataset) DO UPDATE SET as_of=excluded.as_of, payload=excluded.payload, received_at=excluded.received_at""",
        (dataset, as_of, raw, portal.stamp()),
    )


def load_blob(c, dataset):
    row = portal.one(c.execute("SELECT as_of, payload FROM portal_blobs WHERE dataset=?", (dataset,)))
    if not row:
        return None
    try:
        data = json.loads(row["payload"])
    except json.JSONDecodeError:
        return None
    if isinstance(data, dict):
        data["_asOf"] = row["as_of"]
    return data


def _text_card(value, where):
    if isinstance(value, str) and portal.has_card_number(value):
        raise ValueError(f"{where} must not include a card number")


def reject_last_year_metrics(node, where="last-year"):
    if isinstance(node, dict):
        for key, value in node.items():
            if portal._key_name(key) in FORBIDDEN_LY:
                raise ValueError(f"{where} must not include cost, GP, coverage, or a missing-cost field")
            reject_last_year_metrics(value, where)
    elif isinstance(node, list):
        for value in node:
            reject_last_year_metrics(value, where)


def _dec_token(raw):
    if raw is None or raw == "":
        return None
    if raw == "!":
        return "MISSING"
    if raw.startswith("_"):
        return float(raw[1:])
    sign = 1
    if raw.startswith("-"):
        sign = -1
        raw = raw[1:]
    if raw == "":
        return 0
    return sign * int(raw, 36)


def unpack_days(pack):
    """Product-search packed daily: dayIndex:qty,salesDelta[,unused],costDelta.

    `!` in the cost field means that day has no till cost. Sales deltas are
    base-36 cents. A leading underscore marks a plain decimal quantity (fuel litres).
    """
    out = {}
    for part in (pack or "").split(";"):
        if not part or ":" not in part:
            continue
        idx, rest = part.split(":", 1)
        try:
            day_i = int(idx, 36)
        except ValueError:
            continue
        fields = rest.split(",")
        qty = _dec_token(fields[0]) if fields else None
        if qty is None or qty == "MISSING":
            qty = 1
        sales_delta = _dec_token(fields[1]) if len(fields) > 1 else 0
        if sales_delta is None or sales_delta == "MISSING":
            sales_delta = 0
        if len(fields) >= 4:
            cost_delta = _dec_token(fields[3])
        elif len(fields) >= 3:
            cost_delta = _dec_token(fields[2])
        else:
            cost_delta = 0
        if cost_delta is None:
            cost_delta = 0
        out[day_i] = (qty, sales_delta, cost_delta)
    return out


def override_table(c):
    rows = portal.rows(c.execute(
        "SELECT code, effective_from, cost_ex_cents, source FROM portal_cost_overrides ORDER BY code, effective_from"
    ))
    by_code = {}
    for row in rows:
        by_code.setdefault(row["code"], []).append(row)
    return by_code


def override_on(table, code, sale_date):
    chosen = None
    for row in table.get(str(code), []):
        if row["effective_from"] <= sale_date:
            chosen = row
        else:
            break
    return chosen


def _money_cents(cents):
    return float((Decimal(str(cents)) / Decimal(100)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def line_economics(qty, sell_cents, sales_delta, till_cents, cost_delta, gst, override_cents):
    sales_inc = Decimal(str(qty)) * Decimal(int(sell_cents or 0)) + Decimal(int(sales_delta or 0))
    sales_ex = sales_inc if not gst else (sales_inc / Decimal("1.1"))
    missing_till = cost_delta == "MISSING" or ((till_cents or 0) == 0 and cost_delta == 0)
    used = False
    if override_cents is not None:
        cost = Decimal(str(qty)) * Decimal(int(override_cents))
        used = True
        covered = True
    elif missing_till:
        cost = Decimal(0)
        covered = False
    else:
        extra = 0 if cost_delta == "MISSING" else int(cost_delta or 0)
        cost = Decimal(str(qty)) * Decimal(int(till_cents or 0)) + Decimal(extra)
        covered = True
    gp = (sales_ex - cost) if covered else Decimal(0)
    return {
        "qty": qty,
        "sales_inc": sales_inc,
        "sales_ex": sales_ex,
        "cost": cost,
        "gp": gp,
        "covered": covered,
        "override": used,
    }


def _walk_pack(product, dates, start, end, table, use_override):
    code, _name, _ref, _bc, sell, till, flag, pack = product[:8]
    parsed = unpack_days(pack)
    gst = flag == 1
    rows = []
    for day_i, rec in parsed.items():
        if day_i < 0 or day_i >= len(dates):
            continue
        iso = dates[day_i]
        if start and iso < start:
            continue
        if end and iso > end:
            continue
        qty, sales_delta, cost_delta = rec
        ov = override_on(table, code, iso) if use_override else None
        econ = line_economics(qty, sell, sales_delta, till, cost_delta, gst, ov["cost_ex_cents"] if ov else None)
        till_econ = line_economics(qty, sell, sales_delta, till, cost_delta, gst, None)
        econ["date"] = iso
        econ["source"] = ov["source"] if ov else None
        econ["unit_cents"] = ov["cost_ex_cents"] if ov else (None if not till_econ["covered"] else till)
        econ["till_gp"] = till_econ["gp"]
        econ["till_covered"] = till_econ["covered"]
        econ["till_cost"] = till_econ["cost"]
        rows.append(econ)
    return rows


def sum_rows(rows):
    qty = Decimal(0)
    sales_inc = Decimal(0)
    sales_ex = Decimal(0)
    gp = Decimal(0)
    covered_ex = Decimal(0)
    days = 0
    override_days = 0
    source = None
    for row in rows:
        days += 1
        qty += Decimal(str(row["qty"]))
        sales_inc += row["sales_inc"]
        sales_ex += row["sales_ex"]
        if row["covered"]:
            gp += row["gp"]
            covered_ex += row["sales_ex"]
        if row["override"]:
            override_days += 1
            source = row["source"] or source
    return {
        "qty": qty,
        "sales_inc": sales_inc,
        "sales_ex": sales_ex,
        "gp": gp,
        "covered_ex": covered_ex,
        "days": days,
        "override_days": override_days,
        "source": source,
        "gp_pct": (gp / covered_ex) if covered_ex else None,
        "coverage": (covered_ex / sales_ex) if sales_ex else None,
    }


def _dollars(value):
    return float(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def product_archive(c):
    data = load_blob(c, "product-search")
    if not data or not isinstance(data.get("dates"), list) or not isinstance(data.get("products"), list):
        return None
    return data


def dept_of(archive, product):
    refs = archive.get("refs") or []
    ref = product[2] if len(product) > 2 else None
    if isinstance(ref, int) and 0 <= ref < len(refs) and isinstance(refs[ref], list) and refs[ref]:
        return str(refs[ref][0])
    return "Unmapped"


def cost_deltas(c, start, end):
    """Per product, the GP change from till cost to the override cost inside the dates."""
    archive = product_archive(c)
    table = override_table(c)
    if not archive or not table:
        return []
    start_s = start.isoformat() if isinstance(start, date) else start
    end_s = end.isoformat() if isinstance(end, date) else end
    found = []
    dates = archive["dates"]
    for product in archive["products"]:
        if not isinstance(product, list) or len(product) < 8:
            continue
        code = str(product[0])
        if code not in table:
            continue
        rows = _walk_pack(product, dates, start_s, end_s, table, True)
        if not any(row["override"] for row in rows):
            continue
        gp_delta = Decimal(0)
        covered_delta = Decimal(0)
        missing_fixed = False
        was_missing = False
        for row in rows:
            if row["override"]:
                old_gp = row["till_gp"] if row["till_covered"] else Decimal(0)
                gp_delta += row["gp"] - old_gp
                if row["covered"] and not row["till_covered"]:
                    covered_delta += row["sales_ex"]
                    missing_fixed = True
            elif not row["till_covered"]:
                was_missing = True
        found.append({
            "code": code,
            "name": str(product[1]),
            "department": dept_of(archive, product),
            "gp_delta": gp_delta,
            "covered_delta": covered_delta,
            "source": next((row["source"] for row in rows if row["override"]), None),
            "shop": dept_of(archive, product) not in SHOP_EXCLUDED,
            "missing_fixed": missing_fixed and not was_missing,
        })
    return found


def adjust_sales_days(c, days):
    """Return sales-day copies whose shop GP includes cost overrides."""
    if not days:
        return days, []
    days = sorted(days, key=lambda row: row["date"])
    start, end = days[0]["date"], days[-1]["date"]
    deltas = [d for d in cost_deltas(c, start, end) if d["shop"]]
    by_date = {}
    archive = product_archive(c)
    table = override_table(c)
    if archive and deltas:
        wanted = {d["code"] for d in deltas}
        for product in archive["products"]:
            if str(product[0]) not in wanted:
                continue
            if dept_of(archive, product) in SHOP_EXCLUDED:
                continue
            for row in _walk_pack(product, archive["dates"], start, end, table, True):
                if not row["override"]:
                    continue
                bucket = by_date.setdefault(row["date"], {"gp": Decimal(0), "covered": Decimal(0), "fixed": 0})
                old_gp = row["till_gp"] if row["till_covered"] else Decimal(0)
                bucket["gp"] += row["gp"] - old_gp
                if row["covered"] and not row["till_covered"]:
                    bucket["covered"] += row["sales_ex"]
                    bucket["fixed"] += 1
    adjusted = []
    for day in days:
        copy = dict(day)
        shop_ex = None
        if day.get("coverage") and day.get("covered_ex"):
            shop_ex = Decimal(str(day["covered_ex"])) / Decimal(str(day["coverage"]))
        hit = by_date.get(day["date"])
        if hit:
            copy["gp"] = float(Decimal(str(day.get("gp") or 0)) + hit["gp"] / Decimal(100))
            copy["covered_ex"] = float(Decimal(str(day.get("covered_ex") or 0)) + hit["covered"] / Decimal(100))
            if day.get("missing") is not None:
                copy["missing"] = max(0, int(day["missing"]) - hit["fixed"])
            if shop_ex:
                copy["coverage"] = float(Decimal(str(copy["covered_ex"])) / shop_ex)
            if copy.get("covered_ex"):
                copy["gp_pct"] = copy["gp"] / copy["covered_ex"]
        copy["_shop_ex"] = float(shop_ex) if shop_ex is not None else None
        adjusted.append(copy)
    return adjusted, deltas


def matching_period(c, active, start, end):
    periods = (portal.meta_get(c, "sales").get("periods") or [])
    for period in periods:
        if period.get("fromDate") == start.isoformat() and period.get("toDate") == end.isoformat():
            return period
    for period in periods:
        if period.get("id") == active:
            return period
    return None


def product_names(c):
    return portal.meta_get(c, "sales").get("productNames") or {}


def _prev_bounds(active, start, end):
    if active == "week":
        return start - timedelta(days=7), end - timedelta(days=7)
    if active == "month":
        prev_last = start.replace(day=1) - timedelta(days=1)
        day_n = min(end.day, prev_last.day)
        return prev_last.replace(day=1), prev_last.replace(day=day_n)
    span = (end - start).days
    prev_end = start - timedelta(days=1)
    return prev_end - timedelta(days=span), prev_end


def _fmt_qty(qty, litres=False):
    if litres:
        return portal.litres(qty)
    q = Decimal(str(qty)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if q == q.to_integral():
        return f"{int(q):,}"
    return f"{q:,.2f}"


# --- ingest ---

def apply_cost_overrides(c, data):
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        raise ValueError("cost-overrides needs an items list")
    as_of = portal.need_date(data.get("asOf") or portal.today().isoformat(), "asOf")
    c.execute("DELETE FROM portal_cost_overrides")
    seen = set()
    for i, item in enumerate(data["items"]):
        if not isinstance(item, dict):
            raise ValueError(f"items[{i}] must be an object")
        extra = [k for k in item if k not in ("code", "costExCents", "effectiveFrom", "source")]
        if extra:
            raise ValueError("cost-overrides items only allow code, costExCents, effectiveFrom, and source")
        code = item.get("code")
        if isinstance(code, bool):
            raise ValueError(f"items[{i}] code is invalid")
        if isinstance(code, int):
            code = str(code)
        if not isinstance(code, str) or not code.strip() or len(code.strip()) > 40:
            raise ValueError(f"items[{i}] needs a PLU code")
        code = code.strip()
        _text_card(code, "cost-overrides")
        cents = item.get("costExCents")
        if isinstance(cents, bool) or not isinstance(cents, int) or cents < 0 or cents > 10_000_000:
            raise ValueError(f"items[{i}] costExCents must be a whole number of cents")
        effective = portal.need_date(item.get("effectiveFrom"), "effectiveFrom")
        source = item.get("source")
        if source not in OVERRIDE_SOURCES:
            raise ValueError(f"items[{i}] source must be invoice or owner")
        c.execute(
            """INSERT INTO portal_cost_overrides(code,effective_from,cost_ex_cents,source,as_of)
               VALUES(?,?,?,?,?)
               ON CONFLICT(code,effective_from) DO UPDATE SET cost_ex_cents=excluded.cost_ex_cents,
                 source=excluded.source, as_of=excluded.as_of""",
            (code, effective, cents, source, as_of),
        )
        seen.add((code, effective))
    portal.meta_set(c, "cost-overrides", {"asOf": as_of, "items": len(seen)})
    save_blob(c, "cost-overrides", as_of, {"asOf": as_of, "items": data["items"]})
    return {"asOf": as_of, "items": len(seen)}


def apply_product_search(c, data):
    if not isinstance(data, dict):
        raise ValueError("product-search must be an object")
    dates = data.get("dates")
    products = data.get("products")
    if not isinstance(dates, list) or not dates or not isinstance(products, list):
        raise ValueError("product-search needs dates and products")
    if len(products) > 20000:
        raise ValueError("product-search has too many products")
    clean_dates = [portal.need_date(d, "product date") for d in dates]
    for i, row in enumerate(products):
        if not isinstance(row, list) or len(row) < 8:
            raise ValueError(f"products[{i}] must be [code, name, ref, barcode, sellCents, costExCents, flag, packed]")
        _text_card(str(row[1]), "product-search")
        if row[3]:
            _text_card(str(row[1]), "product-search")
    as_of = clean_dates[-1]
    payload = {"dates": clean_dates, "refs": data.get("refs") if isinstance(data.get("refs"), list) else [], "products": products}
    if isinstance(data.get("older"), dict):
        reject_last_year_metrics(data["older"], "product-search older")
        payload["older"] = data["older"]
    save_blob(c, "product-search", as_of, payload)
    portal.meta_set(c, "product-search", {"asOf": as_of, "products": len(products), "from": clean_dates[0], "to": clean_dates[-1]})
    return {"asOf": as_of, "products": len(products)}


def apply_retail_pricing(c, data):
    if not isinstance(data, dict) or not isinstance(data.get("rows"), list):
        raise ValueError("retail-pricing needs rows")
    as_of = portal.need_date(data.get("asOf") or portal.today().isoformat(), "asOf")
    for i, row in enumerate(data["rows"]):
        if not isinstance(row, list):
            raise ValueError(f"rows[{i}] must be a list")
        if len(row) > 1:
            _text_card(str(row[1]), "retail-pricing")
    save_blob(c, "retail-pricing", as_of, data)
    portal.meta_set(c, "retail-pricing", {"asOf": as_of, "rows": len(data["rows"])})
    return {"asOf": as_of, "rows": len(data["rows"])}


def apply_till_prices(c, data):
    if not isinstance(data, dict):
        raise ValueError("till-price-changes must be an object")
    rows = data.get("rows")
    if not isinstance(rows, list):
        raise ValueError("till-price-changes needs rows")
    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ValueError(f"rows[{i}] must be an object")
        _text_card(str(row.get("name") or ""), "till-price-changes")
        _text_card(str(row.get("reason") or ""), "till-price-changes")
    no_cost = data.get("noCost") if isinstance(data.get("noCost"), dict) else {"columns": [], "rows": []}
    new_items = data.get("newItems") if isinstance(data.get("newItems"), dict) else {"columns": [], "rows": []}
    for row in no_cost.get("rows") or []:
        if isinstance(row, list) and len(row) > 2:
            _text_card(str(row[2]), "till-price-changes")
    for row in new_items.get("rows") or []:
        if isinstance(row, list):
            _text_card(" ".join(str(x) for x in row if isinstance(x, str) and not str(x).isdigit()), "till-price-changes")
    as_of = portal.need_date(data.get("asOf") or portal.today().isoformat(), "asOf")
    save_blob(c, "till-price-changes", as_of, data)
    portal.meta_set(c, "till-price-changes", {"asOf": as_of, "rows": len(rows)})
    return {"asOf": as_of, "rows": len(rows)}


def apply_shifts(c, data):
    if not isinstance(data, dict) or not isinstance(data.get("days"), list) or not data["days"]:
        raise ValueError("shifts needs a days list")
    names = data.get("mediaNames")
    if not isinstance(names, list) or not names:
        raise ValueError("shifts needs mediaNames")
    for name in names:
        _text_card(str(name), "shifts")
    basis = str(data.get("differenceBasis") or "")[:300]
    _text_card(basis, "shifts")
    other = [str(x)[:40] for x in data.get("otherTenders") or [] if isinstance(x, str)]
    n = 0
    dates = []
    for i, day in enumerate(data["days"]):
        if not isinstance(day, dict):
            raise ValueError("shift day must be an object")
        d = portal.need_date(day.get("date"), "shift day")
        media = []
        for j, row in enumerate(day.get("media") or []):
            if not isinstance(row, list) or len(row) < 4:
                raise ValueError(f"days[{i}].media[{j}] is too short")
            idx = row[0]
            if isinstance(idx, bool) or not isinstance(idx, int) or not (0 <= idx < len(names)):
                raise ValueError(f"days[{i}].media[{j}] has a bad media index")
            media.append({
                "name": str(names[idx])[:40],
                "expected": portal.num(row[1], "expected"),
                "counted": portal.num(row[2], "counted"),
                "difference": portal.num(row[3], "difference"),
                "count": portal.whole(row[4], "count") if len(row) > 4 else None,
            })
        payload = {
            "date": d,
            "txnNo": str(day.get("txnNo") or "")[:40],
            "dayId": str(day.get("dayId") or "")[:40],
            "clerk": str(day.get("clerk") or "")[:40],
            "windowFrom": str(day.get("windowFrom") or "")[:40],
            "windowTo": str(day.get("windowTo") or "")[:40],
            "committed": str(day.get("committed") or "")[:40],
            "media": media,
            "otherTenders": other,
            "differenceBasis": basis,
        }
        _text_card(payload["clerk"], "shifts")
        c.execute(
            """INSERT INTO portal_shift_days(date,payload,source_as_of) VALUES(?,?,?)
               ON CONFLICT(date) DO UPDATE SET payload=excluded.payload, source_as_of=excluded.source_as_of""",
            (d, json.dumps(payload, separators=(",", ":"), ensure_ascii=False), d),
        )
        dates.append(d)
        n += 1
    as_of = max(dates)
    portal.meta_set(c, "shifts", {
        "asOf": as_of,
        "days": n,
        "basis": basis,
        "otherTenders": other,
        "olderMonths": data.get("olderMonths") if isinstance(data.get("olderMonths"), list) else [],
    })
    return {"asOf": as_of, "days": n}


def _ly_num(value, label, required=False):
    if isinstance(value, dict):
        if value.get("total") is not None:
            return portal.num(value.get("total"), label, required=required)
        total = 0.0
        for key, item in value.items():
            if key == "total":
                continue
            total += portal.num(item, key) or 0
        return total
    return portal.num(value, label, required=required)


def _ly_groups(rows):
    out = []
    for i, row in enumerate(rows or []):
        if isinstance(row, dict):
            name = str(row.get("name") or "")[:80]
            qty = portal.num(row.get("qty"), "qty", required=False) or 0
            sales = portal.num(row.get("salesEx"), "salesEx", required=False) or 0
        elif isinstance(row, list) and len(row) >= 3:
            name = str(row[0])[:80]
            qty = portal.num(row[1], "qty", required=False) or 0
            sales = portal.num(row[2], "salesEx", required=False) or 0
        else:
            raise ValueError(f"groups[{i}] needs a name, quantity, and sales ex GST")
        _text_card(name, "last-year")
        out.append({"name": name, "qty": qty, "salesEx": sales})
    return out


def _ly_products(rows):
    out = []
    for i, row in enumerate(rows or []):
        if isinstance(row, dict):
            item = {
                "code": str(row.get("code") or "")[:40],
                "name": str(row.get("name") or "")[:120],
                "group": str(row.get("group") or "")[:80],
                "qty": portal.num(row.get("qty"), "qty", required=False) or 0,
                "salesEx": portal.num(row.get("salesEx"), "salesEx", required=False) or 0,
            }
        elif isinstance(row, list) and len(row) >= 3:
            if len(row) >= 5:
                item = {"code": str(row[0])[:40], "name": str(row[1])[:120], "group": str(row[2])[:80],
                        "qty": portal.num(row[3], "qty") or 0, "salesEx": portal.num(row[4], "salesEx") or 0}
            else:
                item = {"code": str(row[0])[:40], "name": "", "group": "",
                        "qty": portal.num(row[1], "qty") or 0, "salesEx": portal.num(row[2], "salesEx") or 0}
        else:
            raise ValueError(f"products[{i}] needs code, quantity, and sales ex GST")
        _text_card(item["name"], "last-year")
        out.append(item)
    return out


def _normalise_month(row):
    reject_last_year_metrics(row)
    start = row.get("fromDate")
    end = row.get("toDate")
    if start:
        start = portal.need_date(start, "fromDate")
    if end:
        end = portal.need_date(end, "toDate")
    year = row.get("year")
    month = row.get("month")
    if year is None and start:
        year = int(start[:4])
    if month is None and start:
        month = int(start[5:7])
    if isinstance(year, bool) or not isinstance(year, int) or isinstance(month, bool) or not isinstance(month, int):
        raise ValueError("last-year needs a year and month")
    grades = _ly_products(row.get("fuelGrades"))
    return {
        "label": str(row.get("label") or f"{date(year, month, 1).strftime('%B %Y')}")[:80],
        "year": year,
        "month": month,
        "fromDate": start,
        "toDate": end,
        "days": portal.whole(row.get("days"), "days") if row.get("days") is not None else None,
        "source": str(row.get("source") or "")[:300],
        "fuelLitres": _ly_num(row.get("fuelLitres"), "fuelLitres", required=False),
        "fuelEx": portal.num(row.get("fuelEx"), "fuelEx", required=False),
        "fuelGrades": grades,
        "groups": _ly_groups(row.get("groups")),
        "products": _ly_products(row.get("products")),
    }


def apply_last_year(c, data):
    if isinstance(data, dict) and isinstance(data.get("months"), list):
        months = [_normalise_month(row) for row in data["months"] if isinstance(row, dict)]
    elif isinstance(data, dict):
        months = [_normalise_month(data)]
    else:
        raise ValueError("last-year must be an object")
    if not months:
        raise ValueError("last-year needs a month")
    as_of = months[-1].get("toDate") or f"{months[-1]['year']:04d}-{months[-1]['month']:02d}-01"
    save_blob(c, "last-year", as_of, {"months": months})
    portal.meta_set(c, "last-year", {"asOf": as_of, "months": len(months)})
    return {"asOf": as_of, "months": len(months), "products": sum(len(m["products"]) for m in months)}


def store_sales_extras(c, data):
    meta = portal.meta_get(c, "sales")
    periods_out = []
    has_analytics = False
    for period in data.get("periods") or []:
        if not isinstance(period, dict):
            continue
        if any(period.get(key) for key in ("weekdays", "departments", "top", "low")):
            has_analytics = True
        summary = period.get("summary") if isinstance(period.get("summary"), dict) else None
        periods_out.append({
            "id": str(period.get("id") or "")[:40],
            "label": str(period.get("label") or "")[:40],
            "caption": str(period.get("caption") or "")[:200],
            "fromDate": period.get("fromDate"),
            "toDate": period.get("toDate"),
            "summary": summary,
            "weekdays": period.get("weekdays") if isinstance(period.get("weekdays"), list) else [],
            "departments": period.get("departments") if isinstance(period.get("departments"), list) else [],
            "top": period.get("top") if isinstance(period.get("top"), list) else [],
            "low": period.get("low") if isinstance(period.get("low"), list) else [],
        })
    if has_analytics:
        meta["periods"] = periods_out
    if isinstance(data.get("products"), dict):
        names = {}
        for key, value in list(data["products"].items())[:5000]:
            if isinstance(value, str):
                names[str(key)[:40]] = value[:120]
        meta["productNames"] = names
    if "kitchen" in data:
        kitchen = data.get("kitchen")
        meta["kitchen"] = kitchen if isinstance(kitchen, dict) else {}
    if isinstance(data.get("window"), dict):
        meta["window"] = {k: data["window"].get(k) for k in ("days", "fromDate", "asOf", "rule")}
    if isinstance(data.get("olderMonths"), list):
        meta["olderMonths"] = data["olderMonths"][:36]
    if isinstance(data.get("commission"), dict):
        comm = data["commission"]
        october = comm.get("october") if isinstance(comm.get("october"), dict) else (meta.get("commission") or {}).get("october")
        if isinstance(october, dict):
            october = {k: october.get(k) for k in ("month", "amount", "target", "days", "complete")}
        def dates_of(key):
            rows = []
            for value in comm.get(key) or []:
                if isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                    rows.append(value)
            return rows[:400]
        meta["commission"] = {
            "month": str(comm.get("month") or "")[:40],
            "fromDate": comm.get("fromDate"),
            "toDate": comm.get("toDate"),
            "days": comm.get("days") if isinstance(comm.get("days"), int) else None,
            "amount": portal.num(comm.get("amount"), "amount", required=False),
            "target": portal.num(comm.get("target"), "target", required=False),
            "progress": portal.num(comm.get("progress"), "progress", required=False),
            "remaining": portal.num(comm.get("remaining"), "remaining", required=False),
            "planDaily": portal.num(comm.get("planDaily"), "planDaily", required=False),
            "neededDaily": portal.num(comm.get("neededDaily"), "neededDaily", required=False),
            "remainingFrom": comm.get("remainingFrom") if isinstance(comm.get("remainingFrom"), str) else None,
            "remainingTo": comm.get("remainingTo") if isinstance(comm.get("remainingTo"), str) else None,
            "daysRemaining": comm.get("daysRemaining") if isinstance(comm.get("daysRemaining"), int) else None,
            "basis": str(comm.get("basis") or "")[:300],
            "missingDates": dates_of("missingDates"),
            "pendingDates": dates_of("pendingDates"),
            "gradeDetailMissingDates": dates_of("gradeDetailMissingDates"),
            "october": october,
        }
    if "lastYear" in data and data.get("lastYear") is not None:
        reject_last_year_metrics(data.get("lastYear"), "sales.lastYear")
        meta["lastYear"] = data.get("lastYear")
    portal.meta_set(c, "sales", meta)


def store_purchase_series(c, data, as_of):
    suppliers = data.get("suppliers") or []
    c.execute("DELETE FROM portal_spend")
    c.execute("DELETE FROM portal_shop_days")
    n = 0
    for i, row in enumerate(data.get("spend") or []):
        if not isinstance(row, list) or len(row) < 4:
            raise ValueError(f"spend[{i}] is too short")
        idx = row[0]
        if isinstance(idx, bool) or not isinstance(idx, int) or not (0 <= idx < len(suppliers)):
            raise ValueError(f"spend[{i}] has a bad supplier index")
        supplier = str(suppliers[idx]).strip()[:120]
        spend_date = portal.need_date(str(row[1]), "spend date")
        bucket = str(row[4]).strip()[:20] if len(row) > 4 and row[4] else "shop"
        key = f"{supplier}\n{spend_date}\n{bucket}\n{i}"
        c.execute(
            "INSERT INTO portal_spend(spend_key,supplier,spend_date,ex_gst,inc_gst,bucket,source_as_of) VALUES(?,?,?,?,?,?,?)",
            (key, supplier, spend_date, portal.num(row[2], "ex GST"), portal.num(row[3], "inc GST"), bucket, as_of),
        )
        n += 1
    for i, row in enumerate(data.get("shop") or []):
        if not isinstance(row, list) or len(row) < 2:
            raise ValueError(f"shop[{i}] is too short")
        d = portal.need_date(str(row[0]), "shop date")
        c.execute(
            """INSERT INTO portal_shop_days(date,sales_inc,source_as_of) VALUES(?,?,?)
               ON CONFLICT(date) DO UPDATE SET sales_inc=excluded.sales_inc, source_as_of=excluded.source_as_of""",
            (d, portal.num(row[1], "shop sales"), as_of),
        )
    return n


# --- shared bits of pages ---

def override_note(deltas):
    if not deltas:
        return ""
    names = []
    for row in deltas[:8]:
        names.append(f"{portal.h(row['name'])} ({portal.h(row['source'] or 'override')})")
    extra = f" and {len(deltas) - 8} more" if len(deltas) > 8 else ""
    return (
        '<p class="callout" id="cost-override-note"><span class="pill">Cost override</span> '
        "Shop GP uses a cost override where one is effective on the sale date, and the till cost otherwise. "
        + ", ".join(names) + extra + ".</p>"
    )


def last_year_months(c):
    data = load_blob(c, "last-year")
    if not data:
        return []
    months = data.get("months")
    return months if isinstance(months, list) else []


def november_2025(c):
    for month in last_year_months(c):
        if month.get("year") == 2025 and month.get("month") == 11:
            return month
    return None


def last_year_section(c, sales_days):
    month = november_2025(c)
    if not month:
        return '<section class="card" id="last-year"><h2>November 2025</h2><p class="help">The product file for November 2025 is not loaded. That file is quantity and sales ex GST. No cost or GP is stored for 2025. Monthly totals for the last year comparison are separate.</p></section>'
    nov = [row for row in sales_days if str(row["date"]).startswith("2026-11")]
    n = len(nov)
    full_days = month.get("days") or 30
    factor = (Decimal(n) / Decimal(full_days)) if n and full_days else None
    fuel_l = month.get("fuelLitres")
    fuel_ex = month.get("fuelEx")
    pro_l = float(Decimal(str(fuel_l)) * factor) if factor is not None and fuel_l is not None else None
    pro_ex = float(Decimal(str(fuel_ex)) * factor) if factor is not None and fuel_ex is not None else None
    grades = []
    for grade in month.get("fuelGrades") or []:
        grades.append(
            f"<tr><td>{portal.h(grade.get('name') or grade.get('code'))}</td>"
            f"<td class=\"num\">{portal.litres(grade.get('qty'))}</td>"
            f"<td class=\"num\">{portal.money(grade.get('salesEx'))} ex GST</td></tr>"
        )
    grade_table = ""
    if grades:
        grade_table = '<div class="tw"><table><thead><tr><th>Grade</th><th>Litres</th><th>Sales ex GST</th></tr></thead><tbody>' + "".join(grades) + "</tbody></table></div>"
    pro = ""
    if factor is not None:
        pro = (
            f"<p id=\"last-year-prorata\">Pro-rated November 2025 for the {n} November 2026 sales day{'s' if n != 1 else ''} loaded so far "
            f"({n} of {full_days}): {portal.litres(pro_l)} and {portal.money(pro_ex)} fuel ex GST. "
            "The pro-rate is a straight share of the full month. It is not a daily 2025 file.</p>"
        )
    return f"""<section class="card" id="last-year"><div class="lbl">2025 · quantity and sales ex GST</div>
<h2>{portal.h(month.get('label') or 'November 2025')}</h2>
<p>Whole month {portal.h(month.get('fromDate') or '')} to {portal.h(month.get('toDate') or '')}. Figures are labelled 2025. They are sales ex GST and litres, set beside November 2026 to date.</p>
<div class="grid g3">
<div class="stat"><div class="lbl">2025 fuel litres</div><b>{portal.litres(fuel_l)}</b><span>full November</span></div>
<div class="stat"><div class="lbl">2025 fuel sales</div><b>{portal.money(fuel_ex)}</b><span>ex GST, full November</span></div>
<div class="stat"><div class="lbl">2026 fuel sales to date</div><b>{portal.money(sum(r.get('fuel') or 0 for r in nov))}</b><span>inc GST, {n} days. Not the same GST basis as 2025.</span></div>
</div>
{pro}
{grade_table}
<p class="help">{portal.h(month.get('source') or '')}</p>
<p class="mini" id="ly-fuel-anchor" data-litres="{portal.h(fuel_l)}"></p>
</section>"""


def last_year_with_litres(c, sales_days, commission_days):
    html = last_year_section(c, sales_days)
    month = november_2025(c)
    if not month:
        return html
    litres = 0.0
    have = False
    for row in commission_days:
        if str(row["date"]).startswith("2026-11"):
            have = True
            litres += (row.get("ulp_l") or 0) + (row.get("diesel_l") or 0) + (row.get("pulp_l") or 0)
    if not have:
        return html
    block = f'<p id="ly-2026-litres">November 2026 fuel to date is {portal.litres(litres)}.</p>'
    return html.replace('<p class="help">', block + '<p class="help">', 1)


# --- pages ---

def _selected_archive_span(archive, start, end):
    dates = archive["dates"]
    return [d for d in dates if start.isoformat() <= d <= end.isoformat()]


def _qs(pairs):
    return "&".join(f"{quote(str(key), safe='')}={quote(str(value), safe='')}" for key, value in pairs if value not in (None, ""))


def _range_pairs(active, start, end):
    return [("period", active), ("from", start.isoformat()), ("to", end.isoformat())]


def _department_index(archive):
    refs = archive.get("refs") or []
    grouped = {}
    for product in archive.get("products") or []:
        if not isinstance(product, list) or len(product) < 8:
            continue
        ref = product[2]
        department, group = "Unmapped", ""
        if isinstance(ref, int) and 0 <= ref < len(refs) and isinstance(refs[ref], list) and refs[ref]:
            department = str(refs[ref][0])
            group = str(refs[ref][1]) if len(refs[ref]) > 1 else ""
        bucket = grouped.setdefault(department, {"groups": set(), "products": []})
        if group:
            bucket["groups"].add(group)
        bucket["products"].append((product, group))
    return grouped


def _department_cards(archive, active, start, end):
    grouped = _department_index(archive)
    cards = []
    for name in sorted(grouped, key=str.casefold):
        info = grouped[name]
        groups = ", ".join(sorted(info["groups"], key=str.casefold)[:6])
        href = "/portal/products?" + _qs(_range_pairs(active, start, end) + [("dept", name)])
        cards.append(
            f'<a class="dept-card {portal.tone(name)}" href="{portal.h(href)}"><b>{portal.h(name)}</b>'
            f'<span>{len(info["products"])} products</span><small>{portal.h(groups)}</small></a>'
        )
    if not cards:
        return '<p class="empty">No departments in the product archive yet.</p>'
    return f'<section class="card" id="product-departments"><h2>Departments</h2><div class="dept-grid">{"".join(cards)}</div></section>'


def _product_keep(active, start, end, dept, group, q, sort, arrange, selected, filtering, product=""):
    pairs = _range_pairs(active, start, end) + [
        ("dept", dept), ("group", group), ("q", q), ("sort", sort), ("arrange", arrange), ("product", product),
    ]
    if filtering:
        pairs.extend(("supplier", name) for name in selected)
    return pairs


def _department_products(c, archive, dept, active, start, end, query, user):
    grouped = _department_index(archive)
    info = grouped.get(dept)
    back = "/portal/products?" + _qs(_range_pairs(active, start, end))
    if not info:
        return f'<p class="empty">That department is not in the product archive.</p><p><a class="btn ghost" href="{portal.h(back)}">All departments</a></p>'
    import portal_stock
    on_hand = {row["code"]: row["onHand"] for row in portal_stock.tracked_rows(c)}
    manual = portal.manual_supplier_map(c)
    suggested = portal.suggested_supplier_map(c)
    supplier_names = portal.supplier_filter_names(c)
    selected, filtering = portal.chosen_suppliers(query, supplier_names)
    table = override_table(c)
    needle = (query.get("q") or "").strip().casefold()
    group_filter = query.get("group") or ""
    sort = query.get("sort") if query.get("sort") in ("name", "qty", "sales", "gp", "stock", "supplier") else "name"
    arrange = "supplier" if query.get("arrange") == "supplier" else ""
    built = []
    for product, group in info["products"]:
        if group_filter and group != group_filter:
            continue
        code = str(product[0])
        state = portal.supplier_state(code, manual, suggested)
        label = state["name"] if state["source"] else ""
        if not portal.supplier_kept(label, selected, filtering):
            continue
        hay = f"{product[0]} {product[1]} {product[3] or ''} {group} {label}".casefold()
        if needle and needle not in hay:
            continue
        totals = sum_rows(_walk_pack(product, archive["dates"], start.isoformat(), end.isoformat(), table, True))
        built.append({
            "code": code, "name": str(product[1]), "group": group,
            "qty": totals["qty"], "sales": totals["sales_inc"] / Decimal(100), "gp": totals["gp"] / Decimal(100),
            "onhand": on_hand.get(code), "override": totals["override_days"] > 0,
            "supplier": label, "state": state,
        })
    def sort_key(row):
        if sort == "qty":
            return row["qty"]
        if sort == "sales":
            return row["sales"]
        if sort == "gp":
            return row["gp"]
        if sort == "stock":
            return row["onhand"] if row["onhand"] is not None else Decimal("-1e18")
        if sort == "supplier":
            return (portal.supplier_rank(row["supplier"]), row["name"].casefold())
        return row["name"].casefold()
    built.sort(key=sort_key, reverse=sort not in ("name", "supplier"))
    grouped_rows = arrange == "supplier" or sort == "supplier"
    if grouped_rows and sort != "supplier":
        built.sort(key=lambda row: portal.supplier_rank(row["supplier"]))
    q_text = query.get("q") or ""

    def keep(sort_key_name=sort, group_name=group_filter, arrange_name=arrange, product=""):
        return _product_keep(active, start, end, dept, group_name, q_text, sort_key_name, arrange_name, selected, filtering, product)

    chips = []
    for key, label in (("name", "Name"), ("supplier", "Supplier"), ("qty", "Qty"), ("sales", "Sales"), ("gp", "GP"), ("stock", "Stock")):
        cls = " on" if key == sort else ""
        href = "/portal/products?" + _qs(keep(sort_key_name=key))
        chips.append(f'<a class="chip{cls}" href="{portal.h(href)}">{label}</a>')
    group_on = arrange == "supplier"
    chips.append(
        f'<a class="chip{" on" if group_on else ""}" href="{portal.h("/portal/products?" + _qs(keep(arrange_name="" if group_on else "supplier")))}">'
        "Group by supplier</a>"
    )
    chips.append('<button class="btn ghost" type="button" data-print>Print list</button>')
    group_chips = [
        f'<a class="chip{" on" if not group_filter else ""}" href="'
        + portal.h("/portal/products?" + _qs(keep(group_name="")))
        + '">All groups</a>'
    ]
    for group in sorted(info["groups"], key=str.casefold):
        cls = " on" if group == group_filter else ""
        href = "/portal/products?" + _qs(keep(group_name=group))
        group_chips.append(f'<a class="chip {portal.tone(group)}{cls}" href="{portal.h(href)}">{portal.h(group)}</a>')
    sticky = {
        "period": active, "from": start.isoformat(), "to": end.isoformat(), "dept": dept, "sort": sort,
    }
    if group_filter:
        sticky["group"] = group_filter
    if q_text:
        sticky["q"] = q_text
    if arrange:
        sticky["arrange"] = arrange
    if filtering:
        sticky["supplier"] = selected
    editor_back = portal.return_query(sticky, portal.PRODUCT_RETURN_KEYS)
    body = []
    previous = None
    for row in built:
        if grouped_rows and row["supplier"] != previous:
            previous = row["supplier"]
            label = previous or "Unassigned"
            count = sum(1 for item in built if item["supplier"] == previous)
            noun = "product" if count == 1 else "products"
            body.append(
                f'<tr class="supplier-head"><td colspan="7">{portal.h(label)} '
                f'<span class="mini">{count} {noun}</span></td></tr>'
            )
        pill = ' <span class="pill">Cost override</span>' if row["override"] else ""
        stock = portal_stock.qty_text(row["onhand"]) if row["onhand"] is not None else "—"
        href = "/portal/products?" + _qs(keep(product=row["code"]))
        editor = portal.supplier_editor(user, "/portal/products", row["code"], row["state"], editor_back, row["name"])
        body.append(
            f'<tr id="product-row-{portal.h(row["code"])}"><td><a href="{portal.h(href)}">{portal.h(row["name"])}</a>{pill}<div class="mini">{portal.h(row["code"])}</div></td>'
            f'<td>{portal.h(row["group"] or "—")}</td><td>{editor}</td>'
            f'<td class="num">{_fmt_qty(row["qty"], dept == "A Fuel")}</td>'
            f'<td class="num">{portal.money(_dollars(row["sales"]))}</td><td class="num">{portal.money(_dollars(row["gp"]))}</td>'
            f'<td class="num">{stock}</td></tr>'
        )
    table_html = '<p class="empty">No products in this department match.</p>' if not body else (
        '<div class="tw"><table><thead><tr><th>Product</th><th>Group</th><th>Supplier</th><th class="num">Qty</th><th class="num">Sales</th><th class="num">GP</th><th class="num">On hand</th></tr></thead><tbody>'
        + "".join(body) + "</tbody></table></div>"
    )
    clear_href = "/portal/products?" + _qs([pair for pair in keep() if pair[0] != "supplier"])
    return f'''<p><a class="btn ghost" href="{portal.h(back)}">All departments</a></p>
<section class="card" id="dept-products"><h2>{portal.h(dept)}</h2>
<form class="period" id="product-filters" method="get" action="/portal/products">
<input type="hidden" name="period" value="{portal.h(active)}"><input type="hidden" name="from" value="{start.isoformat()}"><input type="hidden" name="to" value="{end.isoformat()}">
<input type="hidden" name="dept" value="{portal.h(dept)}"><input type="hidden" name="sort" value="{portal.h(sort)}">
<input type="hidden" name="arrange" value="{portal.h(arrange)}">
<input type="hidden" name="group" value="{portal.h(group_filter)}">
<label class="f">Search in this department<input name="q" value="{portal.h(q_text)}"></label>
<button class="btn" type="submit">Search</button>
<div class="row supplier-row">
<div class="f"><span>Suppliers</span>{portal.supplier_filter_box(supplier_names, selected, filtering)}</div>
<button class="btn" type="submit">Apply suppliers</button>
<a class="btn ghost" href="{portal.h(clear_href)}">Clear</a>
</div>
<p class="help">All suppliers is the default. Unassigned is no saved supplier and no invoice suggestion. Clear drops the supplier choice and keeps this department.</p>
</form>
<div class="chips">{"".join(group_chips)}</div>
<div class="chips">{"".join(chips)}</div>
{table_html}
{portal.supplier_datalist(portal.known_supplier_names(c))}
<p class="help">Qty and sales are for the selected dates. GP uses a cost override when one is effective, otherwise the till cost. On hand is shown only when the product is tracked. A supplier you save stays when the nightly upload runs. Suggested is filled from a delivery whose invoice number matches a bill, and it is not saved until you press Save. Group by supplier, or sort by Supplier, for an order list you can print.</p></section>'''


def page_products(c, query, user, error_text=""):
    through = portal.data_through(c)
    anchor = portal.anchor_day(c)
    error = f'<p class="alert">{portal.h(str(error_text)[:180])}</p>' if error_text else ""
    try:
        active, start, end = portal.period_bounds(query.get("period"), query.get("from"), query.get("to"), anchor)
    except ValueError as ex:
        active, start, end = portal.period_bounds("month", None, None, anchor)
        error += f'<p class="alert">{portal.h(str(ex))}</p>'
    ok = ""
    if query.get("ok") == "supplier":
        ok = '<p class="oknote" id="supplier-saved">Supplier saved. The nightly upload will not replace it.</p>'
    names = portal.supplier_filter_names(c)
    selected, filtering = portal.chosen_suppliers(query, names)
    extra = {}
    for key in ("dept", "group", "q", "sort", "arrange", "product", "compare"):
        value = query.get(key)
        if isinstance(value, str) and value:
            extra[key] = value
    if filtering and selected:
        extra["supplier"] = selected
    archive = product_archive(c)
    q = (query.get("q") or "").strip()
    chosen = (query.get("product") or "").strip()
    compare = (query.get("compare") or "").strip()
    body = ""
    if not archive:
        body = '<section class="card"><p class="empty">No product archive yet. It arrives with the nightly product-search upload.</p></section>'
    else:
        dates = archive["dates"]
        matches = []
        needle = q.casefold()
        for product in archive["products"]:
            if not isinstance(product, list) or len(product) < 8:
                continue
            code, name, _ref, barcode = str(product[0]), str(product[1]), product[2], str(product[3] or "")
            hay = f"{code} {name} {barcode}".casefold()
            if needle and needle not in hay:
                continue
            matches.append(product)
            if len(matches) >= 40 and not chosen:
                break
        if not chosen and len(matches) == 1:
            chosen = str(matches[0][0])
        if chosen:
            extra["product"] = chosen
        if compare:
            extra["compare"] = compare
        editor_back = portal.return_query(extra, portal.PRODUCT_RETURN_KEYS)
        items = []
        for product in matches[:40]:
            code = str(product[0])
            cls = " on" if code == chosen else ""
            items.append(f'<a class="chip{cls}" href="/portal/products?period={portal.h(active)}&from={start.isoformat()}&to={end.isoformat()}&q={portal.h(q)}&product={portal.h(code)}">{portal.h(product[1])}</a>')
        search = f"""<form class="period" method="get" action="/portal/products">
<input type="hidden" name="period" value="{portal.h(active)}">
<input type="hidden" name="from" value="{start.isoformat()}"><input type="hidden" name="to" value="{end.isoformat()}">
<div class="row"><label class="f">Name, PLU or barcode<input name="q" value="{portal.h(q)}" maxlength="80"></label>
<button class="btn" type="submit">Search</button></div></form>
<div class="chips">{''.join(items) or '<p class="empty">No matching product in the archive.</p>'}</div>"""
        detail = _product_detail(c, archive, chosen, compare, active, start, end, q, user, editor_back) if chosen else ""
        span = _selected_archive_span(archive, start, end)
        note = f"Archive {portal.h(dates[0])} to {portal.h(dates[-1])}. {len(span)} trading days in this range. Days outside the archive are left out, not filled with zero."
        dept = query.get("dept") or ""
        if chosen:
            back_pairs = _range_pairs(active, start, end) + ([("dept", dept)] if dept else [])
            if filtering:
                back_pairs.extend(("supplier", name) for name in selected)
            back = "/portal/products?" + _qs(back_pairs)
            label = "All departments" if not dept else "Back to " + dept
            body = f'<p><a class="btn ghost" href="{portal.h(back)}">{portal.h(label)}</a></p>' + search + f'<p class="help">{note}</p>' + detail
        elif dept:
            body = _department_products(c, archive, dept, active, start, end, query, user)
        else:
            body = _department_cards(archive, active, start, end) + search + f'<p class="help">{note}</p>'
    return f"""<div class="ph"><h1>Products</h1>{portal.through_line(through)}
<p>Quantity is the sale unit. Fuel quantity is litres. Gross profit uses a cost override when one is effective on that sale date, otherwise the till cost. Each product has one supplier for ordering. A name you save is kept when the nightly files upload.</p></div>
{error}{ok}{portal.period_controls('/portal/products', active, start, end, extra)}{body}"""


def _find_product(archive, code):
    for product in archive["products"]:
        if isinstance(product, list) and str(product[0]) == code:
            return product
    return None


def _product_block(c, archive, product, active, start, end, table, user=None, back=""):
    if not product:
        return '<p class="empty">Choose a product.</p>'
    dates = archive["dates"]
    rows = _walk_pack(product, dates, start.isoformat(), end.isoformat(), table, True)
    totals = sum_rows(rows)
    prev_start, prev_end = _prev_bounds(active, start, end)
    prev_rows = _walk_pack(product, dates, prev_start.isoformat(), prev_end.isoformat(), table, True)
    prev = sum_rows(prev_rows)
    archive_start = dates[0]
    partial = prev_start.isoformat() < archive_start
    litres = dept_of(archive, product) == "A Fuel"
    qty_days = len(_selected_archive_span(archive, start, end)) or 0
    per_day = (totals["qty"] / Decimal(qty_days)) if qty_days else None
    last_cost = None
    last_override = False
    last_source = None
    if rows:
        last = rows[-1]
        last_cost = last["unit_cents"]
        last_override = last["override"]
        last_source = last["source"]
    cost_label = portal.money(_money_cents(last_cost)) + " ex GST" if last_cost is not None else "No till cost"
    pill = f' <span class="pill">Cost override · {portal.h(last_source or "override")}</span>' if last_override else ""
    prev_html = ""
    if prev["days"]:
        prev_html = (
            f"<p>Previous period {portal.h(portal.fmt_date(prev_start.isoformat()))} – {portal.h(portal.fmt_date(prev_end.isoformat()))}: "
            f"{_fmt_qty(prev['qty'], litres)} and {portal.money(_dollars(prev['sales_inc'] / 100))} inc GST"
            f", GP {portal.money(_dollars(prev['gp'] / 100))} ({portal.pct_ratio(float(prev['gp_pct']) if prev['gp_pct'] is not None else None)}), "
            f"coverage {portal.pct_ratio(float(prev['coverage']) if prev['coverage'] is not None else None)}."
            + (" Earlier days in that window are not in the archive and are not treated as zero." if partial else "")
            + "</p>"
        )
    elif partial:
        prev_html = "<p>The previous period is before this archive starts, so it is not shown as zero.</p>"
    series = []
    for row in rows:
        mark = " · override" if row["override"] else ""
        series.append(f"<tr><td>{portal.h(portal.fmt_date(row['date']))}</td><td class=\"num\">{_fmt_qty(row['qty'], litres)}{portal.h(mark)}</td></tr>")
    ly = _ly_product_line(c, str(product[0]), start, end)
    gp_line = portal.money(_dollars(totals["gp"] / 100))
    supplier_html = ""
    if user:
        state = portal.supplier_state(str(product[0]), portal.manual_supplier_map(c), portal.suggested_supplier_map(c))
        supplier_html = (
            '<div class="lbl">Supplier</div>'
            + portal.supplier_editor(user, "/portal/products", str(product[0]), state, back, str(product[1]))
            + '<p class="help">Save a supplier to keep it. A suggestion comes from a delivery whose invoice number matches a purchase or invoice, and it stays a suggestion until you save.</p>'
        )
    return f"""<section class="card" id="product-{portal.h(product[0])}"><h2>{portal.h(product[1])}</h2>
<p class="help">PLU {portal.h(product[0])} · {portal.h(dept_of(archive, product))} · barcode {portal.h(product[3] or '—')}</p>
{supplier_html}
<div class="grid g4">
<div class="stat"><div class="lbl">Qty</div><b>{_fmt_qty(totals['qty'], litres)}</b><span>{_fmt_qty(per_day, litres) if per_day is not None else '—'} per trading day</span></div>
<div class="stat"><div class="lbl">Sales</div><b>{portal.money(_dollars(totals['sales_inc'] / 100))}</b><span>inc GST</span></div>
<div class="stat"><div class="lbl">Covered GP</div><b>{gp_line}</b><span>{portal.pct_ratio(float(totals['gp_pct']) if totals['gp_pct'] is not None else None)}</span></div>
<div class="stat"><div class="lbl">Cost coverage</div><b>{portal.pct_ratio(float(totals['coverage']) if totals['coverage'] is not None else None)}</b><span>Latest unit cost {cost_label}{pill}</span></div>
</div>
{prev_html}
{ly}
<div class="tw"><table><thead><tr><th>Date</th><th>Qty</th></tr></thead><tbody>{''.join(series) or '<tr><td colspan="2">No sales in this range.</td></tr>'}</tbody></table></div>
</section>"""


def _ly_product_line(c, code, start, end):
    month = november_2025(c)
    if not month:
        return ""
    match = None
    for row in month.get("products") or []:
        if str(row.get("code")) == code:
            match = row
            break
    if not match:
        return ""
    n = 0
    # pro-rate only when the selected range sits inside November 2026
    if start.year == 2026 and start.month == 11:
        n = (end - start).days + 1
    full = month.get("days") or 30
    pro = ""
    if n and full:
        factor = Decimal(n) / Decimal(full)
        pro = (
            f" Pro-rated to {n} of {full} days: {_fmt_qty(Decimal(str(match['qty'])) * factor)} and "
            f"{portal.money(float(Decimal(str(match['salesEx'])) * factor))} ex GST."
        )
    return (
        f'<p id="product-2025">November 2025: {_fmt_qty(match["qty"])} and {portal.money(match["salesEx"])} ex GST.{pro} '
        "2025 has no cost or GP on this page.</p>"
    )


def _product_detail(c, archive, chosen, compare, active, start, end, q, user=None, back=""):
    table = override_table(c)
    main = _find_product(archive, chosen) if chosen else None
    html = _product_block(c, archive, main, active, start, end, table, user, back) if main else ""
    compare_html = ""
    if main:
        compare_html = f"""<form class="period" method="get" action="/portal/products">
<input type="hidden" name="period" value="{portal.h(active)}"><input type="hidden" name="from" value="{start.isoformat()}">
<input type="hidden" name="to" value="{end.isoformat()}"><input type="hidden" name="q" value="{portal.h(q)}">
<input type="hidden" name="product" value="{portal.h(chosen)}">
<label class="f">Compare another product<input name="compare" value="{portal.h(compare)}" placeholder="PLU, name or barcode"></label>
<button class="btn" type="submit">Compare</button></form>"""
        other = None
        if compare:
            needle = compare.casefold()
            for product in archive["products"]:
                hay = f"{product[0]} {product[1]} {product[3] or ''}".casefold()
                if needle in hay:
                    other = product
                    break
            if other and str(other[0]) != str(main[0]):
                compare_html += _product_block(c, archive, other, active, start, end, table, user, back)
    datalist = portal.supplier_datalist(portal.known_supplier_names(c)) if user and (html or compare_html) else ""
    return html + compare_html + datalist


def endings_for(raw, ends=(50, 95, 99)):
    if raw is None:
        return []
    base = int(Decimal(str(raw)).to_integral_value(rounding=ROUND_HALF_UP))
    # floor
    base = int(Decimal(str(raw)) // 1)
    out = []
    for end in ends:
        cents = Decimal(end) / Decimal(100)
        cand = Decimal(base) + cents
        if cand + Decimal("0.0001") < Decimal(str(raw)):
            cand = Decimal(base + 1) + cents
        out.append(float(cand.quantize(Decimal("0.01"))))
    return out


def suggest_from_cost(cost_ex, margin, gst):
    if cost_ex is None or margin is None or margin >= 1 or margin < 0:
        return None, []
    sell_ex = Decimal(str(cost_ex)) / (Decimal(1) - Decimal(str(margin)))
    sell = sell_ex * (Decimal("1.1") if gst else Decimal(1))
    sell_f = float(sell)
    return sell_f, endings_for(sell_f)


def _retail_rows(data):
    cols = data.get("columns") or []
    rows = []
    for raw in data.get("rows") or []:
        if not isinstance(raw, list):
            continue
        item = {}
        for i, col in enumerate(cols):
            if i < len(raw):
                item[col] = raw[i]
        if item.get("code") is not None:
            rows.append(item)
    return rows


def _pricing_browser(rows, config, query, c, data, through, chosen):
    if chosen:
        back = "/portal/pricing" + (("?dept=" + quote(query.get("dept") or "")) if query.get("dept") else "")
        return f'<p><a class="btn ghost" href="{portal.h(back)}">All departments</a></p>'
    margins = config.get("department_margins") if isinstance(config.get("department_margins"), dict) else {}
    default_margin = config.get("default_margin") if isinstance(config.get("default_margin"), (int, float)) else 0.35
    excluded = config.get("excluded_departments") if isinstance(config.get("excluded_departments"), list) else []
    grouped = {}
    for row in rows:
        grouped.setdefault(str(row.get("department") or "Other"), []).append(row)
    dept = query.get("dept") or ""
    if not dept:
        cards = []
        for name in sorted(grouped, key=str.casefold):
            cards.append(f'<a class="dept-card {portal.tone(name)}" href="/portal/pricing?dept={quote(name)}"><b>{portal.h(name)}</b><span>{len(grouped[name])} products</span></a>')
        if not cards:
            return '<p class="empty">No priced products yet.</p>'
        return f'<section class="card" id="price-departments"><h2>Departments</h2><div class="dept-grid">{"".join(cards)}</div></section>'
    found = grouped.get(dept) or []
    needle = (query.get("q") or "").strip().casefold()
    sort = query.get("sort") if query.get("sort") in ("name", "cost", "sell", "gp") else "name"
    as_of = (data or {}).get("_asOf") or (data or {}).get("asOf") or through or portal.today().isoformat()
    table = override_table(c)
    built = []
    margin = margins.get(dept, default_margin)
    for row in found:
        hay = f"{row.get('code','')} {row.get('description','')} {row.get('barcodes','')}".casefold()
        if needle and needle not in hay:
            continue
        ov = override_on(table, row.get("code"), as_of)
        till_cost = row.get("costEx")
        cost = _money_cents(ov["cost_ex_cents"]) if ov else till_cost
        sell = row.get("latestObservedSellInc")
        gp_pct = None
        if isinstance(sell, (int, float)) and isinstance(cost, (int, float)) and sell:
            sell_ex = Decimal(str(sell)) / Decimal("1.1")
            if sell_ex:
                gp_pct = float((sell_ex - Decimal(str(cost))) / sell_ex)
        raw, ends = suggest_from_cost(cost, margin, True)
        built.append({"row": row, "cost": cost, "sell": sell, "gp": gp_pct, "suggested": ends[1] if len(ends) > 1 else (ends[0] if ends else raw), "override": bool(ov)})
    def sort_key(item):
        if sort == "cost":
            return item["cost"] if isinstance(item["cost"], (int, float)) else -1
        if sort == "sell":
            return item["sell"] if isinstance(item["sell"], (int, float)) else -1
        if sort == "gp":
            return item["gp"] if item["gp"] is not None else -999
        return str(item["row"].get("description") or "").casefold()
    built.sort(key=sort_key, reverse=sort != "name")
    chips = []
    for key, label in (("name", "Name"), ("cost", "Cost"), ("sell", "Sell"), ("gp", "GP%")):
        cls = " on" if key == sort else ""
        chips.append(f'<a class="chip{cls}" href="/portal/pricing?dept={quote(dept)}&sort={key}&q={quote(query.get("q") or "")}">{label}</a>')
    body = []
    for item in built:
        row = item["row"]
        pill = ' <span class="pill">Cost override</span>' if item["override"] else ""
        href = f'/portal/pricing?dept={quote(dept)}&product={quote(str(row.get("code")))}'
        body.append(
            f'<tr><td><a href="{href}">{portal.h(row.get("description"))}</a>{pill}<div class="mini">{portal.h(row.get("code"))}</div></td>'
            f'<td class="num">{portal.money(item["cost"])}</td><td class="num">{portal.money(item["sell"])}</td>'
            f'<td class="num">{portal.pct_ratio(item["gp"])}</td><td class="num">{portal.money(item["suggested"])}</td></tr>'
        )
    table_html = '<p class="empty">No products in this department match.</p>' if not body else (
        '<div class="tw"><table><thead><tr><th>Product</th><th class="num">Cost ex GST</th><th class="num">Sell inc GST</th><th class="num">GP%</th><th class="num">Suggested</th></tr></thead><tbody>'
        + "".join(body) + "</tbody></table></div>"
    )
    note = "" if dept not in excluded else "<p class=\"help\">This department is excluded from the target shop margin.</p>"
    return f'''<p><a class="btn ghost" href="/portal/pricing">All departments</a></p>
<section class="card" id="price-dept"><h2>{portal.h(dept)}</h2>
<form class="period" method="get" action="/portal/pricing"><input type="hidden" name="dept" value="{portal.h(dept)}"><input type="hidden" name="sort" value="{portal.h(sort)}">
<label class="f">Search in this department<input name="q" value="{portal.h(query.get("q") or "")}"></label>
<button class="btn" type="submit">Search</button></form>
<div class="chips">{"".join(chips)}</div>
{note}{table_html}
<p class="help">Suggested sell uses the department target margin and a 95-cent ending where one is at or above the raw price. Nothing is written to SwiftPOS.</p></section>'''


def page_pricing(c, query):
    through = portal.data_through(c)
    data = load_blob(c, "retail-pricing")
    if not data:
        return f"""<div class="ph"><h1>Pricing</h1>{portal.through_line(through)}
<p>No retail price file yet. This calculator does not write SwiftPOS prices or costs.</p></div>"""
    config = data.get("config") if isinstance(data.get("config"), dict) else {}
    default_margin = config.get("default_margin") if isinstance(config.get("default_margin"), (int, float)) else 0.35
    dept_margins = config.get("department_margins") if isinstance(config.get("department_margins"), dict) else {}
    excluded = config.get("excluded_departments") if isinstance(config.get("excluded_departments"), list) else []
    q = (query.get("q") or "").strip()
    chosen = (query.get("product") or "").strip()
    rows = _retail_rows(data)
    matches = []
    needle = q.casefold()
    for row in rows:
        hay = f"{row.get('code','')} {row.get('description','')} {row.get('barcodes','')}".casefold()
        if needle and needle not in hay:
            continue
        matches.append(row)
        if len(matches) >= 30 and not chosen:
            break
    if not chosen and len(matches) == 1:
        chosen = str(matches[0].get("code"))
    chips = []
    for row in matches[:30]:
        code = str(row.get("code"))
        cls = " on" if code == chosen else ""
        chips.append(f'<a class="chip{cls}" href="/portal/pricing?q={portal.h(q)}&product={portal.h(code)}">{portal.h(row.get("description") or code)}</a>')
    product = next((row for row in rows if str(row.get("code")) == chosen), None)
    detail = ""
    if product:
        department = str(product.get("department") or "")
        margin = dept_margins.get(department, default_margin)
        try:
            if query.get("margin"):
                margin = float(query.get("margin")) / 100.0
        except ValueError:
            margin = default_margin
        as_of = data.get("_asOf") or data.get("asOf") or through or portal.today().isoformat()
        ov = override_on(override_table(c), product.get("code"), as_of)
        till_cost = product.get("costEx")
        cost = _money_cents(ov["cost_ex_cents"]) if ov else till_cost
        gst = department not in excluded
        raw, ends = suggest_from_cost(cost, margin, True)
        end_bits = " ".join(portal.money(n) for n in ends)
        pill = ""
        till_line = ""
        if ov:
            pill = f' <span class="pill">Cost override · {portal.h(ov["source"])}</span>'
            till_line = f"<p class=\"help\">Till cost on the price file is {portal.money(till_cost)} ex GST. GP uses the override.</p>"
        excluded_note = "<p class=\"help\">Fuel, system and surcharge are not given a target shop margin.</p>" if department in excluded else ""
        detail = f"""<section class="card" id="price-card"><h2>{portal.h(product.get('description'))}</h2>
<p>PLU {portal.h(product.get('code'))} · {portal.h(department)} · current sell {portal.money(product.get('latestObservedSellInc'))} inc GST</p>
<p>Cost used {portal.money(cost)} ex GST{pill}</p>
{till_line}
{excluded_note}
<p>Target margin {float(margin) * 100:.1f}%. Suggested sell before endings {portal.money(raw)}. Endings 50 / 95 / 99 at or above that price: {end_bits}.</p>
<form class="period" method="get" action="/portal/pricing">
<input type="hidden" name="product" value="{portal.h(product.get('code'))}"><input type="hidden" name="q" value="{portal.h(q)}">
<label class="f">Target margin %<input name="margin" inputmode="decimal" value="{float(margin) * 100:.1f}"></label>
<button class="btn" type="submit">Update suggestion</button></form>
<p class="help">The margin stays in this page only. It is not written back to the till.</p>
</section>"""
    what = ""
    if query.get("what_cost") or query.get("what_margin") or query.get("what_price"):
        try:
            wcost = float(query.get("what_cost") or 0)
            wmargin = float(query.get("what_margin") or 0) / 100.0
            gst_on = (query.get("what_gst") or "inc") != "ex"
            raw, ends = suggest_from_cost(wcost, wmargin, gst_on)
            what_result = f"<p id=\"what-if-result\">Suggested {portal.money(raw)} {'inc' if gst_on else 'ex'} GST. Endings: {' '.join(portal.money(n) for n in ends)}.</p>"
        except ValueError:
            what_result = '<p class="alert">Enter a cost and a margin.</p>'
    else:
        what_result = ""
    what = f"""<section class="card" id="what-if"><h2>What-if</h2>
<form class="period" method="get" action="/portal/pricing">
<div class="row">
<label class="f">Cost ex GST<input name="what_cost" inputmode="decimal" value="{portal.h(query.get('what_cost') or '')}"></label>
<label class="f">Margin %<input name="what_margin" inputmode="decimal" value="{portal.h(query.get('what_margin') or '')}"></label>
<label class="f">Show sell<select name="what_gst"><option value="inc">inc GST</option><option value="ex"{" selected" if query.get("what_gst")=="ex" else ""}>ex GST</option></select></label>
</div>
<button class="btn" type="submit">Calculate</button></form>
{what_result}
<p class="help">This calculator does not write SwiftPOS prices or costs.</p></section>"""
    margins = []
    for name, value in sorted(dept_margins.items()):
        margins.append(f"<tr><td>{portal.h(name)}</td><td class=\"num\">{float(value) * 100:.1f}%</td></tr>")
    margin_table = '<div class="tw"><table><thead><tr><th>Department</th><th>Target margin</th></tr></thead><tbody>' + "".join(margins) + f'<tr><td>Default</td><td class="num">{float(default_margin) * 100:.1f}%</td></tr></tbody></table></div>'
    dept_name = query.get("dept") or ""
    pricing_nav = _pricing_browser(rows, config, query, c, data, through, chosen)
    return f"""<div class="ph"><h1>Pricing</h1>{portal.through_line(through)}
<p>Look up a product, then a suggested sell from cost and target margin. Retail endings are 50, 95 and 99 cents. Nothing is saved to SwiftPOS.</p></div>
{pricing_nav}
<form class="period" method="get" action="/portal/pricing"><div class="row">
<input type="hidden" name="dept" value="{portal.h(dept_name)}">
<label class="f">Name, PLU or barcode<input name="q" value="{portal.h(q)}"></label>
<button class="btn" type="submit">Search</button></div></form>
<div class="chips">{''.join(chips)}</div>
{detail}{what}
<section class="card"><h2>Department target margins</h2>{margin_table}
<p class="help">{portal.h(config.get('tax_scenario') or '')}</p></section>"""


def _zip_cols(cols, row):
    out = {}
    for i, col in enumerate(cols or []):
        if i < len(row):
            out[col] = row[i]
    return out


def _cost_only(row):
    reason = str(row.get("reason") or "")
    ident = str(row.get("id") or "")
    return "COST-ONLY" in reason.upper() or ident.startswith("fuelcost")


def _till_gp(row, table):
    flagged = row.get("flagged") or "9999-99-99"
    ov = override_on(table, row.get("plu"), flagged)
    sell = row.get("currentSell")
    till_cost = row.get("newCost") if row.get("newCost") is not None else row.get("oldCost")
    cost = _money_cents(ov["cost_ex_cents"]) if ov else till_cost
    gp_pct = row.get("currentGpPct")
    if ov and sell and cost is not None:
        sell_ex = Decimal(str(sell)) / Decimal("1.1")
        cost_d = Decimal(str(cost))
        if sell_ex:
            gp_pct = float((sell_ex - cost_d) / sell_ex)
    return cost, gp_pct, ov


def page_till(c, query):
    through = portal.data_through(c)
    anchor = portal.anchor_day(c)
    try:
        active, start, end = portal.period_bounds(query.get("period"), query.get("from"), query.get("to"), anchor)
        error = ""
    except ValueError as ex:
        active, start, end = portal.period_bounds("month", None, None, anchor)
        error = f'<p class="alert">{portal.h(str(ex))}</p>'
    data = load_blob(c, "till-price-changes")
    if not data:
        return f"""<div class="ph"><h1>Till price changes</h1>{portal.through_line(through)}
<p>No price-change file yet. This page does not write SwiftPOS prices or costs.</p></div>"""
    status = (query.get("status") or "pending").lower()
    if status not in ("pending", "done", "all"):
        status = "pending"
    table = override_table(c)
    links = []
    for key, label in (("pending", "Pending"), ("done", "Done"), ("all", "All")):
        cls = " on" if key == status else ""
        links.append(f'<a class="chip{cls}" href="/portal/till-prices?status={key}">{label}</a>')
    body = []
    for row in data.get("rows") or []:
        if not isinstance(row, dict):
            continue
        row_status = str(row.get("status") or "").lower()
        if status == "pending" and row_status != "pending":
            continue
        if status == "done" and row_status != "done":
            continue
        cost, gp_pct, ov = _till_gp(row, table)
        flags = []
        if _cost_only(row):
            flags.append('<span class="pill">Cost only</span>')
        if ov:
            flags.append(f'<span class="pill">Cost override · {portal.h(ov["source"])}</span>')
        body.append(
            "<tr><td>" + portal.h(row.get("flagged")) + "</td><td>" + portal.h(row.get("name"))
            + "<div class=\"mini\">" + portal.h(row.get("plu")) + "</div></td><td>" + portal.h(row.get("status"))
            + " " + "".join(flags) + "</td><td class=\"num\">" + portal.money(row.get("oldCost"))
            + "</td><td class=\"num\">" + portal.money(row.get("newCost")) + "</td><td class=\"num\">"
            + portal.money(cost) + "</td><td class=\"num\">" + portal.money(row.get("currentSell"))
            + "</td><td class=\"num\">" + portal.pct_ratio(gp_pct) + "</td><td>" + portal.h(row.get("reason")) + "</td></tr>"
        )
    rows_html = '<p class="empty">No rows for this filter.</p>' if not body else (
        '<div class="tw"><table><thead><tr><th>Flagged</th><th>Item</th><th>Status</th><th>Old till cost</th><th>New till cost</th><th>GP cost</th><th>Sell</th><th>GP%</th><th>Note</th></tr></thead><tbody>'
        + "".join(body) + "</tbody></table></div>"
    )
    new_cols = (data.get("newItems") or {}).get("columns") or []
    new_rows = []
    for raw in (data.get("newItems") or {}).get("rows") or []:
        item = _zip_cols(new_cols, raw if isinstance(raw, list) else [])
        new_rows.append("<tr>" + "".join(f"<td>{portal.h(item.get(col))}</td>" for col in new_cols) + "</tr>")
    new_html = '<p class="empty">No new items.</p>' if not new_rows else (
        '<div class="tw"><table><thead><tr>' + "".join(f"<th>{portal.h(col)}</th>" for col in new_cols) + "</tr></thead><tbody>"
        + "".join(new_rows) + "</tbody></table></div>"
    )
    no_cols = (data.get("noCost") or {}).get("columns") or []
    dept = (query.get("dept") or "").strip()
    departments = []
    no_rows = []
    for raw in (data.get("noCost") or {}).get("rows") or []:
        if not isinstance(raw, list):
            continue
        item = _zip_cols(no_cols, raw)
        name = item.get("department") or ""
        if name and name not in departments:
            departments.append(name)
        last = str(item.get("lastSale") or "")
        if last and not (start.isoformat() <= last <= end.isoformat()):
            continue
        if dept and dept != name:
            continue
        ov = override_on(table, item.get("code"), last or (data.get("asOf") or "9999-12-31"))
        if ov:
            continue
        cells = []
        for col in no_cols:
            cells.append(f"<td>{portal.h(item.get(col))}</td>")
        no_rows.append("<tr>" + "".join(cells) + "</tr>")
    dept_links = [f'<a class="chip{" on" if not dept else ""}" href="/portal/till-prices?status={portal.h(status)}&period={portal.h(active)}">All departments</a>']
    for name in departments:
        cls = " on" if name == dept else ""
        dept_links.append(f'<a class="chip{cls}" href="/portal/till-prices?status={portal.h(status)}&period={portal.h(active)}&dept={portal.h(name)}">{portal.h(name)}</a>')
    no_html = '<p class="empty">No blank or $0 till costs in this window. A cost override removes a product from this list.</p>' if not no_rows else (
        '<div class="tw"><table><thead><tr>' + "".join(f"<th>{portal.h(col)}</th>" for col in no_cols) + "</tr></thead><tbody>"
        + "".join(no_rows) + "</tbody></table></div>"
    )
    return f"""<div class="ph"><h1>Till price changes</h1>{portal.through_line(data.get('_asOf') or through)}
<p>Pending, done, and new items for staff to key in Back Office. This page does not write SwiftPOS prices or costs. GP uses a cost override when one is effective on the flagged date, otherwise the till cost. A cost-only fuel row does not change the board price.</p></div>
{error}
<div class="chips">{''.join(links)}</div>
<section class="card" id="till-rows"><h2>Price and cost rows</h2>{rows_html}
<p class="help">{portal.h(data.get('sellSourceNote') or '')}</p></section>
<section class="card" id="new-items"><h2>New items to create</h2>{new_html}</section>
<section class="card" id="no-cost"><h2>No cost price</h2>
<p class="help">Sold in the no-cost window with a blank or $0 till cost, best sellers first. An override cost drops the row.</p>
{portal.period_controls('/portal/till-prices', active, start, end)}
<div class="chips">{''.join(dept_links)}</div>
{no_html}</section>"""


def _tuple_name(names, code):
    return names.get(str(code)) or str(code)


def _adjust_tuple(row, gp_delta, covered_delta):
    if not isinstance(row, list) or len(row) < 3:
        return row
    out = list(row)
    old_gp = Decimal(str(out[2] or 0))
    new_gp = old_gp + (gp_delta / Decimal(100))
    out[2] = float(new_gp.quantize(Decimal("0.01")))
    if len(out) >= 4 and out[3] not in (None, 0) and old_gp:
        covered = old_gp / Decimal(str(out[3]))
        covered += covered_delta / Decimal(100)
        if covered:
            out[3] = float(new_gp / covered)
    return out


def _archive_analytics(c, start, end):
    """Department, top, and low-margin rows from a full product archive.

    A short fixture is not the catalog, so callers keep the nightly snapshot
    until at least 400 products are loaded.
    """
    archive = product_archive(c)
    if not archive or len(archive.get("products") or []) < 400:
        return None
    table = override_table(c)
    departments = {}
    products = []
    names = {}
    for product in archive["products"]:
        if not isinstance(product, list) or len(product) < 8:
            continue
        department = dept_of(archive, product)
        if department == "A Fuel":
            continue
        rows = _walk_pack(product, archive["dates"], start.isoformat(), end.isoformat(), table, True)
        if not rows:
            continue
        totals = sum_rows(rows)
        if totals["days"] == 0:
            continue
        bucket = departments.setdefault(department, {"sales": Decimal(0), "gp": Decimal(0), "covered": Decimal(0), "ex": Decimal(0)})
        bucket["sales"] += totals["sales_inc"]
        bucket["gp"] += totals["gp"]
        bucket["covered"] += totals["covered_ex"]
        bucket["ex"] += totals["sales_ex"]
        names[str(product[0])] = str(product[1])
        products.append({
            "code": str(product[0]),
            "name": str(product[1]),
            "sales": totals["sales_inc"],
            "gp": totals["gp"],
            "covered": totals["covered_ex"],
            "ex": totals["sales_ex"],
            "override": totals["override_days"] > 0,
            "source": totals["source"],
        })
    dept_rows = []
    for name, bucket in departments.items():
        gp_pct = (bucket["gp"] / bucket["covered"]) if bucket["covered"] else None
        coverage = (bucket["covered"] / bucket["ex"]) if bucket["ex"] else None
        dept_rows.append([
            name,
            _dollars(bucket["sales"] / 100),
            _dollars(bucket["gp"] / 100),
            float(gp_pct) if gp_pct is not None else None,
            float(coverage) if coverage is not None else None,
        ])
    ranked = sorted(products, key=lambda row: row["gp"], reverse=True)
    top = []
    for row in ranked[:20]:
        gp_pct = (row["gp"] / row["covered"]) if row["covered"] else None
        coverage = (row["covered"] / row["ex"]) if row["ex"] else None
        item = [row["code"], _dollars(row["sales"] / 100), _dollars(row["gp"] / 100),
                float(gp_pct) if gp_pct is not None else None, float(coverage) if coverage is not None else None]
        if row["override"]:
            item.append(row["source"])
        top.append(item)
    low = []
    for row in products:
        gp_pct = (row["gp"] / row["covered"]) if row["covered"] else None
        gp = _dollars(row["gp"] / 100)
        if gp_pct is None or float(gp_pct) >= 0.15:
            if gp >= 0:
                continue
        item = [row["code"], _dollars(row["sales"] / 100), gp,
                float(gp_pct) if gp_pct is not None else None]
        if row["override"]:
            item.append(row["source"])
        low.append(item)
    low.sort(key=lambda row: row[3] if row[3] is not None else 0)
    return {"departments": dept_rows, "top": top, "low": low, "names": names, "fromArchive": True}


def page_departments(c, query):
    through = portal.data_through(c)
    anchor = portal.anchor_day(c)
    try:
        active, start, end = portal.period_bounds(query.get("period"), query.get("from"), query.get("to"), anchor)
        error = ""
    except ValueError as ex:
        active, start, end = portal.period_bounds("month", None, None, anchor)
        error = f'<p class="alert">{portal.h(str(ex))}</p>'
    days = portal.sales_between(c, start, end)
    buckets = {i: {"fuel": [], "shop": []} for i in range(7)}
    for row in days:
        wd = date.fromisoformat(row["date"]).weekday()
        buckets[wd]["fuel"].append(row["fuel"] or 0)
        buckets[wd]["shop"].append(row["shop"] or 0)
    names_wd = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    wd_rows = []
    for i, label in enumerate(names_wd):
        fuel_n = buckets[i]["fuel"]
        shop_n = buckets[i]["shop"]
        n = len(fuel_n)
        if not n:
            continue
        wd_rows.append(
            f"<tr><td>{label}</td><td class=\"num\">{portal.money(sum(fuel_n) / n)}</td><td class=\"num\">{portal.money(sum(shop_n) / n)}</td><td class=\"num\">{n}</td></tr>"
        )
    wd_table = '<p class="empty">No sales days in this range.</p>' if not wd_rows else (
        '<div class="tw"><table><thead><tr><th>Weekday</th><th>Avg fuel</th><th>Avg shop</th><th>Days</th></tr></thead><tbody>'
        + "".join(wd_rows) + "</tbody></table></div><p class=\"help\">Averages, not weekday totals. The count is the number of loaded days.</p>"
    )
    period = matching_period(c, active, start, end)
    names = product_names(c)
    built = _archive_analytics(c, start, end)
    deltas = [] if built else cost_deltas(c, start, end)
    by_code = {d["code"]: d for d in deltas}
    sort = query.get("sort") if query.get("sort") in ("sales", "gp", "gppct") else "sales"
    low_mode = query.get("low") if query.get("low") in ("under15", "negative") else "under15"
    dept_rows = []
    if built:
        dept_rows = built["departments"]
        names = dict(names)
        names.update(built.get("names") or {})
    elif period:
        for raw in period.get("departments") or []:
            if not isinstance(raw, list) or len(raw) < 3:
                continue
            row = list(raw)
            for delta in deltas:
                if delta["department"] == row[0]:
                    row = _adjust_tuple(row, delta["gp_delta"], delta["covered_delta"])
            dept_rows.append(row)
    def dept_key(row):
        if sort == "gp":
            return row[2] or 0
        if sort == "gppct":
            return row[3] if len(row) > 3 and row[3] is not None else -999
        return row[1] or 0
    dept_rows.sort(key=dept_key, reverse=True)
    dept_html_rows = []
    for row in dept_rows:
        gp_pct = row[3] if len(row) > 3 else None
        cov = row[4] if len(row) > 4 else None
        dept_html_rows.append(
            f"<tr><td>{portal.h(row[0])}</td><td class=\"num\">{portal.money(row[1])}</td><td class=\"num\">{portal.money(row[2])}</td>"
            f"<td class=\"num\">{portal.pct_ratio(gp_pct)}</td><td class=\"num\">{portal.pct_ratio(cov)}</td></tr>"
        )
    sort_links = []
    for key, label in (("sales", "Sales"), ("gp", "GP $"), ("gppct", "GP %")):
        cls = " on" if key == sort else ""
        sort_links.append(f'<a class="chip{cls}" href="/portal/departments?period={portal.h(active)}&sort={key}&low={low_mode}">{label}</a>')
    dept_table = '<p class="empty">No department snapshot for this range. This month, last month, last 7 and last 28 come from the nightly file when those periods were uploaded.</p>' if not dept_html_rows else (
        '<div class="tw"><table><thead><tr><th>Department</th><th>Sales inc GST</th><th>GP</th><th>GP%</th><th>Coverage</th></tr></thead><tbody>'
        + "".join(dept_html_rows) + "</tbody></table></div>"
    )
    top_rows = []
    if built:
        top_rows = built["top"]
    elif period:
        ranked = []
        for raw in period.get("top") or []:
            if isinstance(raw, list) and raw:
                row = list(raw)
                delta = by_code.get(str(row[0]))
                if delta:
                    row = _adjust_tuple(row, delta["gp_delta"], delta["covered_delta"])
                    row.append(delta["source"])
                ranked.append(row)
        ranked.sort(key=lambda r: r[2] or 0, reverse=True)
        top_rows = ranked[:20]
    top_html = []
    for row in top_rows:
        pill = f' <span class="pill">Cost override · {portal.h(row[5])}</span>' if len(row) > 5 else ""
        top_html.append(
            f"<tr><td>{portal.h(_tuple_name(names, row[0]))}{pill}<div class=\"mini\">{portal.h(row[0])}</div></td>"
            f"<td class=\"num\">{portal.money(row[1])}</td><td class=\"num\">{portal.money(row[2])}</td>"
            f"<td class=\"num\">{portal.pct_ratio(row[3] if len(row) > 3 else None)}</td></tr>"
        )
    low_html = []
    low_source = built["low"] if built else None
    if low_source is not None:
        for row in low_source:
            gp_pct = row[3] if len(row) > 3 else None
            gp = row[2] or 0
            negative = (gp_pct is not None and gp_pct < 0) or gp < 0
            under = gp_pct is not None and gp_pct < 0.15
            if low_mode == "negative" and not negative:
                continue
            if low_mode == "under15" and not under:
                continue
            pill = ' <span class="pill">Cost override</span>' if len(row) > 4 else ""
            low_html.append(
                f"<tr><td>{portal.h(_tuple_name(names, row[0]))}{pill}<div class=\"mini\">{portal.h(row[0])}</div></td>"
                f"<td class=\"num\">{portal.money(row[1])}</td><td class=\"num\">{portal.money(row[2])}</td>"
                f"<td class=\"num\">{portal.pct_ratio(gp_pct)}</td></tr>"
            )
    elif period:
        for raw in period.get("low") or []:
            if not isinstance(raw, list) or not raw:
                continue
            row = list(raw)
            delta = by_code.get(str(row[0]))
            if delta:
                row = _adjust_tuple(row, delta["gp_delta"], delta["covered_delta"])
            gp_pct = row[3] if len(row) > 3 else None
            gp = row[2] or 0
            negative = (gp_pct is not None and gp_pct < 0) or gp < 0
            under = gp_pct is not None and gp_pct < 0.15
            if low_mode == "negative" and not negative:
                continue
            if low_mode == "under15" and not under:
                continue
            pill = f' <span class="pill">Cost override</span>' if delta else ""
            low_html.append(
                f"<tr><td>{portal.h(_tuple_name(names, row[0]))}{pill}<div class=\"mini\">{portal.h(row[0])}</div></td>"
                f"<td class=\"num\">{portal.money(row[1])}</td><td class=\"num\">{portal.money(row[2])}</td>"
                f"<td class=\"num\">{portal.pct_ratio(gp_pct)}</td></tr>"
            )
    low_links = []
    for key, label in (("under15", "Under 15%"), ("negative", "Negative GP")):
        cls = " on" if key == low_mode else ""
        low_links.append(f'<a class="chip{cls}" href="/portal/departments?period={portal.h(active)}&sort={sort}&low={key}">{label}</a>')
    caption = portal.h((period or {}).get("caption") or "")
    ly = _ly_groups_section(c)
    return f"""<div class="ph"><h1>Departments and products</h1>{portal.through_line(through)}
<p>Weekday bars are average fuel and shop for the loaded days. Department GP uses a cost override when the product archive can re-cost that line. {caption}</p></div>
{error}{portal.period_controls('/portal/departments', active, start, end)}
{override_note(deltas)}
<section class="card" id="weekdays"><h2>Weekday average</h2>{wd_table}</section>
<section class="card" id="departments"><h2>Departments</h2><div class="chips">{''.join(sort_links)}</div>{dept_table}</section>
<section class="card" id="top-products"><h2>Top 20 by GP $</h2>
<div class="tw"><table><thead><tr><th>Product</th><th>Sales</th><th>GP</th><th>GP%</th></tr></thead><tbody>{''.join(top_html) or '<tr><td colspan="4">No top list for this range.</td></tr>'}</tbody></table></div></section>
<section class="card" id="low-margins"><h2>Low margins</h2><div class="chips">{''.join(low_links)}</div>
<div class="tw"><table><thead><tr><th>Product</th><th>Sales</th><th>GP</th><th>GP%</th></tr></thead><tbody>{''.join(low_html) or '<tr><td colspan="4">Nothing in this filter.</td></tr>'}</tbody></table></div></section>
{ly}"""


def _ly_groups_section(c):
    month = november_2025(c)
    if not month:
        return ""
    rows = sorted(month.get("groups") or [], key=lambda r: r.get("salesEx") or 0, reverse=True)
    body = []
    for row in rows[:25]:
        body.append(f"<tr><td>{portal.h(row.get('name'))}</td><td class=\"num\">{_fmt_qty(row.get('qty') or 0)}</td><td class=\"num\">{portal.money(row.get('salesEx'))}</td></tr>")
    return f"""<section class="card" id="groups-2025"><h2>November 2025 groups</h2>
<p>Quantity and sales ex GST for the full month. No 2025 cost or GP.</p>
<div class="tw"><table><thead><tr><th>Group</th><th>Qty</th><th>Sales ex GST</th></tr></thead><tbody>{''.join(body)}</tbody></table></div></section>"""


def _kitchen_items(packed, catalog):
    items = []
    for part in (packed or "").split(";"):
        if not part:
            continue
        bits = part.split(",")
        try:
            idx = int(bits[0])
        except ValueError:
            continue
        if len(bits) == 1 or bits[1] == "":
            qty = 1
        else:
            try:
                qty = float(bits[1])
            except ValueError:
                qty = 1
        sales_cents = None
        if len(bits) > 2 and bits[2] != "":
            try:
                sales_cents = int(bits[2])
            except ValueError:
                sales_cents = None
        if idx < 0 or idx >= len(catalog):
            continue
        code, name, group_i, sell = catalog[idx][:4]
        if sales_cents is None:
            sales_cents = int(round(qty * (sell or 0)))
        items.append({"code": str(code), "name": name, "group": group_i, "qty": qty, "sales_cents": sales_cents, "sell": sell})
    return items


def _kitchen_delta(c, start, end, codes):
    total = Decimal(0)
    covered = Decimal(0)
    marked = []
    for delta in cost_deltas(c, start, end):
        if delta["code"] not in codes:
            continue
        total += delta["gp_delta"]
        covered += delta["covered_delta"]
        marked.append(delta)
    return total, covered, marked


def kitchen_bounds(query, today_d, anchor):
    period = (query.get("period") or "month").strip()
    if period == "today":
        return "today", today_d, today_d
    if period == "yesterday":
        yesterday = today_d - timedelta(days=1)
        return "yesterday", yesterday, yesterday
    if period == "thisweek":
        monday = today_d - timedelta(days=today_d.weekday())
        return "thisweek", monday, today_d
    return portal.period_bounds(period if period else "month", query.get("from"), query.get("to"), anchor)


def _kitchen_flags(c):
    archive = product_archive(c)
    flags = {}
    if not archive:
        return flags
    for product in archive.get("products") or []:
        if isinstance(product, list) and len(product) > 6:
            flags[str(product[0])] = product[6]
    return flags


def _sales_ex_cents(code, sales_cents, flags):
    amount = Decimal(str(sales_cents or 0))
    if flags.get(code) == 0:
        return amount
    return amount / Decimal("1.1")


def _kitchen_controls(active, start, end, extra):
    presets = (
        ("today", "Today"), ("yesterday", "Yesterday"), ("thisweek", "This week"),
        ("last7", "Last 7 days"), ("month", "This month"), ("lastmonth", "Last month"),
    )
    chips = []
    for key, label in presets:
        cls = " on" if key == active else ""
        href = "/portal/kitchen?" + _qs([("period", key)] + extra)
        chips.append(f'<a class="chip{cls}" href="{portal.h(href)}">{label}</a>')
    hidden = "".join(f'<input type="hidden" name="{portal.h(key)}" value="{portal.h(value)}">' for key, value in extra)
    return f"""<form class="period" method="get" action="/portal/kitchen">
<div class="chips">{''.join(chips)}</div>
<div class="row">
<label class="f">From<input type="date" name="from" value="{start.isoformat()}" required></label>
<label class="f">To<input type="date" name="to" value="{end.isoformat()}" required></label>
<input type="hidden" name="period" value="custom">{hidden}
<button class="btn" type="submit">Apply</button>
</div></form>"""


def _item_series(days, catalog, code):
    series = []
    for day in days:
        if len(day) < 7:
            continue
        for item in _kitchen_items(day[6], catalog):
            if item["code"] == code:
                series.append({"date": str(day[0]), "qty": item["qty"], "sales_cents": item["sales_cents"], "hours": day[7] if len(day) > 7 else None})
    return series


def page_kitchen(c, query):
    through = portal.data_through(c)
    anchor = portal.anchor_day(c)
    try:
        active, start, end = kitchen_bounds(query, portal.today(), anchor)
        error = ""
    except ValueError as ex:
        active, start, end = portal.period_bounds("month", None, None, anchor)
        error = f'<p class="alert">{portal.h(str(ex))}</p>'
    kitchen = portal.meta_get(c, "sales").get("kitchen") or {}
    catalog = kitchen.get("catalog") if isinstance(kitchen.get("catalog"), list) else []
    groups = kitchen.get("groups") if isinstance(kitchen.get("groups"), list) else []
    days = [row for row in (kitchen.get("days") or []) if isinstance(row, list) and row and start.isoformat() <= str(row[0]) <= end.isoformat()]
    if not catalog:
        return f"""<div class="ph"><h1>Kitchen</h1>{portal.through_line(through)}
<p>No kitchen pack in the latest sales upload.</p></div>{error}{portal.period_controls('/portal/kitchen', active, start, end)}"""
    codes = {str(row[0]) for row in catalog if isinstance(row, list) and row}
    gp_delta, covered_delta, marked = _kitchen_delta(c, start, end, codes)
    sales = sum(row[1] or 0 for row in days)
    gp = sum(row[2] or 0 for row in days) + float(gp_delta / Decimal(100))
    covered_ex = sum(row[3] or 0 for row in days) + float(covered_delta / Decimal(100))
    shop = sum(row[5] or 0 for row in days)
    gp_pct = (gp / covered_ex) if covered_ex else None
    share = (sales / shop) if shop else None
    comparison = kitchen.get("comparison") if isinstance(kitchen.get("comparison"), dict) else {}
    comp_html = _kitchen_comparison(c, comparison, codes)
    chosen = query.get("day")
    day_dates = [str(row[0]) for row in days]
    if chosen not in day_dates:
        chosen = day_dates[-1] if day_dates else None
    options = "".join(f'<option value="{portal.h(d)}"{" selected" if d == chosen else ""}>{portal.h(portal.fmt_date(d))}</option>' for d in day_dates)
    group_filter = query.get("group") or ""
    dept = query.get("dept") or ""
    item_code = (query.get("item") or "").strip()
    q = (query.get("q") or "").strip().casefold()
    extra = [("from", start.isoformat()), ("to", end.isoformat()), ("dept", dept), ("group", group_filter), ("item", item_code), ("day", chosen or "")]
    group_links = []
    base_pairs = [("period", active), ("from", start.isoformat()), ("to", end.isoformat()), ("day", chosen or ""), ("dept", dept)]
    group_links.append(f'<a class="chip{" on" if not group_filter else ""}" href="{portal.h("/portal/kitchen?" + _qs(base_pairs))}">All groups</a>')
    for i, name in enumerate(groups):
        cls = " on" if str(i) == group_filter else ""
        href = "/portal/kitchen?" + _qs(base_pairs + [("group", i)])
        group_links.append(f'<a class="chip {portal.tone(name)}{cls}" href="{portal.h(href)}">{portal.h(name)}</a>')
    item_rows = []
    day_row = next((row for row in days if str(row[0]) == chosen), None)
    sales_day = portal.one(c.execute("SELECT * FROM portal_sales_days WHERE date=?", (chosen,))) if chosen else None
    if sales_day:
        adjusted, _deltas = adjust_sales_days(c, [sales_day])
        sales_day = adjusted[0]
    if day_row and len(day_row) > 6:
        table = override_table(c)
        for item in _kitchen_items(day_row[6], catalog):
            if q and q not in item["name"].casefold() and q not in item["code"].casefold():
                continue
            if group_filter.isdigit() and int(group_filter) != item["group"]:
                continue
            gname = groups[item["group"]] if isinstance(item["group"], int) and 0 <= item["group"] < len(groups) else ""
            ov = override_on(table, item["code"], chosen)
            pill = f' <span class="pill">Cost override · {portal.h(ov["source"])}</span>' if ov else ""
            item_rows.append(
                f"<tr><td>{portal.h(item['name'])}{pill}<div class=\"mini\">{portal.h(item['code'])} · {portal.h(gname)}</div></td>"
                f"<td class=\"num\">{_fmt_qty(item['qty'])}</td><td class=\"num\">{portal.money(item['sales_cents'] / 100)}</td></tr>"
            )
    day_card = ""
    if sales_day:
        day_card = (
            f"<p>That day’s shop total {portal.money(sales_day.get('total'))}, fuel {portal.money(sales_day.get('fuel'))}, "
            f"shop {portal.money(sales_day.get('shop'))}, shop GP {portal.money(sales_day.get('gp'))}, "
            f"coverage {portal.pct_ratio(sales_day.get('coverage'))}.</p>"
        )
    mark_note = override_note(marked)
    flags = _kitchen_flags(c)
    browse = _kitchen_browse(days, catalog, groups, flags, active, start, end, dept, group_filter, item_code, q)
    hidden_range = (
        f'<input type="hidden" name="period" value="{portal.h(active)}">'
        f'<input type="hidden" name="from" value="{start.isoformat()}">'
        f'<input type="hidden" name="to" value="{end.isoformat()}">'
        f'<input type="hidden" name="dept" value="{portal.h(dept)}">'
        f'<input type="hidden" name="group" value="{portal.h(group_filter)}">'
        f'<input type="hidden" name="item" value="{portal.h(item_code)}">'
    )
    empty_range = '<p class="empty">No kitchen sales in this range.</p>' if not days else ""
    return f"""<div class="ph"><h1>Kitchen</h1>{portal.through_line(through)}
<p>{portal.h(kitchen.get('caption') or '')} Kitchen GP for 2026 uses a cost override when one is effective, otherwise the till cost. The dates below apply to every figure on this page.</p></div>
{error}{_kitchen_controls(active, start, end, [("dept", dept), ("group", group_filter), ("item", item_code), ("day", chosen or "")])}
{mark_note}
<section class="card" id="kitchen-period"><h2>This range</h2>
{empty_range}
<div class="grid g3">
<div class="stat tone-sales"><div class="lbl">Kitchen sales</div><b>{portal.money(sales)}</b><span>{portal.pct_ratio(share)} of shop</span></div>
<div class="stat tone-gp"><div class="lbl">Covered kitchen GP</div><b>{portal.money(gp)}</b><span>{portal.pct_ratio(gp_pct)}</span></div>
<div class="stat tone-days"><div class="lbl">Days</div><b>{len(days)}</b></div>
</div>
{comp_html}</section>
{browse}
<section class="card" id="kitchen-day"><h2>One trading day</h2>
<form class="period" method="get" action="/portal/kitchen">
{hidden_range}
<label class="f">Day<select name="day">{options}</select></label>
<label class="f">Name or code<input name="q" value="{portal.h(query.get('q') or '')}"></label>
<button class="btn" type="submit">Show</button></form>
{day_card}
<div class="chips">{''.join(group_links)}</div>
<div class="tw"><table><thead><tr><th>Item</th><th class="num">Qty</th><th class="num">Sales</th></tr></thead><tbody>{''.join(item_rows) or '<tr><td colspan="3">No items in this range.</td></tr>'}</tbody></table></div>
</section>"""


def _kitchen_browse(days, catalog, groups, flags, active, start, end, dept, group_filter, item_code, needle):
    departments = {}
    for entry in catalog:
        if not isinstance(entry, list) or not entry:
            continue
        code = str(entry[0])
        name = str(entry[1]) if len(entry) > 1 else code
        group_i = entry[2] if len(entry) > 2 and isinstance(entry[2], int) else None
        group_name = groups[group_i] if group_i is not None and 0 <= group_i < len(groups) else "Kitchen"
        department = group_name
        departments.setdefault(department, {}).setdefault(group_name, []).append((code, name, group_i))
    pairs = [("period", active), ("from", start.isoformat()), ("to", end.isoformat())]
    if item_code:
        series = _item_series(days, catalog, item_code)
        qty = sum(row["qty"] for row in series)
        ex = sum((_sales_ex_cents(item_code, row["sales_cents"], flags) for row in series), Decimal(0))
        inc = sum((Decimal(row["sales_cents"]) for row in series), Decimal(0)) / Decimal(100)
        name = next((entry[1] for entry in catalog if str(entry[0]) == item_code), item_code)
        group_name = ""
        for entry in catalog:
            if str(entry[0]) == item_code and len(entry) > 2 and isinstance(entry[2], int) and entry[2] < len(groups):
                group_name = groups[entry[2]]
        peak = max((row["qty"] for row in series), default=0) or 1
        bars = []
        daily = []
        hourly = []
        for row in series:
            height = max(5, min(100, int(round(row["qty"] / peak * 20)) * 5))
            bars.append(f'<i class="h{height}" title="{portal.h(row["date"])}"></i>')
            daily.append(
                f'<tr><td>{portal.h(portal.fmt_date(row["date"]))}</td><td class="num">{_fmt_qty(row["qty"])}</td>'
                f'<td class="num">{portal.money(_dollars(_sales_ex_cents(item_code, row["sales_cents"], flags) / Decimal(100)))}</td></tr>'
            )
            hours = row.get("hours")
            if isinstance(hours, list):
                for hour in hours:
                    if isinstance(hour, list) and len(hour) >= 2:
                        hourly.append(f'<tr><td>{portal.h(row["date"])} {portal.h(hour[0])}</td><td class="num">{portal.h(hour[1])}</td></tr>')
        hour_html = (
            '<div class="tw"><table><thead><tr><th>Hour</th><th class="num">Qty</th></tr></thead><tbody>' + "".join(hourly) + "</tbody></table></div>"
            if hourly else '<p class="empty" id="kitchen-hourly">No hourly breakdown in this nightly file. The kitchen pack is one total per trading day.</p>'
        )
        back = "/portal/kitchen?" + _qs(pairs + [("dept", group_name or dept), ("group", "")])
        return f'''<p><a class="btn ghost" href="{portal.h(back)}">All kitchen groups</a></p>
<section class="card" id="kitchen-item"><h2>{portal.h(name)}</h2>
<p class="help">PLU {portal.h(item_code)} · {portal.h(group_name or "Kitchen")} · {portal.h(portal.fmt_date(start.isoformat()))} – {portal.h(portal.fmt_date(end.isoformat()))}</p>
<div class="grid g2">
<div class="stat tone-shop"><div class="lbl">Qty</div><b>{_fmt_qty(qty)}</b></div>
<div class="stat tone-sales"><div class="lbl">Sales ex GST</div><b>{portal.money(_dollars(ex / Decimal(100)))}</b><span>{portal.money(_dollars(inc))} inc GST</span></div>
</div>
<div class="vchart" role="img" aria-label="Quantity by day">{''.join(bars)}</div>
<div class="tw"><table><thead><tr><th>Date</th><th class="num">Qty</th><th class="num">Sales ex GST</th></tr></thead><tbody>{''.join(daily) or '<tr><td colspan="3">No sales in this range.</td></tr>'}</tbody></table></div>
{hour_html}</section>'''
    if not dept and not group_filter:
        cards = []
        for name in sorted(departments, key=str.casefold):
            href = "/portal/kitchen?" + _qs(pairs + [("dept", name)])
            groups_here = ", ".join(sorted(departments[name], key=str.casefold)[:4])
            count = sum(len(v) for v in departments[name].values())
            cards.append(f'<a class="dept-card {portal.tone(name)}" href="{portal.h(href)}"><b>{portal.h(name)}</b><span>{count} items</span><small>{portal.h(groups_here)}</small></a>')
        return f'<section class="card" id="kitchen-departments"><h2>Kitchen groups</h2><div class="dept-grid">{"".join(cards)}</div></section>'
    chosen_dept = dept or group_filter
    products = []
    pool = departments.get(chosen_dept) or {}
    if not pool and group_filter.isdigit():
        pool = {groups[int(group_filter)]: departments.get(groups[int(group_filter)], {}).get(groups[int(group_filter)], [])}
    entries = []
    for group_name, items in (pool.items() if isinstance(pool, dict) else []):
        if group_filter.isdigit() and groups[int(group_filter)] != group_name:
            continue
        entries.extend((code, name, group_name) for code, name, _gi in items)
    totals = {}
    for day in days:
        if len(day) < 7:
            continue
        for item in _kitchen_items(day[6], catalog):
            bucket = totals.setdefault(item["code"], {"qty": 0, "ex": Decimal(0), "name": item["name"]})
            bucket["qty"] += item["qty"]
            bucket["ex"] += _sales_ex_cents(item["code"], item["sales_cents"], flags)
    rows = []
    for code, name, group_name in entries:
        if needle and needle not in f"{code} {name} {group_name}".casefold():
            continue
        got = totals.get(code) or {"qty": 0, "ex": Decimal(0)}
        rows.append((name, code, group_name, got["qty"], got["ex"]))
    rows.sort(key=lambda row: row[4], reverse=True)
    body = []
    for name, code, group_name, qty, ex in rows:
        href = "/portal/kitchen?" + _qs(pairs + [("dept", chosen_dept), ("item", code)])
        body.append(
            f'<tr><td><a href="{portal.h(href)}">{portal.h(name)}</a><div class="mini">{portal.h(code)}</div></td>'
            f'<td>{portal.h(group_name)}</td><td class="num">{_fmt_qty(qty)}</td><td class="num">{portal.money(_dollars(ex / Decimal(100)))}</td></tr>'
        )
    back = "/portal/kitchen?" + _qs(pairs)
    table = '<p class="empty">No items in this range.</p>' if not body else (
        '<div class="tw"><table><thead><tr><th>Item</th><th>Group</th><th class="num">Qty</th><th class="num">Sales ex GST</th></tr></thead><tbody>'
        + "".join(body) + "</tbody></table></div>"
    )
    return f'''<p><a class="btn ghost" href="{portal.h(back)}">All kitchen groups</a></p>
<section class="card" id="kitchen-group"><h2>{portal.h(chosen_dept)}</h2>{table}</section>'''


def _kitchen_comparison(c, comparison, codes):
    current = comparison.get("current") if isinstance(comparison.get("current"), dict) else None
    previous = comparison.get("previous") if isinstance(comparison.get("previous"), dict) else None
    if not current:
        return ""
    def adjust(block):
        if not block or not block.get("fromDate"):
            return block
        delta, covered_delta, _marked = _kitchen_delta(c, block["fromDate"], block["toDate"], codes)
        out = dict(block)
        out["gp"] = float(Decimal(str(block.get("gp") or 0)) + delta / Decimal(100))
        if block.get("gpPct") and block.get("gp"):
            covered = Decimal(str(block["gp"])) / Decimal(str(block["gpPct"]))
            covered += covered_delta / Decimal(100)
            if covered:
                out["gpPct"] = float(Decimal(str(out["gp"])) / covered)
        return out
    current = adjust(current)
    previous = adjust(previous) if previous else None
    prev = ""
    if previous:
        prev = (
            f"<p>Same dates last month {portal.h(previous.get('fromDate'))} – {portal.h(previous.get('toDate'))}: "
            f"sales {portal.money(previous.get('sales'))}, GP {portal.money(previous.get('gp'))} "
            f"({portal.pct_ratio(previous.get('gpPct'))}), coverage {portal.pct_ratio(previous.get('coverage'))}, "
            f"shop share {portal.pct_ratio(previous.get('shopShare'))}.</p>"
        )
    return (
        f"<div id=\"kitchen-comparison\"><p>Month to date {portal.h(current.get('fromDate'))} – {portal.h(current.get('toDate'))}: "
        f"sales {portal.money(current.get('sales'))}, GP {portal.money(current.get('gp'))} "
        f"({portal.pct_ratio(current.get('gpPct'))}), coverage {portal.pct_ratio(current.get('coverage'))}, "
        f"shop share {portal.pct_ratio(current.get('shopShare'))}.</p>{prev}</div>"
    )


def page_commission(c, query):
    through = portal.data_through(c)
    anchor = portal.anchor_day(c)
    try:
        active, start, end = portal.period_bounds(query.get("period"), query.get("from"), query.get("to"), anchor)
        error = ""
    except ValueError as ex:
        active, start, end = portal.period_bounds("month", None, None, anchor)
        error = f'<p class="alert">{portal.h(str(ex))}</p>'
    header = (portal.meta_get(c, "sales").get("commission") or {})
    days = portal.commission_between(c, start, end)
    amount = sum(row["amount"] or 0 for row in days)
    target = header.get("target")
    october = header.get("october") or {}
    progress = ""
    if target and header.get("amount") is not None:
        width = portal.bar_class(float(header["amount"]) / float(target) * 100)
        progress = f"""<div class="target"><div class="lbl">{portal.h(header.get('month') or 'This month')}</div>
<b>{portal.money(header.get('amount'))}</b> <span>of {portal.money(target)}</span>
<div class="bar"><i class="{width}"></i></div>
<p>Flat daily plan {portal.money(header.get('planDaily'))}. Remaining {portal.money(header.get('remaining'))}.
Needed {portal.money(header.get('neededDaily'))} per remaining day
({portal.h(header.get('daysRemaining'))} days, {portal.h(header.get('remainingFrom') or '')} to {portal.h(header.get('remainingTo') or '')}).</p></div>"""
    missing = header.get("missingDates") or []
    pending = header.get("pendingDates") or []
    grade_missing = header.get("gradeDetailMissingDates") or []
    def date_list(values, empty):
        if not values:
            return empty
        return ", ".join(portal.h(portal.fmt_date(v)) for v in values)
    body = []
    for row in days:
        body.append(
            "<tr><td>" + portal.h(portal.fmt_date(row["date"])) + "</td><td class=\"num\">" + portal.money(row["amount"])
            + "</td><td>" + portal.h(row.get("status") or "") + "</td><td class=\"num\">" + portal.litres(row.get("ulp_l"))
            + "</td><td class=\"num\">" + (f"{row['ulp_cpl']:.2f}" if row.get("ulp_cpl") is not None else "—")
            + "</td><td class=\"num\">" + portal.litres(row.get("diesel_l"))
            + "</td><td class=\"num\">" + (f"{row['diesel_cpl']:.2f}" if row.get("diesel_cpl") is not None else "—")
            + "</td><td class=\"num\">" + portal.litres(row.get("pulp_l"))
            + "</td><td class=\"num\">" + (f"{row['pulp_cpl']:.2f}" if row.get("pulp_cpl") is not None else "—")
            + "</td></tr>"
        )
    table = '<p class="empty">No commission days in this range.</p>' if not body else (
        '<div class="tw"><table><thead><tr><th>Date</th><th>Amount ex GST</th><th>Status</th><th>ULP L</th><th>ULP cpl</th><th>Diesel L</th><th>Diesel cpl</th><th>P95 L</th><th>P95 cpl</th></tr></thead><tbody>'
        + "".join(body) + "</tbody></table></div>"
    )
    oct = ""
    if october.get("amount") is not None:
        oct = f"<p>October actual {portal.money(october.get('amount'))} of the {portal.money(october.get('target'))} target, {portal.h(october.get('days'))} days.</p>"
    return f"""<div class="ph"><h1>Commission</h1>{portal.through_line(through)}
<p>Workbook fuel commission, ex GST. This is not shop GP. {portal.h(header.get('basis') or '')}</p></div>
{error}{progress}
{portal.period_controls('/portal/commission', active, start, end)}
<section class="card"><h2>{portal.money(amount)} in this range</h2>{table}
<p>Missing dates: {date_list(missing, 'none')}. Pending: {date_list(pending, 'none')}. Grade detail missing: {date_list(grade_missing, 'none')}.</p>
{oct}</section>"""


def page_shifts(c, query):
    through = portal.data_through(c)
    anchor = portal.anchor_day(c)
    try:
        active, start, end = portal.period_bounds(query.get("period"), query.get("from"), query.get("to"), anchor)
        error = ""
    except ValueError as ex:
        active, start, end = portal.period_bounds("month", None, None, anchor)
        error = f'<p class="alert">{portal.h(str(ex))}</p>'
    rows = portal.rows(c.execute(
        "SELECT date, payload FROM portal_shift_days WHERE date>=? AND date<=? ORDER BY date",
        (start.isoformat(), end.isoformat()),
    ))
    view = query.get("view") if query.get("view") in ("cash", "eftpos", "window") else "cash"
    view_links = []
    for key, label in (("cash", "Cash"), ("eftpos", "EFTPOS"), ("window", "Window & commit")):
        cls = " on" if key == view else ""
        view_links.append(f'<a class="chip{cls}" href="/portal/shifts?period={portal.h(active)}&view={key}">{label}</a>')
    if not rows:
        listing = '<p class="empty">No till cash-off days in this range. Staff roster hours stay on Staff hours.</p>'
        inspector = ""
    else:
        body = []
        drive = iou = drops = 0.0
        for row in rows:
            payload = json.loads(row["payload"])
            media = {m["name"].casefold(): m for m in payload.get("media") or []}
            drive += (media.get("drive off") or {}).get("difference") or 0
            iou += (media.get("iou") or {}).get("difference") or 0
            drops += (media.get("cash drop") or {}).get("expected") or 0
            if view == "window":
                body.append(
                    f"<tr><td><a href=\"/portal/shifts?period={portal.h(active)}&view=window&date={portal.h(row['date'])}\">{portal.h(portal.fmt_date(row['date']))}</a></td>"
                    f"<td>{portal.h(payload.get('windowFrom'))}</td><td>{portal.h(payload.get('windowTo'))}</td>"
                    f"<td>{portal.h(payload.get('committed') or 'Not committed')}</td><td>{portal.h(payload.get('txnNo'))}</td></tr>"
                )
            else:
                key = "cash" if view == "cash" else "eftpos"
                media_row = media.get(key) or {}
                note = ""
                counted = media_row.get("counted")
                if view == "cash" and counted == 0 and payload.get("committed"):
                    note = '<span class="pill">Not a cash shortage</span>'
                body.append(
                    f"<tr><td><a href=\"/portal/shifts?period={portal.h(active)}&view={view}&date={portal.h(row['date'])}\">{portal.h(portal.fmt_date(row['date']))}</a></td>"
                    f"<td class=\"num\">{portal.money(media_row.get('expected'))}</td><td class=\"num\">{portal.money(counted)}</td>"
                    f"<td class=\"num\">{portal.money(media_row.get('difference'))}</td><td>{note}</td></tr>"
                )
        head = "<th>Date</th><th>From</th><th>To</th><th>Committed</th><th>Txn</th>" if view == "window" else "<th>Date</th><th>Expected</th><th>Counted</th><th>Difference</th><th></th>"
        listing = f"""<div class="grid g3">
<div class="stat"><div class="lbl">Drive Off still owed</div><b>{portal.money(drive)}</b><span>Not turnover</span></div>
<div class="stat"><div class="lbl">IOU still owed</div><b>{portal.money(iou)}</b><span>Not turnover</span></div>
<div class="stat"><div class="lbl">Cash drops</div><b>{portal.money(drops)}</b></div>
</div>
<div class="tw"><table><thead><tr>{head}</tr></thead><tbody>{''.join(body)}</tbody></table></div>"""
        chosen = query.get("date")
        if chosen not in [r["date"] for r in rows]:
            chosen = rows[-1]["date"]
        payload = json.loads(next(r["payload"] for r in rows if r["date"] == chosen))
        lines = []
        for media_row in payload.get("media") or []:
            note = ""
            if media_row["name"].casefold() == "cash" and media_row.get("counted") == 0 and payload.get("committed"):
                note = "Counted cash is $0 on a committed day. That difference is not a cash shortage."
            if media_row["name"].casefold() in ("drive off", "iou"):
                note = "Still owed, not turnover."
            lines.append(
                f"<tr><td>{portal.h(media_row['name'])}</td><td class=\"num\">{portal.money(media_row.get('expected'))}</td>"
                f"<td class=\"num\">{portal.money(media_row.get('counted'))}</td><td class=\"num\">{portal.money(media_row.get('difference'))}</td>"
                f"<td>{portal.h(note)}</td></tr>"
            )
        others = ", ".join(portal.h(x) for x in payload.get("otherTenders") or [])
        inspector = f"""<section class="card" id="shift-day"><h2>{portal.h(portal.fmt_date(chosen))}</h2>
<p>Window {portal.h(payload.get('windowFrom'))} to {portal.h(payload.get('windowTo'))}. Committed {portal.h(payload.get('committed') or '—')}. Clerk {portal.h(payload.get('clerk'))}. Txn {portal.h(payload.get('txnNo'))}.</p>
<p class="help">{portal.h(payload.get('differenceBasis') or '')} Other tenders: {others or '—'}.</p>
<div class="tw"><table><thead><tr><th>Media</th><th>Expected</th><th>Counted</th><th>Difference</th><th></th></tr></thead><tbody>{''.join(lines)}</tbody></table></div></section>"""
    meta = portal.meta_get(c, "shifts")
    return f"""<div class="ph"><h1>Shift balancing</h1>{portal.through_line(meta.get('asOf') or through)}
<p>Till cash-off for one trading window, roughly 7pm to 7pm Brisbane. This is not the staff roster. Hours for the pay run stay on <a href="/portal/hours">Staff hours</a>. Nothing here is sent to Xero.</p></div>
{error}{portal.period_controls('/portal/shifts', active, start, end)}
<div class="chips">{''.join(view_links)}</div>
<section class="card" id="shift-range">{listing}
<p class="help">Difference is expected minus counted. A committed day with counted cash of $0 is not labelled as a shortage.</p></section>
{inspector}"""


def page_notes(c):
    through = portal.data_through(c)
    meta = portal.meta_get(c, "sales")
    window = meta.get("window") or {}
    older = meta.get("olderMonths") or []
    older_html = "<p>No older monthly totals in this extract. Daily detail starts at the window below.</p>"
    if older:
        older_html = f"<p>{len(older)} older month rows are stored. 2025 rows, when present, are read as quantity and sales ex GST only.</p>"
    packs = []
    for key, label in (
        ("online-orders", "Online orders"),
        ("shift-exceptions", "Till exceptions"),
        ("barcode-map", "Barcode map"),
        ("product-search", "Product archive"),
        ("retail-pricing", "Retail pricing"),
        ("till-price-changes", "Till price changes"),
        ("cost-overrides", "Cost overrides"),
        ("last-year", "November 2025"),
        ("last-year-monthly", "Last-year monthly totals"),
        ("last-year-plu", "Last year comparison"),
        ("shifts", "Shift balancing"),
        ("pay-rates", "Pay rates"),
    ):
        info = portal.meta_get(c, key)
        packs.append(f"<li>{portal.h(label)}: {portal.h(info.get('asOf') or 'not loaded')}</li>")
    return f"""<div class="ph"><h1>Data notes</h1>{portal.through_line(through)}
<p>Australia/Brisbane. Today’s sales are not in until the 9:30pm refresh. Missing days are not filled with zero.</p></div>
<section class="card"><h2>How the figures are built</h2>
<ul>
<li>Shop and fuel totals on the 2026 pages are inc GST. Shop GP is covered sales only. It is not operating profit.</li>
<li>Online orders (Bopple) are extra sales, paid online and not rung through the till. Total sales add the online gross on top of till sales. Shop GP is till GP only. These orders have no cost.</li>
<li>Till exceptions are price overrides, fuel drive-offs, and account charges (IOU and on account). Sending a date again replaces that date, so a repaid charge can move to paid. Paid status for named on-account customers is an estimate.</li>
<li>2026 GP, GP% and cost coverage use a cost override when one is effective on the sale date, and the till cost otherwise. The nightly sales file itself stays on till cost. Do not also rebase that file, or the override would be applied twice.</li>
<li>A product with an override is marked Cost override. A $0 till cost is not warned on 2025, and a 2026 row with an override is not treated as missing a cost.</li>
<li>November 2025 is quantity, sales ex GST, and fuel litres for the whole month. It has no cost, GP, GP% or coverage.</li>
<li>The overview month lines are till only: shop sales inc GST, kitchen food sales inc GST, and fuel litres. Bopple is not included. A past month shows its loaded total. The current month projects the month-end from the daily average, or from the same weekday when that sample exists. Same-month last year comes from the last-year-monthly upload. A blank field there is missing, not zero.</li>
<li>Last year comparison is one calendar month of till sales, inc GST, against the same month last year. This year is the loaded days. Last year is a stored SwiftPOS monthly total. Same days is that total’s daily average, because the file is not day by day. A month that has not been stored says last year not loaded yet and is not shown as zero.</li>
<li>Employee wage costs use the pay-rates upload. The rate follows the day the shift started: weekday, Saturday, Sunday, or a Queensland public holiday. Super is that employee's super rate. Contract hours are typed on Staff hours, day by day, and have no super. They are not in the hours CSV or the staff-hours feed. Tap a name on Staff hours for that person's dates, hours, rates, and cost for the week.</li>
<li>Wages vs sales on the overview is employee wages including super, plus contract costs, divided by till sales excluding GST. Shop and fuel on the daysheet are inc GST, so each is divided by 1.1. A dash means hours, a pay rate, or till sales are missing.</li>
<li>The portal does not pay invoices. Mark as paid only stores a record of a payment already made. It does not set pump prices, or write SwiftPOS prices or costs. Staff hours are not sent to Xero.</li>
</ul>
<p>Daily window: {portal.h(window.get('fromDate') or '—')} · {portal.h(window.get('rule') or '')}</p>
{older_html}
<ul>{''.join(packs)}</ul></section>"""


def purchase_extra(c, active, start, end, bills, suppliers=None):
    prev_start, prev_end = _prev_bounds(active, start, end)
    filtering = suppliers is not None
    clause, extra = portal.supplier_sql(filtering, suppliers or [])
    prev = portal.rows(c.execute(
        "SELECT supplier, inc_gst, amount_due, status FROM portal_bills WHERE bill_date>=? AND bill_date<=?" + clause,
        [prev_start.isoformat(), prev_end.isoformat(), *extra],
    ))
    def due_of(row):
        status = (row["status"] or "").upper()
        if status in ("A", "AUTHORISED", "AUTHORIZED") or ((row["amount_due"] or 0) > 0 and status not in ("P", "PAID")):
            return row["amount_due"] or 0
        return 0
    prev_by = {}
    for row in prev:
        bucket = prev_by.setdefault(row["supplier"], 0.0)
        prev_by[row["supplier"]] = bucket + (row["inc_gst"] or 0)
    this_by = {}
    for row in bills:
        bucket = this_by.setdefault(row["supplier"], {"inc": 0.0, "due": 0.0, "n": 0})
        bucket["inc"] += row["inc_gst"] or 0
        bucket["due"] += due_of(row)
        bucket["n"] += 1
    comp = []
    for name, bucket in sorted(this_by.items(), key=lambda kv: kv[1]["inc"], reverse=True):
        comp.append(
            f"<tr><td>{portal.h(name)}</td><td class=\"num\">{bucket['n']}</td><td class=\"num\">{portal.money(bucket['inc'])}</td>"
            f"<td class=\"num\">{portal.money(prev_by.get(name))}</td><td class=\"num\">{portal.money(bucket['due'])}</td></tr>"
        )
    comp_html = '<p class="empty">No bills in this range.</p>' if not comp else (
        '<div class="tw"><table><thead><tr><th>Supplier</th><th>Bills</th><th>This period inc GST</th><th>Previous period</th><th>Still due</th></tr></thead><tbody>'
        + "".join(comp) + "</tbody></table></div>"
        + f'<p class="help">Previous period is {portal.h(portal.fmt_date(prev_start.isoformat()))} – {portal.h(portal.fmt_date(prev_end.isoformat()))}.</p>'
    )
    bill_rows = []
    for row in sorted(bills, key=lambda r: r["bill_date"], reverse=True)[:300]:
        bill_rows.append(
            f"<tr><td>{portal.h(portal.fmt_date(row['bill_date']))}</td><td>{portal.h(row['supplier'])}</td><td>{portal.h(row['invoice_no'])}</td>"
            f"<td class=\"num\">{portal.money(row['ex_gst'])}</td><td class=\"num\">{portal.money(row['inc_gst'])}</td>"
            f"<td>{portal.h(row['status'] or '')}</td><td class=\"num\">{portal.money(row['amount_due'])}</td></tr>"
        )
    bills_html = '<p class="empty">No bill rows in this range.</p>' if not bill_rows else (
        '<div class="tw"><table><thead><tr><th>Date</th><th>Supplier</th><th>Invoice</th><th>Ex GST</th><th>Inc GST</th><th>Status</th><th>Due</th></tr></thead><tbody>'
        + "".join(bill_rows) + "</tbody></table></div>"
    )
    spend = portal.rows(c.execute(
        "SELECT spend_date, inc_gst, bucket FROM portal_spend WHERE spend_date>=? AND spend_date<=?" + clause,
        [start.isoformat(), end.isoformat(), *extra],
    ))
    shop_rows = portal.rows(c.execute(
        "SELECT date, sales_inc FROM portal_shop_days WHERE date>=? AND date<=? ORDER BY date",
        (start.isoformat(), end.isoformat()),
    ))
    if not shop_rows:
        shop_rows = [{"date": r["date"], "sales_inc": r["shop"]} for r in portal.sales_between(c, start, end)]
    weeks = {}
    for row in bills:
        d = date.fromisoformat(row["bill_date"])
        monday = d - timedelta(days=d.weekday())
        bucket = weeks.setdefault(monday, {"bills": 0.0, "spend": 0.0, "shop": 0.0})
        bucket["bills"] += row["inc_gst"] or 0
    for row in spend:
        if (row.get("bucket") or "shop") != "shop":
            continue
        d = date.fromisoformat(row["spend_date"])
        monday = d - timedelta(days=d.weekday())
        weeks.setdefault(monday, {"bills": 0.0, "spend": 0.0, "shop": 0.0})["spend"] += row["inc_gst"] or 0
    for row in shop_rows:
        d = date.fromisoformat(row["date"])
        monday = d - timedelta(days=d.weekday())
        weeks.setdefault(monday, {"bills": 0.0, "spend": 0.0, "shop": 0.0})["shop"] += row["sales_inc"] or 0
    peak = max([max(v["bills"], v["shop"], v["spend"]) for v in weeks.values()] or [1]) or 1
    week_rows = []
    for monday in sorted(weeks):
        bucket = weeks[monday]
        week_rows.append(
            f"<tr><td>Week of {portal.h(portal.fmt_date(monday.isoformat()))}</td>"
            f"<td class=\"num\">{portal.money(bucket['bills'])}<div class=\"bar\"><i class=\"{portal.bar_class(bucket['bills'] / peak * 100)}\"></i></div></td>"
            f"<td class=\"num\">{portal.money(bucket['spend'])}</td>"
            f"<td class=\"num\">{portal.money(bucket['shop'])}<div class=\"bar\"><i class=\"shop {portal.bar_class(bucket['shop'] / peak * 100)}\"></i></div></td></tr>"
        )
    chart = '<p class="empty">No weekly rows in this range.</p>' if not week_rows else (
        '<div class="tw"><table><thead><tr><th>Week</th><th>Bills inc GST</th><th>Shop spend inc GST</th><th>Shop sales inc GST</th></tr></thead><tbody>'
        + "".join(week_rows) + "</tbody></table></div>"
    )
    return f"""<section class="card" id="purchase-compare"><h2>Against the previous period</h2>{comp_html}</section>
<section class="card" id="purchase-bills"><h2>Bills in this range</h2>{bills_html}</section>
<section class="card" id="purchase-weeks"><h2>Weekly purchases and shop sales</h2>
<p class="help">Bills are supplier invoices. Shop spend is the shop bucket from the purchases file. Fuel direct debits stay out of the bill list.{" Shop sales in this table are the whole shop." if filtering else ""}</p>{chart}</section>"""


def fuel_sections(c, days):
    meta = portal.meta_get(c, "fuel")
    mtd = meta.get("mtd") if isinstance(meta.get("mtd"), dict) else {}
    missing = meta.get("missingCosts") if isinstance(meta.get("missingCosts"), list) else []
    mtd_html = ""
    if mtd.get("amount") is not None:
        status = mtd.get("status") or ""
        mtd_html = f"""<section class="card" id="fuel-mtd"><h2>Month to date vs {portal.money(mtd.get('target'))}</h2>
<p>{portal.money(mtd.get('amount'))} through {portal.h(mtd.get('through') or '')}, {portal.h(mtd.get('days'))} days.
{portal.h(status)}. Ahead/behind {portal.money(mtd.get('aheadBy'))}. At the current average the month finishes near {portal.money(mtd.get('projectedAtCurrentAvg'))}.
Needed {portal.money(mtd.get('neededPerDay'))} on each of the {portal.h(mtd.get('remainingDays'))} days left to cover {portal.money(mtd.get('remaining'))}.</p></section>"""
    missing_html = "none" if not missing else ", ".join(portal.h(str(x)) for x in missing)
    grade_rows = []
    for row in days:
        try:
            grades = json.loads(row["detail"]) if row.get("detail") else []
        except json.JSONDecodeError:
            grades = []
        if not isinstance(grades, list):
            continue
        for grade in grades:
            if not isinstance(grade, dict):
                continue
            grade_rows.append(
                "<tr><td>" + portal.h(portal.fmt_date(row["date"])) + "</td><td>" + portal.h(grade.get("label") or grade.get("grade"))
                + "</td><td class=\"num\">" + portal.litres(grade.get("litres")) + "</td><td class=\"num\">" + portal.money(grade.get("salesInc"))
                + "</td><td class=\"num\">" + portal.money(grade.get("avgSell")) + "</td><td class=\"num\">" + portal.money(grade.get("cost"))
                + "</td><td class=\"num\">" + (f"{grade['marginCplInc']:.2f}" if isinstance(grade.get("marginCplInc"), (int, float)) else "—")
                + "</td><td class=\"num\">" + portal.money(grade.get("commEx")) + "</td></tr>"
            )
    grades_html = '<p class="empty">No grade detail in this range.</p>' if not grade_rows else (
        '<div class="tw"><table><thead><tr><th>Date</th><th>Grade</th><th>Litres</th><th>Sales inc</th><th>Avg sell</th><th>Delivered cost ex</th><th>Margin cpl inc</th><th>Commission ex</th></tr></thead><tbody>'
        + "".join(grade_rows) + "</tbody></table></div>"
    )
    tomorrow = meta.get("tomorrow") if isinstance(meta.get("tomorrow"), dict) else None
    tomorrow_html = ""
    if tomorrow:
        bits = []
        for grade in tomorrow.get("grades") or []:
            if not isinstance(grade, dict):
                continue
            bits.append(
                "<tr><td>" + portal.h(grade.get("label") or grade.get("grade")) + "</td><td class=\"num\">" + portal.money(grade.get("cost"))
                + "</td><td class=\"num\">" + portal.money(grade.get("prevCost")) + "</td><td class=\"num\">"
                + (f"{grade['changeCpl']:.2f}" if isinstance(grade.get("changeCpl"), (int, float)) else "—")
                + "</td><td class=\"num\">" + portal.h(grade.get("boardCpl")) + "</td><td class=\"num\">"
                + (f"{grade['marginCplInc']:.2f}" if isinstance(grade.get("marginCplInc"), (int, float)) else "—")
                + "</td><td class=\"num\">" + portal.money(grade.get("estCommEx")) + "</td></tr>"
            )
        tomorrow_html = f"""<section class="card" id="fuel-tomorrow"><h2>Next delivered cost · {portal.h(portal.fmt_date(tomorrow['date']) if tomorrow.get('date') else '')}</h2>
<p class="help">For staff to key. The portal does not read or set pump prices.</p>
<div class="tw"><table><thead><tr><th>Grade</th><th>Cost ex / L</th><th>Previous cost</th><th>Change cpl</th><th>Board cpl</th><th>Margin cpl inc</th><th>Est. commission ex</th></tr></thead><tbody>{''.join(bits)}</tbody></table></div></section>"""
    return mtd_html + f"""<section class="card" id="fuel-grades"><h2>Price and delivered cost</h2>
<p>Missing delivered-cost days: {missing_html}.</p>{grades_html}</section>{tomorrow_html}"""


def dips_range(c, active, start, end):
    rows = portal.rows(c.execute(
        "SELECT date, payload FROM portal_dip_days WHERE date>=? AND date<=? ORDER BY date",
        (start.isoformat(), end.isoformat()),
    ))
    if not rows:
        return '<section class="card"><p class="empty">No dips in this range.</p></section>'
    body = []
    for row in rows:
        payload = json.loads(row["payload"])
        pumps_by_grade = {}
        for pump in payload.get("pumps") or []:
            grade = pump.get("grade") or ""
            pumps_by_grade[grade] = pumps_by_grade.get(grade, 0) + (pump.get("litresMoved") or 0)
        for tank in payload.get("tanks") or []:
            grade = tank.get("grade") or ""
            flags = []
            if tank.get("deliveryDetected"):
                flags.append("Inferred inflow")
            if tank.get("low"):
                flags.append("Low")
            body.append(
                "<tr><td>" + portal.h(portal.fmt_date(row["date"])) + "</td><td>" + portal.h(tank.get("tank"))
                + "</td><td>" + portal.h(grade) + "</td><td class=\"num\">" + portal.litres(tank.get("litres"))
                + "</td><td class=\"num\">" + portal.pct_points(tank.get("percentFull"))
                + "</td><td class=\"num\">" + portal.litres(pumps_by_grade.get(grade))
                + "</td><td class=\"num\">" + portal.litres(tank.get("variance"))
                + "</td><td>" + portal.h(", ".join(flags)) + "</td></tr>"
            )
    table = '<div class="tw"><table><thead><tr><th>Date</th><th>Tank</th><th>Grade</th><th>Level</th><th>Full</th><th>Pump litres</th><th>Variance</th><th></th></tr></thead><tbody>' + "".join(body) + "</tbody></table></div>"
    return f"""<section class="card" id="dip-range"><h2>Range</h2>
<p class="help">Pump litres are the hoses on that grade. Variance is the tank figure from the dip file. Inferred inflow is not a confirmed delivery. The portal does not read or set pump prices.</p>
{table}</section>"""
