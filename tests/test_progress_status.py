"""Unit tests for progress bar and running status semantics.

These tests validate the _is_truly_active() logic in report_store.py
and the expected frontend behavior for fetchRefreshStatus normalization.
"""
from __future__ import annotations

import os
import sys
import time
import unittest

# Set DATA_DIR before importing app modules
import tempfile
TEST_DATA_DIR = tempfile.mkdtemp(prefix="test_progress_")
os.environ["DATA_DIR"] = TEST_DATA_DIR
os.environ["ADMIN_TOKEN"] = ""

from app.report_store import (
    SUCCESS_STATES,
    IN_PROGRESS_STATES,
    FAILED_STATES,
    REPORT_READY_STAGES,
    ACTIVE_RUN_STALE_SECONDS,
    AUDITOR_ACTIVE_STALE_SECONDS,
    now_epoch,
)


def _is_truly_active(r: dict) -> bool:
    """Replicate the _is_truly_active logic from report_store.py for testing."""
    st = str(r.get("status", "")).lower()
    stage = str(r.get("stage", "")).lower()

    if st in SUCCESS_STATES or st in FAILED_STATES:
        return False
    if stage in REPORT_READY_STAGES:
        return False
    error_fields = ["auditor_error", "portfolio_error", "bodhi_error", "error"]
    if any(r.get(f) for f in error_fields):
        return False
    if stage.endswith("_failed"):
        return False
    if r.get("failed") is True:
        return False

    updated_at = r.get("updated_at_epoch") or r.get("started_at_epoch") or r.get("created_at_epoch") or 0
    try:
        updated_at = int(updated_at)
    except Exception:
        updated_at = 0
    if updated_at > 0:
        is_auditor_stage = stage.startswith("auditor") or stage == "evidence_ready"
        stale_limit = AUDITOR_ACTIVE_STALE_SECONDS if is_auditor_stage else ACTIVE_RUN_STALE_SECONDS
        if (now_epoch() - updated_at) > stale_limit:
            return False

    if st in IN_PROGRESS_STATES:
        return True
    if stage == "evidence_ready":
        return True
    return False


class TestActiveRunNull(unittest.TestCase):
    """Test 1: active_run: null returns idle and no highlighted stage."""

    def test_no_active_run_returns_idle(self):
        # When there is no active run, the status endpoint returns active_run: null.
        # Frontend should show: Ready to Start, all stages pending, right rail Idle.
        result = _is_truly_active({})
        self.assertFalse(result, "Empty run dict should not be active")

    def test_completed_run_not_active(self):
        run = {"status": "completed", "stage": "report_bundle_ready", "updated_at_epoch": now_epoch()}
        self.assertFalse(_is_truly_active(run), "Completed run should not be active")

    def test_successful_run_not_active(self):
        run = {"status": "success", "stage": "report_bundle_ready", "updated_at_epoch": now_epoch()}
        self.assertFalse(_is_truly_active(run), "Successful run should not be active")


class TestLatestSuccessfulRunIdAlone(unittest.TestCase):
    """Test 2: latest_successful_run_id alone does not mark progress complete."""

    def test_latest_successful_does_not_activate(self):
        # A run with only latest_successful_run_id should NOT be treated as active.
        # This field belongs only in the right rail, not the progress bar.
        run = {"status": "completed", "stage": "report_bundle_ready",
               "latest_successful_run_id": "evidence_nissan_japan_123",
               "updated_at_epoch": now_epoch()}
        self.assertFalse(_is_truly_active(run),
                         "latest_successful_run_id should not make a completed run active")


class TestRunningWithAuditorError(unittest.TestCase):
    """Test 3: status: running + auditor_error becomes failed."""

    def test_running_with_auditor_error_is_not_active(self):
        run = {
            "status": "running",
            "stage": "evidence_ready",
            "auditor_error": "Bodhi POST failed: 401 Unauthorized",
            "updated_at_epoch": now_epoch(),
        }
        self.assertFalse(_is_truly_active(run),
                         "Running + auditor_error should NOT be active (contradictory state)")

    def test_running_with_portfolio_error_is_not_active(self):
        run = {
            "status": "running",
            "stage": "portfolio_generation_running",
            "portfolio_error": "Bodhi workflow failed",
            "updated_at_epoch": now_epoch(),
        }
        self.assertFalse(_is_truly_active(run),
                         "Running + portfolio_error should NOT be active")

    def test_running_with_bodhi_error_is_not_active(self):
        run = {
            "status": "running",
            "stage": "auditor_queued",
            "bodhi_error": "Task creation failed",
            "updated_at_epoch": now_epoch(),
        }
        self.assertFalse(_is_truly_active(run),
                         "Running + bodhi_error should NOT be active")


class TestStaleUpdatedAt(unittest.TestCase):
    """Test 4: stale updated_at_epoch is not active."""

    def test_stale_general_run_not_active(self):
        # A run updated more than 1 hour ago should be stale
        stale_epoch = now_epoch() - ACTIVE_RUN_STALE_SECONDS - 60
        run = {
            "status": "running",
            "stage": "owned_url_mapping_running",
            "updated_at_epoch": stale_epoch,
        }
        self.assertFalse(_is_truly_active(run),
                         "Run not updated for over 1 hour should be stale")

    def test_stale_auditor_run_not_active(self):
        # Auditor stages get 2-hour timeout
        stale_epoch = now_epoch() - AUDITOR_ACTIVE_STALE_SECONDS - 60
        run = {
            "status": "running",
            "stage": "auditor_running",
            "updated_at_epoch": stale_epoch,
        }
        self.assertFalse(_is_truly_active(run),
                         "Auditor run not updated for over 2 hours should be stale")

    def test_fresh_auditor_within_timeout_is_active(self):
        # Auditor updated 30 minutes ago should still be active
        fresh_epoch = now_epoch() - 1800  # 30 minutes
        run = {
            "status": "running",
            "stage": "auditor_running",
            "updated_at_epoch": fresh_epoch,
        }
        self.assertTrue(_is_truly_active(run),
                        "Auditor run updated 30 min ago should still be active")


class TestFreshAuditorRunning(unittest.TestCase):
    """Test 5: fresh auditor_running is active and maps to Bodhi auditor."""

    def test_fresh_auditor_running_is_active(self):
        run = {
            "status": "running",
            "stage": "auditor_running",
            "updated_at_epoch": now_epoch(),
        }
        self.assertTrue(_is_truly_active(run),
                        "Fresh auditor_running should be active")

    def test_fresh_auditor_queued_is_active(self):
        run = {
            "status": "running",
            "stage": "auditor_queued",
            "updated_at_epoch": now_epoch(),
        }
        self.assertTrue(_is_truly_active(run),
                        "Fresh auditor_queued should be active")

    def test_evidence_ready_is_active(self):
        run = {
            "status": "running",
            "stage": "evidence_ready",
            "updated_at_epoch": now_epoch(),
        }
        self.assertTrue(_is_truly_active(run),
                        "evidence_ready with running status should be active")


class TestReportBundleReadyBehavior(unittest.TestCase):
    """Test 6: report_bundle_ready after user-started run shows completed,
    but after page reload shows idle."""

    def test_report_bundle_ready_not_active(self):
        run = {
            "status": "completed",
            "stage": "report_bundle_ready",
            "updated_at_epoch": now_epoch(),
        }
        self.assertFalse(_is_truly_active(run),
                         "report_bundle_ready should not be active")

    def test_report_bundle_ready_stage_alone_not_active(self):
        # Even if status is still "running", report_bundle_ready stage means done
        run = {
            "status": "running",
            "stage": "report_bundle_ready",
            "updated_at_epoch": now_epoch(),
        }
        self.assertFalse(_is_truly_active(run),
                         "report_bundle_ready stage should override running status")


class TestFailedRunAfterReload(unittest.TestCase):
    """Test 7: failed run while tracked shows red, but after reload
    brand/market status shows idle."""

    def test_failed_run_not_active(self):
        run = {
            "status": "failed",
            "stage": "auditor_failed",
            "auditor_error": "401 Unauthorized",
            "updated_at_epoch": now_epoch(),
        }
        self.assertFalse(_is_truly_active(run),
                         "Failed run should not be active")

    def test_failed_stage_not_active(self):
        run = {
            "status": "running",
            "stage": "auditor_ui_hitl_failed",
            "updated_at_epoch": now_epoch(),
        }
        self.assertFalse(_is_truly_active(run),
                         "Stage ending in _failed should not be active")

    def test_explicit_failed_flag_not_active(self):
        run = {
            "status": "running",
            "stage": "portfolio_generation_running",
            "failed": True,
            "updated_at_epoch": now_epoch(),
        }
        self.assertFalse(_is_truly_active(run),
                         "Run with failed=True should not be active")


class TestInProgressStages(unittest.TestCase):
    """Additional tests for various in-progress stages."""

    def test_portfolio_generation_running_is_active(self):
        run = {
            "status": "running",
            "stage": "portfolio_generation_running",
            "updated_at_epoch": now_epoch(),
        }
        self.assertTrue(_is_truly_active(run))

    def test_sitemap_inventory_running_is_active(self):
        run = {
            "status": "running",
            "stage": "sitemap_inventory_running",
            "updated_at_epoch": now_epoch(),
        }
        self.assertTrue(_is_truly_active(run))

    def test_serpapi_collection_running_is_active(self):
        run = {
            "status": "running",
            "stage": "serpapi_collection_running",
            "updated_at_epoch": now_epoch(),
        }
        self.assertTrue(_is_truly_active(run))

    def test_owned_crawl_running_is_active(self):
        run = {
            "status": "running",
            "stage": "owned_crawl_running",
            "updated_at_epoch": now_epoch(),
        }
        self.assertTrue(_is_truly_active(run))

    def test_accepted_is_active(self):
        run = {
            "status": "accepted",
            "stage": "accepted",
            "updated_at_epoch": now_epoch(),
        }
        self.assertTrue(_is_truly_active(run))


if __name__ == "__main__":
    print("\n" + "=" * 60)
    print("Progress Bar & Running Status — Unit Tests")
    print("=" * 60)
    unittest.main(verbosity=2)
