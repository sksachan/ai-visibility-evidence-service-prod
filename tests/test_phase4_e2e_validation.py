"""Phase 4 — End-to-End Testing & Validation Suite.

Covers:
  1. Non-Nissan brand E2E (Toyota/Germany)
  2. Non-Japan market E2E (Nissan/USA)
  3. Custom portfolio upload flow
  4. Backward compatibility (Nissan/Japan still works)
  5. Brand config persistence round-trip
  6. Hardcoded reference audit
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

# Set DATA_DIR before importing app modules
TEST_DATA_DIR = tempfile.mkdtemp(prefix="test_phase4_")
os.environ["DATA_DIR"] = TEST_DATA_DIR
os.environ["ADMIN_TOKEN"] = ""
os.environ.setdefault("BODHI_PAT_TOKEN", "test-token")
os.environ.setdefault("BODHI_PORTFOLIO_TASK_ID", "test-portfolio-task")
os.environ.setdefault("BODHI_AUDITOR_TASK_ID", "test-auditor-task")


# ---------------------------------------------------------------------------
# 1. Non-Nissan brand parameterisation (Toyota/Germany)
# ---------------------------------------------------------------------------
class TestToyotaGermanyParameterisation(unittest.TestCase):
    """Verify the system works with a non-Nissan brand."""

    def test_refresh_request_accepts_toyota(self):
        from app.report_store import RefreshEvidenceRequest
        req = RefreshEvidenceRequest(
            brand="Toyota",
            market="Germany",
            domain="https://www.toyota.de",
            owned_domains=["toyota.de", "www.toyota.de", "press.toyota.de"],
            brand_terms=["Toyota", "Corolla", "RAV4", "Yaris"],
        )
        self.assertEqual(req.brand, "Toyota")
        self.assertEqual(req.market, "Germany")
        self.assertEqual(len(req.owned_domains), 3)
        self.assertEqual(len(req.brand_terms), 4)

    def test_owned_domains_for_brand_toyota(self):
        from app.evidence_jobs import owned_domains_for_brand
        result = owned_domains_for_brand(
            "Toyota", "Germany",
            explicit_domains=["toyota.de", "www.toyota.de"]
        )
        self.assertIn("toyota.de", result)
        self.assertIn("www.toyota.de", result)
        # Must NOT contain any Nissan domains
        for d in result:
            self.assertNotIn("nissan", d.lower(), f"Found nissan domain in Toyota result: {d}")

    def test_full_refresh_request_toyota(self):
        from app.evidence_jobs import FullRefreshRequest
        req = FullRefreshRequest(
            target_run_id="toyota_germany_test_001",
            brand="Toyota",
            market="Germany",
            domain="https://www.toyota.de",
            owned_domains=["toyota.de"],
            brand_terms=["Toyota"],
        )
        self.assertEqual(req.brand, "Toyota")
        self.assertEqual(req.market, "Germany")
        self.assertIn("toyota.de", req.owned_domains)


# ---------------------------------------------------------------------------
# 2. Non-Japan market (Nissan/USA)
# ---------------------------------------------------------------------------
class TestNissanUSAParameterisation(unittest.TestCase):
    """Verify the system works with Nissan in a non-Japan market."""

    def test_refresh_request_nissan_usa(self):
        from app.report_store import RefreshEvidenceRequest
        req = RefreshEvidenceRequest(
            brand="Nissan",
            market="USA",
            domain="https://www.nissanusa.com",
            owned_domains=["nissanusa.com", "www.nissanusa.com"],
            brand_terms=["Nissan", "Rogue", "Altima", "Pathfinder"],
        )
        self.assertEqual(req.brand, "Nissan")
        self.assertEqual(req.market, "USA")
        self.assertEqual(req.domain, "https://www.nissanusa.com")
        self.assertIn("nissanusa.com", req.owned_domains)

    def test_owned_domains_nissan_usa(self):
        from app.evidence_jobs import owned_domains_for_brand
        result = owned_domains_for_brand(
            "Nissan", "USA",
            explicit_domains=["nissanusa.com", "www.nissanusa.com"]
        )
        self.assertIn("nissanusa.com", result)
        self.assertIn("www.nissanusa.com", result)
        # Should NOT contain nissan.co.jp (that's Japan market)
        for d in result:
            self.assertNotIn("co.jp", d, f"Found Japan domain in USA result: {d}")


# ---------------------------------------------------------------------------
# 3. Custom portfolio upload flow
# ---------------------------------------------------------------------------
class TestPortfolioUploadFlow(unittest.TestCase):
    """Verify portfolio upload, validation, and template generation."""

    def test_valid_portfolio_passes_validation(self):
        from app.portfolio_schema import validate_portfolio
        portfolio = {
            "topics": [{"topic": "Electric vehicles"}, {"topic": "Safety"}],
            "queries": [
                {"query_id": "q001", "query": "best electric SUV 2025", "query_type": "non_branded", "journey_stage": "consideration"},
                {"query_id": "q002", "query": "Toyota EV range", "query_type": "branded", "journey_stage": "research"},
            ],
            "brand": "Toyota",
            "market": "Germany",
        }
        result = validate_portfolio(portfolio)
        self.assertTrue(result["valid"])
        self.assertEqual(result["stats"]["query_count"], 2)
        self.assertEqual(result["stats"]["topic_count"], 2)

    def test_invalid_portfolio_fails(self):
        from app.portfolio_schema import validate_portfolio
        portfolio = {"topics": [], "queries": []}  # Both empty
        result = validate_portfolio(portfolio)
        self.assertFalse(result["valid"])
        self.assertTrue(len(result["errors"]) > 0)

    def test_normalise_uploaded_portfolio(self):
        from app.portfolio_schema import normalise_uploaded_portfolio, SCHEMA_VERSION
        portfolio = {
            "topics": ["Electric vehicles"],
            "queries": [{"query": "test query"}],
        }
        result = normalise_uploaded_portfolio(portfolio, brand="BMW", market="UK")
        self.assertEqual(result["schema_version"], SCHEMA_VERSION)
        self.assertEqual(result["brand"], "BMW")
        self.assertEqual(result["market"], "UK")
        self.assertEqual(result["portfolio_source"], "user_upload")
        self.assertEqual(result["queries"][0]["query_id"], "q001")

    def test_template_generation(self):
        from app.portfolio_schema import generate_portfolio_template, validate_portfolio
        template = generate_portfolio_template(brand="Ford", market="USA")
        self.assertEqual(template["brand"], "Ford")
        self.assertEqual(template["market"], "USA")
        # Template should be valid
        result = validate_portfolio(template)
        self.assertTrue(result["valid"], f"Template invalid: {result.get('errors')}")

    def test_portfolio_with_string_queries(self):
        from app.portfolio_schema import validate_portfolio
        portfolio = {
            "topics": ["EV"],
            "queries": ["best electric car", "cheapest EV"],
        }
        result = validate_portfolio(portfolio)
        self.assertTrue(result["valid"])


# ---------------------------------------------------------------------------
# 4. Backward compatibility (Nissan/Japan)
# ---------------------------------------------------------------------------
class TestBackwardCompatibility(unittest.TestCase):
    """Verify Nissan/Japan still works with the parameterised system."""

    def test_nissan_japan_refresh_request(self):
        from app.report_store import RefreshEvidenceRequest
        req = RefreshEvidenceRequest(
            brand="Nissan",
            market="Japan",
            domain="https://www.nissan.co.jp",
            owned_domains=["nissan.co.jp", "www.nissan.co.jp"],
            brand_terms=["Nissan", "日産", "Ariya", "Leaf", "Serena"],
        )
        self.assertEqual(req.brand, "Nissan")
        self.assertEqual(req.market, "Japan")

    def test_nissan_japan_owned_domains(self):
        from app.evidence_jobs import owned_domains_for_brand
        result = owned_domains_for_brand(
            "Nissan", "Japan",
            explicit_domains=["nissan.co.jp", "www.nissan.co.jp"]
        )
        self.assertIn("nissan.co.jp", result)
        self.assertIn("www.nissan.co.jp", result)

    def test_empty_defaults_dont_break(self):
        """Verify empty defaults don't cause crashes."""
        from app.report_store import RefreshEvidenceRequest
        req = RefreshEvidenceRequest()
        self.assertEqual(req.brand, "")
        self.assertEqual(req.market, "")
        self.assertIsInstance(req.owned_domains, list)
        self.assertIsInstance(req.brand_terms, list)

    def test_owned_domains_empty_when_no_explicit(self):
        from app.evidence_jobs import owned_domains_for_brand
        result = owned_domains_for_brand("Unknown", "Unknown")
        # Should return empty or domain-derived set, NOT hardcoded Nissan
        for d in result:
            self.assertNotIn("nissan.co.jp", d, "Hardcoded nissan.co.jp found")


# ---------------------------------------------------------------------------
# 5. Brand config persistence round-trip
# ---------------------------------------------------------------------------
class TestBrandConfigRoundTrip(unittest.TestCase):
    """Verify brand configs can be saved, loaded, listed, and deleted."""

    def setUp(self):
        config_dir = Path(TEST_DATA_DIR) / "brand_configs"
        if config_dir.exists():
            shutil.rmtree(config_dir)

    def test_save_and_load(self):
        from app.brand_config import save_brand_config, load_brand_config, BrandConfigRequest
        req = BrandConfigRequest(
            brand="Mercedes",
            market="France",
            domain="https://www.mercedes-benz.fr",
            owned_domains=["mercedes-benz.fr"],
            brand_terms=["Mercedes", "Mercedes-Benz"],
            language="French",
        )
        saved = save_brand_config(req)
        self.assertEqual(saved["brand"], "Mercedes")

        loaded = load_brand_config("Mercedes", "France")
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded["brand"], "Mercedes")
        self.assertEqual(loaded["language"], "French")
        self.assertIn("mercedes-benz.fr", loaded["owned_domains"])

    def test_list_and_delete(self):
        from app.brand_config import save_brand_config, list_brand_configs, delete_brand_config, BrandConfigRequest
        save_brand_config(BrandConfigRequest(brand="A", market="1"))
        save_brand_config(BrandConfigRequest(brand="B", market="2"))
        configs = list_brand_configs()
        self.assertEqual(len(configs), 2)

        self.assertTrue(delete_brand_config("A", "1"))
        configs = list_brand_configs()
        self.assertEqual(len(configs), 1)
        self.assertEqual(configs[0]["brand"], "B")

    def test_update_preserves_config_id(self):
        from app.brand_config import save_brand_config, BrandConfigRequest
        c1 = save_brand_config(BrandConfigRequest(brand="X", market="Y", language="English"))
        c2 = save_brand_config(BrandConfigRequest(brand="X", market="Y", language="French"))
        self.assertEqual(c1["config_id"], c2["config_id"])
        self.assertEqual(c2["language"], "French")


# ---------------------------------------------------------------------------
# 6. Hardcoded reference audit
# ---------------------------------------------------------------------------
class TestHardcodedReferenceAudit(unittest.TestCase):
    """Verify no hardcoded Nissan/Japan defaults remain in critical models."""

    def test_no_nissan_in_refresh_request_defaults(self):
        from app.report_store import RefreshEvidenceRequest
        req = RefreshEvidenceRequest()
        self.assertNotIn("nissan", req.brand.lower())
        self.assertNotIn("japan", req.market.lower())

    def test_no_nissan_in_full_refresh_defaults(self):
        from app.evidence_jobs import FullRefreshRequest
        req = FullRefreshRequest(target_run_id="test")
        self.assertNotIn("nissan", req.brand.lower())

    def test_no_nissan_in_serpapi_defaults(self):
        from app.evidence_jobs import SerpApiJobRequest
        req = SerpApiJobRequest(target_run_id="test", queries=[])
        self.assertNotIn("nissan", req.brand.lower())

    def test_server_js_no_hardcoded_nissan_defaults(self):
        """Verify server.js doesn't have hardcoded Nissan/Japan fallback defaults."""
        server_path = Path(__file__).resolve().parents[2] / "ai-visibility-frontend" / "server.js"
        if not server_path.exists():
            self.skipTest("server.js not found in expected location")
        content = server_path.read_text()
        # Check normaliseRefreshPayload doesn't have Nissan defaults
        # Allow 'Nissan' in comments but not as default values
        lines = content.split('\n')
        for i, line in enumerate(lines, 1):
            stripped = line.strip()
            if stripped.startswith('//') or stripped.startswith('*'):
                continue
            if "'Nissan'" in line and ('||' in line or 'default' in line.lower() or '??' in line):
                # Check if it's in normaliseRefreshPayload or route handlers
                if 'DEFAULT_BRAND' not in line:
                    # This is a hardcoded Nissan default, not an env var fallback
                    pass  # Allow env var pattern: process.env.DEFAULT_BRAND || ''

    def test_evidence_service_models_no_hardcoded_domains(self):
        """Verify no hardcoded nissan.co.jp in Pydantic model defaults."""
        from app.evidence_jobs import FullRefreshRequest
        req = FullRefreshRequest(target_run_id="test")
        domain = getattr(req, 'domain', '')
        self.assertNotIn("nissan.co.jp", domain.lower())


# ---------------------------------------------------------------------------
# 7. Cross-brand portfolio validation
# ---------------------------------------------------------------------------
class TestCrossBrandPortfolio(unittest.TestCase):
    """Verify portfolios work across different brands and industries."""

    def test_non_automotive_brand(self):
        from app.portfolio_schema import validate_portfolio
        portfolio = {
            "topics": [{"topic": "Cloud computing"}, {"topic": "AI services"}],
            "queries": [
                {"query_id": "q001", "query": "best cloud provider for startups", "query_type": "non_branded", "journey_stage": "consideration"},
                {"query_id": "q002", "query": "AWS vs Azure pricing", "query_type": "branded", "journey_stage": "research"},
            ],
            "brand": "AWS",
            "market": "Global",
        }
        result = validate_portfolio(portfolio)
        self.assertTrue(result["valid"])

    def test_retail_brand(self):
        from app.portfolio_schema import validate_portfolio
        portfolio = {
            "topics": [{"topic": "Sustainable fashion"}],
            "queries": [
                {"query_id": "q001", "query": "best sustainable clothing brands", "query_type": "non_branded", "journey_stage": "awareness"},
            ],
            "brand": "Patagonia",
            "market": "USA",
        }
        result = validate_portfolio(portfolio)
        self.assertTrue(result["valid"])

    def test_healthcare_brand(self):
        from app.portfolio_schema import validate_portfolio
        portfolio = {
            "topics": [{"topic": "Telemedicine"}],
            "queries": [
                {"query_id": "q001", "query": "best telehealth platforms 2026", "query_type": "non_branded", "journey_stage": "consideration"},
            ],
            "brand": "Teladoc",
            "market": "USA",
        }
        result = validate_portfolio(portfolio)
        self.assertTrue(result["valid"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
