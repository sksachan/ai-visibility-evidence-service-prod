from __future__ import annotations

import json
import multiprocessing as mp
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


class SafeParityRequest(BaseModel):
    brand: str = "Nissan"
    market: str = "Japan"
    source_run_id: str
    target_run_id: str

    crawl_owned: bool = True
    crawl_external: bool = True

    max_owned_urls: int = 20
    max_external_urls: int = 30

    playwright_timeout_ms: int = 60000
    pdf_max_pages: int = 80

    # Hard process timeout. This is intentionally longer than Playwright timeout.
    per_url_timeout_seconds: int = 120

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
    return f"safeparity_{now_epoch()}_{uuid.uuid4().hex[:8]}"


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


def write_partial_outputs(
    target_dir: Path,
    req: SafeParityRequest,
    owned_pages: list[dict[str, Any]],
    owned_failed: list[dict[str, Any]],
    external_pages: list[dict[str, Any]],
    external_failed: list[dict[str, Any]],
    owned_requested: int,
    external_requested: int,
):
    owned_payload = {
        "collector": "local_full_page_owned_pages",
        "status": "partial" if owned_failed else "success",
        "paid_api_used": False,
        "firecrawl_used": False,
        "railway_parity_mode": True,
        "safe_batched_mode": True,
        "source_scraper": "scripts/local_hybrid_scraper.py from successful local run",
        "brand": req.brand,
        "market": req.market,
        "pages_requested": owned_requested,
        "pages_collected": sum(1 for p in owned_pages if p.get("crawl_status") == "success"),
        "pages_weak": sum(1 for p in owned_pages if p.get("crawl_status") in {"weak", "partial"}),
        "pages_failed": len(owned_failed),
        "pages": owned_pages,
        "failed_pages": owned_failed,
    }

    external_payload = {
        "collector": "local_full_page_external_pages",
        "status": "partial" if external_failed else "success",
        "paid_api_used": False,
        "firecrawl_used": False,
        "railway_parity_mode": True,
        "safe_batched_mode": True,
        "source_scraper": "scripts/local_hybrid_scraper.py from successful local run",
        "brand": req.brand,
        "market": req.market,
        "sources_requested": external_requested,
        "sources_collected": sum(1 for p in external_pages if p.get("crawl_status") == "success"),
        "sources_weak": sum(1 for p in external_pages if p.get("crawl_status") in {"weak", "partial"}),
        "sources_failed": len(external_failed),
        "external_pages": external_pages,
        "pages": external_pages,
        "failed_sources": external_failed,
    }

    write_json(target_dir / "owned_pages_full.json", owned_payload)
    write_json(target_dir / "external_pages_full.json", external_payload)

    # Rebuild compact bundle every time, so the run is inspectable even mid-job.
    build_compact_bundle(target_dir)


def _scrape_one_worker(
    queue: mp.Queue,
    item: dict[str, Any],
    target_run_id: str,
    kind: str,
    index: int,
    playwright_timeout_ms: int,
    pdf_max_pages: int,
):
    try:
        # Use a unique internal run id per page so markdown/raw/rendered/manifests do not overwrite each other.
        internal_run_id = f"{target_run_id}/batches/{kind}_{index:03d}"

        result = run_local_parity_scrape(
            [item],
            internal_run_id,
            kind,
            1,
            {
                "playwright_timeout_ms": playwright_timeout_ms,
                "pdf_parser": {"max_pages": pdf_max_pages},
            },
        )

        pages = result.get("pages", [])
        failed = result.get("failed", [])

        queue.put({
            "ok": True,
            "pages": pages,
            "failed": failed,
        })

    except Exception as e:
        queue.put({
            "ok": False,
            "error": str(e)[:1500],
            "pages": [],
            "failed": [],
        })


def scrape_one_with_timeout(
    item: dict[str, Any],
    target_run_id: str,
    kind: str,
    index: int,
    playwright_timeout_ms: int,
    pdf_max_pages: int,
    timeout_seconds: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    queue: mp.Queue = mp.Queue()

    proc = mp.Process(
        target=_scrape_one_worker,
        args=(
            queue,
            item,
            target_run_id,
            kind,
            index,
            playwright_timeout_ms,
            pdf_max_pages,
        ),
    )

    proc.start()
    proc.join(timeout_seconds)

    url = item.get("url") or item.get("source_url") or ""

    if proc.is_alive():
        proc.terminate()
        proc.join(10)

        return [], [{
            "url": url,
            "crawl_status": "failed",
            "extraction_status": "timeout",
            "error": f"Per-URL hard timeout after {timeout_seconds}s",
            "source_item": item,
        }]

    if queue.empty():
        return [], [{
            "url": url,
            "crawl_status": "failed",
            "extraction_status": "empty_worker_result",
            "error": "Worker exited without returning a result.",
            "source_item": item,
        }]

    payload = queue.get()

    if not payload.get("ok"):
        return [], [{
            "url": url,
            "crawl_status": "failed",
            "extraction_status": "worker_error",
            "error": payload.get("error", "Unknown worker error"),
            "source_item": item,
        }]

    pages = payload.get("pages") or []
    failed = payload.get("failed") or []

    if not pages and not failed:
        failed = [{
            "url": url,
            "crawl_status": "failed",
            "extraction_status": "no_pages_returned",
            "error": "Scraper returned no page and no failure record.",
            "source_item": item,
        }]

    return pages, failed


def run_safe_parity(job_id: str, req: SafeParityRequest):
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

        owned_pages: list[dict[str, Any]] = []
        owned_failed: list[dict[str, Any]] = []
        external_pages: list[dict[str, Any]] = []
        external_failed: list[dict[str, Any]] = []

        update_job(job_id, {
            "stage": "inventory_built",
            "owned_url_count": len(owned_items),
            "external_url_count": len(external_items),
        })

        write_partial_outputs(
            target_dir,
            req,
            owned_pages,
            owned_failed,
            external_pages,
            external_failed,
            len(owned_items),
            len(external_items),
        )

        if req.crawl_owned:
            for i, item in enumerate(owned_items, start=1):
                update_job(job_id, {
                    "stage": "crawl_owned",
                    "current": i,
                    "total": len(owned_items),
                    "current_url": item.get("url"),
                    "owned_collected_so_far": len(owned_pages),
                    "owned_failed_so_far": len(owned_failed),
                })

                pages, failed = scrape_one_with_timeout(
                    item,
                    req.target_run_id,
                    "owned",
                    i,
                    req.playwright_timeout_ms,
                    req.pdf_max_pages,
                    req.per_url_timeout_seconds,
                )

                owned_pages.extend(pages)
                owned_failed.extend(failed)

                write_partial_outputs(
                    target_dir,
                    req,
                    owned_pages,
                    owned_failed,
                    external_pages,
                    external_failed,
                    len(owned_items),
                    len(external_items),
                )

        if req.crawl_external:
            for i, item in enumerate(external_items, start=1):
                update_job(job_id, {
                    "stage": "crawl_external",
                    "current": i,
                    "total": len(external_items),
                    "current_url": item.get("url") or item.get("source_url"),
                    "external_collected_so_far": len(external_pages),
                    "external_failed_so_far": len(external_failed),
                })

                pages, failed = scrape_one_with_timeout(
                    item,
                    req.target_run_id,
                    "external",
                    i,
                    req.playwright_timeout_ms,
                    req.pdf_max_pages,
                    req.per_url_timeout_seconds,
                )

                external_pages.extend(pages)
                external_failed.extend(failed)

                write_partial_outputs(
                    target_dir,
                    req,
                    owned_pages,
                    owned_failed,
                    external_pages,
                    external_failed,
                    len(owned_items),
                    len(external_items),
                )

        write_partial_outputs(
            target_dir,
            req,
            owned_pages,
            owned_failed,
            external_pages,
            external_failed,
            len(owned_items),
            len(external_items),
        )

        update_job(job_id, {
            "status": "completed",
            "stage": "done",
            "target_run_id": req.target_run_id,
            "owned_attempted": len(owned_items),
            "owned_successful": sum(1 for p in owned_pages if p.get("crawl_status") == "success"),
            "owned_failed": len(owned_failed),
            "external_attempted": len(external_items),
            "external_successful": sum(1 for p in external_pages if p.get("crawl_status") == "success"),
            "external_failed": len(external_failed),
            "completed_at_epoch": now_epoch(),
        })

    except Exception as e:
        update_job(job_id, {
            "status": "failed",
            "stage": "error",
            "error": str(e)[:2500],
            "failed_at_epoch": now_epoch(),
        })


@router.post("/jobs/full-refresh-parity-safe")
def create_safe_parity(req: SafeParityRequest, x_admin_token: str | None = Header(default=None)):
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

    thread = threading.Thread(target=run_safe_parity, args=(job_id, req), daemon=True)
    thread.start()

    return {
        "status": "accepted",
        "job_id": job_id,
        "source_run_id": req.source_run_id,
        "target_run_id": req.target_run_id,
        "job_status_url": f"/jobs/parity-safe/{job_id}",
    }


@router.get("/jobs/parity-safe/{job_id}")
def get_safe_parity_job(job_id: str):
    path = job_path(job_id)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Job not found")
    return read_json(path, {})
