import os
import json
import tempfile
import unittest
import importlib.util
from pathlib import Path

from app.ai_hygiene import NOT_FULLY_CHECKED_SUMMARY, build_ai_discoverability_hygiene


class AiHygieneHelperTests(unittest.TestCase):
    def test_not_found_is_checked_summary(self):
        hygiene = build_ai_discoverability_hygiene(
            owned_pages=[
                {
                    "url": "https://example.com/a",
                    "title": "A",
                    "json_ld_present": False,
                    "json_ld_block_count": 0,
                    "schema_types_detected": [],
                }
            ],
            robots_txt={"status": "available", "url": "https://example.com/robots.txt"},
            llms_txt={"status": "not found", "url": "https://example.com/llms.txt"},
        )
        self.assertEqual(hygiene["priority"], "high")
        self.assertNotEqual(hygiene["summary"], NOT_FULLY_CHECKED_SUMMARY)
        self.assertIn("0/1 owned pages (0%)", hygiene["summary"])
        self.assertEqual(hygiene["llms_txt"]["status"], "not found")

    def test_absent_structured_fields_are_not_checked(self):
        hygiene = build_ai_discoverability_hygiene(
            owned_pages=[{"url": "https://example.com/a", "title": "A"}],
            robots_txt={"status": "available"},
            llms_txt={"status": "not found", "checked_urls": [{"url": "https://example.com/llms.txt", "http_status_code": 404}]},
        )
        self.assertEqual(hygiene["priority"], "high")
        self.assertEqual(hygiene["summary"], NOT_FULLY_CHECKED_SUMMARY)
        self.assertEqual(hygiene["structured_data"]["coverage_pct"], 0)

    def test_failed_default_empty_schema_is_not_checked(self):
        hygiene = build_ai_discoverability_hygiene(
            owned_pages=[{"url": "https://example.com/a", "crawl_status": "failed", "schema_types_detected": []}],
            robots_txt={"status": "available"},
            llms_txt={"status": "available"},
        )
        self.assertEqual(hygiene["summary"], NOT_FULLY_CHECKED_SUMMARY)

    def test_geo_scores_do_not_affect_json_ld_counts(self):
        hygiene = build_ai_discoverability_hygiene(
            owned_pages=[
                {
                    "url": "https://example.com/a",
                    "title": "A",
                    "json_ld_present": False,
                    "json_ld_block_count": 0,
                    "schema_types_detected": [],
                    "geo_dimensions": {"structured_data": 100},
                },
                {
                    "url": "https://example.com/b",
                    "title": "B",
                    "json_ld_present": True,
                    "json_ld_block_count": 1,
                    "schema_types": ["Product", "FAQPage"],
                    "geo_dimensions": {"structured_data": 0},
                },
            ],
            robots_txt={"status": "available"},
            llms_txt={"status": "available"},
        )
        structured = hygiene["structured_data"]
        self.assertEqual(structured["pages_with_json_ld"], 1)
        self.assertEqual(structured["pages_with_schema"], 1)
        self.assertEqual(structured["coverage_pct"], 50.0)
        self.assertEqual(structured["schema_types_detected"], [("Product", 1), ("FAQPage", 1)])
        self.assertEqual(structured["pages_missing_json_ld"], [{"url": "https://example.com/a", "title": "A"}])


class ReportStoreHygieneTests(unittest.TestCase):
    @unittest.skipIf(importlib.util.find_spec("fastapi") is None, "FastAPI is not installed in this Python environment")
    def test_enrichment_helper_merges_full_inventory_without_fastapi(self):
        with tempfile.TemporaryDirectory() as tmp:
            import app.report_store as report_store

            report_store.DATA_DIR = Path(tmp)
            run_id = "direct_full_inventory"
            run_dir = Path(tmp) / run_id
            run_dir.mkdir(parents=True)
            pages = [
                {"url": f"https://www.nissan.co.jp/direct-{idx}.html", "current_geo_score_120": idx, "geo_analysis_ready": True}
                for idx in range(4)
            ]
            (run_dir / "owned_pages_full.json").write_text(json.dumps({"pages": pages}), encoding="utf-8")
            bundle = {
                "schema_version": "query_workbench.v1",
                "run_id": run_id,
                "brand": "Nissan",
                "market": "Japan",
                "query_workbench": [{
                    "query_id": "Q001",
                    "query": "direct",
                    "mapped_owned_urls": [{"url": "https://www.nissan.co.jp/direct-0.html"}],
                }],
            }

            enriched = report_store.enrich_report_bundle(run_id, bundle)
            self.assertEqual(len(enriched["owned_url_readiness"]), 4)
            self.assertEqual(enriched["owned_pages_scoreable"], 4)
            mapped = {row["url"]: row["query_mapped"] for row in enriched["owned_url_readiness"]}
            self.assertTrue(mapped["https://www.nissan.co.jp/direct-0.html"])
            self.assertFalse(mapped["https://www.nissan.co.jp/direct-3.html"])

    @unittest.skipIf(importlib.util.find_spec("fastapi") is None, "FastAPI is not installed in this Python environment")
    def test_enrichment_helper_preserves_source_citations_without_fastapi(self):
        with tempfile.TemporaryDirectory() as tmp:
            import app.report_store as report_store

            report_store.DATA_DIR = Path(tmp)
            run_id = "direct_citations"
            run_dir = Path(tmp) / run_id
            run_dir.mkdir(parents=True)
            (run_dir / "google_ai_mode_compact.json").write_text(json.dumps({
                "rows": [{
                    "query_id": "Q001",
                    "query": "citation query",
                    "top_citations": [{"url": "https://example.com/a", "source_domain": "example.com", "source_type": "publisher", "snippet": "Evidence text"}],
                }]
            }), encoding="utf-8")
            enriched = report_store.enrich_report_bundle(run_id, {"schema_version": "query_workbench.v1", "query_workbench": [{"query": "citation query"}]})
            citation = enriched["source_landscape"]["source_citations"][0]
            self.assertEqual(citation["query_id"], "Q001")
            self.assertEqual(citation["source_domain"], "example.com")
            self.assertEqual(citation["snippet"], "Evidence text")

    @unittest.skipIf(importlib.util.find_spec("fastapi") is None, "FastAPI is not installed in this Python environment")
    def test_load_report_bundle_does_not_open_heavy_artifacts_on_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            import app.report_store as report_store

            report_store.DATA_DIR = Path(tmp)
            run_id = "read_path_no_artifact_repair"
            run_dir = Path(tmp) / run_id
            run_dir.mkdir(parents=True)
            valid_hygiene = {
                "priority": "low",
                "summary": "checked",
                "robots_txt": {"status": "available"},
                "llms_txt": {"status": "not found"},
                "structured_data": {
                    "owned_pages_total": 1,
                    "pages_with_schema": 1,
                    "pages_with_json_ld": 1,
                    "coverage_pct": 100,
                },
            }
            report_store.write_json(
                run_dir / "frontend_report_bundle.json",
                {
                    "schema_version": "query_workbench.v1",
                    "run_id": run_id,
                    "brand": "Nissan",
                    "market": "Japan",
                    "query_workbench": [{"query": "test"}],
                    "owned_url_readiness": [{"url": "https://www.nissan.co.jp/a.html", "current_geo_score_120": 42}],
                    "source_landscape": {"source_citations": [{"url": "https://example.com/a", "domain": "example.com"}]},
                    "ai_discoverability_hygiene": valid_hygiene,
                },
            )

            original_owned = report_store.owned_rows_from_run_artifacts
            original_citations = report_store.source_citations_from_run_artifacts
            try:
                def fail_owned(_run_id):
                    raise AssertionError("read path should not open owned artifacts")

                def fail_citations(_run_id):
                    raise AssertionError("read path should not open citation artifacts")

                report_store.owned_rows_from_run_artifacts = fail_owned
                report_store.source_citations_from_run_artifacts = fail_citations
                loaded = report_store.load_report_bundle(run_id)
            finally:
                report_store.owned_rows_from_run_artifacts = original_owned
                report_store.source_citations_from_run_artifacts = original_citations

            self.assertEqual(len(loaded["owned_url_readiness"]), 1)
            self.assertEqual(loaded["owned_pages_scoreable"], 1)
            self.assertEqual(loaded["source_landscape"]["source_citations"][0]["domain"], "example.com")

    @unittest.skipIf(importlib.util.find_spec("fastapi") is None, "FastAPI is not installed in this Python environment")
    def test_store_and_latest_inject_hygiene(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["DATA_DIR"] = tmp
            from fastapi.testclient import TestClient

            import app.report_store as report_store
            import app.main as main

            report_store.DATA_DIR = Path(tmp)
            main.DATA_DIR = Path(tmp)
            run_id = "evidence_nissan_japan_test"
            run_dir = Path(tmp) / run_id
            run_dir.mkdir(parents=True)
            (run_dir / "owned_pages_full.json").write_text(
                json.dumps({"pages": [{"url": "https://www.nissan.co.jp/a", "title": "A", "json_ld_present": False, "json_ld_block_count": 0, "schema_types_detected": []}]}),
                encoding="utf-8",
            )
            bundle = {
                "schema_version": "query_workbench.v1",
                "run_id": run_id,
                "brand": "Nissan",
                "market": "Japan",
                "query_workbench": [{"query": "test"}],
            }

            client = TestClient(main.app)
            response = client.post(f"/runs/{run_id}/report-bundle", json=bundle)
            self.assertEqual(response.status_code, 200)
            latest = client.get("/runs/latest/report-bundle", params={"brand": "Nissan", "market": "Japan"})
            self.assertEqual(latest.status_code, 200)
            payload = latest.json()
            self.assertIn("ai_discoverability_hygiene", payload)
            hygiene = payload["ai_discoverability_hygiene"]
            self.assertEqual(hygiene["robots_txt"]["status"], "not checked")
            self.assertEqual(hygiene["llms_txt"]["status"], "not checked")
            self.assertEqual(hygiene["structured_data"]["pages_with_json_ld"], 0)

    @unittest.skipIf(importlib.util.find_spec("fastapi") is None, "FastAPI is not installed in this Python environment")
    def test_existing_hygiene_is_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["DATA_DIR"] = tmp
            from fastapi.testclient import TestClient

            import app.report_store as report_store
            import app.main as main

            report_store.DATA_DIR = Path(tmp)
            main.DATA_DIR = Path(tmp)
            run_id = "evidence_preserve_test"
            existing = {
                "priority": "low",
                "summary": "custom",
                "robots_txt": {"status": "available"},
                "llms_txt": {"status": "available"},
                "structured_data": {
                    "owned_pages_total": 1,
                    "pages_with_schema": 1,
                    "pages_with_json_ld": 1,
                    "coverage_pct": 100,
                },
            }
            bundle = {
                "schema_version": "query_workbench.v1",
                "run_id": run_id,
                "brand": "Nissan",
                "market": "Japan",
                "query_workbench": [{"query": "test"}],
                "ai_discoverability_hygiene": existing,
            }

            client = TestClient(main.app)
            response = client.post(f"/runs/{run_id}/report-bundle", json=bundle)
            self.assertEqual(response.status_code, 200)
            latest = client.get("/runs/latest/report-bundle", params={"brand": "Nissan", "market": "Japan"})
            self.assertEqual(latest.status_code, 200)
            self.assertEqual(latest.json()["ai_discoverability_hygiene"], existing)

    @unittest.skipIf(importlib.util.find_spec("fastapi") is None, "FastAPI is not installed in this Python environment")
    def test_report_bundle_enriches_full_owned_inventory_and_citations(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["DATA_DIR"] = tmp
            from fastapi.testclient import TestClient

            import app.report_store as report_store
            import app.main as main

            report_store.DATA_DIR = Path(tmp)
            main.DATA_DIR = Path(tmp)
            run_id = "evidence_nissan_japan_full_inventory"
            run_dir = Path(tmp) / run_id
            run_dir.mkdir(parents=True)
            pages = []
            for idx in range(40):
                url = f"https://www.nissan.co.jp/page-{idx}.html"
                pages.append({
                    "url": url,
                    "title": f"Page {idx}",
                    "current_geo_score_120": idx,
                    "geo_dimensions": {"content_clarity": idx % 10, "structured_data": 0},
                    "geo_analysis_ready": True,
                    "inventory_source": "sitemap_inventory",
                    "json_ld_present": idx == 0,
                    "json_ld_block_count": 1 if idx == 0 else 0,
                    "schema_types": ["Product"] if idx == 0 else [],
                    "crawl_status": "success",
                })
            (run_dir / "owned_pages_full.json").write_text(
                json.dumps({"pages": pages}),
                encoding="utf-8",
            )
            (run_dir / "google_ai_mode_compact.json").write_text(
                json.dumps({
                    "rows": [{
                        "query_id": "Q001",
                        "query": "best ev japan",
                        "top_citations": [{
                            "url": "https://example.com/ev",
                            "source_domain": "example.com",
                            "source_type": "publisher_review",
                            "title": "EV guide",
                            "snippet": "Captured citation text",
                            "rank": 1,
                        }],
                    }]
                }),
                encoding="utf-8",
            )
            bundle = {
                "schema_version": "query_workbench.v1",
                "run_id": run_id,
                "brand": "Nissan",
                "market": "Japan",
                "executive": {"headline_metrics": {"owned_page_count": 1}},
                "query_workbench": [{
                    "query_id": "Q001",
                    "query": "best ev japan",
                    "current_ai_visibility": {"status": "external_led"},
                    "mapped_owned_urls": [{"url": "https://www.nissan.co.jp/page-0.html", "current_geo_score_120": 10}],
                }],
                "page_level_cms_recommendations": [{"target_url": "https://www.nissan.co.jp/page-0.html"}],
                "owned_url_readiness": [{"url": "https://www.nissan.co.jp/page-0.html", "current_geo_score_120": 10}],
            }

            client = TestClient(main.app)
            response = client.post(f"/runs/{run_id}/report-bundle", json=bundle)
            self.assertEqual(response.status_code, 200)
            payload = client.get("/runs/latest/report-bundle", params={"brand": "Nissan", "market": "Japan"}).json()
            rows = payload["owned_url_readiness"]
            self.assertEqual(len(rows), 40)
            mapped = {row["url"]: row["query_mapped"] for row in rows}
            self.assertTrue(mapped["https://www.nissan.co.jp/page-0.html"])
            self.assertFalse(mapped["https://www.nissan.co.jp/page-39.html"])
            self.assertEqual(payload["executive"]["headline_metrics"]["owned_page_count"], 40)
            self.assertEqual(payload["owned_pages_scoreable"], 40)
            self.assertEqual(payload["source_landscape"]["source_citations"][0]["source_domain"], "example.com")
            self.assertEqual(payload["source_landscape"]["source_citations"][0]["snippet"], "Captured citation text")

            history = client.get("/reports/history", params={"brand": "Nissan", "market": "Japan"}).json()
            self.assertEqual(history["runs"][0]["owned_pages_scoreable"], 40)
            self.assertEqual(history["runs"][0]["owned_query_mapped_unique"], 1)

    @unittest.skipIf(importlib.util.find_spec("fastapi") is None, "FastAPI is not installed in this Python environment")
    def test_evidence_ready_is_not_latest_successful(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["DATA_DIR"] = tmp
            from fastapi.testclient import TestClient

            import app.report_store as report_store
            import app.main as main

            report_store.DATA_DIR = Path(tmp)
            main.DATA_DIR = Path(tmp)
            client = TestClient(main.app)

            previous = {
                "schema_version": "query_workbench.v1",
                "run_id": "previous_completed",
                "brand": "Nissan",
                "market": "Japan",
                "query_workbench": [{"query": "old"}],
                "owned_url_readiness": [{"url": "https://www.nissan.co.jp/old.html", "current_geo_score_120": 20}],
            }
            self.assertEqual(client.post("/runs/previous_completed/report-bundle", json=previous).status_code, 200)

            active_dir = Path(tmp) / "new_active"
            active_dir.mkdir(parents=True)
            report_store.write_json(active_dir / "report_manifest.json", {
                "run_id": "new_active",
                "brand": "Nissan",
                "market": "Japan",
                "status": "running",
                "stage": "evidence_ready",
                "dashboard_ready": False,
                "created_at_epoch": 9999999999,
            })
            report_store.write_run_status("new_active", "running", {"brand": "Nissan", "market": "Japan", "stage": "evidence_ready"})

            latest = client.get("/runs/latest/report-bundle", params={"brand": "Nissan", "market": "Japan"})
            self.assertEqual(latest.status_code, 200)
            self.assertEqual(latest.json()["run_id"], "previous_completed")
            status = client.get("/runs/status", params={"brand": "Nissan", "market": "Japan"}).json()
            self.assertEqual(status["latest_successful_run_id"], "previous_completed")
            self.assertEqual(status["active_run"]["run_id"], "new_active")

    @unittest.skipIf(importlib.util.find_spec("fastapi") is None, "FastAPI is not installed in this Python environment")
    def test_hygiene_missing_pages_do_not_create_scored_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["DATA_DIR"] = tmp
            from fastapi.testclient import TestClient

            import app.report_store as report_store
            import app.main as main

            report_store.DATA_DIR = Path(tmp)
            main.DATA_DIR = Path(tmp)
            run_id = "hygiene_missing_only"
            bundle = {
                "schema_version": "query_workbench.v1",
                "run_id": run_id,
                "brand": "Nissan",
                "market": "Japan",
                "query_workbench": [{"query": "test"}],
                "ai_discoverability_hygiene": {
                    "priority": "high",
                    "summary": "1/3 checked",
                    "robots_txt": {"status": "available"},
                    "llms_txt": {"status": "not found"},
                    "structured_data": {
                        "owned_pages_total": 3,
                        "pages_with_json_ld": 1,
                        "pages_with_schema": 1,
                        "coverage_pct": 33.3,
                        "pages_missing_json_ld": [
                            {"url": "https://www.nissan.co.jp/missing-a.html"},
                            {"url": "https://www.nissan.co.jp/missing-b.html"},
                        ],
                    },
                },
            }
            client = TestClient(main.app)
            self.assertEqual(client.post(f"/runs/{run_id}/report-bundle", json=bundle).status_code, 200)
            payload = client.get("/runs/latest/report-bundle", params={"brand": "Nissan", "market": "Japan"}).json()
            self.assertEqual(payload["owned_url_readiness"], [])
            self.assertEqual(payload["owned_pages_scoreable"], 0)


if __name__ == "__main__":
    unittest.main()
