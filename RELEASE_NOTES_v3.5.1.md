# AI Visibility Evidence Service v3.5.1

## Purpose
Normalise owned/external crawl outputs before Bodhi Auditor consumption.

## Fixes
- Materialises crawl metadata fields when markdown/text exists:
  - `word_count`
  - `markdown_chars`
  - `raw_markdown_chars`
  - `text_chars`
  - `domain` / `source_domain`
  - `http_status_code` / `status_code` where available
- Merges `owned_pages_full.pages` crawl success evidence back into:
  - `audit_context.pages`
  - `audit_context.owned_urls`
  - `evidence_scope.owned_pages`
  - `evidence_scope.owned_urls`
- Merges `external_pages_full.pages` crawl evidence back into:
  - `evidence_scope.external_sources`
  - `evidence_scope.ai_citations`
  - `source_classification.sources`
- Enriches `visibility_matrix.queries[]` with:
  - `top_citations`
  - `citation_count`
  - `citation_domains`
  - `leading_citation_domain`
  - `winning_source_types`
- Adds `crawl_telemetry.json` and publishes telemetry into run status before Auditor trigger.
- Prevents metadata-only `pending` / `not_requested` records from being upgraded to crawl success.

## Validation
Tested against the owned and external compact bundles supplied from the Nissan Japan smoke tests.
