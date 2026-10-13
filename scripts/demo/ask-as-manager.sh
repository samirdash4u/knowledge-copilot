#!/usr/bin/env bash
# Ask the copilot questions as a MANAGER.
#
# Expected: everything an employee can see, plus manager-only content (salary bands, approval
# limits, disciplinary procedure, on-call pay). Admin-only content is still refused.
#
# Usage: ./scripts/demo/ask-as-manager.sh             (API on http://localhost:8000)
#        BASE_URL=http://host:8000 ./scripts/demo/ask-as-manager.sh
# Needs: curl, jq, and the API running with COPILOT_DEV_TOKEN_ENDPOINT=true.
set -euo pipefail

BASE_URL="${BASE_URL:-http://localhost:8000}"
ROLE="manager"
USER_ID="${USER_ID:-bob.manager}"

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

echo "== Policy tenant: shared with employees =="
ask "$POLICY_TOKEN" answer "How many days of paid annual leave do full-time employees get?"
ask "$POLICY_TOKEN" answer "Who has to approve working from abroad, and is there a limit on days?"

echo
echo "== Policy tenant: manager-only =="
ask "$POLICY_TOKEN" answer "What is the salary range for level L4?"
ask "$POLICY_TOKEN" answer "What is the merit increase budget this cycle?"
ask "$POLICY_TOKEN" answer "Up to what amount can a manager approve an expense claim?"
ask "$POLICY_TOKEN" answer "How quickly must a manager respond to a leave request?"
ask "$POLICY_TOKEN" answer "How long does a performance improvement plan last?"
ask "$POLICY_TOKEN" answer "What happens in a case of gross misconduct?"

echo
echo "== Restricted to admins (should be refused) =="
ask "$POLICY_TOKEN" refuse "What is the cap on the executive bonus pool?"
ask "$POLICY_TOKEN" refuse "When can break-glass accounts be used?"
ask "$POLICY_TOKEN" refuse "How are master encryption keys escrowed?"

echo
echo "== Engineering tenant =="
ask "$ENGINEERING_TOKEN" answer "How fast do I need to acknowledge a page?"
ask "$ENGINEERING_TOKEN" answer "How much is the weekly on-call allowance?"
ask "$ENGINEERING_TOKEN" answer "Who approves an emergency fix during a deploy freeze?"
ask "$ENGINEERING_TOKEN" refuse "How do I fail over the payments Postgres cluster?"

echo
echo "== Cache check: same question twice (second should be cached=true) =="
ask "$POLICY_TOKEN" answer "What is the salary range for level L3?"
ask "$POLICY_TOKEN" answer "What is the salary range for level L3?"
