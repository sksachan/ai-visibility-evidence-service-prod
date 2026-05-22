"""Brand configuration persistence and CRUD.

Stores brand configurations (owned_domains, brand_terms, default settings)
on the Railway persistent volume as JSON files. Each brand/market pair
gets its own config file under DATA_DIR/brand_configs/.
"""
from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Query
from pydantic import BaseModel, Field

router = APIRouter(tags=["brand-config"])

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data/evidence-runs"))
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")


def now_epoch() -> int:
    return int(time.time())


def normalise_key(value: Any) -> str:
    return str(value or "").strip().lower().replace("https://", "").replace("http://", "").replace("/", "_").replace(" ", "_").replace(":", "_")


def brand_config_dir() -> Path:
    return DATA_DIR / "brand_configs"


def config_key(brand: str, market: str) -> str:
    return f"{normalise_key(brand)}__{normalise_key(market)}"


def config_path(brand: str, market: str) -> Path:
    return brand_config_dir() / f"{config_key(brand, market)}.json"


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


def require_admin(token: str | None) -> None:
    if ADMIN_TOKEN and token != ADMIN_TOKEN:
        raise HTTPException(status_code=401, detail="Invalid admin token")


# ── Pydantic models ────────────────────────────────────────────────────────

class BrandConfigRequest(BaseModel):
    """Request body for creating or updating a brand configuration."""
    brand: str
    market: str
    domain: str = ""
    owned_domains: list[str] = Field(default_factory=list)
    brand_terms: list[str] = Field(default_factory=list)
    language: str = "English"
    default_sitemap_url: str = ""
    default_seed_topics: str = ""
    default_topic_count: int = 8
    default_queries_per_topic: int = 6
    default_query_limit: int = 50
    default_portfolio_goal: str = "AI answer visibility audit query portfolio."
    notes: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class BrandConfigResponse(BaseModel):
    """Stored brand configuration."""
    config_id: str
    brand: str
    market: str
    domain: str = ""
    owned_domains: list[str] = Field(default_factory=list)
    brand_terms: list[str] = Field(default_factory=list)
    language: str = "English"
    default_sitemap_url: str = ""
    default_seed_topics: str = ""
    default_topic_count: int = 8
    default_queries_per_topic: int = 6
    default_query_limit: int = 50
    default_portfolio_goal: str = "AI answer visibility audit query portfolio."
    notes: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at_epoch: int = 0
    updated_at_epoch: int = 0


# ── CRUD helpers ───────────────────────────────────────────────────────────

def save_brand_config(req: BrandConfigRequest) -> dict[str, Any]:
    """Create or update a brand configuration. Returns the stored config."""
    existing = read_json(config_path(req.brand, req.market))
    config_id = (
        existing.get("config_id")
        if isinstance(existing, dict) and existing.get("config_id")
        else f"brand_{normalise_key(req.brand)}_{normalise_key(req.market)}_{uuid.uuid4().hex[:6]}"
    )
    created = (
        existing.get("created_at_epoch", now_epoch())
        if isinstance(existing, dict)
        else now_epoch()
    )
    config = {
        "config_id": config_id,
        **req.model_dump(),
        "created_at_epoch": created,
        "updated_at_epoch": now_epoch(),
    }
    write_json(config_path(req.brand, req.market), config)
    return config


def load_brand_config(brand: str, market: str) -> dict[str, Any] | None:
    """Load a brand configuration by brand/market."""
    return read_json(config_path(brand, market))


def list_brand_configs() -> list[dict[str, Any]]:
    """List all stored brand configurations."""
    configs: list[dict[str, Any]] = []
    root = brand_config_dir()
    if not root.exists():
        return configs
    for path in sorted(root.glob("*.json")):
        config = read_json(path)
        if isinstance(config, dict) and config.get("brand") and config.get("market"):
            configs.append(config)
    configs.sort(key=lambda c: (c.get("brand", "").lower(), c.get("market", "").lower()))
    return configs


def delete_brand_config(brand: str, market: str) -> bool:
    """Delete a brand configuration. Returns True if deleted."""
    path = config_path(brand, market)
    if path.exists():
        path.unlink()
        return True
    return False


# ── API routes ────────────────────────────────────────────────────────────

@router.post("/brands")
def create_or_update_brand_config(
    req: BrandConfigRequest,
    x_admin_token: str | None = Header(default=None),
):
    """Create or update a brand configuration.

    If a config for the same brand/market already exists, it is updated.
    """
    require_admin(x_admin_token)
    if not req.brand.strip():
        raise HTTPException(status_code=400, detail="'brand' is required")
    if not req.market.strip():
        raise HTTPException(status_code=400, detail="'market' is required")
    config = save_brand_config(req)
    return {"status": "saved", "config": config}


@router.get("/brands")
def get_brand_configs(
    brand: str | None = Query(default=None),
    market: str | None = Query(default=None),
):
    """List brand configurations, optionally filtered by brand and/or market."""
    configs = list_brand_configs()
    if brand:
        configs = [c for c in configs if normalise_key(c.get("brand")) == normalise_key(brand)]
    if market:
        configs = [c for c in configs if normalise_key(c.get("market")) == normalise_key(market)]
    return {"status": "ok", "count": len(configs), "brands": configs}


@router.get("/brands/{brand}/{market}")
def get_brand_config(brand: str, market: str):
    """Get a specific brand configuration."""
    config = load_brand_config(brand, market)
    if not config:
        raise HTTPException(status_code=404, detail=f"No brand config found for {brand}/{market}")
    return config


@router.delete("/brands/{brand}/{market}")
def remove_brand_config(
    brand: str,
    market: str,
    x_admin_token: str | None = Header(default=None),
):
    """Delete a brand configuration."""
    require_admin(x_admin_token)
    deleted = delete_brand_config(brand, market)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"No brand config found for {brand}/{market}")
    return {"status": "deleted", "brand": brand, "market": market}
