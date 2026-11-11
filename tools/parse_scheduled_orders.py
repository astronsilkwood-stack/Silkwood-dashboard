#!/usr/bin/env python3
"""Turn a Bopple Scheduled Orders CSV into online-order items keyed by order number.

The export is UTF-8 with a BOM. A product row has Product set. A modifier row has
Product blank and Modifier set, and belongs to the product row above it in the
same order. Run on the box:

    python3 tools/parse_scheduled_orders.py scheduled-orders.csv
    python3 tools/parse_scheduled_orders.py scheduled-orders.csv -o items.json

The JSON is keyed by Order No. Merge each value onto the matching online-orders
order (the same order_no) before POST /api/portal/ingest.
"""
import csv
import json
import sys
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

COLUMNS = (
    "Fulfilment Date",
    "Order No.",
    "Order Type",
    "Fulfilment Time",
    "Date Ordered",
    "Customer Name",
    "Product Category",
    "Product",
    "Modifier Category",
    "Modifier",
    "Item Count",
    "Total",
)


def _text(value):
    return " ".join(str(value or "").split())


def _date(value, label):
    raw = _text(value)
    if len(raw) != 10:
        raise ValueError(f"{label} must be YYYY-MM-DD")
    try:
        date.fromisoformat(raw)
    except ValueError:
        raise ValueError(f"{label} must be YYYY-MM-DD")
    return raw


def _number(value, label):
    raw = _text(value).replace(",", "")
    if not raw:
        raise ValueError(f"{label} is required")
    try:
        number = Decimal(raw)
    except InvalidOperation:
        raise ValueError(f"{label} must be a number")
    if not number.is_finite() or abs(number) > Decimal("1000000"):
        raise ValueError(f"{label} is out of range")
    cents = number.quantize(Decimal("0.01"))
    if cents == cents.to_integral():
        return int(cents)
    return float(cents)


def _modifier(row, label):
    name = _text(row.get("Modifier"))
    if not name:
        raise ValueError(f"{label} modifier is required")
    return {
        "category": _text(row.get("Modifier Category")),
        "name": name,
        "qty": _number(row.get("Item Count"), f"{label} modifier qty"),
        "total": _number(row.get("Total"), f"{label} modifier total"),
    }


def parse_scheduled_orders(path):
    """Read a Scheduled Orders CSV and return {order_no: {customer_name, order_type, fulfilment_time, items, ...}}."""
    source = Path(path)
    with source.open(newline="", encoding="utf-8-sig") as handle:
        return parse_scheduled_orders_reader(csv.DictReader(handle), str(source))


def parse_scheduled_orders_reader(reader, label="scheduled orders"):
    fields = list(reader.fieldnames or [])
    missing = [name for name in COLUMNS if name not in fields]
    if missing:
        raise ValueError(label + " is missing " + ", ".join(missing))
    orders = {}
    order_of = []
    for index, row in enumerate(reader, start=2):
        if not isinstance(row, dict) or not any(_text(value) for value in row.values()):
            continue
        where = f"{label} line {index}"
        number = _text(row.get("Order No."))
        if not number:
            raise ValueError(f"{where} needs an order number")
        product = _text(row.get("Product"))
        modifier = _text(row.get("Modifier"))
        if number not in orders:
            orders[number] = {
                "customer_name": _text(row.get("Customer Name")),
                "order_type": _text(row.get("Order Type")),
                "fulfilment_time": _text(row.get("Fulfilment Time")),
                "fulfilment_date": _date(row.get("Fulfilment Date"), f"{where} fulfilment date"),
                "date_ordered": _date(row.get("Date Ordered"), f"{where} date ordered"),
                "items": [],
            }
            order_of.append(number)
        else:
            current = orders[number]
            for key, column in (
                ("customer_name", "Customer Name"),
                ("order_type", "Order Type"),
                ("fulfilment_time", "Fulfilment Time"),
            ):
                value = _text(row.get(column))
                if value and value != current[key]:
                    raise ValueError(f"{where} {column} does not match the earlier rows for {number}")
            fulfilment = _date(row.get("Fulfilment Date"), f"{where} fulfilment date")
            ordered = _date(row.get("Date Ordered"), f"{where} date ordered")
            if fulfilment != current["fulfilment_date"] or ordered != current["date_ordered"]:
                raise ValueError(f"{where} dates do not match the earlier rows for {number}")
        if product:
            item = {
                "category": _text(row.get("Product Category")),
                "product": product,
                "qty": _number(row.get("Item Count"), f"{where} qty"),
                "total": _number(row.get("Total"), f"{where} total"),
                "modifiers": [],
            }
            if modifier:
                item["modifiers"].append(_modifier(row, where))
            orders[number]["items"].append(item)
        elif modifier:
            if not orders[number]["items"]:
                raise ValueError(f"{where} modifier has no product above it")
            orders[number]["items"][-1]["modifiers"].append(_modifier(row, where))
        else:
            raise ValueError(f"{where} needs a product or a modifier")
    return {number: orders[number] for number in order_of}


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    output = None
    if "-o" in args:
        slot = args.index("-o")
        try:
            output = args[slot + 1]
        except IndexError:
            print("usage: python3 tools/parse_scheduled_orders.py <scheduled-orders.csv> [-o out.json]", file=sys.stderr)
            return 2
        del args[slot:slot + 2]
    if len(args) != 1:
        print("usage: python3 tools/parse_scheduled_orders.py <scheduled-orders.csv> [-o out.json]", file=sys.stderr)
        return 2
    try:
        parsed = parse_scheduled_orders(args[0])
    except (OSError, ValueError, csv.Error) as ex:
        print(str(ex), file=sys.stderr)
        return 1
    text = json.dumps(parsed, indent=2, ensure_ascii=False) + "\n"
    if output:
        Path(output).write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
