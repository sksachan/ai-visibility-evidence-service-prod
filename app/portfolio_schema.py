"""Portfolio schema validation and template generation.

Provides validation for uploaded query portfolios against the
brand_topic_query_portfolio.v1 contract, plus downloadable template
generation so users can prepare portfolios offline.
"""
from __future__ import annotations

import json
import re
from typing import Any


# ── Schema version ──────────────────────────────────────────────────────────
SCHEMA_VERSION = "brand_topic_query_portfolio.v1"

# ── Allowed journey stages ──────────────────────────────────────────────────
ALLOWED_JOURNEY_STAGES = {
    "awareness", "consideration", "decision", "retention",
    "advocacy", "research", "comparison", "purchase",
    "post_purchase", "support", "loyalty",
}

# ── Allowed query types ─────────────────────────────────────────────────────
ALLOWED_QUERY_TYPES = {
    "branded", "non_branded", "competitor_comparison",
    "informational", "transactional", "navigational",
    "local", "long_tail",
}


def _str(value: Any) -> str:
    return str(value or "").strip()


def _is_nonempty_str(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def validate_topic(topic: Any, index: int) -> list[str]:
    """Validate a single topic entry. Returns a list of error strings."""
    errors: list[str] = []
    prefix = f"topics[{index}]"
    if isinstance(topic, str):
        if not topic.strip():
            errors.append(f"{prefix}: topic string is empty")
        return errors
    if not isinstance(topic, dict):
        errors.append(f"{prefix}: must be a string or object, got {type(topic).__name__}")
        return errors
    if not _is_nonempty_str(topic.get("topic")) and not _is_nonempty_str(topic.get("name")):
        errors.append(f"{prefix}: missing required field 'topic' or 'name'")
    return errors


def validate_query(query: Any, index: int) -> list[str]:
    """Validate a single query entry. Returns a list of error strings."""
    errors: list[str] = []
    prefix = f"queries[{index}]"
    if isinstance(query, str):
        if not query.strip():
            errors.append(f"{prefix}: query string is empty")
        return errors
    if not isinstance(query, dict):
        errors.append(f"{prefix}: must be a string or object, got {type(query).__name__}")
        return errors
    if not _is_nonempty_str(query.get("query")):
        errors.append(f"{prefix}: missing required field 'query'")
    journey = _str(query.get("journey_stage"))
    if journey and journey.lower() not in ALLOWED_JOURNEY_STAGES:
        errors.append(f"{prefix}: journey_stage '{journey}' is not recognised (allowed: {', '.join(sorted(ALLOWED_JOURNEY_STAGES))})")
    qtype = _str(query.get("query_type"))
    if qtype and qtype.lower() not in ALLOWED_QUERY_TYPES:
        errors.append(f"{prefix}: query_type '{qtype}' is not recognised (allowed: {', '.join(sorted(ALLOWED_QUERY_TYPES))})")
    return errors


def validate_portfolio(payload: Any) -> dict[str, Any]:
    """Validate a portfolio payload against the schema.

    Returns:
        {
            "valid": bool,
            "errors": list[str],
            "warnings": list[str],
            "stats": {"topic_count": int, "query_count": int},
        }
    """
    errors: list[str] = []
    warnings: list[str] = []

    if not isinstance(payload, dict):
        return {"valid": False, "errors": ["Payload must be a JSON object"], "warnings": [], "stats": {}}

    # ── Required top-level fields ───────────────────────────────────────────
    topics = payload.get("topics")
    queries = payload.get("queries")

    if not isinstance(topics, list) or not topics:
        errors.append("'topics' must be a non-empty array")
    if not isinstance(queries, list) or not queries:
        errors.append("'queries' must be a non-empty array")

    if errors:
        return {"valid": False, "errors": errors, "warnings": warnings, "stats": {}}

    # ── Validate schema_version ──────────────────────────────────────────────
    sv = _str(payload.get("schema_version"))
    if sv and not sv.startswith("brand_topic_query_portfolio"):
        warnings.append(f"schema_version '{sv}' does not match expected prefix 'brand_topic_query_portfolio'")

    # ── Validate brand/market ────────────────────────────────────────────────
    if not _is_nonempty_str(payload.get("brand")):
        warnings.append("'brand' is missing or empty; it will be populated from the refresh request")
    if not _is_nonempty_str(payload.get("market")):
        warnings.append("'market' is missing or empty; it will be populated from the refresh request")

    # ── Validate individual topics ──────────────────────────────────────────
    for i, topic in enumerate(topics):
        errors.extend(validate_topic(topic, i))

    # ── Validate individual queries ─────────────────────────────────────────
    seen_ids: set[str] = set()
    seen_texts: set[str] = set()
    for i, query in enumerate(queries):
        errors.extend(validate_query(query, i))
        if isinstance(query, dict):
            qid = _str(query.get("query_id"))
            if qid:
                if qid in seen_ids:
                    warnings.append(f"queries[{i}]: duplicate query_id '{qid}'")
                seen_ids.add(qid)
            qtext = _str(query.get("query")).lower()
            if qtext:
                if qtext in seen_texts:
                    warnings.append(f"queries[{i}]: duplicate query text '{qtext}'")
                seen_texts.add(qtext)

    # ── Size limits ─────────────────────────────────────────────────────────
    if len(queries) > 200:
        warnings.append(f"Portfolio contains {len(queries)} queries; consider limiting to ≤200 for optimal performance")
    if len(topics) > 30:
        warnings.append(f"Portfolio contains {len(topics)} topics; consider limiting to ≤30 for balanced coverage")

    stats = {
        "topic_count": len(topics),
        "query_count": len(queries),
        "queries_with_ids": len(seen_ids),
        "unique_query_texts": len(seen_texts),
    }

    return {
        "valid": len(errors) == 0,
        "errors": errors,
        "warnings": warnings,
        "stats": stats,
    }


def normalise_uploaded_portfolio(
    payload: dict[str, Any],
    brand: str | None = None,
    market: str | None = None,
    domain: str | None = None,
) -> dict[str, Any]:
    """Normalise an uploaded portfolio into canonical shape.

    Assigns query_ids where missing, sets schema_version, and populates
    brand/market/domain from the request if not present in the payload.
    """
    out = dict(payload)
    out.setdefault("schema_version", SCHEMA_VERSION)
    if brand and not _is_nonempty_str(out.get("brand")):
        out["brand"] = brand
    if market and not _is_nonempty_str(out.get("market")):
        out["market"] = market
    if domain and not _is_nonempty_str(out.get("domain")):
        out["domain"] = domain
    out.setdefault("portfolio_source", "user_upload")

    # Normalise topics
    topics = []
    for t in out.get("topics") or []:
        if isinstance(t, str):
            topics.append({"topic": t.strip()})
        elif isinstance(t, dict):
            topics.append(t)
    out["topics"] = topics

    # Normalise queries and assign IDs
    queries = []
    for i, q in enumerate(out.get("queries") or [], start=1):
        if isinstance(q, str):
            q = {"query": q.strip()}
        elif not isinstance(q, dict):
            continue
        q = dict(q)
        if not _is_nonempty_str(q.get("query_id")):
            q["query_id"] = f"q{i:03d}"
        queries.append(q)
    out["queries"] = queries

    return out


def generate_portfolio_template(brand: str = "", market: str = "", domain: str = "") -> dict[str, Any]:
    """Generate a downloadable portfolio template with example data."""
    return {
        "schema_version": SCHEMA_VERSION,
        "brand": brand or "YourBrand",
        "market": market or "YourMarket",
        "domain": domain or "https://www.yourbrand.com",
        "portfolio_source": "user_upload",
        "topics": [
            {
                "topic": "Electric vehicles",
                "description": "Consumer interest in EV models, range, charging, and comparisons",
                "rationale": "High-volume AI answer category with growing search intent",
            },
            {
                "topic": "Safety technology",
                "description": "Advanced driver assistance, crash ratings, and safety certifications",
                "rationale": "Frequently cited in AI answers with authority-weighted sources",
            },
            {
                "topic": "Pricing and financing",
                "description": "Vehicle pricing, financing options, lease deals, and incentives",
                "rationale": "Decision-stage queries where owned content should be the primary source",
            },
        ],
        "queries": [
            {
                "query_id": "q001",
                "query": "best electric SUV 2025",
                "topic": "Electric vehicles",
                "query_type": "non_branded",
                "journey_stage": "consideration",
                "intent": "Compare EV SUV options across brands",
                "priority": "high",
                "recommended_page_type": "EV landing page or model comparison",
            },
            {
                "query_id": "q002",
                "query": "YourBrand EV range and charging time",
                "topic": "Electric vehicles",
                "query_type": "branded",
                "journey_stage": "research",
                "intent": "Find specific EV specs for the brand",
                "priority": "high",
                "recommended_page_type": "Model specification page",
            },
            {
                "query_id": "q003",
                "query": "safest family car crash test ratings",
                "topic": "Safety technology",
                "query_type": "non_branded",
                "journey_stage": "awareness",
                "intent": "Discover which cars score highest in safety tests",
                "priority": "medium",
                "recommended_page_type": "Safety technology overview",
            },
            {
                "query_id": "q004",
                "query": "YourBrand financing options monthly payment",
                "topic": "Pricing and financing",
                "query_type": "branded",
                "journey_stage": "decision",
                "intent": "Find financing and lease options",
                "priority": "high",
                "recommended_page_type": "Finance/offers page",
            },
        ],
        "metadata": {
            "generated_by": "ai-visibility-evidence-service",
            "template_version": "1.0",
            "instructions": [
                "Replace 'YourBrand' with your actual brand name throughout.",
                "Add topics relevant to your brand's AI visibility goals.",
                "Add queries that represent real user search intents.",
                "query_type values: branded, non_branded, competitor_comparison, informational, transactional, navigational, local, long_tail.",
                "journey_stage values: awareness, consideration, decision, retention, advocacy, research, comparison, purchase, post_purchase, support, loyalty.",
                "query_id must be unique per query (e.g. q001, q002, ...).",
                "Upload this file via the Refresh Evidence screen or POST to /portfolios/upload.",
            ],
        },
    }
