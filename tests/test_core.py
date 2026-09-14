"""Engine behavior at input, validation, and comparison failure boundaries."""

from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
import csv
from decimal import Decimal
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from opscheck.core import Dataset, MAX_FILE_BYTES, OpsCheckError, compare, load_csv, load_rules, validate


class CoreTestCase(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def csv(self, text, name="input.csv", delimiter=","):
        path = self.root / name
        if isinstance(text, bytes):
            path.write_bytes(text)
        else:
            path.write_text(text, encoding="utf-8", newline="")
        return load_csv(path, delimiter=delimiter)

    def rules(self, text):
        path = self.root / "rules.json"
        path.write_text(text, encoding="utf-8")
        return load_rules(path)


class CSVTests(CoreTestCase):
    def test_bom_unicode_quoted_comma_and_multiline_record(self):
        data = self.csv('\ufeffid,note\r\n1,"hello, José"\r\n2,"line one\r\nline two"\r\n')
        self.assertEqual(data.source, "input.csv")
        self.assertEqual(data.headers, ["id", "note"])
        self.assertEqual(data.rows[0]["note"], "hello, José")
        self.assertEqual(data.rows[1]["note"], "line one\r\nline two")

    def test_headers_and_values_are_preserved_exactly(self):
        data = self.csv("id, Name \n 01 , James  \n")
        self.assertEqual(data.headers, ["id", " Name "])
        self.assertEqual(data.rows, [{"id": " 01 ", " Name ": " James  "}])

    def test_header_only_is_valid(self):
        self.assertEqual(self.csv("id,total\n").rows, [])

    def test_alternate_delimiter(self):
        self.assertEqual(self.csv("id;name\n1;Ana\n", delimiter=";").rows, [{"id": "1", "name": "Ana"}])

    def test_missing_blank_and_duplicate_headers_are_rejected(self):
        for value in ("", "\n", "id, \n", "id,id\n1,2\n"):
            with self.subTest(value=value), self.assertRaises(OpsCheckError):
                self.csv(value)

    def test_width_mismatch_and_malformed_quotes_are_rejected(self):
        for value in ("id,name\n1\n", "id,name\n1,Ana,extra\n", 'id,name\n1,"unfinished\n', 'id,name\n1,"Ana"extra\n'):
            with self.subTest(value=value), self.assertRaises(OpsCheckError):
                self.csv(value)

    def test_invalid_utf8_is_rejected(self):
        with self.assertRaisesRegex(OpsCheckError, "UTF-8"):
            self.csv(b"id,name\n1,\xff\n")

    def test_invalid_delimiter_is_rejected(self):
        for value in ("", "::", "\n", "\0", '"', None):
            with self.subTest(value=value), self.assertRaises(OpsCheckError):
                load_csv(self.root / "missing.csv", delimiter=value)

    def test_io_failure_is_a_domain_error(self):
        with self.assertRaisesRegex(OpsCheckError, "Cannot read CSV"):
            load_csv(self.root / "missing.csv")

    def test_file_limit_is_enforced_before_parsing(self):
        path = self.root / "large.csv"
        with path.open("wb") as stream:
            stream.write(b"id\n")
            stream.truncate(MAX_FILE_BYTES + 1)
        with self.assertRaisesRegex(OpsCheckError, "10 MiB"):
            load_csv(path)

    def test_large_field_under_file_limit_is_supported(self):
        value = "a" * 200_000
        self.assertEqual(self.csv("note\n" + value + "\n").rows[0]["note"], value)

    def test_concurrent_large_field_loads_preserve_parser_limit(self):
        path = self.root / "parallel.csv"
        path.write_text("note\n" + "a" * 200_000 + "\n", encoding="utf-8")
        real_reader = csv.reader
        original_limit = csv.field_size_limit()

        def cooperative_reader(*args, **kwargs):
            reader = real_reader(*args, **kwargs)
            yield next(reader)
            # Simulate a scheduler handoff between header and large data fields.
            time.sleep(0.002)
            yield from reader

        with patch("opscheck.core.csv.reader", side_effect=cooperative_reader):
            with ThreadPoolExecutor(max_workers=4) as workers:
                datasets = list(workers.map(load_csv, [path] * 12))
        self.assertTrue(all(len(data.rows[0]["note"]) == 200_000 for data in datasets))
        self.assertEqual(csv.field_size_limit(), original_limit)

    def test_record_limit_accepts_boundary_and_rejects_next_record(self):
        with patch("opscheck.core.MAX_RECORDS", 2):
            self.assertEqual(len(self.csv("id\n1\n2\n").rows), 2)
            with self.assertRaisesRegex(OpsCheckError, "record limit"):
                self.csv("id\n1\n2\n3\n")


class RuleTests(CoreTestCase):
    def test_decimal_bounds_are_loaded_without_float_rounding(self):
        config = self.rules('{"version":1,"columns":{"total":{"type":"number","min":0.1234567890123456789}}}')
        self.assertEqual(config["columns"]["total"]["min"], Decimal("0.1234567890123456789"))

    def test_json_duplicate_keys_are_rejected_at_every_level(self):
        for value in (
            '{"version":1,"version":1,"columns":{}}',
            '{"version":1,"columns":{"id":{},"id":{}}}',
            '{"version":1,"columns":{"id":{"required":true,"required":false}}}',
        ):
            with self.subTest(value=value), self.assertRaisesRegex(OpsCheckError, "duplicate key"):
                self.rules(value)

    def test_nonfinite_json_numbers_and_malformed_json_are_rejected(self):
        for constant in ("NaN", "Infinity", "-Infinity"):
            with self.subTest(constant=constant), self.assertRaises(OpsCheckError):
                self.rules('{"version":1,"columns":{"n":{"type":"number","min":' + constant + '}}}')
        with self.assertRaises(OpsCheckError):
            self.rules('{"version":1,}')

    def test_direct_dictionary_calls_reject_invalid_configurations(self):
        data = self.csv("id,n\n1,1\n")
        invalid = [
            [], {}, {"version": True, "columns": {}}, {"version": 2, "columns": {}},
            {"version": 1, "columns": {}, "extra": True}, {"version": 1, "columns": []},
            {"version": 1, "columns": {" ": {}}},
        ]
        bad_rules = [
            [], {"typo": True}, {"required": "yes"}, {"unique": 1}, {"type": "currency"},
            {"type": []}, {"min": 0}, {"type": "number", "min": True},
            {"type": "number", "max": float("inf")}, {"type": "number", "max": Decimal("NaN")},
            {"type": "number", "min": "0"}, {"type": "number", "min": 2, "max": 1},
            {"allowed": []}, {"allowed": [1]}, {"allowed": "paid"},
        ]
        invalid.extend({"version": 1, "columns": {"n": rule}} for rule in bad_rules)
        for config in invalid:
            with self.subTest(config=config), self.assertRaises(OpsCheckError):
                validate(data, config)


class ValidationTests(CoreTestCase):
    def test_realistic_messy_export_has_actionable_counts_and_locations(self):
        data = self.csv("id,email,total,date,status\n1,ana@example.com,0,2024-02-29,paid\n 1 ,bad,-1,2025-02-29,lost\n, ,NaN,2025-2-03,pending\n")
        config = {"version": 1, "columns": {
            "id": {"required": True, "unique": True}, "email": {"type": "email"},
            "total": {"type": "number", "min": 0}, "date": {"type": "date"},
            "status": {"allowed": ["paid", "pending"]},
        }}
        original = deepcopy(data.rows)
        result = validate(data, config)
        self.assertEqual(result["status"], "fail")
        self.assertEqual(result["summary"], {"rows": 3, "issues": 8, "affected_rows": 2, "reported_findings": 8})
        self.assertEqual([(item["row"], item["column"], item["code"]) for item in result["findings"]], [
            (3, "id", "duplicate"), (3, "email", "type"), (3, "total", "min"),
            (3, "date", "type"), (3, "status", "allowed"), (4, "id", "required"),
            (4, "total", "type"), (4, "date", "type"),
        ])
        self.assertEqual(data.rows, original)

    def test_optional_blank_cells_skip_other_rules_and_zero_is_not_blank(self):
        data = self.csv("id,n\n1, \n2,\n3,0\n")
        result = validate(data, {"version": 1, "columns": {"n": {
            "type": "integer", "min": 0, "allowed": ["0"], "unique": True,
        }}})
        self.assertEqual(result["status"], "pass")

    def test_all_duplicate_occurrences_after_first_are_reported(self):
        data = self.csv("id\na\n a \na\n\" \"\n\"\"\n")
        result = validate(data, {"version": 1, "columns": {"id": {"unique": True}}})
        self.assertEqual([item["row"] for item in result["findings"]], [3, 4])
        self.assertTrue(all("record 2" in item["message"] for item in result["findings"]))

    def test_missing_optional_column_fails_even_with_no_data(self):
        result = validate(self.csv("id\n"), {"version": 1, "columns": {"email": {"required": False}}})
        self.assertEqual(result["status"], "fail")
        self.assertEqual(result["summary"]["affected_rows"], 0)
        self.assertEqual(result["findings"][0]["row"], None)
        self.assertEqual(result["findings"][0]["code"], "missing_column")

    def test_trimmed_validation_preserves_original_report_values(self):
        data = self.csv("id,status\n1, paid \n2, lost \n")
        result = validate(data, {"version": 1, "columns": {"status": {"allowed": ["paid"]}}})
        self.assertEqual(result["summary"]["issues"], 1)
        self.assertEqual(result["findings"][0]["value"], " lost ")

    def test_number_parsing_is_finite_and_integer_parsing_is_strict(self):
        for value, number_ok, integer_ok in (
            ("0", True, True), ("-2", True, True), ("+3", True, True),
            ("1.0", True, False), (".5", True, False), ("1e3", True, False),
            ("NaN", False, False), ("Infinity", False, False), ("1_000", False, False),
        ):
            for kind, expected in (("number", number_ok), ("integer", integer_ok)):
                with self.subTest(value=value, kind=kind):
                    data = self.csv("n\n" + value + "\n")
                    result = validate(data, {"version": 1, "columns": {"n": {"type": kind}}})
                    self.assertEqual(result["status"], "pass" if expected else "fail")

    def test_decimal_boundary_is_exact_and_inclusive(self):
        rules = {"version": 1, "columns": {"n": {"type": "number", "min": Decimal("0.1"), "max": Decimal("0.3")}}}
        result = validate(self.csv("n\n0.1\n0.3\n0.30000000000000000001\n"), rules)
        self.assertEqual([(item["row"], item["code"]) for item in result["findings"]], [(4, "max")])

    def test_dates_require_exact_format_and_valid_calendar_dates(self):
        result = validate(self.csv("d\n2024-02-29\n2025-02-29\n2025-2-03\n2025-01-01T00:00:00\n0000-01-01\n"),
                          {"version": 1, "columns": {"d": {"type": "date"}}})
        self.assertEqual([item["row"] for item in result["findings"]], [3, 4, 5, 6])

    def test_caps_keep_complete_counts_and_deterministic_first_findings(self):
        data = self.csv("id,n\n1,-1\n1,-2\n2,-3\n")
        rules = {"version": 1, "columns": {"id": {"unique": True}, "n": {"type": "number", "min": 0}}}
        full, capped, none = [validate(data, rules, max_findings=cap) for cap in (100, 2, 0)]
        self.assertEqual(capped["findings"], full["findings"][:2])
        self.assertEqual(none["findings"], [])
        for result in (full, capped, none):
            self.assertEqual(result["summary"]["issues"], 4)
            self.assertEqual(result["summary"]["affected_rows"], 3)
            self.assertEqual(result["status"], "fail")
        self.assertEqual(capped, validate(data, rules, max_findings=2))

    def test_multiline_values_do_not_shift_logical_record_numbers(self):
        result = validate(self.csv('id,note\n1,"two\nlines"\n1,duplicate\n'),
                          {"version": 1, "columns": {"id": {"unique": True}}})
        self.assertEqual(result["findings"][0]["row"], 3)


class ComparisonTests(CoreTestCase):
    def test_added_removed_changed_and_unchanged_records(self):
        before = self.csv("id,total,status\nD,4,paid\nB,2,pending\nA,1,paid\n", "before.csv")
        after = self.csv("id,total,status\nC,3,paid\nA,1,paid\nB,2,paid\n", "after.csv")
        result = compare(before, after, "id")
        self.assertEqual(result["status"], "changed")
        self.assertEqual(result["summary"], {
            "before_rows": 3, "after_rows": 3, "added": 1, "removed": 1, "changed": 1,
            "unchanged": 1, "total_changes": 3, "reported_findings": 3,
        })
        self.assertEqual([(item["key"], item["kind"]) for item in result["findings"]], [("B", "changed"), ("C", "added"), ("D", "removed")])
        self.assertEqual(result["findings"][0]["fields"], ["status"])
        self.assertEqual(result["findings"][0]["before"]["status"], "pending")
        self.assertEqual(result["findings"][1]["before"], None)
        self.assertEqual(result["findings"][2]["after"], None)

    def test_reordering_records_columns_and_trimming_keys_does_not_change_data(self):
        before = self.csv("id,name\nA,Ana\n B ,Ben\n", "before.csv")
        after = self.csv("name,id\nBen,B\nAna, A \n", "after.csv")
        self.assertEqual(compare(before, after, "id")["status"], "unchanged")

    def test_shared_nonkey_values_are_compared_exactly(self):
        result = compare(self.csv("id,n\n1,1.0\n2,text\n", "before.csv"),
                         self.csv("id,n\n1,1\n2, text \n", "after.csv"), "id")
        self.assertEqual(result["summary"]["changed"], 2)

    def test_schema_change_is_separate_from_shared_value_changes(self):
        result = compare(self.csv("id,old,shared\n1,x,same\n", "before.csv"),
                         self.csv("id,new,shared\n1,y,same\n", "after.csv"), "id")
        self.assertEqual(result["schema"], {"added": ["new"], "removed": ["old"]})
        self.assertEqual(result["status"], "changed")
        self.assertEqual(result["summary"]["total_changes"], 0)
        self.assertEqual(result["summary"]["unchanged"], 1)
        self.assertEqual(result["findings"], [])

    def test_missing_blank_and_duplicate_keys_are_rejected_on_either_side(self):
        valid = self.csv("id,name\n1,Ana\n", "valid.csv")
        for text in ("other,name\n1,Ana\n", "id,name\n ,Ana\n", "id,name\n1,Ana\n 1 ,Ben\n"):
            bad = self.csv(text, "bad.csv")
            for before, after in ((valid, bad), (bad, valid)):
                with self.subTest(text=text, before=before.source), self.assertRaises(OpsCheckError):
                    compare(before, after, "id")

    def test_header_only_comparison_and_schema_change(self):
        empty = self.csv("id\n", "empty.csv")
        self.assertEqual(compare(empty, empty, "id")["status"], "unchanged")
        result = compare(empty, self.csv("id,new\n", "schema.csv"), "id")
        self.assertEqual(result["status"], "changed")
        self.assertEqual(result["summary"]["total_changes"], 0)

    def test_comparison_cap_keeps_full_counts(self):
        before = self.csv("id\nZ\n", "before.csv")
        after = self.csv("id\nB\nA\n", "after.csv")
        for cap in (0, 1):
            with self.subTest(cap=cap):
                result = compare(before, after, "id", max_findings=cap)
                self.assertEqual(result["summary"]["total_changes"], 3)
                self.assertEqual(len(result["findings"]), cap)
                self.assertEqual(result["status"], "changed")
        self.assertEqual(compare(before, after, "id", max_findings=1)["findings"][0]["key"], "A")

    def test_invalid_limits_and_keys_are_domain_errors(self):
        data = self.csv("id\n1\n")
        for cap in (-1, True, 1.5, "1"):
            with self.subTest(cap=cap), self.assertRaises(OpsCheckError):
                compare(data, data, "id", max_findings=cap)
            with self.subTest(cap=cap), self.assertRaises(OpsCheckError):
                validate(data, {"version": 1, "columns": {}}, max_findings=cap)
        for key in ("", " ", None):
            with self.subTest(key=key), self.assertRaises(OpsCheckError):
                compare(data, data, key)


if __name__ == "__main__":
    unittest.main()
