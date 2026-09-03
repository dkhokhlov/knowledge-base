#!/usr/bin/env bash
# KB index: POST /index one (or every) ./root/<name>/ tree into its OWUI KB via
# api-gateway. INDEX-ONLY: the operator delivers the tree under ./root/<name>/
# (gdrive via `make kb-sync`; other dirs via external rsync / copy), THEN this
# reconciles it into the KB named <name>. Sync and index are separate so each is
# independently retryable.
#
# Each top-level non-dot subdir of ./root/ is one KB (named after the subdir).
# The gateway takes `dir` = the single top-dir name (no slash, no wildcard, no
# subpath scoping); glob expansion (KB='xgen-*') lives in the Makefile, which
# calls this script once per matched dir.
#
# The gateway is stateless and takes kb_id, so each KB is resolved BY NAME here
# (paginated, unique-or-fail via kb-bootstrap.sh --resolve). A KB that does not
# resolve fails fast with a "run make kb-bootstrap" hint (no silent create here --
# bootstrap is a separate, explicit step).
#
# Per-KB client-side in-flight guard: refuse to POST /index if a drain is already
# in flight for that KB (pending+processing > 0); --retry-pending is EXEMPT (it
# intentionally re-triggers pending files). Strict + fail-closed: a /status error
# (curl fail / bad JSON / missing pending or processing) is treated as in-flight
# -> refuse (never dispatch blind). Caveats: bypassable by raw curl; has a
# check-then-dispatch TOCTOU; the /status scan caps at 10k files.
#
# Usage:
#   make kb-index                        reconcile EVERY top-level non-dot ./root/ subdir
#   make kb-index KB=<name>              reconcile one KB
#   make kb-index KB='xgen-*'            reconcile every matching subdir (Makefile glob)
#   make kb-index KB=<name> INDEX_ALL=1  full re-index of that KB
#   make kb-index RETRY_PENDING=1        also re-trigger stalled PENDING files
#
# Preconditions:
#   - Stack running + healthy (`make start`).
#   - OPENWEBUI_ADMIN_API_KEY in .env.local (provisioned by `make api-keys`).
#   - KB_HOST set (shell-sourced; see .env.template).
#   - Each target KB already bootstrapped (`make kb-bootstrap KB=<name>`).
set -euo pipefail
cd "$(dirname "$0")/.."

set -a
# shellcheck source=/dev/null
. ./.env
# shellcheck source=/dev/null
. ./.env.local 2>/dev/null || true
set +a

: "${KB_HOST:?FAIL  KB_HOST not set -- export KB_HOST=http://<host>:<port> (see .env.template)}"
: "${OPENWEBUI_ADMIN_API_KEY:?FAIL  OPENWEBUI_ADMIN_API_KEY not set in .env.local (run: make api-keys)}"

KB=""; INDEX_ALL=0; RETRY_PENDING=0
for a in "$@"; do
  case "$a" in
    --kb) shift_next=1 ;;
    --index-all) INDEX_ALL=1 ;;
    --retry-pending) RETRY_PENDING=1 ;;
    -h|--help) grep -E '^#( |$)' "$0" | sed 's/^# //; s/^#$//'; exit 0 ;;
    *) if [ "${shift_next:-0}" = "1" ]; then KB="$a"; shift_next=0; \
       else echo "FAIL  unknown arg: $a" >&2; exit 2; fi ;;
  esac
done

O="${KB_HOST%/}"
adm=(-H "Authorization: Bearer ${OPENWEBUI_ADMIN_API_KEY}")

# Collect the KB name set: one (--kb) or every top-level non-dot subdir of ./root.
if [ -n "$KB" ]; then
  kbs=("$KB")
else
  mapfile -t kbs < <(find root -maxdepth 1 -mindepth 1 -type d ! -name '.*' -printf '%f\n' 2>/dev/null | sort)
  if [ "${#kbs[@]}" -eq 0 ]; then
    echo "FAIL  no top-level subdirs under ./root (drop a folder at ./root/<name>/, then: make kb-bootstrap KB=<name>)" >&2
    exit 1
  fi
fi

# in_flight <kb_id> <name>: print pending+processing, or "ERR" on any failure
# (fail-closed: the caller treats ERR as in-flight -> refuse to dispatch).
in_flight() {
  curl -sS --max-time 1200 "${O}/status?kb_id=${1}&dir=${2}&json=1" "${adm[@]}" 2>/dev/null \
    | python3 -c '
import sys, json
try:
    d = json.load(sys.stdin)
    p, q = d["pending"], d["processing"]   # KeyError if absent -> fail-closed
    if not (isinstance(p, int) and isinstance(q, int) and p >= 0 and q >= 0):
        raise ValueError
    print(p + q)
except Exception:
    print("ERR")
'
}

fail=0
for name in "${kbs[@]}"; do
  echo "==> KB ${name}"
  if ! kid=$(KB="$name" ./scripts/kb-bootstrap.sh --resolve 2>/dev/null); then
    echo "  FAIL  could not resolve KB '${name}' by name (0 or >1 match). Run: make kb-bootstrap KB=${name}" >&2
    fail=1; continue
  fi
  # in-flight guard (exempt --retry-pending)
  if [ "$RETRY_PENDING" = "0" ]; then
    inf=$(in_flight "$kid" "$name")
    if [ "$inf" != "0" ] 2>/dev/null; then
      echo "  FAIL  a drain is in flight for KB ${name} (pending+processing=${inf}); refusing to re-dispatch. Wait: make kb-status KB=${name} ; make kb-index-finalize KB=${name}. Or retry stalled: make kb-index KB=${name} RETRY_PENDING=1" >&2
      fail=1; continue
    fi
  fi
  q="kb_id=${kid}&dir=${name}"
  [ "$INDEX_ALL" = "1" ] && q="${q}&reindex_all=1"
  [ "$RETRY_PENDING" = "1" ] && q="${q}&retry_pending=1"
  mode=incremental; [ "$INDEX_ALL" = "1" ] && mode="full re-index"
  echo "  POST /index (${mode})..."
  if ! resp=$(curl -sS --max-time 1200 -X POST "${O}/index?${q}" \
        -H "Authorization: Bearer ${OPENWEBUI_ADMIN_API_KEY}" \
        -H "Content-Type: application/json" -d '{}' 2>&1); then
    echo "  FAIL  /index call failed (curl error): ${resp}" >&2
    fail=1; continue
  fi
  if ! printf '%s' "$resp" | jq -e 'has("ok")' >/dev/null 2>&1; then
    echo "  FAIL  /index returned a non-JSON/error response: ${resp}" >&2
    fail=1; continue
  fi
  added=$(printf '%s' "$resp" | jq -r '.added')
  modified=$(printf '%s' "$resp" | jq -r '.modified')
  deleted=$(printf '%s' "$resp" | jq -r '.deleted')
  unmodified=$(printf '%s' "$resp" | jq -r '.unmodified')
  retried=$(printf '%s' "$resp" | jq -r '.retried // 0')
  ok=$(printf '%s' "$resp" | jq -r '.ok')
  errn=$(printf '%s' "$resp" | jq -r '(.errors // []) | length')
  echo "  added=${added} modified=${modified} deleted=${deleted} unmodified=${unmodified} retried=${retried} errors=${errn}"
  if [ "$errn" -gt 0 ] 2>/dev/null; then
    printf '  per-file errors:\n' >&2
    printf '%s' "$resp" | jq -r '.errors[] | "    \(.filename // .file_id // "?"): \(.status) — \(.error // "")"' >&2
  fi
  if [ "$ok" != "true" ]; then
    echo "  FAIL  /index completed with errors (ok=false) — see per-file errors above" >&2
    fail=1; continue
  fi
  echo "  OK"
done

if [ "$fail" = "1" ]; then
  echo "FAIL  one or more KBs failed (see above)" >&2
  exit 1
fi
echo "DONE  kb-index: reconciled ${#kbs[@]} KB(s)"