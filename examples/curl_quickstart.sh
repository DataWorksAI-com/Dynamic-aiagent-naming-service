#!/usr/bin/env bash
# agentns curl quick-start
# Works with any language — pure HTTP, no SDK needed.
#
# Usage:
#   chmod +x examples/curl_quickstart.sh
#   ./examples/curl_quickstart.sh
#
# Auth:
#   POST endpoints require an API key when AGENTNS_AUTH=on (the default).
#   For local dev, either:
#     a) Start the server with AGENTNS_AUTH=off agentns-server
#     b) Set AGENTNS_API_KEY below to match your AGENTNS_API_KEYS env var

BASE="${AGENTNS_URL:-http://localhost:8200}"
API_KEY="${AGENTNS_API_KEY:-}"          # leave empty if AGENTNS_AUTH=off

BOLD='\033[1m'
GREEN='\033[0;32m'
CYAN='\033[0;36m'
YELLOW='\033[0;33m'
RESET='\033[0m'

# Build the auth header string (empty if no key configured)
if [ -n "$API_KEY" ]; then
  AUTH_HEADER="-H \"X-API-Key: $API_KEY\""
  echo -e "${GREEN}Auth: using API key (${API_KEY:0:8}...)${RESET}\n"
else
  AUTH_HEADER=""
  echo -e "${YELLOW}Auth: no API key set — server must be running with AGENTNS_AUTH=off${RESET}"
  echo -e "${YELLOW}      Set AGENTNS_API_KEY env var to authenticate.${RESET}\n"
fi

# Helper: POST with optional auth header
post() {
  local url="$1"
  local data="$2"
  if [ -n "$API_KEY" ]; then
    curl -s -X POST "$url" \
      -H "Content-Type: application/json" \
      -H "X-API-Key: $API_KEY" \
      -d "$data"
  else
    curl -s -X POST "$url" \
      -H "Content-Type: application/json" \
      -d "$data"
  fi
}

echo -e "${BOLD}━━━  agentns curl quick-start  ━━━${RESET}\n"

# ── 1. Health check ────────────────────────────────────────────────────────────
echo -e "${CYAN}1. Service health${RESET}"
curl -s "$BASE/health" | python3 -m json.tool
echo

# ── 2. Register two endpoints for "emailer" ───────────────────────────────────
echo -e "${CYAN}2. Register emailer — New York instance${RESET}"
post "$BASE/register" '{
  "label":    "emailer",
  "endpoint": "http://ny-host:9001",
  "region":   "us-east",
  "location": {"city": "New York"},
  "protocols": ["http", "A2A"],
  "flag":     "🇺🇸"
}' | python3 -m json.tool
echo

echo -e "${CYAN}3. Register emailer — London instance${RESET}"
post "$BASE/register" '{
  "label":    "emailer",
  "endpoint": "http://lon-host:9001",
  "region":   "eu-west",
  "location": {"city": "London"},
  "protocols": ["http", "A2A"],
  "flag":     "🇬🇧"
}' | python3 -m json.tool
echo

# ── 3. List agents ─────────────────────────────────────────────────────────────
echo -e "${CYAN}4. List all agents${RESET}"
curl -s "$BASE/agents" | python3 -m json.tool
echo

# ── 4. Resolve by URN (Boston requester → prefers New York) ───────────────────
echo -e "${CYAN}5. Resolve emailer — requester in Boston (expects New York)${RESET}"
post "$BASE/resolve" '{
  "agent_name": "urn:agentns.local:agents.local:emailer",
  "requester_context": {
    "location":  {"city": "Boston"},
    "protocols": ["A2A", "http"]
  }
}' | python3 -m json.tool
echo

# ── 5. Resolve by URN (Paris requester → prefers London) ─────────────────────
echo -e "${CYAN}6. Resolve emailer — requester in Paris (expects London)${RESET}"
post "$BASE/resolve" '{
  "agent_name": "urn:agentns.local:agents.local:emailer",
  "requester_context": {
    "location":  {"city": "Paris"},
    "protocols": ["A2A", "http"]
  }
}' | python3 -m json.tool
echo

# ── 6. Cache stats ─────────────────────────────────────────────────────────────
echo -e "${CYAN}7. Cache stats${RESET}"
curl -s "$BASE/cache/stats" | python3 -m json.tool
echo

echo -e "${GREEN}Done!${RESET}"
