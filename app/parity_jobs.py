from __future__ import annotations

import json
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field

from app.local_scraper_adapter import (
    fallback_collect_urls,
    read_json,
    run_local_parity_scrape,
    selected_external_pages_from_scope,
    selected_owned_pages_from_scope,
    write_json,
)

router = APIRouter()

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data/evidence-runs"))
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")


class FullRefreshParityRequest(BaseModel):
    brand: str = "Nissan"
    market: str = "Japan"
    source_run_id: str
    target_run_id: str

    crawl_owned: bool = True
    crawl_external: bool = True

    max_owned_urls: int = 5
    max_external_urls: int = 5

    playwright_timeout_ms: int = 60000
    pdf_max_pages: int = 80

    owned_domains: list[str] = Field(default_factory=lambda: [
        "nissan.co.jp",
        "www.nissan.co.jp",
        "www2.nissan.co.jp",
        "www3.nissan.co.jp",
        "nissan-global.com",
        "www.nissan-global.com",
        "global.nissannews.com",
    ])


def require_admin(token: str | None):
    if ADMIN_TOKEN and token != ADMIN_TOKEN:
        raise HTTPException(status_code=401, detail="Invalid admin token")


def now_epoch() -> int:
    return int(time.time())


def make_job_id() -> str:
    return f"parityrefresh_{now_epoch()}_{uuid.uuid4().hex[:8]}"


def job_path(job_id: str) -> Path:
    return DATA_DIR / "_jobs" / f"{job_id}.json"


def update_job(job_id: str, patch: dict[str, Any]):
    path = job_path(job_id)
    current = {}
    if path.exists():
        current = read_json(path, {}) or {}
    current.update(patch)
    current["updated_at_epoch"] = now_epoch()
    write_json(path, current)


def copy_baseline_files(source_dir: Path, target_dir: Path):
    target_dir.mkdir(parents=True, exist_ok=True)

    for filename in [
        "audit_context.json",
        "evidence_scope.json",
        "google_ai_mode_compact.json",
        "visibility_matrix.json",
        "source_classification.json",
    ]:
        src = source_dir / filename
        if src.exists():
            (target_dir / filename).write_text(src.read_text(encoding="utf-8"), encoding="utf-8")


def build_compact_bundle(run_dir: Path):
    file_map = {
        "audit_context": "audit_context.json",
        "evidence_scope": "evidence_scope.json",
        "google_ai_mode_compact": "google_ai_mode_compact.json",
        "owned_pages_full": "owned_pages_full.json",
        "external_pages_full": "external_pages_full.json",
        "visibility_matrix": "visibility_matrix.json",
        "source_classification": "source_classification.json",
    }

    bundle = {}
    missing = []

    for key, filename in file_map.items():
        path = run_dir / filename
        if path.exists():
            bundle[key] = read_json(path, {})
        else:
            missing.append(filename)

    manifest = {
        "run_id": run_dir.name,
        "status": "ready" if not missing else "incomplete",
        "missing_files": missing,
        "files": sorted([p.name for p in run_dir.glob("*.json")]),
        "updated_at_epoch": now_epoch(),
        "compact_bundle": str(run_dir / "compact_bundle.json"),
    }

    write_json(run_dir / "compact_bundle.json", bundle)
    write_json(run_dir / "manifest.json", manifest)
    write_json(run_dir / "run_manifest.json", manifest)


def run_parity_refresh(job_id: str, req: FullRefreshParityRequest):
    try:
        source_dir = DATA_DIR / req.source_run_id
        target_dir = DATA_DIR / req.target_run_id

        if not source_dir.exists():
            raise FileNotFoundError(f"Source run does not exist: {source_dir}")

        update_job(job_id, {
            "status": "running",
            "stage": "copy_baseline",
            "source_run_id": req.source_run_id,
            "target_run_id": req.target_run_id,
        })

        copy_baseline_files(source_dir, target_dir)

        scope = read_json(source_dir / "evidence_scope.json", {}) or {}
        audit_context = read_json(source_dir / "audit_context.json", {}) or {}
        google_compact = read_json(source_dir / "google_ai_mode_compact.json", {}) or {}
        source_classification = read_json(source_dir / "source_classification.json", {}) or {}

        owned_items = selected_owned_pages_from_scope(scope)
        external_items = selected_external_pages_from_scope(scope)

        if not owned_items or not external_items:
            fallback_owned, fallback_external = fallback_collect_urls(
                {
                    "scope": scope,
                    "audit_context": audit_context,
                    "google_ai_mode_compact": google_compact,
                    "source_classification": source_classification,
                },
                set(req.owned_domains),
            )
            if not owned_items:
                owned_items = fallback_owned
            if not external_items:
                external_items = fallback_external

        owned_items = owned_items[: max(0, req.max_owned_urls)]
        external_items = external_items[: max(0, req.max_external_urls)]

        update_job(job_id, {
            "stage": "inventory_built",
            "owned_url_count": len(owned_items),
            "external_url_count": len(external_items),
        })

        scrape_cfg = {
            "playwright_timeout_ms": req.playwright_timeout_ms,
            "pdf_parser": {"max_pages": req.pdf_max_pages},
        }

        owned_pages = []
        owned_failed = []
        if req.crawl_owned:
            update_job(job_id, {"stage": "crawl_owned", "current": 0, "total": len(owned_items)})
            owned_result = run_local_parity_scrape(
                owned_items,
                req.target_run_id,
                "owned",
                req.max_owned_urls,
                scrape_cfg,
            )
            owned_pages = owned_result["pages"]
            owned_failed = owned_result["failed"]

        update_job(job_id, {"stage": "crawl_external", "current": 0, "total": len(external_items)})

        external_pages = []
        external_failed = []
        if req.crawl_external:
            external_result = run_local_parity_scrape(
                external_items,
                req.target_run_id,
                "external",
                req.max_external_urls,
                scrape_cfg,
            )
            external_pages = external_result["pages"]
            external_failed = external_result["failed"]

        owned_payload = {
            "collector": "local_full_page_owned_pages",
            "status": "success" if owned_pages else "failed",
            "paid_api_used": False,
            "firecrawl_used": False,
            "railway_parity_mode": True,
            "source_scraper": "scripts/local_hybrid_scraper.py from successful local run",
            "brand": req.brand,
            "market": req.market,
            "pages_requested": len(owned_items),
            "pages_collected": sum(1 for p in owned_pages if p.get("crawl_status") == "success"),
            "pages_weak": sum(1 for p in owned_pages if p.get("crawl_status") in {"weak", "partial"}),
            "pages_failed": sum(1 for p in owned_pages if p.get("crawl_status") in {"failed", "blocked"}),
            "pages": owned_pages,
            "failed_pages": owned_failed,
        }

        external_payload = {
            "collector": "local_full_page_external_pages",
            "status": "success" if external_pages else "failed",
            "paid_api_used": False,
            "firecrawl_used": False,
            "railway_parity_mode": True,
            "source_scraper": "scripts/local_hybrid_scraper.py from successful local run",
            "brand": req.brand,
            "market": req.market,
            "sources_requested": len(external_items),
            "sources_collected": sum(1 for p in external_pages if p.get("crawl_status") == "success"),
            "sources_weak": sum(1 for p in external_pages if p.get("crawl_status") in {"weak", "partial"}),
            "sources_failed": sum(1 for p in external_pages if p.get("crawl_status") in {"failed", "blocked"}),
            "external_pages": external_pages,
            "pages": external_pages,
            "failed_sources": external_failed,
        }

        write_json(target_dir / "owned_pages_full.json", owned_payload)
        write_json(target_dir / "external_pages_full.json", external_payload)

        build_compact_bundle(target_dir)

        update_job(job_id, {
            "status": "completed",
            "stage": "done",
            "target_run_id": req.target_run_id,
            "owned_attempted": len(owned_items),
            "owned_successful": owned_payload["pages_collected"],
            "owned_weak": owned_payload["pages_weak"],
            "owned_failed": owned_payload["pages_failed"],
            "external_attempted": len(external_items),
            "external_successful": external_payload["sources_collected"],
            "external_weak": external_payload["sources_weak"],
            "external_failed": external_payload["sources_failed"],
            "completed_at_epoch": now_epoch(),
        })

    except Exception as e:
        update_job(job_id, {
            "status": "failed",
            "stage": "error",
            "error": str(e)[:2000],
            "failed_at_epoch": now_epoch(),
        })


@router.post("/jobs/full-refresh-parity")
def create_full_refresh_parity(req: FullRefreshParityRequest, x_admin_token: str | None = Header(default=None)):
    require_admin(x_admin_token)

    job_id = make_job_id()

    update_job(job_id, {
        "status": "accepted",
        "job_id": job_id,
        "source_run_id": req.source_run_id,
        "target_run_id": req.target_run_id,
        "brand": req.brand,
        "market": req.market,
        "created_at_epoch": now_epoch(),
        "request": req.model_dump(),
    })

    thread = threading.Thread(target=run_parity_refresh, args=(job_id, req), daemon=True)
    thread.start()

    return {
        "status": "accepted",
        "job_id": job_id,
        "source_run_id": req.source_run_id,
        "target_run_id": req.target_run_id,
        "job_status_url": f"/jobs/parity/{job_id}",
    }


@router.get("/jobs/parity/{job_id}")
def get_parity_job(job_id: str):
    path = job_path(job_id)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Job not found")
    return read_json(path, {})
