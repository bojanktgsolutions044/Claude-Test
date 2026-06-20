"""Tests for the data model and result serialization in brand_finder.

These cover BrandResult defaults and the JSON/CSV output of save_results,
including the manual-worklist split. They use tmp_path so nothing touches the
real working directory.
"""
import csv
import json
import os

import brand_finder as bf
from brand_finder import BrandResult, NO_INFO


class TestBrandResult:
    def test_defaults_are_no_info_except_input(self):
        r = BrandResult(input_brand="Acme")
        assert r.input_brand == "Acme"
        assert r.amazon_seller_name == NO_INFO
        assert r.status == NO_INFO
        assert r.notes == ""  # notes defaults to empty string, not NO_INFO

    def test_every_csv_field_exists_on_the_dataclass(self):
        # Guards against CSV_FIELDS drifting away from the dataclass schema.
        r = BrandResult(input_brand="Acme")
        from dataclasses import asdict

        keys = set(asdict(r).keys())
        for field in bf.CSV_FIELDS:
            assert field in keys, f"CSV field {field!r} missing from BrandResult"


class TestSaveResults:
    def test_writes_json_and_csv(self, tmp_path):
        out = tmp_path / "results.json"
        results = [BrandResult(input_brand="Acme", status="resolved")]
        bf.save_results(results, str(out))

        data = json.loads(out.read_text())
        assert data[0]["input_brand"] == "Acme"

        csv_path = tmp_path / "results.csv"
        rows = list(csv.DictReader(csv_path.open()))
        assert rows[0]["input_brand"] == "Acme"
        assert list(rows[0].keys()) == bf.CSV_FIELDS

    def test_manual_worklist_only_includes_manual_rows(self, tmp_path):
        out = tmp_path / "results.json"
        results = [
            BrandResult(input_brand="Done", status="resolved"),
            BrandResult(input_brand="Todo", status="manual_lookup_needed"),
        ]
        bf.save_results(results, str(out))

        manual_path = tmp_path / "manual_worklist.csv"
        rows = list(csv.DictReader(manual_path.open()))
        assert [r["input_brand"] for r in rows] == ["Todo"]
        assert list(rows[0].keys()) == bf.MANUAL_FIELDS
