# AI Visibility Evidence Service v3.3

Railway evidence execution layer for the AI Brand Visibility dashboard.

## Core responsibilities

- Store latest successful `frontend_report_bundle` for dashboard loading.
- Track refresh run status separately from latest successful report data.
- Store synthetic/manual query portfolios.
- Execute evidence refresh stages owned by Railway: sitemap inventory, query-owned URL mapping seed, SerpAPI collection, owned URL crawl, external citation crawl.
- Trigger Bodhi Brand Topic Query Builder when `query_portfolio_mode=synthetic` and no `query_portfolio_id` is supplied.
- Optionally trigger Bodhi Auditor after evidence collection.

## Required Railway start command

```bash
python start.py
```

Do not use `$PORT` directly in the Railway Start Command. `start.py` reads and validates `PORT`.

## Environment variables

Required for app runtime:

```text
DATA_DIR=/data/evidence-runs
PUBLIC_EVIDENCE_SERVICE_URL=https://ai-visibility-evidence-service-production.up.railway.app
```

Required for synthetic portfolio orchestration:

```text
BODHI_API_BASE_URL=https://psaisuite.com/save
BODHI_PAT_TOKEN=pat_<token>
BODHI_PORTFOLIO_TASK_ID=<BrandTopicQueryBuilder task id>
BODHI_PORTFOLIO_WORKFLOW_ID=<optional workflow id>
```

Required if the service should trigger the Auditor after evidence refresh:

```text
BODHI_AUDITOR_TASK_ID=<Auditor task id>
BODHI_AUDITOR_WORKFLOW_ID=<optional workflow id>
```

Required only for live AI citation collection:

```text
SERPAPI_KEY=<serpapi key>
SERPAPI_ENGINE=google_ai_mode
```

Optional:

```text
BODHI_PORTFOLIO_TIMEOUT_SECONDS=900
BODHI_POLL_SECONDS=10
BODHI_HTTP_TIMEOUT_SECONDS=120
ADMIN_TOKEN=<optional write protection token>
```

## Important routes

```text
GET  /health
GET  /debug/routes
POST /refresh/evidence
GET  /runs/status?brand=Nissan&market=Japan
POST /runs/{run_id}/report-bundle
GET  /runs/latest/report-bundle?brand=Nissan&market=Japan
POST /portfolios
GET  /portfolios/{portfolio_id}
GET  /portfolios/latest?brand=Nissan&market=Japan
```

## Synthetic refresh flow

`POST /refresh/evidence` with:

```json
{
  "brand": "Nissan",
  "market": "Japan",
  "domain": "https://www.nissan.co.jp",
  "query_portfolio_mode": "synthetic",
  "query_portfolio_id": "",
  "topic_count": 8,
  "queries_per_topic": 6,
  "query_limit": 50,
  "max_owned_pages_per_query": 3,
  "run_serpapi": false,
  "crawl_owned": true,
  "crawl_external": false,
  "trigger_auditor": true
}
```

The service returns immediately with a `target_run_id`. Poll:

```text
GET /runs/{target_run_id}/status
GET /runs/status?brand=Nissan&market=Japan
```

The dashboard should keep using `/runs/latest/report-bundle` until a new successful report bundle is stored.

## v3.4 HITL/UI-node automation

Bodhi API-created runs pause at UI nodes. v3.4 now submits the first pending HITL task automatically after creating the Brand Topic Query Builder run.

The HITL response uses the exact UI field labels expected by the portfolio workflow:

```json
{
  "brand": "Nissan",
  "market": "Japan",
  "domain": "https://www.nissan.co.jp",
  "evidence_service_url": "https://ai-visibility-evidence-service-production.up.railway.app",
  "portfolio_id": "",
  "seed_topics": "",
  "topic_count": 8,
  "queries_per_topic": 6,
  "language": "English",
  "portfolio_goal": "AI answer visibility audit query portfolio."
}
```

Recommended env vars:

```text
BODHI_API_BASE_URL=https://sapientaiproducts.com/save
BODHI_PAT_TOKEN=pat_<token_with_tasks_execute_tasks_read_workflows_read>
BODHI_PORTFOLIO_TASK_ID=<Brand Topic Query Builder task id>
BODHI_PORTFOLIO_WORKFLOW_ID=<Brand Topic Query Builder workflow id>
BODHI_PORTFOLIO_HITL_REQUIRED=true
BODHI_HITL_TIMEOUT_SECONDS=300
BODHI_HITL_POLL_SECONDS=2
```

The service still ignores Bodhi `_links` and constructs API URLs from `BODHI_API_BASE_URL` to avoid the observed `/savesave/` link issue.

## v3.4.3 note

This build fixes dry Phase 2 synthetic refreshes where SerpAPI and crawling are disabled. The service now materialises a Bodhi-compatible compact evidence scope from the generated query portfolio plus sitemap mapping, so `audit_context`, `evidence_scope`, `query_owned_url_mapping`, and `owned_pages_full.pages` are populated before the Auditor workflow runs.


## v3.4.5 - SerpAPI ingestion fix

This patch fixes the Phase 2 path where live SerpAPI Google AI Mode calls were executed but `google_ai_mode_compact.json` still contained `SerpAPI collection pending` placeholder rows.

Changes:
- Normalises SerpAPI raw responses into `google_ai_mode_compact.rows`.
- Extracts answer summaries and top citation URLs where SerpAPI returns them.
- Writes `serpapi_completed_with_citations` or `serpapi_completed_no_citations` instead of leaving pending rows.
- Merges citation URLs into `evidence_scope`, `visibility_matrix`, `source_classification`, and citation-metadata `external_pages_full`.
- Re-merges SerpAPI evidence after crawl/full-refresh so placeholders cannot overwrite live results.
- Ensures Auditor is triggered only after SerpAPI normalisation has completed.

Smoke test after deploy:
```bash
curl -X POST "$EVIDENCE_SERVICE_URL/refresh/evidence" \
  -H "Content-Type: application/json" \
  -d '{"brand":"Nissan","market":"Japan","domain":"https://www.nissan.co.jp","query_portfolio_mode":"synthetic","topic_count":2,"queries_per_topic":2,"query_limit":2,"max_owned_pages_per_query":3,"max_external_citations_per_query":3,"run_serpapi":true,"crawl_owned":false,"crawl_external":false,"trigger_auditor":false}'
```

Then inspect `/runs/<run_id>/bodhi-compact`; `google_ai_mode_compact.rows[].answer_summary` must no longer say `SerpAPI collection pending.`

## v3.4.7 notes
- Forces SerpAPI Google AI Mode calls to `hl=en` while preserving market localisation with `gl=jp` for Japan.
- Preserves SerpAPI references, reconstructed markdown, search metadata and search parameters in `google_ai_mode_compact`.
- Classifies citation sources into owned, competitor, authority/partner, publisher, social/video, shopping and generic citation groups for downstream reporting.
