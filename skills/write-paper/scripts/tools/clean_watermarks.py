#!/usr/bin/env python3
"""Deterministic watermark scrubber, AI-cliche & copy-edit scanner, and vetted connector aligner.

Handles (zero false-positive risk to scientific meaning):
1. Invisible Unicode watermarks (zero-width joiners/spaces, bidi overrides, BOM, soft hyphen).
2. Exotic whitespace normalization (NBSP, thin/hair/ideographic spaces -> ASCII space).
3. LaTeX typography hygiene outside math & comments (curly quotes, raw Unicode em/en dashes, ellipsis).
4. Embedded Cyrillic/Greek homoglyphs inside ASCII words outside math mode.
5. High-signal AI cliché blacklist & deterministic copy-edit bugs (`rank rank`, `at most <=`,
   sentence-initial `Sec.`, repeated acronym expansions).
6. Strictly gated connector swaps (`furthermore`/`additionally`/`consequently`, etc.) ONLY outside
   abstract & theorem environments AND ONLY after `lexicon_vetted: true` is certified in profile.

Never touched: math, comments, verbatim-like environments, \\verb, \\url/\\href/\\path arguments,
and CJK text (Chinese/Japanese/Korean dashes, ellipses, quotes and ideographic spaces stay as they are).
`--fix` keeps each file's encoding and newline style and saves the previous version in `.wp_backups/`.

Exit codes: 0 = ran (findings are in the JSON), 2 = usage or tool error.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _texio import iter_files, read_text_file, strip_comments_text, utf8_stdio, write_back, write_new  # noqa: E402


ZERO_WIDTH_CHARS = {
    "\u200b": "ZERO_WIDTH_SPACE",
    "\u200c": "ZERO_WIDTH_NON_JOINER",
    "\u200d": "ZERO_WIDTH_JOINER",
    "\u200e": "LEFT_TO_RIGHT_MARK",
    "\u200f": "RIGHT_TO_LEFT_MARK",
    "\u202a": "LRE",
    "\u202b": "RLE",
    "\u202c": "PDF",
    "\u202d": "LRO",
    "\u202e": "RLO",
    "\u2060": "WORD_JOINER",
    "\u2061": "FUNCTION_APPLICATION",
    "\u2062": "INVISIBLE_TIMES",
    "\u2063": "INVISIBLE_SEPARATOR",
    "\u2064": "INVISIBLE_PLUS",
    "\u2066": "LRI",
    "\u2067": "RLI",
    "\u2068": "FSI",
    "\u2069": "PDI",
    "\ufeff": "BOM_ZWNBSP",
    "\u00ad": "SOFT_HYPHEN",
    "\u180e": "MONGOLIAN_VOWEL_SEPARATOR",
}

EXOTIC_SPACES = {
    "\u00a0": "NBSP",
    "\u1680": "OGHAM_SPACE",
    "\u2000": "EN_QUAD",
    "\u2001": "EM_QUAD",
    "\u2002": "EN_SPACE",
    "\u2003": "EM_SPACE",
    "\u2004": "THREE_PER_EM",
    "\u2005": "FOUR_PER_EM",
    "\u2006": "SIX_PER_EM",
    "\u2007": "FIGURE_SPACE",
    "\u2008": "PUNCTUATION_SPACE",
    "\u2009": "THIN_SPACE",
    "\u200a": "HAIR_SPACE",
    "\u202f": "NARROW_NBSP",
    "\u205f": "MEDIUM_MATH_SPACE",
    "\u3000": "IDEOGRAPHIC_SPACE",
}

HOMOGLYPH_MAP = {
    "\u0410": "A", "\u0412": "B", "\u0415": "E", "\u041a": "K", "\u041c": "M",
    "\u041d": "H", "\u041e": "O", "\u0420": "P", "\u0421": "C", "\u0422": "T",
    "\u0425": "X", "\u0430": "a", "\u0435": "e", "\u043e": "o", "\u0440": "p",
    "\u0441": "c", "\u0443": "y", "\u0445": "x", "\u0456": "i", "\u0458": "j",
    "\u0391": "A", "\u0392": "B", "\u0395": "E", "\u0396": "Z", "\u0397": "H",
    "\u0399": "I", "\u039a": "K", "\u039c": "M", "\u039d": "N", "\u039f": "O",
    "\u03a1": "P", "\u03a4": "T", "\u03a7": "X", "\u03bf": "o",
}

TYPOGRAPHY_REPLACEMENTS = {
    "\u201c": "``",
    "\u201d": "''",
    "\u2018": "`",
    "\u2019": "'",
    "\u2014": "---",
    "\u2013": "--",
    "\u2026": r"\ldots{}",
}

AI_SLOP_PATTERNS = [
    ("ai_cliche_word", re.compile(r"\b(delve|delves|delved|delving|pivotal|tapestry|underscore[sd]?|underscoring|testament|multifaceted|groundbreaking|game[- ]changer|cornerstone|beacon|unravel(?:s|ed|ing)?|plethora|myriad|inasmuch)\b", re.I)),
    ("ai_cliche_phrase", re.compile(r"\b(shed(?:s|ding)?\s+light\s+on|intricate\s+interplay|paradigm\s+shift|realm\s+of|landscape\s+of|pave[sd]?\s+the\s+way\s+for|open[sd]?\s+new\s+avenues|plays?\s+a\s+(?:crucial|pivotal|vital)\s+role|with\s+zero\s+ad[- ]hoc\s+assumptions|proves\s+mandatory)\b", re.I)),
    ("ai_opener_adverb", re.compile(r"(?:^|[.!?]\s+)(Notably|Importantly|Crucially|Remarkably|Interestingly|Significantly),")),
    ("ai_filler_phrase", re.compile(r"\b(it is worth noting that|it is important to note that|it should be noted that|let us delve into)\b", re.I)),
    ("ai_contrast_template", re.compile(r"\bnot only\b[^.]{1,140}\bbut also\b|\bnot (?:merely|simply|just)\b[^.]{1,100}\b(?:rather|instead)\b", re.I)),
]

# Strictly science-neutral rhetorical connectors (NO prepositions, quantifiers, or physics verbs).
SAFE_CONNECTOR_WORDS = frozenset({
    "hence", "therefore", "thus", "consequently", "accordingly",
    "moreover", "furthermore", "additionally",
    "however", "nevertheless", "nonetheless",
    "subsequently", "thereafter",
    "analogously", "similarly", "likewise",
})

# Science-neutral non-scientific rhetorical & evaluative vocabulary forms -> (lemma, category)
LEXICON_FORMS: Dict[str, Tuple[str, str]] = {
    "consequently": ("consequently", "connector"), "hence": ("hence", "connector"),
    "therefore": ("therefore", "connector"), "thus": ("thus", "connector"), "accordingly": ("accordingly", "connector"),
    "furthermore": ("furthermore", "connector"), "moreover": ("moreover", "connector"), "additionally": ("additionally", "connector"),
    "however": ("however", "connector"), "nevertheless": ("nevertheless", "connector"), "nonetheless": ("nonetheless", "connector"),
    "subsequently": ("subsequently", "connector"), "thereafter": ("thereafter", "connector"),
    "analogously": ("analogously", "connector"), "similarly": ("similarly", "connector"), "likewise": ("likewise", "connector"),
    "utilize": ("utilize", "rhetorical"), "utilizes": ("utilize", "rhetorical"), "utilized": ("utilize", "rhetorical"), "utilizing": ("utilize", "rhetorical"),
    "leverage": ("leverage", "rhetorical"), "leverages": ("leverage", "rhetorical"), "leveraged": ("leverage", "rhetorical"), "leveraging": ("leverage", "rhetorical"),
    "use": ("use", "rhetorical"), "uses": ("use", "rhetorical"), "used": ("use", "rhetorical"), "using": ("use", "rhetorical"),
    "important": ("important", "evaluative"), "crucial": ("crucial", "evaluative"), "vital": ("vital", "evaluative"), "essential": ("essential", "evaluative"),
    "significant": ("significant", "evaluative"), "remarkable": ("remarkable", "evaluative"), "notable": ("notable", "evaluative"),
}

MATH_ENVS = frozenset({
    "equation", "equation*", "align", "align*", "alignat", "alignat*",
    "gather", "gather*", "multline", "multline*", "flalign", "flalign*",
    "eqnarray", "eqnarray*", "math", "displaymath", "tikzpicture", "tikzcd", "cd",
})

CLAIM_PROTECT_ENVS = frozenset({
    "abstract", "theorem", "thm", "lemma", "lem", "proposition", "prop", "corollary", "cor", "remark", "rem", "definition", "dfn",
})

VERBATIM_ENVS = frozenset({"verbatim", "verbatim*", "Verbatim", "lstlisting", "minted", "alltt", "comment", "filecontents", "filecontents*"})
_URL_ARG_CMDS = re.compile(r"\\(url|href|path|nolinkurl)\b")
_VERB_CMD = re.compile(r"\\(verb|lstinline|Verb)\*?")
_CS_NAME = re.compile(r"\\[A-Za-z@]+")
_CJK_RE = re.compile(r"[\u2e80-\u303f\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af\uf900-\ufaff\uff00-\uffef]")


def _cjk_context(text: str, i: int) -> bool:
    """True if the run of identical characters around text[i] touches CJK text on either side."""
    ch = text[i]
    s = i
    while s > 0 and text[s - 1] == ch:
        s -= 1
    e = i
    while e + 1 < len(text) and text[e + 1] == ch:
        e += 1
    before = text[s - 1] if s > 0 else ""
    after = text[e + 1] if e + 1 < len(text) else ""
    return bool((before and _CJK_RE.match(before)) or (after and _CJK_RE.match(after)))


def _verbatim_ranges(text: str) -> List[Tuple[int, int]]:
    """Ranges that must never be edited: verbatim-like environments, \\verb|..|, and \\url{..}-style arguments."""
    ranges: List[Tuple[int, int]] = []
    for m in re.finditer(r"\\begin\{(" + "|".join(re.escape(e) for e in sorted(VERBATIM_ENVS)) + r")\}", text):
        end = text.find(f"\\end{{{m.group(1)}}}", m.end())
        ranges.append((m.start(), len(text) if end == -1 else end + len(m.group(1)) + 6))
    for m in _VERB_CMD.finditer(text):
        j = m.end()
        if j < len(text):
            d = text[j]
            close = "}" if d == "{" else d
            end = text.find(close, j + 1)
            ranges.append((m.start(), len(text) if end == -1 else end + 1))
    for m in _URL_ARG_CMDS.finditer(text):
        j = m.end()
        while j < len(text) and text[j] in " \t":
            j += 1
        if j < len(text) and text[j] == "{":
            end = text.find("}", j)
            ranges.append((m.start(), len(text) if end == -1 else end + 1))
    return _merge_ranges(ranges)


def _is_escaped(text: str, pos: int) -> bool:
    b = 0
    idx = pos - 1
    while idx >= 0 and text[idx] == "\\":
        b += 1
        idx -= 1
    return (b % 2) == 1


def _compute_protected_ranges(text: str, protect_claim_envs: bool = False) -> List[Tuple[int, int]]:
    """Return sorted [start, end) ranges for comments, math, verbatim, URLs, protected macros, and optional claim envs."""
    ranges: List[Tuple[int, int]] = list(_verbatim_ranges(text))
    n = len(text)
    i = 0
    begin_re = re.compile(r"\\begin\{([A-Za-z*]+)\}")
    cmd_arg_re = re.compile(
        r"\\(label|ref|eqref|pageref|autoref|cref|Cref|extref|status|declareexternalref|cite[a-zA-Z]*|[a-z]*cite|usepackage|documentclass|input|include|bibliography|bibliographystyle|newcommand|renewcommand|providecommand|def|begin|end|url|href|path|nolinkurl|includegraphics|graphicspath)\b"
    )
    target_envs = MATH_ENVS | VERBATIM_ENVS | (CLAIM_PROTECT_ENVS if protect_claim_envs else frozenset())
    verb_starts = {s: e for s, e in ranges}

    while i < n:
        if i in verb_starts:
            i = verb_starts[i]
            continue
        if text[i] == "%" and not _is_escaped(text, i):
            start = i
            while i < n and text[i] != "\n":
                i += 1
            ranges.append((start, i))
            continue

        if text.startswith("\\begin{", i) and not _is_escaped(text, i):
            m = begin_re.match(text, i)
            if m and m.group(1) in target_envs:
                env = m.group(1)
                end_tag = f"\\end{{{env}}}"
                begin_tag = f"\\begin{{{env}}}"
                depth = 1
                j = m.end()
                while j < n and depth > 0:
                    if text.startswith(begin_tag, j) and not _is_escaped(text, j):
                        depth += 1
                        j += len(begin_tag)
                    elif text.startswith(end_tag, j) and not _is_escaped(text, j):
                        depth -= 1
                        j += len(end_tag)
                    else:
                        j += 1
                ranges.append((i, j))
                i = j
                continue

        if text.startswith("\\[", i) and not _is_escaped(text, i):
            j = i + 2
            while j < n:
                if text.startswith("\\]", j) and not _is_escaped(text, j):
                    j += 2
                    break
                j += 1
            ranges.append((i, j))
            i = j
            continue

        if text.startswith("\\(", i) and not _is_escaped(text, i):
            j = i + 2
            while j < n:
                if text.startswith("\\)", j) and not _is_escaped(text, j):
                    j += 2
                    break
                j += 1
            ranges.append((i, j))
            i = j
            continue

        if text[i] == "$" and not _is_escaped(text, i):
            if i + 1 < n and text[i + 1] == "$":
                j = i + 2
                while j < n:
                    if text[j] == "$" and j + 1 < n and text[j + 1] == "$" and not _is_escaped(text, j):
                        j += 2
                        break
                    j += 1
                ranges.append((i, j))
                i = j
                continue
            j = i + 1
            while j < n:
                if text[j] == "$" and not _is_escaped(text, j):
                    j += 1
                    break
                j += 1
            ranges.append((i, j))
            i = j
            continue

        if text[i] == "\\" and not _is_escaped(text, i):
            m = cmd_arg_re.match(text, i)
            if m:
                j = m.end()
                while j < n and text[j].isspace():
                    j += 1
                while j < n and text[j] in "[{":
                    open_c = text[j]
                    close_c = "]" if open_c == "[" else "}"
                    depth = 1
                    j += 1
                    while j < n and depth > 0:
                        if text[j] == "\\" and j + 1 < n:
                            j += 2
                            continue
                        if text[j] == open_c:
                            depth += 1
                        elif text[j] == close_c:
                            depth -= 1
                        j += 1
                    while j < n and text[j].isspace():
                        j += 1
                ranges.append((i, j))
                i = j
                continue
            m2 = _CS_NAME.match(text, i)  # match at an offset: no text[i:] copy (was O(n^2) on long files)
            if m2:
                ranges.append((i, m2.end()))
                i = m2.end()
                continue

        i += 1
    return ranges


def _merge_ranges(all_prot: List[Tuple[int, int]]) -> List[Tuple[int, int]]:
    merged: List[Tuple[int, int]] = []
    for s, e in sorted(all_prot):
        if not merged or s > merged[-1][1]:
            merged.append((s, e))
        else:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
    return merged


def _in_ranges(pos: int, ranges: Sequence[Tuple[int, int]]) -> bool:
    lo, hi = 0, len(ranges) - 1
    while lo <= hi:
        mid = (lo + hi) // 2
        s, e = ranges[mid]
        if s <= pos < e:
            return True
        if pos < s:
            hi = mid - 1
        else:
            lo = mid + 1
    return False


def _match_case(template: str, replacement: str) -> str:
    if template.isupper():
        return replacement.upper()
    if template[0].isupper():
        return replacement[0].upper() + replacement[1:]
    return replacement


def _scan_copyedit_issues(scan_text: str, prose_only: str) -> List[Dict[str, object]]:
    issues: List[Dict[str, object]] = []

    # 1. Doubled consecutive identical words outside math (e.g., "rank rank")
    for m in re.finditer(r"\b([A-Za-z]{2,})[ \t]+(\1)\b", prose_only, flags=re.I):
        if m.group(1).lower() in {"that", "had"}:
            continue
        line = prose_only.count("\n", 0, m.start()) + 1
        issues.append({"kind": "doubled_word", "match": m.group(0), "line": line})

    # 2. Redundant comparators (`at most <= K`, `at least >= K`)
    no_comments = strip_comments_text(scan_text)
    red_pat = re.compile(
        r"\b(?:at\s+most|no\s+more\s+than)\s*(?:\$\s*)?(?:<=|\\le\b|\\leq\b|<)"
        r"|\b(?:at\s+least|no\s+less\s+than)\s*(?:\$\s*)?(?:>=|\\ge\b|\\geq\b|>)",
        re.I,
    )
    for m in red_pat.finditer(no_comments):
        line = no_comments.count("\n", 0, m.start()) + 1
        issues.append({"kind": "redundant_comparator", "match": m.group(0), "line": line})

    # 3. Sentence-initial APS forbidden abbreviations (`Sec.`, `Eq.`, `Fig.`, `Ref.`)
    for m in re.finditer(r"(?:^|[.!?]\s+)(Sec|Eq|Fig|Ref)\.~", no_comments):
        line = no_comments.count("\n", 0, m.start()) + 1
        issues.append({"kind": "sentence_initial_abbrev", "match": m.group(0).strip(), "line": line})

    # 4. Repeated identical acronym expansion inside one file (`... (MPS)` >= 2 times).
    #    The abstract is a standalone text, so re-expanding an acronym once in the body is standard.
    body_only = re.sub(r"\\begin\{abstract\}[\s\S]*?\\end\{abstract\}", lambda mm: re.sub(r"[^\n]", " ", mm.group(0)), scan_text)
    body_mask = "".join(c if body_only[k] != " " or c == "\n" else " " for k, c in enumerate(prose_only)) if len(body_only) == len(prose_only) else prose_only
    acr_counts: Dict[str, List[int]] = {}
    for m in re.finditer(r"\b[A-Za-z][A-Za-z-]+(?:\s+[A-Za-z][A-Za-z-]+){1,5}\s*\(([A-Z]{2,8})\)", body_mask):
        acr = m.group(1)
        acr_counts.setdefault(acr, []).append(prose_only.count("\n", 0, m.start()) + 1)
    for acr, lines in acr_counts.items():
        if len(lines) >= 2:
            issues.append({"kind": "repeated_acronym_expansion", "match": acr, "lines": lines})

    return issues


def process_file(
    path: Path,
    fix: bool,
    vetted_connector_swaps: Dict[str, str],
    vetted_style_hints: Dict[str, str],
    locked_terms: Sequence[str],
) -> Dict[str, object]:
    tf = read_text_file(path)
    raw = tf.text
    zw_counts: Dict[str, int] = {}
    space_counts: Dict[str, int] = {}
    typo_counts: Dict[str, int] = {}
    homoglyphs: List[Dict[str, object]] = []
    auto_connector_swaps: List[Dict[str, object]] = []

    # Step A: Strip zero-width chars and normalize exotic spaces (never inside verbatim/URLs, never CJK spacing)
    verb_ranges = _verbatim_ranges(raw)
    cleaned_chars: List[str] = []
    for i, ch in enumerate(raw):
        if (ch in ZERO_WIDTH_CHARS or ch in EXOTIC_SPACES) and _in_ranges(i, verb_ranges):
            cleaned_chars.append(ch)
            continue
        if ch in ZERO_WIDTH_CHARS:
            name = ZERO_WIDTH_CHARS[ch]
            zw_counts[name] = zw_counts.get(name, 0) + 1
            if not fix:
                cleaned_chars.append(ch)
            continue
        if ch in EXOTIC_SPACES:
            if ch == "　" and (_cjk_context(raw, i) or i == 0 or raw[i - 1] in "\n　"):
                cleaned_chars.append(ch)  # ideographic space is ordinary CJK spacing/indentation
                continue
            name = EXOTIC_SPACES[ch]
            space_counts[name] = space_counts.get(name, 0) + 1
            if fix:
                cleaned_chars.append("~" if ch in (" ", " ") else " ")  # keep non-breaking spaces non-breaking
            else:
                cleaned_chars.append(ch)
            continue
        cleaned_chars.append(ch)
    step1 = "".join(cleaned_chars)

    # Step B: Outside math and comments, normalize curly quotes/dashes and detect homoglyphs
    ranges = _compute_protected_ranges(step1, protect_claim_envs=False)
    out_chars: List[str] = []
    n = len(step1)
    for i, ch in enumerate(step1):
        in_prot = _in_ranges(i, ranges)
        if not in_prot and ch in HOMOGLYPH_MAP:
            prev_ascii = i > 0 and step1[i - 1].isascii() and step1[i - 1].isalpha()
            next_ascii = i + 1 < n and step1[i + 1].isascii() and step1[i + 1].isalpha()
            if prev_ascii or next_ascii:
                line = step1.count("\n", 0, i) + 1
                homoglyphs.append({"line": line, "char": ch, "codepoint": f"U+{ord(ch):04X}", "ascii_equiv": HOMOGLYPH_MAP[ch]})
                out_chars.append(HOMOGLYPH_MAP[ch] if fix else ch)
                continue
        if not in_prot and ch in TYPOGRAPHY_REPLACEMENTS and not _cjk_context(step1, i):
            typo_counts[ch] = typo_counts.get(ch, 0) + 1
            out_chars.append(TYPOGRAPHY_REPLACEMENTS[ch] if fix else ch)
            continue
        out_chars.append(ch)
    step2 = "".join(out_chars)

    # Step C: Strictly vetted non-scientific connector swaps ONLY outside math, claims, & locked_terms
    working = step2
    if vetted_connector_swaps:
        ranges2 = _compute_protected_ranges(working, protect_claim_envs=True)
        locked_spans: List[Tuple[int, int]] = []
        for term in locked_terms:
            if term:
                for m_term in re.finditer(r"\b" + re.escape(term) + r"\b", working, flags=re.IGNORECASE):
                    locked_spans.append((m_term.start(), m_term.end()))
        merged_prot = _merge_ranges(ranges2 + locked_spans)

        edits: List[Tuple[int, int, str]] = []
        for m_word in re.finditer(r"\b([A-Za-z]+)\b", working):
            if _in_ranges(m_word.start(), merged_prot):
                continue
            w = m_word.group(1)
            wl = w.lower()
            target = vetted_connector_swaps.get(wl)
            if target and wl in SAFE_CONNECTOR_WORDS and target.lower() in SAFE_CONNECTOR_WORDS and wl != target.lower():
                new_word = _match_case(w, target.lower())
                line = working.count("\n", 0, m_word.start()) + 1
                auto_connector_swaps.append({"line": line, "from": w, "to": new_word})
                if fix:
                    edits.append((m_word.start(), m_word.end(), new_word))
        if fix and edits:
            buf = list(working)
            for s, e, rep in reversed(edits):
                buf[s:e] = list(rep)
            working = "".join(buf)

    backup = write_back(tf, working) if (fix and working != raw) else None

    # Step D: Scan prose on current text for AI slop, copy-edit bugs, and vetted rhetorical hints
    scan_text = working if fix else step2
    scan_ranges = _compute_protected_ranges(scan_text, protect_claim_envs=False)
    masked_chars = list(scan_text)
    for s, e in scan_ranges:
        for k in range(s, min(e, len(masked_chars))):
            if masked_chars[k] != "\n":
                masked_chars[k] = " "
    prose_only = "".join(masked_chars)

    ai_hits: List[Dict[str, object]] = []
    for label, pat in AI_SLOP_PATTERNS:
        for m in pat.finditer(prose_only):
            line = prose_only.count("\n", 0, m.start()) + 1
            snippet = prose_only[max(0, m.start() - 30) : min(len(prose_only), m.end() + 30)].replace("\n", " ").strip()
            ai_hits.append({"kind": label, "match": m.group(0), "line": line, "context": snippet})

    for m_bold in re.finditer(r"\\item\s+\\textbf\{[^}]+:\}", scan_text):
        line = scan_text.count("\n", 0, m_bold.start()) + 1
        ai_hits.append({"kind": "bold_bullet_slop", "match": m_bold.group(0), "line": line, "context": m_bold.group(0)})

    em_dash_paras: List[Dict[str, object]] = []
    cursor = 0
    for para in prose_only.split("\n\n"):
        cnt = para.count("---")
        if cnt >= 3:
            line = prose_only.count("\n", 0, cursor) + 1
            em_dash_paras.append({"line": line, "em_dash_count": cnt})
        cursor += len(para) + 2

    copyedit_issues = _scan_copyedit_issues(scan_text, prose_only)

    # Strictly vetted rhetorical / non-scientific hints (never emitted unless vetted in profile)
    rhetorical_hints: List[Dict[str, object]] = []
    if vetted_style_hints:
        locked_spans2: List[Tuple[int, int]] = []
        for term in locked_terms:
            if term:
                for m_term in re.finditer(r"\b" + re.escape(term) + r"\b", scan_text, flags=re.IGNORECASE):
                    locked_spans2.append((m_term.start(), m_term.end()))
        claim_prot = _merge_ranges(_compute_protected_ranges(scan_text, protect_claim_envs=True) + locked_spans2)
        for m_word in re.finditer(r"\b([A-Za-z]+)\b", scan_text):
            if _in_ranges(m_word.start(), claim_prot):
                continue
            w = m_word.group(1)
            info = LEXICON_FORMS.get(w.lower())
            if not info:
                continue
            lemma, cat = info
            if lemma in vetted_style_hints and cat in ("rhetorical", "evaluative"):
                line = scan_text.count("\n", 0, m_word.start()) + 1
                ctx = scan_text[max(0, m_word.start() - 35) : min(len(scan_text), m_word.end() + 35)].replace("\n", " ").strip()
                rhetorical_hints.append({
                    "line": line,
                    "found": w,
                    "category": cat,
                    "suggestion": vetted_style_hints[lemma],
                    "context": ctx,
                })

    return {
        "file": str(path).replace("\\", "/"),
        "modified": bool(fix and working != raw),
        "backup": str(backup).replace("\\", "/") if backup else None,
        "zero_width_removed" if fix else "zero_width_found": zw_counts,
        "exotic_spaces_normalized" if fix else "exotic_spaces_found": space_counts,
        "typography_fixed" if fix else "typography_issues": typo_counts,
        "homoglyphs": homoglyphs,
        "auto_connector_swaps": auto_connector_swaps,
        "ai_slop_hits": ai_hits,
        "copyedit_issues": copyedit_issues,
        "em_dash_dense_paragraphs": em_dash_paras,
        "rhetorical_hints": rhetorical_hints,
    }


def _should_skip_tex(path: Path) -> bool:
    name = path.name.lower()
    return name in {"inventory.tex", "loadbearing.tex"} or name.endswith("_inventory.tex") or name.endswith("_loadbearing.tex")


def collect_paths(raw_paths: Sequence[Path]) -> List[Path]:
    return [p for p in iter_files(raw_paths, [".tex"]) if not _should_skip_tex(p)]


def main() -> int:
    utf8_stdio()
    parser = argparse.ArgumentParser(description="Clean invisible watermarks, typography tells, copy-edit bugs & vetted connectors.")
    parser.add_argument("--paths", nargs="+", type=Path, required=True, help="Files or directories to scan/clean")
    parser.add_argument("--profile", type=Path, help="Optional 00_style_profile.json (only applies connector swaps when lexicon_vetted=true)")
    parser.add_argument("--fix", action="store_true", help="Clean invisible watermarks, typography, and vetted non-scientific connectors in place")
    parser.add_argument("--json-out", type=Path, help="Optional JSON report path")
    args = parser.parse_args()

    vetted_connectors: Dict[str, str] = {}
    vetted_hints: Dict[str, str] = {}
    locked_terms: List[str] = []
    lexicon_vetted = False

    missing = [str(p) for p in args.paths if not p.exists()]
    if missing:
        print(json.dumps({"error": f"path(s) not found: {missing}"}), file=sys.stderr)
        return 2
    if args.profile and not args.profile.exists():
        print(json.dumps({"error": f"profile not found: {args.profile}"}), file=sys.stderr)
        return 2

    if args.profile:
        prof = json.loads(args.profile.read_text(encoding="utf-8"))
        locked_terms = prof.get("locked_terms", [])
        pref = prof.get("preferred_lexicon", {})
        lexicon_vetted = bool(pref.get("lexicon_vetted", False))
        if lexicon_vetted:  # an un-vetted lexicon is never applied, not even its "vetted_*" keys
            for k, v in (pref.get("vetted_replacements") or {}).items():
                kl = str(k).lower().strip()
                vl = str(v).lower().strip()
                if kl in SAFE_CONNECTOR_WORDS and vl in SAFE_CONNECTOR_WORDS:
                    vetted_connectors[kl] = vl
                elif kl in LEXICON_FORMS and LEXICON_FORMS[kl][1] in ("rhetorical", "evaluative"):
                    vetted_hints[kl] = vl
            for k, v in (pref.get("vetted_style_hints") or {}).items():
                vetted_hints[str(k).lower().strip()] = str(v).strip()

    targets = collect_paths(args.paths)
    file_reports = [process_file(p, args.fix, vetted_connectors, vetted_hints, locked_terms) for p in targets]
    summary = {
        "files_processed": len(file_reports),
        "files_modified": sum(1 for r in file_reports if r["modified"]),
        "lexicon_vetted": lexicon_vetted,
        "total_ai_slop_hits": sum(len(r["ai_slop_hits"]) for r in file_reports),
        "total_copyedit_issues": sum(len(r["copyedit_issues"]) for r in file_reports),
        "total_rhetorical_hints": sum(len(r["rhetorical_hints"]) for r in file_reports),
        "total_homoglyphs": sum(len(r["homoglyphs"]) for r in file_reports),
        "files": file_reports,
    }

    if args.json_out:
        write_new(args.json_out, json.dumps(summary, indent=2, ensure_ascii=False))

    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as exc:
        print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}), file=sys.stderr)
        sys.exit(2)
