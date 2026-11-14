#!/usr/bin/env python3
"""Turn a Xero Profit and Loss report into the portal ``xero-pnl`` ingest body.

Xero holds purchases and expenses for Silkwood. It does not hold till sales.
The portal stores one row per account for the month, then builds the statement
from till and Bopple income plus these amounts.

The report can be the JSON array of rows (as in
``tools/fixtures/xero-pnl-oct-2026-sample.txt``), that file with its text
preamble, or the official Xero report object (``Reports[0].Rows``, PascalCase
or camelCase). Pass the month yourself. A report run on 14 November for
October still uses ``--month 2026-10``.

One amount column only. A comparison report with two months is rejected so the
wrong column is not stored.

    python3 tools/xero_pnl.py tools/fixtures/xero-pnl-oct-2026-sample.txt --month 2026-10
    python3 tools/xero_pnl.py report.json --month 2026-10 -o xero-pnl.json

The command prints the ingest body. It does not read ``PORTAL_INGEST_KEY`` and
it does not post. Post the body with the existing ingest key:

    curl -s -X POST "$PORTAL_URL/api/portal/ingest" \\
      -H "Authorization: Bearer $PORTAL_INGEST_KEY" \\
      -H "Content-Type: application/json" \\
      --data-binary @xero-pnl.json

``data`` shape:

    {
      "month": "2026-10",
      "asOf": "2026-10-31",
      "source": "Xero Profit and Loss",
      "accounts": [
        {
          "section": "Less Cost of Sales",
          "accountName": "Purchases - Fuel",
          "accountId": "6db74470-bcc1-4f6d-bb5a-8fb739bea54c",
          "amount": 79072.96
        }
      ]
    }

``section`` is the Xero section title. ``accountName`` is the account name.
``accountId`` is the Xero account id. ``amount`` is the ex-GST figure Xero
prints, in dollars. Summary rows (totals, gross profit, net profit) are left
out. Do not send a field named ``account``: the ingest treats that name as a
bank field and rejects the upload. Re-posting a month replaces that month.
"""
import argparse
import json
import sys
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP


def _month(value):
    text = str(value or "").strip()
    if len(text) != 7 or text[4] != "-":
        raise ValueError("month must be YYYY-MM")
    try:
        year, mon = int(text[:4]), int(text[5:])
        if not 1990 <= year <= 2100 or not 1 <= mon <= 12:
            raise ValueError
        date(year, mon, 1)
    except ValueError:
        raise ValueError("month must be YYYY-MM")
    return f"{year:04d}-{mon:02d}"


def _last_day(month_start):
    if month_start.month == 12:
        nxt = date(month_start.year + 1, 1, 1)
    else:
        nxt = date(month_start.year, month_start.month + 1, 1)
    return date.fromordinal(nxt.toordinal() - 1)


def _as_of(month, raw):
    if not raw:
        year, mon = int(month[:4]), int(month[5:])
        return _last_day(date(year, mon, 1)).isoformat()
    text = str(raw).strip()
    if len(text) != 10:
        raise ValueError("asOf must be YYYY-MM-DD")
    try:
        date.fromisoformat(text)
    except ValueError:
        raise ValueError("asOf must be YYYY-MM-DD")
    return text


def _json_text(text):
    stripped = text.lstrip("\ufeff")
    lines = stripped.splitlines()
    for i, line in enumerate(lines):
        if line.lstrip()[:1] in "[{":
            return "\n".join(lines[i:])
    raise ValueError("Xero P&L report has no JSON rows")


def load_report(source):
    """Accept a parsed report, a JSON string, or the sample file's text."""
    if isinstance(source, (list, dict)):
        return source
    if not isinstance(source, str):
        raise ValueError("Xero P&L report must be JSON")
    try:
        return json.loads(_json_text(source))
    except json.JSONDecodeError as ex:
        raise ValueError("Xero P&L report is not valid JSON") from ex


def _pick(obj, *names):
    if not isinstance(obj, dict):
        return None
    for name in names:
        if name in obj:
            return obj[name]
    return None


def _row_type(row):
    return str(_pick(row, "RowType", "rowType") or "").replace(" ", "").casefold()


def _cell_value(cell):
    if not isinstance(cell, dict):
        return ""
    value = _pick(cell, "Value", "value")
    return "" if value is None else str(value)


def _account_id(cell):
    attrs = _pick(cell, "Attributes", "attributes") or []
    if not isinstance(attrs, list):
        return ""
    for attr in attrs:
        if not isinstance(attr, dict):
            continue
        if str(_pick(attr, "Id", "id") or "").casefold() != "account":
            continue
        return str(_pick(attr, "Value", "value") or "").strip()
    return ""


def _amount(value):
    if isinstance(value, bool) or value is None:
        raise ValueError("amount is missing")
    if isinstance(value, (int, float)):
        if isinstance(value, float) and (value != value or abs(value) == float("inf")):
            raise ValueError("amount is out of range")
        amount = Decimal(str(value))
    else:
        text = str(value).strip().replace(",", "").replace("$", "").replace(" ", "")
        if not text or text in {"-", "–", "—"}:
            raise ValueError("amount is missing")
        if text.startswith("(") and text.endswith(")"):
            text = "-" + text[1:-1]
        try:
            amount = Decimal(text)
        except InvalidOperation:
            raise ValueError("amount must be a number")
    if not amount.is_finite() or abs(amount) > Decimal("1000000000000"):
        raise ValueError("amount is out of range")
    return amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _json_amount(amount):
    return json.loads(format(amount, "f"))


def _report_rows(report):
    if isinstance(report, list):
        return report
    if not isinstance(report, dict):
        raise ValueError("Xero P&L report must be a JSON object or array")
    reports = _pick(report, "Reports", "reports")
    if isinstance(reports, list):
        if not reports or not isinstance(reports[0], dict):
            raise ValueError("Xero P&L report has no rows")
        report = reports[0]
    rows = _pick(report, "Rows", "rows")
    if not isinstance(rows, list):
        raise ValueError("Xero P&L report has no rows")
    return rows


def _amount_column(rows):
    """Single-period reports use the second cell. Two amount columns are rejected."""
    for row in rows:
        if not isinstance(row, dict) or _row_type(row) != "header":
            continue
        cells = _pick(row, "Cells", "cells") or []
        labels = []
        for cell in cells[1:]:
            label = _cell_value(cell).strip()
            if label:
                labels.append(label)
        if len(labels) > 1:
            raise ValueError("Xero P&L report has more than one amount column. Export one month.")
        return 1
    return 1


def _walk(rows, section, column, out):
    for row in rows:
        if not isinstance(row, dict):
            continue
        kind = _row_type(row)
        if kind == "section":
            title = str(_pick(row, "Title", "title") or "").strip()
            nested = _pick(row, "Rows", "rows") or []
            if not isinstance(nested, list):
                continue
            _walk(nested, title or section, column, out)
            continue
        if kind != "row" or not section.strip():
            continue
        cells = _pick(row, "Cells", "cells") or []
        if not isinstance(cells, list) or len(cells) <= column:
            continue
        name = " ".join(_cell_value(cells[0]).split())
        account_id = _account_id(cells[0]) or _account_id(cells[column])
        if not name or not account_id:
            continue
        if name.casefold().startswith("total "):
            continue
        try:
            amount = _amount(_cell_value(cells[column]))
        except ValueError:
            continue
        out.append({
            "section": " ".join(section.split()),
            "accountName": name,
            "accountId": account_id,
            "amount": _json_amount(amount),
        })


def accounts_from_report(report):
    """Account rows only. Totals, gross profit, and net profit are omitted."""
    rows = _report_rows(report)
    found = []
    _walk(rows, "", _amount_column(rows), found)
    if not found:
        raise ValueError("Xero P&L report has no account rows")
    seen = set()
    unique = []
    for row in found:
        if row["accountId"] in seen:
            raise ValueError("Xero P&L report repeats an account id")
        seen.add(row["accountId"])
        unique.append(row)
    return unique


def ingest_data(report, month, as_of=None, source="Xero Profit and Loss"):
    """The ``data`` object for dataset ``xero-pnl``."""
    month = _month(month)
    accounts = accounts_from_report(load_report(report))
    return {
        "month": month,
        "asOf": _as_of(month, as_of),
        "source": " ".join(str(source or "Xero Profit and Loss").split())[:160] or "Xero Profit and Loss",
        "accounts": accounts,
    }


def ingest_body(report, month, as_of=None, source="Xero Profit and Loss"):
    return {"dataset": "xero-pnl", "data": ingest_data(report, month, as_of, source)}


def main(argv):
    parser = argparse.ArgumentParser(description="Convert a Xero Profit and Loss report into the portal xero-pnl ingest body.")
    parser.add_argument("report", help="Path to the Xero report JSON or sample text")
    parser.add_argument("--month", required=True, help="YYYY-MM the report covers")
    parser.add_argument("--as-of", dest="as_of", help="YYYY-MM-DD. Defaults to the last day of the month.")
    parser.add_argument("-o", "--output", help="Write the JSON to this path instead of stdout")
    args = parser.parse_args(argv)
    try:
        text = open(args.report, encoding="utf-8").read()
        body = ingest_body(text, args.month, args.as_of)
    except OSError:
        print("Could not read the report file.", file=sys.stderr)
        return 2
    except ValueError as ex:
        print(str(ex), file=sys.stderr)
        return 2
    raw = json.dumps(body, indent=2) + "\n"
    if args.output:
        try:
            with open(args.output, "w", encoding="utf-8") as handle:
                handle.write(raw)
        except OSError:
            print("Could not write the output file.", file=sys.stderr)
            return 2
    else:
        sys.stdout.write(raw)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
