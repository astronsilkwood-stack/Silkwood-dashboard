"""Purchase categories for the owner portal.

Edit the tables in this file to change how a supplier or a bill line is filed.
A category saved on the portal replaces these rules for that supplier and is
kept when the nightly purchases file is posted again.

A bill with no line detail uses the supplier rules. Campbells is left in Other
until a line description (or a saved category) says otherwise, because that
supplier sells both tobacco and grocery. The Distributors is Drinks; a
confectionery line moves when the invoice lists it.

United Petroleum fuel direct debits are not supplier bills. They stay out of
the bill total, same as the Purchases page. The Fuel category shows them beside
the bills, and the rent note follows the Profit & Loss rule.
"""
import json
import re
from datetime import timedelta
from decimal import Decimal

import portal

CATEGORIES = (
    "Kitchen",
    "Drinks",
    "Tobacco",
    "Confectionery/Snacks",
    "Dairy/Grocery",
    "Fuel",
    "Other",
)

# First matching phrase wins. Matching ignores case, hyphens, and extra spaces.
SUPPLIER_RULES = (
    ("Tobacco", (
        "smokemart",
        "smgb",
        "one distribution",
        "smokestix",
        "smoke stix",
        "cignal",
    )),
    ("Drinks", (
        "coca cola",
        "coke",
        "pepsi",
        "schweppes",
        # The Distributors Cairns / Queensland is mostly drinks. Confectionery
        # lines are split out when the bill has descriptions.
        "the distributors",
        "bells pure ice",
        "pure ice",
    )),
    ("Dairy/Grocery", (
        "bega",
        "lactalis",
        "parmalat",
        "peters icecream",
        "peters ice cream",
        "peters",
    )),
    ("Kitchen", (
        "bidfood",
        "tully pies",
        "woolworths",
        "woolworth",
        "coles",
        "yamagishi",
        "max stocks",
        "crunchy fried chicken",
        "mjchickens",
        "mj chickens",
        "2ot chillies",
    )),
    ("Fuel", (
        "united petroleum",
        "united",
        "motorpass",
    )),
    # Named here so a later phrase cannot pull a utility or a fee into food.
    ("Other", (
        "elgas",
        "telstra",
        "origin energy",
        "origin",
        "rentokil",
        "news corp",
        "zoho",
        "xero",
        "take app",
        "island coast",
        "waste",
        "valvoline",
        "tweed bait",
        "woodham",
        "bpi trading",
        "venkatesh",
        "ss distribution",
        "ipl",
        "bite",
    )),
)

# Sells more than one category. Without line descriptions the whole bill stays
# in Other, so tobacco and grocery are not added together.
MIXED_SUPPLIERS = ("campbell",)

# Checked against the Xero account name and account code only.
ACCOUNT_RULES = (
    ("Fuel", (r"\bfuel\b",)),
)

# Checked against the line description. First match wins.
LINE_RULES = (
    ("Tobacco", (
        "tobacco", "cigarette", "cigar", "smokemart", "winfield", "peter jackson",
        "benson", "horizon", "alpine", "longbeach", "long beach", "bond street",
        "marlboro",
    )),
    ("Drinks", (
        "coca", "coke", "pepsi", "schweppes", "soft drink", "mineral water",
        "spring water", "juice", "cordial", "energy drink", "powerade", "gatorade",
        "sprite", "fanta", "mount franklin", "iced coffee", "milk drink", "milkshake",
    )),
    ("Dairy/Grocery", (
        "milk", "cheese", "yoghurt", "yogurt", "butter", "dairy", "cream",
        "parmalat", "bega", "grocery", "ice cream",
    )),
    ("Confectionery/Snacks", (
        "confection", "chocolate", "snack", "lolly", "lollies", "candy",
        "chewing gum", "crisps", "potato chips", "cadbury", "kitkat", "kit kat",
    )),
    ("Kitchen", (
        "pie", "meat", "chicken", "egg", "flour", "cooking oil", "frozen",
        "bacon", "sausage", "vegetable", "produce", "kitchen",
    )),
    ("Fuel", (
        "diesel", "unleaded", "petroleum", "adblue", "ulp",
    )),
)


def supplier_key(name):
    return _fold(name)


def _fold(name):
    text = str(name or "").casefold().replace("&", " and ").replace("-", " ").replace("/", " ")
    return " ".join(text.split())


def _hit(text, phrase):
    if not phrase:
        return False
    if " " in phrase or not phrase.isalpha():
        return phrase in text
    return re.search(rf"\b{re.escape(phrase)}\b", text) is not None


def _first_rule(text, rules):
    folded = _fold(text)
    if not folded:
        return ""
    for category, phrases in rules:
        for phrase in phrases:
            if phrase.startswith("\\"):
                if re.search(phrase, folded):
                    return category
            elif _hit(folded, phrase):
                return category
    return ""


def supplier_category(name):
    """Category from the supplier name alone. Mixed suppliers stay in Other."""
    folded = _fold(name)
    for phrase in MIXED_SUPPLIERS:
        if _hit(folded, phrase):
            return "Other"
    return _first_rule(folded, SUPPLIER_RULES) or "Other"


def line_category(line):
    account = " ".join(str(line.get(key) or "") for key in ("accountName", "accountCode"))
    found = _first_rule(account, ACCOUNT_RULES)
    if found:
        return found
    return _first_rule(line.get("description") or "", LINE_RULES)


def _close(left, right):
    left, right = portal._cents(left), portal._cents(right)
    diff = abs(left - right)
    if diff <= Decimal("0.05"):
        return True
    base = abs(right) if right != 0 else abs(left)
    if base == 0:
        return True
    return diff / base <= Decimal("0.02")


def _partition(amount, weights):
    """Split amount across weights. The parts add up to amount exactly."""
    amount = portal._cents(amount)
    cents = int((amount * 100).to_integral_value())
    total_w = sum(weights.values(), Decimal("0"))
    names = [key for key, value in weights.items() if value != 0]
    if not names or total_w == 0:
        return {}
    used = 0
    out = {}
    for index, name in enumerate(names):
        if index == len(names) - 1:
            part = cents - used
        else:
            part = int((Decimal(cents) * weights[name] / total_w).to_integral_value())
            used += part
        out[name] = Decimal(part) / Decimal(100)
    return {key: value for key, value in out.items() if value != 0}


def classify_bill(supplier, lines, amount, override=None):
    """Map one bill's inc-GST total onto categories. The parts add up to the bill."""
    amount = portal._cents(amount or 0)
    if override:
        return {override: amount} if amount != 0 else {}
    weights = {}
    known = Decimal("0")
    for line in lines or []:
        if not isinstance(line, dict) or line.get("inc") is None:
            continue
        inc = portal._cents(line["inc"])
        if inc == 0:
            continue
        category = line_category(line) or supplier_category(supplier)
        weights[category] = weights.get(category, Decimal("0")) + inc
        known += inc
    if known == 0:
        category = supplier_category(supplier)
        return {category: amount} if amount != 0 else {}
    ex_gst = portal._cents(amount / portal.GST_DIVISOR) if amount else Decimal("0")
    if _close(known, amount) or _close(known, ex_gst) or abs(known) > abs(amount):
        return _partition(amount, weights)
    part = _partition(known, weights)
    rest = amount - sum(part.values(), Decimal("0"))
    if rest != 0:
        default = supplier_category(supplier)
        part[default] = part.get(default, Decimal("0")) + rest
    return {key: value for key, value in part.items() if value != 0}


def _line_dict(description, account_code, account_name, inc):
    if inc is None and not description and not account_code and not account_name:
        return None
    item = {
        "description": " ".join(str(description or "").split())[:200],
        "accountCode": " ".join(str(account_code or "").split())[:80],
        "accountName": " ".join(str(account_name or "").split())[:120],
    }
    if inc is not None:
        item["inc"] = float(portal._cents(inc))
    portal.reject_sensitive(item["description"], "purchases")
    portal.reject_sensitive(item["accountCode"], "purchases")
    portal.reject_sensitive(item["accountName"], "purchases")
    if not item["description"] and not item["accountCode"] and not item["accountName"] and "inc" not in item:
        return None
    return item


def _parse_line(raw):
    if isinstance(raw, dict):
        description = raw.get("description") or raw.get("desc") or raw.get("item") or raw.get("name") or ""
        account_code = raw.get("accountCode") or raw.get("account_code") or raw.get("code") or ""
        account_name = raw.get("accountName") or raw.get("account_name") or ""
        inc = raw.get("incGst", raw.get("inc", raw.get("amount")))
        if inc in ("", None):
            ex = raw.get("exGst", raw.get("ex"))
            inc = None if ex in ("", None) else portal.num(ex, "line ex GST", required=False)
            if inc is not None:
                inc = float(portal._cents(Decimal(str(inc)) * portal.GST_DIVISOR))
        else:
            inc = portal.num(inc, "line inc GST", required=False)
        return _line_dict(description, account_code, account_name, inc)
    if isinstance(raw, list) and raw:
        description = raw[0] if len(raw) > 0 else ""
        account_code = raw[1] if len(raw) > 1 else ""
        account_name = ""
        inc = None
        if len(raw) >= 5:
            account_name = raw[2]
            inc = raw[4]
        elif len(raw) >= 4:
            inc = raw[3]
        elif len(raw) >= 3:
            inc = raw[2]
        if inc not in ("", None):
            inc = portal.num(inc, "line inc GST", required=False)
        return _line_dict(description, account_code, account_name, inc)
    return None


def parse_line_list(raw):
    """Lines already on a bill. A nightly file without lines returns nothing."""
    if not isinstance(raw, list) or not raw:
        return None
    if len(raw) > 400:
        raise ValueError("a bill has too many lines")
    lines = []
    for item in raw:
        parsed = _parse_line(item)
        if parsed:
            lines.append(parsed)
    return lines or None


def lines_payload(bill):
    """JSON for the optional line list on a bill array, or None when it has none."""
    if not isinstance(bill, list) or len(bill) < 11 or not isinstance(bill[10], list):
        return None
    lines = parse_line_list(bill[10])
    if not lines:
        return None
    return json.dumps(lines, separators=(",", ":"), ensure_ascii=False)


def load_lines(raw):
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return []
    return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []


def load_overrides(c):
    found = {}
    for row in portal.rows(c.execute("SELECT supplier_key, category FROM portal_supplier_categories")):
        if row["category"] in CATEGORIES:
            found[row["supplier_key"]] = row["category"]
    return found


def save_override(c, user, supplier, category):
    name = portal.clean_supplier_name(supplier)
    if not name:
        raise ValueError("Choose a supplier.")
    known = portal.named_suppliers(c, ("portal_bills", "portal_spend"))
    match = next((item for item in known if supplier_key(item) == supplier_key(name)), None)
    if not match:
        raise ValueError("That supplier is not in the purchases.")
    key = supplier_key(match)
    if not category:
        c.execute("DELETE FROM portal_supplier_categories WHERE supplier_key=?", (key,))
        return match
    if category not in CATEGORIES:
        raise ValueError("Choose a category.")
    now = portal.stamp()
    c.execute(
        """INSERT INTO portal_supplier_categories(supplier_key, supplier, category, updated_at, updated_by)
           VALUES(?,?,?,?,?)
           ON CONFLICT(supplier_key) DO UPDATE SET supplier=excluded.supplier, category=excluded.category,
             updated_at=excluded.updated_at, updated_by=excluded.updated_by""",
        (key, match, category, now, user["username"]),
    )
    return match


def _bill_parts(bills, overrides):
    parts = {category: Decimal("0.00") for category in CATEGORIES}
    rows = []
    for bill in bills:
        override = overrides.get(supplier_key(bill["supplier"]))
        split = classify_bill(bill["supplier"], load_lines(bill.get("lines")), bill["inc_gst"] or 0, override)
        for category, amount in split.items():
            if category not in parts:
                category = "Other"
            parts[category] += amount
            rows.append({
                "category": category,
                "supplier": bill["supplier"],
                "invoice_no": bill["invoice_no"],
                "bill_date": bill["bill_date"],
                "status": bill["status"] or "",
                "amount_due": bill["amount_due"] or 0,
                "bill_inc": portal._cents(bill["inc_gst"] or 0),
                "inc": amount,
                "saved": bool(override),
            })
    target = portal._cents(sum(float(bill["inc_gst"] or 0) for bill in bills))
    drift = target - sum(parts.values(), Decimal("0"))
    if drift != 0 and parts:
        largest = max(parts, key=lambda key: abs(parts[key]))
        parts[largest] += drift
        for row in reversed(rows):
            if row["category"] == largest:
                row["inc"] += drift
                break
    return parts, rows, target


def _money_attr(amount):
    return format(portal._cents(amount), "f")


def _option_list(selected):
    options = ['<option value="">Automatic</option>']
    for category in CATEGORIES:
        chosen = " selected" if category == selected else ""
        options.append(f'<option value="{portal.h(category)}"{chosen}>{portal.h(category)}</option>')
    return "".join(options)


def _supplier_form(user, supplier, saved, automatic, back):
    if not user:
        return ""
    chosen = saved or ""
    note = "Saved category" if saved else f"Automatic is {automatic}"
    return (
        f'<form method="post" action="/portal/purchases" class="cat-form">'
        f'<input type="hidden" name="csrf" value="{portal.h(user["csrf"])}">'
        f'<input type="hidden" name="action" value="set_category">'
        f'<input type="hidden" name="supplier" value="{portal.h(supplier)}">'
        f'<input type="hidden" name="back" value="{portal.h(back)}">'
        f'<label class="f">Category<select name="category" aria-label="Category for {portal.h(supplier)}">{_option_list(chosen)}</select></label>'
        f'<button class="btn" type="submit">Save</button>'
        f'<span class="mini">{portal.h(note)}. The nightly upload does not change a saved category.</span>'
        f'</form>'
    )


def _bill_table(items):
    if not items:
        return '<p class="empty">No bills in this category for the current filters.</p>'
    body = []
    cards = []
    for item in sorted(items, key=lambda row: (row["bill_date"], row["supplier"], row["invoice_no"]), reverse=True):
        body.append(
            f"<tr><td>{portal.h(portal.fmt_date(item['bill_date']))}</td>"
            f"<td>{portal.h(item['invoice_no'])}</td>"
            f"<td class=\"num\">{portal.money(item['inc'])}</td>"
            f"<td class=\"num\">{portal.money(item['bill_inc'])}</td>"
            f"<td>{portal.h(item['status'])}</td></tr>"
        )
        cards.append(
            f'<article class="invoice-card"><b>{portal.h(item["invoice_no"])}</b>'
            f'<span>{portal.h(portal.fmt_date(item["bill_date"]))} · {portal.h(item["status"] or "—")}</span>'
            f'<span>In this category {portal.money(item["inc"])}</span>'
            f'<span>Bill {portal.money(item["bill_inc"])}</span></article>'
        )
    table = (
        '<div class="tw cat-table"><table><thead><tr><th>Date</th><th>Invoice</th>'
        '<th>In this category</th><th>Bill inc GST</th><th>Status</th></tr></thead><tbody>'
        + "".join(body) + "</tbody></table></div>"
    )
    return table + f'<div class="cat-cards">{"".join(cards)}</div>'


def fuel_debits(c, start, end, filtering, selected):
    clause, params = portal.supplier_sql(filtering, selected)
    return portal.rows(c.execute(
        "SELECT supplier, spend_date, ex_gst, inc_gst FROM portal_spend "
        "WHERE bucket='fuel' AND spend_date>=? AND spend_date<=?" + clause + " ORDER BY spend_date, supplier",
        [start.isoformat(), end.isoformat(), *params],
    ))


def rent_for_range(c, start, end):
    """Rent the Profit & Loss page removes from fuel, for the same dates.

    A month with no Xero report is left out, and a month that already has a
    rent account is left out. That is the same rule as the Profit & Loss page.
    """
    import portal_pnl
    rates = portal_pnl.load_rent_rates(c)
    inc = Decimal("0.00")
    ex = Decimal("0.00")
    days = 0
    day = start
    loaded = {}
    while day <= end:
        month = f"{day.year:04d}-{day.month:02d}"
        if month not in loaded:
            loaded[month] = portal.rows(c.execute(
                "SELECT account_name FROM portal_xero_pnl WHERE month=?", (month,)
            ))
        rows = loaded[month]
        if rows and not portal_pnl.xero_rent_names(rows):
            rate = portal_pnl.rate_for_day(rates, day)
            if rate:
                days += 1
                inc += rate["daily_inc"]
                ex += portal_pnl.daily_ex_gst(rate["daily_inc"])
        day += timedelta(days=1)
    return {"inc": portal._cents(inc), "ex": portal._cents(ex), "days": days}


def _fuel_note(c, start, end, filtering, selected):
    rows = fuel_debits(c, start, end, filtering, selected)
    if not rows:
        return ""
    inc = portal._cents(sum(float(row["inc_gst"] or 0) for row in rows))
    rent = rent_for_range(c, start, end)
    rent_text = (
        f" Profit &amp; Loss takes {portal.money(rent['inc'])} inc GST "
        f"({portal.money(rent['ex'])} ex GST) of rent out of fuel for {rent['days']} "
        f"{'day' if rent['days'] == 1 else 'days'} in this range."
        if rent["days"] else
        " Profit &amp; Loss takes the daily rent out of fuel only for a month whose Xero report is loaded and has no rent account."
    )
    return (
        f'<section class="card" id="fuel-direct-debits"><h2>United fuel direct debits</h2>'
        f'<p>{portal.money(inc)} inc GST in this range. These direct debits are not in the supplier bill total, '
        f'same as the Purchases list. The daily rent is {portal.money(portal_pnl_rate())} inc GST.'
        f'{rent_text}</p></section>'
    )


def portal_pnl_rate():
    import portal_pnl
    return portal_pnl.DEFAULT_RENT_INC


def _fuel_detail(c, start, end, filtering, selected):
    rows = fuel_debits(c, start, end, filtering, selected)
    if not rows:
        return ""
    body = []
    cards = []
    for row in rows:
        body.append(
            f"<tr><td>{portal.h(portal.fmt_date(row['spend_date']))}</td><td>{portal.h(row['supplier'])}</td>"
            f"<td class=\"num\">{portal.money(row['inc_gst'])}</td></tr>"
        )
        cards.append(
            f'<article class="invoice-card"><b>{portal.h(row["supplier"])}</b>'
            f'<span>{portal.h(portal.fmt_date(row["spend_date"]))}</span>'
            f'<span>{portal.money(row["inc_gst"])} inc GST</span></article>'
        )
    table = (
        '<div class="tw cat-table"><table><thead><tr><th>Date</th><th>Supplier</th><th>Inc GST</th></tr></thead><tbody>'
        + "".join(body) + "</tbody></table></div>"
    )
    return (
        '<section class="card" id="fuel-debit-rows"><h2>Direct debit lines</h2>'
        '<p class="help">Not included in the category total above. The bill total on this page stays the supplier invoices.</p>'
        f'{table}<div class="cat-cards">{"".join(cards)}</div></section>'
    )


def category_page(c, user, query, active, start, end, bills, filtering, selected, back):
    overrides = load_overrides(c)
    parts, rows, target = _bill_parts(bills, overrides)
    category = query.get("category") if query.get("category") in CATEGORIES else ""
    fuel = _fuel_note(c, start, end, filtering, selected)
    if category:
        chosen = [row for row in rows if row["category"] == category]
        by_supplier = {}
        for row in chosen:
            bucket = by_supplier.setdefault(row["supplier"], {"inc": Decimal("0.00"), "bills": []})
            bucket["inc"] += row["inc"]
            bucket["bills"].append(row)
        blocks = []
        for name, bucket in sorted(by_supplier.items(), key=lambda item: item[1]["inc"], reverse=True):
            saved = overrides.get(supplier_key(name))
            automatic = supplier_category(name)
            blocks.append(
                f'<section class="card" id="category-supplier-{portal.h(supplier_key(name).replace(" ", "-"))}">'
                f'<h3>{portal.h(name)}</h3>'
                f'<p class="help">{portal.money(bucket["inc"])} inc GST in {portal.h(category)} · {len(bucket["bills"])} '
                f'{"bill" if len(bucket["bills"]) == 1 else "bills"}</p>'
                f'{_supplier_form(user, name, saved, automatic, back)}'
                f'{_bill_table(bucket["bills"])}</section>'
            )
        if not blocks:
            blocks.append('<p class="empty">No supplier bills in this category for the current filters.</p>')
        back_href = _href(active, start, end, selected, filtering, "")
        extra = ""
        if category == "Fuel":
            extra = _fuel_detail(c, start, end, filtering, selected) + fuel
        return (
            f'<section id="purchase-categories"><p><a class="btn ghost" href="{portal.h(back_href)}">All categories</a></p>'
            f'<section class="card"><h2>{portal.h(category)}</h2>'
            f'<p id="category-one" data-inc="{_money_attr(parts[category])}">{portal.money(parts[category])} inc GST. '
            f'The categories together are {portal.money(target)}, the same Purchases total for these filters.</p></section>'
            f'{"".join(blocks)}{extra}</section>'
        )
    cards = []
    for name in CATEGORIES:
        amount = parts[name]
        count = len([row for row in rows if row["category"] == name])
        href = _href(active, start, end, selected, filtering, name)
        cards.append(
            f'<a class="cat-card {portal.tone(name)}" data-category="{portal.h(name)}" data-inc="{_money_attr(amount)}" '
            f'href="{portal.h(href)}"><span class="lbl">{portal.h(name)}</span>'
            f'<b>{portal.money(amount)}</b><small>{count} {"bill" if count == 1 else "bills"}</small></a>'
        )
    return (
        f'<section id="purchase-categories"><div class="cat-grid">{"".join(cards)}</div>'
        f'<p id="category-total" data-total="{_money_attr(target)}">Categories {portal.money(target)} inc GST. '
        f'This is the Purchases total for the same dates and suppliers.</p>'
        f'<p class="help" id="category-help">Kitchen, drinks, tobacco, confectionery, dairy and fuel come from the supplier. '
        f'Campbells stays in Other until you save a category or the bill lines say tobacco or grocery. '
        f'The Distributors is Drinks, and a confectionery line moves when the invoice lists it. '
        f'Open a category to see its suppliers and bills, and to save a different category for a supplier.</p>'
        f'{fuel}</section>'
    )


def _href(active, start, end, selected, filtering, category):
    pairs = [("period", active), ("view", "category")]
    if active == "custom":
        pairs.extend((("from", start.isoformat()), ("to", end.isoformat())))
    if filtering:
        pairs.extend(("supplier", name) for name in selected)
    if category:
        pairs.append(("category", category))
    from urllib.parse import urlencode
    return "/portal/purchases?" + urlencode(pairs)
