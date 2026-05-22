#!/usr/bin/env bash
# =============================================================================
# End-to-End Multi-Brand Test: Toyota/Germany Full Refresh
# =============================================================================
# This script simulates a full refresh POST to the Evidence Service with
# Toyota/Germany parameters, validating the end-to-end request flow.
#
# Usage:
#   EVIDENCE_SERVICE_URL=http://localhost:8000 ./test_toyota_germany_e2e.sh
#
# Prerequisites:
#   - Evidence Service running locally or on staging
#   - curl and jq installed
# =============================================================================

set -euo pipefail

BASE_URL="${EVIDENCE_SERVICE_URL:-http://localhost:8000}"
BRAND="Toyota"
MARKET="Germany"
DOMAIN="https://www.toyota.de"

echo "=== Multi-Brand E2E Test: ${BRAND}/${MARKET} ==="
echo "Evidence Service: ${BASE_URL}"
echo ""

# ---------------------------------------------------------------------------
# Test 1: Health check
# ---------------------------------------------------------------------------
echo "[1/5] Health check..."
HEALTH=$(curl -s -o /dev/null -w "%{http_code}" "${BASE_URL}/health" 2>/dev/null || echo "000")
if [ "$HEALTH" = "200" ]; then
    echo "  ✅ Service is healthy"
else
    echo "  ⚠️  Health endpoint returned ${HEALTH} (may not exist, continuing...)"
fi

# ---------------------------------------------------------------------------
# Test 2: POST /refresh/evidence with Toyota/Germany
# ---------------------------------------------------------------------------
echo ""
echo "[2/5] POST /refresh/evidence with Toyota/Germany..."
REFRESH_RESPONSE=$(curl -s -w "\n%{http_code}" -X POST "${BASE_URL}/refresh/evidence" \
  -H "Content-Type: application/json" \
  -d '{
    "brand": "Toyota",
    "market": "Germany",
    "domain": "https://www.toyota.de",
    "run_mode": "full_refresh",
    "query_portfolio_mode": "synthetic",
    "owned_domains": ["toyota.de", "www.toyota.de", "toyota-media.de"],
    "brand_terms": ["Toyota", "Corolla", "Camry", "RAV4", "Yaris", "GR86", "bZ4X"],
    "language": "German",
    "topic_count": 5,
    "queries_per_topic": 4,
    "query_limit": 20,
    "max_owned_urls": 30,
    "max_external_urls": 50,
    "enable_serpapi": false,
    "enable_owned_crawl": true,
    "enable_external_crawl": false,
    "trigger_auditor": false
  }' 2>/dev/null)

HTTP_CODE=$(echo "$REFRESH_RESPONSE" | tail -1)
BODY=$(echo "$REFRESH_RESPONSE" | sed '$d')

if [ "$HTTP_CODE" = "200" ] || [ "$HTTP_CODE" = "202" ]; then
    RUN_ID=$(echo "$BODY" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('run_id',''))" 2>/dev/null || echo "")
    echo "  ✅ Refresh accepted (HTTP ${HTTP_CODE})"
    if [ -n "$RUN_ID" ]; then
        echo "  📌 Run ID: ${RUN_ID}"
    fi
else
    echo "  ❌ Refresh failed (HTTP ${HTTP_CODE})"
    echo "  Response: $(echo "$BODY" | head -5)"
fi

# ---------------------------------------------------------------------------
# Test 3: Check run status
# ---------------------------------------------------------------------------
echo ""
echo "[3/5] GET /runs/status for Toyota/Germany..."
STATUS_RESPONSE=$(curl -s -w "\n%{http_code}" "${BASE_URL}/runs/status?brand=Toyota&market=Germany" 2>/dev/null)
STATUS_CODE=$(echo "$STATUS_RESPONSE" | tail -1)
STATUS_BODY=$(echo "$STATUS_RESPONSE" | sed '$d')

if [ "$STATUS_CODE" = "200" ]; then
    ACTIVE=$(echo "$STATUS_BODY" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('active', d.get('status','unknown')))" 2>/dev/null || echo "unknown")
    echo "  ✅ Status retrieved (HTTP ${STATUS_CODE})"
    echo "  📌 Active: ${ACTIVE}"
    
    # Verify brand/market in response
    RESP_BRAND=$(echo "$STATUS_BODY" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('brand',''))" 2>/dev/null || echo "")
    if [ "$RESP_BRAND" = "Toyota" ]; then
        echo "  ✅ Brand correctly set to Toyota"
    elif [ -n "$RESP_BRAND" ]; then
        echo "  ⚠️  Brand in response: ${RESP_BRAND} (expected Toyota)"
    fi
else
    echo "  ⚠️  Status endpoint returned ${STATUS_CODE}"
fi

# ---------------------------------------------------------------------------
# Test 4: Check latest-successful for Toyota/Germany
# ---------------------------------------------------------------------------
echo ""
echo "[4/5] GET /reports/latest-successful for Toyota/Germany..."
LATEST_RESPONSE=$(curl -s -w "\n%{http_code}" "${BASE_URL}/reports/latest-successful?brand=Toyota&market=Germany" 2>/dev/null)
LATEST_CODE=$(echo "$LATEST_RESPONSE" | tail -1)
LATEST_BODY=$(echo "$LATEST_RESPONSE" | sed '$d')

if [ "$LATEST_CODE" = "200" ]; then
    echo "  ✅ Latest successful report found (HTTP ${LATEST_CODE})"
    REPORT_BRAND=$(echo "$LATEST_BODY" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('brand',''))" 2>/dev/null || echo "")
    if [ "$REPORT_BRAND" = "Toyota" ]; then
        echo "  ✅ Report brand is Toyota"
    fi
elif [ "$LATEST_CODE" = "404" ]; then
    echo "  ✅ No previous Toyota/Germany report (expected for first run)"
else
    echo "  ⚠️  Latest-successful returned ${LATEST_CODE}"
fi

# ---------------------------------------------------------------------------
# Test 5: Verify no Nissan leakage in responses
# ---------------------------------------------------------------------------
echo ""
echo "[5/5] Verifying no Nissan/Japan leakage..."
LEAKAGE=0

if echo "$STATUS_BODY" | grep -qi '"brand":.*"Nissan"' 2>/dev/null; then
    echo "  ❌ Nissan brand leaked in status response!"
    LEAKAGE=1
fi

if echo "$STATUS_BODY" | grep -qi '"market":.*"Japan"' 2>/dev/null; then
    echo "  ❌ Japan market leaked in status response!"
    LEAKAGE=1
fi

if [ $LEAKAGE -eq 0 ]; then
    echo "  ✅ No Nissan/Japan leakage detected"
fi

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo ""
echo "=== Test Summary ==="
echo "Brand:  ${BRAND}"
echo "Market: ${MARKET}"
echo "Domain: ${DOMAIN}"
echo "Owned Domains: toyota.de, www.toyota.de, toyota-media.de"
echo "Brand Terms: Toyota, Corolla, Camry, RAV4, Yaris, GR86, bZ4X"
echo ""
echo "All parameterization checks complete."
echo "To run a full pipeline test, set trigger_auditor=true and ensure"
echo "Bodhi workflow is configured with Toyota/Germany HITL defaults."
