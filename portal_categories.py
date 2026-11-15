"""Purchase categories for the owner portal.

Each invoice line is filed from its item code, description, or Xero account.
A category saved on an item (by code, or by description when there is no code)
replaces those rules for that item. The supplier category is only the fallback
for a line that still does not match, and for a bill with no lines.

Freight, GST and rounding lines are not their own category. Their amount is
shared across the other lines on the same bill in proportion to those lines.
A bill that is only freight stays in Other.

Edit the tables in this file to change the automatic rules. Saved categories
are kept when the nightly purchases file is posted again.

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


# A line whose whole description is one of these is freight, GST or rounding.
# Its amount is shared across the other lines on the same bill.
SPREAD_EXACT = {
    "freight", "delivery", "delivery charge", "rounding", "round off", "discount",
    "gst", "tax", "fuel levy",
}


def item_key(line):
    """Remember an item by its code, or by the description when there is no code."""
    code = _fold(line.get("itemCode") or "")
    if code:
        return "code:" + code[:80]
    desc = _fold(line.get("description") or "")
    if desc:
        return "desc:" + desc[:180]
    return ""


def is_spread(line):
    text = _fold(line.get("description") or "")
    if text in SPREAD_EXACT or text.startswith(("freight ", "delivery charge", "rounding ", "discount ")):
        return True
    account = _fold(" ".join(str(line.get(key) or "") for key in ("accountName", "accountCode")))
    return bool(re.search(r"\bfreight\b", account)) or account in {"gst", "tax"}


def line_category(line, supplier, supplier_override=None, item_overrides=None):
    """Return (category, how). how is item, spread, rule, or supplier."""
    key = item_key(line)
    if key and item_overrides and key in item_overrides:
        return item_overrides[key], "item"
    if is_spread(line):
        return "", "spread"
    found = _first_rule(" ".join((line.get("itemCode") or "", line.get("description") or "")), LINE_RULES)
    if not found:
        found = _first_rule(
            " ".join(str(line.get(key) or "") for key in ("accountName", "accountCode")),
            ACCOUNT_RULES,
        )
    if found:
        return found, "rule"
    return (supplier_override or supplier_category(supplier)), "supplier"


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


def _line_inc(line):
    if line.get("inc") is not None:
        return portal._cents(line["inc"])
    if line.get("ex") is not None:
        return portal._cents(Decimal(str(line["ex"])) * portal.GST_DIVISOR)
    return None


def _qty(line):
    qty = line.get("qty")
    if qty is None:
        return None
    number = Decimal(str(qty))
    if number == number.to_integral():
        return int(number)
    return number


def allocate_bill(supplier, lines, amount, supplier_override=None, item_overrides=None):
    """One row per item. The row amounts add up to the bill.

    Freight, GST and rounding are folded into the other lines on this bill,
    in proportion to those lines. A bill with no lines is one supplier row.
    """
    amount = portal._cents(amount or 0)
    fallback = supplier_override or supplier_category(supplier)
    usable = []
    for line in lines or []:
        if not isinstance(line, dict):
            continue
        inc = _line_inc(line)
        if inc is None or inc == 0:
            continue
        usable.append((line, inc))
    if not usable:
        if amount == 0:
            return []
        return [{
            "description": "No line items",
            "itemCode": "",
            "qty": None,
            "own": amount,
            "inc": amount,
            "category": fallback,
            "how": "supplier",
            "whole": True,
        }]
    known = sum((inc for _, inc in usable), Decimal("0"))
    ex_gst = portal._cents(amount / portal.GST_DIVISOR) if amount else Decimal("0")
    weights = {str(index): inc for index, (_, inc) in enumerate(usable)}
    if _close(known, amount) or _close(known, ex_gst) or abs(known) > abs(amount):
        scaled = _partition(amount, weights)
        rest = Decimal("0")
    else:
        scaled = {str(index): inc for index, (_, inc) in enumerate(usable)}
        rest = amount - known
    goods = []
    spreads = []
    for index, (line, _) in enumerate(usable):
        inc = scaled.get(str(index), Decimal("0"))
        if inc == 0:
            continue
        category, how = line_category(line, supplier, supplier_override, item_overrides)
        row = {
            "description": line.get("description") or line.get("itemCode") or "Item",
            "itemCode": line.get("itemCode") or "",
            "qty": _qty(line),
            "own": inc,
            "inc": inc,
            "category": category or "Other",
            "how": how,
            "whole": False,
        }
        if how == "spread":
            spreads.append(row)
        else:
            goods.append(row)
    spread_total = sum((row["inc"] for row in spreads), Decimal("0"))
    if spreads and goods and spread_total != 0:
        share = _partition(spread_total, {str(index): row["inc"] for index, row in enumerate(goods) if row["inc"] != 0})
        for key, extra in share.items():
            goods[int(key)]["inc"] += extra
        rows = goods
    elif spreads and not goods:
        for row in spreads:
            row["category"] = "Other"
            row["description"] = row["description"] or "Freight"
        rows = spreads
    else:
        rows = goods
    if rest != 0:
        rows.append({
            "description": "Rest of bill",
            "itemCode": "",
            "qty": None,
            "own": rest,
            "inc": rest,
            "category": fallback,
            "how": "supplier",
            "whole": False,
        })
    return [row for row in rows if row["inc"] != 0]


def classify_bill(supplier, lines, amount, override=None, item_overrides=None):
    """Category totals for one bill. The parts add up to the bill."""
    totals = {}
    for row in allocate_bill(supplier, lines, amount, override, item_overrides):
        totals[row["category"]] = totals.get(row["category"], Decimal("0")) + row["inc"]
    return totals


def _line_dict(description, account_code, account_name, inc, item_code="", qty=None, ex=None):
    if inc is None and ex is None and not description and not account_code and not account_name and not item_code:
        return None
    item = {
        "description": " ".join(str(description or "").split())[:200],
        "itemCode": " ".join(str(item_code or "").split())[:40],
        "accountCode": " ".join(str(account_code or "").split())[:80],
        "accountName": " ".join(str(account_name or "").split())[:120],
    }
    if qty is not None:
        item["qty"] = qty
    if inc is not None:
        item["inc"] = float(portal._cents(inc))
    elif ex is not None:
        item["ex"] = float(portal._cents(ex))
    portal.reject_sensitive(item["description"], "purchases")
    portal.reject_sensitive(item["itemCode"], "purchases")
    portal.reject_sensitive(item["accountCode"], "purchases")
    portal.reject_sensitive(item["accountName"], "purchases")
    if not any(item.values()) and "inc" not in item and "ex" not in item:
        return None
    return item


def _parse_line(raw):
    if isinstance(raw, dict):
        description = (
            raw.get("description") or raw.get("Description") or raw.get("desc")
            or raw.get("item") or raw.get("name") or ""
        )
        item_code = raw.get("itemCode") or raw.get("ItemCode") or ""
        account_code = raw.get("accountCode") or raw.get("AccountCode") or raw.get("account_code") or ""
        account_name = raw.get("accountName") or raw.get("AccountName") or raw.get("account_name") or ""
        qty_raw = raw.get("qty", raw.get("Quantity"))
        qty = None if qty_raw in ("", None) else portal.num(qty_raw, "line qty", required=False)
        inc = raw.get("incGst", raw.get("inc", raw.get("amount")))
        ex = None
        if inc in ("", None) and (raw.get("LineAmount") not in (None, "") or raw.get("lineAmount") not in (None, "")):
            line_amount = portal.num(raw.get("LineAmount", raw.get("lineAmount")), "line amount", required=False)
            tax_raw = raw.get("TaxAmount", raw.get("taxAmount"))
            tax = Decimal("0") if tax_raw in (None, "") else Decimal(str(portal.num(tax_raw, "line tax", required=False)))
            if tax:
                inc = float(portal._cents(Decimal(str(line_amount)) + tax))
            else:
                ex = line_amount
                inc = None
        elif inc in ("", None):
            ex_raw = raw.get("exGst", raw.get("ex"))
            if ex_raw not in ("", None):
                ex = portal.num(ex_raw, "line ex GST", required=False)
            inc = None
        else:
            inc = portal.num(inc, "line inc GST", required=False)
        return _line_dict(description, account_code, account_name, inc, item_code, qty, ex)
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


def line_index(data, suppliers):
    """Optional lines sent beside the bills, keyed by supplier, invoice and date."""
    found = {}
    raw = data.get("lines") if isinstance(data, dict) else None
    if isinstance(raw, dict):
        for key, value in raw.items():
            found[str(key)] = value
    elif isinstance(raw, list):
        for item in raw:
            if not isinstance(item, dict):
                continue
            supplier = item.get("supplier") or ""
            if not supplier and isinstance(item.get("supplierIndex"), int) and not isinstance(item.get("supplierIndex"), bool):
                idx = item["supplierIndex"]
                if 0 <= idx < len(suppliers):
                    supplier = str(suppliers[idx])
            invoice = item.get("invoice") or item.get("invoiceNumber") or item.get("InvoiceNumber") or ""
            bill_date = item.get("date") or item.get("billDate") or ""
            lines = item.get("items") or item.get("lines") or item.get("LineItems")
            if supplier and invoice and bill_date and lines is not None:
                found[f"{str(supplier).strip()}\n{str(invoice).strip()}\n{str(bill_date).strip()}"] = lines
    return found


def lines_payload(bill, extra=None):
    """JSON for one bill's lines, or None when this upload has none for it.

    The 11th bill value wins. Otherwise `extra` is the matching entry from the
    purchases `lines` list, which is where a nightly push can put the Xero
    lines cached under xero/raw/lines/.
    """
    raw = None
    if isinstance(bill, list) and len(bill) > 10 and bill[10] not in (None, "", []):
        raw = bill[10]
    if raw is None:
        raw = extra
    if isinstance(raw, dict):
        raw = raw.get("LineItems") or raw.get("lines") or raw.get("items")
    if not isinstance(raw, list):
        return None
    lines = parse_line_list(raw)
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


def load_item_overrides(c):
    found = {}
    for row in portal.rows(c.execute("SELECT item_key, category FROM portal_item_categories")):
        if row["category"] in CATEGORIES:
            found[row["item_key"]] = row["category"]
    return found


def save_item(c, user, item_code, description, category):
    line = {"itemCode": item_code or "", "description": description or ""}
    key = item_key(line)
    if not key:
        raise ValueError("That line needs an item code or a description.")
    if category and category not in CATEGORIES:
        raise ValueError("Choose a category.")
    if not category:
        c.execute("DELETE FROM portal_item_categories WHERE item_key=?", (key,))
        return key
    now = portal.stamp()
    c.execute(
        """INSERT INTO portal_item_categories(item_key, item_code, description, category, updated_at, updated_by)
           VALUES(?,?,?,?,?,?)
           ON CONFLICT(item_key) DO UPDATE SET item_code=excluded.item_code, description=excluded.description,
             category=excluded.category, updated_at=excluded.updated_at, updated_by=excluded.updated_by""",
        (key, " ".join(str(item_code or "").split())[:40], " ".join(str(description or "").split())[:200],
         category, now, user["username"]),
    )
    return key


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


def _bill_parts(bills, overrides, item_overrides):
    parts = {category: Decimal("0.00") for category in CATEGORIES}
    rows = []
    missing = 0
    for bill in bills:
        source = load_lines(bill.get("lines"))
        if not source:
            missing += 1
        override = overrides.get(supplier_key(bill["supplier"]))
        for row in allocate_bill(bill["supplier"], source, bill["inc_gst"] or 0, override, item_overrides):
            category = row["category"] if row["category"] in parts else "Other"
            parts[category] += row["inc"]
            rows.append({
                "category": category,
                "supplier": bill["supplier"],
                "invoice_no": bill["invoice_no"],
                "bill_date": bill["bill_date"],
                "description": row["description"],
                "itemCode": row["itemCode"],
                "qty": row["qty"],
                "own": row["own"],
                "inc": row["inc"],
                "how": row["how"],
                "whole": row["whole"],
                "saved_supplier": bool(override),
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
    return parts, rows, target, missing


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
    note = "Saved supplier fallback" if saved else f"Automatic fallback is {automatic}"
    return (
        f'<form method="post" action="/portal/purchases" class="cat-form">'
        f'<input type="hidden" name="csrf" value="{portal.h(user["csrf"])}">'
        f'<input type="hidden" name="action" value="set_category">'
        f'<input type="hidden" name="supplier" value="{portal.h(supplier)}">'
        f'<input type="hidden" name="back" value="{portal.h(back)}">'
        f'<label class="f">Supplier fallback<select name="category" aria-label="Fallback category for {portal.h(supplier)}">{_option_list(chosen)}</select></label>'
        f'<button class="btn" type="submit">Save</button>'
        f'<span class="mini">{portal.h(note)}. Used only for a line that does not match, and for a bill with no lines. The nightly upload does not change it.</span>'
        f'</form>'
    )


def _item_form(user, item, back, saved):
    if not user or item.get("whole") or not item_key(item):
        return ""
    chosen = saved or ""
    return (
        f'<form method="post" action="/portal/purchases" class="cat-form">'
        f'<input type="hidden" name="csrf" value="{portal.h(user["csrf"])}">'
        f'<input type="hidden" name="action" value="set_item">'
        f'<input type="hidden" name="item_code" value="{portal.h(item.get("itemCode") or "")}">'
        f'<input type="hidden" name="description" value="{portal.h(item.get("description") or "")}">'
        f'<input type="hidden" name="back" value="{portal.h(back)}">'
        f'<label class="f">Item category<select name="category" aria-label="Category for {portal.h(item.get("description") or item.get("itemCode") or "item")}">{_option_list(chosen)}</select></label>'
        f'<button class="btn" type="submit">Save item</button>'
        f'</form>'
    )


def _qty_text(qty):
    if qty is None:
        return "—"
    return str(qty)


def _line_rows(user, items, back, item_overrides):
    if not items:
        return '<p class="empty">No lines in this category for the current filters.</p>'
    body = []
    cards = []
    ordered = sorted(items, key=lambda row: (-row["inc"], row["supplier"], row["invoice_no"], row["description"]))
    for item in ordered:
        saved = item_overrides.get(item_key(item)) if item_key(item) else None
        share = ""
        if item["inc"] != item["own"]:
            share = f'<span class="mini">includes freight share, line was {portal.money(item["own"])}</span>'
        code = f'<div class="mini">{portal.h(item["itemCode"])}</div>' if item.get("itemCode") else ""
        pill = ' <span class="pill">Saved</span>' if saved else ""
        if share:
            share = f'<div class="mini">Includes this bill’s freight, GST or rounding. The line itself was {portal.money(item["own"])}.</div>'
        body.append(
            f"<tr><td>{portal.h(item['description'])}{pill}{code}{share}</td>"
            f"<td class=\"num\">{portal.h(_qty_text(item['qty']))}</td>"
            f"<td class=\"num\">{portal.money(item['inc'])}</td>"
            f"<td>{portal.h(item['supplier'])}</td>"
            f"<td>{portal.h(item['invoice_no'])}</td>"
            f"<td>{_item_form(user, item, back, saved)}</td></tr>"
        )
        cards.append(
            f'<article class="invoice-card"><b>{portal.h(item["description"])}</b>{code}{pill}'
            f'<span>{portal.h(item["supplier"])} · {portal.h(item["invoice_no"])}</span>'
            f'<span>Qty {_qty_text(item["qty"])} · {portal.money(item["inc"])}</span>{share}'
            f'{_item_form(user, item, back, saved)}</article>'
        )
    table = (
        '<div class="tw cat-table"><table><thead><tr><th>Item</th><th>Qty</th><th>Amount</th>'
        '<th>Supplier</th><th>Bill</th><th>Category</th></tr></thead><tbody>'
        + "".join(body) + "</tbody></table></div>"
    )
    return table + f'<div class="cat-cards">{"".join(cards)}</div>'


def _missing_note(missing, total):
    if not missing:
        return ""
    bills = "bill" if missing == 1 else "bills"
    return (
        f'<p class="callout" id="lines-missing" data-missing="{missing}" data-bills="{total}">'
        f'{missing} of {total} {bills} in this range have no line items, so the whole bill uses the supplier category. '
        f'The nightly purchases file should include each bill’s Xero lines, already cached on the shop computer under xero/raw/lines/.</p>'
    )


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
    items = load_item_overrides(c)
    parts, rows, target, missing = _bill_parts(bills, overrides, items)
    category = query.get("category") if query.get("category") in CATEGORIES else ""
    fuel = _fuel_note(c, start, end, filtering, selected)
    note = _missing_note(missing, len(bills))
    help_text = (
        '<p class="help" id="category-help">Each line is filed from its item code, description, or Xero account. '
        'Freight, GST and rounding on a bill are shared across that bill’s other lines in proportion to those lines. '
        'A line that still does not match uses the supplier, and so does a bill with no lines. '
        'Save an item to remember it by its code, or by the description when it has no code. '
        'The supplier fallback is only for what a line does not decide.</p>'
    )
    if category:
        chosen = [row for row in rows if row["category"] == category]
        suppliers = []
        seen = set()
        for row in chosen:
            if row["supplier"] not in seen:
                seen.add(row["supplier"])
                suppliers.append(row["supplier"])
        forms = []
        for name in suppliers:
            forms.append(_supplier_form(user, name, overrides.get(supplier_key(name)), supplier_category(name), back))
        back_href = _href(active, start, end, selected, filtering, "")
        extra = ""
        if category == "Fuel":
            extra = _fuel_detail(c, start, end, filtering, selected) + fuel
        count = len(chosen)
        unit = "line" if count == 1 else "lines"
        return (
            f'<section id="purchase-categories"><p><a class="btn ghost" href="{portal.h(back_href)}">All categories</a></p>'
            f'{note}'
            f'<section class="card"><h2>{portal.h(category)}</h2>'
            f'<p id="category-one" data-inc="{_money_attr(parts[category])}">{portal.money(parts[category])} inc GST · {count} {unit}. '
            f'The categories together are {portal.money(target)}, the same Purchases total for these filters.</p>'
            f'{help_text}'
            f'{"".join(forms)}'
            f'{_line_rows(user, chosen, back, items)}</section>{extra}</section>'
        )
    cards = []
    for name in CATEGORIES:
        amount = parts[name]
        count = len([row for row in rows if row["category"] == name])
        href = _href(active, start, end, selected, filtering, name)
        unit = "line" if count == 1 else "lines"
        cards.append(
            f'<a class="cat-card {portal.tone(name)}" data-category="{portal.h(name)}" data-inc="{_money_attr(amount)}" '
            f'href="{portal.h(href)}"><span class="lbl">{portal.h(name)}</span>'
            f'<b>{portal.money(amount)}</b><small>{count} {unit}</small></a>'
        )
    return (
        f'<section id="purchase-categories"><div class="cat-grid">{"".join(cards)}</div>'
        f'<p id="category-total" data-total="{_money_attr(target)}">Categories {portal.money(target)} inc GST. '
        f'This is the Purchases total for the same dates and suppliers.</p>'
        f'{note}{help_text}{fuel}</section>'
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
