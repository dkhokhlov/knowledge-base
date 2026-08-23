#!/usr/bin/env bash
# System integration test: markitdown-ocr /process auth gate.
#
# Asserts OCR_SERVICE_TOKEN is wired end to end:
#   - the kb-markitdown-ocr container holds a non-empty token (a recreate that
#     bypassed .env.local would leave it empty -> _token=None -> no auth; this
#     catches that stale-recreate regression), AND
#   - /process rejects a request without a Bearer (401), AND
#   - /process accepts the matching Bearer (200).
#
# The token is read from the container's own env at runtime; it is never
# printed or passed on the command line.
#
# Tolerant: SKIPs (passes with a notice) when markitdown-ocr is not provisioned
# (MARKITDOWN_OCR_PROVISIONED!=1 in .env.local) so `make test` runs clean in a
# bare environment.
set -u
. "$(dirname "$0")/lib.sh"
load_env
require_stack_up

# --- skip condition ----------------------------------------------------------
if [ "${MARKITDOWN_OCR_PROVISIONED:-0}" != "1" ]; then
  section "ocr auth"
  pass "SKIP: MARKITDOWN_OCR_PROVISIONED!=1 (run: make ocr-bootstrap); test skipped"
  finish
  exit 0
fi

section "kb-markitdown-ocr holds the token (stale-recreate guard)"
# A recreate that bypassed .env.local leaves OCR_SERVICE_TOKEN empty in the
# container env -> oursvc.py _token=None -> /process skips the auth check. This
# catches that regression (the gap this test locks in).
toklen=$(docker inspect kb-markitdown-ocr \
  --format '{{range .Config.Env}}{{println .}}{{end}}' \
  | awk -F= '/^OCR_SERVICE_TOKEN=/{print length($2)}')
if [ "${toklen:-0}" -gt 0 ]; then
  pass "kb-markitdown-ocr OCR_SERVICE_TOKEN is set (len=$toklen)"
else
  fail "kb-markitdown-ocr OCR_SERVICE_TOKEN is EMPTY (stale recreate? rerun: make ocr-bootstrap)"
fi

section "/process auth gate (401 without Bearer, 200 with)"
# /process is internal (no published port; owui_net only). Probe it inside the
# container via python3 + stdlib urllib. The token comes from the container env
# (os.environ), not the host, so it never appears in the command line or output.
out=$(docker exec -i kb-markitdown-ocr python3 <<'PY'
import os, urllib.request, urllib.error
tok = os.environ.get("OCR_SERVICE_TOKEN", "")
def put(auth):
    h = {"Content-Type": "text/plain", "X-Filename": "ocr_authtest.txt"}
    if auth:
        h["Authorization"] = "Bearer " + tok
    req = urllib.request.Request("http://127.0.0.1:8080/process",
        data=b"ocr auth test 123", method="PUT", headers=h)
    try:
        return urllib.request.urlopen(req, timeout=30).status
    except urllib.error.HTTPError as e:
        return e.code
print("NOAUTH=" + str(put(False)))
print("AUTH=" + str(put(True)))
PY
)
noauth=$(printf '%s\n' "$out" | sed -n 's/^NOAUTH=//p')
auth=$(printf '%s\n' "$out" | sed -n 's/^AUTH=//p')
if [ -z "${noauth:-}" ]; then
  fail "/process probe returned no result (kb-markitdown-ocr not running? out=$out)"
else
  [ "$noauth" = "401" ] \
    && pass "/process without Bearer -> 401 (auth enforced)" \
    || fail "/process without Bearer -> ${noauth} (want 401; _token is None -> auth not enforced)"
  [ "$auth" = "200" ] \
    && pass "/process with Bearer -> 200" \
    || fail "/process with Bearer -> ${auth:-?} (want 200)"
fi

finish