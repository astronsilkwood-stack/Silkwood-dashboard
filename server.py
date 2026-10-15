#!/usr/bin/env python3
"""Silkwood daily ops dashboard: stdlib HTTP server + SQLite. Run: python3 server.py (port 8787)."""
import json, sqlite3, os, re
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
    CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY, date TEXT, at TEXT, staff TEXT, action TEXT, detail TEXT);
    """)
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
    for t in c.execute("SELECT * FROM tasks WHERE active=1 AND start_date<=? ORDER BY due_time IS NULL, due_time, id", (d,)):
        t = dict(t)
        if t["repeat"] == "weekly" and t["weekday"] != wd: continue
        if t["repeat"] == "once" and t["start_date"] != d:
            # carry an undone one-off forward to later days
            if c.execute("SELECT 1 FROM task_done WHERE task_id=?", (t["id"],)).fetchone() or d != today(): continue
        dn = c.execute("SELECT staff,at FROM task_done WHERE task_id=? AND date=?", (t["id"], d)).fetchone()
        t["done"] = dict(dn) if dn else None
        out.append(t)
    return out

def day_state(d):
    c = db()
    items = [dict(r) for r in c.execute("SELECT * FROM items WHERE active=1 ORDER BY kind, sort, id")]
    logs = [dict(r, data=json.loads(r["data"])) for r in c.execute("SELECT * FROM logs WHERE date=? ORDER BY at", (d,))]
    tasks = tasks_for(c, d)
    lock = c.execute("SELECT * FROM days WHERE date=?", (d,)).fetchone()
    shifts = [dict(r) for r in c.execute("SELECT * FROM shifts WHERE date=? ORDER BY start_at", (d,))]
    c.close()
    return {"date": d, "today": today(), "items": items, "logs": logs, "tasks": tasks, "tanks": TANKS,
            "rules": RULES, "locked": dict(lock) if lock else None, "shifts": shifts, "summary": summarise(items, logs, tasks)}

def shift_hours(a, b):
    f = "%Y-%m-%d %H:%M:%S"
    return round((datetime.strptime(b, f) - datetime.strptime(a, f)).total_seconds() / 3600, 2)

def summarise(items, logs, tasks):
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
    return {"tasks_done": sum(v["tasks_done"] for v in tabs.values()), "tasks_total": sum(v["tasks_total"] for v in tabs.values()),
            "missing": sum(len(v["missing"]) for v in tabs.values()), "alerts": alerts, "tabs": tabs}

class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def send(self, code, obj, ctype="application/json"):
        b = obj if isinstance(obj, bytes) else json.dumps(obj).encode()
        self.send_response(code); self.send_header("Content-Type", ctype); self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")
    def do_GET(self):
        u = urlparse(self.path); q = {k: v[0] for k, v in parse_qs(u.query).items()}
        if u.path in ("/", "/index.html"):
            return self.send(200, (ROOT / "index.html").read_bytes(), "text/html; charset=utf-8")
        if u.path == "/healthz": return self.send(200, {"ok": True})
        if not self.authed(): return self.send(401, {"error": "staff PIN required", "pin": True})
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
                s = day_state(d)
                names = sorted({r["staff"] for r in c.execute("SELECT staff FROM logs WHERE date=? UNION SELECT staff FROM task_done WHERE date=? UNION SELECT staff FROM audit WHERE date=?", (d, d, d)) if r["staff"]})
                out.append({"date": d, "locked": s["locked"], "summary": s["summary"], "logs": len(s["logs"]), "staff": names, "shifts": s["shifts"]})
            c.close(); return self.send(200, out)
        self.send(404, {"error": "not found"})
    def authed(self):
        return not STAFF_PIN or self.headers.get("X-Staff-Pin", "") == STAFF_PIN
    def do_POST(self):
        if not self.authed(): return self.send(401, {"error": "staff PIN required", "pin": True})
        self.handle_write("POST")
    def do_DELETE(self): self.handle_write("DELETE")
    def handle_write(self, m):
        try: b = self.body()
        except Exception: return self.send(400, {"error": "bad json"})
        p = urlparse(self.path).path; c = db(); ts = now().strftime("%Y-%m-%d %H:%M:%S")
        d = b.get("date") or today()
        def locked(): return c.execute("SELECT 1 FROM days WHERE date=?", (d,)).fetchone()
        staff = (b.get("staff") or "").strip()[:40]
        try:
            if not staff:  # every write must carry the staff member's name
                return self.send(400, {"error": "staff name required", "name": True})
            if p in ("/api/tick", "/api/log", "/api/submit", "/api/log/delete") and locked():
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
                if b.get("log_id"):
                    c.execute("UPDATE logs SET data=?,staff=?,at=?,bad=? WHERE id=? AND date=?", (json.dumps(data), staff, ts, bad, b["log_id"], d))
                else:
                    c.execute("INSERT INTO logs(date,kind,ref,data,staff,at,bad) VALUES(?,?,?,?,?,?,?)", (d, kind, str(b.get("ref") or ""), json.dumps(data), staff, ts, bad))
            elif p == "/api/log/delete":
                c.execute("DELETE FROM logs WHERE id=? AND date=?", (b["id"], d))
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
                c.execute("INSERT INTO days VALUES(?,?,?,?)", (d, staff, ts, json.dumps(s)))
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
        finally: c.close()
        self.send(200, day_state(d))

if __name__ == "__main__":
    init()
    print(f"Silkwood dashboard on :{PORT} ({'postgres' if PG else DB})", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
