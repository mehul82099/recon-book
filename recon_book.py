#!/usr/bin/env python3
"""recon-book: offline bank statement reconciliation ledger for small businesses.

Match bank statement lines against your own business records (invoices you
expect money in for, expenses you expect money out for) and see what is left
unmatched on both sides.

Zero-dependency. Python 3.9+. Single JSON store written with atomic replace.

Data model
----------
statement line: id, date (YYYY-MM-DD), description, amount (signed rupees:
positive = money in, negative = money out), reference (optional)
record:         id, date, party, amount (positive rupees), direction
                ("in" for receivables like invoices, "out" for payables like
                expenses), reference (optional)
match:          id, statement id, record id, how ("auto" or "manual")

Matching is greedy one-to-one: each statement line matches at most one record
and vice versa. A pair is eligible when direction agrees, the absolute amounts
differ by at most the tolerance, and the dates are within the window.
"""

import argparse
import csv
import datetime
import json
import os
import sys
import tempfile
from decimal import Decimal, InvalidOperation

DB_VERSION = 1

CATEGORY_STATEMENT = "statement"
CATEGORY_RECORD = "record"


class ReconError(Exception):
    """User-facing error: printed with exit code 2."""


def today_iso():
    return datetime.date.today().isoformat()


def parse_date(value, field):
    try:
        return datetime.date.fromisoformat(value.strip()).isoformat()
    except (ValueError, AttributeError):
        raise ReconError("invalid %s date %r (want YYYY-MM-DD)" % (field, value))


def parse_money(value, field):
    try:
        amount = Decimal(value.strip())
    except (InvalidOperation, AttributeError):
        raise ReconError("invalid %s amount %r" % (field, value))
    return amount.quantize(Decimal("0.01"))


def empty_db():
    return {
        "version": DB_VERSION,
        "statements": [],
        "records": [],
        "matches": [],
        "counters": {"statement": 0, "record": 0, "match": 0},
    }


def load_db(path):
    if not os.path.exists(path):
        return empty_db()
    with open(path, "r", encoding="utf-8") as handle:
        try:
            data = json.load(handle)
        except json.JSONDecodeError as exc:
            raise ReconError("store %s is not valid JSON: %s" % (path, exc))
    if data.get("version") != DB_VERSION:
        raise ReconError("store %s has unsupported version %r" % (path, data.get("version")))
    return data


def save_db(path, data):
    directory = os.path.dirname(os.path.abspath(path))
    fd, tmp_path = tempfile.mkstemp(prefix=".recon-book-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def next_id(data, kind):
    data["counters"][kind] += 1
    prefix = {"statement": "S", "record": "R", "match": "M"}[kind]
    return "%s%d" % (prefix, data["counters"][kind])


def import_statement_csv(data, csv_path):
    added = 0
    with open(csv_path, "r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        require_columns(reader, csv_path, ["date", "description", "amount"])
        for line_no, row in enumerate(reader, start=2):
            try:
                amount = parse_money(row["amount"], "statement amount (row %d)" % line_no)
                date = parse_date(row["date"], "statement (row %d)" % line_no)
            except ReconError:
                raise
            description = (row.get("description") or "").strip()
            if not description:
                raise ReconError("statement row %d has an empty description" % line_no)
            data["statements"].append({
                "id": next_id(data, "statement"),
                "date": date,
                "description": description,
                "amount": str(amount),
                "reference": (row.get("reference") or "").strip(),
            })
            added += 1
    return added


def import_records_csv(data, csv_path):
    added = 0
    with open(csv_path, "r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        require_columns(reader, csv_path, ["date", "party", "amount", "direction"])
        for line_no, row in enumerate(reader, start=2):
            amount = parse_money(row["amount"], "record amount (row %d)" % line_no)
            date = parse_date(row["date"], "record (row %d)" % line_no)
            direction = (row.get("direction") or "").strip().lower()
            if direction not in ("in", "out"):
                raise ReconError(
                    "record row %d direction %r (want in or out)" % (line_no, direction))
            if amount <= 0:
                raise ReconError("record row %d amount must be positive" % line_no)
            party = (row.get("party") or "").strip()
            if not party:
                raise ReconError("record row %d has an empty party" % line_no)
            data["records"].append({
                "id": next_id(data, "record"),
                "date": date,
                "party": party,
                "amount": str(amount),
                "direction": direction,
                "reference": (row.get("reference") or "").strip(),
            })
            added += 1
    return added


def require_columns(reader, csv_path, wanted):
    header = [h.strip().lower() for h in (reader.fieldnames or [])]
    missing = [col for col in wanted if col not in header]
    if missing:
        raise ReconError(
            "%s is missing column(s): %s (header found: %s)"
            % (csv_path, ", ".join(missing), ", ".join(header) or "none"))


def statement_direction(statement):
    return "in" if Decimal(statement["amount"]) > 0 else "out"


def candidate_pairs(data, tolerance, window_days):
    matched_statements = {m["statement_id"] for m in data["matches"]}
    matched_records = {m["record_id"] for m in data["matches"]}
    pairs = []
    for statement in data["statements"]:
        if statement["id"] in matched_statements:
            continue
        s_amount = abs(Decimal(statement["amount"]))
        s_date = datetime.date.fromisoformat(statement["date"])
        for record in data["records"]:
            if record["id"] in matched_records:
                continue
            if record["direction"] != statement_direction(statement):
                continue
            r_amount = Decimal(record["amount"])
            amount_gap = abs(s_amount - r_amount)
            if amount_gap > tolerance:
                continue
            r_date = datetime.date.fromisoformat(record["date"])
            date_gap = abs((s_date - r_date).days)
            if date_gap > window_days:
                continue
            ref_boost = 0
            if record["reference"] and record["reference"].lower() in statement["description"].lower():
                ref_boost = 1
            # Higher is better: reference boost, then smaller amount gap,
            # then smaller date gap, then lower record id for determinism.
            score = (ref_boost, -amount_gap, -date_gap, -int(record["id"][1:]))
            pairs.append((score, statement, record))
    pairs.sort(key=lambda item: item[0], reverse=True)
    return pairs


def run_match(data, tolerance, window_days):
    pairs = candidate_pairs(data, tolerance, window_days)
    used_statements = set()
    used_records = set()
    created = []
    for score, statement, record in pairs:
        if statement["id"] in used_statements or record["id"] in used_records:
            continue
        match_id = next_id(data, "match")
        data["matches"].append({
            "id": match_id,
            "statement_id": statement["id"],
            "record_id": record["id"],
            "how": "auto",
            "matched_on": today_iso(),
        })
        used_statements.add(statement["id"])
        used_records.add(record["id"])
        created.append(match_id)
    return created


def build_report(data):
    matched_statements = {m["statement_id"] for m in data["matches"]}
    matched_records = {m["record_id"] for m in data["matches"]}
    lines = []
    lines.append("recon-book report")
    lines.append("matches: %d" % len(data["matches"]))
    for match in data["matches"]:
        statement = find(data["statements"], match["statement_id"])
        record = find(data["records"], match["record_id"])
        lines.append("  %s: %s %s (%s %s) <-> %s %s (%s, %s)"
                     % (match["id"], statement["id"], statement["date"],
                        statement["amount"], statement["description"],
                        record["id"], record["date"], record["party"],
                        record["amount"]))
    unmatched_statements = [s for s in data["statements"] if s["id"] not in matched_statements]
    unmatched_records = [r for r in data["records"] if r["id"] not in matched_records]
    lines.append("unmatched statement lines: %d" % len(unmatched_statements))
    for statement in unmatched_statements:
        lines.append("  %s %s %s %s" % (statement["id"], statement["date"],
                                        statement["amount"], statement["description"]))
    lines.append("unmatched records: %d" % len(unmatched_records))
    for record in unmatched_records:
        lines.append("  %s %s %s %s (%s)" % (record["id"], record["date"],
                                             record["direction"], record["party"],
                                             record["amount"]))
    return lines


def find(items, item_id):
    for item in items:
        if item["id"] == item_id:
            return item
    raise ReconError("unknown id %s" % item_id)


def export_csv(data, out_path):
    matched_statements = {m["statement_id"]: m for m in data["matches"]}
    matched_records = {m["record_id"]: m for m in data["matches"]}
    with open(out_path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["side", "id", "date", "amount", "label", "status", "match_id"])
        for statement in data["statements"]:
            match = matched_statements.get(statement["id"])
            writer.writerow([
                "statement", statement["id"], statement["date"], statement["amount"],
                statement["description"],
                "matched" if match else "unmatched",
                match["id"] if match else "",
            ])
        for record in data["records"]:
            match = matched_records.get(record["id"])
            writer.writerow([
                "record", record["id"], record["date"], record["amount"],
                "%s %s" % (record["direction"], record["party"]),
                "matched" if match else "unmatched",
                match["id"] if match else "",
            ])


def cmd_import_statement(args):
    data = load_db(args.db)
    added = import_statement_csv(data, args.csv)
    save_db(args.db, data)
    print("imported %d statement line(s) into %s" % (added, args.db))


def cmd_import_records(args):
    data = load_db(args.db)
    added = import_records_csv(data, args.csv)
    save_db(args.db, data)
    print("imported %d record(s) into %s" % (added, args.db))


def cmd_match(args):
    tolerance = parse_money(args.tolerance, "tolerance")
    if tolerance < 0:
        raise ReconError("tolerance must be zero or positive")
    if args.days < 0:
        raise ReconError("--days must be zero or positive")
    data = load_db(args.db)
    created = run_match(data, tolerance, args.days)
    save_db(args.db, data)
    print("created %d new match(es)" % len(created))


def cmd_unmatch(args):
    data = load_db(args.db)
    before = len(data["matches"])
    data["matches"] = [m for m in data["matches"] if m["id"] != args.match_id]
    if len(data["matches"]) == before:
        raise ReconError("unknown match id %s" % args.match_id)
    save_db(args.db, data)
    print("removed match %s" % args.match_id)


def cmd_status(args):
    data = load_db(args.db)
    matched_statements = {m["statement_id"] for m in data["matches"]}
    matched_records = {m["record_id"] for m in data["matches"]}
    print("statements: %d (%d matched, %d unmatched)"
          % (len(data["statements"]), len(matched_statements),
             len(data["statements"]) - len(matched_statements)))
    print("records: %d (%d matched, %d unmatched)"
          % (len(data["records"]), len(matched_records),
             len(data["records"]) - len(matched_records)))


def cmd_report(args):
    data = load_db(args.db)
    for line in build_report(data):
        print(line)


def cmd_export(args):
    data = load_db(args.db)
    export_csv(data, args.out)
    print("wrote %s" % args.out)


def build_parser():
    parser = argparse.ArgumentParser(
        prog="recon-book",
        description="Offline bank statement reconciliation ledger (zero-dependency).")
    parser.add_argument("--db", default="recon-book.json",
                        help="path to the JSON store (default: recon-book.json)")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("import-statement", help="import a bank statement CSV")
    p.add_argument("csv", help="CSV with columns date,description,amount[,reference]")
    p.set_defaults(func=cmd_import_statement)

    p = sub.add_parser("import-records", help="import a business records CSV")
    p.add_argument("csv", help="CSV with columns date,party,amount,direction[,reference]")
    p.set_defaults(func=cmd_import_records)

    p = sub.add_parser("match", help="auto-match unmatched lines one-to-one")
    p.add_argument("--days", type=int, default=3,
                   help="date window in days on either side (default: 3)")
    p.add_argument("--tolerance", default="0",
                   help="amount tolerance in rupees (default: 0)")
    p.set_defaults(func=cmd_match)

    p = sub.add_parser("unmatch", help="remove a match by id, freeing both sides")
    p.add_argument("match_id")
    p.set_defaults(func=cmd_unmatch)

    p = sub.add_parser("status", help="counts of matched and unmatched entries")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("report", help="full reconciliation report")
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("export", help="write a CSV of every line with its match status")
    p.add_argument("out", help="output CSV path")
    p.set_defaults(func=cmd_export)

    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        args.func(args)
    except ReconError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
