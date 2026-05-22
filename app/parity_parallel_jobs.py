from __future__ import annotations

import os
import time
import uuid
import threading
from concurrent.futures import ProcessPoolExecutor, wait, FIRST_COMPLETED
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


class ParallelParityRequest(BaseModel):
    brand: str = "Nissan"
    market: str = "Japan"
    source_run_id: str
    target_run_id: str

    crawl_owned: bool = True
    crawl_external: bool = True

    max_owned_urls: int = 20
    max_external_urls: int = 30

    owned_batch_size: int = 5
    external_batch_size: int = 5

    # Trial/free: use 2. Hobby: test 3-4.
    max_parallel_batches: int = 2

    playwright_timeout_ms: int = 60000
    pdf_max_pages: int = 80

    # Hard timeout per batch. Prevents one slow page batch blocking the whole job.
    batch_timeout_seconds: int = 300

    owned_domains: list[str] = Field(default_factory=lambda: [
        "nissan.co.jp",
        "www.nissan.co.jp",
        "www2.nissan.co.jp",
        "www3.nissan.co.jp",
        "nissan-global.com",
        "www.nissan-global.com",
        "global.nissannews.com",
    ])


def now_epoch() -> int:
    return int(time.time())


def require_admin(token: str | None):
    if ADMIN_TOKEN and token != ADMIN_TOKEN:
        raise HTTPException(status_code=401, detail="Invalid admin token")


def make_job_id() -> str:
    return f"parallelparity_{now_epoch()}_{uuid.uuid4().hex[:8]}"


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


def write_outputs(
    target_dir: Path,
    req: ParallelParityRequest,
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
        "parallel_batched_mode": True,
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
        "parallel_batched_mode": True,
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
    build_compact_bundle(target_dir)


def chunk(items: list[dict[str, Any]], size: int) -> list[list[dict[str, Any]]]:
    size = max(1, int(size or 1))
    return [items[i:i + size] for i in range(0, len(items), size)]


def scrape_batch_worker(
    items: list[dict[str, Any]],
    target_run_id: str,
    kind: str,
    batch_index: int,
    playwright_timeout_ms: int,
    pdf_max_pages: int,
) -> dict[str, Any]:
    internal_run_id = f"{target_run_id}/batches/{kind}_batch_{batch_index:03d}"

    try:
        result = run_local_parity_scrape(
            items,
            internal_run_id,
            kind,
            len(items),
            {
                "playwright_timeout_ms": playwright_timeout_ms,
                "pdf_parser": {"max_pages": pdf_max_pages},
            },
        )

        return {
            "ok": True,
            "kind": kind,
            "batch_index": batch_index,
            "pages": result.get("pages", []) or [],
            "failed": result.get("failed", []) or [],
            "item_count": len(items),
        }

    except Exception as e:
        return {
            "ok": False,
            "kind": kind,
            "batch_index": batch_index,
            "pages": [],
            "failed": [
                {
                    "url": item.get("url") or item.get("source_url"),
                    "crawl_status": "failed",
                    "extraction_status": "batch_worker_error",
                    "error": str(e)[:2000],
                    "source_item": item,
                }
                for item in items
            ],
            "item_count": len(items),
        }


def run_stage_parallel(
    job_id: str,
    req: ParallelParityRequest,
    target_dir: Path,
    kind: str,
    batches: list[list[dict[str, Any]]],
    owned_pages: list[dict[str, Any]],
    owned_failed: list[dict[str, Any]],
    external_pages: list[dict[str, Any]],
    external_failed: list[dict[str, Any]],
    owned_requested: int,
    external_requested: int,
):
    if not batches:
        return

    max_workers = max(1, int(req.max_parallel_batches or 1))
    batch_timeout = max(60, int(getattr(req, "batch_timeout_seconds", 300) or 300))

    update_job(job_id, {
        "stage": f"crawl_{kind}",
        "total_batches": len(batches),
        "max_parallel_batches": max_workers,
        "batch_timeout_seconds": batch_timeout,
    })

    completed_batches = 0
    submitted_batches = 0

    with ProcessPoolExecutor(max_workers=max_workers) as executor:
        pending = {}

        def submit_next():
            nonlocal submitted_batches
            if submitted_batches >= len(batches):
                return
            batch_index = submitted_batches + 1
            batch = batches[submitted_batches]
            future = executor.submit(
                scrape_batch_worker,
                batch,
                req.target_run_id,
                kind,
                batch_index,
                req.playwright_timeout_ms,
                req.pdf_max_pages,
            )
            pending[future] = {
                "batch_index": batch_index,
                "batch_size": len(batch),
                "urls": [x.get("url") or x.get("source_url") for x in batch],
                "started_at": now_epoch(),
            }
            submitted_batches += 1

        for _ in range(min(max_workers, len(batches))):
            submit_next()

        while pending:
            done, _ = wait(list(pending.keys()), timeout=5, return_when=FIRST_COMPLETED)

            # Handle completed batches.
            for future in list(done):
                meta = pending.pop(future)
                batch_index = meta["batch_index"]

                try:
                    payload = future.result()
                except Exception as e:
                    payload = {
                        "ok": False,
                        "kind": kind,
                        "batch_index": batch_index,
                        "pages": [],
                        "failed": [
                            {
                                "url": u,
                                "crawl_status": "failed",
                                "extraction_status": "parallel_future_error",
                                "error": str(e)[:2000],
                            }
                            for u in meta["urls"]
                        ],
                    }

                pages = payload.get("pages", []) or []
                failed = payload.get("failed", []) or []

                if kind == "owned":
                    owned_pages.extend(pages)
                    owned_failed.extend(failed)
                else:
                    external_pages.extend(pages)
                    external_failed.extend(failed)

                completed_batches += 1

                write_outputs(
                    target_dir,
                    req,
                    owned_pages,
                    owned_failed,
                    external_pages,
                    external_failed,
                    owned_requested,
                    external_requested,
                )

                update_job(job_id, {
                    "stage": f"crawl_{kind}",
                    "completed_batches": completed_batches,
                    "submitted_batches": submitted_batches,
                    "total_batches": len(batches),
                    "last_completed_batch": batch_index,
                    "last_batch_ok": payload.get("ok", False),
                    "last_batch_pages": len(pages),
                    "last_batch_failed": len(failed),
                    "owned_collected_so_far": len(owned_pages),
                    "owned_failed_so_far": len(owned_failed),
                    "external_collected_so_far": len(external_pages),
                    "external_failed_so_far": len(external_failed),
                })

                submit_next()

            # Mark timed-out batches as failed and move on.
            now = now_epoch()
            for future, meta in list(pending.items()):
                elapsed = now - int(meta.get("started_at") or now)
                if elapsed < batch_timeout:
                    continue

                pending.pop(future)
                batch_index = meta["batch_index"]

                failed = [
                    {
                        "url": u,
                        "crawl_status": "failed",
                        "extraction_status": "batch_timeout",
                        "error": f"Batch {batch_index} exceeded hard timeout of {batch_timeout}s",
                    }
                    for u in meta["urls"]
                ]

                if kind == "owned":
                    owned_failed.extend(failed)
                else:
                    external_failed.extend(failed)

                completed_batches += 1

                write_outputs(
                    target_dir,
                    req,
                    owned_pages,
                    owned_failed,
                    external_pages,
                    external_failed,
                    owned_requested,
                    external_requested,
                )

                update_job(job_id, {
                    "stage": f"crawl_{kind}",
                    "completed_batches": completed_batches,
                    "submitted_batches": submitted_batches,
                    "total_batches": len(batches),
                    "last_completed_batch": batch_index,
                    "last_batch_ok": False,
                    "last_batch_failed": len(failed),
                    "last_batch_timeout": True,
                    "timed_out_urls": meta["urls"],
                    "owned_collected_so_far": len(owned_pages),
                    "owned_failed_so_far": len(owned_failed),
                    "external_collected_so_far": len(external_pages),
                    "external_failed_so_far": len(external_failed),
                })

                # Try to cancel; if the worker keeps running, the process pool may retain it,
                # but this prevents the orchestration loop from waiting indefinitely.
                future.cancel()

                submit_next()


def run_parallel_parity(job_id: str, req: ParallelParityRequest):
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

        write_outputs(
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
            "stage": "inventory_built",
            "owned_url_count": len(owned_items),
            "external_url_count": len(external_items),
            "owned_batch_size": req.owned_batch_size,
            "external_batch_size": req.external_batch_size,
            "max_parallel_batches": req.max_parallel_batches,
        })

        if req.crawl_owned:
            owned_batches = chunk(owned_items, req.owned_batch_size)
            run_stage_parallel(
                job_id,
                req,
                target_dir,
                "owned",
                owned_batches,
                owned_pages,
                owned_failed,
                external_pages,
                external_failed,
                len(owned_items),
                len(external_items),
            )

        if req.crawl_external:
            external_batches = chunk(external_items, req.external_batch_size)
            run_stage_parallel(
                job_id,
                req,
                target_dir,
                "external",
                external_batches,
                owned_pages,
                owned_failed,
                external_pages,
                external_failed,
                len(owned_items),
                len(external_items),
            )

        write_outputs(
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


@router.post("/jobs/full-refresh-parity-parallel")
def create_parallel_parity(req: ParallelParityRequest, x_admin_token: str | None = Header(default=None)):
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

    thread = threading.Thread(target=run_parallel_parity, args=(job_id, req), daemon=True)
    thread.start()

    return {
        "status": "accepted",
        "job_id": job_id,
        "source_run_id": req.source_run_id,
        "target_run_id": req.target_run_id,
        "job_status_url": f"/jobs/parity-parallel/{job_id}",
    }


@router.get("/jobs/parity-parallel/{job_id}")
def get_parallel_parity_job(job_id: str):
    path = job_path(job_id)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Job not found")
    return read_json(path, {})
