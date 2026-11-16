#!/usr/bin/env python3
"""Deterministic TeX reader-knowledge, internal-tag leak & symbol-definition auditor.

Expands the \\input / \\include / \\subfile tree of main.tex (and optional supplement.tex) in
reading order (several inputs per line, brace-less `\\input file`, dotted file names), strips
comments, `comment` environments and \\status{...} metadata (also when it spans lines), and catches:
1. Visible internal ledger/workspace tag leaks (`Lemma L1`, `Grade N`, `Refl-2`, uncited
   `Author Condition N`, `TASK_*`, `complex128`, raw `\\iffalse`), in the preamble and in macro
   bodies too, plus project-specific patterns from `--extra-tag-patterns` or a
   `.wp_internal_tags.txt` file (one regex per line) next to main.tex.
2. Unresolved \\ref / \\eqref / \\cref / \\autoref / \\pageref / \\nameref / \\extref targets and
   \\input files that do not exist.
3. Ghost unnumbered-Letter section references & Appendix-vs-Sec header mismatches in SM.
4. Main-text symbols used without an in-main definition at (or before) first use: symbols defined
   only in the Supplemental Material, bespoke descriptive subscripts (`E_{\\mathrm{gap}}`,
   `\\kappa_{\\mathrm{mix}}`), numbered capitals (`H_7`), and caller `--extra-symbols`.
5. Terminology mixing across main text and Supplemental Material.

Exit codes: 0 = pass, 1 = issues found, 2 = usage or tool error.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Set, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _texio import read_text, resolve_tex_input, strip_comments_text, utf8_stdio, write_new  # noqa: E402


_NOT_A_LOCAL_TAG = r"(?![^.\n]{0,40}(?:\\cite|\bRefs?\.|\bof\s+Ref|\bin\s+Ref))"

INTERNAL_TAG_PATTERNS: List[Tuple[str, re.Pattern]] = [
    ("internal_lemma_L_tag", re.compile(r"\bLemma(?:~|\s+)L\d+\b" + _NOT_A_LOCAL_TAG)),
    ("internal_theorem_alpha", re.compile(r"\bTheorem(?:~|\s+)[A-Z]\b(?!\s*\\ref)" + _NOT_A_LOCAL_TAG)),
    ("internal_grade_tag", re.compile(r"\bGrade(?:~|\s+)[A-Z0-9]+\b")),
    ("internal_refl_tag", re.compile(r"\bRefl-\d+\b")),
    ("internal_task_id", re.compile(r"\bTASK_[A-Za-z0-9_]+\b")),
    ("internal_condition_jargon", re.compile(r"\b[A-Z][a-z]{2,}\s+Condition\s+\d+\b" + _NOT_A_LOCAL_TAG)),
    ("internal_dtype_leak", re.compile(r"\bcomplex128\b|\bfloat64\b")),
    ("source_iffalse_callout", re.compile(r"\\textbackslash\s*iffalse|\\iffalse\b")),
]

TRIVIAL_MACRO_NAMES = frozenset({
    "documentclass", "usepackage", "input", "include", "bibliography", "bibliographystyle",
    "newcommand", "renewcommand", "providecommand", "DeclareMathOperator", "def",
    "begin", "end", "section", "subsection", "subsubsection", "letterhead", "paragraph",
    "label", "ref", "eqref", "extref", "pageref", "autoref", "cref", "Cref", "cite", "citep", "citet",
    "status", "stafile", "showstatustrue", "showstatusfalse", "declareexternalref",
    "textbf", "emph", "textit", "textrm", "textsc", "texttt", "mathrm", "mathcal", "mathbb", "mathfrak",
    "boldsymbol", "mathbf", "operatorname", "tilde", "widetilde", "bar", "overline", "hat", "widehat",
    "frac", "sqrt", "sum", "prod", "int", "left", "right", "bigl", "bigr", "Bigl", "Bigr",
    "equiv", "approx", "lesssim", "gtrsim", "cdot", "times", "otimes", "oplus", "circ", "mid", "nmid",
    "to", "mapsto", "leftrightarrow", "longrightarrow", "implies", "iff", "le", "leq", "ge", "geq", "neq", "ne",
    "in", "notin", "subset", "subseteq", "cup", "cap", "setminus", "pmod", "bmod", "gcd", "dim", "ker",
    "text", "mbox", "par", "noindent", "centering", "caption", "includegraphics", "clearpage", "newpage",
    "onecolumngrid", "twocolumngrid", "appendix", "maketitle", "thanks", "author", "affiliation", "title", "date",
    "footnote", "item", "hline", "toprule", "midrule", "bottomrule", "colrule", "texorpdfstring",
    "alpha", "beta", "gamma", "delta", "epsilon", "varepsilon", "zeta", "eta", "theta", "iota", "kappa",
    "lambda", "mu", "nu", "xi", "pi", "rho", "sigma", "tau", "upsilon", "phi", "varphi", "chi", "psi", "omega",
    "Gamma", "Delta", "Theta", "Lambda", "Xi", "Pi", "Sigma", "Phi", "Psi", "Omega",
    "infty", "partial", "nabla", "dag", "dagger", "pm", "mp", "ldots", "cdots", "dots",
    "url", "href", "email", "acknowledgments", "endinput",
})

GENERIC_SUBSCRIPTS = frozenset({"max", "min", "tot", "eff", "int", "ext", "loc", "sys", "env", "ini", "fin", "obs", "th", "exp", "num", "avg", "ref", "in", "out"})

_INC_RE = re.compile(r"\\(input|include|subfile)(?:\s*\{([^{}]+)\}|\s+([^\s{}%\\]+))")
_REF_RE = re.compile(r"\\(ref|eqref|extref|cref|Cref|autoref|Autoref|pageref|nameref|vref|Vref|cpageref|labelcref)\*?\{([^}]+)\}")


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


def _strip_macro_calls(text: str, macro_names: Sequence[str] = ("status", "stafile")) -> str:
    """Blank out \\status{...}{...}... calls (also across lines) keeping line numbers unchanged."""
    out = list(text)
    pat = re.compile(r"\\(?:" + "|".join(re.escape(m) for m in macro_names) + r")\b")
    n = len(text)
    for m in pat.finditer(text):
        prefix = text[max(0, m.start() - 30) : m.start()]
        if re.search(r"\\(?:re|provide)?newcommand\*?\{\s*$|\\def\s*$", prefix):
            continue
        j = m.end()
        while j < n and text[j] in " \t\n":
            j += 1
        while j < n and text[j] in "[{":
            oc = text[j]
            cc = "]" if oc == "[" else "}"
            blk = _read_balanced(text, j, oc, cc)
            if blk is None:
                break
            j = blk[1]
            while j < n and text[j] in " \t\n":
                j += 1
        for k in range(m.start(), min(j, n)):
            if out[k] != "\n":
                out[k] = " "
    return "".join(out)


def _expand_tex_tree(entry_tex: Path, root_dir: Path, missing: List[str], visited: Optional[Set[Path]] = None) -> List[Tuple[str, int, str]]:
    """(rel_file, line_no, comment-free code) in reading order; inputs are expanded in place."""
    if visited is None:
        visited = set()
    res = entry_tex.resolve()
    if res in visited or not res.exists():
        return []
    visited.add(res)
    try:
        rel = str(entry_tex.relative_to(root_dir)).replace("\\", "/")
    except ValueError:
        rel = entry_tex.name

    out: List[Tuple[str, int, str]] = []
    code_text = strip_comments_text(read_text(entry_tex))
    for lno, code in enumerate(code_text.split("\n"), 1):
        last = 0
        for m in _INC_RE.finditer(code):
            target = m.group(2) if m.group(2) is not None else m.group(3)
            cand = resolve_tex_input(target or "", entry_tex.parent, root_dir)
            if cand is None:
                if target and "#" not in target and "\\" not in target:
                    missing.append(f"{rel}:{lno}: \\{m.group(1)}{{{target}}}")
                continue
            if any(x in cand.name.lower() for x in ("inventory", "loadbearing")):
                continue
            if code[last : m.start()].strip():
                out.append((rel, lno, code[last : m.start()]))
            out.extend(_expand_tex_tree(cand, root_dir, missing, visited))
            last = m.end()
        out.append((rel, lno, code[last:]))
    visited.remove(res)
    return out


def _split_preamble_body(lines: List[Tuple[str, int, str]]) -> Tuple[List[Tuple[str, int, str]], List[Tuple[str, int, str]]]:
    has_begin_doc = any("\\begin{document}" in ln for _, _, ln in lines)
    if not has_begin_doc:
        return [], lines
    pre: List[Tuple[str, int, str]] = []
    body: List[Tuple[str, int, str]] = []
    in_doc = False
    for rel, lno, ln in lines:
        if not in_doc:
            if "\\begin{document}" in ln:
                in_doc = True
                before, after = ln.split("\\begin{document}", 1)
                pre.append((rel, lno, before))
                body.append((rel, lno, after))
            else:
                pre.append((rel, lno, ln))
            continue
        if "\\end{document}" in ln:
            body.append((rel, lno, ln.split("\\end{document}", 1)[0]))
            break
        body.append((rel, lno, ln))
    return pre, body


def _macro_defs(lines: List[Tuple[str, int, str]]) -> Dict[str, Tuple[str, str]]:
    macro_defs: Dict[str, Tuple[str, str]] = {}
    pats = (
        re.compile(r"\\(?:re|provide)?newcommand\*?\{?\\([A-Za-z]+)\}?(?:\[\d+\])?(?:\[[^\]]*\])?\{(.*)\}"),
        re.compile(r"\\def\\([A-Za-z]+)[^{]*\{(.*)\}"),
        re.compile(r"\\DeclareMathOperator\*?\{\\([A-Za-z]+)\}\{(.*)\}"),
    )
    for rel, lno, ln in lines:
        for pat in pats:
            mm = pat.search(ln)
            if mm and mm.group(1) not in TRIVIAL_MACRO_NAMES:
                macro_defs[mm.group(1)] = (f"{rel}:{lno}", mm.group(2))
    return macro_defs


def _clean_lines(lines: List[Tuple[str, int, str]]) -> List[Tuple[str, int, str]]:
    """Strip \\status-like calls across the joined stream, then split back onto the original locations."""
    if not lines:
        return []
    joined = "\n".join(ln.replace("\n", " ") for _, _, ln in lines)
    stripped = _strip_macro_calls(joined).split("\n")
    return [(rel, lno, s) for (rel, lno, _), s in zip(lines, stripped)]


def _first_definition_pos(text: str, sym_regex: str) -> Optional[int]:
    bind_re = re.compile(
        rf"(?:define|defined\s+(?:as|by)|denote|denoting|specify|call|called|write|set|let|where|introduc(?:e|ing))\s+[^.;\n]{{0,28}}(?<![A-Za-z]){sym_regex}"
        rf"|(?<![A-Za-z]){sym_regex}(?:\([^()\n]{{1,24}}\))?\s*(?:=|:=|\\equiv|\\coloneqq|\\propto)"
        rf"|(?<![A-Za-z]){sym_regex}(?:\([^()\n]{{1,24}}\))?\s+(?:is|denotes|stands\s+for|counts|labels|gives)\b",
        re.I,
    )
    m = bind_re.search(text)
    return m.start() if m else None


def _extract_defined_symbols(text: str) -> Set[str]:
    syms: Set[str] = set()
    cand_pat = (
        r"(?:\\[a-zA-Z]{2,}_(?:\{[^{}\n]{1,20}\}|[a-zA-Z0-9])"
        r"|\b[A-Z]_[0-9]+\b"
        r"|(?:\\[A-Za-z]{2,}|\b[A-Za-z])_\{\\(?:mathrm|text|textrm|rm|mathit)\{[A-Za-z]{2,18}\}\})"
    )
    skip_exact = {"Z_2", "U_1", "H_0", "S_z", "S_x", "S_y"}
    def_before = re.compile(rf"(?:define|denote|specify|let|where|set|write|call|begin\{{definition\}}\[[^\]]*)\s*[^.;\n]{{0,24}}({cand_pat})", re.I)
    def_eq = re.compile(rf"({cand_pat})(?:\([^()\n]{{1,24}}\))?\s*(?:=|:=|\\equiv|\\coloneqq)")
    for pat in (def_before, def_eq):
        for m in pat.finditer(text):
            tok = m.group(1).strip()
            if tok in skip_exact or re.match(r"^(?:\\sigma|\\tau|X|Y|Z)_[ijkmnl]$", tok):
                continue
            syms.add(tok)
    return syms


def _load_extra_tag_patterns(main_tex: Path, cli_patterns: Sequence[str]) -> List[Tuple[str, re.Pattern]]:
    pats: List[Tuple[str, re.Pattern]] = []
    sources: List[str] = list(cli_patterns)
    tag_file = main_tex.parent / ".wp_internal_tags.txt"
    if tag_file.exists():
        sources += [ln.strip() for ln in read_text(tag_file).split("\n") if ln.strip() and not ln.strip().startswith("#")]
    for s in sources:
        try:
            pats.append((f"project_tag:{s}", re.compile(s)))
        except re.error as exc:
            raise SystemExit(f"invalid internal-tag regex {s!r}: {exc}")
    return pats


def audit_project(main_tex: Path, supplement_tex: Optional[Path] = None, extra_symbols: Sequence[str] = (), extra_tag_patterns: Sequence[str] = ()) -> Dict[str, object]:
    root_dir = main_tex.parent
    missing_inputs: List[str] = []
    main_stream = _expand_tex_tree(main_tex, root_dir, missing_inputs)
    main_pre, main_body = _split_preamble_body(main_stream)
    main_macros = _macro_defs(main_stream)
    main_lines = _clean_lines(main_body)
    main_pre_clean = _clean_lines(main_pre)
    main_clean = "\n".join(ln for _, _, ln in main_lines)

    supp_stream: List[Tuple[str, int, str]] = []
    supp_lines: List[Tuple[str, int, str]] = []
    supp_pre_clean: List[Tuple[str, int, str]] = []
    supp_clean = ""
    if supplement_tex and supplement_tex.exists():
        supp_stream = _expand_tex_tree(supplement_tex, root_dir, missing_inputs)
        supp_pre, supp_body = _split_preamble_body(supp_stream)
        supp_lines = _clean_lines(supp_body)
        supp_pre_clean = _clean_lines(supp_pre)
        supp_clean = "\n".join(ln for _, _, ln in supp_lines)
        for k, v in _macro_defs(supp_stream).items():
            main_macros.setdefault(k, v)

    all_clean_lines: List[Tuple[str, str, int, str]] = (
        [("main", r, n, ln) for r, n, ln in main_lines] + [("supplement", r, n, ln) for r, n, ln in supp_lines]
    )
    preamble_lines: List[Tuple[str, str, int, str]] = (
        [("main-preamble", r, n, ln) for r, n, ln in main_pre_clean] + [("supplement-preamble", r, n, ln) for r, n, ln in supp_pre_clean]
    )

    # 1. Internal ledger tag leaks (body, preamble, macro bodies)
    tag_patterns = INTERNAL_TAG_PATTERNS + _load_extra_tag_patterns(main_tex, extra_tag_patterns)
    tag_leaks: List[Dict[str, object]] = []
    for scope, rel, lno, ln in all_clean_lines + preamble_lines:
        for kind, pat in tag_patterns:
            if kind == "internal_condition_jargon" and "\\cite" in ln:
                continue
            for m in pat.finditer(ln):
                tag_leaks.append({"scope": scope, "location": f"{rel}:{lno}", "kind": kind, "match": m.group(0)})

    # 2. Label / ref resolution
    labels: Set[str] = set()
    ext_refs_map: Dict[str, str] = {}
    for _, _, ln in main_stream + supp_stream:
        for dm in re.finditer(r"\\declareexternalref\{([^}]+)\}\{([^}]*)\}", ln):
            ext_refs_map[dm.group(1).strip()] = dm.group(2).strip()
    refs_used: List[Tuple[str, str, str]] = []
    for scope, rel, lno, ln in all_clean_lines:
        for lm in re.finditer(r"\\label\{([^}]+)\}", ln):
            labels.add(lm.group(1).strip())
        for rm in _REF_RE.finditer(ln):
            for tgt in rm.group(2).split(","):
                if tgt.strip():
                    refs_used.append((rm.group(1), tgt.strip(), f"{scope}:{rel}:{lno}"))

    unresolved_refs: List[Dict[str, object]] = []
    for rtype, target, loc in refs_used:
        if target in labels or (rtype == "extref" and target in ext_refs_map):
            continue
        unresolved_refs.append({"kind": f"unresolved_{rtype}", "target": target, "location": loc})
    for mi in missing_inputs:
        unresolved_refs.append({"kind": "missing_input_file", "target": mi, "location": mi.split(":", 1)[0]})

    def _expand_extrefs(txt: str) -> str:
        return re.sub(r"\\extref\{([^}]+)\}", lambda mm: ext_refs_map.get(mm.group(1).strip(), mm.group(0)), txt)

    # 3. Ghost PRL sections & Appendix-vs-Sec SM mismatch & bare SM mentions
    section_mismatches: List[Dict[str, object]] = []
    main_header = "\n".join(ln for _, _, ln in main_stream[:80])
    is_prl = "prl" in main_header.lower()
    uses_numbered_sections = bool(re.search(r"\\section(?!\*)\{", main_clean))
    if is_prl and not uses_numbered_sections:
        for scope, rel, lno, ln in all_clean_lines:
            expanded_ln = _expand_extrefs(ln)
            for gm in re.finditer(r"\b(?:Sec\.|Section)(?:~|\s+)(?:I|II|III|IV|V|VI)\b", expanded_ln):
                if re.match(r"[^.\n]{0,40}(?:\\cite|\bRefs?\.|\bof\s+Ref|\bin\s+Ref)", expanded_ln[gm.end():]):
                    continue  # a section of a cited paper
                section_mismatches.append({"scope": scope, "location": f"{rel}:{lno}", "kind": "ghost_prl_roman_section", "excerpt": expanded_ln.strip()[:120]})

    if supp_lines:
        supp_uses_appendix = any("\\appendix" in ln for _, _, ln in supp_lines[:80])
        main_expanded = _expand_extrefs(main_clean)
        main_cites_sec_s = bool(re.search(r"\b(?:SM\s+Sec\.|Secs?\.|Supplemental\s+Material\s+Secs?\.)(?:~|\s*)S\d+\b", main_expanded))
        if supp_uses_appendix and main_cites_sec_s:
            section_mismatches.append({"scope": "cross", "location": supplement_tex.name if supplement_tex else "supplement.tex", "kind": "sm_appendix_vs_sec_header_mismatch",
                                       "excerpt": "supplement.tex uses \\appendix (Appendix S1...) while main text cites 'Sec. S...'"})

    bare_sm_without_cite: List[str] = []
    for scope, rel, lno, ln in all_clean_lines:
        if scope == "main" and re.search(r"\b(?:SM\s+Sec\.|Supplemental\s+Material)\b", ln) and "\\cite" not in ln:
            bare_sm_without_cite.append(f"{rel}:{lno}")

    # 4. Symbols used without an in-main definition at (or before) first use
    undefined_main_symbols: List[Dict[str, object]] = []
    abs_m = re.search(r"\\begin\{abstract\}([\s\S]*?)\\end\{abstract\}", main_clean)
    abstract_text = abs_m.group(1) if abs_m else ""
    if abstract_text:
        for om in re.finditer(r"\b([A-Z])\(((?:\\[A-Za-z]{2,}|[a-zA-Z])_(?:\{[^{}\n]{1,18}\}|[a-zA-Z0-9]))\)", abstract_text):
            op_head = re.escape(om.group(1) + "(")
            if not re.search(rf"{op_head}[^\n)]*\)\s*(?:=|:=|\\equiv)", abstract_text):
                undefined_main_symbols.append({"scope": "abstract", "symbol": f"{om.group(0)} undefined abstract operator notation", "kind": "abstract_undefined_notation"})
                break

    sm_defined = _extract_defined_symbols(supp_clean) if supp_clean else set()
    candidate_tokens: Dict[str, str] = {sym: re.escape(sym) for sym in sorted(sm_defined)}
    for m_besp in re.finditer(r"(?:\\[A-Za-z]{2,}|(?<![A-Za-z\\])[A-Za-z])_\{\\(?:mathrm|text|textrm|rm|mathit)\s*\{?([A-Za-z]{2,16})\}?\}", main_clean):
        if m_besp.group(1).lower() in GENERIC_SUBSCRIPTS:
            continue
        tok = m_besp.group(0)
        candidate_tokens.setdefault(tok, re.escape(tok))
    for m_hm in re.finditer(r"\bH_[2-9]\d*\b(?!\s*\()", main_clean):  # H_2(M;Z_2) is homology, not a Hamiltonian label
        candidate_tokens.setdefault(m_hm.group(0), re.escape(m_hm.group(0)))
    for ex_sym in extra_symbols:
        if ex_sym.strip():
            candidate_tokens.setdefault(ex_sym.strip(), re.escape(ex_sym.strip()))

    for label_s, pat_s in sorted(candidate_tokens.items()):
        pat_c = re.compile(rf"(?<![A-Za-z]){pat_s}(?![A-Za-z0-9_])")
        first_use = pat_c.search(main_clean)
        if not first_use:
            continue
        def_pos = _first_definition_pos(main_clean, pat_s)
        if def_pos is None:
            kind = "symbol_used_without_main_definition"
        elif def_pos > first_use.start() + 400:
            kind = "symbol_defined_after_first_use"
        else:
            continue
        undefined_main_symbols.append({"scope": "main", "symbol": label_s, "kind": kind, "defined_only_in_sm": bool(supp_clean and pat_c.search(supp_clean)),
                                       "first_use_excerpt": main_clean[max(0, first_use.start() - 40) : first_use.end() + 40].replace("\n", " ")})

    macro_use_re = re.compile(r"\\([A-Za-z]{2,})")
    main_macro_counts: Dict[str, int] = {}
    for mm in macro_use_re.finditer(main_clean):
        mc = mm.group(1)
        if mc in main_macros and mc not in TRIVIAL_MACRO_NAMES:
            main_macro_counts[mc] = main_macro_counts.get(mc, 0) + 1

    # 5. Terminology mixing
    dual_conflicts: List[Dict[str, object]] = []
    combined_txt = main_clean + "\n" + supp_clean
    term_pairs = [("trivially gapped", "featureless", r"\btrivially\s+gapped\b", r"\bfeatureless\b")]
    for t1, t2, r1, r2 in term_pairs:
        if re.search(r1, combined_txt, re.I) and re.search(r2, combined_txt, re.I):
            if not re.search(rf"(?:{r1}[^.;\n]{{0,45}}(?:equivalently|i\.e\.|same\s+as|synonym)[^.;\n]{{0,45}}{r2}|{r2}[^.;\n]{{0,45}}(?:equivalently|i\.e\.|same\s+as|synonym)[^.;\n]{{0,45}}{r1})", combined_txt, re.I):
                dual_conflicts.append({"kind": "terminology_mixing", "detail": f"Manuscript mixes '{t1}' and '{t2}' (main+SM); unify unless explicitly equated once."})

    checks = {
        "no_internal_tag_leaks": not tag_leaks,
        "no_unresolved_refs": not unresolved_refs,
        "no_ghost_sections": not section_mismatches,
        "no_undefined_main_symbols": not undefined_main_symbols,
        "no_dual_symbol_conflicts": not dual_conflicts,
    }
    return {
        "passed": all(checks.values()),
        "checks": checks,
        "files_scanned": sorted({rel for rel, _, _ in main_stream + supp_stream}),
        "internal_tag_leaks": tag_leaks,
        "unresolved_refs": unresolved_refs,
        "section_mismatches": section_mismatches,
        "bare_sm_without_cite": bare_sm_without_cite,
        "undefined_main_symbols": undefined_main_symbols,
        "dual_symbol_conflicts": dual_conflicts,
        "main_custom_macro_usage": main_macro_counts,
    }


def main() -> int:
    utf8_stdio()
    parser = argparse.ArgumentParser(description="Audit TeX manuscripts for leaked internal tags, unresolved refs, ghost sections and undefined symbols.")
    parser.add_argument("--main", type=Path, required=True, help="Path to main.tex")
    parser.add_argument("--supplement", type=Path, help="Optional path to supplement.tex")
    parser.add_argument("--extra-symbols", nargs="*", default=[], help="Extra TeX symbols requiring an explicit in-main definition")
    parser.add_argument("--extra-tag-patterns", nargs="*", default=[], help="Extra regexes for project-internal tags (also read from .wp_internal_tags.txt next to main.tex)")
    parser.add_argument("--json-out", type=Path, help="Optional JSON output path")
    parser.add_argument("--md-out", type=Path, help="Optional Markdown summary output path")
    args = parser.parse_args()

    if not args.main.is_file():
        print(json.dumps({"passed": False, "error": f"Main file not found: {args.main}"}), file=sys.stderr)
        return 2
    supp = args.supplement
    if supp is not None and not supp.is_file():
        print(json.dumps({"passed": False, "error": f"Supplement file not found: {supp}"}), file=sys.stderr)
        return 2
    if supp is None and (args.main.parent / "supplement.tex").exists():
        supp = args.main.parent / "supplement.tex"

    rep = audit_project(args.main, supp, extra_symbols=args.extra_symbols, extra_tag_patterns=args.extra_tag_patterns)
    if args.json_out:
        write_new(args.json_out, json.dumps(rep, indent=2, ensure_ascii=False))
    if args.md_out:
        lines = [
            "# TeX Knowledge-Curse, Internal-Tag & Symbol Hygiene Report",
            "",
            f"- **Overall Verdict**: `{'PASS' if rep['passed'] else 'ISSUES_FOUND'}`",
            f"- **Internal Tag Leaks**: {len(rep['internal_tag_leaks'])} | **Unresolved Refs / Inputs**: {len(rep['unresolved_refs'])}",  # type: ignore[arg-type]
            f"- **Ghost Section / Header Mismatches**: {len(rep['section_mismatches'])} | **Undefined Main Symbols**: {len(rep['undefined_main_symbols'])}",  # type: ignore[arg-type]
            f"- **Terminology Conflicts**: {len(rep['dual_symbol_conflicts'])} | **Bare SM Mentions without `\\cite`**: {len(rep['bare_sm_without_cite'])}",  # type: ignore[arg-type]
            "",
        ]
        for title_k, key in (
            ("## Internal Tag Leaks", "internal_tag_leaks"),
            ("## Unresolved Refs / Missing Inputs", "unresolved_refs"),
            ("## Section / Header Mismatches", "section_mismatches"),
            ("## Undefined Main-Text Symbols", "undefined_main_symbols"),
            ("## Terminology Conflicts", "dual_symbol_conflicts"),
        ):
            items = rep[key]  # type: ignore[index]
            if items:
                lines += [title_k, ""] + [f"- `{json.dumps(it, ensure_ascii=False)}`" for it in items] + [""]
        write_new(args.md_out, "\n".join(lines))
    print(json.dumps(rep, indent=2, ensure_ascii=False))
    return 0 if rep["passed"] else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as exc:
        print(json.dumps({"passed": False, "error": f"{type(exc).__name__}: {exc}"}), file=sys.stderr)
        sys.exit(2)
