# Evidence Service v3.5.2

Adds report history, basic sitemap auto-discovery, and AI Discoverability Hygiene.

## New
- `GET /reports/history` returns dashboard-ready successful runs with lightweight analytics.
- Sitemap URL is now optional: if blank, the service checks robots.txt sitemap entries plus common sitemap paths.
- Site hygiene detection for robots.txt, llms.txt, and structured data / JSON-LD coverage.
- `site_ai_hygiene.json` is written into each evidence run and injected into compact evidence files.
- Run status exposes `llms_txt_status`, `robots_txt_status`, `json_ld_coverage_pct`, and hygiene priority.

## Notes
- Full recursive sitemap scoring and URL quality prioritisation is reserved for v3.5.3.
