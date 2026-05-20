from __future__ import annotations

import json
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Query, Request
from pydantic import BaseModel, Field, ConfigDict
from app.ai_hygiene import hygiene_from_run_dir, is_valid_hygiene

try:
    from app.evidence_jobs import FullRefreshRequest, run_full_refresh, make_job_id, update_job
except Exception:  # pragma: no cover
    FullRefreshRequest = None  # type: ignore
    run_full_refresh = None  # type: ignore
    make_job_id = None  # type: ignore
    update_job = None  # type: ignore

router = APIRouter()

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data/evidence-runs"))
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")

SUCCESS_STATES = {"success", "successful", "completed", "succeeded", "ready"}
IN_PROGRESS_STATES = {"queued", "accepted", "pending", "running", "in_progress", "processing"}
FAILED_STATES = {"failed", "error", "cancelled", "canceled"}
REPORT_READY_STAGES = {"report_bundle_ready"}


def now_epoch() -> int:
    return int(time.time())


def require_admin(token: str | None):
    if ADMIN_TOKEN and token != ADMIN_TOKEN:
        raise HTTPException(status_code=401, detail="Invalid admin token")


def normalise_key(value: Any) -> str:
    return str(value or "").strip().lower().replace("https://", "").replace("http://", "").replace("/", "_").replace(" ", "_").replace(":", "_")


def safe_brand_market_domain(brand: str | None, market: str | None, domain: str | None = None) -> str:
    parts = [normalise_key(brand or "unknown_brand"), normalise_key(market or "unknown_market")]
    if domain:
        parts.append(normalise_key(domain))
    return "__".join(parts)


def read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def redact_sensitive(value: Any) -> Any:
    sensitive_keys = {"access_token", "authorization", "auth", "token", "pat", "pat_token", "api_key", "secret", "password"}
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            lk = str(k).lower()
            if lk in sensitive_keys or lk.endswith("_token") or "access_token" in lk:
                out[k] = "[REDACTED]"
            else:
                out[k] = redact_sensitive(v)
        return out
    if isinstance(value, list):
        return [redact_sensitive(x) for x in value]
    return value


def run_dir(run_id: str) -> Path:
    return DATA_DIR / run_id


def latest_index_dir() -> Path:
    return DATA_DIR / "latest_successful"


def status_dir() -> Path:
    return DATA_DIR / "run_status"


def portfolio_dir() -> Path:
    return DATA_DIR / "portfolios"


def extract_bundle_metadata(bundle: dict[str, Any], fallback_run_id: str | None = None) -> dict[str, Any]:
    meta = bundle.get("metadata") if isinstance(bundle.get("metadata"), dict) else {}
    executive = bundle.get("executive") if isinstance(bundle.get("executive"), dict) else {}
    return {
        "run_id": bundle.get("run_id") or meta.get("run_id") or fallback_run_id,
        "brand": bundle.get("brand") or meta.get("brand") or executive.get("brand"),
        "market": bundle.get("market") or meta.get("market") or executive.get("market"),
        "domain": bundle.get("domain") or meta.get("domain"),
        "schema_version": bundle.get("schema_version"),
        "contract_version": bundle.get("contract_version"),
    }


def first_value(*values: Any) -> Any:
    for value in values:
        if value is not None and value != "":
            return value
    return None


def as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def url_key(value: Any) -> str:
    return str(value or "").strip().split("#", 1)[0].rstrip("/").lower()


def page_url(page: dict[str, Any]) -> str:
    return str(first_value(page.get("url"), page.get("page_url"), page.get("target_url"), page.get("canonical_url"), page.get("final_url"), "") or "")


def source_domain(url: str) -> str:
    try:
        from urllib.parse import urlparse

        return urlparse(url).netloc.lower()
    except Exception:
        return ""


def score_value(page: dict[str, Any]) -> Any:
    return first_value(page.get("current_geo_score_120"), page.get("score_120"), page.get("geo_readiness_score"), page.get("geo_score_120"))


def has_scored_signal(page: dict[str, Any]) -> bool:
    return score_value(page) is not None or page.get("geo_analysis_ready") is True


def geo_dimensions(page: dict[str, Any]) -> dict[str, Any]:
    value = first_value(page.get("geo_dimensions"), page.get("dimensions"), page.get("dimension_scores"))
    return value if isinstance(value, dict) else {}


def technical_signals(page: dict[str, Any]) -> dict[str, Any]:
    tech = page.get("technical_signals") if isinstance(page.get("technical_signals"), dict) else {}
    schema_types = first_value(tech.get("schema_types"), tech.get("schemaTypes"), page.get("schema_types"), page.get("schema_types_detected"), [])
    block_count = first_value(tech.get("json_ld_block_count"), tech.get("jsonLdBlockCount"), page.get("json_ld_block_count"))
    json_ld_present = first_value(tech.get("json_ld_present"), tech.get("jsonLdPresent"), page.get("json_ld_present"))
    out = {
        **tech,
        "json_ld_present": json_ld_present,
        "json_ld_block_count": block_count,
        "schema_types": schema_types if isinstance(schema_types, list) else [],
        "crawl_status": first_value(tech.get("crawl_status"), tech.get("crawlStatus"), page.get("crawl_status")),
        "canonical_url": first_value(tech.get("canonical_url"), tech.get("canonicalUrl"), page.get("canonical_url"), page.get("final_url")),
        "word_count": first_value(tech.get("word_count"), tech.get("wordCount"), page.get("word_count")),
    }
    return {k: v for k, v in out.items() if v is not None and v != ""}


def related_queries_from(value: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in as_list(value):
        if not isinstance(row, dict):
            continue
        related = {
            "query_id": first_value(row.get("query_id"), row.get("id")),
            "query": first_value(row.get("query"), row.get("text")),
            "visibility_status": first_value(row.get("visibility_status"), row.get("status")),
        }
        if related.get("query_id") or related.get("query"):
            out.append(related)
    return out


def canonical_owned_row(page: dict[str, Any], *, query_mapped: bool = False, related_queries: list[dict[str, Any]] | None = None) -> dict[str, Any] | None:
    url = page_url(page)
    if not url:
        return None
    extract = page.get("owned_page_extract") if isinstance(page.get("owned_page_extract"), dict) else {}
    tech = technical_signals(page)
    score = score_value(page)
    row = {
        "url": url,
        "title": first_value(extract.get("title"), page.get("title"), page.get("page_title"), ""),
        "current_geo_score_120": score if score is not None else 0,
        "geo_dimensions": geo_dimensions(page),
        "query_mapped": bool(page.get("query_mapped") is True or page.get("queryMapped") is True or query_mapped),
        "inventory_source": first_value(page.get("inventory_source"), page.get("inventorySource"), "query_mapped" if query_mapped else "sitemap_inventory"),
        "related_queries": related_queries if related_queries is not None else related_queries_from(first_value(page.get("related_queries"), page.get("related_query_evidence"), page.get("mapped_queries"))),
        "technical_signals": tech,
        "json_ld_present": tech.get("json_ld_present"),
        "json_ld_block_count": tech.get("json_ld_block_count"),
        "schema_types": tech.get("schema_types", []),
    }
    for key in ("score_band", "crawl_status", "extraction_status", "geo_analysis_ready"):
        if page.get(key) is not None:
            row[key] = page.get(key)
    return {k: v for k, v in row.items() if v is not None}


def bundle_query_mapping(bundle: dict[str, Any]) -> tuple[set[str], dict[str, list[dict[str, Any]]]]:
    mapped: set[str] = set()
    related_by_url: dict[str, list[dict[str, Any]]] = {}
    for row in as_list(bundle.get("query_workbench")):
        if not isinstance(row, dict):
            continue
        related = {
            "query_id": first_value(row.get("query_id"), row.get("id")),
            "query": row.get("query"),
            "visibility_status": (row.get("current_ai_visibility") or {}).get("status") if isinstance(row.get("current_ai_visibility"), dict) else None,
        }
        for page in as_list(row.get("mapped_owned_urls")):
            if not isinstance(page, dict):
                continue
            key = url_key(page_url(page))
            if not key:
                continue
            mapped.add(key)
            if related.get("query_id") or related.get("query"):
                related_by_url.setdefault(key, []).append(related)
    for rec_key in ("page_level_cms_recommendations", "cms_recommendations"):
        for rec in as_list(bundle.get(rec_key)):
            if not isinstance(rec, dict):
                continue
            key = url_key(first_value(rec.get("target_url"), rec.get("targetUrl"), rec.get("url")))
            if key:
                mapped.add(key)
    return mapped, related_by_url


def owned_rows_from_run_artifacts(run_id: str) -> list[dict[str, Any]]:
    rdir = run_dir(run_id)
    rows: list[dict[str, Any]] = []
    for path in [
        rdir / "owned_pages_full.json",
        rdir / "bodhi_bundle.json",
        rdir / "compact_bundle.json",
        rdir / "evidence_scope.json",
        rdir / "audit_context.json",
    ]:
        payload = read_json(path, {}) or {}
        if not isinstance(payload, dict):
            continue
        sources: list[Any] = []
        if path.name == "bodhi_bundle.json":
            owned = payload.get("owned_pages_full") if isinstance(payload.get("owned_pages_full"), dict) else {}
            sources.extend(as_list(owned.get("pages")))
        elif path.name == "compact_bundle.json":
            files = payload.get("files") if isinstance(payload.get("files"), dict) else {}
            owned = files.get("owned_pages_full") if isinstance(files.get("owned_pages_full"), dict) else payload.get("owned_pages_full")
            if isinstance(owned, dict):
                sources.extend(as_list(owned.get("pages")))
        else:
            sources.extend(as_list(payload.get("pages")))
            sources.extend(as_list(payload.get("owned_pages")))
            sources.extend(as_list(payload.get("owned_urls")))
        rows.extend([page for page in sources if isinstance(page, dict) and has_scored_signal(page)])
    return rows


def source_citations_from_run_artifacts(run_id: str) -> list[dict[str, Any]]:
    rdir = run_dir(run_id)
    citations: list[dict[str, Any]] = []
    google = read_json(rdir / "google_ai_mode_compact.json", {}) or {}
    if isinstance(google, dict):
        for row in as_list(first_value(google.get("rows"), google.get("queries"))):
            if not isinstance(row, dict):
                continue
            for index, ref in enumerate(as_list(first_value(row.get("top_citations"), row.get("top_cited_sources"), row.get("references"))), start=1):
                if isinstance(ref, str):
                    ref = {"url": ref}
                if not isinstance(ref, dict):
                    continue
                url = str(first_value(ref.get("url"), ref.get("source_url"), ref.get("link"), "") or "")
                if not url:
                    continue
                citations.append({
                    "query_id": row.get("query_id"),
                    "query": row.get("query"),
                    "url": url,
                    "source_url": url,
                    "domain": first_value(ref.get("domain"), ref.get("source_domain"), source_domain(url)),
                    "source_domain": first_value(ref.get("source_domain"), ref.get("domain"), source_domain(url)),
                    "source_type": first_value(ref.get("source_type"), ref.get("sourceType"), "external_citation"),
                    "title": first_value(ref.get("title"), ref.get("source_name"), source_domain(url)),
                    "snippet": first_value(ref.get("snippet"), ref.get("citation_text"), ref.get("text"), ""),
                    "citation_text": first_value(ref.get("citation_text"), ref.get("snippet"), ref.get("text"), ""),
                    "rank": first_value(ref.get("rank"), ref.get("citation_position"), index),
                    "citation_position": first_value(ref.get("citation_position"), ref.get("rank"), index),
                })
    evidence = read_json(rdir / "evidence_scope.json", {}) or {}
    if isinstance(evidence, dict):
        for ref in as_list(evidence.get("ai_citations")):
            if not isinstance(ref, dict):
                continue
            url = str(first_value(ref.get("url"), ref.get("source_url"), ref.get("link"), "") or "")
            if not url:
                continue
            citations.append({
                "query_id": ref.get("query_id"),
                "query": ref.get("query"),
                "url": url,
                "source_url": url,
                "domain": first_value(ref.get("domain"), ref.get("source_domain"), source_domain(url)),
                "source_domain": first_value(ref.get("source_domain"), ref.get("domain"), source_domain(url)),
                "source_type": first_value(ref.get("source_type"), ref.get("sourceType"), "external_citation"),
                "title": first_value(ref.get("title"), ref.get("source_name"), source_domain(url)),
                "snippet": first_value(ref.get("snippet"), ref.get("citation_text"), ref.get("text"), ""),
                "citation_text": first_value(ref.get("citation_text"), ref.get("snippet"), ref.get("text"), ""),
                "rank": first_value(ref.get("rank"), ref.get("citation_position")),
                "citation_position": first_value(ref.get("citation_position"), ref.get("rank")),
            })
    deduped: dict[tuple[str, str, str], dict[str, Any]] = {}
    for citation in citations:
        key = (str(citation.get("query_id") or ""), url_key(citation.get("url")), str(citation.get("citation_position") or ""))
        deduped.setdefault(key, citation)
    return list(deduped.values())


def is_report_ready_manifest(manifest: dict[str, Any]) -> bool:
    stage = str(manifest.get("stage") or "").lower()
    status = str(manifest.get("status") or "").lower()
    return bool(manifest.get("dashboard_ready", True)) and stage in REPORT_READY_STAGES and status in SUCCESS_STATES



def _list_len(value: Any) -> int:
    return len(value) if isinstance(value, list) else 0


def has_recognised_report_payload(bundle: Any) -> bool:
    """Return True only for dashboard-ready frontend report bundles.

    The Auditor can sometimes post a status/error shaped JSON object after a smoke
    run with no portfolio/query evidence. Those objects should be stored for
    debugging but must not replace the latest successful report because the
    frontend cannot render them as an AI visibility dashboard.
    """
    if not isinstance(bundle, dict):
        return False

    # Direct canonical report fields.
    list_fields = [
        "query_workbench",
        "queries",
        "owned_url_readiness",
        "owned_pages",
        "cms_recommendations",
        "page_level_cms_recommendations",
        "action_checklist",
    ]
    if any(_list_len(bundle.get(k)) > 0 for k in list_fields):
        return True

    # Nested/common alternates used by older and newer bundles.
    executive = bundle.get("executive") if isinstance(bundle.get("executive"), dict) else {}
    if executive and (executive.get("headline") or executive.get("summary") or executive.get("ai_visibility_score") is not None):
        # Executive-only bundles are not enough; require some drilldown evidence too.
        pass

    source_landscape = bundle.get("source_landscape") if isinstance(bundle.get("source_landscape"), dict) else {}
    if _list_len(source_landscape.get("sources")) > 0 or _list_len(source_landscape.get("citation_domains")) > 0:
        return True

    visibility = bundle.get("visibility_matrix") if isinstance(bundle.get("visibility_matrix"), dict) else {}
    if _list_len(visibility.get("queries")) > 0 or _list_len(visibility.get("rows")) > 0:
        return True

    # Preview-node style legacy shape.
    preview = bundle.get("preview") if isinstance(bundle.get("preview"), dict) else {}
    if _list_len(preview.get("tiles")) > 0:
        return True
    if _list_len(bundle.get("tiles")) > 0:
        return True

    return False


def load_valid_report_bundle(run_id: str) -> dict[str, Any] | None:
    bundle = load_report_bundle(run_id)
    if bundle and has_recognised_report_payload(bundle):
        return bundle
    return None

def update_latest_successful_index(manifest: dict[str, Any]) -> None:
    brand = manifest.get("brand")
    market = manifest.get("market")
    domain = manifest.get("domain")
    if not brand or not market:
        return
    latest_index_dir().mkdir(parents=True, exist_ok=True)
    keys = [
        safe_brand_market_domain(brand, market),
    ]
    if domain:
        keys.append(safe_brand_market_domain(brand, market, domain))
    for key in keys:
        write_json(latest_index_dir() / f"{key}.json", manifest)


def write_run_status(run_id: str, status: str, patch: dict[str, Any] | None = None) -> dict[str, Any]:
    current = read_json(status_dir() / f"{run_id}.json", {}) or {}
    current.update(redact_sensitive(patch or {}))
    current["run_id"] = run_id
    current["status"] = status
    current["updated_at_epoch"] = now_epoch()
    if status in SUCCESS_STATES:
        current.setdefault("completed_at_epoch", now_epoch())
    elif status in FAILED_STATES:
        current.setdefault("failed_at_epoch", now_epoch())
    elif status in IN_PROGRESS_STATES:
        current.setdefault("started_at_epoch", now_epoch())
    write_json(status_dir() / f"{run_id}.json", current)
    # Keep a copy inside the run folder when a run_id directory exists.
    if run_dir(run_id).exists():
        write_json(run_dir(run_id) / "run_status.json", current)
    return current


def load_run_status(run_id: str) -> dict[str, Any] | None:
    status = read_json(status_dir() / f"{run_id}.json")
    if status:
        return status
    return read_json(run_dir(run_id) / "run_status.json")


def report_bundle_path(run_id: str) -> Path:
    return run_dir(run_id) / "frontend_report_bundle.json"


def load_report_bundle(run_id: str) -> dict[str, Any] | None:
    bundle = read_json(report_bundle_path(run_id))
    if isinstance(bundle, dict):
        # Read paths must not rewrite the full frontend bundle. Persisting during
        # GET /runs/latest, /reports/history or /runs/status repeatedly reloads
        # large crawl artifacts and can amplify memory usage under polling.
        return enrich_report_bundle(run_id, bundle, persist=False)
    return bundle


def ensure_ai_hygiene(run_id: str, bundle: dict[str, Any], *, persist: bool = False) -> dict[str, Any]:
    if is_valid_hygiene(bundle.get("ai_discoverability_hygiene")):
        return bundle
    enriched = dict(bundle)
    executive = enriched.get("executive") if isinstance(enriched.get("executive"), dict) else {}
    nested = executive.get("ai_discoverability_hygiene") if isinstance(executive, dict) else None
    hygiene = nested if is_valid_hygiene(nested) else hygiene_from_run_dir(run_dir(run_id))
    enriched["ai_discoverability_hygiene"] = hygiene
    enriched.setdefault("site_ai_hygiene", hygiene)
    if persist:
        write_json(report_bundle_path(run_id), enriched)
    return enriched


def enrich_report_bundle(run_id: str, bundle: dict[str, Any], *, persist: bool = False) -> dict[str, Any]:
    enriched = ensure_ai_hygiene(run_id, dict(bundle), persist=False)
    mapped_keys, related_by_url = bundle_query_mapping(enriched)
    rows_by_url: dict[str, dict[str, Any]] = {}

    for row in as_list(first_value(enriched.get("owned_url_readiness"), enriched.get("owned_readiness"), enriched.get("owned_pages"))):
        if not isinstance(row, dict):
            continue
        key = url_key(page_url(row))
        if not key:
            continue
        canonical = canonical_owned_row(
            row,
            query_mapped=key in mapped_keys,
            related_queries=related_by_url.get(key) or related_queries_from(first_value(row.get("related_queries"), row.get("related_query_evidence"))),
        )
        if canonical:
            rows_by_url[key] = canonical

    for page in owned_rows_from_run_artifacts(run_id):
        key = url_key(page_url(page))
        if not key or key in rows_by_url:
            continue
        canonical = canonical_owned_row(page, query_mapped=key in mapped_keys, related_queries=related_by_url.get(key, []))
        if canonical:
            rows_by_url[key] = canonical

    owned_rows = list(rows_by_url.values())
    enriched["owned_url_readiness"] = owned_rows
    enriched["owned_pages_scoreable"] = len(owned_rows)
    enriched["owned_query_mapped_unique"] = sum(1 for row in owned_rows if row.get("query_mapped") is True)

    executive = enriched.get("executive")
    if isinstance(executive, dict):
        headline = executive.setdefault("headline_metrics", {})
        if isinstance(headline, dict) and owned_rows:
            headline["owned_page_count"] = len(owned_rows)

    citations = source_citations_from_run_artifacts(run_id)
    if citations:
        landscape = enriched.setdefault("source_landscape", {})
        if isinstance(landscape, dict) and not isinstance(landscape.get("source_citations"), list):
            landscape["source_citations"] = citations

    if persist:
        write_json(report_bundle_path(run_id), enriched)
    return enriched


def scan_latest_successful(brand: str | None, market: str | None, domain: str | None = None) -> dict[str, Any] | None:
    candidates: list[dict[str, Any]] = []
    for child in DATA_DIR.iterdir() if DATA_DIR.exists() else []:
        if not child.is_dir() or child.name.startswith("_") or child.name in {"latest", "latest_successful", "run_status", "portfolios"}:
            continue
        manifest = read_json(child / "report_manifest.json") or read_json(child / "run_manifest.json") or {}
        if not is_report_ready_manifest(manifest):
            continue
        if brand and normalise_key(manifest.get("brand")) != normalise_key(brand):
            continue
        if market and normalise_key(manifest.get("market")) != normalise_key(market):
            continue
        if domain and manifest.get("domain") and normalise_key(manifest.get("domain")) != normalise_key(domain):
            continue
        if not report_bundle_path(child.name).exists():
            continue
        candidates.append(manifest)
    candidates.sort(key=lambda x: x.get("completed_at_epoch") or x.get("created_at_epoch") or 0, reverse=True)
    return candidates[0] if candidates else None




def _epoch_from_any(value: Any) -> int:
    try:
        return int(value or 0)
    except Exception:
        return 0

def _report_history_row(manifest: dict[str, Any], status: dict[str, Any] | None = None) -> dict[str, Any]:
    status = status or {}
    run_id = str(manifest.get("run_id") or status.get("run_id") or "")
    # Keep history lightweight. Loading every full frontend bundle just to render
    # the Previous Runs table can retain large JSON objects and crawl snippets in
    # memory while the frontend polls refresh status.
    query_count = status.get("query_count") or manifest.get("query_count")
    owned_pages_scoreable = (
        status.get("owned_pages_scoreable")
        or manifest.get("owned_pages_scoreable")
        or manifest.get("owned_url_readiness_count")
    )
    owned_query_mapped_unique = (
        status.get("owned_query_mapped_unique")
        or manifest.get("owned_query_mapped_unique")
    )
    hygiene = manifest.get("ai_hygiene") or status.get("ai_hygiene")
    return {
        "run_id": run_id,
        "brand": manifest.get("brand") or status.get("brand"),
        "market": manifest.get("market") or status.get("market"),
        "domain": manifest.get("domain") or status.get("domain"),
        "stage": status.get("stage") or manifest.get("stage"),
        "status": status.get("status") or manifest.get("status"),
        "dashboard_ready": bool(manifest.get("dashboard_ready", True)),
        "created_at_epoch": manifest.get("created_at_epoch") or status.get("created_at_epoch") or status.get("started_at_epoch"),
        "completed_at_epoch": manifest.get("completed_at_epoch") or status.get("completed_at_epoch"),
        "query_count": query_count,
        "owned_pages_scoreable": owned_pages_scoreable,
        "owned_inventory_selected": status.get("owned_inventory_selected") or status.get("owned_url_count"),
        "owned_query_mapped_unique": owned_query_mapped_unique,
        "external_pages_scoreable": status.get("external_pages_scoreable"),
        "citation_count": status.get("external_citation_count") or status.get("serpapi_citation_count"),
        "serpapi_enabled": bool((status.get("request") or {}).get("run_serpapi") or (status.get("request") or {}).get("enable_serpapi")),
        "serpapi_query_count": status.get("serpapi_query_count") or status.get("serpapi_rows"),
        "crawl_owned": bool((status.get("request") or {}).get("crawl_owned") or status.get("owned_pages_attempted")),
        "crawl_external": bool((status.get("request") or {}).get("crawl_external") or status.get("external_pages_attempted")),
        "crawl_success_rate": status.get("crawl_success_rate"),
        "portfolio_id": status.get("query_portfolio_id") or manifest.get("query_portfolio_id"),
        "source_run_id": status.get("source_run_id"),
        "ai_hygiene": hygiene,
        "label": f"{manifest.get('brand') or status.get('brand') or 'Brand'} / {manifest.get('market') or status.get('market') or 'Market'} — {run_id}",
    }

def _scan_report_history(brand: str | None, market: str | None, domain: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not DATA_DIR.exists():
        return []
    for child in DATA_DIR.iterdir():
        if not child.is_dir() or child.name.startswith("_") or child.name in {"latest", "latest_successful", "run_status", "portfolios"}:
            continue
        manifest = read_json(child / "report_manifest.json") or read_json(child / "run_manifest.json") or {}
        if not manifest:
            continue
        if not is_report_ready_manifest(manifest):
            continue
        if not bool(manifest.get("dashboard_ready", True)):
            continue
        if brand and normalise_key(manifest.get("brand")) != normalise_key(brand):
            continue
        if market and normalise_key(manifest.get("market")) != normalise_key(market):
            continue
        if domain and manifest.get("domain") and normalise_key(manifest.get("domain")) != normalise_key(domain):
            continue
        if not report_bundle_path(child.name).exists():
            continue
        status = load_run_status(child.name) or {}
        rows.append(_report_history_row(manifest, status))
    rows.sort(key=lambda x: _epoch_from_any(x.get("completed_at_epoch") or x.get("created_at_epoch")), reverse=True)
    return rows[: max(1, min(int(limit or 20), 100))]


class RunStatusRequest(BaseModel):
    status: str
    brand: str | None = None
    market: str | None = None
    domain: str | None = None
    task_id: str | None = None
    bodhi_run_id: str | None = None
    evidence_run_id: str | None = None
    message: str | None = None
    error: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class PortfolioRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    portfolio_id: str | None = None
    schema_version: str | None = "brand_topic_query_portfolio.v1"
    deepresearch_status: str | None = None
    brand: str
    market: str
    domain: str | None = None
    portfolio_source: str = "synthetic_deepresearch"
    topics: list[Any] = Field(default_factory=list)
    queries: list[Any] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class RefreshEvidenceRequest(BaseModel):
    brand: str = "Nissan"
    market: str = "Japan"
    domain: str | None = None
    evidence_service_url: str | None = None
    source_run_id: str | None = None
    target_run_id: str | None = None
    mode: str = "refresh_owned_pages"
    run_mode: str | None = None

    # Query portfolio orchestration. Synthetic mode is handled by Railway evidence
    # service through the Bodhi Brand Topic Query Builder task.
    query_portfolio_mode: str = "reuse"
    query_portfolio_id: str | None = None
    manual_queries_json: str | None = None
    topics_json: str | None = None
    seed_topics: str | None = None
    topic_count: int = 8
    queries_per_topic: int = 6
    language: str = "English"
    portfolio_goal: str | None = None

    # Sitemap / mapping controls.
    sitemap_url: str | None = None
    sitemap_max_urls: int = 2000
    query_limit: int = 50
    max_owned_pages_per_query: int = 3
    max_external_citations_per_query: int = 3
    max_owned_urls: int = 60
    max_external_urls: int = 30

    # Evidence execution flags owned by Railway evidence service.
    crawl_owned: bool = True
    crawl_external: bool = False
    enable_owned_crawl: bool | None = None
    enable_external_crawl: bool | None = None
    run_serpapi: bool = False
    enable_serpapi: bool | None = None
    use_existing_google_ai_mode: bool = True
    trigger_auditor: bool = True

    metadata: dict[str, Any] = Field(default_factory=dict)


@router.post("/runs/{run_id}/report-bundle")
async def store_report_bundle(run_id: str, request: Request, x_admin_token: str | None = Header(default=None)):
    # Report writes can be protected by ADMIN_TOKEN. If ADMIN_TOKEN is unset, local/dev mode remains open.
    require_admin(x_admin_token)
    bundle = await request.json()
    if not isinstance(bundle, dict):
        raise HTTPException(status_code=400, detail="Report bundle must be a JSON object")
    bundle = enrich_report_bundle(run_id, bundle)

    is_dashboard_ready = has_recognised_report_payload(bundle)

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    rdir = run_dir(run_id)
    rdir.mkdir(parents=True, exist_ok=True)
    write_json(report_bundle_path(run_id), bundle)

    meta = extract_bundle_metadata(bundle, fallback_run_id=run_id)
    manifest = {
        "status": "completed" if is_dashboard_ready else "completed_invalid_report",
        "stage": "report_bundle_ready" if is_dashboard_ready else "report_bundle_invalid",
        "run_id": run_id,
        "brand": meta.get("brand"),
        "market": meta.get("market"),
        "domain": meta.get("domain"),
        "schema_version": meta.get("schema_version"),
        "contract_version": meta.get("contract_version"),
        "report_bundle": str(report_bundle_path(run_id)),
        "created_at_epoch": now_epoch(),
        "completed_at_epoch": now_epoch(),
        "query_count": len(bundle.get("query_workbench") or bundle.get("queries") or []),
        "owned_pages_scoreable": len(bundle.get("owned_url_readiness") or []),
        "owned_query_mapped_unique": sum(1 for row in (bundle.get("owned_url_readiness") or []) if isinstance(row, dict) and row.get("query_mapped") is True),
        "source_citation_count": len(((bundle.get("source_landscape") or {}).get("source_citations") or []) if isinstance(bundle.get("source_landscape"), dict) else []),
    }
    manifest["dashboard_ready"] = is_dashboard_ready
    if not is_dashboard_ready:
        manifest["validation_error"] = "Stored report bundle does not contain a recognised dashboard payload; it was not promoted to latest successful."

    write_json(rdir / "report_manifest.json", manifest)
    write_json(rdir / "run_manifest.json", {**(read_json(rdir / "run_manifest.json", {}) or {}), **manifest})
    write_run_status(run_id, "completed" if is_dashboard_ready else "completed_invalid_report", manifest)
    if is_dashboard_ready:
        update_latest_successful_index(manifest)
    return {"status": "stored" if is_dashboard_ready else "stored_not_promoted", "manifest": manifest}


@router.get("/runs/{run_id}/report-bundle")
def get_report_bundle(run_id: str):
    bundle = load_report_bundle(run_id)
    if not bundle:
        raise HTTPException(status_code=404, detail=f"No frontend report bundle found for run_id={run_id}")
    return bundle


@router.get("/runs/latest/report-bundle")
def get_latest_report_bundle(brand: str = Query(...), market: str = Query(...), domain: str | None = None):
    key_candidates = []
    if domain:
        key_candidates.append(safe_brand_market_domain(brand, market, domain))
    key_candidates.append(safe_brand_market_domain(brand, market))

    manifest = None
    for key in key_candidates:
        manifest = read_json(latest_index_dir() / f"{key}.json")
        if manifest and is_report_ready_manifest(manifest):
            break
        manifest = None
    if not manifest:
        manifest = scan_latest_successful(brand, market, domain)
    if not manifest:
        raise HTTPException(status_code=404, detail="No latest successful report bundle found")

    bundle = load_valid_report_bundle(manifest["run_id"])
    if not bundle:
        # The latest index may point at a malformed/empty smoke-test output.
        # Fall back to scanning for the most recent dashboard-ready report instead
        # of returning a frontend-breaking payload.
        fallback = scan_latest_successful(brand, market, domain)
        if fallback and fallback.get("run_id") != manifest.get("run_id"):
            bundle = load_valid_report_bundle(fallback["run_id"])
            if bundle:
                return bundle
        raise HTTPException(status_code=404, detail="Latest manifest exists but no dashboard-ready report bundle was found")
    return bundle




@router.get("/reports/history")
def get_report_history(brand: str | None = None, market: str | None = None, domain: str | None = None, limit: int = 20):
    rows = _scan_report_history(brand, market, domain, limit)
    return {
        "status": "ok",
        "count": len(rows),
        "runs": rows,
    }


@router.get("/reports/latest-successful")
def get_latest_successful_report(brand: str = Query(...), market: str = Query(...), domain: str | None = None):
    return get_latest_report_bundle(brand=brand, market=market, domain=domain)


@router.get("/reports/{run_id}")
def get_report_by_run_id(run_id: str):
    return get_report_bundle(run_id)


@router.post("/runs/{run_id}/status")
def post_run_status(run_id: str, req: RunStatusRequest, x_admin_token: str | None = Header(default=None)):
    require_admin(x_admin_token)
    patch = req.model_dump(exclude={"status"})
    return write_run_status(run_id, req.status, patch)


@router.get("/runs/{run_id}/status")
def get_single_run_status(run_id: str):
    status = load_run_status(run_id)
    if not status:
        raise HTTPException(status_code=404, detail=f"No status found for run_id={run_id}")
    return status


@router.get("/runs/status")
def get_run_statuses(brand: str | None = None, market: str | None = None, domain: str | None = None, limit: int = 20):
    rows: list[dict[str, Any]] = []
    if status_dir().exists():
        for path in status_dir().glob("*.json"):
            status = read_json(path, {}) or {}
            if brand and normalise_key(status.get("brand")) != normalise_key(brand):
                continue
            if market and normalise_key(status.get("market")) != normalise_key(market):
                continue
            if domain and status.get("domain") and normalise_key(status.get("domain")) != normalise_key(domain):
                continue
            rows.append(status)
    rows.sort(key=lambda x: x.get("updated_at_epoch") or x.get("created_at_epoch") or 0, reverse=True)
    latest_successful = scan_latest_successful(brand, market, domain)
    latest_active = next((r for r in rows if str(r.get("status", "")).lower() in IN_PROGRESS_STATES or str(r.get("stage", "")).lower() == "evidence_ready"), None)
    return {
        "status": "ok",
        "latest_successful_run_id": (latest_successful or {}).get("run_id"),
        "active_run": latest_active,
        "runs": rows[: max(1, min(limit, 100))],
    }


@router.post("/portfolios")
def store_query_portfolio(req: PortfolioRequest, x_admin_token: str | None = Header(default=None)):
    require_admin(x_admin_token)
    portfolio_id = req.portfolio_id or f"portfolio_{normalise_key(req.brand)}_{normalise_key(req.market)}_{now_epoch()}_{uuid.uuid4().hex[:6]}"
    payload = req.model_dump(exclude_none=True)
    payload["portfolio_id"] = portfolio_id
    payload.setdefault("schema_version", "brand_topic_query_portfolio.v1")
    payload["created_at_epoch"] = now_epoch()
    write_json(portfolio_dir() / f"{portfolio_id}.json", payload)

    latest_key = safe_brand_market_domain(req.brand, req.market, req.domain)
    write_json(portfolio_dir() / "latest" / f"{latest_key}.json", payload)
    write_json(portfolio_dir() / "latest" / f"{safe_brand_market_domain(req.brand, req.market)}.json", payload)
    return {"status": "stored", "portfolio_id": portfolio_id, "portfolio": payload}


@router.get("/portfolios/latest")
def get_latest_query_portfolio(brand: str = Query(...), market: str = Query(...), domain: str | None = None):
    # IMPORTANT: this static route must be registered before /portfolios/{portfolio_id}.
    # Otherwise FastAPI treats "latest" as a portfolio_id and returns
    # "No portfolio found for portfolio_id=latest".
    keys = []
    if domain:
        keys.append(safe_brand_market_domain(brand, market, domain))
    keys.append(safe_brand_market_domain(brand, market))
    for key in keys:
        payload = read_json(portfolio_dir() / "latest" / f"{key}.json")
        if payload:
            return payload
    raise HTTPException(status_code=404, detail="No latest portfolio found")


@router.get("/portfolios/{portfolio_id}")
def get_query_portfolio(portfolio_id: str):
    payload = read_json(portfolio_dir() / f"{portfolio_id}.json")
    if not payload:
        raise HTTPException(status_code=404, detail=f"No portfolio found for portfolio_id={portfolio_id}")
    return payload


@router.post("/refresh/evidence")
def trigger_refresh_evidence(req: RefreshEvidenceRequest, x_admin_token: str | None = Header(default=None)):
    require_admin(x_admin_token)
    # Phase 2 orchestration lives in Railway evidence service. It can trigger the
    # Bodhi portfolio workflow, wait for completion, ingest the portfolio, run
    # SerpAPI/crawling, and optionally trigger the Bodhi Auditor workflow.
    from app.refresh_orchestrator import start_phase2_refresh

    payload = req.model_dump()
    result = start_phase2_refresh(payload)
    result["note"] = "Dashboard should keep showing latest successful report until this refresh run completes and Bodhi stores a new frontend_report_bundle."
    return result
