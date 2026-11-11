"""Owner portal: login, nightly ingest, sales pages, invoices, and staff hours.

Separate from the staff PIN. Pages and JSON under /portal and /api/portal require a
session cookie, except the login page and the bearer-key ingest/hours endpoints.
The portal never takes payment and never posts payroll.
"""
import csv, hashlib, hmac, io, json, os, re, secrets
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

DATASETS = (
    "sales", "purchases", "fuel-commission", "fuel-dips", "insights", "invoices",
    "product-search", "retail-pricing", "till-price-changes", "shifts", "last-year", "cost-overrides",
    "stock-movements", "online-orders",
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
    ("/portal/online", "Online orders", "online"),
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
  due_date TEXT, amount_due REAL, status TEXT NOT NULL, pay_url TEXT, source_as_of TEXT, updated_at TEXT NOT NULL);
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
CREATE TABLE IF NOT EXISTS portal_chat(
  id INTEGER PRIMARY KEY, message TEXT NOT NULL, page TEXT, context TEXT, reply TEXT,
  created_at TEXT NOT NULL, replied_at TEXT, created_by TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS portal_online_days(
  date TEXT PRIMARY KEY, orders INTEGER NOT NULL, total REAL NOT NULL, fees REAL NOT NULL,
  refunds REAL NOT NULL, net REAL NOT NULL, source_as_of TEXT);
CREATE TABLE IF NOT EXISTS portal_online_orders(
  order_no TEXT PRIMARY KEY, ordered_at TEXT NOT NULL, trading_date TEXT NOT NULL,
  wallet TEXT NOT NULL, card_brand TEXT NOT NULL, total REAL NOT NULL, fees REAL NOT NULL,
  refunds REAL NOT NULL, net REAL NOT NULL, source_as_of TEXT);
CREATE INDEX IF NOT EXISTS ix_portal_online_orders_date ON portal_online_orders(trading_date);
"""


def stock():
    import portal_stock
    return portal_stock


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
        key = supplier.casefold() + "\n" + number.casefold()
        pay = clean_pay_url(inv.get("pay_url"))
        c.execute(
            """INSERT INTO portal_invoices(invoice_key,supplier,invoice_number,invoice_date,due_date,amount_due,status,pay_url,source_as_of,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(invoice_key) DO UPDATE SET supplier=excluded.supplier, invoice_number=excluded.invoice_number,
                 invoice_date=excluded.invoice_date, due_date=excluded.due_date, amount_due=excluded.amount_due,
                 status=excluded.status, pay_url=excluded.pay_url, source_as_of=excluded.source_as_of, updated_at=excluded.updated_at""",
            (key, supplier, number,
             need_date(inv.get("date"), "invoice date") if inv.get("date") else None,
             need_date(inv.get("due_date"), "due date") if inv.get("due_date") else None,
             num(inv.get("amount_due"), "amount_due"), status, pay, as_of, stamp()),
        )
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
            """INSERT INTO portal_online_orders(order_no,ordered_at,trading_date,wallet,card_brand,total,fees,refunds,net,source_as_of)
               VALUES(?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(order_no) DO UPDATE SET ordered_at=excluded.ordered_at, trading_date=excluded.trading_date,
                 wallet=excluded.wallet, card_brand=excluded.card_brand, total=excluded.total, fees=excluded.fees,
                 refunds=excluded.refunds, net=excluded.net, source_as_of=excluded.source_as_of""",
            (item["order_no"], item["time"], item["trading_date"], item["wallet"], item["cardBrand"],
             item["total"], item["fees"], item["refunds"], item["net"], as_of),
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
    "online-orders": apply_online_orders,
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


def unpaid_invoices(c, today_d):
    found = []
    for row in rows(c.execute("SELECT * FROM portal_invoices")):
        status = (row["status"] or "").strip().lower()
        amount = float(row["amount_due"] or 0)
        if status in PAID_STATUS or amount == 0 and status == "paid":
            continue
        if status in PAID_STATUS:
            continue
        if status in ("paid", "void", "voided", "deleted", "draft", "cancelled", "canceled", "absent"):
            continue
        row["overdue"] = bool(row["due_date"] and row["due_date"] < today_d.isoformat() and amount > 0)
        found.append(row)
    found.sort(key=lambda r: (not r["overdue"], r["due_date"] or "9999-99-99", r["supplier"].casefold()))
    return found


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


def bar_class(percent):
    n = max(0, min(100, int(round(float(percent) / 5.0) * 5)))
    return f"w{n}"


def _icon(key):
    paths = {
        "overview": '<path d="M4 10.5 12 4l8 6.5V20a1 1 0 0 1-1 1h-5v-6H10v6H5a1 1 0 0 1-1-1z"/>',
        "online": '<path d="M6 7h12l-1 13H7L6 7zM9 7a3 3 0 0 1 6 0"/>',
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
        ("Trading", ("overview", "online", "products", "kitchen", "departments")),
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


def period_controls(base, active, start, end):
    labels = [("month", "This month"), ("lastmonth", "Last month"), ("last7", "Last 7 days"), ("last28", "Last 28 days")]
    buttons = []
    for key, label in labels:
        cls = " on" if key == active else ""
        buttons.append(f'<a class="chip{cls}" href="{base}?period={key}">{label}</a>')
    return f"""<form class="period" method="get" action="{base}">
<div class="chips">{''.join(buttons)}</div>
<div class="row">
<label class="f">From<input type="date" name="from" value="{h(start.isoformat())}" required></label>
<label class="f">To<input type="date" name="to" value="{h(end.isoformat())}" required></label>
<input type="hidden" name="period" value="custom">
<button class="btn" type="submit">Apply</button>
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
<section class="card"><h2>Daily sales</h2>{daily_table(display)}</section>"""


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
    if day_filter:
        order_sql = "SELECT * FROM portal_online_orders WHERE trading_date=? ORDER BY order_no"
        order_rows = rows(c.execute(order_sql, (day_filter,)))
    else:
        order_rows = rows(c.execute(
            "SELECT * FROM portal_online_orders WHERE trading_date>=? AND trading_date<=?",
            (start.isoformat(), end.isoformat()),
        ))
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
    for trading_date in sorted(grouped, reverse=True):
        items = grouped[trading_date]
        gross = sum(float(item["total"] or 0) for item in items)
        opened = " open" if trading_date == (day_filter or max(grouped)) else ""
        lines = []
        for item in items:
            lines.append(
                "<tr><td>" + h(item["order_no"]) + "</td><td>" + h(item["ordered_at"]) + "</td><td>"
                + h(item["wallet"]) + "</td><td>" + h(item["card_brand"] or "—") + "</td><td class=\"num\">"
                + money(item["total"]) + "</td><td class=\"num\">" + money(item["fees"]) + "</td><td class=\"num\">"
                + money(item["refunds"]) + "</td><td class=\"num\">" + money(item["net"]) + "</td></tr>"
            )
        blocks.append(
            f'<details class="shift"{opened}><summary>{h(fmt_date(trading_date))} · {len(items)} '
            f'order{"s" if len(items) != 1 else ""} · {money(gross)}</summary>'
            '<div class="tw"><table><thead><tr><th>Order</th><th>Time</th><th>Wallet</th><th>Card</th>'
            '<th>Gross</th><th>Bopple fees</th><th>Refunds</th><th>Net</th></tr></thead><tbody>'
            + "".join(lines) + "</tbody></table></div></details>"
        )
    if not blocks:
        order_html = '<p class="empty">No orders in this range.</p>'
    else:
        order_html = "".join(blocks)
    clear = ""
    if day_filter:
        clear = f'<a class="chip" href="{_online_query(active, start, end)}">All dates</a>'
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
<section class="card" id="online-order-list"><h2>Orders</h2>
<form class="row" method="get" action="/portal/online">{hidden}
<label class="f">Date<select name="day">{''.join(options)}</select></label>
<button class="btn" type="submit">Show</button></form>
{clear}
{order_html}</section>"""


def page_purchases(c, query):
    through = data_through(c)
    anchor = anchor_day(c)
    try:
        active, start, end = period_bounds(query.get("period"), query.get("from"), query.get("to"), anchor)
        error = ""
    except ValueError as ex:
        active, start, end = period_bounds("month", None, None, anchor)
        error = f'<p class="alert">{h(str(ex))}</p>'
    bills = rows(c.execute(
        "SELECT * FROM portal_bills WHERE bill_date>=? AND bill_date<=? ORDER BY inc_gst DESC",
        (start.isoformat(), end.isoformat()),
    ))
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
    table = '<p class="empty">No supplier bills in this range.</p>' if not body else (
        '<div class="tw"><table><thead><tr><th>Supplier</th><th>Bills</th><th>Inc GST</th><th>Still due</th></tr></thead><tbody>'
        + "".join(body) + f'<tr class="total"><td>Total</td><td class="num">{len(bills)}</td><td class="num">{money(total_inc)}</td><td></td></tr></tbody></table></div>'
    )
    return f"""<div class="ph"><h1>Purchases by supplier</h1>{through_line(through)}
<p>Bills dated in the selected range. This is not a cost-of-goods figure, and fuel direct debits are not in this table.</p></div>
{error}{period_controls('/portal/purchases', active, start, end)}
<section class="card"><h2>{money(total_inc)} inc GST</h2>
<p class="help">{len(bills)} bills. Shop sales in the same dates: {money(shop) if shop_rows else '—'}.</p>{table}</section>
{parity().purchase_extra(c, active, start, end, bills)}"""


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


def page_invoices(c):
    today_d = today()
    found = unpaid_invoices(c, today_d)
    overdue_n = sum(1 for r in found if r["overdue"])
    overdue_sum = sum(r["amount_due"] or 0 for r in found if r["overdue"])
    total = sum(r["amount_due"] or 0 for r in found)
    body = []
    for row in found:
        cls = ' class="overdue"' if row["overdue"] else ""
        pay = ""
        if row["pay_url"]:
            pay = f'<a class="btn sm" href="{h(row["pay_url"])}" target="_blank" rel="noopener noreferrer">Pay</a>'
        due = fmt_date(row["due_date"]) if row["due_date"] else "No due date"
        flag = '<span class="pill bad">Overdue</span>' if row["overdue"] else '<span class="pill">Unpaid</span>'
        body.append(
            f"<tr{cls}><td>{h(row['supplier'])}</td><td>{h(row['invoice_number'])}</td><td>{h(fmt_date(row['invoice_date'])) if row['invoice_date'] else '—'}</td>"
            f"<td>{h(due)}</td><td class=\"num\">{money(row['amount_due'])}</td><td>{flag}</td><td>{pay}</td></tr>"
        )
    table = '<p class="empty">No unpaid invoices.</p>' if not body else (
        '<div class="tw"><table><thead><tr><th>Supplier</th><th>Invoice</th><th>Date</th><th>Due</th><th>Amount due</th><th>Status</th><th></th></tr></thead><tbody>'
        + "".join(body) + "</tbody></table></div>"
    )
    as_of = meta_get(c, "invoices").get("asOf")
    return f"""<div class="ph"><h1>Unpaid invoices</h1>{through_line(data_through(c))}
<p>Pay opens that supplier's own page in a new tab. This portal does not pay anyone, and it does not store card or bank numbers.</p></div>
<section class="grid g3">
<div class="card stat"><div class="lbl">Unpaid</div><b>{money(total)}</b><span>{len(found)} invoice{'s' if len(found) != 1 else ''}</span></div>
<div class="card stat"><div class="lbl">Overdue</div><b>{money(overdue_sum)}</b><span>{overdue_n} past the due date</span></div>
<div class="card stat"><div class="lbl">Invoice list through</div><b class="date">{h(fmt_date(as_of)) if as_of else '—'}</b><span>from the nightly ingest</span></div>
</section>
<section class="card" id="invoice-list">{table}</section>"""


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
    head_cells = []
    for day in payload["days"]:
        cls = "weekend" if day["weekend"] else ""
        if day["publicHoliday"]:
            cls = (cls + " holiday").strip()
        label = day["weekday"] + " " + str(date.fromisoformat(day["date"]).day)
        extra = ""
        if day["publicHoliday"]:
            extra = f'<small>{h(day["publicHoliday"])}</small>'
        elif day["weekend"]:
            extra = "<small>Weekend</small>"
        head_cells.append(f'<th class="{cls}">{label}{extra}</th>')
    body_rows = []
    cards = []
    for person in payload["people"]:
        cells = []
        lines = []
        for day in payload["days"]:
            hours = person["dailyHours"].get(day["date"])
            cls = "weekend" if day["weekend"] else ""
            if day["publicHoliday"]:
                cls = (cls + " holiday").strip()
            shown = hours_text(hours) if hours else "—"
            n = sum(1 for s in person["shifts"] if s["date"] == day["date"])
            if n > 1:
                shown += f" ({n})"
            cells.append(f'<td class="num {cls}">{h(shown)}</td>')
            flag = ""
            if day["publicHoliday"]:
                flag = " · " + day["publicHoliday"]
            elif day["weekend"]:
                flag = " · Weekend"
            lines.append(f"<li><span>{h(day['weekday'])} {h(str(date.fromisoformat(day['date']).day))}{h(flag)}</span><b>{h(shown)}</b></li>")
        body_rows.append(f"<tr><th scope=\"row\">{h(person['name'])}</th>{''.join(cells)}<td class=\"num total\">{h(hours_text(person['totalHours']))}</td></tr>")
        cards.append(f'<article class="card person"><h3>{h(person["name"])}</h3><ul class="days">{"".join(lines)}</ul><p>Week <b>{h(hours_text(person["totalHours"]))}</b></p></article>')
    table = '<p class="empty">No staff yet. Add someone below, then add a shift.</p>' if not payload["people"] else f"""
<div class="week-table tw"><table><thead><tr><th>Staff</th>{''.join(head_cells)}<th>Week</th></tr></thead><tbody>{''.join(body_rows)}</tbody></table></div>
<div class="week-cards">{''.join(cards)}</div>"""
    active_people = [p for p in people_rows(c) if p["active"]]
    options = "".join(f'<option value="{p["id"]}">{h(p["name"])}</option>' for p in active_people)
    shift_forms = []
    for person in payload["people"]:
        for shift in person["shifts"]:
            shift_forms.append(f"""<details class="shift"><summary>{h(person['name'])} · {h(fmt_date(shift['date']))} · {h(shift['start'])}–{h(shift['finish'])} · {h(hours_text(shift['hours']))} h</summary>
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
<p>Week of Monday {h(fmt_date(monday.isoformat()))} to Sunday {h(fmt_date((monday + timedelta(days=6)).isoformat()))}, Australia/Brisbane. A shift that finishes after midnight counts on the day it started. Weekend days and Queensland public holidays are marked. The Brisbane show holiday is not included.</p>
<p class="help">For the pay run. This page does not send hours to Xero or pay anyone. Astron still approves and posts every pay run.</p></div>
{alert}{ok_html}
<div class="weekbar"><a class="btn ghost sm" href="/portal/hours?week={prev_w}">Previous week</a>
<form method="get" action="/portal/hours"><label class="f">Week of<input type="date" name="week" value="{h(monday.isoformat())}"></label><button class="btn sm" type="submit">Show</button></form>
<a class="btn ghost sm" href="/portal/hours?week={next_w}">Next week</a>
<a class="btn sm" href="/portal/hours.csv?week={h(monday.isoformat())}">Download CSV</a></div>
<section class="card"><div class="lbl">Grand total</div><b class="stat" id="grand-total">{h(hours_text(payload['grandTotal']))} h</b>{table}</section>
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


def send_html(handler, code, html, extra=None):
    handler.send(code, html.encode(), "text/html; charset=utf-8", extra=extra or [])


def send_portal(handler, code, html, user=None, extra=None):
    headers = [("X-Robots-Tag", "noindex, nofollow"), ("Content-Security-Policy", CSP)]
    if user:
        age = int(session_hours() * 3600)
        headers.append(cookie_pair(CSRF_COOKIE, user["csrf"], handler, age, False))
    if extra:
        headers.extend(extra)
    send_html(handler, code, html, headers)


def handle_get(handler, parsed):
    path = parsed.path.rstrip("/") or "/"
    if path == "/portal":
        path = "/portal"
    query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
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
    if path == "/api/portal/stock":
        return handle_stock_api(handler)
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
        elif path == "/portal/products":
            html = layout("Products", "products", parity().page_products(c, query), user, _srv().now())
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
            html = layout("Invoices", "invoices", page_invoices(c), user, _srv().now())
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
    send_portal(handler, 200, html, user)


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


def handle_post(handler):
    path = urlparse(handler.path).path.rstrip("/") or "/"
    if path == "/api/portal/ingest":
        return handle_ingest(handler)
    if path == "/api/portal/chat/reply":
        return handle_chat_reply(handler)
    if path == "/api/portal/chat":
        return handle_chat_post(handler)
    if path == "/api/portal/chat/clear":
        return handle_chat_clear(handler)
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
    handler.send(404, {"error": "not found"})


def handle_stock_post(handler, user, form):
    c = db()
    try:
        action = form.get("action") or ""
        try:
            if action == "set_count":
                stock().save_count(c, user, form)
            elif action == "add_manual":
                stock().add_manual(c, user, form)
            elif action == "edit_manual":
                stock().edit_manual(c, user, form)
            elif action == "untrack":
                stock().untrack(c, user, form)
            else:
                handler.send(400, {"error": "bad action"})
                return
        except ValueError as ex:
            html = layout("Stock on hand", "stock", stock().page_stock(c, user, {}, str(ex)), user, _srv().now())
            send_portal(handler, 400, html, user)
            return
        c.commit()
    finally:
        c.close()
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
        body = json.loads(raw.decode("utf-8") or "{}")
    except OverflowError:
        handler.send(413, {"error": "payload too large"})
        return
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


def install():
    srv = _srv()
    c = srv.PgConn(direct=True) if srv.PG else srv.db()
    try:
        c.executescript(SCHEMA)
        c.commit()
    finally:
        c.close()
    bootstrap_admin()
