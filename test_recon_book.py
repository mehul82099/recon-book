import csv
import json
import os
import subprocess
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
CLI = os.path.join(REPO_ROOT, "recon_book.py")


def run_cli(db, *args, cwd=None):
    cmd = [sys.executable, CLI, "--db", db] + list(args)
    return subprocess.run(cmd, capture_output=True, text=True, cwd=cwd or REPO_ROOT)


def write_csv(path, header, rows):
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        writer.writerows(rows)


STATEMENT_HEADER = ["date", "description", "amount", "reference"]
RECORD_HEADER = ["date", "party", "amount", "direction", "reference"]


class ReconBookTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "store.json")
        self.statement_csv = os.path.join(self.tmp.name, "statement.csv")
        self.records_csv = os.path.join(self.tmp.name, "records.csv")

    def tearDown(self):
        self.tmp.cleanup()

    def import_both(self, statement_rows, record_rows):
        write_csv(self.statement_csv, STATEMENT_HEADER, statement_rows)
        write_csv(self.records_csv, RECORD_HEADER, record_rows)
        r1 = run_cli(self.db, "import-statement", self.statement_csv)
        r2 = run_cli(self.db, "import-records", self.records_csv)
        self.assertEqual(r1.returncode, 0, r1.stderr)
        self.assertEqual(r2.returncode, 0, r2.stderr)

    def load_db(self):
        with open(self.db, "r", encoding="utf-8") as handle:
            return json.load(handle)

    def test_exact_match(self):
        self.import_both(
            [["2026-11-02", "NEFT from Sharma Traders", "5000.00", ""]],
            [["2026-11-01", "Sharma Traders", "5000.00", "in", "INV-101"]],
        )
        result = run_cli(self.db, "match")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("created 1 new match", result.stdout)
        data = self.load_db()
        self.assertEqual(len(data["matches"]), 1)
        self.assertEqual(data["matches"][0]["statement_id"], "S1")
        self.assertEqual(data["matches"][0]["record_id"], "R1")

    def test_amount_mismatch_no_match(self):
        self.import_both(
            [["2026-11-02", "NEFT from Sharma Traders", "5000.00", ""]],
            [["2026-11-01", "Sharma Traders", "4990.00", "in", "INV-101"]],
        )
        run_cli(self.db, "match")
        self.assertEqual(self.load_db()["matches"], [])

    def test_tolerance_match(self):
        self.import_both(
            [["2026-11-02", "NEFT from Sharma Traders", "5000.00", ""]],
            [["2026-11-01", "Sharma Traders", "4990.00", "in", "INV-101"]],
        )
        result = run_cli(self.db, "match", "--tolerance", "10")
        self.assertIn("created 1 new match", result.stdout)

    def test_tolerance_boundary_inclusive(self):
        self.import_both(
            [["2026-11-02", "NEFT", "5000.00", ""]],
            [["2026-11-01", "Sharma Traders", "4990.00", "in", ""]],
        )
        result = run_cli(self.db, "match", "--tolerance", "10.00")
        self.assertIn("created 1 new match", result.stdout)

    def test_tolerance_just_over_no_match(self):
        self.import_both(
            [["2026-11-02", "NEFT", "5000.00", ""]],
            [["2026-11-01", "Sharma Traders", "4989.99", "in", ""]],
        )
        run_cli(self.db, "match", "--tolerance", "10.00")
        self.assertEqual(self.load_db()["matches"], [])

    def test_date_window_excludes_old_record(self):
        self.import_both(
            [["2026-11-10", "NEFT", "1000.00", ""]],
            [["2026-11-01", "Old party", "1000.00", "in", ""]],
        )
        run_cli(self.db, "match", "--days", "3")
        self.assertEqual(self.load_db()["matches"], [])

    def test_date_window_boundary_inclusive(self):
        self.import_both(
            [["2026-11-04", "NEFT", "1000.00", ""]],
            [["2026-11-01", "Party", "1000.00", "in", ""]],
        )
        result = run_cli(self.db, "match", "--days", "3")
        self.assertIn("created 1 new match", result.stdout)

    def test_direction_mismatch_no_match(self):
        self.import_both(
            [["2026-11-02", "Payment to supplier", "-800.00", ""]],
            [["2026-11-02", "Customer", "800.00", "in", ""]],
        )
        run_cli(self.db, "match")
        self.assertEqual(self.load_db()["matches"], [])

    def test_expense_direction_matches(self):
        self.import_both(
            [["2026-11-02", "Payment to supplier", "-800.00", ""]],
            [["2026-11-02", "Supplier", "800.00", "out", ""]],
        )
        result = run_cli(self.db, "match")
        self.assertIn("created 1 new match", result.stdout)

    def test_greedy_one_to_one(self):
        self.import_both(
            [["2026-11-02", "NEFT", "1000.00", ""]],
            [
                ["2026-11-02", "Party A", "1000.00", "in", ""],
                ["2026-11-02", "Party B", "1000.00", "in", ""],
            ],
        )
        result = run_cli(self.db, "match")
        self.assertIn("created 1 new match", result.stdout)
        data = self.load_db()
        # Deterministic tie-break: the lower record id wins.
        self.assertEqual(data["matches"][0]["record_id"], "R1")

    def test_reference_boost_wins(self):
        self.import_both(
            [["2026-11-02", "NEFT INV-202 from customer", "1000.00", ""]],
            [
                ["2026-11-02", "Party A", "1000.00", "in", "INV-201"],
                ["2026-11-02", "Party B", "1000.00", "in", "INV-202"],
            ],
        )
        run_cli(self.db, "match")
        data = self.load_db()
        self.assertEqual(data["matches"][0]["record_id"], "R2")

    def test_match_is_idempotent(self):
        self.import_both(
            [["2026-11-02", "NEFT", "1000.00", ""]],
            [["2026-11-02", "Party", "1000.00", "in", ""]],
        )
        run_cli(self.db, "match")
        result = run_cli(self.db, "match")
        self.assertIn("created 0 new match", result.stdout)
        self.assertEqual(len(self.load_db()["matches"]), 1)

    def test_unmatch_frees_both_sides(self):
        self.import_both(
            [["2026-11-02", "NEFT", "1000.00", ""]],
            [["2026-11-02", "Party", "1000.00", "in", ""]],
        )
        run_cli(self.db, "match")
        result = run_cli(self.db, "unmatch", "M1")
        self.assertEqual(result.returncode, 0, result.stderr)
        data = self.load_db()
        self.assertEqual(data["matches"], [])
        # And it can be matched again.
        run_cli(self.db, "match")
        self.assertEqual(len(self.load_db()["matches"]), 1)

    def test_unmatch_unknown_id(self):
        self.import_both(
            [["2026-11-02", "NEFT", "1000.00", ""]],
            [["2026-11-02", "Party", "1000.00", "in", ""]],
        )
        result = run_cli(self.db, "unmatch", "M99")
        self.assertEqual(result.returncode, 2)
        self.assertIn("unknown match id", result.stderr)

    def test_negative_tolerance_rejected(self):
        self.import_both(
            [["2026-11-02", "NEFT", "1000.00", ""]],
            [["2026-11-02", "Party", "1000.00", "in", ""]],
        )
        result = run_cli(self.db, "match", "--tolerance", "-5")
        self.assertEqual(result.returncode, 2)
        self.assertIn("tolerance", result.stderr)

    def test_negative_days_rejected(self):
        self.import_both([], [])
        result = run_cli(self.db, "match", "--days", "-1")
        self.assertEqual(result.returncode, 2)
        self.assertIn("days", result.stderr)

    def test_statement_csv_missing_column(self):
        write_csv(self.statement_csv, ["date", "description"], [["2026-11-02", "x"]])
        result = run_cli(self.db, "import-statement", self.statement_csv)
        self.assertEqual(result.returncode, 2)
        self.assertIn("amount", result.stderr)

    def test_statement_csv_bad_amount(self):
        write_csv(self.statement_csv, STATEMENT_HEADER, [["2026-11-02", "x", "not-a-number", ""]])
        result = run_cli(self.db, "import-statement", self.statement_csv)
        self.assertEqual(result.returncode, 2)
        self.assertIn("invalid", result.stderr)

    def test_statement_csv_bad_date(self):
        write_csv(self.statement_csv, STATEMENT_HEADER, [["02/11/2026", "x", "100", ""]])
        result = run_cli(self.db, "import-statement", self.statement_csv)
        self.assertEqual(result.returncode, 2)
        self.assertIn("invalid", result.stderr)

    def test_record_bad_direction(self):
        write_csv(self.records_csv, RECORD_HEADER,
                  [["2026-11-02", "Party", "100", "sideways", ""]])
        result = run_cli(self.db, "import-records", self.records_csv)
        self.assertEqual(result.returncode, 2)
        self.assertIn("direction", result.stderr)

    def test_record_negative_amount_rejected(self):
        write_csv(self.records_csv, RECORD_HEADER,
                  [["2026-11-02", "Party", "-100", "in", ""]])
        result = run_cli(self.db, "import-records", self.records_csv)
        self.assertEqual(result.returncode, 2)
        self.assertIn("positive", result.stderr)

    def test_status_counts(self):
        self.import_both(
            [
                ["2026-11-02", "NEFT A", "1000.00", ""],
                ["2026-11-03", "NEFT B", "2000.00", ""],
            ],
            [["2026-11-02", "Party A", "1000.00", "in", ""]],
        )
        run_cli(self.db, "match")
        result = run_cli(self.db, "status")
        self.assertIn("statements: 2 (1 matched, 1 unmatched)", result.stdout)
        self.assertIn("records: 1 (1 matched, 0 unmatched)", result.stdout)

    def test_report_lists_unmatched_both_sides(self):
        self.import_both(
            [
                ["2026-11-02", "NEFT A", "1000.00", ""],
                ["2026-11-20", "UNKNOWN UPI", "777.00", ""],
            ],
            [
                ["2026-11-02", "Party A", "1000.00", "in", ""],
                ["2026-11-05", "Unpaid Customer", "9000.00", "in", "INV-300"],
            ],
        )
        run_cli(self.db, "match")
        result = run_cli(self.db, "report")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("matches: 1", result.stdout)
        self.assertIn("unmatched statement lines: 1", result.stdout)
        self.assertIn("UNKNOWN UPI", result.stdout)
        self.assertIn("unmatched records: 1", result.stdout)
        self.assertIn("Unpaid Customer", result.stdout)

    def test_report_empty_store(self):
        result = run_cli(self.db, "report")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("matches: 0", result.stdout)
        self.assertIn("unmatched statement lines: 0", result.stdout)

    def test_export_csv(self):
        self.import_both(
            [
                ["2026-11-02", "NEFT A", "1000.00", ""],
                ["2026-11-03", "NEFT B", "2000.00", ""],
            ],
            [["2026-11-02", "Party A", "1000.00", "in", ""]],
        )
        run_cli(self.db, "match")
        out = os.path.join(self.tmp.name, "out.csv")
        result = run_cli(self.db, "export", out)
        self.assertEqual(result.returncode, 0, result.stderr)
        with open(out, newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(len(rows), 3)
        by_id = {row["id"]: row for row in rows}
        self.assertEqual(by_id["S1"]["status"], "matched")
        self.assertEqual(by_id["S1"]["match_id"], "M1")
        self.assertEqual(by_id["S2"]["status"], "unmatched")
        self.assertEqual(by_id["R1"]["status"], "matched")

    def test_persistence_across_invocations(self):
        self.import_both(
            [["2026-11-02", "NEFT", "1000.00", ""]],
            [],
        )
        result = run_cli(self.db, "status")
        self.assertIn("statements: 1", result.stdout)

    def test_store_file_is_valid_json_with_no_temp_leftovers(self):
        self.import_both(
            [["2026-11-02", "NEFT", "1000.00", ""]],
            [["2026-11-02", "Party", "1000.00", "in", ""]],
        )
        run_cli(self.db, "match")
        with open(self.db, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        self.assertEqual(data["version"], 1)
        leftovers = [name for name in os.listdir(self.tmp.name)
                     if name.startswith(".recon-book-")]
        self.assertEqual(leftovers, [])

    def test_paise_precision(self):
        self.import_both(
            [["2026-11-02", "NEFT", "99.99", ""]],
            [["2026-11-02", "Party", "99.99", "in", ""]],
        )
        result = run_cli(self.db, "match")
        self.assertIn("created 1 new match", result.stdout)
        self.import_both(
            [["2026-11-03", "NEFT2", "99.99", ""]],
            [["2026-11-03", "Party2", "99.98", "in", ""]],
        )
        run_cli(self.db, "match")
        self.assertEqual(len(self.load_db()["matches"]), 1)

    def test_corrupt_store_rejected(self):
        with open(self.db, "w", encoding="utf-8") as handle:
            handle.write("{not json")
        result = run_cli(self.db, "status")
        self.assertEqual(result.returncode, 2)
        self.assertIn("not valid JSON", result.stderr)

    def test_missing_file_import_rejected(self):
        result = run_cli(self.db, "import-statement",
                         os.path.join(self.tmp.name, "nope.csv"))
        self.assertNotEqual(result.returncode, 0)

    def test_multiple_statements_two_records_best_assignment(self):
        self.import_both(
            [
                ["2026-11-02", "NEFT 1", "1000.00", ""],
                ["2026-11-02", "NEFT 2", "1000.00", ""],
            ],
            [
                ["2026-11-02", "Party A", "1000.00", "in", ""],
                ["2026-11-02", "Party B", "1000.00", "in", ""],
            ],
        )
        result = run_cli(self.db, "match")
        self.assertIn("created 2 new match", result.stdout)
        data = self.load_db()
        used_records = sorted(m["record_id"] for m in data["matches"])
        used_statements = sorted(m["statement_id"] for m in data["matches"])
        self.assertEqual(used_records, ["R1", "R2"])
        self.assertEqual(used_statements, ["S1", "S2"])


if __name__ == "__main__":
    unittest.main()
