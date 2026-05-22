from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel


router = APIRouter()

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data/evidence-runs"))
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")


class CrawlRequest(BaseModel):
    source_run_id: str
    target_run_id: str
    brand: str = "Nissan"
    market: str = "Japan"
    crawl_owned: bool = True
    crawl_external: bool = True
    max_owned_urls: int = 5
    max_external_urls: int = 5
    recrawl: bool = True


class TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.skip = False
        self.parts: list[str] = []
        self.title_parts: list[str] = []
        self.in_title = False

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in {"script", "style", "noscript", "svg"}:
            self.skip = True
        if tag == "title":
            self.in_title = True
        if tag in {"h1", "h2", "h3", "p", "li", "td", "th", "div", "section", "article"}:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in {"script", "style", "noscript", "svg"}:
            self.skip = False
        if tag == "title":
            self.in_title = False
        if tag in {"h1", "h2", "h3", "p", "li", "tr", "section", "article"}:
            self.parts.append("\n")

    def handle_data(self, data):
        if self.skip:
            return
        text = data.strip()
        if not text:
            return
        if self.in_title:
            self.title_parts.append(text)
        self.parts.append(text + " ")

    @property
    def title(self) -> str:
        return " ".join(self.title_parts).strip()

    @property
    def text(self) -> str:
        raw = "".join(self.parts)
        raw = re.sub(r"\n{3,}", "\n\n", raw)
        raw = re.sub(r"[ \t]{2,}", " ", raw)
        return raw.strip()


def require_admin(token: str | None):
    if ADMIN_TOKEN and token != ADMIN_TOKEN:
        raise HTTPException(status_code=401, detail="Invalid admin token")


def job_path(job_id: str) -> Path:
    return DATA_DIR / "_jobs" / f"{job_id}.json"


def write_json(path: Path, value: Any):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def update_job(job_id: str, patch: dict[str, Any]):
    path = job_path(job_id)
    current = {}
    if path.exists():
        current = read_json(path)
    current.update(patch)
    current["updated_at_epoch"] = int(time.time())
    write_json(path, current)


def normalise_url(url: str) -> str | None:
    if not isinstance(url, str):
        return None
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        return None
    parsed = urlparse(url)
    if not parsed.netloc:
        return None
    return url.split("#")[0]


def collect_urls_from_obj(obj: Any, owned_domains: set[str]) -> tuple[list[str], list[str]]:
    owned: list[str] = []
    external: list[str] = []

    def walk(x: Any):
        if isinstance(x, dict):
            for k, v in x.items():
                key = str(k).lower()
                if key in {"url", "source_url", "page_url", "owned_url", "target_url", "canonical_url"} and isinstance(v, str):
                    u = normalise_url(v)
                    if u:
                        host = urlparse(u).netloc.lower()
                        if any(host.endswith(d) for d in owned_domains):
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


def dedupe(urls: list[str]) -> list[str]:
    seen = set()
    out = []
    for u in urls:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def fetch_page(url: str, timeout: int = 30) -> dict[str, Any]:
    started = time.time()
    result: dict[str, Any] = {
        "url": url,
        "source_url": url,
        "crawl_status": "failed",
        "http_status": None,
        "title": "",
        "markdown": "",
        "text": "",
        "word_count": 0,
        "content_length": 0,
        "error": None,
        "fetched_at_epoch": None,
        "elapsed_ms": None,
    }

    try:
        req = Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 AIVisibilityEvidenceBot/1.0",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            },
        )
        with urlopen(req, timeout=timeout) as resp:
            body = resp.read(2_500_000)
            status = getattr(resp, "status", None)
            content_type = resp.headers.get("content-type", "")

        result["http_status"] = status
        result["content_length"] = len(body)
        result["fetched_at_epoch"] = int(time.time())

        if "html" not in content_type.lower() and not url.lower().endswith((".html", ".htm", "/")):
            result["crawl_status"] = "skipped_non_html"
            result["error"] = f"Unsupported content-type: {content_type}"
            return result

        html = body.decode("utf-8", errors="replace")
        parser = TextExtractor()
        parser.feed(html)

        text = parser.text
        result["title"] = parser.title
        result["text"] = text
        result["markdown"] = text
        result["word_count"] = len(re.findall(r"\w+", text))
        result["crawl_status"] = "success" if text else "empty_extract"
        return result

    except Exception as e:
        result["error"] = str(e)[:500]
        return result

    finally:
        result["elapsed_ms"] = int((time.time() - started) * 1000)


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
        "updated_at_epoch": int(time.time()),
    }

    write_json(run_dir / "compact_bundle.json", bundle)
    write_json(run_dir / "manifest.json", manifest)


def copy_baseline_files(source_dir: Path, target_dir: Path):
    keep = [
        "audit_context.json",
        "evidence_scope.json",
        "google_ai_mode_compact.json",
        "visibility_matrix.json",
        "source_classification.json",
    ]

    target_dir.mkdir(parents=True, exist_ok=True)

    for name in keep:
        src = source_dir / name
        if src.exists():
            (target_dir / name).write_text(src.read_text(encoding="utf-8"), encoding="utf-8")


def run_crawl_job(job_id: str, req: CrawlRequest):
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

        owned_domains = {
            "nissan.co.jp",
            "www.nissan.co.jp",
            "www2.nissan.co.jp",
            "www3.nissan.co.jp",
        }

        inventory_files = [
            source_dir / "audit_context.json",
            source_dir / "evidence_scope.json",
            source_dir / "google_ai_mode_compact.json",
            source_dir / "source_classification.json",
        ]

        owned_urls: list[str] = []
        external_urls: list[str] = []

        for path in inventory_files:
            if not path.exists():
                continue
            obj = read_json(path)
            o, e = collect_urls_from_obj(obj, owned_domains)
            owned_urls.extend(o)
            external_urls.extend(e)

        owned_urls = dedupe(owned_urls)[: max(0, req.max_owned_urls)]
        external_urls = dedupe(external_urls)[: max(0, req.max_external_urls)]

        update_job(job_id, {
            "stage": "crawl_owned",
            "owned_url_count": len(owned_urls),
            "external_url_count": len(external_urls),
        })

        owned_pages = []
        if req.crawl_owned:
            for i, url in enumerate(owned_urls, start=1):
                update_job(job_id, {"stage": "crawl_owned", "current": i, "total": len(owned_urls), "current_url": url})
                owned_pages.append(fetch_page(url))

        update_job(job_id, {"stage": "crawl_external"})

        external_pages = []
        if req.crawl_external:
            for i, url in enumerate(external_urls, start=1):
                update_job(job_id, {"stage": "crawl_external", "current": i, "total": len(external_urls), "current_url": url})
                external_pages.append(fetch_page(url))

        owned_payload = {
            "run_id": req.target_run_id,
            "brand": req.brand,
            "market": req.market,
            "generated_at_epoch": int(time.time()),
            "source": "railway_fresh_crawl_small_test",
            "pages": owned_pages,
            "summary": {
                "attempted": len(owned_pages),
                "successful": sum(1 for p in owned_pages if p.get("crawl_status") == "success"),
            },
        }

        external_payload = {
            "run_id": req.target_run_id,
            "brand": req.brand,
            "market": req.market,
            "generated_at_epoch": int(time.time()),
            "source": "railway_fresh_crawl_small_test",
            "pages": external_pages,
            "summary": {
                "attempted": len(external_pages),
                "successful": sum(1 for p in external_pages if p.get("crawl_status") == "success"),
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
            "completed_at_epoch": int(time.time()),
        })

    except Exception as e:
        update_job(job_id, {
            "status": "failed",
            "stage": "error",
            "error": str(e)[:1000],
            "failed_at_epoch": int(time.time()),
        })


@router.post("/jobs/crawl")
def create_crawl_job(req: CrawlRequest, x_admin_token: str | None = Header(default=None)):
    require_admin(x_admin_token)

    job_id = f"crawl_{int(time.time())}_{uuid.uuid4().hex[:8]}"

    update_job(job_id, {
        "status": "accepted",
        "job_id": job_id,
        "source_run_id": req.source_run_id,
        "target_run_id": req.target_run_id,
        "brand": req.brand,
        "market": req.market,
        "created_at_epoch": int(time.time()),
        "request": req.model_dump(),
    })

    thread = threading.Thread(target=run_crawl_job, args=(job_id, req), daemon=True)
    thread.start()

    return {
        "status": "accepted",
        "job_id": job_id,
        "source_run_id": req.source_run_id,
        "target_run_id": req.target_run_id,
    }


@router.get("/jobs/{job_id}")
def get_crawl_job(job_id: str):
    path = job_path(job_id)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Job not found")
    return read_json(path)


@router.get("/runs/{run_id}/manifest")
def get_run_manifest(run_id: str):
    run_dir = DATA_DIR / run_id
    manifest = run_dir / "manifest.json"
    if manifest.exists():
        return read_json(manifest)

    if not run_dir.exists():
        raise HTTPException(status_code=404, detail="Run not found")

    return {
        "run_id": run_id,
        "status": "exists_without_manifest",
        "files": sorted([p.name for p in run_dir.glob("*.json")]),
    }
