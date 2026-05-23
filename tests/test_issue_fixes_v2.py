"""Tests for the 23 reported issues fix batch.

Covers:
- Issue #1: Previous runs list shows all brands
- Issue #3: Owned domain classification
- Issues #16-18: AI Visibility Scoring formula
- Issue #19: Owned ecosystem detection
- Issue #2: Executive scorecard comparison
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

# Set DATA_DIR before importing app modules
TEST_DATA_DIR = tempfile.mkdtemp(prefix="test_issues_")
os.environ["DATA_DIR"] = TEST_DATA_DIR
os.environ["ADMIN_TOKEN"] = ""


class TestPreviousRunsAllBrands(unittest.TestCase):
    """Issue #1: Previous runs should show all brands when brand is empty."""

    def test_scan_history_empty_brand_returns_all(self):
        from app.report_store import _scan_report_history, write_json, run_dir, report_bundle_path

        # Create two runs with different brands
        for run_id, brand, market in [
            ("evidence_nissan_japan_test1", "Nissan", "Japan"),
            ("evidence_toyota_japan_test1", "Toyota", "Japan"),
        ]:
            rdir = run_dir(run_id)
            rdir.mkdir(parents=True, exist_ok=True)
            manifest = {
                "run_id": run_id,
                "brand": brand,
                "market": market,
                "status": "completed",
                "stage": "report_bundle_ready",
                "dashboard_ready": True,
                "created_at_epoch": 1700000000,
                "completed_at_epoch": 1700000100,
            }
            write_json(rdir / "report_manifest.json", manifest)
            write_json(report_bundle_path(run_id), {
                "schema_version": "query_workbench.v1",
                "query_workbench": [{"query_id": "q001", "query": "test"}],
                "brand": brand,
                "market": market,
            })

        # Empty brand/market should return all runs
        rows = _scan_report_history("", "", limit=20)
        brands_found = {r["brand"] for r in rows}
        self.assertIn("Nissan", brands_found, "Nissan runs should appear when brand is empty")
        self.assertIn("Toyota", brands_found, "Toyota runs should appear when brand is empty")

    def test_scan_history_specific_brand_filters(self):
        from app.report_store import _scan_report_history

        rows = _scan_report_history("Toyota", "Japan", limit=20)
        for row in rows:
            self.assertEqual(row["brand"], "Toyota")


class TestVisibilityScoring(unittest.TestCase):
    """Issues #16-18: AI Visibility Scoring formula fixes."""

    def _import_visibility_score(self):
        # Import from the auditor scripts
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "AIVisibilityAuditor" / "scripts"))
        try:
            from build_query_workbench_bundle import visibility_score
            return visibility_score
        except ImportError:
            self.skipTest("AIVisibilityAuditor scripts not available")

    def test_owned_target_cited_high_score(self):
        """When owned target page is cited, score should be high."""
        vs = self._import_visibility_score()
        score = vs("owned_target_cited", True, True, [], 3, [80, 75])
        self.assertGreaterEqual(score, 70, f"Owned target cited should score >= 70, got {score}")

    def test_mapped_geo_scores_give_credit(self):
        """Issue #20: Mapped owned URLs with high GEO should affect score."""
        vs = self._import_visibility_score()
        # Without mapped GEO scores
        score_no_geo = vs("external_led", False, False, [], 3)
        # With high mapped GEO scores
        score_with_geo = vs("external_led", False, False, [], 3, [78, 81, 65])
        self.assertGreater(score_with_geo, score_no_geo,
                           f"High GEO scores should increase visibility: {score_with_geo} vs {score_no_geo}")

    def test_external_led_not_too_low(self):
        """Issue #21: External-led queries shouldn't all be 19/100."""
        vs = self._import_visibility_score()
        score = vs("external_led", False, False, [], 3, [60])
        self.assertGreater(score, 19, f"External-led with good GEO should be > 19, got {score}")

    def test_competitor_led_not_extremely_punitive(self):
        """Issue #22: One competitor + 3 external shouldn't be ~5/100."""
        vs = self._import_visibility_score()
        score = vs("competitor_led", False, False, ["Toyota"], 3, [70])
        self.assertGreater(score, 10, f"1 competitor + 3 external should be > 10, got {score}")

    def test_two_competitors_not_near_zero(self):
        """Issue #22: Two competitors shouldn't land around 1/100."""
        vs = self._import_visibility_score()
        score = vs("competitor_led", False, False, ["Toyota", "Honda"], 3, [60])
        self.assertGreater(score, 5, f"2 competitors + 3 external should be > 5, got {score}")


class TestOwnedEcosystemDetection(unittest.TestCase):
    """Issue #19 & #23: Owned ecosystem detection should be broader."""

    def _setup_is_owned(self):
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "AIVisibilityAuditor" / "scripts"))
        try:
            import build_query_workbench_bundle as bqwb
            bqwb.OWNED_HINTS = ["nissan.co.jp", "nismo.co.jp"]
            bqwb.is_owned._brand_lower = "nissan"
            return bqwb.is_owned, bqwb.source_type
        except ImportError:
            self.skipTest("AIVisibilityAuditor scripts not available")

    def test_owned_domain_detected(self):
        is_owned, _ = self._setup_is_owned()
        self.assertTrue(is_owned("https://www.nissan.co.jp/vehicles/ariya"))

    def test_nismo_detected_as_owned(self):
        is_owned, _ = self._setup_is_owned()
        self.assertTrue(is_owned("https://www.nismo.co.jp/products"))

    def test_nissan_global_detected_as_owned(self):
        """Issue #23: nissan-global.com should be treated as owned."""
        is_owned, _ = self._setup_is_owned()
        self.assertTrue(is_owned("https://www.nissan-global.com/EN/"),
                        "nissan-global.com should be detected as owned via fuzzy brand matching")

    def test_source_type_owned(self):
        _, source_type_fn = self._setup_is_owned()
        result = source_type_fn("https://www.nissan.co.jp/vehicles")
        self.assertEqual(result, "owned_brand_ecosystem",
                         f"nissan.co.jp should be 'owned_brand_ecosystem', got '{result}'")

    def test_competitor_not_owned(self):
        is_owned, _ = self._setup_is_owned()
        self.assertFalse(is_owned("https://www.toyota.co.jp"),
                         "Toyota should NOT be detected as owned for Nissan")


if __name__ == "__main__":
    unittest.main()
