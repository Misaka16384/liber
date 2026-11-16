#!/usr/bin/env python3
"""Deterministic LaTeX + BibTeX/Biber compiler, layout/leak inspector, and single-file arXiv sanitizer (v2).

Supports dual main.tex + supplement.tex compilation, log parsing that survives TeX's 79-column
line wrapping (errors, undefined refs/cites, multiply-defined labels, >10pt overfull, hyperref
bookmark warnings), macro \\par & PyMuPDF rendered-page inspection (APS page/word budget,
mid-line run-in heading collisions, orphan last page), and arXiv flattening that removes
comments, `\\iffalse` blocks, `comment` environments and \\status{...}/\\stafile macros without
changing what TeX typesets.

Exit codes: 0 = clean, 1 = issues found (see JSON), 2 = tool/usage error.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _texio import find_comment_start, read_text, utf8_stdio, write_new  # noqa: E402

TEX_TIMEOUT_S = int(os.environ.get("WRITE_PAPER_TEX_TIMEOUT", "300"))


def _find_tex_bin(name: str) -> Optional[str]:
    env_bin = os.environ.get("TEXLIVE_BIN", "").strip()
    if env_bin:
        for ext in (".exe", ""):
            cand = Path(env_bin) / f"{name}{ext}"
            if cand.exists():
                return str(cand)
    found = shutil.which(name)
    if found:
        return found
    candidate_dirs: List[Path] = []
    if sys.platform.startswith("win"):
        for drive in ("C:", "D:", "E:", "F:"):
            tl_root = Path(f"{drive}\\texlive")
            if tl_root.exists():
                candidate_dirs.extend(sorted(tl_root.glob("*/bin/windows"), reverse=True))
                candidate_dirs.extend(sorted(tl_root.glob("*/bin/win32"), reverse=True))
        candidate_dirs.extend([
            Path.home() / "AppData" / "Local" / "Programs" / "MiKTeX" / "miktex" / "bin" / "x64",
            Path(r"C:\Program Files\MiKTeX\miktex\bin\x64"),
        ])
    elif sys.platform == "darwin":
        candidate_dirs.extend([Path("/Library/TeX/texbin"), Path("/opt/homebrew/bin"), Path("/usr/local/bin")])
    else:
        tl_unix = Path("/usr/local/texlive")
        if tl_unix.exists():
            candidate_dirs.extend(sorted(tl_unix.glob("*/bin/*"), reverse=True))
        candidate_dirs.extend([Path("/usr/bin"), Path("/usr/local/bin")])
    for cdir in candidate_dirs:
        if not cdir.exists():
            continue
        for ext in (".exe", ""):
            cand = cdir / f"{name}{ext}"
            if cand.exists():
                return str(cand)
    return None


def _run_cmd(cmd: List[str], cwd: Path) -> Tuple[int, str, str]:
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    try:
        res = subprocess.run(
            cmd,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            timeout=TEX_TIMEOUT_S,
            stdin=subprocess.DEVNULL,
        )
        return res.returncode, res.stdout, res.stderr
    except subprocess.TimeoutExpired:
        return 124, "", f"timeout after {TEX_TIMEOUT_S}s: {' '.join(cmd)}"
    except OSError as exc:
        return 127, "", f"could not run {cmd[0]}: {exc}"


# ---------------------------------------------------------------------------------------------
# Log parsing
# ---------------------------------------------------------------------------------------------

_MAX_PRINT_LINE = 79  # TeX Live default `max_print_line`
_FILE_LINE_ERR_RE = re.compile(r"^(.*?\.(?:tex|sty|cls|bbl|cfg|def|clo|ltx|aux|toc|out|fd)):(\d+): (.*)$")


def _unwrap_log_lines(log_text: str) -> List[str]:
    """Re-join lines that TeX hard-wrapped at `max_print_line` columns."""
    out: List[str] = []
    buf = ""
    for ln in log_text.splitlines():
        buf += ln
        if len(ln) >= _MAX_PRINT_LINE:
            continue
        out.append(buf)
        buf = ""
    if buf:
        out.append(buf)
    return out


def parse_latex_log(log_text: str) -> Dict[str, object]:
    latex_errors: List[str] = []
    undef_refs: List[str] = []
    undef_cites: List[str] = []
    multiply_defined: List[str] = []
    rerun_warnings: List[str] = []
    overfull_count = 0
    overfull_gt10pt: List[Dict[str, object]] = []
    float_warnings: List[str] = []
    hyperref_warnings: List[str] = []
    summary_flags: List[str] = []

    raw_lines = log_text.splitlines()
    lines = _unwrap_log_lines(log_text)
    overfull_re = re.compile(r"Overfull \\hbox \(([0-9.]+)pt too wide\)")

    for idx, line in enumerate(raw_lines):
        if line.startswith("! "):
            ctx = line
            if idx + 1 < len(raw_lines):
                ctx += " | " + raw_lines[idx + 1].strip()
            latex_errors.append(ctx)

    for line in lines:
        m = _FILE_LINE_ERR_RE.match(line)
        if m:
            latex_errors.append(line.strip())
        if re.search(r"(?:LaTeX|Package \w+) Warning: (?:Reference|Hyper reference) [`'].*undefined", line):
            undef_refs.append(line.strip())
        if re.search(r"(?:LaTeX|Package \w+) Warning: Citation [`'].*undefined", line) or "could not be found in database" in line:
            undef_cites.append(line.strip())
        if re.search(r"Warning: Label [`'].*multiply defined", line):
            multiply_defined.append(line.strip())
        if "There were undefined references" in line:
            summary_flags.append("undefined_references")
        if "There were undefined citations" in line or "Please (re)run Biber" in line:
            summary_flags.append("undefined_citations")
        if "There were multiply-defined labels" in line:
            summary_flags.append("multiply_defined_labels")
        if "Rerun to get" in line or "Label(s) may have changed" in line:
            rerun_warnings.append(line.strip())
        if line.startswith("Overfull \\hbox"):
            overfull_count += 1
            om = overfull_re.search(line)
            if om and float(om.group(1)) > 10.0:
                overfull_gt10pt.append({"pt": float(om.group(1)), "line": line.strip()})
        if "Float too large" in line or "Too many unprocessed floats" in line or "float specifier changed" in line:
            float_warnings.append(line.strip())
        if "Token not allowed in a PDF string" in line:
            hyperref_warnings.append(line.strip())

    # The end-of-run summary is authoritative even when the per-item warning was mangled.
    if "undefined_references" in summary_flags and not undef_refs:
        undef_refs.append("LaTeX Warning: There were undefined references (see .log)")
    if "undefined_citations" in summary_flags and not undef_cites:
        undef_cites.append("LaTeX Warning: There were undefined citations (see .log)")
    if "multiply_defined_labels" in summary_flags and not multiply_defined:
        multiply_defined.append("LaTeX Warning: There were multiply-defined labels (see .log)")

    seen: Set[str] = set()
    errs_dedup = [e for e in latex_errors if not (e in seen or seen.add(e))]
    return {
        "latex_errors": errs_dedup,
        "undefined_references": sorted(set(undef_refs)),
        "undefined_citations": sorted(set(undef_cites)),
        "multiply_defined_labels": sorted(set(multiply_defined)),
        "rerun_warnings": sorted(set(rerun_warnings)),
        "overfull_hbox_count": overfull_count,
        "overfull_gt10pt": overfull_gt10pt,
        "float_warnings": sorted(set(float_warnings)),
        "hyperref_warnings": sorted(set(hyperref_warnings)),
    }


def parse_bibtex_log(blg_text: str) -> Dict[str, object]:
    errors: List[str] = []
    warnings: List[str] = []
    for line in blg_text.splitlines():
        s = line.strip()
        if s.startswith("Warning--") or " WARN - " in s or s.startswith("WARN - "):
            warnings.append(s)
        elif any(tok in s for tok in ("I found no ", "I couldn't open ", "error message", "Illegal,", "Repeated entry", " ERROR - ", "FATAL")) or s.startswith("ERROR - "):
            errors.append(s)
    return {"bibtex_errors": errors, "bibtex_warnings": warnings}


# ---------------------------------------------------------------------------------------------
# arXiv sanitising
# ---------------------------------------------------------------------------------------------

def _read_balanced(text: str, pos: int, open_ch: str, close_ch: str) -> Optional[Tuple[str, int]]:
    n = len(text)
    if pos >= n or text[pos] != open_ch:
        return None
    d = 0
    i = pos
    while i < n:
        if text[i] == "\\" and i + 1 < n:
            i += 2
            continue
        if text[i] == open_ch:
            d += 1
        elif text[i] == close_ch:
            d -= 1
            if d == 0:
                return text[pos : i + 1], i + 1
        i += 1
    return None


def strip_macro_calls(text: str, macro_names: Tuple[str, ...] = ("status", "stafile")) -> str:
    """Strip \\status{...}... and \\stafile{...} balanced invocations from TeX text."""
    out = list(text)
    pat = re.compile(r"\\(?:" + "|".join(re.escape(m) for m in macro_names) + r")\b")
    n = len(text)
    for m in pat.finditer(text):
        prefix = text[max(0, m.start() - 30) : m.start()]
        if re.search(r"\\(?:re|provide)?newcommand\*?\{\s*$|\\def\s*$", prefix):
            continue
        j = m.end()
        while j < n and text[j] in " \t\r\n":
            j += 1
        while j < n and text[j] in "[{":
            oc = text[j]
            cc = "]" if oc == "[" else "}"
            blk = _read_balanced(text, j, oc, cc)
            if blk is None:
                break
            j = blk[1]
            while j < n and text[j] in " \t":
                j += 1
        line_start = text.rfind("\n", 0, m.start()) + 1
        line_end = text.find("\n", j)
        if line_end == -1:
            line_end = n
        before_on_line = text[line_start : m.start()].strip()
        after_on_line = text[j:line_end].strip()
        if not before_on_line and (not after_on_line or after_on_line.startswith("%")):
            end_del = min(n, line_end + (1 if line_end < n and text[line_end] == "\n" else 0))
            for k in range(line_start, end_del):
                out[k] = ""
        else:
            for k in range(m.start(), j):
                out[k] = ""
    return "".join(out)


_IF_TOKEN_RE = re.compile(r"\\(if[a-zA-Z@]*|fi)(?![a-zA-Z@])")


def strip_iffalse_blocks(text: str) -> str:
    """Remove `\\iffalse ... \\fi` blocks that start a line (the usual way to comment out text).

    Nested `\\if..`/`\\fi` pairs inside the block are balanced. Blocks that do not close are left alone.
    """
    out: List[str] = []
    pos = 0
    start_re = re.compile(r"(?m)^[ \t]*\\iffalse(?![a-zA-Z@])")
    while True:
        m = start_re.search(text, pos)
        if not m:
            out.append(text[pos:])
            break
        depth = 0
        end = None
        for t in _IF_TOKEN_RE.finditer(text, m.start()):
            # tokens inside comments do not count
            ls = text.rfind("\n", 0, t.start()) + 1
            cpos = find_comment_start(text[ls : text.find("\n", t.start()) if text.find("\n", t.start()) != -1 else len(text)])
            if cpos != -1 and ls + cpos < t.start():
                continue
            if t.group(1) == "fi":
                depth -= 1
                if depth == 0:
                    end = t.end()
                    break
            elif t.group(1) == "iff" or text[t.end() : t.end() + 1] == "{":
                # `\iff` is a math symbol; `\ifthenelse{..}`, `\ifdef{..}` (ifthen/etoolbox) are not closed by \fi
                continue
            else:
                depth += 1
        if end is None:
            out.append(text[pos:])
            break
        out.append(text[pos : m.start()])
        pos = end
    return "".join(out)


def strip_comment_envs(text: str) -> str:
    return re.sub(r"(?ms)^[ \t]*\\begin\{comment\}.*?\\end\{comment\}[ \t]*\n?", "", text)


_FLATTEN_MARKERS = ("% >>> Begin flattened:", "% <<< End flattened:", "% --- Begin inlined .bbl ---", "% --- End inlined .bbl ---")
_VERBATIM_BEGIN_RE = re.compile(r"\\begin\{(verbatim\*?|Verbatim|lstlisting|minted|alltt)\}")


def sanitize_tex_for_arxiv(tex_text: str, strip_comments: bool = True) -> str:
    """Remove \\status / \\stafile definitions & calls, \\showstatus toggles, comments, \\iffalse blocks and
    `comment` environments, without changing what TeX typesets.

    A comment that follows code is cut down to a bare `%`, because that `%` also suppresses the
    end-of-line space (removing it would insert a space into macros, URLs and hyphenated words).
    """
    cleaned = strip_macro_calls(tex_text, ("status", "stafile"))
    if strip_comments:
        cleaned = strip_comment_envs(cleaned)
        cleaned = strip_iffalse_blocks(cleaned)
    def_drop_re = re.compile(
        r"\\(?:re|provide)?newcommand\*?\{\\(?:status|stafile)\}|\\def\\(?:status|stafile)\b|\\newif\\ifshowstatus|\\showstatus(?:true|false)\b"
    )
    out_lines: List[str] = []
    in_verb: Optional[str] = None
    blank_run = 0
    for ln in cleaned.split("\n"):
        if in_verb is not None:
            out_lines.append(ln)
            if f"\\end{{{in_verb}}}" in ln:
                in_verb = None
            blank_run = 0
            continue
        if def_drop_re.search(ln):
            continue
        if strip_comments:
            if ln.lstrip().startswith(_FLATTEN_MARKERS):
                out_lines.append(ln)
                blank_run = 0
                continue
            cut = find_comment_start(ln)
            if cut != -1:
                code = ln[:cut]
                if not code.strip():
                    continue  # whole-line comment: TeX ignores the line entirely
                ln = code + "%"  # keep the end-of-line-suppressing %, drop the comment text
        if not ln.strip():
            blank_run += 1
            if blank_run > 1:
                continue
        else:
            blank_run = 0
        out_lines.append(ln)
        mv = _VERBATIM_BEGIN_RE.search(ln)
        if mv and f"\\end{{{mv.group(1)}}}" not in ln[mv.end():]:
            in_verb = mv.group(1)
    return "\n".join(out_lines).rstrip() + "\n"


# ---------------------------------------------------------------------------------------------
# Flattening
# ---------------------------------------------------------------------------------------------

_INC_RE = re.compile(r"\\(input|include|subfile)(?:\s*\{([^{}]+)\}|\s+([^\s{}%\\]+))")
_BIB_RE = re.compile(r"\\bibliography\{[^}]+\}")


def resolve_tex_input(target: str, tex_dir: Path, root_dir: Path) -> Optional[Path]:
    """Resolve `\\input{target}` the way TeX does: try `target`, then `target.tex`, in both dirs."""
    t = target.strip().strip('"')
    if not t or "#" in t:
        return None
    for base in (tex_dir, root_dir):
        for cand in (base / t, base / f"{t}.tex"):
            if cand.is_file():
                return cand
    return None


def flatten_tex(
    tex_path: Path,
    root_dir: Path,
    bbl_text: Optional[str],
    visited: Optional[Set[Path]] = None,
    missing: Optional[List[str]] = None,
) -> str:
    """Recursively expand every \\input / \\include outside comments (several per line, with or without braces)."""
    if visited is None:
        visited = set()
    resolved = tex_path.resolve()
    if resolved in visited:
        return f"% [SKIPPED CYCLIC INCLUDE: {tex_path.name}]\n"
    visited.add(resolved)

    text = read_text(tex_path)
    out_parts: List[str] = []
    for line in text.split("\n"):
        cpos = find_comment_start(line)
        code = line if cpos == -1 else line[:cpos]
        tail = "" if cpos == -1 else line[cpos:]

        if bbl_text is not None and _BIB_RE.search(code):
            mb = _BIB_RE.search(code)
            assert mb is not None
            before, after = code[: mb.start()], code[mb.end():]
            if before.strip():
                out_parts.append(before + "%\n")
            out_parts.append(f"% --- Begin inlined .bbl ---\n{bbl_text.rstrip()}\n% --- End inlined .bbl ---\n")
            if after.strip() or tail:
                out_parts.append(after + tail + "\n")
            continue

        pieces: List[str] = []
        last = 0
        changed = False
        for m in _INC_RE.finditer(code):
            cmd, braced, bare = m.group(1), m.group(2), m.group(3)
            target = braced if braced is not None else bare
            cand = resolve_tex_input(target or "", tex_path.parent, root_dir)
            if cand is None:
                if missing is not None and target and "#" not in target:
                    missing.append(f"{tex_path.name}: \\{cmd}{{{target}}}")
                continue
            changed = True
            pieces.append(code[last : m.start()])
            pieces.append("%\n")
            if cmd == "include":
                pieces.append("\\clearpage\n")
            pieces.append(f"% >>> Begin flattened: {target.strip()} >>>\n")
            sub = flatten_tex(cand, root_dir, bbl_text, visited, missing)
            # keep the child's trailing blank lines (they are paragraph breaks), only ensure a final newline
            pieces.append(sub if sub.endswith("\n") else sub + "\n")
            pieces.append(f"% <<< End flattened: {target.strip()} <<<\n")
            if cmd == "include":
                pieces.append("\\clearpage\n")
            last = m.end()
        if changed:
            rest = code[last:] + tail
            if rest.strip():
                pieces.append(rest + "\n")
            out_parts.append("".join(pieces))
        else:
            out_parts.append(line + "\n")

    visited.remove(resolved)
    return "".join(out_parts)


# ---------------------------------------------------------------------------------------------
# Layout & leak inspection
# ---------------------------------------------------------------------------------------------

def graphics_search_dirs(tex_text: str, root_dir: Path) -> List[Path]:
    dirs = [root_dir]
    for gm in re.finditer(r"\\graphicspath\s*\{((?:\s*\{[^{}]*\})+)\s*\}", tex_text):
        for d in re.findall(r"\{([^{}]*)\}", gm.group(1)):
            if d.strip():
                dirs.append(root_dir / d.strip())
    return dirs


def resolve_graphic(rel: str, search_dirs: List[Path]) -> Optional[Path]:
    for base in search_dirs:
        cand = base / rel
        if cand.is_file():
            return cand
        for ext in (".pdf", ".png", ".eps", ".jpg", ".jpeg"):
            c2 = base / f"{rel}{ext}"
            if c2.is_file():
                return c2
    return None


_NON_PROSE_CMD_RE = re.compile(
    r"\\(?:label|ref|eqref|cref|Cref|autoref|pageref|nameref|cite[a-zA-Z]*|nocite|includegraphics|url|input|include|"
    r"bibliography|bibliographystyle|begin|end|vspace|hspace|setlength|addtolength|newcommand|renewcommand|providecommand|"
    r"usepackage|documentclass|graphicspath|affiliation|email|thanks)\*?(?:\[[^\]]*\])*(?:\{[^{}]*\})?"
)


def count_prose_words(body: str) -> int:
    no_inline = re.sub(r"\$[^$\n]+\$", " MATH ", body)
    no_inline = re.sub(r"\\\((.*?)\\\)", " MATH ", no_inline)
    no_np = _NON_PROSE_CMD_RE.sub(" ", no_inline)
    no_cmds = re.sub(r"\\[A-Za-z@]+\*?(?:\[[^\]]*\])?", " ", no_np)  # keep the {argument} text of \emph, \textbf, ...
    no_braces = re.sub(r"[{}]", " ", no_cmds)
    return len(re.findall(r"[A-Za-z0-9'-]+", no_braces))


def inspect_layout_and_macros(main_tex: Path, pdf_path: Optional[Path], root_dir: Path) -> Dict[str, object]:
    """Check macro \\par safety, section trailing blank lines, APS word-eq & rendered PDF geometry."""
    flat_raw = flatten_tex(main_tex, root_dir, None)
    issues: List[Dict[str, object]] = []
    warnings: List[str] = []

    for m in re.finditer(r"\\(?:re|provide)?newcommand\*?\{\\([A-Za-z]*head[A-Za-z]*)\}(?:\[\d+\])?", flat_raw):
        mname = m.group(1)
        blk = _read_balanced(flat_raw, m.end(), "{", "}")
        if blk is not None:
            mbody = blk[0]
            if ("\\emph{" in mbody or "\\textbf{" in mbody or "---" in mbody) and "\\par" not in mbody:
                issues.append({"kind": "missing_par_in_heading_macro", "macro": f"\\{mname}", "detail": "Heading macro lacks leading \\par and can collide onto previous paragraph"})

    sec_files: List[Tuple[str, Path]] = []
    main_code = "\n".join(ln[: find_comment_start(ln)] if find_comment_start(ln) != -1 else ln for ln in read_text(main_tex).split("\n"))
    for inc in _INC_RE.finditer(main_code):
        t = (inc.group(2) or inc.group(3) or "").strip()
        p = resolve_tex_input(t, main_tex.parent, root_dir)
        if p is not None and ("macros" not in p.name.lower()):
            sec_files.append((t, p))
    for idx in range(len(sec_files) - 1):
        prev_name, prev_p = sec_files[idx]
        next_name, next_p = sec_files[idx + 1]
        prev_txt = read_text(prev_p)
        next_txt = read_text(next_p).lstrip()
        if next_txt.startswith("\\letterhead") and prev_txt.strip() and not prev_txt.endswith("\n\n"):
            issues.append({"kind": "adjacent_section_missing_blank_line", "detail": f"{prev_name} ends without blank line before {next_name} \\letterhead"})

    clean_body = sanitize_tex_for_arxiv(flat_raw, strip_comments=True)
    if "\\begin{document}" in clean_body:
        clean_body = clean_body.split("\\begin{document}", 1)[1]
    for end_tok in ("\\begin{acknowledgments}", "\\begin{acknowledgements}", "\\section*{Acknowledgments}", "\\section*{Acknowledgements}", "\\bibliography{", "\\begin{thebibliography}", "\\end{document}"):
        if end_tok in clean_body:
            clean_body = clean_body.split(end_tok, 1)[0]

    display_eq_words = 0
    for env in ("equation", "equation*", "align", "align*", "gather", "gather*", "multline", "multline*"):
        for em in re.finditer(rf"\\begin\{{{re.escape(env)}\}}([\s\S]*?)\\end\{{{re.escape(env)}\}}", clean_body):
            rows = max(1, em.group(1).count("\\\\") + 1)
            display_eq_words += 16 * rows
    no_disp = re.sub(r"\\begin\{(equation|align|gather|multline|figure|table)(\*?)\}[\s\S]*?\\end\{\1\2\}", " ", clean_body)
    no_disp = re.sub(r"\\\[[\s\S]*?\\\]|\$\$[\s\S]*?\$\$", " ", no_disp)
    prose_words = count_prose_words(no_disp)

    fig_words = 0.0
    total_pages = 0
    body_pages = 0
    last_page_lines = 0
    midline_heading_collisions: List[Dict[str, object]] = []
    gdirs = graphics_search_dirs(flat_raw, root_dir)

    try:
        import pymupdf  # type: ignore
    except ImportError:
        try:
            import fitz as pymupdf  # type: ignore
        except ImportError:
            pymupdf = None  # type: ignore
            warnings.append("pymupdf not installed: figure word-equivalents and rendered-page checks skipped")

    if pymupdf is not None:
        for fm in re.finditer(r"\\begin\{(figure\*?)\}[\s\S]*?\\includegraphics(?:\[[^\]]*\])?\{([^}]+)\}", clean_body):
            is_wide = fm.group(1) == "figure*"
            fcand = resolve_graphic(fm.group(2).strip(), gdirs)
            if fcand is not None and fcand.suffix.lower() == ".pdf":
                try:
                    with pymupdf.open(fcand) as fdoc:
                        fr = fdoc[0].rect
                    asp = float(fr.width) / max(float(fr.height), 1e-6)
                    fig_words += (300.0 / (0.5 * asp) + 40.0) if is_wide else (150.0 / asp + 20.0)
                except Exception as exc:  # corrupt figure PDF
                    issues.append({"kind": "figure_pdf_unreadable", "path": str(fcand), "detail": str(exc)[:200]})

        if pdf_path and pdf_path.exists():
            try:
                doc = pymupdf.open(pdf_path)
                total_pages = len(doc)
                body_pages = total_pages
                for pidx in range(total_pages):
                    ptxt = doc[pidx].get_text("text")
                    if pidx > 0 and body_pages == total_pages and re.search(r"(?:^|\n)\s*Acknowledg(?:e)?ments?\b", ptxt):
                        body_pages = pidx + 1
                    blocks = doc[pidx].get_text("dict", flags=0).get("blocks", [])
                    half_x = doc[pidx].rect.width * 0.5
                    col_xs = [ln["bbox"][0] for b in blocks for ln in b.get("lines", []) if ln.get("bbox")]
                    left_col_min = min([x for x in col_xs if x < half_x], default=48.0)
                    right_col_min = min([x for x in col_xs if x >= half_x - 5], default=half_x + 10)
                    for b in blocks:
                        for ln in b.get("lines", []):
                            line_txt = "".join(s.get("text", "") for s in ln.get("spans", []))
                            if ".—" in line_txt or ".---" in line_txt:
                                x0 = float(ln["bbox"][0])
                                col_origin = left_col_min if x0 < half_x else right_col_min
                                if (x0 - col_origin) > 18.0:
                                    midline_heading_collisions.append({"page": pidx + 1, "x0": round(x0, 1), "col_origin": round(col_origin, 1), "text": line_txt.strip()[:80]})
                if total_pages > 0:
                    last_lines = [x.strip() for x in doc[-1].get_text("text").splitlines() if x.strip()]
                    last_page_lines = len(last_lines)
                    if total_pages >= 4 and last_page_lines <= 8:
                        issues.append({"kind": "orphan_last_page", "page": total_pages, "non_empty_lines": last_page_lines})
                doc.close()
            except Exception as exc:
                issues.append({"kind": "pdf_unreadable", "path": str(pdf_path), "detail": str(exc)[:200]})

    aps_word_eq = round(prose_words + display_eq_words + fig_words, 1)
    if midline_heading_collisions:
        issues.append({"kind": "midline_runin_heading_collision", "hits": midline_heading_collisions})

    return {
        "prose_words_est": prose_words,
        "display_eq_words_est": display_eq_words,
        "figure_words_est": round(fig_words, 1),
        "aps_word_equivalent_est": aps_word_eq,
        "total_pages": total_pages,
        "body_end_page_est": body_pages,
        "last_page_nonempty_lines": last_page_lines,
        "layout_issues": issues,
        "inspection_warnings": warnings,
    }


_LEAK_PATTERNS = (
    r"REPORT_[A-Za-z0-9_]+",
    r"\.workflow\b",
    r"ground_truth_ledger",
    r"CLAIM_SCOPE",
    r"GROUND_TRUTH",
    r"TASK_[A-Za-z0-9_]+",
    r"(?<![A-Za-z])[A-Za-z]:[\\/](?![\\/])[^\s{}\\/]+[\\/][^\s{}]*",  # absolute Windows paths (drive letter + folder)
    r"(?<![\w.\-:/])/(?:Users|home)/[^\s{}/]+",  # macOS/Linux home directories (not when part of a URL)
)


def _leftover_comment_lines(text: str) -> List[str]:
    """Comment text that survived sanitising (outside verbatim-like environments and flatten markers)."""
    hits: List[str] = []
    in_verb: Optional[str] = None
    for ln in text.split("\n"):
        if in_verb is not None:
            if f"\\end{{{in_verb}}}" in ln:
                in_verb = None
            continue
        mv = _VERBATIM_BEGIN_RE.search(ln)
        if mv and f"\\end{{{mv.group(1)}}}" not in ln[mv.end():]:
            in_verb = mv.group(1)
            continue
        if ln.lstrip().startswith(_FLATTEN_MARKERS):
            continue
        k = find_comment_start(ln)
        if k != -1 and ln[k + 1 :].strip():
            hits.append(ln.strip()[:120])
    return hits


def scan_submission_leaks(flat_text: str, root_dir: Path, is_main: bool = True, check_comments: bool = True) -> List[Dict[str, object]]:
    leaks: List[Dict[str, object]] = []
    if check_comments:
        leftover = _leftover_comment_lines(flat_text)
        if leftover:
            leaks.append({"kind": "comment_text_left", "sample": leftover[:5]})
    for pat_s in _LEAK_PATTERNS:
        for m in re.finditer(pat_s, flat_text):
            leaks.append({"kind": "internal_path_or_doc_leak", "match": m.group(0)})
    cjk = re.findall(r"[\u4e00-\u9fff]+", flat_text)
    if cjk:
        leaks.append({"kind": "cjk_text_leak", "sample": cjk[:5]})
    if re.search(r"\bAuthor Name\b", flat_text):
        leaks.append({"kind": "placeholder_author", "match": "Author Name"})
    if is_main:
        if not re.search(r"acknowledg(?:e)?ments?", flat_text, re.I):
            leaks.append({"kind": "missing_acknowledgments", "match": "Acknowledgments"})
        if not re.search(r"data\s+availability", flat_text, re.I):
            leaks.append({"kind": "missing_data_availability", "match": "Data Availability"})
    gdirs = graphics_search_dirs(flat_text, root_dir)
    for gm in re.finditer(r"\\includegraphics(?:\[[^\]]*\])?\{([^}]+)\}", flat_text):
        rel_g = gm.group(1).strip()
        if resolve_graphic(rel_g, gdirs) is None:
            leaks.append({"kind": "missing_included_figure", "path": rel_g})
    return leaks


# ---------------------------------------------------------------------------------------------
# Compilation
# ---------------------------------------------------------------------------------------------

def compile_latex(main_tex: Path) -> Dict[str, object]:
    workdir = main_tex.parent
    stem = main_tex.stem
    pdflatex = _find_tex_bin("pdflatex")
    if not pdflatex:
        return {"compiled": False, "clean_pass": False, "pdf_generated": False, "error": "pdflatex binary not found", "latex_errors": ["pdflatex binary not found"]}

    tool_errors: List[str] = []
    base_cmd = [pdflatex, "-interaction=nonstopmode", "-file-line-error", main_tex.name]
    rc1, _, e1 = _run_cmd(base_cmd, workdir)
    if rc1 in (124, 127):
        tool_errors.append(e1)

    bib_ran = False
    bib_tool: Optional[str] = None
    bib_rc = 0
    bib_errors_extra: List[str] = []
    aux_path = workdir / f"{stem}.aux"
    bcf_path = workdir / f"{stem}.bcf"
    if bcf_path.exists():
        biber = _find_tex_bin("biber")
        bib_tool = "biber"
        if biber:
            bib_rc, _, eb = _run_cmd([biber, stem], workdir)
            bib_ran = True
            if bib_rc in (124, 127):
                tool_errors.append(eb)
        else:
            bib_errors_extra.append("Document uses biblatex/biber but the `biber` binary was not found")
    elif aux_path.exists():
        aux_txt = read_text(aux_path)
        if "\\citation{" in aux_txt and "\\bibdata{" in aux_txt:
            bibtex = _find_tex_bin("bibtex")
            bib_tool = "bibtex"
            if bibtex:
                bib_rc, _, eb = _run_cmd([bibtex, stem], workdir)
                bib_ran = True
                if bib_rc in (124, 127):
                    tool_errors.append(eb)
            else:
                bib_errors_extra.append("Document cites references but the `bibtex` binary was not found")

    rc2, _, e2 = _run_cmd(base_cmd, workdir)
    rc3, _, e3 = _run_cmd(base_cmd, workdir)
    for rc, e in ((rc2, e2), (rc3, e3)):
        if rc in (124, 127):
            tool_errors.append(e)

    log_path = workdir / f"{stem}.log"
    blg_path = workdir / f"{stem}.blg"
    pdf_path = workdir / f"{stem}.pdf"

    if log_path.exists():
        log_info = parse_latex_log(read_text(log_path))
    else:
        log_info = parse_latex_log("")
        log_info["latex_errors"] = ["No .log file produced"]
    blg_info = parse_bibtex_log(read_text(blg_path)) if blg_path.exists() else {"bibtex_errors": [], "bibtex_warnings": []}
    blg_info["bibtex_errors"] = list(blg_info["bibtex_errors"]) + bib_errors_extra  # type: ignore[arg-type]
    if bib_ran and bib_rc not in (0, 1) and not blg_info["bibtex_errors"]:
        blg_info["bibtex_errors"] = [f"{bib_tool} exited with code {bib_rc}"]
    if tool_errors:
        log_info["latex_errors"] = list(log_info["latex_errors"]) + tool_errors  # type: ignore[arg-type]

    pdf_exists = pdf_path.exists() and pdf_path.stat().st_size > 0
    clean = bool(
        pdf_exists
        and rc3 == 0
        and not log_info["latex_errors"]
        and not log_info["undefined_references"]
        and not log_info["undefined_citations"]
        and not log_info["multiply_defined_labels"]
        and not blg_info["bibtex_errors"]
    )

    return {
        "compiled": True,
        "clean_pass": clean,
        "pdf_generated": pdf_exists,
        "pdf_path": str(pdf_path).replace("\\", "/") if pdf_exists else None,
        "pdf_bytes": pdf_path.stat().st_size if pdf_exists else 0,
        "pass_returncodes": [rc1, rc2, rc3],
        "bibliography_tool": bib_tool,
        "bibtex_ran": bib_ran,
        "bibtex_returncode": bib_rc if bib_ran else None,
        **log_info,
        **blg_info,
    }


def main() -> int:
    utf8_stdio()
    parser = argparse.ArgumentParser(description="Compile dual main/supplement LaTeX, inspect layout & flatten sanitized arXiv source (v2).")
    parser.add_argument("--main", type=Path, required=True, help="Path to main.tex")
    parser.add_argument("--supplement", type=Path, help="Optional path to supplement.tex")
    parser.add_argument("--compile", action="store_true", help="Run pdflatex + bibtex/biber + pdflatex x2")
    parser.add_argument("--flatten-out", type=Path, help="Output path for single-file flattened main_arxiv.tex")
    parser.add_argument("--flatten-supplement-out", type=Path, help="Optional output path for flattened supplement_arxiv.tex")
    parser.add_argument("--inline-bbl", action="store_true", help="Inline .bbl into flattened output")
    parser.add_argument("--keep-comments", action="store_true", help="Keep %% comments and \\status macros when flattening (NOT recommended for arXiv)")
    parser.add_argument("--json-out", type=Path, help="Optional JSON build report output")
    args = parser.parse_args()

    if not args.main.exists():
        print(json.dumps({"error": f"Main file not found: {args.main}"}), file=sys.stderr)
        return 2

    supp = args.supplement
    if supp is not None and not supp.exists():
        print(json.dumps({"error": f"Supplement file not found: {supp}"}), file=sys.stderr)
        return 2
    if supp is None and (args.main.parent / "supplement.tex").exists():
        supp = args.main.parent / "supplement.tex"

    report: Dict[str, object] = {"main": str(args.main).replace("\\", "/")}
    main_pdf: Optional[Path] = args.main.with_suffix(".pdf") if args.main.with_suffix(".pdf").exists() else None
    if args.compile:
        cr = compile_latex(args.main)
        report["compile_result"] = cr
        if cr.get("pdf_path"):
            main_pdf = Path(str(cr["pdf_path"]))
        if supp and supp.exists():
            report["supplement_compile_result"] = compile_latex(supp)

    report["layout_check"] = inspect_layout_and_macros(args.main, main_pdf, args.main.parent)

    all_leaks: List[Dict[str, object]] = []
    missing_inputs: List[str] = []
    if args.flatten_out:
        bbl_path = args.main.parent / f"{args.main.stem}.bbl"
        bbl_text = read_text(bbl_path) if (args.inline_bbl and bbl_path.exists()) else None
        flattened = flatten_tex(args.main, args.main.parent, bbl_text, missing=missing_inputs)
        if not args.keep_comments:
            flattened = sanitize_tex_for_arxiv(flattened, strip_comments=True)
        write_new(args.flatten_out, flattened)
        leaks_m = scan_submission_leaks(flattened, args.main.parent, is_main=True, check_comments=not args.keep_comments)
        all_leaks.extend(leaks_m)
        report["flattened_out"] = str(args.flatten_out).replace("\\", "/")
        report["flattened_bytes"] = args.flatten_out.stat().st_size
        report["bbl_inlined"] = bbl_text is not None
        report["submission_leaks"] = leaks_m

    if args.flatten_supplement_out and supp and supp.exists():
        sbbl_path = supp.parent / f"{supp.stem}.bbl"
        sbbl_text = read_text(sbbl_path) if (args.inline_bbl and sbbl_path.exists()) else None
        sflat = flatten_tex(supp, supp.parent, sbbl_text, missing=missing_inputs)
        if not args.keep_comments:
            sflat = sanitize_tex_for_arxiv(sflat, strip_comments=True)
        write_new(args.flatten_supplement_out, sflat)
        leaks_s = scan_submission_leaks(sflat, supp.parent, is_main=False, check_comments=not args.keep_comments)
        all_leaks.extend(leaks_s)
        report["flattened_supplement_out"] = str(args.flatten_supplement_out).replace("\\", "/")
        report["supplement_submission_leaks"] = leaks_s

    if missing_inputs:
        report["missing_inputs"] = sorted(set(missing_inputs))

    cr_d: Dict[str, object] = report.get("compile_result", {}) or {}  # type: ignore[assignment]
    scr_d: Dict[str, object] = report.get("supplement_compile_result", {}) or {}  # type: ignore[assignment]
    layout_issues = (report["layout_check"] or {}).get("layout_issues", [])  # type: ignore[union-attr]
    passed = (
        (not args.compile or bool(cr_d.get("clean_pass")))
        and (not scr_d or bool(scr_d.get("clean_pass")))
        and not all_leaks
        and not missing_inputs
        and not layout_issues
    )
    report["passed"] = passed

    if args.json_out:
        write_new(args.json_out, json.dumps(report, indent=2, ensure_ascii=False))

    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0 if passed else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # crash != "issues found"
        print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}), file=sys.stderr)
        sys.exit(2)
