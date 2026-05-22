# Evidence Service v3.5.0

Adds source evidence reuse for Google AI Mode / SerpAPI citation rows.

## Key changes

- Adds explicit reuse of AI citation evidence from `source_run_id`.
- When `source_run_id` is provided and SerpAPI is off, the service copies matching `google_ai_mode_compact` rows from the source run into the new run.
- Rebuilds `evidence_scope.ai_citations`, `evidence_scope.external_sources`, `source_classification.sources`, `visibility_matrix.queries[].citations`, and `external_pages_full` from reused citation rows.
- Preserves the ability to remap owned URLs and rerun Auditor/CMS without spending new SerpAPI calls.

## Expected payload pattern

```json
{
  "source_run_id": "evidence_nissan_japan_1779101052_5d1acd",
  "query_portfolio_id": "nissan_japan_1779101279_synthetic_v1",
  "run_serpapi": false,
  "use_existing_google_ai_mode": true,
  "trigger_auditor": true
}
```
