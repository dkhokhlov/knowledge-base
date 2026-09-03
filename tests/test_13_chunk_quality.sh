#!/usr/bin/env bash
# System integration test: chunk-QUALITY audit over synthetic fixtures for
# every allowlisted file type (docx, pdf, pptx, xlsx, txt, md, html, json,
# log, tex). The automated counterpart of the manual patch-5 audit: per-chunk
# sliceability (base[si:si+len]==chunk), span/page correctness, coalescing,
# distinct offsets, and content fidelity.
#
# Fixtures: COMMITTED (tracked in git) under root/.tests/chunkq/, produced
# by tests/fixtures_chunkq_gen.py (rerun it with --out root/.tests/chunkq
# to regenerate). The test re-derives only the manifest oracle via
# --manifest-only; it writes no file. Every section carries a
# unique marker (chunkq-<type>-s<N>), so the audit can find its chunks
# without depending on embedding RANKING: ONE whole-KB retrieval query with
# k=2000 returns the entire collection (hybrid:true in the body is a no-op
# when ENABLE_RAG_HYBRID_SEARCH=false in the config, and dense ranking over
# near-identical filler bodies is noise -- never a chunking oracle).
#
# Pipeline mirrors test_11: index root/.tests/ (a dot-dir the generic walk
# skips) into a throwaway temp KB via POST /index?dir=.tests, poll the
# real async drain via GET /status, then audit. The committed fixture files
# (fixture-*, chunkq-*) and the Google-native trio (google_native.{docx,xlsx,
# pptx}, when committed) ride along and get the universal checks too.
#
# OCR gate: OCR_ENABLED=true (default) -> all 10 types audited. Off -> only
# the text types (txt,md,json,log,tex) are audited (the binary types need the
# markitdown-ocr sidecar; OWUI default loaders have different shapes for
# html/docx/pdf/pptx/xlsx); the committed binary chunkq fixtures still ride
# along through the default loaders, and their failures surface as notices
# (not hard fails) -- the iso env runs OCR=true, so all 10 types are covered
# where it counts.
set -u
. "$(dirname "$0")/lib.sh"
load_env
require_stack_up

O="$(kb_host)"
ALLOW_RE='[.](docx|pdf|pptx|xlsx|txt|md|html|json|log|tex)$'
GEN="tests/fixtures_chunkq_gen.py"
OUTDIR="root/.tests/chunkq"
# The e2e-iso wrapper forwards only GDRIVE_TEST_WAIT (2400s) to the inner
# `make test`; fall back to it so the cold-stack budget reaches this test.
CHUNKQ_WAIT="${CHUNKQ_WAIT:-${GDRIVE_TEST_WAIT:-300}}"

# --- OCR gate ---------------------------------------------------------------
if [ "${OCR_ENABLED:-true}" = "true" ]; then
  TYPES="md,html,docx,pdf,pptx,xlsx,txt,json,log,tex"
  OCR_ON=1
else
  TYPES="txt,md,json,log,tex"
  OCR_ON=0
fi

# --- Google-native gate -----------------------------------------------------
# The trio is a codex handoff (synthetic Google-authored Docs/Sheets/Slides
# exported to Office formats). 0/3 -> smoke section SKIPs (not committed
# yet); 1-2/3 -> broken commit, hard fail; 3/3 -> smoke audit.
gn_count=0
for f in google_native.docx google_native.xlsx google_native.pptx; do
  [ -f "root/.tests/$f" ] && gn_count=$((gn_count + 1))
done
GOOGLE_ON=0
if [ "$gn_count" -eq 3 ]; then
  GOOGLE_ON=1
elif [ "$gn_count" -gt 0 ]; then
  section "google-native gate"
  fail "google-native fixture set incomplete: ${gn_count}/3 present (commit all three or none)"
  finish
  exit 1
fi

require_env OPENWEBUI_ADMIN_API_KEY KB_API_KEY || { finish; exit 1; }
AK="$OPENWEBUI_ADMIN_API_KEY"
UK="$KB_API_KEY"
ADM=(-H "Authorization: Bearer $AK")
RD=(-H "Authorization: Bearer $UK")
KB_ID=""
MANIFEST="$(mktemp)"

# --- cleanup: temp KB + files, manifest ----------------------------------------
cleanup() {
  local fid
  if [ -n "$KB_ID" ]; then
    # Enumerate files tagged with this KB (covers uploaded-but-unlinked
    # orphans; same filter as gateway list_file_status). One page (50/page)
    # covers a small temp KB.
    for fid in $(curl -s "$O/api/v1/files/?content=false&page=1" "${ADM[@]}" 2>/dev/null \
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
      curl -sf -X DELETE "$O/api/v1/files/${fid}" "${ADM[@]}" >/dev/null 2>&1 \
        || echo "  cleanup: DELETE file ${fid} failed" >&2
    done
    curl -sf -X DELETE "$O/api/v1/knowledge/${KB_ID}/delete" "${ADM[@]}" >/dev/null 2>&1 \
      || echo "  cleanup: DELETE kb ${KB_ID} failed" >&2
  fi
  rm -f "$MANIFEST"
}
trap cleanup EXIT

# --- committed fixture set + manifest oracle ---------------------------------
section "chunk-quality fixture set ($(echo "$TYPES" | tr ',' ' '))"
if ! python3 "$GEN" --manifest-only --types "$TYPES" > "$MANIFEST"; then
  fail "manifest oracle failed (tests/fixtures_chunkq_gen.py --manifest-only)"
  finish
  exit 1
fi
miss_out=$(python3 -c 'import sys, json, os
man = json.load(open(sys.argv[1]))["files"]
missing = [e["file"] for e in man.values()
           if not os.path.isfile(os.path.join(sys.argv[2], e["file"]))]
print(len(missing))
for m in missing:
    print(m)' "$MANIFEST" "$OUTDIR")
if [ "$(printf '%s' "$miss_out" | head -1)" != "0" ]; then
  fail "committed chunkq fixture(s) missing: $(printf '%s\n' "$miss_out" | tail -n +2 | tr '\n' ' ')"
  finish
  exit 1
fi
if [ ! -s "$MANIFEST" ] || [ "$(python3 -c 'import sys,json;print(len(json.load(open(sys.argv[1]))["files"]))' "$MANIFEST" 2>/dev/null || echo 0)" -eq 0 ]; then
  fail "manifest oracle is empty"
  finish
  exit 1
fi
pass "committed fixture set present: $(python3 -c 'import sys,json;print(",".join(sorted(json.load(open(sys.argv[1]))["files"])))' "$MANIFEST")"

# --- source count (all allowlisted files under .tests) -----------------------
# Exclude .meta/.meta.json sidecars: the gateway's _entry_for skips them by
# name (app.py), so they are never indexed. src_count must match what the drain
# can account, not what the allowlist regex alone matches (.meta.json ends in
# .json, so the regex alone would over-count sidecars the gateway drops).
src_count=$(find root/.tests -type f -regextype posix-extended -iregex ".*${ALLOW_RE}" \
  ! -name '*.meta' ! -name '*.meta.json' 2>/dev/null | wc -l)

# --- create temp KB + grant '*' read ------------------------------------------
section "create temp chunk-quality KB"
KB_ID=$(curl -s -X POST "$O/api/v1/knowledge/create" "${ADM[@]}" -H 'Content-Type: application/json' \
  -d '{"name":"chunkq-quality-test","description":"integration test: chunk-quality fixture audit"}' \
  | python3 -c 'import sys,json;print(json.load(sys.stdin).get("id",""))' 2>/dev/null)
[ -n "$KB_ID" ] && pass "KB id: $KB_ID" || { fail "KB create failed"; finish; exit 1; }

grant=$(curl -s -X POST "$O/api/v1/knowledge/${KB_ID}/access/update" "${ADM[@]}" -H 'Content-Type: application/json' \
  -d "{\"access_grants\":[{\"resource_type\":\"knowledge\",\"resource_id\":\"${KB_ID}\",\"principal_type\":\"user\",\"principal_id\":\"*\",\"permission\":\"read\"}]}")
if printf '%s' "$grant" | python3 -c 'import sys,json;d=json.load(sys.stdin);gs=d.get("access_grants") or [];sys.exit(0 if any(g.get("principal_id")=="*" and g.get("permission")=="read" for g in gs) else 1)' 2>/dev/null; then
  pass "granted '*' read on temp KB"
else
  fail "grant '*' read failed: $(printf '%s' "$grant" | head -c 160)"; finish; exit 1
fi

# --- POST /index (admin): reconcile root/.tests into the temp KB (dir=.tests) -
section "POST /index (api-gateway, dir=.tests)"
idx_resp=$(curl -sS --max-time 1200 -X POST \
  "$O/index?dir=.tests&kb_id=${KB_ID}" \
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
section "poll GET /status (real drain, dir=.tests)"
deadline=$(( $(date +%s) + CHUNKQ_WAIT ))
completed=0; pending=0; processing=0; failed=0; status_json=""
while :; do
  status_json=$(curl -sS "$O/status?dir=.tests&kb_id=${KB_ID}&json=1" "${ADM[@]}" 2>/dev/null || true)
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
  sleep 5
done
in_flight=$(( pending + processing ))
accounted=$(( completed + failed ))
if [ "$in_flight" = "0" ] && [ "$accounted" -ge "$src_count" ]; then
  pass "drain terminal: completed=${completed} failed=${failed} pending=${pending} processing=${processing} source=${src_count}"
else
  fail "drain did not terminate after ${CHUNKQ_WAIT}s: completed=${completed} failed=${failed} pending=${pending} processing=${processing} source=${src_count} (accounted=${accounted})"
  fail "check: docker logs ${OWUI_CONTAINER:-kb-openwebui} + docker logs ${MARKITDOWN_CONTAINER:-kb-markitdown-ocr}"
  finish
  exit 1
fi

# --- failure scoping ----------------------------------------------------------
# "Failed to link" = double-link race (hard fail, same as test_11). FAILED
# text-type chunkq fixtures are hard fails (they extract everywhere). Binary
# chunkq types + google_native.* are hard fails only with OCR on (they need
# the markitdown-ocr sidecar); with OCR off they are notices. Committed
# fixture-* failures are notices (test_11 semantics).
section "failure audit + scoping"
scope_out=$(printf '%s' "$status_json" | python3 -c '
import sys, json
d = json.load(sys.stdin)
failed = d.get("failed_files") or []
link_fails = sum(1 for f in failed if "Failed to link" in (f.get("error") or ""))
print("LINKFAILS", link_fails)
for f in failed:
    name = str(f.get("filename") or "?").split("/")[-1]
    print("FAILED", name, str(f.get("error") or "")[:100])
' 2>/dev/null)
link_fails=$(printf '%s\n' "$scope_out" | awk '$1=="LINKFAILS"{print $2}')
chunkq_failed=0; google_failed=0; other_failed=0
while read -r tag name err; do
  [ "$tag" = "FAILED" ] || continue
  case "$name" in
    chunkq-*.txt|chunkq-*.md|chunkq-*.json|chunkq-*.log|chunkq-*.tex)
      chunkq_failed=$((chunkq_failed + 1)); fail "chunkq fixture FAILED extraction: ${name}: ${err}" ;;
    chunkq*)
      if [ "$OCR_ON" = "1" ]; then
        chunkq_failed=$((chunkq_failed + 1)); fail "chunkq fixture FAILED extraction: ${name}: ${err}"
      else
        printf '  NOTICE  %s failed (OCR off; expected for binary types): %s\n' "$name" "$err"
      fi ;;
    google_native*)
      if [ "$OCR_ON" = "1" ]; then
        google_failed=$((google_failed + 1)); fail "google-native fixture FAILED extraction: ${name}: ${err}"
      else
        printf '  NOTICE  %s failed (OCR off; expected for Office files): %s\n' "$name" "$err"
      fi ;;
    *)        other_failed=$((other_failed + 1)); printf '  NOTICE  committed fixture failed (see test_11 semantics): %s: %s\n' "$name" "$err" ;;
  esac
done < <(printf '%s\n' "$scope_out" | grep '^FAILED' || true)
if [ "${link_fails:-0}" -gt 0 ]; then
  fail "${link_fails} file(s) failed with 'Failed to link' (a double-link race -- the background task must be the sole linker)"
else
  pass "no 'Failed to link' errors"
fi
if [ "$other_failed" -gt 0 ]; then
  pass "committed-fixture failures treated as notices (${other_failed}; test_11 semantics)"
fi
if [ "$chunkq_failed" -gt 0 ] || { [ "$google_failed" -gt 0 ] && [ "$OCR_ON" = "1" ]; }; then
  finish
  exit 1
fi
[ "$failed" -eq 0 ] && pass "no failures at all (all ${src_count} files completed)"

# --- completed vs source -----------------------------------------------------
if [ "$completed" -lt 1 ]; then
  fail "no files completed at all"
  finish
  exit 1
fi

# --- chunk-quality audit ------------------------------------------------------
# One python3 heredoc; emits PASS/INFO/FAIL lines; exit code = number of FAILs.
section "chunk-quality audit"
mapfile -t audit_lines < <(python3 - "$O" "$AK" "$UK" "$KB_ID" "$MANIFEST" "$OCR_ON" "$GOOGLE_ON" "$OUTDIR" <<'PY'
import json
import sys
import time
import urllib.request
from pathlib import Path

host, ak, uk, kb_id, manifest_path, ocr_on, google_on, outdir = sys.argv[1:9]
ocr_on = int(ocr_on)
google_on = int(google_on)
fails = 0


def emit(kind, msg):
    print("%s %s" % (kind, msg))


def ok(msg):
    emit("PASS", msg)


def info(msg):
    emit("INFO", msg)


def bad(msg):
    global fails
    fails += 1
    emit("FAIL", msg)


def req(method, path, key, body=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(
        host + path, data=data, method=method,
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(r, timeout=120) as resp:
        return resp.status, resp.read().decode()


man = json.loads(Path(manifest_path).read_text())["files"]

# --- 1. ONE whole-KB query, k > any collection size. Dense ranking over the
# near-identical filler bodies is noise and NOT a chunking oracle; the full
# collection + local marker matching is deterministic. Bounded retry for
# cold-collection warm-up only.
hits = []
last_err = ""
for attempt in range(3):
    try:
        code, txt = req("POST", "/api/v1/retrieval/query/collection", uk,
                        {"collection_names": [kb_id], "query": "chunkq", "k": 2000})
        d = json.loads(txt)
        docs = d.get("documents") or []
        metas = d.get("metadatas") or []
        for sub_d, sub_m in zip(docs, metas):
            for t, m in zip(sub_d or [], sub_m or []):
                if t:
                    hits.append({"doc": t, "meta": m or {}})
        if hits:
            break
    except Exception as e:  # noqa: BLE001 - report + retry
        last_err = str(e)
    if attempt < 2:
        time.sleep(5)
if not hits:
    bad("whole-KB retrieval returned 0 chunks after 3 attempts (%s)" % last_err)
    print("AUDIT_RC %d" % fails)
    sys.exit(0)
ok("whole-KB query: %d chunk(s) returned (k=2000)" % len(hits))

# --- 2. file content (base text) per file_id, cached (admin key)
content_cache = {}


def file_content(fid):
    if fid not in content_cache:
        try:
            code, txt = req("GET", "/api/v1/files/%s/data/content" % fid, ak)
            content_cache[fid] = (json.loads(txt) or {}).get("content") or ""
        except Exception as e:  # noqa: BLE001
            content_cache[fid] = None
            bad("GET /files/%s/data/content failed: %s" % (fid, e))
    return content_cache[fid]


# --- 3. universal invariants over EVERY chunk in the KB
by_file = {}
for h in hits:
    by_file.setdefault(h["meta"].get("file_id"), []).append(h)
for fid, chunks in by_file.items():
    content = file_content(fid)
    if content is None:
        continue
    sis = [c["meta"].get("start_index") for c in chunks]
    if any(s is None for s in sis):
        bad("file %s: a chunk has no start_index metadata" % fid)
        continue
    if min(sis) != 0:
        bad("file %s: min start_index is %d, expected 0" % (fid, min(sis)))
    if len(set(sis)) != len(sis):
        bad("file %s: duplicate start_index values (%d chunks, %d distinct)" % (fid, len(sis), len(set(sis))))
    n_slice = 0
    for c in chunks:
        si = c["meta"]["start_index"]
        if content[si : si + len(c["doc"])] != c["doc"]:
            bad("file %s: chunk at start_index %d is NOT sliceable (base[si:si+len]!=chunk)" % (fid, si))
        else:
            n_slice += 1
    ok("file %s: %d/%d chunks exactly sliceable" % (fid, n_slice, len(chunks)))


def chunks_with(marker):
    return [h for h in hits if marker in h["doc"]]


# --- 4. per-manifest section audit
disk_cache = {}


def disk_text(relname):
    if relname not in disk_cache:
        disk_cache[relname] = Path(outdir, relname).read_bytes().decode("ascii")
    return disk_cache[relname]


HEADER_MODES = ("headers",)
PAGE_MODES = ("pages",)
for type_key, entry in sorted(man.items()):
    fname = entry["file"]
    mode = entry["mode"]
    for sec in entry["sections"]:
        marker = sec["marker"]
        found = chunks_with(marker)
        if not found:
            bad("%s: marker %s not retrievable (0 chunks contain it)" % (fname, marker))
            continue
        if mode == "single" and type_key == "txt":
            # splitter-driven chunks: a marker can legitimately appear in
            # overlap chunks; assert presence only
            pass
        elif len(found) != 1:
            bad("%s: marker %s in %d chunks, expected exactly 1" % (fname, marker, len(found)))
            continue
        if "coalesce_with" in sec:
            partner = chunks_with(sec["coalesce_with"])
            if len(partner) != 1 or len(found) != 1:
                bad("%s: coalesce pair %s/%s did not land in single chunks (%d/%d)"
                    % (fname, sec["coalesce_with"], marker, len(partner), len(found)))
            else:
                a, b = partner[0], found[0]
                if (a["meta"].get("file_id"), a["meta"].get("start_index")) != \
                   (b["meta"].get("file_id"), b["meta"].get("start_index")):
                    bad("%s: coalesce pair %s + %s landed in DIFFERENT chunks (start_index %s vs %s)"
                        % (fname, sec["coalesce_with"], marker,
                           a["meta"].get("start_index"), b["meta"].get("start_index")))
                else:
                    ok("%s: coalesce pair %s + %s -> ONE chunk (start_index %s)"
                       % (fname, sec["coalesce_with"], marker, a["meta"].get("start_index")))
            continue
        h = found[0]
        meta = h["meta"]
        fid = meta.get("file_id")
        content = file_content(fid)
        if content is None:
            continue
        si = meta.get("start_index")
        # txt: splitter-driven chunks (2..n mid-span) carry no boundary or
        # count guarantee -- universal invariants already cover sliceability.
        strict = not (type_key == "txt")
        if strict:
            if mode in PAGE_MODES:
                want_page = sec.get("page")
                if meta.get("page") != want_page:
                    bad("%s: marker %s chunk has page=%s, expected %s"
                        % (fname, marker, meta.get("page"), want_page))
                # unit first chunk: preceded by the single-space page joiner
                # or a newline (unit 1 of its file starts at 0)
                if si != 0 and content[si - 1] not in (" ", "\n"):
                    bad("%s: marker %s chunk at start_index %s not at a unit boundary (preceded by %r)"
                        % (fname, marker, si, content[si - 1]))
            else:
                if meta.get("page") is not None:
                    bad("%s: marker %s chunk has page=%s, expected none (single-doc type)"
                        % (fname, marker, meta.get("page")))
                if si != 0 and content[si - 1] != "\n":
                    bad("%s: marker %s chunk at start_index %s not at a line start (preceded by %r)"
                        % (fname, marker, si, content[si - 1]))
            # header structure for ATX-header types (md/html/docx)
            if mode in HEADER_MODES:
                first_line = h["doc"].lstrip().split("\n", 1)[0]
                if not first_line.startswith("#") or marker not in first_line:
                    bad("%s: marker %s chunk first line is not an ATX header holding the marker: %r"
                        % (fname, marker, first_line[:60]))
        ok("%s: %s ok (start_index %s, page %s)" % (fname, marker, si, meta.get("page")))

# --- 5. fidelity
for type_key, entry in sorted(man.items()):
    fname = entry["file"]
    for sec in entry["sections"]:
        found = chunks_with(sec["marker"])
        if not found:
            continue
        fid = found[0]["meta"].get("file_id")
        content = file_content(fid)
        if content is None:
            continue
        if sec["marker"] not in content:
            bad("%s: marker %s in chunks but NOT in file content" % (fname, sec["marker"]))
# text types are exact UTF-8 decodes at extraction: content == disk bytes
# (sanitize is identity for pure-ASCII). Gate on OCR-on (conservative).
if ocr_on:
    for type_key in ("md", "txt", "json", "log", "tex"):
        if type_key not in man:
            continue
        fname = man[type_key]["file"]
        fid = None
        for sec in man[type_key]["sections"]:
            found = chunks_with(sec["marker"])
            if found:
                fid = found[0]["meta"].get("file_id")
                break
        content = file_content(fid) if fid else None
        if content is None:
            bad("%s: no chunk found for byte-equality check" % fname)
            continue
        disk = disk_text(fname)
        if content == disk:
            ok("%s: content byte-identical to disk (%d chars)" % (fname, len(disk)))
        else:
            bad("%s: content != disk text (stored %d chars, disk %d chars)"
                % (fname, len(content), len(disk)))

# --- 6. google-native smoke (no structure asserts; sliceability already
# covered by the universal invariants)
if google_on:
    google_markers = [
        "chunkq-google-doc",
        "chunkq-google-sheet-A", "chunkq-google-sheet-B", "chunkq-google-sheet-C",
        "chunkq-google-slides-s1", "chunkq-google-slides-s2",
        "chunkq-google-slides-s3", "chunkq-google-slides-s4",
    ]
    n_found = 0
    for m in google_markers:
        if chunks_with(m):
            n_found += 1
        else:
            bad("google-native: marker %s not retrievable" % m)
    if n_found:
        ok("google-native smoke: %d/%d markers retrievable" % (n_found, len(google_markers)))
else:
    info("google-native trio not committed; smoke section skipped")

print("AUDIT_RC %d" % fails)
PY
)
audit_rc=$(printf '%s\n' "${audit_lines[@]}" | awk '$1=="AUDIT_RC"{print $2}')
# No AUDIT_RC line = the audit python itself crashed; treat as failure.
audit_rc="${audit_rc:-1}"
for ln in "${audit_lines[@]}"; do
  case "$ln" in
    PASS*) pass "${ln#PASS }" ;;
    INFO*) printf '  INFO  %s\n' "${ln#INFO }" ;;
    FAIL*) fail "${ln#FAIL }" ;;
  esac
done
if [ "${audit_rc:-0}" != "0" ]; then
  fail "chunk-quality audit: ${audit_rc} check(s) failed"
  finish
  exit 1
fi
pass "chunk-quality audit clean"

finish