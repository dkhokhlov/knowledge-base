#!/usr/bin/env bash
# Isolated e2e for the api-gateway Graphiti agent surface: drives
# skills/claude/scripts/kb_gateway.py through every gateway endpoint the agent
# surface exposes (whoami, status, groups, add, retrieve, episodes, delete-edge,
# delete-episode, forget) against a THROWAWAY stack (separate compose project,
# NOT the live kb-* stack).
#
# Why isolated: the `forget` at the end deletes the agent's ENTIRE Graphiti
# group. Against the live stack that destroys the live agent's facts memory.
# Against a throwaway stack it only touches the throwaway agent's OWN freshly
# created group: e2e_provision makes a fresh admin + agent account + the clone
# has a fresh empty Neo4j, so the agent identity + its Graphiti group are
# disposable. Isolation contains the destructive op to throwaway state.
#
# Uses scripts/e2e-env.sh (clone + compose project + container-rename override
# + provision + teardown) so the live stack is never touched and the isolation
# logic is NOT duplicated here. See e2e-env.sh.
#
# e2e_provision (make start + admin-signup + api-keys) suffices for the /memory
# endpoints -- no extra provisioning:
#   - make start brings up api-gateway + graphiti + neo4j (compose.yml services).
#   - /memory derives identity from the agent bearer key (the gateway calls OWUI
#     /api/v1/auths/ with it) and needs NO admin key (api-gateway starts with a
#     bare ${OPENWEBUI_ADMIN_API_KEY:-}, empty -- /memory does not use it), NO
#     rag-config (that is OWUI RAG embedding config for /retrieval/query), NO
#     projects-bootstrap (OWUI KB workspace perm), NO gdrive.
#   - The shared EXTERNAL Ollama already has the extraction model (qwen2.5:14b,
#     graphiti/config.yaml default) pulled by the live stack, so the async
#     extraction after `add` works without a model pull.
# So test_08 is a standalone isolated e2e (like test_12), NOT run by
# test-e2e-iso (it would clone a nested stack + collide). Run it directly.
#
# Usage: bash tests/test_08_e2e.sh [TEST08_PORT=3030]
#   TEST08_PORT - host port for the isolated Caddy (default 3030; must not
#                collide with the live KB_HOST_PORT 3000, e2e 3010, or kbcheck 3020).
#   TEST08_KEEP=1 - keep the stack + clone on FAILURE for inspection.
# Requires: OLLAMA_HOST resolvable (shell env, live .env, or live stack up) and
# the locally-built open-webui overlay image present.
set -u

PORT="${TEST08_PORT:-3030}"
NAME="test08"

# Source the reusable isolation lib (sets E2E_SRC + the e2e_* functions).
# ORDER MATTERS: lib.sh (next) does `cd "$KB_ROOT"` at SOURCE time, where
# KB_ROOT resolves from BASH_SOURCE to the LIVE repo (this script is invoked
# from there). e2e_isolate below then cds INTO the clone. load_env (called after
# e2e_isolate) reads ./.env from the clone cwd. Swapping these two source lines
# would point load_env + every `make` at the live tree -- keep e2e-env.sh before
# lib.sh. (Same ordering as test_12_kb_check.sh.)
. "$(cd "$(dirname "$0")/.." && pwd)/scripts/e2e-env.sh"

# Source the test helpers (pass/fail/section/finish) for consistent output.
. "$(dirname "$0")/lib.sh"

ISOLATED=0  # set 1 once e2e_isolate succeeds (we own the clone); the EXIT trap
            # tears down ONLY a clone we created, never a pre-existing leftover.

cleanup() {
  local rc=$?
  # Tear down the isolated stack + remove the clone -- but ONLY if e2e_isolate
  # created it (ISOLATED=1). e2e_isolate stamps a unique clone per run, so it
  # never refuses on a leftover; ISOLATED=1 means THIS run's clone exists and the
  # trap cleans only it. The live stack is never touched (separate project +
  # container names).
  # TEST08_KEEP=1 keeps the stack + clone on FAILURE for inspection (mirrors
  # test_12's KBCHECK_KEEP); default 0 always cleans up.
  if [ "$ISOLATED" = "1" ]; then
    if [ "$rc" -ne 0 ] && [ "${TEST08_KEEP:-0}" = "1" ]; then
      echo "==> KEEP (TEST08_KEEP=1): stack left for inspection (port $PORT, project $COMPOSE_PROJECT_NAME, clone $E2E_CLONE); tear down with: make clean-test NAME=$NAME STAMP=$E2E_STAMP" >&2
    else
      e2e_down "$NAME" 2>/dev/null || true
    fi
  fi
  exit "$rc"
}
trap cleanup EXIT

# --- 1. isolate + provision a throwaway stack -------------------------------
section "isolated stack (project kb-$NAME, port $PORT)"
e2e_resolve_ollama || { fail "OLLAMA_HOST resolution failed"; finish; exit 1; }
e2e_isolate "$NAME" "$PORT" || { fail "e2e_isolate failed"; finish; exit 1; }
ISOLATED=1
pass "clone + isolation env ready ($E2E_CLONE)"
e2e_provision || { fail "e2e_provision failed (start/admin-signup/api-keys)"; finish; exit 1; }
pass "isolated stack up + admin/agent keys provisioned"

# Load the clone's secrets (agent key) the standard way (lib.sh load_env, but in
# the clone cwd -- it reads ./.env + ./.env.local relative to cwd=$E2E_CLONE).
# e2e_provision already waited for /health, so no separate require_stack_up.
load_env
require_env OPENWEBUI_USER_API_KEY || { finish; exit 1; }

# --- 2. agent-surface body (verbatim from the non-isolated original) --------
# Drives skills/claude/scripts/kb_gateway.py through every gateway endpoint the
# agent surface exposes:
#   whoami, status, groups, add, retrieve, episodes, delete-edge,
#   delete-episode, forget
# Exercises delete-edge (via a fact uuid, since /memory/episodes does not
# serialize entity_edges) and delete-episode, which test_06 does not cover.
# Non-destructive: operates only on the agent's own group; forgets the agent
# group at the end.
G="$(kb_host)"
KB_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# The wrapper is a thin client: it reads ONLY KB_HOST + KB_API_KEY from the
# shell env (no --env-file / --key / --base-url flags). Inline `env` sets both
# per invocation. KB = agent key. KB_ROOT resolves to the CLONE root here (cwd
# is the clone after e2e_isolate), so the clone's kb_gateway.py is the code under
# test -- NOT the live repo's copy (clean-tree guard guarantees they are at the
# same commit, but the clone is the code being verified).
KB="env KB_API_KEY=${OPENWEBUI_USER_API_KEY} KB_HOST=${G} python3 ${KB_ROOT}/skills/claude/scripts/kb_gateway.py"

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
AGENT_EMAIL=$(printf '%s' "$WHO" | python3 -c 'import sys,json;print(json.load(sys.stdin).get("email") or "")')
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
AGENT_GROUP_ID=$(printf '%s' "$ADDOUT" | python3 -c 'import sys,json;print(json.load(sys.stdin).get("group") or "")')
[ -n "$AGENT_GROUP_ID" ] && pass "agent add -> ${AGENT_GROUP_ID}" || fail "agent add failed"

section "fact extracted (searchable after async extraction)"
# Budget 420 s @ 10 s. The ctx-baked model (num_ctx=8192, ~20 GB) fits the
# GPU, so a fact is searchable in ~9 s warm; the budget covers a cold first
# load (model load ~30 s) plus concurrent test load. Detection greps for the
# stable descriptive noun ("cryostat" / "lattice-D"), NOT the run-id number
# (which extraction sometimes drops). The group was emptied above, so the
# first cryostat fact to appear IS this add's.
fact_found=0
for i in $(seq 1 42); do
  if kbrun $KB retrieve "cryostat lattice-D" --k 5 2>/dev/null | grep -qE "cryostat|lattice-D"; then fact_found=1; break; fi
  sleep 10
done
if [ "$fact_found" = "1" ]; then pass "fact extracted (cryostat in /memory/retrieve)"; else fail "no fact in 420s (cryostat not searchable)"; fi

section "delete-edge (fact uuid) + delete-episode"
# Pick the first fact whose JSON contains the stable noun "cryostat" (the
# group was emptied above, so this is this add's fact). Gating on fact_found
# avoids f[0] being an unrelated fact when the probe is absent. The run-id
# number is NOT used (extraction sometimes drops it).
FACT_UUID=$(kbrun $KB retrieve "cryostat lattice-D" --k 5 2>/dev/null | python3 -c 'import sys,json; f=json.load(sys.stdin).get("facts") or []; print(next((x["uuid"] for x in f if "cryostat" in json.dumps(x).lower()), ""))' 2>/dev/null)
EP_UUID=$(kbrun $KB episodes --max 20 2>/dev/null | python3 -c 'import sys,json; eps=json.load(sys.stdin).get("episodes") or []; print(next((e["uuid"] for e in eps if str(e.get("name","")).startswith("e2e-")), eps[0]["uuid"] if eps else ""))' 2>/dev/null)
if [ "$fact_found" = "1" ] && [ -n "$FACT_UUID" ]; then
  DELOUT=$(kbrun $KB delete-edge "$FACT_UUID") || true
  printf '%s' "$DELOUT" | python3 -c 'import sys,json;d=json.load(sys.stdin);exit(0 if d.get("uuid") else 1)' && pass "delete-edge ${FACT_UUID}" || fail "delete-edge failed"
  # verify the edge is gone from retrieve
  kbrun $KB retrieve "$RID" --k 5 2>/dev/null | grep -q "$FACT_UUID" \
    && fail "delete-edge: ${FACT_UUID} still in retrieve" \
    || pass "delete-edge verified gone"
else
  fail "no probe fact uuid to delete-edge (fact_found=${fact_found})"
fi
if [ -n "$EP_UUID" ]; then
  DELOUT=$(kbrun $KB delete-episode "$EP_UUID") || true
  printf '%s' "$DELOUT" | python3 -c 'import sys,json;d=json.load(sys.stdin);exit(0 if d.get("uuid") else 1)' && pass "delete-episode ${EP_UUID}" || fail "delete-episode failed"
else
  fail "no episode uuid to delete-episode"
fi

section "forget own group -> agent group gone"
FORGOT=$(kbrun $KB forget "$AGENT_GROUP") || true
printf '%s' "$FORGOT" | python3 -c 'import sys,json;d=json.load(sys.stdin);exit(0 if d.get("group") else 1)' && pass "agent forget ${AGENT_GROUP}" || fail "agent forget failed"
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
[ "$gone" = "1" ] && pass "agent group ${AGENT_GROUP_ID} gone from /memory/groups" \
                || fail "agent group ${AGENT_GROUP_ID} still in /memory/groups after 60s"

finish