"""Owner portal: login, ingest, pages, invoices, and staff hours. Uses a temp SQLite file."""
import gzip, http.client, http.server, json, os, re, sys, tempfile, threading, unittest
from datetime import datetime, date
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tools" / "fixtures"
os.environ["DATABASE_URL"] = ""
os.environ["DATABASE_URL_UNPOOLED"] = ""
os.environ["STAFF_PIN"] = "9999"
os.environ["SMTP_HOST"] = ""
os.environ["PORTAL_COOKIE_SECURE"] = "0"
os.environ["PORTAL_INGEST_KEY"] = "test-ingest-key"
os.environ["PORTAL_ADMIN_USER"] = "astron"
os.environ["PORTAL_INSIGHTS_GENERATE"] = ""
os.environ["SILKWOOD_DB"] = str(Path(tempfile.mkdtemp()) / "silkwood.db")
sys.path.insert(0, str(ROOT))
from argon2 import PasswordHasher
PASSWORD = "correct-horse-battery"
ORIGINAL_HASH = PasswordHasher(time_cost=2, memory_cost=19456, parallelism=1).hash(PASSWORD)
os.environ["PORTAL_ADMIN_PASSWORD_HASH"] = ORIGINAL_HASH
import server
import portal

FIXED = datetime(2026, 11, 11, 15, 0, tzinfo=server.TZ)
server.now = lambda: FIXED


def load(name):
    return json.loads((FIXTURES / name).read_text())


class PortalTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        server.STAFF_PIN = "9999"
        server.init()
        cls.httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.H)
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()

    def setUp(self):
        os.environ["PORTAL_ADMIN_PASSWORD_HASH"] = ORIGINAL_HASH
        os.environ["PORTAL_INGEST_KEY"] = "test-ingest-key"
        os.environ["PORTAL_INSIGHTS_GENERATE"] = ""
        c = server.db()
        c.execute("DELETE FROM portal_login_attempts")
        c.execute("DELETE FROM portal_roster_audit")
        c.execute("DELETE FROM portal_roster")
        c.execute("DELETE FROM portal_people")
        c.execute("DELETE FROM portal_contract_entries")
        c.execute("DELETE FROM portal_contractors")
        c.execute("DELETE FROM portal_blobs WHERE dataset='pay-rates'")
        c.execute("DELETE FROM portal_snapshots WHERE dataset='pay-rates'")
        c.execute("DELETE FROM portal_meta WHERE k='pay-rates'")
        c.execute("DELETE FROM portal_invoice_payments")
        c.execute("UPDATE portal_users SET password_hash=? WHERE username='astron'", (ORIGINAL_HASH,))
        c.commit(); c.close()
        self.jar = {}

    def request(self, method, path, body=None, headers=None, cookies=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        hdrs = dict(headers or {})
        if cookies:
            hdrs["Cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items())
        if body is not None and not isinstance(body, bytes):
            body = body.encode()
        conn.request(method, path, body=body, headers=hdrs)
        resp = conn.getresponse()
        raw = resp.read()
        cookies_out = []
        simple = {}
        for key, value in resp.getheaders():
            simple[key] = value
            if key.lower() == "set-cookie":
                cookies_out.append(value)
        conn.close()
        return resp.status, simple, raw, cookies_out

    def merge(self, set_cookies):
        for raw in set_cookies:
            name, rest = raw.split("=", 1)
            value = rest.split(";", 1)[0]
            if "Max-Age=0" in raw or value == "":
                self.jar.pop(name, None)
            else:
                self.jar[name] = value

    def csrf_from(self, html):
        return re.search(r'name="csrf" value="([^"]+)"', html).group(1)

    def login(self, password=PASSWORD, username="astron", ip="127.0.0.1"):
        status, _, body, cookies = self.request("GET", "/portal/login", headers={"X-Forwarded-For": ip})
        self.assertEqual(status, 200)
        self.merge(cookies)
        form = urlencode({"csrf": self.csrf_from(body.decode()), "username": username, "password": password})
        status, hdrs, body, cookies = self.request(
            "POST", "/portal/login", form, {"Content-Type": "application/x-www-form-urlencoded", "X-Forwarded-For": ip}, self.jar
        )
        self.merge(cookies)
        return status, hdrs, body, cookies

    def authed(self, method, path, body=None, headers=None):
        hdrs = dict(headers or {})
        if body is not None and "Content-Type" not in hdrs:
            hdrs["Content-Type"] = "application/x-www-form-urlencoded"
        return self.request(method, path, body, hdrs, self.jar)

    def ingest(self, dataset, data, key="test-ingest-key"):
        raw = json.dumps({"dataset": dataset, "data": data}).encode()
        return self.request("POST", "/api/portal/ingest", raw, {
            "Content-Type": "application/json",
            "Authorization": "Bearer " + key,
            "Content-Length": str(len(raw)),
        })

    def test_logged_out_pages_redirect_and_staff_routes_stay(self):
        for path in ("/portal", "/portal/day", "/portal/invoices", "/portal/hours", "/portal/purchases", "/portal/fuel", "/portal/dips", "/portal/hours.csv",
                     "/portal/products", "/portal/pricing", "/portal/till-prices", "/portal/departments", "/portal/kitchen",
                     "/portal/shifts", "/portal/commission", "/portal/notes", "/portal/stock", "/portal/exceptions"):
            status, hdrs, body, _ = self.request("GET", path, headers={"X-Staff-Pin": "9999"})
            self.assertEqual(status, 302, path)
            self.assertEqual(hdrs.get("Location"), "/portal/login")
            self.assertNotIn(b"167,270", body)
        status, _, body, _ = self.request("GET", "/api/portal/staff-hours")
        self.assertEqual(status, 401)
        self.assertNotIn(b"people", body)
        status, _, body, _ = self.request("GET", "/order")
        self.assertEqual(status, 200)
        self.assertIn(b"Silkwood Takeaway", body)
        status, _, body, _ = self.request("GET", "/api/day")
        self.assertEqual(status, 401)
        status, hdrs, _, _ = self.request("GET", "/healthz")
        self.assertEqual(status, 200)
        self.assertIn("payment=()", hdrs.get("Permissions-Policy", ""))
        sw = (ROOT / "static" / "sw.js").read_text()
        self.assertLess(sw.index("'/portal'"), sw.index("r.mode==='navigate'"))

    def test_login_cookie_ingest_and_pages(self):
        status, hdrs, body, cookies = self.login()
        self.assertEqual(status, 302)
        self.assertEqual(hdrs.get("Location"), "/portal")
        session = next(c for c in cookies if c.startswith("silkwood_portal="))
        self.assertIn("HttpOnly", session)
        self.assertIn("SameSite=Lax", session)
        self.assertNotIn("Secure", session)
        self.assertIn("silkwood_portal", self.jar)
        for name, data in (
            ("sales", load("sales.json")),
            ("purchases", load("purchases.json")),
            ("fuel-commission", load("fuel-commission.json")),
            ("fuel-dips", load("fuel-dips.json")),
            ("insights", load("insights.json")),
            ("invoices", load("invoices.json")),
        ):
            code, _, raw, _ = self.ingest(name, data)
            self.assertEqual(code, 200, raw.decode()[:300])
            self.assertTrue(json.loads(raw)["ok"])
        # A second copy of the same day is an upsert, not a second row.
        code, _, raw, _ = self.ingest("insights", load("insights.json"))
        self.assertEqual(code, 200)
        c = server.db()
        n = c.execute("SELECT COUNT(*) AS n FROM portal_insights WHERE date='2026-11-10'").fetchone()["n"]
        oct_rows = portal.rows(c.execute("SELECT total FROM portal_sales_days WHERE date>='2026-10-01' AND date<='2026-10-31'"))
        c.close()
        self.assertEqual(n, 1)
        status, hdrs, body, _ = self.authed("GET", "/portal")
        html = body.decode()
        self.assertEqual(status, 200)
        self.assertIn("text/html", hdrs["Content-Type"])
        self.assertIn("Data through 10 Nov 2026", html)
        self.assertIn("AI insights", html)
        self.assertIn("Through 10 Nov, fuel commission is ahead of the $9,500 pace", html)
        self.assertIn("Shop GP", html)
        self.assertIn("Fuel vs shop", html)
        self.assertIn("November 2026 · month-end projection", html)
        self.assertIn("$9,500.00", html)
        self.assertIn("Content-Security-Policy", hdrs)
        self.assertIn("noindex", hdrs.get("X-Robots-Tag", ""))
        status, _, body, _ = self.authed("GET", "/portal?period=lastmonth")
        last = body.decode()
        self.assertIn("November 2026 · month-end projection", last)
        self.assertIn(portal.money(sum(r["total"] or 0 for r in oct_rows)), last)
        status, _, body, _ = self.authed("GET", "/portal?period=custom&from=2026-11-01&to=2026-11-01")
        self.assertIn("1 completed sales day", body.decode())
        status, _, body, _ = self.authed("GET", "/portal/purchases")
        self.assertIn("Purchases by supplier", body.decode())
        self.assertIn("Tully Pies", body.decode())
        status, _, body, _ = self.authed("GET", "/portal/fuel")
        self.assertIn("Fuel commission", body.decode())
        self.assertIn("Commission ex GST", body.decode())
        status, _, body, _ = self.authed("GET", "/portal/dips")
        dips = body.decode()
        self.assertIn("Pump readings", dips)
        self.assertIn("Unleaded", dips)
        self.assertIn("10 Nov 2026", dips)
        status, _, body, _ = self.authed("GET", "/portal/invoices")
        invoices = body.decode()
        self.assertIn("Unpaid invoices", invoices)
        self.assertIn("Bega Dairy", invoices)
        self.assertIn('class="overdue"', invoices)
        self.assertIn('href="https://example.com/pay/bega/577803222"', invoices)
        self.assertIn('target="_blank"', invoices)
        self.assertIn("rel=\"noopener noreferrer\"", invoices)
        self.assertNotIn("Telstra", invoices)
        self.assertNotIn("4242", invoices)
        status, _, body, _ = self.authed("GET", "/api/day", headers={"X-Staff-Pin": ""})
        self.assertEqual(status, 401)
        status, hdrs, body, cookies = self.authed("POST", "/portal/logout", urlencode({"csrf": self.csrf_from(self.authed("GET", "/portal")[2].decode())}))
        self.assertEqual(status, 302)
        self.assertEqual(hdrs.get("Location"), "/portal/login")
        self.merge(cookies)
        status, hdrs, _, _ = self.authed("GET", "/portal")
        self.assertEqual(status, 302)

    def test_ingest_auth_upsert_and_secrets(self):
        code, _, body, _ = self.ingest("sales", {"periods": []}, key="nope")
        self.assertEqual(code, 401)
        self.assertNotIn(b"nope", body)
        code, _, _, _ = self.request("POST", "/api/portal/ingest", b"{}", {"Content-Type": "application/json", "Cookie": "silkwood_portal=abc"})
        self.assertEqual(code, 401)
        os.environ["PORTAL_INGEST_KEY"] = ""
        try:
            code, _, body, _ = self.ingest("sales", load("sales.json"), key="")
            self.assertEqual(code, 503)
            self.assertNotIn(b"postgres", body.lower())
        finally:
            os.environ["PORTAL_INGEST_KEY"] = "test-ingest-key"
        code, _, body, _ = self.ingest("sales", {
            "asOf": "2026-08-02", "latestSalesDate": "2026-08-02",
            "periods": [{"daily": [
                {"date": "2026-08-01", "total": 10, "shop": 4, "fuel": 6, "gp": 1},
                {"date": "2026-08-02", "total": 20, "shop": 8, "fuel": 12, "gp": 2},
            ]}],
        })
        self.assertEqual(code, 200, body)
        code, _, body, _ = self.ingest("sales", {
            "asOf": "2026-08-03", "latestSalesDate": "2026-08-03",
            "periods": [{"daily": [
                {"date": "2026-08-02", "total": 21, "shop": 9, "fuel": 12, "gp": 2},
                {"date": "2026-08-03", "total": 5, "shop": 5, "fuel": 0, "gp": 1},
            ]}],
        })
        self.assertEqual(code, 200, body)
        c = server.db()
        got = {r["date"]: r["total"] for r in c.execute("SELECT date, total FROM portal_sales_days WHERE date LIKE '2026-08-%'")}
        c.close()
        self.assertEqual(got["2026-08-01"], 10)
        self.assertEqual(got["2026-08-02"], 21)
        self.assertEqual(got["2026-08-03"], 5)
        code, _, body, _ = self.ingest("invoices", {"date": "2026-11-11", "invoices": [{
            "supplier": "Card Co", "invoice_number": "1", "date": "2026-11-01", "due_date": "2026-11-08",
            "amount_due": 10, "status": "unpaid", "card_number": "4242424242424242",
        }]})
        self.assertEqual(code, 400)
        self.assertIn(b"card", body.lower())
        c = server.db()
        self.assertIsNone(c.execute("SELECT 1 FROM portal_invoices WHERE supplier='Card Co'").fetchone())
        c.close()
        code, _, _, _ = self.ingest("invoices", {"date": "2026-11-11", "invoices": [{
            "supplier": "Pan Co", "invoice_number": "4242424242424242", "date": "2026-11-01",
            "due_date": "2026-11-08", "amount_due": 10, "status": "unpaid",
        }]})
        self.assertEqual(code, 400)
        bad = self.ingest("insights", {"date": "2026-11-01", "headline": "<script>alert(1)</script>", "bullets": ["ok"]})
        self.assertEqual(bad[0], 200)
        self.login()
        html = self.authed("GET", "/portal")[2].decode()
        if "alert(1)" in html:
            self.assertNotIn("<script>alert(1)</script>", html)
            self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", html)
        boxed = portal.insights_box({"date": "2026-11-01", "headline": "<script>alert(1)</script>", "source": "box", "bullet_list": ["ok"]})
        self.assertNotIn("<script>alert(1)</script>", boxed)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", boxed)

    def test_lockout_csrf_password_and_session_timeout(self):
        ip = "198.51.100.20"
        for _ in range(5):
            status, _, body, _ = self.login("wrong-password", ip=ip)
            self.assertEqual(status, 401)
            self.assertIn(b"not right", body)
        status, _, body, _ = self.login(PASSWORD, ip=ip)
        self.assertEqual(status, 429)
        self.assertIn(b"Too many attempts", body)
        status, _, _, _ = self.login(PASSWORD, ip="198.51.100.21")
        self.assertEqual(status, 302, "lockout is per network, so the owner can still sign in")
        self.login()
        status, _, body, _ = self.authed("POST", "/portal/logout", urlencode({"csrf": "nope"}))
        self.assertEqual(status, 403)
        status, _, _, _ = self.authed("GET", "/portal")
        self.assertEqual(status, 200)
        page = self.authed("GET", "/portal/password")[2].decode()
        csrf = self.csrf_from(page)
        bad = urlencode({"csrf": csrf, "current": "nope", "new": "a-new-password", "confirm": "a-new-password"})
        self.assertEqual(self.authed("POST", "/portal/password", bad)[0], 400)
        mismatch = urlencode({"csrf": csrf, "current": PASSWORD, "new": "a-new-password", "confirm": "other-password"})
        self.assertEqual(self.authed("POST", "/portal/password", mismatch)[0], 400)
        changed = urlencode({"csrf": csrf, "current": PASSWORD, "new": "a-new-password", "confirm": "a-new-password"})
        status, hdrs, _, _ = self.authed("POST", "/portal/password", changed)
        self.assertEqual(status, 302)
        self.assertEqual(hdrs.get("Location"), "/portal/password?ok=1")
        self.jar = {}
        self.assertEqual(self.login(PASSWORD)[0], 401)
        self.assertEqual(self.login("a-new-password")[0], 302)
        # Env hash is unchanged, so a restart must not put the old password back.
        portal.bootstrap_admin()
        self.jar = {}
        self.assertEqual(self.login("a-new-password")[0], 302)
        self.jar = {}
        self.assertEqual(self.login("a-new-password")[0], 302)
        c = server.db()
        c.execute("UPDATE portal_sessions SET expires_at='2020-01-01 00:00:00'")
        c.commit(); c.close()
        status, hdrs, _, _ = self.authed("GET", "/portal/invoices")
        self.assertEqual(status, 302)
        self.assertEqual(hdrs.get("Location"), "/portal/login")

    def test_staff_hours_week_csv_and_read_api(self):
        self.login()
        c = server.db()
        c.execute(
            "INSERT INTO shifts(date,staff,area,start_at,end_at,hours) VALUES('2026-11-09','Ops Only','shop','2026-11-09 09:00:00','2026-11-09 17:00:00',8)"
        )
        c.commit(); c.close()
        page = self.authed("GET", "/portal/hours?week=2026-11-09")[2].decode()
        self.assertIn("Staff hours", page)
        self.assertIn("does not send hours to Xero", page)
        self.assertNotIn("Ops Only", page)
        csrf = self.csrf_from(page)
        week = "2026-11-09"

        def post(fields):
            fields = dict(fields)
            fields["csrf"] = csrf
            fields["week"] = week
            return self.authed("POST", "/portal/hours", urlencode(fields))

        self.assertEqual(post({"action": "save_shift", "person_id": "1", "work_date": "2026-11-09", "start_time": "09:00", "finish_time": "17:00", "break_minutes": "0"})[0], 400)
        status, hdrs, _, _ = post({"action": "add_staff", "name": "Alex"})
        self.assertEqual(status, 302, hdrs)
        page = self.authed("GET", "/portal/hours?week=2026-11-09")[2].decode()
        csrf = self.csrf_from(page)
        person = re.search(r'name="person_id" value="(\d+)"', page).group(1)
        status, _, body, _ = post({
            "action": "save_shift", "person_id": person, "work_date": "2026-11-10",
            "start_time": "22:00", "finish_time": "02:00", "break_minutes": "0", "notes": "=cmd",
        })
        self.assertEqual(status, 302, body)
        status, _, _, _ = post({
            "action": "save_shift", "person_id": person, "work_date": "2026-11-11",
            "start_time": "18:00", "finish_time": "02:30", "break_minutes": "30", "notes": "close",
        })
        self.assertEqual(status, 302)
        page = self.authed("GET", "/portal/hours?week=2026-11-09")[2].decode()
        self.assertIn("Grand total", page)
        self.assertIn('id="grand-total">12 h', page)
        self.assertIn("Weekend", page)
        self.assertIn("added Alex", page)
        self.assertIn("astron", page)
        shift_id = re.search(r'name="shift_id" value="(\d+)"', page).group(1)
        csrf = self.csrf_from(page)
        status, _, _, _ = post({
            "action": "save_shift", "shift_id": shift_id, "person_id": person, "work_date": "2026-11-10",
            "start_time": "21:00", "finish_time": "06:00", "break_minutes": "0", "notes": "=cmd",
        })
        self.assertEqual(status, 302)
        page = self.authed("GET", "/portal/hours?week=2026-11-09")[2].decode()
        self.assertIn("start 22:00 → 21:00", page)
        csrf = self.csrf_from(page)
        self.assertEqual(post({"action": "delete_shift", "shift_id": shift_id})[0], 302)
        page = self.authed("GET", "/portal/hours?week=2026-11-09")[2].decode()
        self.assertIn("deleted Alex", page)
        self.assertNotIn("<summary>Alex · 10 Nov 2026", page)
        self.assertIn('id="grand-total">8 h', page)
        status, hdrs, body, _ = self.authed("GET", "/portal/hours.csv?week=2026-11-09")
        self.assertEqual(status, 200)
        self.assertIn("text/csv", hdrs["Content-Type"])
        self.assertIn("silkwood-hours-2026-11-09.csv", hdrs["Content-Disposition"])
        text = body.decode("utf-8-sig")
        self.assertIn("Alex", text)
        self.assertIn("18:00", text)
        self.assertIn("close", text)
        self.assertIn("8.00", text)
        holiday = self.authed("GET", "/portal/hours?week=2026-05-04")[2].decode()
        self.assertIn("Labour Day", holiday)
        anzac = self.authed("GET", "/portal/hours?week=2026-04-20")[2].decode()
        self.assertIn("Anzac Day", anzac)
        code, _, body, _ = self.request("GET", "/api/portal/staff-hours?week=2026-11-11", headers={"Authorization": "Bearer test-ingest-key"})
        payload = json.loads(body)
        self.assertEqual(code, 200)
        self.assertEqual(payload["week"], "2026-11-09")
        self.assertEqual(payload["timezone"], "Australia/Brisbane")
        self.assertIn("Nothing is sent to Xero", payload["note"])
        self.assertEqual(payload["grandTotal"], 8)
        self.assertTrue(payload["days"][5]["weekend"])
        self.assertIsNone(payload["days"][0]["publicHoliday"])
        code, _, _, _ = self.request("GET", "/api/portal/staff-hours?week=2026-11-09", headers={"Authorization": "Bearer wrong"})
        self.assertEqual(code, 401)
        code, _, _, _ = self.request("GET", "/portal/hours.csv?week=2026-11-09", headers={"Authorization": "Bearer test-ingest-key"})
        self.assertEqual(code, 302)
        code, _, body, _ = self.authed("GET", "/api/portal/staff-hours?week=2026-04-25")
        holiday_days = {d["date"]: d["publicHoliday"] for d in json.loads(body)["days"]}
        self.assertEqual(holiday_days["2026-04-25"], "Anzac Day")
        # No payroll endpoint exists.
        self.assertEqual(self.authed("POST", "/api/portal/payroll", "{}")[0], 404)

    def test_pay_rates_wages_and_contractors(self):
        from decimal import Decimal, ROUND_HALF_UP
        self.assertEqual(portal.pay_kind("2026-11-09", "06:00"), "ordinary")
        self.assertEqual(portal.pay_kind("2026-11-14", "08:00"), "saturday")
        self.assertEqual(portal.pay_kind("2026-11-15", "09:00"), "sunday")
        self.assertEqual(portal.pay_kind("2026-04-25", "09:00"), "publicHoliday")
        self.assertEqual(portal.pay_kind("2026-05-04", "09:00"), "publicHoliday")
        self.assertEqual(portal.pay_kind("2026-12-24", "17:00"), "ordinary")
        self.assertEqual(portal.pay_kind("2026-12-24", "18:00"), "publicHoliday")
        sample = portal.shift_wage(7.5, {"ordinary": "31.25", "superRate": "0.12"}, "ordinary")
        self.assertEqual(sample["excl"], Decimal("234.38"))
        self.assertEqual(sample["incl"], Decimal("262.51"))
        sales = portal.till_sales_ex_gst([
            {"shop": "6804.47", "fuel": "6159.77", "total": "12964.24"},
            {"shop": "6941.08", "fuel": "8759.49", "total": "15700.57"},
        ])
        self.assertEqual(sales["total_ex"], Decimal("26058.91"))
        self.assertEqual(sales["total_inc"], Decimal("28664.81"))

        self.login()
        # Lock the two till days this overview week uses, in case another test changed them.
        self.assertEqual(self.ingest("sales", {"asOf": "2026-11-11", "latestSalesDate": "2026-11-11", "periods": [{"daily": [
            {"date": "2026-11-09", "total": 12964.24, "shop": 6804.47, "fuel": 6159.77, "gp": 1},
            {"date": "2026-11-10", "total": 15700.57, "shop": 6941.08, "fuel": 8759.49, "gp": 1},
        ]}]})[0], 200)
        page = self.authed("GET", "/portal/hours?week=2026-11-09")[2].decode()
        csrf = self.csrf_from(page)

        def post(fields, week="2026-11-09"):
            fields = dict(fields)
            fields["csrf"] = csrf
            fields["week"] = week
            return self.authed("POST", "/portal/hours", urlencode(fields))

        def refresh(week="2026-11-09"):
            nonlocal csrf
            html = self.authed("GET", f"/portal/hours?week={week}")[2].decode()
            csrf = self.csrf_from(html)
            return html

        self.assertEqual(post({"action": "add_staff", "name": "Alex"})[0], 302)
        page = refresh()
        alex = re.search(r'name="person_id" value="(\d+)"', page).group(1)
        self.assertEqual(post({"action": "add_staff", "name": "Sam"})[0], 302)
        page = refresh()
        ids = re.findall(r'name="person_id" value="(\d+)"', page)
        sam = [i for i in ids if i != alex][0]
        self.assertEqual(post({
            "action": "save_shift", "person_id": alex, "work_date": "2026-11-09",
            "start_time": "06:00", "finish_time": "14:30", "break_minutes": "30",
        })[0], 302)
        self.assertEqual(post({
            "action": "save_shift", "person_id": alex, "work_date": "2026-11-14",
            "start_time": "08:00", "finish_time": "13:00", "break_minutes": "0",
        })[0], 302)
        self.assertEqual(post({
            "action": "save_shift", "person_id": sam, "work_date": "2026-11-11",
            "start_time": "09:00", "finish_time": "17:00", "break_minutes": "30",
        })[0], 302)
        page = refresh()
        self.assertIn('id="grand-total">20.5 h', page)
        self.assertIn("No rate yet", page)
        self.assertIn("No pay rates yet", page)
        home = self.authed("GET", "/portal")[2].decode()
        week = home.split('id="wages-week"', 1)[1].split('id="wages-month"', 1)[0]
        self.assertIn("<b>—</b>", week)
        self.assertIn("No pay rates yet", week)
        self.assertNotIn("3.0%", week)

        denied = self.ingest("pay-rates", {"employees": []}, key="nope")
        self.assertEqual(denied[0], 401)
        bad_super = self.ingest("pay-rates", {"employees": [{
            "name": "Alex", "ordinary": 32, "saturday": 48, "sunday": 64, "publicHoliday": 80, "superRate": 12,
        }]})
        self.assertEqual(bad_super[0], 400)
        self.assertIn(b"0.12", bad_super[2])
        dup = self.ingest("pay-rates", {"employees": [
            {"name": "Alex", "ordinary": 32, "saturday": 48, "sunday": 64, "publicHoliday": 80, "superRate": 0.12},
            {"name": "alex", "ordinary": 32, "saturday": 48, "sunday": 64, "publicHoliday": 80, "superRate": 0.12},
        ]})
        self.assertEqual(dup[0], 400)
        card = self.ingest("pay-rates", {"employees": [{
            "name": "Alex 4242424242424242", "ordinary": 32, "saturday": 48, "sunday": 64,
            "publicHoliday": 80, "superRate": 0.12,
        }]})
        self.assertEqual(card[0], 400)

        rates = {"asOf": "2026-11-11", "employees": [
            {"name": "Alex", "ordinary": 32, "saturday": 48, "sunday": 64, "publicHoliday": 80, "superRate": 0.12},
            {"name": "sam", "ordinary": 30, "saturday": 45, "sunday": 60, "publicHoliday": 75, "superRate": 0.12},
        ]}
        code, _, raw, _ = self.ingest("pay-rates", rates)
        self.assertEqual(code, 200, raw)
        self.assertEqual(json.loads(raw)["employees"], 2)
        # Re-posting replaces the list. Sam drops out, then both come back.
        only_alex = {"asOf": "2026-11-11", "employees": rates["employees"][:1]}
        self.assertEqual(self.ingest("pay-rates", only_alex)[0], 200)
        page = refresh()
        self.assertIn("No rate yet for Sam (7.5 h)", page)
        self.assertIn("$256.00 excl", page)
        self.assertNotIn("$225.00 excl", page)
        self.assertEqual(self.ingest("pay-rates", rates)[0], 200)

        page = refresh()
        self.assertIn("$256.00 excl", page)
        self.assertIn("$286.72 incl", page)
        self.assertIn("$240.00 excl", page)
        self.assertIn("Saturday", page)
        self.assertIn("Weekday $32.00/h", page)
        self.assertIn("Weekday $30.00/h", page)
        self.assertIn("$225.00 excl", page)
        self.assertIn("$252.00 incl", page)
        self.assertIn('id="employee-wages-excl">$721.00', page)
        self.assertIn('id="employee-wages-incl">$807.52', page)
        self.assertIn("incl. 12% super", page)
        self.assertNotIn("No rate yet", page)

        self.assertEqual(post({
            "action": "save_contract", "name": "Pat", "work_date": "2026-11-11",
            "pay_style": "hourly", "hours": "4", "hourly_rate": "40",
        })[0], 302)
        self.assertEqual(post({
            "action": "save_contract", "name": "Pat", "work_date": "2026-11-14",
            "pay_style": "hourly", "hours": "2",
        })[0], 302)
        self.assertEqual(post({
            "action": "save_contract", "name": "Riley", "work_date": "2026-11-10",
            "pay_style": "flat", "flat_amount": "90",
        })[0], 302)
        page = refresh()
        self.assertIn('id="contractor-wages-cost">$330.00', page)
        self.assertIn('id="combined-wages-excl">$1,051.00', page)
        self.assertIn('id="combined-wages-incl">$1,137.52', page)
        self.assertIn(">Contract<", page)
        self.assertIn("Pat", page)
        self.assertIn("4 h × $40.00/h", page)
        self.assertIn("2 h × $40.00/h", page)
        self.assertIn("Flat $90.00", page)
        self.assertIn("no super", page)
        self.assertIn('id="grand-total">20.5 h', page)
        self.assertIn("not in the hours CSV", page)

        home = self.authed("GET", "/portal")[2].decode()
        self.assertIn('id="wages-vs-sales"', home)
        self.assertIn("divided by 1.1", home)
        self.assertIn("excl. GST", home)
        week = home.split('id="wages-week"', 1)[1].split('id="wages-month"', 1)[0]
        month = home.split('id="wages-month"', 1)[1].split("</section>", 1)[0]
        self.assertIn("<b>3.0%</b>", week)
        self.assertIn("$788.72", week)
        self.assertIn("$28,664.81", week)
        self.assertIn("$26,058.91", week)
        self.assertIn("inc GST", week)
        c = server.db()
        month_rows = c.execute(
            "SELECT shop, fuel, total FROM portal_sales_days WHERE date>='2026-11-01' AND date<='2026-11-11'"
        ).fetchall()
        c.close()
        shop = sum((Decimal(str(r["shop"] or 0)) for r in month_rows), Decimal(0))
        fuel = sum((Decimal(str(r["fuel"] or 0)) for r in month_rows), Decimal(0))
        month_ex = (shop / Decimal("1.1")).quantize(Decimal("0.01"), ROUND_HALF_UP)
        month_ex += (fuel / Decimal("1.1")).quantize(Decimal("0.01"), ROUND_HALF_UP)
        month_pct = (Decimal("788.72") / month_ex * Decimal(100)).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
        self.assertIn(f"<b>{month_pct}%</b>", month)
        self.assertIn("First of the month through today", month)

        logged_out = self.request("GET", "/portal")
        self.assertEqual(logged_out[0], 302)
        self.assertNotIn(b"788.72", logged_out[2])
        self.assertNotIn(b"pay-rates", logged_out[2].lower())
        code, _, body, _ = self.request(
            "GET", "/api/portal/staff-hours?week=2026-11-09",
            headers={"Authorization": "Bearer test-ingest-key"},
        )
        payload = json.loads(body)
        self.assertEqual(code, 200)
        self.assertEqual(payload["grandTotal"], 20.5)
        raw_api = body.decode()
        self.assertNotIn("Pat", raw_api)
        self.assertNotIn("Riley", raw_api)
        self.assertNotIn("32.00", raw_api)
        self.assertNotIn("superRate", raw_api)
        self.assertNotIn("contract", raw_api.lower())
        status, _, csv_body, _ = self.authed("GET", "/portal/hours.csv?week=2026-11-09")
        self.assertEqual(status, 200)
        csv_text = csv_body.decode("utf-8-sig")
        self.assertIn("Alex", csv_text)
        self.assertIn("Sam", csv_text)
        self.assertNotIn("Pat", csv_text)
        self.assertNotIn("Riley", csv_text)
        self.assertNotIn("32.00", csv_text)
        self.assertNotIn("90.00", csv_text)

        self.assertEqual(post({"action": "add_staff", "name": "Jo"})[0], 302)
        page = refresh()
        ids = re.findall(r'name="person_id" value="(\d+)"', page)
        jo = [i for i in ids if i not in (alex, sam)][0]
        self.assertEqual(post({
            "action": "save_shift", "person_id": jo, "work_date": "2026-11-11",
            "start_time": "12:00", "finish_time": "16:00", "break_minutes": "0",
        })[0], 302)
        page = refresh()
        self.assertIn("No rate yet for Jo (4 h)", page)
        self.assertIn('id="employee-wages-excl">$721.00', page)
        home = self.authed("GET", "/portal")[2].decode()
        week = home.split('id="wages-week"', 1)[1].split('id="wages-month"', 1)[0]
        self.assertIn("<b>—</b>", week)
        self.assertIn("No rate yet for Jo", week)
        self.assertNotIn("<b>3.0%</b>", week)
        code, _, body, _ = self.request(
            "GET", "/api/portal/staff-hours?week=2026-11-09",
            headers={"Authorization": "Bearer test-ingest-key"},
        )
        self.assertEqual(code, 200)
        self.assertIn("Jo", body.decode())
        self.assertNotIn("32.00", body.decode())
        self.assertNotIn("Pat", body.decode())

        self.assertEqual(post({
            "action": "save_shift", "person_id": alex, "work_date": "2026-05-04",
            "start_time": "09:00", "finish_time": "17:00", "break_minutes": "0",
        }, week="2026-05-04")[0], 302)
        holiday = refresh("2026-05-04")
        self.assertIn("Public holiday $80.00/h", holiday)
        self.assertIn("$640.00 excl", holiday)
        self.assertIn("$716.80 incl", holiday)
        self.assertEqual(post({
            "action": "save_shift", "person_id": alex, "work_date": "2026-04-25",
            "start_time": "09:00", "finish_time": "17:00", "break_minutes": "0",
        }, week="2026-04-20")[0], 302)
        anzac = refresh("2026-04-20")
        self.assertIn("Public holiday $80.00/h", anzac)
        self.assertNotIn("Saturday $48.00/h", anzac)
        self.assertEqual(post({
            "action": "save_shift", "person_id": alex, "work_date": "2026-12-24",
            "start_time": "09:00", "finish_time": "13:00", "break_minutes": "0",
        }, week="2026-12-21")[0], 302)
        self.assertEqual(post({
            "action": "save_shift", "person_id": alex, "work_date": "2026-12-24",
            "start_time": "18:00", "finish_time": "22:00", "break_minutes": "0",
        }, week="2026-12-21")[0], 302)
        eve = refresh("2026-12-21")
        self.assertIn("Weekday $32.00/h", eve)
        self.assertIn("$128.00 excl", eve)
        self.assertIn("Public holiday $80.00/h", eve)
        self.assertIn("$320.00 excl", eve)

    def test_contract_days_and_name_breakdown(self):
        self.login()

        def post(fields, week="2026-11-09"):
            page = self.authed("GET", f"/portal/hours?week={week}")[2].decode()
            fields = dict(fields)
            fields["csrf"] = self.csrf_from(page)
            fields["week"] = week
            return self.authed("POST", "/portal/hours", urlencode(fields))

        def show(week="2026-11-09"):
            return self.authed("GET", f"/portal/hours?week={week}")[2].decode()

        self.assertEqual(post({"action": "add_staff", "name": "Alex"})[0], 302)
        page = show()
        alex = re.search(r'name="person_id" value="(\d+)"', page).group(1)
        self.assertEqual(post({
            "action": "save_shift", "person_id": alex, "work_date": "2026-11-09",
            "start_time": "06:00", "finish_time": "14:30", "break_minutes": "30",
        })[0], 302)
        self.assertEqual(self.ingest("pay-rates", {"asOf": "2026-11-11", "employees": [
            {"name": "Alex", "ordinary": 32, "saturday": 48, "sunday": 64, "publicHoliday": 80, "superRate": 0.12},
        ]})[0], 200)
        self.assertEqual(post({
            "action": "save_contract", "name": "Pat", "work_date": "2026-11-11",
            "pay_style": "hourly", "hours": "4", "hourly_rate": "40",
        })[0], 302)
        self.assertEqual(post({
            "action": "save_contract", "name": "Pat", "work_date": "2026-11-14",
            "pay_style": "hourly", "hours": "2",
        })[0], 302)
        self.assertEqual(post({
            "action": "save_contract", "name": "Riley", "work_date": "2026-11-10",
            "pay_style": "flat", "flat_amount": "90",
        })[0], 302)
        page = show()
        self.assertIn("<h2>Pay run</h2>", page)
        self.assertIn('id="contract-week"', page)
        self.assertIn('id="contract-grand-total">6 h', page)
        self.assertIn('id="contractor-wages-cost">$330.00', page)
        self.assertIn('id="grand-total">8 h', page)
        self.assertIn("not in the hours CSV", page)
        alex_id = re.search(r'for="open-pay-(\d+)">Alex<span', page).group(1)
        pay_row = page.split(f'id="wage-person-{alex_id}"', 1)[1].split("</tr>", 1)[0]
        self.assertNotIn("day-input", pay_row)
        pay_break = page.split(f'id="pay-{alex_id}-break"', 1)[1].split("</tr>", 1)[0]
        self.assertIn("Mon 9 Nov 2026", pay_break)
        self.assertIn("8 h", pay_break)
        self.assertIn("Weekday $32.00/h", pay_break)
        self.assertIn("$256.00 excl · $286.72 incl super", pay_break)
        self.assertIn(">Total<", pay_break)
        self.assertIn('id="pay-card-%s"' % alex_id, page)
        pat = re.search(r'for="open-contract-(\d+)">Pat<span', page).group(1)
        riley = re.search(r'for="open-contract-(\d+)">Riley<span', page).group(1)
        wed = page.split(f'id="contract-cell-{pat}-2026-11-11"', 1)[1].split("</td>", 1)[0]
        self.assertIn('data-entry="hourly"', wed)
        self.assertIn('value="4.00"', wed)
        self.assertIn("$160.00 · no super", wed)
        sat = page.split(f'id="contract-cell-{pat}-2026-11-14"', 1)[1].split("</td>", 1)[0]
        self.assertIn('value="2.00"', sat)
        self.assertIn("$80.00 · no super", sat)
        flat = page.split(f'id="contract-cell-{riley}-2026-11-10"', 1)[1].split("</td>", 1)[0]
        self.assertIn('data-entry="flat"', flat)
        self.assertIn("Flat $90.00", flat)
        self.assertNotIn('name="hours_', flat)
        pat_break = page.split(f'id="contract-{pat}-break"', 1)[1].split("</tr>", 1)[0]
        self.assertIn("Wed 11 Nov 2026", pat_break)
        self.assertIn("$40.00/h", pat_break)
        self.assertIn("$160.00 · no super", pat_break)
        self.assertIn("6 h", pat_break)
        self.assertIn("$240.00 · no super", pat_break)
        self.assertIn('id="contract-card-%s"' % pat, page)
        self.assertIn('id="contract-days-add"', page)

        self.assertEqual(post({
            "action": "save_contract_days", "contractor_id": pat, "hourly_rate": "40",
            "hours_2026-11-11": "5",
        })[0], 302)
        page = show()
        wed = page.split(f'id="contract-cell-{pat}-2026-11-11"', 1)[1].split("</td>", 1)[0]
        self.assertIn('value="5.00"', wed)
        self.assertIn("$200.00 · no super", wed)
        self.assertIn('value="2.00"', page.split(f'id="contract-cell-{pat}-2026-11-14"', 1)[1].split("</td>", 1)[0])
        self.assertIn('id="contractor-wages-cost">$370.00', page)

        self.assertEqual(post({
            "action": "save_contract_days", "contractor_id": pat,
            "hours_2026-11-09": "3",
        })[0], 302)
        page = show()
        mon = page.split(f'id="contract-cell-{pat}-2026-11-09"', 1)[1].split("</td>", 1)[0]
        self.assertIn('value="3.00"', mon)
        self.assertIn("$120.00 · no super", mon)
        self.assertIn('id="contractor-wages-cost">$490.00', page)
        self.assertIn('id="contract-grand-total">10 h', page)

        self.assertEqual(post({
            "action": "save_contract_days", "contractor_id": pat, "hours_2026-11-14": "",
        })[0], 302)
        page = show()
        sat = page.split(f'id="contract-cell-{pat}-2026-11-14"', 1)[1].split("</td>", 1)[0]
        self.assertIn('data-entry="empty"', sat)
        self.assertNotIn('value="2.00"', sat)
        self.assertIn('id="contractor-wages-cost">$410.00', page)

        self.assertEqual(post({
            "action": "save_contract_days", "name": "Samira", "hourly_rate": "25",
            "hours_2026-11-12": "3", "hours_2026-11-13": "1.5",
        })[0], 302)
        page = show()
        samira = re.search(r'for="open-contract-(\d+)">Samira<span', page).group(1)
        self.assertIn('value="25.00" aria-label="Samira hourly rate"', page)
        thu = page.split(f'id="contract-cell-{samira}-2026-11-12"', 1)[1].split("</td>", 1)[0]
        self.assertIn('value="3.00"', thu)
        self.assertIn("$75.00 · no super", thu)
        fri = page.split(f'id="contract-cell-{samira}-2026-11-13"', 1)[1].split("</td>", 1)[0]
        self.assertIn('value="1.50"', fri)
        self.assertIn("$37.50 · no super", fri)
        self.assertIn('id="contractor-wages-cost">$522.50', page)

        self.assertEqual(post({
            "action": "save_contract_days", "contractor_id": pat, "hourly_rate": "",
            "hours_2026-11-16": "2",
        }, week="2026-11-16")[0], 302)
        later = show("2026-11-16")
        later_mon = later.split(f'id="contract-cell-{pat}-2026-11-16"', 1)[1].split("</td>", 1)[0]
        self.assertIn('value="2.00"', later_mon)
        self.assertIn("$80.00 · no super", later_mon)
        self.assertIn('value="40.00" aria-label="Pat hourly rate"', later)
        self.assertIn('id="contractor-wages-cost">$80.00', later)

        refused = post({
            "action": "save_contract_days", "contractor_id": riley, "hourly_rate": "40",
            "hours_2026-11-10": "5",
        })
        self.assertEqual(refused[0], 400)
        self.assertIn("Tue 10 Nov 2026 has a flat amount", refused[2].decode())
        c = server.db()
        flat_row = c.execute(
            "SELECT flat_amount, cost FROM portal_contract_entries e JOIN portal_contractors k ON k.id=e.contractor_id WHERE k.name='Riley'"
        ).fetchone()
        self.assertEqual(flat_row["cost"], 90)
        before = c.execute("SELECT COUNT(*) AS n FROM portal_contract_entries").fetchone()["n"]
        c.close()

        self.assertEqual(post({
            "action": "save_contract", "name": "Pat", "work_date": "2026-11-09",
            "pay_style": "hourly", "hours": "1", "hourly_rate": "40",
        })[0], 302)
        mixed = post({
            "action": "save_contract_days", "contractor_id": pat, "hours_2026-11-09": "8",
        })
        self.assertEqual(mixed[0], 400)
        self.assertIn("more than one entry", mixed[2].decode())
        c = server.db()
        monday_rows = c.execute(
            """SELECT hours FROM portal_contract_entries e
               JOIN portal_contractors k ON k.id=e.contractor_id
               WHERE k.name='Pat' AND e.work_date='2026-11-09' ORDER BY e.id"""
        ).fetchall()
        self.assertEqual([row["hours"] for row in monday_rows], [3, 1])
        self.assertEqual(self.ingest("pay-rates", {"asOf": "2026-11-12", "employees": [
            {"name": "Alex", "ordinary": 32, "saturday": 48, "sunday": 64, "publicHoliday": 80, "superRate": 0.12},
        ]})[0], 200)
        self.assertEqual(self.ingest("sales", {"asOf": "2026-11-12", "latestSalesDate": "2026-11-12", "periods": [{"daily": [
            {"date": "2026-11-12", "total": 10, "shop": 4, "fuel": 6, "gp": 1},
        ]}]})[0], 200)
        after = c.execute("SELECT COUNT(*) AS n FROM portal_contract_entries").fetchone()["n"]
        names = [row["name"] for row in c.execute("SELECT name FROM portal_contractors ORDER BY name").fetchall()]
        c.execute("DELETE FROM portal_sales_days WHERE date='2026-11-12'")
        c.commit()
        c.close()
        self.assertEqual(before + 1, after)
        self.assertEqual(names, ["Pat", "Riley", "Samira"])

        code, _, body, _ = self.request(
            "GET", "/api/portal/staff-hours?week=2026-11-09",
            headers={"Authorization": "Bearer test-ingest-key"},
        )
        self.assertEqual(code, 200)
        raw = body.decode().lower()
        self.assertIn("alex", raw)
        self.assertNotIn("pat", raw)
        self.assertNotIn("samira", raw)
        self.assertNotIn("riley", raw)
        self.assertNotIn("contract", raw)
        self.assertNotIn("40.00", raw)
        csv_text = self.authed("GET", "/portal/hours.csv?week=2026-11-09")[2].decode("utf-8-sig")
        self.assertIn("Alex", csv_text)
        self.assertNotIn("Pat", csv_text)
        self.assertNotIn("Samira", csv_text)
        self.assertNotIn("Riley", csv_text)
        self.assertNotIn("90.00", csv_text)

    def test_optional_insight_generator_stays_off(self):
        tiny = {"asOf": "2026-07-02", "latestSalesDate": "2026-07-02", "periods": [{"daily": [
            {"date": "2026-07-02", "total": 10, "shop": 4, "fuel": 6, "gp": 1}
        ]}]}
        self.assertEqual(self.ingest("sales", tiny)[0], 200)
        c = server.db()
        self.assertIsNone(c.execute("SELECT 1 FROM portal_insights WHERE date='2026-07-02'").fetchone())
        c.close()
        os.environ["PORTAL_INSIGHTS_GENERATE"] = "1"
        self.ingest("insights", {"date": "2026-07-02", "headline": "From the shop computer", "bullets": ["Keep this."]})
        self.ingest("sales", tiny)
        c = server.db()
        row = c.execute("SELECT headline, source FROM portal_insights WHERE date='2026-07-02'").fetchone()
        c.close()
        self.assertEqual(row["headline"], "From the shop computer")
        self.assertEqual(row["source"], "box")
        os.environ["PORTAL_INSIGHTS_GENERATE"] = "1"
        self.assertEqual(self.ingest("sales", {"asOf": "2026-07-03", "latestSalesDate": "2026-07-03", "periods": [{"daily": [
            {"date": "2026-07-03", "total": 12, "shop": 5, "fuel": 7, "gp": 2}
        ]}]})[0], 200)
        c = server.db()
        row = c.execute("SELECT source FROM portal_insights WHERE date='2026-07-03'").fetchone()
        c.close()
        self.assertEqual(row["source"], "server")

    def test_sales_dashboard_parity(self):
        portal._ingest_hits.clear()
        self.login()
        for name in ("sales", "purchases", "fuel-commission", "fuel-dips", "insights", "invoices", "product-search",
                     "retail-pricing", "till-price-changes", "shifts", "last-year", "cost-overrides"):
            code, _, raw, _ = self.ingest(name, load(name + ".json"))
            self.assertEqual(code, 200, name + " " + raw.decode()[:400])
        code, _, raw, _ = self.ingest("last-year", {"year": 2025, "month": 11, "fromDate": "2025-11-01", "toDate": "2025-11-30", "groups": [{"name": "A", "qty": 1, "salesEx": 2, "gp": 1}]})
        self.assertEqual(code, 400, raw)
        self.assertIn(b"GP", raw)
        code, _, raw, _ = self.ingest("cost-overrides", {"asOf": "2026-11-10", "items": [
            {"code": "1", "costExCents": 1, "effectiveFrom": "2026-11-01", "source": "till"}
        ]})
        self.assertEqual(code, 400, raw)
        code, _, raw, _ = self.ingest("shifts", {"mediaNames": ["4111111111111111"], "days": [{
            "date": "2026-11-01", "media": [[0, 1, 1, 0, 1]]
        }]})
        self.assertEqual(code, 400, raw)
        # Full table replace, not a patch.
        c = server.db()
        n = c.execute("SELECT COUNT(*) AS n FROM portal_cost_overrides").fetchone()["n"]
        c.close()
        self.assertEqual(n, 3)

        page = self.authed("GET", "/portal")[2].decode()
        self.assertIn("Cost coverage", page)
        self.assertIn("87.2%", page)
        self.assertIn(">158<", page)
        self.assertIn("Daily shop average", page)
        self.assertIn("$6,933.32", page)
        self.assertIn("$25,018.09", page)
        self.assertIn("Cost override", page)
        self.assertIn("November 2026 · month-end projection", page)
        self.assertIn("9:30pm", page)
        self.assertIn("Australia/Brisbane", page)
        section = page.split('id="last-year"', 1)[1].split("</section>", 1)[0]
        self.assertIn("115,281.95", section)
        self.assertIn("$193,763.72", section)
        self.assertIn("Pro-rated", section)
        self.assertIn("2025", section)
        self.assertNotIn("GP", section)
        self.assertNotIn("coverage", section.lower())
        self.assertNotIn("missing", section.lower())
        for label in ("Products", "Pricing", "Till price changes", "Departments", "Kitchen", "Shift balancing",
                      "Commission", "Fuel commission", "Invoices", "Staff hours", "Data notes"):
            self.assertIn(label, page)

        last = self.authed("GET", "/portal/products?period=lastmonth&product=804040")[2].decode()
        self.assertNotIn("Cost override", last.split('id="product-804040"', 1)[-1].split("</section>", 1)[0])
        prod = self.authed("GET", "/portal/products?product=804040")[2].decode()
        block = prod.split('id="product-804040"', 1)[1].split("</section>", 1)[0]
        self.assertIn("Cost override", block)
        self.assertIn("$1,320.64", block)
        self.assertIn("BACON EGG MUFFIN", prod)
        fuel_product = self.authed("GET", "/portal/products?product=2")[2].decode()
        self.assertIn("November 2025", fuel_product)
        self.assertIn("no cost or GP", fuel_product)

        depts = self.authed("GET", "/portal/departments")[2].decode()
        self.assertIn("BACON EGG MUFFIN", depts)
        self.assertIn("Avg fuel", depts)
        self.assertIn("Under 15%", depts)
        self.assertIn("Negative GP", depts)
        neg = self.authed("GET", "/portal/departments?low=negative")[2].decode()
        self.assertIn("24132", neg)

        kitchen = self.authed("GET", "/portal/kitchen?day=2026-11-10")[2].decode()
        self.assertIn("SAUSAGE ROLL", kitchen)
        self.assertIn("Month to date", kitchen)
        self.assertIn("$10,979.33", kitchen)

        purchases = self.authed("GET", "/portal/purchases")[2].decode()
        self.assertIn("TILL8122", purchases)
        self.assertIn("Previous period", purchases)
        self.assertIn("Weekly purchases", purchases)

        fuel = self.authed("GET", "/portal/fuel")[2].decode()
        self.assertIn("does not read or set pump prices", fuel)
        self.assertIn("Margin cpl", fuel)
        self.assertIn("Missing delivered-cost days: none", fuel)
        self.assertIn("Est. commission", fuel)

        dips = self.authed("GET", "/portal/dips")[2].decode()
        self.assertIn("Pump readings", dips)
        self.assertIn("does not read or set pump prices", dips)
        self.assertIn("Inferred inflow", dips)

        shifts = self.authed("GET", "/portal/shifts")[2].decode()
        self.assertIn("not a cash shortage", shifts.lower())
        self.assertIn("Drive Off", shifts)
        self.assertIn("IOU", shifts)
        self.assertIn("Cash drops", shifts)
        self.assertIn("Staff hours", shifts)
        window = self.authed("GET", "/portal/shifts?view=window")[2].decode()
        self.assertIn("19:10", window)

        commission = self.authed("GET", "/portal/commission")[2].decode()
        self.assertIn("$11,915.09", commission)
        self.assertIn("$9,000.00", commission)
        self.assertIn("ULP cpl", commission)

        till = self.authed("GET", "/portal/till-prices")[2].decode()
        self.assertIn("PREMIUM 95", till)
        self.assertIn("COST-ONLY", till)
        self.assertIn("2.0460", till)
        self.assertIn("$2.10", till)
        self.assertIn("Cost only", till)
        self.assertIn("does not write SwiftPOS", till)
        self.assertIn("CHICKEN BREAST WRAP", till)
        self.assertIn("Mainland Shredded Cheese", till)
        grocery = self.authed("GET", "/portal/till-prices?dept=Grocery")[2].decode()
        self.assertIn("ZERO COST BISCUIT", grocery)
        self.assertNotIn("CHICKEN BREAST WRAP", grocery.split('id="no-cost"', 1)[1])

        price = self.authed("GET", "/portal/pricing?product=10000")[2].decode()
        self.assertIn("Cost override", price)
        self.assertIn("$4.00", price)
        self.assertIn("does not write SwiftPOS", price)
        what = self.authed("GET", "/portal/pricing?what_cost=3.55&what_margin=35&what_gst=inc")[2].decode()
        self.assertIn("$6.50", what)
        self.assertIn("$6.95", what)
        self.assertIn("$6.99", what)

        notes = self.authed("GET", "/portal/notes")[2].decode()
        self.assertIn("9:30pm", notes)
        self.assertIn("sales ex GST", notes)

        # Invoices and staff hours still answer.
        invoices = self.authed("GET", "/portal/invoices")[2].decode()
        self.assertIn("Bega Dairy", invoices)
        self.assertIn('target="_blank"', invoices)
        hours = self.authed("GET", "/portal/hours?week=2026-11-09")[2].decode()
        self.assertIn("does not send hours to Xero", hours)

    def test_stock_on_hand(self):
        portal._ingest_hits.clear()
        status, _, body, _ = self.request("GET", "/api/portal/stock")
        self.assertEqual(status, 401)
        self.assertNotIn(b"baselineRule", body)
        self.login()
        code, _, raw, _ = self.ingest("product-search", load("product-search.json"))
        self.assertEqual(code, 200, raw)
        code, _, raw, _ = self.ingest("stock-movements", load("stock-movements.json"))
        self.assertEqual(code, 200, raw)
        self.assertEqual(json.loads(raw)["movements"], 2)

        def save(code, qty, day):
            page = self.authed("GET", "/portal/stock?pick=" + code)[2].decode()
            csrf = self.csrf_from(page)
            return self.authed("POST", "/portal/stock", urlencode({
                "csrf": csrf, "action": "set_count", "code": code, "qty": str(qty),
                "baseline_date": day, "baseline_time": "18:30", "note": "stocktake",
            }))

        status, hdrs, _, _ = save("804040", 40, "2026-11-08")
        self.assertEqual(status, 302, hdrs)
        self.assertEqual(hdrs.get("Location"), "/portal/stock?ok=1")
        page = self.authed("GET", "/portal/stock")[2].decode()
        row = page.split('id="stock-804040"', 1)[1].split("</tr>", 1)[0]
        self.assertIn("BACON EGG MUFFIN", row)
        self.assertIn("ROADHOUSE BREAKFAST", row)
        self.assertIn(">40<", row)
        self.assertIn(">23<", row)
        self.assertIn(">12<", row)
        self.assertIn(">29<", row)
        self.assertIn("end of 8 Nov 2026", row)

        replaced = {"asOf": "2026-11-10", "movements": [
            {"code": "804040", "date": "2026-11-10", "source": "till-sales", "sold": 20, "received": 0},
        ]}
        self.assertEqual(self.ingest("stock-movements", replaced)[0], 200)
        self.assertEqual(self.ingest("stock-movements", {"asOf": "2026-11-08", "movements": [
            {"code": "804040", "date": "2026-11-08", "source": "till-sales", "sold": 100, "received": 0},
        ]})[0], 200)
        page = self.authed("GET", "/portal/stock")[2].decode()
        row = page.split('id="stock-804040"', 1)[1].split("</tr>", 1)[0]
        self.assertIn(">20<", row)
        self.assertIn(">32<", row)
        self.assertIn("1.6", row)
        self.assertNotIn(">100<", row)
        c = server.db()
        stored = c.execute(
            "SELECT COUNT(*) AS n, SUM(sold) AS sold FROM portal_stock_movements WHERE code='804040' AND move_date='2026-11-10' AND source='till-sales'"
        ).fetchone()
        c.close()
        self.assertEqual(stored["n"], 1)
        self.assertEqual(stored["sold"], 20)

        self.assertEqual(save("804040", 10, "2026-11-10")[0], 302)
        self.assertEqual(save("10000", 1, "2026-11-01")[0], 302)
        self.assertEqual(self.ingest("stock-movements", {"asOf": "2026-11-02", "movements": [
            {"code": "10000", "date": "2026-11-02", "source": "till-sales", "sold": 4, "received": 0},
        ]})[0], 200)
        page = self.authed("GET", "/portal/stock")[2].decode()
        muffin = page.split('id="stock-804040"', 1)[1].split("</tr>", 1)[0]
        self.assertIn(">10<", muffin)
        self.assertIn("to 40", page)
        self.assertIn("to 10", page)
        biscuit = page.split('id="stock-10000"', 1)[1].split("</tr>", 1)[0]
        self.assertIn('class="negative"', biscuit)
        self.assertIn(">-3<", biscuit)
        negative = self.authed("GET", "/portal/stock?filter=negative")[2].decode()
        self.assertIn("stock-10000", negative)
        self.assertNotIn("stock-804040", negative)

        status, _, body, _ = self.request("GET", "/api/portal/stock", headers={"Authorization": "Bearer test-ingest-key"})
        payload = json.loads(body)
        self.assertEqual(status, 200)
        self.assertEqual(payload["timezone"], "Australia/Brisbane")
        self.assertIn("end of baselineDate", payload["baselineRule"])
        item = next(row for row in payload["products"] if row["code"] == "804040")
        self.assertEqual(item["baselineDate"], "2026-11-10")
        self.assertEqual(item["openingQty"], 10)
        self.assertEqual(item["onHand"], 10)
        self.assertEqual(item["group"], "ROADHOUSE BREAKFAST")
        self.assertEqual(self.request("GET", "/api/portal/stock", headers={"Authorization": "Bearer wrong"})[0], 401)
        bad = self.ingest("stock-movements", {"asOf": "2026-11-10", "movements": [
            {"code": "804040", "date": "2026-11-10", "source": "till-sales", "sold": 1, "received": 0, "reference": "4111111111111111"},
        ]})
        self.assertEqual(bad[0], 400)
        self.assertIn(b"card", bad[2].lower())
        c = server.db()
        sold = c.execute(
            "SELECT sold FROM portal_stock_movements WHERE code='804040' AND move_date='2026-11-10' AND source='till-sales'"
        ).fetchone()["sold"]
        c.close()
        self.assertEqual(sold, 20)

    def test_manual_stock_kitchen_range_and_chat(self):
        portal._ingest_hits.clear()
        self.login()
        self.assertEqual(self.ingest("product-search", load("product-search.json"))[0], 200)
        self.assertEqual(self.ingest("retail-pricing", load("retail-pricing.json"))[0], 200)
        self.assertEqual(self.ingest("sales", load("sales.json"))[0], 200)

        def post_stock(fields):
            page = self.authed("GET", "/portal/stock")[2].decode()
            fields = dict(fields)
            fields["csrf"] = self.csrf_from(page)
            return self.authed("POST", "/portal/stock", urlencode(fields))

        status, hdrs, _, _ = post_stock({
            "action": "add_manual", "name": "House sauce", "code": "", "barcode": "", "group": "Kitchen",
            "qty": "5", "baseline_date": "2026-11-01", "baseline_time": "23:59",
        })
        self.assertEqual(status, 302, hdrs)
        status, _, body, _ = self.request("GET", "/api/portal/stock", headers={"Authorization": "Bearer test-ingest-key"})
        payload = json.loads(body)
        manual = next(row for row in payload["products"] if row.get("manual"))
        self.assertEqual(manual["code"], "M-0001")
        self.assertEqual(manual["name"], "House sauce")
        self.assertEqual(manual["openingQty"], 5)
        self.assertEqual(self.ingest("stock-movements", {"asOf": "2026-11-02", "movements": [
            {"code": "M-0001", "date": "2026-11-02", "source": "supplier-invoice", "sold": 0, "received": 2, "reference": "HAND"},
        ]})[0], 200)
        edited = post_stock({
            "action": "edit_manual", "old_code": "M-0001", "code": "SAUCE1", "name": "House sauce",
            "barcode": "123", "group": "Kitchen",
        })
        self.assertEqual(edited[0], 302, edited[2][:200])
        payload = json.loads(self.request("GET", "/api/portal/stock", headers={"Authorization": "Bearer test-ingest-key"})[2])
        sauce = next(row for row in payload["products"] if row["code"] == "SAUCE1")
        self.assertTrue(sauce["manual"])
        self.assertEqual(sauce["onHand"], 7)
        self.assertEqual(sauce["barcode"], "123")
        self.assertEqual(post_stock({"action": "untrack", "code": "SAUCE1"})[0], 302)
        payload = json.loads(self.request("GET", "/api/portal/stock", headers={"Authorization": "Bearer test-ingest-key"})[2])
        self.assertFalse(any(row["code"] == "SAUCE1" for row in payload["products"]))
        history = self.authed("GET", "/portal/stock")[2].decode()
        self.assertIn("House sauce", history)
        self.assertIn("untrack", history)

        depts = self.authed("GET", "/portal/products?dept=Roadhouse")[2].decode()
        self.assertIn("BACON EGG MUFFIN", depts)
        self.assertIn("ROADHOUSE BREAKFAST", depts)
        self.assertIn("All departments", depts)
        prices = self.authed("GET", "/portal/pricing?dept=Grocery")[2].decode()
        self.assertIn("ARNOTTS SHAPES", prices)
        self.assertIn("Suggested", prices)
        self.assertIn("All departments", prices)

        one_day = self.authed("GET", "/portal/kitchen?period=custom&from=2026-11-10&to=2026-11-10&item=14494")[2].decode()
        item = one_day.split('id="kitchen-item"', 1)[1].split("</section>", 1)[0]
        self.assertIn("SAUSAGE ROLL", item)
        self.assertIn(">12<", item)
        self.assertIn("$68.73", item)
        self.assertIn("No hourly breakdown", item)
        period = one_day.split('id="kitchen-period"', 1)[1].split("</section>", 1)[0]
        self.assertIn('<div class="lbl">Kitchen sales</div><b>$2,165.77</b>', period)
        self.assertIn("Month to date", period)
        groups = self.authed("GET", "/portal/kitchen?period=custom&from=2026-11-10&to=2026-11-10&dept=Hot%20Pies%20%26%20Sausage%20Rolls")[2].decode()
        self.assertIn("SAUSAGE ROLL", groups)
        self.assertIn("All kitchen groups", groups)

        token = self.csrf_from(self.authed("GET", "/portal")[2].decode())
        raw = json.dumps({"message": "What sold yesterday?", "page": "/portal/kitchen", "context": {"period": "yesterday"}}).encode()
        status, _, body, _ = self.request("POST", "/api/portal/chat", raw, {
            "Content-Type": "application/json", "X-CSRF-Token": token, "Content-Length": str(len(raw)),
        }, self.jar)
        self.assertEqual(status, 200, body)
        saved = json.loads(body)
        self.assertEqual(saved["status"], "offline")
        self.assertNotIn("PORTAL_CHAT", body.decode())
        thread = json.loads(self.authed("GET", "/api/portal/chat")[2])
        self.assertEqual(thread["messages"][0]["message"], "What sold yesterday?")
        self.assertIsNone(thread["messages"][0]["reply"])
        reply = json.dumps({"id": saved["id"], "reply": "Yesterday was **quiet**."}).encode()
        status, _, body, _ = self.request("POST", "/api/portal/chat/reply", reply, {
            "Content-Type": "application/json", "Authorization": "Bearer test-ingest-key", "Content-Length": str(len(reply)),
        })
        self.assertEqual(status, 200, body)
        thread = json.loads(self.authed("GET", "/api/portal/chat")[2])
        self.assertEqual(thread["messages"][0]["reply"], "Yesterday was **quiet**.")
        page = self.authed("GET", "/portal/kitchen")[2].decode()
        self.assertIn('id="help-panel"', page)
        self.assertIn("Today", page)
        self.assertIn("Yesterday", page)

        received = {}

        class Hook(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                size = int(self.headers.get("Content-Length") or 0)
                received["auth"] = self.headers.get("Authorization")
                received["body"] = json.loads(self.rfile.read(size))
                self.send_response(204)
                self.end_headers()

            def log_message(self, *_args):
                return

        hook = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Hook)
        threading.Thread(target=hook.serve_forever, daemon=True).start()
        os.environ["PORTAL_CHAT_WEBHOOK_URL"] = "http://127.0.0.1:%s/hook" % hook.server_address[1]
        os.environ["PORTAL_CHAT_WEBHOOK_KEY"] = "chat-test-key"
        try:
            raw = json.dumps({"message": "Relay this", "page": "/portal", "context": {"period": "month"}}).encode()
            status, _, body, _ = self.request("POST", "/api/portal/chat", raw, {
                "Content-Type": "application/json", "X-CSRF-Token": token, "Content-Length": str(len(raw)),
            }, self.jar)
            self.assertEqual(status, 200, body)
            self.assertEqual(json.loads(body)["status"], "sent")
            self.assertNotIn(b"chat-test-key", body)
            self.assertEqual(received["auth"], "Bearer chat-test-key")
            self.assertEqual(received["body"]["message"], "Relay this")
            self.assertEqual(received["body"]["page"], "/portal")
            self.assertEqual(received["body"]["context"]["period"], "month")
            self.assertEqual(received["body"]["context"]["onlineOrders"], "not loaded")
            self.assertIn("tillSales", received["body"]["context"])
            self.assertTrue(received["body"]["context"]["totalSales"].startswith("$"))
            self.assertTrue(received["body"]["context"]["shopGp"].startswith("till GP"))
            self.assertIn("createdAt", received["body"])
        finally:
            os.environ.pop("PORTAL_CHAT_WEBHOOK_URL", None)
            os.environ.pop("PORTAL_CHAT_WEBHOOK_KEY", None)
            hook.shutdown()
            hook.server_close()
        cleared = json.dumps({}).encode()
        status, _, body, _ = self.request("POST", "/api/portal/chat/clear", cleared, {
            "Content-Type": "application/json", "X-CSRF-Token": token, "Content-Length": str(len(cleared)),
        }, self.jar)
        self.assertEqual(status, 200, body)
        self.assertEqual(json.loads(self.authed("GET", "/api/portal/chat")[2])["messages"], [])

    def _clear_online_fixture(self):
        c = server.db()
        c.execute("DELETE FROM portal_online_days")
        c.execute("DELETE FROM portal_online_orders")
        c.execute("DELETE FROM portal_snapshots WHERE dataset='online-orders'")
        c.execute("DELETE FROM portal_meta WHERE k='online-orders'")
        c.execute("DELETE FROM portal_sales_days WHERE date='2026-11-12'")
        c.execute("DELETE FROM portal_insights WHERE date='2026-11-12'")
        c.commit()
        c.close()

    def test_online_orders_add_to_sales(self):
        portal._ingest_hits.clear()
        self.login()
        self.addCleanup(self._clear_online_fixture)
        status, _, body, _ = self.authed("GET", "/portal/online")
        self.assertEqual(status, 200)
        missing = body.decode()
        self.assertIn("No online orders yet", missing)
        self.assertNotIn("Traceback", missing)
        overview = self.authed("GET", "/portal")[2].decode()
        self.assertIn("Online orders (Bopple)", overview)
        self.assertIn("not loaded yet", overview)
        self.assertIn("Till sales", overview)
        self.assertIn("Shop GP", overview)

        empty = self.ingest("online-orders", {"daily": [], "orders": []})
        self.assertEqual(empty[0], 400)
        data = load("online-orders.json")
        code, _, raw, _ = self.ingest("online-orders", data)
        self.assertEqual(code, 200, raw[:400])
        stored = json.loads(raw)
        self.assertEqual(stored["asOf"], "2026-11-11")
        self.assertEqual(stored["days"], len({row["date"] for row in data["daily"]}))
        self.assertEqual(stored["orders"], len({row["order_no"] for row in data["orders"]}))
        poisoned = json.loads(json.dumps(data))
        poisoned["orders"][0]["card"] = "4242424242424242"
        rejected = self.ingest("online-orders", poisoned)
        self.assertEqual(rejected[0], 400)
        self.assertNotIn(b"4242424242424242", rejected[2])
        c = server.db()
        self.assertIsNone(c.execute("SELECT 1 FROM portal_online_orders WHERE card_brand LIKE '%4242%'").fetchone())
        self.assertIsNotNone(c.execute("SELECT 1 FROM portal_online_orders WHERE order_no=?", (data["orders"][0]["order_no"],)).fetchone())
        c.close()

        page = self.authed("GET", "/portal/online")[2].decode()
        self.assertIn("Month to date", page)
        self.assertIn("Bopple fees", page)
        self.assertIn("Net payout", page)
        self.assertIn("26672951", page)
        self.assertIn("Visa", page)
        daily = page.split('id="online-daily"', 1)[1].split("</section>", 1)[0]
        self.assertLess(daily.index("11 Nov 2026"), daily.index("9 Nov 2026"))
        c = server.db()
        anchor = portal.online_anchor(c)
        mtd_rows = portal.online_days_between(c, anchor.replace(day=1), anchor)
        mtd = portal.online_totals(mtd_rows)
        c.close()
        mtd_html = page.split('id="online-mtd"', 1)[1].split('id="online-period"', 1)[0]
        self.assertIn(f"<b>{mtd['orders']}</b>", mtd_html)
        self.assertIn(portal.money(mtd["total"]), mtd_html)
        self.assertIn(portal.money(mtd["fees"]), mtd_html)
        self.assertIn(portal.money(mtd["net"]), mtd_html)

        filtered = self.authed("GET", "/portal/online?period=custom&from=2026-10-01&to=2026-11-11&day=2026-10-24")[2].decode()
        listing = filtered.split('id="online-order-list"', 1)[1]
        self.assertIn("26325169", listing)
        self.assertIn("-$77.40", listing)
        self.assertNotIn("26672951", listing)

        c = server.db()
        anchor = portal.anchor_day(c)
        _active, start, end = portal.period_bounds("month", None, None, anchor)
        till = sum(r["total"] or 0 for r in portal.sales_between(c, start, end))
        online = portal.online_totals(portal.online_days_between(c, start, end))
        gp_rows, _deltas = portal.parity().adjust_sales_days(c, portal.sales_between(c, start, end))
        gp = sum(r["gp"] or 0 for r in gp_rows)
        c.close()
        home = self.authed("GET", "/portal")[2].decode()
        split = home.split('id="sales-split"', 1)[1].split("</section>", 1)[0]
        total_card = split.split('id="total-sales"', 1)[1].split('id="till-sales"', 1)[0]
        till_card = split.split('id="till-sales"', 1)[1].split('id="online-sales"', 1)[0]
        online_card = split.split('id="online-sales"', 1)[1].split('id="shop-gp"', 1)[0]
        gp_card = split.split('id="shop-gp"', 1)[1]
        self.assertIn(portal.money(till + online["total"]), total_card)
        self.assertIn(portal.money(till), till_card)
        self.assertIn(portal.money(online["total"]), online_card)
        self.assertNotIn(portal.money(till + online["total"]), gp_card)
        self.assertIn("Till GP", gp_card)
        self.assertIn(portal.money(gp), gp_card)
        if online["total"]:
            self.assertNotEqual(portal.money(till + online["total"]), portal.money(till))

        os.environ["PORTAL_INSIGHTS_GENERATE"] = "1"
        try:
            portal._ingest_hits.clear()
            code, _, raw, _ = self.ingest("sales", {"asOf": "2026-11-12", "latestSalesDate": "2026-11-12", "periods": [{"daily": [
                {"date": "2026-11-12", "total": 50, "shop": 20, "fuel": 30, "gp": 7, "gpPct": 0.35, "coverage": 1, "coveredEx": 18, "missing": 0, "products": 1},
            ]}]})
            self.assertEqual(code, 200, raw)
            code, _, raw, _ = self.ingest("online-orders", {"daily": [
                {"date": "2026-11-12", "orders": 1, "total": 10, "fees": -0.4, "refunds": 0, "net": 9.6},
            ], "orders": [{
                "order_no": "9001", "time": "2026-11-12 01:00:00 PM (Thu)", "trading_date": "2026-11-12",
                "wallet": "Card", "card": "Visa", "total": 10, "fees": -0.4, "refunds": 0, "net": 9.6,
            }]})
            self.assertEqual(code, 200, raw)
            c = server.db()
            self.assertAlmostEqual(c.execute("SELECT total FROM portal_online_days WHERE date='2026-10-01'").fetchone()["total"], 126.17, places=2)
            row = c.execute("SELECT bullets, source FROM portal_insights WHERE date='2026-11-12'").fetchone()
            sales = portal.rows(c.execute("SELECT * FROM portal_sales_days"))
            amounts = portal.online_amounts(c)
            proj = portal.project_month(sales, [], None, "2026-11-12", amounts)
            c.close()
            self.assertEqual(row["source"], "server")
            text = " ".join(json.loads(row["bullets"]))
            self.assertIn(portal.money(proj["mtd"]["total"] + proj["mtd"]["online"]), text)
            self.assertIn("Bopple", text)
            self.assertIn("till GP", text)
            self.assertIn(portal.money(proj["mtd"]["gp"]), text)
        finally:
            os.environ["PORTAL_INSIGHTS_GENERATE"] = ""

        day = self.authed("GET", "/portal?period=custom&from=2026-11-12&to=2026-11-12")[2].decode()
        self.assertIn("1 completed sales day", day)
        split = day.split('id="sales-split"', 1)[1].split("</section>", 1)[0]
        self.assertIn("$60.00", split.split('id="total-sales"', 1)[1].split('id="till-sales"', 1)[0])
        self.assertIn("$50.00", split.split('id="till-sales"', 1)[1].split('id="online-sales"', 1)[0])
        self.assertIn("$10.00", split.split('id="online-sales"', 1)[1].split('id="shop-gp"', 1)[0])
        gp_card = split.split('id="shop-gp"', 1)[1]
        self.assertIn("$7.00", gp_card)
        self.assertNotIn("$60.00", gp_card)

        token = self.csrf_from(self.authed("GET", "/portal/online")[2].decode())
        raw = json.dumps({"message": "How were online orders?", "page": "/portal/online", "context": {"period": "month"}}).encode()
        status, _, body, _ = self.request("POST", "/api/portal/chat", raw, {
            "Content-Type": "application/json", "X-CSRF-Token": token, "Content-Length": str(len(raw)),
        }, self.jar)
        self.assertEqual(status, 200, body)
        thread = json.loads(self.authed("GET", "/api/portal/chat")[2])
        ctx = thread["messages"][-1]["context"]
        self.assertEqual(ctx["period"], "month")
        self.assertIn("gross", ctx["onlineOrders"])
        self.assertNotEqual(ctx["onlineOrders"], "not loaded")
        self.assertTrue(ctx["totalSales"].startswith("$"))
        self.assertTrue(ctx["shopGp"].startswith("till GP"))

    def test_invoice_paid_and_unpaid_lists(self):
        portal._ingest_hits.clear()
        self.login()
        c = server.db()
        c.execute("DELETE FROM portal_invoices")
        c.commit()
        c.close()
        code, _, raw, _ = self.ingest("invoices", load("invoices.json"))
        self.assertEqual(code, 200, raw[:300])
        page = self.authed("GET", "/portal/invoices")[2].decode()
        self.assertIn("Unpaid invoices", page)
        self.assertIn("Total owing", page)
        self.assertIn("Total paid", page)
        self.assertIn("Bega Dairy", page)
        self.assertIn('class="overdue"', page)
        self.assertIn('href="https://example.com/pay/bega/577803222"', page)
        self.assertIn('target="_blank"', page)
        self.assertIn('rel="noopener noreferrer"', page)
        self.assertNotIn("Telstra", page)
        unpaid_rows, paid_rows = portal.invoice_lists(server.db(), portal.today())
        owing = sum(row["amount_due"] or 0 for row in unpaid_rows)
        self.assertIn(portal.money(owing), page.split('id="unpaid-total"', 1)[1].split('id="overdue-total"', 1)[0])
        self.assertIn("<b>—</b>", page.split('id="paid-total"', 1)[1].split('id="invoice-tabs"', 1)[0])

        paid = self.authed("GET", "/portal/invoices?list=paid")[2].decode()
        paid_table = paid.split('id="paid-list"', 1)[1]
        self.assertIn("Telstra", paid_table)
        self.assertIn("Paid invoices", paid_table)
        self.assertNotIn("Bega Dairy", paid_table)
        self.assertNotIn("Pay", paid_table)
        self.assertNotIn('class="overdue"', paid_table)
        self.assertNotIn("https://example.com/pay/", paid)

        code, _, raw, _ = self.ingest("invoices", {"date": "2026-11-11", "invoices": [
            {
                "supplier": "Paid Co", "invoice_number": "P-1", "date": "2026-10-02", "due_date": "2026-10-16",
                "amount_due": 0, "amount": 40, "paid_date": "2026-11-01", "status": "paid",
                "pay_url": "https://example.com/pay/paid-co/P-1",
            },
            {
                "supplier": "Void Co", "invoice_number": "V-1", "date": "2026-10-03", "due_date": "2026-10-17",
                "amount_due": 12, "status": "void",
            },
        ]})
        self.assertEqual(code, 200, raw[:300])
        unpaid = self.authed("GET", "/portal/invoices")[2].decode()
        self.assertIn("Bega Dairy", unpaid)
        self.assertNotIn("Paid Co", unpaid.split('id="invoice-list"', 1)[1])
        self.assertNotIn("Void Co", unpaid)
        self.assertNotIn("https://example.com/pay/paid-co/P-1", unpaid)
        paid = self.authed("GET", "/portal/invoices?list=paid")[2].decode()
        paid_table = paid.split('id="paid-list"', 1)[1]
        self.assertIn("Paid Co", paid_table)
        self.assertIn("Telstra", paid_table)
        self.assertIn("1 Nov 2026", paid_table)
        self.assertIn("$40.00", paid.split('id="paid-total"', 1)[1].split('id="invoice-tabs"', 1)[0])
        self.assertIn("no total in the file", paid)
        self.assertNotIn("https://example.com/pay/paid-co/P-1", paid)
        self.assertNotIn("Void Co", paid)
        self.assertNotIn(">Pay<", paid_table)

    def _clear_exceptions(self):
        c = server.db()
        c.execute("DELETE FROM portal_shift_exceptions")
        c.execute("DELETE FROM portal_snapshots WHERE dataset='shift-exceptions'")
        c.execute("DELETE FROM portal_meta WHERE k='shift-exceptions'")
        c.commit()
        c.close()

    def test_shift_exceptions(self):
        import portal_exceptions
        portal._ingest_hits.clear()
        self._clear_exceptions()
        self.addCleanup(self._clear_exceptions)
        self.login()
        status, _, body, _ = self.authed("GET", "/portal/exceptions")
        self.assertEqual(status, 200)
        empty = body.decode()
        self.assertIn("No till exceptions yet", empty)
        self.assertIn('class="on" href="/portal/exceptions" aria-current="page"', empty)
        self.assertIn('name="csrf"', empty)
        self.assertNotIn("Traceback", empty)
        home = self.authed("GET", "/portal")[2].decode()
        card = home.split('id="overview-exceptions"', 1)[1].split("</section>", 1)[0]
        self.assertIn("not loaded yet", card)
        self.assertIn('href="/portal/exceptions"', card)
        self.assertIn("Till exceptions", home)

        data = load("shift-exceptions.json")
        code, _, raw, _ = self.ingest("shift-exceptions", data)
        self.assertEqual(code, 200, raw[:500])
        stored = json.loads(raw)
        self.assertEqual(stored["asOf"], "2026-11-10")
        self.assertEqual(stored["dates"], 1)
        self.assertEqual(stored["rows"], 6)
        self.assertEqual(stored["replaced"], ["2026-11-10"])
        nov9 = {
            "date": "2026-11-09",
            "timezone": "Australia/Brisbane",
            "rows": [{
                "type": "account", "account_type": "IOU", "date": "2026-11-09", "time": "16:40:02",
                "receipt": "362934", "staff": "Silkwood Clerk 1", "item": "RED BULL ENERGY DRINK 473ML x2",
                "quantity": 2, "amount": 11.98, "outstanding": 11.98, "paid_on_account": 0,
                "customer": None, "status": "unpaid", "status_basis": "allocated_payments",
                "lines": [{"item": "RED BULL ENERGY DRINK 473ML", "plu": "45110", "quantity": 2, "amount": 11.98, "adjustment": False}],
            }],
        }
        code, _, raw, _ = self.ingest("shift-exceptions", nov9)
        self.assertEqual(code, 200, raw[:400])
        again = json.loads(raw)
        self.assertEqual(again["asOf"], "2026-11-10")
        self.assertEqual(again["rows"], 1)
        self.assertEqual(again["replaced"], ["2026-11-09"])

        self.assertEqual(portal_exceptions._mask_secrets("4242424242424242"), "••••")
        self.assertNotIn("4242", portal_exceptions._mask_secrets("paid on 4242 4242 4242 4242"))

        page = self.authed("GET", "/portal/exceptions")[2].decode()
        self.assertIn("Paid status for named accounts is an estimate.", page)
        self.assertIn("ORANGE &amp; MANGO", page)
        self.assertNotIn("ORANGE & MANGO", page.split('id="exception-rows"', 1)[1].split("</section>", 1)[0])
        self.assertIn("Short dated", page)
        self.assertIn("Silkwood Clerk 1", page)
        self.assertIn("362100", page)
        self.assertIn("adjustment", page)
        self.assertIn("Estimate", page)
        table = page.split('id="exception-rows"', 1)[1].split("</section>", 1)[0]
        order = ["361911", "362200", "362150", "362110", "362101", "362100", "362934"]
        positions = [table.index(number) for number in order]
        self.assertEqual(positions, sorted(positions))
        staff = page.split('id="exception-staff"', 1)[1].split("</section>", 1)[0]
        self.assertLess(staff.index("Silkwood Clerk 1"), staff.index("Alex"))
        self.assertIn(">2<", staff)
        self.assertIn(">1<", staff)

        c = server.db()
        overrides = portal.rows(c.execute("SELECT difference, amount FROM portal_shift_exceptions WHERE exc_type='price_override'"))
        owed = portal.rows(c.execute(
            "SELECT exc_type, outstanding FROM portal_shift_exceptions WHERE exc_type IN ('drive_off','account')"
        ))
        c.close()
        difference = sum(row["difference"] or 0 for row in overrides)
        outstanding = sum(row["outstanding"] or 0 for row in owed)
        account_owed = sum(row["outstanding"] or 0 for row in owed if row["exc_type"] == "account")
        drive_owed = sum(row["outstanding"] or 0 for row in owed if row["exc_type"] == "drive_off")
        self.assertAlmostEqual(difference, sum(row["difference"] for row in data["rows"] if row["type"] == "price_override"), places=2)
        totals = page.split('id="exception-totals"', 1)[1].split('id="exception-staff"', 1)[0]
        override_card = totals.split('id="exception-price_override"', 1)[1].split('id="exception-drive_off"', 1)[0]
        drive_card = totals.split('id="exception-drive_off"', 1)[1].split('id="exception-account"', 1)[0]
        account_card = totals.split('id="exception-account"', 1)[1]
        self.assertIn("<b>3</b>", override_card)
        self.assertIn("Price difference", override_card)
        self.assertIn(portal.money(difference), override_card)
        self.assertIn(portal.money(drive_owed), drive_card)
        self.assertIn("1 unpaid", drive_card)
        self.assertIn("<b>3</b>", account_card)
        self.assertIn(portal.money(account_owed), account_card)
        self.assertIn("1 unpaid", account_card)
        self.assertIn("1 partial", account_card)

        drives = self.authed("GET", "/portal/exceptions?type=drive_off")[2].decode()
        drive_table = drives.split('id="exception-rows"', 1)[1].split("</section>", 1)[0]
        self.assertIn("361911", drive_table)
        self.assertNotIn("362100", drive_table)
        self.assertNotIn("SILKWOOD STATE SCHOOL", drive_table)
        self.assertIn("Paid status for named accounts is an estimate.", drives)
        self.assertIn("type=drive_off", drives)
        accounts = self.authed("GET", "/portal/exceptions?type=account")[2].decode()
        account_table = accounts.split('id="exception-rows"', 1)[1].split("</section>", 1)[0]
        self.assertEqual(account_table.count('class="pill warn">Estimate'), 1)
        self.assertIn("SILKWOOD STATE SCHOOL", account_table)
        self.assertNotIn("DIESEL", account_table)
        one_day = self.authed("GET", "/portal/exceptions?period=custom&from=2026-11-09&to=2026-11-09")[2].decode()
        self.assertIn("362934", one_day)
        self.assertNotIn("362100", one_day)
        last_month = self.authed("GET", "/portal/exceptions?period=lastmonth")[2].decode()
        self.assertIn("No till exceptions in this range.", last_month)
        self.assertNotIn("362100", last_month)

        home = self.authed("GET", "/portal")[2].decode()
        card = home.split('id="overview-exceptions"', 1)[1].split("</section>", 1)[0]
        self.assertIn("<b>7</b>", card)
        self.assertIn("3 price overrides", card)
        self.assertIn("1 drive-off", card)
        self.assertIn("3 account charges", card)
        self.assertIn(portal.money(outstanding), card)
        self.assertIn("Paid status for named accounts is an estimate.", card)
        notes = self.authed("GET", "/portal/notes")[2].decode()
        self.assertIn("Paid status for named on-account customers is an estimate.", notes)
        self.assertIn("Till exceptions: 2026-11-10", notes)

        replaced = json.loads(json.dumps(data))
        replaced["rows"] = [row for row in replaced["rows"] if row["staff"] != "Alex"]
        for row in replaced["rows"]:
            if row["receipt"] == "361911":
                row["status"] = "paid"
                row["outstanding"] = 0
                row["paid_on_account"] = row["amount"]
        code, _, raw, _ = self.ingest("shift-exceptions", replaced)
        self.assertEqual(code, 200, raw[:400])
        self.assertEqual(json.loads(raw)["rows"], 5)
        c = server.db()
        drive = c.execute("SELECT status, outstanding FROM portal_shift_exceptions WHERE receipt='361911'").fetchone()
        self.assertEqual(drive["status"], "paid")
        self.assertEqual(drive["outstanding"], 0)
        self.assertIsNone(c.execute("SELECT 1 FROM portal_shift_exceptions WHERE staff='Alex'").fetchone())
        self.assertIsNotNone(c.execute("SELECT 1 FROM portal_shift_exceptions WHERE receipt='362934'").fetchone())
        self.assertIsNotNone(c.execute("SELECT 1 FROM portal_shift_exceptions WHERE receipt='362100'").fetchone())
        c.close()

        code, _, raw, _ = self.ingest("shift-exceptions", {"date": "2026-11-09", "rows": []})
        self.assertEqual(code, 200, raw[:300])
        self.assertEqual(json.loads(raw)["rows"], 0)
        c = server.db()
        self.assertIsNone(c.execute("SELECT 1 FROM portal_shift_exceptions WHERE receipt='362934'").fetchone())
        self.assertIsNotNone(c.execute("SELECT 1 FROM portal_shift_exceptions WHERE receipt='362100'").fetchone())
        c.close()

        batch = {"days": [
            {"date": "2026-11-08", "rows": [{
                "type": "drive_off", "account_type": "Drive Off", "time": "12:00:00", "receipt": 361000,
                "staff": "Silkwood Clerk 1", "item": "UNLEADED", "quantity": 10, "amount": 20,
                "outstanding": 20, "paid_on_account": 0, "status": "unpaid", "status_basis": "allocated_payments",
            }]},
            {"date": "2026-11-07", "rows": []},
        ]}
        code, _, raw, _ = self.ingest("shift-exceptions", batch)
        self.assertEqual(code, 200, raw[:400])
        self.assertEqual(json.loads(raw)["replaced"], ["2026-11-08", "2026-11-07"])
        c = server.db()
        self.assertEqual(c.execute("SELECT receipt FROM portal_shift_exceptions WHERE trade_date='2026-11-08'").fetchone()["receipt"], "361000")
        kept = c.execute("SELECT COUNT(*) AS n FROM portal_shift_exceptions WHERE trade_date='2026-11-10'").fetchone()["n"]
        c.close()
        self.assertEqual(kept, 5)

        flat = {"dates": ["2026-10-02", "2026-10-03"], "rows": [{
            "type": "price_override", "subtype": "shop", "date": "2026-10-02", "time": "08:00:00",
            "receipt": "340001", "staff": "Silkwood Clerk 1", "item": "Chicken chips", "quantity": 1,
            "normal_price": 5.99, "charged_price": 4, "difference": -1.99, "amount": 4,
        }]}
        code, _, raw, _ = self.ingest("shift-exceptions", flat)
        self.assertEqual(code, 200, raw[:400])
        self.assertEqual(sorted(json.loads(raw)["replaced"]), ["2026-10-02", "2026-10-03"])
        c = server.db()
        self.assertIsNotNone(c.execute("SELECT 1 FROM portal_shift_exceptions WHERE receipt='340001'").fetchone())
        self.assertIsNone(c.execute("SELECT 1 FROM portal_shift_exceptions WHERE trade_date='2026-10-03'").fetchone())
        meta = json.loads(c.execute("SELECT v FROM portal_meta WHERE k='shift-exceptions'").fetchone()["v"])
        self.assertEqual(meta["asOf"], "2026-11-10")
        c.close()

        dup = {"date": "2026-10-04", "rows": [
            {"type": "price_override", "subtype": "shop", "time": "09:00:00", "receipt": "340010",
             "staff": "Silkwood Clerk 1", "item": "Chicken chips", "quantity": 1,
             "normal_price": 5, "charged_price": 4, "difference": -1, "amount": 4},
            {"type": "price_override", "subtype": "shop", "time": "09:00:00", "receipt": "340010",
             "staff": "Silkwood Clerk 1", "item": "Chicken chips", "quantity": 1,
             "normal_price": 5, "charged_price": 3, "difference": -2, "amount": 3},
        ]}
        code, _, raw, _ = self.ingest("shift-exceptions", dup)
        self.assertEqual(code, 200, raw[:300])
        self.assertEqual(json.loads(raw)["rows"], 1)
        c = server.db()
        self.assertEqual(c.execute("SELECT amount FROM portal_shift_exceptions WHERE receipt='340010'").fetchone()["amount"], 3)
        c.close()

        def reject(payload):
            portal._ingest_hits.clear()
            status, _, body, _ = self.ingest("shift-exceptions", payload)
            self.assertEqual(status, 400, body[:300])
            self.assertNotIn(b"4242424242424242", body)
            self.assertNotIn(b"123456789012", body)
            c = server.db()
            still = c.execute("SELECT 1 FROM portal_shift_exceptions WHERE receipt='362100'").fetchone()
            c.close()
            self.assertIsNotNone(still)

        poisoned = json.loads(json.dumps(data))
        poisoned["rows"][0]["customer"] = "4242424242424242"
        reject(poisoned)
        poisoned["rows"][0]["customer"] = None
        poisoned["rows"][0]["card_number"] = "4111"
        reject(poisoned)
        poisoned["rows"][0].pop("card_number")
        poisoned["rows"][0]["bank_account"] = "12345678"
        reject(poisoned)
        poisoned["rows"][0].pop("bank_account")
        poisoned["rows"][0]["item"] = "123456789012"
        reject(poisoned)
        poisoned = json.loads(json.dumps(data))
        poisoned["rows"][0]["date"] = "2026-11-09"
        reject(poisoned)
        reject({"date": "2026-11-10", "rows": [{"type": "void", "time": "09:15:00", "receipt": "1", "staff": "A", "item": "B", "amount": 1}]})
        reject({"rows": []})
        self.assertNotIn("4242424242424242", self.authed("GET", "/portal/exceptions")[2].decode())


    def test_online_order_items_search_and_top(self):
        import importlib.util
        portal._ingest_hits.clear()
        self.login()
        self.addCleanup(self._clear_online_fixture)
        spec = importlib.util.spec_from_file_location(
            "parse_scheduled_orders", ROOT / "tools" / "parse_scheduled_orders.py",
        )
        parser = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(parser)
        parsed = parser.parse_scheduled_orders(ROOT / "tools" / "fixtures" / "scheduled-orders.csv")
        orders = []
        for number, info in parsed.items():
            gross = 0.0
            for item in info["items"]:
                gross += float(item["total"] or 0)
                for mod in item["modifiers"]:
                    gross += float(mod["total"] or 0)
            orders.append({
                "order_no": number,
                "time": info["fulfilment_date"] + " 12:00:00 PM (Wed)",
                "trading_date": info["fulfilment_date"],
                "wallet": "Card",
                "card": "Visa",
                "total": round(gross, 2),
                "fees": 0,
                "refunds": 0,
                "net": round(gross, 2),
                "customer_name": info["customer_name"],
                "order_type": info["order_type"],
                "fulfilment_time": info["fulfilment_time"],
                "items": info["items"],
            })
        code, _, raw, _ = self.ingest("online-orders", {"asOf": "2026-11-11", "orders": orders})
        self.assertEqual(code, 200, raw[:400])
        page = self.authed("GET", "/portal/online")[2].decode()
        self.assertIn("Jess Tabone", page)
        self.assertIn("Medium Chips", page)
        self.assertIn("Chicken Salt", page)
        self.assertIn("Pickup", page)
        self.assertIn("02:21pm", page)
        self.assertIn("Top online items", page)
        self.assertIn('id="online-search"', page)
        self.assertIn("Visa", page)
        top = page.split('id="online-top-items"', 1)[1].split("</section>", 1)[0]
        self.assertIn("Small Chips", top)
        self.assertNotIn("Chicken Salt", top)

        found = self.authed("GET", "/portal/online?q=Jess+Tabone")[2].decode()
        listing = found.split('id="online-order-list"', 1)[1]
        self.assertIn("Jess Tabone", listing)
        self.assertNotIn("Ben Coutts", listing)
        self.assertIn("Medium Chips", listing)
        nuggets = self.authed("GET", "/portal/online?q=nugget")[2].decode()
        self.assertIn("15 Crunchy Chicken Nuggets", nuggets)
        self.assertNotIn("Helen Belbin", nuggets.split('id="online-order-list"', 1)[1])

        plain = json.loads(json.dumps(orders[0]))
        for key in ("customer_name", "order_type", "fulfilment_time", "items"):
            plain.pop(key)
        code, _, raw, _ = self.ingest("online-orders", {"orders": [plain]})
        self.assertEqual(code, 200, raw[:300])
        c = server.db()
        kept = c.execute(
            "SELECT customer_name, items FROM portal_online_orders WHERE order_no=?",
            (plain["order_no"],),
        ).fetchone()
        c.close()
        self.assertEqual(kept["customer_name"], orders[0]["customer_name"])
        self.assertIn("Medium Chips", kept["items"])

        poisoned = json.loads(json.dumps(orders[0]))
        poisoned["customer_name"] = "4242424242424242"
        rejected = self.ingest("online-orders", {"orders": [poisoned]})
        self.assertEqual(rejected[0], 400)
        self.assertNotIn(b"4242424242424242", rejected[2])

    def test_stock_barcode_links(self):
        import portal_stock
        portal._ingest_hits.clear()
        self.assertIn("012345000065", portal_stock.barcode_keys("01234565"))
        self.assertIn("0012345000065", portal_stock.barcode_keys("01234565"))
        self.assertNotIn("012345000065", portal_stock.barcode_keys("01234566"))

        self.assertEqual(self.request("GET", "/api/portal/stock/barcode?barcode=9310072028866")[0], 401)
        self.assertEqual(self.request("POST", "/api/portal/stock/barcode", b"{}", {
            "Content-Type": "application/json", "Content-Length": "2",
        })[0], 401)
        self.assertEqual(self.request(
            "GET", "/api/portal/stock/barcode?barcode=9310072028866",
            headers={"Authorization": "Bearer test-ingest-key"},
        )[0], 401)

        self.login()

        def post_json(payload, token="omit", cookies=None):
            raw = json.dumps(payload).encode()
            headers = {"Content-Type": "application/json", "Content-Length": str(len(raw))}
            if token != "omit":
                headers["X-CSRF-Token"] = token
            return self.request(
                "POST", "/api/portal/stock/barcode", raw, headers,
                self.jar if cookies is None else cookies,
            )

        def movement_rows():
            c = server.db()
            rows = [tuple(row) for row in c.execute(
                "SELECT movement_key, code, move_date, source, sold, received FROM portal_stock_movements ORDER BY movement_key"
            )]
            c.close()
            return rows

        try:
            self.assertEqual(self.ingest("product-search", load("product-search.json"))[0], 200)
            self.assertEqual(self.ingest("retail-pricing", load("retail-pricing.json"))[0], 200)
            self.assertEqual(self.ingest("stock-movements", load("stock-movements.json"))[0], 200)
            before_moves = movement_rows()
            self.assertTrue(before_moves)

            page = self.authed("GET", "/portal/stock")[2].decode()
            token = self.csrf_from(page)
            self.assertIn('id="stock-scan"', page)
            self.assertIn("Scan barcode", page)
            self.assertIn("camera=(self)", self.authed("GET", "/portal/stock")[1].get("Permissions-Policy", ""))
            self.assertIn("camera=(),", self.authed("GET", "/portal")[1].get("Permissions-Policy", ""))
            self.assertIn("payment=()", self.request("GET", "/healthz")[1].get("Permissions-Policy", ""))
            self.assertIn('id="stock-scan-mode"', page)
            self.assertIn("Keep camera open", page)
            self.assertIn("/static/stock-scan.js", page)
            self.assertIn('name="q"', page)
            self.assertNotIn("stock-scan.js", self.authed("GET", "/portal")[2].decode())

            script = self.request("GET", "/static/stock-scan.js")
            self.assertEqual(script[0], 200)
            self.assertIn(b"BarcodeDetector", script[2])
            self.assertIn(b"Camera permission was denied", script[2])
            self.assertIn(b"/static/zxing.js", script[2])
            self.assertIn(b"getTracks", script[2])
            self.assertIn(b".stop()", script[2])
            self.assertNotIn(b"https://", script[2])
            self.assertNotIn(b"http://", script[2])
            zxing = self.request("GET", "/static/zxing.js")
            self.assertEqual(zxing[0], 200)
            self.assertIn("javascript", zxing[1].get("Content-Type", ""))
            self.assertIn(b"BrowserMultiFormatReader", zxing[2])
            self.assertIn(b"Apache License", zxing[2])

            shapes = json.loads(self.authed("GET", "/api/portal/stock/barcode?barcode=9310072028866")[2])
            self.assertTrue(shapes["ok"])
            self.assertEqual(shapes["code"], "10000")
            self.assertEqual(shapes["source"], "catalog")
            self.assertIn("ARNOTTS", shapes["name"])
            short = json.loads(self.authed("GET", "/api/portal/stock/barcode?barcode=2901")[2])
            self.assertEqual(short["code"], "1010")
            self.assertEqual(short["source"], "catalog")
            fuel = self.authed("GET", "/api/portal/stock/barcode?barcode=2")
            self.assertEqual(fuel[0], 400)
            self.assertNotIn(b"UNLEADED", fuel[2])
            missing = self.authed("GET", "/api/portal/stock/barcode?barcode=9900000000099")
            self.assertEqual(missing[0], 404)
            self.assertIn(b"Barcode not linked", missing[2])
            self.assertIn("ARNOTTS", self.authed("GET", "/portal/stock?q=9310072028866")[2].decode())

            self.assertEqual(post_json({"barcode": "9900000000011", "code": "804040"})[0], 403)
            self.assertEqual(post_json({"barcode": "9900000000011", "code": "804040"}, token="nope")[0], 403)
            no_cookie = {k: v for k, v in self.jar.items() if k != "silkwood_csrf"}
            self.assertEqual(post_json({"barcode": "9900000000011", "code": "804040"}, token=token, cookies=no_cookie)[0], 403)
            unknown = post_json({"barcode": "9900000000011", "code": "NO-SUCH"}, token=token)
            self.assertEqual(unknown[0], 400, unknown[2])
            pan = "4111111111111111"
            card = post_json({"barcode": pan, "code": "804040"}, token=token)
            self.assertEqual(card[0], 400, card[2])
            self.assertNotIn(pan.encode(), card[2])
            self.assertIn(b"card", card[2].lower())

            for barcode in ("9900000000011", "9900000000028"):
                linked = post_json({"barcode": barcode, "code": "804040"}, token=token)
                self.assertEqual(linked[0], 200, linked[2])
                body = json.loads(linked[2])
                self.assertTrue(body["ok"])
                self.assertEqual(body["code"], "804040")
                self.assertIsNone(body["movedFrom"])
                found = json.loads(self.authed("GET", "/api/portal/stock/barcode?barcode=" + barcode)[2])
                self.assertEqual(found["code"], "804040")
                self.assertEqual(found["source"], "link")
                self.assertIn("BACON", found["name"])
            again = json.loads(post_json({"barcode": "9900000000011", "code": "804040"}, token=token)[2])
            self.assertIsNone(again["movedFrom"])
            moved = json.loads(post_json({"barcode": "9900000000011", "code": "10000"}, token=token)[2])
            self.assertEqual(moved["movedFrom"], "804040")
            self.assertEqual(json.loads(self.authed("GET", "/api/portal/stock/barcode?barcode=9900000000011")[2])["code"], "10000")
            self.assertEqual(json.loads(self.authed("GET", "/api/portal/stock/barcode?barcode=9900000000028")[2])["code"], "804040")

            upc = post_json({"barcode": "012345000065", "code": "10000"}, token=token)
            self.assertEqual(upc[0], 200, upc[2])
            for barcode in ("01234565", "0012345000065", "012345000065"):
                hit = json.loads(self.authed("GET", "/api/portal/stock/barcode?barcode=" + barcode)[2])
                self.assertEqual(hit["code"], "10000", barcode)
                self.assertEqual(hit["source"], "link", barcode)

            unlinked = self.authed("GET", "/portal/stock?link=9900000000004&q=muffin")[2].decode()
            self.assertIn("Barcode not linked", unlinked)
            self.assertIn("BACON EGG MUFFIN", unlinked)
            self.assertIn('id="stock-search"', unlinked)
            choose = self.authed("GET", "/portal/stock?link=9900000000004&pick=804040&q=muffin")[2].decode()
            self.assertIn('id="barcode-link"', choose)
            self.assertIn("Link barcode", choose)
            status, hdrs, _, _ = self.authed("POST", "/portal/stock", urlencode({
                "csrf": self.csrf_from(choose), "action": "link_barcode",
                "barcode": "9900000000004", "code": "804040",
            }))
            self.assertEqual(status, 302, hdrs)
            self.assertEqual(hdrs.get("Location"), "/portal/stock?pick=804040&ok=linked")
            saved = self.authed("GET", hdrs.get("Location"))[2].decode()
            self.assertIn("Barcode linked", saved)
            self.assertIn('value="804040"', saved)
            self.assertEqual(movement_rows(), before_moves)

            c = server.db()
            stored = c.execute(
                "SELECT code, created_by FROM portal_product_barcodes WHERE barcode=?",
                ("9900000000028",),
            ).fetchone()
            c.close()
            self.assertEqual(stored["code"], "804040")
            self.assertEqual(stored["created_by"], "astron")

            page = self.authed("GET", "/portal/stock")[2].decode()
            added = self.authed("POST", "/portal/stock", urlencode({
                "csrf": self.csrf_from(page), "action": "add_manual", "name": "Scan biscuit",
                "code": "SCAN1", "barcode": "87654321", "group": "Kitchen",
                "qty": "4", "baseline_date": "2026-11-01", "baseline_time": "23:59",
            }))
            self.assertEqual(added[0], 302, added[2][:240])
            manual = json.loads(self.authed("GET", "/api/portal/stock/barcode?barcode=87654321")[2])
            self.assertEqual(manual["code"], "SCAN1")
            self.assertEqual(manual["source"], "manual")
            self.assertTrue(manual["tracked"])
            self.assertEqual(manual["onHand"], 4)
            extra = post_json({"barcode": "87654322", "code": "SCAN1"}, token=token)
            self.assertEqual(extra[0], 200, extra[2])
            page = self.authed("GET", "/portal/stock?edit=SCAN1")[2].decode()
            renamed = self.authed("POST", "/portal/stock", urlencode({
                "csrf": self.csrf_from(page), "action": "edit_manual", "old_code": "SCAN1",
                "code": "SCAN2", "name": "Scan biscuit", "barcode": "87654321", "group": "Kitchen",
            }))
            self.assertEqual(renamed[0], 302, renamed[2][:240])
            self.assertEqual(json.loads(self.authed("GET", "/api/portal/stock/barcode?barcode=87654322")[2])["code"], "SCAN2")
            self.assertEqual(json.loads(self.authed("GET", "/api/portal/stock/barcode?barcode=87654321")[2])["code"], "SCAN2")
            payload = json.loads(self.request("GET", "/api/portal/stock", headers={"Authorization": "Bearer test-ingest-key"})[2])
            biscuit = next(row for row in payload["products"] if row["code"] == "SCAN2")
            self.assertEqual(biscuit["onHand"], 4)
            self.assertEqual(biscuit["barcode"], "87654321")
            self.assertEqual(movement_rows(), before_moves)

            twin = load("product-search.json")
            twin["products"] = list(twin["products"]) + [[
                "55501", "TWIN BAR TEST", 46, "9310000000011,9310000000028", 100, 50, 1, "",
            ]]
            self.assertEqual(self.ingest("product-search", twin)[0], 200)
            for barcode in ("9310000000011", "9310000000028"):
                hit = json.loads(self.authed("GET", "/api/portal/stock/barcode?barcode=" + barcode)[2])
                self.assertEqual(hit["code"], "55501", barcode)
                self.assertEqual(hit["source"], "catalog", barcode)
            self.assertEqual(self.ingest("product-search", load("product-search.json"))[0], 200)
            self.assertEqual(self.authed("GET", "/api/portal/stock/barcode?barcode=9310000000011")[0], 404)

            priced = load("retail-pricing.json")
            priced["rows"] = [list(row) for row in priced["rows"]]
            priced["rows"][1][3] = "4006381333931"
            self.assertEqual(self.ingest("retail-pricing", priced)[0], 200)
            priced_hit = json.loads(self.authed("GET", "/api/portal/stock/barcode?barcode=4006381333931")[2])
            self.assertEqual(priced_hit["code"], "804040")
            self.assertEqual(priced_hit["source"], "pricing")
            self.assertEqual(json.loads(self.authed("GET", "/api/portal/stock/barcode?barcode=9310072028866")[2])["source"], "catalog")
        finally:
            c = server.db()
            c.execute("DELETE FROM portal_product_barcodes")
            c.execute("DELETE FROM portal_stock_counts WHERE code IN ('SCAN1', 'SCAN2', '55501')")
            c.execute("DELETE FROM portal_stock_items WHERE code IN ('SCAN1', 'SCAN2', '55501')")
            c.execute("DELETE FROM portal_stock_movements WHERE code IN ('SCAN1', 'SCAN2', '55501')")
            c.commit()
            c.close()
            portal._ingest_hits.clear()
            self.ingest("product-search", load("product-search.json"))
            self.ingest("retail-pricing", load("retail-pricing.json"))

    def test_barcode_map_ingest_and_scan(self):
        import portal_stock
        portal._ingest_hits.clear()
        self.assertEqual(
            portal_stock._barcode_tokens("9305555555558 9305555555565"),
            ["9305555555558", "9305555555565"],
        )
        self.assertEqual(portal_stock._barcode_tokens("111, 222\t333|444"), ["111", "222", "333", "444"])

        denied = self.ingest("barcode-map", {
            "products": [["77001", "SHELF ONLY SOAP", "9305555555551"]],
        }, key="nope")
        self.assertEqual(denied[0], 401)

        self.login()
        archive = load("product-search.json")
        self.assertFalse(any(str(row[0]) == "77001" for row in archive["products"]))
        self.assertEqual(self.ingest("product-search", archive)[0], 200)
        self.assertEqual(self.authed("GET", "/api/portal/stock/barcode?barcode=9305555555551")[0], 404)

        shaped = self.ingest("barcode-map", {"rows": []})
        self.assertEqual(shaped[0], 400)
        self.assertIn(b"products", shaped[2])

        products = [
            ["77001", "FIRST", "9305555555551"],
            ["77001", "SHELF ONLY SOAP", "9305555555551"],
            ["77003", "SPACE JOINED BISCUIT", "9305555555558 9305555555565"],
            ["77004", "LIST JOINED CHIPS", ["9305555555572", "9305555555589"]],
            ["77005", "UPC MAP CHOCOLATE", "012345000065"],
            ["77099", "SHOULD NOT WIN", "9310072028866"],
            ["77006", "LINK LOSER", "9900000000082"],
            ["77008", "COMMA JOINED MINT", "9305555555626,9305555555633"],
            [88001, "NUMBER PLU", "9305555555640"],
        ]
        code, _, raw, _ = self.ingest("barcode-map", {"products": products})
        self.assertEqual(code, 200, raw[:300])
        body = json.loads(raw)
        self.assertTrue(body["ok"])
        self.assertEqual(body["dataset"], "barcode-map")
        self.assertEqual(body["asOf"], "2026-11-11")
        self.assertEqual(body["products"], 8)
        self.assertEqual(body["barcodes"], 11)
        notes = self.authed("GET", "/portal/notes")[2].decode()
        self.assertIn("Barcode map: 2026-11-11", notes)

        def lookup(barcode):
            status, _, payload, _ = self.authed("GET", "/api/portal/stock/barcode?barcode=" + barcode)
            return status, json.loads(payload)

        status, hit = lookup("9305555555551")
        self.assertEqual(status, 200, hit)
        self.assertTrue(hit["ok"])
        self.assertEqual(hit["code"], "77001")
        self.assertEqual(hit["name"], "SHELF ONLY SOAP")
        self.assertEqual(hit["source"], "barcode-map")
        self.assertFalse(hit["tracked"])
        self.assertIsNone(hit["onHand"])

        for barcode in ("9305555555558", "9305555555565"):
            status, hit = lookup(barcode)
            self.assertEqual(status, 200, hit)
            self.assertEqual(hit["code"], "77003", barcode)
            self.assertEqual(hit["source"], "barcode-map", barcode)
        for barcode in ("9305555555572", "9305555555589"):
            status, hit = lookup(barcode)
            self.assertEqual(hit["code"], "77004", barcode)
            self.assertEqual(hit["source"], "barcode-map", barcode)
        for barcode in ("9305555555626", "9305555555633"):
            status, hit = lookup(barcode)
            self.assertEqual(hit["code"], "77008", barcode)
        status, numbered = lookup("9305555555640")
        self.assertEqual(status, 200, numbered)
        self.assertEqual(numbered["code"], "88001")
        self.assertEqual(numbered["name"], "NUMBER PLU")

        c = server.db()
        c.execute(
            "DELETE FROM portal_product_barcodes WHERE barcode IN ('012345000065','0012345000065','01234565')"
        )
        c.commit()
        c.close()
        for barcode in ("01234565", "0012345000065", "012345000065"):
            status, hit = lookup(barcode)
            self.assertEqual(status, 200, hit)
            self.assertEqual(hit["code"], "77005", barcode)
            self.assertEqual(hit["source"], "barcode-map", barcode)
            self.assertEqual(hit["name"], "UPC MAP CHOCOLATE", barcode)

        status, catalog_hit = lookup("9310072028866")
        self.assertEqual(status, 200, catalog_hit)
        self.assertEqual(catalog_hit["code"], "10000")
        self.assertEqual(catalog_hit["source"], "catalog")
        self.assertIn("ARNOTTS", catalog_hit["name"])

        page = self.authed("GET", "/portal/stock")[2].decode()
        token = self.csrf_from(page)
        raw_link = json.dumps({"barcode": "9900000000082", "code": "804040"}).encode()
        linked = self.request("POST", "/api/portal/stock/barcode", raw_link, {
            "Content-Type": "application/json",
            "Content-Length": str(len(raw_link)),
            "X-CSRF-Token": token,
        }, self.jar)
        self.assertEqual(linked[0], 200, linked[2])
        status, link_hit = lookup("9900000000082")
        self.assertEqual(status, 200, link_hit)
        self.assertEqual(link_hit["code"], "804040")
        self.assertEqual(link_hit["source"], "link")
        self.assertIn("BACON", link_hit["name"])
        self.assertEqual(lookup("9305555555551")[1]["source"], "barcode-map")

        pick = self.authed("GET", "/portal/stock?pick=77001")[2].decode()
        self.assertIn('id="stock-pick">SHELF ONLY SOAP', pick)
        self.assertIn('name="code" value="77001"', pick)
        missing = self.authed("POST", "/portal/stock", urlencode({
            "csrf": self.csrf_from(pick), "action": "set_count", "code": "NO-SUCH",
            "qty": "1", "baseline_date": "2026-11-10", "baseline_time": "18:30",
        }))
        self.assertEqual(missing[0], 400)
        self.assertIn("Choose a product from the catalog.", missing[2].decode())
        saved = self.authed("POST", "/portal/stock", urlencode({
            "csrf": self.csrf_from(pick), "action": "set_count", "code": "77001",
            "qty": "6", "baseline_date": "2026-11-10", "baseline_time": "18:30", "note": "scanned",
        }))
        self.assertEqual(saved[0], 302, saved[1])
        self.assertEqual(saved[1].get("Location"), "/portal/stock?ok=1")
        page = self.authed("GET", "/portal/stock")[2].decode()
        row = page.split('id="stock-77001"', 1)[1].split("</tr>", 1)[0]
        self.assertIn("SHELF ONLY SOAP", row)
        self.assertIn(">6<", row)
        self.assertIn("SHELF ONLY SOAP", page.split('id="stock-history"', 1)[1])
        status, counted = lookup("9305555555551")
        self.assertEqual(counted["tracked"], True)
        self.assertEqual(counted["onHand"], 6)
        payload = json.loads(self.request(
            "GET", "/api/portal/stock", headers={"Authorization": "Bearer test-ingest-key"},
        )[2])
        item = next(row for row in payload["products"] if row["code"] == "77001")
        self.assertEqual(item["name"], "SHELF ONLY SOAP")
        self.assertEqual(item["onHand"], 6)
        self.assertEqual(item["openingQty"], 6)
        self.assertEqual(item["soldSince"], 0)
        self.assertEqual(item["receivedSince"], 0)
        self.assertFalse(item["manual"])
        c = server.db()
        moves = c.execute("SELECT COUNT(*) AS n FROM portal_stock_movements WHERE code='77001'").fetchone()["n"]
        c.close()
        self.assertEqual(moves, 0)

        rejected = self.ingest("barcode-map", {"products": [["77001", 5, "9305555555551"]]})
        self.assertEqual(rejected[0], 400)
        pan = "4111111111111111"
        rejected = self.ingest("barcode-map", {"products": [["77001", pan + " soap", "9305555555551"]]})
        self.assertEqual(rejected[0], 400)
        self.assertNotIn(pan.encode(), rejected[2])
        rejected = self.ingest("barcode-map", {"products": [["77001", "SHELF ONLY SOAP", pan]]})
        self.assertEqual(rejected[0], 400)
        self.assertNotIn(pan.encode(), rejected[2])
        self.assertEqual(lookup("9305555555551")[1]["code"], "77001")

        plain = json.dumps({"dataset": "barcode-map", "data": {"products": products}}).encode()
        bad_gzip = self.request("POST", "/api/portal/ingest", b"not-gzip", {
            "Content-Type": "application/json",
            "Content-Encoding": "gzip",
            "Authorization": "Bearer test-ingest-key",
            "Content-Length": "8",
        })
        self.assertEqual(bad_gzip[0], 400)
        self.assertIn(b"gzip", bad_gzip[2])
        deflate = self.request("POST", "/api/portal/ingest", plain, {
            "Content-Type": "application/json",
            "Content-Encoding": "deflate",
            "Authorization": "Bearer test-ingest-key",
            "Content-Length": str(len(plain)),
        })
        self.assertEqual(deflate[0], 400)
        self.assertIn(b"gzip", deflate[2])
        self.assertEqual(lookup("9305555555551")[1]["name"], "SHELF ONLY SOAP")

        bulk = []
        for i in range(4400):
            bulk.append([f"9{i:05d}", f"BULK ITEM {i}", f"98{i:011d}"])
        packed = json.dumps({
            "dataset": "barcode-map",
            "data": {"asOf": "2026-11-12", "products": bulk},
        }).encode()
        self.assertGreater(len(packed), 100_000)
        self.assertLess(len(packed), portal.INGEST_MAX)
        wire = gzip.compress(packed)
        self.assertLess(len(wire), len(packed))
        status, _, raw, _ = self.request("POST", "/api/portal/ingest", wire, {
            "Content-Type": "application/json",
            "Content-Encoding": "gzip",
            "Authorization": "Bearer test-ingest-key",
            "Content-Length": str(len(wire)),
        })
        self.assertEqual(status, 200, raw[:300])
        replaced = json.loads(raw)
        self.assertEqual(replaced["products"], 4400)
        self.assertEqual(replaced["barcodes"], 4400)
        self.assertEqual(replaced["asOf"], "2026-11-12")
        self.assertEqual(lookup("9305555555551")[0], 404)
        status, first = lookup("9800000000000")
        self.assertEqual(status, 200, first)
        self.assertEqual(first["code"], "900000")
        self.assertEqual(first["name"], "BULK ITEM 0")
        self.assertEqual(first["source"], "barcode-map")
        status, last = lookup(f"98{4399:011d}")
        self.assertEqual(status, 200, last)
        self.assertEqual(last["code"], "904399")
        self.assertEqual(lookup("9310072028866")[1]["source"], "catalog")
        payload = json.loads(self.request(
            "GET", "/api/portal/stock", headers={"Authorization": "Bearer test-ingest-key"},
        )[2])
        kept = next(row for row in payload["products"] if row["code"] == "77001")
        self.assertEqual(kept["onHand"], 6)
        self.assertEqual(kept["soldSince"], 0)

    def test_month_lines_projection_and_last_year(self):
        """Shop, food and fuel-litre month lines, plus last-year-monthly ingest."""
        nov = date(2026, 11, 1)
        through = date(2026, 11, 3)
        weekday = {
            "2026-11-01": 10, "2026-11-02": 20, "2026-11-03": 30,
            "2026-10-25": 99, "2026-10-26": 99, "2026-10-27": 99,
            "2026-10-28": 40, "2026-10-29": 50, "2026-10-30": 60, "2026-10-31": 70,
        }
        projected = portal.project_metric(weekday, nov, through)
        self.assertEqual(projected["method"], "weekday")
        self.assertEqual(projected["daysTraded"], 3)
        self.assertEqual(projected["daysInMonth"], 30)
        self.assertEqual(projected["mtd"], 60)
        self.assertEqual(projected["projected"], 1150)

        prefer_month = dict(weekday)
        del prefer_month["2026-11-01"]
        sunday_from_prior = portal.project_metric(prefer_month, nov, through)
        self.assertEqual(sunday_from_prior["method"], "weekday")
        self.assertEqual(sunday_from_prior["mtd"], 50)
        # No Sunday in November, so the 25 Oct Sunday (99) is used. 4 remaining Sundays.
        self.assertEqual(sunday_from_prior["projected"], 1496)

        outside_four_weeks = {
            "2026-11-02": 20, "2026-11-03": 30, "2026-10-04": 8,
            "2026-10-28": 40, "2026-10-29": 50, "2026-10-30": 60, "2026-10-31": 70,
        }
        averaged = portal.project_metric(outside_four_weeks, nov, through)
        self.assertEqual(averaged["method"], "average")
        self.assertEqual(averaged["projected"], 750)
        self.assertEqual(portal.project_metric({"2026-11-01": 0, "2026-11-02": 10}, nov, date(2026, 11, 2))["daysTraded"], 2)

        october = portal.project_metric({"2026-10-01": 5, "2026-10-02": 7}, date(2026, 10, 1), date(2026, 11, 10))
        self.assertEqual(october["method"], "actual")
        self.assertEqual(october["status"], "actual")
        self.assertEqual(october["mtd"], 12)
        self.assertEqual(october["projected"], 12)
        self.assertEqual(october["daysTraded"], 2)
        self.assertEqual(october["daysInMonth"], 31)

        december = portal.project_metric({}, date(2026, 12, 1), date(2026, 11, 10))
        self.assertEqual(december["status"], "empty")
        self.assertIsNone(december["mtd"])
        self.assertIsNone(december["projected"])

        portal._ingest_hits.clear()
        self.login()
        c = server.db()
        c.execute("DELETE FROM portal_ly_monthly")
        c.execute("DELETE FROM portal_meta WHERE k='last-year-monthly'")
        c.commit()
        c.close()
        self.addCleanup(self._clear_last_year_monthly)

        denied = self.ingest("last-year-monthly", {"month": "2025-11", "shop_sales_inc": 1}, key="nope")
        self.assertEqual(denied[0], 401)
        self.assertNotIn(b"nope", denied[2])
        bad = self.ingest("last-year-monthly", {"months": [{"month": "2025-13", "shop_sales_inc": 1}]})
        self.assertEqual(bad[0], 400)
        both = self.ingest("last-year-monthly", {"month": "2025-11", "months": [{"month": "2025-11"}]})
        self.assertEqual(both[0], 400)
        pan = "4111111111111111"
        leaked = self.ingest("last-year-monthly", {"month": "2025-11", "shop_sales_inc": 1, "note": pan})
        self.assertEqual(leaked[0], 400)
        self.assertNotIn(pan.encode(), leaked[2])

        code, _, raw, _ = self.ingest("last-year-monthly", {"months": [
            {"month": "2025-09", "shop_sales_inc": 1, "food_sales_inc": 1, "fuel_litres": 1, "source": "first", "note": "first"},
            {"month": "2025-09", "shop_sales_inc": 2, "food_sales_inc": None, "fuel_litres": 3, "source": "kept", "note": ""},
            {"month": "2025-10", "shop_sales_inc": 500, "food_sales_inc": 40, "fuel_litres": 80, "source": "October sheet", "note": "till only"},
        ]})
        self.assertEqual(code, 200, raw)
        body = json.loads(raw)
        self.assertTrue(body["ok"])
        self.assertEqual(body["dataset"], "last-year-monthly")
        self.assertEqual(body["months"], 2)
        self.assertEqual(body["asOf"], "2025-10-31")
        c = server.db()
        sept = dict(c.execute("SELECT * FROM portal_ly_monthly WHERE month='2025-09'").fetchone())
        c.close()
        self.assertEqual(sept["shop_sales_inc"], 2)
        self.assertIsNone(sept["food_sales_inc"])
        self.assertEqual(sept["fuel_litres"], 3)
        self.assertEqual(sept["source"], "kept")

        code, _, raw, _ = self.ingest("last-year-monthly", {
            "month": "2025-10", "shop_sales_inc": None, "food_sales_inc": 40, "fuel_litres": 80, "source": "October sheet", "note": "shop missing",
        })
        self.assertEqual(code, 200, raw)
        c = server.db()
        oct_row = dict(c.execute("SELECT * FROM portal_ly_monthly WHERE month='2025-10'").fetchone())
        still = c.execute("SELECT shop_sales_inc FROM portal_ly_monthly WHERE month='2025-09'").fetchone()["shop_sales_inc"]
        c.close()
        self.assertIsNone(oct_row["shop_sales_inc"])
        self.assertEqual(oct_row["food_sales_inc"], 40)
        self.assertEqual(still, 2)

        self.assertEqual(self.ingest("sales", load("sales.json"))[0], 200)
        self.assertEqual(self.ingest("fuel-commission", load("fuel-commission.json"))[0], 200)
        c = server.db()
        through_iso = portal.data_through(c)
        anchor = date.fromisoformat(through_iso)
        shop_days = portal.shop_sales_by_date(c)
        food_days = portal.kitchen_sales_by_date(c)
        fuel_days = portal.fuel_litres_by_date(c)
        c.close()
        shop = portal.project_metric(shop_days, date(anchor.year, anchor.month, 1), anchor)
        food = portal.project_metric(food_days, date(anchor.year, anchor.month, 1), anchor)
        fuel = portal.project_metric(fuel_days, date(anchor.year, anchor.month, 1), anchor)
        self.assertEqual(shop["method"], "weekday")
        self.assertEqual(food["method"], "weekday")
        self.assertEqual(fuel["method"], "weekday")
        self.assertGreater(shop["daysTraded"], 0)
        self.assertNotEqual(shop["projected"], shop["mtd"])

        home = self.authed("GET", "/portal")[2].decode()
        self.assertIn("November 2026 · month-end projection", home)
        card = home.split('id="month-lines"', 1)[1].split("</section>", 1)[0]
        self.assertIn('data-phase="project"', card)
        self.assertIn("Shop sales", card)
        self.assertIn("Food sales", card)
        self.assertIn("Fuel litres", card)
        self.assertIn(portal.PROJECTION_NOTES["weekday"], card)
        self.assertNotIn(portal.PROJECTION_NOTES["average"], card)
        self.assertIn("Till only", card)
        self.assertIn("do not include Bopple", card)
        self.assertIn("already part of shop sales", card)
        self.assertIn("No last-year figure yet", card)
        shop_line = card.split('id="line-shop"', 1)[1].split('id="line-food"', 1)[0]
        food_line = card.split('id="line-food"', 1)[1].split('id="line-fuel"', 1)[0]
        fuel_line = card.split('id="line-fuel"', 1)[1]
        self.assertIn(portal.money(shop["mtd"]), shop_line.split('id="line-shop-mtd"', 1)[1].split('id="line-shop-days"', 1)[0])
        self.assertIn(f"{shop['daysTraded']} of {shop['daysInMonth']}", shop_line)
        self.assertIn(portal.money(shop["projected"]), shop_line.split('id="line-shop-projected"', 1)[1].split('id="line-shop-ly"', 1)[0])
        self.assertIn(portal.money(food["mtd"]), food_line.split('id="line-food-mtd"', 1)[1].split('id="line-food-days"', 1)[0])
        self.assertIn(portal.litres(fuel["mtd"]), fuel_line.split('id="line-fuel-mtd"', 1)[1].split('id="line-fuel-days"', 1)[0])
        self.assertIn(portal.litres(fuel["projected"]), fuel_line.split('id="line-fuel-projected"', 1)[1].split('id="line-fuel-ly"', 1)[0])
        self.assertNotIn("$0.00", shop_line.split('id="line-shop-ly"', 1)[1])
        self.assertNotIn("0.00 L", fuel_line.split('id="line-fuel-ly"', 1)[1])
        self.assertIn('data-method="weekday"', shop_line)

        one_day = self.authed("GET", "/portal?period=custom&from=2026-11-01&to=2026-11-01")[2].decode()
        self.assertIn("1 completed sales day", one_day)
        one_card = one_day.split('id="month-lines"', 1)[1].split("</section>", 1)[0]
        self.assertIn(f"{shop['daysTraded']} of {shop['daysInMonth']}", one_card)
        self.assertIn(portal.money(shop["projected"]), one_card)

        last = self.authed("GET", "/portal?period=lastmonth")[2].decode()
        last_card = last.split('id="month-lines"', 1)[1].split("</section>", 1)[0]
        self.assertIn("October 2026", last_card)
        self.assertIn('data-phase="actual"', last_card)
        self.assertNotIn("Projected month-end", last_card)
        self.assertIn(portal.PROJECTION_NOTES["actual"], last_card)
        self.assertIn("shop missing", last_card)
        oct_shop = portal.project_metric(shop_days, date(2026, 10, 1), anchor)
        oct_food = portal.project_metric(food_days, date(2026, 10, 1), anchor)
        self.assertEqual(oct_shop["method"], "actual")
        self.assertIn(f"{oct_shop['daysTraded']} of {oct_shop['daysInMonth']}", last_card)
        self.assertIn(portal.money(oct_shop["projected"]), last_card.split('id="line-shop-projected"', 1)[1].split('id="line-shop-ly"', 1)[0])
        oct_shop_ly = last_card.split('id="line-shop-ly"', 1)[1].split('id="line-food"', 1)[0]
        oct_food_ly = last_card.split('id="line-food-ly"', 1)[1].split("</article>", 1)[0]
        self.assertIn("No last-year figure yet", oct_shop_ly)
        self.assertNotIn("$0.00", oct_shop_ly)
        self.assertIn(portal.money(40), oct_food_ly)
        food_change = portal._ly_change(oct_food["projected"], 40)
        self.assertIsNotNone(food_change)
        self.assertIn(f"{abs(food_change):.1f}% vs Oct 2025", oct_food_ly)

        code, _, raw, _ = self.ingest("last-year-monthly", {
            "asOf": "2026-11-12",
            "months": [{
                "month": "2025-11",
                "shop_sales_inc": 123456.78,
                "food_sales_inc": None,
                "fuel_litres": 0,
                "source": "Owner upload",
                "note": "Food not extracted yet",
            }],
        })
        self.assertEqual(code, 200, raw)
        self.assertEqual(json.loads(raw)["asOf"], "2026-11-12")
        compared = self.authed("GET", "/portal")[2].decode()
        compared_card = compared.split('id="month-lines"', 1)[1].split("</section>", 1)[0]
        shop_ly = compared_card.split('id="line-shop-ly"', 1)[1].split("</article>", 1)[0]
        food_ly = compared_card.split('id="line-food-ly"', 1)[1].split("</article>", 1)[0]
        fuel_ly = compared_card.split('id="line-fuel-ly"', 1)[1].split("</article>", 1)[0]
        self.assertIn("$123,456.78", shop_ly)
        self.assertNotIn("No last-year figure yet", shop_ly)
        shop_change = portal._ly_change(shop["projected"], 123456.78)
        self.assertIn(f"{abs(shop_change):.1f}% vs Nov 2025", shop_ly)
        self.assertIn("No last-year figure yet", food_ly)
        self.assertNotIn("$0.00", food_ly)
        self.assertIn("0.00 L", fuel_ly)
        self.assertNotIn("No last-year figure yet", fuel_ly)
        self.assertNotIn("% vs Nov 2025", fuel_ly)
        self.assertIn("Food not extracted yet", compared_card)
        c = server.db()
        untouched = dict(c.execute("SELECT shop_sales_inc, food_sales_inc FROM portal_ly_monthly WHERE month='2025-10'").fetchone())
        c.close()
        self.assertIsNone(untouched["shop_sales_inc"])
        self.assertEqual(untouched["food_sales_inc"], 40)

        future = self.authed("GET", "/portal?period=custom&from=2026-12-01&to=2026-12-31")[2].decode()
        future_card = future.split('id="month-lines"', 1)[1].split("</section>", 1)[0]
        self.assertIn("December 2026", future_card)
        self.assertIn("No shop sales loaded", future_card)
        self.assertIn("No kitchen sales loaded", future_card)
        self.assertIn("No fuel litres loaded", future_card)
        self.assertNotIn("$0.00", future_card.split('id="line-shop"', 1)[1].split('id="line-food"', 1)[0])
        self.assertNotIn("0.00 L", future_card.split('id="line-fuel"', 1)[1])

    def _clear_last_year_monthly(self):
        c = server.db()
        try:
            c.execute("DELETE FROM portal_ly_monthly")
            c.execute("DELETE FROM portal_meta WHERE k='last-year-monthly'")
            c.execute("DELETE FROM portal_snapshots WHERE dataset='last-year-monthly'")
            c.commit()
        finally:
            c.close()

    def test_owner_invoice_payments_survive_reingest(self):
        portal._ingest_hits.clear()
        self.login()
        c = server.db()
        c.execute("DELETE FROM portal_invoices")
        c.execute("DELETE FROM portal_invoice_payments")
        c.commit()
        c.close()
        code, _, raw, _ = self.ingest("invoices", load("invoices.json"))
        self.assertEqual(code, 200, raw[:300])
        page = self.authed("GET", "/portal/invoices")[2].decode()
        self.assertIn("Mark as paid", page)
        self.assertIn('name="method"', page)
        self.assertIn("Direct debit", page)
        self.assertIn('href="https://example.com/pay/bega/577803222"', page)
        token = self.csrf_from(page)
        form = urlencode({
            "csrf": token, "action": "save_payment", "supplier": "Bega Dairy", "invoice_number": "577803222",
            "method": "Cash", "paid_on": "2026-11-11", "amount": "", "note": "till float",
        })
        status, hdrs, body, _ = self.authed("POST", "/portal/invoices", form)
        self.assertEqual(status, 302, body[:300])
        self.assertIn("list=paid", hdrs.get("Location", ""))
        c = server.db()
        saved = dict(c.execute("SELECT * FROM portal_invoice_payments").fetchone())
        c.close()
        self.assertAlmostEqual(saved["amount"], 89.72, places=2)
        self.assertEqual(saved["method"], "Cash")
        self.assertEqual(saved["paid_on"], "2026-11-11")
        self.assertEqual(saved["note"], "till float")
        unpaid = self.authed("GET", "/portal/invoices")[2].decode()
        unpaid_list = unpaid.split('id="invoice-list"', 1)[1]
        self.assertNotIn("Bega Dairy", unpaid_list)
        self.assertIn('href="https://example.com/pay/smokestix/14027"', unpaid_list)
        paid = self.authed("GET", "/portal/invoices?list=paid")[2].decode()
        paid_list = paid.split('id="paid-list"', 1)[1]
        self.assertIn("Paid – awaiting Xero match", paid_list)
        self.assertIn("Cash", paid_list)
        self.assertIn("11 Nov 2026", paid_list)
        self.assertNotIn(">Pay<", paid_list)
        self.assertNotIn("https://example.com/pay/bega", paid)

        token = self.csrf_from(paid)
        edited = urlencode({
            "csrf": token, "action": "save_payment", "supplier": "Bega Dairy", "invoice_number": "577803222",
            "method": "Bank transfer", "paid_on": "2026-11-10", "amount": "90", "note": "bank",
        })
        status, _, body, _ = self.authed("POST", "/portal/invoices?list=paid", edited)
        self.assertEqual(status, 302, body[:200])
        c = server.db()
        saved = dict(c.execute("SELECT method, amount, note FROM portal_invoice_payments").fetchone())
        c.close()
        self.assertEqual(saved["method"], "Bank transfer")
        self.assertAlmostEqual(saved["amount"], 90, places=2)

        code, _, raw, _ = self.ingest("invoices", {"date": "2026-11-12", "invoices": [{
            "supplier": "Bega Dairy", "invoice_number": "577803222", "invoice_id": "xero-bega-1",
            "date": "2026-09-08", "due_date": "2026-09-15", "amount_due": 10, "status": "unpaid",
        }]})
        self.assertEqual(code, 200, raw[:300])
        c = server.db()
        kept = dict(c.execute("SELECT amount, method, note, xero_invoice_id FROM portal_invoice_payments").fetchone())
        c.close()
        self.assertAlmostEqual(kept["amount"], 90, places=2)
        self.assertEqual(kept["method"], "Bank transfer")
        self.assertEqual(kept["note"], "bank")
        self.assertEqual(kept["xero_invoice_id"], "xero-bega-1")
        waiting = json.loads(self.request("GET", "/api/portal/invoice-payments", headers={"Authorization": "Bearer test-ingest-key"})[2])
        self.assertEqual(waiting["payments"][0]["invoiceId"], "xero-bega-1")
        self.assertEqual(waiting["payments"][0]["invoiceNumber"], "577803222")
        self.assertEqual(waiting["payments"][0]["supplier"], "Bega Dairy")
        self.assertEqual(waiting["payments"][0]["method"], "Bank transfer")
        self.assertEqual(waiting["payments"][0]["date"], "2026-11-10")
        self.assertEqual(waiting["payments"][0]["note"], "bank")
        self.assertAlmostEqual(waiting["payments"][0]["amount"], 90, places=2)

        code, _, raw, _ = self.ingest("invoices", {"date": "2026-11-12", "sync": True, "invoices": [{
            "supplier": "Bega Dairy", "invoice_number": "577803222", "date": "2026-09-08",
            "due_date": "2026-09-15", "amount_due": 0, "amount": 90, "paid_date": "2026-11-11", "status": "paid",
        }]})
        self.assertEqual(code, 200, raw[:300])
        c = server.db()
        still = c.execute("SELECT amount, note, matched_at FROM portal_invoice_payments").fetchone()
        c.close()
        self.assertAlmostEqual(still["amount"], 90, places=2)
        self.assertEqual(still["note"], "bank")
        self.assertIsNone(still["matched_at"])
        matched_page = self.authed("GET", "/portal/invoices?list=paid")[2].decode()
        self.assertIn("Paid – matched in Xero", matched_page.split('id="paid-list"', 1)[1])
        self.assertIn("Bank transfer", matched_page)
        self.assertEqual(json.loads(self.request("GET", "/api/portal/invoice-payments", headers={"Authorization": "Bearer test-ingest-key"})[2])["payments"], [])
        token = self.csrf_from(matched_page)
        undo = urlencode({
            "csrf": token, "action": "undo_payment", "supplier": "Bega Dairy", "invoice_number": "577803222",
        })
        status, _, body, _ = self.authed("POST", "/portal/invoices?list=paid", undo)
        self.assertEqual(status, 400, body[:300])
        self.assertIn(b"matched in Xero", body)
        c = server.db()
        self.assertIsNotNone(c.execute("SELECT 1 FROM portal_invoice_payments").fetchone())
        c.close()

        fresh = self.ingest("invoices", {"date": "2026-11-12", "invoices": [{
            "supplier": "Tully Pies", "invoice_number": "TILL8122", "date": "2026-11-10",
            "due_date": "2026-12-10", "amount_due": 118.50, "status": "unpaid",
        }]})
        self.assertEqual(fresh[0], 200, fresh[2][:300])
        token = self.csrf_from(self.authed("GET", "/portal/invoices")[2].decode())
        bad_note = urlencode({
            "csrf": token, "action": "save_payment", "supplier": "Tully Pies", "invoice_number": "TILL8122",
            "method": "Card", "paid_on": "2026-11-11", "amount": "", "note": "4242424242424242",
        })
        refused = self.authed("POST", "/portal/invoices", bad_note)
        self.assertEqual(refused[0], 400)
        self.assertIn(b"card", refused[2].lower())
        c = server.db()
        self.assertIsNone(c.execute("SELECT 1 FROM portal_invoice_payments WHERE supplier='Tully Pies'").fetchone())
        c.close()
        token = self.csrf_from(self.authed("GET", "/portal/invoices")[2].decode())
        mark = urlencode({
            "csrf": token, "action": "save_payment", "supplier": "Tully Pies", "invoice_number": "TILL8122",
            "method": "Direct debit", "paid_on": "2026-11-11", "amount": "118.50", "note": "",
        })
        self.assertEqual(self.authed("POST", "/portal/invoices", mark)[0], 302)
        listed = json.loads(self.request("GET", "/api/portal/invoice-payments", headers={"Authorization": "Bearer test-ingest-key"})[2])
        self.assertEqual(listed["payments"][0]["supplier"], "Tully Pies")
        payload = json.dumps({
            "supplier": "Tully Pies", "invoiceNumber": "TILL8122", "xeroPaymentReference": "BANK-1",
        }).encode()
        status, _, body, _ = self.request("POST", "/api/portal/invoice-payments/match", payload, {
            "Content-Type": "application/json", "Authorization": "Bearer test-ingest-key", "Content-Length": str(len(payload)),
        })
        self.assertEqual(status, 200, body[:300])
        matched = json.loads(body)
        self.assertTrue(matched["ok"])
        self.assertEqual(matched["xeroPaymentReference"], "BANK-1")
        self.assertFalse(matched["alreadyMatched"])
        self.assertEqual(matched["invoiceNumber"], "TILL8122")
        status, _, body, _ = self.request("POST", "/api/portal/invoice-payments/match", payload, {
            "Content-Type": "application/json", "Authorization": "Bearer test-ingest-key", "Content-Length": str(len(payload)),
        })
        self.assertEqual(status, 200, body[:300])
        self.assertTrue(json.loads(body)["alreadyMatched"])
        self.assertEqual(json.loads(body)["xeroPaymentReference"], "BANK-1")
        self.assertEqual(json.loads(self.request("GET", "/api/portal/invoice-payments", headers={"Authorization": "Bearer test-ingest-key"})[2])["payments"], [])
        token = self.csrf_from(self.authed("GET", "/portal/invoices?list=paid")[2].decode())
        again = urlencode({
            "csrf": token, "action": "save_payment", "supplier": "Tully Pies", "invoice_number": "TILL8122",
            "method": "Cash", "paid_on": "2026-11-11", "amount": "1", "note": "",
        })
        status, _, body, _ = self.authed("POST", "/portal/invoices?list=paid", again)
        self.assertEqual(status, 400)
        self.assertIn(b"matched in Xero", body)

        status, _, body, _ = self.request("GET", "/api/portal/invoice-payments", headers={"Authorization": "Bearer nope"})
        self.assertEqual(status, 401)
        self.assertNotIn(b"nope", body)
        status, _, body, _ = self.request("POST", "/api/portal/invoice-payments/match", payload, {
            "Content-Type": "application/json", "Authorization": "Bearer nope", "Content-Length": str(len(payload)),
        })
        self.assertEqual(status, 401)
        self.assertNotIn(b"nope", body)
        os.environ["PORTAL_INGEST_KEY"] = ""
        try:
            status, _, body, _ = self.request("GET", "/api/portal/invoice-payments")
            self.assertEqual(status, 503)
            self.assertNotIn(b"test-ingest-key", body)
        finally:
            os.environ["PORTAL_INGEST_KEY"] = "test-ingest-key"

    def test_purchase_filters_and_invoice_supplier(self):
        portal._ingest_hits.clear()
        self.login()
        self.ingest("purchases", load("purchases.json"))
        self.ingest("invoices", load("invoices.json"))
        page = self.authed("GET", "/portal/purchases")[2].decode()
        self.assertIn("This week", page)
        self.assertIn("This month", page)
        self.assertIn("Last month", page)
        self.assertIn("All suppliers", page)
        self.assertIn('id="purchase-filters"', page)
        self.assertIn(">Clear<", page)
        self.assertIn("TILL8122", page.split('id="purchase-bills"', 1)[1])
        week = self.authed("GET", "/portal/purchases?period=week")[2].decode()
        week_bills = week.split('id="purchase-bills"', 1)[1].split('id="purchase-weeks"', 1)[0]
        self.assertIn("TILL8122", week_bills)
        self.assertNotIn("TILL8019", week_bills)
        last = self.authed("GET", "/portal/purchases?period=lastmonth")[2].decode()
        last_bills = last.split('id="purchase-bills"', 1)[1].split('id="purchase-weeks"', 1)[0]
        self.assertIn("TILL8062", last_bills)
        self.assertNotIn("TILL8122", last_bills)
        custom = self.authed("GET", "/portal/purchases?period=custom&from=2026-11-10&to=2026-11-10")[2].decode()
        custom_bills = custom.split('id="purchase-bills"', 1)[1].split('id="purchase-weeks"', 1)[0]
        self.assertIn("TILL8122", custom_bills)
        self.assertNotIn("TILL8109", custom_bills)
        one = self.authed("GET", "/portal/purchases?period=month&supplier=Tully+Pies")[2].decode()
        self.assertIn('value="Tully Pies" checked', one)
        one_bills = one.split('id="purchase-bills"', 1)[1].split('id="purchase-weeks"', 1)[0]
        compare = one.split('id="purchase-compare"', 1)[1].split('id="purchase-bills"', 1)[0]
        summary = one.split('id="purchase-summary"', 1)[1].split('id="purchase-compare"', 1)[0]
        self.assertIn("TILL8122", one_bills)
        self.assertNotIn("578066285", one_bills)
        self.assertIn("Tully Pies", compare)
        self.assertNotIn("Bega Dairy", compare)
        self.assertIn("whole shop", one.split('id="purchase-weeks"', 1)[1])
        c = server.db()
        anchor = portal.anchor_day(c)
        _active, start, end = portal.period_bounds("month", None, None, anchor)
        rows = portal.bills_between(c, start, end, True, ["Tully Pies"])
        c.close()
        self.assertIn(portal.money(sum(row["inc_gst"] or 0 for row in rows)), summary)

        inv = self.authed("GET", "/portal/invoices?supplier=Bega+Dairy")[2].decode()
        listing = inv.split('id="invoice-list"', 1)[1]
        self.assertIn("Bega Dairy", listing)
        self.assertIn('href="https://example.com/pay/bega/577803222"', listing)
        self.assertNotIn("SMOKESTIX", listing)
        self.assertIn("Mark as paid", listing)
        c = server.db()
        unpaid_rows, _paid_rows = portal.invoice_lists(c, portal.today())
        c.close()
        bega = [row for row in unpaid_rows if row["supplier"] == "Bega Dairy"]
        owing = inv.split('id="unpaid-total"', 1)[1].split('id="overdue-total"', 1)[0]
        self.assertIn(portal.money(sum(row["amount_due"] or 0 for row in bega)), owing)

    def test_daily_summary_keeps_overview_month_figures(self):
        portal._ingest_hits.clear()
        self.login()
        for name in ("sales", "fuel-commission", "fuel-dips", "invoices", "purchases"):
            code, _, raw, _ = self.ingest(name, load(name + ".json"))
            self.assertEqual(code, 200, raw[:300])
        c = server.db()
        c.execute("DELETE FROM hotbox_items WHERE date='2026-11-10'")
        c.execute("DELETE FROM audit WHERE date='2026-11-10'")
        c.commit()
        through = portal.data_through(c)
        c.close()
        home = self.authed("GET", "/portal")[2].decode()
        self.assertIn('id="day-glance"', home)
        self.assertIn("/portal/day?date=2026-11-10", home)
        self.assertIn("/portal/day?date=2026-11-11", home)
        self.assertIn("Fuel vs shop", home)
        self.assertIn("AI insights", home)
        self.assertIn('id="month-lines"', home)
        self.assertIn('id="last-year"', home)
        self.assertIn('id="overview-exceptions"', home)
        self.assertNotIn("<h2>Daily sales</h2>", home)
        opened = self.authed("GET", "/portal/day")[2].decode()
        self.assertIn(portal.fmt_date(through), opened)
        self.assertIn('href="/portal/day" aria-current="page"', opened)
        day = self.authed("GET", "/portal/day?date=2026-11-10")[2].decode()
        self.assertIn("<h2>Daily sales</h2>", day)
        self.assertIn("Previous day", day)
        self.assertIn("Next day", day)
        self.assertIn('value="2026-11-10"', day)
        self.assertIn("Tully Pies", day.split('id="day-invoices"', 1)[1])
        self.assertIn("TILL8122", day.split('id="day-invoices"', 1)[1])
        self.assertIn("Unleaded", day.split('id="day-dips"', 1)[1])
        self.assertIn("No staff hours for this day.", day.split('id="day-hours"', 1)[1])
        self.assertIn("No closing checklist or food activity for this day.", day.split('id="day-activity"', 1)[1])
        bad = self.authed("GET", "/portal/day?date=nope")[2].decode()
        self.assertIn("YYYY-MM-DD", bad)

    def test_product_supplier_survives_ingest(self):
        portal._ingest_hits.clear()
        self.login()
        self.assertEqual(self.ingest("product-search", load("product-search.json"))[0], 200)
        self.assertEqual(self.ingest("purchases", load("purchases.json"))[0], 200)
        self.assertEqual(self.ingest("invoices", load("invoices.json"))[0], 200)
        self.assertEqual(self.ingest("stock-movements", load("stock-movements.json"))[0], 200)
        self.assertEqual(portal._supplier_from_invoice([("2026-01-01", "A"), ("2026-01-02", "B")]), "")
        self.assertEqual(
            portal._supplier_from_invoice([("2026-01-01", "Tully Pies"), ("2026-02-01", "tully pies")]),
            "tully pies",
        )

        def supplier_block(page, code):
            return page.split(f'id="supplier-{code}"', 1)[1].split("</form>", 1)[0]

        def post(path, fields):
            page = self.authed("GET", path)[2].decode()
            fields = dict(fields)
            fields["csrf"] = self.csrf_from(page)
            target = path.split("?", 1)[0]
            return self.authed("POST", target, urlencode(fields))

        try:
            road = self.authed("GET", "/portal/products?dept=Roadhouse")[2].decode()
            hinted = supplier_block(road, "804040")
            self.assertIn("Tully Pies", hinted)
            self.assertIn('class="pill">Suggested', hinted)
            self.assertIn('value="Tully Pies"', hinted)
            self.assertIn('placeholder="Search suppliers"', road)
            self.assertIn('data-supplier="Unassigned"', road)
            self.assertIn("Print list", road)
            self.assertIn('id="known-suppliers"', road)
            self.assertNotIn("test-ingest-key", road)
            self.assertNotIn(PASSWORD, road)

            grocery = self.authed("GET", "/portal/products?dept=Grocery&supplier=Unassigned")[2].decode()
            self.assertIn("ARNOTTS SHAPES", grocery.split('id="dept-products"', 1)[1])
            missed = self.authed("GET", "/portal/products?dept=Roadhouse&supplier=Unassigned")[2].decode()
            self.assertNotIn("BACON EGG MUFFIN", missed.split('id="dept-products"', 1)[1])

            self.assertEqual(self.ingest("stock-movements", {"asOf": "2026-11-11", "movements": [
                {"code": "804040", "date": "2026-11-11", "source": "supplier-invoice", "sold": 0, "received": 1, "reference": "577803222"},
            ]})[0], 200)
            later = supplier_block(self.authed("GET", "/portal/products?dept=Roadhouse")[2].decode(), "804040")
            self.assertIn("Bega Dairy", later)
            self.assertIn('class="pill">Suggested', later)
            self.assertNotIn('value="Tully Pies"', later)

            status, hdrs, _, _ = post("/portal/products?dept=Roadhouse", {
                "action": "set_supplier", "code": "804040", "supplier": "Yamagishism",
                "back": "https://evil.example/phish",
            })
            self.assertEqual(status, 302, hdrs)
            self.assertTrue(hdrs.get("Location", "").startswith("/portal/products"))
            self.assertNotIn("evil", hdrs.get("Location", ""))
            saved = self.authed("GET", "/portal/products?dept=Roadhouse")[2].decode()
            block = supplier_block(saved, "804040")
            self.assertIn('value="Yamagishism"', block)
            self.assertNotIn('class="pill">Suggested', block)
            self.assertIn("Invoices suggest Bega Dairy", block)
            self.assertIn('value="Yamagishism"', saved)

            for dataset, payload in (
                ("product-search", load("product-search.json")),
                ("purchases", load("purchases.json")),
                ("invoices", load("invoices.json")),
                ("stock-movements", load("stock-movements.json")),
            ):
                self.assertEqual(self.ingest(dataset, payload)[0], 200)
            c = server.db()
            row = c.execute("SELECT supplier FROM portal_product_suppliers WHERE code='804040'").fetchone()
            c.close()
            self.assertEqual(row["supplier"], "Yamagishism")
            kept = supplier_block(self.authed("GET", "/portal/products?dept=Roadhouse")[2].decode(), "804040")
            self.assertIn('value="Yamagishism"', kept)
            self.assertNotIn('class="pill">Suggested', kept)
            c = server.db()
            c.execute("DELETE FROM portal_stock_movements WHERE move_date='2026-11-11'")
            c.commit()
            c.close()

            one = self.authed("GET", "/portal/products?dept=Roadhouse&supplier=Yamagishism")[2].decode()
            self.assertIn("BACON EGG MUFFIN", one.split('id="dept-products"', 1)[1])
            other = self.authed("GET", "/portal/products?dept=Roadhouse&supplier=Bega+Dairy")[2].decode()
            self.assertNotIn("BACON EGG MUFFIN", other.split('id="dept-products"', 1)[1])
            grouped = self.authed("GET", "/portal/products?dept=Roadhouse&arrange=supplier")[2].decode()
            self.assertIn('class="supplier-head"', grouped)
            self.assertIn("Yamagishism", grouped.split('class="supplier-head"', 1)[1].split("</tr>", 1)[0])

            status, hdrs, _, _ = post("/portal/products?dept=Roadhouse", {
                "action": "set_supplier", "code": "804040", "supplier": "",
                "back": "dept=Roadhouse&period=month",
            })
            self.assertEqual(status, 302, hdrs)
            self.assertIn("dept=Roadhouse", hdrs.get("Location", ""))
            self.assertIn("ok=supplier", hdrs.get("Location", ""))
            cleared = supplier_block(self.authed("GET", hdrs["Location"])[2].decode(), "804040")
            self.assertIn("Tully Pies", cleared)
            self.assertIn('class="pill">Suggested', cleared)

            for code, day in (("804040", "2026-11-01"), ("10000", "2026-11-01")):
                status, hdrs, body, _ = post("/portal/stock", {
                    "action": "set_count", "code": code, "qty": "4",
                    "baseline_date": day, "baseline_time": "23:59", "note": "supplier-test",
                })
                self.assertEqual(status, 302, body[:200])
            stock = self.authed("GET", "/portal/stock")[2].decode()
            self.assertIn('id="stock-scan"', stock)
            self.assertIn("Scan barcode", stock)
            self.assertIn("Print list", stock)
            self.assertIn('data-supplier-filter', stock)
            stock_hint = supplier_block(stock, "804040")
            self.assertIn("Tully Pies", stock_hint)
            self.assertIn('class="pill">Suggested', stock_hint)
            shapes = supplier_block(stock, "10000")
            self.assertIn("Unassigned", shapes)

            status, hdrs, _, _ = post("/portal/stock", {
                "action": "set_supplier", "code": "10000", "supplier": "Woolworths",
                "back": "filter=all",
            })
            self.assertEqual(status, 302, hdrs)
            self.assertTrue(hdrs.get("Location", "").startswith("/portal/stock"))
            both = self.authed("GET", "/portal/stock?supplier=Woolworths&supplier=Unassigned")[2].decode()
            self.assertIn('id="stock-10000"', both)
            self.assertNotIn('id="stock-804040"', both)
            named = self.authed("GET", "/portal/stock?sort=name")[2].decode()
            self.assertLess(named.index('id="stock-10000"'), named.index('id="stock-804040"'))
            by_supplier = self.authed("GET", "/portal/stock?sort=supplier")[2].decode()
            self.assertLess(by_supplier.index('id="stock-804040"'), by_supplier.index('id="stock-10000"'))
            self.assertIn('class="supplier-head"', by_supplier)
            api = json.loads(self.request("GET", "/api/portal/stock", headers={"Authorization": "Bearer test-ingest-key"})[2])
            shapes_api = next(row for row in api["products"] if row["code"] == "10000")
            muffin_api = next(row for row in api["products"] if row["code"] == "804040")
            self.assertEqual(shapes_api["supplier"], "Woolworths")
            self.assertEqual(shapes_api["supplierSource"], "manual")
            self.assertEqual(muffin_api["supplier"], "Tully Pies")
            self.assertEqual(muffin_api["supplierSource"], "suggested")

            bad = post("/portal/stock", {
                "action": "set_supplier", "code": "10000", "supplier": "4111111111111111", "back": "filter=all",
            })
            self.assertEqual(bad[0], 400)
            self.assertIn("card", bad[2].decode().lower())
            c = server.db()
            still = c.execute("SELECT supplier FROM portal_product_suppliers WHERE code='10000'").fetchone()["supplier"]
            c.close()
            self.assertEqual(still, "Woolworths")
            unknown = post("/portal/products", {"action": "set_supplier", "code": "NO-SUCH", "supplier": "Bega Dairy"})
            self.assertEqual(unknown[0], 400)

            status, hdrs, _, _ = post("/portal/stock", {
                "action": "add_manual", "name": "Order sauce", "code": "SUP1", "barcode": "", "group": "Kitchen",
                "supplier": "Bega Dairy", "qty": "2", "baseline_date": "2026-11-01", "baseline_time": "23:59",
            })
            self.assertEqual(status, 302, hdrs)
            status, hdrs, _, _ = post("/portal/stock?edit=SUP1", {
                "action": "edit_manual", "old_code": "SUP1", "code": "SUP2", "name": "Order sauce",
                "barcode": "", "group": "Kitchen", "supplier": "Bega Dairy",
            })
            self.assertEqual(status, 302, hdrs)
            c = server.db()
            moved = c.execute("SELECT supplier FROM portal_product_suppliers WHERE code='SUP2'").fetchone()
            left = c.execute("SELECT supplier FROM portal_product_suppliers WHERE code='SUP1'").fetchone()
            c.close()
            self.assertEqual(moved["supplier"], "Bega Dairy")
            self.assertIsNone(left)
            detail = self.authed("GET", "/portal/products?product=804040")[2].decode()
            self.assertIn('id="supplier-804040"', detail.split('id="product-804040"', 1)[1])
            self.assertIn('id="stock-scan"', self.authed("GET", "/portal/stock")[2].decode())
        finally:
            self.ingest("stock-movements", load("stock-movements.json"))
            c = server.db()
            c.execute("DELETE FROM portal_product_suppliers")
            c.execute("DELETE FROM portal_stock_counts WHERE note='supplier-test'")
            c.execute("DELETE FROM portal_stock_movements WHERE move_date='2026-11-11'")
            for code in ("SUP1", "SUP2"):
                c.execute("DELETE FROM portal_stock_items WHERE code=?", (code,))
                c.execute("DELETE FROM portal_stock_counts WHERE code=?", (code,))
                c.execute("DELETE FROM portal_stock_movements WHERE code=?", (code,))
                c.execute("DELETE FROM portal_stock_audit WHERE code=?", (code,))
            c.commit()
            c.close()

    def test_last_year_plu_comparison(self):
        """November 2025 from the PLU report, and a month with nothing stored."""
        import portal_compare
        portal._ingest_hits.clear()
        self.login()
        self.addCleanup(self._restore_ly_plu)

        c = server.db()
        raw = c.execute("SELECT payload FROM portal_ly_plu WHERE month='2025-11'").fetchone()["payload"]
        c.close()
        stored = json.loads(raw)
        self.assertEqual(stored["totalInc"], 397328.81)
        self.assertEqual(stored["totalEx"], 368303.31)
        self.assertEqual(stored["shopInc"], 184188.71)
        self.assertEqual(stored["fuelInc"], 213140.10)
        self.assertEqual(stored["fuelLitres"], 115281.947)
        self.assertEqual(stored["days"], 30)
        grades = {row["grade"]: row for row in stored["fuelGrades"]}
        self.assertEqual(grades["DIS"]["salesInc"], 125029.59)
        self.assertEqual(grades["DIS"]["litres"], 66848.947)
        self.assertEqual(grades["ULP"]["salesInc"], 72443.88)
        self.assertEqual(grades["ULP"]["litres"], 40269.06)
        self.assertEqual(grades["P95"]["salesInc"], 15666.63)
        self.assertEqual(grades["P95"]["litres"], 8163.94)
        departments = {row["name"]: row["salesInc"] for row in stored["departments"]}
        self.assertEqual(departments["Roadhouse"], 64707.18)
        self.assertEqual(departments["Beverages"], 55078.72)
        self.assertEqual(departments["Tobacco"], 35896.78)
        self.assertEqual(departments["Confectionery"], 5406.36)
        self.assertEqual(departments["Ice Cream"], 3866.61)
        self.assertEqual(departments["Cafe"], 3167.47)
        self.assertEqual(departments["Ice"], 2420.15)
        self.assertAlmostEqual(sum(departments.values()), 397328.81, places=2)
        self.assertNotEqual(departments["Confectionery"], 5445.23)

        self.assertEqual(self.ingest("last-year-plu", {"month": "2025-10", "totalInc": 1}, key="nope")[0], 401)
        self.assertEqual(self.ingest("last-year-plu", {"months": [{"month": "2025-13", "totalInc": 1}]})[0], 400)
        self.assertEqual(self.ingest("last-year-plu", {"month": "2025-10", "months": [{"month": "2025-10"}]})[0], 400)
        pan = "4111111111111111"
        leaked = self.ingest("last-year-plu", {"month": "2025-10", "totalInc": 1, "note": pan})
        self.assertEqual(leaked[0], 400)
        self.assertNotIn(pan.encode(), leaked[2])
        self.assertEqual(self.ingest("last-year-plu", {"month": "2025-10", "totalInc": 1, "gp": 1})[0], 400)

        self.assertEqual(self.ingest("sales", load("sales.json"))[0], 200)
        self.assertEqual(self.ingest("fuel-commission", load("fuel-commission.json"))[0], 200)
        c = server.db()
        report = portal_compare.build(c, date(2026, 11, 1))
        c.close()
        self.assertTrue(report["loaded"])
        self.assertEqual(report["daysThis"], 10)
        self.assertEqual(report["daysLast"], 30)
        by_key = {row["key"]: row for row in report["rows"]}
        self.assertEqual(by_key["total"]["this"], Decimal("167270.80"))
        self.assertEqual(by_key["total"]["full"], Decimal("397328.81"))
        self.assertEqual(by_key["total"]["same"], portal_compare.prorate("397328.81", 10, 30))
        self.assertEqual(by_key["shop"]["full"], Decimal("184188.71"))
        self.assertEqual(by_key["fuel"]["full"], Decimal("213140.10"))
        self.assertEqual(by_key["dis"]["full"], Decimal("125029.59"))
        self.assertEqual(by_key["ulp"]["full"], Decimal("72443.88"))
        self.assertEqual(by_key["p95"]["full"], Decimal("15666.63"))
        self.assertEqual(by_key["dept-roadhouse"]["full"], Decimal("64707.18"))
        self.assertEqual(by_key["dept-roadhouse"]["this"], Decimal("21621.95"))
        self.assertNotIn("dept-confectionery-group-7", by_key)
        self.assertEqual(by_key["dept-confectionery"]["full"], Decimal("5406.36"))

        page = self.authed("GET", "/portal")[2].decode()
        section = page.split('id="year-compare"', 1)[1].split("</section>", 1)[0]
        self.assertIn("November 2026 vs November 2025", section)
        self.assertIn('value="2026-11"', section)
        self.assertIn("daily average", section)
        self.assertIn("10 loaded days", section)
        self.assertIn("compare-cards", section)
        self.assertIn("compare-wrap", section)
        for amount in (
            "397328.81", "213140.10", "184188.71", "125029.59", "72443.88", "15666.63",
            "64707.18", "55078.72", "35896.78", "5406.36", "3866.61", "3167.47", "2420.15",
            "167270.80",
        ):
            self.assertIn(portal.money(amount), section)
        self.assertIn(portal.litres("115281.947"), section)
        self.assertIn(portal.litres("66848.947"), section)
        self.assertIn(portal.money(by_key["total"]["same"]), section)
        self.assertIn(portal.money(by_key["dept-roadhouse"]["same"]), section)
        self.assertIn("% above", section)
        self.assertNotIn("Last year not loaded yet", section)
        self.assertNotIn("$5,445.23", section)

        september = self.authed("GET", "/portal?compare=2026-09")[2].decode()
        empty = september.split('id="year-compare"', 1)[1].split("</section>", 1)[0]
        self.assertIn("September 2026 vs September 2025", empty)
        self.assertIn("Last year not loaded yet", empty)
        self.assertIn("September 2025", empty)
        self.assertNotIn("$0.00", empty)
        self.assertNotIn("0.00 L", empty)
        self.assertIn("November 2026 · month-end projection", september)

        october = self.authed("GET", "/portal?period=lastmonth")[2].decode()
        oct_section = october.split('id="year-compare"', 1)[1].split("</section>", 1)[0]
        self.assertIn("October 2026 vs October 2025", oct_section)
        self.assertIn("Last year not loaded yet", oct_section)
        same = oct_section.split('id="yc-total-same"', 1)[1].split("</div>", 1)[0]
        self.assertNotIn("$0.00", same)
        self.assertNotIn("0.00 L", same)

        code, _, raw, _ = self.ingest("last-year-plu", {
            "month": "2025-10", "days": 31, "totalInc": 100, "shopInc": 40, "fuelInc": 60,
            "fuelLitres": 10, "source": "test sheet",
            "fuelGrades": [{"grade": "DIS", "name": "Diesel", "litres": 6, "salesInc": 30}],
            "departments": [{"name": "Roadhouse", "salesInc": 25}],
        })
        self.assertEqual(code, 200, raw)
        self.assertEqual(json.loads(raw)["months"], 1)
        c = server.db()
        still = json.loads(c.execute("SELECT payload FROM portal_ly_plu WHERE month='2025-11'").fetchone()["payload"])
        oct_row = json.loads(c.execute("SELECT payload FROM portal_ly_plu WHERE month='2025-10'").fetchone()["payload"])
        c.close()
        self.assertEqual(still["totalInc"], 397328.81)
        self.assertEqual(oct_row["totalInc"], 100)
        self.assertEqual(oct_row["shopInc"], 40)

        self.ingest("last-year-plu", {"month": "2025-11", "days": 30, "totalInc": 1, "shopInc": 1, "fuelInc": 1})
        c = server.db()
        portal_compare.seed(c)
        c.commit()
        kept = json.loads(c.execute("SELECT payload FROM portal_ly_plu WHERE month='2025-11'").fetchone()["payload"])
        october_kept = json.loads(c.execute("SELECT payload FROM portal_ly_plu WHERE month='2025-10'").fetchone()["payload"])
        c.close()
        self.assertEqual(kept["totalInc"], 1)
        self.assertEqual(october_kept["totalInc"], 100)

        compared = self.authed("GET", "/portal?period=lastmonth")[2].decode()
        oct_now = compared.split('id="year-compare"', 1)[1].split("</section>", 1)[0]
        self.assertIn(portal.money(100), oct_now)
        self.assertNotIn("Last year not loaded yet", oct_now)
        self.assertIn("daily average", oct_now)

    def _restore_ly_plu(self):
        import portal_compare
        c = server.db()
        try:
            c.execute("DELETE FROM portal_ly_plu")
            c.execute("DELETE FROM portal_meta WHERE k='last-year-plu'")
            c.execute("DELETE FROM portal_snapshots WHERE dataset='last-year-plu'")
            c.commit()
            portal_compare.seed(c)
            c.commit()
        finally:
            c.close()

    def tearDown(self):
        # The barcode-map test is the only one that writes this dataset. Clearing it here
        # keeps a failed assertion from leaking barcodes into later cases.
        c = server.db()
        try:
            codes = ("77001", "77003", "77004", "77005", "77006", "77008", "77099", "88001")
            marks = ",".join("?" for _ in codes)
            c.execute("DELETE FROM portal_blobs WHERE dataset='barcode-map'")
            c.execute("DELETE FROM portal_meta WHERE k='barcode-map'")
            c.execute(f"DELETE FROM portal_stock_counts WHERE code IN ({marks})", codes)
            c.execute(f"DELETE FROM portal_stock_audit WHERE code IN ({marks})", codes)
            c.execute(f"DELETE FROM portal_stock_items WHERE code IN ({marks})", codes)
            c.execute(
                "DELETE FROM portal_product_barcodes WHERE barcode IN ('9900000000082','012345000065','0012345000065','01234565')"
            )
            c.commit()
        finally:
            c.close()


if __name__ == "__main__":
    unittest.main()
