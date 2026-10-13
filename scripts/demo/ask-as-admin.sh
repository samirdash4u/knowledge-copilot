#!/usr/bin/env bash
# Ask the copilot questions as an ADMIN.
#
# Expected: everything in the admin's tenant is answerable, including admin-only content.
# Documents from the other tenant are still refused: admin rights do not cross tenants.
#
# Usage: ./scripts/demo/ask-as-admin.sh               (API on http://localhost:8000)
#        BASE_URL=http://host:8000 ./scripts/demo/ask-as-admin.sh
# Needs: curl, jq, and the API running with COPILOT_DEV_TOKEN_ENDPOINT=true.
set -euo pipefail

BASE_URL="${BASE_URL:-http://localhost:8000}"
ROLE="admin"
USER_ID="${USER_ID:-carol.admin}"

# 1. Create a token for each tenant. The role comes only from this token.
POLICY_TOKEN=$(curl -sf -X POST "$BASE_URL/auth/dev-token" \
  -H "Content-Type: application/json" \
  -d "{\"user_id\": \"$USER_ID\", \"role\": \"$ROLE\", \"tenant\": \"policy\"}" | jq -r .access_token)

ENGINEERING_TOKEN=$(curl -sf -X POST "$BASE_URL/auth/dev-token" \
  -H "Content-Type: application/json" \
  -d "{\"user_id\": \"$USER_ID\", \"role\": \"$ROLE\", \"tenant\": \"engineering\"}" | jq -r .access_token)

# 2. Ask a question and print the answer, citations and flags.
ask() {
  local token="$1" expect="$2" question="$3"
  echo "────────────────────────────────────────────────────────────"
  echo "Q ($ROLE, expect: $expect): $question"
  curl -sf -X POST "$BASE_URL/ask" \
    -H "Authorization: Bearer $token" \
    -H "Content-Type: application/json" \
    -d "$(jq -n --arg q "$question" '{question: $q}')" \
  | jq -r '
      "A: \(.text)",
      (if (.citations | length) > 0 then "Sources:" else empty end),
      (.citations[] | "  [\(.marker)] \(.title) — \(.section)"),
      "refused=\(.refused)  cached=\(.cached)  total_ms=\(.timings_ms.total // 0 | floor)"'
}

echo "== Policy tenant: admin-only =="
ask "$POLICY_TOKEN" answer "What is the cap on the executive bonus pool?"
ask "$POLICY_TOKEN" answer "How often is privileged access reviewed?"
ask "$POLICY_TOKEN" answer "When can break-glass accounts be used?"
ask "$POLICY_TOKEN" answer "How are master encryption keys escrowed?"
ask "$POLICY_TOKEN" answer "How soon must a personal data breach be reported to the regulator?"

echo
echo "== Policy tenant: manager and employee content is visible too =="
ask "$POLICY_TOKEN" answer "What is the salary range for level L5?"
ask "$POLICY_TOKEN" answer "What is the daily meal allowance for domestic travel?"

echo
echo "== Engineering tenant: admin-only =="
ask "$ENGINEERING_TOKEN" answer "How do I fail over the payments Postgres cluster?"
ask "$ENGINEERING_TOKEN" answer "How often are database root credentials rotated?"
ask "$ENGINEERING_TOKEN" answer "How do I roll back a service?"

echo
echo "== Cross-tenant: other tenant's documents (should be refused) =="
ask "$POLICY_TOKEN" refuse "How often are database root credentials rotated?"
ask "$ENGINEERING_TOKEN" refuse "What is the cap on the executive bonus pool?"
