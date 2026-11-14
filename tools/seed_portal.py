#!/usr/bin/env python3
"""Load tools/fixtures into the local database and add a sample week of staff hours.

The shop computer should use POST /api/portal/ingest. This script is only for a local look.
PORTAL_ADMIN_USER and PORTAL_ADMIN_PASSWORD_HASH must already be set.
"""
import json, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import server
import portal

FIXTURES = (
    ("sales.json", "sales"),
    ("purchases.json", "purchases"),
    ("fuel-commission.json", "fuel-commission"),
    ("fuel-dips.json", "fuel-dips"),
    ("insights.json", "insights"),
    ("invoices.json", "invoices"),
    ("online-orders.json", "online-orders"),
    ("shift-exceptions.json", "shift-exceptions"),
)
SAMPLE = (
    ("Alex", (
        ("2026-11-09", "06:00", "14:30", 30, "Open"),
        ("2026-11-10", "22:00", "06:00", 0, "Night fill"),
    )),
    ("Sam", (
        ("2026-11-11", "09:00", "17:00", 30, ""),
        ("2026-11-14", "08:00", "13:00", 0, "Saturday"),
    )),
)


def main():
    server.init()
    c = server.db()
    try:
        for filename, dataset in FIXTURES:
            data = json.loads((ROOT / "tools" / "fixtures" / filename).read_text())
            result = portal.APPLIERS[dataset](c, data)
            print(f"loaded {dataset} through {result.get('asOf')}")
        sys.path.insert(0, str(ROOT / "tools"))
        import xero_pnl
        sample = (ROOT / "tools" / "fixtures" / "xero-pnl-oct-2026-sample.txt").read_text()
        result = portal.APPLIERS["xero-pnl"](c, xero_pnl.ingest_data(sample, "2026-10"))
        print(f"loaded xero-pnl {result.get('month')} ({result.get('accounts')} accounts)")
        if not portal.one(c.execute("SELECT 1 AS x FROM portal_people")):
            now = portal.stamp()
            for name, shifts in SAMPLE:
                person_id = portal.one(c.execute(
                    "INSERT INTO portal_people(name,active,created_at,updated_at) VALUES(?,1,?,?) RETURNING id",
                    (name, now, now),
                ))["id"]
                portal.audit(c, "astron", "add_staff", person_id, None, None, {"name": name})
                for work_date, start, finish, break_minutes, notes in shifts:
                    hours = portal.shift_hours(work_date, start, finish, break_minutes)
                    shift_id = portal.one(c.execute(
                        """INSERT INTO portal_roster(person_id,work_date,start_time,finish_time,break_minutes,notes,hours,created_at,updated_at,created_by,updated_by)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?) RETURNING id""",
                        (person_id, work_date, start, finish, break_minutes, notes, hours, now, now, "astron", "astron"),
                    ))["id"]
                    portal.audit(c, "astron", "add_shift", person_id, shift_id, None, {
                        "staff": name, "date": work_date, "start": start, "finish": finish,
                        "break": break_minutes, "notes": notes, "hours": hours,
                    })
            print("sample staff hours added for the week of 9 Nov 2026")
        c.commit()
    finally:
        c.close()


if __name__ == "__main__":
    main()
