"""Pool slots are returned when a request fails, and a history read never holds two.

Uses a fake pool (no Postgres). A later case starts the real server on SQLite.
"""
import json, os, sys, tempfile, threading, unittest, urllib.error, urllib.request
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


class Boom(Exception):
    pass


class Rows:
    def __init__(self, rows):
        self.rows = rows
    def __iter__(self):
        return iter(self.rows)
    def fetchone(self):
        return self.rows[0] if self.rows else None
    def fetchall(self):
        return list(self.rows)


class Conn:
    def __init__(self, pool):
        self.pool = pool
        self.n = 0
        self.rollback_calls = 0
        self.info_status = 2  # in a transaction, as psycopg is after the first statement
    @property
    def info(self):
        return self
    @property
    def transaction_status(self):
        return self.info_status
    def execute(self, sql, params=()):
        self.n += 1
        if self.pool.mode == "fail":
            raise Boom("query failed")
        if self.pool.mode == "history-fail" and self.n >= 2:
            raise Boom("query failed")
        if "SELECT date FROM logs UNION" in sql:
            return Rows([{"date": "2026-11-18"}])
        return Rows([])
    def rollback(self):
        self.rollback_calls += 1
        raise RuntimeError("neon dropped the connection")
    def commit(self):
        pass
    def close(self):
        self.closed = True


class Pool:
    def __init__(self):
        self.mode = "ok"
        self.out = 0
        self.max_out = 0
        self.gets = 0
        self.puts = 0
    def getconn(self):
        self.gets += 1
        self.out += 1
        self.max_out = max(self.max_out, self.out)
        return Conn(self)
    def putconn(self, conn):
        self.puts += 1
        self.out -= 1


class PoolReturnTest(unittest.TestCase):
    def setUp(self):
        self._pg = server.PG
        self._pool = server.PgConn._pool
        self.pool = Pool()
        server.PG = True
        server.PgConn._pool = self.pool

    def tearDown(self):
        server.PG = self._pg
        server.PgConn._pool = self._pool

    def test_failing_day_reads_return_every_connection(self):
        self.pool.mode = "fail"
        for _ in range(20):
            with self.assertRaises(Boom):
                server.day_state("2026-11-18")
        self.assertEqual(self.pool.out, 0)
        self.assertEqual(self.pool.gets, 20)
        self.assertEqual(self.pool.puts, 20)
        self.assertEqual(self.pool.max_out, 1)

    def test_history_failure_uses_one_connection_and_returns_it(self):
        self.pool.mode = "history-fail"
        for _ in range(20):
            with self.assertRaises(Boom):
                server.history_payload()
        self.assertEqual(self.pool.out, 0)
        self.assertEqual(self.pool.gets, 20)
        self.assertEqual(self.pool.puts, 20)
        self.assertEqual(self.pool.max_out, 1)

    def test_history_success_does_not_borrow_a_second_connection(self):
        self.pool.mode = "ok"
        out = server.history_payload()
        self.assertEqual([row["date"] for row in out], ["2026-11-18"])
        self.assertEqual(self.pool.max_out, 1)
        self.assertEqual(self.pool.out, 0)
        self.assertEqual(self.pool.gets, self.pool.puts)

    def test_close_returns_a_dropped_connection_without_rolling_back_first(self):
        self.pool.mode = "ok"
        c = server.PgConn()
        c.close()
        c.close()
        self.assertEqual(c.c.rollback_calls, 0)
        self.assertEqual(self.pool.puts, 1)
        self.assertEqual(self.pool.out, 0)

    def test_pool_wait_is_a_few_seconds_and_idle_connections_are_recycled(self):
        import psycopg_pool
        captured = {}
        class Capture:
            @staticmethod
            def check_connection(conn):
                pass
            def __init__(self, *args, **kwargs):
                captured["args"] = args
                captured["kwargs"] = kwargs
        orig = psycopg_pool.ConnectionPool
        psycopg_pool.ConnectionPool = Capture
        try:
            server._open_pg_pool()
        finally:
            psycopg_pool.ConnectionPool = orig
        kw = captured["kwargs"]
        self.assertEqual(kw["timeout"], 5)
        self.assertEqual(kw["max_size"], 10)
        self.assertLessEqual(kw["max_idle"], 180)
        self.assertLessEqual(kw["max_lifetime"], 600)
        self.assertIs(kw["check"], Capture.check_connection)
        self.assertIsNone(kw["kwargs"]["prepare_threshold"])
        self.assertLessEqual(kw["kwargs"]["connect_timeout"], 10)


class SqliteEndpointsTest(unittest.TestCase):
    def test_day_history_and_staff_on_sqlite(self):
        self.assertFalse(server.PG)
        if server.DB.exists():
            server.DB.unlink()
        server.init()
        c = server.db()
        try:
            c.execute("INSERT INTO logs(date,kind,ref,data,staff,at,bad) VALUES(?,?,?,?,?,?,?)",
                      ("2026-11-18", "prep", "", json.dumps({"item": "Chips", "qty": "2"}), "Priya", "2026-11-18 14:28:00", 0))
            c.commit()
        finally:
            c.close()
        day = server.day_state("2026-11-18")
        self.assertEqual(day["date"], "2026-11-18")
        self.assertEqual(day["logs"][0]["staff"], "Priya")
        self.assertIsNone(day["locked"])
        hist = server.history_payload()
        self.assertEqual(hist[0]["date"], "2026-11-18")
        self.assertEqual(hist[0]["logs"], 1)
        self.assertEqual(hist[0]["staff"], ["Priya"])
        self.assertEqual(hist[0]["submissions"], [])

        httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.H)
        port = httpd.server_address[1]
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            def get(path):
                try:
                    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}") as r:
                        return r.status, r.read()
                except urllib.error.HTTPError as e:
                    return e.code, e.read()
            status, body = get("/")
            self.assertEqual(status, 200)
            self.assertIn(b"Silkwood", body)
            status, body = get("/healthz")
            self.assertEqual(json.loads(body)["ok"], True)
            status, body = get("/api/pincheck?pin=nope")
            self.assertEqual(status, 403)
            status, body = get("/api/day?date=2026-11-18")
            self.assertEqual(json.loads(body)["logs"][0]["data"]["item"], "Chips")
            status, body = get("/api/history")
            self.assertEqual(json.loads(body)[0]["date"], "2026-11-18")
            status, body = get("/api/staff")
            names = [s["name"] for s in json.loads(body)["staff"]]
            self.assertIn("Priya", names)
            self.assertIn("Manasa", names)
        finally:
            httpd.shutdown()
            httpd.server_close()


if __name__ == "__main__":
    unittest.main()
