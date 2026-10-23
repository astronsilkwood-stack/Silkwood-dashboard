#!/usr/bin/env python3
"""Silkwood daily ops dashboard: stdlib HTTP server + SQLite. Run: python3 server.py (port 8787)."""
import json, sqlite3, os, re, threading, smtplib, ssl, html as H_
from email.message import EmailMessage
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs

ROOT = Path(__file__).resolve().parent
DB = Path(os.environ.get("SILKWOOD_DB", ROOT / "silkwood.db"))
TZ = ZoneInfo("Australia/Brisbane")
PORT = int(os.environ.get("PORT", "8787"))
RULES = {"fridge": ("max", 5), "freezer": ("max", -15), "hot": ("min", 60), "core": ("min", 75)}
TANKS = ["1 Unleaded", "2 P95", "3 Diesel", "4 Diesel", "5 Diesel"]
STARTERS = [  # kind, name, type, example(1=starter to confirm)
    ('fridge', 'Coca-Cola Drinks Fridge 1', 'fridge', 0),
    ('fridge', 'Coca-Cola Drinks Fridge 2', 'fridge', 0),
    ('fridge', 'Coca-Cola Drinks Fridge 3', 'fridge', 0),
    ('fridge', 'Parmalat Ice Break Fridge', 'fridge', 0),
    ('fridge', 'Dairy Farmers Fridge', 'fridge', 0),
    ('fridge', 'Monster Fridge', 'fridge', 0),
    ('fridge', 'Coca-Cola Mid-Size Fridge (Sandwiches)', 'fridge', 0),
    ('fridge', 'Trifle & Cheesecake Fridge', 'fridge', 0),
    ('freezer', 'Peters Ice Cream Freezer', 'freezer', 0),
    ('freezer', 'Stand-Up Freezer (Take-Home Ice Creams)', 'freezer', 0),
    ('kcheck', 'Kitchen Freezer 1', 'freezer', 0),
    ('kcheck', 'Kitchen Freezer 2', 'freezer', 0),
    ('kcheck', 'Kitchen Freezer 3', 'freezer', 0),
    ('kcheck', 'Walk-In Cool Room', 'fridge', 0),
    ('kcheck', 'Bain-marie', 'hot', 1),
    ('kcheck', 'Hot Box (Big)', 'hot', 0),
    ('kcheck', 'Hot Box (Crunchy Chicken)', 'hot', 0),
    ('kcheck', 'Cooked chicken core', 'core', 1),
    ('prep', 'Chips (portioned)', '', 1),
    ('prep', 'Salad mix', '', 1),
    ('prep', 'Crumbed chicken', '', 1),
    ('prep', 'Burger patties', '', 1),
    ('hotbox', 'Pies', '', 1),
    ('hotbox', 'Sausage rolls', '', 1),
    ('hotbox', 'Chicken tenders', '', 1),
    ('hotbox', 'Dim sims', '', 1),
    ('hotbox', 'Hash browns', '', 1),
]
STARTER_TASKS = [
    ("kitchen", "Wipe down benches and prep area", "daily", None), ("kitchen", "Turn on fryers and bain-marie", "daily", "06:00"),
    ("kitchen", "Clean fryer oil / filter", "weekly", None),
    ("shop", "Check and face up drinks fridges", "daily", None), ("shop", "Empty forecourt bins", "daily", "10:00"),
    ("shop", "Clean toilets", "daily", "14:00"),
]

def now(): return datetime.now(TZ)
def today(): return now().date().isoformat()
def _load_env():
    """Load .env.local then .env (Neon CLI writes these) without overriding real env vars."""
    for name in (".env.local", ".env"):
        f = Path(__file__).with_name(name)
        if not f.exists(): continue
        for line in f.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line: continue
            k, v = line.split("=", 1)
            k = k.strip().removeprefix("export ").strip(); v = v.strip().strip('"').strip("'")
            os.environ.setdefault(k, v)
_load_env()

def _ssl(url):
    if url and "sslmode=" not in url: url += ("&" if "?" in url else "?") + "sslmode=require"
    return url
DATABASE_URL = _ssl(os.environ.get("DATABASE_URL", "").strip())           # pooled (-pooler) for the app
DATABASE_URL_UNPOOLED = _ssl(os.environ.get("DATABASE_URL_UNPOOLED", "").strip()) or DATABASE_URL  # direct, for schema DDL
STAFF_PIN = os.environ.get("STAFF_PIN", "").strip()      # shared staff PIN; empty = no PIN (local use only)
MANAGER_PIN = os.environ.get("MANAGER_PIN") or os.environ.get("SILKWOOD_PIN") or "1945"
PG = DATABASE_URL.startswith(("postgres://", "postgresql://"))

class PgConn:
    """Tiny adapter so the SQLite-style code also runs on Postgres (psycopg 3)."""
    _pool = None
    def __init__(self, direct=False):
        import psycopg
        from psycopg.rows import dict_row
        self.direct = direct
        if direct:   # schema/migrations over the direct (unpooled) endpoint, per Neon guidance
            self.c = psycopg.connect(DATABASE_URL_UNPOOLED, row_factory=dict_row); return
        if PgConn._pool is None:
            from psycopg_pool import ConnectionPool
            # Neon's -pooler endpoint is PgBouncer (transaction mode): disable server-side prepared statements
            PgConn._pool = ConnectionPool(DATABASE_URL, min_size=1, max_size=10, open=True,
                kwargs={"row_factory": dict_row, "prepare_threshold": None}, check=ConnectionPool.check_connection)
        self.c = PgConn._pool.getconn()
    def execute(self, sql, params=()):
        sql = sql.replace("INSERT OR REPLACE INTO task_done VALUES(?,?,?,?)",
              "INSERT INTO task_done VALUES(?,?,?,?) ON CONFLICT (task_id,date) DO UPDATE SET staff=EXCLUDED.staff, at=EXCLUDED.at")
        return self.c.execute(sql.replace("?", "%s"), params)
    def executescript(self, script):
        for st in script.split(";"):
            if st.strip(): self.c.execute(st.replace("INTEGER PRIMARY KEY", "SERIAL PRIMARY KEY"))
    def commit(self): self.c.commit()
    def close(self):
        if self.direct: self.c.close()
        else:
            if self.c.info.transaction_status != 0: self.c.rollback()
            PgConn._pool.putconn(self.c)

def db():
    if PG: return PgConn()
    c = sqlite3.connect(DB); c.row_factory = sqlite3.Row; return c

def init():
    c = PgConn(direct=True) if PG else db()
    c.executescript("""
    CREATE TABLE IF NOT EXISTS items(id INTEGER PRIMARY KEY, kind TEXT, name TEXT, type TEXT, example INTEGER DEFAULT 0, active INTEGER DEFAULT 1, sort INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS tasks(id INTEGER PRIMARY KEY, tab TEXT, title TEXT, repeat TEXT, weekday INTEGER, due_time TEXT, start_date TEXT, active INTEGER DEFAULT 1, example INTEGER DEFAULT 0, created_by TEXT);
    CREATE TABLE IF NOT EXISTS task_done(task_id INT, date TEXT, staff TEXT, at TEXT, PRIMARY KEY(task_id,date));
    CREATE TABLE IF NOT EXISTS logs(id INTEGER PRIMARY KEY, date TEXT, kind TEXT, ref TEXT, data TEXT, staff TEXT, at TEXT, bad INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS days(date TEXT PRIMARY KEY, staff TEXT, at TEXT, summary TEXT);
    CREATE TABLE IF NOT EXISTS shifts(id INTEGER PRIMARY KEY, date TEXT, staff TEXT, area TEXT, start_at TEXT, end_at TEXT, hours REAL);
    CREATE TABLE IF NOT EXISTS submissions(id INTEGER PRIMARY KEY, date TEXT, area TEXT, staff TEXT, at TEXT, snapshot TEXT, email_status TEXT, email_error TEXT, webhook_status TEXT);
    CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY, date TEXT, at TEXT, staff TEXT, action TEXT, detail TEXT);
    CREATE INDEX IF NOT EXISTS ix_task_done_date ON task_done(date);
    CREATE INDEX IF NOT EXISTS ix_logs_date ON logs(date);
    CREATE INDEX IF NOT EXISTS ix_shifts_date ON shifts(date);
    CREATE INDEX IF NOT EXISTS ix_submissions_date ON submissions(date);
    CREATE INDEX IF NOT EXISTS ix_audit_date ON audit(date);
    CREATE INDEX IF NOT EXISTS ix_tasks_active ON tasks(active, start_date);
    CREATE TABLE IF NOT EXISTS food_temps(id INTEGER PRIMARY KEY, date TEXT, food TEXT, stage TEXT, temp REAL, bad INTEGER DEFAULT 0, action TEXT, staff TEXT, at TEXT);
    CREATE TABLE IF NOT EXISTS hotbox_items(id INTEGER PRIMARY KEY, date TEXT, box TEXT, item TEXT, qty TEXT, time_in TEXT, out_at TEXT, out_by TEXT, outcome TEXT, staff TEXT, at TEXT);
    CREATE TABLE IF NOT EXISTS discards(id INTEGER PRIMARY KEY, date TEXT, item TEXT, qty TEXT, reason TEXT, hotbox_id INTEGER, staff TEXT, at TEXT);
    CREATE INDEX IF NOT EXISTS ix_food_temps_date ON food_temps(date);
    CREATE INDEX IF NOT EXISTS ix_hotbox_date ON hotbox_items(date);
    CREATE INDEX IF NOT EXISTS ix_hotbox_open ON hotbox_items(out_at);
    CREATE INDEX IF NOT EXISTS ix_discards_date ON discards(date);
    CREATE TABLE IF NOT EXISTS closing_photos(id TEXT PRIMARY KEY, kind TEXT, mime TEXT, size INTEGER, data BYTEA, staff TEXT, at TEXT, submitted INTEGER DEFAULT 0);
    CREATE INDEX IF NOT EXISTS ix_closing_photos_at ON closing_photos(at)
    """)
    c.commit()
    try:  # migrate older submissions tables
        c.execute("ALTER TABLE submissions ADD COLUMN webhook_status TEXT"); c.commit()
    except Exception:
        if PG: c.c.rollback()
    if not c.execute("SELECT 1 FROM items LIMIT 1").fetchone():
        for i, (k, n, t, ex) in enumerate(STARTERS):
            c.execute("INSERT INTO items(kind,name,type,example,sort) VALUES(?,?,?,?,?)", (k, n, t, ex, i))
        for tab, title, rep, due in STARTER_TASKS:
            c.execute("INSERT INTO tasks(tab,title,repeat,weekday,due_time,start_date,example) VALUES(?,?,?,?,?,?,1)",
                      (tab, title, rep, 0 if rep == "weekly" else None, due, "2026-01-01"))
    c.commit(); c.close()

def is_bad(typ, v):
    if typ not in RULES or v is None: return 0
    op, lim = RULES[typ]; return int(v > lim if op == "max" else v < lim)

def tasks_for(c, d):
    wd = date.fromisoformat(d).weekday()
    out = []
    rows = [dict(t) for t in c.execute("SELECT * FROM tasks WHERE active=1 AND start_date<=? ORDER BY due_time IS NULL, due_time, id", (d,))]
    # one query for all ticks instead of 1-2 per task (DB is cross-region, every round trip costs ~100ms)
    done_on = {r["task_id"]: dict(staff=r["staff"], at=r["at"]) for r in c.execute("SELECT task_id,staff,at FROM task_done WHERE date=?", (d,))}
    ever_done = None
    for t in rows:
        if t["repeat"] == "weekly" and t["weekday"] != wd: continue
        if t["repeat"] == "once" and t["start_date"] != d:
            # carry an undone one-off forward to later days
            if d != today(): continue
            if ever_done is None:
                ever_done = {r["task_id"] for r in c.execute("SELECT DISTINCT task_id FROM task_done td JOIN tasks t ON t.id=td.task_id WHERE t.repeat='once' AND t.active=1")}
            if t["id"] in ever_done: continue
        t["done"] = done_on.get(t["id"])
        out.append(t)
    return out

FOOD_STAGES = {"cook": ("Cooking (core)", "core"), "reheat": ("Reheating (core)", "core"), "hothold": ("Hot holding", "hot"),
               "coldhold": ("Cold holding", "fridge"), "chilled": ("Receiving chilled", "fridge"), "frozen": ("Receiving frozen", "freezer")}
HOTBOX_MAX_H = 2

def day_state(d, extras=True):
    c = db()
    items = [dict(r) for r in c.execute("SELECT * FROM items WHERE active=1 ORDER BY kind, sort, id")]
    logs = [dict(r, data=json.loads(r["data"])) for r in c.execute("SELECT * FROM logs WHERE date=? ORDER BY at", (d,))]
    tasks = tasks_for(c, d)
    lock = c.execute("SELECT * FROM days WHERE date=?", (d,)).fetchone()
    shifts = [dict(r) for r in c.execute("SELECT * FROM shifts WHERE date=? ORDER BY start_at", (d,))]
    ex = {}
    if extras:
        ex["food"] = [dict(r) for r in c.execute("SELECT * FROM food_temps WHERE date=? ORDER BY at", (d,))]
        ex["hotbox"] = [dict(r) for r in c.execute("SELECT * FROM hotbox_items WHERE date=? OR (out_at IS NULL AND date<=?) ORDER BY time_in", (d, d))]
        ex["discards"] = [dict(r) for r in c.execute("SELECT * FROM discards WHERE date=? ORDER BY at", (d,))]
    c.close()
    return {**ex, "now": now().strftime("%Y-%m-%d %H:%M:%S"), "food_stages": FOOD_STAGES, "hotbox_max_h": HOTBOX_MAX_H, "pg": PG,"date": d, "today": today(), "items": items, "logs": logs, "tasks": tasks, "tanks": TANKS,
            "rules": RULES, "locked": dict(lock) if lock else None, "shifts": shifts, "summary": summarise(items, logs, tasks, ex.get("food"))}

def shift_hours(a, b):
    f = "%Y-%m-%d %H:%M:%S"
    return round((datetime.strptime(b, f) - datetime.strptime(a, f)).total_seconds() / 3600, 2)

def summarise(items, logs, tasks, food=None):
    def has(kind, ref): return any(l["kind"] == kind and l["ref"] == ref for l in logs)
    tabs = {}
    for tab in ("kitchen", "shop"):
        tt = [t for t in tasks if t["tab"] == tab]
        if tab == "kitchen":
            req = [("ktemp", str(i["id"]), i["name"]) for i in items if i["kind"] == "kcheck"]
        else:
            req = [("stemp", str(i["id"]), i["name"]) for i in items if i["kind"] in ("fridge", "freezer")] + \
                  [("fuel", t, "Tank " + t) for t in TANKS]
        missing = [n for k, r, n in req if not has(k, r)]
        done = sum(1 for t in tt if t["done"]) + len(req) - len(missing)
        tabs[tab] = {"tasks_done": sum(1 for t in tt if t["done"]), "tasks_total": len(tt),
                     "missing": missing, "progress": round(100 * done / max(1, len(tt) + len(req)))}
    alerts = [f'{l["data"].get("name")}: {l["data"].get("temp")}°C' for l in logs if l["bad"]]
    alerts += [f'Tank {l["ref"]}: water {l["data"].get("water_mm")}mm' for l in logs if l["kind"] == "fuel" and l["data"].get("water") == "yes"]
    alerts += [f'{f["food"]} ({FOOD_STAGES.get(f["stage"], (f["stage"],))[0]}): {f["temp"]}°C' for f in (food or []) if f["bad"]]
    return {"tasks_done": sum(v["tasks_done"] for v in tabs.values()), "tasks_total": sum(v["tasks_total"] for v in tabs.values()),
            "missing": sum(len(v["missing"]) for v in tabs.values()), "alerts": alerts, "tabs": tabs}

AREAS = {"kitchen": "Kitchen", "shop": "Shop", "both": "Kitchen + Shop"}

def snapshot(ds, area):
    """Full record of the day for one area (kitchen/shop) or both."""
    tabs = ["kitchen", "shop"] if area == "both" else [area]
    L = ds["logs"]; units = {str(i["id"]): i for i in ds["items"]}
    tk = {"kitchen": ("ktemp",), "shop": ("stemp",)}
    temps = []
    for t in tabs:
        for l in L:
            if l["kind"] in tk[t]:
                temps.append({"area": t, "unit": l["data"].get("name") or units.get(l["ref"], {}).get("name", "?"), "type": l["data"].get("type"),
                              "temp": l["data"].get("temp"), "ok": not l["bad"], "at": l["at"][11:16], "staff": l["staff"], "action": l["data"].get("action")})
    jobs = [{"area": t["tab"], "title": t["title"], "done": bool(t["done"]), "by": (t["done"] or {}).get("staff"), "at": ((t["done"] or {}).get("at") or "")[11:16]}
            for t in ds["tasks"] if t["tab"] in tabs]
    pick = lambda k: [dict(l["data"], staff=l["staff"], at=l["at"][11:16]) for l in L if l["kind"] == k]
    snap = {"date": ds["date"], "area": area, "temps": temps, "jobs": jobs,
            "not_logged": sum((ds["summary"]["tabs"][t]["missing"] for t in tabs), []),
            "shifts": [x for x in ds["shifts"] if area == "both" or x["area"] in (area, "")]}
    if "kitchen" in tabs:
        snap.update(prep=pick("prep"), hotbox=pick("hotbox"), tomorrow=pick("tomorrow"))
        snap["food_temps"] = [{"food": f["food"], "stage": FOOD_STAGES.get(f["stage"], (f["stage"],))[0], "temp": f["temp"], "ok": not f["bad"],
                               "action": f["action"], "at": (f["at"] or "")[11:16], "staff": f["staff"]} for f in ds.get("food", [])]
        snap["hotbox_items"] = [{"box": x["box"], "item": x["item"], "qty": x["qty"], "in": (x["time_in"] or "")[11:16], "out": (x["out_at"] or "")[11:16],
                                 "outcome": x["outcome"] or "in hot box", "staff": x["staff"]} for x in ds.get("hotbox", []) if x["date"] == ds["date"]]
        snap["discards"] = [{"item": x["item"], "qty": x["qty"], "reason": x["reason"], "at": (x["at"] or "")[11:16], "staff": x["staff"]} for x in ds.get("discards", [])]
    if "shop" in tabs: snap["fuel"] = [dict(l["data"], tank=l["ref"], staff=l["staff"], at=l["at"][11:16]) for l in L if l["kind"] == "fuel"]
    alerts = [f'{t["unit"]}: {t["temp"]}°C' for t in temps if not t["ok"]]
    alerts += [f'{f["food"]} ({f["stage"]}): {f["temp"]}°C' for f in snap.get("food_temps", []) if not f["ok"]]
    alerts += [f'Tank {f["tank"]}: WATER {f.get("water_mm")}mm' for f in snap.get("fuel", []) if f.get("water") == "yes"]
    snap["alerts"] = alerts
    return snap

def snapshot_html(sub, snap, standalone=False):
    e = lambda v: H_.escape(str(v if v is not None else ""))
    def table(head, rows):
        if not rows: return '<p style="color:#666">None.</p>'
        th = "".join(f'<th style="text-align:left;padding:6px;border-bottom:2px solid #C62828">{e(h)}</th>' for h in head)
        trs = "".join("<tr>" + "".join(f'<td style="padding:6px;border-bottom:1px solid #eee;{st}">{v}</td>' for v, st in r) + "</tr>" for r in rows)
        return f'<table style="border-collapse:collapse;width:100%;font-size:14px"><tr>{th}</tr>{trs}</table>'
    ok = lambda b: ("✅ OK", "color:#2E7D32;font-weight:700") if b else ("⚠ ALERT", "color:#C62828;font-weight:700")
    h2 = lambda t: f'<h2 style="font-size:18px;margin:22px 0 6px;color:#C62828">{t}</h2>'
    out = [f'<h1 style="font-size:22px;margin:0">Silkwood daily log – {e(snap["date"])} – {e(AREAS.get(sub["area"], sub["area"]))}</h1>',
           f'<p style="margin:4px 0 14px">Submitted by <b>{e(sub["staff"])}</b> at {e(sub["at"])}</p>']
    if snap["alerts"]:
        out.append('<div style="background:#C62828;color:#fff;padding:12px 14px;border-radius:8px;font-weight:700">⚠ ALERTS – tell the manager<br>' + "<br>".join(e(a) for a in snap["alerts"]) + "</div>")
    else: out.append('<div style="background:#E8F5E9;color:#2E7D32;padding:12px 14px;border-radius:8px;font-weight:700">✅ No alerts</div>')
    if snap["not_logged"]: out.append(f'<p style="color:#C62828;font-weight:700">Not logged: {e(", ".join(snap["not_logged"]))}</p>')
    out.append(h2("🌡 Temperatures") + table(["Unit", "Temp", "Result", "Time", "By"], [[(e(t["unit"]), ""), (e(t["temp"]) + "°C", ""), (ok(t["ok"])[0] + (" – " + e(t["action"]) if t.get("action") else ""), ok(t["ok"])[1]), (e(t["at"]), ""), (e(t["staff"]), "")] for t in snap["temps"]]))
    out.append(h2("✅ Jobs") + table(["Job", "Status", "By"], [[(e(j["title"]), ""), ("✓ Done " + e(j["at"]), "color:#2E7D32;font-weight:700") if j["done"] else ("✗ Not done", "color:#C62828;font-weight:700"), (e(j["by"]), "")] for j in snap["jobs"]]))
    if "prep" in snap:
        out.append(h2("🍳 Prep") + table(["Item", "Qty", "Prepped", "Use by", "By"], [[(e(r.get("item")), ""), (e(r.get("qty")), ""), (e(r.get("time")), ""), (e(r.get("use_by")), ""), (e(r["staff"]), "")] for r in snap["prep"]]))
        out.append(h2("🔥 Hot box") + table(["Hot box", "Item", "Qty", "In", "Discard", "By"], [[(e(r.get("box")), ""), (e(r.get("item")), ""), (e(r.get("qty")), ""), (e(r.get("time")), ""), (e(r.get("discard")), ""), (e(r["staff"]), "")] for r in snap["hotbox"]]))
        out.append(h2("📋 For tomorrow") + table(["Item", "Qty / note", "By"], [[(e(r.get("item")), ""), (e(r.get("qty")), ""), (e(r["staff"]), "")] for r in snap["tomorrow"]]))
    if snap.get("food_temps") is not None:
        out.append(h2("🍗 Food temps") + table(["Food", "Stage", "Temp", "Result", "Time", "By"], [[(e(f["food"]), ""), (e(f["stage"]), ""), (e(f["temp"]) + "°C", ""), (ok(f["ok"])[0] + (" – " + e(f["action"]) if f.get("action") else ""), ok(f["ok"])[1]), (e(f["at"]), ""), (e(f["staff"]), "")] for f in snap["food_temps"]]))
        out.append(h2("🔥 Hot box items") + table(["Box", "Item", "Qty", "In", "Out", "Result", "By"], [[(e(x["box"]), ""), (e(x["item"]), ""), (e(x["qty"]), ""), (e(x["in"]), ""), (e(x["out"]), ""), (e(x["outcome"]), ""), (e(x["staff"]), "")] for x in snap.get("hotbox_items", [])]))
        out.append(h2("🗑 Discards") + table(["Item", "Qty", "Reason", "Time", "By"], [[(e(x["item"]), ""), (e(x["qty"]), ""), (e(x["reason"]), ""), (e(x["at"]), ""), (e(x["staff"]), "")] for x in snap.get("discards", [])]))
    if "fuel" in snap:
        out.append(h2("⛽ Fuel water check") + table(["Tank", "Result", "Time", "By"], [[(e(f["tank"]), ""), ("⚠ WATER " + e(f.get("water_mm")) + "mm" + (f' · fuel {e(f.get("fuel_l"))}L' if f.get("fuel_l") else ""), "color:#C62828;font-weight:700") if f.get("water") == "yes" else ("No water" + (f' · fuel {e(f.get("fuel_l"))}L' if f.get("fuel_l") else ""), "color:#2E7D32"), (e(f["at"]), ""), (e(f["staff"]), "")] for f in snap["fuel"]]))
    hm = lambda t: (t or "")[11:16] or "on shift"
    out.append(h2("🕒 Shifts") + table(["Name", "Area", "Start", "End", "Hours"], [[(e(x["staff"]), ""), (e(AREAS.get(x["area"], x["area"])), ""), (e(hm(x["start_at"])), ""), (e(hm(x["end_at"])), ""), (e(x["hours"]), "")] for x in snap["shifts"]]))
    body = f'<div style="font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;max-width:760px;margin:0 auto;color:#111;padding:16px">{"".join(out)}</div>'
    if not standalone: return body
    em = {"sent": "✅ Email sent", "failed": "⚠ Email failed", "not_configured": "Email not configured", "sending": "Email sending…"}.get(sub["email_status"], sub["email_status"])
    bar = f'<div class="np" style="background:#C62828;color:#fff;padding:10px 16px;display:flex;gap:10px;align-items:center;font-family:system-ui"><b style="flex:1">{e(em)}</b><button onclick="print()" style="font-size:17px;padding:10px 18px;border:0;border-radius:10px;font-weight:700">🖨 Print</button><button onclick="history.length>1?history.back():close()" style="font-size:17px;padding:10px 18px;border:0;border-radius:10px">Close</button></div>'
    return f'<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Silkwood log {e(snap["date"])}</title><style>@media print{{.np{{display:none}}}}</style></head><body style="margin:0">{bar}{body}</body></html>'

def send_email(sub_id, subject, html_body):
    host, port = os.environ.get("SMTP_HOST", "").strip(), int(os.environ.get("SMTP_PORT") or 587)
    user, pw = os.environ.get("SMTP_USER", "").strip(), os.environ.get("SMTP_PASS", "")
    to = os.environ.get("MAIL_TO", "").strip() or "silkwoodup@gmail.com"
    def set_status(st, err=""):
        if sub_id is None: return
        c = db(); c.execute("UPDATE submissions SET email_status=?, email_error=? WHERE id=?", (st, err[:300], sub_id)); c.commit(); c.close()
    if not host:
        set_status("not_configured"); return "not_configured", ""
    step = "build"
    try:
        m = EmailMessage(); m["Subject"] = subject; m["From"] = os.environ.get("MAIL_FROM") or user or "silkwood@localhost"; m["To"] = to
        m.set_content("This email is HTML. Open it in a mail app that shows HTML."); m.add_alternative(html_body, subtype="html")
        step = "connect"
        if port == 465: srv = smtplib.SMTP_SSL(host, port, timeout=30, context=ssl.create_default_context())
        else:
            srv = smtplib.SMTP(host, port, timeout=30); step = "ehlo"; srv.ehlo()
            if srv.has_extn("starttls"): step = "starttls"; srv.starttls(context=ssl.create_default_context()); srv.ehlo()
        with srv:
            step = "login"
            if user: srv.login(user, pw.replace(" ", "").strip().strip('"').strip("'"))  # app passwords are often pasted as 'abcd efgh ijkl mnop'
            step = "send"; srv.send_message(m)
        set_status("sent"); return "sent", to
    except Exception as ex:
        msg = f"{type(ex).__name__} at {locals().get('step', '?')}: {ex}"
        if isinstance(ex, smtplib.SMTPAuthenticationError) or locals().get("step") == "login":
            msg += " (Gmail rejected the login: SMTP_PASS must be a 16-letter Google App Password for SMTP_USER)"
        set_status("failed", msg[:300]); return "failed", msg[:300]

def webhook_payload(sub, snap):
    return {"date": snap["date"], "area": sub["area"], "submitted_by": sub["staff"], "submitted_at": sub["at"],
            "alerts": snap["alerts"], "not_logged": snap["not_logged"], "temperatures": snap["temps"],
            "jobs_done": [j for j in snap["jobs"] if j["done"]], "jobs_not_done": [j for j in snap["jobs"] if not j["done"]],
            "prep": snap.get("prep", []), "hotbox_rotation": snap.get("hotbox", []), "fuel": snap.get("fuel", []),
            "food_temps": snap.get("food_temps", []), "hotbox_items": snap.get("hotbox_items", []), "discards": snap.get("discards", []),
            "shifts": snap["shifts"]}

def post_webhook(sub_id, payload):
    """POST JSON to WEBHOOK_URL with Authorization: WEBHOOK_AUTH. Returns a short status string (no secrets)."""
    import urllib.request, urllib.error
    url = os.environ.get("WEBHOOK_URL", "").strip()
    auth = re.sub(r"^\s*Authorization:\s*", "", os.environ.get("WEBHOOK_AUTH", ""), flags=re.I).strip()
    if not url: st = "not_configured"
    else:
        hd = {"Content-Type": "application/json", "User-Agent": "silkwood-dashboard"}
        if auth: hd["Authorization"] = auth
        try:
            with urllib.request.urlopen(urllib.request.Request(url, json.dumps(payload).encode(), hd, method="POST"), timeout=20) as r:
                st = f"sent {r.status}"
        except urllib.error.HTTPError as e: st = f"failed HTTP {e.code}"
        except Exception as e: st = f"failed {type(e).__name__}"
    if sub_id is not None:
        c = db(); c.execute("UPDATE submissions SET webhook_status=? WHERE id=?", (st, sub_id)); c.commit(); c.close()
    return st

CLOSING_TASKS = ['coffee_cleaned','coffee_milk','coffee_cups','coffee_sugar','coffee_water','coffee_tray',
    'hotbox_pans','hotbox_interior','hotbox_glass','hotbox_exterior','hotbox_papers',
    'toilet_cleaned','toilet_towels','toilet_rolls','toilet_bins','toilet_floors','outside_bins','outside_buckets',
    'kitchen_floor','kitchen_fryers_off','kitchen_oil_filter','kitchen_benches','kitchen_crumbs','kitchen_hotplate',
    'kitchen_switches','kitchen_coolroom','kitchen_bin','kitchen_expired','eod_shift','eod_pos','eod_pump_photos','eod_dip_photos',
    'lock_pumps','lock_cigs','lock_float','lock_ice']
CLOSING_PAPERS = ['cairnsPost','courierMail','saturdayMail','sundayMail','australian','weekendPost','weekendAustralian']

PHOTO_MAX = 1_500_000      # bytes after browser resize (~1600px JPEG q0.7 is usually 150-500 KB)
PHOTO_PER_KIND = 10
def public_base():
    return (os.environ.get("PUBLIC_BASE_URL") or os.environ.get("RENDER_EXTERNAL_URL") or "https://silkwood-dashboard.onrender.com").rstrip("/")

def photo_upload(b):
    import base64, secrets
    kind = b.get("kind")
    if kind not in ("pump", "dip"): return 400, {"error": "kind must be pump or dip"}
    try: raw = base64.b64decode(str(b.get("data") or ""), validate=True)
    except Exception: return 400, {"error": "bad image data"}
    if not raw: return 400, {"error": "empty image"}
    if len(raw) > PHOTO_MAX: return 413, {"error": "photo too large after resize (max 1.5 MB)"}
    if not (raw[:3] == b"\xff\xd8\xff" and raw[-2:] == b"\xff\xd9"): return 415, {"error": "only JPEG photos are accepted"}
    pid = secrets.token_urlsafe(24)
    c = db(); ts = now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        c.execute("DELETE FROM closing_photos WHERE submitted=0 AND at<?", ((now() - timedelta(days=3)).strftime("%Y-%m-%d %H:%M:%S"),))
        c.execute("INSERT INTO closing_photos(id,kind,mime,size,data,staff,at,submitted) VALUES(?,?,?,?,?,?,?,0)",
                  (pid, kind, "image/jpeg", len(raw), raw, str(b.get("staff") or "")[:40], ts))
        c.commit()
    finally: c.close()
    return 200, {"ok": True, "id": pid, "url": f"{public_base()}/p/{pid}.jpg", "size": len(raw)}

def closing_submit(b):
    """Validate the Closing Checklist and forward it server-side (same payload as the Apps Script form).
    The webhook URL and bearer key live only in env (CLOSING_WEBHOOK_URL / CLOSING_WEBHOOK_KEY)."""
    import urllib.request, urllib.error
    d = str(b.get("checklistDate") or "").strip(); staff = str(b.get("staffName") or "").strip()[:60]
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", d): return 400, {"error": "Choose the checklist date."}
    if not staff: return 400, {"error": "Enter staff name."}
    tasks_in = b.get("tasks") if isinstance(b.get("tasks"), dict) else {}
    tasks = {}
    for k in CLOSING_TASKS:
        v = str(tasks_in.get(k) or "").strip().lower(); v = "na" if v == "n/a" else v
        if v not in ("done", "na"): return 400, {"error": f"Mark Done or N/A for every task (missing: {k})"}
        tasks[k] = v
    cm_in = b.get("taskComments") if isinstance(b.get("taskComments"), dict) else {}
    comments = {k: str(cm_in[k]).strip()[:300] for k in CLOSING_TASKS if str(cm_in.get(k) or "").strip()}
    papers = {}
    for k in CLOSING_PAPERS:
        v = str(b.get(k) if b.get(k) is not None else "").strip()
        if not v: papers[k] = 0; continue
        if not re.fullmatch(r"\d{1,4}", v): return 400, {"error": "Invalid newspaper quantity"}
        papers[k] = int(v)
    ph_in = b.get("photos") if isinstance(b.get("photos"), dict) else {}
    photos = {"pump": [], "dip": []}; ids = []
    c = db()
    try:
        for kind in ("pump", "dip"):
            lst = ph_in.get(kind) or []
            if not isinstance(lst, list) or len(lst) > PHOTO_PER_KIND: return 400, {"error": f"up to {PHOTO_PER_KIND} {kind} photos"}
            for pid in lst:
                pid = str(pid)
                if not re.fullmatch(r"[A-Za-z0-9_-]{32}", pid) or not c.execute("SELECT 1 FROM closing_photos WHERE id=? AND kind=?", (pid, kind)).fetchone():
                    return 400, {"error": f"a {kind} photo is missing, please re-add it"}
                photos[kind].append(f"{public_base()}/p/{pid}.jpg"); ids.append(pid)
    finally: c.close()
    test = b.get("test") is True
    body = {"type": "closing_checklist", "site": "United Silkwood", "checklistDate": d, "staffName": staff,
            "tasks": tasks, "taskComments": comments, "newspapers": papers,
            "dayComments": str(b.get("dayComments") or "").strip()[:1000],
            "submittedAt": datetime.now(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%S.000Z"), "source": "silkwood_dashboard",
            "photos": photos, "pumpPhotoCount": len(photos["pump"]), "dipPhotoCount": len(photos["dip"])}
    if test: body.update({"test": True, "dayComments": "TEST ONLY - do not lodge iDeliver returns. " + body["dayComments"]})
    url = os.environ.get("CLOSING_WEBHOOK_URL", "").strip(); key = os.environ.get("CLOSING_WEBHOOK_KEY", "").strip()
    if not url or not key: return 503, {"error": "Closing checklist is not configured on the server"}
    hd = {"Content-Type": "application/json", "User-Agent": "silkwood-dashboard", "Authorization": "Bearer " + key}
    try:
        with urllib.request.urlopen(urllib.request.Request(url, json.dumps(body).encode(), hd, method="POST"), timeout=25) as r:
            st = f"sent {r.status}"; ok = 200 <= r.status < 300
    except urllib.error.HTTPError as e: st, ok = f"failed HTTP {e.code}", False
    except Exception as e: st, ok = f"failed {type(e).__name__}", False
    if ok and ids:
        try:
            c = db()
            for pid in ids: c.execute("UPDATE closing_photos SET submitted=1 WHERE id=?", (pid,))
            c.commit(); c.close()
        except Exception: pass
    try:
        c = db(); c.execute("INSERT INTO audit(date,at,staff,action,detail) VALUES(?,?,?,?,?)",
            (d, now().strftime("%Y-%m-%d %H:%M:%S"), staff, "closing" + ("-test" if test else ""), json.dumps({"status": st, "newspapers": papers, "pumpPhotos": len(photos["pump"]), "dipPhotos": len(photos["dip"])})[:500]))
        c.commit(); c.close()
    except Exception: pass
    return (200, {"ok": True, "status": st}) if ok else (502, {"error": "Webhook " + st + ". Try again."})

class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def send(self, code, obj, ctype="application/json"):
        b = obj if isinstance(obj, bytes) else json.dumps(obj).encode()
        self.send_response(code); self.send_header("Content-Type", ctype); self.send_header("Cache-Control", "no-store")
        self.send_header("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        self.send_header("X-Content-Type-Options", "nosniff"); self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")
    def do_GET(self):
        u = urlparse(self.path); q = {k: v[0] for k, v in parse_qs(u.query).items()}
        if u.path in ("/", "/index.html"):
            return self.send(200, (ROOT / "index.html").read_bytes(), "text/html; charset=utf-8")
        if u.path == "/healthz": return self.send(200, {"ok": True})
        m = re.fullmatch(r"/p/([A-Za-z0-9_-]{32})\.jpg", u.path)
        if m:  # unguessable capability link (192-bit random id) so the webhook / routines can fetch it
            c = db()
            try: r = c.execute("SELECT data FROM closing_photos WHERE id=?", (m.group(1),)).fetchone()
            finally: c.close()
            if not r: return self.send(404, {"error": "not found"})
            self.send_response(200); self.send_header("Content-Type", "image/jpeg"); self.send_header("Content-Length", str(len(bytes(r["data"]))))
            self.send_header("Cache-Control", "private, max-age=86400"); self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'none'"); self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Robots-Tag", "noindex"); self.end_headers(); self.wfile.write(bytes(r["data"])); return
        if u.path == "/sw.js":  # served from root so its scope covers the whole app
            return self.send(200, (ROOT / "static" / "sw.js").read_bytes(), "text/javascript; charset=utf-8")
        m = re.fullmatch(r"/static/([a-z0-9-]+\.(png|webmanifest|js))", u.path)
        if m and (ROOT / "static" / m.group(1)).is_file():
            ct = {"png": "image/png", "webmanifest": "application/manifest+json", "js": "text/javascript; charset=utf-8"}[m.group(2)]
            return self.send(200, (ROOT / "static" / m.group(1)).read_bytes(), ct)
        if u.path in ("/apple-touch-icon.png", "/apple-touch-icon-precomposed.png", "/favicon.ico"):
            return self.send(200, (ROOT / "static" / ("icon-192.png" if u.path == "/favicon.ico" else "apple-touch-icon.png")).read_bytes(), "image/png")
        if not self.authed(): return self.send(401, {"error": "staff PIN required", "pin": True})
        m = re.fullmatch(r"/submission/(\d+)", u.path)
        if m:
            c = db(); r = c.execute("SELECT * FROM submissions WHERE id=?", (int(m.group(1)),)).fetchone(); c.close()
            if not r: return self.send(404, {"error": "not found"})
            r = dict(r); return self.send(200, snapshot_html(r, json.loads(r["snapshot"]), True).encode(), "text/html; charset=utf-8")
        if u.path == "/api/test-email":  # manager only; sends synchronously and reports the result
            if q.get("pin") != MANAGER_PIN: return self.send(403, {"error": "wrong PIN"})
            who = (q.get("staff") or "manager")[:40]
            st, info = send_email(None, f"TEST – Silkwood dashboard email check – {now():%Y-%m-%d %H:%M}",
                f'<div style="font-family:system-ui;padding:16px"><h2 style="color:#C62828">TEST email</h2><p>Sent from the Silkwood dashboard by {H_.escape(who)} at {now():%Y-%m-%d %H:%M} (Brisbane). Daily submissions will arrive like this.</p></div>')
            return self.send(200, {"status": st, "info": info})
        if u.path == "/api/test-webhook":  # manager only; sends a clearly-marked TEST payload synchronously
            if q.get("pin") != MANAGER_PIN: return self.send(403, {"error": "wrong PIN"})
            t = now().strftime("%Y-%m-%d %H:%M:%S")
            st = post_webhook(None, {"TEST": True, "note": "TEST payload from the Silkwood dashboard — not a real submission",
                "date": t[:10], "area": "kitchen", "submitted_by": "TEST", "submitted_at": t,
                "alerts": ["TEST Kitchen Freezer 1: -10.0°C"], "not_logged": ["TEST Walk-In Cool Room"],
                "temperatures": [{"unit": "TEST Kitchen Freezer 1", "temp": -10.0, "ok": False, "at": t[11:16], "staff": "TEST"}],
                "jobs_done": [{"title": "TEST Wipe benches", "done": True, "by": "TEST", "at": t[11:16]}],
                "jobs_not_done": [{"title": "TEST Clean fryer oil", "done": False}],
                "prep": [{"item": "TEST Chips", "qty": "2", "staff": "TEST"}], "hotbox_rotation": [{"box": "TEST Hot Box", "item": "Pies", "qty": "6", "time": t[11:16]}],
                "fuel": [], "shifts": [{"staff": "TEST", "area": "kitchen", "start_at": t[:11] + "06:00:00", "end_at": t, "hours": 1.0}]})
            return self.send(200, {"status": st})
        if u.path == "/api/pincheck":  # manager PIN check (read-only)
            return self.send(200 if q.get("pin") == MANAGER_PIN else 403, {"ok": q.get("pin") == MANAGER_PIN})
        if u.path == "/api/day":
            d = q.get("date") or today()
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", d): return self.send(400, {"error": "bad date"})
            return self.send(200, day_state(d))
        if u.path == "/api/history":
            c = db()
            dates = sorted({list(dict(r).values())[0] for r in c.execute("SELECT date FROM logs UNION SELECT date FROM task_done UNION SELECT date FROM days UNION SELECT date FROM audit UNION SELECT date FROM shifts")}, reverse=True)[:60]
            out = []
            for d in dates:
                s = day_state(d, extras=False)
                names = sorted({r["staff"] for r in c.execute("SELECT staff FROM logs WHERE date=? UNION SELECT staff FROM task_done WHERE date=? UNION SELECT staff FROM audit WHERE date=?", (d, d, d)) if r["staff"]})
                out.append({"date": d, "locked": s["locked"], "summary": s["summary"], "logs": len(s["logs"]), "staff": names, "shifts": s["shifts"],
                            "submissions": [dict(r) for r in c.execute("SELECT id,area,staff,at,email_status,email_error,webhook_status FROM submissions WHERE date=? ORDER BY id", (d,))]})
            c.close(); return self.send(200, out)
        self.send(404, {"error": "not found"})
    def authed(self):
        return not STAFF_PIN or self.headers.get("X-Staff-Pin", "") == STAFF_PIN
    def do_POST(self):
        if not self.authed(): return self.send(401, {"error": "staff PIN required", "pin": True})
        if urlparse(self.path).path == "/api/closing/photo":
            n = int(self.headers.get("Content-Length") or 0)
            if n > 2_200_000: return self.send(413, {"error": "photo too large (max 1.5 MB)"})
            try: b = self.body()
            except Exception: return self.send(400, {"error": "bad json"})
            if not isinstance(b, dict): return self.send(400, {"error": "bad json"})
            return self.send(*photo_upload(b))
        if urlparse(self.path).path == "/api/closing":
            try: b = self.body()
            except Exception: return self.send(400, {"error": "bad json"})
            if not isinstance(b, dict): return self.send(400, {"error": "bad json"})
            return self.send(*closing_submit(b))
        self.handle_write("POST")
    def do_DELETE(self):
        if not self.authed(): return self.send(401, {"error": "staff PIN required", "pin": True})
        self.handle_write("DELETE")
    def handle_write(self, m):
        try: b = self.body()
        except Exception: return self.send(400, {"error": "bad json"})
        p = urlparse(self.path).path; c = db(); ts = now().strftime("%Y-%m-%d %H:%M:%S")
        d = b.get("date") or today()
        def locked(): return c.execute("SELECT 1 FROM days WHERE date=?", (d,)).fetchone()
        staff = (b.get("staff") or "").strip()[:40]; after = []
        try:
            if not staff:  # every write must carry the staff member's name
                return self.send(400, {"error": "staff name required", "name": True})
            if p in ("/api/tick", "/api/log", "/api/submit", "/api/log/delete", "/api/food", "/api/food/delete",
                     "/api/hotbox", "/api/hotbox/out", "/api/hotbox/delete", "/api/discard", "/api/discard/delete") and locked():
                return self.send(409, {"error": f"{d} is submitted and locked"})
            if p == "/api/task":
                if b.get("delete"): c.execute("UPDATE tasks SET active=0 WHERE id=?", (b["id"],))
                else:
                    title = (b.get("title") or "").strip()[:120]
                    if not title or b.get("tab") not in ("kitchen", "shop") or b.get("repeat") not in ("once", "daily", "weekly"):
                        return self.send(400, {"error": "title, tab and repeat required"})
                    wd = date.fromisoformat(d).weekday() if b["repeat"] == "weekly" else None
                    if b.get("weekday") not in (None, ""): wd = int(b["weekday"])
                    due = b.get("due_time") or None
                    if b.get("id"):
                        c.execute("UPDATE tasks SET title=?,tab=?,repeat=?,weekday=?,due_time=?,example=0 WHERE id=?", (title, b["tab"], b["repeat"], wd, due, b["id"]))
                    else:
                        c.execute("INSERT INTO tasks(tab,title,repeat,weekday,due_time,start_date,created_by) VALUES(?,?,?,?,?,?,?)", (b["tab"], title, b["repeat"], wd, due, d, staff))
            elif p == "/api/tick":
                if b.get("undo"): c.execute("DELETE FROM task_done WHERE task_id=? AND date=?", (b["id"], d))
                else:
                    if not staff: return self.send(400, {"error": "staff name required"})
                    c.execute("INSERT OR REPLACE INTO task_done VALUES(?,?,?,?)", (b["id"], d, staff, ts))
            elif p == "/api/log":
                if not staff: return self.send(400, {"error": "staff name required"})
                kind = b.get("kind"); data = b.get("data") or {}
                if kind not in ("ktemp", "stemp", "prep", "hotbox", "fuel", "tomorrow"): return self.send(400, {"error": "bad kind"})
                bad = 0
                if kind in ("ktemp", "stemp"):
                    it = c.execute("SELECT * FROM items WHERE id=?", (b.get("ref"),)).fetchone()
                    if not it: return self.send(400, {"error": "unknown unit"})
                    try: data["temp"] = float(data.get("temp"))
                    except Exception: return self.send(400, {"error": "temperature required"})
                    data["name"], data["type"] = it["name"], it["type"]; bad = is_bad(it["type"], data["temp"])
                if kind == "fuel":
                    data = {"water": "yes" if data.get("water") == "yes" else "no", "water_mm": data.get("water_mm") or 0,
                            **({"fuel_l": str(data["fuel_l"])[:12]} if data.get("fuel_l") not in (None, "") else {}),
                            **({"notes": str(data["notes"])[:200]} if (data.get("notes") or "").strip() else {})}
                if b.get("log_id"):
                    c.execute("UPDATE logs SET data=?,staff=?,at=?,bad=? WHERE id=? AND date=?", (json.dumps(data), staff, ts, bad, b["log_id"], d))
                else:
                    c.execute("INSERT INTO logs(date,kind,ref,data,staff,at,bad) VALUES(?,?,?,?,?,?,?)", (d, kind, str(b.get("ref") or ""), json.dumps(data), staff, ts, bad))
            elif p == "/api/log/delete":
                c.execute("DELETE FROM logs WHERE id=? AND date=?", (b["id"], d))
            elif p == "/api/food":
                stage = b.get("stage"); food = (b.get("food") or "").strip()[:60]
                if stage not in FOOD_STAGES or not food: return self.send(400, {"error": "food and stage required"})
                try: t = float(b.get("temp"))
                except Exception: return self.send(400, {"error": "temperature required"})
                c.execute("INSERT INTO food_temps(date,food,stage,temp,bad,action,staff,at) VALUES(?,?,?,?,?,?,?,?)",
                          (d, food, stage, t, is_bad(FOOD_STAGES[stage][1], t), (b.get("action") or "")[:120] or None, staff, ts))
            elif p == "/api/hotbox":
                item = (b.get("item") or "").strip()[:60]
                if not item: return self.send(400, {"error": "item required"})
                tin = b.get("time_in") or ts[11:16]
                if not re.fullmatch(r"\d{2}:\d{2}", tin): return self.send(400, {"error": "time must be HH:MM"})
                c.execute("INSERT INTO hotbox_items(date,box,item,qty,time_in,staff,at) VALUES(?,?,?,?,?,?,?)",
                          (d, (b.get("box") or "")[:60], item, str(b.get("qty") or "")[:12], f"{d} {tin}:00", staff, ts))
            elif p == "/api/hotbox/out":
                x = c.execute("SELECT * FROM hotbox_items WHERE id=? AND out_at IS NULL", (b.get("id"),)).fetchone()
                if not x: return self.send(404, {"error": "not in hot box"})
                oc = "discarded" if b.get("outcome") == "discarded" else "sold"
                c.execute("UPDATE hotbox_items SET out_at=?, out_by=?, outcome=? WHERE id=?", (ts, staff, oc, x["id"]))
                if oc == "discarded":
                    c.execute("INSERT INTO discards(date,item,qty,reason,hotbox_id,staff,at) VALUES(?,?,?,?,?,?,?)",
                              (today(), x["item"], x["qty"], (b.get("reason") or "Hot box time expired")[:120], x["id"], staff, ts))
            elif p == "/api/discard":
                item = (b.get("item") or "").strip()[:60]
                if not item: return self.send(400, {"error": "item required"})
                c.execute("INSERT INTO discards(date,item,qty,reason,staff,at) VALUES(?,?,?,?,?,?)",
                          (d, item, str(b.get("qty") or "")[:12], (b.get("reason") or "")[:120], staff, ts))
            elif p in ("/api/food/delete", "/api/hotbox/delete", "/api/discard/delete"):
                tbl = {"food": "food_temps", "hotbox": "hotbox_items", "discard": "discards"}[p.split("/")[2]]
                c.execute(f"DELETE FROM {tbl} WHERE id=? AND date=?", (b.get("id"), d))
            elif p == "/api/item":
                if b.get("delete"): c.execute("UPDATE items SET active=0 WHERE id=?", (b["id"],))
                elif b.get("id"): c.execute("UPDATE items SET name=?,type=COALESCE(?,type),example=0 WHERE id=?", (b["name"].strip()[:60], b.get("type"), b["id"]))
                else:
                    kind = b.get("kind"); typ = b.get("type") or {"fridge": "fridge", "freezer": "freezer"}.get(kind, "")
                    if kind not in ("fridge", "freezer", "kcheck", "prep", "hotbox") or not (b.get("name") or "").strip(): return self.send(400, {"error": "kind and name required"})
                    c.execute("INSERT INTO items(kind,name,type,sort) VALUES(?,?,?,999)", (kind, b["name"].strip()[:60], typ))
            elif p == "/api/submit":
                if not staff: return self.send(400, {"error": "staff name required"})
                ds = day_state(d); s = ds["summary"]; s["shifts"] = ds["shifts"]
                area = b.get("area") if b.get("area") in AREAS else "both"
                c.execute("INSERT INTO days VALUES(?,?,?,?)", (d, staff, ts, json.dumps(s)))
                snap = snapshot(ds, area)
                sid = c.execute("INSERT INTO submissions(date,area,staff,at,snapshot,email_status) VALUES(?,?,?,?,?,?) RETURNING id",
                                (d, area, staff, ts, json.dumps(snap), "sending")).fetchone()["id"]
                sub = {"area": area, "staff": staff, "at": ts}
                subj = f"Silkwood daily log – {d} – {AREAS[area]} – submitted by {staff}"
                after.append(lambda: threading.Thread(target=send_email, args=(sid, subj, snapshot_html(sub, snap)), daemon=True).start())
                after.append(lambda: threading.Thread(target=post_webhook, args=(sid, webhook_payload(sub, snap)), daemon=True).start())
            elif p == "/api/shift":  # staff start/end shift (server time, Brisbane)
                td = today()
                cur = c.execute("SELECT * FROM shifts WHERE date=? AND staff=? AND end_at IS NULL", (td, staff)).fetchone()
                if b.get("action") == "start":
                    if not cur:  # one open shift per name per day; an open one is resumed
                        area = b.get("area") if b.get("area") in ("kitchen", "shop") else ""
                        c.execute("INSERT INTO shifts(date,staff,area,start_at) VALUES(?,?,?,?)", (td, staff, area, ts))
                elif b.get("action") == "end":
                    if not cur: return self.send(409, {"error": "no open shift"})
                    c.execute("UPDATE shifts SET end_at=?, hours=? WHERE id=?", (ts, shift_hours(cur["start_at"], ts), cur["id"]))
                else: return self.send(400, {"error": "action start or end"})
            elif p in ("/api/shift/fix", "/api/shift/delete"):  # manager only
                if str(b.get("pin")) != MANAGER_PIN: return self.send(403, {"error": "wrong PIN"})
                sh = c.execute("SELECT * FROM shifts WHERE id=?", (b.get("id"),)).fetchone()
                if not sh: return self.send(404, {"error": "no such shift"})
                if p.endswith("delete"): c.execute("DELETE FROM shifts WHERE id=?", (sh["id"],))
                else:
                    def at(hm, old):
                        if not hm: return old
                        if not re.fullmatch(r"\d{2}:\d{2}", hm): raise ValueError("time must be HH:MM")
                        return f'{sh["date"]} {hm}:00'
                    try: st, en = at(b.get("start"), sh["start_at"]), at(b.get("end"), sh["end_at"])
                    except ValueError as e: return self.send(400, {"error": str(e)})
                    if en and en < st: return self.send(400, {"error": "end is before start"})
                    c.execute("UPDATE shifts SET start_at=?, end_at=?, hours=? WHERE id=?", (st, en, shift_hours(st, en) if en else None, sh["id"]))
            elif p == "/api/unlock":  # manager reopen
                if str(b.get("pin")) != MANAGER_PIN: return self.send(403, {"error": "wrong PIN"})
                c.execute("DELETE FROM days WHERE date=?", (d,))
            else: return self.send(404, {"error": "not found"})
            detail = {k: v for k, v in b.items() if k not in ("pin", "staff", "date")}
            c.execute("INSERT INTO audit(date,at,staff,action,detail) VALUES(?,?,?,?,?)", (d, ts, staff, p.removeprefix("/api/"), json.dumps(detail)[:500]))
            c.commit()
            for f in after: f()
        finally: c.close()
        self.send(200, day_state(d))

if __name__ == "__main__":
    init()
    print(f"Silkwood dashboard on :{PORT} ({'postgres' if PG else DB})", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
