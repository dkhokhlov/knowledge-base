#!/usr/bin/env bash
# System integration test: comprehensive kb-gateway end-to-end surface.
# Drives skills/claude/scripts/kb_gateway.py through every gateway endpoint
# the agent + admin surfaces expose:
#   agent:  whoami, status, groups, add, search, episodes, delete-edge,
#           delete-episode, forget
#   admin:  user-create (+ the issued key resolves to the new user)
#   deny:   non-admin POST /admin/users -> 403
# Exercises delete-edge (via a fact uuid, since /memory/episodes does not
# serialize entity_edges) and delete-episode, which test_06 does not cover.
# Non-destructive: operates only on the agent's own group + a throwaway created
# user; forgets the agent group at the end.
set -u
. "$(dirname "$0")/lib.sh"
load_env
require_stack_up
require_env OPENWEBUI_ADMIN_API_KEY OPENWEBUI_USER_API_KEY || { finish; exit 1; }

G="$(kb_host)"
KB_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
KB="python3 ${KB_ROOT}/skills/claude/scripts/kb_gateway.py --env-file .env --env-file .env.local"
KBA="${KB} --key ${OPENWEBUI_ADMIN_API_KEY}"
CT="Content-Type: application/json"

# kbrun <cmd...>: print stdout; record a failure (and return 1) if the cmd
# exits non-zero. Does not exit the script (lib.sh uses pass/fail/finish).
kbrun() {
  local out rc
  out=$("$@" 2>/tmp/t08_err); rc=$?
  if [ "$rc" != 0 ]; then fail "kb_gateway failed ($(cat /tmp/t08_err 2>/dev/null))"; return 1; fi
  printf '%s' "$out"
}

section "agent identity + status"
WHO=$(kbrun $KB whoami) || { finish; exit 1; }
AGENT_EMAIL=$(printf '%s' "$WHO" | awk '{print $1}')
AGENT_GROUP="user:${AGENT_EMAIL}"
[ -n "$AGENT_EMAIL" ] && pass "agent whoami -> ${AGENT_EMAIL}" || fail "agent whoami empty"
STAT=$(kbrun $KB status) || true
printf '%s' "$STAT" | grep -q '"healthy"' && pass "agent status -> healthy" || fail "agent status not healthy"

section "agent add (probe) -> own group"
# Clean slate: forget the agent's own group first so any data left by an
# earlier test (test_06 shares this agent identity) is gone before we add.
# forget (DELETE /group/<id>) is synchronous, so this completes before add.
kbrun $KB forget "$AGENT_GROUP" >/dev/null 2>&1 || true
TS=$(date +%s)
# The probe embeds a 6-digit RID as a descriptive quantity, but the test does
# NOT rely on the number surviving extraction: at temperature=0 ggml batching
# is still non-deterministic, and the model sometimes rephrases the number out
# of the fact text. The STABLE signal is the descriptive noun phrase
# ("cryostat", "lattice-D"), which extraction preserves in every observed
# case (like "calorimeter"/"bay-5" in other probes). Detection greps for that.
# $TS (10-digit) makes the episode name (e2e-${TS}) run-unique for delete-episode.
RID=$(( TS % 900000 + 100000 ))
PROBE="E2E comprehensive probe: the cryostat on lattice-D holds exactly ${RID} cells at 15 millikelvin for calibration."
ADDOUT=$(kbrun $KB add "$PROBE" --name "e2e-${TS}") || true
AGENT_GROUP_ID=$(printf '%s' "$ADDOUT" | sed 's/^added to group //')
printf '%s' "$ADDOUT" | grep -q "added to group" && pass "agent add -> ${AGENT_GROUP_ID}" || fail "agent add failed"

section "fact extracted (searchable after async extraction)"
# Budget 420 s @ 10 s. The ctx-baked model (num_ctx=8192, ~20 GB) fits the
# GPU, so a fact is searchable in ~9 s warm; the budget covers a cold first
# load (model load ~30 s) plus concurrent test load. Detection greps for the
# stable descriptive noun ("cryostat" / "lattice-D"), NOT the run-id number
# (which extraction sometimes drops). The group was emptied above, so the
# first cryostat fact to appear IS this add's.
fact_found=0
for i in $(seq 1 42); do
  if kbrun $KB search "cryostat lattice-D" --k 5 2>/dev/null | grep -qE "cryostat|lattice-D"; then fact_found=1; break; fi
  sleep 10
done
if [ "$fact_found" = 1 ]; then pass "fact extracted (cryostat in /memory/search)"; else fail "no fact in 420s (cryostat not searchable)"; fi

section "delete-edge (fact uuid) + delete-episode"
# Pick the first fact whose JSON contains the stable noun "cryostat" (the
# group was emptied above, so this is this add's fact). Gating on fact_found
# avoids f[0] being an unrelated fact when the probe is absent. The run-id
# number is NOT used (extraction sometimes drops it).
FACT_UUID=$(kbrun $KB search "cryostat lattice-D" --k 5 2>/dev/null | python3 -c 'import sys,json; f=json.load(sys.stdin) or []; print(next((x["uuid"] for x in f if "cryostat" in json.dumps(x).lower()), ""))' 2>/dev/null)
EP_UUID=$(kbrun $KB episodes --max 20 2>/dev/null | python3 -c 'import sys,json; eps=json.load(sys.stdin) or []; print(next((e["uuid"] for e in eps if str(e.get("name","")).startswith("e2e-")), eps[0]["uuid"] if eps else ""))' 2>/dev/null)
if [ "$fact_found" = 1 ] && [ -n "$FACT_UUID" ]; then
  DELOUT=$(kbrun $KB delete-edge "$FACT_UUID") || true
  printf '%s' "$DELOUT" | grep -q "deleted edge" && pass "delete-edge ${FACT_UUID}" || fail "delete-edge failed"
  # verify the edge is gone from search
  kbrun $KB search "$RID" --k 5 2>/dev/null | grep -q "$FACT_UUID" \
    && fail "delete-edge: ${FACT_UUID} still in search" \
    || pass "delete-edge verified gone"
else
  fail "no probe fact uuid to delete-edge (fact_found=${fact_found})"
fi
if [ -n "$EP_UUID" ]; then
  DELOUT=$(kbrun $KB delete-episode "$EP_UUID") || true
  printf '%s' "$DELOUT" | grep -q "deleted episode" && pass "delete-episode ${EP_UUID}" || fail "delete-episode failed"
else
  fail "no episode uuid to delete-episode"
fi

section "forget own group -> agent group gone"
FORGOT=$(kbrun $KB forget "$AGENT_GROUP") || true
printf '%s' "$FORGOT" | grep -q "forgot group" && pass "agent forget ${AGENT_GROUP}" || fail "agent forget failed"
# Assert the agent's OWN group is gone from the read-all list. Do NOT assert
# global emptiness: other groups (admin, other tests) may have data. add_episode
# keeps writing edges briefly after the first fact is searchable, so a residual
# write can re-create group data right after forget; poll + re-forget to ride it
# out (the worker stops once extraction completes).
gone=0
for i in $(seq 1 12); do
  if ! kbrun $KB groups 2>/dev/null | grep -q "${AGENT_GROUP_ID}"; then gone=1; break; fi
  kbrun $KB forget "$AGENT_GROUP" >/dev/null 2>&1 || true
  sleep 5
done
[ "$gone" = 1 ] && pass "agent group ${AGENT_GROUP_ID} gone from /memory/groups" \
                || fail "agent group ${AGENT_GROUP_ID} still in /memory/groups after 60s"

section "admin user-create + issued key + non-admin deny"
NEW_EMAIL="e2e-${TS}@local.test"
UCOUT=$(kbrun $KBA user-create --email "$NEW_EMAIL" --name "E2E User") || true
NEW_KEY=$(printf '%s' "$UCOUT" | awk '/^kb_api_key:/ {print $2}')
if [ -n "$NEW_KEY" ]; then
  printf '%s' "$UCOUT" | grep -q "role:           user" && pass "admin user-create -> ${NEW_EMAIL} (role=user)" || fail "user-create role mismatch"
  NUWHO=$(python3 "${KB_ROOT}/skills/claude/scripts/kb_gateway.py" --base-url "$G" --key "$NEW_KEY" whoami 2>/dev/null) || true
  printf '%s' "$NUWHO" | grep -q "${NEW_EMAIL}" && pass "issued key whoami -> ${NEW_EMAIL}" || fail "issued key whoami failed"
else
  fail "admin user-create did not return a kb_api_key"
fi
code=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$G/admin/users" -H "Authorization: Bearer ${OPENWEBUI_USER_API_KEY}" -H "$CT" -d "{\"email\":\"blocked-${TS}@local.test\",\"name\":\"x\"}")
[ "$code" = 403 ] && pass "non-admin user-create -> 403" || fail "non-admin user-create -> ${code} (want 403)"

# Cleanup: the created user has no memory; nothing to forget. Leave the account
# (deterministic; like test_07).
finish