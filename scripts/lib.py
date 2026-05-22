from __future__ import annotations

import json
import os
import re
import time
from collections import Counter
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional
from urllib.parse import urlparse, urlunparse

import yaml
from dotenv import load_dotenv

load_dotenv()
ROOT = Path(__file__).resolve().parents[1]


def load_yaml(path: str | Path) -> Dict[str, Any]:
    p = ROOT / path if not Path(path).is_absolute() else Path(path)
    if not p.exists():
        return {}
    with p.open('r', encoding='utf-8') as f:
        return yaml.safe_load(f) or {}


def get_config() -> Dict[str, Any]:
    return load_yaml(os.getenv('PIPELINE_CONFIG', 'config/pipeline_config.yaml'))


def get_weights() -> Dict[str, Any]:
    return load_yaml(os.getenv('SCORING_WEIGHTS', 'config/scoring_weights.yaml'))


def resolve_path(path: str | Path) -> Path:
    p = Path(path)
    return p if p.is_absolute() else ROOT / p


def read_json(path: str | Path, default: Any = None) -> Any:
    p = resolve_path(path)
    if not p.exists():
        if default is not None:
            return default
        raise FileNotFoundError(str(p))
    with p.open('r', encoding='utf-8') as f:
        return json.load(f)


def write_json(path: str | Path, data: Any) -> None:
    p = resolve_path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open('w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def write_text(path: str | Path, text: str) -> None:
    p = resolve_path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text or '', encoding='utf-8')


def read_text(path: str | Path, default: str = '') -> str:
    p = resolve_path(path)
    if not p.exists():
        return default
    return p.read_text(encoding='utf-8', errors='ignore')


def domain_of(url: str) -> str:
    try:
        return urlparse(url or '').netloc.lower().replace(':443', '').replace(':80', '')
    except Exception:
        return ''


def registeredish_domain(host: str) -> str:
    host = (host or '').lower().strip('.')
    parts = host.split('.')
    if len(parts) <= 2:
        return host
    if parts[-2] in {'co', 'ac', 'go', 'or', 'ne'} and len(parts) >= 3:
        return '.'.join(parts[-3:])
    return '.'.join(parts[-2:])


def is_valid_url(url: str) -> bool:
    try:
        parsed = urlparse(url or '')
        return parsed.scheme in {'http', 'https'} and bool(parsed.netloc)
    except Exception:
        return False


def normalize_url(url: str, strip_query: bool = False) -> str:
    if not url:
        return ''
    try:
        p = urlparse(url.strip())
        scheme = p.scheme or 'https'
        netloc = p.netloc.lower()
        path = p.path.rstrip('/') if p.path != '/' else p.path
        query = '' if strip_query else p.query
        return urlunparse((scheme, netloc, path, '', query, ''))
    except Exception:
        return url.strip()


def is_domain_match(host: str, domains: Iterable[str]) -> bool:
    h = (host or '').lower()
    for d in {x.lower() for x in domains or []}:
        if h == d or h.endswith('.' + d):
            return True
    return False


def is_owned_url(url: str, owned_domains: Iterable[str]) -> bool:
    return is_domain_match(domain_of(url), owned_domains)


def compact_whitespace(text: str) -> str:
    return re.sub(r'\s+', ' ', text or '').strip()


def safe_slug(value: str, max_len: int = 80) -> str:
    value = re.sub(r'[^a-zA-Z0-9]+', '_', value or '').strip('_').lower()
    return (value[:max_len] or 'item')


def dedupe_list(values: Iterable[Any], key: Optional[Callable[[Any], str]] = None) -> List[Any]:
    out: List[Any] = []
    seen = set()
    for v in values or []:
        k = key(v) if key else json.dumps(v, ensure_ascii=False, sort_keys=True) if isinstance(v, (dict, list)) else str(v)
        if not k or k in seen:
            continue
        seen.add(k)
        out.append(v)
    return out


def dedupe_queries(values: Iterable[Any]) -> List[Any]:
    def k(v: Any) -> str:
        if isinstance(v, dict):
            return compact_whitespace(v.get('query', '')).lower()
        return compact_whitespace(str(v)).lower()
    return dedupe_list(values or [], key=k)


def keyword_tokens(*values: str) -> List[str]:
    text = ' '.join(v or '' for v in values).lower()
    tokens = re.findall(r'[a-z0-9一-龥ぁ-んァ-ンー]+', text)
    stop = {
        'the','and','for','with','from','that','this','what','how','can','are','into','page','html','www','https','http','co','jp','com',
        'nissan','日産','ニッサン','new','vehicles','vehicle','cars','car','japan','japanese','details','specifications','model','models'
    }
    return [t for t in tokens if len(t) > 1 and t not in stop]


def count_matches(text: str, terms: Iterable[str]) -> int:
    t = (text or '').lower()
    return sum(1 for term in terms if term and term.lower() in t)


def url_tokens(url: str) -> set[str]:
    parsed = urlparse(url or '')
    raw = f"{parsed.netloc} {parsed.path}".lower()
    return {x for x in re.split(r'[^a-z0-9一-龥ぁ-んァ-ンー]+', raw) if x}


def infer_page_type(url: str, title: str = '', description: str = '') -> str:
    text = f'{url} {title} {description}'.lower()
    toks = url_tokens(url) | set(keyword_tokens(title, description))
    path = urlparse(url or '').path.lower()
    if 'comparison' in path:
        return 'comparison_specs'
    if any(x in path for x in ['range_and_charging', 'cruising-distance', 'charge_cruising', '/charge', '/charging']) or bool({'battery','v2h','charger','charging'} & toks):
        return 'ev_range_charging'
    if bool({'e-power','epower','hybrid','powertrain','e-4orce'} & toks) or any(x in path for x in ['e-power', 'e_4orce', 'e-4orce']):
        return 'powertrain'
    if any(x in path for x in ['interior', 'seat', 'seat_arrangement', 'luggage', 'storage', 'comfort', 'loadability', 'interior_space']) or bool({'isofix','stroller','seating'} & toks):
        return 'family_practicality'
    if any(x in path for x in ['safety', 'propilot', '360_safety', '/icc', '/nim']) or bool({'adas','jncap','warranty'} & toks):
        return 'safety_trust'
    if any(x in path for x in ['/credit', '/subscription', '/subsidy', '/campaign', '/event']) or bool({'finance','loan','lease','leasing','offer','bvc','price','cost'} & toks):
        return 'finance_value'
    if any(x in path for x in ['ease_of_driving', '/kei', '/dayz', '/roox', '/sakura']) or bool({'parking','compact','urban','turning'} & toks):
        return 'urban_mobility'
    if any(x in path for x in ['/service', '/maintenance', '/connect', '/faq', '/owners', 'maintepro', '/recall']) or bool({'dealer','support','aftersales','roadside'} & toks):
        return 'ownership_aftersales'
    return 'model_overview' if re.search(r'/vehicles/new/[^/]+\.html?$', path) else 'other'


def infer_journey_from_queries(queries: Iterable[Any]) -> str:
    cats: List[str] = []
    for q in queries or []:
        if isinstance(q, dict) and q.get('brand_topic_category'):
            cats.append(q['brand_topic_category'])
    if not cats:
        return ''
    return Counter(cats).most_common(1)[0][0]


def infer_priority_from_queries(queries: Iterable[Any]) -> str:
    vals = []
    for q in queries or []:
        if isinstance(q, dict) and q.get('priority'):
            vals.append(q['priority'])
    return Counter(vals).most_common(1)[0][0] if vals else ''


def query_type_mix(queries: Iterable[Any]) -> Dict[str, int]:
    c = Counter()
    for q in queries or []:
        qt = q.get('query_type', 'not_validated') if isinstance(q, dict) else 'not_validated'
        c[qt] += 1
    return dict(c)


def mapping_quality_from_score(score: float, selected_page_type: str = '', expected_page_types: Iterable[str] = ()) -> str:
    if score >= 44:
        return 'strong'
    if score >= 26:
        return 'acceptable'
    return 'weak'


def clean_markdown_for_scoring(markdown: str) -> str:
    text = markdown or ''
    if text.startswith("markdown='") or text.startswith('markdown="'):
        text = re.sub(r"^markdown=['\"]", '', text).rstrip("'")
    text = text.replace('\\n', '\n')
    text = re.sub(r'!\[[^\]]*\]\([^)]*\)', ' ', text)
    text = re.sub(r'\[([^\]]{0,120})\]\((https?://[^)]*)\)', r'\1', text)
    text = re.sub(r'https?://\S+', ' ', text)
    text = re.sub(r'\b[A-Za-z0-9_%.-]*AdobeOrg[A-Za-z0-9_%.-]*\b', ' ', text)
    text = re.sub(r'\b(?:utm_[a-z]+|srsltid|fbclid|gclid)=[^\s&]+', ' ', text, flags=re.I)
    bad_terms = [
        'cookie', 'privacy policy', 'accept all', 'reject all', 'disable the ad blocking', 'javascript', 'ad blocker',
        '企業・ir情報', 'ニュースリリース', 'サステナビリティ', '投資家の皆さまへ', '販売店検索', 'カタログ請求',
        'nissan online shop', 'corporate website', 'site map', 'サイトマップ', 'faq/お問い合わせ', 'リコール情報'
    ]
    kept = []
    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue
        low = s.lower()
        if any(t in low for t in bad_terms):
            continue
        if len(re.findall(r'https?://|\.jpg|\.gif|\.png|\.svg|\.pdf|#container|AdobeOrg|utm_', s, flags=re.I)):
            continue
        if len(s) < 3:
            continue
        if (s.count('|') >= 4 or s.count(' - ') >= 4) and len(re.findall(r'[。.!?]', s)) == 0:
            continue
        kept.append(s)
    cleaned = '\n'.join(kept)
    cleaned = re.sub(r'\n{3,}', '\n\n', cleaned)
    return cleaned.strip()


def extract_real_questions(text: str, max_items: int = 8) -> List[str]:
    out: List[str] = []
    patterns = [r'(?m)^#{1,4}\s*(.{6,120}[?？])\s*$', r'(?m)^[-*]\s*(.{6,120}[?？])\s*$', r'([^\n。.!?]{8,120}[?？])']
    for pat in patterns:
        for m in re.findall(pat, text or ''):
            q = compact_whitespace(m)
            low = q.lower()
            if any(x in low for x in ['http', '.jpg', '.gif', '.png', '.pdf', 'adobeorg', 'utm_', '#container', 'site_domain']):
                continue
            if '?' in q[:-1] or len(q.split()) < 4 and not re.search(r'[一-龥ぁ-んァ-ンー]{6,}', q):
                continue
            out.append(q)
            if len(out) >= max_items:
                return dedupe_list(out)[:max_items]
    return dedupe_list(out)[:max_items]


def content_numeric_mentions(text: str) -> List[str]:
    raw = re.findall(r'(?<![A-Za-z0-9_])(?:¥\s?\d[\d,]*(?:\.\d+)?|\d[\d,]*(?:\.\d+)?\s?(?:%|km|kWh|kW|円|yen|万円|年|ヶ月|か月|人|名|seats?|L|litres?|リットル|mm|m|hours?|minutes?|分|歳))(?![A-Za-z0-9_])', text or '', re.I)
    filtered = []
    for x in raw:
        lx = x.lower()
        if re.search(r'20[0-9]{2}/|260[0-9]|251[0-9]|2540|1778|adobe|utm', lx):
            continue
        filtered.append(x.strip())
    return dedupe_list(filtered)


def score_text_features(markdown: str, query: str = '', weights: Optional[Dict[str, Any]] = None, page_url: str = '', page_type: str = '', extraction_manifest: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Score a page using the shared GEO / AI Source Readiness framework.

    The same scoring logic is used for owned and external pages. It returns:
    - feature_scores: atomic 0..5 diagnostics for debugging
    - dimension_scores: six GEO dimensions, each 0..20
    - geo_score_120: total out of 120
    - score/readiness_score: backwards-compatible 0..100 score
    """
    weights = weights or get_weights()
    heur = weights.get('heuristics', {})
    raw_text = markdown or ''
    manifest = extraction_manifest or {}
    manifest_metrics = manifest.get('extraction_metrics', {}) if isinstance(manifest, dict) else {}
    manifest_signals = manifest.get('geo_signals', {}) if isinstance(manifest, dict) else {}
    manifest_regions = manifest.get('content_regions', {}) if isinstance(manifest, dict) else {}
    manifest_metadata = manifest.get('metadata', {}) if isinstance(manifest, dict) else {}
    text = clean_markdown_for_scoring(raw_text)
    low = text.lower()
    words = re.findall(r'[A-Za-z0-9一-龥ぁ-んァ-ンー]+', text)
    word_count = len(words)
    first_block = low[: int(heur.get('answer_first_max_chars', 1200))]
    q_tokens = set(keyword_tokens(query))
    t_tokens = set(keyword_tokens(text[:10000], page_url, page_type))
    relevance_overlap = len(q_tokens & t_tokens)
    relevance_ratio = relevance_overlap / max(1, min(len(q_tokens), 12))
    headings = [compact_whitespace(h) for h in re.findall(r'(?m)^#{1,4}\s+(.+)$', text) if len(compact_whitespace(h)) > 2]
    questions = extract_real_questions(text, int(heur.get('max_questions', 8)))
    numeric_mentions = content_numeric_mentions(text)
    table_lines = [ln for ln in text.splitlines() if ln.count('|') >= 2]
    tables = len(table_lines) >= 3 or '<table' in low
    json_ld = bool('application/ld+json' in raw_text.lower() or 'schema.org' in raw_text.lower() or manifest_signals.get('schema_json_ld') or manifest_signals.get('schema_types'))
    robots_hint = 'robots.txt' in low
    llms_hint = 'llms.txt' in low
    comparison_terms = heur.get('comparison_terms', [])
    citation_terms = heur.get('citation_terms', [])
    authority_terms = heur.get('authority_terms', [])
    promo_terms = heur.get('promotional_terms', [])

    def clamp(v: float) -> int:
        return max(0, min(5, int(round(v))))

    has_direct_answer = relevance_overlap >= 2 and len(first_block) > 250 and any(tok in first_block for tok in list(q_tokens)[:8])
    extractable = 0
    extractable += 1 if len(headings) >= 2 else 0
    extractable += 1 if word_count >= int(heur.get('min_word_count_basic', 300)) else 0
    extractable += 1 if word_count >= int(heur.get('min_word_count_strong', 900)) else 0
    extractable += 1 if len(questions) >= 2 else 0
    extractable += 1 if tables else 0

    relevant_numbers = len(numeric_mentions)
    citation_score_seed = count_matches(low, citation_terms)
    authority_score_seed = count_matches(low, authority_terms)
    comparison_seed = count_matches(low, comparison_terms)
    promo_count = count_matches(low, promo_terms)
    nav_noise = count_matches(raw_text.lower(), ['cookie consent', 'adobeorg', 'utm_', '#container', 'accept all cookies', 'reject all'])

    # Free hybrid local scraper provides structured evidence. Use it to avoid
    # scoring solely from prose length and to preserve rendered assets lost by
    # text-only extraction.
    manifest_numeric_count = int(manifest_metrics.get('numeric_fact_count') or 0)
    manifest_heading_count = int(manifest_metrics.get('heading_count') or 0)
    manifest_link_count = int(manifest_metrics.get('link_count') or 0)
    manifest_image_count = int(manifest_metrics.get('image_count') or 0)
    manifest_pdf_count = int(manifest_metrics.get('pdf_link_count') or 0)
    manifest_bullet_count = int(manifest_metrics.get('bullet_count') or 0)
    relevant_numbers = max(relevant_numbers, manifest_numeric_count)
    if manifest_signals.get('source_citations'):
        citation_score_seed = max(citation_score_seed, 3)
    if manifest_signals.get('authority_signals') or manifest_signals.get('pdf_or_spec_links'):
        authority_score_seed = max(authority_score_seed, 3)
    if manifest_signals.get('comparison_terms') or manifest_bullet_count >= 3:
        comparison_seed = max(comparison_seed, 3)
    if manifest_signals.get('faq_like') or manifest_signals.get('faq_schema'):
        questions = questions or ['FAQ-like structure detected from extraction manifest']
    if manifest_signals.get('low_noise_ratio'):
        nav_noise = min(nav_noise, 1)
    # Full-page crawler keeps header/footer as scoring evidence. Use manifest signals
    # for dimensions where page chrome is relevant, but keep clarity/depth driven by
    # main content and query relevance.
    if manifest_signals.get('freshness_signals') or manifest_signals.get('visible_dates') or manifest_regions.get('footer_chars', 0) > 150:
        freshness_seed_from_manifest = True
    else:
        freshness_seed_from_manifest = False
    if manifest_signals.get('schema_types') or manifest_signals.get('schema_json_ld') or manifest_metadata.get('schema_types'):
        schema_seed_from_manifest = True
    else:
        schema_seed_from_manifest = False

    features = {
        'answer_first': clamp(5 if (has_direct_answer or manifest_signals.get('answer_first')) and relevant_numbers >= 2 else 3 if (has_direct_answer or manifest_signals.get('answer_first')) else 1 if relevance_overlap else 0),
        'query_relevance': clamp(5 if relevance_ratio >= 0.55 else 4 if relevance_ratio >= 0.38 else 3 if relevance_ratio >= 0.25 else 2 if relevance_overlap >= 2 else 1 if relevance_overlap else 0),
        'extractable_passages': clamp(max(extractable, 5 if manifest_signals.get('extractable_passages') else 4 if manifest_heading_count >= 4 and manifest_metrics.get('unique_line_count', 0) >= 20 else extractable)),
        'specific_facts': clamp(5 if relevant_numbers >= 8 and relevance_overlap >= 2 else 4 if relevant_numbers >= 5 and relevance_overlap >= 1 else 3 if relevant_numbers >= 3 else 1 if relevant_numbers else 0),
        'statistics': clamp(5 if relevant_numbers >= 10 else 4 if relevant_numbers >= 7 else 3 if relevant_numbers >= int(heur.get('statistics_min_numeric_mentions', 5)) else 1 if relevant_numbers else 0),
        'source_citations': clamp(5 if citation_score_seed >= 4 and authority_score_seed >= 2 else 4 if citation_score_seed >= 3 or manifest_link_count >= 5 else 2 if citation_score_seed >= 1 or manifest_link_count >= 2 else 0),
        'comparison_readiness': clamp(5 if comparison_seed >= 4 and (tables or manifest_bullet_count >= 5) else 4 if comparison_seed >= 4 else 3 if comparison_seed >= 2 or tables or manifest_bullet_count >= 3 else 1 if comparison_seed else 0),
        'faq_readiness': clamp(5 if len(questions) >= 5 else 4 if len(questions) >= int(heur.get('faq_min_questions', 3)) else 2 if len(questions) else 0),
        'schema': clamp(5 if (json_ld or schema_seed_from_manifest) else 4 if manifest_signals.get('canonical') and manifest_signals.get('meta_description') else 0),
        'freshness': clamp(4 if (freshness_seed_from_manifest or re.search(r'202[5-9]|2026|令和[7-9]|last updated|updated|valid until|更新日|掲載日|有効期限', text, re.I)) else 2 if re.search(r'202[3-4]|令和[5-6]', text) else 0),
        'neutral_tone': clamp(5 - min(5, promo_count)),
        'authority_signals': clamp(5 if authority_score_seed >= 5 or manifest_pdf_count >= 1 else 4 if authority_score_seed >= 3 or manifest_signals.get('authority_signals') else 2 if authority_score_seed >= 1 else 0),
        'accessibility': clamp(5 if word_count >= 1000 or manifest_metrics.get('markdown_chars', 0) >= 5000 or manifest_regions.get('full_markdown_chars', 0) >= 5000 else 4 if word_count >= 600 or manifest_metrics.get('markdown_chars', 0) >= 2500 or manifest_regions.get('full_markdown_chars', 0) >= 2500 else 3 if word_count >= 300 or manifest_metrics.get('markdown_chars', 0) >= 1200 else 1 if word_count >= 80 or manifest_metrics.get('markdown_chars', 0) >= 600 else 0),
    }
    penalties = {
        'promotional_language_penalty': min(5, promo_count),
        'noise_penalty': min(5, nav_noise // 3),
        'keyword_stuffing': 0,
    }

    # Dimension scoring: each dimension is out of 20, based on weighted feature scores.
    dimension_config = weights.get('dimensions', {})
    dimension_scores: Dict[str, Dict[str, Any]] = {}
    for dim, spec in dimension_config.items():
        fw = spec.get('feature_weights', {}) or {}
        denom = sum(float(v) for v in fw.values()) or 1.0
        weighted_feature = sum((features.get(k, 0) / 5.0) * float(w) for k, w in fw.items()) / denom
        raw_dim_score = int(round(weighted_feature * int(spec.get('max', 20))))
        # Apply targeted penalties to relevant dimensions.
        if dim in {'content_clarity', 'eeat_signals'}:
            raw_dim_score -= min(3, penalties['promotional_language_penalty'])
        if dim in {'content_clarity', 'freshness_index'}:
            raw_dim_score -= min(3, penalties['noise_penalty'])
        dim_score = max(0, min(int(spec.get('max', 20)), raw_dim_score))
        if dim_score <= 5:
            band = 'weak_or_not_observed'
        elif dim_score <= 10:
            band = 'basic_limited'
        elif dim_score <= 15:
            band = 'adequate_incomplete'
        else:
            band = 'strong_citation_ready'
        dimension_scores[dim] = {
            'score': dim_score,
            'max': int(spec.get('max', 20)),
            'band': band,
            'description': spec.get('description', ''),
        }

    # Fallback for older weights files.
    if not dimension_scores:
        feature_weights = weights.get('feature_weights', {})
        max_weighted = sum(float(feature_weights.get(k, 1.0)) * 5 for k in features)
        weighted = sum(float(feature_weights.get(k, 1.0)) * v for k, v in features.items())
        penalty = sum(float(weights.get('penalties', {}).get(k, 0.0)) * v for k, v in penalties.items())
        score_100 = 0 if max_weighted == 0 else max(0, min(100, round((weighted - penalty) / max_weighted * 100)))
        geo_score_120 = int(round(score_100 * 1.2))
    else:
        geo_score_120 = sum(d['score'] for d in dimension_scores.values())
        score_100 = max(0, min(100, int(round(geo_score_120 / 1.2))))

    likely_min = int(weights.get('framework', {}).get('citation_likelihood', {}).get('likely_cited_min', 90))
    occasional_min = int(weights.get('framework', {}).get('citation_likelihood', {}).get('occasionally_cited_min', 60))
    if geo_score_120 >= likely_min:
        citation_likelihood = 'likely cited'
    elif geo_score_120 >= occasional_min:
        citation_likelihood = 'occasionally cited'
    else:
        citation_likelihood = 'rarely cited'

    schema_types = dedupe_list(list(manifest_signals.get('schema_types') or []) + re.findall(r'(?:FAQPage|Product|Offer|OfferCatalog|Organization|WebPage|BreadcrumbList|Vehicle|Review|AggregateRating)', raw_text, flags=re.I))[:20]
    return {
        'score': score_100,
        'readiness_score': score_100,
        'geo_score_120': geo_score_120,
        'max_geo_score': 120,
        'citation_likelihood': citation_likelihood,
        'dimension_scores': dimension_scores,
        'features': features,
        'penalties': penalties,
        'word_count': word_count,
        'headings': dedupe_list(headings)[: int(heur.get('max_headings', 20))],
        'questions': questions,
        'numeric_mentions_sample': numeric_mentions[:20],
        'links_sample': re.findall(r'https?://[^\s)\]"\']+', raw_text)[: int(heur.get('max_links', 10))],
        'schema_types': schema_types,
        'cleaned_text_chars': len(text),
        'standards_signals': {'json_ld_observed': bool(json_ld or manifest_signals.get('schema_json_ld')), 'robots_txt_mentioned': bool(robots_hint), 'llms_txt_mentioned': bool(llms_hint), 'canonical': bool(manifest_signals.get('canonical')), 'meta_description': bool(manifest_signals.get('meta_description')), 'robots_indexable': manifest_signals.get('robots_indexable'), 'footer_present': manifest_signals.get('footer_present'), 'header_present': manifest_signals.get('header_present'), 'visible_dates': manifest_signals.get('visible_dates', [])},
        'extraction_manifest_signals': manifest_signals,
        'extraction_manifest_metrics': manifest_metrics,
    }


def classify_source(url: str, source_name: str = '', title: str = '', cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    cfg = cfg or get_config()
    host = domain_of(url)
    text = f'{host} {source_name} {title}'.lower()
    owned = cfg.get('owned_domains', [])
    ecosystem = cfg.get('owned_ecosystem_domains', [])
    off_market = cfg.get('off_market_owned_domains', [])
    source_type = 'other'
    flags = {
        'is_owned_domain': is_domain_match(host, owned),
        'is_owned_ecosystem': is_domain_match(host, ecosystem),
        'is_off_market': is_domain_match(host, off_market),
        'is_social_or_forum': False,
        'is_low_authority': False,
    }
    if flags['is_owned_domain']:
        source_type = 'owned_brand'
    elif flags['is_owned_ecosystem']:
        source_type = 'owned_ecosystem'
    elif flags['is_off_market'] or re.search(r'nissan\.(co\.uk|com\.au)|nissanusa|libertyvillenissan|group1nissan|nissan\.co\.th', text):
        source_type = 'off_market_owned'; flags['is_off_market'] = True
    elif re.search(r'jsae|自動車技術会|spglobal|s&p|kobe-u|\.ac\.jp|go\.jp|meti|mlit|nasva|jncap|jaf|kokusen|enecho|university|standards|regulator|省|庁|機構|oist', text):
        source_type = 'authority_body'
    elif re.search(r'reuters|nikkei|asahi|yomiuri|bloomberg|japannews|japan news|press|新聞|green car reports', text):
        source_type = 'news_media'
    elif re.search(r'bank|finance|loan|leasing|lease|credit|insurance|insurer|ratings|abs', text):
        source_type = 'finance_lender'
    elif re.search(r'toyota|honda|mazda|subaru|mitsubishi|suzuki|lexus|daihatsu', text):
        source_type = 'competitor_owned'
    elif re.search(r'reddit|facebook|forum|community|quora|x\.com|twitter|minkara', text):
        source_type = 'forum_community'; flags['is_social_or_forum'] = True
    elif re.search(r'youtube|youtu\.be|tiktok|instagram', text):
        source_type = 'video_social'; flags['is_social_or_forum'] = True
    elif re.search(r'carsensor|goo-net|kakaku|carview|autotrader|marketplace|autocraft|used|listing', text):
        source_type = 'marketplace_listing'
    elif re.search(r'charge|charging|evsmart|eneos|tepco|e-mobility|plug|recharged|eleport|abrp', text):
        source_type = 'partner_infrastructure'
    elif re.search(r'motortrend|carsguide|carsales|autocar|carwow|topgear|response\.jp|webcg|bestcar|clicccar|review|car reports|davey|choosemycar|szabo|bambinos|japan wonder travel', text):
        source_type = 'publisher_review'
    elif re.search(r'dealer|retailer|sales|ucar', text):
        source_type = 'dealer_retailer'
    if source_type == 'other':
        flags['is_low_authority'] = True
    high = {'authority_body', 'owned_brand', 'owned_ecosystem'}
    medium = {'publisher_review', 'partner_infrastructure', 'news_media', 'finance_lender', 'marketplace_listing', 'dealer_retailer', 'competitor_owned'}
    if source_type in high:
        quality = 'high'
    elif source_type in medium:
        quality = 'medium'
    else:
        quality = 'low'
    if flags['is_off_market'] or flags['is_social_or_forum']:
        quality = 'low' if source_type in {'forum_community', 'video_social'} else 'medium'
    notes = []
    if flags['is_off_market']:
        notes.append('Off-market owned or dealer source; use cautiously for Japan-specific recommendations.')
    if flags['is_social_or_forum']:
        notes.append('Social/forum/video source; useful as directional evidence but lower authority.')
    if flags['is_low_authority']:
        notes.append('Source type could not be confidently classified; authority is uncertain.')
    return {'source_type': source_type, 'source_quality': quality, 'source_quality_notes': notes, **flags}


def classify_source_type(url: str, source_name: str = '') -> str:
    return classify_source(url, source_name).get('source_type', 'other')


def confidence_from_source_quality(source_quality: str, has_crawl: bool = True) -> str:
    if not has_crawl:
        return 'low'
    return 'high' if source_quality == 'high' else 'medium' if source_quality == 'medium' else 'low'


def run_step(name: str, fn) -> None:
    print(f'\n=== {name} ===')
    started = time.time()
    fn()
    print(f'=== {name} complete in {time.time() - started:.1f}s ===')
