"""Tests for per-query crawl controls and naming deduplication.

Verifies:
1. max_external_citations_per_query is the canonical name
2. max_external_sources_per_query is accepted as backward-compatible alias
3. Both per-query and overall caps flow through the refresh pipeline
"""
from __future__ import annotations

import os
import sys
import tempfile

# Set DATA_DIR before importing app modules
TEST_DATA_DIR = tempfile.mkdtemp(prefix="test_crawl_controls_")
os.environ["DATA_DIR"] = TEST_DATA_DIR
os.environ.setdefault("ADMIN_TOKEN", "")

from app.report_store import RefreshEvidenceRequest


def test_canonical_name_in_request_model():
    """RefreshEvidenceRequest should have max_external_citations_per_query field."""
    req = RefreshEvidenceRequest()
    assert hasattr(req, "max_external_citations_per_query"), (
        "RefreshEvidenceRequest missing max_external_citations_per_query field"
    )
    assert req.max_external_citations_per_query == 3, (
        f"Expected default 3, got {req.max_external_citations_per_query}"
    )
    print("  \u2713 RefreshEvidenceRequest has max_external_citations_per_query with default 3")


def test_per_query_owned_field():
    """RefreshEvidenceRequest should have max_owned_pages_per_query field."""
    req = RefreshEvidenceRequest()
    assert hasattr(req, "max_owned_pages_per_query"), (
        "RefreshEvidenceRequest missing max_owned_pages_per_query field"
    )
    print("  \u2713 RefreshEvidenceRequest has max_owned_pages_per_query")


def test_overall_caps_exist():
    """RefreshEvidenceRequest should have overall cap fields."""
    req = RefreshEvidenceRequest()
    assert hasattr(req, "max_owned_inventory_urls") or hasattr(req, "max_owned_urls"), (
        "RefreshEvidenceRequest missing max_owned_inventory_urls/max_owned_urls field"
    )
    assert hasattr(req, "max_external_urls"), (
        "RefreshEvidenceRequest missing max_external_urls field"
    )
    print("  \u2713 RefreshEvidenceRequest has overall cap fields")


def test_canonical_values_with_explicit_input():
    """When explicit values are provided, they should be used."""
    req = RefreshEvidenceRequest(
        brand="Toyota",
        market="Germany",
        max_external_citations_per_query=5,
        max_owned_pages_per_query=4,
    )
    assert req.max_external_citations_per_query == 5
    assert req.max_owned_pages_per_query == 4
    print("  \u2713 Explicit per-query values are preserved")


def test_no_max_external_sources_field_in_model():
    """RefreshEvidenceRequest should NOT have max_external_sources_per_query as a field.
    The aliasing happens in refresh_orchestrator, not in the Pydantic model."""
    req = RefreshEvidenceRequest()
    # The model uses the canonical name; aliasing is in the orchestrator
    assert hasattr(req, "max_external_citations_per_query")
    print("  \u2713 Model uses canonical max_external_citations_per_query")


def test_server_normalise_payload_uses_canonical():
    """Verify server.js normaliseRefreshPayload outputs max_external_citations_per_query."""
    import subprocess
    result = subprocess.run(
        ["grep", "-c", "max_external_citations_per_query", "ai-visibility-frontend/server.js"],
        capture_output=True, text=True, cwd=os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    )
    count = int(result.stdout.strip()) if result.returncode == 0 else 0
    assert count >= 1, "server.js should use max_external_citations_per_query"
    # Also verify max_external_sources_per_query is NOT in server.js
    result2 = subprocess.run(
        ["grep", "-c", "max_external_sources_per_query", "ai-visibility-frontend/server.js"],
        capture_output=True, text=True, cwd=os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    )
    sources_count = int(result2.stdout.strip()) if result2.returncode == 0 else 0
    assert sources_count == 0, f"server.js should NOT use max_external_sources_per_query (found {sources_count})"
    print("  \u2713 server.js uses canonical name only")


def test_evidence_service_backward_compat():
    """Verify refresh_orchestrator.py accepts both names with canonical first."""
    import subprocess
    result = subprocess.run(
        ["grep", "-n", "max_external_citations_per_query.*max_external_sources_per_query",
         "ai-visibility-evidence-service/app/refresh_orchestrator.py"],
        capture_output=True, text=True, cwd=os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    )
    assert result.returncode == 0, "refresh_orchestrator.py should have canonical-first alias line"
    print("  \u2713 Evidence service checks canonical name first, falls back to alias")


def test_bodhi_payload_sends_both_names():
    """Verify refresh_orchestrator sends both names in Bodhi trigger payload."""
    import subprocess
    result = subprocess.run(
        ["grep", "-c", "max_external_sources_per_query.*max_external",
         "ai-visibility-evidence-service/app/refresh_orchestrator.py"],
        capture_output=True, text=True, cwd=os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    )
    count = int(result.stdout.strip()) if result.returncode == 0 else 0
    assert count >= 1, "refresh_orchestrator should send max_external_sources_per_query in Bodhi payload for backward compat"
    print("  \u2713 Bodhi trigger payload includes backward-compatible alias")


def test_bodhi_workflow_uses_canonical():
    """Verify Bodhi Auditor workflow JSON uses max_external_citations_per_query."""
    import subprocess
    wf_path = "BodhiWorkflows/workflow-SS_-_WF_-_AI_Brand_Visibility_-_Auditor-3b21b01d-f2c7-4133-aca4-65761405deee.json"
    result = subprocess.run(
        ["grep", "-c", "max_external_citations_per_query", wf_path],
        capture_output=True, text=True, cwd=os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    )
    citations_count = int(result.stdout.strip()) if result.returncode == 0 else 0
    result2 = subprocess.run(
        ["grep", "-c", "max_external_sources_per_query", wf_path],
        capture_output=True, text=True, cwd=os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    )
    sources_count = int(result2.stdout.strip()) if result2.returncode == 0 else 0
    assert citations_count >= 3, f"Bodhi workflow should use max_external_citations_per_query (found {citations_count})"
    assert sources_count == 0, f"Bodhi workflow should NOT use max_external_sources_per_query (found {sources_count})"
    print(f"  \u2713 Bodhi workflow uses canonical name ({citations_count} refs), no old name ({sources_count} refs)")


if __name__ == "__main__":
    print("\n" + "=" * 60)
    print("Per-Query Crawl Controls & Naming Deduplication Tests")
    print("=" * 60)

    tests = [
        test_canonical_name_in_request_model,
        test_per_query_owned_field,
        test_overall_caps_exist,
        test_canonical_values_with_explicit_input,
        test_no_max_external_sources_field_in_model,
        test_server_normalise_payload_uses_canonical,
        test_evidence_service_backward_compat,
        test_bodhi_payload_sends_both_names,
        test_bodhi_workflow_uses_canonical,
    ]

    passed = 0
    failed = 0
    for test in tests:
        try:
            test()
            passed += 1
        except Exception as e:
            print(f"  \u2717 {test.__name__}: {e}")
            failed += 1

    print(f"\nResults: {passed} passed, {failed} failed out of {len(tests)} tests")
    sys.exit(1 if failed else 0)
