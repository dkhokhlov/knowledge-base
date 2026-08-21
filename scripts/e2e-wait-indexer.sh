#!/usr/bin/env bash
# Wait for the gdrive-indexer (oikb) to reach a healthy source-sync state after
# `make gdrive-index-bootstrap` provisions it. Used by `make test-e2e` so
# test_09_gdrive_index runs against a synced KB instead of a first-sync-in-
# progress state.
#
# oikb 0.4.0 /health per-source status (daemon.py):
#   running  - a sync cycle is active (transient; the daemon syncs every
#              GDRIVE_INDEX_INTERVAL, so a one-shot read can land here on an
#              otherwise-healthy indexer)
#   success  - the cycle completed with no errors
#   partial  - the cycle completed with some per-file errors (expected +
#              permanent when the source has duplicate-content files: OWUI
#              dedups by content hash and rejects the duplicate; oikb records
#              it as a per-cycle error)
#   error    - an exception
# success / partial (and "ok", the top-level daemon health string, kept
# defensively) are healthy completion states; "running" is transient; a stuck
# "running" or "error" is a failure.
#
# No fallback / no silent skip of a bad state: exit 0 only when the source
# reaches success/ok/partial (or ./gdrive has no allowlisted files, in which
# case test_09 will SKIP on its own allowlist check). exit 1 if the indexer
# does not reach a completion state within the timeout — the failure is loud,
# with the last observed status + a logs hint.
set -euo pipefail
cd "$(dirname "$0")/.."

CTN="kb-gdrive-indexer"
# Allowlist must match .oikb.yaml include + tests/test_09_gdrive_index.sh.
ALLOW_RE='[.](docx|pdf|pptx|xlsx|txt|md|csv|html|json|log|tex|py|s|c|h|inc|cfg)$'
TIMEOUT="${E2E_INDEXER_WAIT:-900}"   # 15 min default budget for a cold first sync
INTERVAL=10

src=$(find gdrive -type f -regextype posix-extended -iregex ".*${ALLOW_RE}" 2>/dev/null | wc -l)
if [ "${src:-0}" -eq 0 ]; then
  echo "    ./gdrive has no allowlisted files - indexer idle; test_09 will SKIP"
  exit 0
fi

echo "    ./gdrive allowlisted files: ${src} - waiting for indexer source status success/ok/partial (timeout ${TIMEOUT}s)"
i=0
st=""
while :; do
  st=$(docker exec "$CTN" python -c \
    "import urllib.request,json; d=json.load(urllib.request.urlopen('http://localhost:8080/health',timeout=5)); print(d.get('sources',{}).get('/gdrive',{}).get('status','?'))" \
    2>/dev/null || true)
  case "$st" in
    success|ok|partial)
      echo "    gdrive indexer source status=${st} (waited ~$((i*INTERVAL))s)"
      exit 0
      ;;
  esac
  i=$((i+1))
  if [ $((i*INTERVAL)) -ge "$TIMEOUT" ]; then
    printf 'FAIL  gdrive indexer did not reach a completion state in %ss (last=%q) - check: docker logs %s\n' \
      "$TIMEOUT" "${st:-?}" "$CTN" >&2
    exit 1
  fi
  sleep "$INTERVAL"
done