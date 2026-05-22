from app.crawl_jobs import router as crawl_jobs_router
from fastapi import FastAPI, UploadFile, File, Form, Header, HTTPException
from pathlib import Path
from typing import Optional

import json
import os
import shutil
import sys
import zipfile
from app.evidence_jobs import router as evidence_jobs_router
from app.parity_jobs import router as parity_jobs_router
from app.parity_safe_jobs import router as parity_safe_jobs_router
from app.parity_parallel_jobs import router as parity_parallel_jobs_router
from app.bodhi_compact import router as bodhi_compact_router
from app.report_store import router as report_store_router


app = FastAPI(title="AI Visibility Evidence Service")

DATA_DIR = Path(os.getenv("DATA_DIR", "/data/evidence-runs"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

ADMIN_SEED_TOKEN = os.getenv("ADMIN_SEED_TOKEN", "ad6878sd8d87sd87")

REQUIRED_FILES = {
    "audit_context.json": [
        "outputs/audit_context/audit_context.json",
        "inputs/audit_context.json"
    ],
    "evidence_scope.json": [
        "outputs/evidence_scope/evidence_scope.json"
    ],
    "google_ai_mode_compact.json": [
        "outputs/google_ai_mode/google_ai_mode_compact.json"
    ],
    "owned_pages_full.json": [
        "outputs/content_intelligence/owned_pages_full.json"
    ],
    "external_pages_full.json": [
        "outputs/external_pages/external_pages_full.json"
    ],
    "visibility_matrix.json": [
        "outputs/visibility/visibility_matrix.json"
    ],
    "source_classification.json": [
        "outputs/source_landscape/source_classification.json"
    ]
}


def normalise_key(value: str) -> str:
    return value.lower().replace(" ", "_")


def read_json_file(path: Path, default=None):
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def find_first(root: Path, candidates: list[str]) -> Optional[Path]:
    for rel in candidates:
        candidate = root / rel
        if candidate.exists():
            return candidate
    return None


def seed_from_zip(zip_path: Path, brand: str, market: str, run_id: str):
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    run_dir = DATA_DIR / run_id
    latest_dir = DATA_DIR / "latest"
    tmp_dir = DATA_DIR / "_tmp_seed_extract"

    if tmp_dir.exists():
        shutil.rmtree(tmp_dir)
    tmp_dir.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(zip_path, "r") as zf:
        zf.extractall(tmp_dir)

    possible_roots = [tmp_dir] + [p for p in tmp_dir.iterdir() if p.is_dir()]
    root = None

    for candidate in possible_roots:
        if (candidate / "outputs").exists() or (candidate / "inputs").exists():
            root = candidate
            break

    if root is None:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise FileNotFoundError("Could not find outputs/ or inputs/ inside uploaded zip")

    run_dir.mkdir(parents=True, exist_ok=True)

    copied = {}
    missing = []

    for output_name, candidates in REQUIRED_FILES.items():
        src = find_first(root, candidates)
        if src:
            dest = run_dir / output_name
            shutil.copyfile(src, dest)
            copied[output_name] = str(dest)
        else:
            missing.append(output_name)

    compact_bundle = {
        "status": "ready" if not missing else "partial",
        "run_id": run_id,
        "brand": brand,
        "market": market,
        "files": {
            "audit_context": read_json_file(run_dir / "audit_context.json", {}),
            "evidence_scope": read_json_file(run_dir / "evidence_scope.json", {}),
            "google_ai_mode_compact": read_json_file(run_dir / "google_ai_mode_compact.json", {}),
            "owned_pages_full": read_json_file(run_dir / "owned_pages_full.json", {}),
            "external_pages_full": read_json_file(run_dir / "external_pages_full.json", {}),
            "visibility_matrix": read_json_file(run_dir / "visibility_matrix.json", {}),
            "source_classification": read_json_file(run_dir / "source_classification.json", {})
        },
        "counts": {
            "missing_required_files": len(missing)
        },
        "missing_files": missing
    }

    compact_path = run_dir / "compact_bundle.json"
    compact_path.write_text(
        json.dumps(compact_bundle, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )

    manifest = {
        "run_id": run_id,
        "brand": brand,
        "market": market,
        "status": compact_bundle["status"],
        "copied_files": copied,
        "missing_files": missing,
        "compact_bundle": str(compact_path)
    }

    (run_dir / "run_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )

    latest_dir.mkdir(parents=True, exist_ok=True)
    latest_key = f"{normalise_key(brand)}_{normalise_key(market)}.json"
    (latest_dir / latest_key).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )

    shutil.rmtree(tmp_dir, ignore_errors=True)

    return manifest


@app.post("/admin/seed-run")
async def seed_run(
    file: UploadFile = File(...),
    brand: str = Form("Nissan"),
    market: str = Form("Japan"),
    run_id: str = Form("nissan_japan_demo_v1"),
    x_admin_token: Optional[str] = Header(None)
):
    if ADMIN_SEED_TOKEN and x_admin_token != ADMIN_SEED_TOKEN:
        raise HTTPException(status_code=401, detail="Invalid admin token")

    DATA_DIR.mkdir(parents=True, exist_ok=True)

    upload_dir = DATA_DIR / "_uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)

    zip_path = upload_dir / f"{run_id}.zip"

    with zip_path.open("wb") as f:
        while True:
            chunk = await file.read(1024 * 1024)
            if not chunk:
                break
            f.write(chunk)

    try:
        manifest = seed_from_zip(zip_path, brand=brand, market=market, run_id=run_id)
    finally:
        zip_path.unlink(missing_ok=True)

    return {
        "status": "seeded",
        "manifest": manifest
    }

@app.get("/runs/latest")
def get_latest_run(brand: str, market: str):
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    key = f"{brand.lower()}_{market.lower()}".replace(" ", "_")
    latest_path = DATA_DIR / "latest" / f"{key}.json"

    if not latest_path.exists():
        return {
            "status": "not_found",
            "message": "No latest run found for this brand/market",
            "brand": brand,
            "market": market,
            "expected_path": str(latest_path),
            "data_dir": str(DATA_DIR)
        }

    return json.loads(latest_path.read_text(encoding="utf-8"))


@app.get("/runs/{run_id}/compact")
def get_compact_run(run_id: str):
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    compact_path = DATA_DIR / run_id / "compact_bundle.json"

    if not compact_path.exists():
        return {
            "status": "not_found",
            "message": "No compact bundle found for this run_id",
            "run_id": run_id,
            "expected_path": str(compact_path),
            "data_dir": str(DATA_DIR)
        }

    return json.loads(compact_path.read_text(encoding="utf-8"))


@app.get("/runs/{run_id}/manifest")
def get_run_manifest(run_id: str):
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    manifest_path = DATA_DIR / run_id / "run_manifest.json"

    if not manifest_path.exists():
        return {
            "status": "not_found",
            "message": "No manifest found for this run_id",
            "run_id": run_id,
            "expected_path": str(manifest_path),
            "data_dir": str(DATA_DIR)
        }

    return json.loads(manifest_path.read_text(encoding="utf-8"))


@app.get("/debug/routes")
def debug_routes():
    return {
        "routes": [
            {
                "path": route.path,
                "methods": sorted(list(route.methods or []))
            }
            for route in app.routes
        ]
    }

@app.get("/health")
def health():
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    return {
        "status": "ok",
        "service": "ai-visibility-evidence-service",
        "python": sys.version,
        "data_dir": str(DATA_DIR),
        "data_dir_exists": DATA_DIR.exists(),
        "data_dir_is_dir": DATA_DIR.is_dir(),
        "volume_root_exists": Path("/data").exists(),
        "volume_root_is_dir": Path("/data").is_dir(),
        "port_env": os.getenv("PORT")
    }

app.include_router(crawl_jobs_router)

app.include_router(evidence_jobs_router)

app.include_router(parity_jobs_router)

app.include_router(parity_safe_jobs_router)

app.include_router(parity_parallel_jobs_router)

app.include_router(bodhi_compact_router)

app.include_router(report_store_router)
