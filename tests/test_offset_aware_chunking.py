#!/usr/bin/env python3
"""Unit tests for the offset-aware chunking patch (patch 5).

Tests `_atx_header_spans` and `split_docs_with_base` by extracting their source
from the apply script (`open-webui/apply_offset_aware_chunking.py`) and exec'ing
it in a namespace with stubs. This is a single-source-of-truth harness: the
functions under test are the exact strings the build injects, so a change to
the apply script is exercised here with no copy to drift.

Stubs match the real dependencies closely enough to validate the offset
arithmetic and the span logic:

- `sanitize_text_for_db`: pure deletion of NUL + surrogates (matches
  `open_webui/utils/misc.py`: `SURROGATE_RE = re.compile('[\\ud800-\\udfff]')`,
  `text.replace('\\x00','')` then `SURROGATE_RE.sub('')`).
- `RecursiveCharacterTextSplitter`: a fixed-window splitter that emits VERBATIM
  substrings with `add_start_index=True` setting metadata `start_index` to the
  relative offset. The real splitter also emits verbatim substrings (verified
  separately by a 3000-trial fuzz against langchain); the stub validates THIS
  chunker's offset rebasing, the page-joiner math, and span coverage.
- `Document`: minimal page_content + metadata holder.

The invariant under test for every chunk:

    base_text[start_index : start_index + len(chunk.page_content)] == chunk.page_content

No real gdrive, no stack, no network. Run: `python3 tests/test_offset_aware_chunking.py -v`.
"""
import os
import re
import sys
import types
import unittest
import random

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "docker", "open-webui"))
import apply_offset_aware_chunking as patch  # noqa: E402

FUNCS_SRC = patch.NEW_FUNCS

# --- stubs -----------------------------------------------------------------

SURROGATE_RE = re.compile("[\ud800-\udfff]")


def sanitize_text_for_db(text):
    # Match open_webui/utils/misc.py: drop NUL then surrogates (pure deletion).
    if not isinstance(text, str):
        return text
    if "\x00" not in text and not SURROGATE_RE.search(text):
        return text
    return SURROGATE_RE.sub("", text.replace("\x00", ""))


class Document:
    def __init__(self, page_content, metadata=None):
        self.page_content = page_content
        self.metadata = dict(metadata) if metadata else {}


class RecursiveCharacterTextSplitter:
    """Stub: verbatim-substring fixed-window splitter with overlap. Emits
    chunks that are exact substrings of the input; with add_start_index it
    sets metadata['start_index'] to the chunk's offset within that input
    (the section-relative offset the real splitter also sets)."""

    def __init__(self, chunk_size, chunk_overlap=0, add_start_index=False, **kw):
        self.cs = chunk_size
        self.co = chunk_overlap
        self.asi = add_start_index

    def split_documents(self, docs):
        out = []
        for d in docs:
            text = d.page_content
            if not text:
                m = dict(d.metadata)
                if self.asi:
                    m["start_index"] = 0
                out.append(Document("", m))
                continue
            i = 0
            n = len(text)
            while i < n:
                chunk = text[i : i + self.cs]
                m = dict(d.metadata)
                if self.asi:
                    m["start_index"] = i
                out.append(Document(chunk, m))
                if i + self.cs >= n:
                    break
                i += max(1, self.cs - self.co)
        return out


class _Log:
    def warning(self, *a, **k):
        pass


class StubConfig:
    def __init__(self, chunk_size=40, chunk_overlap=8):
        self.CHUNK_SIZE = chunk_size
        self.CHUNK_OVERLAP = chunk_overlap
        self.TEXT_SPLITTER = "character"
        self.CHUNK_MIN_SIZE_TARGET = 0


NS = {
    "re": re,
    "Document": Document,
    "RecursiveCharacterTextSplitter": RecursiveCharacterTextSplitter,
    "sanitize_text_for_db": sanitize_text_for_db,
    "log": _Log(),
}
exec(compile(FUNCS_SRC, "<patch5-funcs>", "exec"), NS)
_atx_header_spans = NS["_atx_header_spans"]
split_docs_with_base = NS["split_docs_with_base"]


# --- helpers ---------------------------------------------------------------

def make_doc(text, **meta):
    m = {"file_id": "f1", "name": "doc.md", "source": "doc.md",
         "created_by": "u1"}
    m.update(meta)
    return Document(text, m)


def assert_sliceable(base_text, chunks, msg=""):
    """Every chunk must be a verbatim slice of base_text at its start_index."""
    for c in chunks:
        si = c.metadata["start_index"]
        seg = base_text[si : si + len(c.page_content)]
        assert seg == c.page_content, (
            f"{msg}: base[si:si+len] != chunk. si={si} len={len(c.page_content)} "
            f"seg={seg!r} chunk={c.page_content!r}"
        )


# --- tests -----------------------------------------------------------------

class TestHeaderSpans(unittest.TestCase):
    def _covers(self, text):
        spans = _atx_header_spans(text)
        self.assertEqual(spans[0][0], 0)
        self.assertEqual(spans[-1][1], len(text))
        for k in range(1, len(spans)):
            self.assertEqual(spans[k][0], spans[k - 1][1])
        # concatenation is the whole text
        self.assertEqual("".join(text[a:b] for a, b in spans), text)
        return spans

    def test_multi_section(self):
        spans = self._covers("# A\nbody A\n# B\nbody B\n")
        self.assertEqual(len(spans), 2)

    def test_header_inside_fence_does_not_split(self):
        spans = self._covers("intro\n```python\n# not a header\nx = 1\n```\n# Real\nbody\n")
        self.assertEqual(len(spans), 2)

    def test_tildes_fence(self):
        # # A at 0 (start), # C after the closed ~~~ fence -> 2 sections
        spans = self._covers("# A\nb\n~~~\n# nope\n~~~\n# C\nd\n")
        self.assertEqual(len(spans), 2)

    def test_indented_fence_does_not_open(self):
        # A 4-space-indented ``` is an indented code block, not a fence opener
        # (CommonMark: fences allow <=3 leading spaces). A header after it must
        # still split. The old lstrip()-without-limit code falsely opened a fence
        # here and swallowed the header.
        spans = self._covers("intro\n    ```python\n# Real\nbody\n")
        self.assertEqual(len(spans), 2)

    def test_three_space_fence_opens(self):
        # <=3 leading spaces is a valid fence opener (CommonMark).
        spans = self._covers("# A\nb\n   ```\n# nope\n```\n# C\nd\n")
        self.assertEqual(len(spans), 2)

    def test_unclosed_fence_swallows_headers(self):
        spans = self._covers("```\n# swallowed\nstill no close\n")
        self.assertEqual(len(spans), 1)

    def test_headerless_single_span(self):
        spans = self._covers("just text no headers\n")
        self.assertEqual(spans, [(0, 21)])

    def test_empty(self):
        self.assertEqual(_atx_header_spans(""), [(0, 0)])

    def test_edge_headers(self):
        # #NoSpace not a header, 7 hashes not a header, bare # is a header.
        spans = self._covers("#NoSpace\n####### seven\n#\n# Real H\n")
        self.assertEqual(spans, [(0, 23), (23, 25), (25, 34)])

    def test_repeated_headers_within_page(self):
        spans = self._covers("# H\nbody\n# H\nbody\n# H\nbody\n")
        self.assertEqual(len(spans), 3)


class TestSplitDocsWithBase(unittest.TestCase):
    def setUp(self):
        self.cfg = StubConfig(chunk_size=40, chunk_overlap=8)

    def test_multi_section_slice_and_monotonic(self):
        text = "# Alpha\n" + ("alpha body sentence. " * 8) + "\n# Beta\n" + ("beta body line. " * 8) + "\n"
        base, chunks = split_docs_with_base([make_doc(text)], self.cfg)
        self.assertGreater(len(chunks), 2)
        assert_sliceable(base, chunks, "multi-section")
        sis = [c.metadata["start_index"] for c in chunks]
        self.assertTrue(all(s > 0 for s in sis[1:]), "offsets after first are non-zero")
        self.assertEqual(sis, sorted(sis), "offsets non-decreasing")
        self.assertEqual(len(sis), len(set(sis)), "offsets distinct")
        # metadata preserved, no Header leak
        for c in chunks:
            self.assertEqual(c.metadata["file_id"], "f1")
            self.assertEqual(c.metadata["source"], "doc.md")
            self.assertNotIn("Header 1", c.metadata)
            self.assertNotIn("Header 2", c.metadata)

    def test_headerless_page(self):
        text = "no headers here, just a body paragraph that is long enough to split. " * 4
        base, chunks = split_docs_with_base([make_doc(text)], self.cfg)
        assert_sliceable(base, chunks, "headerless")
        # base is the sanitized verbatim page
        self.assertEqual(base, sanitize_text_for_db(text))

    def test_fenced_code_block_preserved_verbatim(self):
        code = "```python\n# not a header\n    def f():\n        return 1\n```\n"
        text = "# Section\n" + code + "# Next\nbody\n"
        base, chunks = split_docs_with_base([make_doc(text)], self.cfg)
        assert_sliceable(base, chunks, "fenced")
        # the indented code line survives verbatim in base (no strip)
        self.assertIn("    def f():", base)
        self.assertIn("        return 1", base)

    def test_multi_page_joiner_math(self):
        p1 = "# P1\n" + "page one content here. " * 6
        p2 = "# P2\n" + "page two content here. " * 6
        base, chunks = split_docs_with_base(
            [make_doc(p1, page=1), make_doc(p2, page=2)], self.cfg
        )
        assert_sliceable(base, chunks, "multi-page")
        # base = sanitized(p1) + " " + sanitized(p2)
        self.assertEqual(base, sanitize_text_for_db(p1) + " " + sanitize_text_for_db(p2))
        # offsets are global and increasing across the joiner
        sis = [c.metadata["start_index"] for c in chunks]
        self.assertEqual(sis, sorted(sis))
        # a chunk from page 2 must sit at an offset >= len(page1)+1
        self.assertGreaterEqual(sis[-1], len(sanitize_text_for_db(p1)) + 1)

    def test_null_and_surrogate_sanitization(self):
        text = "# H\nbody\x00with null and \udcff surrogate more text " * 5
        base, chunks = split_docs_with_base([make_doc(text)], self.cfg)
        assert_sliceable(base, chunks, "sanitized")
        self.assertNotIn("\x00", base)
        # base is the sanitized text (deletions), chunks are its substrings
        self.assertEqual(base, sanitize_text_for_db(text))

    def test_empty_and_whitespace_page(self):
        base, chunks = split_docs_with_base([make_doc("")], self.cfg)
        self.assertEqual(base, "")
        self.assertEqual(chunks, [])
        base2, chunks2 = split_docs_with_base([make_doc("   \n  \n  ")], self.cfg)
        # whitespace-only: base is the verbatim whitespace; chunks may be empty/whitespace
        for c in chunks2:
            seg = base2[c.metadata["start_index"] : c.metadata["start_index"] + len(c.page_content)]
            self.assertEqual(seg, c.page_content)

    def test_fallback_single_doc_base_equals_input(self):
        # Branch B fallback rechunks file.data['content']; span-preserving is
        # verbatim, so base_text == the sanitized input (no desync).
        text = "# H\nsome body content to chunk. " * 6
        base, chunks = split_docs_with_base([make_doc(text)], self.cfg)
        self.assertEqual(base, sanitize_text_for_db(text))
        assert_sliceable(base, chunks, "fallback")

    def test_rel_negative_keeps_chunk(self):
        # A splitter that returns start_index = -1: the chunker must keep the
        # chunk (best-effort offset = section start), not drop it.
        class BadSplitter:
            def __init__(self, *a, **k):
                pass
            def split_documents(self, docs):
                out = []
                for d in docs:
                    m = dict(d.metadata)
                    m["start_index"] = -1  # simulate "not found"
                    out.append(Document(d.page_content, m))
                return out
        ns = dict(NS)
        ns["RecursiveCharacterTextSplitter"] = BadSplitter
        exec(compile(FUNCS_SRC, "<patch5-funcs>", "exec"), ns)
        fn = ns["split_docs_with_base"]
        text = "# H\nbody content here. " * 6
        base, chunks = fn([make_doc(text)], self.cfg)
        self.assertGreater(len(chunks), 0, "rel<0 must not drop chunks")
        # best-effort offset is the section start (0 here); chunk kept
        for c in chunks:
            self.assertEqual(c.metadata["start_index"], 0)

    def test_repeated_header_text_disambiguates(self):
        # Repeated identical headers + identical bodies: offsets still distinct
        # and sliceable (the splitter's hinted find + span-relative math).
        text = "# H\nAB\n# H\nAB\n# H\nAB\n"
        base, chunks = split_docs_with_base([make_doc(text)], self.cfg)
        assert_sliceable(base, chunks, "repeated")
        sis = [c.metadata["start_index"] for c in chunks]
        self.assertEqual(len(sis), len(set(sis)), "distinct offsets despite repeats")

    def test_coalesce_small_sections(self):
        # Many tiny header-only sections coalesce when CHUNK_MIN_SIZE_TARGET > 0:
        # fewer, larger chunks; sliceability + distinct offsets still hold. This
        # is the span-level mitigation for the tiny-chunk explosion (P1-1).
        cfg = StubConfig(chunk_size=200, chunk_overlap=0)
        cfg.CHUNK_MIN_SIZE_TARGET = 80
        text = "".join(f"# Item {i}\nbody{i}.\n" for i in range(40))
        base, chunks = split_docs_with_base([make_doc(text)], cfg)
        assert_sliceable(base, chunks, "coalesced")
        cfg0 = StubConfig(chunk_size=200, chunk_overlap=0)  # min_size=0 -> no coalesce
        _, chunks0 = split_docs_with_base([make_doc(text)], cfg0)
        self.assertGreater(len(chunks0), len(chunks), "coalesce must reduce chunk count")
        self.assertLess(len(chunks), 40, "tiny sections must merge, not stay 1-per-header")
        sis = [c.metadata["start_index"] for c in chunks]
        self.assertEqual(len(sis), len(set(sis)), "distinct offsets after coalesce")

    def test_coalesce_off_at_zero(self):
        # CHUNK_MIN_SIZE_TARGET=0 -> no coalescing (header-strict), matches the
        # legacy =0 behavior: one chunk per section when each fits CHUNK_SIZE.
        cfg = StubConfig(chunk_size=200, chunk_overlap=0)
        text = "".join(f"# H{i}\nshort.\n" for i in range(20))
        base, chunks = split_docs_with_base([make_doc(text)], cfg)
        assert_sliceable(base, chunks, "no-coalesce")
        self.assertEqual(len(chunks), 20)

    def test_transform_is_not_idempotent(self):
        """Guard against the double-apply defect (P0-1): applying
        split_docs_with_base to its own chunk output must NOT reproduce the
        correct base. A second application re-splits the chunk list, duplicating
        overlap regions and inserting page-joiners, so base2 != base1 and grows.
        This is why a second call site (e.g. once in Branch A, once at the outer
        level) silently corrupts /data/content while the slice invariant still
        holds against the corrupted base. A single application is correct."""
        text = "# H\n" + "body content here. " * 10
        base1, chunks1 = split_docs_with_base([make_doc(text)], self.cfg)
        assert_sliceable(base1, chunks1, "first apply")
        self.assertEqual(base1, sanitize_text_for_db(text))
        self.assertGreater(len(chunks1), 1, "fixture must yield >1 chunk")
        # second apply on the chunk output corrupts the base
        base2, _ = split_docs_with_base(chunks1, self.cfg)
        self.assertNotEqual(base2, base1, "double-apply must change base")
        self.assertGreater(len(base2), len(base1), "overlap duplication grows base")

    def test_context_window_is_raw(self):
        # base_text[si-W:si+W] must be raw text (no MDS strip). Put indentation
        # and a code fence around a chunk and check the window keeps it.
        text = "# H\n    indented code line one\n    indented code line two\n# H2\nplain\n"
        cfg = StubConfig(chunk_size=60, chunk_overlap=0)
        base, chunks = split_docs_with_base([make_doc(text)], cfg)
        assert_sliceable(base, chunks, "context")
        # find a chunk inside the indented section and check its window keeps leading spaces
        indented = [c for c in chunks if "indented" in c.page_content][0]
        si = indented.metadata["start_index"]
        win = base[max(0, si - 4) : si + len(indented.page_content) + 4]
        self.assertIn("    ", win)


class TestFuzz(unittest.TestCase):
    def test_fuzz_sliceable(self):
        rng = random.Random(20260828)
        # adversarial alphabet: headers, fences, indentation, blanks, CRLF-ish, null
        alphabet = (
            "# H\n## H\nbody\n  \n    code\n```python\n# not header\n```\n"
            "word " * 3 + "𐏿\x00"
        )
        cfg = StubConfig(chunk_size=50, chunk_overlap=10)
        for trial in range(500):
            npages = rng.randint(1, 4)
            pages = []
            for _ in range(npages):
                ntok = rng.randint(0, 60)
                text = "".join(rng.choice(alphabet) for _ in range(ntok))
                pages.append(make_doc(text, page=pages.__len__() + 1))
            base, chunks = split_docs_with_base(pages, cfg)
            assert_sliceable(base, chunks, f"fuzz trial {trial}")
            # offsets non-negative and within bounds
            for c in chunks:
                si = c.metadata["start_index"]
                self.assertGreaterEqual(si, 0)
                self.assertLessEqual(si + len(c.page_content), len(base))


if __name__ == "__main__":
    unittest.main(verbosity=2)