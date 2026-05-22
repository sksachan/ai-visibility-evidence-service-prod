# Evidence Service v3.5.2.2

- Extends robots.txt / llms.txt and sitemap discovery to conservative sibling hosts such as www3.* and www2.* when the configured domain is www.*.
- Uses discovered sitemap and crawled owned-page hosts as first-class candidates for AI hygiene checks.
- Fixes false “robots.txt missing” cases where the active product-site crawl host differs from the primary input domain.
