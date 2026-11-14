"""Owner portal: login, nightly ingest, sales pages, till exceptions, invoices, and staff hours.

Separate from the staff PIN. Pages and JSON under /portal and /api/portal require a
session cookie, except the login page and the bearer-key ingest/hours endpoints.
The portal never takes a payment and never posts payroll. Mark as paid only
stores the owner's own record. Pay rates and contract costs are shown only on
the logged-in pages. They are not in the staff-hours feed.
"""
import csv, gzip, hashlib, hmac, io, json, os, re, secrets, zlib
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from urllib.parse import parse_qs, quote, urlencode, urlparse

DATASETS = (
    "sales", "purchases", "fuel-commission", "fuel-dips", "insights", "invoices",
    "product-search", "retail-pricing", "till-price-changes", "shifts", "last-year", "cost-overrides",
    "stock-movements", "online-orders", "shift-exceptions", "barcode-map", "last-year-monthly",
    "pay-rates",
)
INGEST_MAX = 8_000_000
FORM_MAX = 100_000
SESSION_COOKIE = "silkwood_portal"
CSRF_COOKIE = "silkwood_csrf"
CSP = (
    "default-src 'self'; img-src 'self'; style-src 'self'; script-src 'self'; "
    "form-action 'self'; base-uri 'self'; frame-ancestors 'none'; object-src 'none'; connect-src 'self'"
)
SENSITIVE_KEYS = {
    "card", "card_number", "cardnumber", "pan", "cvv", "cvc", "ccv", "security_code",
    "account", "account_number", "accountnumber", "bank", "bank_account", "bankaccount",
    "bsb", "routing", "routing_number", "iban", "sort_code", "expiry", "exp_month", "exp_year",
}
# Queensland statewide public holidays. The Brisbane show day is not included.
# Dates follow the Queensland Government public-holiday list (qld.gov.au).
HOLIDAYS = {
    "2025-01-01": "New Year's Day", "2025-01-27": "Australia Day",
    "2025-04-18": "Good Friday", "2025-04-19": "The day after Good Friday",
    "2025-04-20": "Easter Sunday", "2025-04-21": "Easter Monday", "2025-04-25": "Anzac Day",
    "2025-05-05": "Labour Day", "2025-10-06": "King's Birthday",
    "2025-12-25": "Christmas Day", "2025-12-26": "Boxing Day",
    "2026-01-01": "New Year's Day", "2026-01-26": "Australia Day",
    "2026-04-03": "Good Friday", "2026-04-04": "The day after Good Friday",
    "2026-04-05": "Easter Sunday", "2026-04-06": "Easter Monday", "2026-04-25": "Anzac Day",
    "2026-05-04": "Labour Day", "2026-10-05": "King's Birthday",
    "2026-12-25": "Christmas Day", "2026-12-26": "Boxing Day", "2026-12-28": "Boxing Day holiday",
    "2027-01-01": "New Year's Day", "2027-01-26": "Australia Day",
    "2027-03-26": "Good Friday", "2027-03-27": "The day after Good Friday",
    "2027-03-28": "Easter Sunday", "2027-03-29": "Easter Monday", "2027-04-26": "Anzac Day",
    "2027-05-03": "Labour Day", "2027-10-04": "King's Birthday",
    "2027-12-25": "Christmas Day", "2027-12-26": "Boxing Day",
    "2027-12-27": "Christmas Day holiday", "2027-12-28": "Boxing Day holiday",
    "2028-01-01": "New Year's Day", "2028-01-03": "New Year's Day holiday", "2028-01-26": "Australia Day",
    "2028-04-14": "Good Friday", "2028-04-15": "The day after Good Friday",
    "2028-04-16": "Easter Sunday", "2028-04-17": "Easter Monday", "2028-04-25": "Anzac Day",
    "2028-05-01": "Labour Day", "2028-10-02": "King's Birthday",
    "2028-12-25": "Christmas Day", "2028-12-26": "Boxing Day",
    "2029-01-01": "New Year's Day", "2029-01-26": "Australia Day",
    "2029-03-30": "Good Friday", "2029-03-31": "The day after Good Friday",
    "2029-04-01": "Easter Sunday", "2029-04-02": "Easter Monday", "2029-04-25": "Anzac Day",
    "2029-05-07": "Labour Day", "2029-10-01": "King's Birthday",
    "2029-12-25": "Christmas Day", "2029-12-26": "Boxing Day",
}
PART_HOLIDAYS = {
    "2025-12-24": "Christmas Eve (from 6pm)", "2026-12-24": "Christmas Eve (from 6pm)",
    "2027-12-24": "Christmas Eve (from 6pm)", "2028-12-24": "Christmas Eve (from 6pm)",
    "2029-12-24": "Christmas Eve (from 6pm)",
}
NAV = [
    ("/portal", "Overview", "overview"),
    ("/portal/day", "Daily summary", "daily"),
    ("/portal/online", "Online orders", "online"),
    ("/portal/exceptions", "Till exceptions", "exceptions"),
    ("/portal/products", "Products", "products"),
    ("/portal/stock", "Stock on hand", "stock"),
    ("/portal/pricing", "Pricing", "pricing"),
    ("/portal/till-prices", "Till price changes", "till"),
    ("/portal/departments", "Departments & products", "departments"),
    ("/portal/kitchen", "Kitchen", "kitchen"),
    ("/portal/purchases", "Purchases", "purchases"),
    ("/portal/dips", "Pumps & dips", "dips"),
    ("/portal/shifts", "Shift balancing", "shifts"),
    ("/portal/commission", "Commission", "commission"),
    ("/portal/fuel", "Fuel commission", "fuel"),
    ("/portal/invoices", "Invoices", "invoices"),
    ("/portal/hours", "Staff hours", "hours"),
    ("/portal/notes", "Data notes", "notes"),
]
REDIRECT_OK = {href for href, _label, _key in NAV} | {"/portal/login", "/portal/password"}
PAID_STATUS = {"paid", "p", "void", "voided", "deleted", "draft", "cancelled", "canceled", "absent", "d"}
XERO_PAID = {"paid", "p"}
PAYMENT_METHODS = ("Card", "Cash", "Bank transfer", "Direct debit")
_dummy_hash = None
_ingest_hits = {}

SCHEMA = """
CREATE TABLE IF NOT EXISTS portal_users(
  id INTEGER PRIMARY KEY, username TEXT NOT NULL UNIQUE, password_hash TEXT NOT NULL,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS portal_sessions(
  token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL, csrf TEXT NOT NULL,
  created_at TEXT NOT NULL, expires_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS portal_login_attempts(
  id INTEGER PRIMARY KEY, username TEXT NOT NULL, ip TEXT NOT NULL, at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ix_portal_attempts ON portal_login_attempts(username, ip, at);
CREATE TABLE IF NOT EXISTS portal_meta(k TEXT PRIMARY KEY, v TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS portal_snapshots(
  dataset TEXT NOT NULL, as_of TEXT NOT NULL, payload TEXT NOT NULL, received_at TEXT NOT NULL,
  PRIMARY KEY(dataset, as_of));
CREATE TABLE IF NOT EXISTS portal_sales_days(
  date TEXT PRIMARY KEY, total REAL, shop REAL, fuel REAL, gp REAL, gp_pct REAL, coverage REAL,
  missing INTEGER, products INTEGER, covered_ex REAL, source_as_of TEXT);
CREATE TABLE IF NOT EXISTS portal_commission_days(
  date TEXT PRIMARY KEY, amount REAL, status TEXT, ulp_l REAL, ulp_cpl REAL, diesel_l REAL, diesel_cpl REAL,
  pulp_l REAL, pulp_cpl REAL, litres REAL, sales_inc REAL, detail TEXT, source_as_of TEXT);
CREATE TABLE IF NOT EXISTS portal_dip_days(date TEXT PRIMARY KEY, payload TEXT NOT NULL, source_as_of TEXT);
CREATE TABLE IF NOT EXISTS portal_bills(
  bill_key TEXT PRIMARY KEY, supplier TEXT NOT NULL, invoice_no TEXT NOT NULL, bill_date TEXT NOT NULL,
  ex_gst REAL, inc_gst REAL, status TEXT, due_date TEXT, amount_due REAL, shop_share REAL, note TEXT, source_as_of TEXT);
CREATE INDEX IF NOT EXISTS ix_portal_bills_date ON portal_bills(bill_date);
CREATE TABLE IF NOT EXISTS portal_insights(
  date TEXT PRIMARY KEY, headline TEXT NOT NULL, bullets TEXT NOT NULL, source TEXT NOT NULL, received_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS portal_invoices(
  invoice_key TEXT PRIMARY KEY, supplier TEXT NOT NULL, invoice_number TEXT NOT NULL, invoice_date TEXT,
  due_date TEXT, amount_due REAL, status TEXT NOT NULL, pay_url TEXT, source_as_of TEXT, updated_at TEXT NOT NULL,
  paid_date TEXT, amount REAL, xero_invoice_id TEXT);
CREATE TABLE IF NOT EXISTS portal_invoice_payments(
  invoice_key TEXT PRIMARY KEY, supplier TEXT NOT NULL, invoice_number TEXT NOT NULL, xero_invoice_id TEXT,
  method TEXT NOT NULL, paid_on TEXT NOT NULL, amount REAL NOT NULL, note TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL, created_by TEXT NOT NULL, updated_by TEXT NOT NULL,
  matched_at TEXT, xero_payment_ref TEXT);
CREATE TABLE IF NOT EXISTS portal_people(
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS portal_roster(
  id INTEGER PRIMARY KEY, person_id INTEGER NOT NULL, work_date TEXT NOT NULL, start_time TEXT NOT NULL,
  finish_time TEXT NOT NULL, break_minutes INTEGER NOT NULL, notes TEXT, hours REAL NOT NULL,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL, created_by TEXT NOT NULL, updated_by TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ix_portal_roster_date ON portal_roster(work_date);
CREATE TABLE IF NOT EXISTS portal_roster_audit(
  id INTEGER PRIMARY KEY, at TEXT NOT NULL, actor TEXT NOT NULL, action TEXT NOT NULL,
  person_id INTEGER, shift_id INTEGER, before_json TEXT, after_json TEXT);
CREATE TABLE IF NOT EXISTS portal_blobs(
  dataset TEXT PRIMARY KEY, as_of TEXT NOT NULL, payload TEXT NOT NULL, received_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS portal_cost_overrides(
  code TEXT NOT NULL, effective_from TEXT NOT NULL, cost_ex_cents INTEGER NOT NULL,
  source TEXT NOT NULL, as_of TEXT NOT NULL, PRIMARY KEY(code, effective_from));
CREATE TABLE IF NOT EXISTS portal_shift_days(
  date TEXT PRIMARY KEY, payload TEXT NOT NULL, source_as_of TEXT);
CREATE TABLE IF NOT EXISTS portal_spend(
  spend_key TEXT PRIMARY KEY, supplier TEXT NOT NULL, spend_date TEXT NOT NULL,
  ex_gst REAL, inc_gst REAL, bucket TEXT NOT NULL, source_as_of TEXT);
CREATE TABLE IF NOT EXISTS portal_shop_days(
  date TEXT PRIMARY KEY, sales_inc REAL NOT NULL, source_as_of TEXT);
CREATE TABLE IF NOT EXISTS portal_stock_counts(
  id INTEGER PRIMARY KEY, code TEXT NOT NULL, qty REAL NOT NULL, baseline_date TEXT NOT NULL,
  baseline_at TEXT NOT NULL, note TEXT, created_at TEXT NOT NULL, created_by TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ix_portal_stock_counts_code ON portal_stock_counts(code, baseline_date);
CREATE TABLE IF NOT EXISTS portal_stock_movements(
  movement_key TEXT PRIMARY KEY, code TEXT NOT NULL, move_date TEXT NOT NULL, source TEXT NOT NULL,
  sold REAL NOT NULL, received REAL NOT NULL, reference TEXT, source_as_of TEXT, updated_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ix_portal_stock_movements_code ON portal_stock_movements(code, move_date);
CREATE TABLE IF NOT EXISTS portal_stock_items(
  code TEXT PRIMARY KEY, name TEXT NOT NULL, barcode TEXT, group_name TEXT,
  manual INTEGER NOT NULL DEFAULT 0, active INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL, updated_by TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS portal_stock_audit(
  id INTEGER PRIMARY KEY, at TEXT NOT NULL, actor TEXT NOT NULL, action TEXT NOT NULL,
  code TEXT NOT NULL, detail TEXT);
CREATE TABLE IF NOT EXISTS portal_product_barcodes(
  barcode TEXT PRIMARY KEY, code TEXT NOT NULL, created_at TEXT NOT NULL, created_by TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ix_portal_product_barcodes_code ON portal_product_barcodes(code);
CREATE TABLE IF NOT EXISTS portal_product_suppliers(
  code TEXT PRIMARY KEY, supplier TEXT NOT NULL, updated_at TEXT NOT NULL, updated_by TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS portal_chat(
  id INTEGER PRIMARY KEY, message TEXT NOT NULL, page TEXT, context TEXT, reply TEXT,
  created_at TEXT NOT NULL, replied_at TEXT, created_by TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS portal_online_days(
  date TEXT PRIMARY KEY, orders INTEGER NOT NULL, total REAL NOT NULL, fees REAL NOT NULL,
  refunds REAL NOT NULL, net REAL NOT NULL, source_as_of TEXT);
CREATE TABLE IF NOT EXISTS portal_online_orders(
  order_no TEXT PRIMARY KEY, ordered_at TEXT NOT NULL, trading_date TEXT NOT NULL,
  wallet TEXT NOT NULL, card_brand TEXT NOT NULL, total REAL NOT NULL, fees REAL NOT NULL,
  refunds REAL NOT NULL, net REAL NOT NULL, source_as_of TEXT,
  customer_name TEXT, order_type TEXT, fulfilment_time TEXT, items TEXT);
CREATE INDEX IF NOT EXISTS ix_portal_online_orders_date ON portal_online_orders(trading_date);
CREATE TABLE IF NOT EXISTS portal_shift_exceptions(
  row_key TEXT PRIMARY KEY, trade_date TEXT NOT NULL, till_time TEXT NOT NULL, exc_type TEXT NOT NULL,
  subtype TEXT, receipt TEXT NOT NULL, staff TEXT NOT NULL, item TEXT NOT NULL, quantity REAL,
  normal_price REAL, charged_price REAL, difference REAL, amount REAL NOT NULL, customer TEXT,
  status TEXT, account_type TEXT, outstanding REAL, paid_on_account REAL, status_basis TEXT,
  plu TEXT, reason TEXT, rego TEXT, lines TEXT, source_as_of TEXT, updated_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ix_portal_shift_exceptions_date ON portal_shift_exceptions(trade_date, exc_type);
CREATE TABLE IF NOT EXISTS portal_ly_monthly(
  month TEXT PRIMARY KEY, shop_sales_inc REAL, food_sales_inc REAL, fuel_litres REAL,
  source TEXT, note TEXT, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS portal_contractors(
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, hourly_rate REAL,
  active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
-- Owner-entered contract hours, one row per date. More than one row on a date is allowed.
-- Nightly ingest does not write these tables.
CREATE TABLE IF NOT EXISTS portal_contract_entries(
  id INTEGER PRIMARY KEY, contractor_id INTEGER NOT NULL, work_date TEXT NOT NULL,
  hours REAL, hourly_rate REAL, flat_amount REAL, cost REAL NOT NULL, notes TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL, created_by TEXT NOT NULL, updated_by TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ix_portal_contract_date ON portal_contract_entries(work_date);
"""


def stock():
    import portal_stock
    return portal_stock


def daypage():
    import portal_day
    return portal_day


def exceptions():
    import portal_exceptions
    return portal_exceptions


def chat():
    import portal_chat
    return portal_chat


def parity():
    import portal_parity
    return portal_parity


def wants(path):
    return path == "/portal" or path.startswith("/portal/") or path.startswith("/api/portal")


def log(msg):
    """Operational notes only. Callers must not pass secrets, hashes, cookies, or connection strings."""
    print("portal: " + msg, flush=True)


def _srv():
    import server
    return server


def db():
    return _srv().db()


def stamp(when=None):
    when = when or _srv().now()
    return when.strftime("%Y-%m-%d %H:%M:%S")


def today():
    return _srv().now().date()


def tone(name):
    """Stable palette slot for a department or group. Visual only."""
    total = 0
    for ch in str(name or "").casefold():
        total = (total * 33 + ord(ch)) & 0xFFFFFFFF
    return "tone-" + str(total % 8)


def h(v):
    import html as _html
    return _html.escape("" if v is None else str(v), quote=True)


def money(n):
    if n is None:
        return "—"
    q = Decimal(str(n)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    sign = "-" if q < 0 else ""
    return sign + "$" + f"{abs(q):,.2f}"


def litres(n):
    if n is None:
        return "—"
    q = Decimal(str(n)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return f"{q:,.2f} L"


def pct_ratio(n):
    if n is None:
        return "—"
    return f"{float(n) * 100:.1f}%"


def pct_points(n):
    if n is None:
        return "—"
    return f"{float(n):.1f}%"


def hours_text(n):
    if n is None:
        return "—"
    q = Decimal(str(n)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    s = f"{q:.2f}"
    if s.endswith("00"):
        return s[:-3]
    if s.endswith("0"):
        return s[:-1]
    return s


def fmt_date(s):
    d = date.fromisoformat(s)
    return f"{d.day} {d.strftime('%b')} {d.year}"


def fmt_stamp(s):
    try:
        d = datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return h(s)
    hour = d.hour % 12 or 12
    am = "am" if d.hour < 12 else "pm"
    return f"{d.day} {d.strftime('%b')} {d.year}, {hour}:{d.minute:02d} {am}"


def need_date(v, label):
    if not isinstance(v, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
        raise ValueError(f"{label} must be YYYY-MM-DD")
    try:
        date.fromisoformat(v)
    except ValueError:
        raise ValueError(f"{label} must be YYYY-MM-DD")
    return v


def num(v, label, required=True):
    if v is None or v == "":
        if required:
            raise ValueError(f"{label} is required")
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float, str)):
        raise ValueError(f"{label} must be a number")
    try:
        n = float(v)
    except (TypeError, ValueError):
        raise ValueError(f"{label} must be a number")
    if n != n or n in (float("inf"), float("-inf")) or abs(n) > 1e12:
        raise ValueError(f"{label} is out of range")
    return n


def whole(v, label, required=False):
    if v is None or v == "":
        if required:
            raise ValueError(f"{label} is required")
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise ValueError(f"{label} must be a whole number")
    if isinstance(v, float) and not v.is_integer():
        raise ValueError(f"{label} must be a whole number")
    return int(v)


def rows(cur):
    return [dict(r) for r in cur.fetchall()]


def one(cur):
    r = cur.fetchone()
    return dict(r) if r else None


def meta_get(c, k):
    r = one(c.execute("SELECT v FROM portal_meta WHERE k=?", (k,)))
    if not r:
        return {}
    try:
        v = json.loads(r["v"])
    except json.JSONDecodeError:
        return {}
    return v if isinstance(v, dict) else {}


def meta_set(c, k, obj):
    raw = json.dumps(obj, separators=(",", ":"), ensure_ascii=False)
    c.execute(
        "INSERT INTO portal_meta(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
        (k, raw),
    )


def save_snapshot(c, dataset, as_of, data):
    raw = json.dumps(data, separators=(",", ":"), ensure_ascii=False)
    c.execute(
        """INSERT INTO portal_snapshots(dataset,as_of,payload,received_at) VALUES(?,?,?,?)
           ON CONFLICT(dataset,as_of) DO UPDATE SET payload=excluded.payload, received_at=excluded.received_at""",
        (dataset, as_of, raw, stamp()),
    )


def holiday_name(iso):
    return HOLIDAYS.get(iso) or PART_HOLIDAYS.get(iso)


def week_monday(d):
    return d - timedelta(days=d.weekday())


def session_hours():
    try:
        hours = float(os.environ.get("PORTAL_SESSION_HOURS") or 12)
    except ValueError:
        hours = 12
    return min(24 * 7, max(1, hours))


def lockout_max():
    try:
        return max(3, int(os.environ.get("PORTAL_LOCKOUT_MAX") or 5))
    except ValueError:
        return 5


def cookie_secure(handler):
    flag = os.environ.get("PORTAL_COOKIE_SECURE", "").strip()
    if flag == "0":
        return False
    if flag == "1":
        return True
    proto = (handler.headers.get("X-Forwarded-Proto") or "").split(",")[0].strip().lower()
    if proto == "https":
        return True
    return os.environ.get("RENDER", "").strip().lower() in ("true", "1", "yes")


def cookie_pair(name, value, handler, max_age, httponly):
    parts = [f"{name}={value}", "Path=/", "SameSite=Lax", f"Max-Age={int(max_age)}"]
    if httponly:
        parts.append("HttpOnly")
    if cookie_secure(handler):
        parts.append("Secure")
    return ("Set-Cookie", "; ".join(parts))


def read_cookies(handler):
    out = {}
    for part in (handler.headers.get("Cookie") or "").split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def tokens_eq(a, b):
    if not a or not b or len(a) > 200 or len(b) > 200:
        return False
    return hmac.compare_digest(hashlib.sha256(a.encode()).digest(), hashlib.sha256(b.encode()).digest())


def bearer_token(handler):
    header = handler.headers.get("Authorization") or ""
    if not header.lower().startswith("bearer "):
        return ""
    return header[7:].strip()[:256]


def secret_eq(got, expected):
    if not got or not expected:
        return False
    return hmac.compare_digest(
        hashlib.sha256(got.encode()).digest(), hashlib.sha256(expected.encode()).digest()
    )


def ingest_key():
    return os.environ.get("PORTAL_INGEST_KEY", "").strip()


def _hasher():
    from argon2 import PasswordHasher
    return PasswordHasher(time_cost=2, memory_cost=19456, parallelism=1)


def hash_password(password):
    return _hasher().hash(password)


def _dummy():
    global _dummy_hash
    if _dummy_hash is None:
        _dummy_hash = hash_password("not-a-real-portal-password")
    return _dummy_hash


def password_ok(password, stored):
    if not isinstance(password, str):
        password = ""
    try:
        if stored and stored.startswith("$argon2"):
            from argon2.exceptions import VerifyMismatchError, VerificationError, InvalidHashError
            try:
                return _hasher().verify(stored, password)
            except (VerifyMismatchError, VerificationError, InvalidHashError):
                return False
        if stored and stored.startswith(("$2a$", "$2b$", "$2y$")):
            import bcrypt
            try:
                return bcrypt.checkpw(password.encode(), stored.encode())
            except ValueError:
                return False
        _hasher().verify(_dummy(), password)
    except Exception:
        return False
    return False


def valid_stored_hash(stored):
    return isinstance(stored, str) and (
        stored.startswith(("$2a$", "$2b$", "$2y$", "$argon2"))
    ) and 20 < len(stored) < 400 and "\n" not in stored and " " not in stored


def check_new_password(password, username):
    if not isinstance(password, str) or not (10 <= len(password) <= 200):
        return "Use a password of 10 to 200 characters."
    if password.casefold() == (username or "").casefold():
        return "The password cannot match the username."
    return None


def bootstrap_admin():
    """Create the owner from env, or apply a changed env hash. A password changed in the portal stays until the env hash changes."""
    username = os.environ.get("PORTAL_ADMIN_USER", "").strip().casefold()
    stored = os.environ.get("PORTAL_ADMIN_PASSWORD_HASH", "").strip()
    if not username and not stored:
        return
    if not username or not stored:
        log("owner login was not changed; set both PORTAL_ADMIN_USER and PORTAL_ADMIN_PASSWORD_HASH")
        return
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{1,62}", username):
        log("owner login was not changed; PORTAL_ADMIN_USER must be 2 to 63 letters, numbers, dots, dashes or underscores")
        return
    if not valid_stored_hash(stored):
        log("owner login was not changed; PORTAL_ADMIN_PASSWORD_HASH must be an argon2 or bcrypt hash")
        return
    c = db()
    try:
        applied = meta_get(c, "admin_env_hash").get("hash")
        user = one(c.execute("SELECT * FROM portal_users WHERE username=?", (username,)))
        now_s = stamp()
        if user is None:
            c.execute(
                "INSERT INTO portal_users(username,password_hash,created_at,updated_at,active) VALUES(?,?,?,?,1)",
                (username, stored, now_s, now_s),
            )
            meta_set(c, "admin_env_hash", {"hash": stored})
            log("owner login created for " + username)
        elif applied != stored:
            c.execute(
                "UPDATE portal_users SET password_hash=?, updated_at=? WHERE id=?",
                (stored, now_s, user["id"]),
            )
            c.execute("DELETE FROM portal_sessions WHERE user_id=?", (user["id"],))
            meta_set(c, "admin_env_hash", {"hash": stored})
            log("owner password updated from PORTAL_ADMIN_PASSWORD_HASH")
        c.commit()
    finally:
        c.close()


def session_user(handler):
    token = read_cookies(handler).get(SESSION_COOKIE, "")
    if not token or len(token) > 200:
        return None
    digest = hashlib.sha256(token.encode()).hexdigest()
    c = db()
    try:
        row = one(c.execute(
            """SELECT s.token_hash, s.csrf, s.expires_at, s.user_id, u.username, u.active
               FROM portal_sessions s JOIN portal_users u ON u.id=s.user_id WHERE s.token_hash=?""",
            (digest,),
        ))
        if not row or not row["active"]:
            return None
        if row["expires_at"] <= stamp():
            c.execute("DELETE FROM portal_sessions WHERE token_hash=?", (digest,))
            c.commit()
            return None
        return row
    finally:
        c.close()


def start_session(user_id, handler):
    token = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(32)
    created = _srv().now()
    expires = created + timedelta(hours=session_hours())
    digest = hashlib.sha256(token.encode()).hexdigest()
    c = db()
    try:
        c.execute(
            "INSERT INTO portal_sessions(token_hash,user_id,csrf,created_at,expires_at) VALUES(?,?,?,?,?)",
            (digest, user_id, csrf, stamp(created), stamp(expires)),
        )
        c.commit()
    finally:
        c.close()
    age = int(session_hours() * 3600)
    return token, [
        cookie_pair(SESSION_COOKIE, token, handler, age, True),
        cookie_pair(CSRF_COOKIE, csrf, handler, age, False),
    ]


def clear_session(handler):
    token = read_cookies(handler).get(SESSION_COOKIE, "")
    if token:
        c = db()
        try:
            c.execute("DELETE FROM portal_sessions WHERE token_hash=?", (hashlib.sha256(token.encode()).hexdigest(),))
            c.commit()
        finally:
            c.close()
    return [
        cookie_pair(SESSION_COOKIE, "", handler, 0, True),
        cookie_pair(CSRF_COOKIE, "", handler, 0, False),
    ]


def csrf_ok(handler, form, user):
    sent = (form.get("csrf") if isinstance(form, dict) else None) or handler.headers.get("X-CSRF-Token") or ""
    cookie = read_cookies(handler).get(CSRF_COOKIE, "")
    if not user:
        return False
    return tokens_eq(sent, user["csrf"]) and tokens_eq(cookie, user["csrf"])


def login_locked(c, username, ip, when):
    cutoff = stamp(when - timedelta(minutes=15))
    pair = one(c.execute(
        "SELECT COUNT(*) AS n FROM portal_login_attempts WHERE username=? AND ip=? AND at>=?",
        (username, ip, cutoff),
    ))
    ip_n = one(c.execute(
        "SELECT COUNT(*) AS n FROM portal_login_attempts WHERE ip=? AND at>=?",
        (ip, cutoff),
    ))
    limit = lockout_max()
    return (pair["n"] if pair else 0) >= limit or (ip_n["n"] if ip_n else 0) >= limit * 6


def note_failure(c, username, ip, when):
    c.execute("DELETE FROM portal_login_attempts WHERE at<?", (stamp(when - timedelta(days=2)),))
    c.execute("INSERT INTO portal_login_attempts(username,ip,at) VALUES(?,?,?)", (username, ip, stamp(when)))


def client_ip(handler):
    return _srv().client_ip(handler)


def luhn(digits):
    total = 0
    alt = False
    for ch in reversed(digits):
        n = ord(ch) - 48
        if alt:
            n *= 2
            if n > 9:
                n -= 9
        total += n
        alt = not alt
    return total % 10 == 0 and digits != "0" * len(digits)


def has_card_number(text):
    """A full card number: 13–19 digits, spaces or dashes allowed, passing the Luhn check.

    Dollar amounts and invoice fragments stay, because a comma, dot or word breaks the run.
    """
    for match in re.finditer(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)", text or ""):
        digits = re.sub(r"\D", "", match.group())
        if 13 <= len(digits) <= 19 and luhn(digits):
            return True
    return False


def _key_name(k):
    return re.sub(r"[^a-z0-9_]", "", str(k).casefold().replace("-", "_"))


def reject_sensitive(node, where="payload"):
    """Refuse card or bank fields and any string that contains a full card number."""
    if isinstance(node, dict):
        for k, v in node.items():
            if _key_name(k) in SENSITIVE_KEYS:
                raise ValueError(f"{where} must not include card or bank details")
            reject_sensitive(v, where)
    elif isinstance(node, list):
        for v in node:
            reject_sensitive(v, where)
    elif isinstance(node, str):
        if has_card_number(node):
            raise ValueError(f"{where} must not include a card number")


def clean_pay_url(url):
    if url is None or url == "":
        return None
    if not isinstance(url, str):
        raise ValueError("pay_url must be a URL")
    url = url.strip()
    if len(url) > 500:
        raise ValueError("pay_url is too long")
    parts = urlparse(url)
    if parts.scheme not in ("https", "http") or not parts.hostname:
        raise ValueError("pay_url must be an http or https link")
    if parts.username or parts.password:
        raise ValueError("pay_url must not contain a username or password")
    if parts.hostname in ("localhost",) or parts.hostname.endswith(".local"):
        raise ValueError("pay_url must be the supplier's own payment page")
    return url


def read_limited(handler, limit):
    raw_len = handler.headers.get("Content-Length")
    if raw_len is None:
        return b""
    if not re.fullmatch(r"\d{1,8}", raw_len.strip()):
        raise ValueError("bad length")
    n = int(raw_len)
    if n > limit:
        handler.close_connection = True
        raise OverflowError("too large")
    return handler.rfile.read(n) if n else b""


def _gunzip_limited(raw, limit):
    """Decompress one gzip body, stopping once it would pass the ingest limit."""
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(raw)) as gz:
            out = gz.read(limit + 1)
    except (OSError, EOFError, zlib.error):
        raise ValueError("bad gzip")
    if len(out) > limit:
        raise OverflowError("too large")
    return out


def read_form(handler, limit=FORM_MAX):
    raw = read_limited(handler, limit)
    ctype = (handler.headers.get("Content-Type") or "").split(";")[0].strip().lower()
    if ctype == "application/json":
        try:
            body = json.loads(raw.decode("utf-8") or "{}")
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ValueError("bad json")
        if not isinstance(body, dict):
            raise ValueError("bad json")
        return body
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError("bad form")
    return {k: v[0] for k, v in parse_qs(text, keep_blank_values=True).items()}


def upsert_commission(c, d, **fields):
    current = one(c.execute("SELECT * FROM portal_commission_days WHERE date=?", (d,))) or {}
    merged = {
        "amount": fields["amount"] if fields.get("amount") is not None else current.get("amount"),
        "status": fields["status"] if fields.get("status") is not None else current.get("status"),
        "ulp_l": fields["ulp_l"] if fields.get("ulp_l") is not None else current.get("ulp_l"),
        "ulp_cpl": fields["ulp_cpl"] if fields.get("ulp_cpl") is not None else current.get("ulp_cpl"),
        "diesel_l": fields["diesel_l"] if fields.get("diesel_l") is not None else current.get("diesel_l"),
        "diesel_cpl": fields["diesel_cpl"] if fields.get("diesel_cpl") is not None else current.get("diesel_cpl"),
        "pulp_l": fields["pulp_l"] if fields.get("pulp_l") is not None else current.get("pulp_l"),
        "pulp_cpl": fields["pulp_cpl"] if fields.get("pulp_cpl") is not None else current.get("pulp_cpl"),
        "litres": fields["litres"] if fields.get("litres") is not None else current.get("litres"),
        "sales_inc": fields["sales_inc"] if fields.get("sales_inc") is not None else current.get("sales_inc"),
        "detail": fields["detail"] if fields.get("detail") is not None else current.get("detail"),
        "source_as_of": fields.get("source_as_of"),
    }
    c.execute(
        """INSERT INTO portal_commission_days(date,amount,status,ulp_l,ulp_cpl,diesel_l,diesel_cpl,pulp_l,pulp_cpl,litres,sales_inc,detail,source_as_of)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(date) DO UPDATE SET amount=excluded.amount, status=excluded.status, ulp_l=excluded.ulp_l,
             ulp_cpl=excluded.ulp_cpl, diesel_l=excluded.diesel_l, diesel_cpl=excluded.diesel_cpl,
             pulp_l=excluded.pulp_l, pulp_cpl=excluded.pulp_cpl, litres=excluded.litres, sales_inc=excluded.sales_inc,
             detail=excluded.detail, source_as_of=excluded.source_as_of""",
        (d, merged["amount"], merged["status"], merged["ulp_l"], merged["ulp_cpl"], merged["diesel_l"],
         merged["diesel_cpl"], merged["pulp_l"], merged["pulp_cpl"], merged["litres"], merged["sales_inc"],
         merged["detail"], merged["source_as_of"]),
    )


def apply_sales(c, data):
    if not isinstance(data, dict) or not isinstance(data.get("periods"), list) or not data["periods"]:
        raise ValueError("sales data needs a periods list")
    days = {}
    for period in data["periods"]:
        if not isinstance(period, dict):
            continue
        for row in period.get("daily") or []:
            if not isinstance(row, dict):
                raise ValueError("sales daily row must be an object")
            d = need_date(row.get("date"), "sales day")
            days[d] = {
                "total": num(row.get("total"), "total"),
                "shop": num(row.get("shop"), "shop"),
                "fuel": num(row.get("fuel"), "fuel"),
                "gp": num(row.get("gp"), "gp"),
                "gp_pct": num(row.get("gpPct"), "gpPct", required=False),
                "coverage": num(row.get("coverage"), "coverage", required=False),
                "missing": whole(row.get("missing"), "missing"),
                "products": whole(row.get("products"), "products"),
                "covered_ex": num(row.get("coveredEx"), "coveredEx", required=False),
            }
    if not days:
        raise ValueError("sales data has no daily rows")
    as_of = need_date(data.get("asOf") or data.get("latestSalesDate") or max(days), "asOf")
    for d, row in days.items():
        c.execute(
            """INSERT INTO portal_sales_days(date,total,shop,fuel,gp,gp_pct,coverage,missing,products,covered_ex,source_as_of)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(date) DO UPDATE SET total=excluded.total, shop=excluded.shop, fuel=excluded.fuel, gp=excluded.gp,
                 gp_pct=excluded.gp_pct, coverage=excluded.coverage, missing=excluded.missing, products=excluded.products,
                 covered_ex=excluded.covered_ex, source_as_of=excluded.source_as_of""",
            (d, row["total"], row["shop"], row["fuel"], row["gp"], row["gp_pct"], row["coverage"],
             row["missing"], row["products"], row["covered_ex"], as_of),
        )
    comm = data.get("commission") if isinstance(data.get("commission"), dict) else {}
    comm_n = 0
    for row in comm.get("daily") or []:
        if not isinstance(row, dict):
            raise ValueError("commission daily row must be an object")
        d = need_date(row.get("date"), "commission day")
        upsert_commission(
            c, d, amount=num(row.get("amount"), "amount", required=False),
            status=(str(row.get("status")).strip()[:40] if row.get("status") else None),
            ulp_l=num(row.get("ulpL"), "ulpL", required=False), ulp_cpl=num(row.get("ulpCpl"), "ulpCpl", required=False),
            diesel_l=num(row.get("dieselL"), "dieselL", required=False), diesel_cpl=num(row.get("dieselCpl"), "dieselCpl", required=False),
            pulp_l=num(row.get("pulpL"), "pulpL", required=False), pulp_cpl=num(row.get("pulpCpl"), "pulpCpl", required=False),
            litres=None, sales_inc=None, detail=None, source_as_of=as_of,
        )
        comm_n += 1
    meta = meta_get(c, "sales")
    latest = data.get("latestSalesDate") or max(days)
    meta.update({"asOf": as_of, "latestSalesDate": need_date(latest, "latestSalesDate"), "timezone": "Australia/Brisbane"})
    if comm:
        october = comm.get("october") if isinstance(comm.get("october"), dict) else None
        if october:
            october = {k: october.get(k) for k in ("month", "amount", "target", "days", "complete")}
        meta["commission"] = {
            "month": str(comm.get("month") or "")[:40],
            "target": num(comm.get("target"), "target", required=False),
            "amount": num(comm.get("amount"), "amount", required=False),
            "october": october,
        }
    meta_set(c, "sales", meta)
    save_snapshot(c, "sales", as_of, data)
    parity().store_sales_extras(c, data)
    maybe_generate_insights(c, as_of)
    return {"asOf": as_of, "days": len(days), "commissionDays": comm_n}


def apply_purchases(c, data):
    if not isinstance(data, dict):
        raise ValueError("purchases data must be an object")
    suppliers = data.get("suppliers")
    bills = data.get("bills")
    if not isinstance(suppliers, list) or not isinstance(bills, list):
        raise ValueError("purchases data needs suppliers and bills")
    as_of = need_date(data.get("asOf") or data.get("toDate"), "asOf")
    n = 0
    for i, b in enumerate(bills):
        if not isinstance(b, list) or len(b) < 5:
            raise ValueError(f"bills[{i}] is too short")
        idx = b[0]
        if isinstance(idx, bool) or not isinstance(idx, int) or not (0 <= idx < len(suppliers)):
            raise ValueError(f"bills[{i}] has a bad supplier index")
        supplier = str(suppliers[idx]).strip()[:120]
        if not supplier:
            raise ValueError(f"bills[{i}] has no supplier")
        bill_date = need_date(str(b[1]), f"bills[{i}] date")
        invoice_no = str(b[2]).strip()[:80]
        if not invoice_no:
            raise ValueError(f"bills[{i}] has no invoice number")
        note = str(b[9]).strip()[:200] if len(b) > 9 and b[9] else ""
        reject_sensitive({"supplier": supplier, "invoice_no": invoice_no, "note": note}, "purchases")
        due = need_date(str(b[6]), f"bills[{i}] due") if len(b) > 6 and b[6] else None
        key = supplier + "\n" + invoice_no + "\n" + bill_date
        c.execute(
            """INSERT INTO portal_bills(bill_key,supplier,invoice_no,bill_date,ex_gst,inc_gst,status,due_date,amount_due,shop_share,note,source_as_of)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(bill_key) DO UPDATE SET ex_gst=excluded.ex_gst, inc_gst=excluded.inc_gst, status=excluded.status,
                 due_date=excluded.due_date, amount_due=excluded.amount_due, shop_share=excluded.shop_share, note=excluded.note,
                 source_as_of=excluded.source_as_of""",
            (key, supplier, invoice_no, bill_date, num(b[3], "ex GST"), num(b[4], "inc GST"),
             (str(b[5]).strip()[:12] if len(b) > 5 and b[5] is not None else ""),
             due, num(b[7], "amount due", required=False) if len(b) > 7 else None,
             num(b[8], "shop share", required=False) if len(b) > 8 else None, note, as_of),
        )
        n += 1
    meta = meta_get(c, "purchases")
    meta.update({"asOf": as_of, "suppliers": len(suppliers)})
    meta_set(c, "purchases", meta)
    save_snapshot(c, "purchases", as_of, data)
    parity().store_purchase_series(c, data, as_of)
    return {"asOf": as_of, "bills": n}


def _grade_fields(grades):
    out = {"ulp_l": None, "diesel_l": None, "pulp_l": None}
    if not isinstance(grades, list):
        raise ValueError("grades must be a list")
    for g in grades:
        if not isinstance(g, dict):
            raise ValueError("grade row must be an object")
        code = g.get("grade")
        litres_n = num(g.get("litres"), "litres", required=False)
        if code == "ULP":
            out["ulp_l"] = litres_n
        elif code == "DIS":
            out["diesel_l"] = litres_n
        elif code == "P95":
            out["pulp_l"] = litres_n
    return out


def apply_fuel_commission(c, data):
    if not isinstance(data, dict) or not isinstance(data.get("days"), list):
        raise ValueError("fuel-commission data needs a days list")
    if not data["days"]:
        raise ValueError("fuel-commission data has no days")
    as_of = need_date(data.get("asOf") or data.get("latestReported") or data["days"][-1].get("date"), "asOf")
    for row in data["days"]:
        if not isinstance(row, dict):
            raise ValueError("fuel-commission day must be an object")
        d = need_date(row.get("date"), "fuel day")
        grades = _grade_fields(row.get("grades") or [])
        detail = json.dumps(row.get("grades") or [], separators=(",", ":"), ensure_ascii=False)
        upsert_commission(
            c, d, amount=num(row.get("totalCommEx"), "totalCommEx", required=False), status="Reported",
            ulp_l=grades["ulp_l"], ulp_cpl=None, diesel_l=grades["diesel_l"], diesel_cpl=None,
            pulp_l=grades["pulp_l"], pulp_cpl=None, litres=num(row.get("litres"), "litres", required=False),
            sales_inc=num(row.get("salesInc"), "salesInc", required=False), detail=detail, source_as_of=as_of,
        )
    meta = meta_get(c, "fuel")
    mtd = data.get("mtd") if isinstance(data.get("mtd"), dict) else {}
    tomorrow = data.get("tomorrow") if isinstance(data.get("tomorrow"), dict) else None
    basis = data.get("basis") if isinstance(data.get("basis"), dict) else {}
    meta.update({
        "asOf": as_of,
        "latestReported": data.get("latestReported"),
        "mtd": {k: mtd.get(k) for k in ("month", "through", "days", "amount", "target", "daysInMonth", "projectedAtCurrentAvg", "aheadBy", "status", "remaining", "remainingDays", "neededPerDay")},
        "tomorrow": tomorrow,
        "missingCosts": data.get("missingCosts") if isinstance(data.get("missingCosts"), list) else [],
        "basis": {k: basis.get(k) for k in ("margin", "commission", "cost", "board", "targets") if isinstance(basis.get(k), str)},
    })
    meta_set(c, "fuel", meta)
    save_snapshot(c, "fuel-commission", as_of, data)
    return {"asOf": as_of, "days": len(data["days"])}


def apply_fuel_dips(c, data):
    if not isinstance(data, dict):
        raise ValueError("fuel-dips data must be an object")
    days = data.get("days")
    tank_cols = data.get("tankColumns")
    pump_cols = data.get("pumpColumns")
    if not isinstance(days, list) or not days:
        raise ValueError("fuel-dips data needs days")
    if not isinstance(tank_cols, list) or not isinstance(pump_cols, list):
        raise ValueError("fuel-dips data needs tankColumns and pumpColumns")

    def zip_row(cols, row, label):
        if not isinstance(row, list) or len(row) != len(cols):
            raise ValueError(label + " length does not match the columns")
        out = {}
        for col, val in zip(cols, row):
            if isinstance(val, str):
                reject_sensitive(val, "fuel-dips")
                out[str(col)] = val[:80]
            elif isinstance(val, bool) or val is None or isinstance(val, (int, float)):
                if isinstance(val, float) and (val != val or abs(val) == float("inf")):
                    raise ValueError(label + " has a bad number")
                out[str(col)] = val
            else:
                raise ValueError(label + " has a bad value")
        return out

    dates = []
    threshold = data.get("lowThresholdPercent")
    for i, day in enumerate(days):
        if not isinstance(day, dict):
            raise ValueError("dip day must be an object")
        d = need_date(day.get("date"), "dip day")
        dip_time = day.get("dipTime") if isinstance(day.get("dipTime"), str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", day.get("dipTime")) else None
        payload = {
            "date": d,
            "dipTime": dip_time,
            "tanks": [zip_row(tank_cols, row, f"tanks[{i}]") for row in day.get("tanks") or []],
            "pumps": [zip_row(pump_cols, row, f"pumps[{i}]") for row in day.get("pumps") or []],
            "deliveryDetected": day.get("deliveryDetected") if isinstance(day.get("deliveryDetected"), (bool, type(None))) else None,
            "source": Path(str(day.get("source") or "")).name[:120],
            "lowThresholdPercent": threshold if isinstance(threshold, (int, float)) and not isinstance(threshold, bool) else 25,
            "deliveryBasis": str(data.get("deliveryBasis") or "")[:300],
        }
        c.execute(
            """INSERT INTO portal_dip_days(date,payload,source_as_of) VALUES(?,?,?)
               ON CONFLICT(date) DO UPDATE SET payload=excluded.payload, source_as_of=excluded.source_as_of""",
            (d, json.dumps(payload, separators=(",", ":"), ensure_ascii=False), d),
        )
        dates.append(d)
    as_of = max(dates)
    meta_set(c, "dips", {
        "asOf": as_of,
        "days": len(dates),
        "missingDates": data.get("missingDates") if isinstance(data.get("missingDates"), list) else [],
        "varianceToleranceLitres": data.get("varianceToleranceLitres") if isinstance(data.get("varianceToleranceLitres"), (int, float)) and not isinstance(data.get("varianceToleranceLitres"), bool) else None,
        "deliveryBasis": str(data.get("deliveryBasis") or "")[:300],
        "olderMonths": data.get("olderMonths") if isinstance(data.get("olderMonths"), list) else [],
    })
    save_snapshot(c, "fuel-dips", as_of, data)
    return {"asOf": as_of, "days": len(dates)}


def apply_insights(c, data):
    if not isinstance(data, dict):
        raise ValueError("insights data must be an object")
    d = need_date(data.get("date"), "date")
    headline = data.get("headline")
    bullets = data.get("bullets")
    if not isinstance(headline, str) or not headline.strip() or len(headline.strip()) > 200:
        raise ValueError("headline is required and must be 200 characters or less")
    if not isinstance(bullets, list) or not (1 <= len(bullets) <= 12):
        raise ValueError("bullets must be a list of 1 to 12 strings")
    clean = []
    for b in bullets:
        if not isinstance(b, str) or not b.strip() or len(b.strip()) > 500:
            raise ValueError("each bullet must be text of 500 characters or less")
        clean.append(b.strip())
    reject_sensitive({"headline": headline, "bullets": clean}, "insights")
    c.execute(
        """INSERT INTO portal_insights(date,headline,bullets,source,received_at) VALUES(?,?,?,?,?)
           ON CONFLICT(date) DO UPDATE SET headline=excluded.headline, bullets=excluded.bullets, source='box', received_at=excluded.received_at""",
        (d, headline.strip(), json.dumps(clean, ensure_ascii=False), "box", stamp()),
    )
    save_snapshot(c, "insights", d, data)
    return {"asOf": d, "days": 1}


def invoice_key(supplier, number):
    """Stable bill identity: supplier plus invoice number. Case does not matter."""
    return str(supplier).strip().casefold() + "\n" + str(number).strip().casefold()


def clean_invoice_id(value):
    """Optional Xero invoice id. Empty means the upload did not send one."""
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ValueError("invoice_id must be text")
    text = value.strip()
    if not text:
        return None
    if len(text) > 80 or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", text) or has_card_number(text):
        raise ValueError("invoice_id must be a short id")
    return text


def taken_invoice_id(inv):
    for key in ("invoice_id", "xero_invoice_id"):
        if key in inv and inv.get(key) not in (None, ""):
            return clean_invoice_id(inv.get(key))
    return None


def touch_owner_payment(c, key, supplier, number, xero_id):
    """Keep the owner's payment row pointed at this bill. Do not change what they entered."""
    if xero_id:
        c.execute(
            """UPDATE portal_invoice_payments SET supplier=?, invoice_number=?, xero_invoice_id=COALESCE(?, xero_invoice_id)
               WHERE invoice_key=?""",
            (supplier, number, xero_id, key),
        )
    else:
        c.execute(
            "UPDATE portal_invoice_payments SET supplier=?, invoice_number=? WHERE invoice_key=?",
            (supplier, number, key),
        )


def apply_invoices(c, data):
    if not isinstance(data, dict) or not isinstance(data.get("invoices"), list):
        raise ValueError("invoices data needs an invoices list")
    reject_sensitive(data, "invoices")
    as_of = need_date(data.get("date") or today().isoformat(), "date")
    seen = set()
    n = 0
    for i, inv in enumerate(data["invoices"]):
        if not isinstance(inv, dict):
            raise ValueError(f"invoices[{i}] must be an object")
        supplier = str(inv.get("supplier") or "").strip()[:120]
        number = str(inv.get("invoice_number") or "").strip()[:80]
        if not supplier or not number:
            raise ValueError(f"invoices[{i}] needs supplier and invoice_number")
        status = str(inv.get("status") or "unpaid").strip().lower()[:24]
        if not re.fullmatch(r"[a-z][a-z _-]{0,23}", status):
            raise ValueError(f"invoices[{i}] has a bad status")
        key = invoice_key(supplier, number)
        pay = clean_pay_url(inv.get("pay_url"))
        paid_date = need_date(inv.get("paid_date"), "paid date") if inv.get("paid_date") else None
        amount = num(inv.get("amount"), "amount") if "amount" in inv and inv.get("amount") not in (None, "") else None
        xero_id = taken_invoice_id(inv)
        c.execute(
            """INSERT INTO portal_invoices(invoice_key,supplier,invoice_number,invoice_date,due_date,amount_due,status,pay_url,source_as_of,updated_at,paid_date,amount,xero_invoice_id)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(invoice_key) DO UPDATE SET supplier=excluded.supplier, invoice_number=excluded.invoice_number,
                 invoice_date=excluded.invoice_date, due_date=excluded.due_date, amount_due=excluded.amount_due,
                 status=excluded.status, pay_url=excluded.pay_url, source_as_of=excluded.source_as_of, updated_at=excluded.updated_at,
                 paid_date=excluded.paid_date, amount=excluded.amount,
                 xero_invoice_id=COALESCE(excluded.xero_invoice_id, portal_invoices.xero_invoice_id)""",
            (key, supplier, number,
             need_date(inv.get("date"), "invoice date") if inv.get("date") else None,
             need_date(inv.get("due_date"), "due date") if inv.get("due_date") else None,
             num(inv.get("amount_due"), "amount_due"), status, pay, as_of, stamp(), paid_date, amount, xero_id),
        )
        touch_owner_payment(c, key, supplier, number, xero_id)
        seen.add(key)
        n += 1
    if data.get("sync") is True:
        for row in rows(c.execute("SELECT invoice_key, status FROM portal_invoices")):
            if row["invoice_key"] not in seen and (row["status"] or "").lower() not in PAID_STATUS:
                c.execute(
                    "UPDATE portal_invoices SET status='absent', updated_at=?, source_as_of=? WHERE invoice_key=?",
                    (stamp(), as_of, row["invoice_key"]),
                )
    meta_set(c, "invoices", {"asOf": as_of, "count": n})
    save_snapshot(c, "invoices", as_of, data)
    return {"asOf": as_of, "invoices": n}


def _drop_card_brand(node):
    """Skip the Bopple card-brand field. Card numbers and other bank keys are still refused."""
    if isinstance(node, dict):
        return {k: _drop_card_brand(v) for k, v in node.items() if _key_name(k) != "card"}
    if isinstance(node, list):
        return [_drop_card_brand(v) for v in node]
    return node


def _order_text(value, label, limit, required=False):
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            raise ValueError(f"{label} is required")
        return None
    if not isinstance(value, str):
        raise ValueError(f"{label} must be text")
    text = " ".join(value.split())
    if len(text) > limit or has_card_number(text):
        raise ValueError(f"{label} is too long" if len(text) > limit else f"{label} must not include a card number")
    return text


def _order_qty(value, label):
    count = num(value, label)
    if count < 0 or count > 10000:
        raise ValueError(f"{label} is out of range")
    if float(count).is_integer():
        return int(count)
    return round(count, 2)


def _order_items(value, label):
    """Items from the Scheduled Orders export. None means the key was present but empty."""
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"{label} items must be a list")
    if len(value) > 80:
        raise ValueError(f"{label} has too many items")
    items = []
    for i, raw in enumerate(value):
        if not isinstance(raw, dict):
            raise ValueError(f"{label} items[{i}] must be an object")
        mods_raw = raw.get("modifiers") if raw.get("modifiers") is not None else []
        if not isinstance(mods_raw, list) or len(mods_raw) > 40:
            raise ValueError(f"{label} items[{i}] modifiers must be a list")
        modifiers = []
        for j, mod in enumerate(mods_raw):
            if not isinstance(mod, dict):
                raise ValueError(f"{label} items[{i}] modifiers[{j}] must be an object")
            modifiers.append({
                "category": _order_text(mod.get("category"), f"{label} modifier category", 80) or "",
                "name": _order_text(mod.get("name"), f"{label} modifier", 120, required=True),
                "qty": _order_qty(mod.get("qty"), f"{label} modifier qty"),
                "total": num(mod.get("total"), f"{label} modifier total"),
            })
        items.append({
            "category": _order_text(raw.get("category"), f"{label} category", 80) or "",
            "product": _order_text(raw.get("product"), f"{label} product", 120, required=True),
            "qty": _order_qty(raw.get("qty"), f"{label} qty"),
            "total": num(raw.get("total"), f"{label} item total"),
            "modifiers": modifiers,
        })
    return items


def _kept_order_text(row, key, previous, label, limit):
    if key not in row:
        return previous
    return _order_text(row.get(key), label, limit)


def _kept_order_items(row, label, previous_json):
    if "items" not in row:
        if not previous_json:
            return []
        try:
            previous = json.loads(previous_json)
        except json.JSONDecodeError:
            return []
        return previous if isinstance(previous, list) else []
    return _order_items(row.get("items"), label)


def _fulfilment_time(value, label):
    if value is None:
        return None
    text = _order_text(value, label, 16, required=True)
    if not re.fullmatch(r"(?i)(?:[01]?\d:[0-5]\d\s?(?:am|pm)|[0-2]\d:[0-5]\d(?::[0-5]\d)?)", text):
        raise ValueError(f"{label} must be a time such as 02:21pm")
    return text


def _brand(value, label, required=True):
    """A wallet or card brand such as Visa or Apple pay. Not a card number."""
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            raise ValueError(f"{label} is required")
        return ""
    if not isinstance(value, str):
        raise ValueError(f"{label} must be text")
    text = " ".join(value.split())
    if len(text) > 24 or not re.fullmatch(r"[A-Za-z][A-Za-z &'./-]*", text) or has_card_number(text):
        raise ValueError(f"{label} must be a short name, not a card number")
    return text


def apply_online_orders(c, data):
    """Bopple pickup orders paid online. Gross is extra sales, not till sales."""
    if not isinstance(data, dict):
        raise ValueError("online-orders data must be an object")
    reject_sensitive(_drop_card_brand(data), "online-orders")
    daily = data.get("daily") if data.get("daily") is not None else []
    orders = data.get("orders") if data.get("orders") is not None else []
    if not isinstance(daily, list) or not isinstance(orders, list):
        raise ValueError("online-orders daily and orders must be lists")
    if len(daily) > 800 or len(orders) > 5000:
        raise ValueError("online-orders payload is too large")
    days = {}
    for i, row in enumerate(daily):
        if not isinstance(row, dict):
            raise ValueError(f"daily[{i}] must be an object")
        d = need_date(row.get("date"), f"daily[{i}] date")
        count = whole(row.get("orders"), f"daily[{i}] orders", required=True)
        if count < 0 or count > 100000:
            raise ValueError(f"daily[{i}] orders is out of range")
        days[d] = {
            "orders": count,
            "total": num(row.get("total"), f"daily[{i}] total"),
            "fees": num(row.get("fees"), f"daily[{i}] fees"),
            "refunds": num(row.get("refunds"), f"daily[{i}] refunds"),
            "net": num(row.get("net"), f"daily[{i}] net"),
        }
    ensure_online_order_columns(c)
    stored_orders = {
        row["order_no"]: row
        for row in rows(c.execute("SELECT order_no, customer_name, order_type, fulfilment_time, items FROM portal_online_orders"))
    }
    by_no = {}
    for i, row in enumerate(orders):
        if not isinstance(row, dict):
            raise ValueError(f"orders[{i}] must be an object")
        number = str(row.get("order_no") or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9-]{1,24}", number) or has_card_number(number):
            raise ValueError(f"orders[{i}] has a bad order number")
        when = row.get("time")
        if not isinstance(when, str) or not when.strip() or len(when.strip()) > 60:
            raise ValueError(f"orders[{i}] time is required")
        when = " ".join(when.split())
        reject_sensitive(when, "online-orders")
        previous = stored_orders.get(number) or {}
        fulfilment = previous.get("fulfilment_time")
        if "fulfilment_time" in row:
            fulfilment = _fulfilment_time(row.get("fulfilment_time"), f"orders[{i}] fulfilment_time") if row.get("fulfilment_time") else None
        by_no[number] = {
            "order_no": number,
            "time": when,
            "trading_date": need_date(row.get("trading_date"), f"orders[{i}] trading_date"),
            "wallet": _brand(row.get("wallet"), f"orders[{i}] wallet"),
            "cardBrand": _brand(row.get("card"), f"orders[{i}] card", required=False),
            "total": num(row.get("total"), f"orders[{i}] total"),
            "fees": num(row.get("fees"), f"orders[{i}] fees"),
            "refunds": num(row.get("refunds"), f"orders[{i}] refunds"),
            "net": num(row.get("net"), f"orders[{i}] net"),
            "customer_name": _kept_order_text(row, "customer_name", previous.get("customer_name"), f"orders[{i}] customer_name", 80),
            "order_type": _kept_order_text(row, "order_type", previous.get("order_type"), f"orders[{i}] order_type", 24),
            "fulfilment_time": fulfilment,
            "items": _kept_order_items(row, f"orders[{i}]", previous.get("items")),
        }
    clean_orders = list(by_no.values())
    if not days and clean_orders:
        for item in clean_orders:
            bucket = days.setdefault(item["trading_date"], {"orders": 0, "total": 0.0, "fees": 0.0, "refunds": 0.0, "net": 0.0})
            bucket["orders"] += 1
            bucket["total"] += item["total"]
            bucket["fees"] += item["fees"]
            bucket["refunds"] += item["refunds"]
            bucket["net"] += item["net"]
    if not days and not clean_orders:
        raise ValueError("online-orders needs daily rows or orders")
    dates = list(days) + [item["trading_date"] for item in clean_orders]
    as_of = need_date(data.get("asOf"), "asOf") if data.get("asOf") else max(dates)
    for d, row in days.items():
        c.execute(
            """INSERT INTO portal_online_days(date,orders,total,fees,refunds,net,source_as_of) VALUES(?,?,?,?,?,?,?)
               ON CONFLICT(date) DO UPDATE SET orders=excluded.orders, total=excluded.total, fees=excluded.fees,
                 refunds=excluded.refunds, net=excluded.net, source_as_of=excluded.source_as_of""",
            (d, row["orders"], row["total"], row["fees"], row["refunds"], row["net"], as_of),
        )
    for item in clean_orders:
        c.execute(
            """INSERT INTO portal_online_orders(order_no,ordered_at,trading_date,wallet,card_brand,total,fees,refunds,net,source_as_of,customer_name,order_type,fulfilment_time,items)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(order_no) DO UPDATE SET ordered_at=excluded.ordered_at, trading_date=excluded.trading_date,
                 wallet=excluded.wallet, card_brand=excluded.card_brand, total=excluded.total, fees=excluded.fees,
                 refunds=excluded.refunds, net=excluded.net, source_as_of=excluded.source_as_of,
                 customer_name=excluded.customer_name, order_type=excluded.order_type,
                 fulfilment_time=excluded.fulfilment_time, items=excluded.items""",
            (item["order_no"], item["time"], item["trading_date"], item["wallet"], item["cardBrand"],
             item["total"], item["fees"], item["refunds"], item["net"], as_of,
             item["customer_name"], item["order_type"], item["fulfilment_time"],
             json.dumps(item["items"], separators=(",", ":"), ensure_ascii=False) if item["items"] else None),
        )
    meta_set(c, "online-orders", {
        "asOf": as_of,
        "source": str(data.get("source") or "")[:160],
        "days": len(days),
        "orders": len(clean_orders),
    })
    save_snapshot(c, "online-orders", as_of, {
        "source": str(data.get("source") or "")[:160],
        "asOf": as_of,
        "daily": [{"date": d, **row} for d, row in sorted(days.items())],
        "orders": clean_orders,
    })
    note_online_for_insights(c)
    return {"asOf": as_of, "days": len(days), "orders": len(clean_orders)}


def _ly_text(value, label, limit):
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError(f"{label} must be text")
    return " ".join(value.split())[:limit]


def _ly_month_row(row, label):
    if not isinstance(row, dict):
        raise ValueError(f"{label} must be an object")
    month = row.get("month")
    if not isinstance(month, str) or not re.fullmatch(r"\d{4}-\d{2}", month):
        raise ValueError(f"{label} month must be YYYY-MM")
    year, mon = int(month[:4]), int(month[5:])
    if year < 1990 or year > 2100 or not 1 <= mon <= 12:
        raise ValueError(f"{label} month must be YYYY-MM")

    def optional_num(key):
        if key not in row or row.get(key) is None:
            return None
        return num(row.get(key), key)

    return {
        "month": f"{year:04d}-{mon:02d}",
        "shop_sales_inc": optional_num("shop_sales_inc"),
        "food_sales_inc": optional_num("food_sales_inc"),
        "fuel_litres": optional_num("fuel_litres"),
        "source": _ly_text(row.get("source"), "source", 300),
        "note": _ly_text(row.get("note"), "note", 500),
    }


def apply_last_year_monthly(c, data):
    """Upsert same-month-last-year totals. A null field stays missing; it is not stored as zero."""
    if not isinstance(data, dict):
        raise ValueError("last-year-monthly must be an object")
    reject_sensitive(data, "last-year-monthly")
    has_one = "month" in data
    has_many = "months" in data
    if has_one and has_many:
        raise ValueError("last-year-monthly needs months or one month, not both")
    if has_many:
        rows_in = data.get("months")
        if not isinstance(rows_in, list) or not rows_in:
            raise ValueError("last-year-monthly needs a months list")
    elif has_one:
        rows_in = [data]
    else:
        raise ValueError("last-year-monthly needs a month")
    if len(rows_in) > 240:
        raise ValueError("last-year-monthly has too many months")
    ordered = {}
    for i, row in enumerate(rows_in):
        clean = _ly_month_row(row, f"months[{i}]")
        ordered[clean["month"]] = clean
    if data.get("asOf"):
        as_of = need_date(data.get("asOf"), "asOf")
    else:
        latest = max(ordered)
        year, mon = (int(latest[:4]), int(latest[5:]))
        as_of = month_last_day(date(year, mon, 1)).isoformat()
    for row in ordered.values():
        c.execute(
            """INSERT INTO portal_ly_monthly(month,shop_sales_inc,food_sales_inc,fuel_litres,source,note,updated_at)
               VALUES(?,?,?,?,?,?,?)
               ON CONFLICT(month) DO UPDATE SET shop_sales_inc=excluded.shop_sales_inc,
                 food_sales_inc=excluded.food_sales_inc, fuel_litres=excluded.fuel_litres,
                 source=excluded.source, note=excluded.note, updated_at=excluded.updated_at""",
            (row["month"], row["shop_sales_inc"], row["food_sales_inc"], row["fuel_litres"],
             row["source"], row["note"], stamp()),
        )
    total = one(c.execute("SELECT COUNT(*) AS n FROM portal_ly_monthly"))["n"]
    meta_set(c, "last-year-monthly", {"asOf": as_of, "months": total})
    save_snapshot(c, "last-year-monthly", as_of, {"asOf": as_of, "months": list(ordered.values())})
    return {"asOf": as_of, "months": len(ordered)}


def apply_pay_rates(c, data):
    """Replace the owner pay-rate list. Re-posting replaces every employee."""
    if not isinstance(data, dict):
        raise ValueError("pay-rates data must be an object")
    reject_sensitive(data, "pay-rates")
    employees = data.get("employees")
    if not isinstance(employees, list):
        raise ValueError("pay-rates data needs an employees list")
    if len(employees) > 200:
        raise ValueError("pay-rates has too many employees")
    as_of = need_date(data.get("asOf") or today().isoformat(), "asOf")
    clean = []
    seen = set()
    fields = (
        ("ordinary", "ordinary"),
        ("saturday", "saturday"),
        ("sunday", "sunday"),
        ("publicHoliday", "publicHoliday"),
    )
    for i, row in enumerate(employees):
        if not isinstance(row, dict):
            raise ValueError(f"employees[{i}] must be an object")
        raw_name = row.get("name")
        try:
            name = clean_name(raw_name if isinstance(raw_name, str) else "")
        except ValueError:
            raise ValueError(f"employees[{i}] needs a name")
        reject_sensitive(name, "pay-rates")
        key = rate_key(name)
        if key in seen:
            raise ValueError(f"employees[{i}] repeats a name")
        seen.add(key)
        rates = {"name": name}
        for field, label in fields:
            amount = num(row.get(field), label)
            q = _cents(amount)
            if q < 0 or q > 1000:
                raise ValueError(f"{label} must be from 0 to 1000")
            rates[field] = format(q, "f")
        if "superRate" not in row or row.get("superRate") in (None, ""):
            raise ValueError("superRate is required, as a fraction such as 0.12 for 12% super")
        super_rate = Decimal(str(num(row.get("superRate"), "superRate"))).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
        if super_rate < 0 or super_rate > 1:
            raise ValueError("superRate is a fraction from 0 to 1, such as 0.12 for 12% super")
        rates["superRate"] = format(super_rate, "f")
        clean.append(rates)
    payload = {"asOf": as_of, "employees": clean}
    parity().save_blob(c, "pay-rates", as_of, payload)
    meta_set(c, "pay-rates", {"asOf": as_of, "employees": len(clean)})
    save_snapshot(c, "pay-rates", as_of, payload)
    return {"asOf": as_of, "employees": len(clean)}


APPLIERS = {
    "sales": apply_sales,
    "purchases": apply_purchases,
    "fuel-commission": apply_fuel_commission,
    "fuel-dips": apply_fuel_dips,
    "insights": apply_insights,
    "invoices": apply_invoices,
    "product-search": lambda c, data: parity().apply_product_search(c, data),
    "retail-pricing": lambda c, data: parity().apply_retail_pricing(c, data),
    "till-price-changes": lambda c, data: parity().apply_till_prices(c, data),
    "shifts": lambda c, data: parity().apply_shifts(c, data),
    "last-year": lambda c, data: parity().apply_last_year(c, data),
    "cost-overrides": lambda c, data: parity().apply_cost_overrides(c, data),
    "stock-movements": lambda c, data: stock().apply_stock_movements(c, data),
    "barcode-map": lambda c, data: stock().apply_barcode_map(c, data),
    "online-orders": apply_online_orders,
    "shift-exceptions": lambda c, data: exceptions().apply_shift_exceptions(c, data),
    "last-year-monthly": apply_last_year_monthly,
    "pay-rates": apply_pay_rates,
}


def _insights_enabled():
    return os.environ.get("PORTAL_INSIGHTS_GENERATE", "").strip().lower() in ("1", "true", "yes", "on")


def maybe_generate_insights(c, as_of):
    """Optional plain-text summary from stored figures. Off unless PORTAL_INSIGHTS_GENERATE=1. No LLM."""
    if not _insights_enabled():
        return
    if one(c.execute("SELECT 1 AS x FROM portal_insights WHERE date=?", (as_of,))):
        return
    sales = rows(c.execute("SELECT * FROM portal_sales_days"))
    comm = rows(c.execute("SELECT * FROM portal_commission_days"))
    if not sales:
        return
    latest = max(r["date"] for r in sales)
    amounts = online_amounts(c)
    proj = project_month(
        sales, comm, target_for(latest, meta_get(c, "sales"), meta_get(c, "fuel")), latest, amounts or {},
    )
    if not proj:
        return
    mtd = proj["mtd"]
    online_gross = mtd.get("online") or 0
    if amounts is not None and online_gross:
        bullets = [
            f"Sales through {fmt_date(latest)} are {money(mtd['total'] + online_gross)}: "
            f"till {money(mtd['total'])} plus online orders (Bopple) {money(online_gross)}. "
            f"Fuel {money(mtd['fuel'])} and shop {money(mtd['shop'])} are the till split.",
            f"Shop gross profit is till GP only, {money(mtd['gp'])}. Online orders have no cost in this figure.",
        ]
        later = sum(float(amt or 0) for day, amt in (amounts or {}).items() if day > latest and day[:7] == latest[:7])
        if later:
            bullets.append(
                f"Online orders after the latest till day add {money(later)} gross. "
                "They join the total once that till day is loaded."
            )
    else:
        bullets = [
            f"Sales through {fmt_date(latest)} are {money(mtd['total'])}: fuel {money(mtd['fuel'])} and shop {money(mtd['shop'])}.",
            f"Shop gross profit on the loaded days is {money(mtd['gp'])}.",
        ]
    if proj["commissionMtd"] is not None and proj["target"]:
        bullets.append(
            f"Fuel commission is {money(proj['commissionMtd'])} of the {money(proj['target'])} target. "
            f"The straight run-rate is {money(proj['commissionRun'])}."
        )
    bullets.append("This note was built on the server from stored figures. It is not a forecast promise.")
    c.execute(
        "INSERT INTO portal_insights(date,headline,bullets,source,received_at) VALUES(?,?,?,?,?)",
        (as_of, f"Figures through {fmt_date(latest)}", json.dumps(bullets, ensure_ascii=False), "server", stamp()),
    )


def note_online_for_insights(c):
    """Refresh a server-written figure summary so it picks up online totals. A shop headline is left as sent."""
    if not _insights_enabled():
        return
    row = one(c.execute("SELECT MAX(date) AS d FROM portal_sales_days"))
    if not row or not row["d"]:
        return
    latest = row["d"]
    existing = one(c.execute("SELECT source FROM portal_insights WHERE date=?", (latest,)))
    if existing and existing["source"] != "server":
        return
    if existing:
        c.execute("DELETE FROM portal_insights WHERE date=? AND source='server'", (latest,))
    maybe_generate_insights(c, latest)


def project_month(sales, commission, target, latest, online_by=None):
    online_by = online_by or {}
    by_date = {}
    for row in sales:
        if row.get("date"):
            by_date[row["date"]] = row
    if not by_date:
        return None
    if latest not in by_date:
        latest = max(by_date)
    latest_d = date.fromisoformat(latest)
    start = latest_d.replace(day=1)
    dim = (start.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
    dim = dim.day
    end = start.replace(day=dim)
    month_days = [by_date[k] for k in sorted(by_date) if start.isoformat() <= k <= latest]
    if not month_days:
        return None
    n = len(month_days)

    def total_of(rows_, key):
        if key == "online":
            return sum(float(online_by.get(r["date"]) or 0) for r in rows_)
        return sum(float(r.get(key) or 0) for r in rows_)

    mtd = {key: total_of(month_days, key) for key in ("total", "shop", "fuel", "gp")}
    mtd["online"] = sum(
        float(amt or 0) for day, amt in online_by.items() if start.isoformat() <= day <= latest
    )
    last28 = [by_date[k] for k in sorted(by_date)[-28:]]
    buckets = {i: [] for i in range(7)}
    for row in last28:
        buckets[date.fromisoformat(row["date"]).weekday()].append(row)
    weighted = dict(mtd)
    cur = latest_d + timedelta(days=1)
    while cur <= end:
        bucket = buckets[cur.weekday()]
        if bucket:
            for key in weighted:
                weighted[key] += total_of(bucket, key) / len(bucket)
        else:
            for key in weighted:
                weighted[key] += mtd[key] / n
        cur += timedelta(days=1)
    comm_by = {}
    for row in commission:
        if row.get("date") and row.get("amount") is not None and start.isoformat() <= row["date"] <= latest:
            comm_by[row["date"]] = float(row["amount"])
    comm_values = list(comm_by.values())
    return {
        "month": start.strftime("%B %Y"),
        "latest": latest,
        "days": n,
        "daysInMonth": dim,
        "complete": latest_d >= end,
        "remainingDays": (end - latest_d).days,
        "mtd": mtd,
        "run": {key: mtd[key] / n * dim for key in mtd},
        "weighted": weighted,
        "commissionMtd": sum(comm_values) if comm_values else None,
        "commissionRun": (sum(comm_values) / len(comm_values) * dim) if comm_values else None,
        "commissionDays": len(comm_values),
        "target": target,
    }


def target_for(latest, sales_meta, fuel_meta):
    y, m, _ = latest.split("-")
    month_name = date(int(y), int(m), 1).strftime("%B %Y")
    comm = (sales_meta or {}).get("commission") or {}
    if comm.get("month") == month_name and comm.get("target") is not None:
        return float(comm["target"])
    mtd = (fuel_meta or {}).get("mtd") or {}
    if mtd.get("month") == f"{y}-{m}" and mtd.get("target") is not None:
        return float(mtd["target"])
    if (y, m) == ("2026", "11"):
        return 9500.0
    if (y, m) == ("2026", "10"):
        return 9000.0
    return None


def anchor_day(c):
    """Period chips end on the latest loaded sales day, not today. Today's sales wait for 9:30pm."""
    through = data_through(c)
    if through:
        return date.fromisoformat(through)
    return today()


def data_through(c):
    row = one(c.execute("SELECT MAX(date) AS d FROM portal_sales_days"))
    if row and row["d"]:
        return row["d"]
    for table in ("portal_commission_days", "portal_dip_days"):
        row = one(c.execute(f"SELECT MAX(date) AS d FROM {table}"))
        if row and row["d"]:
            return row["d"]
    return None


def period_bounds(period, start_s, end_s, today_d):
    period = period or "month"
    if period == "custom":
        start = date.fromisoformat(need_date(start_s, "from"))
        end = date.fromisoformat(need_date(end_s, "to"))
        if end < start:
            start, end = end, start
        if (end - start).days > 400:
            raise ValueError("Choose 400 days or fewer")
        return "custom", start, end
    if period == "week":
        return "week", week_monday(today_d), today_d
    if period == "lastmonth":
        first = today_d.replace(day=1)
        end = first - timedelta(days=1)
        return "lastmonth", end.replace(day=1), end
    if period == "last7":
        return "last7", today_d - timedelta(days=6), today_d
    if period == "last28":
        return "last28", today_d - timedelta(days=27), today_d
    return "month", today_d.replace(day=1), today_d


def sales_between(c, start, end):
    return rows(c.execute(
        "SELECT * FROM portal_sales_days WHERE date>=? AND date<=? ORDER BY date",
        (start.isoformat(), end.isoformat()),
    ))


def commission_between(c, start, end):
    return rows(c.execute(
        "SELECT * FROM portal_commission_days WHERE date>=? AND date<=? ORDER BY date",
        (start.isoformat(), end.isoformat()),
    ))


def online_loaded(c):
    return bool(meta_get(c, "online-orders").get("asOf"))


def online_amounts(c):
    """Gross online sales by trading date, or None when the dataset has never been stored."""
    if not online_loaded(c):
        return None
    found = {}
    for row in rows(c.execute("SELECT date, total FROM portal_online_days")):
        found[row["date"]] = float(row["total"] or 0)
    return found


def online_days_between(c, start, end):
    if not online_loaded(c):
        return None
    return rows(c.execute(
        "SELECT * FROM portal_online_days WHERE date>=? AND date<=? ORDER BY date",
        (start.isoformat(), end.isoformat()),
    ))


def online_totals(day_rows):
    day_rows = day_rows or []
    return {
        "orders": sum(int(r["orders"] or 0) for r in day_rows),
        "total": sum(float(r["total"] or 0) for r in day_rows),
        "fees": sum(float(r["fees"] or 0) for r in day_rows),
        "refunds": sum(float(r["refunds"] or 0) for r in day_rows),
        "net": sum(float(r["net"] or 0) for r in day_rows),
        "days": len(day_rows),
    }


def online_anchor(c):
    dates = []
    through = data_through(c)
    if through:
        dates.append(date.fromisoformat(through))
    as_of = meta_get(c, "online-orders").get("asOf")
    if as_of:
        dates.append(date.fromisoformat(as_of))
    return max(dates) if dates else today()


def chat_figures(c):
    """Month-to-date till, online, and combined sales for the help assistant. Strings only."""
    sales_rows = rows(c.execute("SELECT date, total, gp FROM portal_sales_days"))
    amounts = online_amounts(c)
    if not sales_rows and amounts is None:
        return {
            "salesThrough": "",
            "tillSales": "not loaded",
            "onlineOrders": "not loaded",
            "totalSales": "not loaded",
            "shopGp": "not loaded",
        }
    if sales_rows:
        end = max(r["date"] for r in sales_rows)
        start = end[:8] + "01"
    else:
        end = max(amounts) if amounts else today().isoformat()
        start = end[:8] + "01"
    till = sum(float(r["total"] or 0) for r in sales_rows if start <= r["date"] <= end)
    gp = sum(float(r["gp"] or 0) for r in sales_rows if start <= r["date"] <= end)
    if amounts is None:
        online_text = "not loaded"
        online_gross = 0.0
    else:
        totals = online_totals(online_days_between(c, date.fromisoformat(start), date.fromisoformat(end)))
        online_gross = totals["total"]
        online_text = (
            f"{totals['orders']} orders, gross {money(totals['total'])}, "
            f"fees {money(totals['fees'])}, net {money(totals['net'])}"
        )
    combined = till + online_gross
    return {
        "salesThrough": end,
        "tillSales": money(till) if sales_rows else "not loaded",
        "onlineOrders": online_text,
        "totalSales": money(combined) if sales_rows or amounts is not None else "not loaded",
        "shopGp": ("till GP " + money(gp)) if sales_rows else "not loaded",
    }


def order_time_key(text, trading_date):
    match = re.match(r"(\d{4}-\d{2}-\d{2}) (\d{1,2}):(\d{2}):(\d{2}) (AM|PM)", text or "", re.I)
    if not match:
        return (trading_date, 0, 0, 0, text or "")
    hour = int(match.group(2)) % 12
    if match.group(5).upper() == "PM":
        hour += 12
    return (match.group(1), hour, int(match.group(3)), int(match.group(4)), text or "")


def latest_insight(c):
    row = one(c.execute("SELECT * FROM portal_insights ORDER BY date DESC LIMIT 1"))
    if not row:
        return None
    try:
        row["bullet_list"] = json.loads(row["bullets"])
    except json.JSONDecodeError:
        row["bullet_list"] = []
    return row


def owner_payment_map(c):
    return {row["invoice_key"]: row for row in rows(c.execute("SELECT * FROM portal_invoice_payments"))}


def payment_is_matched(payment, invoice):
    """Matched once the bot stored a Xero reference, or a later ingest shows the bill paid."""
    if not payment:
        return False
    if payment.get("matched_at"):
        return True
    status = (invoice.get("status") or "").strip().lower() if invoice else ""
    return status in XERO_PAID


def invoice_lists(c, today_d):
    """Unpaid bills, and bills marked paid. Void, draft, and absent rows stay off both lists.

    An owner payment moves the bill onto the paid list until it is undone. The nightly
    ingest does not delete that payment.
    """
    payments = owner_payment_map(c)
    unpaid, paid = [], []
    for row in rows(c.execute("SELECT * FROM portal_invoices")):
        status = (row["status"] or "").strip().lower()
        payment = payments.get(row["invoice_key"])
        row["owner_payment"] = payment
        row["owner_matched"] = bool(payment) and payment_is_matched(payment, row)
        row["owner_awaiting"] = bool(payment) and not row["owner_matched"]
        if status in PAID_STATUS and status not in XERO_PAID:
            continue
        if status in XERO_PAID or row["owner_awaiting"] or row["owner_matched"]:
            paid.append(row)
            continue
        owing = float(row["amount_due"] or 0)
        row["overdue"] = bool(row["due_date"] and row["due_date"] < today_d.isoformat() and owing > 0)
        unpaid.append(row)
    unpaid.sort(key=lambda r: (not r["overdue"], r["due_date"] or "9999-99-99", r["supplier"].casefold()))
    paid.sort(key=lambda r: ((r.get("owner_payment") or {}).get("paid_on") or r.get("paid_date") or r.get("invoice_date") or ""), reverse=True)
    paid.sort(key=lambda r: 0 if r.get("owner_awaiting") else 1)
    return unpaid, paid


def shift_hours(work_date, start, finish, break_minutes):
    if not re.fullmatch(r"\d{2}:\d{2}", start or "") or not re.fullmatch(r"\d{2}:\d{2}", finish or ""):
        raise ValueError("Use 24-hour times, HH:MM")
    sh, sm = int(start[:2]), int(start[3:])
    fh, fm = int(finish[:2]), int(finish[3:])
    if sh > 23 or fh > 23 or sm > 59 or fm > 59:
        raise ValueError("Use 24-hour times, HH:MM")
    if not isinstance(break_minutes, int) or isinstance(break_minutes, bool) or break_minutes < 0 or break_minutes > 600:
        raise ValueError("Unpaid break must be 0 to 600 minutes")
    d = date.fromisoformat(work_date)
    start_dt = datetime(d.year, d.month, d.day, sh, sm, tzinfo=_srv().TZ)
    end_dt = datetime(d.year, d.month, d.day, fh, fm, tzinfo=_srv().TZ)
    if end_dt <= start_dt:
        end_dt += timedelta(days=1)
    gross = int((end_dt - start_dt).total_seconds() // 60)
    if gross <= 0 or gross > 20 * 60:
        raise ValueError("A shift must be longer than 0 and at most 20 hours")
    if break_minutes >= gross:
        raise ValueError("The unpaid break must be shorter than the shift")
    net = (Decimal(gross - break_minutes) / Decimal(60)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return float(net)


def audit(c, actor, action, person_id, shift_id, before, after):
    c.execute(
        "INSERT INTO portal_roster_audit(at,actor,action,person_id,shift_id,before_json,after_json) VALUES(?,?,?,?,?,?,?)",
        (stamp(), actor, action, person_id, shift_id,
         json.dumps(before, ensure_ascii=False) if before is not None else None,
         json.dumps(after, ensure_ascii=False) if after is not None else None),
    )


def shift_snapshot(row, name):
    return {
        "staff": name, "date": row["work_date"], "start": row["start_time"], "finish": row["finish_time"],
        "break": row["break_minutes"], "notes": row["notes"] or "", "hours": row["hours"],
    }


def people_rows(c):
    return rows(c.execute("SELECT * FROM portal_people ORDER BY active DESC, lower(name), id"))


def roster_between(c, start, end):
    return rows(c.execute(
        """SELECT r.*, p.name, p.active AS person_active FROM portal_roster r
           JOIN portal_people p ON p.id=r.person_id
           WHERE r.work_date>=? AND r.work_date<=? ORDER BY lower(p.name), r.work_date, r.start_time, r.id""",
        (start.isoformat(), end.isoformat()),
    ))


def week_payload(c, monday):
    end = monday + timedelta(days=6)
    days = []
    for i in range(7):
        d = monday + timedelta(days=i)
        days.append({
            "date": d.isoformat(),
            "weekday": d.strftime("%a"),
            "weekend": d.weekday() >= 5,
            "publicHoliday": holiday_name(d.isoformat()),
        })
    shifts = roster_between(c, monday, end)
    grouped = {}
    for person in people_rows(c):
        if person["active"]:
            grouped[person["id"]] = {"id": person["id"], "name": person["name"], "active": True, "shifts": [], "dailyHours": {}, "totalHours": 0.0}
    for shift in shifts:
        bucket = grouped.setdefault(shift["person_id"], {
            "id": shift["person_id"], "name": shift["name"], "active": bool(shift["person_active"]),
            "shifts": [], "dailyHours": {}, "totalHours": 0.0,
        })
        bucket["shifts"].append({
            "id": shift["id"], "date": shift["work_date"], "start": shift["start_time"], "finish": shift["finish_time"],
            "breakMinutes": shift["break_minutes"], "hours": shift["hours"], "notes": shift["notes"] or "",
            "updatedAt": shift["updated_at"], "updatedBy": shift["updated_by"],
        })
        bucket["dailyHours"][shift["work_date"]] = round(bucket["dailyHours"].get(shift["work_date"], 0) + shift["hours"], 2)
        bucket["totalHours"] = round(bucket["totalHours"] + shift["hours"], 2)
    people = sorted(grouped.values(), key=lambda p: (p["name"].casefold(), p["id"]))
    return {
        "timezone": "Australia/Brisbane",
        "week": monday.isoformat(),
        "weekEnding": end.isoformat(),
        "days": days,
        "people": people,
        "grandTotal": round(sum(p["totalHours"] for p in people), 2),
        "note": "Hours for pay preparation only. Nothing is sent to Xero or payroll.",
    }


def csv_cell(value):
    text = "" if value is None else str(value)
    if text[:1] in "=+-@\t\r":
        text = "'" + text
    return text


def hours_csv(payload):
    buf = io.StringIO()
    buf.write("\ufeff")
    writer = csv.writer(buf)
    writer.writerow(["Staff", "Date", "Weekday", "Start", "Finish", "Unpaid break (minutes)", "Hours", "Notes", "Weekend", "Public holiday"])
    day_info = {d["date"]: d for d in payload["days"]}
    for person in payload["people"]:
        for shift in person["shifts"]:
            info = day_info.get(shift["date"], {})
            writer.writerow([
                csv_cell(person["name"]), shift["date"], info.get("weekday", ""), shift["start"], shift["finish"],
                shift["breakMinutes"], f"{shift['hours']:.2f}", csv_cell(shift["notes"]),
                "yes" if info.get("weekend") else "no", csv_cell(info.get("publicHoliday") or ""),
            ])
    return buf.getvalue().encode("utf-8")


GST_DIVISOR = Decimal("1.1")
PAY_KIND_LABEL = {
    "ordinary": "Weekday",
    "saturday": "Saturday",
    "sunday": "Sunday",
    "publicHoliday": "Public holiday",
}


def rate_key(name):
    return " ".join(str(name or "").split()).casefold()


def pay_kind(iso_date, start_time):
    """Rate band for the day a shift started. A full public holiday wins over the weekend.

    Christmas Eve is listed from 6pm only. A shift that starts at or after 18:00 uses the
    public holiday rate for the whole shift. An earlier start stays on the weekday rate.
    """
    if iso_date in HOLIDAYS:
        return "publicHoliday"
    if iso_date in PART_HOLIDAYS and (start_time or "") >= "18:00":
        return "publicHoliday"
    weekday = date.fromisoformat(iso_date).weekday()
    if weekday == 5:
        return "saturday"
    if weekday == 6:
        return "sunday"
    return "ordinary"


def _cents(amount):
    return Decimal(str(amount)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def shift_wage(hours, rate_row, kind):
    """Cost of one shift, excl and incl that employee's super rate. Already rounded to cents."""
    rate = Decimal(str(rate_row[kind]))
    super_rate = Decimal(str(rate_row["superRate"]))
    excl = _cents(Decimal(str(hours)) * rate)
    incl = _cents(excl * (Decimal(1) + super_rate))
    return {
        "kind": kind,
        "kindLabel": PAY_KIND_LABEL[kind],
        "rate": rate,
        "superRate": super_rate,
        "excl": excl,
        "incl": incl,
    }


def load_rate_index(c):
    """Name key to rate row, or None when pay-rates has never been stored."""
    data = parity().load_blob(c, "pay-rates")
    if not isinstance(data, dict):
        return None
    index = {}
    for row in data.get("employees") or []:
        if isinstance(row, dict) and row.get("name"):
            index[rate_key(row["name"])] = row
    return index


def build_wage_view(people, rate_index):
    """Cost employee shifts. People with hours and no rate are flagged and left out of sums.

    `people` matches week_payload: id, name, and shifts with id, date, start, hours.
    The staff-hours feed keeps using week_payload and never receives this view.
    """
    view_people = []
    for person in people:
        rate_row = None if rate_index is None else rate_index.get(rate_key(person["name"]))
        days = {}
        shift_costs = {}
        any_hours = False
        hours_total = Decimal("0.00")
        excl = Decimal("0.00")
        incl = Decimal("0.00")
        for shift in person.get("shifts") or []:
            any_hours = True
            hours = Decimal(str(shift["hours"]))
            hours_total += hours
            day = days.setdefault(shift["date"], {
                "excl": Decimal("0.00"), "incl": Decimal("0.00"), "kinds": set(),
            })
            if rate_row is None:
                shift_costs[shift["id"]] = None
                day["missing"] = True
                continue
            cost = shift_wage(hours, rate_row, pay_kind(shift["date"], shift.get("start") or ""))
            shift_costs[shift["id"]] = cost
            day["excl"] += cost["excl"]
            day["incl"] += cost["incl"]
            day["kinds"].add(cost["kindLabel"])
            day["missing"] = False
            excl += cost["excl"]
            incl += cost["incl"]
        missing = any_hours and rate_row is None
        view_people.append({
            "id": person["id"],
            "name": person["name"],
            "days": days,
            "shifts": shift_costs,
            "hours": hours_total,
            "excl": None if missing or not any_hours else excl,
            "incl": None if missing or not any_hours else incl,
            "missing": missing,
            "superRate": None if rate_row is None else Decimal(str(rate_row["superRate"])),
        })
    rated_excl = [p["excl"] for p in view_people if p["excl"] is not None]
    rated_incl = [p["incl"] for p in view_people if p["incl"] is not None]
    return {
        "people": view_people,
        "rates_loaded": rate_index is not None,
        "employee_excl": sum(rated_excl, Decimal("0.00")) if rated_excl else None,
        "employee_incl": sum(rated_incl, Decimal("0.00")) if rated_incl else None,
        "missing": [p for p in view_people if p["missing"]],
    }


def contract_between(c, start, end):
    return rows(c.execute(
        """SELECT e.*, k.name, k.hourly_rate AS saved_rate FROM portal_contract_entries e
           JOIN portal_contractors k ON k.id=e.contractor_id
           WHERE e.work_date>=? AND e.work_date<=?
           ORDER BY lower(k.name), e.work_date, e.id""",
        (start.isoformat(), end.isoformat()),
    ))


def contractor_directory(c):
    return rows(c.execute(
        "SELECT * FROM portal_contractors WHERE active=1 ORDER BY lower(name), id"
    ))


def contractor_by_name(c, name):
    return one(c.execute(
        "SELECT * FROM portal_contractors WHERE lower(name)=lower(?) ORDER BY active DESC, id LIMIT 1",
        (name,),
    ))


def _money_sum(values):
    if not values:
        return None
    return sum(values, Decimal("0.00"))


def wage_bill(view, contracts):
    """Employee totals omit anyone without a rate. Combined adds contract costs, which have no super."""
    contract_costs = [_cents(row["cost"]) for row in contracts]
    contract = _money_sum(contract_costs)
    employee_excl = view["employee_excl"]
    employee_incl = view["employee_incl"]

    def add(left, right):
        if left is None and right is None:
            return None
        return (left or Decimal("0.00")) + (right or Decimal("0.00"))

    return {
        "employee_excl": employee_excl,
        "employee_incl": employee_incl,
        "contract": contract,
        "combined_excl": add(employee_excl, contract),
        "combined_incl": add(employee_incl, contract),
        "missing": view["missing"],
        "rates_loaded": view["rates_loaded"],
        "complete": not view["missing"] and (employee_incl is not None or contract is not None),
    }


def super_pct(rate):
    pct = (Decimal(str(rate)) * Decimal(100)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return format(pct, "f").rstrip("0").rstrip(".") + "%"


def employee_super_label(view):
    rates = {p["superRate"] for p in view["people"] if p["superRate"] is not None}
    if len(rates) == 1:
        return f"incl. {super_pct(rates.pop())} super"
    return "incl. super"


def missing_rate_note(view):
    if view["missing"] and not view["rates_loaded"]:
        return "No pay rates yet. Employee hours are shown without a cost. Those hours are not in the wage totals."
    if not view["missing"]:
        return ""
    bits = [f"{person['name']} ({hours_text(person['hours'])} h)" for person in view["missing"]]
    return f"No rate yet for {', '.join(bits)}. Those hours are not in the employee or combined totals."


def till_sales_ex_gst(day_rows):
    """Estimate ex-GST till sales. Shop and fuel on the daysheet are inc GST, so each is divided by 1.1."""
    if not day_rows:
        return None
    shop = sum((Decimal(str(row["shop"] or 0)) for row in day_rows), Decimal("0"))
    fuel = sum((Decimal(str(row["fuel"] or 0)) for row in day_rows), Decimal("0"))
    total = sum((Decimal(str(row["total"] or 0)) for row in day_rows), Decimal("0"))
    shop_ex = _cents(shop / GST_DIVISOR)
    fuel_ex = _cents(fuel / GST_DIVISOR)
    if shop + fuel == 0 and total > 0:
        total_ex = _cents(total / GST_DIVISOR)
    else:
        total_ex = shop_ex + fuel_ex
    return {
        "shop_inc": _cents(shop),
        "fuel_inc": _cents(fuel),
        "total_inc": _cents(total),
        "shop_ex": shop_ex,
        "fuel_ex": fuel_ex,
        "total_ex": total_ex,
        "days": len(day_rows),
    }


def wage_ratio(part, whole):
    if part is None or whole is None or whole <= 0:
        return None
    return (Decimal(part) / Decimal(whole) * Decimal(100)).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)


def pct_text(value):
    if value is None:
        return "—"
    return f"{format(value, 'f')}%"


def _range_people(c, start, end):
    grouped = {}
    for shift in roster_between(c, start, end):
        bucket = grouped.setdefault(shift["person_id"], {
            "id": shift["person_id"], "name": shift["name"], "shifts": [],
        })
        bucket["shifts"].append({
            "id": shift["id"], "date": shift["work_date"], "start": shift["start_time"], "hours": shift["hours"],
        })
    return list(grouped.values())


def wages_for_range(c, start, end):
    contracts = contract_between(c, start, end)
    view = build_wage_view(_range_people(c, start, end), load_rate_index(c))
    bill = wage_bill(view, contracts)
    sales = till_sales_ex_gst(sales_between(c, start, end))
    wages_incl = bill["combined_incl"] if bill["complete"] else None
    ratio = wage_ratio(wages_incl, sales["total_ex"] if sales else None)
    return {"bill": bill, "view": view, "sales": sales, "ratio": ratio, "wages_incl": wages_incl}


def _hours_cost_html(hours, count, excl, incl, missing, kind_label=""):
    if not hours:
        return "—"
    shown = hours_text(hours)
    if count and count > 1:
        shown += f" ({count})"
    if missing:
        extra = '<small class="wage norate">No rate yet</small>'
    elif excl is None:
        extra = ""
    else:
        kind = f'<small class="wage">{h(kind_label)}</small>' if kind_label else ""
        extra = (
            f"{kind}<small class=\"wage\">{money(excl)} excl</small>"
            f"<small class=\"wage\">{money(incl)} incl</small>"
        )
    return f"{h(shown)}{extra}"


def _day_kind_label(day_cost):
    if not day_cost or day_cost.get("missing"):
        return ""
    kinds = day_cost.get("kinds") or set()
    if len(kinds) == 1:
        return next(iter(kinds))
    if len(kinds) > 1:
        return "Mixed"
    return ""


def _shift_cost_text(cost):
    if cost is None:
        return "No rate yet"
    return (
        f"{cost['kindLabel']} {money(cost['rate'])}/h · "
        f"{money(cost['excl'])} excl · {money(cost['incl'])} incl super"
    )


def bar_class(percent):
    n = max(0, min(100, int(round(float(percent) / 5.0) * 5)))
    return f"w{n}"


def _icon(key):
    paths = {
        "overview": '<path d="M4 10.5 12 4l8 6.5V20a1 1 0 0 1-1 1h-5v-6H10v6H5a1 1 0 0 1-1-1z"/>',
        "daily": '<path d="M7 3v3M17 3v3M4 9h16M6 5h12a1 1 0 0 1 1 1v13H5V6a1 1 0 0 1 1-1z"/>',
        "online": '<path d="M6 7h12l-1 13H7L6 7zM9 7a3 3 0 0 1 6 0"/>',
        "exceptions": '<path d="M7 3h8l4 4v14H7zM15 3v5h5M12 11v4M12 17h.01"/>',
        "products": '<path d="M4 7h16M4 12h16M4 17h10"/>',
        "kitchen": '<path d="M6 3v8a3 3 0 0 0 6 0V3M9 14v7M15 3c2 3 2 6 0 8v10"/>',
        "departments": '<path d="M4 4h7v7H4zM13 4h7v7h-7zM4 13h7v7H4zM13 13h7v7h-7z"/>',
        "pricing": '<path d="M12 3v18M7 7.5c1.5-2 8.5-2 10 0s-8.5 3-10 5-1.5 4 2 5 8.5 1 10 0"/>',
        "till": '<path d="M4 7h16v10H4zM8 17v3M16 17v3M8 11h2M12 11h4"/>',
        "stock": '<path d="M4 8h16v11H4zM8 8V5h8v3"/>',
        "dips": '<path d="M12 3c4 4 6 7 6 10a6 6 0 0 1-12 0c0-3 2-6 6-10z"/>',
        "commission": '<path d="M5 19V9M12 19V5M19 19v-7"/>',
        "fuel": '<path d="M6 20V6h8v14M14 9h3l2 2v6a2 2 0 0 1-2 2h-3M8 10h4"/>',
        "purchases": '<path d="M6 7h12l-1 12H7L6 7zM9 7V5h6v2"/>',
        "invoices": '<path d="M7 3h8l4 4v14H7zM15 3v5h5M9 13h6M9 17h4"/>',
        "shifts": '<path d="M8 7V5h8v2M6 7h12v12H6zM12 11v4l2 1"/>',
        "hours": '<path d="M8 4h8M8 20h8M9 4c0 4 6 4 6 8s-6 4-6 8M15 4c0 4-6 4-6 8s6 4 6 8"/>',
        "notes": '<path d="M6 4h12v16H6zM9 8h6M9 12h6M9 16h3"/>',
    }
    return f'<svg class="nav-ico" viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round" stroke-linecap="round">{paths.get(key, "")}</svg>'


def layout(title, active, body, user, when):
    by_key = {key: (href, label) for href, label, key in NAV}
    groups = (
        ("Trading", ("overview", "daily", "online", "exceptions", "products", "kitchen", "departments")),
        ("Price and stock", ("pricing", "till", "stock")),
        ("Fuel", ("dips", "commission", "fuel")),
        ("Money", ("purchases", "invoices")),
        ("Team", ("shifts", "hours")),
        ("Reference", ("notes",)),
    )
    blocks = []
    for heading, keys in groups:
        links = []
        for key in keys:
            href, label = by_key[key]
            cls = " on" if key == active else ""
            current = ' aria-current="page"' if key == active else ""
            links.append(f'<a class="{cls.strip()}" href="{href}"{current}>{_icon(key)}<span>{h(label)}</span></a>')
        blocks.append(f'<div class="nav-group"><div class="nav-label">{heading}</div>{"".join(links)}</div>')
    try:
        clock = when.strftime("%a %-d %b %Y, %-I:%M %p")
    except ValueError:
        clock = when.strftime("%a %d %b %Y, %I:%M %p")
    clock = clock.replace("AM", "am").replace("PM", "pm")
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="robots" content="noindex, nofollow"><meta name="theme-color" content="#0f2a44">
<meta name="csrf-token" content="{h(user['csrf'])}">
<title>{h(title)} · Silkwood owner</title>
<link rel="icon" type="image/png" href="/static/icon-192.png">
<link rel="apple-touch-icon" href="/static/apple-touch-icon.png">
<link rel="stylesheet" href="/static/portal.css"></head><body>
<a class="skip" href="#content">Skip to content</a>
<div class="app"><aside id="portal-nav">
<div class="logo"><i></i><div>SILK<span>WOOD</span><small>Owner portal</small></div></div>
<nav aria-label="Portal">{''.join(blocks)}</nav>
<div class="side-foot"><div>{h(user['username'])}</div>
<a href="/portal/password">Change password</a>
<form method="post" action="/portal/logout"><input type="hidden" name="csrf" value="{h(user['csrf'])}"><button type="submit">Log out</button></form>
</div></aside>
<div class="workspace"><header class="top">
<button class="nav-toggle" type="button" id="nav-toggle" aria-expanded="false" aria-controls="portal-nav">Menu</button>
<div class="mlogo">SILK<span>WOOD</span></div>
<div class="top-title"><b>{h(title)}</b><span class="tz">Australia/Brisbane</span></div>
<time id="clock" datetime="{h(when.isoformat())}">{h(clock)}</time>
<div class="account"><span class="who">{h(user['username'])}</span><a href="/portal/password">Password</a>
<form method="post" action="/portal/logout"><input type="hidden" name="csrf" value="{h(user['csrf'])}"><button type="submit">Log out</button></form>
<button class="btn ghost sm" type="button" id="help-open" aria-expanded="false" aria-controls="help-panel">Help</button></div>
</header>
<main id="content">{body}</main></div>
<button class="nav-backdrop" type="button" id="nav-backdrop" aria-label="Close menu"></button>
<aside id="help-panel" class="help-panel" aria-label="Assistant" hidden>
<div class="help-head"><h2>Assistant</h2><button type="button" class="btn ghost sm" id="help-close">Close</button></div>
<div id="help-log" class="help-log" aria-live="polite"></div>
<form id="help-form"><label class="f">Message<textarea name="message" maxlength="2000" required placeholder="Ask about this page"></textarea></label>
<div class="row"><button class="btn" type="submit">Send</button><button class="btn ghost" type="button" id="help-clear">Clear</button></div>
<p class="help" id="help-note">Replies show here. The portal does not call a model itself.</p></form>
</aside></div>
<script src="/static/portal.js"></script></body></html>"""


def through_line(iso):
    if not iso:
        return '<p class="through as-of" id="data-through">No sales loaded yet</p>'
    shown = fmt_date(iso)
    return f'<p class="through as-of" id="data-through">As of {h(shown)}, Australia/Brisbane. Data through {h(shown)}</p>'


def _period_pairs(extra):
    """Date-bar fields. A list value repeats the key, so several suppliers can travel with the dates."""
    pairs = []
    for key, value in (extra or {}).items():
        values = value if isinstance(value, (list, tuple)) else [value]
        for item in values:
            if item not in (None, ""):
                pairs.append((str(key), str(item)))
    return pairs


def period_controls(base, active, start, end, extra=None):
    kept = _period_pairs(extra)
    labels = [("month", "This month"), ("lastmonth", "Last month"), ("last7", "Last 7 days"), ("last28", "Last 28 days")]
    buttons = []
    for key, label in labels:
        cls = " on" if key == active else ""
        href = base + "?" + urlencode([("period", key), *kept]) if kept else f"{base}?period={key}"
        buttons.append(f'<a class="chip{cls}" href="{href}">{label}</a>')
    hidden = "".join(f'<input type="hidden" name="{h(k)}" value="{h(v)}">' for k, v in kept)
    if hidden:
        hidden += "\n"
    return f"""<form class="period" method="get" action="{base}">
<div class="chips">{''.join(buttons)}</div>
<div class="row">
<label class="f">From<input type="date" name="from" value="{h(start.isoformat())}" required></label>
<label class="f">To<input type="date" name="to" value="{h(end.isoformat())}" required></label>
<input type="hidden" name="period" value="custom">
{hidden}<button class="btn" type="submit">Apply</button>
</div></form>"""


def insights_box(insight):
    if not insight:
        return """<section class="card insight" id="insights"><h2>AI insights</h2>
<p class="help">No insights yet. The shop computer sends them with the nightly refresh. This portal does not call an AI service.</p></section>"""
    source = "Written on the shop computer and sent with the nightly refresh." if insight["source"] != "server" else "Built on the server from the stored figures. No AI service is called."
    items = "".join(f"<li>{h(b)}</li>" for b in insight["bullet_list"])
    return f"""<section class="card insight" id="insights"><div class="lbl">AI insights · {h(fmt_date(insight['date']))}</div>
<h2>{h(insight['headline'])}</h2><ul>{items}</ul><p class="help">{source}</p></section>"""


def _shown_sales(bucket, online_loaded):
    total = bucket.get("total") or 0
    if online_loaded:
        return total + (bucket.get("online") or 0)
    return total


def projection_card(proj, october, online_loaded=False):
    if not proj:
        return '<section class="card"><h2>Month projection</h2><p class="help">Load sales first.</p></section>'
    title = "month actual" if proj["complete"] else "month-end projection"
    mtd, run, wt = proj["mtd"], proj["run"], proj["weighted"]
    target = proj["target"]
    comm = ""
    if proj["commissionMtd"] is not None and target:
        pace = target / proj["daysInMonth"] * proj["commissionDays"]
        width = bar_class(proj["commissionMtd"] / target * 100)
        gap = proj["commissionMtd"] - pace
        gap_label = f"{money(abs(gap))} ahead of an even pace" if gap >= 0 else f"{money(abs(gap))} behind an even pace"
        need = ""
        if proj["remainingDays"] and proj["commissionMtd"] < target:
            daily = (target - proj["commissionMtd"]) / proj["remainingDays"]
            need = f"<p>About {money(daily)} a day for the {proj['remainingDays']} days left to reach {money(target)}.</p>"
        elif proj["commissionMtd"] >= target:
            need = "<p>The loaded days have already reached the target.</p>"
        comm = f"""<div class="target"><div class="lbl">Fuel commission target</div>
<b>{money(proj['commissionMtd'])}</b> <span>of {money(target)}</span>
<div class="bar"><i class="{width}"></i></div>
<p>{gap_label}. If this daily average holds, the month finishes near {money(proj['commissionRun'])}.</p>{need}</div>"""
    elif proj["commissionMtd"] is not None:
        comm = f"<p>Fuel commission loaded: {money(proj['commissionMtd'])}. No approved target is stored for this month.</p>"
    oct = ""
    if october and october.get("amount") is not None:
        oct = f"<p class=\"help\">October actual {money(october.get('amount'))}"
        if october.get("target") is not None:
            oct += f" of the {money(october.get('target'))} target"
        oct += ".</p>"
    online_note = ""
    if online_loaded:
        online_note = (
            f"<p class=\"help\">Sales totals add Bopple online gross ({money(mtd.get('online'))} month to date). "
            "Fuel, shop and GP stay on the till.</p>"
        )
    return f"""<section class="card" id="projection"><div class="lbl">{h(proj['month'])}</div>
<h2>{h(proj['month'])} · {title}</h2>
<p class="help">Current month, data through {h(fmt_date(proj['latest']))}. This card ignores the date range above. It updates after the nightly 9:30pm Brisbane refresh; today's sales are not in yet.</p>
{online_note}{comm}
<div class="grid g2">
<div><div class="lbl">Straight run-rate</div><b class="stat">{money(_shown_sales(run, online_loaded))}</b>
<p class="help">Fuel {money(run['fuel'])} · shop {money(run['shop'])} · till GP {money(run['gp'])}</p></div>
<div><div class="lbl">Weekday-weighted</div><b class="stat">{money(_shown_sales(wt, online_loaded))}</b>
<p class="help">Fuel {money(wt['fuel'])} · shop {money(wt['shop'])} · till GP {money(wt['gp'])}. Remaining days use the same weekday's average from the last 28 loaded days.</p></div>
</div>{oct}</section>"""


def _dash_money(value):
    if value is None:
        return "—"
    return money(value)


def sales_display_rows(days, online_by, loaded):
    """Till rows plus any online-only days. combined is till gross plus online gross."""
    online_by = online_by or {}
    shown = []
    seen = set()
    for row in days:
        seen.add(row["date"])
        item = dict(row)
        item["till"] = float(row.get("total") or 0)
        item["online"] = float(online_by.get(row["date"]) or 0) if loaded else None
        item["combined"] = item["till"] + (item["online"] or 0)
        shown.append(item)
    if loaded:
        for day, amt in online_by.items():
            if day in seen:
                continue
            shown.append({
                "date": day, "online_only": True, "till": None, "online": float(amt or 0),
                "combined": float(amt or 0), "fuel": None, "shop": None, "gp": None,
                "gp_pct": None, "coverage": None, "missing": None,
            })
    shown.sort(key=lambda row: row["date"])
    return shown


def daily_table(days):
    if not days:
        return '<p class="empty">No completed sales days in this range.</p>'
    loaded = any(row.get("online") is not None or row.get("online_only") for row in days)
    body = []
    for row in days:
        online_only = row.get("online_only")
        if online_only:
            till_cell = fuel_cell = shop_cell = gp_cell = gpp_cell = cov_cell = miss_cell = "—"
            online_cell = money(row.get("online") or 0)
            total_cell = money(row.get("combined") or 0)
        else:
            till_cell = money(row.get("till") if row.get("till") is not None else row.get("total"))
            total_cell = money(row.get("combined") if row.get("combined") is not None else row.get("total"))
            online_cell = money(row.get("online")) if loaded else "—"
            fuel_cell = _dash_money(row.get("fuel"))
            shop_cell = _dash_money(row.get("shop"))
            gp_cell = _dash_money(row.get("gp"))
            gpp_cell = pct_ratio(row.get("gp_pct"))
            cov_cell = pct_ratio(row.get("coverage"))
            miss_cell = h(row.get("missing") if row.get("missing") is not None else "—")
        body.append(
            "<tr><td>" + h(fmt_date(row["date"])) + "</td><td class=\"num\">" + total_cell +
            "</td><td class=\"num\">" + till_cell + "</td><td class=\"num\">" + online_cell +
            "</td><td class=\"num\">" + fuel_cell + "</td><td class=\"num\">" + shop_cell +
            "</td><td class=\"num\">" + gp_cell + "</td><td class=\"num\">" + gpp_cell +
            "</td><td class=\"num\">" + cov_cell + "</td><td class=\"num\">" + miss_cell + "</td></tr>"
        )
    note = (
        "Total is till plus Bopple online gross. Shop GP is till GP. Online orders have no cost here."
        if loaded else
        "Online orders are not loaded, so Total equals till sales. Shop GP is till GP."
    )
    return (
        """<div class="tw"><table><thead><tr><th>Date</th><th>Total</th><th>Till</th><th>Online</th><th>Fuel</th><th>Shop</th><th>Shop GP</th><th>GP%</th><th>Coverage</th><th>Missing cost</th></tr></thead><tbody>"""
        + "".join(body) + f'</tbody></table></div><p class="help">{note}</p>'
    )


def month_last_day(month_start):
    return (month_start.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)


def viewed_month(start, end):
    """Calendar month the overview lines describe. A range that crosses months uses the month it ends in."""
    if start.year == end.year and start.month == end.month:
        return date(start.year, start.month, 1)
    return date(end.year, end.month, 1)


def project_metric(daily, month_start, data_through):
    """Project one daily series across a calendar month in Australia/Brisbane.

    Loaded days are never filled with zero. When data_through falls inside the
    month, each remaining day after the latest loaded day uses the average of
    that weekday so far this month. If this month has no sample of that
    weekday, the average comes from the last 4 weeks (28 days ending on that
    latest day). If any remaining weekday has neither sample, the whole
    projection falls back to the month-to-date daily average times the days
    in the month. A finished month returns the loaded total, not a projection.
    """
    end = month_last_day(month_start)
    dim = end.day
    parsed = []
    for key, value in (daily or {}).items():
        if value is None:
            continue
        parsed.append((date.fromisoformat(key), float(value)))
    in_month = [(d, v) for d, v in parsed if d.year == month_start.year and d.month == month_start.month]
    result = {
        "monthKey": f"{month_start.year:04d}-{month_start.month:02d}",
        "month": month_start.strftime("%B %Y"),
        "daysInMonth": dim,
        "daysTraded": 0,
        "mtd": None,
        "projected": None,
        "method": "none",
        "status": "empty",
        "through": None,
    }
    phase_through = data_through
    if phase_through is None and in_month:
        phase_through = max(d for d, _ in in_month)
    if phase_through is None or month_start > phase_through:
        return result
    if end <= phase_through:
        if not in_month:
            return result
        total = sum(v for _, v in in_month)
        latest = max(d for d, _ in in_month)
        result.update(
            daysTraded=len(in_month), mtd=total, projected=total,
            method="actual", status="actual", through=latest.isoformat(),
        )
        return result
    traded = [(d, v) for d, v in in_month if d <= phase_through]
    if not traded:
        return result
    cutoff = max(d for d, _ in traded)
    mtd = sum(v for _, v in traded)
    n = len(traded)
    month_buckets = {i: [] for i in range(7)}
    for d, v in traded:
        month_buckets[d.weekday()].append(v)
    look_start = cutoff - timedelta(days=27)
    prior_buckets = {i: [] for i in range(7)}
    for d, v in parsed:
        if look_start <= d <= cutoff:
            prior_buckets[d.weekday()].append(v)
    estimates = []
    dow_ok = True
    cur = cutoff + timedelta(days=1)
    while cur <= end:
        pool = month_buckets[cur.weekday()] or prior_buckets[cur.weekday()]
        if not pool:
            dow_ok = False
            break
        estimates.append(sum(pool) / len(pool))
        cur += timedelta(days=1)
    result.update(daysTraded=n, mtd=mtd, status="project", through=cutoff.isoformat())
    if dow_ok:
        result["projected"] = mtd + sum(estimates)
        result["method"] = "weekday"
    else:
        result["projected"] = mtd / n * dim
        result["method"] = "average"
    return result


PROJECTION_NOTES = {
    "weekday": (
        "Projection: loaded days stay as they are, and a missing day already inside the month is not filled in. "
        "Each remaining day is the average of that weekday so far this month, or from the last 4 weeks when this month has no sample of that weekday."
    ),
    "average": (
        "Projection: month-to-date daily average times the days in the month. Weekday weighting needs "
        "a sample of each remaining weekday in this month or the last 4 weeks."
    ),
    "actual": (
        "Finished month. This is the total of the loaded trading days, not a projection. "
        "Missing days are not filled with zero."
    ),
}


def shop_sales_by_date(c):
    found = {}
    for row in rows(c.execute("SELECT date, shop FROM portal_sales_days")):
        if row["shop"] is not None:
            found[row["date"]] = float(row["shop"])
    return found


def kitchen_sales_by_date(c):
    """Kitchen sales inc GST, the same daily total the Kitchen page sums."""
    kitchen = meta_get(c, "sales").get("kitchen") or {}
    found = {}
    for row in kitchen.get("days") or []:
        if not isinstance(row, list) or len(row) < 2 or not row[0] or row[1] is None:
            continue
        try:
            day = need_date(str(row[0]), "kitchen day")
            found[day] = float(row[1])
        except (TypeError, ValueError):
            continue
    return found


def fuel_litres_by_date(c):
    """Sum of fuel qty. Prefer the stored daily total, otherwise add the grade litres."""
    found = {}
    for row in rows(c.execute("SELECT date, litres, ulp_l, diesel_l, pulp_l FROM portal_commission_days")):
        if row["litres"] is not None:
            found[row["date"]] = float(row["litres"])
            continue
        grades = [row["ulp_l"], row["diesel_l"], row["pulp_l"]]
        if any(value is not None for value in grades):
            found[row["date"]] = sum(float(value or 0) for value in grades)
    return found


def last_year_month_row(c, month_start):
    key = f"{month_start.year - 1:04d}-{month_start.month:02d}"
    return one(c.execute(
        "SELECT month, shop_sales_inc, food_sales_inc, fuel_litres, source, note FROM portal_ly_monthly WHERE month=?",
        (key,),
    ))


def _ly_change(projected, baseline):
    if projected is None or baseline is None or baseline == 0:
        return None
    return (float(projected) - float(baseline)) / float(baseline) * 100


def _metric_text(amount, kind):
    if amount is None:
        return None
    if kind == "litres":
        return litres(amount)
    return money(amount)


def _month_line(line_id, title, subtitle, tone_name, metric, kind, empty_label, ly_amount, ly_label, figure_label, mtd_label):
    if metric["mtd"] is None:
        mtd_html = f'<b class="ly-missing">{h(empty_label)}</b>'
    else:
        mtd_html = f'<b>{_metric_text(metric["mtd"], kind)}</b>'
    if metric["projected"] is None:
        projected_html = '<b class="ly-missing">—</b>'
    else:
        projected_html = f'<b>{_metric_text(metric["projected"], kind)}</b>'
    if ly_amount is None:
        ly_html = '<b class="ly-missing">No last-year figure yet</b>'
    else:
        change = _ly_change(metric["projected"], ly_amount)
        change_html = ""
        if change is not None:
            cls = "up" if change >= 0 else "down"
            change_html = f'<span class="trend {cls}">{h(f"{abs(change):.1f}% vs {ly_label}")}</span>'
        ly_html = f'<b>{_metric_text(ly_amount, kind)}</b>{change_html}'
    days = f'{metric["daysTraded"]} of {metric["daysInMonth"]}'
    return f"""<article class="month-line {tone_name}" id="{line_id}" data-method="{h(metric["method"])}">
<h3>{h(title)}</h3>
<p class="mini">{h(subtitle)}</p>
<div class="line-metrics">
<div id="{line_id}-mtd"><div class="lbl">{h(mtd_label)}</div>{mtd_html}</div>
<div id="{line_id}-days"><div class="lbl">Days traded</div><b>{h(days)}</b></div>
<div id="{line_id}-projected"><div class="lbl">{h(figure_label)}</div>{projected_html}</div>
<div id="{line_id}-ly"><div class="lbl">{h(ly_label)}</div>{ly_html}</div>
</div></article>"""


def month_lines_section(c, start, end):
    month_start = viewed_month(start, end)
    through_iso = data_through(c)
    through = date.fromisoformat(through_iso) if through_iso else None
    shop = project_metric(shop_sales_by_date(c), month_start, through)
    food = project_metric(kitchen_sales_by_date(c), month_start, through)
    fuel = project_metric(fuel_litres_by_date(c), month_start, through)
    if through is None:
        dated = [row["through"] for row in (shop, food, fuel) if row.get("through")]
        if dated:
            through = date.fromisoformat(max(dated))
    end_day = month_last_day(month_start)
    if through is None or month_start > through:
        phase = "empty"
    elif end_day <= through:
        phase = "actual"
    else:
        phase = "project"
    ly = last_year_month_row(c, month_start) or {}
    ly_label = date(month_start.year - 1, month_start.month, 1).strftime("%b %Y")
    figure_label = "Projected month-end" if phase == "project" else "Month total"
    mtd_label = "Month to date" if phase == "project" else "Loaded total"
    lines = (
        ("line-shop", "Shop sales", "Till non-fuel, inc GST", "tone-shop", shop, "money",
         "No shop sales loaded", ly.get("shop_sales_inc"), "shop"),
        ("line-food", "Food sales", "Kitchen sales, inc GST", "tone-food", food, "money",
         "No kitchen sales loaded", ly.get("food_sales_inc"), "food"),
        ("line-fuel", "Fuel litres", "Sum of fuel qty", "tone-fuel", fuel, "litres",
         "No fuel litres loaded", ly.get("fuel_litres"), "fuel"),
    )
    body = "".join(
        _month_line(line_id, title, subtitle, tone_name, metric, kind, empty_label, ly_amount, ly_label, figure_label, mtd_label)
        for line_id, title, subtitle, tone_name, metric, kind, empty_label, ly_amount, _key in lines
    )
    notes = []
    if phase == "empty" and all(item[4]["status"] == "empty" for item in lines):
        notes.append("No shop, food or fuel figures are loaded for this month yet.")
    else:
        for item in lines:
            text = PROJECTION_NOTES.get(item[4]["method"])
            if text and text not in notes:
                notes.append(text)
    if phase == "project":
        compare = "The percentage compares this projected month-end with the same month last year."
    else:
        compare = "The percentage compares this month total with the same month last year."
    compare += " A last-year figure that has not been uploaded is left blank. It is not shown as zero."
    if phase == "project":
        kicker = "Month-end projection"
    elif phase == "actual":
        kicker = "Month total"
    else:
        kicker = "Monthly lines"
    through_bit = ""
    if through_iso:
        through_bit = f" Data through {h(fmt_date(through_iso))}."
    source_bits = []
    if ly.get("source"):
        source_bits.append(str(ly["source"]))
    if ly.get("note"):
        source_bits.append(str(ly["note"]))
    source_html = ""
    if source_bits:
        source_html = f'<p class="help" id="month-lines-source">{h(" ".join(source_bits))}</p>'
    return f"""<section class="card" id="month-lines" data-phase="{h(phase)}">
<div class="lbl">{h(kicker)}</div>
<h2>{h(month_start.strftime("%B %Y"))}</h2>
<p class="help">Australia/Brisbane. Shop sales, food sales and fuel litres for this calendar month. The date range above filters the other cards on this page.{through_bit}</p>
<div class="month-lines">{body}</div>
<p class="help" id="month-lines-method">{h(" ".join(notes))}</p>
<p class="help" id="month-lines-compare">{h(compare)}</p>
<p class="help" id="month-lines-footnote">Till only. Shop sales are the till daysheet shop total, inc GST, and do not include Bopple. Food sales are the kitchen total from the Kitchen page, inc GST, and are already part of shop sales. Fuel litres are the sum of unleaded, diesel and premium. Bopple online sales stay on Online orders.</p>
{source_html}</section>"""


def _wage_ratio_card(figure_id, label, window_label, start, end, figure):
    bill = figure["bill"]
    sales = figure["sales"]
    if figure["ratio"] is None:
        if bill["missing"]:
            reason = missing_rate_note(bill) or "A pay rate is missing, so this percentage is not shown."
        elif not bill["complete"]:
            reason = "No staff hours or contract entries in this period."
        elif not sales or sales["total_ex"] <= 0:
            reason = "No till sales loaded for this period."
        else:
            reason = "This percentage is not shown."
        sales_bit = ""
        if sales and sales["total_ex"] > 0:
            sales_bit = (
                f'<p class="mini">Till sales {money(sales["total_inc"])} inc GST, '
                f'{money(sales["total_ex"])} excl GST. The percentage is left blank.</p>'
            )
        return (
            f'<div class="card stat" id="{figure_id}"><div class="lbl">{h(label)}</div>'
            f'<b>—</b><span>{h(reason)}</span>'
            f'<p class="mini">{h(window_label)} · {h(fmt_date(start.isoformat()))} – {h(fmt_date(end.isoformat()))}</p>'
            f"{sales_bit}</div>"
        )
    excl = bill["combined_excl"]
    return (
        f'<div class="card stat" id="{figure_id}"><div class="lbl">{h(label)}</div>'
        f'<b>{pct_text(figure["ratio"])}</b>'
        f'<span>wages incl. super as % of till sales excl. GST</span>'
        f'<p class="mini">{h(window_label)} · {h(fmt_date(start.isoformat()))} – {h(fmt_date(end.isoformat()))}. '
        f'Wages {money(figure["wages_incl"])} incl. super ({money(excl)} excl. super). '
        f'Till sales {money(sales["total_inc"])} inc GST, {money(sales["total_ex"])} excl GST '
        f'({sales["days"]} till day{"s" if sales["days"] != 1 else ""}).</p></div>'
    )


def wages_vs_sales_section(c):
    """Current Brisbane week (Monday through today) and calendar month to date. Ignores the period chips."""
    end = today()
    week_start = week_monday(end)
    month_start = end.replace(day=1)
    week = wages_for_range(c, week_start, end)
    month = wages_for_range(c, month_start, end)
    week_card = _wage_ratio_card("wages-week", "This week", "Monday through today", week_start, end, week)
    month_card = _wage_ratio_card("wages-month", "Month to date", "First of the month through today", month_start, end, month)
    return f"""<section class="card" id="wages-vs-sales"><h2>Wages vs sales</h2>
<p class="help" id="wages-basis">These two figures ignore the date range above. They use the current week and this calendar month, through today, Australia/Brisbane. The percentage is employee wages including super, plus contract costs with no super, divided by till sales excluding GST. Till shop and fuel amounts are stored including GST, so each is divided by 1.1. The portal does not store an ex-GST till total. Online orders are not included. A dash means hours, a pay rate, or till sales are missing. Contractors are included. Nothing here is sent to Xero.</p>
<div class="grid g2">{week_card}{month_card}</div></section>"""


def page_overview(c, query):
    through = data_through(c)
    anchor = anchor_day(c)
    try:
        active, start, end = period_bounds(query.get("period"), query.get("from"), query.get("to"), anchor)
        error = ""
    except ValueError as ex:
        active, start, end = period_bounds("month", None, None, anchor)
        error = f'<p class="alert">{h(str(ex))}</p>'
    days, deltas = parity().adjust_sales_days(c, sales_between(c, start, end))
    till_total = sum(r["total"] or 0 for r in days)
    fuel = sum(r["fuel"] or 0 for r in days)
    shop = sum(r["shop"] or 0 for r in days)
    gp = sum(r["gp"] or 0 for r in days)
    covered = sum(r["covered_ex"] or 0 for r in days)
    gp_pct = (gp / covered) if covered else None
    share = (fuel / till_total * 100) if till_total else 0
    shop_avg = (shop / len(days)) if days else None
    loaded = online_loaded(c)
    online_rows = online_days_between(c, start, end) or []
    online_sum = online_totals(online_rows) if loaded else None
    online_gross = online_sum["total"] if online_sum else 0
    total = till_total + online_gross
    online_by = {r["date"]: float(r["total"] or 0) for r in online_rows} if loaded else {}
    display = sales_display_rows(days, online_by, loaded)
    period = parity().matching_period(c, active, start, end)
    summary = (period or {}).get("summary") or {}
    exact = period and period.get("fromDate") == start.isoformat() and period.get("toDate") == end.isoformat()
    covered_changed = any(d.get("covered_delta") for d in deltas)
    if exact and summary and not covered_changed:
        coverage = summary.get("coverage")
        missing = summary.get("missing")
        missing_label = "products missing cost"
    else:
        weighted = shop_ex = 0.0
        missing = 0
        for row in days:
            if row.get("_shop_ex"):
                shop_ex += row["_shop_ex"]
                weighted += row.get("covered_ex") or 0
            if row.get("missing") is not None:
                missing += row["missing"]
        coverage = (weighted / shop_ex) if shop_ex else None
        missing_label = "sum of each day's missing-cost count"
    all_sales, _all_deltas = parity().adjust_sales_days(c, rows(c.execute("SELECT * FROM portal_sales_days")))
    all_comm = rows(c.execute("SELECT * FROM portal_commission_days"))
    latest = through or (max((r["date"] for r in all_sales), default=None))
    amounts = online_amounts(c)
    proj = project_month(all_sales, all_comm, target_for(latest, meta_get(c, "sales"), meta_get(c, "fuel")), latest, amounts or {}) if latest else None
    october = (meta_get(c, "sales").get("commission") or {}).get("october")
    prev_start, prev_end = parity()._prev_bounds(active, start, end)
    prev_rows, _prev_delta = parity().adjust_sales_days(c, sales_between(c, prev_start, prev_end))
    prev_online = online_totals(online_days_between(c, prev_start, prev_end) or [])["total"] if loaded else 0
    prev_total = sum(r["total"] or 0 for r in prev_rows) + prev_online
    if prev_total:
        move = (total - prev_total) / prev_total * 100
        trend = f'<span class="trend {"up" if move >= 0 else "down"}">{abs(move):.1f}% vs previous period</span>'
    else:
        trend = ""
    series = display[-14:]
    peak = max((r["combined"] or 0 for r in series), default=0) or 1
    bars = []
    for row in series:
        height = max(5, min(100, int(round((row["combined"] or 0) / peak * 20)) * 5))
        bars.append(f'<i class="h{height}"></i>')
    chart = f'<div class="vchart" role="img" aria-label="Daily total sales, till plus online">{"".join(bars)}</div>' if bars else ""
    caption = f"{len(days)} completed sales day{'s' if len(days) != 1 else ''} in {fmt_date(start.isoformat())} – {fmt_date(end.isoformat())}"
    later_note = ""
    if loaded and active == "month":
        month_end = (end.replace(day=28) + timedelta(days=4)).replace(day=1)
        later_rows = rows(c.execute(
            "SELECT total FROM portal_online_days WHERE date>? AND date<?",
            (end.isoformat(), month_end.isoformat()),
        ))
        if later_rows:
            later_amt = sum(float(r["total"] or 0) for r in later_rows)
            later_note = (
                f'<p class="mini">Plus {money(later_amt)} online after {h(fmt_date(end.isoformat()))}, '
                'not in this total yet. <a href="/portal/online">Online orders</a>.</p>'
            )
    if loaded:
        online_card = (
            f'<div class="card stat tone-online" id="online-sales"><div class="lbl">Online orders (Bopple)</div>'
            f'<b>{money(online_gross)}</b><span>{online_sum["orders"]} orders · gross, not in the till</span>'
            f'<a href="/portal/online">View orders</a>{later_note}</div>'
        )
        sales_note = "Total sales add Bopple online order gross on top of till sales. Those orders are not rung through the till. Shop GP is till GP only."
        total_span = "till plus online gross, inc GST"
    else:
        online_card = (
            '<div class="card stat tone-online" id="online-sales"><div class="lbl">Online orders (Bopple)</div>'
            '<b>—</b><span>not loaded yet</span><a href="/portal/online">View orders</a></div>'
        )
        sales_note = "Online orders (Bopple) are not loaded yet, so total sales are the till. Shop GP is till GP."
        total_span = "till sales, online not loaded"
    callout = ""
    if missing:
        callout = (
            f'<p class="callout" id="provisional">Shop GP is provisional. Cost coverage is {pct_ratio(coverage)}. '
            f'{h(missing)} {missing_label}. This is 2026 only and is not a warning about 2025.</p>'
        )
    return f"""<div class="ph"><h1>Sales overview</h1>{through_line(through)}<p>{h(caption)}. Figures are the loaded trading days, with no zero filled in for a missing day. Australia/Brisbane. Today’s sales wait for the 9:30pm refresh. {h(sales_note)}</p></div>
{daypage().glance(c)}
{error}{period_controls('/portal', active, start, end)}
{parity().override_note(deltas)}{callout}
<section class="grid g4" id="sales-split">
<div class="card stat tone-sales" id="total-sales"><div class="lbl">Total sales</div><b>{money(total)}</b><span>{h(total_span)}</span>{trend}</div>
<div class="card stat" id="till-sales"><div class="lbl">Till sales</div><b>{money(till_total)}</b><span>daysheet, inc GST</span></div>
{online_card}
<div class="card stat tone-gp" id="shop-gp"><div class="lbl">Shop GP</div><b>{money(gp)}</b><span>Till GP · {pct_ratio(gp_pct)} of covered ex-GST sales</span></div>
</section>
<section class="grid g3">
<div class="card stat tone-cover" id="coverage"><div class="lbl">Cost coverage</div><b>{pct_ratio(coverage)}</b><span>covered ex-GST shop sales</span></div>
<div class="card stat tone-warn" id="missing-cost"><div class="lbl">Products missing cost</div><b>{h(missing if missing is not None else '—')}</b><span>{h(missing_label)}</span></div>
<div class="card stat tone-avg" id="shop-average"><div class="lbl">Daily shop average</div><b>{money(shop_avg)}</b><span>shop sales inc GST per loaded day</span></div>
</section>
{wages_vs_sales_section(c)}
{month_lines_section(c, start, end)}
{exceptions().overview_card(c)}
{insights_box(latest_insight(c))}
<section class="card"><h2>Fuel vs shop</h2>
<div class="grid g2">
<div class="stat tone-fuel"><div class="lbl">Fuel</div><b>{money(fuel)}</b><span>{pct_points(share) if till_total else '—'} of till</span></div>
<div class="stat tone-shop"><div class="lbl">Shop</div><b>{money(shop)}</b><span>{pct_points(100 - share) if till_total else '—'} of till</span></div>
</div>
{chart}
<div class="split"><i class="fuel {bar_class(share)}"></i><i class="shop {bar_class(100 - share if till_total else 0)}"></i></div>
<p class="help">Fuel and shop are the till daysheet. Online orders are not in this split. Shop GP is till GP and excludes sales with no usable cost. It is not operating profit.</p></section>
{projection_card(proj, october, amounts is not None)}
{parity().last_year_with_litres(c, all_sales, all_comm)}
<p class="help">Day by day sales, dips, hours, and the bills received that day are on <a href="/portal/day">Daily summary</a>.</p>"""


def _online_query(active, start, end, day=""):
    params = {"period": active}
    if active == "custom":
        params["from"] = start.isoformat()
        params["to"] = end.isoformat()
    if day:
        params["day"] = day
    return "/portal/online?" + urlencode(params)


def _online_stats(title, totals, elem_id):
    refund = ""
    if totals["refunds"]:
        refund = f'<p class="help">Refunds {money(totals["refunds"])}, signed as Bopple reports them.</p>'
    return f"""<section class="card" id="{elem_id}"><h2>{h(title)}</h2>
<section class="grid g4">
<div class="card stat"><div class="lbl">Orders</div><b>{totals['orders']}</b><span>in this range</span></div>
<div class="card stat tone-sales"><div class="lbl">Gross total</div><b>{money(totals['total'])}</b><span>online order total</span></div>
<div class="card stat tone-online"><div class="lbl">Bopple fees</div><b>{money(totals['fees'])}</b><span>signed as reported</span></div>
<div class="card stat"><div class="lbl">Net payout</div><b>{money(totals['net'])}</b><span>gross plus fees plus refunds</span></div>
</section>
{refund}</section>"""


def _loaded_items(raw):
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    return data if isinstance(data, list) else []


def _qty_label(value):
    if value is None:
        return "—"
    number = Decimal(str(value))
    if number == number.to_integral():
        return f"{int(number):,}"
    return f"{number:,.2f}"


def _order_matches(order, needle):
    if not needle:
        return True
    hay = [order.get("customer_name") or ""]
    for item in _loaded_items(order.get("items")):
        hay.append(str(item.get("product") or ""))
        for mod in item.get("modifiers") or []:
            if isinstance(mod, dict):
                hay.append(str(mod.get("name") or ""))
    return needle.casefold() in " ".join(hay).casefold()


def _top_online_items(order_rows, limit=8):
    found = {}
    for order in order_rows:
        seen = set()
        for item in _loaded_items(order.get("items")):
            name = " ".join(str(item.get("product") or "").split())
            if not name:
                continue
            key = name.casefold()
            bucket = found.setdefault(key, {"name": name, "qty": 0.0, "total": 0.0, "orders": 0})
            bucket["qty"] += float(item.get("qty") or 0)
            bucket["total"] += float(item.get("total") or 0)
            if key not in seen:
                bucket["orders"] += 1
                seen.add(key)
    ranked = sorted(found.values(), key=lambda row: (-row["qty"], -row["total"], row["name"].casefold()))
    return ranked[:limit]


def _top_items_html(order_rows):
    ranked = _top_online_items(order_rows)
    if not ranked:
        body = '<p class="empty">No item lines in this period yet.</p>'
    else:
        lines = []
        for row in ranked:
            lines.append(
                f"<tr><td>{h(row['name'])}</td><td class=\"num\">{row['orders']}</td>"
                f"<td class=\"num\">{_qty_label(row['qty'])}</td><td class=\"num\">{money(row['total'])}</td></tr>"
            )
        body = (
            '<div class="tw"><table><thead><tr><th>Item</th><th>Orders</th><th>Qty</th><th>Item total</th></tr></thead><tbody>'
            + "".join(lines) + "</tbody></table></div>"
        )
    return (
        '<section class="card" id="online-top-items"><h2>Top online items</h2>'
        '<p class="help">Products in the selected period, by quantity. Modifiers are listed on the order, not in this ranking.</p>'
        f"{body}</section>"
    )


def _item_lines_html(raw):
    items = _loaded_items(raw)
    if not items:
        return '<p class="help">No item lines for this order.</p>'
    lines = []
    for item in items:
        mods = []
        for mod in item.get("modifiers") or []:
            if not isinstance(mod, dict):
                continue
            category = f" · {h(mod.get('category'))}" if mod.get("category") else ""
            mods.append(
                f"<li>{h(mod.get('name') or '—')}{category} · {_qty_label(mod.get('qty'))} · {money(mod.get('total'))}</li>"
            )
        extra = f"<ul>{''.join(mods)}</ul>" if mods else ""
        category = f'<div class="mini">{h(item.get("category"))}</div>' if item.get("category") else ""
        lines.append(
            f"<li><b>{h(item.get('product') or '—')}</b>{category} · {_qty_label(item.get('qty'))} · {money(item.get('total'))}{extra}</li>"
        )
    return f"<ul>{''.join(lines)}</ul>"


def _order_card(item, open_it):
    who = item.get("customer_name") or "No customer name"
    kind = item.get("order_type") or ""
    when = item.get("fulfilment_time") or ""
    bits = [h(item["order_no"]), h(who)]
    if kind:
        bits.append(h(kind))
    if when:
        bits.append(h(when))
    bits.append(money(item["total"]))
    opened = " open" if open_it else ""
    return (
        f'<details class="order" id="order-{h(item["order_no"])}"{opened}><summary>{" · ".join(bits)}</summary>'
        f'<p class="help">{h(item["ordered_at"])} · {h(item["wallet"])} · {h(item["card_brand"] or "—")} · '
        f'fees {money(item["fees"])} · refunds {money(item["refunds"])} · net {money(item["net"])}</p>'
        f'{_item_lines_html(item.get("items"))}</details>'
    )


def page_online(c, query):
    meta = meta_get(c, "online-orders")
    anchor = online_anchor(c)
    try:
        active, start, end = period_bounds(query.get("period"), query.get("from"), query.get("to"), anchor)
        error = ""
    except ValueError as ex:
        active, start, end = period_bounds("month", None, None, anchor)
        error = f'<p class="alert">{h(str(ex))}</p>'
    if not meta.get("asOf"):
        return f"""<div class="ph"><h1>Online orders</h1>{through_line(data_through(c))}
<p>Bopple pickup orders paid online. They are extra sales, not rung through the till.</p></div>
<section class="card"><p class="empty" id="online-empty">No online orders yet. The nightly refresh sends them as dataset online-orders. Till sales are unchanged until then.</p></section>"""
    mtd_end = anchor
    mtd_start = anchor.replace(day=1)
    mtd = online_totals(online_days_between(c, mtd_start, mtd_end))
    period_rows = online_days_between(c, start, end) or []
    period = online_totals(period_rows)
    day_filter = query.get("day") if query.get("day") in {r["date"] for r in period_rows} else ""
    daily_body = []
    for row in sorted(period_rows, key=lambda r: r["date"], reverse=True):
        href = _online_query(active, start, end, row["date"])
        daily_body.append(
            f'<tr><td><a href="{href}">{h(fmt_date(row["date"]))}</a></td>'
            f'<td class="num">{int(row["orders"] or 0)}</td>'
            f'<td class="num">{money(row["total"])}</td><td class="num">{money(row["fees"])}</td>'
            f'<td class="num">{money(row["refunds"])}</td><td class="num">{money(row["net"])}</td></tr>'
        )
    if daily_body:
        daily_table_html = (
            '<div class="tw" id="online-daily"><table><thead><tr><th>Date</th><th>Orders</th><th>Gross</th>'
            '<th>Bopple fees</th><th>Refunds</th><th>Net payout</th></tr></thead><tbody>'
            + "".join(daily_body) + "</tbody></table></div>"
        )
    else:
        daily_table_html = '<p class="empty" id="online-daily">No online orders in this range.</p>'
    period_orders = rows(c.execute(
        "SELECT * FROM portal_online_orders WHERE trading_date>=? AND trading_date<=?",
        (start.isoformat(), end.isoformat()),
    ))
    order_rows = [row for row in period_orders if row["trading_date"] == day_filter] if day_filter else list(period_orders)
    needle = " ".join((query.get("q") or "").split())[:80]
    if needle:
        order_rows = [row for row in order_rows if _order_matches(row, needle)]
    order_rows.sort(key=lambda r: order_time_key(r["ordered_at"], r["trading_date"]), reverse=True)
    grouped = {}
    for order in order_rows:
        grouped.setdefault(order["trading_date"], []).append(order)
    options = ['<option value="">All dates</option>']
    for row in sorted(period_rows, key=lambda r: r["date"], reverse=True):
        selected = " selected" if row["date"] == day_filter else ""
        options.append(f'<option value="{h(row["date"])}"{selected}>{h(fmt_date(row["date"]))}</option>')
    hidden = f'<input type="hidden" name="period" value="{h(active)}">'
    if active == "custom":
        hidden += f'<input type="hidden" name="from" value="{h(start.isoformat())}"><input type="hidden" name="to" value="{h(end.isoformat())}">'
    blocks = []
    newest_no = order_rows[0]["order_no"] if order_rows else ""
    for trading_date in sorted(grouped, reverse=True):
        items = grouped[trading_date]
        gross = sum(float(item["total"] or 0) for item in items)
        opened = " open" if trading_date == (day_filter or max(grouped)) else ""
        cards = []
        for item in items:
            open_it = item["order_no"] == newest_no or (bool(needle) and len(order_rows) <= 12)
            cards.append(_order_card(item, open_it))
        blocks.append(
            f'<details class="shift"{opened}><summary>{h(fmt_date(trading_date))} · {len(items)} '
            f'order{"s" if len(items) != 1 else ""} · {money(gross)}</summary>'
            + "".join(cards) + "</details>"
        )
    if not blocks:
        order_html = '<p class="empty">No orders match that search.</p>' if needle else '<p class="empty">No orders in this range.</p>'
    else:
        order_html = "".join(blocks)
    clear = ""
    if day_filter:
        clear = f'<a class="chip" href="{_online_query(active, start, end)}">All dates</a>'
    if needle:
        clear += f' <a class="chip" href="{_online_query(active, start, end, day_filter)}">Clear search</a>'
    source = meta.get("source") or "Bopple"
    return f"""<div class="ph"><h1>Online orders</h1>{through_line(meta.get("asOf"))}
<p>Pickup orders paid online through {h(source)}. These are extra sales and are not rung through the till. The gross total is added on top of till sales on the overview. Shop GP stays till GP, because these orders have no cost.</p>
<p class="help">Fees and refunds keep the sign Bopple sends. A fee is usually negative. Net payout is gross plus fees plus refunds.</p></div>
{error}{period_controls('/portal/online', active, start, end)}
{_online_stats(f"Month to date · {fmt_date(mtd_start.isoformat())} – {fmt_date(mtd_end.isoformat())}", mtd, "online-mtd")}
{_online_stats(f"Selected period · {fmt_date(start.isoformat())} – {fmt_date(end.isoformat())}", period, "online-period")}
<section class="card"><h2>Daily totals</h2>
<p class="help">Newest day first. Choose a date to list that day’s orders.</p>
{daily_table_html}</section>
{_top_items_html(period_orders)}
<section class="card" id="online-order-list"><h2>Orders</h2>
<form class="row" method="get" action="/portal/online" id="online-search">{hidden}
<label class="f">Date<select name="day">{''.join(options)}</select></label>
<label class="f">Customer or item<input name="q" value="{h(needle)}" maxlength="80" placeholder="Name or item"></label>
<button class="btn" type="submit">Show</button></form>
{clear}
{order_html}</section>"""


def query_list(query, key):
    if not query:
        return []
    value = query.get(key)
    if value is None:
        return []
    if isinstance(value, list):
        items = value
    else:
        items = [value]
    return [str(item).strip() for item in items if str(item).strip()]


def named_suppliers_from(rows_in):
    found = {}
    for row in rows_in:
        name = (row.get("supplier") or "").strip()
        if name:
            found.setdefault(name.casefold(), name)
    return [found[key] for key in sorted(found)]


def named_suppliers(c, tables):
    found = {}
    for table in tables:
        for row in rows(c.execute(f"SELECT DISTINCT supplier FROM {table}")):
            name = (row["supplier"] or "").strip()
            if name:
                found.setdefault(name.casefold(), name)
    return [found[key] for key in sorted(found)]


def chosen_suppliers(query, known):
    """Supplier names from the query that exist in `known`. Empty means no filter."""
    wanted = query_list(query, "supplier")
    if not wanted:
        return [], False
    by_key = {name.casefold(): name for name in known}
    chosen, seen = [], set()
    for raw in wanted:
        name = by_key.get(raw.casefold())
        if name and name.casefold() not in seen:
            chosen.append(name)
            seen.add(name.casefold())
    return chosen, True


def supplier_sql(filtering, selected):
    if not filtering:
        return "", []
    if not selected:
        return " AND 1=0", []
    marks = ",".join("?" for _ in selected)
    return f" AND supplier IN ({marks})", list(selected)


UNASSIGNED_SUPPLIER = "Unassigned"
SUPPLIER_NAME_LIMIT = 120
PRODUCT_RETURN_KEYS = ("period", "from", "to", "dept", "group", "q", "sort", "product", "compare", "arrange")
STOCK_RETURN_KEYS = ("q", "filter", "pick", "edit", "sort", "arrange")
_RETURN_KEYS = set(PRODUCT_RETURN_KEYS) | set(STOCK_RETURN_KEYS) | {"supplier"}


def clean_supplier_name(value):
    """Blank clears the saved supplier. Anything else is one supplier name."""
    if value is None:
        return ""
    if isinstance(value, bool) or not isinstance(value, str):
        raise ValueError("Enter a supplier name.")
    text = re.sub(r"\s+", " ", value.strip())
    if not text:
        return ""
    if len(text) > SUPPLIER_NAME_LIMIT:
        raise ValueError("That supplier name is too long.")
    if text.casefold() == UNASSIGNED_SUPPLIER.casefold():
        raise ValueError("Leave the supplier blank to show it as unassigned.")
    if has_card_number(text):
        raise ValueError("The supplier must not include a card number.")
    return text


def known_supplier_names(c):
    """Suppliers already on purchases, invoices, or a product the owner named."""
    return named_suppliers(c, ("portal_bills", "portal_invoices", "portal_spend", "portal_product_suppliers"))


def supplier_filter_names(c):
    names = [name for name in known_supplier_names(c) if name.casefold() != UNASSIGNED_SUPPLIER.casefold()]
    return [UNASSIGNED_SUPPLIER] + names


def manual_supplier_map(c):
    """Owner choices. Nightly ingest does not write this table."""
    return {row["code"]: row["supplier"] for row in rows(c.execute("SELECT code, supplier FROM portal_product_suppliers"))}


def _invoice_index(c):
    """Invoice number to the suppliers that used it, spelling kept from the latest row."""
    found = {}
    queries = (
        "SELECT supplier, invoice_no AS number, bill_date AS dated FROM portal_bills",
        "SELECT supplier, invoice_number AS number, COALESCE(invoice_date, '') AS dated FROM portal_invoices",
    )
    for sql in queries:
        for row in rows(c.execute(sql)):
            key = (row["number"] or "").strip().casefold()
            name = (row["supplier"] or "").strip()
            if not key or not name:
                continue
            found.setdefault(key, []).append((row["dated"] or "", name))
    return found


def _supplier_from_invoice(matches):
    """One supplier for an invoice number. Two different suppliers is not a guess."""
    if not matches:
        return ""
    folded = {}
    for dated, name in matches:
        folded.setdefault(name.casefold(), []).append((dated or "", name))
    if len(folded) != 1:
        return ""
    rows_for = next(iter(folded.values()))
    rows_for.sort()
    return rows_for[-1][1]


def _pick_suggested(items):
    """Latest delivery wins. A tie on that date needs a single most-used supplier."""
    if not items:
        return ""
    latest = max(when for when, _name in items)
    newest = []
    seen = set()
    for when, name in items:
        if when == latest and name.casefold() not in seen:
            seen.add(name.casefold())
            newest.append(name)
    if len(newest) == 1:
        return newest[0]
    counts = {}
    order = []
    for _when, name in items:
        key = name.casefold()
        if key not in counts:
            order.append(name)
            counts[key] = 0
        counts[key] += 1
    order.sort(key=lambda name: (-counts[name.casefold()], name.casefold()))
    if len(order) == 1 or counts[order[0].casefold()] > counts[order[1].casefold()]:
        return order[0]
    return ""


def suggested_supplier_map(c):
    """Supplier implied by a delivery whose invoice number is already on a bill or invoice.

    The newest unambiguous delivery wins. A saved owner choice is applied later and replaces this.
    """
    invoices = _invoice_index(c)
    votes = {}
    for row in rows(c.execute(
        """SELECT code, move_date, reference FROM portal_stock_movements
           WHERE source='supplier-invoice' AND reference IS NOT NULL AND TRIM(reference) != ''"""
    )):
        name = _supplier_from_invoice(invoices.get(row["reference"].strip().casefold()) or [])
        if not name:
            continue
        votes.setdefault(row["code"], []).append((row["move_date"] or "", name))
    out = {}
    for code, items in votes.items():
        chosen = _pick_suggested(items)
        if chosen:
            out[code] = chosen
    return out


def supplier_state(code, manual, suggested):
    """Manual choice wins. A suggestion is only the pre-fill until the owner saves one."""
    chosen = (manual.get(code) or "").strip()
    hint = (suggested.get(code) or "").strip()
    if chosen:
        return {"name": chosen, "source": "manual", "suggestion": hint}
    if hint:
        return {"name": hint, "source": "suggested", "suggestion": hint}
    return {"name": "", "source": "", "suggestion": ""}


def supplier_rank(name):
    """Named suppliers first, Unassigned last, then A–Z."""
    text = (name or "").strip()
    if not text:
        return (1, "")
    return (0, text.casefold())


def assign_product_supplier(c, user, code, raw):
    """Save or clear one supplier for a product. Does not run during ingest."""
    code = stock()._clean_code(code)
    if stock()._known_product(c, code) is None:
        raise ValueError("Choose a product from the catalog.")
    name = clean_supplier_name(raw)
    if not name:
        c.execute("DELETE FROM portal_product_suppliers WHERE code=?", (code,))
        return ""
    c.execute(
        """INSERT INTO portal_product_suppliers(code,supplier,updated_at,updated_by) VALUES(?,?,?,?)
           ON CONFLICT(code) DO UPDATE SET supplier=excluded.supplier, updated_at=excluded.updated_at,
             updated_by=excluded.updated_by""",
        (code, name, stamp(), user["username"]),
    )
    return name


def supplier_kept(name, selected, filtering):
    if not filtering:
        return True
    if not selected:
        return False
    label = name or UNASSIGNED_SUPPLIER
    return label.casefold() in {item.casefold() for item in selected}


def return_query(query, keys):
    pairs = []
    for key in keys:
        value = (query or {}).get(key)
        if isinstance(value, list):
            value = value[0] if value else ""
        if value not in (None, ""):
            pairs.append((key, str(value)[:200]))
    for name in query_list(query, "supplier")[:40]:
        pairs.append(("supplier", str(name)[:120]))
    return urlencode(pairs)


def back_pairs(raw):
    if not isinstance(raw, str):
        return []
    raw = raw.strip()
    if not raw or len(raw) > 4000 or "://" in raw or raw.startswith("/") or any(ch in raw for ch in "\r\n"):
        return []
    parsed = parse_qs(raw, keep_blank_values=False)
    pairs = []
    for key, values in parsed.items():
        if key not in _RETURN_KEYS:
            continue
        limit = 40 if key == "supplier" else 1
        for value in values[:limit]:
            text = str(value).strip()[:200]
            if text:
                pairs.append((key, text))
    return pairs


def back_query(raw):
    query = {}
    for key, value in back_pairs(raw):
        if key in query:
            current = query[key]
            query[key] = current + [value] if isinstance(current, list) else [current, value]
        else:
            query[key] = value
    return query


def safe_portal_return(base, raw):
    if base not in ("/portal/products", "/portal/stock"):
        base = "/portal/products"
    pairs = back_pairs(raw)
    if not pairs:
        return base
    return base + "?" + urlencode(pairs)


def with_ok(path, flag):
    if not re.fullmatch(r"[a-z0-9-]{1,32}", str(flag or "")):
        return path
    join = "&" if "?" in path else "?"
    return path + join + "ok=" + flag


def supplier_datalist(names):
    options = []
    for name in names:
        if name.casefold() == UNASSIGNED_SUPPLIER.casefold():
            continue
        options.append(f'<option value="{h(name)}"></option>')
    return f'<datalist id="known-suppliers">{"".join(options)}</datalist>'


def supplier_editor(user, action, code, state, back, label):
    """Small picker. Known suppliers are listed, and a new name can be typed."""
    shown = state["name"]
    bits = [f'<span class="supplier-read">{h(shown or UNASSIGNED_SUPPLIER)}</span>']
    if state["source"] == "suggested":
        bits.append('<span class="pill">Suggested</span>')
    elif not shown:
        bits.append('<span class="mini">Unassigned</span>')
    if state["source"] == "manual" and state["suggestion"] and state["suggestion"].casefold() != shown.casefold():
        bits.append(f'<div class="mini">Invoices suggest {h(state["suggestion"])}</div>')
    bits.append(
        f'<form method="post" action="{h(action)}" class="supplier-set">'
        f'<input type="hidden" name="csrf" value="{h(user["csrf"])}">'
        f'<input type="hidden" name="action" value="set_supplier">'
        f'<input type="hidden" name="code" value="{h(code)}">'
        f'<input type="hidden" name="back" value="{h(back)}">'
        f'<input name="supplier" list="known-suppliers" value="{h(shown)}" maxlength="{SUPPLIER_NAME_LIMIT}" '
        f'aria-label="Supplier for {h(label)}" autocomplete="off">'
        f'<button class="btn sm" type="submit">Save</button></form>'
    )
    return f'<div class="supplier-block" id="supplier-{h(code)}">{"".join(bits)}</div>'


def supplier_filter_box(names, selected, filtering):
    if filtering and selected:
        summary = f"{len(selected)} supplier" + ("s" if len(selected) != 1 else "")
    elif filtering:
        summary = "No matching supplier"
    else:
        summary = "All suppliers"
    chosen = {name.casefold() for name in selected}
    boxes = []
    for name in names:
        checked = " checked" if name.casefold() in chosen else ""
        boxes.append(
            f'<label class="check" data-supplier="{h(name)}">'
            f'<input type="checkbox" name="supplier" value="{h(name)}"{checked}>{h(name)}</label>'
        )
    choices = '<p class="empty">No suppliers in this data yet.</p>' if not boxes else (
        '<input type="search" placeholder="Search suppliers" aria-label="Search suppliers" autocomplete="off">'
        f'<div class="choices">{"".join(boxes)}</div>'
    )
    opened = " open" if filtering else ""
    return (
        f'<details class="supplier-filter" data-supplier-filter{opened}>'
        f"<summary>{h(summary)}</summary>{choices}</details>"
    )


def _purchase_query(period, start, end, selected, filtering):
    pairs = [("period", period)]
    if period == "custom":
        pairs.extend((("from", start.isoformat()), ("to", end.isoformat())))
    if filtering:
        pairs.extend(("supplier", name) for name in selected)
    return "/portal/purchases?" + urlencode(pairs)


def purchase_filter_form(active, start, end, names, selected, filtering):
    presets = (("week", "This week"), ("month", "This month"), ("lastmonth", "Last month"))
    chips = []
    for key, label in presets:
        cls = " on" if key == active else ""
        chips.append(f'<a class="chip{cls}" href="{h(_purchase_query(key, start, end, selected, filtering))}">{label}</a>')
    keep = active if active != "custom" else "custom"
    clear_href = _purchase_query(keep, start, end, [], False)
    return f"""<form class="period" id="purchase-filters" method="get" action="/portal/purchases">
<div class="chips" role="group" aria-label="Date range">{''.join(chips)}</div>
<div class="row supplier-row">
<div class="f"><span>Suppliers</span>{supplier_filter_box(names, selected, filtering)}</div>
<button class="btn" type="submit" name="period" value="{h(keep)}">Apply suppliers</button>
<a class="btn ghost" href="{h(clear_href)}">Clear</a>
</div>
<div class="row date-row">
<label class="f">From<input type="date" name="from" value="{h(start.isoformat())}" required></label>
<label class="f">To<input type="date" name="to" value="{h(end.isoformat())}" required></label>
<button class="btn" type="submit" name="period" value="custom">Apply custom dates</button>
</div>
<p class="help">All suppliers is the default. Clear drops the supplier choice and keeps this date range. The address is the bookmark.</p>
</form>"""


def bills_between(c, start, end, filtering, selected):
    clause, params = supplier_sql(filtering, selected)
    return rows(c.execute(
        "SELECT * FROM portal_bills WHERE bill_date>=? AND bill_date<=?" + clause + " ORDER BY inc_gst DESC",
        [start.isoformat(), end.isoformat(), *params],
    ))


def page_purchases(c, query):
    through = data_through(c)
    anchor = anchor_day(c)
    try:
        active, start, end = period_bounds(query.get("period"), query.get("from"), query.get("to"), anchor)
        error = ""
    except ValueError as ex:
        active, start, end = period_bounds("month", None, None, anchor)
        error = f'<p class="alert">{h(str(ex))}</p>'
    names = named_suppliers(c, ("portal_bills", "portal_spend"))
    selected, filtering = chosen_suppliers(query, names)
    bills = bills_between(c, start, end, filtering, selected)
    by_supplier = {}
    for bill in bills:
        bucket = by_supplier.setdefault(bill["supplier"], {"bills": 0, "inc": 0.0, "due": 0.0})
        bucket["bills"] += 1
        bucket["inc"] += bill["inc_gst"] or 0
        status = (bill["status"] or "").upper()
        if status in ("A", "AUTHORISED", "AUTHORIZED") or (bill["amount_due"] or 0) > 0 and status not in ("P", "PAID"):
            bucket["due"] += bill["amount_due"] or 0
    ordered = sorted(by_supplier.items(), key=lambda kv: kv[1]["inc"], reverse=True)
    body = []
    for name, bucket in ordered:
        body.append(
            f"<tr><td>{h(name)}</td><td class=\"num\">{bucket['bills']}</td><td class=\"num\">{money(bucket['inc'])}</td><td class=\"num\">{money(bucket['due'])}</td></tr>"
        )
    total_inc = sum(b["inc_gst"] or 0 for b in bills)
    shop_rows = sales_between(c, start, end)
    shop = sum(r["shop"] or 0 for r in shop_rows)
    shop_note = " Shop sales are the whole shop, not split by supplier." if filtering else ""
    table = '<p class="empty">No supplier bills in this range.</p>' if not body else (
        '<div class="tw"><table><thead><tr><th>Supplier</th><th>Bills</th><th>Inc GST</th><th>Still due</th></tr></thead><tbody>'
        + "".join(body) + f'<tr class="total"><td>Total</td><td class="num">{len(bills)}</td><td class="num">{money(total_inc)}</td><td></td></tr></tbody></table></div>'
    )
    return f"""<div class="ph"><h1>Purchases by supplier</h1>{through_line(through)}
<p>Bills dated in the selected range. This is not a cost-of-goods figure, and fuel direct debits are not in this table.</p></div>
{error}{purchase_filter_form(active, start, end, names, selected, filtering)}
<section class="card" id="purchase-summary"><h2>{money(total_inc)} inc GST</h2>
<p class="help">{len(bills)} bills. Shop sales in the same dates: {money(shop) if shop_rows else '—'}.{h(shop_note)}</p>{table}</section>
{parity().purchase_extra(c, active, start, end, bills, selected if filtering else None)}"""


def page_fuel(c, query):
    through = data_through(c)
    anchor = anchor_day(c)
    try:
        active, start, end = period_bounds(query.get("period"), query.get("from"), query.get("to"), anchor)
        error = ""
    except ValueError as ex:
        active, start, end = period_bounds("month", None, None, anchor)
        error = f'<p class="alert">{h(str(ex))}</p>'
    days = commission_between(c, start, end)
    body = []
    for row in days:
        body.append(
            "<tr><td>" + h(fmt_date(row["date"])) + "</td><td class=\"num\">" + money(row["amount"]) +
            "</td><td class=\"num\">" + litres(row["litres"]) + "</td><td class=\"num\">" + litres(row["ulp_l"]) +
            "</td><td class=\"num\">" + litres(row["diesel_l"]) + "</td><td class=\"num\">" + litres(row["pulp_l"]) + "</td></tr>"
        )
    table = '<p class="empty">No commission days in this range.</p>' if not body else (
        '<div class="tw"><table><thead><tr><th>Date</th><th>Commission ex GST</th><th>Litres</th><th>ULP</th><th>Diesel</th><th>P95</th></tr></thead><tbody>'
        + "".join(body) + "</tbody></table></div>"
    )
    amount = sum(r["amount"] or 0 for r in days)
    all_sales = rows(c.execute("SELECT * FROM portal_sales_days"))
    all_comm = rows(c.execute("SELECT * FROM portal_commission_days"))
    latest = max((r["date"] for r in all_comm), default=None) or data_through(c)
    amounts = online_amounts(c)
    proj = project_month(all_sales, all_comm, target_for(latest, meta_get(c, "sales"), meta_get(c, "fuel")), latest, amounts or {}) if latest and all_sales else None
    fuel_meta = meta_get(c, "fuel")
    basis = fuel_meta.get("basis") or {}
    basis_html = "".join(f"<li>{h(basis[k])}</li>" for k in ("commission", "margin", "targets") if basis.get(k))
    return f"""<div class="ph"><h1>Fuel commission</h1>{through_line(through)}
<p>Commission is ex GST. The portal does not read or set pump prices.</p></div>
{error}{projection_card(proj, (meta_get(c, 'sales').get('commission') or {}).get('october'), amounts is not None) if proj else ''}
{period_controls('/portal/fuel', active, start, end)}
<section class="card"><h2>{money(amount)} in this range</h2>{table}
{f'<ul class="help">{basis_html}</ul>' if basis_html else ''}</section>
{parity().fuel_sections(c, days)}"""


def page_dips(c, query):
    dates = [r["date"] for r in rows(c.execute("SELECT date FROM portal_dip_days ORDER BY date DESC"))]
    chosen = query.get("date") if query.get("date") in dates else (dates[0] if dates else None)
    through = dates[0] if dates else data_through(c)
    if not chosen:
        body = '<section class="card"><p class="empty">No pump readings or tank dips yet.</p></section>'
    else:
        row = one(c.execute("SELECT payload FROM portal_dip_days WHERE date=?", (chosen,)))
        payload = json.loads(row["payload"])
        options = "".join(
            f'<option value="{h(d)}"{" selected" if d == chosen else ""}>{h(fmt_date(d))}</option>' for d in dates
        )
        cards = []
        threshold = payload.get("lowThresholdPercent") or 25
        for tank in payload.get("tanks") or []:
            low = bool(tank.get("low")) or (tank.get("percentFull") is not None and float(tank["percentFull"]) <= float(threshold))
            flags = []
            if low:
                flags.append('<span class="pill bad">Low</span>')
            if tank.get("deliveryDetected"):
                flags.append('<span class="pill">Inferred delivery</span>')
            if tank.get("ullageAnomaly"):
                flags.append('<span class="pill bad">Ullage check</span>')
            pct = tank.get("percentFull")
            cards.append(f"""<article class="card tank{' low' if low else ''}"><div class="lbl">Tank {h(tank.get('tank'))} · {h(tank.get('grade'))}</div>
<b>{pct_points(pct)}</b><div class="bar"><i class="{bar_class(pct or 0)}"></i></div>
<p>{litres(tank.get('litres'))} of {litres(tank.get('capacity'))}</p>
<p class="help">Ullage {litres(tank.get('ullage'))} · overnight {litres(tank.get('overnightDrop'))} · variance {litres(tank.get('variance'))}</p>
<p>{''.join(flags)}</p></article>""")
        pumps = []
        for pump in payload.get("pumps") or []:
            reset = "yes" if pump.get("meterReset") else "no"
            pumps.append(
                f"<tr><td>{h(pump.get('pump'))}</td><td>{h(pump.get('hose'))}</td><td>{h(pump.get('grade'))}</td><td>{h(pump.get('tank'))}</td>"
                f"<td class=\"num\">{litres(pump.get('meterLitres'))}</td><td class=\"num\">{litres(pump.get('litresMoved'))}</td><td>{reset}</td></tr>"
            )
        dip_time = payload.get("dipTime") or ""
        shown_time = dip_time[11:16] if len(dip_time) >= 16 else ""
        basis = payload.get("deliveryBasis") or ""
        body = f"""<form class="period" method="get" action="/portal/dips"><label class="f">Dip date<select name="date">{options}</select></label><button class="btn" type="submit">Show</button></form>
<p class="help">Read time {h(shown_time)} Australia/Brisbane. {h(basis)} The portal does not read or set pump prices.</p>
<section class="grid g3">{''.join(cards)}</section>
<section class="card"><h2>Pump readings</h2><div class="tw"><table><thead><tr><th>Pump</th><th>Hose</th><th>Grade</th><th>Tank</th><th>Meter</th><th>Moved</th><th>Reset</th></tr></thead><tbody>{''.join(pumps)}</tbody></table></div></section>"""
    anchor = anchor_day(c)
    try:
        active, start, end = period_bounds(query.get("period"), query.get("from"), query.get("to"), anchor)
        range_error = ""
    except ValueError as ex:
        active, start, end = period_bounds("month", None, None, anchor)
        range_error = f'<p class="alert">{h(str(ex))}</p>'
    return f"""<div class="ph"><h1>Pump readings and tank dips</h1>{through_line(through)}
<p>Latest archived dip unless another date is chosen, plus the selected range. Dips are archived readings, not live till data. Inferred inflow is not a confirmed delivery. The portal does not read or set pump prices.</p></div>
{range_error}{period_controls('/portal/dips', active, start, end)}
{parity().dips_range(c, active, start, end)}{body}"""


def _paid_amount(row):
    if row.get("amount") is not None:
        return row["amount"]
    payment = row.get("owner_payment") or {}
    if payment.get("amount") is not None:
        return payment["amount"]
    return None


def _paid_total(paid):
    known = [_paid_amount(row) for row in paid if _paid_amount(row) is not None]
    if not paid:
        return "—", "no paid invoices"
    if len(known) == len(paid):
        return money(sum(known)), f"{len(paid)} paid invoice{'s' if len(paid) != 1 else ''}"
    if known:
        missing = len(paid) - len(known)
        return money(sum(known)), f"{missing} paid invoice{'s' if missing != 1 else ''} ha{'ve' if missing != 1 else 's'} no total in the file"
    return "—", "paid status is in the file; the amount paid is not"


def _payment_amount(raw, fallback):
    if raw is None or str(raw).strip() == "":
        amount = float(fallback or 0)
    else:
        text = str(raw).strip().replace(",", "").replace("$", "")
        amount = num(text, "amount")
    if amount < 0 or amount > 1_000_000_000:
        raise ValueError("Amount is out of range.")
    return float(_cents(amount))


def _payment_date(raw):
    paid_on = need_date(raw, "payment date")
    day = date.fromisoformat(paid_on)
    if day < date(2024, 1, 1) or day > today():
        raise ValueError("Payment date must be from 2024 through today, Australia/Brisbane.")
    return paid_on


def _payment_note(raw):
    note = " ".join(str(raw or "").split())
    if len(note) > 500:
        raise ValueError("Note must be 500 characters or less.")
    try:
        reject_sensitive(note, "note")
    except ValueError:
        raise ValueError("Note cannot include a card number.")
    return note or None


def _payment_method(raw):
    method = " ".join(str(raw or "").split())
    if method not in PAYMENT_METHODS:
        raise ValueError("Choose Card, Cash, Bank transfer, or Direct debit.")
    return method


def save_owner_payment(c, username, supplier, number, method, paid_on, amount_raw, note_raw):
    supplier = " ".join(str(supplier or "").split())
    number = str(number or "").strip()
    if not supplier or not number:
        raise ValueError("That invoice was not found.")
    invoice = one(c.execute("SELECT * FROM portal_invoices WHERE invoice_key=?", (invoice_key(supplier, number),)))
    if not invoice:
        raise ValueError("That invoice was not found.")
    status = (invoice["status"] or "").strip().lower()
    if status in PAID_STATUS and status not in XERO_PAID:
        raise ValueError("That invoice is not open.")
    existing = one(c.execute("SELECT * FROM portal_invoice_payments WHERE invoice_key=?", (invoice["invoice_key"],)))
    if payment_is_matched(existing, invoice):
        raise ValueError("That payment is already matched in Xero.")
    if status in XERO_PAID and not existing:
        raise ValueError("That invoice is already paid in Xero.")
    method = _payment_method(method)
    paid_on = _payment_date(paid_on)
    amount = _payment_amount(amount_raw, invoice["amount_due"] or 0)
    note = _payment_note(note_raw)
    now_s = stamp()
    c.execute(
        """INSERT INTO portal_invoice_payments(
             invoice_key,supplier,invoice_number,xero_invoice_id,method,paid_on,amount,note,
             created_at,updated_at,created_by,updated_by)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(invoice_key) DO UPDATE SET
             supplier=excluded.supplier, invoice_number=excluded.invoice_number,
             xero_invoice_id=COALESCE(excluded.xero_invoice_id, portal_invoice_payments.xero_invoice_id),
             method=excluded.method, paid_on=excluded.paid_on, amount=excluded.amount, note=excluded.note,
             updated_at=excluded.updated_at, updated_by=excluded.updated_by""",
        (invoice["invoice_key"], invoice["supplier"], invoice["invoice_number"], invoice.get("xero_invoice_id"),
         method, paid_on, amount, note, now_s, now_s, username, username),
    )
    return invoice


def undo_owner_payment(c, supplier, number):
    supplier = " ".join(str(supplier or "").split())
    number = str(number or "").strip()
    invoice = one(c.execute("SELECT * FROM portal_invoices WHERE invoice_key=?", (invoice_key(supplier, number),)))
    if not invoice:
        raise ValueError("That invoice was not found.")
    existing = one(c.execute("SELECT * FROM portal_invoice_payments WHERE invoice_key=?", (invoice["invoice_key"],)))
    if not existing:
        raise ValueError("There is no payment record on that invoice.")
    if payment_is_matched(existing, invoice):
        raise ValueError("That payment is already matched in Xero.")
    c.execute("DELETE FROM portal_invoice_payments WHERE invoice_key=?", (invoice["invoice_key"],))
    return invoice


def awaiting_owner_payments(c):
    """Owner records still waiting for a Xero match. Void, draft, and absent bills are left out."""
    invoices = {row["invoice_key"]: row for row in rows(c.execute("SELECT * FROM portal_invoices"))}
    out = []
    for payment in rows(c.execute("SELECT * FROM portal_invoice_payments ORDER BY paid_on, supplier, invoice_number")):
        invoice = invoices.get(payment["invoice_key"])
        if payment_is_matched(payment, invoice or {"status": ""}):
            continue
        if invoice and (invoice["status"] or "").strip().lower() in PAID_STATUS:
            continue
        xero_id = payment.get("xero_invoice_id") or (invoice or {}).get("xero_invoice_id")
        out.append({
            "invoiceId": xero_id,
            "invoiceNumber": payment["invoice_number"],
            "supplier": payment["supplier"],
            "amount": float(_cents(payment["amount"])),
            "method": payment["method"],
            "date": payment["paid_on"],
            "note": payment["note"] or "",
        })
    return out


def _match_reference(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("xeroPaymentReference is required")
    text = " ".join(value.split())
    if len(text) > 120 or has_card_number(text):
        raise ValueError("xeroPaymentReference must be a short reference")
    return text


def _body_text(body, *keys):
    for key in keys:
        value = body.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def find_owner_payment(c, body):
    invoice_id = _body_text(body, "invoiceId", "invoice_id")
    if invoice_id:
        cleaned = clean_invoice_id(invoice_id)
        row = one(c.execute("SELECT * FROM portal_invoice_payments WHERE xero_invoice_id=?", (cleaned,)))
        if row:
            return row
        invoice = one(c.execute("SELECT * FROM portal_invoices WHERE xero_invoice_id=?", (cleaned,)))
        if invoice:
            row = one(c.execute("SELECT * FROM portal_invoice_payments WHERE invoice_key=?", (invoice["invoice_key"],)))
            if row:
                return row
    supplier = _body_text(body, "supplier")
    number = _body_text(body, "invoiceNumber", "invoice_number")
    if supplier and number:
        return one(c.execute("SELECT * FROM portal_invoice_payments WHERE invoice_key=?", (invoice_key(supplier, number),)))
    raise ValueError("supplier and invoiceNumber, or invoiceId, is required")


def match_owner_payment(c, body):
    if not isinstance(body, dict):
        raise ValueError("body must be an object")
    reference = _match_reference(body.get("xeroPaymentReference") if "xeroPaymentReference" in body else body.get("xero_payment_reference"))
    payment = find_owner_payment(c, body)
    if not payment:
        return None
    if payment.get("matched_at"):
        return payment, True
    now_s = stamp()
    c.execute(
        "UPDATE portal_invoice_payments SET matched_at=?, xero_payment_ref=? WHERE invoice_key=?",
        (now_s, reference, payment["invoice_key"]),
    )
    payment["matched_at"] = now_s
    payment["xero_payment_ref"] = reference
    return payment, False


def payment_public(payment, already):
    return {
        "ok": True,
        "invoiceId": payment.get("xero_invoice_id"),
        "invoiceNumber": payment["invoice_number"],
        "supplier": payment["supplier"],
        "xeroPaymentReference": payment.get("xero_payment_ref") or "",
        "matchedAt": payment.get("matched_at"),
        "alreadyMatched": already,
    }


def _invoice_pairs(list_name, selected, filtering):
    pairs = [("list", list_name)]
    if filtering:
        pairs.extend(("supplier", name) for name in selected)
    return pairs


def _invoice_href(list_name, selected, filtering):
    return "/portal/invoices?" + urlencode(_invoice_pairs(list_name, selected, filtering))


def _method_options(current):
    options = []
    for label in PAYMENT_METHODS:
        selected = " selected" if label == current else ""
        options.append(f'<option value="{h(label)}"{selected}>{h(label)}</option>')
    return "".join(options)


def _payment_fields(user, row, payment, action, open_form):
    method = (payment or {}).get("method") or "Card"
    paid_on = (payment or {}).get("paid_on") or today().isoformat()
    amount = _amount_value(payment["amount"]) if payment else _amount_value(row.get("amount_due"))
    note = (payment or {}).get("note") or ""
    summary = "Edit" if payment else "Mark as paid"
    opened = " open" if open_form else ""
    return f"""<details class="pay-form"{opened}><summary class="btn">{summary}</summary>
<form method="post" action="{h(action)}">
<input type="hidden" name="csrf" value="{h(user['csrf'])}">
<input type="hidden" name="action" value="save_payment">
<input type="hidden" name="supplier" value="{h(row['supplier'])}">
<input type="hidden" name="invoice_number" value="{h(row['invoice_number'])}">
<label class="f">Method<select name="method" required>{_method_options(method)}</select></label>
<label class="f">Date<input type="date" name="paid_on" value="{h(paid_on)}" required></label>
<label class="f">Amount<input name="amount" inputmode="decimal" value="{h(amount)}" autocomplete="off"></label>
<label class="f">Note<input name="note" maxlength="500" value="{h(note)}"></label>
<button class="btn" type="submit">Save</button>
</form></details>"""


def _undo_form(user, row, action):
    return f"""<form method="post" action="{h(action)}" data-confirm="Remove your payment record? This does not change Xero.">
<input type="hidden" name="csrf" value="{h(user['csrf'])}">
<input type="hidden" name="action" value="undo_payment">
<input type="hidden" name="supplier" value="{h(row['supplier'])}">
<input type="hidden" name="invoice_number" value="{h(row['invoice_number'])}">
<button class="btn ghost" type="submit">Undo</button></form>"""


def _pay_link(row):
    if not row.get("pay_url"):
        return ""
    return f'<a class="btn sm" href="{h(row["pay_url"])}" target="_blank" rel="noopener noreferrer">Pay</a>'


def _owner_state(row):
    payment = row.get("owner_payment") or {}
    when = fmt_date(payment["paid_on"]) if payment.get("paid_on") else "—"
    meta = f'{h(payment.get("method") or "")} · {h(when)}'
    if payment.get("amount") is not None:
        meta += f' · {money(payment["amount"])}'
    if payment.get("note"):
        meta += f' · {h(payment["note"])}'
    if row.get("owner_matched"):
        ref = ""
        if payment.get("xero_payment_ref"):
            ref = f' Reference {h(payment["xero_payment_ref"])}.'
        return (
            f'<span class="pay-state matched">Paid – matched in Xero</span>'
            f'<div class="pay-meta">Your record: {meta}.{ref}</div>'
        )
    return f'<span class="pay-state await">Paid – awaiting Xero match</span><div class="pay-meta">{meta}</div>'


def _filter_rows(rows_in, selected, filtering):
    if not filtering:
        return rows_in
    wanted = {name.casefold() for name in selected}
    return [row for row in rows_in if row["supplier"].casefold() in wanted]


def page_invoices(c, query=None, user=None, error="", open_invoice=None):
    query = query or {}
    active = "paid" if query.get("list") == "paid" else "unpaid"
    today_d = today()
    all_unpaid, all_paid = invoice_lists(c, today_d)
    source = all_paid if active == "paid" else all_unpaid
    names = named_suppliers_from(source)
    selected, filtering = chosen_suppliers(query, names)
    unpaid = _filter_rows(all_unpaid, selected, filtering)
    paid = _filter_rows(all_paid, selected, filtering)
    overdue_n = sum(1 for r in unpaid if r["overdue"])
    overdue_sum = sum(r["amount_due"] or 0 for r in unpaid if r["overdue"])
    owing = sum(r["amount_due"] or 0 for r in unpaid)
    paid_figure, paid_note = _paid_total(paid)
    post_action = _invoice_href(active, selected, filtering)
    open_key = None
    if open_invoice and open_invoice[0] and open_invoice[1]:
        open_key = invoice_key(open_invoice[0], open_invoice[1])
    tabs = []
    for key, label in (("unpaid", "Unpaid"), ("paid", "Paid")):
        cls = " on" if key == active else ""
        tabs.append(f'<a class="chip{cls}" href="{h(_invoice_href(key, selected, filtering))}">{label}</a>')
    clear_href = "/portal/invoices?list=" + active
    supplier_form = f"""<form class="period" id="invoice-supplier-filter" method="get" action="/portal/invoices">
<input type="hidden" name="list" value="{h(active)}">
<div class="row supplier-row">
<div class="f"><span>Suppliers</span>{supplier_filter_box(names, selected, filtering)}</div>
<button class="btn" type="submit">Apply</button>
<a class="btn ghost" href="{h(clear_href)}">Clear</a>
</div></form>"""
    if active == "paid":
        body, cards = [], []
        for row in paid:
            paid_on = fmt_date(row["paid_date"]) if row.get("paid_date") else "—"
            if row.get("owner_payment") and not row.get("paid_date"):
                paid_on = fmt_date(row["owner_payment"]["paid_on"])
            shown_amount = _paid_amount(row)
            total = money(shown_amount) if shown_amount is not None else "—"
            if row.get("owner_payment"):
                state = _owner_state(row)
                actions = ""
                if row.get("owner_awaiting") and user:
                    opened = open_key == row["invoice_key"]
                    actions = _payment_fields(user, row, row["owner_payment"], post_action, opened) + _undo_form(user, row, post_action)
            else:
                state = '<span class="pill ok">Paid</span>'
                actions = ""
            body.append(
                f"<tr class=\"ok\"><td>{h(row['supplier'])}</td><td>{h(row['invoice_number'])}</td>"
                f"<td>{h(fmt_date(row['invoice_date'])) if row['invoice_date'] else '—'}</td>"
                f"<td>{h(fmt_date(row['due_date'])) if row['due_date'] else '—'}</td>"
                f"<td>{h(paid_on)}</td><td class=\"num\">{total}</td><td>{state}</td>"
                f"<td class=\"actions\">{actions}</td></tr>"
            )
            cards.append(
                f'<article class="invoice-card"><b>{h(row["supplier"])}</b><div>{h(row["invoice_number"])}</div>'
                f'<p class="mini">{h(paid_on)} · {total}</p><div>{state}</div><div class="pay-actions">{actions}</div></article>'
            )
        table = '<p class="empty">No paid invoices.</p>' if not body else (
            '<div class="invoice-cards">' + "".join(cards) + "</div>"
            '<div class="tw invoice-table"><table><thead><tr><th>Supplier</th><th>Invoice</th><th>Invoice date</th><th>Due</th>'
            '<th>Paid</th><th>Invoice total</th><th>Status</th><th></th></tr></thead><tbody>'
            + "".join(body) + "</tbody></table></div>"
        )
        listing = f'<section class="card" id="paid-list"><h2>Paid invoices</h2>{table}</section>'
    else:
        body, cards = [], []
        for row in unpaid:
            cls = ' class="overdue"' if row["overdue"] else ""
            card_cls = " overdue" if row["overdue"] else ""
            pay = _pay_link(row)
            due = fmt_date(row["due_date"]) if row["due_date"] else "No due date"
            flag = '<span class="pill bad">Overdue</span>' if row["overdue"] else '<span class="pill">Unpaid</span>'
            form = ""
            if user:
                opened = open_key == row["invoice_key"]
                form = _payment_fields(user, row, None, post_action, opened)
            actions = f'<div class="pay-actions">{form}{pay}</div>'
            body.append(
                f"<tr{cls}><td>{h(row['supplier'])}</td><td>{h(row['invoice_number'])}</td><td>{h(fmt_date(row['invoice_date'])) if row['invoice_date'] else '—'}</td>"
                f"<td>{h(due)}</td><td class=\"num\">{money(row['amount_due'])}</td><td>{flag}</td><td class=\"actions\">{actions}</td></tr>"
            )
            cards.append(
                f'<article class="invoice-card{card_cls}"><b>{h(row["supplier"])}</b><div>{h(row["invoice_number"])}</div>'
                f'<p>{h(due)} · {money(row["amount_due"])}</p><div>{flag}</div>{actions}</article>'
            )
        table = '<p class="empty">No unpaid invoices.</p>' if not body else (
            '<div class="invoice-cards">' + "".join(cards) + "</div>"
            '<div class="tw invoice-table"><table><thead><tr><th>Supplier</th><th>Invoice</th><th>Date</th><th>Due</th><th>Amount due</th><th>Status</th><th></th></tr></thead><tbody>'
            + "".join(body) + "</tbody></table></div>"
        )
        listing = f'<section class="card" id="invoice-list"><h2>Unpaid invoices</h2>{table}</section>'
    as_of = meta_get(c, "invoices").get("asOf")
    alert = f'<p class="alert">{h(error)}</p>' if error else ""
    ok_text = {"saved": "Saved. This records the payment here only. Nothing was paid and nothing was sent to Xero.",
               "removed": "Your payment record was removed."}.get(query.get("ok") or "", "")
    ok_html = f'<p class="oknote">{h(ok_text)}</p>' if ok_text else ""
    return f"""<div class="ph"><h1>Invoices</h1>{through_line(data_through(c))}
<p>Supplier bills from the nightly invoice list. Unpaid is shown first. Pay opens that supplier's own page in a new tab, and only on an unpaid bill. Mark as paid stores your record on this portal. It does not pay the supplier, and it does not send anything to Xero. Card and bank numbers are not stored.</p></div>
{alert}{ok_html}
{supplier_form}
<section class="grid g4" id="invoice-totals">
<div class="card stat" id="unpaid-total"><div class="lbl">Total owing</div><b>{money(owing)}</b><span>{len(unpaid)} unpaid invoice{'s' if len(unpaid) != 1 else ''}</span></div>
<div class="card stat" id="overdue-total"><div class="lbl">Overdue</div><b>{money(overdue_sum)}</b><span>{overdue_n} past the due date</span></div>
<div class="card stat" id="paid-total"><div class="lbl">Total paid</div><b>{paid_figure}</b><span>{h(paid_note)}</span></div>
<div class="card stat"><div class="lbl">Invoice list through</div><b class="date">{h(fmt_date(as_of)) if as_of else '—'}</b><span>from the nightly ingest</span></div>
</section>
<div class="chips" id="invoice-tabs">{''.join(tabs)}</div>
{listing}"""


ERR_TEXT = {
    "name": "Enter a staff name.",
    "date": "Choose a valid date.",
    "time": "Check the start and finish times.",
    "break": "Check the unpaid break.",
    "hours": "That shift could not be saved.",
    "staff": "Choose an active staff member.",
    "missing": "That entry was not found.",
    "saved": "Saved.",
    "deleted": "Deleted.",
}


def _amount_value(value):
    if value is None or value == "":
        return ""
    return format(_cents(value), "f")


def _stat_pair(element_id, label, primary, primary_note, secondary=None, secondary_note=""):
    second = ""
    if secondary is not None or secondary_note:
        second = f'<b class="substat" id="{element_id}-incl">{money(secondary)}</b><span>{h(secondary_note)}</span>'
    return (
        f'<div class="card stat" id="{element_id}"><div class="lbl">{h(label)}</div>'
        f'<b id="{element_id}-excl">{money(primary)}</b><span>{h(primary_note)}</span>{second}</div>'
    )


def wage_summary_html(view, bill):
    note = missing_rate_note(view)
    note_html = f'<p class="callout" id="missing-rates">{h(note)}</p>' if note else ""
    super_label = employee_super_label(view)
    employees = _stat_pair(
        "employee-wages", "Employees", bill["employee_excl"], "excl. super",
        bill["employee_incl"], super_label,
    )
    contractors = (
        f'<div class="card stat" id="contractor-wages"><div class="lbl">Contractors</div>'
        f'<b id="contractor-wages-cost">{money(bill["contract"])}</b>'
        f'<span>no super · <span class="pill">Contract</span></span></div>'
    )
    combined = _stat_pair(
        "combined-wages", "Combined", bill["combined_excl"], "excl. super",
        bill["combined_incl"], "incl. super on employees only",
    )
    return f"""<section class="card" id="wage-bill"><h2>Wage bill</h2>
<p class="help">Employee costs use the pay-rates upload and the rate for the day the shift started. Saturday, Sunday, and Queensland public holidays use their own rates. A public holiday on a weekend uses the public holiday rate. Christmas Eve uses that rate when the shift starts at or after 6pm. Super is each employee's super rate. Contractors are typed on this page, are not in the pay-rates list, and have no super.</p>
<div class="grid g3">{employees}{contractors}{combined}</div>
{note_html}</section>"""


def week_head_cells(days):
    head_cells = []
    for day in days:
        label = day["weekday"] + " " + str(date.fromisoformat(day["date"]).day)
        extra = ""
        if day["publicHoliday"]:
            extra = f'<small>{h(day["publicHoliday"])}</small>'
        elif day["weekend"]:
            extra = "<small>Weekend</small>"
        head_cells.append(f'<th class="{day_cell_class(day)}">{label}{extra}</th>')
    return "".join(head_cells)


def day_cell_class(day):
    cls = "weekend" if day["weekend"] else ""
    if day["publicHoliday"]:
        cls = (cls + " holiday").strip()
    return cls


def period_label(monday):
    end = monday + timedelta(days=6)
    return f"Week of {fmt_date(monday.isoformat())} to {fmt_date(end.isoformat())}"


def long_day(iso):
    d = date.fromisoformat(iso)
    return f"{d.strftime('%a')} {d.day} {d.strftime('%b')} {d.year}"


def person_toggle(dom_id, name):
    return (
        f'<input type="checkbox" class="person-toggle" id="open-{dom_id}" aria-controls="{dom_id}-break">'
        f'<label class="person-name" for="open-{dom_id}">{h(name)}'
        f'<span class="sr">, daily hours, rate and cost</span></label>'
    )


def person_panel(dom_id, colspan, body):
    return f'<tr class="person-panel" id="{dom_id}-break"><td colspan="{colspan}">{body}</td></tr>'


def person_sheet(name, body):
    return (
        f'<details class="person-sheet"><summary class="person-name">{h(name)}'
        f'<span class="sr">, daily hours, rate and cost</span></summary>{body}</details>'
    )


def breakdown_html(period, lines, total_hours, total_cost):
    intro = f'<p class="mini">{h(period)}</p>'
    if not lines:
        return f'<div class="day-break">{intro}<p class="empty">No hours this week.</p></div>'
    items = ['<li class="break-head"><span>Date</span><span>Hours</span><span>Rate</span><span>Cost</span></li>']
    for line in lines:
        items.append(
            f'<li><span>{h(line["date"])}</span><span>{h(line["hours"])}</span>'
            f'<span>{h(line["rate"])}</span><span>{h(line["cost"])}</span></li>'
        )
    shown = "—" if total_hours is None else hours_text(total_hours) + " h"
    items.append(
        f'<li class="total"><span>Total</span><span>{h(shown)}</span><span></span><span>{h(total_cost)}</span></li>'
    )
    return f'<div class="day-break">{intro}<ul class="break-days">{"".join(items)}</ul></div>'


def _pay_cost_text(excl, incl):
    return f"{money(excl)} excl · {money(incl)} incl super"


def pay_breakdown(person, person_view, period):
    grouped = {}
    for shift in person.get("shifts") or []:
        grouped.setdefault(shift["date"], []).append(shift)
    lines = []
    total_hours = Decimal("0")
    total_excl = Decimal("0")
    total_incl = Decimal("0")
    saw_cost = False
    for iso in sorted(grouped):
        shifts = sorted(grouped[iso], key=lambda s: ((s.get("start") or ""), s["id"]))
        if person_view["missing"]:
            hours = sum((Decimal(str(s["hours"])) for s in shifts), Decimal("0"))
            total_hours += hours
            lines.append({
                "date": long_day(iso), "hours": hours_text(hours) + " h",
                "rate": "No rate yet", "cost": "—",
            })
            continue
        costs = [person_view["shifts"].get(s["id"]) for s in shifts]
        rated = [c for c in costs if c is not None]
        keys = {(c["kind"], str(c["rate"])) for c in rated}
        if len(rated) == len(shifts) and len(keys) == 1:
            hours = sum((Decimal(str(s["hours"])) for s in shifts), Decimal("0"))
            excl = sum((c["excl"] for c in rated), Decimal("0"))
            incl = sum((c["incl"] for c in rated), Decimal("0"))
            total_hours += hours
            total_excl += excl
            total_incl += incl
            saw_cost = True
            cost = rated[0]
            lines.append({
                "date": long_day(iso),
                "hours": hours_text(hours) + " h",
                "rate": f"{cost['kindLabel']} {money(cost['rate'])}/h",
                "cost": _pay_cost_text(excl, incl),
            })
            continue
        for shift, cost in zip(shifts, costs):
            hours = Decimal(str(shift["hours"]))
            total_hours += hours
            if not cost:
                lines.append({
                    "date": long_day(iso), "hours": hours_text(hours) + " h",
                    "rate": "No rate yet", "cost": "—",
                })
                continue
            total_excl += cost["excl"]
            total_incl += cost["incl"]
            saw_cost = True
            lines.append({
                "date": long_day(iso),
                "hours": hours_text(hours) + " h",
                "rate": f"{cost['kindLabel']} {money(cost['rate'])}/h",
                "cost": _pay_cost_text(cost["excl"], cost["incl"]),
            })
    total_cost = _pay_cost_text(total_excl, total_incl) if saw_cost else "—"
    return breakdown_html(period, lines, total_hours if lines else None, total_cost)


def contract_breakdown(entries, period):
    lines = []
    total_hours = Decimal("0")
    saw_hours = False
    total_cost = Decimal("0")
    for row in entries:
        if row["flat_amount"] is not None:
            rate = f"Flat {money(row['flat_amount'])}"
        elif row["hourly_rate"] is not None:
            rate = f"{money(row['hourly_rate'])}/h"
        else:
            rate = "—"
        if row["hours"] is not None:
            hours = Decimal(str(row["hours"]))
            total_hours += hours
            saw_hours = True
            hours_label = hours_text(hours) + " h"
        else:
            hours_label = "—"
        cost = _cents(row["cost"])
        total_cost += cost
        lines.append({
            "date": long_day(row["work_date"]),
            "hours": hours_label,
            "rate": rate,
            "cost": f"{money(cost)} · no super",
        })
    total_h = total_hours if saw_hours else None
    total_c = f"{money(total_cost)} · no super" if lines else "—"
    return breakdown_html(period, lines, total_h, total_c)


def _hours_sum(entries):
    vals = [Decimal(str(e["hours"])) for e in entries if e["hours"] is not None]
    if not vals:
        return None
    return sum(vals, Decimal("0"))


def _cost_sum(entries):
    if not entries:
        return None
    return sum((_cents(e["cost"]) for e in entries), Decimal("0"))


def _simple_contract_day(entries):
    """A day the grid can edit: nothing yet, or one hourly entry."""
    return not entries or (len(entries) == 1 and entries[0]["flat_amount"] is None)


def contract_people(directory, contracts, day_isos):
    people = {}
    for person in directory:
        people[person["id"]] = {
            "id": person["id"], "name": person["name"],
            "saved_rate": person["hourly_rate"], "entries": [],
        }
    for row in contracts:
        bucket = people.get(row["contractor_id"])
        if bucket is None:
            bucket = {
                "id": row["contractor_id"], "name": row["name"],
                "saved_rate": row["saved_rate"], "entries": [],
            }
            people[row["contractor_id"]] = bucket
        bucket["entries"].append(row)
    ordered = sorted(people.values(), key=lambda p: (p["name"].casefold(), p["id"]))
    for person in ordered:
        by_day = {iso: [] for iso in day_isos}
        for row in person["entries"]:
            by_day.setdefault(row["work_date"], []).append(row)
        person["by_day"] = by_day
    return ordered


def _day_hours_input(iso, value, label, form_id):
    form = f' form="{h(form_id)}"' if form_id else ""
    return (
        f'<input class="day-input" type="text"{form} name="hours_{iso}" value="{h(value)}" '
        f'inputmode="decimal" autocomplete="off" aria-label="{h(label)}">'
    )


def _contract_day_mode(entries):
    if not entries:
        return "empty"
    if len(entries) == 1 and entries[0]["flat_amount"] is None:
        return "hourly"
    if len(entries) == 1 and entries[0]["flat_amount"] is not None:
        return "flat"
    return "mixed"


def _contract_day_body(person, day, entries, form_id):
    mode = _contract_day_mode(entries)
    label = f"{person['name']} {long_day(day['date'])} hours"
    if mode in ("empty", "hourly"):
        value = "" if mode == "empty" else _amount_value(entries[0]["hours"])
        body = _day_hours_input(day["date"], value, label, form_id)
        if mode == "hourly":
            body += f'<small class="wage">{money(_cost_sum(entries))} · no super</small>'
        return body
    if mode == "flat":
        hours = _hours_sum(entries)
        shown = "—" if hours is None else h(hours_text(hours) + " h")
        return f'{shown}<small class="wage">Flat {money(entries[0]["flat_amount"])} · no super</small>'
    hours = _hours_sum(entries)
    text = "—" if hours is None else hours_text(hours) + " h"
    if len(entries) > 1:
        text += f" ({len(entries)})"
    return (
        f'{h(text)}<small class="wage">{money(_cost_sum(entries))} · no super</small>'
        f'<small class="wage">Edit each entry below</small>'
    )


def _contract_day_cell(person, day, entries, form_id):
    mode = _contract_day_mode(entries)
    body = _contract_day_body(person, day, entries, form_id)
    return (
        f'<td class="num day-cell {day_cell_class(day)}" id="contract-cell-{person["id"]}-{day["date"]}" '
        f'data-entry="{mode}">{body}</td>'
    )


def _week_total_html(entries):
    hours = _hours_sum(entries)
    cost = _cost_sum(entries)
    if hours is None and cost is None:
        return "—"
    shown = "—" if hours is None else hours_text(hours) + " h"
    note = f'<small class="wage">{money(cost)} · no super</small>' if cost is not None else ""
    return f"{h(shown)}{note}"


def _days_form(user, monday, form_id, contractor_id):
    cid = ""
    if contractor_id is not None:
        cid = f'<input type="hidden" name="contractor_id" value="{int(contractor_id)}">'
    return (
        f'<form method="post" action="/portal/hours" id="{form_id}" class="day-form">'
        f'<input type="hidden" name="csrf" value="{h(user["csrf"])}">'
        f'<input type="hidden" name="week" value="{h(monday.isoformat())}">'
        f'<input type="hidden" name="action" value="save_contract_days">{cid}</form>'
    )


def _day_flag(day):
    if day["publicHoliday"]:
        return " · " + day["publicHoliday"]
    if day["weekend"]:
        return " · Weekend"
    return ""


def contract_grid_html(user, monday, days, directory, contracts):
    day_isos = [day["date"] for day in days]
    people = contract_people(directory, contracts, day_isos)
    period = period_label(monday)
    colspan = len(days) + 2
    rows_html = []
    cards = []
    grand_hours = []
    for person in people:
        form_id = f'contract-days-{person["id"]}'
        dom = f'contract-{person["id"]}'
        rate_value = "" if person["saved_rate"] is None else _amount_value(person["saved_rate"])
        breakdown = contract_breakdown(person["entries"], period)
        cells = [
            _contract_day_cell(person, day, person["by_day"].get(day["date"], []), form_id)
            for day in days
        ]
        week = _week_total_html(person["entries"])
        part = _hours_sum(person["entries"])
        if part is not None:
            grand_hours.append(part)
        rows_html.append(
            f'<tr id="contract-person-{person["id"]}">'
            f'<th scope="row" class="person-cell">{person_toggle(dom, person["name"])}'
            f'{_days_form(user, monday, form_id, person["id"])}'
            f'<label class="f">Hourly rate<input class="day-rate" form="{form_id}" name="hourly_rate" '
            f'inputmode="decimal" value="{h(rate_value)}" aria-label="{h(person["name"])} hourly rate" '
            f'placeholder="Saved rate"></label>'
            f'<button class="btn sm" type="submit" form="{form_id}">Save days</button></th>'
            f'{"".join(cells)}<td class="num total">{week}</td></tr>'
        )
        rows_html.append(person_panel(dom, colspan, breakdown))
        card_lines = []
        for day in days:
            entries = person["by_day"].get(day["date"], [])
            body = _contract_day_body(person, day, entries, None)
            when = f'{day["weekday"]} {date.fromisoformat(day["date"]).day}{_day_flag(day)}'
            card_lines.append(f'<li><span>{h(when)}</span><b>{body}</b></li>')
        cards.append(
            f'<article class="card person" id="contract-card-{person["id"]}">'
            f'{person_sheet(person["name"], breakdown)}'
            f'<form method="post" action="/portal/hours" id="contract-card-form-{person["id"]}" class="day-form">'
            f'<input type="hidden" name="csrf" value="{h(user["csrf"])}">'
            f'<input type="hidden" name="week" value="{h(monday.isoformat())}">'
            f'<input type="hidden" name="action" value="save_contract_days">'
            f'<input type="hidden" name="contractor_id" value="{person["id"]}">'
            f'<label class="f">Hourly rate<input class="day-rate" name="hourly_rate" inputmode="decimal" '
            f'value="{h(rate_value)}" aria-label="{h(person["name"])} hourly rate"></label>'
            f'<ul class="days">{"".join(card_lines)}</ul>'
            f'<p>Week <b>{week}</b></p>'
            f'<button class="btn" type="submit">Save days</button></form></article>'
        )
    add_id = "contract-days-new"
    add_cells = []
    add_lines = []
    for day in days:
        label = f'New contractor {long_day(day["date"])} hours'
        add_cells.append(
            f'<td class="num day-cell {day_cell_class(day)}">{_day_hours_input(day["date"], "", label, add_id)}</td>'
        )
        when = f'{day["weekday"]} {date.fromisoformat(day["date"]).day}{_day_flag(day)}'
        add_lines.append(f'<li><span>{h(when)}</span><b>{_day_hours_input(day["date"], "", label, None)}</b></li>')
    rows_html.append(
        f'<tr id="contract-days-add"><th scope="row" class="person-cell">{_days_form(user, monday, add_id, None)}'
        f'<label class="f">Contractor<input form="{add_id}" name="name" maxlength="60" required '
        f'placeholder="New contractor" autocomplete="off" list="contract-names"></label>'
        f'<label class="f">Hourly rate<input class="day-rate" form="{add_id}" name="hourly_rate" '
        f'inputmode="decimal" placeholder="e.g. 40" aria-label="New contractor hourly rate"></label>'
        f'<button class="btn sm" type="submit" form="{add_id}">Save days</button></th>'
        f'{"".join(add_cells)}<td class="num total">—</td></tr>'
    )
    cards.append(
        f'<article class="card person" id="contract-days-add-card"><h3>Add contract hours</h3>'
        f'<form method="post" action="/portal/hours" id="contract-days-new-card" class="day-form">'
        f'<input type="hidden" name="csrf" value="{h(user["csrf"])}">'
        f'<input type="hidden" name="week" value="{h(monday.isoformat())}">'
        f'<input type="hidden" name="action" value="save_contract_days">'
        f'<label class="f">Contractor<input name="name" maxlength="60" required placeholder="New contractor" '
        f'autocomplete="off" list="contract-names"></label>'
        f'<label class="f">Hourly rate<input class="day-rate" name="hourly_rate" inputmode="decimal" '
        f'placeholder="e.g. 40" aria-label="New contractor hourly rate"></label>'
        f'<ul class="days">{"".join(add_lines)}</ul>'
        f'<button class="btn" type="submit">Save days</button></form></article>'
    )
    grand = hours_text(sum(grand_hours, Decimal("0"))) + " h" if grand_hours else "—"
    table = (
        f'<div class="week-table tw" id="contract-week"><table><thead><tr><th>Contractor</th>'
        f'{week_head_cells(days)}<th>Week</th></tr></thead><tbody>{"".join(rows_html)}</tbody></table></div>'
        f'<div class="week-cards" id="contract-cards">{"".join(cards)}</div>'
    )
    return (
        f'<div class="lbl">Contract hours this week</div><b class="stat" id="contract-grand-total">{h(grand)}</b>'
        f'<p class="help">Not in the pay run total above. Cost is in the wage bill, with no super.</p>{table}'
    )


def _contract_basis(row):
    if row["flat_amount"] is not None:
        recorded = ""
        if row["hours"] is not None:
            recorded = f"{hours_text(row['hours'])} h recorded · "
        return recorded + f"Flat {money(row['flat_amount'])}"
    return f"{hours_text(row['hours'])} h × {money(row['hourly_rate'])}/h"


def contract_section_html(c, user, monday, days, contracts):
    directory = contractor_directory(c)
    rates = {}
    saved = []
    for person in directory:
        if person["hourly_rate"] is None:
            saved.append(h(person["name"]))
            continue
        shown = _amount_value(person["hourly_rate"])
        rates[rate_key(person["name"])] = shown
        saved.append(f"{h(person['name'])} {money(person['hourly_rate'])}/h")
    saved_line = ""
    if saved:
        saved_line = f'<p class="help" id="saved-contract-rates">Saved rates: {"; ".join(saved)}. Leave the hourly rate blank to reuse the saved rate. Type a new rate to replace it.</p>'
    rate_attr = h(json.dumps(rates, ensure_ascii=False))
    names = "".join(f'<option value="{h(person["name"])}"></option>' for person in directory)
    rows_html = []
    for row in contracts:
        hourly_selected = "" if row["flat_amount"] is not None else " selected"
        flat_selected = " selected" if row["flat_amount"] is not None else ""
        rows_html.append(f"""<details class="shift" id="contract-{row['id']}"><summary><span class="pill">Contract</span> {h(row['name'])} · {h(fmt_date(row['work_date']))} · {h(_contract_basis(row))} · {money(row['cost'])} · no super</summary>
<form method="post" action="/portal/hours">
<input type="hidden" name="csrf" value="{h(user['csrf'])}"><input type="hidden" name="week" value="{h(monday.isoformat())}">
<input type="hidden" name="action" value="save_contract"><input type="hidden" name="contract_id" value="{row['id']}">
<div class="row">
<label class="f">Contractor<input name="name" maxlength="60" required value="{h(row['name'])}"></label>
<label class="f">Date<input type="date" name="work_date" value="{h(row['work_date'])}" required></label>
<label class="f">Cost<select name="pay_style"><option value="hourly"{hourly_selected}>Hours × hourly rate</option><option value="flat"{flat_selected}>Flat amount</option></select></label>
<label class="f">Hours<input name="hours" inputmode="decimal" value="{h(_amount_value(row['hours']))}"></label>
<label class="f">Hourly rate<input name="hourly_rate" inputmode="decimal" value="{h(_amount_value(row['hourly_rate'] if row['hourly_rate'] is not None else row['saved_rate']))}"></label>
<label class="f">Flat amount<input name="flat_amount" inputmode="decimal" value="{h(_amount_value(row['flat_amount']))}"></label>
</div>
<label class="f">Notes<input name="notes" maxlength="500" value="{h(row['notes'] or '')}"></label>
<button class="btn" type="submit">Save changes</button>
</form>
<form method="post" action="/portal/hours" data-confirm="Delete this contract entry?">
<input type="hidden" name="csrf" value="{h(user['csrf'])}"><input type="hidden" name="week" value="{h(monday.isoformat())}">
<input type="hidden" name="action" value="delete_contract"><input type="hidden" name="contract_id" value="{row['id']}">
<button class="btn danger" type="submit">Delete</button></form></details>""")
    listing = "".join(rows_html) or '<p class="empty">No contract entries this week.</p>'
    grid = contract_grid_html(user, monday, days, directory, contracts)
    return f"""<section class="card" id="contract-hours"><h2>Contract hours</h2>
<p class="help">Daily hours for contractors paid outside the pay run. No super. The week grid matches the pay run. Tap a name for the date, hours, rate, and cost. Type hours for a day and save. Leave a day blank and save to remove that day's hours. The hourly rate is remembered for the person. A flat amount, or more than one entry on the same day, stays as entered and is edited in the list below. These hours are not in the hours CSV or the staff-hours feed, so they are not part of the pay run. They are included in the wage bill on this page and in wages vs sales.</p>
{saved_line}
{grid}
<h3>Entries this week</h3>
{listing}
<h3>Add one date or a flat amount</h3>
<form method="post" action="/portal/hours" id="contract-form" data-rates="{rate_attr}">
<input type="hidden" name="csrf" value="{h(user['csrf'])}"><input type="hidden" name="week" value="{h(monday.isoformat())}"><input type="hidden" name="action" value="save_contract">
<div class="row">
<label class="f">Contractor<input name="name" id="contract-name" maxlength="60" required placeholder="e.g. Pat" list="contract-names" autocomplete="off"></label>
<datalist id="contract-names">{names}</datalist>
<label class="f">Date<input type="date" name="work_date" value="{h(monday.isoformat())}" required></label>
<label class="f">Cost<select name="pay_style" id="contract-style"><option value="hourly">Hours × hourly rate</option><option value="flat">Flat amount</option></select></label>
<label class="f">Hours<input name="hours" id="contract-hours-input" inputmode="decimal" placeholder="e.g. 4"></label>
<label class="f">Hourly rate<input name="hourly_rate" id="contract-rate" inputmode="decimal" placeholder="Saved rate if blank"></label>
<label class="f">Flat amount<input name="flat_amount" id="contract-flat" inputmode="decimal" placeholder="Used instead of hours × rate"></label>
</div>
<label class="f">Notes<input name="notes" maxlength="500"></label>
<button class="btn" type="submit">Add contract hours</button>
</form></section>"""


def _work_date_in_range(raw):
    work_date = need_date(raw, "date")
    d = date.fromisoformat(work_date)
    if d < date(2024, 1, 1) or d > today() + timedelta(days=400):
        raise ValueError("Check the date.")
    return work_date


def _money_input(raw, label, required, lo, hi):
    text = (raw or "").strip().replace("$", "").replace(",", "")
    if text == "":
        if required:
            raise ValueError(f"Enter the {label}.")
        return None
    amount = _cents(num(text, label))
    if amount < lo or amount > hi:
        raise ValueError(f"The {label} must be between {lo} and {hi}.")
    return amount


def _hours_input(raw, required):
    text = (raw or "").strip()
    if text == "":
        if required:
            raise ValueError("Enter the hours.")
        return None
    amount = _cents(num(text, "hours"))
    if amount <= 0 or amount > 24:
        raise ValueError("Hours must be more than 0 and at most 24 for one date.")
    return amount


def _upsert_contractor(c, name, saved_rate, existing, current):
    now_s = stamp()
    rate_f = float(saved_rate) if saved_rate is not None else None
    if existing and (not current or existing["id"] != current["contractor_id"]):
        if rate_f is not None:
            c.execute(
                "UPDATE portal_contractors SET name=?, hourly_rate=?, active=1, updated_at=? WHERE id=?",
                (name, rate_f, now_s, existing["id"]),
            )
        else:
            c.execute(
                "UPDATE portal_contractors SET name=?, active=1, updated_at=? WHERE id=?",
                (name, now_s, existing["id"]),
            )
        return one(c.execute("SELECT * FROM portal_contractors WHERE id=?", (existing["id"],)))
    if current:
        if rate_f is not None:
            c.execute(
                "UPDATE portal_contractors SET name=?, hourly_rate=?, active=1, updated_at=? WHERE id=?",
                (name, rate_f, now_s, current["contractor_id"]),
            )
        else:
            c.execute(
                "UPDATE portal_contractors SET name=?, active=1, updated_at=? WHERE id=?",
                (name, now_s, current["contractor_id"]),
            )
        return one(c.execute("SELECT * FROM portal_contractors WHERE id=?", (current["contractor_id"],)))
    new_id = one(c.execute(
        "INSERT INTO portal_contractors(name,hourly_rate,active,created_at,updated_at) VALUES(?,?,1,?,?) RETURNING id",
        (name, rate_f, now_s, now_s),
    ))["id"]
    return one(c.execute("SELECT * FROM portal_contractors WHERE id=?", (new_id,)))


def _contract_snapshot(name, work_date, hours, rate, flat, cost):
    return {
        "name": name, "date": work_date, "kind": "contract",
        "hours": float(hours) if hours is not None else None,
        "rate": float(rate) if rate is not None else None,
        "flat": float(flat) if flat is not None else None,
        "cost": float(cost),
    }


def save_contract(c, actor, form):
    try:
        name = clean_name(form.get("name") if isinstance(form.get("name"), str) else "")
    except ValueError:
        raise ValueError("Enter a contractor name.")
    reject_sensitive(name, "name")
    work_date = _work_date_in_range(form.get("work_date"))
    style = (form.get("pay_style") or "hourly").strip()
    if style not in ("hourly", "flat"):
        raise ValueError("Choose hours × rate or a flat amount.")
    notes = (form.get("notes") or "").strip()[:500]
    reject_sensitive(notes, "notes")
    existing = contractor_by_name(c, name)
    typed_rate = _money_input(form.get("hourly_rate"), "hourly rate", False, Decimal("0.01"), Decimal("1000"))
    current = None
    if form.get("contract_id"):
        current = one(c.execute(
            """SELECT e.*, k.name FROM portal_contract_entries e
               JOIN portal_contractors k ON k.id=e.contractor_id WHERE e.id=?""",
            (form_id(form.get("contract_id")),),
        ))
        if not current:
            raise ValueError("That contract entry was not found.")
    if style == "hourly":
        hours = _hours_input(form.get("hours"), True)
        rate = typed_rate
        if rate is None and existing and existing["hourly_rate"] is not None:
            rate = _cents(existing["hourly_rate"])
        if rate is None and current and current["hourly_rate"] is not None and rate_key(current["name"]) == rate_key(name):
            rate = _cents(current["hourly_rate"])
        if rate is None:
            raise ValueError("Enter an hourly rate. It is saved for this contractor.")
        cost = _cents(hours * rate)
        flat = None
        saved_rate = rate
    else:
        hours = _hours_input(form.get("hours"), False)
        flat = _money_input(form.get("flat_amount"), "flat amount", True, Decimal("0.01"), Decimal("100000"))
        cost = flat
        rate = None
        saved_rate = typed_rate
        if saved_rate is None and existing and existing["hourly_rate"] is not None:
            saved_rate = _cents(existing["hourly_rate"])
    contractor = _upsert_contractor(c, name, saved_rate, existing, current)
    now_s = stamp()
    hours_f = float(hours) if hours is not None else None
    rate_f = float(rate) if rate is not None else None
    flat_f = float(flat) if flat is not None else None
    cost_f = float(cost)
    after = _contract_snapshot(contractor["name"], work_date, hours, rate, flat, cost)
    if current:
        before = _contract_snapshot(current["name"], current["work_date"], current["hours"], current["hourly_rate"], current["flat_amount"], current["cost"])
        c.execute(
            """UPDATE portal_contract_entries SET contractor_id=?, work_date=?, hours=?, hourly_rate=?, flat_amount=?, cost=?, notes=?, updated_at=?, updated_by=? WHERE id=?""",
            (contractor["id"], work_date, hours_f, rate_f, flat_f, cost_f, notes, now_s, actor, current["id"]),
        )
        audit(c, actor, "edit_contract", None, current["id"], before, after)
        return
    new_id = one(c.execute(
        """INSERT INTO portal_contract_entries(contractor_id,work_date,hours,hourly_rate,flat_amount,cost,notes,created_at,updated_at,created_by,updated_by)
           VALUES(?,?,?,?,?,?,?,?,?,?,?) RETURNING id""",
        (contractor["id"], work_date, hours_f, rate_f, flat_f, cost_f, notes, now_s, now_s, actor, actor),
    ))["id"]
    audit(c, actor, "add_contract", None, new_id, None, after)


def _delete_contract_row(c, actor, current, name):
    before = _contract_snapshot(name, current["work_date"], current["hours"], current["hourly_rate"], current["flat_amount"], current["cost"])
    c.execute("DELETE FROM portal_contract_entries WHERE id=?", (current["id"],))
    audit(c, actor, "delete_contract", None, current["id"], before, None)


def delete_contract(c, actor, form):
    current = one(c.execute(
        """SELECT e.*, k.name FROM portal_contract_entries e
           JOIN portal_contractors k ON k.id=e.contractor_id WHERE e.id=?""",
        (form_id(form.get("contract_id")),),
    ))
    if not current:
        raise ValueError("That contract entry was not found.")
    _delete_contract_row(c, actor, current, current["name"])


def _posted_week(raw):
    try:
        anchor = date.fromisoformat(need_date(raw, "week"))
    except ValueError:
        raise ValueError("Choose a valid week.")
    monday = week_monday(anchor)
    return monday, [(monday + timedelta(days=i)).isoformat() for i in range(7)]


def _save_hourly_day(c, actor, contractor, work_date, hours, rate, current):
    """Write one hourly contract day. Leaves notes in place. Does not touch a flat entry."""
    cost = _cents(hours * rate)
    notes = (current["notes"] if current else "") or ""
    now_s = stamp()
    after = _contract_snapshot(contractor["name"], work_date, hours, rate, None, cost)
    if current:
        unchanged = (
            current["flat_amount"] is None
            and current["work_date"] == work_date
            and current["hours"] is not None
            and _cents(current["hours"]) == hours
            and current["hourly_rate"] is not None
            and _cents(current["hourly_rate"]) == rate
            and _cents(current["cost"]) == cost
        )
        if unchanged:
            return
        before = _contract_snapshot(
            contractor["name"], current["work_date"], current["hours"],
            current["hourly_rate"], current["flat_amount"], current["cost"],
        )
        c.execute(
            """UPDATE portal_contract_entries
               SET contractor_id=?, work_date=?, hours=?, hourly_rate=?, flat_amount=NULL, cost=?, notes=?, updated_at=?, updated_by=?
               WHERE id=?""",
            (contractor["id"], work_date, float(hours), float(rate), float(cost), notes, now_s, actor, current["id"]),
        )
        audit(c, actor, "edit_contract", None, current["id"], before, after)
        return
    new_id = one(c.execute(
        """INSERT INTO portal_contract_entries(contractor_id,work_date,hours,hourly_rate,flat_amount,cost,notes,created_at,updated_at,created_by,updated_by)
           VALUES(?,?,?,?,NULL,?,?,?,?,?,?) RETURNING id""",
        (contractor["id"], work_date, float(hours), float(rate), float(cost), notes, now_s, now_s, actor, actor),
    ))["id"]
    audit(c, actor, "add_contract", None, new_id, None, after)


def save_contract_days(c, actor, form):
    """Save hourly contract hours for the days of one week.

    A missing day field is left alone, so a flat amount or several entries on
    one date are not replaced from the grid. A blank day field removes the
    single hourly entry for that date. Nightly ingest never calls this.
    """
    _monday, dates = _posted_week(form.get("week"))
    typed_rate = _money_input(form.get("hourly_rate"), "hourly rate", False, Decimal("0.01"), Decimal("1000"))
    if (form.get("contractor_id") or "").strip():
        existing = one(c.execute(
            "SELECT * FROM portal_contractors WHERE id=?",
            (form_id(form.get("contractor_id")),),
        ))
        if not existing:
            raise ValueError("That contractor was not found.")
        name = existing["name"]
    else:
        try:
            name = clean_name(form.get("name") if isinstance(form.get("name"), str) else "")
        except ValueError:
            raise ValueError("Enter a contractor name.")
        reject_sensitive(name, "name")
        existing = contractor_by_name(c, name)

    plans = []
    any_hours = False
    for iso in dates:
        field = "hours_" + iso
        if field not in form:
            continue
        raw = (form.get(field) or "").strip()
        if raw == "":
            hours = None
        else:
            hours = _hours_input(raw, True)
            any_hours = True
        plans.append((iso, hours))
    if existing is None and not any_hours:
        raise ValueError("Enter hours for at least one day.")
    if not plans and typed_rate is None:
        raise ValueError("Enter hours for a day.")

    saved = typed_rate
    if saved is None and existing is not None and existing["hourly_rate"] is not None:
        saved = _cents(existing["hourly_rate"])
    rate_changed = typed_rate is not None and (
        existing is None or existing["hourly_rate"] is None or _cents(existing["hourly_rate"]) != typed_rate
    )
    found = {}
    if existing is not None:
        for row in rows(c.execute(
            """SELECT * FROM portal_contract_entries
               WHERE contractor_id=? AND work_date>=? AND work_date<=? ORDER BY id""",
            (existing["id"], dates[0], dates[-1]),
        )):
            found.setdefault(row["work_date"], []).append(row)
    jobs = []
    for iso, hours in plans:
        current_rows = found.get(iso, [])
        if not _simple_contract_day(current_rows):
            raise ValueError(f"{long_day(iso)} has a flat amount or more than one entry. Edit it in the list below.")
        current = current_rows[0] if current_rows else None
        if hours is None:
            if current is not None:
                jobs.append(("delete", iso, None, None, current))
            continue
        if current is not None and not rate_changed and current["hourly_rate"] is not None:
            rate = _cents(current["hourly_rate"])
        else:
            rate = saved
        if rate is None:
            raise ValueError("Enter an hourly rate. It is saved for this contractor.")
        jobs.append(("save", iso, hours, rate, current))

    contractor = _upsert_contractor(c, name, typed_rate if typed_rate is not None else None, existing, None)
    for kind, iso, hours, rate, current in jobs:
        if kind == "delete":
            _delete_contract_row(c, actor, current, contractor["name"])
        else:
            _save_hourly_day(c, actor, contractor, iso, hours, rate, current)


def page_hours(c, user, query, error=""):
    try:
        anchor = date.fromisoformat(query.get("week")) if query.get("week") else today()
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", query.get("week") or anchor.isoformat()):
            raise ValueError
    except ValueError:
        anchor = today()
    monday = week_monday(anchor)
    payload = week_payload(c, monday)
    prev_w = (monday - timedelta(days=7)).isoformat()
    next_w = (monday + timedelta(days=7)).isoformat()
    head_cells = week_head_cells(payload["days"])
    week_end = monday + timedelta(days=6)
    period = period_label(monday)
    pay_span = len(payload["days"]) + 2
    contracts = contract_between(c, monday, week_end)
    wage_view = build_wage_view(payload["people"], load_rate_index(c))
    wage_by_id = {person["id"]: person for person in wage_view["people"]}
    bill = wage_bill(wage_view, contracts)
    body_rows = []
    cards = []
    for person in payload["people"]:
        person_view = wage_by_id[person["id"]]
        cells = []
        lines = []
        for day in payload["days"]:
            hours = person["dailyHours"].get(day["date"])
            cls = "weekend" if day["weekend"] else ""
            if day["publicHoliday"]:
                cls = (cls + " holiday").strip()
            day_cost = person_view["days"].get(day["date"])
            n = sum(1 for s in person["shifts"] if s["date"] == day["date"])
            if hours:
                shown = _hours_cost_html(
                    hours, n,
                    None if person_view["missing"] else (day_cost or {}).get("excl"),
                    None if person_view["missing"] else (day_cost or {}).get("incl"),
                    person_view["missing"], _day_kind_label(day_cost),
                )
            else:
                shown = "—"
            cells.append(f'<td class="num {cls}">{shown}</td>')
            flag = ""
            if day["publicHoliday"]:
                flag = " · " + day["publicHoliday"]
            elif day["weekend"]:
                flag = " · Weekend"
            lines.append(f"<li><span>{h(day['weekday'])} {h(str(date.fromisoformat(day['date']).day))}{h(flag)}</span><b>{shown}</b></li>")
        week_shown = _hours_cost_html(
            person["totalHours"], 0, person_view["excl"], person_view["incl"], person_view["missing"], "",
        )
        breakdown = pay_breakdown(person, person_view, period)
        dom = f"pay-{person['id']}"
        body_rows.append(
            f'<tr id="wage-person-{person["id"]}"><th scope="row" class="person-cell">{person_toggle(dom, person["name"])}</th>'
            f'{"".join(cells)}<td class="num total">{week_shown}</td></tr>'
        )
        body_rows.append(person_panel(dom, pay_span, breakdown))
        cards.append(
            f'<article class="card person" id="pay-card-{person["id"]}">{person_sheet(person["name"], breakdown)}'
            f'<ul class="days">{"".join(lines)}</ul><p>Week <b>{week_shown}</b></p></article>'
        )
    table = '<p class="empty">No staff yet. Add someone below, then add a shift.</p>' if not payload["people"] else f"""
<div class="week-table tw"><table><thead><tr><th>Staff</th>{head_cells}<th>Week</th></tr></thead><tbody>{''.join(body_rows)}</tbody></table></div>
<div class="week-cards">{''.join(cards)}</div>"""
    active_people = [p for p in people_rows(c) if p["active"]]
    options = "".join(f'<option value="{p["id"]}">{h(p["name"])}</option>' for p in active_people)
    shift_forms = []
    for person in payload["people"]:
        for shift in person["shifts"]:
            cost_text = _shift_cost_text(wage_by_id[person["id"]]["shifts"].get(shift["id"]))
            shift_forms.append(f"""<details class="shift"><summary>{h(person['name'])} · {h(fmt_date(shift['date']))} · {h(shift['start'])}–{h(shift['finish'])} · {h(hours_text(shift['hours']))} h · {h(cost_text)}</summary>
<form method="post" action="/portal/hours">
<input type="hidden" name="csrf" value="{h(user['csrf'])}"><input type="hidden" name="week" value="{h(monday.isoformat())}">
<input type="hidden" name="action" value="save_shift"><input type="hidden" name="shift_id" value="{shift['id']}">
<div class="row">
<label class="f">Staff<select name="person_id">{''.join(f'<option value="{p["id"]}"{" selected" if p["id"]==person["id"] else ""}>{h(p["name"])}</option>' for p in active_people)}</select></label>
<label class="f">Date<input type="date" name="work_date" value="{h(shift['date'])}" required></label>
<label class="f">Start<input type="time" name="start_time" value="{h(shift['start'])}" required></label>
<label class="f">Finish<input type="time" name="finish_time" value="{h(shift['finish'])}" required></label>
<label class="f">Unpaid break (minutes)<input type="number" name="break_minutes" min="0" max="600" value="{shift['breakMinutes']}" required></label>
</div>
<label class="f">Notes<input name="notes" maxlength="500" value="{h(shift['notes'])}"></label>
<button class="btn" type="submit">Save changes</button>
</form>
<form method="post" action="/portal/hours" data-confirm="Delete this shift?">
<input type="hidden" name="csrf" value="{h(user['csrf'])}"><input type="hidden" name="week" value="{h(monday.isoformat())}">
<input type="hidden" name="action" value="delete_shift"><input type="hidden" name="shift_id" value="{shift['id']}">
<button class="btn danger" type="submit">Delete</button></form></details>""")
    staff_rows = []
    for person in people_rows(c):
        state = "Active" if person["active"] else "Inactive"
        toggle = "deactivate_staff" if person["active"] else "activate_staff"
        toggle_label = "Deactivate" if person["active"] else "Make active"
        staff_rows.append(f"""<div class="li"><form method="post" action="/portal/hours" class="rename">
<input type="hidden" name="csrf" value="{h(user['csrf'])}"><input type="hidden" name="week" value="{h(monday.isoformat())}">
<input type="hidden" name="action" value="rename_staff"><input type="hidden" name="person_id" value="{person['id']}">
<label class="f"><span class="lbl">{state}</span><input name="name" value="{h(person['name'])}" maxlength="60" required></label>
<button class="btn ghost sm" type="submit">Rename</button></form>
<form method="post" action="/portal/hours"><input type="hidden" name="csrf" value="{h(user['csrf'])}">
<input type="hidden" name="week" value="{h(monday.isoformat())}"><input type="hidden" name="action" value="{toggle}">
<input type="hidden" name="person_id" value="{person['id']}"><button class="btn ghost sm" type="submit">{toggle_label}</button></form></div>""")
    history = []
    for row in rows(c.execute("SELECT * FROM portal_roster_audit ORDER BY id DESC LIMIT 40")):
        history.append(f"<li><b>{h(fmt_stamp(row['at']))}</b> · {h(row['actor'])} · {h(describe_audit(row))}</li>")
    alert = f'<p class="alert">{h(error)}</p>' if error else ""
    ok = ERR_TEXT.get(query.get("ok") or "", "")
    ok_html = f'<p class="oknote">{h(ok)}</p>' if ok else ""
    return f"""<div class="ph"><h1>Staff hours</h1>
<p>Week of Monday {h(fmt_date(monday.isoformat()))} to Sunday {h(fmt_date(week_end.isoformat()))}, Australia/Brisbane. A shift that finishes after midnight counts on the day it started, and the pay rate follows that day. Weekend days and Queensland public holidays are marked. The Brisbane show holiday is not included.</p>
<p class="help">Employee hours are for the pay run. The CSV download and the staff-hours feed include those hours only. Contract entries and pay rates are not in them. Tap a name in the pay run, or a contract name, to open that person's date, hours, rate, and cost for this week. This page does not send hours to Xero or pay anyone. Astron still approves and posts every pay run.</p></div>
{alert}{ok_html}
<div class="weekbar"><a class="btn ghost sm" href="/portal/hours?week={prev_w}">Previous week</a>
<form method="get" action="/portal/hours"><label class="f">Week of<input type="date" name="week" value="{h(monday.isoformat())}"></label><button class="btn sm" type="submit">Show</button></form>
<a class="btn ghost sm" href="/portal/hours?week={next_w}">Next week</a>
<a class="btn sm" href="/portal/hours.csv?week={h(monday.isoformat())}">Download CSV</a></div>
{wage_summary_html(wage_view, bill)}
<section class="card" id="hours-week"><h2>Pay run</h2>
<p class="help">Employee hours by day for this week. Tap a name for the rate and cost.</p>
<div class="lbl">Grand total</div><b class="stat" id="grand-total">{h(hours_text(payload['grandTotal']))} h</b>
<p class="help">Employee hours only. Contract hours are not in this total.</p>{table}</section>
{contract_section_html(c, user, monday, payload["days"], contracts)}
<section class="card"><h2>Add a shift</h2>
<form method="post" action="/portal/hours"><input type="hidden" name="csrf" value="{h(user['csrf'])}"><input type="hidden" name="week" value="{h(monday.isoformat())}"><input type="hidden" name="action" value="save_shift">
<div class="row"><label class="f">Staff<select name="person_id" required>{options or '<option value="">Add staff first</option>'}</select></label>
<label class="f">Date<input type="date" name="work_date" value="{h(monday.isoformat())}" required></label>
<label class="f">Start<input type="time" name="start_time" required></label>
<label class="f">Finish<input type="time" name="finish_time" required></label>
<label class="f">Unpaid break (minutes)<input type="number" name="break_minutes" min="0" max="600" value="0" required></label></div>
<label class="f">Notes<input name="notes" maxlength="500"></label>
<button class="btn" type="submit">Add shift</button></form></section>
<section class="card"><h2>Shifts this week</h2>{''.join(shift_forms) or '<p class="empty">No shifts this week.</p>'}</section>
<section class="card" id="staff-list"><h2>Staff</h2>
<form method="post" action="/portal/hours" class="row"><input type="hidden" name="csrf" value="{h(user['csrf'])}"><input type="hidden" name="week" value="{h(monday.isoformat())}"><input type="hidden" name="action" value="add_staff">
<label class="f">Name<input name="name" maxlength="60" required placeholder="e.g. Alex"></label>
<button class="btn" type="submit">Add staff</button></form>
<div class="list">{''.join(staff_rows) or '<p class="empty">No staff yet.</p>'}</div></section>
<section class="card" id="history"><h2>Change history</h2><ul class="history">{''.join(history) or '<li>No changes yet.</li>'}</ul></section>"""


def describe_audit(row):
    try:
        before = json.loads(row["before_json"]) if row["before_json"] else None
        after = json.loads(row["after_json"]) if row["after_json"] else None
    except json.JSONDecodeError:
        return row["action"]
    if row["action"] == "add_staff":
        return "added staff " + (after or {}).get("name", "")
    if row["action"] == "rename_staff":
        return f"renamed {(before or {}).get('name', '')} to {(after or {}).get('name', '')}"
    if row["action"] == "deactivate_staff":
        return "deactivated " + (after or before or {}).get("name", "")
    if row["action"] == "activate_staff":
        return "activated " + (after or before or {}).get("name", "")
    if row["action"] == "delete_shift":
        b = before or {}
        return f"deleted {b.get('staff', '')} {b.get('date', '')} {b.get('start', '')}–{b.get('finish', '')}"
    if row["action"] in ("add_contract", "edit_contract", "delete_contract"):
        snap = after or before or {}
        if snap.get("flat") is not None:
            basis = f"flat {money(snap.get('cost'))}"
        elif snap.get("hours") is not None:
            basis = f"{hours_text(snap.get('hours'))} h, {money(snap.get('cost'))}"
        else:
            basis = money(snap.get("cost"))
        verb = {"add_contract": "added", "edit_contract": "edited", "delete_contract": "deleted"}[row["action"]]
        return f"{verb} contract {snap.get('name', '')} {snap.get('date', '')} ({basis})"
    if before is None:
        a = after or {}
        return f"added {a.get('staff', '')} {a.get('date', '')} {a.get('start', '')}–{a.get('finish', '')} ({hours_text(a.get('hours'))} h)"
    bits = []
    labels = {"staff": "staff", "date": "date", "start": "start", "finish": "finish", "break": "break", "notes": "notes", "hours": "hours"}
    for key, label in labels.items():
        if (before or {}).get(key) != (after or {}).get(key):
            bits.append(f"{label} {(before or {}).get(key)} → {(after or {}).get(key)}")
    who = (after or before or {}).get("staff", "")
    when = (after or before or {}).get("date", "")
    return f"edited {who} {when}: " + ", ".join(bits or ["saved"])


def login_page(error, csrf, setup_missing=False):
    message = ""
    if setup_missing:
        message = '<p class="alert">Owner login is not set up yet. Set PORTAL_ADMIN_USER and PORTAL_ADMIN_PASSWORD_HASH on the server, then restart.</p>'
    elif error == "locked":
        message = '<p class="alert">Too many attempts. Try again in a few minutes.</p>'
    elif error == "csrf":
        message = '<p class="alert">That form expired. Reload the page and try again.</p>'
    elif error:
        message = '<p class="alert">That username or password is not right.</p>'
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex, nofollow"><meta name="theme-color" content="#0f2a44">
<title>Owner login · Silkwood</title>
<link rel="icon" type="image/png" href="/static/icon-192.png">
<link rel="apple-touch-icon" href="/static/apple-touch-icon.png">
<link rel="stylesheet" href="/static/portal.css"></head>
<body class="login-body"><main class="login-wrap"><section class="login-hero">
<div class="logo"><i></i><div>SILK<span>WOOD</span><small>United Silkwood</small></div></div>
<p>Sales, kitchen, stock and the pay week. Australia/Brisbane.</p>
</section><section class="login card">
<h1>Owner login</h1>
<p class="help">Private figures for United Silkwood. Staff PIN pages are separate.</p>
{message}
<form method="post" action="/portal/login" accept-charset="utf-8">
<input type="hidden" name="csrf" value="{h(csrf)}">
<label class="f">Username<input name="username" autocomplete="username" autocapitalize="none" required maxlength="63"></label>
<label class="f">Password<input type="password" name="password" autocomplete="current-password" required></label>
<button class="btn full" type="submit">Log in</button>
</form></section></main></body></html>"""


def password_page(user, error="", ok=False):
    message = '<p class="oknote">Password changed.</p>' if ok else ""
    if error:
        message = f'<p class="alert">{h(error)}</p>'
    body = f"""<div class="ph"><h1>Change password</h1><p>This only changes the owner portal. It does not change the staff PIN.</p></div>
{message}
<section class="card"><form method="post" action="/portal/password">
<input type="hidden" name="csrf" value="{h(user['csrf'])}">
<label class="f">Current password<input type="password" name="current" autocomplete="current-password" required></label>
<label class="f">New password<input type="password" name="new" autocomplete="new-password" required minlength="10" maxlength="200"></label>
<label class="f">Confirm new password<input type="password" name="confirm" autocomplete="new-password" required minlength="10" maxlength="200"></label>
<button class="btn" type="submit">Change password</button>
</form></section>"""
    return body


def send_html(handler, code, html, extra=None, camera=False):
    handler.send(code, html.encode(), "text/html; charset=utf-8", extra=extra or [], camera=camera)


def send_portal(handler, code, html, user=None, extra=None, camera=False):
    headers = [("X-Robots-Tag", "noindex, nofollow"), ("Content-Security-Policy", CSP)]
    if user:
        age = int(session_hours() * 3600)
        headers.append(cookie_pair(CSRF_COOKIE, user["csrf"], handler, age, False))
    if extra:
        headers.extend(extra)
    send_html(handler, code, html, headers, camera=camera)


def handle_get(handler, parsed):
    path = parsed.path.rstrip("/") or "/"
    if path == "/portal":
        path = "/portal"
    raw_query = parse_qs(parsed.query)
    query = {k: (v if len(v) > 1 else v[0]) for k, v in raw_query.items()}
    if path == "/portal/login":
        csrf = secrets.token_urlsafe(32)
        c = db()
        try:
            missing = one(c.execute("SELECT COUNT(*) AS n FROM portal_users"))["n"] == 0
        finally:
            c.close()
        html = login_page("", csrf, missing)
        send_portal(handler, 200, html, extra=[cookie_pair(CSRF_COOKIE, csrf, handler, 3600, False)])
        return
    if path == "/api/portal/staff-hours":
        return handle_hours_api(handler, query)
    if path == "/api/portal/invoice-payments":
        return handle_invoice_payments_get(handler)
    if path == "/api/portal/stock":
        return handle_stock_api(handler)
    if path == "/api/portal/stock/barcode":
        return handle_barcode_get(handler, query)
    if path == "/api/portal/chat":
        return handle_chat_get(handler)
    user = session_user(handler)
    if not user:
        if path.startswith("/api/"):
            handler.send(401, {"error": "login required"})
        else:
            handler.redirect("/portal/login")
        return
    c = db()
    try:
        if path == "/portal":
            html = layout("Sales overview", "overview", page_overview(c, query), user, _srv().now())
        elif path == "/portal/online":
            html = layout("Online orders", "online", page_online(c, query), user, _srv().now())
        elif path == "/portal/exceptions":
            html = layout("Till exceptions", "exceptions", exceptions().page(c, query), user, _srv().now())
        elif path == "/portal/products":
            html = layout("Products", "products", parity().page_products(c, query, user), user, _srv().now())
        elif path == "/portal/stock":
            html = layout("Stock on hand", "stock", stock().page_stock(c, user, query), user, _srv().now())
        elif path == "/portal/pricing":
            html = layout("Pricing", "pricing", parity().page_pricing(c, query), user, _srv().now())
        elif path == "/portal/till-prices":
            html = layout("Till price changes", "till", parity().page_till(c, query), user, _srv().now())
        elif path == "/portal/departments":
            html = layout("Departments", "departments", parity().page_departments(c, query), user, _srv().now())
        elif path == "/portal/kitchen":
            html = layout("Kitchen", "kitchen", parity().page_kitchen(c, query), user, _srv().now())
        elif path == "/portal/purchases":
            html = layout("Purchases", "purchases", page_purchases(c, query), user, _srv().now())
        elif path == "/portal/fuel":
            html = layout("Fuel commission", "fuel", page_fuel(c, query), user, _srv().now())
        elif path == "/portal/dips":
            html = layout("Pumps and dips", "dips", page_dips(c, query), user, _srv().now())
        elif path == "/portal/shifts":
            html = layout("Shift balancing", "shifts", parity().page_shifts(c, query), user, _srv().now())
        elif path == "/portal/commission":
            html = layout("Commission", "commission", parity().page_commission(c, query), user, _srv().now())
        elif path == "/portal/notes":
            html = layout("Data notes", "notes", parity().page_notes(c), user, _srv().now())
        elif path == "/portal/invoices":
            html = layout("Invoices", "invoices", page_invoices(c, query, user), user, _srv().now())
        elif path == "/portal/day":
            html = layout("Daily summary", "daily", daypage().page(c, query), user, _srv().now())
        elif path == "/portal/hours":
            html = layout("Staff hours", "hours", page_hours(c, user, query), user, _srv().now())
        elif path == "/portal/password":
            html = layout("Change password", "", password_page(user, ok=query.get("ok") == "1"), user, _srv().now())
        elif path == "/portal/hours.csv":
            try:
                anchor = date.fromisoformat(query.get("week")) if query.get("week") else today()
            except ValueError:
                handler.send(400, {"error": "week must be YYYY-MM-DD"})
                return
            monday = week_monday(anchor)
            payload = week_payload(c, monday)
            extra = [
                ("X-Robots-Tag", "noindex, nofollow"),
                ("Content-Disposition", f'attachment; filename="silkwood-hours-{monday.isoformat()}.csv"'),
            ]
            handler.send(200, hours_csv(payload), "text/csv; charset=utf-8", extra=extra)
            return
        else:
            handler.send(404, {"error": "not found"})
            return
    finally:
        c.close()
    send_portal(handler, 200, html, user, camera=(path == "/portal/stock"))


def handle_hours_api(handler, query):
    key = ingest_key()
    user = session_user(handler)
    if not (key and secret_eq(bearer_token(handler), key)) and not user:
        handler.send(401, {"error": "login required"})
        return
    if query.get("week"):
        try:
            anchor = date.fromisoformat(need_date(query.get("week"), "week"))
        except ValueError:
            handler.send(400, {"error": "week must be YYYY-MM-DD"})
            return
    else:
        anchor = today()
    c = db()
    try:
        payload = week_payload(c, week_monday(anchor))
    finally:
        c.close()
    handler.send(200, payload, extra=[("X-Robots-Tag", "noindex, nofollow")])


def _chat_user(handler):
    user = session_user(handler)
    if not user:
        handler.send(401, {"error": "login required"})
        return None
    return user


def handle_chat_get(handler):
    user = _chat_user(handler)
    if not user:
        return
    c = db()
    try:
        payload = {
            "messages": chat().list_messages(c),
            "assistant": "online" if os.environ.get("PORTAL_CHAT_WEBHOOK_URL", "").strip() else "offline",
        }
    finally:
        c.close()
    handler.send(200, payload, extra=[("X-Robots-Tag", "noindex, nofollow")])


def handle_chat_post(handler):
    user = _chat_user(handler)
    if not user:
        return
    try:
        form = read_form(handler)
    except (OverflowError, ValueError):
        handler.send(400, {"error": "bad request"})
        return
    if not csrf_ok(handler, form, user):
        handler.send(403, {"error": "form expired"})
        return
    c = db()
    try:
        try:
            message_id, status = chat().add_message(c, user, form.get("message"), form.get("page"), form.get("context") or {})
        except ValueError as ex:
            handler.send(400, {"error": str(ex)[:180]})
            return
        c.commit()
    finally:
        c.close()
    handler.send(200, {"ok": True, "id": message_id, "status": status})


def handle_chat_clear(handler):
    user = _chat_user(handler)
    if not user:
        return
    try:
        form = read_form(handler)
    except (OverflowError, ValueError):
        form = {}
    if not csrf_ok(handler, form, user):
        handler.send(403, {"error": "form expired"})
        return
    c = db()
    try:
        chat().clear_messages(c)
        c.commit()
    finally:
        c.close()
    handler.send(200, {"ok": True, "messages": []})


def handle_chat_reply(handler):
    key = ingest_key()
    if not key:
        log("chat reply refused; PORTAL_INGEST_KEY is not set")
        handler.send(503, {"error": "ingest is not configured"})
        return
    if not secret_eq(bearer_token(handler), key):
        handler.send(401, {"error": "unauthorized"})
        return
    try:
        form = read_form(handler)
    except (OverflowError, ValueError):
        handler.send(400, {"error": "bad request"})
        return
    raw_id = form.get("id")
    if isinstance(raw_id, bool) or not isinstance(raw_id, int) or raw_id < 1:
        handler.send(400, {"error": "id is required"})
        return
    c = db()
    try:
        try:
            chat().set_reply(c, raw_id, form.get("reply"))
        except LookupError:
            handler.send(404, {"error": "not found"})
            return
        except ValueError as ex:
            handler.send(400, {"error": str(ex)[:180]})
            return
        c.commit()
    finally:
        c.close()
    handler.send(200, {"ok": True, "id": raw_id})


def handle_stock_api(handler):
    key = ingest_key()
    user = session_user(handler)
    if not (key and secret_eq(bearer_token(handler), key)) and not user:
        handler.send(401, {"error": "login required"})
        return
    c = db()
    try:
        payload = stock().tracked_payload(c)
    finally:
        c.close()
    handler.send(200, payload, extra=[("X-Robots-Tag", "noindex, nofollow")])


def handle_barcode_get(handler, query):
    """Session only. The ingest key can read stock totals, not barcode links."""
    if not session_user(handler):
        handler.send(401, {"error": "login required"})
        return
    c = db()
    try:
        try:
            found = stock().find_barcode(c, query.get("barcode") or "")
        except ValueError as ex:
            handler.send(400, {"ok": False, "error": str(ex)[:180]})
            return
    finally:
        c.close()
    extra = [("X-Robots-Tag", "noindex, nofollow")]
    if not found:
        handler.send(404, {"ok": False, "error": "Barcode not linked", "barcode": stock().clean_barcode(query.get("barcode") or "")}, extra=extra)
        return
    handler.send(200, found, extra=extra)


def handle_barcode_post(handler):
    user = session_user(handler)
    if not user:
        handler.send(401, {"error": "login required"})
        return
    try:
        form = read_form(handler)
    except (OverflowError, ValueError):
        handler.send(400, {"error": "bad request"})
        return
    if not csrf_ok(handler, form, user):
        handler.send(403, {"error": "form expired"})
        return
    c = db()
    try:
        try:
            result = stock().link_barcode(c, user, form.get("barcode"), form.get("code"))
        except ValueError as ex:
            handler.send(400, {"ok": False, "error": str(ex)[:180]})
            return
        c.commit()
    finally:
        c.close()
    handler.send(200, result)


def handle_post(handler):
    path = urlparse(handler.path).path.rstrip("/") or "/"
    if path == "/api/portal/ingest":
        return handle_ingest(handler)
    if path == "/api/portal/invoice-payments/match":
        return handle_invoice_payment_match(handler)
    if path == "/api/portal/chat/reply":
        return handle_chat_reply(handler)
    if path == "/api/portal/chat":
        return handle_chat_post(handler)
    if path == "/api/portal/chat/clear":
        return handle_chat_clear(handler)
    if path == "/api/portal/stock/barcode":
        return handle_barcode_post(handler)
    if path.startswith("/api/"):
        if not session_user(handler):
            handler.send(401, {"error": "login required"})
            return
        handler.send(404, {"error": "not found"})
        return
    if path == "/portal/login":
        return handle_login(handler)
    user = session_user(handler)
    if not user:
        if path.startswith("/api/"):
            handler.send(401, {"error": "login required"})
        else:
            handler.redirect("/portal/login")
        return
    try:
        form = read_form(handler)
    except OverflowError:
        handler.send(413, {"error": "too large"})
        return
    except ValueError:
        handler.send(400, {"error": "bad request"})
        return
    if not csrf_ok(handler, form, user):
        send_portal(handler, 403, layout("Expired", "", '<div class="ph"><h1>Form expired</h1><p><a href="/portal">Back to the portal</a></p></div>', user, _srv().now()), user)
        return
    if path == "/portal/logout":
        handler.redirect("/portal/login", clear_session(handler))
        return
    if path == "/portal/password":
        return handle_password(handler, user, form)
    if path == "/portal/hours":
        return handle_hours_post(handler, user, form)
    if path == "/portal/stock":
        return handle_stock_post(handler, user, form)
    if path == "/portal/products":
        return handle_products_post(handler, user, form)
    if path == "/portal/invoices":
        return handle_invoices_post(handler, user, form)
    handler.send(404, {"error": "not found"})


def handle_products_post(handler, user, form):
    c = db()
    try:
        action = form.get("action") or ""
        try:
            if action != "set_supplier":
                handler.send(400, {"error": "bad action"})
                return
            assign_product_supplier(c, user, form.get("code"), form.get("supplier"))
        except ValueError as ex:
            query = back_query(form.get("back"))
            html = layout(
                "Products", "products",
                parity().page_products(c, query, user, str(ex)),
                user, _srv().now(),
            )
            send_portal(handler, 400, html, user)
            return
        c.commit()
    finally:
        c.close()
    log("product supplier saved")
    handler.redirect(with_ok(safe_portal_return("/portal/products", form.get("back")), "supplier"))


def handle_stock_post(handler, user, form):
    c = db()
    try:
        action = form.get("action") or ""
        linked = None
        saved_supplier = False
        try:
            if action == "set_count":
                stock().save_count(c, user, form)
            elif action == "add_manual":
                stock().add_manual(c, user, form)
            elif action == "edit_manual":
                stock().edit_manual(c, user, form)
            elif action == "untrack":
                stock().untrack(c, user, form)
            elif action == "link_barcode":
                linked = stock().link_barcode(c, user, form.get("barcode"), form.get("code"))
            elif action == "set_supplier":
                assign_product_supplier(c, user, form.get("code"), form.get("supplier"))
                saved_supplier = True
            else:
                handler.send(400, {"error": "bad action"})
                return
        except ValueError as ex:
            query = back_query(form.get("back")) if action == "set_supplier" else {}
            if action == "link_barcode":
                query = {"link": form.get("barcode") or "", "pick": form.get("code") or ""}
            html = layout("Stock on hand", "stock", stock().page_stock(c, user, query, str(ex)), user, _srv().now())
            send_portal(handler, 400, html, user, camera=True)
            return
        c.commit()
    finally:
        c.close()
    if linked:
        moved = "&moved=1" if linked.get("movedFrom") else ""
        handler.redirect("/portal/stock?pick=" + quote(linked["code"]) + "&ok=linked" + moved)
        return
    if saved_supplier:
        log("product supplier saved")
        handler.redirect(with_ok(safe_portal_return("/portal/stock", form.get("back")), "supplier"))
        return
    handler.redirect("/portal/stock?ok=1")


def handle_login(handler):
    try:
        form = read_form(handler)
    except (OverflowError, ValueError):
        handler.send(400, {"error": "bad request"})
        return
    cookie = read_cookies(handler).get(CSRF_COOKIE, "")
    if not tokens_eq(form.get("csrf", ""), cookie):
        csrf = secrets.token_urlsafe(32)
        send_portal(handler, 403, login_page("csrf", csrf), extra=[cookie_pair(CSRF_COOKIE, csrf, handler, 3600, False)])
        return
    username = (form.get("username") or "").strip().casefold()
    password = form.get("password") or ""
    ip = client_ip(handler)
    when = _srv().now()
    c = db()
    try:
        if login_locked(c, username or "-", ip, when):
            c.commit()
            csrf = secrets.token_urlsafe(32)
            send_portal(handler, 429, login_page("locked", csrf), extra=[cookie_pair(CSRF_COOKIE, csrf, handler, 3600, False)])
            return
        user = one(c.execute("SELECT * FROM portal_users WHERE username=? AND active=1", (username,))) if username else None
        ok = password_ok(password, user["password_hash"] if user else "")
        if not user or not ok:
            note_failure(c, username or "-", ip, when)
            c.commit()
            csrf = secrets.token_urlsafe(32)
            send_portal(handler, 401, login_page("bad", csrf), extra=[cookie_pair(CSRF_COOKIE, csrf, handler, 3600, False)])
            return
        c.execute("DELETE FROM portal_login_attempts WHERE username=? AND ip=?", (username, ip))
        c.commit()
        user_id = user["id"]
    finally:
        c.close()
    _token, cookies = start_session(user_id, handler)
    handler.redirect("/portal", cookies)


def handle_password(handler, user, form):
    current = form.get("current") or ""
    new = form.get("new") or ""
    confirm = form.get("confirm") or ""
    c = db()
    try:
        row = one(c.execute("SELECT * FROM portal_users WHERE id=?", (user["user_id"],)))
        if not row or not password_ok(current, row["password_hash"]):
            html = layout("Change password", "", password_page(user, "The current password is not right."), user, _srv().now())
            send_portal(handler, 400, html, user)
            return
        if new != confirm:
            html = layout("Change password", "", password_page(user, "The new passwords do not match."), user, _srv().now())
            send_portal(handler, 400, html, user)
            return
        problem = check_new_password(new, row["username"])
        if problem:
            html = layout("Change password", "", password_page(user, problem), user, _srv().now())
            send_portal(handler, 400, html, user)
            return
        if password_ok(new, row["password_hash"]):
            html = layout("Change password", "", password_page(user, "Choose a different password."), user, _srv().now())
            send_portal(handler, 400, html, user)
            return
        c.execute("UPDATE portal_users SET password_hash=?, updated_at=? WHERE id=?", (hash_password(new), stamp(), row["id"]))
        c.execute("DELETE FROM portal_sessions WHERE user_id=? AND token_hash<>?", (row["id"], user["token_hash"]))
        c.commit()
    finally:
        c.close()
    handler.redirect("/portal/password?ok=1")


def form_id(value):
    if value is None or not re.fullmatch(r"\d{1,9}", str(value)):
        return -1
    return int(value)


def clean_name(value):
    name = re.sub(r"\s+", " ", (value or "").strip())
    if not name or len(name) > 60 or not re.search(r"[A-Za-z]", name):
        raise ValueError("name")
    return name


def handle_hours_post(handler, user, form):
    action = form.get("action") or ""
    week = form.get("week") or ""
    actor = user["username"]
    c = db()
    try:
        if action == "add_staff":
            try:
                name = clean_name(form.get("name"))
            except ValueError:
                _hours_error(handler, c, user, week, "Enter a staff name.")
                return
            if one(c.execute("SELECT 1 AS x FROM portal_people WHERE lower(name)=lower(?) AND active=1", (name,))):
                _hours_error(handler, c, user, week, "That name is already on the active list.")
                return
            person_id = one(c.execute(
                "INSERT INTO portal_people(name,active,created_at,updated_at) VALUES(?,1,?,?) RETURNING id",
                (name, stamp(), stamp()),
            ))["id"]
            audit(c, actor, "add_staff", person_id, None, None, {"name": name})
        elif action in ("rename_staff", "deactivate_staff", "activate_staff"):
            person = one(c.execute("SELECT * FROM portal_people WHERE id=?", (form_id(form.get("person_id")),)))
            if not person:
                _hours_error(handler, c, user, week, "That staff member was not found.")
                return
            if action == "rename_staff":
                try:
                    name = clean_name(form.get("name"))
                except ValueError:
                    _hours_error(handler, c, user, week, "Enter a staff name.")
                    return
                c.execute("UPDATE portal_people SET name=?, updated_at=? WHERE id=?", (name, stamp(), person["id"]))
                audit(c, actor, "rename_staff", person["id"], None, {"name": person["name"]}, {"name": name})
            else:
                active = 0 if action == "deactivate_staff" else 1
                c.execute("UPDATE portal_people SET active=?, updated_at=? WHERE id=?", (active, stamp(), person["id"]))
                audit(c, actor, action, person["id"], None, {"name": person["name"], "active": person["active"]}, {"name": person["name"], "active": active})
        elif action == "save_shift":
            person = one(c.execute("SELECT * FROM portal_people WHERE id=?", (form_id(form.get("person_id")),)))
            if not person or not person["active"]:
                _hours_error(handler, c, user, week, "Choose an active staff member.")
                return
            try:
                work_date = need_date(form.get("work_date"), "date")
                d = date.fromisoformat(work_date)
                if d < date(2024, 1, 1) or d > today() + timedelta(days=400):
                    raise ValueError("date")
                break_raw = form.get("break_minutes") or "0"
                if not re.fullmatch(r"\d{1,3}", break_raw):
                    raise ValueError("break")
                hours = shift_hours(work_date, form.get("start_time") or "", form.get("finish_time") or "", int(break_raw))
            except ValueError as ex:
                text = str(ex)
                if "break" in text.lower():
                    text = "The unpaid break must be shorter than the shift and between 0 and 600 minutes."
                elif "HH:MM" in text or "20 hours" in text or "longer" in text:
                    text = text
                else:
                    text = "Check the date and times. A shift can cross midnight."
                _hours_error(handler, c, user, week, text)
                return
            notes = (form.get("notes") or "").strip()[:500]
            try:
                reject_sensitive(notes, "notes")
            except ValueError:
                _hours_error(handler, c, user, week, "Notes cannot include card or bank numbers.")
                return
            if form.get("shift_id"):
                current = one(c.execute("SELECT * FROM portal_roster WHERE id=?", (form_id(form.get("shift_id")),)))
                if not current:
                    _hours_error(handler, c, user, week, "That shift was not found.")
                    return
                old_name = one(c.execute("SELECT name FROM portal_people WHERE id=?", (current["person_id"],)))
                before = shift_snapshot(current, old_name["name"] if old_name else "")
                c.execute(
                    """UPDATE portal_roster SET person_id=?, work_date=?, start_time=?, finish_time=?, break_minutes=?, notes=?, hours=?, updated_at=?, updated_by=? WHERE id=?""",
                    (person["id"], work_date, form.get("start_time"), form.get("finish_time"), int(break_raw), notes, hours, stamp(), actor, current["id"]),
                )
                after_row = dict(current)
                after_row.update({"person_id": person["id"], "work_date": work_date, "start_time": form.get("start_time"),
                                  "finish_time": form.get("finish_time"), "break_minutes": int(break_raw), "notes": notes, "hours": hours})
                audit(c, actor, "edit_shift", person["id"], current["id"], before, shift_snapshot(after_row, person["name"]))
            else:
                new_id = one(c.execute(
                    """INSERT INTO portal_roster(person_id,work_date,start_time,finish_time,break_minutes,notes,hours,created_at,updated_at,created_by,updated_by)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?) RETURNING id""",
                    (person["id"], work_date, form.get("start_time"), form.get("finish_time"), int(break_raw), notes, hours, stamp(), stamp(), actor, actor),
                ))["id"]
                snapshot = {"staff": person["name"], "date": work_date, "start": form.get("start_time"), "finish": form.get("finish_time"),
                            "break": int(break_raw), "notes": notes, "hours": hours}
                audit(c, actor, "add_shift", person["id"], new_id, None, snapshot)
        elif action == "delete_shift":
            current = one(c.execute(
                """SELECT r.*, p.name FROM portal_roster r JOIN portal_people p ON p.id=r.person_id WHERE r.id=?""",
                (form_id(form.get("shift_id")),),
            ))
            if not current:
                _hours_error(handler, c, user, week, "That shift was not found.")
                return
            before = shift_snapshot(current, current["name"])
            c.execute("DELETE FROM portal_roster WHERE id=?", (current["id"],))
            audit(c, actor, "delete_shift", current["person_id"], current["id"], before, None)
        elif action in ("save_contract", "delete_contract", "save_contract_days"):
            try:
                if action == "delete_contract":
                    delete_contract(c, actor, form)
                elif action == "save_contract_days":
                    save_contract_days(c, actor, form)
                else:
                    save_contract(c, actor, form)
            except ValueError as ex:
                _hours_error(handler, c, user, week, str(ex)[:180])
                return
        else:
            handler.send(400, {"error": "bad action"})
            return
        c.commit()
    finally:
        c.close()
    dest = "/portal/hours"
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", week or ""):
        dest += "?week=" + week + "&ok=saved"
    else:
        dest += "?ok=saved"
    handler.redirect(dest)


def _hours_error(handler, c, user, week, message):
    query = {"week": week} if re.fullmatch(r"\d{4}-\d{2}-\d{2}", week or "") else {}
    html = layout("Staff hours", "hours", page_hours(c, user, query, message), user, _srv().now())
    send_portal(handler, 400, html, user)


def _request_suppliers(handler):
    return parse_qs(urlparse(handler.path).query).get("supplier") or []


def _invoices_redirect(handler, list_name, ok):
    pairs = [("list", list_name), ("ok", ok)]
    for name in _request_suppliers(handler):
        pairs.append(("supplier", name))
    return "/portal/invoices?" + urlencode(pairs)


def _invoices_query(handler):
    raw = parse_qs(urlparse(handler.path).query)
    query = {k: (v if len(v) > 1 else v[0]) for k, v in raw.items()}
    return query


def handle_invoices_post(handler, user, form):
    action = form.get("action") or ""
    if action not in ("save_payment", "undo_payment"):
        handler.send(400, {"error": "bad action"})
        return
    c = db()
    try:
        try:
            if action == "undo_payment":
                undo_owner_payment(c, form.get("supplier"), form.get("invoice_number"))
                ok = "removed"
                dest_list = "unpaid"
            else:
                save_owner_payment(
                    c, user["username"], form.get("supplier"), form.get("invoice_number"),
                    form.get("method"), form.get("paid_on"), form.get("amount"), form.get("note"),
                )
                ok = "saved"
                dest_list = "paid"
        except ValueError as ex:
            html = layout(
                "Invoices", "invoices",
                page_invoices(c, _invoices_query(handler), user, str(ex)[:180], (form.get("supplier"), form.get("invoice_number"))),
                user, _srv().now(),
            )
            send_portal(handler, 400, html, user)
            return
        c.commit()
    finally:
        c.close()
    log("owner invoice payment " + ("removed" if action == "undo_payment" else "saved"))
    handler.redirect(_invoices_redirect(handler, dest_list, ok))


def ingest_authorized(handler):
    """Bearer PORTAL_INGEST_KEY, same rule as the nightly ingest. Never logs the key."""
    key = ingest_key()
    if not key:
        log("portal api refused; PORTAL_INGEST_KEY is not set")
        handler.send(503, {"error": "ingest is not configured"})
        return False
    if not secret_eq(bearer_token(handler), key):
        handler.send(401, {"error": "unauthorized"})
        return False
    return True


def handle_invoice_payments_get(handler):
    if not ingest_authorized(handler):
        return
    c = db()
    try:
        payload = {"payments": awaiting_owner_payments(c)}
    finally:
        c.close()
    handler.send(200, payload, extra=[("X-Robots-Tag", "noindex, nofollow")])


def handle_invoice_payment_match(handler):
    if not ingest_authorized(handler):
        return
    try:
        form = read_form(handler)
    except (OverflowError, ValueError):
        handler.send(400, {"error": "bad request"})
        return
    c = db()
    try:
        try:
            found = match_owner_payment(c, form)
        except ValueError as ex:
            handler.send(400, {"error": str(ex)[:180]})
            return
        if not found:
            handler.send(404, {"error": "not found"})
            return
        payment, already = found
        c.commit()
    finally:
        c.close()
    log("invoice payment matched")
    handler.send(200, payment_public(payment, already))


def handle_ingest(handler):
    key = ingest_key()
    if not key:
        log("ingest refused; PORTAL_INGEST_KEY is not set")
        handler.send(503, {"error": "ingest is not configured"})
        return
    if not secret_eq(bearer_token(handler), key):
        handler.send(401, {"error": "unauthorized"})
        return
    ip = client_ip(handler)
    now_ts = __import__("time").time()
    hits = [t for t in _ingest_hits.get(ip, []) if now_ts - t < 60]
    if len(hits) >= 30:
        handler.send(429, {"error": "too many ingest requests"})
        return
    hits.append(now_ts)
    _ingest_hits[ip] = hits
    try:
        raw = read_limited(handler, INGEST_MAX)
    except OverflowError:
        handler.send(413, {"error": "payload too large"})
        return
    except ValueError:
        log("ingest rejected: bad json")
        handler.send(400, {"error": "body must be JSON"})
        return
    encoding = (handler.headers.get("Content-Encoding") or "").strip().lower()
    if encoding == "gzip":
        try:
            raw = _gunzip_limited(raw, INGEST_MAX)
        except OverflowError:
            handler.send(413, {"error": "payload too large"})
            return
        except ValueError:
            log("ingest rejected: bad gzip")
            handler.send(400, {"error": "body must be gzip-compressed JSON"})
            return
    elif encoding and encoding != "identity":
        handler.send(400, {"error": "content encoding must be gzip or identity"})
        return
    try:
        body = json.loads(raw.decode("utf-8") or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        log("ingest rejected: bad json")
        handler.send(400, {"error": "body must be JSON"})
        return
    if not isinstance(body, dict) or body.get("dataset") not in DATASETS or "data" not in body:
        handler.send(400, {"error": "body must be {dataset, data} with a known dataset"})
        return
    dataset = body["dataset"]
    try:
        c = db()
        try:
            result = APPLIERS[dataset](c, body["data"])
            c.commit()
        finally:
            c.close()
    except ValueError as ex:
        log("ingest rejected: " + dataset)
        handler.send(400, {"error": str(ex)[:180]})
        return
    except Exception:
        log("ingest failed: " + dataset)
        handler.send(500, {"error": "could not store that payload"})
        return
    log(f"ingest {dataset} {result.get('asOf', '')}")
    handler.send(200, {"ok": True, "dataset": dataset, **result})


def ensure_online_order_columns(c):
    """Older databases were created before customer and item lines existed."""
    wanted = (("customer_name", "TEXT"), ("order_type", "TEXT"), ("fulfilment_time", "TEXT"), ("items", "TEXT"))
    if _srv().PG:
        found = {
            row["column_name"]
            for row in c.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_name='portal_online_orders'"
            ).fetchall()
        }
    else:
        found = {row["name"] for row in c.execute("PRAGMA table_info(portal_online_orders)").fetchall()}
    for name, kind in wanted:
        if name not in found:
            c.execute(f"ALTER TABLE portal_online_orders ADD COLUMN {name} {kind}")


def ensure_invoice_columns(c):
    """Older databases were created before paid_date and amount existed."""
    wanted = (("paid_date", "TEXT"), ("amount", "REAL"), ("xero_invoice_id", "TEXT"))
    if _srv().PG:
        found = {
            row["column_name"]
            for row in c.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_name='portal_invoices'"
            ).fetchall()
        }
    else:
        found = {row["name"] for row in c.execute("PRAGMA table_info(portal_invoices)").fetchall()}
    for name, kind in wanted:
        if name not in found:
            c.execute(f"ALTER TABLE portal_invoices ADD COLUMN {name} {kind}")


def install():
    srv = _srv()
    c = srv.PgConn(direct=True) if srv.PG else srv.db()
    try:
        c.executescript(SCHEMA)
        ensure_invoice_columns(c)
        ensure_online_order_columns(c)
        c.commit()
    finally:
        c.close()
    bootstrap_admin()
