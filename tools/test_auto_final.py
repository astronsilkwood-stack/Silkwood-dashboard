"""Auto end-of-day final, cold-holding list, and staff-facing copy. Uses a temp SQLite file."""
import json, os, sys, tempfile, threading, time, unittest, urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.environ["DATABASE_URL"] = ""
os.environ["DATABASE_URL_UNPOOLED"] = ""
os.environ["STAFF_PIN"] = ""
os.environ["SMTP_HOST"] = ""
os.environ["WEBHOOK_URL"] = ""
os.environ["WEBHOOK_AUTH"] = ""
os.environ["SILKWOOD_DB"] = str(Path(tempfile.mkdtemp()) / "silkwood.db")
sys.path.insert(0, str(ROOT))
import server

TZ = server.TZ
DAY = "2026-10-29"
BEFORE = datetime(2026, 10, 29, 20, 29, tzinfo=TZ)
AT = datetime(2026, 10, 29, 20, 30, tzinfo=TZ)


def finals(d):
    c = server.db()
    try:
        rows = [dict(r) for r in c.execute(
            "SELECT id, area, staff, email_status, snapshot FROM submissions WHERE date=? AND email_status IS NOT NULL AND email_status!='skipped_update'",
            (d,))]
        lock = c.execute("SELECT staff FROM days WHERE date=?", (d,)).fetchone()
        temps = c.execute("SELECT COUNT(*) AS n FROM food_temps WHERE date=?", (d,)).fetchone()["n"]
        logs = c.execute("SELECT COUNT(*) AS n FROM logs WHERE date=?", (d,)).fetchone()["n"]
    finally:
        c.close()
    return rows, (lock["staff"] if lock else None), temps, logs


class AutoFinalTest(unittest.TestCase):
    def setUp(self):
        server._auto_done.clear()
        if server.DB.exists():
            server.DB.unlink()
        server.init()
        self.payloads = []
        self._webhook, self._email = server.post_webhook, server.send_email
        def capture(sub_id, payload):
            self.payloads.append(payload)
            return "sent"
        server.post_webhook = capture
        server.send_email = lambda *a, **k: ("sent", "")

    def tearDown(self):
        server.post_webhook = self._webhook
        server.send_email = self._email

    def log_prep(self, d, staff):
        c = server.db()
        c.execute("INSERT INTO logs(date,kind,ref,data,staff,at,bad) VALUES(?,?,?,?,?,?,?)",
                  (d, "prep", "", json.dumps({"item": "Chips", "qty": "2"}), staff, d + " 18:04:00", 0))
        c.commit(); c.close()

    def test_cutoff_and_idempotent_final(self):
        self.assertEqual(server.auto_final_dates(BEFORE), ["2026-10-28"])
        self.assertEqual(server.auto_final_dates(AT), ["2026-10-28", DAY])
        self.log_prep(DAY, "Priya")
        self.assertEqual(server.maybe_auto_final(BEFORE), ["sent"])
        rows, staff, temps, logs = finals("2026-10-28")
        self.assertEqual(len(rows), 1)
        self.assertEqual(staff, "")  # nobody had saved a name; do not invent one
        self.assertEqual(temps, 0)
        self.assertIsNone(finals(DAY)[1])  # today is still open at 8:29pm

        self.assertEqual(server.maybe_auto_final(AT), ["already_submitted", "sent"])
        rows, staff, temps, logs = finals(DAY)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["area"], "both")
        self.assertEqual(staff, "Priya")
        self.assertNotEqual(rows[0]["email_status"], "skipped_update")
        snap = json.loads(rows[0]["snapshot"])
        self.assertEqual(snap["temps"], [])
        self.assertEqual(temps, 0)
        self.assertEqual(logs, 1)
        self.assertEqual(self.payloads[-1]["submissionType"], "final")
        self.assertIs(self.payloads[-1]["final"], True)
        self.assertEqual(self.payloads[-1]["submitted_by"], "Priya")
        self.assertEqual(self.payloads[-1]["date"], DAY)
        self.assertTrue(all(p.get("submitted_by") != "Closing" for p in self.payloads))

        server._auto_done.clear()
        c = server.db()
        c.execute("DELETE FROM days WHERE date=?", (DAY,))  # manager reopen
        c.commit(); c.close()
        self.assertEqual(server.maybe_auto_final(AT), ["already_submitted", "already_submitted"])
        self.assertEqual(len(finals(DAY)[0]), 1)

    def test_cold_list_renames_and_activity(self):
        c = server.db()
        names = [r["name"] for r in c.execute("SELECT name FROM items WHERE kind='cold' AND active=1 ORDER BY sort, id")]
        self.assertEqual(names, server.COLD_STARTERS)
        c.execute("DELETE FROM items WHERE kind='cold'")
        for n in ["Salad Sandwiches", "Wraps", "Salad Rolls", "Meals", "Cheesecake", "Tiramisu", "Truffle", "Quiche"]:
            c.execute("INSERT INTO items(kind,name,type,example,sort) VALUES('cold',?,'',0,0)", (n,))
        c.commit()
        server.ensure_cold_items(c); c.commit()
        names = [r["name"] for r in c.execute("SELECT name FROM items WHERE kind='cold' AND active=1 ORDER BY sort, id")]
        c.close()
        self.assertEqual([n for n in names if n in server.COLD_STARTERS], server.COLD_STARTERS)
        self.assertIn("Quiche", names)

        httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.H)
        port = httpd.server_address[1]
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            def post(path, body):
                req = urllib.request.Request(
                    f"http://127.0.0.1:{port}{path}", data=json.dumps(body).encode(),
                    headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(req) as r:
                    return r.status, json.loads(r.read().decode())

            code, day = post("/api/cold", {"date": DAY, "staff": "Priya", "item": "Sandwiches", "qty": "4",
                                            "time_in": "11:05", "temp": 5})
            self.assertEqual(code, 200)
            row = day["cold_items"][0]
            self.assertEqual(row["item"], "Sandwiches")
            self.assertEqual(row["bad"], 0)
            code, day = post("/api/cold", {"date": DAY, "staff": "Priya", "item": "Trifle", "qty": "1",
                                            "time_in": "11:10", "temp": 5.1, "action": "Returned to fridge"})
            warm = next(x for x in day["cold_items"] if x["item"] == "Trifle")
            self.assertEqual(warm["bad"], 1)
            cold_names = [i["name"] for i in day["items"] if i["kind"] == "cold"]
            for n in server.COLD_STARTERS:
                self.assertIn(n, cold_names)
            code, day = post("/api/cold/out", {"date": DAY, "staff": "Priya", "id": row["id"], "outcome": "sold"})
            self.assertEqual(code, 200)
            self.assertEqual(next(x for x in day["cold_items"] if x["id"] == row["id"])["outcome"], "sold")
            code, day = post("/api/cold/out", {"date": DAY, "staff": "Priya", "id": warm["id"], "outcome": "discarded"})
            self.assertTrue(any(x["item"] == "Trifle" for x in day["discards"]))
            # a second final must not appear just because cold food was logged; the day is not yet closed
            self.assertIsNone(day["locked"])
            html = urllib.request.urlopen(f"http://127.0.0.1:{port}/").read().decode()
            self.assertIn("Food Activity Log", html)
            self.assertNotIn("['hotbox','♨','Hot Box']", html)
            self.assertIn("Submit cold holding", html)
            self.assertIn("Send to print", html)
            self.assertIn("Print draft", html)
            self.assertIn("printFoodActivity", html)
            self.assertIn("printColdHolding", html)
            self.assertIn("window.print()", html)
            self.assertIn("DRAFT — this day has not been submitted", html)
            self.assertIn("Submitted record", html)
            self.assertIn("United Silkwood", html)
            self.assertIn("Submit kitchen checklist", html)
            self.assertIn("Submit closing checklist", html)
            self.assertIn("Submit equipment temps", html)
            self.assertIn("Submit fuel checks", html)
            self.assertIn("refreshDay()},15000)", html)
            self.assertIn("visibilitychange", html)
            self.assertNotIn("Daily Tasks", html)
        finally:
            httpd.shutdown()


if __name__ == "__main__":
    unittest.main()
