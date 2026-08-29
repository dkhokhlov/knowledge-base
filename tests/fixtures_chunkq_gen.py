#!/usr/bin/env python3
"""Deterministic chunk-quality fixture generator for tests/test_13_chunk_quality.sh.

Writes one synthetic file per allowlisted type (docx, pdf, pptx, xlsx, txt, md,
html, json, log, tex) into --out (default gdrive/.tests/chunkq) and prints a
manifest JSON to stdout. The manifest is the test's oracle: per file it lists
the unique marker strings and the chunk structure to expect.

Design constraints (verified against the live markitdown-ocr sidecar; see
tests/test_13_chunk_quality.sh and docs/ocr.md):

- Pure ASCII everywhere, alphabet [A-Za-z0-9 .,:-] only. The html/docx/xlsx
  converters pass output through markdownify, which escapes _ * [ ] backtick
  (`col_a` becomes `col\\_a`) -- escaped text would break substring asserts.
- .json has no markitdown converter (0.1.7) and rides PlainTextConverter,
  which mis-detects charset for non-ASCII -- so json MUST be pure ASCII.
- md/html/docx carry 8 ATX-header sections: 6 sized 200..1000 chars (1 chunk
  each, no coalesce) + a deliberate coalesce pair (s7 span < 200 chars, so the
  chunker merges it forward into s8 -> ONE chunk holding both markers).
- pdf/pptx/xlsx: one unit (page/slide/sheet) per marker, each unit sized
  200..1000 chars so units never coalesce across the page joiner and each
  unit is exactly 1 chunk with meta.page set.
- The pdf needs Td + TL + T* per line: consecutive Tj without line advance
  make pdfminer glue header and body onto one line (the ATX scan then swallows
  the whole page).
- xlsx row 1 is the pandas header row, so each sheet emits an explicit header
  row plus data rows.

Determinism: no timestamps, no RNG, fixed zip member mtimes -- two runs must
produce byte-identical trees (sha256 check in the test).

Usage:
  python3 tests/fixtures_chunkq_gen.py [--out DIR] [--types md,html,...]
"""

import argparse
import io
import json
import sys
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SKELETON_DIR = REPO / "gdrive" / ".tests"

# Word pool for filler bodies: alphabet-constrained (no markdownify-escapable
# characters), no word that could start a line with '#'.
_WORDS = (
    "alpha bravo charlie delta echo foxtrot golf hotel india juliet kilo lima "
    "mike november oscar papa quebec romeo sierra tango uniform victor whiskey "
    "xray yankee zulu data logic cache queue frame batch pulse token state"
).split()


def _filler(n_chars, seed):
    """Return a deterministic filler text of at least n_chars characters.

    Word i is _WORDS[(seed*31 + i*7) % len] -- stable across runs, varied
    across seeds. A period lands after every 9th word so the text stays
    sentence-shaped.
    """
    words = []
    i = 0
    while len(" ".join(words)) < n_chars:
        w = _WORDS[(seed * 31 + i * 7) % len(_WORDS)]
        if i % 9 == 8:
            w += "."
        words.append(w)
        i += 1
    return " ".join(words) + "."


def _sections():
    """The 8 header sections shared by md/html/docx.

    Body lengths: 260 chars (span ~285: >=200 no-coalesce, <1000 one chunk)
    except s7 at 52 chars (span ~76 <200 -> coalesces forward into s8).
    """
    return [
        {"level": 1 if i == 1 else 2, "body": _filler(52 if i == 7 else 260, seed=i)}
        for i in range(1, 9)
    ]


def _word(i):
    return _WORDS[i % len(_WORDS)]


# --- text formats -------------------------------------------------------------


def _md_text(sec):
    parts = []
    for idx, s in enumerate(sec, 1):
        head = ("#" * s["level"]) + " chunkq-md-s%d %s" % (idx, _word(idx))
        parts.append(head + "\n\n" + s["body"] + "\n\n")
    return "".join(parts)


def _html_text(sec):
    parts = ["<!DOCTYPE html>", "<html>", "<body>"]
    for idx, s in enumerate(sec, 1):
        tag = "h1" if s["level"] == 1 else "h2"
        parts.append("<%s>chunkq-html-s%d %s</%s>" % (tag, idx, _word(idx), tag))
        parts.append("<p>%s</p>" % s["body"])
    parts.append("</body>")
    parts.append("</html>")
    return "\n".join(parts) + "\n"


def _txt_text():
    # Two ~1150-char blocks: no ATX headers, so the whole doc is one span and
    # the splitter (not the header scan) produces the chunks -- exercises
    # sliceability + distinct offsets under CHUNK_OVERLAP.
    return (
        "chunkq-txt-s1 " + _filler(1130, seed=21) + "\n\n"
        "chunkq-txt-s2 " + _filler(1130, seed=22) + "\n"
    )


def _json_text():
    return (
        '{"marker": "chunkq-json-s1 alpha", '
        '"payload": "%s", "kind": "chunkq-fixture"}\n' % _filler(250, seed=31)
    )


def _log_text():
    return (
        "2026-01-01T00:00:01Z INFO chunkq-log-s1 alpha\n"
        "2026-01-01T00:00:02Z INFO %s\n"
        "2026-01-01T00:00:03Z INFO end of chunkq-log fixture\n" % _filler(220, seed=41)
    )


def _tex_text():
    return (
        "\\section{chunkq-tex-s1 alpha}\n\n" + _filler(250, seed=51) + "\n\n"
        "\\section{chunkq-tex-tail}\n\nEnd of chunkq-tex fixture.\n"
    )


# --- docx: clone the committed skeleton, replace document.xml -----------------


def _docx_bytes(sec):
    """Clone gdrive/.tests/fixture-doc.docx; swap in Heading1/Heading2 paragraphs.

    No styles.xml needed: mammoth's default style map matches pStyle
    Heading1/Heading2 by styleId, so bare <w:pStyle> emits #/## headers.
    """
    body = []
    for idx, s in enumerate(sec, 1):
        style = "Heading1" if s["level"] == 1 else "Heading2"
        body.append(
            '<w:p><w:pPr><w:pStyle w:val="%s"/></w:pPr>'
            '<w:r><w:t xml:space="preserve">chunkq-docx-s%d %s</w:t></w:r></w:p>'
            % (style, idx, _word(idx))
        )
        body.append('<w:p><w:r><w:t xml:space="preserve">%s</w:t></w:r></w:p>' % s["body"])
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        "<w:body>%s</w:body></w:document>" % "".join(body)
    )
    return _clone_zip(
        SKELETON_DIR / "fixture-doc.docx",
        {"word/document.xml": document.encode("ascii")},
    )


# --- pdf: hand-written 4-page text-only ----------------------------------------


def _pdf_bytes():
    """Build a minimal 4-page PDF, one ATX-header section per page.

    Content streams use one Td, then 14 TL with T* per line: without the line
    advance, pdfminer glues the header and the body onto one line and the ATX
    scan swallows the page. Object payloads are assembled with tracked byte
    offsets so the xref table is correct.
    """
    streams = []
    for p in range(1, 5):
        body = _filler(220, seed=60 + p)
        half = len(body) // 2
        lines = ["# chunkq-pdf-p%d %s" % (p, _word(p)), body[:half], body[half:]]
        parts = ["BT /F1 12 Tf 72 720 Td 14 TL"]
        parts += ["(%s) Tj T*" % ln for ln in lines]
        parts.append("ET")
        streams.append("\n".join(parts).encode("ascii"))

    objs = []  # payload bytes per object number (1-based), None = placeholder

    def add(payload):
        objs.append(payload)
        return len(objs)

    add(None)  # 1: catalog
    add(None)  # 2: pages
    content_nums = [add(b"<< /Length %d >>\nstream\n%s\nendstream" % (len(s), s)) for s in streams]
    page_nums = [add(None) for _ in streams]  # placeholders; need the font num
    font_num = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    objs[0] = b"<< /Type /Catalog /Pages 2 0 R >>"
    kids = " ".join("%d 0 R" % n for n in page_nums)
    objs[1] = ("<< /Type /Pages /Kids [%s] /Count 4 >>" % kids).encode("ascii")
    for i, pn in enumerate(page_nums):
        objs[pn - 1] = (
            "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            "/Resources << /Font << /F1 %d 0 R >> >> /Contents %d 0 R >>"
            % (font_num, content_nums[i])
        ).encode("ascii")

    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for num, payload in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % num
        out += payload
        out += b"\nendobj\n"
    xref_at = len(out)
    out += b"xref\n0 %d\n" % (len(objs) + 1)
    out += b"0000000000 65535 f \n"
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objs) + 1,
        xref_at,
    )
    return bytes(out)


# --- pptx: clone the committed skeleton, duplicate the slide ------------------


def _pptx_bytes():
    """Clone gdrive/.tests/fixture-slide.pptx and grow it to 4 slides.

    Surgery per slide N: ppt/slides/slideN.xml (title text patched),
    ppt/slides/_rels/slideN.xml.rels (clone), one <p:sldId> in the presentation
    sldIdLst, one Relationship in presentation.xml.rels, one Override in
    [Content_Types].xml. Slide titles stay < 1000 chars (one span per slide).
    The skeleton already carries rId2 -> slides/slide1.xml, so extra slides
    start at rId3.
    """
    with zipfile.ZipFile(SKELETON_DIR / "fixture-slide.pptx") as src:
        data = {n: src.read(n) for n in src.namelist()}
        title_tpl = (
            src.read("ppt/slides/slide1.xml")
            .decode("ascii")
            .replace("gdrive-fixture-marker-7f3a2 fixture-pptx-content", "{TITLE}")
        )
        pres = src.read("ppt/presentation.xml").decode("ascii")
        pres_rels = src.read("ppt/_rels/presentation.xml.rels").decode("ascii")
        ct = src.read("[Content_Types].xml").decode("ascii")
        slide_rels = src.read("ppt/slides/_rels/slide1.xml.rels")

    sld_ids = []
    for n in range(1, 5):
        title = "chunkq-pptx-s%d %s %s" % (n, _word(n), _filler(80, seed=70 + n))
        data["ppt/slides/slide%d.xml" % n] = title_tpl.replace("{TITLE}", title).encode("ascii")
        data["ppt/slides/_rels/slide%d.xml.rels" % n] = slide_rels
        sld_ids.append('<p:sldId id="p:%d" r:id="rId%d"/>' % (255 + n, n + 1))
        if n >= 2:
            # rId2 (slide1) exists in the skeleton; slides 2..4 add rId3..rId5.
            pres_rels = pres_rels.replace(
                "</Relationships>",
                '<Relationship Id="rId%d" Type="http://schemas.openxmlformats.org/'
                "officeDocument/2006/relationships/slide\" Target=\"slides/slide%d.xml\"/>"
                % (n + 1, n)
                + "</Relationships>",
            )
        ct = ct.replace(
            "</Types>",
            '<Override PartName="/ppt/slides/slide%d.xml" ContentType="application/vnd.'
            "openxmlformats-officedocument.presentationml.slide+xml\"/>" % n
            + "</Types>",
        )

    data["ppt/presentation.xml"] = pres.replace(
        '<p:sldId id="p:256" r:id="rId2"/>', "".join(sld_ids)
    ).encode("ascii")
    data["ppt/_rels/presentation.xml.rels"] = pres_rels.encode("ascii")
    data["[Content_Types].xml"] = ct.encode("ascii")
    return _zip_bytes(data)


# --- xlsx: built from scratch, inlineStr cells ---------------------------------


def _xlsx_bytes():
    """Build a 3-sheet xlsx: [Content_Types], _rels, workbook + 3 sheets.

    inlineStr cells (no sharedStrings), no styles.xml -- both verified against
    the sidecar's openpyxl/pandas path. Row 1 is the pandas header row, so
    each sheet gets an explicit header row + 2 data rows; one data cell carries
    a long filler so each sheet's unit is >=200 chars (no cross-sheet
    coalesce).
    """
    sheet_names = ["chunkqA", "chunkqB", "chunkqC"]
    data = {
        "[Content_Types].xml": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.'
            "openxmlformats-officedocument.spreadsheetml.sheet.main+xml\"/>"
            + "".join(
                '<Override PartName="/xl/worksheets/sheet%d.xml"'
                ' ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
                % (i + 1)
                for i in range(3)
            )
            + "</Types>"
        ),
        "_rels/.rels": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
            'relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>'
        ),
        "xl/workbook.xml": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            "<sheets>%s</sheets></workbook>"
            % "".join(
                '<sheet name="%s" sheetId="%d" r:id="rId%d"/>' % (name, i + 1, i + 1)
                for i, name in enumerate(sheet_names)
            )
        ),
        "xl/_rels/workbook.xml.rels": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            + "".join(
                '<Relationship Id="rId%d" Type="http://schemas.openxmlformats.org/officeDocument/'
                "2006/relationships/worksheet\" Target=\"worksheets/sheet%d.xml\"/>" % (i + 1, i + 1)
                for i in range(3)
            )
            + "</Relationships>"
        ),
    }
    for i, name in enumerate(sheet_names, 1):
        marker = "chunkq-xlsx-s%s" % name[-1]
        cells = [
            ("A1", "marker"),
            ("B1", "value"),
            ("A2", "%s %s" % (marker, _word(i))),
            ("B2", _filler(150, seed=80 + i)),
            ("A3", "note %d" % i),
            ("B3", _filler(60, seed=90 + i)),
        ]
        rows_xml = "".join(
            '<row r="%d">%s</row>'
            % (
                r,
                "".join(
                    '<c r="%s" t="inlineStr"><is><t>%s</t></is></c>' % (cell, text)
                    # cells holds 2 entries per row: [2*(r-1) : 2*r]
                    for cell, text in cells[2 * (r - 1) : 2 * r]
                ),
            )
            for r in range(1, 4)
        )
        data["xl/worksheets/sheet%d.xml" % i] = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            "<sheetData>%s</sheetData></worksheet>" % rows_xml
        )
    return _zip_bytes(data)


# --- zip helpers ---------------------------------------------------------------


def _zip_bytes(data):
    """Deterministic zip: fixed member order (sorted) + fixed mtimes."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name in sorted(data):
            info = zipfile.ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0))
            z.writestr(info, data[name])
    return buf.getvalue()


def _clone_zip(path, replacements):
    with zipfile.ZipFile(path) as src:
        data = {n: src.read(n) for n in src.namelist()}
    data.update(replacements)
    return _zip_bytes(data)


# --- build + manifest ----------------------------------------------------------

ALL_TYPES = ["md", "html", "docx", "pdf", "pptx", "xlsx", "txt", "json", "log", "tex"]


def build(out_dir, types):
    """Write the fixtures into out_dir; return the manifest dict."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    sec = _sections()
    manifest = {}

    def write(name, blob):
        (out_dir / name).write_bytes(blob if isinstance(blob, bytes) else blob.encode("ascii"))

    if "md" in types:
        write("chunkq-md.md", _md_text(sec))
        manifest["md"] = {
            "file": "chunkq-md.md",
            "mode": "headers",
            "sections": [
                {"marker": "chunkq-md-s%d" % i, "level": sec[i - 1]["level"],
                 **({"coalesce_with": "chunkq-md-s7"} if i == 8 else {})}
                for i in range(1, 9)
            ],
        }

    if "html" in types:
        write("chunkq-html.html", _html_text(sec))
        manifest["html"] = {
            "file": "chunkq-html.html",
            "mode": "headers",
            "sections": [
                {"marker": "chunkq-html-s%d" % i, "level": sec[i - 1]["level"],
                 **({"coalesce_with": "chunkq-html-s7"} if i == 8 else {})}
                for i in range(1, 9)
            ],
        }

    if "docx" in types:
        write("chunkq-docx.docx", _docx_bytes(sec))
        manifest["docx"] = {
            "file": "chunkq-docx.docx",
            "mode": "headers",
            "sections": [
                {"marker": "chunkq-docx-s%d" % i, "level": sec[i - 1]["level"],
                 **({"coalesce_with": "chunkq-docx-s7"} if i == 8 else {})}
                for i in range(1, 9)
            ],
        }

    if "pdf" in types:
        write("chunkq-pdf.pdf", _pdf_bytes())
        manifest["pdf"] = {
            "file": "chunkq-pdf.pdf",
            "mode": "pages",
            "sections": [{"marker": "chunkq-pdf-p%d" % p, "page": p} for p in range(1, 5)],
        }

    if "pptx" in types:
        write("chunkq-pptx.pptx", _pptx_bytes())
        manifest["pptx"] = {
            "file": "chunkq-pptx.pptx",
            "mode": "pages",
            "sections": [{"marker": "chunkq-pptx-s%d" % n, "page": n} for n in range(1, 5)],
        }

    if "xlsx" in types:
        write("chunkq-xlsx.xlsx", _xlsx_bytes())
        manifest["xlsx"] = {
            "file": "chunkq-xlsx.xlsx",
            "mode": "pages",
            "sections": [
                {"marker": "chunkq-xlsx-s%s" % c, "page": i + 1, "sheet": "chunkq%s" % c}
                for i, c in enumerate("ABC")
            ],
        }

    if "txt" in types:
        write("chunkq-txt.txt", _txt_text())
        manifest["txt"] = {
            "file": "chunkq-txt.txt",
            "mode": "single",
            "sections": [{"marker": "chunkq-txt-s1"}, {"marker": "chunkq-txt-s2"}],
        }

    if "json" in types:
        write("chunkq-json.json", _json_text())
        manifest["json"] = {
            "file": "chunkq-json.json",
            "mode": "single",
            "sections": [{"marker": "chunkq-json-s1"}],
        }

    if "log" in types:
        write("chunkq-log.log", _log_text())
        manifest["log"] = {
            "file": "chunkq-log.log",
            "mode": "single",
            "sections": [{"marker": "chunkq-log-s1"}],
        }

    if "tex" in types:
        write("chunkq-tex.tex", _tex_text())
        manifest["tex"] = {
            "file": "chunkq-tex.tex",
            "mode": "single",
            "sections": [{"marker": "chunkq-tex-s1"}],
        }

    return manifest


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Generate deterministic chunk-quality fixtures + manifest."
    )
    parser.add_argument(
        "--out", default="gdrive/.tests/chunkq", help="output directory (default: gdrive/.tests/chunkq)"
    )
    parser.add_argument(
        "--types",
        default=",".join(ALL_TYPES),
        help="comma-separated subset of: %s" % ",".join(ALL_TYPES),
    )
    args = parser.parse_args(argv)
    types = [t.strip() for t in args.types.split(",") if t.strip()]
    unknown = [t for t in types if t not in ALL_TYPES]
    if unknown:
        parser.error("unknown type(s): %s" % ",".join(unknown))
    manifest = build(args.out, types)
    json.dump({"out": args.out, "types": types, "files": manifest}, sys.stdout, indent=1)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())