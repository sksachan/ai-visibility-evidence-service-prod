"""Sprint 1 — Multi-Brand/Multi-Market Parameterisation Tests.

Verifies that hardcoded Nissan/Japan defaults have been removed and
that owned_domains/brand_terms flow through the refresh pipeline.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# 1. RefreshEvidenceRequest has owned_domains and brand_terms fields
# ---------------------------------------------------------------------------
def test_refresh_request_schema():
    from app.report_store import RefreshEvidenceRequest
    req = RefreshEvidenceRequest()
    assert req.brand == "", f"Expected empty default brand, got '{req.brand}'"
    assert req.market == "", f"Expected empty default market, got '{req.market}'"
    assert hasattr(req, 'owned_domains'), "RefreshEvidenceRequest missing owned_domains field"
    assert hasattr(req, 'brand_terms'), "RefreshEvidenceRequest missing brand_terms field"
    assert isinstance(req.owned_domains, list), "owned_domains should be a list"
    assert isinstance(req.brand_terms, list), "brand_terms should be a list"
    print("  ✓ RefreshEvidenceRequest schema has owned_domains and brand_terms")


def test_refresh_request_with_values():
    from app.report_store import RefreshEvidenceRequest
    req = RefreshEvidenceRequest(
        brand="Toyota",
        market="Germany",
        domain="https://www.toyota.de",
        owned_domains=["toyota.de", "www.toyota.de", "press.toyota.de"],
        brand_terms=["Toyota", "トヨタ", "Corolla", "RAV4"],
    )
    assert req.brand == "Toyota"
    assert req.market == "Germany"
    assert len(req.owned_domains) == 3
    assert len(req.brand_terms) == 4
    print("  ✓ RefreshEvidenceRequest accepts Toyota/Germany values")


# ---------------------------------------------------------------------------
# 2. FullRefreshRequest has owned_domains and brand_terms fields
# ---------------------------------------------------------------------------
def test_full_refresh_request_schema():
    from app.evidence_jobs import FullRefreshRequest
    req = FullRefreshRequest(target_run_id="test_run")
    assert req.brand == "", f"Expected empty default brand, got '{req.brand}'"
    assert req.market == "", f"Expected empty default market, got '{req.market}'"
    assert hasattr(req, 'owned_domains'), "FullRefreshRequest missing owned_domains field"
    assert hasattr(req, 'brand_terms'), "FullRefreshRequest missing brand_terms field"
    print("  ✓ FullRefreshRequest schema has owned_domains and brand_terms")


# ---------------------------------------------------------------------------
# 3. owned_domains_for_brand is data-driven
# ---------------------------------------------------------------------------
def test_owned_domains_for_brand_explicit():
    from app.evidence_jobs import owned_domains_for_brand
    result = owned_domains_for_brand("Toyota", "Germany", ["toyota.de", "www.toyota.de"])
    assert "toyota.de" in result
    assert "www.toyota.de" in result
    # Should NOT contain any Nissan domains
    assert not any("nissan" in d for d in result), f"Found nissan in result: {result}"
    print("  ✓ owned_domains_for_brand returns explicit domains")


def test_owned_domains_for_brand_empty_when_no_explicit():
    from app.evidence_jobs import owned_domains_for_brand
    result = owned_domains_for_brand("Toyota", "Germany")
    # Should return empty set when no explicit domains and no hardcoded fallback
    assert not any("nissan" in d for d in result), f"Found nissan in result: {result}"
    print("  ✓ owned_domains_for_brand returns no hardcoded Nissan domains")


def test_owned_domains_for_brand_url_stripping():
    from app.evidence_jobs import owned_domains_for_brand
    result = owned_domains_for_brand("BMW", "USA", ["https://www.bmw.com", "bmwusa.com"])
    assert "www.bmw.com" in result or "bmw.com" in result
    assert "bmwusa.com" in result
    print("  ✓ owned_domains_for_brand strips URLs to domains")


# ---------------------------------------------------------------------------
# 4. Verify no hardcoded Nissan in key files
# ---------------------------------------------------------------------------
def test_no_hardcoded_nissan_in_models():
    """Verify Pydantic model defaults don't contain Nissan."""
    from app.report_store import RefreshEvidenceRequest
    from app.evidence_jobs import FullRefreshRequest, SerpApiJobRequest
    
    for cls in [RefreshEvidenceRequest, FullRefreshRequest]:
        instance = cls() if cls != FullRefreshRequest else cls(target_run_id="test")
        assert "nissan" not in instance.brand.lower(), f"{cls.__name__}.brand defaults to Nissan"
        assert "japan" not in instance.market.lower(), f"{cls.__name__}.market defaults to Japan"
    
    serp = SerpApiJobRequest(target_run_id="test", queries=[])
    assert "nissan" not in serp.brand.lower(), "SerpApiJobRequest.brand defaults to Nissan"
    print("  ✓ No hardcoded Nissan/Japan in model defaults")


if __name__ == "__main__":
    print("\n" + "=" * 60)
    print("Sprint 1 — Multi-Brand Parameterisation Tests")
    print("=" * 60)
    
    tests = [
        test_refresh_request_schema,
        test_refresh_request_with_values,
        test_full_refresh_request_schema,
        test_owned_domains_for_brand_explicit,
        test_owned_domains_for_brand_empty_when_no_explicit,
        test_owned_domains_for_brand_url_stripping,
        test_no_hardcoded_nissan_in_models,
    ]
    
    passed = 0
    failed = 0
    for test in tests:
        try:
            test()
            passed += 1
        except Exception as e:
            print(f"  ✗ {test.__name__}: {e}")
            failed += 1
    
    print(f"\nResults: {passed} passed, {failed} failed out of {len(tests)} tests")
    sys.exit(1 if failed else 0)
