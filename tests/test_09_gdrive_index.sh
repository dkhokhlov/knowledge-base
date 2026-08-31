#!/usr/bin/env bash
# Comprehensive at-scale e2e body (iso long): run via the iso_env_named("gdrive",
# at_scale=True) fixture, which owns isolate + e2e_provision_at_scale (clean-all
# + image rebuild + preflight + real rclone gdrive corpus + make ci) + teardown.
# This body runs in the clone: first the in-clone suite (unit + live-RO 01/02/03
# against the fresh stack), then the full real-gdrive drain below.
#
# gdrive indexing via api-gateway (stateless, no sidecar). POSTs /index (admin
# key) to reconcile ./gdrive into the OWUI "gdrive" KB.
# The gateway uploads via POST /files/ (process_in_background=True) and does NOT
# link files itself — OWUI's per-upload background task is the sole linker
# (extract via markitdown-ocr -> embed into the KB collection -> link). That
# drain is async, so this test polls GET /status for the REAL drain terminal
# state, then audits failures + runs a deterministic semantic search.
#
# /status reads file.data.status (via GET /files/?content=false): pending =
# in extraction (OCR/GPU), processing = embedding + linking, completed =
# searchable, failed = error. Drain is terminal when pending+processing == 0
# AND completed+failed covers the source.
#
# Failure audit: a "Failed to link file ... to knowledge ..." error means a
# second link insert collided with an existing one (a double-link race) and
# marks a successfully-extracted file as failed. This must not occur (hard
# fail) — the background task is the sole linker. Other failures (empty
# content, OCR timeout) are genuine source-file issues: surfaced as a notice,
# not a hard fail (re-run /index to re-trigger them).
#
# Deterministic semantic search: query the filename stem of the first COMPLETED
# file against the KB collection and require >=1 hit. A completed file has
# vectors, so its own stem (usually present in its content) must retrieve a
# document. 0 hits on a completed file's stem means vectors are not searchable.
#
# /index is idempotent (re-run reports unmodified + re-triggers any failed).
#
# No SKIP: the at-scale fixture always provisions the gdrive KB + syncs the
# corpus (or provision fails first + this body never runs). A missing
# GDRIVE_KB_ID or empty corpus is a hard fail (require_env / completed<source),
# not a silent pass.
set -u
. "$(dirname "$0")/lib.sh"
load_env
require_stack_up

# --- in-clone suite: unit + live-RO (01/02/03) against THIS clone's fresh stack
# Direct pytest -- NOT `make test` (which targets the LIVE stack for RO) -- with
# marker "not iso and not long". Runs the unit tests + the integration tests
# (test_01/02/03, live-RO against the clone stack) and EXCLUDES every iso/long
# test (test_09 itself is iso long -> excluded -> no recursion; test_08/test_12/
# shared excluded too). The .venv was provisioned by `make ci` at the tail of
# e2e_provision_at_scale. Fail on any non-zero exit.
section "in-clone suite (unit + live-RO)"
if ! .venv/bin/python -m pytest -m "not iso and not long" -v; then
  fail "in-clone suite failed (see output above)"
  finish
  exit 1
fi
pass "in-clone suite green"

# --- full real-gdrive drain --------------------------------------------------
O="$(kb_host)"
# Allowlist must match gateway DEFAULT_ALLOW (gateway/app.py). find's default
# Emacs regex treats (a|b) as LITERAL (matches 0 files), so every -iregex call
# below MUST use -regextype posix-extended for the alternation to work.
ALLOW_RE='[.](docx|pdf|pptx|xlsx|txt|md|html|json|log|tex)$'
# Exclude dot-dirs (.tests, .sync-reports) from the source count: gateway.walk_source prunes them from a full walk (gateway/app.py "Prune dot-dirs"), so counting them leaves the drain `accounted < src_count` -> a false timeout. `path` opts into a dot-subtree (test_11); this full walk does not.
src_count=$(find gdrive -type f -not -path '*/.*' -regextype posix-extended -iregex ".*${ALLOW_RE}" 2>/dev/null | wc -l)

require_env OPENWEBUI_ADMIN_API_KEY KB_API_KEY GDRIVE_KB_ID || { finish; exit 1; }
AK="$OPENWEBUI_ADMIN_API_KEY"
UK="$KB_API_KEY"
ADM=(-H "Authorization: Bearer $AK")
RD=(-H "Authorization: Bearer $UK")

# --- POST /index (admin): reconcile ./gdrive into the KB ---------------------
section "POST /index (api-gateway)"
idx_resp=$(curl -sS --max-time 1200 -X POST \
  "$O/index?source=gdrive&kb_id=${GDRIVE_KB_ID}" \
  "${ADM[@]}" -H 'Content-Type: application/json' -d '{}' 2>&1)
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
fi

# --- poll GET /status until the drain reaches a terminal state ---------------
# Real completion = no pending AND no processing AND completed+failed covers the
# source (every uploaded file reached a terminal state). /status reads
# file.data.status via GET /files/. Poll up to GDRIVE_TEST_WAIT (default 2400s:
# cold first-extraction per-figure OCR budget over the full fresh-synced corpus).
# Admin key: /status uses the gateway's held admin key for the file scan
# internally; any valid key authorizes the call.
section "poll GET /status (real drain)"
wait_s="${GDRIVE_TEST_WAIT:-2400}"
deadline=$(( $(date +%s) + wait_s ))
completed=0; pending=0; processing=0; failed=0; status_json=""
while :; do
  status_json=$(curl -sS "$O/status?source=gdrive&kb_id=${GDRIVE_KB_ID}&json=1" "${ADM[@]}" 2>/dev/null || true)
  read -r completed pending processing failed < <(printf '%s' "$status_json" | python3 -c '
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
  sleep 10
done
in_flight=$(( pending + processing ))
accounted=$(( completed + failed ))
if [ "$in_flight" = "0" ] && [ "$accounted" -ge "$src_count" ]; then
  pass "drain terminal: completed=${completed} failed=${failed} pending=${pending} processing=${processing} source=${src_count}"
else
  fail "drain did not terminate after ${wait_s}s: completed=${completed} failed=${failed} pending=${pending} processing=${processing} source=${src_count} (accounted=${accounted})"
  fail "check: docker logs ${OWUI_CONTAINER:-kb-openwebui} + docker logs ${MARKITDOWN_CONTAINER:-kb-markitdown-ocr}"
  finish
  exit 1
fi

# --- payload invariants: each list length == its count; lists sum == counts sum
section "status payload invariants (lists vs counts)"
inv_out=$(printf '%s' "$status_json" | python3 -c '
import sys, json
d = json.load(sys.stdin)
idx = d.get("indexed_files") or []
pend = d.get("pending_files") or []
fl = d.get("failed_files") or []
ic = d.get("indexed_count", 0); p = d.get("pending", 0)
pc = d.get("processing", 0); fc = d.get("failed", 0)
errs = []
if len(idx) != ic: errs.append("len(indexed_files)=%d != indexed_count=%d" % (len(idx), ic))
if len(pend) != p: errs.append("len(pending_files)=%d != pending=%d" % (len(pend), p))
if len(fl) != fc: errs.append("len(failed_files)=%d != failed=%d" % (len(fl), fc))
if len(idx) + len(pend) + len(fl) + pc != ic + p + fc + pc:
    errs.append("list sum + processing=%d != count sum=%d" % (len(idx)+len(pend)+len(fl)+pc, ic+p+fc+pc))
print("OK" if not errs else "FAIL")
print("%d %d %d" % (len(idx), len(pend), len(fl)))
print("\n".join(errs))
' 2>/dev/null || echo "FAIL"$'\n''0 0 0'$'\n''parse error')
inv_verdict=$(printf '%s' "$inv_out" | sed -n 1p)
read -r inv_idx inv_pend inv_fail < <(printf '%s' "$inv_out" | sed -n 2p)
inv_errs=$(printf '%s' "$inv_out" | sed -n '3,$p')
if [ "$inv_verdict" = "OK" ]; then
  pass "indexed_files=${inv_idx} pending_files=${inv_pend} failed_files=${inv_fail} == indexed_count=${completed} pending=${pending} failed=${failed}"
else
  fail "payload invariants violated:"
  printf '%s\n' "$inv_errs"
  finish
  exit 1
fi

# --- failure audit: "Failed to link" = double-link race (must be 0) -----------
section "failure audit"
link_fails=$(printf '%s' "$status_json" | python3 -c '
import sys, json
d = json.load(sys.stdin)
print(sum(1 for f in (d.get("failed_files") or []) if "Failed to link" in (f.get("error") or "")))
' 2>/dev/null || echo 0)
if [ "${link_fails:-0}" -gt 0 ]; then
  fail "${link_fails} file(s) failed with 'Failed to link' (a double-link race — the background task must be the sole linker):"
  printf '%s' "$status_json" | python3 -c '
import sys, json
d = json.load(sys.stdin)
for f in (d.get("failed_files") or [])[:20]:
    if "Failed to link" in (f.get("error") or ""):
        print("    " + str(f.get("filename") or "?") + ": " + str(f.get("error") or "")[:100])
'
  finish
  exit 1
fi
# Surface genuine (non-link) failures as a notice, not a hard fail.
genuine_out=$(printf '%s' "$status_json" | python3 -c '
import sys, json
d = json.load(sys.stdin)
gf = [f for f in (d.get("failed_files") or []) if "Failed to link" not in (f.get("error") or "")]
print(len(gf))
for f in gf[:10]:
    print("    " + str(f.get("filename") or "?") + ": " + str(f.get("error") or "")[:100])
' 2>/dev/null || true)
genuine_n=$(printf '%s' "$genuine_out" | head -1)
if [ "${genuine_n:-0}" -gt 0 ]; then
  pass "no 'Failed to link' errors; ${genuine_n} genuine failure(s) surfaced (re-run /index to re-trigger):"
  printf '%s' "$genuine_out" | tail -n +2
else
  pass "no failures (all ${src_count} files completed)"
fi

# --- completed vs source -----------------------------------------------------
section "completed vs source"
expected_min=$(( src_count - failed ))
if [ "$completed" -ge "$expected_min" ] && [ "$completed" -gt 0 ]; then
  pass "completed=${completed} >= source(${src_count}) - failed(${failed}) = ${expected_min}"
else
  fail "completed=${completed} < source(${src_count}) - failed(${failed}) = ${expected_min} (unexplained gap)"
  fail "re-run: make gdrive-sync; then docker logs ${OWUI_CONTAINER:-kb-openwebui} / ${MARKITDOWN_CONTAINER:-kb-markitdown-ocr}"
  finish
  exit 1
fi

# --- deterministic semantic search (vectors written + searchable) ------------
# Query the filename stem of the first COMPLETED file against the KB collection
# and require >=1 hit. A completed file has vectors, so its own stem (usually
# present in its content) must retrieve a document. 0 hits on a completed
# file's stem means vectors are not searchable (embedding/RAG config issue).
section "semantic search over gdrive KB (deterministic)"
q_stem=$(printf '%s' "$status_json" | python3 -c '
import sys, json, re
d = json.load(sys.stdin)
for f in (d.get("indexed_files") or []):
    if f.get("status") == "completed" and f.get("filename"):
        stem = re.sub(r"[^A-Za-z0-9]+", " ", f["filename"].rsplit(".", 1)[0]).strip()
        if stem:
            print(stem); break
' 2>/dev/null || true)
if [ -z "$q_stem" ]; then
  pass "SKIP semantic search: no completed file with a usable filename stem"
else
  hits=$(curl -s -X POST "$O/api/v1/retrieval/query/collection" "${RD[@]}" -H 'Content-Type: application/json' \
    -d "{\"collection_names\":[\"${GDRIVE_KB_ID}\"],\"query\":$(python3 -c 'import sys,json;print(json.dumps(sys.argv[1]))' "$q_stem"),\"k\":4,\"hybrid\":true}" \
    | python3 -c '
import sys,json
d=json.load(sys.stdin)
docs=[]
if isinstance(d,list): docs=d
elif isinstance(d,dict):
    if "documents" in d: docs=[t for sub in d["documents"] for t in (sub if isinstance(sub,list) else [sub])]
    else: docs=d.get("files") or d.get("results") or d.get("docs") or []
print(len(docs))
' 2>/dev/null || echo 0)
  if [ "${hits:-0}" -gt 0 ]; then
    pass "search q=\"$q_stem\" -> ${hits} hit(s) (vectors searchable)"
  else
    fail "search q=\"$q_stem\" -> 0 hits (completed file has vectors but none searchable — check embedding/RAG config)"
    finish
    exit 1
  fi
fi

finish