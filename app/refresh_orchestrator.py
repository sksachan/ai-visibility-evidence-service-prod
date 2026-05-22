from __future__ import annotations

import json
import os
import re
import html
import threading
import time
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urljoin

import requests

from app.bodhi_client import BodhiClient
from app.evidence_jobs import FullRefreshRequest, SerpApiJobRequest, run_full_refresh, run_serpapi_collection, make_job_id, update_job
from app.portfolio_ingestion import extract_query_portfolio
from app.compact_normaliser import canonicalise_bundle_files
from app.ai_hygiene import check_site_ai_hygiene

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data/evidence-runs"))


def now_epoch() -> int:
    return int(time.time())


def normalise_key(value: Any) -> str:
    return str(value or "").strip().lower().replace("https://", "").replace("http://", "").replace("/", "_").replace(" ", "_").replace(":", "_")


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def redact_sensitive(value: Any) -> Any:
    """Remove access tokens and other credentials before writing status files."""
    sensitive_keys = {"access_token", "authorization", "auth", "token", "pat", "pat_token", "api_key", "secret", "password"}
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            lk = str(k).lower()
            if lk in sensitive_keys or lk.endswith("_token") or "access_token" in lk:
                out[k] = "[REDACTED]"
            elif lk == "exec_metadata" and isinstance(v, dict):
                red = redact_sensitive(v)
                if isinstance(red, dict):
                    out[k] = red
                else:
                    out[k] = "[REDACTED]"
            else:
                out[k] = redact_sensitive(v)
        return out
    if isinstance(value, list):
        return [redact_sensitive(x) for x in value]
    return value


def status_dir() -> Path:
    return DATA_DIR / "run_status"


def run_dir(run_id: str) -> Path:
    return DATA_DIR / run_id


def portfolio_dir() -> Path:
    return DATA_DIR / "portfolios"


def write_run_status(run_id: str, status: str, patch: dict[str, Any] | None = None) -> dict[str, Any]:
    current = read_json(status_dir() / f"{run_id}.json", {}) or {}
    current.update(redact_sensitive(patch or {}))
    current["run_id"] = run_id
    current["status"] = status
    current["updated_at_epoch"] = now_epoch()
    write_json(status_dir() / f"{run_id}.json", current)
    if run_dir(run_id).exists():
        write_json(run_dir(run_id) / "run_status.json", current)
    return current


def store_portfolio(portfolio: dict[str, Any], fallback_brand: str, fallback_market: str, fallback_domain: str | None = None) -> dict[str, Any]:
    pid = portfolio.get("portfolio_id") or f"portfolio_{normalise_key(fallback_brand)}_{normalise_key(fallback_market)}_{now_epoch()}_{uuid.uuid4().hex[:6]}"
    portfolio["portfolio_id"] = pid
    portfolio.setdefault("schema_version", "brand_topic_query_portfolio.v1")
    portfolio.setdefault("brand", fallback_brand)
    portfolio.setdefault("market", fallback_market)
    if fallback_domain and not portfolio.get("domain"):
        portfolio["domain"] = fallback_domain
    portfolio.setdefault("created_at_epoch", now_epoch())
    write_json(portfolio_dir() / f"{pid}.json", portfolio)
    for key in [
        f"{normalise_key(portfolio.get('brand'))}__{normalise_key(portfolio.get('market'))}",
        f"{normalise_key(portfolio.get('brand'))}__{normalise_key(portfolio.get('market'))}__{normalise_key(portfolio.get('domain'))}",
    ]:
        write_json(portfolio_dir() / "latest" / f"{key}.json", portfolio)
    return portfolio


def load_portfolio(portfolio_id: str) -> dict[str, Any] | None:
    return read_json(portfolio_dir() / f"{portfolio_id}.json")


def load_latest_portfolio(brand: str | None, market: str | None, domain: str | None = None) -> dict[str, Any] | None:
    keys: list[str] = []
    if domain:
        keys.append(f"{normalise_key(brand)}__{normalise_key(market)}__{normalise_key(domain)}")
    keys.append(f"{normalise_key(brand)}__{normalise_key(market)}")
    for key in keys:
        payload = read_json(portfolio_dir() / "latest" / f"{key}.json")
        if isinstance(payload, dict) and isinstance(payload.get("queries"), list) and payload.get("queries"):
            payload.setdefault("schema_version", "brand_topic_query_portfolio.v1")
            return payload
    return None



def find_recent_portfolio(brand: str | None, market: str | None, domain: str | None = None, min_created_at: int | None = None) -> dict[str, Any] | None:
    """Robust fallback for portfolio workflows that persist directly to /portfolios.

    Some Bodhi Query Builder runs do not write downloadable run files. The portfolio
    is instead POSTed to this Evidence Service. Route-level latest files can miss in
    production when the domain key differs (www vs www3, trailing slash, etc.), so
    scan stored portfolio JSON files and select the newest usable brand/market match.
    """
    root = portfolio_dir()
    if not root.exists():
        return None
    target_brand = normalise_key(brand)
    target_market = normalise_key(market)
    target_domain = normalise_key(domain) if domain else ""
    candidates: list[dict[str, Any]] = []
    for path in root.glob("*.json"):
        payload = read_json(path)
        if not isinstance(payload, dict):
            continue
        if normalise_key(payload.get("brand")) != target_brand:
            continue
        if normalise_key(payload.get("market")) != target_market:
            continue
        if not is_usable_portfolio(payload, min_created_at=min_created_at):
            continue
        # Prefer exact domain when present, but do not require it. Query Builder
        # uses the primary domain, while UI may pass a sitemap on www3.
        payload_domain = normalise_key(payload.get("domain"))
        payload["_domain_match_score"] = 1 if target_domain and payload_domain == target_domain else 0
        candidates.append(payload)
    if not candidates:
        return None
    candidates.sort(key=lambda x: (x.get("_domain_match_score", 0), int(x.get("created_at_epoch") or 0)), reverse=True)
    out = dict(candidates[0])
    out.pop("_domain_match_score", None)
    out.setdefault("schema_version", "brand_topic_query_portfolio.v1")
    return out

def is_usable_portfolio(portfolio: dict[str, Any] | None, min_created_at: int | None = None) -> bool:
    if not isinstance(portfolio, dict):
        return False
    if not isinstance(portfolio.get("queries"), list) or not portfolio.get("queries"):
        return False
    if not isinstance(portfolio.get("topics"), list) or not portfolio.get("topics"):
        return False
    if min_created_at is not None:
        try:
            created = int(portfolio.get("created_at_epoch") or 0)
        except Exception:
            created = 0
        if created and created < min_created_at:
            return False
    return True


def trigger_and_wait_for_portfolio(req: dict[str, Any], target_run_id: str) -> dict[str, Any]:
    run_started_epoch = now_epoch()
    task_id = os.getenv("BODHI_PORTFOLIO_TASK_ID", "")
    workflow_id = os.getenv("BODHI_PORTFOLIO_WORKFLOW_ID", "")
    if not task_id:
        raise RuntimeError("BODHI_PORTFOLIO_TASK_ID is not set. Cannot generate synthetic portfolio.")
    client = BodhiClient()
    if not client.enabled:
        raise RuntimeError("BODHI_PAT_TOKEN is not set. Cannot trigger Bodhi portfolio task.")
    if not workflow_id:
        workflow_id = client.get_task_default_workflow_id(task_id) or ""

    inputs = {
        "brand": req.get("brand"),
        "market": req.get("market"),
        "domain": req.get("domain"),
        "owned_domains": req.get("owned_domains") or req.get("ownedDomains") or req.get("domain") or "",
        "brand_terms": req.get("brand_terms") or req.get("brandTerms") or req.get("brand") or "",
        "evidence_service_url": os.getenv("PUBLIC_EVIDENCE_SERVICE_URL") or req.get("evidence_service_url") or "",
        "portfolio_id": req.get("query_portfolio_id") or "",
        "seed_topics": req.get("seed_topics") or "",
        "topic_count": req.get("topic_count") or 8,
        "queries_per_topic": req.get("queries_per_topic") or 6,
        "language": req.get("language") or "English",
        "portfolio_goal": req.get("portfolio_goal") or "AI answer visibility audit query portfolio.",
    }
    write_run_status(target_run_id, "running", {"stage": "portfolio_generation_queued", "portfolio_task_id": task_id, "portfolio_workflow_id": workflow_id or None})
    trigger = client.trigger_task_run(
        task_id=task_id,
        workflow_id=workflow_id or None,
        run_name=f"Portfolio generation - {req.get('brand')} {req.get('market')} - {target_run_id}",
        inputs=inputs,
    )
    bodhi_run_id = client.extract_run_id(trigger)
    if not bodhi_run_id:
        raise RuntimeError(f"Could not determine Bodhi portfolio run id from response: {str(trigger)[:500]}")
    write_run_status(target_run_id, "running", {"stage": "portfolio_generation_running", "portfolio_bodhi_run_id": bodhi_run_id})

    # API-created Bodhi runs pause at the workflow UI node. Submit the UI form
    # through HITL using the same field labels as the UI node variables.
    write_run_status(target_run_id, "running", {"stage": "portfolio_ui_hitl_waiting", "portfolio_bodhi_run_id": bodhi_run_id})
    hitl_required = str(os.getenv("BODHI_PORTFOLIO_HITL_REQUIRED", "true")).lower() not in {"false", "0", "no"}
    hitl_result = client.submit_first_ui_hitl(
        bodhi_run_id,
        inputs,
        timeout_seconds=int(os.getenv("BODHI_PORTFOLIO_HITL_TIMEOUT_SECONDS", os.getenv("BODHI_HITL_TIMEOUT_SECONDS", "300"))),
        poll_seconds=int(os.getenv("BODHI_HITL_POLL_SECONDS", "2")),
        required=hitl_required,
    )
    write_run_status(target_run_id, "running", {
        "stage": "portfolio_ui_hitl_submitted" if hitl_result else "portfolio_ui_hitl_not_found",
        "portfolio_bodhi_run_id": bodhi_run_id,
        "portfolio_hitl_task_id": (hitl_result or {}).get("hitl_task_id"),
    })

    timeout = int(os.getenv("BODHI_PORTFOLIO_TIMEOUT_SECONDS", "900"))
    poll = int(os.getenv("BODHI_POLL_SECONDS", "10"))
    client.wait_for_run(task_id, bodhi_run_id, timeout_seconds=timeout, poll_seconds=poll)

    # Try known file outputs first, then outputs.json.
    candidates = [
        "outputs.json", "output.json", "outputs/query_portfolio.json", "query_portfolio.json",
        "outputs/frontend_report_bundle.json", "result.json",
    ]
    errors: list[str] = []
    for src in candidates:
        try:
            payload = client.get_run_file(bodhi_run_id, src)
            portfolio = extract_query_portfolio(payload)
            if portfolio:
                portfolio = normalise_portfolio_for_evidence(portfolio, req)
                portfolio.setdefault("metadata", {})["bodhi_portfolio_run_id"] = bodhi_run_id
                stored = store_portfolio(portfolio, req.get("brand", ""), req.get("market", ""), req.get("domain"))
                write_run_status(target_run_id, "running", {
                    "stage": "portfolio_generation_completed",
                    "query_portfolio_id": stored.get("portfolio_id"),
                    "portfolio_query_count": len(stored.get("queries") or []),
                    "portfolio_topic_count": len(stored.get("topics") or []),
                    "portfolio_source": "bodhi_run_file",
                })
                return stored
        except Exception as e:
            errors.append(f"{src}: {str(e)[:180]}")

    # Query builder workflows may persist directly to /portfolios and not write a
    # downloadable file to the Bodhi run directory. In that case, use the latest
    # portfolio created for this brand/market/domain after this refresh started.
    latest = load_latest_portfolio(req.get("brand"), req.get("market"), req.get("domain"))
    if not is_usable_portfolio(latest, min_created_at=run_started_epoch - 10):
        latest = find_recent_portfolio(req.get("brand"), req.get("market"), req.get("domain"), min_created_at=run_started_epoch - 10)
    if not is_usable_portfolio(latest, min_created_at=run_started_epoch - 10):
        # Allow a short grace period for the Query Builder persistence POST to land
        # after Bodhi marks the run complete.
        for _ in range(12):
            time.sleep(5)
            latest = load_latest_portfolio(req.get("brand"), req.get("market"), req.get("domain"))
            if is_usable_portfolio(latest, min_created_at=run_started_epoch - 10):
                break
            latest = find_recent_portfolio(req.get("brand"), req.get("market"), req.get("domain"), min_created_at=run_started_epoch - 10)
            if is_usable_portfolio(latest, min_created_at=run_started_epoch - 10):
                break
    if is_usable_portfolio(latest, min_created_at=run_started_epoch - 10):
        latest = normalise_portfolio_for_evidence(latest or {}, req)
        latest.setdefault("metadata", {})["bodhi_portfolio_run_id"] = bodhi_run_id
        stored = store_portfolio(latest, req.get("brand", ""), req.get("market", ""), req.get("domain"))
        write_run_status(target_run_id, "running", {
            "stage": "portfolio_generation_completed",
            "query_portfolio_id": stored.get("portfolio_id"),
            "portfolio_query_count": len(stored.get("queries") or []),
            "portfolio_topic_count": len(stored.get("topics") or []),
            "portfolio_source": "evidence_service_latest_or_recent_fallback",
            "portfolio_file_errors": errors[:5],
        })
        return stored

    raise RuntimeError("Portfolio run completed but no valid portfolio was found in Bodhi files or Evidence Service portfolio store. " + "; ".join(errors[:5]))


def discover_sitemap_candidates(domain: str | None, explicit_sitemap_url: str | None = None) -> dict[str, Any]:
    """Basic sitemap auto-discovery foundation.

    Manual sitemap_url remains an override. If blank, check robots.txt Sitemap
    directives and a few common sitemap locations. Full recursive scoring lands
    in v3.5.3.
    """
    candidates: list[str] = []
    errors: list[str] = []
    robots_url = ""
    robots_status = "not_checked"
    headers = {"User-Agent": "ai-visibility-evidence-service/3.5.2.2"}
    base = str(domain or "").strip().rstrip("/")
    if explicit_sitemap_url:
        candidates.append(str(explicit_sitemap_url).strip())
    if base:
        robots_url = base + "/robots.txt"
        try:
            r = requests.get(robots_url, timeout=15, headers=headers)
            robots_status = "present" if r.status_code < 400 else "missing"
            if r.status_code < 400:
                for line in r.text.splitlines():
                    if line.lower().startswith("sitemap:"):
                        loc = line.split(":", 1)[1].strip()
                        if loc:
                            candidates.append(loc)
        except Exception as exc:
            robots_status = "error"
            errors.append(f"robots.txt: {exc}")
        common_paths = ["/sitemap.xml", "/sitemap_index.xml", "/sitemap-index.xml", "/sitemap/sitemap.xml", "/index.pages-sitemap.xml"]
        bases = [base]
        try:
            parsed = urlparse(base)
            if parsed.scheme and parsed.netloc.startswith("www."):
                suffix = parsed.netloc[4:]
                bases.extend([f"{parsed.scheme}://www3.{suffix}", f"{parsed.scheme}://www2.{suffix}"])
        except Exception:
            pass
        for b in bases:
            for path in common_paths:
                candidates.append(b + path)
    # dedupe
    out=[]; seen=set()
    for c in candidates:
        if c and c not in seen:
            seen.add(c); out.append(c)
    return {"explicit_override": bool(explicit_sitemap_url), "robots_url": robots_url, "robots_status": robots_status, "candidates": out, "errors": errors}


def fetch_sitemap_urls(domain: str | None, sitemap_url: str | None, max_urls: int = 2000) -> tuple[list[str], dict[str, Any]]:
    discovery = discover_sitemap_candidates(domain, sitemap_url)
    seen: set[str] = set()
    urls: list[str] = []
    sitemaps_fetched: list[dict[str, Any]] = []
    to_fetch = list(discovery.get("candidates") or [])
    headers = {"User-Agent": "ai-visibility-evidence-service/3.5.2.2"}
    while to_fetch and len(urls) < max_urls:
        current = to_fetch.pop(0)
        if current in seen:
            continue
        seen.add(current)
        try:
            resp = requests.get(current, timeout=45, headers=headers)
            if resp.status_code >= 400:
                sitemaps_fetched.append({"url": current, "status": "error", "http_status_code": resp.status_code, "url_count": 0})
                continue
            root = ET.fromstring(resp.content)
            locs = [el.text.strip() for el in root.iter() if el.tag.lower().endswith("loc") and el.text]
            added = 0
            for loc in locs:
                lower = loc.lower()
                if (lower.endswith(".xml") or ".xml?" in lower) and len(seen) < 75:
                    to_fetch.append(loc)
                elif loc.startswith(("http://", "https://")):
                    urls.append(loc); added += 1
                    if len(urls) >= max_urls:
                        break
            sitemaps_fetched.append({"url": current, "status": "success", "http_status_code": resp.status_code, "loc_count": len(locs), "url_count": added})
        except Exception as exc:
            sitemaps_fetched.append({"url": current, "status": "error", "error": str(exc)[:240], "url_count": 0})
    out=[]; seen2=set()
    for u in urls:
        if u not in seen2:
            seen2.add(u); out.append(u)
    discovery.update({"sitemaps_fetched": sitemaps_fetched, "discovered_sitemaps": [x.get("url") for x in sitemaps_fetched if x.get("status") == "success"], "selected_url_count": len(out)})
    return out, discovery


def tokenise(value: Any) -> set[str]:
    text = str(value or "").lower()
    return {t for t in re.findall(r"[a-z0-9ぁ-んァ-ン一-龥]+", text) if len(t) >= 3}


def map_queries_to_sitemap(portfolio: dict[str, Any], urls: list[str], max_per_query: int = 3) -> tuple[list[dict[str, Any]], list[str]]:
    mappings: list[dict[str, Any]] = []
    selected: list[str] = []
    url_tokens = [(u, tokenise(u.replace("-", " ").replace("_", " ").replace("/", " "))) for u in urls]
    for q in (portfolio.get("queries") or []):
        qtext = " ".join([str(q.get("query") or ""), str(q.get("topic") or ""), str(q.get("recommended_page_type") or "")])
        qt = tokenise(qtext)
        ranked = []
        for u, ut in url_tokens:
            score = len(qt & ut)
            # light boosts for common page intent terms
            lower = u.lower()
            if "ev" in qtext.lower() and ("ev" in lower or "ariya" in lower or "leaf" in lower): score += 3
            if "service" in qtext.lower() and ("service" in lower or "support" in lower): score += 3
            if "dealer" in qtext.lower() and ("dealer" in lower or "shop" in lower): score += 3
            if "safety" in qtext.lower() and ("safety" in lower): score += 3
            if "finance" in qtext.lower() and ("finance" in lower or "price" in lower): score += 3
            ranked.append((score, u))
        ranked.sort(key=lambda x: (-x[0], len(x[1])))
        top = [u for score, u in ranked[:max_per_query] if score > 0] or [u for _, u in ranked[:max_per_query]]
        for idx, u in enumerate(top, start=1):
            mappings.append({
                "query_id": q.get("query_id"),
                "query": q.get("query"),
                "rank": idx,
                "url": u,
                "mapping_score": next((score for score, ru in ranked if ru == u), 0),
                "mapping_reason": "Sitemap lexical match against query, topic and recommended page type.",
            })
            selected.append(u)
    deduped = []
    seen = set()
    for u in selected:
        if u not in seen:
            seen.add(u)
            deduped.append(u)
    return mappings, deduped



def clean_text_value(value: Any, max_len: int | None = None) -> str:
    """Return a compact plain-text string safe for JSON report fields.

    v3.4.7 introduced calls such as clean_text_value(text, 8000) when
    preserving SerpAPI reconstructed_markdown. The helper accidentally kept
    a single-argument signature, which caused refresh runs to fail before
    SerpAPI execution. Keep the helper backwards-compatible and allow an
    optional maximum length.
    """
    if value is None:
        return ""
    text = html.unescape(str(value))
    text = re.sub(r"\s+", " ", text).strip()
    if max_len is not None and max_len > 0 and len(text) > max_len:
        return text[:max_len].rstrip()
    return text


def normalise_portfolio_for_evidence(portfolio: dict[str, Any], req: dict[str, Any]) -> dict[str, Any]:
    """Return a portfolio shape safe for downstream compact bundle builders.

    Some Bodhi/portfolio-store responses intentionally store only the useful top-level
    fields. This normalises schema/version fields and HTML entities without changing
    the user-facing query intent.
    """
    out = dict(portfolio or {})
    out.setdefault("schema_version", "brand_topic_query_portfolio.v1")
    out.setdefault("brand", req.get("brand"))
    out.setdefault("market", req.get("market"))
    out.setdefault("domain", req.get("domain"))
    out.setdefault("portfolio_source", out.get("portfolio_source") or "synthetic_deepresearch")
    out.setdefault("metadata", {})
    topics = []
    for t in out.get("topics") or []:
        if isinstance(t, dict):
            tt = dict(t)
            for key in ("topic", "name", "description", "rationale"):
                if key in tt:
                    tt[key] = clean_text_value(tt[key])
            topics.append(tt)
        else:
            topics.append({"topic": clean_text_value(t)})
    queries = []
    for i, q in enumerate(out.get("queries") or [], start=1):
        if not isinstance(q, dict):
            q = {"query": clean_text_value(q)}
        qq = dict(q)
        qq.setdefault("query_id", f"q{i:03d}")
        for key in ("query", "topic", "journey_stage", "intent", "priority", "recommended_page_type", "reason_selected", "market_localisation_notes"):
            if key in qq:
                qq[key] = clean_text_value(qq[key])
        queries.append(qq)
    out["topics"] = topics
    out["queries"] = queries
    return out


def fallback_owned_urls(domain: str | None) -> list[str]:
    if not domain:
        return []
    base = str(domain).rstrip("/")
    return [
        base + "/",
        base + "/vehicles/",
        base + "/ev/",
        base + "/dealers/",
        base + "/service/",
        base + "/purchase/",
        base + "/afterservice/",
        base + "/news/",
    ]



def source_domain(url: Any) -> str:
    try:
        return urlparse(str(url or "")).netloc.lower().replace("www.", "")
    except Exception:
        return ""



def source_type_from_url(url: Any, owned_domains: set[str] | list[str] | None = None) -> str:
    """Classify a URL's source type.

    When *owned_domains* is supplied, any URL whose domain matches is classified
    as 'owned_oem'. Otherwise the function falls back to generic heuristics
    without hardcoded brand names.
    """
    d = source_domain(url)
    u = str(url or "").lower()
    if not d:
        return "external_citation"
    # Check against explicitly provided owned domains
    if owned_domains:
        bare = d.removeprefix("www.")
        if d in owned_domains or bare in owned_domains or f"www.{bare}" in owned_domains:
            return "owned_oem"
    if any(x in d for x in ["youtube", "reddit", "x.com", "twitter", "facebook", "instagram"]):
        return "forum_social_video"
    if any(x in d for x in ["go.jp", "or.jp", ".gov", ".edu"]):
        return "authority_or_partner"
    if "google" in d and "product" in u:
        return "shopping_result"
    return "external_citation"

def normalise_serpapi_compact_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Make SerpAPI output compatible with the Auditor contract.

    Older service versions wrote google_ai_mode_compact.queries with raw_response only.
    v3.4.5 guarantees rows/top_citations/status so Bodhi never sees stale
    "collection pending" placeholders after SerpAPI has actually run.
    """
    if not isinstance(payload, dict):
        return {}
    rows = payload.get("rows") or payload.get("queries") or []
    if not isinstance(rows, list):
        rows = []
    norm_rows: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        rr = dict(row)
        refs = rr.get("top_citations") or rr.get("top_cited_sources") or rr.get("references") or rr.get("citations") or []
        if not isinstance(refs, list):
            refs = []
        clean_refs = []
        seen = set()
        for i, ref in enumerate(refs, start=1):
            if isinstance(ref, str):
                ref = {"url": ref, "source_url": ref}
            if not isinstance(ref, dict):
                continue
            url = ref.get("url") or ref.get("source_url") or ref.get("link")
            if not isinstance(url, str) or not url.startswith(("http://", "https://")) or url in seen:
                continue
            seen.add(url)
            clean_refs.append({
                "rank": ref.get("rank") or i,
                "title": clean_text_value(ref.get("title") or ref.get("source_name") or source_domain(url)),
                "url": url,
                "source_url": url,
                "source_domain": ref.get("source_domain") or source_domain(url),
                "source_name": clean_text_value(ref.get("source_name") or ref.get("source") or source_domain(url)),
                "snippet": clean_text_value(ref.get("snippet") or ref.get("description") or ref.get("text")),
                "source_type": ref.get("source_type") or source_type_from_url(url),
            })
        summary = clean_text_value(rr.get("answer_summary") or rr.get("summary") or rr.get("answer"))
        if not summary or summary == "SerpAPI collection pending.":
            summary = "SerpAPI completed but no AI answer summary was returned." if not clean_refs else "SerpAPI completed; citation references were returned."
        status = rr.get("status")
        if not status or str(status).startswith("pending"):
            status = "serpapi_completed_with_citations" if clean_refs else "serpapi_completed_no_citations"
        rr.update({
            "answer_summary": summary,
            "reconstructed_markdown": clean_text_value(rr.get("reconstructed_markdown"), 8000),
            "search_parameters": rr.get("search_parameters") if isinstance(rr.get("search_parameters"), dict) else {},
            "search_metadata": rr.get("search_metadata") if isinstance(rr.get("search_metadata"), dict) else {},
            "references": clean_refs,
            "top_citations": clean_refs[:3],
            "top_cited_sources": clean_refs[:3],
            "citation_count": len(clean_refs),
            "status": status,
        })
        norm_rows.append(rr)
    out = dict(payload)
    out.setdefault("schema_version", "google_ai_mode_compact.v2")
    out["rows"] = norm_rows
    out["queries"] = norm_rows
    out.setdefault("summary", {})
    if isinstance(out["summary"], dict):
        out["summary"].update({
            "captured_queries": len(norm_rows),
            "queries_with_citations": sum(1 for r in norm_rows if int(r.get("citation_count") or 0) > 0),
            "total_citations": sum(int(r.get("citation_count") or 0) for r in norm_rows),
            "normalised_by": "evidence_service_v3.4.7",
        })
    return out


def merge_serpapi_into_phase_files(target: Path, req: dict[str, Any]) -> None:
    google_path = target / "google_ai_mode_compact.json"
    google = normalise_serpapi_compact_payload(read_json(google_path, {}) or {})
    rows = google.get("rows") if isinstance(google, dict) else []
    if not isinstance(rows, list) or not rows:
        return
    write_json(google_path, google)

    citation_records: list[dict[str, Any]] = []
    source_by_url: dict[str, dict[str, Any]] = {}
    for row in rows:
        qid = row.get("query_id")
        qtext = row.get("query")
        for ref in row.get("top_citations") or []:
            url = ref.get("url") or ref.get("source_url")
            if not url:
                continue
            rec = {**ref, "query_id": qid, "query": qtext, "citation_position": ref.get("rank"), "source_type": ref.get("source_type") or source_type_from_url(url)}
            citation_records.append(rec)
            source_by_url.setdefault(url, {**ref, "url": url, "source_url": url, "source_domain": ref.get("source_domain") or source_domain(url), "citation_count": 0, "queries": []})
            source_by_url[url]["citation_count"] = int(source_by_url[url].get("citation_count") or 0) + 1
            source_by_url[url].setdefault("queries", []).append(qid)

    evidence = read_json(target / "evidence_scope.json", {}) or {}
    if isinstance(evidence, dict):
        evidence["ai_citations"] = citation_records
        evidence["external_sources"] = list(source_by_url.values())
        evidence["external_citation_urls"] = list(source_by_url.keys())
        evidence.setdefault("evidence_collection", {})
        if isinstance(evidence["evidence_collection"], dict):
            evidence["evidence_collection"].update({
                "serpapi_enabled": True,
                "serpapi_status": "completed_with_citations" if citation_records else "completed_no_citations",
                "serpapi_rows": len(rows),
                "serpapi_citation_count": len(citation_records),
            })
        write_json(target / "evidence_scope.json", evidence)

    visibility = read_json(target / "visibility_matrix.json", {}) or {}
    if isinstance(visibility, dict) and isinstance(visibility.get("queries"), list):
        cites_by_q: dict[str, list[dict[str, Any]]] = {}
        for rec in citation_records:
            cites_by_q.setdefault(str(rec.get("query_id")), []).append(rec)
        for q in visibility["queries"]:
            if not isinstance(q, dict):
                continue
            qid = str(q.get("query_id"))
            cites = cites_by_q.get(qid, [])
            q["citations"] = cites
            q["top_citations"] = cites[:3]
            q["visibility_status"] = "serpapi_collected_with_citations" if cites else "serpapi_collected_no_citations"
            q["citation_count"] = len(cites)
        write_json(target / "visibility_matrix.json", visibility)

    source_type_counts: dict[str, int] = {}
    for src in source_by_url.values():
        st = src.get("source_type") or "external_citation"
        source_type_counts[st] = source_type_counts.get(st, 0) + 1
    write_json(target / "source_classification.json", {
        "schema_version": "source_classification.v2",
        "brand": req.get("brand"),
        "market": req.get("market"),
        "sources": list(source_by_url.values()),
        "source_type_counts": source_type_counts,
        "source": "serpapi_live_normalised",
    })

    external_existing = read_json(target / "external_pages_full.json", {}) or {}
    existing_pages = external_existing.get("pages") or external_existing.get("external_pages") or []
    if not existing_pages and source_by_url:
        pages = []
        for idx, src in enumerate(source_by_url.values(), start=1):
            pages.append({
                "url": src.get("url"),
                "source_url": src.get("url"),
                "source_domain": src.get("source_domain"),
                "source_name": src.get("source_name"),
                "source_type": src.get("source_type") or "external_citation",
                "title": src.get("title"),
                "snippet": src.get("snippet"),
                "citation_count": src.get("citation_count"),
                "citation_position": idx,
                "related_queries_seed": src.get("queries", []),
                "crawl_status": "not_requested" if not bool(req.get("crawl_external") or req.get("enable_external_crawl")) else "pending",
                "extraction_status": "not_requested" if not bool(req.get("crawl_external") or req.get("enable_external_crawl")) else "pending",
                "geo_analysis_ready": False,
                "content_score_policy": "citation_metadata_until_crawled",
            })
        write_json(target / "external_pages_full.json", {
            "run_id": req.get("target_run_id"),
            "brand": req.get("brand"),
            "market": req.get("market"),
            "source": "serpapi_citation_metadata",
            "external_pages": pages,
            "pages": pages,
            "failed_sources": [],
            "summary": {"attempted": len(pages), "successful": 0, "crawl_enabled": bool(req.get("crawl_external") or req.get("enable_external_crawl"))},
        })


def copy_existing_ai_evidence_from_source(target: Path, req: dict[str, Any], queries: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Reuse Google AI Mode / SerpAPI citation evidence from a prior evidence run.

    This supports the UI pattern: rerun mapping/CMS/Auditor without spending new
    SerpAPI calls. Portfolio IDs identify query sets only; citation evidence lives
    on prior evidence run IDs, so this function copies and filters citation files
    from source_run_id into the new target run before Auditor trigger.
    """
    source_run_id = str(req.get("source_run_id") or "").strip()
    if not source_run_id:
        return {"reused": False, "reason": "source_run_id_not_supplied"}
    source = DATA_DIR / source_run_id
    if not source.exists():
        return {"reused": False, "reason": f"source_run_id_not_found:{source_run_id}"}

    query_ids = {str(q.get("query_id") or "").strip() for q in (queries or []) if isinstance(q, dict) and q.get("query_id")}
    query_texts = {clean_text_value(q.get("query") or "").lower() for q in (queries or []) if isinstance(q, dict) and q.get("query")}

    def row_matches(row: dict[str, Any]) -> bool:
        if not query_ids and not query_texts:
            return True
        qid = str(row.get("query_id") or "").strip()
        qtext = clean_text_value(row.get("query") or "").lower()
        return (qid and qid in query_ids) or (qtext and qtext in query_texts)

    source_google = normalise_serpapi_compact_payload(read_json(source / "google_ai_mode_compact.json", {}) or {})
    rows = source_google.get("rows") or source_google.get("queries") or []
    if not isinstance(rows, list):
        rows = []
    rows = [r for r in rows if isinstance(r, dict) and row_matches(r)]

    if not rows:
        return {"reused": False, "reason": "source_run_has_no_matching_google_ai_rows", "source_run_id": source_run_id}

    reused_google = dict(source_google)
    reused_google["rows"] = rows
    reused_google["queries"] = rows
    reused_google["source"] = "reused_google_ai_mode_from_source_run"
    reused_google["source_run_id"] = source_run_id
    reused_google.setdefault("summary", {})
    if isinstance(reused_google["summary"], dict):
        reused_google["summary"].update({
            "reused_from_source_run_id": source_run_id,
            "reused_rows": len(rows),
            "queries_with_citations": sum(1 for r in rows if int(r.get("citation_count") or 0) > 0),
            "total_citations": sum(int(r.get("citation_count") or 0) for r in rows),
        })
    write_json(target / "google_ai_mode_compact.json", reused_google)

    # Preserve existing source/citation fields where possible, then rebuild them
    # from the copied Google AI rows so the contract is stable.
    merge_serpapi_into_phase_files(target, {**req, "target_run_id": req.get("target_run_id")})

    # If a source run had richer classification/external files, keep useful fields
    # but never overwrite the rebuilt query-level citations.
    for filename in ["source_classification.json", "external_pages_full.json"]:
        src_file = source / filename
        dst_file = target / filename
        if src_file.exists() and not dst_file.exists():
            payload = read_json(src_file, {}) or {}
            if isinstance(payload, dict):
                payload["source_run_id"] = source_run_id
                payload["source"] = f"reused_{filename.replace('.json','')}"
                write_json(dst_file, payload)

    citation_total = sum(int(r.get("citation_count") or 0) for r in rows)
    return {
        "reused": True,
        "source_run_id": source_run_id,
        "reused_rows": len(rows),
        "reused_citation_count": citation_total,
    }

def materialise_phase2_evidence_files(
    target: Path,
    req: dict[str, Any],
    portfolio: dict[str, Any] | None,
    queries: list[dict[str, Any]],
    sitemap_urls: list[str],
    mappings: list[dict[str, Any]],
    owned_urls: list[str],
    portfolio_id: str | None,
) -> None:
    """Write Bodhi-compact compatible files even when SerpAPI/crawling is disabled.

    In a dry evidence refresh, the audit still needs a query portfolio and mapped owned
    URL candidates. Crawl and SerpAPI evidence can be empty-but-shaped, but audit_context,
    evidence_scope and owned_pages_full must not be empty.
    """
    brand = req.get("brand") or ""
    market = req.get("market") or ""
    domain = req.get("domain") or ""
    query_by_id = {q.get("query_id"): q for q in queries if isinstance(q, dict)}
    mappings_by_url: dict[str, list[dict[str, Any]]] = {}
    for m in mappings:
        u = m.get("url")
        if not u:
            continue
        mappings_by_url.setdefault(u, []).append(m)

    owned_pages = []
    for idx, url in enumerate(owned_urls, start=1):
        rel = mappings_by_url.get(url, [])
        related_queries = []
        categories = []
        for m in rel:
            q = query_by_id.get(m.get("query_id")) or {}
            if q.get("query"):
                related_queries.append(q.get("query"))
            if q.get("topic"):
                categories.append(q.get("topic"))
        owned_pages.append({
            "url": url,
            "final_url": url,
            "rank": idx,
            "selection_reason": "Mapped from synthetic query portfolio and sitemap inventory.",
            "mapping_quality": "candidate" if rel else "fallback_candidate",
            "mapping_score": max([int(m.get("mapping_score") or 0) for m in rel] or [0]),
            "mapping_reason": "; ".join([clean_text_value(m.get("mapping_reason")) for m in rel[:3] if m.get("mapping_reason")]) or "Sitemap candidate retained for owned-page GEO mapping.",
            "brand_topic_category": categories[0] if categories else None,
            "related_queries_seed": related_queries[:10],
            "crawl_status": "not_requested" if not bool(req.get("crawl_owned", req.get("enable_owned_crawl", False))) else "pending",
            "extraction_status": "not_requested" if not bool(req.get("crawl_owned", req.get("enable_owned_crawl", False))) else "pending",
            "geo_analysis_ready": False,
            "content_score_policy": "metadata_only_until_crawled",
            "title": "",
            "description": "Owned URL candidate mapped from sitemap; crawl disabled for this refresh." if not bool(req.get("crawl_owned", req.get("enable_owned_crawl", False))) else "Owned URL candidate awaiting crawl.",
            "metadata": {"query_count": len(rel), "query_ids": [m.get("query_id") for m in rel if m.get("query_id")]},
            "markdown_chars": 0,
            "raw_markdown_chars": 0,
            "markdown": "",
            "content_extract": "",
            "main_text": "",
            "text": "",
        })

    audit_context = {
        "schema_version": "audit_context.v2",
        "brand": brand,
        "market": market,
        "domain": domain,
        "portfolio_id": portfolio_id,
        "query_portfolio_source": (portfolio or {}).get("portfolio_source") if portfolio else req.get("query_portfolio_mode"),
        "topics": (portfolio or {}).get("topics", []) if portfolio else [],
        "queries": queries,
        "pages": owned_pages,
        "owned_urls": owned_pages,
        "query_owned_url_mapping": mappings,
        "counts": {"queries": len(queries), "owned_pages": len(owned_pages), "mappings": len(mappings), "sitemap_urls": len(sitemap_urls)},
    }
    evidence_scope = {
        "schema_version": "evidence_scope.v2",
        "brand": brand,
        "market": market,
        "domain": domain,
        "run_id": req.get("target_run_id"),
        "query_portfolio_id": portfolio_id,
        "queries": queries,
        "owned_pages": owned_pages,
        "owned_urls": owned_pages,
        "query_owned_url_mapping": mappings,
        "external_sources": [],
        "ai_citations": [],
        "evidence_collection": {
            "serpapi_enabled": bool(req.get("run_serpapi") or req.get("enable_serpapi")),
            "owned_crawl_enabled": bool(req.get("crawl_owned", req.get("enable_owned_crawl", False))),
            "external_crawl_enabled": bool(req.get("crawl_external", req.get("enable_external_crawl", False))),
            "mode": req.get("mode") or req.get("run_mode") or "phase2_refresh",
            "output_language": req.get("output_language") or req.get("language") or "English",
            "serpapi_hl": "en",
        },
    }
    google_ai_mode = {
        "schema_version": "google_ai_mode_compact.v2",
        "brand": brand,
        "market": market,
        "rows": [
            {
                "query_id": q.get("query_id"),
                "query": q.get("query"),
                "brand_topic_category": q.get("topic"),
                "references": [],
                "top_cited_sources": [],
                "answer_summary": "SerpAPI collection was not run for this refresh." if not bool(req.get("run_serpapi") or req.get("enable_serpapi")) else "SerpAPI collection pending.",
            }
            for q in queries
        ],
    }
    visibility_matrix = {
        "schema_version": "visibility_matrix.v2",
        "brand": brand,
        "market": market,
        "queries": [
            {
                "query_id": q.get("query_id"),
                "query": q.get("query"),
                "brand_topic_category": q.get("topic"),
                "visibility_status": "not_collected" if not bool(req.get("run_serpapi") or req.get("enable_serpapi")) else "pending_collection",
                "owned_target_page_cited": False,
                "owned_domain_citations": 0,
                "competitor_mentions_count": 0,
                "citations": [],
            }
            for q in queries
        ],
    }
    source_classification = {
        "schema_version": "source_classification.v2",
        "brand": brand,
        "market": market,
        "sources": [],
        "source_type_counts": {},
    }

    # Do not overwrite richer crawl/SerpAPI outputs if they already exist, except for
    # audit/evidence/mapping files which define the refresh scope.
    write_json(target / "query_portfolio.json", portfolio or {})
    write_json(target / "audit_context.json", audit_context)
    write_json(target / "evidence_scope.json", evidence_scope)
    write_json(target / "query_owned_url_mapping.json", {"run_id": req.get("target_run_id"), "query_portfolio_id": portfolio_id, "mappings": mappings, "mapped_owned_url_count": len(owned_urls)})
    existing_google = read_json(target / "google_ai_mode_compact.json", {}) or {}
    existing_rows = existing_google.get("rows") or existing_google.get("queries") if isinstance(existing_google, dict) else []
    has_completed_serpapi = isinstance(existing_rows, list) and any(str(r.get("status", "")).startswith("serpapi_completed") or r.get("source") == "serpapi_live" for r in existing_rows if isinstance(r, dict))
    if has_completed_serpapi or (isinstance(existing_google, dict) and existing_google.get("source") == "serpapi_live"):
        write_json(target / "google_ai_mode_compact.json", normalise_serpapi_compact_payload(existing_google))
    else:
        write_json(target / "google_ai_mode_compact.json", google_ai_mode)
    if not (target / "visibility_matrix.json").exists():
        write_json(target / "visibility_matrix.json", visibility_matrix)
    if not (target / "source_classification.json").exists():
        write_json(target / "source_classification.json", source_classification)
    merge_serpapi_into_phase_files(target, req)
    owned_existing = read_json(target / "owned_pages_full.json", {})
    existing_pages = owned_existing.get("pages") if isinstance(owned_existing, dict) else None
    if not existing_pages:
        write_json(target / "owned_pages_full.json", {"run_id": req.get("target_run_id"), "brand": brand, "market": market, "source": "phase2_mapping_metadata", "pages": owned_pages, "summary": {"attempted": len(owned_pages), "successful": 0, "crawl_enabled": bool(req.get("crawl_owned", req.get("enable_owned_crawl", False)))}})
    external_existing = read_json(target / "external_pages_full.json", {})
    existing_external = (external_existing.get("external_pages") or external_existing.get("pages")) if isinstance(external_existing, dict) else None
    if not existing_external:
        write_json(target / "external_pages_full.json", {"run_id": req.get("target_run_id"), "brand": brand, "market": market, "source": "phase2_mapping_metadata", "external_pages": [], "pages": [], "failed_sources": [], "summary": {"attempted": 0, "successful": 0, "crawl_enabled": bool(req.get("crawl_external", req.get("enable_external_crawl", False)))}})


def trigger_auditor_if_configured(req: dict[str, Any], target_run_id: str, portfolio_id: str | None) -> dict[str, Any] | None:
    task_id = os.getenv("BODHI_AUDITOR_TASK_ID", "")
    workflow_id = os.getenv("BODHI_AUDITOR_WORKFLOW_ID", "")
    if not task_id:
        write_run_status(target_run_id, "running", {"stage": "auditor_skipped", "auditor_skip_reason": "BODHI_AUDITOR_TASK_ID not set"})
        return None
    client = BodhiClient()
    if not client.enabled:
        write_run_status(target_run_id, "running", {"stage": "auditor_skipped", "auditor_skip_reason": "BODHI_PAT_TOKEN not set"})
        return None
    if not workflow_id:
        workflow_id = client.get_task_default_workflow_id(task_id) or ""
    max_external = req.get("max_external_sources_per_query") or req.get("max_external_citations_per_query") or 3
    inputs = {
        "brand": req.get("brand"),
        "market": req.get("market"),
        "domain": req.get("domain"),
        "evidence_service_url": os.getenv("PUBLIC_EVIDENCE_SERVICE_URL") or req.get("evidence_service_url") or "",
        "evidence_run_id": target_run_id,
        "run_mode": req.get("mode") or req.get("run_mode") or "fresh_evidence",
        "query_portfolio_mode": req.get("query_portfolio_mode") or "reuse",
        "query_portfolio_id": portfolio_id or "",
        "max_owned_pages_per_query": req.get("max_owned_pages_per_query") or req.get("max_owned_urls_per_query") or 3,
        # Keep both names for compatibility with older evidence payloads and the v9.2 UI field.
        "max_external_sources_per_query": max_external,
        "max_external_citations_per_query": max_external,
        "query_limit": req.get("query_limit") or 50,
    }
    write_run_status(target_run_id, "running", {"stage": "auditor_queued", "auditor_task_id": task_id, "auditor_workflow_id": workflow_id or None})
    trigger = client.trigger_task_run(task_id, workflow_id or None, f"Auditor - {target_run_id}", inputs)
    rid = client.extract_run_id(trigger)
    write_run_status(target_run_id, "running", {"stage": "auditor_run_created", "bodhi_auditor_run_id": rid, "trigger_response": trigger})
    hitl = None
    if rid:
        try:
            write_run_status(target_run_id, "running", {"stage": "auditor_ui_hitl_waiting", "bodhi_auditor_run_id": rid})
            hitl = client.submit_first_ui_hitl(
                rid,
                inputs,
                timeout_seconds=int(os.getenv("BODHI_AUDITOR_HITL_TIMEOUT_SECONDS", os.getenv("BODHI_HITL_TIMEOUT_SECONDS", "240"))),
                poll_seconds=int(os.getenv("BODHI_HITL_POLL_SECONDS", "2")),
                required=str(os.getenv("BODHI_AUDITOR_HITL_REQUIRED", "false")).lower() in {"true", "1", "yes"},
            )
            write_run_status(target_run_id, "running", {
                "stage": "auditor_ui_hitl_submitted" if hitl else "auditor_ui_hitl_not_found",
                "bodhi_auditor_run_id": rid,
                "auditor_hitl_task_id": (hitl or {}).get("hitl_task_id"),
                "hitl": hitl,
            })
        except Exception as e:
            hitl = {"error": str(e)[:500]}
            write_run_status(target_run_id, "running", {"stage": "auditor_ui_hitl_failed", "bodhi_auditor_run_id": rid, "auditor_error": str(e)[:500]})
    write_run_status(target_run_id, "running", {"stage": "auditor_running", "bodhi_auditor_run_id": rid})
    return {"bodhi_auditor_run_id": rid, "trigger_response": trigger, "hitl": hitl}


def run_phase2_refresh(job_id: str, req: dict[str, Any]) -> None:
    target_run_id = req["target_run_id"]
    try:
        target = run_dir(target_run_id)
        target.mkdir(parents=True, exist_ok=True)
        write_json(target / "refresh_request.json", req)
        write_run_status(target_run_id, "running", {"stage": "initialising", "job_id": job_id, "request": req})
        update_job(job_id, {"status": "running", "stage": "initialising", "target_run_id": target_run_id})

        portfolio: dict[str, Any] | None = None
        portfolio_id = req.get("query_portfolio_id")
        mode = str(req.get("query_portfolio_mode") or "reuse").lower()
        if portfolio_id:
            portfolio = load_portfolio(portfolio_id)
            if not portfolio:
                raise RuntimeError(f"query_portfolio_id was supplied but not found: {portfolio_id}")
        elif mode == "synthetic":
            portfolio = trigger_and_wait_for_portfolio(req, target_run_id)
            portfolio_id = portfolio.get("portfolio_id")
        elif mode == "upload":
            # Custom portfolio uploaded via the Refresh Evidence screen.
            # The portfolio JSON is passed inline in the request as 'custom_portfolio'
            # or 'uploaded_portfolio', or as a pre-stored portfolio_id.
            custom = req.get("custom_portfolio") or req.get("uploaded_portfolio")
            if isinstance(custom, str) and custom.strip():
                try:
                    custom = json.loads(custom)
                except json.JSONDecodeError as e:
                    raise RuntimeError(f"Invalid JSON in custom_portfolio: {e}")
            if isinstance(custom, dict) and (custom.get("queries") or custom.get("topics")):
                from app.portfolio_schema import validate_portfolio, normalise_uploaded_portfolio
                validation = validate_portfolio(custom)
                if not validation["valid"]:
                    raise RuntimeError(f"Uploaded portfolio validation failed: {'; '.join(validation['errors'][:5])}")
                normalised = normalise_uploaded_portfolio(custom, brand=req.get("brand", ""), market=req.get("market", ""), domain=req.get("domain"))
                portfolio = store_portfolio(normalised, req.get("brand", ""), req.get("market", ""), req.get("domain"))
                portfolio_id = portfolio.get("portfolio_id")
                write_run_status(target_run_id, "running", {
                    "stage": "portfolio_upload_stored",
                    "query_portfolio_id": portfolio_id,
                    "portfolio_query_count": len(portfolio.get("queries") or []),
                    "portfolio_topic_count": len(portfolio.get("topics") or []),
                    "portfolio_source": "user_upload",
                    "portfolio_validation": validation,
                })
            else:
                raise RuntimeError("query_portfolio_mode is 'upload' but no valid custom_portfolio was provided in the request")
        elif mode in {"manual", "manual_topics_and_queries"}:
            manual = req.get("manual_queries_json") or req.get("queries_json") or req.get("topics_json")
            if isinstance(manual, str) and manual.strip():
                parsed = json.loads(manual)
                if isinstance(parsed, dict) and parsed.get("queries"):
                    portfolio = store_portfolio(parsed, req.get("brand", ""), req.get("market", ""), req.get("domain"))
                    portfolio_id = portfolio.get("portfolio_id")
        if portfolio:
            portfolio = normalise_portfolio_for_evidence(portfolio, req)
            write_json(target / "query_portfolio.json", portfolio)

        queries = (portfolio or {}).get("queries") or []
        query_limit = int(req.get("query_limit") or 50)
        queries = queries[:query_limit]

        write_run_status(target_run_id, "running", {"stage": "sitemap_inventory_running", "query_count": len(queries), "query_portfolio_id": portfolio_id})
        sitemap_url = req.get("sitemap_url")
        sitemap_urls, sitemap_discovery = fetch_sitemap_urls(req.get("domain"), sitemap_url, max_urls=int(req.get("sitemap_max_urls") or 2000))
        if not sitemap_urls:
            sitemap_urls = fallback_owned_urls(req.get("domain"))
        write_json(target / "sitemap_inventory.json", {"source": sitemap_url or "auto_discovered", "url_count": len(sitemap_urls), "urls": sitemap_urls, "generated_at_epoch": now_epoch(), "fallback_used": not bool(sitemap_urls), "sitemap_discovery": sitemap_discovery})

        owned_urls: list[str] = []
        mappings: list[dict[str, Any]] = []
        if portfolio and sitemap_urls:
            write_run_status(target_run_id, "running", {"stage": "owned_url_mapping_running", "sitemap_url_count": len(sitemap_urls)})
            mappings, mapped_owned = map_queries_to_sitemap(portfolio, sitemap_urls, int(req.get("max_owned_pages_per_query") or req.get("max_owned_urls_per_query") or 3))
            owned_urls = mapped_owned[: int(req.get("max_owned_urls") or 60)]

        # Materialise scope before optional crawl so /bodhi-compact is useful even
        # for no-SerpAPI/no-crawl dry refreshes.
        materialise_phase2_evidence_files(target, {**req, "target_run_id": target_run_id}, portfolio, queries, sitemap_urls, mappings, owned_urls, portfolio_id)

        # Reuse Google AI Mode/SerpAPI citation evidence from a prior evidence run
        # when requested. This lets CMS/Auditor changes be tested without spending
        # new SerpAPI calls.
        if req.get("source_run_id") and not bool(req.get("run_serpapi") or req.get("enable_serpapi")) and bool(req.get("use_existing_google_ai_mode", True)):
            reuse_info = copy_existing_ai_evidence_from_source(target, {**req, "target_run_id": target_run_id}, queries)
            write_run_status(target_run_id, "running", {"stage": "ai_citation_reuse_completed", **reuse_info})

        # Optional live SerpAPI collection owned by evidence service.
        if bool(req.get("run_serpapi") or req.get("enable_serpapi")) and queries:
            write_run_status(target_run_id, "running", {"stage": "serpapi_collection_running", "serpapi_query_count": len(queries)})
            serp_job = make_job_id("serpapi_inline")
            update_job(serp_job, {"status": "accepted", "job_id": serp_job, "parent_job_id": job_id, "target_run_id": target_run_id})
            run_serpapi_collection(serp_job, SerpApiJobRequest(brand=req.get("brand", ""), market=req.get("market", ""), target_run_id=target_run_id, queries=queries, max_queries=query_limit))
            merge_serpapi_into_phase_files(target, {**req, "target_run_id": target_run_id})
            google_after_serp = read_json(target / "google_ai_mode_compact.json", {}) or {}
            serp_rows = google_after_serp.get("rows") or google_after_serp.get("queries") or []
            serp_citations = sum(int(r.get("citation_count") or 0) for r in serp_rows if isinstance(r, dict)) if isinstance(serp_rows, list) else 0
            write_run_status(target_run_id, "running", {"stage": "serpapi_collection_completed", "serpapi_job_id": serp_job, "serpapi_rows": len(serp_rows) if isinstance(serp_rows, list) else 0, "serpapi_citation_count": serp_citations})

        # Crawl mapped owned URLs and top external URLs from existing/source/current evidence.
        write_run_status(target_run_id, "running", {"stage": "crawl_refresh_running", "owned_url_count": len(owned_urls)})
        refresh_req = FullRefreshRequest(
            brand=req.get("brand", ""),
            market=req.get("market", ""),
            source_run_id=req.get("source_run_id"),
            target_run_id=target_run_id,
            mode=req.get("mode") or req.get("run_mode") or "phase2_refresh",
            use_existing_google_ai_mode=bool(req.get("use_existing_google_ai_mode", not bool(req.get("run_serpapi") or req.get("enable_serpapi")))),
            run_serpapi=False,
            queries=queries,
            owned_urls=owned_urls,
            external_urls=req.get("external_urls") or [],
            crawl_owned=bool(req.get("crawl_owned", req.get("enable_owned_crawl", True))),
            crawl_external=bool(req.get("crawl_external", req.get("enable_external_crawl", False))),
            max_queries=query_limit,
            max_owned_urls=int(req.get("max_owned_urls") or max(20, len(owned_urls) or 20)),
            max_external_urls=int(req.get("max_external_urls") or 30),
        )
        run_full_refresh(job_id, refresh_req)

        # run_full_refresh can legitimately write empty crawl files when crawling is
        # disabled. Re-materialise metadata scope afterwards so the Auditor can still
        # see the portfolio, mappings and owned URL candidates.
        materialise_phase2_evidence_files(target, {**req, "target_run_id": target_run_id}, portfolio, queries, sitemap_urls, mappings, owned_urls, portfolio_id)
        if req.get("source_run_id") and not bool(req.get("run_serpapi") or req.get("enable_serpapi")) and bool(req.get("use_existing_google_ai_mode", True)):
            reuse_info = copy_existing_ai_evidence_from_source(target, {**req, "target_run_id": target_run_id}, queries)
            write_run_status(target_run_id, "running", {"stage": "ai_citation_reuse_completed", **reuse_info})
        else:
            merge_serpapi_into_phase_files(target, {**req, "target_run_id": target_run_id})

        # v3.5.1: canonicalise crawl/citation evidence before Auditor reads /bodhi-compact.
        canonical = canonicalise_bundle_files(target, {**req, "target_run_id": target_run_id})
        hygiene = check_site_ai_hygiene(req.get("domain"), (((canonical.get("owned_pages_full") or {}).get("pages") or []) if isinstance(canonical, dict) else []), locals().get("sitemap_discovery", {}))
        write_json(target / "site_ai_hygiene.json", hygiene)
        # Inject site hygiene into the core compact files for Auditor/frontend consumption.
        for fname in ["audit_context.json", "evidence_scope.json", "visibility_matrix.json"]:
            payload = read_json(target / fname, {}) or {}
            if isinstance(payload, dict):
                payload["site_ai_hygiene"] = hygiene
                write_json(target / fname, payload)
        write_run_status(target_run_id, "running", {"stage": "evidence_canonicalised", **(canonical.get("telemetry") or {}), "ai_hygiene_priority": hygiene.get("priority"), "llms_txt_status": (hygiene.get("llms_txt") or {}).get("status"), "robots_txt_status": (hygiene.get("robots_txt") or {}).get("status"), "json_ld_coverage_pct": (hygiene.get("structured_data") or {}).get("coverage_pct")})

        auditor = None
        if bool(req.get("trigger_auditor", True)):
            try:
                auditor = trigger_auditor_if_configured(req, target_run_id, portfolio_id)
            except Exception as e:
                write_run_status(target_run_id, "running", {"stage": "auditor_trigger_failed", "auditor_error": str(e)[:500]})
            if auditor:
                # The Auditor workflow stores /runs/{run_id}/report-bundle when it finishes.
                # Keep the refresh run active until report_store marks it report_bundle_ready.
                write_run_status(target_run_id, "running", {"stage": "auditor_running", "query_portfolio_id": portfolio_id, "auditor": auditor})
                update_job(job_id, {"status": "running", "stage": "auditor_running", "target_run_id": target_run_id, "updated_at_epoch": now_epoch()})
            else:
                write_run_status(target_run_id, "running", {"stage": "evidence_ready", "query_portfolio_id": portfolio_id, "awaiting_report_bundle": True})
                update_job(job_id, {"status": "running", "stage": "evidence_ready", "target_run_id": target_run_id, "updated_at_epoch": now_epoch()})
        else:
            write_run_status(target_run_id, "running", {"stage": "evidence_ready", "query_portfolio_id": portfolio_id, "awaiting_report_bundle": True})
            update_job(job_id, {"status": "running", "stage": "evidence_ready", "target_run_id": target_run_id, "updated_at_epoch": now_epoch()})
    except Exception as e:
        write_run_status(target_run_id, "failed", {"stage": "failed", "error": str(e)[:1500], "failed_at_epoch": now_epoch()})
        update_job(job_id, {"status": "failed", "stage": "failed", "error": str(e)[:1500], "failed_at_epoch": now_epoch()})


def start_phase2_refresh(req: dict[str, Any]) -> dict[str, Any]:
    target_run_id = req.get("target_run_id") or f"evidence_{normalise_key(req.get('brand'))}_{normalise_key(req.get('market'))}_{now_epoch()}_{uuid.uuid4().hex[:6]}"
    req = {**req, "target_run_id": target_run_id}
    job_id = make_job_id("phase2")
    write_run_status(target_run_id, "accepted", {"stage": "accepted", "job_id": job_id, "request": req, "brand": req.get("brand"), "market": req.get("market"), "domain": req.get("domain")})
    update_job(job_id, {"status": "accepted", "stage": "accepted", "target_run_id": target_run_id, "request": req, "created_at_epoch": now_epoch()})
    thread = threading.Thread(target=run_phase2_refresh, args=(job_id, req), daemon=True)
    thread.start()
    return {"status": "accepted", "job_id": job_id, "target_run_id": target_run_id, "run_status": read_json(status_dir() / f"{target_run_id}.json", {})}
