#!/usr/bin/env python3
"""Fetch a cited work's full text as plain text with page markers, for the fine pass of the citation check.

  fetch_fulltext.py --arxiv 2401.00505 --out .workflow/citation_notes/Key.txt
  fetch_fulltext.py --pdf ./reference_pdfs/Key.pdf --out .workflow/citation_notes/Key.txt

The PDF is read with PyMuPDF and never shown to a model; agents read only the extracted text, which marks
each page as `=== page N ===` so a verdict can cite where its evidence is.

Exit codes: 0 = text written, 1 = not available (network, missing file, no text layer), 2 = usage error.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tempfile
import urllib.request
from pathlib import Path

ARXIV_RE = re.compile(r"^(\d{4}\.\d{4,5}|[a-z\-]+(\.[A-Z]{2})?/\d{7})(v\d+)?$", re.I)
MIN_TEXT_CHARS = 500


def extract(pdf: Path, out: Path) -> int:
    import fitz  # PyMuPDF

    with fitz.open(str(pdf)) as doc:
        parts = [f"=== page {i} ===\n{page.get_text()}" for i, page in enumerate(doc, 1)]
    text = "\n".join(parts)
    if len(re.sub(r"\s+", "", text)) < MIN_TEXT_CHARS:
        return 0
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    return len(parts)


def main() -> int:
    ap = argparse.ArgumentParser(description="Extract a cited work's full text (arXiv id or local PDF) with page markers.")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--arxiv", help="arXiv identifier, e.g. 2401.00505 or cond-mat/0101001")
    src.add_argument("--pdf", help="path to a local PDF of the work")
    ap.add_argument("--out", required=True, help="where to write the extracted text")
    ap.add_argument("--timeout", type=int, default=60, help="network timeout in seconds (arXiv only)")
    a = ap.parse_args()
    out = Path(a.out)
    try:
        if a.pdf:
            pdf = Path(a.pdf)
            if not pdf.is_file():
                print(json.dumps({"ok": False, "error": f"no such PDF: {pdf}"}))
                return 1
            pages = extract(pdf, out)
        else:
            aid = a.arxiv.strip()
            if aid.lower().startswith("arxiv:"):
                aid = aid[6:]
            if not ARXIV_RE.match(aid):
                print(json.dumps({"ok": False, "error": f"not an arXiv identifier: {a.arxiv}"}), file=sys.stderr)
                return 2
            req = urllib.request.Request(f"https://arxiv.org/pdf/{aid}", headers={"User-Agent": "write-paper-citation-check/4"})
            with urllib.request.urlopen(req, timeout=a.timeout) as resp:
                data = resp.read()
            if not data.startswith(b"%PDF"):
                print(json.dumps({"ok": False, "error": "arXiv did not return a PDF"}))
                return 1
            with tempfile.TemporaryDirectory() as td:
                pdf = Path(td) / "paper.pdf"
                pdf.write_bytes(data)
                pages = extract(pdf, out)
    except Exception as exc:  # network, corrupt PDF, missing PyMuPDF
        print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}))
        return 1
    if not pages:
        print(json.dumps({"ok": False, "error": "no text layer (scanned PDF?)"}))
        return 1
    print(json.dumps({"ok": True, "out": str(out), "pages": pages}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
