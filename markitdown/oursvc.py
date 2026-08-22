#!/usr/bin/env python3
"""markitdown-ocr extraction service for the Open WebUI external extraction engine.

Purpose:
  Run MarkItDown with the markitdown-ocr plugin, but replace the plugin's
  OpenAI-compatible (Ollama /v1) LLMVisionOCRService with OllamaNativeOCRService,
  which calls Ollama's NATIVE /api/chat endpoint.

Why native /api/chat (not the /v1 shim):
  deepseek-ocr needs its Gundam image preprocessing (a 1024x1024 global view
  plus dynamic 640x640 local tiles). Ollama's native /api/chat runner applies
  this preprocessing automatically. The OpenAI-compatible /v1 shim does NOT.
  Through /v1, a large image or a full-page render collapses into a repetition
  loop (the model emits one token thousands of times until the token cap).
  Through /api/chat, the same image returns clean markdown.

Why a custom OCR service (not a fork, not a source patch):
  markitdown-ocr exposes a public, injectable OCR service interface
  (LLMVisionOCRService.extract_text -> OCRResult). The PDF/DOCX/PPTX/XLSX
  converters accept an ocr_service= argument and call only extract_text on it.
  We provide a drop-in service with the same method, and register the public
  converter classes ourselves with MarkItDown(enable_plugins=False). No
  markitdown-ocr source is modified. No fragile str.replace anchors.

Per-unit metadata:
  The PDF converter emits a native "## Page N" header per page. The PPTX
  converter emits a "<!-- Slide number: N -->" comment per slide. The XLSX
  converter emits a "## {sheet_name}" header per sheet. This service splits
  the markdown on the marker that matches the input type and returns a JSON
  LIST of per-unit documents {page_content, metadata: {page: N}} (XLSX also
  carries {sheet: name}). The OWUI external extraction loader turns each into
  a Document; process_file adds file_id/source and filter_metadata keeps page.
  Non-paginated output (DOCX, text, csv, json, html) is returned as a single
  {page_content, metadata} document. Standalone image files are OCR'd directly
  (markitdown-ocr has no standalone-image converter) and returned as one document.

Contract (OWUI external extraction engine):
  PUT /process
  body: raw file bytes
  headers: Content-Type (mime), X-Filename (percent-encoded name),
           Authorization: Bearer <token> (optional; set OCR_SERVICE_TOKEN to enforce)
  response: application/json
    paginated -> [ {"page_content": str, "metadata": {"page": int, ...}}, ... ]
    other     -> { "page_content": str, "metadata": {} }

Zero extra dependencies: stdlib urllib (for /api/chat and the HTTP server) and
stdlib http.server. Does NOT use the openai SDK (the plugin's [llm] extra is
not installed). Pillow (a core markitdown-ocr dep) is used for the min-size skip.
"""

import base64
import io
import json
import os
import re
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from markitdown import MarkItDown
from markitdown_ocr import (
    DocxConverterWithOCR,
    PdfConverterWithOCR,
    PptxConverterWithOCR,
    XlsxConverterWithOCR,
)
from markitdown_ocr._ocr_service import OCRResult


# deepseek-ocr emits internal grounding/markdown tokens that Ollama's /api/chat
# does NOT strip. Strip every <|...|> token from the OCR text before returning.
_LEAK = re.compile(r"<\|[^|]*\|>")

# Per-unit markers emitted by the upstream converters (after PPTX normalization).
_PDF_HEADER = re.compile(r"^## Page (\d+)\s*$", re.MULTILINE)
_PPTX_HEADER = re.compile(r"^<!-- Slide number: (\d+) -->\s*$", re.MULTILINE)
_XLSX_HEADER = re.compile(r"^## (.+?)\s*$", re.MULTILINE)

# Input type, inferred from the file suffix (or content-type), selects the split.
_KIND_BY_EXT = {
    ".pdf": "pdf",
    ".pptx": "pptx",
    ".xlsx": "xlsx",
    ".docx": "docx",
}

# Standalone image suffixes (markitdown-ocr has no converter for these; we OCR
# the raw bytes directly).
_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".tiff", ".tif"}

_EXT_BY_MIME = {
    "application/pdf": ".pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
}

_DEFAULT_PROMPT = (
    "Extract all text from this image. Return ONLY the extracted text, "
    "maintaining the original layout and order. Do not add any commentary "
    "or description."
)


class OllamaNativeOCRService:
    """Drop-in replacement for markitdown-ocr's LLMVisionOCRService.

    Same public method contract: extract_text(image_stream, prompt, stream_info)
    -> OCRResult. Calls Ollama native /api/chat with a base64 images array.
    """

    _lock = threading.Lock()  # serialize OCR calls across threads (bound GPU eviction thrash)

    def __init__(
        self,
        base_url,
        model="deepseek-ocr",
        prompt=_DEFAULT_PROMPT,
        num_predict=8192,
        timeout=120,
        keep_alive="5m",
        repeat_penalty=1.1,
        min_dim=64,
    ):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.default_prompt = prompt
        self.num_predict = int(num_predict)
        self.timeout = int(timeout)
        self.keep_alive = keep_alive
        self.repeat_penalty = float(repeat_penalty)
        self.min_dim = int(min_dim)

    def extract_text(self, image_stream, prompt=None, stream_info=None, **kwargs):
        """Extract text from one image via Ollama /api/chat. Never raises:
        returns OCRResult(error=...) on failure (fail-open, like the upstream
        service)."""
        try:
            image_stream.seek(0)
            if not self._passes_min_size(image_stream):
                return OCRResult(text="", backend_used="ollama-native")
            image_stream.seek(0)
            b64 = base64.b64encode(image_stream.read()).decode("utf-8")
            body = {
                "model": self.model,
                "messages": [
                    {
                        "role": "user",
                        "content": prompt or self.default_prompt,
                        "images": [b64],
                    }
                ],
                "options": {
                    "temperature": 0,
                    "seed": 0,
                    "num_predict": self.num_predict,
                    "repeat_penalty": self.repeat_penalty,
                },
                "keep_alive": self.keep_alive,
                "stream": False,
            }
            req = urllib.request.Request(
                self.base_url + "/api/chat",
                data=json.dumps(body).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with self._lock:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    resp = json.loads(r.read().decode("utf-8"))
            text = (resp.get("message", {}).get("content", "") or "")
            text = _LEAK.sub("", text).strip()
            return OCRResult(text=text, backend_used="ollama-native")
        except Exception as e:  # noqa: BLE001 - fail-open, mirror upstream
            return OCRResult(text="", backend_used="ollama-native", error=str(e))
        finally:
            try:
                image_stream.seek(0)
            except Exception:
                pass

    def _passes_min_size(self, image_stream):
        """Drop tiny icons before the Ollama call: returns False if the smaller
        image dimension is below min_dim (saves GPU + avoids icon hallucination)."""
        if self.min_dim <= 0:
            return True
        try:
            from PIL import Image  # core markitdown-ocr dep

            img = Image.open(image_stream)
            w, h = img.size
            return min(w, h) >= self.min_dim
        except Exception:
            return True  # cannot determine size -> do not skip (let OCR try)


def build_markitdown(service):
    """Build a MarkItDown instance with the OCR converters bound to our service.
    enable_plugins=False skips the plugin's register_converters (which would
    build the /v1 LLMVisionOCRService). We register the public converter
    classes ourselves at priority -1.0 (before built-ins at 0.0)."""
    md = MarkItDown(enable_plugins=False)
    for converter in (
        PdfConverterWithOCR,
        DocxConverterWithOCR,
        PptxConverterWithOCR,
        XlsxConverterWithOCR,
    ):
        md.register_converter(converter(ocr_service=service), priority=-1.0)
    return md


def _split_pairs(markdown, header_re, group=1):
    """Split markdown into [(unit_id, content)] on header_re matches. unit_id is
    the int (PDF page / PPTX slide) from group 1 when group='int', else the
    string (XLSX sheet name)."""
    matches = list(header_re.finditer(markdown))
    if not matches:
        return []
    pairs = []
    for i, m in enumerate(matches):
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(markdown)
        content = markdown[start:end].strip()
        if not content:
            continue
        val = m.group(1)
        pairs.append((val, content))
    return pairs


def split_units(markdown, kind):
    """Split markdown into per-unit documents for the given input kind.
    Returns a list of {page_content, metadata} dicts, or [] if no units parse.
    kind in {pdf, pptx, xlsx, docx, None}."""
    if kind == "pdf":
        pairs = _split_pairs(markdown, _PDF_HEADER)
        return [
            {"page_content": content, "metadata": {"page": int(num)}}
            for num, content in pairs
        ]
    if kind == "pptx":
        pairs = _split_pairs(markdown, _PPTX_HEADER)
        return [
            {"page_content": content, "metadata": {"page": int(num)}}
            for num, content in pairs
        ]
    if kind == "xlsx":
        pairs = _split_pairs(markdown, _XLSX_HEADER)
        units = []
        for idx, (sheet_name, content) in enumerate(pairs, 1):
            units.append(
                {
                    "page_content": content,
                    "metadata": {"page": idx, "sheet": sheet_name},
                }
            )
        return units
    return []  # docx, None, or unknown -> single blob (caller handles)


def _suffix(filename, content_type):
    """Pick a file suffix for type inference: prefer the filename extension,
    fall back to the content-type map."""
    if filename:
        _, ext = os.path.splitext(filename)
        if ext:
            return ext.lower()
    return _EXT_BY_MIME.get((content_type or "").split(";")[0].strip().lower(), "")


def _kind_of(suffix, content_type):
    """Infer the input kind. Returns 'image' for standalone images, else the
    document kind in _KIND_BY_EXT, else None."""
    if suffix in _IMAGE_EXTS or (content_type or "").startswith("image/"):
        return "image"
    return _KIND_BY_EXT.get(suffix)


def extract(file_bytes, filename, content_type, md, service):
    """Run markitdown on raw file bytes and return the OWUI external-engine
    response: a list of per-unit docs (paginated) or a single doc dict.

    Standalone images bypass the converters and are OCR'd directly. PPTX output
    is normalized (the upstream converter emits literal "\\n") before splitting.
    """
    suffix = _suffix(filename, content_type)
    kind = _kind_of(suffix, content_type)

    if kind == "image":
        ocr = service.extract_text(io.BytesIO(file_bytes))
        return {"page_content": ocr.text or "", "metadata": {}}

    import tempfile

    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tf:
        tf.write(file_bytes)
        tmp_path = tf.name
    try:
        result = md.convert(tmp_path)
        markdown = result.text_content or ""
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

    if kind == "pptx":
        # Upstream PPTX converter emits literal "\n" (two chars) not real
        # newlines. Normalize so per-slide split works AND slide text is not
        # glued. OCR blocks already use real "\n" and contain no literal "\n".
        markdown = markdown.replace("\\n", "\n")

    units = split_units(markdown, kind)
    if units:
        return units
    return {"page_content": markdown, "metadata": {}}


class _Handler(BaseHTTPRequestHandler):
    server_version = "markitdown-ocr/1.0"
    _md = None  # set by serve()
    _service = None  # set by serve()
    _token = None  # set by serve()

    def _send(self, code, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            self._send(200, {"status": "ok"})
        else:
            self._send(404, {"error": "not found"})

    def do_PUT(self):
        if self.path != "/process":
            self._send(404, {"error": "not found"})
            return
        if self._token is not None:
            auth = self.headers.get("Authorization", "")
            if auth != "Bearer " + self._token:
                self._send(401, {"error": "unauthorized"})
                return
        length = int(self.headers.get("Content-Length", 0) or 0)
        body = self.rfile.read(length) if length else b""
        content_type = self.headers.get("Content-Type")
        filename = urllib.parse.unquote(self.headers.get("X-Filename", "") or "")
        try:
            result = extract(body, filename, content_type, self._md, self._service)
        except Exception as e:  # noqa: BLE001 - fail to OWUI (orphan, greppable), no fallback
            self._send(500, {"error": "extraction failed: " + str(e)})
            return
        self._send(200, result)

    def log_message(self, fmt, *args):  # stderr, one line per request
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))


def serve(host, port, service, md, token):
    _Handler._md = md
    _Handler._service = service
    _Handler._token = token
    httpd = ThreadingHTTPServer((host, port), _Handler)
    sys.stderr.write("markitdown-ocr listening on %s:%d\n" % (host, port))
    httpd.serve_forever()


def _build_service_from_env():
    return OllamaNativeOCRService(
        base_url=os.environ["OLLAMA_BASE_URL"],
        model=os.environ.get("OCR_MODEL", "deepseek-ocr"),
        prompt=os.environ.get("OCR_PROMPT", _DEFAULT_PROMPT),
        num_predict=os.environ.get("OCR_NUM_PREDICT", 8192),
        timeout=os.environ.get("OCR_TIMEOUT", 120),
        keep_alive=os.environ.get("OCR_KEEP_ALIVE", "5m"),
        repeat_penalty=os.environ.get("OCR_REPEAT_PENALTY", 1.1),
        min_dim=os.environ.get("OCR_MIN_DIM", 64),
    )


def main():
    base_url = os.environ.get("OLLAMA_BASE_URL")
    if not base_url:
        sys.stderr.write("OLLAMA_BASE_URL is required\n")
        sys.exit(2)
    service = _build_service_from_env()
    md = build_markitdown(service)
    token = os.environ.get("OCR_SERVICE_TOKEN") or None
    if token == "":
        token = None
    port = int(os.environ.get("PORT", 8080))
    host = os.environ.get("HOST", "0.0.0.0")
    serve(host, port, service, md, token)


if __name__ == "__main__":
    sys.exit(main())