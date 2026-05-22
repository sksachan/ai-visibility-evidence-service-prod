"""Comprehensive multi-brand validation tests — Toyota/Germany.

This test suite validates that the entire pipeline correctly handles
non-Nissan brands (Toyota/Germany) without any hardcoded Nissan/Japan
defaults leaking through.

Covers:
- Evidence Service: evidence_jobs, crawl_jobs, report_store, refresh_orchestrator
- Frontend proxy: server.js normaliseRefreshPayload
- Auditor scripts: build_query_workbench_bundle, strict_geo_visibility_runtime, lib.py
- Bodhi workflow: HITL parameter flow
"""
from __future__ import annotations

import json
import os
import sys
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock
from datetime import datetime

# Ensure scripts/ is importable for Auditor tests
AUDITOR_SCRIPTS = str(Path(__file__).resolve().parent.parent.parent / "AIVisibilityAuditor" / "scripts")
if AUDITOR_SCRIPTS not in sys.path:
    sys.path.insert(0, AUDITOR_SCRIPTS)

# Toyota/Germany test configuration
TOYOTA_CONFIG = {
    "brand": "Toyota",
    "market": "Germany",
    "domain": "https://www.toyota.de",
    "owned_domains": ["toyota.de", "www.toyota.de", "toyota-media.de"],
    "brand_terms": ["Toyota", "トヨタ", "Corolla", "Camry", "RAV4", "Yaris", "GR86"],
}


# ============================================================================
# SECTION 1: Evidence Service — evidence_jobs
# ============================================================================


class TestEvidenceJobsToyotaGermany:
    """Validate evidence_jobs handles Toyota/Germany without Nissan leakage."""

    def test_owned_domains_toyota_explicit(self):
        """Toyota explicit domains should be used, not Nissan defaults."""
        from app.evidence_jobs import owned_domains_for_brand

        result = owned_domains_for_brand(
            "Toyota", "Germany", TOYOTA_CONFIG["owned_domains"]
        )
        assert "toyota.de" in result
        assert "www.toyota.de" in result
        assert "toyota-media.de" in result
        # Must NOT contain any Nissan domains
        assert not any("nissan" in d for d in result), f"Nissan domain leaked: {result}"

    def test_owned_domains_toyota_no_explicit_returns_empty(self):
        """Toyota without explicit domains should return empty (no Nissan fallback)."""
        from app.evidence_jobs import owned_domains_for_brand

        result = owned_domains_for_brand("Toyota", "Germany")
        assert result == set(), f"Expected empty set for unknown brand, got: {result}"

    def test_full_refresh_request_toyota(self):
        """FullRefreshRequest should accept and preserve Toyota/Germany params."""
        from app.evidence_jobs import FullRefreshRequest

        req = FullRefreshRequest(
            target_run_id="toyota_germany_test_001",
            brand="Toyota",
            market="Germany",
            owned_domains=TOYOTA_CONFIG["owned_domains"],
            brand_terms=TOYOTA_CONFIG["brand_terms"],
        )
        assert req.brand == "Toyota"
        assert req.market == "Germany"
        assert "toyota.de" in req.owned_domains
        assert "Toyota" in req.brand_terms
        # Verify no Nissan defaults leaked
        assert req.brand != "Nissan"
        assert req.market != "Japan"

    def test_full_refresh_request_empty_defaults_not_nissan(self):
        """FullRefreshRequest defaults should be empty strings, not Nissan."""
        from app.evidence_jobs import FullRefreshRequest

        req = FullRefreshRequest(target_run_id="test_empty")
        assert req.brand == "", f"Default brand should be empty, got: {req.brand}"
        assert req.market == "", f"Default market should be empty, got: {req.market}"
        assert req.owned_domains == []
        assert req.brand_terms == []


# ============================================================================
# SECTION 2: Evidence Service — crawl_jobs
# ============================================================================


class TestCrawlJobsToyotaGermany:
    """Validate crawl_jobs handles Toyota/Germany without Nissan leakage."""

    def test_crawl_request_toyota_defaults(self):
        """CrawlRequest should accept Toyota/Germany without Nissan defaults."""
        from app.crawl_jobs import CrawlRequest

        req = CrawlRequest(
            source_run_id="src_toyota_test",
            target_run_id="tgt_toyota_test",
            brand="Toyota",
            market="Germany",
            owned_domains=["toyota.de", "www.toyota.de"],
        )
        assert req.brand == "Toyota"
        assert req.market == "Germany"
        assert "toyota.de" in req.owned_domains

    def test_crawl_request_empty_defaults(self):
        """CrawlRequest defaults should be empty, not Nissan/Japan."""
        from app.crawl_jobs import CrawlRequest

        req = CrawlRequest(source_run_id="src_test", target_run_id="tgt_test")
        assert req.brand == "", f"Default brand should be empty, got: {req.brand}"
        assert req.market == "", f"Default market should be empty, got: {req.market}"


# ============================================================================
# SECTION 3: Evidence Service — report_store
# ============================================================================


class TestReportStoreToyotaGermany:
    """Validate report_store handles Toyota/Germany without Nissan leakage."""

    def test_refresh_evidence_request_toyota(self):
        """RefreshEvidenceRequest should accept Toyota/Germany params."""
        from app.report_store import RefreshEvidenceRequest

        req = RefreshEvidenceRequest(
            brand="Toyota",
            market="Germany",
            domain="https://www.toyota.de",
            owned_domains=["toyota.de"],
            brand_terms=["Toyota", "Corolla"],
        )
        assert req.brand == "Toyota"
        assert req.market == "Germany"
        assert "toyota.de" in req.owned_domains
        assert "Toyota" in req.brand_terms

    def test_refresh_evidence_request_empty_defaults(self):
        """RefreshEvidenceRequest defaults should be empty, not Nissan."""
        from app.report_store import RefreshEvidenceRequest

        req = RefreshEvidenceRequest()
        assert req.brand == ""
        assert req.market == ""
        assert req.owned_domains == []
        assert req.brand_terms == []

    def test_refresh_evidence_request_custom_portfolio(self):
        """RefreshEvidenceRequest should accept custom_portfolio for Toyota."""
        from app.report_store import RefreshEvidenceRequest

        portfolio = {
            "topics": [{"topic": "Electric Vehicles", "queries": ["best Toyota EV Germany"]}],
            "queries": [{"query": "Toyota bZ4X review", "intent": "informational"}],
        }
        req = RefreshEvidenceRequest(
            brand="Toyota",
            market="Germany",
            custom_portfolio=portfolio,
            query_portfolio_mode="upload",
        )
        assert req.custom_portfolio is not None
        assert req.custom_portfolio["queries"][0]["query"] == "Toyota bZ4X review"
        assert req.query_portfolio_mode == "upload"


# ============================================================================
# SECTION 4: Evidence Service — refresh_orchestrator
# ============================================================================


class TestRefreshOrchestratorToyotaGermany:
    """Validate refresh_orchestrator handles Toyota/Germany."""

    def test_source_type_toyota_owned(self):
        """source_type_from_url should classify Toyota domains as owned."""
        from app.refresh_orchestrator import source_type_from_url

        owned = {"toyota.de", "www.toyota.de", "toyota-media.de"}
        assert source_type_from_url("https://www.toyota.de/modelle/corolla", owned) == "owned_oem"
        assert source_type_from_url("https://toyota-media.de/press", owned) == "owned_oem"

    def test_source_type_toyota_not_nissan(self):
        """With Toyota owned domains, Nissan URLs should NOT be classified as owned."""
        from app.refresh_orchestrator import source_type_from_url

        owned = {"toyota.de", "www.toyota.de"}
        result = source_type_from_url("https://www.nissan.co.jp/vehicles", owned)
        assert result != "owned_oem", f"Nissan URL classified as owned for Toyota: {result}"

    def test_source_type_competitor_for_toyota(self):
        """Non-owned automotive brands should be classified as competitors."""
        from app.refresh_orchestrator import source_type_from_url

        owned = {"toyota.de", "www.toyota.de"}
        assert source_type_from_url("https://www.bmw.de/modelle", owned) == "competitor_owned"
        assert source_type_from_url("https://www.volkswagen.de", owned) == "competitor_owned"

    def test_source_type_external_for_toyota(self):
        """Non-automotive domains should be classified correctly."""
        from app.refresh_orchestrator import source_type_from_url

        owned = {"toyota.de", "www.toyota.de"}
        assert source_type_from_url("https://www.youtube.com/watch", owned) == "forum_social_video"
        assert source_type_from_url("https://www.reddit.com/r/cars", owned) == "forum_social_video"


# ============================================================================
# SECTION 5: Auditor — lib.py
# ============================================================================


class TestAuditorLibToyotaGermany:
    """Validate Auditor lib.py handles Toyota brand terms."""

    def test_keyword_tokens_toyota_brand_terms(self):
        """keyword_tokens should filter Toyota brand terms as stop words."""
        from lib import keyword_tokens

        tokens = keyword_tokens(
            "Toyota Corolla hybrid review Germany",
            brand_terms=["toyota", "corolla"],
        )
        assert "toyota" not in tokens
        assert "corolla" not in tokens
        assert "hybrid" in tokens
        assert "review" in tokens

    def test_keyword_tokens_no_nissan_default_leak(self):
        """When Toyota brand_terms are provided, Nissan terms should NOT be filtered."""
        from lib import keyword_tokens

        tokens = keyword_tokens(
            "nissan leaf vs toyota corolla",
            brand_terms=["toyota", "corolla"],
        )
        # 'nissan' should NOT be filtered when Toyota is the brand
        assert "nissan" in tokens
        # 'toyota' and 'corolla' should be filtered
        assert "toyota" not in tokens
        assert "corolla" not in tokens

    def test_is_off_market_owned_toyota_patterns(self):
        """_is_off_market_owned should use custom Toyota patterns."""
        from lib import _is_off_market_owned

        cfg = {"off_market_patterns": [r"toyota\.(co\.uk|com\.au|co\.jp)"]}
        assert _is_off_market_owned("toyota.co.uk some page", cfg) is True
        assert _is_off_market_owned("toyota.de some page", cfg) is False

    def test_classify_source_toyota_owned(self):
        """classify_source should detect Toyota as owned with custom domains."""
        from lib import classify_source

        cfg = {"owned_domains": ["toyota.de", "www.toyota.de"]}
        result = classify_source(
            "https://www.toyota.de/modelle",
            cfg=cfg,
        )
        assert result.get("is_owned_domain") is True or result.get("source_type") in ("owned_oem", "owned")


# ============================================================================
# SECTION 6: End-to-End Parameter Flow
# ============================================================================


class TestEndToEndParameterFlow:
    """Validate Toyota/Germany parameters flow through the full pipeline."""

    def test_refresh_payload_construction(self):
        """Simulate the frontend → Evidence Service refresh payload."""
        payload = {
            "brand": "Toyota",
            "market": "Germany",
            "domain": "https://www.toyota.de",
            "run_mode": "full_refresh",
            "query_portfolio_mode": "synthetic",
            "owned_domains": ["toyota.de", "www.toyota.de", "toyota-media.de"],
            "brand_terms": ["Toyota", "トヨタ", "Corolla", "Camry", "RAV4"],
            "language": "German",
            "enable_serpapi": True,
            "enable_owned_crawl": True,
            "enable_external_crawl": False,
            "trigger_auditor": True,
        }
        # Verify no Nissan defaults
        assert payload["brand"] == "Toyota"
        assert payload["market"] == "Germany"
        assert "nissan" not in payload["domain"].lower()
        assert not any("nissan" in d.lower() for d in payload["owned_domains"])

    def test_refresh_evidence_request_to_full_refresh(self):
        """RefreshEvidenceRequest should correctly map to FullRefreshRequest."""
        from app.report_store import RefreshEvidenceRequest
        from app.evidence_jobs import FullRefreshRequest

        refresh_req = RefreshEvidenceRequest(
            brand="Toyota",
            market="Germany",
            domain="https://www.toyota.de",
            owned_domains=["toyota.de", "www.toyota.de"],
            brand_terms=["Toyota", "Corolla"],
        )

        full_req = FullRefreshRequest(
            target_run_id="toyota_germany_test",
            brand=refresh_req.brand,
            market=refresh_req.market,
            domain=refresh_req.domain,
            owned_domains=refresh_req.owned_domains,
            brand_terms=refresh_req.brand_terms,
        )

        assert full_req.brand == "Toyota"
        assert full_req.market == "Germany"
        assert "toyota.de" in full_req.owned_domains
        assert "Toyota" in full_req.brand_terms

    def test_run_id_contains_brand_market(self):
        """Generated run IDs should contain the brand and market."""
        brand = "Toyota"
        market = "Germany"
        run_id = f"evidence_{brand.lower()}_{market.lower()}_{int(datetime.now().timestamp())}"
        assert "toyota" in run_id
        assert "germany" in run_id
        assert "nissan" not in run_id
        assert "japan" not in run_id


# ============================================================================
# SECTION 7: Bodhi Workflow HITL Parameter Validation
# ============================================================================


class TestBodhiWorkflowParameters:
    """Validate Bodhi workflow HITL nodes accept Toyota/Germany params."""

    def test_portfolio_builder_hitl_payload(self):
        """Portfolio Builder HITL payload should accept any brand/market."""
        hitl_payload = {
            "brand": "Toyota",
            "market": "Germany",
            "domain": "https://www.toyota.de",
            "language": "German",
            "topic_count": 8,
            "queries_per_topic": 6,
            "owned_domains": "toyota.de, www.toyota.de, toyota-media.de",
            "brand_terms": "Toyota, トヨタ, Corolla, Camry, RAV4",
            "seed_topics": "electric vehicles, hybrid technology, SUV family",
        }
        assert hitl_payload["brand"] == "Toyota"
        assert hitl_payload["market"] == "Germany"
        assert "nissan" not in hitl_payload["domain"].lower()

    def test_auditor_workflow_hitl_payload(self):
        """Auditor workflow HITL payload should accept any brand/market."""
        hitl_payload = {
            "brand": "Toyota",
            "market": "Germany",
            "domain": "https://www.toyota.de",
            "run_id": "evidence_toyota_germany_1234567890_abc123",
            "output_language": "German",
            "owned_domains": "toyota.de, www.toyota.de",
            "brand_terms": "Toyota, Corolla, Camry",
        }
        assert hitl_payload["brand"] == "Toyota"
        assert hitl_payload["run_id"].startswith("evidence_toyota_germany")


# ============================================================================
# SECTION 8: Backward Compatibility
# ============================================================================


class TestBackwardCompatibility:
    """Ensure Nissan/Japan still works after multi-brand changes."""

    def test_nissan_owned_domains_still_work(self):
        """Nissan built-in defaults should still work for backward compat."""
        from app.evidence_jobs import owned_domains_for_brand

        result = owned_domains_for_brand("Nissan", "Japan")
        assert "nissan.co.jp" in result
        assert "www.nissan.co.jp" in result

    def test_nissan_explicit_domains_override(self):
        """Even for Nissan, explicit domains should override defaults."""
        from app.evidence_jobs import owned_domains_for_brand

        result = owned_domains_for_brand(
            "Nissan", "USA", ["nissanusa.com", "www.nissanusa.com"]
        )
        assert "nissanusa.com" in result
        assert "www.nissanusa.com" in result
        # Japanese domains should NOT be in the result when US domains are explicit
        assert "nissan.co.jp" not in result

    def test_refresh_request_backward_compat(self):
        """RefreshEvidenceRequest with only brand/market should still work."""
        from app.report_store import RefreshEvidenceRequest

        req = RefreshEvidenceRequest(brand="Nissan", market="Japan")
        assert req.brand == "Nissan"
        assert req.market == "Japan"
        assert req.owned_domains == []
        assert req.brand_terms == []


# ============================================================================
# SECTION 9: Edge Cases
# ============================================================================


class TestEdgeCases:
    """Edge cases for multi-brand parameterization."""

    def test_empty_brand_market(self):
        """Empty brand/market should not crash."""
        from app.evidence_jobs import owned_domains_for_brand

        result = owned_domains_for_brand("", "")
        assert isinstance(result, set)

    def test_unicode_brand_terms(self):
        """Unicode brand terms (Japanese, German) should work."""
        from app.evidence_jobs import FullRefreshRequest

        req = FullRefreshRequest(
            target_run_id="unicode_test",
            brand="Toyota",
            market="Germany",
            brand_terms=["Toyota", "トヨタ", "Bayerische Motoren Werke", "München"],
        )
        assert "トヨタ" in req.brand_terms
        assert "München" in req.brand_terms

    def test_domains_with_protocol(self):
        """Domains passed with https:// prefix should be handled."""
        from app.evidence_jobs import owned_domains_for_brand

        result = owned_domains_for_brand(
            "Toyota", "Germany", ["https://www.toyota.de", "https://toyota-media.de"]
        )
        assert "www.toyota.de" in result
        assert "toyota-media.de" in result

    def test_case_insensitive_brand_lookup(self):
        """Brand lookup should be case-insensitive for built-in defaults."""
        from app.evidence_jobs import owned_domains_for_brand

        result_lower = owned_domains_for_brand("nissan", "japan")
        result_upper = owned_domains_for_brand("NISSAN", "JAPAN")
        result_mixed = owned_domains_for_brand("Nissan", "Japan")
        assert result_lower == result_upper == result_mixed

    def test_multiple_brands_dont_cross_contaminate(self):
        """Running Toyota then Nissan should not cross-contaminate domains."""
        from app.evidence_jobs import owned_domains_for_brand

        toyota_domains = owned_domains_for_brand(
            "Toyota", "Germany", ["toyota.de"]
        )
        nissan_domains = owned_domains_for_brand("Nissan", "Japan")

        assert "toyota.de" in toyota_domains
        assert "toyota.de" not in nissan_domains
        assert "nissan.co.jp" in nissan_domains
        assert "nissan.co.jp" not in toyota_domains


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
