"""Tests for multi-brand / multi-market support.

Verifies that the evidence service correctly handles:
- Dynamic owned_domains from request parameters
- Brand-agnostic defaults (no hardcoded Nissan fallback)
- Custom portfolio upload and storage
- Backward compatibility with known brand defaults
"""
from __future__ import annotations

import json
import pytest
from unittest.mock import patch, MagicMock
from pathlib import Path


# ---------------------------------------------------------------------------
# owned_domains_for_brand
# ---------------------------------------------------------------------------


def test_owned_domains_explicit_domains():
    """When explicit domains are provided, they should be used directly."""
    from app.evidence_jobs import owned_domains_for_brand

    result = owned_domains_for_brand("Toyota", "USA", ["toyota.com", "toyotausa.com"])
    assert "toyota.com" in result
    assert "www.toyota.com" in result
    assert "toyotausa.com" in result
    assert "www.toyotausa.com" in result
    # Should NOT contain Nissan domains
    assert "nissan.co.jp" not in result


def test_owned_domains_explicit_with_urls():
    """Explicit domains can be passed as full URLs; netloc should be extracted."""
    from app.evidence_jobs import owned_domains_for_brand

    result = owned_domains_for_brand("BMW", "Germany", ["https://www.bmw.de", "bmw-motorrad.de"])
    assert "www.bmw.de" in result
    assert "bmw-motorrad.de" in result


def test_owned_domains_nissan_backward_compat():
    """When no explicit domains are provided for Nissan, built-in defaults are used."""
    from app.evidence_jobs import owned_domains_for_brand

    result = owned_domains_for_brand("Nissan", "Japan")
    assert "nissan.co.jp" in result
    assert "www.nissan.co.jp" in result
    assert "nissan-global.com" in result


def test_owned_domains_unknown_brand_returns_empty():
    """Unknown brands without explicit domains should return empty set."""
    from app.evidence_jobs import owned_domains_for_brand

    result = owned_domains_for_brand("UnknownBrand", "Mars")
    assert result == set()


def test_owned_domains_empty_list_returns_empty():
    """Empty explicit domains list should fall through to defaults."""
    from app.evidence_jobs import owned_domains_for_brand

    result = owned_domains_for_brand("UnknownBrand", "Mars", [])
    assert result == set()


def test_owned_domains_case_insensitive():
    """Brand lookup should be case-insensitive."""
    from app.evidence_jobs import owned_domains_for_brand

    result_lower = owned_domains_for_brand("nissan", "Japan")
    result_upper = owned_domains_for_brand("NISSAN", "Japan")
    assert result_lower == result_upper


# ---------------------------------------------------------------------------
# FullRefreshRequest schema
# ---------------------------------------------------------------------------


def test_full_refresh_request_has_owned_domains_field():
    """FullRefreshRequest should accept owned_domains and brand_terms."""
    from app.evidence_jobs import FullRefreshRequest

    req = FullRefreshRequest(
        target_run_id="test_run",
        brand="Toyota",
        market="USA",
        owned_domains=["toyota.com"],
        brand_terms=["Toyota", "Lexus"],
    )
    assert req.owned_domains == ["toyota.com"]
    assert req.brand_terms == ["Toyota", "Lexus"]
    assert req.brand == "Toyota"


def test_full_refresh_request_defaults_empty():
    """FullRefreshRequest brand/market should default to empty string."""
    from app.evidence_jobs import FullRefreshRequest

    req = FullRefreshRequest(target_run_id="test_run")
    assert req.brand == ""
    assert req.market == ""
    assert req.owned_domains == []
    assert req.brand_terms == []


# ---------------------------------------------------------------------------
# RefreshEvidenceRequest schema
# ---------------------------------------------------------------------------


def test_refresh_evidence_request_has_multi_brand_fields():
    """RefreshEvidenceRequest should accept owned_domains, brand_terms, custom_portfolio."""
    from app.report_store import RefreshEvidenceRequest

    req = RefreshEvidenceRequest(
        brand="BMW",
        market="Germany",
        owned_domains=["bmw.de"],
        brand_terms=["BMW", "Bayerische Motoren Werke"],
        custom_portfolio={"topics": [{"topic": "EV"}], "queries": [{"query": "best BMW EV"}]},
    )
    assert req.owned_domains == ["bmw.de"]
    assert req.brand_terms == ["BMW", "Bayerische Motoren Werke"]
    assert req.custom_portfolio is not None
    assert req.custom_portfolio["queries"][0]["query"] == "best BMW EV"


def test_refresh_evidence_request_defaults_empty():
    """RefreshEvidenceRequest brand/market should default to empty string."""
    from app.report_store import RefreshEvidenceRequest

    req = RefreshEvidenceRequest()
    assert req.brand == ""
    assert req.market == ""
    assert req.owned_domains == []
    assert req.brand_terms == []
    assert req.custom_portfolio is None


# ---------------------------------------------------------------------------
# source_type_from_url with owned_domains
# ---------------------------------------------------------------------------


def test_source_type_owned_with_explicit_domains():
    """source_type_from_url should classify owned domains correctly."""
    from app.refresh_orchestrator import source_type_from_url

    owned = {"toyota.com", "www.toyota.com"}
    assert source_type_from_url("https://www.toyota.com/camry", owned) == "owned_oem"
    assert source_type_from_url("https://www.honda.com/civic", owned) == "competitor_owned"
    assert source_type_from_url("https://www.reddit.com/r/cars", owned) == "forum_social_video"


def test_source_type_backward_compat_no_domains():
    """Without explicit domains, source_type_from_url uses pattern matching."""
    from app.refresh_orchestrator import source_type_from_url

    # Nissan is still detected via the backward-compatible pattern matching
    assert source_type_from_url("https://www.nissan.co.jp/vehicles") == "competitor_owned"
    assert source_type_from_url("https://www.youtube.com/watch") == "forum_social_video"


# ---------------------------------------------------------------------------
# lib.py keyword_tokens with brand_terms
# ---------------------------------------------------------------------------


def test_keyword_tokens_custom_brand_terms():
    """keyword_tokens should use custom brand_terms as stop words."""
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    from lib import keyword_tokens

    # With Toyota brand terms, 'toyota' should be filtered out
    tokens = keyword_tokens("Toyota Camry hybrid review", brand_terms=["toyota"])
    assert "toyota" not in tokens
    assert "camry" in tokens
    assert "hybrid" in tokens


def test_keyword_tokens_default_brand_terms():
    """Without brand_terms, keyword_tokens should use Nissan defaults."""
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    from lib import keyword_tokens

    tokens = keyword_tokens("nissan leaf electric vehicle")
    assert "nissan" not in tokens  # filtered by default stop words
    assert "leaf" in tokens
    assert "electric" in tokens


# ---------------------------------------------------------------------------
# lib.py _is_off_market_owned
# ---------------------------------------------------------------------------


def test_is_off_market_owned_custom_patterns():
    """_is_off_market_owned should use custom patterns from config."""
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    from lib import _is_off_market_owned

    cfg = {"off_market_patterns": [r"toyota\.(co\.uk|com\.au)"]}
    assert _is_off_market_owned("toyota.co.uk some page", cfg) is True
    assert _is_off_market_owned("toyota.co.jp some page", cfg) is False


def test_is_off_market_owned_default_nissan():
    """Without custom patterns, _is_off_market_owned uses Nissan defaults."""
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    from lib import _is_off_market_owned

    assert _is_off_market_owned("nissanusa.com some page") is True
    assert _is_off_market_owned("toyota.com some page") is False
