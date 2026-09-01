#!/usr/env/bin bash
# System integration test: gdrive `.meta.json` sidecar -> File.meta.data.gdrive
# -> kb skill retrieve gdrive-join, end-to-end on a real stack.
#
# The unit tests (test_gateway_unit _gdrive_meta_for, test_output_json canned join)
# cover the sidecar parse + the join in isolation with synthetic fixtures. This
# test closes the e2e gap (claude review risk #5): no test before it created a
# REAL `.meta.json` sidecar, indexed it, and asserted the grounding lands in
# File.meta.data.gdrive AND comes back through the kb skill `retrieve` join.
#
# Flow: index a small committed fixture set (docs + their .meta.json sidecars
# under root/.tests/meta-sidecar/) into a throwaway temp KB via
# POST /index?dir=.tests&path=meta-sidecar (the gateway reads each sidecar at
# upload via _gdrive_meta_for and stores it in File.meta.data.gdrive). Poll the
# drain. Then invoke the kb skill `retrieve` (skills/claude/scripts/kb.py) for
# each fixture's rare marker token and assert the returned hit's `gdrive` field
# carries the sidecar's grounded flag, labels, and approval status -- proving
# the full sidecar -> gateway -> DB -> kb-skill-join -> output path. One fixture
# has no sidecar (control: its hit's gdrive must be None, not fabricated).
#
# Self-contained: the temp KB is created with the admin key, granted '*' read so
# the agent (user) key can retrieve + read file meta (the kb skill _file_gdrive
# join does one GET /files/{id} per hit), and deleted on EXIT (its files too).
# The committed fixture files under root/.tests/meta-sidecar/ are NOT deleted.
set -u
. "$(dirname "$0")/lib.sh"
load_env
require_stack_up

G="$(kb_host)"
# Allowlist must match gateway DEFAULT_ALLOW (gateway/app.py). find's default
# Emacs regex treats (a|b) as LITERAL, so every -iregex below uses -regextype
# posix-extended.
ALLOW_RE='[.](docx|pdf|pptx|xlsx|txt|md|html|json|log|tex)$'
FIXDIR="root/.tests/meta-sidecar"

# --- skip condition: the committed fixture must exist -------------------------
# Count only files the gateway indexes: exclude the .meta/.meta.json sidecars
# (_entry_for skips them by name), so src_count matches what the drain will
# complete. A missing fixture dir -> SKIP (clean `make test` in a sparse checkout).
if [ ! -d "$FIXDIR" ]; then
  section "gdrive meta sidecar (fixture)"
  pass "SKIP: $FIXDIR missing (committed fixture not present)"
  finish
  exit 0
fi
src_count=$(find "$FIXDIR" -type f -regextype posix-extended -iregex ".*${ALLOW_RE}" \
  ! -name '*.meta' ! -name '*.meta.json' 2>/dev/null | wc -l)
if [ "${src_count:-0}" -eq 0 ]; then
  section "gdrive meta sidecar (fixture)"
  pass "SKIP: $FIXDIR has no allowlisted fixture files"
  finish
  exit 0
fi

require_env OPENWEBUI_ADMIN_API_KEY KB_API_KEY || { finish; exit 1; }
AK="$OPENWEBUI_ADMIN_API_KEY"
UK="$KB_API_KEY"
ADM=(-H "Authorization: Bearer $AK")
RD=(-H "Authorization: Bearer $UK")
CT="Content-Type: application/json"
KB_ID=""
KB_NAME="meta-sidecar-test"

# The kb skill is a thin client: it reads ONLY KB_HOST + KB_API_KEY from the
# shell env. KB_ROOT resolves to the CLONE root (cwd is the clone in the iso
# fixture), so the clone's kb.py is the code under test (same pattern as test_08).
KB_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
KB="env KB_HOST=${G} KB_API_KEY=${UK} python3 ${KB_ROOT}/skills/claude/scripts/kb.py"

# --- cleanup: delete the temp KB + its files (NOT the committed fixture) ------
cleanup() {
  local fid
  if [ -n "$KB_ID" ]; then
    for fid in $(curl -s "$G/api/v1/files/?content=false&page=1" "${ADM[@]}" 2>/dev/null \
        | python3 -c 'import sys,json
try:
  d=json.load(sys.stdin)
except Exception:
  sys.exit(0)
kb=sys.argv[1]
for it in (d.get("items") or []):
  if ((it.get("meta") or {}).get("data") or {}).get("knowledge_id")==kb and it.get("id"):
    print(it["id"])
' "$KB_ID" 2>/dev/null); do
      curl -sf -X DELETE "$G/api/v1/files/${fid}" "${ADM[@]}" >/dev/null 2>&1 \
        || echo "  cleanup: DELETE file ${fid} failed" >&2
    done
    curl -sf -X DELETE "$G/api/v1/knowledge/${KB_ID}/delete" "${ADM[@]}" >/dev/null 2>&1 \
      || echo "  cleanup: DELETE kb ${KB_ID} failed" >&2
  fi
}
trap cleanup EXIT

# --- create a temp KB + grant '*' read so the user key can retrieve+read meta -
section "create temp meta-sidecar KB"
KB_ID=$(curl -s -X POST "$G/api/v1/knowledge/create" "${ADM[@]}" -H "$CT" \
  -d '{"name":"meta-sidecar-test","description":"integration test: .meta.json sidecar -> gdrive join"}' \
  | python3 -c 'import sys,json;print(json.load(sys.stdin).get("id",""))' 2>/dev/null)
[ -n "$KB_ID" ] && pass "KB id: $KB_ID" || { fail "KB create failed"; finish; exit 1; }

curl -s -X POST "$G/api/v1/knowledge/${KB_ID}/access/update" "${ADM[@]}" -H "$CT" \
  -d "{\"access_grants\":[{\"resource_type\":\"knowledge\",\"resource_id\":\"${KB_ID}\",\"principal_type\":\"user\",\"principal_id\":\"*\",\"permission\":\"read\"}]}" >/dev/null 2>&1
pass "granted '*' read on temp KB"

# --- POST /index (admin): reconcile root/.tests/meta-sidecar into the temp KB --
# path=meta-sidecar scopes the walk to the fixture subpath (the .meta.json is
# skipped by _entry_for; the .txt is indexed). The gateway reads the sidecar via
# _gdrive_meta_for at upload + stores it in File.meta.data.gdrive.
section "POST /index (api-gateway, dir=.tests path=meta-sidecar)"
idx_resp=$(curl -sS --max-time 1200 -X POST \
  "$G/index?dir=.tests&path=meta-sidecar&kb_id=${KB_ID}" \
  "${ADM[@]}" -H "$CT" -d '{}' 2>&1)
read -r added modified deleted unmodified retried errn < <(printf '%s' "$idx_resp" | python3 -c '
import sys, json
try:
    d = json.load(sys.stdin)
except Exception:
    print("0 0 0 0 0 0"); sys.exit(0)
if not isinstance(d, dict) or "ok" not in d:
    print("0 0 0 0 0 0"); sys.exit(0)
print(d.get("added", 0), d.get("modified", 0), d.get("deleted", 0),
      d.get("unmodified", 0), d.get("retried", 0), len(d.get("errors") or []))
' 2>/dev/null || echo "0 0 0 0 0 0")
if [ "$added" = "0" ] && [ "$modified" = "0" ] && [ "$unmodified" = "0" ] && [ "$errn" = "0" ]; then
  fail "POST /index returned no parseable result: ${idx_resp}"
  finish
  exit 1
fi
pass "/index: added=${added} modified=${modified} deleted=${deleted} unmodified=${unmodified} retried=${retried} errors=${errn}"
if [ "${errn:-0}" -gt 0 ]; then
  fail "/index reported ${errn} per-file error(s):"
  printf '%s' "$idx_resp" | python3 -c '
import sys, json
d = json.load(sys.stdin)
for e in (d.get("errors") or [])[:20]:
    print("    " + str(e.get("filename") or e.get("file_id") or "?") +
          ": " + str(e.get("status", "")) + " - " + str(e.get("error", "")))
'
  finish
  exit 1
fi

# --- poll GET /status until the drain reaches a terminal state ---------------
section "poll GET /status (real drain, path=meta-sidecar)"
wait_s="${META_SIDECAR_WAIT:-180}"
deadline=$(( $(date +%s) + wait_s ))
completed=0; pending=0; processing=0; failed=0
while :; do
  read -r completed pending processing failed < <(curl -sS "$G/status?dir=.tests&kb_id=${KB_ID}&json=1" "${ADM[@]}" 2>/dev/null | python3 -c '
import sys, json
try:
    d = json.load(sys.stdin)
    print(d.get("indexed_count",0), d.get("pending",0), d.get("processing",0), d.get("failed",0))
except Exception:
    print("0 0 0 0")
')
  in_flight=$(( pending + processing ))
  accounted=$(( completed + failed ))
  if [ "$in_flight" = "0" ] && [ "$accounted" -ge "$src_count" ]; then break; fi
  if [ "$(date +%s)" -ge "$deadline" ]; then break; fi
  sleep 5
done
in_flight=$(( pending + processing ))
accounted=$(( completed + failed ))
if [ "$in_flight" = "0" ] && [ "$accounted" -ge "$src_count" ]; then
  pass "drain terminal: completed=${completed} failed=${failed} source=${src_count}"
else
  fail "drain did not terminate after ${wait_s}s: completed=${completed} failed=${failed} pending=${pending} processing=${processing} source=${src_count}"
  fail "check: docker logs ${OWUI_CONTAINER:-kb-openwebui}"
  finish
  exit 1
fi

# --- kb skill retrieve: assert the gdrive join over the fixture manifest ------
# Loop over the fixture manifest: each doc carries a rare marker token and an
# expected gdrive record (grounded / label / approval), or none (the no-sidecar
# control). For each: retrieve the marker, find the hit whose text carries it,
# and assert the joined gdrive record matches the sidecar (or is None for the
# control, proving no fabrication). Bounded retry (3 attempts) for a cold
# collection; a decisive result (OK / MISMATCH) stops the retry early.
# The kb skill `retrieve` POSTs /retrieve (gateway -> OWUI, caller's key) for the
# marker, then joins File.meta.data.gdrive per hit file_id (one GET /files/{id}).
section "kb skill retrieve -> gdrive join over the fixture manifest"
# manifest rows: marker|exp_grounded|exp_label|exp_approval|has_gdrive
manifest=(
  "meta-sidecar-marker-9d4e1|true|grounded|approved|yes"
  "meta-sidecar-marker-7b2f4|false|draft|pending_review|yes"
  "meta-sidecar-marker-5c8a1|true|approved|approved|yes"
  "meta-sidecar-marker-3e6d9|false|_|_|no"
)
fail_count=0
for row in "${manifest[@]}"; do
  IFS='|' read -r marker exp_grounded exp_label exp_approval has_gdrive <<< "$row"
  verdict=""
  for _attempt in 1 2 3; do
    verdict=$(env KB_HOST="$G" KB_API_KEY="$UK" \
      python3 "${KB_ROOT}/skills/claude/scripts/kb.py" retrieve "$KB_NAME" "$marker" --k 5 2>/tmp/t17_err \
      | python3 -c '
import sys, json
marker, exp_grounded, exp_label, exp_approval, has_gdrive = sys.argv[1:6]
try:
    d = json.load(sys.stdin)
except Exception:
    print("PARSE_ERR"); sys.exit(0)
hits = d.get("hits") or []
# find the hit whose text carries this marker (all chunks of one file share
# the same file-level gdrive record, so the first match is representative)
hit = None
for h in hits:
    if marker in (h.get("text") or ""):
        hit = h
        break
if hit is None:
    print("MISSING"); sys.exit(0)
g = hit.get("gdrive")
if has_gdrive == "no":
    # control: no sidecar -> gdrive must be None (no fabrication)
    if g is None:
        print("OK gdrive=None (no-sidecar control)")
    else:
        print("MISMATCH control got gdrive=%r want None" % (g,))
    sys.exit(0)
# has_gdrive == yes: assert the sidecar fields landed
if g is None:
    print("MISMATCH no gdrive record (sidecar not stored / join failed)")
    sys.exit(0)
errs = []
if g.get("grounded") != (exp_grounded == "true"):
    errs.append("grounded=%r want %s" % (g.get("grounded"), exp_grounded))
if exp_label not in (g.get("labels") or []):
    errs.append("labels=%r want %s" % (g.get("labels"), exp_label))
if g.get("approval_status") != exp_approval:
    errs.append("approval_status=%r want %s" % (g.get("approval_status"), exp_approval))
if errs:
    print("MISMATCH " + "; ".join(errs))
else:
    print("OK grounded=%s labels=%r approval=%s" % (
        g.get("grounded"), g.get("labels"), g.get("approval_status")))
' "$marker" "$exp_grounded" "$exp_label" "$exp_approval" "$has_gdrive" 2>/dev/null || echo "PARSE_ERR")
    case "$verdict" in
      OK*|MISMATCH*) break ;;           # decisive -> stop retrying
      MISSING|PARSE_ERR|"") [ "$_attempt" -lt 3 ] && sleep 5 ;;
    esac
  done
  case "$verdict" in
    OK*) pass "q=\"${marker}\" -> ${verdict}" ;;
    *) fail "q=\"${marker}\" -> ${verdict:-no result} (err: $(cat /tmp/t17_err 2>/dev/null | head -c 160))"
       fail_count=$((fail_count + 1)) ;;
  esac
done
if [ "$fail_count" -gt 0 ]; then
  fail "${fail_count} of ${#manifest[@]} manifest rows failed the gdrive join"
  finish
  exit 1
fi

finish