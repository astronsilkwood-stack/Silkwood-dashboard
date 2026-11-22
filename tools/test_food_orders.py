"""Kitchen order board, optional till ingest, and wait report. Uses a temp SQLite file."""
import gzip
import http.client
import json
import os
import re
import sys
import tempfile
import threading
import unittest
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parents[1]
os.environ["DATABASE_URL"] = ""
os.environ["DATABASE_URL_UNPOOLED"] = ""
os.environ["STAFF_PIN"] = "4242"
os.environ["SMTP_HOST"] = ""
os.environ["WEBHOOK_URL"] = ""
os.environ["PORTAL_COOKIE_SECURE"] = "0"
os.environ["PORTAL_INGEST_KEY"] = "test-ingest-key"
os.environ["PORTAL_ADMIN_USER"] = "astron"
os.environ["PORTAL_INSIGHTS_GENERATE"] = ""
os.environ["SILKWOOD_DB"] = str(Path(tempfile.mkdtemp()) / "silkwood.db")
sys.path.insert(0, str(ROOT))
from argon2 import PasswordHasher

PASSWORD = "correct-horse-battery"
os.environ["PORTAL_ADMIN_PASSWORD_HASH"] = PasswordHasher(
    time_cost=2, memory_cost=19456, parallelism=1
).hash(PASSWORD)
import food_orders
import server

PIN = "4242"
HELD_AT = "2026-11-22T12:10:00+10:00"
RUNG_AT = "2026-11-22T12:04:00+10:00"


class Clock:
    when = datetime(2026, 11, 22, 12, 0, tzinfo=server.TZ)


server.now = lambda: Clock.when


class FoodOrderTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        server.STAFF_PIN = PIN
        server.init()
        cls.httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.H)
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()

    def setUp(self):
        Clock.when = datetime(2026, 11, 22, 12, 0, tzinfo=server.TZ)
        os.environ["PORTAL_INGEST_KEY"] = "test-ingest-key"
        food_orders.RATE_MAX = 60
        food_orders._hits.clear()
        c = server.db()
        c.execute("DELETE FROM food_order_undo")
        c.execute("DELETE FROM food_orders")
        c.execute("DELETE FROM food_order_receipts")
        c.execute("DELETE FROM days")
        c.commit()
        c.close()
        self.jar = {}

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        hdrs = dict(headers or {})
        if body is not None and not isinstance(body, bytes):
            body = body.encode()
        conn.request(method, path, body=body, headers=hdrs)
        resp = conn.getresponse()
        raw = resp.read()
        simple = {key: value for key, value in resp.getheaders()}
        cookies = [value for key, value in resp.getheaders() if key.lower() == "set-cookie"]
        conn.close()
        return resp.status, simple, raw, cookies

    def post_staff(self, path, payload, pin=PIN):
        raw = json.dumps(payload).encode()
        code, _, body, _ = self.request("POST", path, raw, {
            "Content-Type": "application/json",
            "Content-Length": str(len(raw)),
            "X-Staff-Pin": pin,
        })
        return code, json.loads(body.decode() or "{}")

    def ingest(self, payload, key="test-ingest-key", headers=None):
        raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        hdrs = {
            "Content-Type": "application/json",
            "Content-Length": str(len(raw)),
            "Authorization": "Bearer " + key,
        }
        hdrs.update(headers or {})
        code, _, body, _ = self.request("POST", "/api/portal/food-orders", raw, hdrs)
        return code, json.loads(body.decode() or "{}"), body

    def receipts(self):
        c = server.db()
        try:
            return [dict(row) for row in c.execute("SELECT * FROM food_order_receipts ORDER BY id")]
        finally:
            c.close()

    def kitchen(self):
        code, day = self.post_staff("/api/food-order", {"staff": "Sam", "source": "counter"})
        # helper is not this; use GET
        return code, day

    def day(self):
        code, _, body, _ = self.request("GET", "/api/day", headers={"X-Staff-Pin": PIN})
        return code, json.loads(body.decode())

    def test_manual_board_does_not_need_the_till(self):
        os.environ["PORTAL_INGEST_KEY"] = ""
        try:
            code, body, _ = self.ingest({"orders": []}, key="")
            self.assertEqual(code, 503)
            self.assertEqual(body["error"], "ingest is not configured")
            code, day = self.post_staff("/api/food-order", {"staff": "Sam"})
            self.assertEqual(code, 200, day)
            self.assertEqual(day["food_orders"][0]["ticket"], 1)
            self.assertEqual(day["food_orders"][0]["source"], "counter")
            self.assertEqual(day["food_orders"][0]["customer"], "")
            self.assertEqual(day["food_orders"][0]["started_at"], "2026-11-22 12:00:00")
        finally:
            os.environ["PORTAL_INGEST_KEY"] = "test-ingest-key"

    def test_new_order_number_phone_name_and_skip(self):
        code, day = self.post_staff("/api/food-order", {"staff": "Sam", "source": "phone", "customer": "  Sam  Lee "})
        self.assertEqual(code, 200, day)
        first = day["food_orders"][0]
        self.assertEqual(first["ticket"], 1)
        self.assertEqual(first["source"], "phone")
        self.assertEqual(first["customer"], "Sam Lee")
        self.assertEqual(day["food_order_undo"]["kind"], "create")
        code, day = self.post_staff("/api/food-order", {"staff": "Sam", "source": "counter", "customer": "   "})
        self.assertEqual([row["ticket"] for row in day["food_orders"]], [1, 2])
        self.assertEqual(day["food_orders"][1]["customer"], "")
        self.assertEqual(day["food_orders"][0]["source"], "phone")
        code, body = self.post_staff("/api/food-order", {"staff": "Sam", "source": "bopple"})
        self.assertEqual(code, 400)
        self.assertIn("counter or phone", body["error"])
        code, body = self.post_staff("/api/food-order", {"source": "counter"})
        self.assertEqual(code, 400)
        self.assertTrue(body.get("name"))
        code, body = self.post_staff("/api/food-order", {"staff": "Sam"}, pin="nope")
        self.assertEqual(code, 401)

    def test_done_undo_and_a_locked_day(self):
        _, day = self.post_staff("/api/food-order", {"staff": "Sam", "source": "counter"})
        first = day["food_orders"][0]["id"]
        _, day = self.post_staff("/api/food-order", {"staff": "Sam", "source": "phone", "customer": "Alex"})
        second = day["food_orders"][-1]["id"]
        code, body = self.post_staff("/api/food-order/undo", {"staff": "Sam", "id": first})
        self.assertEqual(code, 409, body)
        code, day = self.post_staff("/api/food-order/undo", {"staff": "Sam", "id": second})
        self.assertEqual(code, 200, day)
        self.assertEqual([row["ticket"] for row in day["food_orders"]], [1])
        self.assertIsNone(day["food_order_undo"])
        code, day = self.post_staff("/api/food-order", {"staff": "Sam"})
        self.assertEqual(day["food_orders"][-1]["ticket"], 2)
        code, body = self.post_staff("/api/food-order/undo", {"staff": "Sam", "id": day["food_order_undo"]["id"]})
        self.assertEqual(code, 200)
        self.assertEqual([row["ticket"] for row in body["food_orders"]], [1])
        _, day = self.post_staff("/api/food-order", {"staff": "Sam"})
        ticket = day["food_orders"][-1]
        self.assertEqual(ticket["ticket"], 2)
        Clock.when = datetime(2026, 11, 22, 12, 16, tzinfo=server.TZ)
        code, day = self.post_staff("/api/food-order/done", {"staff": "Sam", "id": ticket["id"]})
        self.assertEqual(code, 200, day)
        self.assertNotIn(ticket["id"], [row["id"] for row in day["food_orders"]])
        self.assertEqual(day["food_order_undo"]["kind"], "done")
        code, day = self.post_staff("/api/food-order/undo", {"staff": "Sam", "id": ticket["id"]})
        self.assertEqual(code, 200, day)
        self.assertIn(ticket["id"], [row["id"] for row in day["food_orders"]])
        self.assertIsNone(day["food_order_undo"])
        code, body = self.post_staff("/api/food-order/undo", {"staff": "Sam", "id": ticket["id"]})
        self.assertEqual(code, 404, body)
        Clock.when = datetime(2026, 11, 22, 12, 20, tzinfo=server.TZ)
        _, day = self.post_staff("/api/food-order/done", {"staff": "Sam", "id": ticket["id"]})
        Clock.when = datetime(2026, 11, 22, 12, 26, tzinfo=server.TZ)
        code, body = self.post_staff("/api/food-order/undo", {"staff": "Sam", "id": ticket["id"]})
        self.assertEqual(code, 404, body)
        self.assertEqual(self.day()[1]["food_orders"][0]["id"], first)

        c = server.db()
        c.execute(
            "INSERT INTO days(date, staff, at, summary) VALUES(?,?,?,?)",
            ("2026-11-22", "Sam", "2026-11-22 12:00:00", "{}"),
        )
        c.commit()
        c.close()
        code, day = self.post_staff("/api/food-order", {"staff": "Sam", "date": "2026-11-22"})
        self.assertEqual(code, 200, day)
        self.assertTrue(any(row["ticket"] == 3 for row in day["food_orders"]))

    def test_screen_colours_and_existing_tabs_stay(self):
        html = self.request("GET", "/")[2].decode()
        self.assertIn("New order", html)
        self.assertIn("orderDone", html)
        self.assertIn('class="pill phone"', html)
        self.assertIn("/api/food-order", html)
        self.assertIn(">10*60000", html)
        self.assertIn(">15*60000", html)
        for label in ("Food Activity Log", "Cold Holding", "Kitchen Daily Checklist", "Closing Checklist", "Discards"):
            self.assertIn(label, html)
        self.assertIn("refreshDay()},15000)", html)

    def test_ingest_links_held_to_paid_without_a_kitchen_card(self):
        held = {
            "receipt": "H88",
            "orderId": "H88",
            "status": "held",
            "customer": "Sam Lee",
            "heldAt": HELD_AT,
            "source": "counter",
            "items": [{"name": "Fish and chips", "qty": 1}],
        }
        code, body, _ = self.ingest({"orders": [held]})
        self.assertEqual(code, 200, body)
        self.assertEqual(body["created"], 1)
        code, body, _ = self.ingest({"orders": [held]})
        self.assertEqual(body["unchanged"], 1)
        self.assertEqual(len(self.receipts()), 1)
        code, body, _ = self.ingest({"orders": [{
            "receipt": "10455",
            "link": "H88",
            "status": "paid",
            "paidAt": "2026-11-22T12:28:00+10:00",
            "source": "counter",
            "items": [{"name": "Fish and chips", "qty": 1}, {"name": "Chips", "qty": "2"}],
        }]})
        self.assertEqual(code, 200, body)
        self.assertEqual(body["updated"], 1)
        self.assertEqual(body["created"], 0)
        rows = self.receipts()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["receipt"], "10455")
        self.assertEqual(rows[0]["order_key"], "H88")
        self.assertEqual(rows[0]["held_receipt"], "H88")
        self.assertEqual(rows[0]["status"], "paid")
        self.assertEqual(rows[0]["rung_up"], "2026-11-22 12:10:00")
        self.assertEqual(rows[0]["customer"], "Sam Lee")
        self.assertEqual(json.loads(rows[0]["items"]), [
            {"name": "Fish and chips", "qty": 1},
            {"name": "Chips", "qty": 2},
        ])
        code, body, _ = self.ingest({"orders": [held]})
        self.assertEqual(body["unchanged"], 1, body)
        rows = self.receipts()
        self.assertEqual(rows[0]["status"], "paid")
        self.assertEqual(rows[0]["rung_up"], "2026-11-22 12:10:00")
        self.assertEqual(len(json.loads(rows[0]["items"])), 2)
        code, day = self.day()
        self.assertEqual(code, 200)
        self.assertEqual(day["food_orders"], [])

        code, body, _ = self.ingest({"orders": [{
            "receipt": 10421,
            "status": "paid",
            "rungUp": "2026-11-22T02:04:00Z",
            "source": "bopple",
            "items": [{"description": "Wrap", "quantity": 1}],
        }]})
        self.assertEqual(code, 200, body)
        self.assertEqual(body["created"], 1)
        stored = next(row for row in self.receipts() if row["receipt"] == "10421")
        self.assertEqual(stored["rung_up"], "2026-11-22 12:04:00")
        self.assertEqual(stored["source"], "bopple")
        code, day = self.day()
        self.assertEqual(day["food_orders"], [])

    def test_ingest_auth_rejects_secrets_and_a_bad_batch(self):
        good = {
            "receipt": "50",
            "status": "paid",
            "rungUp": RUNG_AT,
            "source": "counter",
            "items": [{"name": "Pie", "qty": 1}],
        }
        code, body, _ = self.ingest({"orders": [good]}, key="nope")
        self.assertEqual(code, 401, body)
        code, _, raw, _ = self.request("POST", "/api/portal/food-orders", json.dumps({"orders": [good]}).encode(), {
            "Content-Type": "application/json",
            "X-Staff-Pin": PIN,
        })
        self.assertEqual(code, 401)
        self.assertEqual(self.receipts(), [])
        code, body, _ = self.ingest({"orders": [good, {"receipt": "51", "card": "4111111111111111"}]})
        self.assertEqual(code, 400, body)
        self.assertEqual(self.receipts(), [])
        code, body, _ = self.ingest({"orders": [{
            "receipt": "52",
            "source": "counter",
            "rungUp": RUNG_AT,
            "items": [{"name": "4111111111111111", "qty": 1}],
        }]})
        self.assertEqual(code, 400, body)
        self.assertEqual(self.receipts(), [])
        code, body, _ = self.ingest({"orders": [{
            "receipt": "H1",
            "status": "held",
            "heldAt": HELD_AT,
            "source": "counter",
            "items": [{"name": "Pie", "qty": 1}],
        }]})
        self.assertEqual(code, 400, body)
        self.assertIn("customer", body["error"])
        code, body, _ = self.ingest({"orders": [{
            "receipt": "H2",
            "status": "held",
            "customer": "Pat",
            "source": "phone",
            "items": [{"name": "Pie", "qty": 1}],
        }]})
        self.assertEqual(code, 400, body)
        code, _, raw, _ = self.request(
            "POST", "/api/portal/ingest",
            json.dumps({"dataset": "food-orders", "data": {}}).encode(),
            {"Content-Type": "application/json", "Authorization": "Bearer test-ingest-key"},
        )
        self.assertEqual(code, 400)
        self.assertNotIn(b"test-ingest-key", raw)

        raw = gzip.compress(json.dumps({"orders": [good]}).encode())
        code, body, _ = self.ingest(raw, headers={"Content-Encoding": "gzip", "Content-Length": str(len(raw))})
        self.assertEqual(code, 200, body)
        self.assertEqual(body["created"], 1)
        code, body, _ = self.ingest({"orders": [good]})
        self.assertEqual(body["unchanged"], 1)
        self.assertEqual(len(self.receipts()), 1)

    def test_ingest_rate_limit(self):
        food_orders.RATE_MAX = 2
        payload = {"orders": [{
            "receipt": "70",
            "status": "paid",
            "rungUp": RUNG_AT,
            "source": "counter",
            "items": [{"name": "Pie", "qty": 1}],
        }]}
        headers = {"X-Forwarded-For": "203.0.113.9"}
        self.assertEqual(self.ingest(payload, headers=headers)[0], 200)
        payload["orders"][0]["receipt"] = "71"
        self.assertEqual(self.ingest(payload, headers=headers)[0], 200)
        payload["orders"][0]["receipt"] = "72"
        code, body, _ = self.ingest(payload, headers=headers)
        self.assertEqual(code, 429, body)
        self.assertEqual({row["receipt"] for row in self.receipts()}, {"70", "71"})

    def test_report_math_and_page(self):
        self.assertFalse(food_orders.over_15("2026-11-22 12:00:00", "2026-11-22 12:15:00"))
        self.assertTrue(food_orders.over_15("2026-11-22 12:00:00", "2026-11-22 12:15:01"))
        ids = []
        for index in range(5):
            payload = {"staff": "Sam", "source": "phone" if index == 0 else "counter"}
            if index == 0:
                payload["customer"] = "Alex"
            code, day = self.post_staff("/api/food-order", payload)
            self.assertEqual(code, 200, day)
            ids.append(day["food_orders"][-1]["id"])
        specs = [
            (ids[0], "2026-11-22 09:00:00", "2026-11-22 09:04:00"),
            (ids[1], "2026-11-22 12:00:00", "2026-11-22 12:08:00"),
            (ids[2], "2026-11-22 12:10:00", "2026-11-22 12:20:00"),
            (ids[3], "2026-11-22 12:30:00", "2026-11-22 12:46:00"),
            (ids[4], "2026-11-22 13:00:00", None),
        ]
        c = server.db()
        for order_id, start, done in specs:
            c.execute(
                "UPDATE food_orders SET started_at=?, trading_date=?, done_at=?, done_by=? WHERE id=?",
                (start, start[:10], done, "Sam" if done else None, order_id),
            )
        c.execute(
            """INSERT INTO food_order_receipts(
                 order_key, receipt, held_receipt, rung_up, trading_date, source, status, customer, items, paid_at, received_at, updated_at)
               VALUES('H88','10455','H88','2026-11-22 12:10:00','2026-11-22','counter','paid','Sam Lee','[]',
                      '2026-11-22 12:28:00','2026-11-22 12:40:00','2026-11-22 12:40:00')"""
        )
        c.commit()
        c.close()
        code, _, raw, _ = self.request("GET", "/api/portal/order-times?from=2026-11-22&to=2026-11-22")
        self.assertEqual(code, 401)
        code, _, raw, _ = self.request(
            "GET", "/api/portal/order-times?from=2026-11-22&to=2026-11-22",
            headers={"X-Staff-Pin": PIN},
        )
        self.assertEqual(code, 401)
        code, _, raw, _ = self.request(
            "GET", "/api/portal/order-times?from=2026-11-22&to=2026-11-22",
            headers={"Authorization": "Bearer test-ingest-key"},
        )
        self.assertEqual(code, 200, raw)
        report = json.loads(raw.decode())
        summary = report["summary"]
        self.assertEqual(summary["orders"], 5)
        self.assertEqual(summary["done"], 4)
        self.assertEqual(summary["open"], 1)
        self.assertEqual(summary["over15"], 1)
        self.assertEqual(summary["avgMin"], 9.5)
        self.assertEqual(summary["medianMin"], 9.0)
        self.assertEqual(summary["maxMin"], 16.0)
        self.assertEqual(report["days"][0]["date"], "2026-11-22")
        by_hour = {row["hour"]: row for row in report["hours"]}
        self.assertEqual(by_hour[9]["avgMin"], 4.0)
        self.assertEqual(by_hour[9]["over15"], 0)
        self.assertAlmostEqual(by_hour[12]["avgMin"], 11.3, places=1)
        self.assertEqual(by_hour[12]["medianMin"], 10.0)
        self.assertEqual(by_hour[12]["over15"], 1)
        self.assertEqual(by_hour[13]["open"], 1)
        self.assertIsNone(by_hour[13]["avgMin"])
        phone = next(row for row in report["orders"] if row["customer"] == "Alex")
        self.assertEqual(phone["source"], "phone")
        self.assertEqual(phone["waitMin"], 4.0)
        self.assertEqual(len(report["receipts"]), 1)
        self.assertEqual(report["receipts"][0]["orderId"], "H88")
        self.assertEqual(report["receipts"][0]["status"], "paid")
        narrow = self.request(
            "GET", "/api/portal/order-times?from=2026-11-21&to=2026-11-21",
            headers={"Authorization": "Bearer test-ingest-key"},
        )[2]
        self.assertEqual(json.loads(narrow.decode())["summary"]["orders"], 0)

        c = server.db()
        html = food_orders.page(c, {"period": "custom", "from": "2026-11-22", "to": "2026-11-22"})
        c.close()
        self.assertIn("9.5", html)
        self.assertIn("Alex", html)
        self.assertIn("Phone", html)
        self.assertIn("Not marked done", html)
        self.assertIn("Till receipts", html)
        self.assertIn("10455", html)
        over = html.split('id="order-times-over"', 1)[1].split("<span>", 1)[0]
        self.assertIn(">1<", over)
        c = server.db()
        c.execute("UPDATE food_orders SET customer=? WHERE id=?", ("A<B", ids[1]))
        c.commit()
        html = food_orders.page(c, {"period": "custom", "from": "2026-11-22", "to": "2026-11-22"})
        c.close()
        self.assertIn("A&lt;B", html)
        self.assertNotIn("A<B", html)

        status, hdrs, body, _ = self.request("GET", "/portal/order-times")
        self.assertEqual(status, 302)
        self.assertEqual(hdrs.get("Location"), "/portal/login")
        self.assertNotIn(b"9.5", body)

    def test_owner_page_lists_order_times(self):
        status, _, body, cookies = self.request("GET", "/portal/login")
        self.assertEqual(status, 200)
        token = re.search(r'name="csrf" value="([^"]+)"', body.decode()).group(1)
        jar = {}
        for raw in cookies:
            name, rest = raw.split("=", 1)
            jar[name] = rest.split(";", 1)[0]
        form = urlencode({"csrf": token, "username": "astron", "password": PASSWORD})
        status, _, _, cookies = self.request(
            "POST", "/portal/login", form,
            {"Content-Type": "application/x-www-form-urlencoded", "Cookie": "; ".join(f"{k}={v}" for k, v in jar.items())},
        )
        self.assertEqual(status, 302)
        for raw in cookies:
            name, rest = raw.split("=", 1)
            jar[name] = rest.split(";", 1)[0]
        status, _, body, _ = self.request(
            "GET", "/portal/order-times",
            headers={"Cookie": "; ".join(f"{k}={v}" for k, v in jar.items())},
        )
        self.assertEqual(status, 200)
        page = body.decode()
        self.assertIn("Order times", page)
        self.assertIn("Not marked done", page)
        self.assertIn("No kitchen orders in this range.", page)
        self.assertIn('href="/portal/order-times"', page)


if __name__ == "__main__":
    unittest.main()
