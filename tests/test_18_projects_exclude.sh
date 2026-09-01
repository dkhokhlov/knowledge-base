#!/usr/env/bin bash
# System integration test: index-projects honors <root>/.kb-ignore (gitignore-style
# allowlist) end-to-end on a real stack.
#
# The unit tests (test_kb_projects_select _select_projects, test_kb_ignore matcher)
# cover the selection logic in isolation with synthetic temp trees and no stack.
# This test closes the e2e gap: build a throwaway projects root with three project
# dirs + a `.kb-ignore` allowlist (`*` + `!allowA` + `!allowB`), run the REAL
# `index-projects --root <fixture> --host test18h --wait` (kb skill, user key),
# and assert:
#   1. the index-projects JSON lists exactly allowA + allowB (denyC absent) --
#      `--project` is absent (full scan) and `.kb-ignore` drops denyC;
#   2. GET /api/v1/knowledge/ shows KBs test18h--allowA + test18h--allowB and NOT
#      test18h--denyC -- the allowlisted KBs were really created;
#   3. retrieve-projects on allowA's rare marker returns hits > 0 -- the drain
#      landed vectors (indexing was not broken by the .kb-ignore path).
#
# Self-contained: the fixture projects root is a mktemp -d temp dir (removed on
# EXIT); the two KBs + their files are deleted on EXIT (admin key). No committed
# fixture files are touched.
set -u
. "$(dirname "$0")/lib.sh"
load_env
require_stack_up

G="$(kb_host)"
require_env OPENWEBUI_ADMIN_API_KEY KB_API_KEY || { finish; exit 1; }
AK="$OPENWEBUI_ADMIN_API_KEY"
UK="$KB_API_KEY"
ADM=(-H "Authorization: Bearer $AK")
RD=(-H "Authorization: Bearer $UK")
CT="Content-Type: application/json"
HOSTSEG="test18h"
KB_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
KB="env KB_HOST=${G} KB_API_KEY=${UK} python3 ${KB_ROOT}/skills/claude/scripts/kb.py"

# Created KB ids + the temp fixture root -> cleaned on EXIT.
KB_IDS=""
FIXDIR=""
cleanup() {
  local id fid
  for id in $KB_IDS; do
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
' "$id" 2>/dev/null); do
      curl -sf -X DELETE "$G/api/v1/files/${fid}" "${ADM[@]}" >/dev/null 2>&1 \
        || echo "  cleanup: DELETE file ${fid} failed" >&2
    done
    curl -sf -X DELETE "$G/api/v1/knowledge/${id}/delete" "${ADM[@]}" >/dev/null 2>&1 \
      || echo "  cleanup: DELETE kb ${id} failed" >&2
  done
  [ -n "$FIXDIR" ] && rm -rf "$FIXDIR"
}
trap cleanup EXIT

# --- build a throwaway projects root: 3 project dirs + a .kb-ignore allowlist ----
FIXDIR="$(mktemp -d)"
mkdir -p "$FIXDIR/allowA/memory" "$FIXDIR/allowB/memory" "$FIXDIR/denyC/memory"
printf '# test18 allowlist: index only allowA + allowB\n*\n!allowA\n!allowB\n' \
  > "$FIXDIR/.kb-ignore"
printf 'test18-allowA-marker-4f2c7 notes for allowA project.\n' \
  > "$FIXDIR/allowA/memory/note.md"
printf 'test18-allowB-marker-8d1e3 notes for allowB project.\n' \
  > "$FIXDIR/allowB/memory/note.md"
printf 'test18-denyC-marker-2b9a5 SECRET notes for denyC project.\n' \
  > "$FIXDIR/denyC/memory/note.md"

# --- index-projects (full scan, no --project; .kb-ignore filters) ----------------
section "index-projects --root <fixture> (full scan, .kb-ignore allowlist)"
idx_json=$($KB index-projects --root "$FIXDIR" --host "$HOSTSEG" --wait 2>/tmp/t18_err)
if [ -z "$idx_json" ]; then
  fail "index-projects produced no output (err: $(cat /tmp/t18_err 2>/dev/null | head -c 200))"
  finish; exit 1
fi
# Parse the result: project names selected + per-project added + the waited state.
read -r pnames paddeds pwpend < <(printf '%s' "$idx_json" | python3 -c '
import sys, json
try:
    d = json.load(sys.stdin)
except Exception:
    print("", "", ""); sys.exit(0)
ps = d.get("projects") or []
names = ",".join(sorted(p.get("kb_name", "").split("--", 1)[-1] for p in ps))
added = sum(p.get("added", 0) for p in ps)
waited = d.get("waited") or []
pend = sum(w.get("pending", 0) + w.get("processing", 0) for w in waited)
print(names or "", added, pend)
' 2>/dev/null || echo "" "" "")
pexpect="allowA,allowB"
if [ "$pnames" = "$pexpect" ]; then
  pass "selected projects: ${pnames} (denyC excluded by .kb-ignore)"
else
  fail "selected projects: '${pnames}' want '${pexpect}'"
fi
if [ "${paddeds:-0}" -ge 2 ]; then
  pass "files added: ${paddeds} (>=2: one per allowlisted project)"
else
  fail "files added: ${paddeds:-0} want >=2"
fi
if [ "${pwpend:-}" = "0" ]; then
  pass "drain terminal: pending+processing=0 (--wait)"
else
  fail "drain not terminal: pending+processing=${pwpend}"
fi
# Capture the created KB ids for cleanup + the denyC-absent check.
KB_IDS=$(printf '%s' "$idx_json" | python3 -c '
import sys, json
d = json.load(sys.stdin)
print(" ".join(p["kb_id"] for p in (d.get("projects") or []) if p.get("kb_id")))
' 2>/dev/null)

# --- GET /knowledge/ (admin): the allowlisted KBs exist, denyC does NOT -----------
section "GET /api/v1/knowledge/ (admin): allowlisted present, denyC absent"
mapfile -t kbvis < <(curl -s "$G/api/v1/knowledge/" "${ADM[@]}" 2>/dev/null \
  | python3 -c '
import sys, json
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(0)
items = d.get("items") if isinstance(d, dict) else d
for k in (items or []):
    print(k.get("name", ""))
' 2>/dev/null)
has_a=0; has_b=0; has_c=0
for n in "${kbvis[@]}"; do
  case "$n" in
    "${HOSTSEG}--allowA") has_a=1 ;;
    "${HOSTSEG}--allowB") has_b=1 ;;
    "${HOSTSEG}--denyC")  has_c=1 ;;
  esac
done
[ "$has_a" = 1 ] && pass "KB ${HOSTSEG}--allowA present" || fail "KB ${HOSTSEG}--allowA missing"
[ "$has_b" = 1 ] && pass "KB ${HOSTSEG}--allowB present" || fail "KB ${HOSTSEG}--allowB missing"
[ "$has_c" = 0 ] && pass "KB ${HOSTSEG}--denyC absent (excluded by .kb-ignore)" \
                 || fail "KB ${HOSTSEG}--denyC present (should have been excluded)"

# --- retrieve-projects on allowA's marker: hits > 0 (the drain landed vectors) ----
section "retrieve-projects: allowA marker retrievable (drain landed vectors)"
verdict=""
for _attempt in 1 2 3; do
  verdict=$(env KB_HOST="$G" KB_API_KEY="$UK" \
    python3 "${KB_ROOT}/skills/claude/scripts/kb.py" retrieve-projects \
    "test18-allowA-marker-4f2c7" --host "$HOSTSEG" --project allowA --k 5 2>/tmp/t18_rerr \
    | python3 -c '
import sys, json
try:
    d = json.load(sys.stdin)
except Exception:
    print("PARSE_ERR"); sys.exit(0)
hits = d.get("hits") or []
if hits:
    print("OK hits=%d" % len(hits))
else:
    print("MISSING")
' 2>/dev/null || echo "PARSE_ERR")
  case "$verdict" in
    OK*|MISMATCH*) break ;;
    MISSING|PARSE_ERR|"") [ "$_attempt" -lt 3 ] && sleep 5 ;;
  esac
done
case "$verdict" in
  OK*) pass "allowA marker -> ${verdict}" ;;
  *) fail "allowA marker -> ${verdict:-no result} (err: $(cat /tmp/t18_rerr 2>/dev/null | head -c 160))" ;;
esac

finish