#!/usr/bin/env bash
# System integration test: gdrive auto-indexing (oikb sidecar).
#
# Asserts the gdrive-indexer sidecar is up, oikb's daemon is ready, its source
# sync status is healthy, and the OWUI "gdrive" KB has files indexed. Does a
# best-effort semantic search over the KB (a hit is a bonus, not a hard
# requirement — name/content matching is not guaranteed for arbitrary docs).
#
# Tolerant: SKIPs (passes with a notice) when gdrive auto-indexing is not
# provisioned — GDRIVE_KB_ID unset in .env.local, or ./gdrive has no
# allowlisted files — so `make test` still runs clean in a bare environment.
set -u
. "$(dirname "$0")/lib.sh"
load_env
require_stack_up

O="$(kb_host)"
CTN="kb-gdrive-indexer"
# Allowlist must match scripts/gdrive-status.sh (Python set) + .oikb.yaml. find's
# default Emacs regex treats (a|b) as LITERAL (matches 0 files), so every -iregex
# call below MUST use -regextype posix-extended for the alternation to work.
ALLOW_RE='[.](docx|pdf|pptx|xlsx|txt|md|csv|html|json|log|tex|py|s|c|h|inc|cfg)$'

# --- skip conditions ---------------------------------------------------------
if [ -z "${GDRIVE_KB_ID:-}" ]; then
  section "gdrive index"
  pass "SKIP: GDRIVE_KB_ID not set in .env.local (run: make gdrive-index-bootstrap)"
  finish
  exit 0
fi
src_count=$(find gdrive -type f -regextype posix-extended -iregex ".*${ALLOW_RE}" 2>/dev/null | wc -l)
if [ "${src_count:-0}" -eq 0 ]; then
  section "gdrive index"
  pass "SKIP: ./gdrive has no allowlisted files to index (run: make gdrive-sync)"
  finish
  exit 0
fi

require_env OPENWEBUI_USER_API_KEY || { finish; exit 1; }
UK="$OPENWEBUI_USER_API_KEY"
AUTH=(-H "Authorization: Bearer $UK")

# --- indexer container up ----------------------------------------------------
section "gdrive-indexer container"
st=$(docker ps --filter "name=^/${CTN}$" --format '{{.Status}}' 2>/dev/null)
if printf '%s' "$st" | grep -qi 'Up'; then
  pass "container up: $st"
else
  fail "container not up: ${st:-<missing>}"
  fail "run: make gdrive-index-bootstrap"
  finish
  exit 1
fi

# --- oikb daemon ready (via docker exec; no published port) ------------------
section "oikb daemon ready"
ready=$(docker exec "$CTN" python -c \
  "import urllib.request,sys; sys.stdout.write(str(urllib.request.urlopen('http://localhost:8080/health/ready',timeout=5).status))" \
  2>/dev/null || true)
if [ "$ready" = "200" ]; then
  pass "oikb /health/ready -> 200"
else
  fail "oikb /health/ready not 200 (got ${ready:-<no response>})"
  finish
  exit 1
fi

# --- oikb source sync status -------------------------------------------------
# oikb 0.4.0 per-source status (daemon.py): "running" while a sync cycle is
# active, "success" when the cycle completed with no errors, "partial" when it
# completed with some per-file errors, "error" on an exception. The daemon
# syncs on a 30s interval, so a one-shot read can land mid-cycle ("running") on
# an otherwise-healthy indexer. Poll briefly (60s) for a completion state
# (success/ok/partial); only a stuck "running" or "error" is a failure. ("ok" is
# the top-level daemon health string, kept here defensively for older oikb.)
section "oikb gdrive source status"
oikb_status=""
src_state=""
for _ in $(seq 1 12); do
  src_state=$(docker exec "$CTN" python -c \
    "import urllib.request,json; d=json.load(urllib.request.urlopen('http://localhost:8080/health',timeout=5)); s=d.get('sources',{}).get('/gdrive',{}); print(s.get('status','?'), '|', s.get('error',''))" \
    2>/dev/null || true)
  oikb_status="${src_state%% *}"
  case "$oikb_status" in
    success|ok|partial) break;;
  esac
  sleep 5
done
case "$oikb_status" in
  success|ok|partial) pass "oikb source status=${oikb_status}";;
  *)
    fail "oikb source status=${oikb_status} (${src_state})"
    fail "indexer did not reach a completion state (success/partial) in 60s - stuck mid-cycle or error; check: docker logs $CTN"
    finish
    exit 1
    ;;
esac

# --- OWUI KB has files indexed ----------------------------------------------
# file_count is exposed only on the LIST endpoint (GET /api/v1/knowledge/), not
# the detail endpoint (whose files array is empty). List with the agent key and
# pick the gdrive KB by id.
section "OWUI gdrive KB file_count"
fc=$(curl -s "$O/api/v1/knowledge/" "${AUTH[@]}" \
  | python3 -c '
import sys,json
d=json.load(sys.stdin)
items=d.get("items",d) if isinstance(d,dict) else d
fc=0
for k in items:
    if k.get("id")==sys.argv[1]:
        fc=k.get("file_count",0) or 0; break
print(fc)
' "$GDRIVE_KB_ID" 2>/dev/null || true)
if [ "${fc:-0}" -gt 0 ]; then
  pass "KB file_count=${fc} (source allowlisted=${src_count})"
else
  fail "KB file_count=0 — nothing indexed yet (source=${src_count})"
  fail "if first sync is still running, wait and re-run; else check: docker logs $CTN"
  finish
  exit 1
fi

# --- best-effort semantic search --------------------------------------------
section "semantic search over gdrive KB (best-effort)"
qfile=$(find gdrive -type f -regextype posix-extended -iregex ".*${ALLOW_RE}" 2>/dev/null | head -1)
q=$(basename "$qfile" | sed -E 's/\.[A-Za-z0-9]+$//' | tr -c '[:alnum:]' ' ' | tr -s ' ' | sed -E 's/^ | $//g')
if [ -z "$q" ]; then q="verification plan"; fi
hits=$(curl -s -X POST "$O/api/v1/retrieval/query/collection" "${AUTH[@]}" -H 'Content-Type: application/json' \
  -d "{\"collection_names\":[\"${GDRIVE_KB_ID}\"],\"query\":$(python3 -c 'import sys,json;print(json.dumps(sys.argv[1]))' "$q"),\"k\":4,\"hybrid\":true}" \
  | python3 -c '
import sys,json
d=json.load(sys.stdin)
docs=[]
if isinstance(d,list): docs=d
elif isinstance(d,dict):
    if "documents" in d: docs=[t for sub in d["documents"] for t in (sub if isinstance(sub,list) else [sub])]
    else: docs=d.get("files") or d.get("results") or d.get("docs") or []
print(len(docs))
' 2>/dev/null || true)
if [ "${hits:-0}" -gt 0 ]; then
  pass "search q=\"$q\" -> ${hits} hit(s)"
else
  pass "search q=\"$q\" -> 0 hits (best-effort; not a failure — name/content matching is not guaranteed)"
fi

finish