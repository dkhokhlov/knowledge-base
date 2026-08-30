# markitdown-OCR external extraction service

The `markitdown-ocr` sidecar is an **external extraction engine** for Open WebUI.
It runs [markitdown-ocr][markitdown-ocr] with the OCR service replaced by an
Ollama-native `/api/chat` client (`markitdown/oursvc.py`), so embedded figures
and image-only pages are OCR'd by `deepseek-ocr` and become searchable KB
members. Off when `OCR_ENABLED=false` (default `true`); compose-profile-gated
(`COMPOSE_PROFILES=ocr` in `.env`, baked by `make bootstrap`); no fallback.

This is the per-figure OCR path for the `gdrive` knowledge base: image-only
PDFs (no text layer) and figure/diagram content in text+figure PDFs become
searchable instead of orphaning. Office files (PPTX/DOCX/XLSX) are converted
natively (no libreoffice), which also solves the PPTX orphan.

## Data flow

`gdrive → api-gateway (POST /index) → Open WebUI → markitdown-ocr →
deepseek-ocr (Ollama) → per-page / per-slide / per-sheet chunks`

api-gateway uploads a file to Open WebUI (driving OWUI's sync protocol); Open
WebUI calls the external engine (`PUT /process`); markitdown-ocr runs the OCR
converters with `OllamaNativeOCRService`, splits the result into per-unit
documents, and returns a JSON list. OWUI turns each into a Document, adds
`file_id` / `source`, and chunks it. The existing `pathdedup` + idem dedup
still run (the engine replaces only the loader, not the rest of
`process_file`).

## Why native `/api/chat` (not the `/v1` shim)

`deepseek-ocr` needs its Gundam image preprocessing (a 1024×1024 global view
plus dynamic 640×640 local tiles). Ollama's native `/api/chat` runner applies
this preprocessing automatically. The OpenAI-compatible `/v1` shim does NOT —
through `/v1`, a large image or a full-page render collapses into a repetition
loop (the model emits one token thousands of times until the token cap).

`OllamaNativeOCRService` is a drop-in for markitdown-ocr's
`LLMVisionOCRService` (same `extract_text -> OCRResult` contract). The four
converter classes are registered with `MarkItDown(enable_plugins=False)` at
priority `-1.0`. **No markitdown-ocr source is modified; no fork.** Versions are
pinned (`markitdown==0.1.7`, `markitdown-ocr==0.1.0`).

## Per-unit metadata

Each converter emits its own unit marker; the service splits on the marker that
matches the input type and returns a JSON list of `{page_content, metadata}`:

| Type | Marker | Metadata |
|---|---|---|
| PDF | `## Page N` | `{page: N}` |
| PPTX | `<!-- Slide number: N -->` | `{page: N}` |
| XLSX | `## {sheet name}` | `{page: <sheet index>, sheet: <name>}` |
| DOCX / text / csv / json / html | (none) | `{}` — one document |

OWUI `filter_metadata` keeps `page` (+ `sheet`), so a hit carries `file_id` +
`page` → the exact original page / slide / sheet. Round-trip: `kb.py file
<file_id>` saves the file; `pdftoppm -f <page>` renders the page.

> **PPTX upstream bug:** the PPTX converter emits **literal `\n`** (two chars),
> not real newlines, around the slide marker and all native text. The service
> normalizes `\\n` → `\n` on PPTX output before splitting — without this, the
> per-slide split matches nothing and the slide text is glued.

## Standalone images

markitdown-ocr OCRs only images **embedded** in PDF/DOCX/PPTX/XLSX; there is no
standalone-image converter. A standalone `.png`/`.jpg` routed to the engine
would hit markitdown's plain `ImageConverter` (exif + optional LLM caption) and
return near-empty content → orphan. The service instead feeds standalone image
bytes straight to `OllamaNativeOCRService` and returns one document. (The gdrive
set excludes standalone images via the gateway `DEFAULT_ALLOW` allowlist, so
this branch matters for direct uploads, not the synced set.)

## Provisioning (config flag, decided at provision time)

OCR is gated by `OCR_ENABLED` in `.env` (default `true`). It is a config flag,
not a runtime toggle: `make provision` bakes `COMPOSE_PROFILES=ocr` into `.env`
from `OCR_ENABLED`, and `docker compose` reads that for every command, so the
sidecar is governed by `.env` (no `--profile` flag, no per-run override). To
disable: `make clean-all && make provision OCR_ENABLED=false` (bakes an empty
`COMPOSE_PROFILES`; `preflight` asserts the two agree). When enabled, OCR is a
prereq provisioned by the standard chain — no separate step:

1. `make bootstrap` generates `OCR_SERVICE_TOKEN` into `.env.local` (secret);
2. `make pull-models` pulls `deepseek-ocr` (a first-class prereq alongside the
   base LLM + embedder);
3. `make start` builds + starts the `markitdown-ocr` sidecar (via `COMPOSE_PROFILES=ocr` in `.env`);
4. `make api-keys` sets `CONTENT_EXTRACTION_ENGINE=external` +
   `EXTERNAL_DOCUMENT_LOADER_URL=http://markitdown-ocr:8080` + the API key in
   the OWUI DB (`/api/v1/retrieval/config/update`, merge semantics) and
   read-back-asserts each key stuck.

`make preflight` HARD-FAILs when enabled if `deepseek-ocr` is not pulled (a
half-provisioned engine would orphan every ingest), and WARNs on
`rag.content_extraction_engine` drift. `make ocr-config` re-asserts the OWUI DB
keys (re-run after a DB reset). Idempotent. **No fallback:** OWUI's external
engine is global + all-or-nothing — an empty result orphans (no per-type
fallback).

## Scope and limits

- **Global + all-or-nothing.** When the engine is enabled, OWUI routes **every**
  ingest to markitdown-ocr — no per-type fallback; an empty result orphans.
  Covered: PDF/DOCX/PPTX/XLSX (OCR converters), standalone images (direct OCR),
  csv/json/html (markitdown built-in converters). Plain-text / code
  (`.txt`/`.md`/`.py`/`.S`/`.c`/`.h`/`.inc`/`.cfg`/`.log`/`.tex`/`.jsonl`) is
  decoded UTF-8 directly in the service — **not** via markitdown's
  `PlainTextConverter`, which mis-detects `stream_info.charset` as `ascii` and
  raises `UnicodeDecodeError` on UTF-8 text (orphaning ~12 UTF-8 docs in the
  first sync). `.csv`/`.html`/`.json` keep their dedicated converters (they
  handle UTF-8 and structure the output). **Not covered:** audio/video (no
  ffmpeg in v1) → would orphan. Keep those out of the synced set (the gateway
  `DEFAULT_ALLOW` allowlist already excludes audio/video).
- **API key required.** An empty `EXTERNAL_DOCUMENT_LOADER_API_KEY` makes OWUI
  silently skip the external engine and fall back to its default loaders.
  `make bootstrap` generates a non-empty `OCR_SERVICE_TOKEN` (when `OCR_ENABLED=true`).
- **No silent fallback.** If `markitdown-ocr` is down, OWUI extraction fails
  (logged, greppable) and the file orphans — it does not fall back to a default
  loader. This is by design (the operator sees the outage).
- **GPU.** `deepseek-ocr` (~6.7 GB) evicts `qwen2.5:14b` on a single-GPU host
  during OCR. Ollama serializes (`num_parallel=1`); the service's module lock +
  `OCR_KEEP_ALIVE` bound the thrash. OCR is ingest-time only — run the first
  sync off-peak (a one-time bulk OCR of all image-bearing files; later syncs OCR
  only changed/new files via the idem byte-dedup).
- **Reindex caveat.** A reindex rebuilds one document per file from stored
  metadata (no `page` key) → per-unit `page` metadata is lost, `file_id`
  survives. After a reindex, round-trip by `file_id` only.

## Service guards

`OllamaNativeOCRService.extract_text`:

- strips deepseek-ocr's internal `<|...|>` tokens (the `/api/chat` runner does
  not strip them);
- skips images below `OCR_MIN_DIM` (default 64px, min dimension) before the
  Ollama call — drops icons, saves GPU, avoids icon hallucination;
- serializes OCR calls across request threads (module `threading.Lock`) to bound
  GPU eviction thrash;
- never raises — returns `OCRResult(error=…)` on failure (fail-open, mirroring
  upstream). An empty OCR result leaves the surrounding text layer intact.

## Monitoring

- `docker logs -f kb-markitdown-ocr` — per-request log + OCR errors.
- `make preflight` — when enabled, hard-fails if `deepseek-ocr` is not pulled
  and warns if `rag.content_extraction_engine` has drifted from `external`
  (fix: `make ocr-config`).
- `GET /api/v1/retrieval/config` (admin key) — confirm the engine is set.

## Disabling (no runtime toggle; no KB reset)

There is no runtime toggle — `make ocr-disable` is gone. To disable, run
`make clean-all && make provision OCR_ENABLED=false` (wipes `.env.local` +
`./data`; `make bootstrap` persists `OCR_ENABLED=false` + an empty
`COMPOSE_PROFILES` into `.env`): the token is no longer generated, the ocr
sidecar is no longer in the compose project, and `make api-keys` no longer
sets the OWUI routing — so new uploads use OWUI's default loaders. Existing
OCR'd members keep their content until re-ingested;
re-ingested image-only files return to orphans (the pre-OCR state). No full KB
reset.

[markitdown-ocr]: https://github.com/microsoft/markitdown/tree/main/packages/markitdown-ocr