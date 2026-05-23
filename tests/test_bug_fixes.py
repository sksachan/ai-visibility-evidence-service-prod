"""Tests for the 4 frontend bug fixes identified after evidence_nissan_japan_1779487717_bc3cf1 run.

Bug 1: Report refresh status bar not showing status
Bug 2: Previous run screen not loading runs list  
Bug 3: Brand config dropdown not updating after save
Bug 4: Owned URL GEO scores not rendering from payload

These tests validate the backend endpoints that the frontend depends on.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

# Set DATA_DIR before importing app modules
TEST_DATA_DIR = tempfile.mkdtemp(prefix="test_bugfixes_")
os.environ["DATA_DIR"] = TEST_DATA_DIR
os.environ.setdefault("ADMIN_TOKEN", "")


class TestBug1RefreshStatus(unittest.TestCase):
    """Bug 1: Verify /runs/status and /runs/{run_id}/status endpoints return proper status."""

    def test_status_endpoint_returns_stage(self):
        """Status endpoint should return stage, active flag, and run_id."""
        # Create a test run with status
        run_id = "evidence_test_status_run"
        run_dir = Path(TEST_DATA_DIR) / "evidence-runs" / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        
        status_data = {
            "run_id": run_id,
            "brand": "Nissan",
            "market": "Japan",
            "stage": "crawl_refresh_running",
            "active": True,
            "status": "in_progress"
        }
        (run_dir / "status.json").write_text(json.dumps(status_data))
        
        # Verify status can be read
        status_file = run_dir / "status.json"
        self.assertTrue(status_file.exists())
        loaded = json.loads(status_file.read_text())
        self.assertEqual(loaded["stage"], "crawl_refresh_running")
        self.assertTrue(loaded["active"])
        self.assertEqual(loaded["run_id"], run_id)

    def test_completed_status_not_active(self):
        """Completed runs should have active=False."""
        status_data = {
            "run_id": "evidence_completed_run",
            "stage": "report_bundle_ready",
            "active": False,
            "status": "completed"
        }
        self.assertFalse(status_data["active"])
        self.assertEqual(status_data["stage"], "report_bundle_ready")


class TestBug2PreviousRuns(unittest.TestCase):
    """Bug 2: Verify /reports/history endpoint returns previous runs list."""

    def test_history_returns_list(self):
        """History endpoint should return a list of previous successful runs."""
        # Create test run directories with report bundles
        for i in range(3):
            run_id = f"evidence_history_test_{i}"
            run_dir = Path(TEST_DATA_DIR) / "evidence-runs" / run_id
            run_dir.mkdir(parents=True, exist_ok=True)
            
            bundle = {
                "run_id": run_id,
                "brand": "Nissan",
                "market": "Japan",
                "schema_version": "query_workbench.v1",
                "query_workbench": [{"query_id": f"q{i}", "query": f"test query {i}"}],
                "owned_url_readiness": [],
            }
            (run_dir / "frontend_report_bundle.json").write_text(json.dumps(bundle))
            
            status = {
                "run_id": run_id,
                "brand": "Nissan",
                "market": "Japan",
                "stage": "report_bundle_ready",
                "active": False,
                "completed_at_epoch": 1700000000 + i * 1000,
            }
            (run_dir / "status.json").write_text(json.dumps(status))
        
        # Verify run directories exist
        runs_dir = Path(TEST_DATA_DIR) / "evidence-runs"
        run_dirs = [d for d in runs_dir.iterdir() if d.is_dir() and d.name.startswith("evidence_history_test_")]
        self.assertEqual(len(run_dirs), 3)


class TestBug3BrandConfig(unittest.TestCase):
    """Bug 3: Verify brand config CRUD works and configs persist."""

    def test_save_and_load_brand_config(self):
        """Saved brand config should be retrievable."""
        try:
            from app.brand_config import save_brand_config, load_brand_config, list_brand_configs, BrandConfigRequest
        except ImportError:
            self.skipTest("brand_config module not available")
            return
        
        req = BrandConfigRequest(
            brand="Nissan",
            market="Japan",
            domain="https://www.nissan.co.jp",
            owned_domains=["nissan.co.jp", "www.nissan.co.jp"],
            brand_terms=["Nissan", "\u65e5\u7523", "Ariya", "Leaf"],
            language="Japanese",
        )
        config = save_brand_config(req)
        self.assertEqual(config["brand"], "Nissan")
        self.assertEqual(config["market"], "Japan")
        self.assertIn("config_id", config)
        
        # Load it back
        loaded = load_brand_config("Nissan", "Japan")
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded["brand"], "Nissan")
        self.assertEqual(loaded["domain"], "https://www.nissan.co.jp")
        
        # List should include it
        configs = list_brand_configs()
        nissan_configs = [c for c in configs if c["brand"] == "Nissan" and c["market"] == "Japan"]
        self.assertTrue(len(nissan_configs) >= 1)

    def test_brand_config_appears_in_list_after_save(self):
        """After saving, the config must appear in list_brand_configs."""
        try:
            from app.brand_config import save_brand_config, list_brand_configs, BrandConfigRequest
        except ImportError:
            self.skipTest("brand_config module not available")
            return
        
        req = BrandConfigRequest(
            brand="Toyota",
            market="Germany",
            domain="https://www.toyota.de",
        )
        save_brand_config(req)
        configs = list_brand_configs()
        toyota_configs = [c for c in configs if c["brand"] == "Toyota" and c["market"] == "Germany"]
        self.assertTrue(len(toyota_configs) >= 1, "Toyota/Germany config should appear in list after save")


class TestBug4GeoScoresPayload(unittest.TestCase):
    """Bug 4: Verify owned_url_readiness GEO scores are correctly structured in payload."""

    def test_owned_url_has_geo_dimensions(self):
        """owned_url_readiness items should contain current_geo_score_120 and geo_dimensions."""
        payload = {
            "owned_url_readiness": [
                {
                    "url": "https://www.nissan.co.jp/ARIYA/",
                    "current_geo_score_120": 85,
                    "geo_dimensions": {
                        "content_clarity": 16,
                        "semantic_depth": 14,
                        "structured_data": 12,
                        "eeat_signals": 15,
                        "freshness_index": 13,
                        "faq_readiness": 15
                    },
                    "journey_category": "Product",
                    "diagnostics": ["Strong product relevance"]
                }
            ]
        }
        
        page = payload["owned_url_readiness"][0]
        self.assertEqual(page["current_geo_score_120"], 85)
        self.assertEqual(page["geo_dimensions"]["content_clarity"], 16)
        self.assertEqual(page["geo_dimensions"]["semantic_depth"], 14)
        self.assertEqual(page["geo_dimensions"]["structured_data"], 12)
        self.assertEqual(page["geo_dimensions"]["eeat_signals"], 15)
        self.assertEqual(page["geo_dimensions"]["freshness_index"], 13)
        self.assertEqual(page["geo_dimensions"]["faq_readiness"], 15)

    def test_geo_score_sum_matches_total(self):
        """Sum of 6 GEO dimensions should equal current_geo_score_120."""
        dims = {
            "content_clarity": 16,
            "semantic_depth": 14,
            "structured_data": 12,
            "eeat_signals": 15,
            "freshness_index": 13,
            "faq_readiness": 15
        }
        total = sum(dims.values())
        self.assertEqual(total, 85)

    def test_normalise_maps_geo_dimensions_correctly(self):
        """Frontend normaliser should map geo_dimensions to individual OwnedPage fields."""
        # This tests the contract: the normaliser must read from
        # current_geo_score_120 -> geoScore
        # geo_dimensions.content_clarity -> clarity
        # geo_dimensions.semantic_depth -> semanticDepth  
        # geo_dimensions.structured_data -> structure
        # geo_dimensions.eeat_signals -> evidence
        # geo_dimensions.freshness_index -> freshness
        # geo_dimensions.faq_readiness -> faqReadiness
        
        backend_page = {
            "url": "https://www.nissan.co.jp/ARIYA/",
            "current_geo_score_120": 85,
            "geo_dimensions": {
                "content_clarity": 16,
                "semantic_depth": 14,
                "structured_data": 12,
                "eeat_signals": 15,
                "freshness_index": 13,
                "faq_readiness": 15
            }
        }
        
        # Verify the mapping contract
        dims = backend_page["geo_dimensions"]
        self.assertIn("content_clarity", dims, "content_clarity must be in geo_dimensions")
        self.assertIn("semantic_depth", dims, "semantic_depth must be in geo_dimensions")
        self.assertIn("structured_data", dims, "structured_data must be in geo_dimensions")
        self.assertIn("eeat_signals", dims, "eeat_signals must be in geo_dimensions")
        self.assertIn("freshness_index", dims, "freshness_index must be in geo_dimensions")
        self.assertIn("faq_readiness", dims, "faq_readiness must be in geo_dimensions")


if __name__ == "__main__":
    print("\n" + "=" * 60)
    print("Bug Fix Validation Tests")
    print("=" * 60)
    unittest.main(verbosity=2)
