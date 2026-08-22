# markitdown-OCR external extraction service

The `markitdown-ocr` sidecar is an **external extraction engine** for Open WebUI.
It runs [markitdown-ocr][markitdown-ocr] with the OCR service replaced by an
Ollama-native `/api/chat` client (`markitdown/oursvc.py`), so embedded figures
and image-only pages are OCR'd by `deepseek-ocr` and become searchable KB
members. Off by default; profile-gated (`--profile ocr`); no fallback.

This is the per-figure OCR path for the `gdrive` knowledge base: image-only
PDFs (no text layer) and figure/diagram content in text+figure PDFs become
searchable instead of orphaning. Office files (PPTX/DOCX/XLSX) are converted
natively (no libreoffice), which also solves the PPTX orphan.

## Data flow

`gdrive → oikb → Open WebUI → markitdown-ocr → deepseek-ocr (Ollama) →
per-page / per-slide / per-sheet chunks`

oikb uploads a file to Open WebUI; Open WebUI calls the external engine (`PUT
/process`); markitdown-ocr runs the OCR converters with
`OllamaNativeOCRService`, splits the result into per-unit documents, and returns
a JSON list. OWUI turns each into a Document, adds `file_id` / `source`, and
chunks it. The existing `pathdedup` + idem dedup still run (the engine replaces
only the loader, not the rest of `process_file`).

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
`page` → the exact original page / slide / sheet. Round-trip: `owui.py file
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
set excludes standalone images via `.oikb.yaml`, so this branch matters for
direct uploads, not the synced set.)

## Provisioning (one-time, after `make api-keys`)

`make ocr-bootstrap`:

1. generates `OCR_SERVICE_TOKEN` (if missing) into `.env.local`;
2. builds the `markitdown-ocr` image;
3. (re)creates the `markitdown-ocr` service (`--profile ocr`);
4. waits for `/health` (the compose healthcheck);
5. runs `make ocr-config` — sets `CONTENT_EXTRACTION_ENGINE=external` +
   `EXTERNAL_DOCUMENT_LOADER_URL=http://markitdown-ocr:8080` + the API key in
   the OWUI DB (`/api/v1/retrieval/config/update`, merge semantics), then
   read-back-asserts each key stuck;
6. writes `MARKITDOWN_OCR_PROVISIONED=1` to `.env.local` **only on success**.

`make start` / `make restart` then add `--profile ocr` automatically when the
marker is set. Idempotent. **No fallback:** if any step fails, the marker is NOT
written and OWUI keeps its default loaders (a half-provisioned global engine
would orphan every ingest).

`make ocr-config` re-asserts the OWUI DB keys (re-run after a DB reset).
`make preflight` warns when provisioned if `deepseek-ocr` is not pulled or
`rag.content_extraction_engine` has drifted from `external`.

## Scope and limits

- **Global + all-or-nothing.** When the engine is enabled, OWUI routes **every**
  ingest to markitdown-ocr — no per-type fallback; an empty result orphans.
  Covered: PDF/DOCX/PPTX/XLSX (OCR converters), standalone images (direct OCR),
  text/csv/json/html (built-in converters). **Not covered:** audio/video (no
  ffmpeg in v1) → would orphan. Keep those out of the synced set (`.oikb.yaml`
  already excludes audio/video).
- **API key required.** An empty `EXTERNAL_DOCUMENT_LOADER_API_KEY` makes OWUI
  silently skip the external engine and fall back to its default loaders.
  `make ocr-bootstrap` sets a non-empty `OCR_SERVICE_TOKEN`.
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

- `docker logs -f markitdown-ocr` — per-request log + OCR errors.
- `make preflight` — when provisioned, warns if `deepseek-ocr` is not pulled or
  `rag.content_extraction_engine` has drifted from `external` (fix: `make
  ocr-config`).
- `GET /api/v1/retrieval/config` (admin key) — confirm the engine is set.

## Reversion (no KB reset)

`make ocr-disable` clears `CONTENT_EXTRACTION_ENGINE` in the OWUI DB, removes
the `MARKITDOWN_OCR_PROVISIONED` marker, and recreates `openwebui`. New uploads
then use OWUI's default loaders. Existing OCR'd members keep their content
until re-ingested; re-ingested image-only files return to orphans (the pre-OCR
state). No full KB reset.

[markitdown-ocr]: https://github.com/microsoft/markitdown/tree/main/packages/markitdown-ocr