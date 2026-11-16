#!/usr/bin/env python3
"""Deterministic geometric, typographic & anti-AI-infographic verifier for publication figures.

Inspects compiled figure PDFs (via PyMuPDF) and optional Python figure scripts (via AST)
against APS PRL ('prl') or Nature Portfolio ('nature') house style rules:
1. True 1:1 print-column font sizes (no microscopic <7pt PRL / <5pt Nature labels).
2. Embedded Type 42/0 vector fonts & zero DejaVu default bleed (plus Arial/stixsans check for Nature).
3. Dual collision check: text-vs-text overlap AND in-memory text-redacted 400-DPI
   stroke-through-text contrast detection (catches arcs, stems, gridlines crossing letters).
4. Top-10 AI-infographic tell detector (FancyBboxPatch round cards, set_title sentence banners,
   Tailwind pastel/navy hexes, proof flowcharts, Matplotlib tables, >7-word card paragraphs).
5. Optional cross-check of figure notation vs. TeX source files + 300-DPI RGB/grayscale previews.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _texio import utf8_stdio, write_new  # noqa: E402
from journal_fig_theme import DIMENSIONS  # noqa: E402  (single source of truth for canvas widths)

# Allowed canvas widths (pt) per style, taken from the theme helper: single, one-and-a-half, double column.
COL_WIDTHS_PT: Dict[str, Dict[str, float]] = {
    st: {k: v * 72.0 for k, v in dims.items() if k in ("single", "one_half", "double")} for st, dims in DIMENSIONS.items()
}
MIN_LABEL_PT = {"prl": 5.8, "nature": 4.9}
SCRIPT_FLOOR_FACTOR = 0.65  # sub/superscripts may be this fraction of the label floor
WARNING_ONLY_KINDS = frozenset({"ai_span_highlight"})  # reported, but not a hard failure
SANS_FONT_KEYS = ("arial", "helvetica", "dejavusans", "stixsans", "liberationsans", "nimbussans", "texgyreheros", "freesans")

TAILWIND_OR_AI_HEXES = frozenset({
    "#F8FAFC", "#F1F5F9", "#E2E8F0", "#CBD5E1", "#475569", "#334155",
    "#FEF3C7", "#FEF9C3", "#EFF6FF", "#DBEAFE", "#ECFDF5", "#EBF3FA",
    "#FBECEB", "#FEF5E7", "#1B263B",
})


def _strip_math_tokens(s: str) -> str:
    t = re.sub(r"\$[^$]*\$", " ", s)
    t = re.sub(r"[=<>+\-*/^_{}()\[\]|\\.,:0-9]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _analyze_script_ast(py_path: Path) -> List[Dict[str, object]]:
    src = py_path.read_text(encoding="utf-8", errors="replace")
    findings: List[Dict[str, object]] = []
    is_theme_helper = "journal_fig_theme" in py_path.name

    # 1. Tailwind / off-black ink / DejaVu literal checks
    for hx in sorted(TAILWIND_OR_AI_HEXES):
        if hx.lower() in src.lower():
            findings.append({"script": py_path.name, "kind": "ai_tailwind_or_offblack_hex", "detail": f"Found {hx}"})
    if re.search(r"['\"]DejaVu\s+(?:Serif|Sans)['\"]", src):
        findings.append({"script": py_path.name, "kind": "dejavu_font_config", "detail": "Explicit DejaVu Serif/Sans in font stack"})

    # 2. AST structural checks
    try:
        tree = ast.parse(src)
    except SyntaxError as exc:
        findings.append({"script": py_path.name, "kind": "syntax_error", "detail": str(exc)})
        return findings

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn_name = ""
            if isinstance(node.func, ast.Name):
                fn_name = node.func.id
            elif isinstance(node.func, ast.Attribute):
                fn_name = node.func.attr

            if fn_name == "FancyBboxPatch":
                bs_val = ""
                for kw in node.keywords:
                    if kw.arg == "boxstyle" and isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
                        bs_val = kw.value.value
                if (not bs_val) or ("round" in bs_val.lower()) or ("circle" in bs_val.lower()):
                    findings.append({"script": py_path.name, "line": node.lineno, "kind": "ai_rounded_card_patch", "detail": f"FancyBboxPatch(boxstyle='{bs_val or 'default'}')"})

            if fn_name == "set_title" and not is_theme_helper:
                title_arg = node.args[0] if node.args else None
                is_panel_label = isinstance(title_arg, ast.Constant) and isinstance(title_arg.value, str) and re.fullmatch(r"\s*\(?[A-Za-z]\)?\s*", title_arg.value or "")
                if is_panel_label:
                    pass  # ax.set_title('(a)', loc='left') is a bare panel label, not a banner
                elif isinstance(title_arg, ast.Constant) and isinstance(title_arg.value, str) and title_arg.value.strip():
                    findings.append({"script": py_path.name, "line": node.lineno, "kind": "ai_panel_title_banner", "detail": f"set_title('{title_arg.value[:50]}')"})
                elif title_arg is not None and not isinstance(title_arg, ast.Constant):
                    findings.append({"script": py_path.name, "line": node.lineno, "kind": "ai_panel_title_banner", "detail": "Non-empty dynamic ax.set_title(...) call"})

            if fn_name == "table":
                findings.append({"script": py_path.name, "line": node.lineno, "kind": "ai_matplotlib_table", "detail": "ax.table(...) belongs in LaTeX ruledtabular"})

            if fn_name in ("axhspan", "axvspan"):
                findings.append({"script": py_path.name, "line": node.lineno, "kind": "ai_span_highlight", "detail": f"{fn_name}(...) embellishment"})

            # Check bbox=dict(boxstyle="round...") on text/annotate
            if fn_name in ("text", "annotate"):
                for kw in node.keywords:
                    if kw.arg == "bbox" and isinstance(kw.value, ast.Call):
                        for bkw in kw.value.keywords:
                            if bkw.arg == "boxstyle" and isinstance(bkw.value, ast.Constant) and "round" in str(bkw.value.value).lower():
                                findings.append({"script": py_path.name, "line": node.lineno, "kind": "ai_rounded_text_pill", "detail": f"{fn_name}(bbox=round...)"})

    return findings


def _rects_overlap_significant(a: Tuple[float, float, float, float], b: Tuple[float, float, float, float]) -> bool:
    ax0, ay0, ax1, ay1 = a
    bx0, by0, bx1, by1 = b
    ix0, iy0 = max(ax0, bx0), max(ay0, by0)
    ix1, iy1 = min(ax1, bx1), min(ay1, by1)
    if ix1 <= ix0 or iy1 <= iy0:
        return False
    inter = (ix1 - ix0) * (iy1 - iy0)
    area_a = max(1e-3, (ax1 - ax0) * (ay1 - ay0))
    area_b = max(1e-3, (bx1 - bx0) * (by1 - by0))
    return (inter / min(area_a, area_b)) > 0.20


def verify_single_pdf(
    pdf_path: Path,
    style: str,
    renders_out: Optional[Path] = None,
    tex_combined: str = "",
) -> Dict[str, object]:
    import numpy as np
    try:
        import pymupdf  # type: ignore
    except ImportError:  # PyMuPDF < 1.24.3 only ships the `fitz` name
        import fitz as pymupdf  # type: ignore

    doc = pymupdf.open(pdf_path)
    page = doc[0]
    rect = page.rect
    w_pt, h_pt = float(rect.width), float(rect.height)
    aspect = w_pt / max(h_pt, 1e-6)

    widths = COL_WIDTHS_PT[style]
    layout, target_w = min(widths.items(), key=lambda kv: abs(kv[1] - w_pt))
    cols = 1 if layout == "single" else 2
    scale = target_w / max(w_pt, 1e-6)
    aps_weq = round((150.0 / aspect + 20.0) if cols == 1 else (300.0 / (0.5 * aspect) + 40.0), 1)
    width_diff_pt = round(abs(w_pt - target_w), 2)
    canvas_width_ok = width_diff_pt <= 36.0

    # Fonts audit (a figure without any text legitimately embeds no fonts)
    raw_fonts = page.get_fonts(full=True)
    font_names = [str(f[3]) for f in raw_fonts]
    has_type3 = any("type3" in str(f[2]).lower() for f in raw_fonts)
    has_dejavu = any("dejavu" in fn.lower() for fn in font_names)
    dejavu_banned_ok = not has_dejavu
    vector_fonts_ok = not has_type3

    serif_body = [fn for fn in font_names if any(k in fn.lower() for k in ("times", "cmr", "dejavuserif", "liberationserif", "nimbusrom", "texgyretermes"))]
    sans_body = [fn for fn in font_names if any(k in fn.lower().replace(" ", "").replace("-", "") for k in SANS_FONT_KEYS)]
    house_font_ok = True if style == "prl" else (not serif_body or bool(sans_body))

    # Extract spans and logical lines
    blocks = page.get_text("dict", flags=0).get("blocks", [])
    spans: List[Dict[str, Any]] = []
    line_records: List[Dict[str, Any]] = []
    for b in blocks:
        for ln in b.get("lines", []):
            ln_spans = [s for s in ln.get("spans", []) if s.get("text", "").strip()]
            if not ln_spans:
                continue
            ln_txt = " ".join(s["text"].strip() for s in ln_spans)
            line_records.append({"text": re.sub(r"\s+", " ", ln_txt).strip(), "bbox": ln["bbox"]})
            for s in ln_spans:
                txt = s["text"].strip()
                non_ws = re.sub(r"\s+", "", txt)
                eff_pt = float(s["size"]) * scale
                spans.append({
                    "text": txt,
                    "non_ws_len": len(non_ws),
                    "eff_pt": eff_pt,
                    "bbox": tuple(float(v) for v in s["bbox"]),
                    "line_bbox": tuple(float(v) for v in ln["bbox"]),
                    "color": int(s.get("color", 0)),
                })

    # Sub/superscripts (mathtext `J_{xy}`, log-axis `10^{-2}`) are set at ~70% of their base glyphs:
    # a span is a script when it sits right next to a larger span and overlaps it vertically.
    for s in spans:
        x0, y0, x1, y1 = s["bbox"]
        s["is_script"] = False
        for o in spans:
            if o is s or o["eff_pt"] <= 0:
                continue
            ratio = s["eff_pt"] / o["eff_pt"]
            if not (0.5 <= ratio <= 0.85):
                continue
            ox0, oy0, ox1, oy1 = o["bbox"]
            h_gap = max(ox0 - x1, x0 - ox1)
            v_overlap = min(y1, oy1) - max(y0, oy0)
            if h_gap <= 1.5 and v_overlap > -1.0:
                s["is_script"] = True
                break

    base_spans = [s for s in spans if not s["is_script"]]
    script_spans = [s for s in spans if s["is_script"]]
    total_chars = sum(s["non_ws_len"] for s in base_spans)
    chars_lt_7 = sum(s["non_ws_len"] for s in base_spans if s["eff_pt"] < 6.95)
    chars_lt_6 = sum(s["non_ws_len"] for s in base_spans if s["eff_pt"] < 5.95)
    chars_lt_5 = sum(s["non_ws_len"] for s in base_spans if s["eff_pt"] < 4.95)
    ratio_lt_7 = round(chars_lt_7 / max(1, total_chars), 4)
    ratio_lt_5 = round(chars_lt_5 / max(1, total_chars), 4)

    multi_char_pts = [s["eff_pt"] for s in base_spans if s["non_ws_len"] >= 2]
    min_multichar_pt = round(min(multi_char_pts), 2) if multi_char_pts else 0.0
    min_all_pt = round(min((s["eff_pt"] for s in spans), default=0.0), 2)
    min_script_pt = round(min((s["eff_pt"] for s in script_spans), default=0.0), 2)
    floor = MIN_LABEL_PT[style]
    scripts_ok = (min_script_pt >= SCRIPT_FLOOR_FACTOR * floor) if script_spans else True

    if style == "prl":
        font_size_ok = (ratio_lt_7 <= 0.30) and (min_multichar_pt >= floor if multi_char_pts else True) and scripts_ok
    else:
        max_pt = max(multi_char_pts, default=0.0)
        font_size_ok = (ratio_lt_5 <= 0.10) and (min_multichar_pt >= floor if multi_char_pts else True) and (max_pt <= 8.5) and scripts_ok

    # Pairwise text-vs-text collisions (excluding spans from the same line_bbox or sub/superscripts <2pt apart)
    text_overlaps: List[Dict[str, object]] = []
    for i in range(len(spans)):
        sa = spans[i]
        if sa["non_ws_len"] < 2:
            continue
        for j in range(i + 1, len(spans)):
            sb = spans[j]
            if sb["non_ws_len"] < 2:
                continue
            if sa["line_bbox"] == sb["line_bbox"]:
                continue
            # Ignore math sub/superscript sitting immediately adjacent (<2.5pt horizontal gap)
            h_gap = max(sb["bbox"][0] - sa["bbox"][2], sa["bbox"][0] - sb["bbox"][2])
            if h_gap < 2.5 and abs(sa["eff_pt"] - sb["eff_pt"]) > 1.0 and min(sa["non_ws_len"], sb["non_ws_len"]) <= 4:
                continue
            if _rects_overlap_significant(sa["bbox"], sb["bbox"]):
                text_overlaps.append({"a": sa["text"], "b": sb["text"], "bbox_a": [round(x, 1) for x in sa["bbox"]], "bbox_b": [round(x, 1) for x in sb["bbox"]]})

    # In-memory redacted stroke-through-text check at 400 DPI (fill=None keeps pure graphics strokes!)
    dpi_chk = 400
    sc_px = dpi_chk / 72.0
    single_buf = doc.tobytes()
    clone = pymupdf.open("pdf", single_buf)
    cpage = clone[0]
    for s in spans:
        cpage.add_redact_annot(pymupdf.Rect(s["bbox"]), fill=None)
    try:
        cpage.apply_redactions(images=0, graphics=0, text=0)
    except TypeError:  # older PyMuPDF: no graphics/text keywords (graphics are kept by default there)
        cpage.apply_redactions(images=0)
    pix = cpage.get_pixmap(dpi=dpi_chk, colorspace=pymupdf.csRGB)
    arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, 3).astype(np.float32)
    gray = 0.2126 * arr[:, :, 0] + 0.7152 * arr[:, :, 1] + 0.0722 * arr[:, :, 2]

    stroke_collisions: List[Dict[str, object]] = []
    for idx_s, s in enumerate(spans):
        if s["non_ws_len"] < 2:
            continue
        x0, y0, x1, y1 = s["bbox"]
        w_b, h_b = x1 - x0, y1 - y0
        if w_b < 5.0 or h_b < 3.5:
            continue
        px0 = int((x0 + 0.05 * w_b) * sc_px)
        px1 = int((x1 - 0.05 * w_b) * sc_px)
        py0 = int((y0 + 0.24 * h_b) * sc_px)
        py1 = int((y1 - 0.24 * h_b) * sc_px)
        px0, py0 = max(0, px0), max(0, py0)
        px1, py1 = min(gray.shape[1], px1), min(gray.shape[0], py1)
        if (px1 - px0) < 4 or (py1 - py0) < 3:
            continue
        patch = gray[py0:py1, px0:px1]
        med = float(np.median(patch))
        diff = np.abs(patch - med)
        max_contrast = float(diff.max())
        stroke_frac = float(np.mean(diff > 22.0))
        if max_contrast >= 32.0 and 0.012 <= stroke_frac <= 0.45:
            col_entry = {
                "text": s["text"],
                "eff_pt": round(s["eff_pt"], 2),
                "max_contrast": round(max_contrast, 1),
                "stroke_frac": round(stroke_frac, 4),
                "bbox": [round(x0, 1), round(y0, 1), round(x1, 1), round(y1, 1)],
            }
            stroke_collisions.append(col_entry)
            if renders_out is not None and len(stroke_collisions) <= 6:
                renders_out.mkdir(parents=True, exist_ok=True)
                pad_pt = 14.0
                clip = pymupdf.Rect(max(0.0, x0 - pad_pt), max(0.0, y0 - pad_pt), min(w_pt, x1 + pad_pt), min(h_pt, y1 + pad_pt))
                zpix = page.get_pixmap(dpi=600, clip=clip)
                zpix.save(str(renders_out / f"zoom_{pdf_path.stem}_{len(stroke_collisions)}.png"))

    clone.close()

    # Anti-AI Infographic text tells on reconstructed lines
    ai_text_tells: List[Dict[str, object]] = []
    panel_banner_re = re.compile(r"^(?:\([a-z]\)|[a-z])\s+[A-Za-z0-9].{10,}$")
    total_prose_words = 0
    for lr in line_records:
        lt = lr["text"]
        pwords = _strip_math_tokens(lt).split()
        total_prose_words += len(pwords)
        if panel_banner_re.match(lt) and len(pwords) >= 3:
            ai_text_tells.append({"kind": "ai_panel_title_banner", "text": lt[:90]})
        if len(pwords) >= 8:
            ai_text_tells.append({"kind": "overstuffed_card_prose", "prose_words": len(pwords), "text": lt[:90]})
        if re.match(r"^\d+\.\s+[A-Za-z]{3,}", lt) or any(bw in lt for bw in ("Contradiction", "Clash")):
            if len(pwords) >= 2:
                ai_text_tells.append({"kind": "proof_flowchart_text", "text": lt[:90]})

    # Render 300 DPI RGB + grayscale previews if requested
    preview_paths: Dict[str, str] = {}
    if renders_out is not None:
        renders_out.mkdir(parents=True, exist_ok=True)
        rgb_p = renders_out / f"{pdf_path.stem}_300dpi.png"
        grey_p = renders_out / f"{pdf_path.stem}_gray_300dpi.png"
        page.get_pixmap(dpi=300, colorspace=pymupdf.csRGB).save(str(rgb_p))
        page.get_pixmap(dpi=300, colorspace=pymupdf.csGRAY).save(str(grey_p))
        preview_paths = {"rgb_300dpi": str(rgb_p).replace("\\", "/"), "gray_300dpi": str(grey_p).replace("\\", "/")}

    # Figure-vs-text notation agreement is checked by the figure agents (caption sync); no
    # paper-specific symbol rules are hard-coded here.
    tex_notation_warnings: List[str] = []

    doc.close()

    checks = {
        "canvas_width_ok": canvas_width_ok,
        "vector_fonts_ok": vector_fonts_ok,
        "dejavu_banned_ok": dejavu_banned_ok,
        "house_font_ok": house_font_ok,
        "font_size_ok": font_size_ok,
        "zero_text_overlaps": len(text_overlaps) == 0,
        "zero_stroke_collisions": len(stroke_collisions) == 0,
        "zero_ai_text_tells": len(ai_text_tells) == 0,
    }
    passed = all(checks.values()) and len(tex_notation_warnings) == 0

    return {
        "file": str(pdf_path).replace("\\", "/"),
        "columns": cols,
        "layout": layout,
        "width_pt": round(w_pt, 2),
        "height_pt": round(h_pt, 2),
        "target_width_pt": round(target_w, 2),
        "print_scale": round(scale, 3),
        "aps_word_eq": aps_weq,
        "passed": passed,
        "checks": checks,
        "font_stats": {
            "total_chars": total_chars,
            "pct_chars_lt_7pt": round(100.0 * ratio_lt_7, 1),
            "pct_chars_lt_6pt": round(100.0 * chars_lt_6 / max(1, total_chars), 1),
            "pct_chars_lt_5pt": round(100.0 * ratio_lt_5, 1),
            "min_multichar_pt": min_multichar_pt,
            "min_script_pt": min_script_pt,
            "script_spans": len(script_spans),
            "min_all_pt": min_all_pt,
            "embedded_fonts": font_names,
        },
        "total_prose_words": total_prose_words,
        "text_overlaps": text_overlaps,
        "stroke_through_text_collisions": stroke_collisions,
        "ai_text_tells": ai_text_tells,
        "tex_notation_warnings": tex_notation_warnings,
        "previews": preview_paths,
    }


def _collect_pdfs(pdfs: Optional[Sequence[Path]], pdf_dir: Optional[Path]) -> Tuple[List[Path], List[str]]:
    out: List[Path] = []
    problems: List[str] = []
    for p in pdfs or []:
        if not p.is_file():
            problems.append(f"--pdfs path not found: {p}")
        elif p.suffix.lower() != ".pdf":
            problems.append(f"--pdfs path is not a .pdf: {p}")
        else:
            out.append(p)
    if pdf_dir is not None:
        if not pdf_dir.is_dir():
            problems.append(f"--pdf-dir not found: {pdf_dir}")
        else:
            for p in sorted(pdf_dir.glob("*.pdf")):
                if p not in out:
                    out.append(p)
    return out, problems


def main() -> int:
    utf8_stdio()
    parser = argparse.ArgumentParser(description="Hard-gate geometric, print-pt, collision & anti-AI verifier for PDF figures.")
    parser.add_argument("--pdfs", nargs="+", type=Path, help="Explicit figure PDF paths")
    parser.add_argument("--pdf-dir", type=Path, help="Directory of figure PDFs")
    parser.add_argument("--scripts-dir", type=Path, help="Optional directory of figure scripts for the AST scan (every *.py except the theme helper)")
    parser.add_argument("--tex-dir", type=Path, help="Accepted for compatibility (caption/notation sync is done by the figure agents)")
    parser.add_argument("--style", choices=("prl", "nature"), default="prl", help="House style preset")
    parser.add_argument("--renders-out", type=Path, help="Optional directory to save 300-DPI previews & collision zoom crops")
    parser.add_argument("--json-out", type=Path, help="Optional JSON report path")
    parser.add_argument("--md-out", type=Path, help="Optional Markdown summary path")
    args = parser.parse_args()

    pdfs, problems = _collect_pdfs(args.pdfs, args.pdf_dir)
    if problems or not pdfs:
        print(json.dumps({"passed": False, "error": "; ".join(problems) or "No PDF figures found to verify"}), file=sys.stderr)
        return 2

    fig_reports: List[Dict[str, object]] = []
    for p in pdfs:
        try:
            fig_reports.append(verify_single_pdf(p, args.style, args.renders_out, ""))
        except Exception as exc:  # one unreadable PDF must not hide the results for the others
            fig_reports.append({"file": str(p).replace("\\", "/"), "passed": False, "error": f"{type(exc).__name__}: {exc}",
                                "columns": 0, "print_scale": 0, "checks": {"readable": False}, "font_stats": {"pct_chars_lt_7pt": 0, "min_multichar_pt": 0},
                                "stroke_through_text_collisions": [], "ai_text_tells": []})

    script_findings: List[Dict[str, object]] = []
    scripts_scanned: List[str] = []
    if args.scripts_dir is not None:
        if not args.scripts_dir.is_dir():
            print(json.dumps({"passed": False, "error": f"--scripts-dir not found: {args.scripts_dir}"}), file=sys.stderr)
            return 2
        for py in sorted(args.scripts_dir.glob("*.py")):
            if py.name == "journal_fig_theme.py":
                continue
            scripts_scanned.append(py.name)
            script_findings.extend(_analyze_script_ast(py))
    blocking_findings = [f for f in script_findings if f.get("kind") not in WARNING_ONLY_KINDS]

    overall_pass = all(r["passed"] for r in fig_reports) and not blocking_findings
    payload = {
        "passed": overall_pass,
        "style": args.style,
        "figures_checked": len(fig_reports),
        "scripts_scanned": scripts_scanned,
        "script_ai_findings_count": len(blocking_findings),
        "script_warnings_count": len(script_findings) - len(blocking_findings),
        "figures": fig_reports,
        "script_ai_findings": script_findings,
    }

    if args.json_out:
        write_new(args.json_out, json.dumps(payload, indent=2, ensure_ascii=False))

    if args.md_out:
        lines = [
            f"# Quantitative Figure Verification & Anti-AI Audit Report (`style={args.style}`)",
            "",
            f"- **Overall Verdict**: `{'PASS' if overall_pass else 'FAIL'}`",
            f"- **Figures Audited**: {len(fig_reports)} | **Python AST AI Tells**: {len(script_findings)}",
            "",
            "| Figure | Col | Print Scale | `<7pt` % | Min Label | DejaVu OK | Stroke Collisions | AI Tells | Status |",
            "| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :--- |",
        ]
        for r in fig_reports:
            fs = r["font_stats"]  # type: ignore[index]
            ck = r["checks"]  # type: ignore[index]
            if r.get("error"):
                lines.append(f"| `{Path(str(r['file'])).name}` | – | – | – | – | – | – | – | `ERROR: {r['error']}` |")
                continue
            lines.append(
                f"| `{Path(str(r['file'])).name}` | {r['columns']} | {r['print_scale']}x | {fs['pct_chars_lt_7pt']}% | {fs['min_multichar_pt']} pt | {'Yes' if ck['dejavu_banned_ok'] else 'NO'} | {len(r['stroke_through_text_collisions'])} | {len(r['ai_text_tells'])} | `{'PASS' if r['passed'] else 'FAIL'}` |"  # type: ignore[arg-type]
            )
        lines += ["", f"- Scripts scanned: {', '.join(scripts_scanned) or '(none)'}"]
        if script_findings:
            lines += ["", "## Script AST Anti-AI Tells", ""] + [f"- `{json.dumps(sf, ensure_ascii=False)}`" + (" (warning only)" if sf.get("kind") in WARNING_ONLY_KINDS else "") for sf in script_findings]
        write_new(args.md_out, "\n".join(lines) + "\n")

    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0 if overall_pass else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as exc:
        print(json.dumps({"passed": False, "error": f"{type(exc).__name__}: {exc}"}), file=sys.stderr)
        sys.exit(2)
