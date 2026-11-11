"""Stock on hand for products the owner has counted.

The opening count is the shelf at the end of the baseline date (Australia/Brisbane).
Movements dated on that day are already in the count. Later days add deliveries and
subtract till sales. A new count does not delete earlier ones; it becomes the baseline
when its date is the latest.
"""
import json
import re
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP

import portal

SOURCES = {"till-sales", "supplier-invoice"}
LOW_AT = Decimal(5)
BASELINE_RULE = (
    "The opening count is the stock at the end of baselineDate, Australia/Brisbane. "
    "Movements on that date are already in the count and are not applied again. "
    "Stock on hand = opening + received on later dates − sold on later dates."
)


def qty_text(n):
    if n is None:
        return "—"
    q = Decimal(str(n)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if q == q.to_integral():
        return f"{int(q):,}"
    return f"{q:,.2f}"


def _dec(value):
    return Decimal(str(value if value is not None else 0))


def catalog_index(c):
    archive = portal.parity().product_archive(c)
    if not archive:
        return {}
    refs = archive.get("refs") or []
    out = {}
    for product in archive.get("products") or []:
        if not isinstance(product, list) or len(product) < 2:
            continue
        ref = product[2] if len(product) > 2 else None
        department, group = "", ""
        if isinstance(ref, int) and 0 <= ref < len(refs) and isinstance(refs[ref], list) and refs[ref]:
            department = str(refs[ref][0])
            group = str(refs[ref][1]) if len(refs[ref]) > 1 else department
        code = str(product[0])
        out[code] = {
            "code": code,
            "name": str(product[1]),
            "group": group,
            "department": department,
            "barcode": str(product[3] or "") if len(product) > 3 else "",
        }
    return out


def _clean_code(value):
    if isinstance(value, bool):
        raise ValueError("Enter a product code")
    if isinstance(value, int):
        value = str(value)
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > 40:
        raise ValueError("Enter a product code")
    code = value.strip()
    if portal.has_card_number(code):
        raise ValueError("stock must not include a card number")
    return code


def _clean_time(value):
    if value is None or value == "":
        return "23:59"
    if not isinstance(value, str) or not re.fullmatch(r"\d{2}:\d{2}", value):
        raise ValueError("Use 24-hour time, HH:MM")
    hour, minute = int(value[:2]), int(value[3:])
    if hour > 23 or minute > 59:
        raise ValueError("Use 24-hour time, HH:MM")
    return value


def current_baselines(c):
    """Latest count per product. Latest baseline date wins; then the later entry."""
    chosen = {}
    for row in portal.rows(c.execute("SELECT * FROM portal_stock_counts")):
        code = row["code"]
        key = (row["baseline_date"], row["baseline_at"], row["id"])
        if code not in chosen or key > (chosen[code]["baseline_date"], chosen[code]["baseline_at"], chosen[code]["id"]):
            chosen[code] = row
    return chosen


def _movements_for(c, codes):
    if not codes:
        return []
    marks = ",".join("?" for _ in codes)
    return portal.rows(c.execute(
        f"SELECT * FROM portal_stock_movements WHERE code IN ({marks})",
        tuple(codes),
    ))


def _items(c):
    return {row["code"]: row for row in portal.rows(c.execute("SELECT * FROM portal_stock_items"))}


def _audit(c, actor, action, code, detail):
    c.execute(
        "INSERT INTO portal_stock_audit(at,actor,action,code,detail) VALUES(?,?,?,?,?)",
        (portal.stamp(), actor, action, code, json.dumps(detail, ensure_ascii=False) if detail is not None else None),
    )


def next_manual_code(c):
    meta = portal.meta_get(c, "stock_manual_seq")
    n = int(meta.get("n") or 0)
    while True:
        n += 1
        code = f"M-{n:04d}"
        if not portal.one(c.execute("SELECT 1 AS x FROM portal_stock_items WHERE code=?", (code,))):
            break
    portal.meta_set(c, "stock_manual_seq", {"n": n})
    return code


def _label(value, label, limit, required=False):
    text = re.sub(r"\s+", " ", (value or "").strip())
    if required and (not text or not re.search(r"[A-Za-z]", text)):
        raise ValueError(f"Enter a {label}.")
    if len(text) > limit:
        raise ValueError(f"The {label} is too long.")
    if text and portal.has_card_number(text):
        raise ValueError(f"The {label} must not include a card number.")
    return text


def position(baseline, movements, catalog, item=None):
    """SOH and the pieces the page and the bot read."""
    code = baseline["code"]
    info = dict(catalog.get(code) or {"code": code, "name": code, "group": "", "department": "", "barcode": ""})
    manual = bool(item and item["manual"])
    if item and manual:
        if item["name"]:
            info["name"] = item["name"]
        if item["group_name"]:
            info["group"] = item["group_name"]
        if item["barcode"]:
            info["barcode"] = item["barcode"]
    opening = _dec(baseline["qty"])
    sold = Decimal(0)
    received = Decimal(0)
    last_move = None
    sale_rows = []
    for row in movements:
        if row["code"] != code or row["move_date"] <= baseline["baseline_date"]:
            continue
        sold += _dec(row["sold"])
        received += _dec(row["received"])
        if last_move is None or (row["updated_at"] or "") > last_move:
            last_move = row["updated_at"]
        if _dec(row["sold"]) > 0:
            sale_rows.append(row)
    on_hand = opening + received - sold
    cover = None
    if sale_rows and on_hand >= 0:
        latest = max(row["move_date"] for row in sale_rows)
        start = (date.fromisoformat(latest) - timedelta(days=27)).isoformat()
        window = [row for row in sale_rows if row["move_date"] >= start]
        days = len({row["move_date"] for row in window})
        window_sold = sum((_dec(row["sold"]) for row in window), Decimal(0))
        if days and window_sold > 0:
            cover = (on_hand / (window_sold / Decimal(days))).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
    updated = baseline["created_at"]
    if last_move and last_move > updated:
        updated = last_move
    return {
        "code": code,
        "name": info["name"],
        "group": info["group"],
        "department": info["department"],
        "barcode": info["barcode"],
        "baselineDate": baseline["baseline_date"],
        "baselineAt": baseline["baseline_at"],
        "openingQty": float(opening),
        "soldSince": float(sold),
        "receivedSince": float(received),
        "onHand": float(on_hand),
        "daysCover": float(cover) if cover is not None else None,
        "lastUpdated": updated,
        "note": baseline["note"] or "",
        "countedBy": baseline["created_by"],
        "manual": manual,
    }


def tracked_rows(c):
    catalog = catalog_index(c)
    items = _items(c)
    baselines = current_baselines(c)
    codes = [code for code in baselines if not (items.get(code) and not items[code]["active"])]
    movements = _movements_for(c, codes)
    rows = [position(baselines[code], movements, catalog, items.get(code)) for code in codes]
    rows.sort(key=lambda row: (row["name"].casefold(), row["code"]))
    return rows


def tracked_payload(c):
    return {
        "timezone": "Australia/Brisbane",
        "baselineRule": BASELINE_RULE,
        "products": [
            {key: row[key] for key in (
                "code", "name", "group", "department", "barcode", "baselineDate", "baselineAt",
                "openingQty", "soldSince", "receivedSince", "onHand", "daysCover", "lastUpdated", "note", "manual",
            )}
            for row in tracked_rows(c)
        ],
    }


def apply_stock_movements(c, data):
    if not isinstance(data, dict) or not isinstance(data.get("movements"), list):
        raise ValueError("stock-movements needs a movements list")
    portal.reject_sensitive(data, "stock-movements")
    dates = []
    n = 0
    for i, row in enumerate(data["movements"]):
        if not isinstance(row, dict):
            raise ValueError(f"movements[{i}] must be an object")
        code = _clean_code(row.get("code"))
        move_date = portal.need_date(row.get("date"), "date")
        source = row.get("source")
        if source not in SOURCES:
            raise ValueError(f"movements[{i}] source must be till-sales or supplier-invoice")
        sold = portal.num(row.get("sold") if row.get("sold") is not None else 0, "sold")
        received = portal.num(row.get("received") if row.get("received") is not None else 0, "received")
        if sold < 0 or received < 0:
            raise ValueError(f"movements[{i}] sold and received cannot be negative")
        reference = str(row.get("reference") or "").strip()[:80]
        if portal.has_card_number(reference):
            raise ValueError("stock-movements must not include a card number")
        key = code + "\n" + move_date + "\n" + source
        c.execute(
            """INSERT INTO portal_stock_movements(movement_key,code,move_date,source,sold,received,reference,source_as_of,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?)
               ON CONFLICT(movement_key) DO UPDATE SET sold=excluded.sold, received=excluded.received,
                 reference=excluded.reference, source_as_of=excluded.source_as_of, updated_at=excluded.updated_at""",
            (key, code, move_date, source, sold, received, reference, None, portal.stamp()),
        )
        dates.append(move_date)
        n += 1
    as_of = portal.need_date(data.get("asOf") or (max(dates) if dates else portal.today().isoformat()), "asOf")
    c.execute("UPDATE portal_stock_movements SET source_as_of=? WHERE source_as_of IS NULL", (as_of,))
    portal.meta_set(c, "stock-movements", {"asOf": as_of, "movements": n})
    return {"asOf": as_of, "movements": n}


def save_count(c, user, form):
    code = _clean_code(form.get("code"))
    catalog = catalog_index(c)
    item = portal.one(c.execute("SELECT * FROM portal_stock_items WHERE code=?", (code,)))
    if catalog and code not in catalog and not (item and item["manual"]):
        raise ValueError("Choose a product from the catalog.")
    if item and not item["active"]:
        c.execute(
            "UPDATE portal_stock_items SET active=1, updated_at=?, updated_by=? WHERE code=?",
            (portal.stamp(), user["username"], code),
        )
    qty = portal.num(form.get("qty"), "count")
    if qty < 0:
        raise ValueError("The count cannot be negative.")
    baseline_date = portal.need_date(form.get("baseline_date"), "date")
    clock = _clean_time(form.get("baseline_time"))
    note = (form.get("note") or "").strip()[:500]
    if portal.has_card_number(note):
        raise ValueError("The note cannot include a card number.")
    when = portal.stamp()
    baseline_at = baseline_date + " " + clock + ":00"
    c.execute(
        """INSERT INTO portal_stock_counts(code,qty,baseline_date,baseline_at,note,created_at,created_by)
           VALUES(?,?,?,?,?,?,?)""",
        (code, qty, baseline_date, baseline_at, note, when, user["username"]),
    )
    _audit(c, user["username"], "count", code, {"qty": qty, "baselineDate": baseline_date})
    return code


def add_manual(c, user, form):
    name = _label(form.get("name"), "name", 120, required=True)
    raw_code = (form.get("code") or "").strip()
    code = _clean_code(raw_code) if raw_code else next_manual_code(c)
    if portal.one(c.execute("SELECT 1 AS x FROM portal_stock_items WHERE code=? AND active=1", (code,))):
        raise ValueError("That code is already being tracked.")
    barcode = _label(form.get("barcode"), "barcode", 32)
    group = _label(form.get("group"), "group", 80)
    when = portal.stamp()
    c.execute(
        """INSERT INTO portal_stock_items(code,name,barcode,group_name,manual,active,created_at,updated_at,updated_by)
           VALUES(?,?,?,?,1,1,?,?,?)
           ON CONFLICT(code) DO UPDATE SET name=excluded.name, barcode=excluded.barcode, group_name=excluded.group_name,
             manual=1, active=1, updated_at=excluded.updated_at, updated_by=excluded.updated_by""",
        (code, name, barcode, group, when, when, user["username"]),
    )
    _audit(c, user["username"], "add", code, {"name": name, "barcode": barcode, "group": group})
    form = dict(form)
    form["code"] = code
    if not form.get("baseline_time"):
        form["baseline_time"] = "23:59"
    save_count(c, user, form)
    return code


def edit_manual(c, user, form):
    old = _clean_code(form.get("old_code") or form.get("code"))
    item = portal.one(c.execute("SELECT * FROM portal_stock_items WHERE code=?", (old,)))
    if not item or not item["manual"]:
        raise ValueError("That manual product was not found.")
    name = _label(form.get("name"), "name", 120, required=True)
    raw_code = (form.get("code") or "").strip()
    code = _clean_code(raw_code) if raw_code else old
    barcode = _label(form.get("barcode"), "barcode", 32)
    group = _label(form.get("group"), "group", 80)
    if code != old:
        clash = portal.one(c.execute("SELECT code, active FROM portal_stock_items WHERE code=?", (code,)))
        if clash and clash["active"]:
            raise ValueError("That code is already being tracked.")
        for row in portal.rows(c.execute("SELECT movement_key, move_date, source FROM portal_stock_movements WHERE code=?", (old,))):
            new_key = code + "\n" + row["move_date"] + "\n" + row["source"]
            c.execute(
                "UPDATE portal_stock_movements SET code=?, movement_key=? WHERE movement_key=?",
                (code, new_key, row["movement_key"]),
            )
        c.execute("UPDATE portal_stock_counts SET code=? WHERE code=?", (code, old))
        c.execute("UPDATE portal_stock_audit SET code=? WHERE code=?", (code, old))
        c.execute("DELETE FROM portal_stock_items WHERE code=?", (old,))
    when = portal.stamp()
    c.execute(
        """INSERT INTO portal_stock_items(code,name,barcode,group_name,manual,active,created_at,updated_at,updated_by)
           VALUES(?,?,?,?,1,?,?,?,?)
           ON CONFLICT(code) DO UPDATE SET name=excluded.name, barcode=excluded.barcode, group_name=excluded.group_name,
             manual=1, active=excluded.active, updated_at=excluded.updated_at, updated_by=excluded.updated_by""",
        (code, name, barcode, group, item["active"], item["created_at"], when, user["username"]),
    )
    _audit(c, user["username"], "edit", code, {"from": old, "name": name, "barcode": barcode, "group": group})
    return code


def untrack(c, user, form):
    code = _clean_code(form.get("code"))
    item = portal.one(c.execute("SELECT * FROM portal_stock_items WHERE code=?", (code,)))
    catalog = catalog_index(c).get(code) or {}
    when = portal.stamp()
    name = (item["name"] if item else "") or catalog.get("name") or code
    if item:
        c.execute(
            "UPDATE portal_stock_items SET active=0, updated_at=?, updated_by=? WHERE code=?",
            (when, user["username"], code),
        )
    else:
        c.execute(
            """INSERT INTO portal_stock_items(code,name,barcode,group_name,manual,active,created_at,updated_at,updated_by)
               VALUES(?,?,?,?,0,0,?,?,?)""",
            (code, name, catalog.get("barcode") or "", catalog.get("group") or "", when, when, user["username"]),
        )
    _audit(c, user["username"], "untrack", code, {"name": name})
    return code


def _matches(row, needle):
    if not needle:
        return True
    hay = f"{row['code']} {row['name']} {row['group']} {row['department']}".casefold()
    return needle in hay


def _row_class(on_hand):
    if on_hand < 0:
        return "negative"
    if Decimal(str(on_hand)) <= LOW_AT:
        return "low"
    return ""


def _paint_class(row):
    """Row colour only. Filters still use _row_class."""
    base = _row_class(row["onHand"])
    if base:
        return base
    cover = row.get("daysCover")
    if cover is not None and Decimal(str(cover)) < Decimal("7"):
        return "low"
    return "ok"


def page_stock(c, user, query, error=""):
    rows = tracked_rows(c)
    needle = (query.get("q") or "").strip()
    mode = query.get("filter") if query.get("filter") in ("low", "negative") else "all"
    shown = []
    for row in rows:
        if not _matches(row, needle.casefold()):
            continue
        if mode == "low" and _row_class(row["onHand"]) != "low":
            continue
        if mode == "negative" and row["onHand"] >= 0:
            continue
        shown.append(row)
    body = []
    for row in shown:
        cls = _row_class(row["onHand"])
        flag = ""
        if row.get("manual"):
            flag += ' <span class="pill">Manual</span>'
        if cls == "negative":
            flag += ' <span class="pill bad">Negative</span>'
        elif cls == "low":
            flag += ' <span class="pill warn">Low</span>'
        cover = "—" if row["daysCover"] is None else f"{row['daysCover']:.1f}"
        edit = f'<a href="/portal/stock?edit={portal.h(row["code"])}">Edit</a>' if row.get("manual") else ""
        body.append(
            f'<tr id="stock-{portal.h(row["code"])}" class="{_paint_class(row)}"><td class="nowrap">{portal.h(row["code"])}</td>'
            f'<td>{portal.h(row["name"])}{flag}</td><td>{portal.h(row["group"] or "—")}</td>'
            f'<td class="num">{qty_text(row["openingQty"])}<div class="mini">end of {portal.h(portal.fmt_date(row["baselineDate"]))}</div></td>'
            f'<td class="num">{qty_text(row["soldSince"])}</td><td class="num">{qty_text(row["receivedSince"])}</td>'
            f'<td class="num">{qty_text(row["onHand"])}</td><td class="num">{portal.h(cover)}</td>'
            f'<td>{portal.h(portal.fmt_stamp(row["lastUpdated"]))}</td>'
            f'<td class="actions">{edit}<form method="post" action="/portal/stock" data-confirm="Stop tracking this product? The count history is kept.">'
            f'<input type="hidden" name="csrf" value="{portal.h(user["csrf"])}"><input type="hidden" name="action" value="untrack">'
            f'<input type="hidden" name="code" value="{portal.h(row["code"])}">'
            f'<button class="btn ghost sm" type="submit">Stop tracking</button></form></td></tr>'
        )
    table = '<p class="empty">No tracked products yet. Set an opening count below. Only those products are updated from till sales and deliveries.</p>' if not rows else (
        '<p class="empty">Nothing in this search.</p>' if not body else
        '<div class="tw"><table><thead><tr><th>Code</th><th>Name</th><th>Group</th><th class="num">Opening</th><th class="num">Sold since</th><th class="num">Received since</th><th class="num">On hand</th><th class="num">Days of cover</th><th>Last updated</th><th></th></tr></thead><tbody>'
        + "".join(body) + "</tbody></table></div>"
    )
    chips = []
    for key, label in (("all", "All"), ("low", "Low stock"), ("negative", "Negative")):
        cls = " on" if key == mode else ""
        chips.append(f'<a class="chip{cls}" href="/portal/stock?filter={key}&q={portal.h(needle)}">{label}</a>')
    catalog = catalog_index(c)
    matches = []
    if needle and catalog:
        q = needle.casefold()
        for item in catalog.values():
            hay = f"{item['code']} {item['name']} {item['barcode']} {item['group']}".casefold()
            if q in hay:
                matches.append(item)
            if len(matches) >= 20:
                break
        matches.sort(key=lambda item: item["name"].casefold())
    pick = (query.get("pick") or "").strip()
    picked = catalog.get(pick) if pick else None
    match_html = []
    for item in matches:
        match_html.append(
            f'<a class="chip" href="/portal/stock?pick={portal.h(item["code"])}&q={portal.h(needle)}">{portal.h(item["name"])} · {portal.h(item["code"])}</a>'
        )
    now = portal._srv().now()
    default_date = now.date().isoformat()
    default_time = now.strftime("%H:%M")
    form_code = picked["code"] if picked else ""
    form_label = f'{picked["name"]} · {picked["group"]}' if picked else "Search above, then choose a product."
    alert = f'<p class="alert">{portal.h(error)}</p>' if error else ""
    ok = '<p class="oknote">Opening count saved. Earlier counts stay in the history.</p>' if query.get("ok") == "1" else ""
    stored = _items(c)
    editing = stored.get((query.get("edit") or "").strip()) if query.get("edit") else None
    edit_html = ""
    if editing and editing["manual"]:
        edit_html = f"""<section class="card" id="stock-edit"><h2>Edit {portal.h(editing["name"])}</h2>
<form method="post" action="/portal/stock">
<input type="hidden" name="csrf" value="{portal.h(user['csrf'])}"><input type="hidden" name="action" value="edit_manual">
<input type="hidden" name="old_code" value="{portal.h(editing['code'])}">
<div class="row">
<label class="f">Name<input name="name" value="{portal.h(editing['name'])}" required maxlength="120"></label>
<label class="f">Code / PLU<input name="code" value="{portal.h(editing['code'])}" maxlength="40"></label>
<label class="f">Barcode<input name="barcode" value="{portal.h(editing['barcode'] or '')}" maxlength="32"></label>
<label class="f">Group<input name="group" value="{portal.h(editing['group_name'] or '')}" maxlength="80"></label>
</div>
<button class="btn" type="submit">Save product</button></form>
<p class="help">Changing the code keeps the opening counts and deliveries already stored.</p></section>"""
    history = []
    for row in portal.rows(c.execute("SELECT * FROM portal_stock_audit ORDER BY id DESC LIMIT 20")):
        history.append(
            f"<li><b>{portal.h(portal.fmt_stamp(row['at']))}</b> · {portal.h(row['actor'])} · {portal.h(row['action'])} · {portal.h(row['code'])}</li>"
        )
    for row in portal.rows(c.execute(
        "SELECT * FROM portal_stock_counts ORDER BY id DESC LIMIT 40"
    )):
        info = catalog.get(row["code"]) or {}
        item = stored.get(row["code"]) or {}
        name = (item.get("name") if item else "") or info.get("name") or row["code"]
        history.append(
            f'<li><b>{portal.h(portal.fmt_stamp(row["created_at"]))}</b> · {portal.h(row["created_by"])} set '
            f'{portal.h(name)} ({portal.h(row["code"])}) to {qty_text(row["qty"])} '
            f'at the end of {portal.h(portal.fmt_date(row["baseline_date"]))} '
            f'({portal.h(row["baseline_at"][11:16])}). {portal.h(row["note"] or "")}</li>'
        )
    return f"""<div class="ph"><h1>Stock on hand</h1>
<p>Enter an opening count. After that, till sales reduce it and supplier deliveries increase it. Products without a count are not tracked. Counts and units only. No cost or gross profit on this page.</p>
<p class="help">{portal.h(BASELINE_RULE)} A count during the day should use yesterday’s date if tonight’s sales should still come off it.</p></div>
{alert}{ok}
<form class="period" method="get" action="/portal/stock">
<div class="chips">{''.join(chips)}</div>
<div class="row"><label class="f">Search code, name or group<input name="q" value="{portal.h(needle)}" maxlength="80"></label>
<input type="hidden" name="filter" value="{portal.h(mode)}">
<button class="btn" type="submit">Search</button></div></form>
<section class="card" id="stock-list">{table}
<p class="help">Low means {qty_text(LOW_AT)} or fewer on hand, and still zero or above. Negative is below zero. Days of cover uses till sales on the recent days after the count.</p></section>
<section class="card" id="stock-count"><h2>Set or reset an opening count</h2>
<p class="help">This is a stocktake. It starts a fresh baseline from the end of the date you enter. The previous count stays in the history.</p>
<div class="chips">{''.join(match_html) or ('<p class="empty">Type a name or PLU to search the product catalog.</p>' if not needle else '<p class="empty">No catalog match.</p>')}</div>
<form method="post" action="/portal/stock">
<input type="hidden" name="csrf" value="{portal.h(user['csrf'])}">
<input type="hidden" name="action" value="set_count">
<input type="hidden" name="code" value="{portal.h(form_code)}">
<p id="stock-pick">{portal.h(form_label)}</p>
<div class="row">
<label class="f">Count<input name="qty" inputmode="decimal" min="0" step="any" required></label>
<label class="f">Date<input type="date" name="baseline_date" value="{portal.h(default_date)}" required></label>
<label class="f">Time<input type="time" name="baseline_time" value="{portal.h(default_time)}" required></label>
</div>
<label class="f">Note<input name="note" maxlength="500"></label>
<button class="btn" type="submit">Save opening count</button>
</form></section>
<section class="card" id="stock-manual"><h2>Add a product not on the till</h2>
<p class="help">For a new line that is not in the product catalog yet. Leave the code blank and the portal assigns one, such as M-0001, so deliveries can be posted against it.</p>
<form method="post" action="/portal/stock">
<input type="hidden" name="csrf" value="{portal.h(user['csrf'])}"><input type="hidden" name="action" value="add_manual">
<div class="row">
<label class="f">Name<input name="name" required maxlength="120"></label>
<label class="f">Code / PLU<input name="code" maxlength="40" placeholder="Optional"></label>
<label class="f">Barcode<input name="barcode" maxlength="32" placeholder="Optional"></label>
<label class="f">Group<input name="group" maxlength="80" placeholder="Optional"></label>
</div>
<div class="row">
<label class="f">Opening count<input name="qty" inputmode="decimal" min="0" step="any" required></label>
<label class="f">Date<input type="date" name="baseline_date" value="{portal.h(default_date)}" required></label>
<label class="f">Time<input type="time" name="baseline_time" value="23:59" required></label>
</div>
<button class="btn" type="submit">Add product</button>
</form></section>
{edit_html}
<section class="card" id="stock-history"><h2>Count history</h2>
<ul class="history">{''.join(history) or '<li>No counts yet.</li>'}</ul></section>"""
