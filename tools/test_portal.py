"""Owner portal: login, ingest, pages, invoices, and staff hours. Uses a temp SQLite file."""
import http.client, json, os, re, sys, tempfile, threading, unittest
from datetime import datetime, date
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
        for path in ("/portal", "/portal/invoices", "/portal/hours", "/portal/purchases", "/portal/fuel", "/portal/dips", "/portal/hours.csv"):
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


if __name__ == "__main__":
    unittest.main()
