"""Public pickup orders: /order page and POST /api/order. Uses a temp SQLite file."""
import contextlib, io, json, os, sys, tempfile, threading, unittest, urllib.error, urllib.request
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.environ["DATABASE_URL"] = ""
os.environ["DATABASE_URL_UNPOOLED"] = ""
os.environ["STAFF_PIN"] = "9999"
os.environ["SMTP_HOST"] = ""
os.environ["ORDER_WEBHOOK_URL"] = ""
os.environ["ORDER_WEBHOOK_KEY"] = ""
os.environ["SILKWOOD_DB"] = str(Path(tempfile.mkdtemp()) / "silkwood.db")
sys.path.insert(0, str(ROOT))
import server

WHEN = datetime(2026, 10, 31, 9, 5, 6, tzinfo=server.TZ)
SECRET = "super-secret-order-key"
SNACK = 1631384  # 2 Piece Snack, $6.00
FISH = 1631385   # Fish & Chips, $13.00


class Capture(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass
    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n)
        self.server.hits.append({"auth": self.headers.get("Authorization"), "raw": raw, "body": json.loads(raw.decode())})
        msg = b"ok"
        self.send_response(self.server.status)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(msg)))
        self.end_headers()
        self.wfile.write(msg)


def order_body(**kw):
    body = {
        "type": "takeaway",
        "customer": {"name": "Sam Lee", "mobile": "0412 345 678", "email": "sam@example.com"},
        "pickup_date": "2026-10-31",
        "pickup_time": "12:30",
        "items": [
            {"id": SNACK, "qty": 2, "notes": "extra sauce", "price": 0.01},
            {"id": FISH, "qty": 1, "notes": "", "price": 1},
        ],
        "notes": "Please call when it's ready",
        "company_website": "",
    }
    body.update(kw)
    return body


class OrderTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        server.STAFF_PIN = "9999"
        server.init()
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.H)
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.wh = ThreadingHTTPServer(("127.0.0.1", 0), Capture)
        cls.wh.hits = []
        cls.wh.status = 200
        threading.Thread(target=cls.wh.serve_forever, daemon=True).start()
        cls.wh_url = f"http://127.0.0.1:{cls.wh.server_address[1]}/hook"

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.wh.shutdown()

    def setUp(self):
        c = server.db()
        c.execute("DELETE FROM orders")
        c.commit(); c.close()
        server._order_hits.clear()
        server.ORDER_RATE_MAX = 8
        server.STAFF_PIN = "9999"
        os.environ["ORDER_WEBHOOK_URL"] = ""
        os.environ["ORDER_WEBHOOK_KEY"] = ""
        self.wh.hits.clear()
        self.wh.status = 200

    def place(self, body, ip="203.0.113.8", when=WHEN):
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            code, res = server.place_order(body, ip, when)
        return code, res, buf.getvalue()

    def req(self, method, path, data=None, headers=None):
        hdrs = {"Content-Type": "application/json"} if data is not None else {}
        if headers:
            hdrs.update(headers)
        r = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=data, headers=hdrs, method=method)
        try:
            with urllib.request.urlopen(r, timeout=5) as resp:
                return resp.status, dict(resp.headers), resp.read()
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), e.read()

    def stored(self, oid):
        c = server.db()
        try:
            row = c.execute("SELECT payload, webhook_status, ip FROM orders WHERE id=?", (oid,)).fetchone()
        finally:
            c.close()
        return dict(row) if row else None

    def count(self):
        c = server.db()
        try:
            return c.execute("SELECT COUNT(*) AS n FROM orders").fetchone()["n"]
        finally:
            c.close()

    def test_page_is_public_and_has_no_staff_surface(self):
        code, hdrs, body = self.req("GET", "/order")
        self.assertEqual(code, 200)
        self.assertIn("text/html", hdrs["Content-Type"])
        html = body.decode()
        self.assertIn("Silkwood Takeaway – Order for pickup", html)
        self.assertIn("1 Silkwood Japoon Road, Silkwood QLD 4856", html)
        self.assertIn("(07) 4065 2263", html)
        self.assertIn("Takeaway pickup", html)
        self.assertIn("Catering / bulk order", html)
        self.assertIn("Pay at pickup", html)
        self.assertIn("No online payment", html)
        self.assertIn("Pay when you collect", html)
        self.assertIn("Same-day catering is fine", html)
        for banned in ("innerHTML", "document.write", "insertAdjacentHTML", "/api/day", "/api/history",
                       "/api/submit", "X-Staff-Pin", "STAFF_PIN", "9999", 'href="/"'):
            self.assertNotIn(banned, html)
        code, _, _ = self.req("GET", "/order/")
        self.assertEqual(code, 200)
        code, _, day = self.req("GET", "/api/day")
        self.assertEqual(code, 401)
        self.assertNotIn(b"logs", day)
        code, _, _ = self.req("GET", "/api/history")
        self.assertEqual(code, 401)
        code, _, _ = self.req("POST", "/api/log", json.dumps({"staff": "Sam", "kind": "prep", "data": {}}).encode())
        self.assertEqual(code, 401)
        sw = (ROOT / "static" / "sw.js").read_text()
        self.assertLess(sw.index("'/order'"), sw.index("r.mode==='navigate'"))

    def test_menu_json_and_images_are_public(self):
        code, hdrs, body = self.req("GET", "/static/order/menu.json")
        self.assertEqual(code, 200)
        self.assertIn("application/json", hdrs["Content-Type"])
        self.assertIn("max-age=60", hdrs["Cache-Control"])
        menu = json.loads(body)
        self.assertEqual(len(menu["categories"]), 11)
        items = [it for cat in menu["categories"] for it in cat["items"]]
        self.assertEqual(len(items), 120)
        self.assertEqual(len({it["id"] for it in items}), 120)
        for it in items:
            cents = server.to_cents(it["price"])
            self.assertEqual(server.money(cents), f"{it['price']:.2f}")
            self.assertEqual(cents, round(float(it["price"]) * 100))
            if it.get("image"):
                self.assertTrue((ROOT / "static" / "order" / it["image"]).is_file(), it["image"])
        code, hdrs, body = self.req("GET", "/static/order/img/1631384_2-Piece-Snack.webp")
        self.assertEqual(code, 200)
        self.assertEqual(hdrs["Content-Type"], "image/webp")
        self.assertTrue(body.startswith(b"RIFF"))
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", "/static/order/img/../../server.py")
        resp = conn.getresponse(); leaked = resp.read(); conn.close()
        self.assertNotEqual(resp.status, 200)
        self.assertNotIn(b"ORDER_WEBHOOK_KEY", leaked)
        self.assertNotIn(b"sqlite3", leaked)

    def test_webhook_payload_recomputes_prices(self):
        os.environ["ORDER_WEBHOOK_URL"] = self.wh_url
        os.environ["ORDER_WEBHOOK_KEY"] = SECRET
        code, res, err = self.place(order_body())
        self.assertEqual(code, 200, res)
        self.assertNotIn(SECRET, err)
        self.assertNotIn(SECRET, json.dumps(res))
        self.assertEqual(res["payment"], "Pay when you collect")
        self.assertEqual(res["id"], "SW-1031-001")
        self.assertEqual(res["total"], "25.00")
        self.assertEqual(set(res), {"ok", "id", "type", "pickup", "total", "payment", "items"})
        self.assertEqual(self.wh.hits[0]["auth"], "Bearer " + SECRET)
        row = self.stored(res["id"])
        self.assertEqual(self.wh.hits[0]["raw"].decode(), row["payload"])
        self.assertEqual(row["webhook_status"], "sent 200")
        self.assertEqual(row["ip"], "203.0.113.8")
        self.assertNotIn("ip", json.loads(row["payload"]))
        body = self.wh.hits[0]["body"]
        self.assertEqual(list(body), ["id", "type", "customer", "pickup", "items", "total", "notes", "created_at"])
        self.assertEqual(body["type"], "takeaway")
        self.assertEqual(body["customer"], {"name": "Sam Lee", "mobile": "0412345678", "email": "sam@example.com"})
        self.assertEqual(body["pickup"], "2026-10-31T12:30:00+10:00")
        self.assertEqual(body["created_at"], "2026-10-31T09:05:06+10:00")
        self.assertTrue(body["pickup"].endswith("+10:00") and body["created_at"].endswith("+10:00"))
        self.assertEqual(body["total"], "25.00")
        self.assertEqual(body["notes"], "Please call when it's ready")
        self.assertEqual(body["items"], [
            {"id": SNACK, "name": "2 Piece Snack", "qty": 2, "unit": "6.00", "line": "12.00", "notes": "extra sauce"},
            {"id": FISH, "name": "Fish & Chips", "qty": 1, "unit": "13.00", "line": "13.00", "notes": ""},
        ])
        self.assertEqual(list(body["items"][0]), ["id", "name", "qty", "unit", "line", "notes"])
        self.assertNotIn("0.01", row["payload"])
        code, res2, _ = self.place(order_body(type="catering", pickup_date="2026-11-02", pickup_time="10:30",
                                              customer={"name": "Sam Lee", "mobile": "+61 412 345 678", "email": ""},
                                              items=[{"id": str(SNACK), "qty": "2", "notes": ""}]))
        self.assertEqual(code, 200, res2)
        self.assertEqual(res2["id"], "SW-1031-002")
        self.assertEqual(res2["type"], "catering")
        second = json.loads(self.stored(res2["id"])["payload"])
        self.assertEqual(second["customer"]["mobile"], "0412345678")
        self.assertEqual(second["customer"]["email"], "")
        self.assertEqual(second["total"], "12.00")
        self.assertEqual(second["pickup"], "2026-11-02T10:30:00+10:00")

    def test_customer_text_is_not_html(self):
        nasty = "<script>alert(1)</script>"
        body = order_body(customer={"name": nasty, "mobile": "0412345678", "email": ""},
                          items=[{"id": SNACK, "qty": 1, "notes": "a\nb <b>"}],
                          notes="line1\nline2")
        code, res, _ = self.place(body)
        self.assertEqual(code, 200, res)
        raw = json.dumps(res)
        self.assertNotIn("<script>", raw)
        self.assertEqual(json.loads(self.stored(res["id"])["payload"])["customer"]["name"], nasty)
        item = json.loads(self.stored(res["id"])["payload"])["items"][0]
        self.assertEqual(item["notes"], "a b <b>")
        self.assertEqual(json.loads(self.stored(res["id"])["payload"])["notes"], "line1\nline2")

    def test_rejects_bad_carts_and_contact(self):
        cases = [
            order_body(items=[]),
            order_body(items=[{"id": 999999, "qty": 1}]),
            order_body(items=[{"id": SNACK, "qty": 1}, {"id": SNACK, "qty": 2}]),
            order_body(items=[{"id": SNACK, "qty": True}]),
            order_body(items=[{"id": SNACK, "qty": 1.5}]),
            order_body(items=[{"id": SNACK, "qty": 0}]),
            order_body(items=[{"id": SNACK, "qty": 31}]),
            order_body(type="catering", items=[{"id": SNACK, "qty": 201}]),
            order_body(type="dine-in"),
            order_body(customer={"name": "  ", "mobile": "0412345678", "email": ""}),
            order_body(customer={"name": "Sam", "mobile": "(07) 4065 2263", "email": ""}),
            order_body(customer={"name": "Sam", "mobile": "12345", "email": ""}),
            order_body(customer={"name": "Sam", "mobile": "0412345678", "email": "not-an-email"}),
            order_body(customer={"name": "S" * 81, "mobile": "0412345678", "email": ""}),
            order_body(pickup_date="2026-10-30", pickup_time="12:30"),
            order_body(pickup_date="2026-11-08", pickup_time="12:30"),
            order_body(pickup_time="10:15"),
            order_body(pickup_time="10:40"),
            order_body(pickup_time="19:30"),
            order_body(pickup_time="19:00:00"),
            {"type": "takeaway"},
        ]
        for body in cases:
            with self.subTest(body=body):
                code, res, _ = self.place(body)
                self.assertEqual(code, 400, res)
                self.assertIn("error", res)
                self.assertNotIn("<", res["error"])
        self.assertEqual(self.count(), 0)
        code, res, _ = self.place(order_body(items=[{"id": SNACK, "qty": 30}]))
        self.assertEqual(code, 200, res)
        self.assertEqual(res["total"], "180.00")
        code, res, _ = self.place(order_body(type="catering", items=[{"id": SNACK, "qty": 200}],
                                             customer={"name": "  Sam   Lee ", "mobile": "04-1234-5678", "email": "  "}))
        self.assertEqual(code, 200, res)
        saved = json.loads(self.stored(res["id"])["payload"])
        self.assertEqual(saved["customer"]["name"], "Sam Lee")
        self.assertEqual(saved["customer"]["mobile"], "0412345678")
        self.assertEqual(saved["customer"]["email"], "")
        self.assertEqual(saved["items"][0]["qty"], 200)
        self.assertEqual(saved["items"][0]["line"], "1200.00")
        self.assertEqual(saved["total"], "1200.00")

    def test_pickup_lead_time_and_seven_days(self):
        early = datetime(2026, 10, 31, 10, 10, tzinfo=server.TZ)
        code, res, _ = self.place(order_body(pickup_time="10:30"), when=early)
        self.assertEqual(code, 200, res)
        self.assertEqual(json.loads(self.stored(res["id"])["payload"])["pickup"], "2026-10-31T10:30:00+10:00")
        late = datetime(2026, 10, 31, 10, 10, 1, tzinfo=server.TZ)
        code, res, _ = self.place(order_body(pickup_time="10:30"), when=late)
        self.assertEqual(code, 400, res)
        self.assertIn("20 minutes", res["error"])
        code, res, _ = self.place(order_body(type="catering", pickup_time="19:15"), when=WHEN)
        self.assertEqual(code, 200, res)
        code, res, _ = self.place(order_body(pickup_date="2026-11-07", pickup_time="19:15"), when=WHEN)
        self.assertEqual(code, 200, res)
        self.assertTrue(res["id"].startswith("SW-1031-"))
        closed = datetime(2026, 10, 31, 23, 0, tzinfo=server.TZ)
        code, _, _ = self.place(order_body(pickup_time="19:15"), when=closed)
        self.assertEqual(code, 400)
        code, res, _ = self.place(order_body(pickup_date="2026-11-01", pickup_time="10:30"), when=closed)
        self.assertEqual(code, 200, res)
        for mobile in ("0412 345 678", "+61 412 345 678", "61412345678", "+61 (0) 412 345 678",
                       "0412\u00a0345\u00a0678", "0412.345.678"):
            with self.subTest(mobile=mobile):
                self.assertEqual(server.parse_mobile(mobile), "0412345678")

    def test_honeypot_skips_save_and_webhook(self):
        os.environ["ORDER_WEBHOOK_URL"] = self.wh_url
        os.environ["ORDER_WEBHOOK_KEY"] = SECRET
        code, res, err = self.place(order_body(company_website="http://spam.test"))
        self.assertEqual(code, 400, res)
        self.assertEqual(self.count(), 0)
        self.assertEqual(self.wh.hits, [])
        self.assertNotIn(SECRET, err)
        code, _, _ = self.place(order_body(company_website={"x": 1}))
        self.assertEqual(code, 400)
        self.assertEqual(self.count(), 0)

    def test_missing_or_failed_webhook_still_confirms(self):
        os.environ["ORDER_WEBHOOK_URL"] = self.wh_url
        os.environ["ORDER_WEBHOOK_KEY"] = ""
        code, res, err = self.place(order_body())
        self.assertEqual(code, 200, res)
        self.assertIn("not configured", err)
        self.assertNotIn(SECRET, err)
        self.assertEqual(self.wh.hits, [])
        self.assertEqual(self.stored(res["id"])["webhook_status"], "skipped")

        os.environ["ORDER_WEBHOOK_KEY"] = SECRET
        self.wh.status = 500
        code, res, err = self.place(order_body(customer={"name": "Ava", "mobile": "0499000111", "email": ""}))
        self.assertEqual(code, 200, res)
        self.assertTrue(self.stored(res["id"])["webhook_status"].startswith("failed"))
        self.assertIn("order webhook: failed", err)
        self.assertNotIn(SECRET, err)
        self.assertEqual(res["payment"], "Pay when you collect")

        os.environ["ORDER_WEBHOOK_URL"] = "http://127.0.0.1:9/hook"
        code, res, err = self.place(order_body(customer={"name": "Bo", "mobile": "0499000222", "email": ""}))
        self.assertEqual(code, 200, res)
        self.assertIn("failed", self.stored(res["id"])["webhook_status"])
        self.assertNotIn(SECRET, err)
        self.assertNotIn(self.wh_url, err)

    def test_rate_limit_body_size_and_forwarded_for(self):
        server.ORDER_RATE_MAX = 1
        code, _, _ = self.req("GET", "/order", headers={"X-Forwarded-For": "8.8.8.8, 1.1.1.1"})
        self.assertEqual(code, 200)
        code, _, _ = self.req("POST", "/api/order", b"{}", headers={"X-Forwarded-For": "8.8.8.8, 1.1.1.1"})
        self.assertEqual(code, 400)
        code, _, body = self.req("POST", "/api/order", b"{}", headers={"X-Forwarded-For": "9.9.9.9, 1.1.1.1"})
        self.assertEqual(code, 429)
        self.assertIn(b"Too many orders", body)
        code, _, _ = self.req("POST", "/api/order", b"{}", headers={"X-Forwarded-For": "8.8.8.8"})
        self.assertEqual(code, 400)
        code, _, _ = self.req("POST", "/api/order", b"x" * (server.ORDER_MAX_BODY + 1),
                              headers={"X-Forwarded-For": "203.0.113.9"})
        self.assertEqual(code, 413)
        self.assertEqual(self.count(), 0)

        class Fake:
            headers = {"X-Forwarded-For": "  8.8.8.8, 203.0.113.4  "}
            client_address = ("127.0.0.1", 1)
        self.assertEqual(server.client_ip(Fake()), "203.0.113.4")

    def test_http_order_needs_no_pin(self):
        os.environ["ORDER_WEBHOOK_URL"] = self.wh_url
        os.environ["ORDER_WEBHOOK_KEY"] = SECRET
        raw = json.dumps(order_body(items=[{"id": SNACK, "qty": 1, "notes": ""}])).encode()
        code, hdrs, body = self.req("POST", "/api/order", raw)
        self.assertEqual(code, 200, body)
        self.assertIn("application/json", hdrs["Content-Type"])
        self.assertEqual(hdrs["X-Content-Type-Options"], "nosniff")
        res = json.loads(body)
        self.assertTrue(res["ok"])
        self.assertEqual(res["total"], "6.00")
        self.assertEqual(self.wh.hits[0]["auth"], "Bearer " + SECRET)
        self.assertNotIn(b"shifts", body)
        self.assertNotIn(b"staff", body.lower())


if __name__ == "__main__":
    unittest.main()
