"""Till sales against purchases, one row per portal category.

Department sales are SaleType 0, the same basis as the till daysheet. They
arrive as sales-by-department. Master groups and inventory groups are filed
with config/till_category_map.json. Purchases are the By category split for
the same dates and supplier filter.

United fuel direct debits are not in the Purchases total, so the Fuel row
does not treat them as a cost.
"""
import json
from decimal import Decimal
from pathlib import Path

import portal
import portal_categories

MAP_PATH = Path(__file__).resolve().parent / "config" / "till_category_map.json"
BASIS = "SaleType 0"


def _maps():
    data = json.loads(MAP_PATH.read_text())
    mapping = data["mapping"]
    masters = {}
    names = {}
    for key, row in (mapping.get("masterGroups") or {}).items():
        portal_name = row.get("portal")
        if portal_name not in portal_categories.CATEGORIES:
            continue
        masters[str(key)] = portal_name
        label = portal_categories._fold(row.get("name") or "")
        if label:
            names[label] = portal_name
    groups = {}
    for key, row in (mapping.get("groupOverrides") or {}).items():
        portal_name = row.get("portal")
        if portal_name not in portal_categories.CATEGORIES:
            continue
        groups[str(key)] = portal_name
    for label, portal_name in (mapping.get("names") or {}).items():
        if portal_name in portal_categories.CATEGORIES:
            names[portal_categories._fold(label)] = portal_name
    return masters, groups, names


def category_for(row):
    """Portal category for one till row. Unknown groups stay in Other so the dollars are kept."""
    masters, groups, names = _maps()
    explicit = row.get("category")
    if explicit not in (None, ""):
        if explicit not in portal_categories.CATEGORIES:
            raise ValueError("category must be a portal category")
        return explicit
    group = str(row.get("group") or row.get("groupId") or "").strip()
    master = str(row.get("masterGroup") or row.get("masterGroupId") or "").strip()
    label = portal_categories._fold(row.get("name") or row.get("masterGroupName") or "")
    if group and group in groups:
        return groups[group]
    if master and master in masters:
        return masters[master]
    if label and label in names:
        return names[label]
    if group or master or label:
        return "Other"
    raise ValueError("a sales row needs a category, masterGroup, or group")


def _money(value, label, required=True):
    number = portal.num(value, label, required=required)
    if number is None:
        return None
    return float(portal._cents(number))


def _day_rows(raw):
    """Accept days[].rows or a flat rows list. Each date replaces that date."""
    if isinstance(raw.get("days"), list):
        found = []
        for index, day in enumerate(raw["days"]):
            if not isinstance(day, dict):
                raise ValueError(f"days[{index}] must be an object")
            sale_date = portal.need_date(day.get("date"), f"days[{index}] date")
            rows = day.get("rows")
            if not isinstance(rows, list):
                raise ValueError(f"days[{index}] needs rows")
            found.append((sale_date, rows, index))
        return found
    if isinstance(raw.get("rows"), list):
        grouped = {}
        order = []
        for index, row in enumerate(raw["rows"]):
            if not isinstance(row, dict):
                raise ValueError(f"rows[{index}] must be an object")
            sale_date = portal.need_date(row.get("date"), f"rows[{index}] date")
            if sale_date not in grouped:
                order.append(sale_date)
                grouped[sale_date] = []
            grouped[sale_date].append(row)
        return [(sale_date, grouped[sale_date], 0) for sale_date in order]
    raise ValueError("sales-by-department needs days or rows")


def apply_sales_by_department(c, data):
    """Replace the listed dates. Other dates stay. SaleType 0 only."""
    if not isinstance(data, dict):
        raise ValueError("sales-by-department must be an object")
    basis = str(data.get("basis") or BASIS)
    if "saletype 0" not in basis.casefold().replace("-", " "):
        raise ValueError("sales-by-department must be SaleType 0")
    packed = _day_rows(data)
    if not packed:
        raise ValueError("sales-by-department needs at least one day")
    if len(packed) > 400:
        raise ValueError("sales-by-department has too many days")
    as_of = portal.need_date(data.get("asOf") or packed[-1][0], "asOf")
    stored_rows = 0
    for sale_date, rows, index in packed:
        if len(rows) > 400:
            raise ValueError(f"days[{index}] has too many rows")
        buckets = {}
        for row_i, row in enumerate(rows):
            if not isinstance(row, dict):
                raise ValueError(f"days[{index}] rows[{row_i}] must be an object")
            portal.reject_sensitive(
                {"name": str(row.get("name") or row.get("masterGroupName") or "")},
                "sales-by-department",
            )
            category = category_for(row)
            inc = _money(row.get("salesInc", row.get("sales_inc")), f"days[{index}] salesInc")
            ex = _money(row.get("salesEx", row.get("sales_ex")), f"days[{index}] salesEx", required=False)
            bucket = buckets.setdefault(category, {"inc": Decimal("0.00"), "ex": Decimal("0.00"), "ex_known": True})
            bucket["inc"] += portal._cents(inc)
            if ex is None:
                bucket["ex"] += portal._cents(Decimal(str(inc)) / portal.GST_DIVISOR)
            else:
                bucket["ex"] += portal._cents(ex)
        c.execute("DELETE FROM portal_dept_sales WHERE sale_date=?", (sale_date,))
        for category, bucket in buckets.items():
            c.execute(
                """INSERT INTO portal_dept_sales(sale_date, category, sales_inc, sales_ex, source_as_of)
                   VALUES(?,?,?,?,?)""",
                (sale_date, category, float(bucket["inc"]), float(portal._cents(bucket["ex"])), as_of),
            )
            stored_rows += 1
    portal.meta_set(c, "sales-by-department", {"asOf": as_of, "days": len(packed), "basis": BASIS})
    portal.save_snapshot(c, "sales-by-department", as_of, {"asOf": as_of, "basis": BASIS, "days": len(packed)})
    return {"asOf": as_of, "days": len(packed), "rows": stored_rows}


def _dept_between(c, start, end):
    return portal.rows(c.execute(
        """SELECT category, SUM(sales_inc) AS sales_inc, SUM(sales_ex) AS sales_ex
           FROM portal_dept_sales WHERE sale_date>=? AND sale_date<=? GROUP BY category""",
        (start.isoformat(), end.isoformat()),
    ))


def _pct(purchases, sales):
    if not sales:
        return None
    return float(purchases / sales * 100)


def page(c, start, end, bills, filtering):
    overrides = portal_categories.load_overrides(c)
    items = portal_categories.load_item_overrides(c)
    parts, _rows, purchase_total, _missing = portal_categories._bill_parts(bills, overrides, items)
    sales_rows = portal.sales_between(c, start, end)
    till_inc = portal._cents(sum(float(row["total"] or 0) for row in sales_rows))
    fuel_inc = portal._cents(sum(float(row["fuel"] or 0) for row in sales_rows))
    dept = {row["category"]: row for row in _dept_between(c, start, end)}
    loaded = bool(dept)
    fuel_from_till = loaded and "Fuel" not in dept and fuel_inc != 0
    shown = {}
    if loaded:
        for name in portal_categories.CATEGORIES:
            row = dept.get(name)
            if name == "Fuel" and fuel_from_till:
                shown[name] = {"inc": fuel_inc, "ex": portal._cents(fuel_inc / portal.GST_DIVISOR), "from_till": True}
            elif row:
                shown[name] = {
                    "inc": portal._cents(row["sales_inc"] or 0),
                    "ex": portal._cents(row["sales_ex"] or 0),
                    "from_till": False,
                }
            else:
                shown[name] = {"inc": Decimal("0.00"), "ex": Decimal("0.00"), "from_till": False}
        filed = sum((item["inc"] for item in shown.values()), Decimal("0.00"))
        remainder = portal._cents(till_inc - filed)
    else:
        remainder = Decimal("0.00")
    body = []
    cards = []
    for name in portal_categories.CATEGORIES:
        purchases = parts.get(name, Decimal("0.00"))
        sales = shown.get(name)
        if loaded:
            sales_inc = sales["inc"]
            sales_ex = sales["ex"]
            gap = portal._cents(sales_inc - purchases)
            pct = _pct(purchases, sales_inc)
            sales_ex_txt = portal.money(sales_ex)
            sales_inc_txt = portal.money(sales_inc)
            gap_txt = portal.money(gap)
            pct_txt = "—" if pct is None else f"{pct:.1f}%"
        else:
            sales_ex_txt = sales_inc_txt = gap_txt = pct_txt = "—"
        note = ""
        if name == "Fuel":
            note = (
                '<div class="mini vs-note">Till fuel sales. United fuel direct debits are not in the Purchases total, '
                "so this is not a fuel cost.</div>"
            )
            if loaded and fuel_from_till:
                note += '<div class="mini">Fuel sales here are the daysheet, because this range has no Fuel row in the department file.</div>'
        body.append(
            f"<tr{' id=\"vs-fuel\"' if name == 'Fuel' else ''}>"
            f"<td>{portal.h(name)}{note}</td>"
            f"<td class=\"num\">{sales_ex_txt}</td><td class=\"num\">{sales_inc_txt}</td>"
            f"<td class=\"num\">{portal.money(purchases)}</td>"
            f"<td class=\"num\">{gap_txt}</td><td class=\"num\">{pct_txt}</td></tr>"
        )
        cards.append(
            f'<article class="invoice-card"><h3>{portal.h(name)}</h3>{note}'
            f'<span>Sales ex {sales_ex_txt}</span><span>Sales inc {sales_inc_txt}</span>'
            f'<span>Purchases {portal.money(purchases)}</span>'
            f'<span>Gap {gap_txt}</span><span>Purchases {pct_txt} of sales</span></article>'
        )
    extra = ""
    if loaded and remainder != 0:
        extra = (
            f'<tr id="vs-unallocated"><td>Not in a department</td>'
            f'<td class="num">—</td><td class="num">{portal.money(remainder)}</td>'
            f'<td class="num">{portal.money(0)}</td><td class="num">{portal.money(remainder)}</td><td class="num">—</td></tr>'
        )
        cards.append(
            f'<article class="invoice-card" id="vs-unallocated-card"><h3>Not in a department</h3>'
            f'<span>Sales inc {portal.money(remainder)}</span>'
            f'<span>Kept so the sales total matches the till daysheet.</span></article>'
        )
    if loaded:
        total_sales = till_inc
        total_gap = portal._cents(total_sales - purchase_total)
        total_pct = _pct(purchase_total, total_sales)
        total_pct_txt = "—" if total_pct is None else f"{total_pct:.1f}%"
        sales_ex_total = sum((item["ex"] for item in shown.values()), Decimal("0.00"))
        if remainder == 0:
            total_ex = portal.money(sales_ex_total)
        else:
            total_ex = "—"
        total_row = (
            f'<tr class="total" id="vs-total" data-sales="{format(total_sales, "f")}" '
            f'data-purchases="{format(purchase_total, "f")}">'
            f"<td>Total</td><td class=\"num\">{total_ex}</td><td class=\"num\">{portal.money(total_sales)}</td>"
            f"<td class=\"num\">{portal.money(purchase_total)}</td>"
            f"<td class=\"num\">{portal.money(total_gap)}</td><td class=\"num\">{total_pct_txt}</td></tr>"
        )
    else:
        total_row = (
            f'<tr class="total" id="vs-total" data-sales="{format(till_inc, "f")}" '
            f'data-purchases="{format(purchase_total, "f")}">'
            f"<td>Total</td><td class=\"num\">—</td><td class=\"num\">{portal.money(till_inc)}</td>"
            f"<td class=\"num\">{portal.money(purchase_total)}</td><td class=\"num\">—</td><td class=\"num\">—</td></tr>"
        )
    empty = ""
    if not loaded:
        empty = '<p class="empty" id="dept-sales-empty">No department sales loaded yet.</p>'
    supplier_note = ""
    if filtering:
        supplier_note = (
            '<p class="help" id="vs-supplier-note">Till sales stay the whole till. '
            "The supplier filter changes the purchases column only.</p>"
        )
    help_text = (
        '<p class="help" id="vs-basis">Till sales are SaleType 0, the same basis as the daysheet. '
        "Sales ex GST is the figure from the department file, or the inc-GST amount divided by 1.1 when that file omits it. "
        "The gap is sales inc GST minus purchases inc GST. "
        "The total sales figure is the till daysheet for these dates. "
        "The total purchases figure is the Purchases total for these dates and suppliers. "
        "A department file that does not add up to the daysheet shows the remainder as not in a department.</p>"
    )
    table = (
        '<div class="tw vs-table"><table><thead><tr><th>Department</th><th>Sales ex GST</th><th>Sales inc GST</th>'
        '<th>Purchases</th><th>Gap</th><th>Purchases % of sales</th></tr></thead><tbody>'
        + "".join(body) + extra + total_row + "</tbody></table></div>"
    )
    return (
        f'<section class="card" id="sales-vs">{empty}{supplier_note}{help_text}'
        f'{table}<div class="vs-cards">{"".join(cards)}</div></section>'
    )
