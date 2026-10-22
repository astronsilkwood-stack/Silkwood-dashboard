"""One-off DB move helper (no secrets in code). Reads SRC from DATABASE_URL_UNPOOLED and DST from MIGRATE_DST_URL.
Usage: python tools/migrate_db.py copy|sync|verify   (prints only table names and counts/hashes)"""
import os, sys, hashlib
import psycopg
TABLES = ["items", "tasks", "task_done", "logs", "days", "shifts", "submissions", "audit"]
PK = {"task_done": "task_id,date", "days": "date"}
def ssl(u): return u if "sslmode=" in u else u + ("&" if "?" in u else "?") + "sslmode=require"
SRC = ssl(os.environ.get("DATABASE_URL_UNPOOLED") or os.environ["DATABASE_URL"]); DST = ssl(os.environ["MIGRATE_DST_URL"])
def schema():
    os.environ["DATABASE_URL"] = DST; os.environ["DATABASE_URL_UNPOOLED"] = DST
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__)))); import server; server.init()
def rows(c, t):
    return c.execute(f"SELECT * FROM {t} ORDER BY {PK.get(t, 'id')}").fetchall()
def cols(c, t): return [r[0] for r in c.execute("SELECT column_name FROM information_schema.columns WHERE table_name=%s ORDER BY ordinal_position", (t,))]
def report(s, d):
    ok = True
    for t in TABLES:
        a, b = rows(s, t), rows(d, t)
        ha, hb = hashlib.md5(repr(a).encode()).hexdigest()[:10], hashlib.md5(repr(b).encode()).hexdigest()[:10]
        ok &= (ha == hb); print(f"{t:12} src={len(a):5} dst={len(b):5} {'MATCH' if ha == hb else 'DIFF'}", flush=True)
    print("ALL MATCH" if ok else "MISMATCH", flush=True)
mode = sys.argv[1]
if mode == "copy": schema()
with psycopg.connect(SRC) as s, psycopg.connect(DST) as d:
    if mode in ("copy", "sync"):
        for t in TABLES:
            cs = cols(s, t); data = rows(s, t)
            if mode == "copy": d.execute(f"TRUNCATE {t}")
            q = f"INSERT INTO {t}({','.join(cs)}) VALUES({','.join(['%s']*len(cs))}) ON CONFLICT ({PK.get(t,'id')}) DO UPDATE SET " + ",".join(f"{x}=EXCLUDED.{x}" for x in cs)
            with d.cursor() as cur: cur.executemany(q, [tuple(r) for r in data])
            if t not in PK: d.execute(f"SELECT setval(pg_get_serial_sequence('{t}','id'), GREATEST((SELECT COALESCE(MAX(id),0) FROM {t}),1))")
        d.commit()
    report(s, d)
