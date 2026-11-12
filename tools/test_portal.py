"""Owner portal: login, ingest, pages, invoices, and staff hours. Uses a temp SQLite file."""
import http.client, http.server, json, os, re, sys, tempfile, threading, unittest
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
        for path in ("/portal", "/portal/invoices", "/portal/hours", "/portal/purchases", "/portal/fuel", "/portal/dips", "/portal/hours.csv",
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


if __name__ == "__main__":
    unittest.main()
