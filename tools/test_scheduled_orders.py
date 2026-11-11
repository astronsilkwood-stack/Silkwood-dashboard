"""Bopple Scheduled Orders CSV -> online order items. Uses the November 2026 sample."""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import parse_scheduled_orders

SAMPLE = ROOT / "tools" / "fixtures" / "scheduled-orders.csv"


class ScheduledOrdersTest(unittest.TestCase):
    def test_sample_orders_items_and_modifiers(self):
        raw = SAMPLE.read_bytes()
        self.assertTrue(raw.startswith(b"\xef\xbb\xbf"))
        parsed = parse_scheduled_orders.parse_scheduled_orders(SAMPLE)
        self.assertEqual(len(parsed), 19)
        lines = sum(1 + len(item["modifiers"]) for order in parsed.values() for item in order["items"])
        self.assertEqual(lines, 115)

        jess = parsed["26489417"]
        self.assertEqual(jess["customer_name"], "Jess Tabone")
        self.assertEqual(jess["order_type"], "Pickup")
        self.assertEqual(jess["fulfilment_time"], "02:21pm")
        self.assertEqual(jess["fulfilment_date"], "2026-11-01")
        self.assertEqual(jess["date_ordered"], "2026-11-01")
        self.assertEqual(jess["items"][0]["product"], "Medium Chips")
        self.assertEqual(jess["items"][0]["qty"], 1)
        self.assertEqual(jess["items"][0]["total"], 8.5)
        self.assertEqual(jess["items"][0]["modifiers"], [
            {"category": "Salt", "name": "Chicken Salt", "qty": 1, "total": 0},
        ])
        self.assertEqual(jess["items"][1]["product"], "15 Crunchy Chicken Nuggets")
        self.assertEqual(jess["items"][1]["modifiers"], [])

        ben = parsed["26491589"]["items"][0]
        self.assertEqual(ben["product"], "Aussie Chicken Burger")
        self.assertEqual([mod["name"] for mod in ben["modifiers"]], ["Cheese Sauce Dipping Sauce", "BBQ Sauce"])

        fish = next(item for item in parsed["26493576"]["items"] if item["product"] == "Fish")
        self.assertEqual(fish["qty"], 2)
        self.assertEqual(fish["total"], 21.98)
        self.assertEqual(fish["modifiers"][0]["name"], "Battered")
        self.assertEqual(fish["modifiers"][0]["qty"], 2)

        self.assertEqual(parsed["26503992"]["customer_name"], "Joann Stephensen")
        self.assertNotIn("  ", parsed["26503992"]["customer_name"])
        basket = next(item for item in parsed["26607475"]["items"] if item["product"] == "Seafood Basket")
        self.assertEqual(basket["modifiers"][0], {"category": "Fish", "name": "Crumbed", "qty": 3, "total": 0})
        self.assertIn("26672951", parsed)
        deal = next(item for item in parsed["26602482"]["items"] if item["product"] == "Fish Burger")
        self.assertEqual(deal["modifiers"][0]["name"], "Small Chips & 375ml Can")

    def test_modifier_without_a_product_is_rejected(self):
        text = (
            "Fulfilment Date,Order No.,Order Type,Fulfilment Time,Date Ordered,Customer Name,"
            "Product Category,Product,Modifier Category,Modifier,Item Count,Total\n"
            "2026-11-01,1,Pickup,02:21pm,2026-11-01,Ada,Chips,,Salt,Chicken Salt,1,0.00\n"
        )
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "bad.csv"
            path.write_text("\ufeff" + text, encoding="utf-8")
            with self.assertRaises(ValueError) as caught:
                parse_scheduled_orders.parse_scheduled_orders(path)
        self.assertIn("no product", str(caught.exception))

    def test_cli_writes_json(self):
        with tempfile.TemporaryDirectory() as folder:
            dest = Path(folder) / "out.json"
            done = subprocess.run(
                [sys.executable, str(ROOT / "tools" / "parse_scheduled_orders.py"), str(SAMPLE), "-o", str(dest)],
                check=False, capture_output=True, text=True,
            )
            self.assertEqual(done.returncode, 0, done.stderr)
            parsed = json.loads(dest.read_text(encoding="utf-8"))
        self.assertEqual(len(parsed), 19)
        self.assertEqual(parsed["26489417"]["customer_name"], "Jess Tabone")


if __name__ == "__main__":
    unittest.main()
