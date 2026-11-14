"""Convert a Xero Profit and Loss report into the portal xero-pnl payload."""
import json
import subprocess
import sys
import unittest
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import xero_pnl

SAMPLE = ROOT / "tools" / "fixtures" / "xero-pnl-oct-2026-sample.txt"


class XeroPnlTest(unittest.TestCase):
    def test_sample_file_drops_totals_and_keeps_accounts(self):
        body = xero_pnl.ingest_body(SAMPLE.read_text(), "2026-10")
        self.assertEqual(body["dataset"], "xero-pnl")
        data = body["data"]
        self.assertEqual(data["month"], "2026-10")
        self.assertEqual(data["asOf"], "2026-10-31")
        names = [row["accountName"] for row in data["accounts"]]
        self.assertEqual(names[:3], ["Purchases - Fuel", "Purchases - Others Shop", "Purchases - Shop"])
        self.assertIn("Wages and Salaries", names)
        self.assertIn("Superannuation", names)
        for skipped in ("Gross Profit", "Net Profit", "Total Cost of Sales", "Total Operating Expenses"):
            self.assertNotIn(skipped, names)
        by_name = {row["accountName"]: Decimal(str(row["amount"])) for row in data["accounts"]}
        self.assertEqual(by_name["Purchases - Fuel"], Decimal("79072.96"))
        self.assertEqual(by_name["Purchases - Others Shop"], Decimal("15.15"))
        self.assertEqual(by_name["Purchases - Shop"], Decimal("54258.74"))
        self.assertEqual(by_name["Wages and Salaries"], Decimal("21199.73"))
        self.assertEqual(by_name["Superannuation"], Decimal("2543.98"))
        cost = sum(Decimal(str(row["amount"])) for row in data["accounts"] if row["section"] == "Less Cost of Sales")
        opex = sum(Decimal(str(row["amount"])) for row in data["accounts"] if row["section"] == "Less Operating Expenses")
        self.assertEqual(cost, Decimal("133346.85"))
        self.assertEqual(opex, Decimal("28251.63"))
        self.assertEqual(len(data["accounts"]), 13)
        self.assertTrue(all(row["accountId"] for row in data["accounts"]))

    def test_pascal_case_report_and_credit_amounts(self):
        report = {
            "Reports": [{
                "Rows": [
                    {"RowType": "Header", "Cells": [{"Value": ""}, {"Value": "31 Oct 26"}]},
                    {"RowType": "Section", "Title": "Less Operating Expenses", "Rows": [
                        {"RowType": "Row", "Cells": [
                            {"Value": "Cleaning", "Attributes": [{"Id": "account", "Value": "clean-1"}]},
                            {"Value": "(12.50)", "Attributes": [{"Id": "account", "Value": "clean-1"}]},
                        ]},
                        {"RowType": "SummaryRow", "Cells": [{"Value": "Total Operating Expenses"}, {"Value": "-12.50"}]},
                    ]},
                    {"RowType": "Section", "Title": "", "Rows": [
                        {"RowType": "Row", "Cells": [{"Value": "Net Profit"}, {"Value": "12.50"}]},
                    ]},
                ],
            }],
        }
        accounts = xero_pnl.accounts_from_report(report)
        self.assertEqual(accounts, [{
            "section": "Less Operating Expenses",
            "accountName": "Cleaning",
            "accountId": "clean-1",
            "amount": -12.5,
        }])

    def test_two_amount_columns_are_rejected(self):
        report = [{
            "rowType": "Header",
            "cells": [{"value": ""}, {"value": "31 Oct 26"}, {"value": "30 Sep 26"}],
        }, {
            "rowType": "Section",
            "title": "Less Cost of Sales",
            "rows": [],
        }]
        with self.assertRaises(ValueError):
            xero_pnl.accounts_from_report(report)

    def test_cli_writes_the_ingest_body(self):
        proc = subprocess.run(
            [sys.executable, str(ROOT / "tools" / "xero_pnl.py"), str(SAMPLE), "--month", "2026-10"],
            check=False, capture_output=True, text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        body = json.loads(proc.stdout)
        self.assertEqual(body["dataset"], "xero-pnl")
        self.assertEqual(len(body["data"]["accounts"]), 13)
        self.assertNotIn("PORTAL_INGEST_KEY", proc.stdout)
        bad = subprocess.run(
            [sys.executable, str(ROOT / "tools" / "xero_pnl.py"), str(SAMPLE), "--month", "2026-13"],
            check=False, capture_output=True, text=True,
        )
        self.assertEqual(bad.returncode, 2)
        self.assertIn("YYYY-MM", bad.stderr)
        self.assertEqual(bad.stdout, "")


if __name__ == "__main__":
    unittest.main()
