from __future__ import annotations

import io
import json
import os
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import JSONResponse
from markdownify import markdownify as md
from pydantic import BaseModel, Field

try:
    import trafilatura
except Exception:
    trafilatura = None

try:
    from pypdf import PdfReader
except Exception:
    PdfReader = None

try:
    from playwright.sync_api import sync_playwright
except Exception:
    sync_playwright = None


router = APIRouter()

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data/evidence-runs"))
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")
SERPAPI_KEY = os.environ.get("SERPAPI_KEY", "")
SERPAPI_ENGINE = os.environ.get("SERPAPI_ENGINE", "google_ai_mode")


class FullRefreshRequest(BaseModel):
    brand: str = "Nissan"
    market: str = "Japan"
    source_run_id: str | None = None
    target_run_id: str
    mode: str = "crawl_only"

    use_existing_google_ai_mode: bool = True
    run_serpapi: bool = False

    queries: list[dict[str, Any]] = Field(default_factory=list)
    owned_urls: list[str] = Field(default_factory=list)
    external_urls: list[str] = Field(default_factory=list)
    brand_topic_categories: list[Any] = Field(default_factory=list)

    crawl_owned: bool = True
    crawl_external: bool = True

    max_queries: int = 10
    max_owned_urls: int = 20
    max_external_urls: int = 30

    use_playwright_fallback: bool = True
    timeout_seconds: int = 35


class SerpApiJobRequest(BaseModel):
    brand: str = "Nissan"
    market: str = "Japan"
    target_run_id: str
    queries: list[dict[str, Any]]
    max_queries: int = 10


def require_admin(token: str | None):
    if ADMIN_TOKEN and token != ADMIN_TOKEN:
        raise HTTPException(status_code=401, detail="Invalid admin token")


def now_epoch() -> int:
    return int(time.time())


def write_json(path: Path, value: Any):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def job_path(job_id: str) -> Path:
    return DATA_DIR / "_jobs" / f"{job_id}.json"


def update_job(job_id: str, patch: dict[str, Any]):
    path = job_path(job_id)
    current = {}
    if path.exists():
        current = read_json(path)
    current.update(patch)
    current["updated_at_epoch"] = now_epoch()
    write_json(path, current)


def make_job_id(prefix: str) -> str:
    return f"{prefix}_{now_epoch()}_{uuid.uuid4().hex[:8]}"


def normalise_url(url: Any) -> str | None:
    if not isinstance(url, str):
        return None
    u = url.strip()
    if not u.startswith(("http://", "https://")):
        return None
    parsed = urlparse(u)
    if not parsed.netloc:
        return None
    return u.split("#")[0]


def dedupe(urls: list[str]) -> list[str]:
    seen = set()
    out = []
    for u in urls:
        nu = normalise_url(u)
        if not nu:
            continue
        if nu not in seen:
            seen.add(nu)
            out.append(nu)
    return out


def owned_domains_for_brand(brand: str, market: str) -> set[str]:
    return {
        "nissan.co.jp",
        "www.nissan.co.jp",
        "www2.nissan.co.jp",
        "www3.nissan.co.jp",
        "nissan-global.com",
        "www.nissan-global.com",
        "global.nissannews.com",
    }


def is_owned_url(url: str, owned_domains: set[str]) -> bool:
    host = urlparse(url).netloc.lower()
    return any(host == d or host.endswith("." + d) for d in owned_domains)


def collect_urls_from_obj(obj: Any, owned_domains: set[str]) -> tuple[list[str], list[str]]:
    owned: list[str] = []
    external: list[str] = []

    url_keys = {
        "url",
        "source_url",
        "page_url",
        "owned_url",
        "target_url",
        "canonical_url",
        "mapped_url",
        "recommended_url",
    }

    def walk(x: Any):
        if isinstance(x, dict):
            for k, v in x.items():
                key = str(k).lower()
                if key in url_keys and isinstance(v, str):
                    u = normalise_url(v)
                    if u:
                        if is_owned_url(u, owned_domains):
                            owned.append(u)
                        else:
                            external.append(u)
                else:
                    walk(v)
        elif isinstance(x, list):
            for item in x:
                walk(item)

    walk(obj)
    return owned, external


def extract_inventory(source_dir: Path, req: FullRefreshRequest) -> tuple[list[str], list[str]]:
    owned_domains = owned_domains_for_brand(req.brand, req.market)

    owned_urls = list(req.owned_urls or [])
    external_urls = list(req.external_urls or [])

    inventory_files = [
        "audit_context.json",
        "evidence_scope.json",
        "google_ai_mode_compact.json",
        "source_classification.json",
        "visibility_matrix.json",
    ]

    if source_dir and source_dir.exists():
        for filename in inventory_files:
            path = source_dir / filename
            if not path.exists():
                continue
            try:
                obj = read_json(path)
                owned, external = collect_urls_from_obj(obj, owned_domains)
                owned_urls.extend(owned)
                external_urls.extend(external)
            except Exception:
                continue

    owned_urls = dedupe(owned_urls)[: max(0, req.max_owned_urls)]
    external_urls = dedupe(external_urls)[: max(0, req.max_external_urls)]

    return owned_urls, external_urls


def static_fetch(url: str, timeout: int) -> tuple[int | None, str, bytes, str]:
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; AIVisibilityEvidenceBot/1.0; +https://example.com/bot)",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/pdf;q=0.8,*/*;q=0.7",
        "Accept-Language": "ja,en-GB;q=0.9,en;q=0.8",
    }
    resp = requests.get(url, headers=headers, timeout=timeout, allow_redirects=True)
    content_type = resp.headers.get("content-type", "")
    return resp.status_code, str(resp.url), resp.content, content_type


def playwright_fetch(url: str, timeout: int) -> tuple[int | None, str, bytes, str]:
    if sync_playwright is None:
        raise RuntimeError("Playwright is not available in this Railway image.")

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
            ],
        )
        page = browser.new_page(
            user_agent="Mozilla/5.0 AIVisibilityEvidenceBot/1.0",
            locale="ja-JP",
        )
        page.set_default_timeout(timeout * 1000)
        response = page.goto(url, wait_until="networkidle", timeout=timeout * 1000)
        final_url = page.url
        html = page.content()
        status = response.status if response else None
        browser.close()

    return status, final_url, html.encode("utf-8", errors="replace"), "text/html; rendered=playwright"


def extract_pdf_text(body: bytes) -> str:
    if PdfReader is None:
        return ""
    try:
        reader = PdfReader(io.BytesIO(body))
        pages = []
        for page in reader.pages[:25]:
            pages.append(page.extract_text() or "")
        return "\n\n".join(pages).strip()
    except Exception:
        return ""


def extract_html_features(html: str, final_url: str) -> dict[str, Any]:
    soup = BeautifulSoup(html, "lxml")

    title = ""
    if soup.title and soup.title.string:
        title = soup.title.string.strip()

    meta = {}
    for tag in soup.find_all("meta"):
        name = tag.get("name") or tag.get("property")
        content = tag.get("content")
        if name and content:
            meta[str(name).strip()] = str(content).strip()

    canonical_url = ""
    canonical = soup.find("link", rel=lambda v: v and "canonical" in str(v).lower())
    if canonical and canonical.get("href"):
        canonical_url = str(canonical.get("href")).strip()

    headings = []
    for level in ["h1", "h2", "h3", "h4"]:
        for h in soup.find_all(level):
            text = " ".join(h.get_text(" ", strip=True).split())
            if text:
                headings.append({"level": level, "text": text})

    links = []
    pdf_links = []
    for a in soup.find_all("a"):
        href = a.get("href")
        text = " ".join(a.get_text(" ", strip=True).split())
        if not href:
            continue
        href = str(href).strip()
        item = {"url": href, "text": text[:250]}
        links.append(item)
        if ".pdf" in href.lower():
            pdf_links.append(item)

    schema_blocks = []
    for s in soup.find_all("script", type=lambda v: v and "ld+json" in str(v).lower()):
        raw = s.string or s.get_text()
        if raw:
            schema_blocks.append(raw[:5000])

    for bad in soup(["script", "style", "noscript", "svg"]):
        bad.decompose()

    body_text = soup.get_text("\n", strip=True)
    body_text = re.sub(r"\n{3,}", "\n\n", body_text)
    body_text = re.sub(r"[ \t]{2,}", " ", body_text).strip()

    markdown = md(str(soup), heading_style="ATX")
    markdown = re.sub(r"\n{4,}", "\n\n\n", markdown).strip()

    trafilatura_text = ""
    if trafilatura is not None:
        try:
            trafilatura_text = trafilatura.extract(html, url=final_url, include_links=True, include_tables=True) or ""
        except Exception:
            trafilatura_text = ""

    best_text = trafilatura_text.strip() if len(trafilatura_text or "") > len(body_text) * 0.3 else body_text

    return {
        "title": title,
        "meta": meta,
        "canonical_url": canonical_url,
        "headings": headings[:120],
        "links": links[:500],
        "pdf_links": pdf_links[:100],
        "schema_blocks": schema_blocks[:20],
        "schema_types_detected": detect_schema_types(schema_blocks),
        "schema_types": detect_schema_types(schema_blocks),
        "json_ld_present": bool(schema_blocks),
        "json_ld_block_count": len(schema_blocks),
        "text": best_text,
        "markdown": markdown if len(markdown) >= len(best_text) else best_text,
    }


def detect_schema_types(schema_blocks: list[str]) -> list[str]:
    found = set()
    for raw in schema_blocks:
        for match in re.finditer(r'"@type"\s*:\s*"([^"]+)"', raw):
            found.add(match.group(1))
    return sorted(found)


def extraction_quality(markdown: str, headings: list[Any], metadata: dict[str, Any], links: list[Any]) -> dict[str, Any]:
    word_count = len(re.findall(r"\w+", markdown or ""))
    question_count = len(re.findall(r"\?", markdown or "")) + len(re.findall(r"(what|how|why|when|where|which|can|does|is)\b", markdown or "", re.I))
    return {
        "word_count": word_count,
        "heading_count": len(headings or []),
        "metadata_count": len(metadata or {}),
        "link_count": len(links or []),
        "question_signal_count": question_count,
        "has_substantial_markdown": word_count >= 250,
        "has_heading_structure": len(headings or []) >= 2,
        "has_metadata": len(metadata or {}) > 0,
    }


def crawl_one_url(url: str, timeout: int = 35, use_playwright_fallback: bool = True) -> dict[str, Any]:
    started = time.time()

    result: dict[str, Any] = {
        "url": url,
        "source_url": url,
        "final_url": url,
        "crawl_status": "failed",
        "fetch_method": "none",
        "http_status": None,
        "content_type": "",
        "title": "",
        "meta_description": "",
        "metadata": {},
        "canonical_url": "",
        "headings": [],
        "links": [],
        "pdf_links": [],
        "schema_types_detected": [],
        "text": "",
        "markdown": "",
        "word_count": 0,
        "content_length": 0,
        "extraction_quality": {},
        "error": None,
        "fetched_at_epoch": now_epoch(),
        "elapsed_ms": None,
    }

    try:
        status, final_url, body, content_type = static_fetch(url, timeout)
        result["fetch_method"] = "static"
        result["http_status"] = status
        result["final_url"] = final_url
        result["content_type"] = content_type
        result["content_length"] = len(body)

        lower_url = final_url.lower()
        lower_ct = content_type.lower()

        if lower_url.endswith(".pdf") or "application/pdf" in lower_ct:
            text = extract_pdf_text(body)
            result["title"] = Path(urlparse(final_url).path).name
            result["text"] = text
            result["markdown"] = text
            result["word_count"] = len(re.findall(r"\w+", text or ""))
            result["crawl_status"] = "success" if text else "empty_pdf_extract"
            result["fetch_method"] = "static_pdf"
            result["extraction_quality"] = extraction_quality(text, [], {}, [])
            return result

        html = body.decode("utf-8", errors="replace")
        features = extract_html_features(html, final_url)

        markdown = features["markdown"]
        word_count = len(re.findall(r"\w+", markdown or ""))

        if use_playwright_fallback and word_count < 250:
            try:
                p_status, p_final_url, p_body, p_content_type = playwright_fetch(url, timeout)
                p_html = p_body.decode("utf-8", errors="replace")
                p_features = extract_html_features(p_html, p_final_url)
                p_markdown = p_features["markdown"]
                p_word_count = len(re.findall(r"\w+", p_markdown or ""))

                if p_word_count > word_count:
                    status, final_url, content_type, features, markdown, word_count = (
                        p_status,
                        p_final_url,
                        p_content_type,
                        p_features,
                        p_markdown,
                        p_word_count,
                    )
                    result["fetch_method"] = "playwright"
            except Exception as e:
                result["playwright_fallback_error"] = str(e)[:500]

        metadata = features.get("meta", {})
        result.update({
            "http_status": status,
            "final_url": final_url,
            "content_type": content_type,
            "title": features.get("title", ""),
            "metadata": metadata,
            "meta_description": metadata.get("description", "") or metadata.get("og:description", ""),
            "canonical_url": features.get("canonical_url", ""),
            "headings": features.get("headings", []),
            "links": features.get("links", []),
            "pdf_links": features.get("pdf_links", []),
            "schema_types_detected": features.get("schema_types_detected", []),
            "schema_types": features.get("schema_types", []),
            "json_ld_present": bool(features.get("json_ld_present")),
            "json_ld_block_count": int(features.get("json_ld_block_count") or 0),
            "text": features.get("text", ""),
            "markdown": markdown,
            "word_count": word_count,
            "crawl_status": "success" if word_count >= 20 else "empty_extract",
        })
        result["extraction_quality"] = extraction_quality(
            result["markdown"],
            result["headings"],
            result["metadata"],
            result["links"],
        )
        return result

    except Exception as e:
        result["error"] = str(e)[:1000]
        return result

    finally:
        result["elapsed_ms"] = int((time.time() - started) * 1000)


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
            bundle[key] = read_json(path)
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



def _domain_from_url(url: str) -> str:
    try:
        return urlparse(str(url)).netloc.lower().replace('www.', '')
    except Exception:
        return ''

def _compact_text(value: Any, max_chars: int = 900) -> str:
    if value is None:
        return ''
    if isinstance(value, str):
        return re.sub(r"\s+", " ", value).strip()[:max_chars]
    if isinstance(value, list):
        parts = [_compact_text(v, max_chars=300) for v in value[:8]]
        return re.sub(r"\s+", " ", " ".join([x for x in parts if x])).strip()[:max_chars]
    if isinstance(value, dict):
        for key in ('answer', 'summary', 'snippet', 'text', 'content', 'description'):
            if key in value:
                txt = _compact_text(value.get(key), max_chars=max_chars)
                if txt:
                    return txt
    return ''

def _walk_dicts(value: Any):
    if isinstance(value, dict):
        yield value
        for v in value.values():
            yield from _walk_dicts(v)
    elif isinstance(value, list):
        for v in value:
            yield from _walk_dicts(v)

def _extract_serpapi_references(raw: dict[str, Any], limit: int = 10) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add_ref(obj: dict[str, Any], position: int | None = None):
        url = obj.get('link') or obj.get('url') or obj.get('source_url') or obj.get('citation_url') or obj.get('redirect_link')
        if not isinstance(url, str) or not url.startswith(('http://', 'https://')):
            return
        if url in seen:
            return
        seen.add(url)
        refs.append({
            'rank': len(refs) + 1 if position is None else position,
            'title': _compact_text(obj.get('title') or obj.get('source') or obj.get('name') or _domain_from_url(url), 220),
            'url': url,
            'source_url': url,
            'source_domain': _domain_from_url(url),
            'source_name': _compact_text(obj.get('source') or obj.get('name') or _domain_from_url(url), 120),
            'snippet': _compact_text(obj.get('snippet') or obj.get('description') or obj.get('text'), 500),
            'source_type': 'external_citation',
        })

    preferred_keys = ('references', 'citations', 'sources', 'source_links')
    for obj in _walk_dicts(raw):
        for key in preferred_keys:
            val = obj.get(key)
            if isinstance(val, list):
                for item in val:
                    if isinstance(item, dict):
                        add_ref(item)
                    elif isinstance(item, str) and item.startswith(('http://', 'https://')):
                        add_ref({'url': item})
            elif isinstance(val, dict):
                add_ref(val)

    # Fallback to organic/search results if AI-mode specific references are not exposed.
    for key in ('organic_results', 'top_stories', 'news_results', 'knowledge_graph'):
        val = raw.get(key)
        if isinstance(val, list):
            for item in val:
                if isinstance(item, dict):
                    add_ref(item)
        elif isinstance(val, dict):
            add_ref(val)

    return refs[:limit]

def _extract_serpapi_answer_summary(raw: dict[str, Any]) -> str:
    for key in ('ai_overview', 'ai_mode', 'answer_box', 'knowledge_graph'):
        block = raw.get(key)
        txt = _compact_text(block, 1200)
        if txt:
            return txt
    for obj in _walk_dicts(raw):
        for key in ('answer', 'summary', 'snippet', 'text'):
            txt = _compact_text(obj.get(key), 1200) if isinstance(obj, dict) else ''
            if txt and len(txt) > 40:
                return txt
    return ''

def normalise_serpapi_row(query_id: str, query_text: str, q: dict[str, Any], raw: dict[str, Any], raw_file: str | None = None) -> dict[str, Any]:
    refs = _extract_serpapi_references(raw, limit=10)
    summary = _extract_serpapi_answer_summary(raw)
    status = 'serpapi_completed_with_citations' if refs else 'serpapi_completed_no_citations'
    return {
        'query_id': query_id,
        'query': query_text,
        'query_type': q.get('query_type', ''),
        'brand_topic_category': q.get('brand_topic_category') or q.get('journey_category') or q.get('topic') or '',
        'journey_stage': q.get('journey_stage') or q.get('journey_category') or '',
        'intent': q.get('intent', ''),
        'answer_summary': summary or ('SerpAPI completed but no AI answer summary was returned.' if not refs else 'SerpAPI completed; citation references were returned.'),
        'references': refs,
        'top_citations': refs[:3],
        'top_cited_sources': refs[:3],
        'citation_count': len(refs),
        'status': status,
        'raw_response_keys': list(raw.keys())[:50] if isinstance(raw, dict) else [],
        'reconstructed_markdown': _compact_text(raw.get('reconstructed_markdown'), 5000) if isinstance(raw, dict) else '',
        'search_parameters': raw.get('search_parameters', {}) if isinstance(raw, dict) else {},
        'search_metadata': raw.get('search_metadata', {}) if isinstance(raw, dict) else {},
        'raw_file': raw_file,
    }

def run_serpapi_collection(job_id: str, req: SerpApiJobRequest):
    try:
        if not SERPAPI_KEY:
            raise RuntimeError("SERPAPI_KEY is not set in Railway variables.")

        run_dir = DATA_DIR / req.target_run_id
        raw_dir = run_dir / "google_ai_mode" / "raw"
        raw_dir.mkdir(parents=True, exist_ok=True)

        compact_rows = []
        queries = req.queries[: max(0, req.max_queries)]

        update_job(job_id, {"status": "running", "stage": "serpapi_collect", "query_count": len(queries)})

        for i, q in enumerate(queries, start=1):
            query_text = q.get("query") or q.get("q") or q.get("text") or ""
            query_id = q.get("query_id") or q.get("id") or f"q{i:03d}"

            if not query_text:
                continue

            update_job(job_id, {"current": i, "total": len(queries), "current_query": query_text})

            params = {
                "engine": SERPAPI_ENGINE,
                "q": query_text,
                "api_key": SERPAPI_KEY,
                "hl": "en",  # Executive dashboard output language; keep gl market-localised separately.
                "gl": "jp" if req.market.lower() == "japan" else "us",
            }

            resp = requests.get("https://serpapi.com/search.json", params=params, timeout=90)
            raw = resp.json()

            write_json(raw_dir / f"{query_id}.json", raw)

            compact_rows.append(normalise_serpapi_row(
                query_id=query_id,
                query_text=query_text,
                q=q,
                raw=raw if isinstance(raw, dict) else {},
                raw_file=f"google_ai_mode/raw/{query_id}.json",
            ))

        google_payload = {
            "schema_version": "google_ai_mode_compact.v2",
            "run_id": req.target_run_id,
            "brand": req.brand,
            "market": req.market,
            "source": "serpapi_live",
            "generated_at_epoch": now_epoch(),
            "rows": compact_rows,
            "queries": compact_rows,
            "summary": {
                "attempted_queries": len(queries),
                "captured_queries": len(compact_rows),
                "engine": SERPAPI_ENGINE,
                "queries_with_citations": sum(1 for r in compact_rows if r.get("citation_count", 0) > 0),
                "total_citations": sum(int(r.get("citation_count") or 0) for r in compact_rows),
            },
        }

        write_json(run_dir / "google_ai_mode_compact.json", google_payload)

        update_job(job_id, {
            "status": "completed",
            "stage": "done",
            "target_run_id": req.target_run_id,
            "captured_queries": len(compact_rows),
            "completed_at_epoch": now_epoch(),
        })

    except Exception as e:
        update_job(job_id, {
            "status": "failed",
            "stage": "error",
            "error": str(e)[:1000],
            "failed_at_epoch": now_epoch(),
        })


def run_full_refresh(job_id: str, req: FullRefreshRequest):
    try:
        target_dir = DATA_DIR / req.target_run_id
        target_dir.mkdir(parents=True, exist_ok=True)

        source_dir = DATA_DIR / req.source_run_id if req.source_run_id else None
        if req.source_run_id and (not source_dir or not source_dir.exists()):
            raise FileNotFoundError(f"Source run does not exist: {source_dir}")

        update_job(job_id, {
            "status": "running",
            "stage": "initialise",
            "source_run_id": req.source_run_id,
            "target_run_id": req.target_run_id,
        })

        if source_dir:
            copy_baseline_files(source_dir, target_dir)

        owned_urls, external_urls = extract_inventory(source_dir or target_dir, req)

        update_job(job_id, {
            "stage": "inventory_built",
            "owned_url_count": len(owned_urls),
            "external_url_count": len(external_urls),
        })

        owned_pages = []
        if req.crawl_owned:
            for i, url in enumerate(owned_urls, start=1):
                update_job(job_id, {
                    "stage": "crawl_owned",
                    "current": i,
                    "total": len(owned_urls),
                    "current_url": url,
                })
                owned_pages.append(crawl_one_url(
                    url,
                    timeout=req.timeout_seconds,
                    use_playwright_fallback=req.use_playwright_fallback,
                ))

        external_pages = []
        if req.crawl_external:
            for i, url in enumerate(external_urls, start=1):
                update_job(job_id, {
                    "stage": "crawl_external",
                    "current": i,
                    "total": len(external_urls),
                    "current_url": url,
                })
                external_pages.append(crawl_one_url(
                    url,
                    timeout=req.timeout_seconds,
                    use_playwright_fallback=req.use_playwright_fallback,
                ))

        owned_payload = {
            "run_id": req.target_run_id,
            "brand": req.brand,
            "market": req.market,
            "generated_at_epoch": now_epoch(),
            "source": "railway_full_crawler",
            "pages": owned_pages,
            "summary": {
                "attempted": len(owned_pages),
                "successful": sum(1 for p in owned_pages if p.get("crawl_status") == "success"),
                "playwright_available": sync_playwright is not None,
                "trafilatura_available": trafilatura is not None,
                "pdf_parser_available": PdfReader is not None,
            },
        }

        external_payload = {
            "run_id": req.target_run_id,
            "brand": req.brand,
            "market": req.market,
            "generated_at_epoch": now_epoch(),
            "source": "railway_full_crawler",
            "pages": external_pages,
            "summary": {
                "attempted": len(external_pages),
                "successful": sum(1 for p in external_pages if p.get("crawl_status") == "success"),
                "playwright_available": sync_playwright is not None,
                "trafilatura_available": trafilatura is not None,
                "pdf_parser_available": PdfReader is not None,
            },
        }

        write_json(target_dir / "owned_pages_full.json", owned_payload)
        write_json(target_dir / "external_pages_full.json", external_payload)

        build_compact_bundle(target_dir)

        update_job(job_id, {
            "status": "completed",
            "stage": "done",
            "target_run_id": req.target_run_id,
            "owned_attempted": len(owned_pages),
            "owned_successful": owned_payload["summary"]["successful"],
            "external_attempted": len(external_pages),
            "external_successful": external_payload["summary"]["successful"],
            "completed_at_epoch": now_epoch(),
        })

    except Exception as e:
        update_job(job_id, {
            "status": "failed",
            "stage": "error",
            "error": str(e)[:1500],
            "failed_at_epoch": now_epoch(),
        })


@router.post("/jobs/full-refresh")
def create_full_refresh(req: FullRefreshRequest, x_admin_token: str | None = Header(default=None)):
    require_admin(x_admin_token)

    job_id = make_job_id("fullrefresh")

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

    thread = threading.Thread(target=run_full_refresh, args=(job_id, req), daemon=True)
    thread.start()

    return {
        "status": "accepted",
        "job_id": job_id,
        "source_run_id": req.source_run_id,
        "target_run_id": req.target_run_id,
    }


@router.post("/jobs/collect-serpapi")
def create_serpapi_job(req: SerpApiJobRequest, x_admin_token: str | None = Header(default=None)):
    require_admin(x_admin_token)

    job_id = make_job_id("serpapi")

    update_job(job_id, {
        "status": "accepted",
        "job_id": job_id,
        "target_run_id": req.target_run_id,
        "brand": req.brand,
        "market": req.market,
        "created_at_epoch": now_epoch(),
        "request": req.model_dump(),
    })

    thread = threading.Thread(target=run_serpapi_collection, args=(job_id, req), daemon=True)
    thread.start()

    return {
        "status": "accepted",
        "job_id": job_id,
        "target_run_id": req.target_run_id,
    }


@router.get("/runs/{run_id}/files/{filename}")
def get_run_file(run_id: str, filename: str):
    allowed = {
        "audit_context.json",
        "evidence_scope.json",
        "google_ai_mode_compact.json",
        "owned_pages_full.json",
        "external_pages_full.json",
        "visibility_matrix.json",
        "source_classification.json",
        "compact_bundle.json",
        "manifest.json",
        "run_manifest.json",
    }

    if filename not in allowed:
        raise HTTPException(status_code=400, detail="File is not exposed by this endpoint.")

    path = DATA_DIR / run_id / filename
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"File not found: {filename}")

    return JSONResponse(content=read_json(path))
