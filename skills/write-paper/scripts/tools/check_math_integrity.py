#!/usr/bin/env python3
"""Deterministic LaTeX math, label, ref, citation, macro, and logical-content integrity checker.

Compares a .tex file (or directory) before and after a prose edit and fails when any of these
changed:
- math (inline, display, math environments), \\label / \\ref-family / \\cite-family commands and
  extra protected macros (\\extref, \\status, ...);
- macro definitions (\\newcommand, \\renewcommand, \\def, \\DeclareMathOperator, \\newtheorem, ...);
- the multiset of negation / logical-strength / scaling words in the whole prose ("not", "never",
  "necessary", "sufficient", "if and only if", "exactly", "at least", "exponentially", ...);
- the multiset of numbers written in prose (number words count as digits: "two" == "2");
- the ordered quantifiers/modalities inside abstract and claim environments (theorem, lemma,
  their starred forms, and any environment declared with \\newtheorem / \\declaretheorem).

Exit codes: 0 = unchanged, 1 = violation found, 2 = usage or input error.
"""

from __future__ import annotations

import argparse
import difflib
import json
import re
import sys
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, FrozenSet, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _texio import iter_files, read_text, utf8_stdio, write_new  # noqa: E402


MATH_ENVS = frozenset({
    "equation", "equation*", "align", "align*", "alignat", "alignat*", "gather", "gather*",
    "multline", "multline*", "flalign", "flalign*", "eqnarray", "eqnarray*", "math", "displaymath",
    "subequations", "tikzpicture", "tikzcd", "cd",
})

REF_COMMANDS = frozenset({
    "label", "ref", "eqref", "pageref", "autoref", "Autoref", "cref", "Cref", "cpageref", "Cpageref",
    "crefrange", "Crefrange", "cpagerefrange", "labelcref", "vref", "Vref", "nameref", "subref",
    "extref", "hyperref", "hyperlink", "hypertarget",
})
MULTI_ARG_REFS = {"crefrange": 2, "Crefrange": 2, "cpagerefrange": 2, "hyperlink": 1, "hypertarget": 1}
NOT_CITE_CMDS = frozenset({"bibcite", "citation", "citenamefont", "citestyle", "citeform", "citepunct", "citeleft", "citeright"})

DEF_COMMANDS = frozenset({"newcommand", "renewcommand", "providecommand", "DeclareMathOperator", "DeclareRobustCommand", "def", "gdef", "edef", "newtheorem", "declaretheorem", "newenvironment", "renewenvironment"})

DEFAULT_EXTRA_PROTECTED_CMDS = ("extref", "status", "declareexternalref")

BASE_CLAIM_ENVS = frozenset({
    "abstract", "theorem", "thm", "lemma", "lem", "proposition", "prop", "corollary", "cor",
    "remark", "rem", "definition", "dfn", "defn", "conjecture", "conj", "claim", "observation",
    "fact", "hypothesis", "assumption", "criterion", "result", "principle",
})

QUANTIFIER_PATTERNS: Tuple[str, ...] = (
    r"counted\s+with\s+multiplicity", r"with\s+multiplicity", r"if\s+and\s+only\s+if",
    r"necessary\s+and\s+sufficient", r"translation[- ]invariant", r"finite[- ]range",
    r"for\s+all", r"for\s+every", r"for\s+some", r"there\s+exists?", r"at\s+most", r"at\s+least",
    r"only\s+if", r"non-?palindromic", r"square-?free", r"every", r"each", r"any", r"some", r"all",
    r"exists?", r"unique(?:ly)?", r"iff", r"whenever", r"unless", r"below", r"above", r"within",
    r"predicts?", r"predicted", r"fixing", r"fixes", r"fixed", r"preserves?", r"preserving",
    r"admits?", r"realizes?", r"irreducible", r"multiplicity", r"bare", r"centered", r"undetermined",
    r"always", r"never", r"none", r"only", r"not", r"no", r"cannot", r"necessary", r"necessarily",
    r"sufficient", r"exactly", r"strictly", r"generic(?:ally)?", r"almost",
)
QUANTIFIER_RE = re.compile(r"\b(?:" + "|".join(QUANTIFIER_PATTERNS) + r")\b", re.I)

# Words whose count may not change anywhere in the prose (meaning-bearing; copy-edits never need to touch them).
PROSE_LOCK_PATTERNS: Tuple[str, ...] = (
    r"if\s+and\s+only\s+if", r"only\s+if", r"necessary\s+and\s+sufficient", r"at\s+least", r"at\s+most",
    r"not", r"no", r"never", r"none", r"neither", r"nor", r"cannot", r"can't", r"won't", r"don't", r"doesn't",
    r"isn't", r"aren't", r"wasn't", r"weren't", r"didn't", r"without", r"unless", r"impossible",
    r"necessary", r"necessarily", r"sufficient", r"sufficiently", r"iff", r"exactly", r"strictly", r"unique", r"uniquely",
    r"exponential(?:ly)?", r"polynomial(?:ly)?", r"linear(?:ly)?", r"quadratic(?:ally)?", r"logarithmic(?:ally)?",
    r"sublinear(?:ly)?", r"superlinear(?:ly)?",
)
PROSE_LOCK_RE = re.compile(r"\b(?:" + "|".join(PROSE_LOCK_PATTERNS) + r")\b", re.I)

NUMBER_WORDS = {
    "zero": "0", "one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6", "seven": "7",
    "eight": "8", "nine": "9", "ten": "10", "eleven": "11", "twelve": "12", "thirteen": "13", "fourteen": "14",
    "fifteen": "15", "sixteen": "16", "seventeen": "17", "eighteen": "18", "nineteen": "19", "twenty": "20",
    "thirty": "30", "forty": "40", "fifty": "50", "hundred": "100", "thousand": "1000", "million": "1e6",
    "billion": "1e9", "twice": "x2", "double": "x2", "triple": "x3", "half": "1/2", "quarter": "1/4",
}
NUMBER_RE = re.compile(r"(?<![A-Za-z_])(\d+(?:[.,]\d+)*(?:[eE][-+]?\d+)?)(?![A-Za-z_]*\d)|\b(" + "|".join(NUMBER_WORDS) + r")\b", re.I)


@dataclass
class ProtectedToken:
    kind: str
    raw: str
    normalized: str
    line: int


def _line_of_offset(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


def _is_escaped(text: str, pos: int) -> bool:
    backslashes = 0
    idx = pos - 1
    while idx >= 0 and text[idx] == "\\":
        backslashes += 1
        idx -= 1
    return (backslashes % 2) == 1


def _strip_comments_preserve_lines(text: str) -> str:
    """Replace unescaped % comments with spaces while keeping line numbers intact."""
    out: List[str] = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "%" and not _is_escaped(text, i):
            while i < n and text[i] != "\n":
                out.append(" ")
                i += 1
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def _read_balanced(text: str, pos: int, open_ch: str, close_ch: str) -> Optional[Tuple[str, int]]:
    n = len(text)
    if pos >= n or text[pos] != open_ch:
        return None
    depth = 0
    i = pos
    while i < n:
        ch = text[i]
        if ch == "\\" and i + 1 < n:
            i += 2
            continue
        if ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                return text[pos : i + 1], i + 1
        i += 1
    return None


def _normalize_ws(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def _is_cite_cmd(name: str) -> bool:
    return ("cite" in name.lower()) and name not in NOT_CITE_CMDS and name not in REF_COMMANDS


def _normalize_cite_or_ref(cmd: str, opt_args: List[str], braced_args: List[str]) -> str:
    opts = "".join(_normalize_ws(o) for o in opt_args)
    if _is_cite_cmd(cmd) and len(braced_args) == 1:
        inner = braced_args[0][1:-1]
        keys = ",".join(k.strip() for k in inner.split(",") if k.strip())
        return f"\\{cmd}{opts}{{{keys}}}"
    bodies = "".join("{" + _normalize_ws(b[1:-1]) + "}" for b in braced_args)
    return f"\\{cmd}{opts}{bodies}"


def _read_definition(cleaned: str, i: int, cmd: str, start_after_name: int) -> Optional[Tuple[str, int]]:
    """Read a macro/environment definition starting at `\\cmd`; returns (raw, end)."""
    n = len(cleaned)
    j = start_after_name
    if j < n and cleaned[j] == "*":
        j += 1
    while j < n and cleaned[j] in " \t\n":
        j += 1
    if cmd in ("def", "gdef", "edef"):
        m = re.compile(r"\\[A-Za-z@]+|\\.").match(cleaned, j)
        if not m:
            return None
        j = m.end()
        k = cleaned.find("{", j)
        if k == -1:
            return None
        body = _read_balanced(cleaned, k, "{", "}")
        return (cleaned[i : body[1]], body[1]) if body else None
    groups = 0
    max_groups = {"newenvironment": 3, "renewenvironment": 3, "newtheorem": 2, "declaretheorem": 1}.get(cmd, 2)
    while j < n and groups < max_groups:
        while j < n and cleaned[j] in " \t\n":
            j += 1
        if j < n and cleaned[j] == "[":
            opt = _read_balanced(cleaned, j, "[", "]")
            if opt is None:
                break
            j = opt[1]
            continue
        if j < n and cleaned[j] == "{":
            grp = _read_balanced(cleaned, j, "{", "}")
            if grp is None:
                break
            j = grp[1]
            groups += 1
            continue
        if j < n and cleaned[j] == "\\" and groups == 0:  # \newcommand\foo{...}
            m = re.compile(r"\\[A-Za-z@]+").match(cleaned, j)
            if not m:
                break
            j = m.end()
            groups += 1
            continue
        break
    return (cleaned[i:j], j) if groups else None


def extract_protected_tokens(
    text: str,
    extra_cmds: FrozenSet[str] = frozenset(DEFAULT_EXTRA_PROTECTED_CMDS),
) -> List[ProtectedToken]:
    """Scan LaTeX source and extract all protected math/ref/cite/macro/definition tokens in order."""
    cleaned = _strip_comments_preserve_lines(text)
    tokens: List[ProtectedToken] = []
    n = len(cleaned)
    i = 0

    begin_re = re.compile(r"\\begin\{([A-Za-z*]+)\}")
    cmd_re = re.compile(r"\\([A-Za-z]+)")

    while i < n:
        if cleaned.startswith("\\begin{", i) and not _is_escaped(cleaned, i):
            m = begin_re.match(cleaned, i)
            if m and m.group(1) in MATH_ENVS:
                env = m.group(1)
                end_tag = f"\\end{{{env}}}"
                start_line = _line_of_offset(cleaned, i)
                if env == "subequations":
                    tokens.append(ProtectedToken("env_tag", m.group(0), m.group(0), start_line))
                    i = m.end()
                    continue
                depth = 1
                j = m.end()
                begin_tag = f"\\begin{{{env}}}"
                while j < n and depth > 0:
                    if cleaned.startswith(begin_tag, j) and not _is_escaped(cleaned, j):
                        depth += 1
                        j += len(begin_tag)
                    elif cleaned.startswith(end_tag, j) and not _is_escaped(cleaned, j):
                        depth -= 1
                        j += len(end_tag)
                    else:
                        j += 1
                raw = cleaned[i:j]
                tokens.append(ProtectedToken(f"env:{env}", raw, _normalize_ws(raw), start_line))
                i = j
                continue

        if cleaned.startswith("\\end{subequations}", i) and not _is_escaped(cleaned, i):
            tag = "\\end{subequations}"
            tokens.append(ProtectedToken("env_tag", tag, tag, _line_of_offset(cleaned, i)))
            i += len(tag)
            continue

        if cleaned.startswith("\\[", i) and not _is_escaped(cleaned, i):
            start_line = _line_of_offset(cleaned, i)
            j = i + 2
            while j < n:
                if cleaned.startswith("\\]", j) and not _is_escaped(cleaned, j):
                    j += 2
                    break
                j += 1
            raw = cleaned[i:j]
            tokens.append(ProtectedToken("display_bracket", raw, _normalize_ws(raw), start_line))
            i = j
            continue

        if cleaned.startswith("\\(", i) and not _is_escaped(cleaned, i):
            start_line = _line_of_offset(cleaned, i)
            j = i + 2
            while j < n:
                if cleaned.startswith("\\)", j) and not _is_escaped(cleaned, j):
                    j += 2
                    break
                j += 1
            raw = cleaned[i:j]
            tokens.append(ProtectedToken("inline_paren", raw, _normalize_ws(raw), start_line))
            i = j
            continue

        if cleaned[i] == "$" and not _is_escaped(cleaned, i):
            start_line = _line_of_offset(cleaned, i)
            if i + 1 < n and cleaned[i + 1] == "$":
                j = i + 2
                while j < n:
                    if cleaned[j] == "$" and j + 1 < n and cleaned[j + 1] == "$" and not _is_escaped(cleaned, j):
                        j += 2
                        break
                    j += 1
                raw = cleaned[i:j]
                tokens.append(ProtectedToken("display_dollar", raw, _normalize_ws(raw), start_line))
                i = j
                continue
            j = i + 1
            while j < n:
                if cleaned[j] == "$" and not _is_escaped(cleaned, j):
                    j += 1
                    break
                j += 1
            raw = cleaned[i:j]
            tokens.append(ProtectedToken("inline_dollar", raw, _normalize_ws(raw), start_line))
            i = j
            continue

        if cleaned[i] == "\\" and not _is_escaped(cleaned, i):
            m = cmd_re.match(cleaned, i)
            if m and m.group(1) in DEF_COMMANDS:
                cmd = m.group(1)
                rd = _read_definition(cleaned, i, cmd, m.end())
                if rd:
                    tokens.append(ProtectedToken("macro_def", rd[0], _normalize_ws(rd[0]), _line_of_offset(cleaned, i)))
                    i = rd[1]
                    continue
            if m and (m.group(1) in REF_COMMANDS or _is_cite_cmd(m.group(1)) or m.group(1) in extra_cmds):
                cmd = m.group(1)
                start_line = _line_of_offset(cleaned, i)
                j = m.end()
                if j < n and cleaned[j] == "*":
                    j += 1
                while j < n and cleaned[j].isspace():
                    j += 1
                opt_args: List[str] = []
                while j < n and cleaned[j] == "[":
                    opt = _read_balanced(cleaned, j, "[", "]")
                    if opt is None:
                        break
                    opt_args.append(opt[0])
                    j = opt[1]
                    while j < n and cleaned[j].isspace():
                        j += 1
                braced_args: List[str] = []
                j_last = j
                want = MULTI_ARG_REFS.get(cmd, 1)
                while j < n and cleaned[j] == "{":
                    brace = _read_balanced(cleaned, j, "{", "}")
                    if brace is None:
                        break
                    braced_args.append(brace[0])
                    j = brace[1]
                    j_last = j
                    if cmd not in extra_cmds and len(braced_args) >= want:
                        break
                    while j < n and cleaned[j] in " \t":
                        j += 1
                    if j >= n or cleaned[j] != "{":
                        j = j_last
                        break
                if braced_args or (cmd == "hyperref" and opt_args):
                    raw = cleaned[i:j_last]
                    norm = _normalize_cite_or_ref(cmd, opt_args, braced_args)
                    kind = "cite" if _is_cite_cmd(cmd) else ("ref_label" if cmd in REF_COMMANDS else f"macro:{cmd}")
                    tokens.append(ProtectedToken(kind, raw, norm, start_line))
                    i = j_last
                    continue

        i += 1

    return tokens


_LAYOUT_CMDS = r"(?:vspace|hspace|vskip|hskip|setlength|addtolength|resizebox|scalebox|includegraphics|rule|raisebox|linespread|fontsize|columnwidth|textwidth)"


def _mask_math_for_quantifiers(snippet: str) -> str:
    s = re.sub(r"\\begin\{(" + "|".join(re.escape(e) for e in sorted(MATH_ENVS)) + r")\}[\s\S]*?\\end\{\1\}", " <M> ", snippet)
    s = re.sub(r"\$\$[\s\S]*?\$\$|\\\[[\s\S]*?\\\]", " <M> ", s)
    s = re.sub(r"\\\([\s\S]*?\\\)", " <M> ", s)
    s = re.sub(r"(?<!\\)\$(?:\\\$|[^$])+?(?<!\\)\$", " <M> ", s)  # inline math may be hard-wrapped
    s = re.sub(r"\\(?:label|[A-Za-z]*ref|[A-Za-z]*cite[A-Za-z]*|extref|status|url|href|input|include)\*?(?:\[[^\]]*\])*(?:\{[^}]*\})+", " ", s)
    s = re.sub(r"\\" + _LAYOUT_CMDS + r"\*?(?:\[[^\]]*\])*(?:\{[^}]*\})*", " ", s)
    s = re.sub(r"\\(?:begin|end)\{[^}]*\}(?:\[[^\]]*\])?", " ", s)
    s = re.sub(r"\\(?:emph|textbf|textit|textrm|textsc|text|mbox)\{([^}]*)\}", r" \1 ", s)
    s = re.sub(r"\\[A-Za-z@]+\*?", " ", s)
    s = re.sub(r"\\.", " ", s)
    return s


def _claim_envs_for(text: str, extra: FrozenSet[str]) -> FrozenSet[str]:
    found = set(BASE_CLAIM_ENVS) | set(extra)
    for m in re.finditer(r"\\(?:newtheorem\*?|declaretheorem(?:\[[^\]]*\])?|spnewtheorem\*?)\s*\{([A-Za-z]+)\}", text):
        found.add(m.group(1))
    return frozenset(found)


def extract_claim_scopes(text: str, claim_envs: FrozenSet[str]) -> List[Dict[str, object]]:
    """Abstract + theorem-like scopes (incl. starred forms) with their ordered quantifier/modality tokens."""
    cleaned = _strip_comments_preserve_lines(text)
    scopes: List[Tuple[int, str, str]] = []
    env_pat = re.compile(r"\\begin\{(" + "|".join(sorted(re.escape(e) for e in claim_envs)) + r")(\*?)\}(?:\[[^\]]*\])?([\s\S]*?)\\end\{\1\2\}")
    occupied: List[Tuple[int, int]] = []
    for m in env_pat.finditer(cleaned):
        occupied.append((m.start(), m.end()))
        scopes.append((_line_of_offset(cleaned, m.start()), m.group(1) + m.group(2), m.group(3)))

    runin_pat = re.compile(
        r"(?:^|\n)[ \t]*\\(?:emph|textbf|textit|paragraph|letterhead)\{(?:Theorem|Proposition|Corollary|Lemma|Remark|Conjecture|Claim)\b[^}]*\}(?:(?!\n[ \t]*\n|\\section|\\letterhead)[\s\S])+",
    )
    for m in runin_pat.finditer(cleaned):
        if any(s <= m.start() < e for s, e in occupied):
            continue
        scopes.append((_line_of_offset(cleaned, m.start()), "runin_claim", m.group(0)))

    scopes.sort(key=lambda item: item[0])
    out: List[Dict[str, object]] = []
    for line, kind, raw_body in scopes:
        masked = _mask_math_for_quantifiers(raw_body)
        qtoks = [re.sub(r"\s+", " ", q.group(0).lower()).replace(" -", "-").replace("- ", "-") for q in QUANTIFIER_RE.finditer(masked)]
        out.append({"line": line, "scope": kind, "quantifiers": qtoks})
    return out


def compare_quantifier_scopes(before_scopes: List[Dict[str, object]], after_scopes: List[Dict[str, object]], file_label: str) -> List[Dict[str, object]]:
    mismatches: List[Dict[str, object]] = []
    if len(before_scopes) == len(after_scopes):
        for idx, (bs, as_) in enumerate(zip(before_scopes, after_scopes)):
            bq: List[str] = bs["quantifiers"]  # type: ignore[assignment]
            aq: List[str] = as_["quantifiers"]  # type: ignore[assignment]
            if bq != aq:
                matcher = difflib.SequenceMatcher(None, bq, aq, autojunk=False)
                diffs = [{"op": tag, "before": bq[i1:i2], "after": aq[j1:j2]} for tag, i1, i2, j1, j2 in matcher.get_opcodes() if tag != "equal"]
                mismatches.append({"file": file_label, "scope_index": idx, "scope": bs["scope"], "before_line": bs["line"], "after_line": as_["line"], "diffs": diffs})
    else:
        b_flat = [f"{s['scope']}:{q}" for s in before_scopes for q in s["quantifiers"]]  # type: ignore[attr-defined]
        a_flat = [f"{s['scope']}:{q}" for s in after_scopes for q in s["quantifiers"]]  # type: ignore[attr-defined]
        mismatches.append({"file": file_label, "op": "claim_scope_count_changed", "before_count": len(before_scopes), "after_count": len(after_scopes), "before_flat": b_flat[:40], "after_flat": a_flat[:40]})
    return mismatches


def _prose_only(text: str) -> str:
    """Prose with math, refs/cites/labels, layout commands and comments removed (for global word/number locks)."""
    cleaned = _strip_comments_preserve_lines(text)
    if "\\begin{document}" in cleaned:
        pre, body = cleaned.split("\\begin{document}", 1)
        title_bits = " ".join(re.findall(r"\\(?:title|caption)\s*\{([^{}]*)\}", pre))
        cleaned = title_bits + "\n" + body
    return _mask_math_for_quantifiers(cleaned)


def prose_lock_counts(text: str) -> Tuple[Counter, Counter]:
    prose = _prose_only(text)
    words = Counter(re.sub(r"\s+", " ", m.group(0).lower()) for m in PROSE_LOCK_RE.finditer(prose))
    nums: Counter = Counter()
    for m in NUMBER_RE.finditer(prose):
        if m.group(1):
            nums[m.group(1).replace(",", "")] += 1
        else:
            nums[NUMBER_WORDS[m.group(2).lower()]] += 1
    return words, nums


def compare_prose_locks(b_text: str, a_text: str, file_label: str) -> List[Dict[str, object]]:
    bw, bn = prose_lock_counts(b_text)
    aw, an = prose_lock_counts(a_text)
    out: List[Dict[str, object]] = []
    for kind, bc, ac in (("logical_word", bw, aw), ("prose_number", bn, an)):
        removed = dict(bc - ac)
        added = dict(ac - bc)
        if removed or added:
            out.append({"file": file_label, "kind": kind, "removed": removed, "added": added})
    return out


def compare_token_lists(before_tokens: List[ProtectedToken], after_tokens: List[ProtectedToken], file_label: str, allow_cite_changes: bool = False) -> List[Dict[str, object]]:
    b_filt = [t for t in before_tokens if not (allow_cite_changes and t.kind == "cite")]
    a_filt = [t for t in after_tokens if not (allow_cite_changes and t.kind == "cite")]
    b_norms = [f"{t.kind}::{t.normalized}" for t in b_filt]
    a_norms = [f"{t.kind}::{t.normalized}" for t in a_filt]
    matcher = difflib.SequenceMatcher(None, b_norms, a_norms, autojunk=False)
    mismatches: List[Dict[str, object]] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        b_slice = b_filt[i1:i2]
        a_slice = a_filt[j1:j2]
        mismatches.append({"file": file_label, "op": tag, "before_lines": [t.line for t in b_slice], "after_lines": [t.line for t in a_slice], "before_tokens": [asdict(t) for t in b_slice], "after_tokens": [asdict(t) for t in a_slice]})
    return mismatches


def check_file_pair(before_path: Path, after_path: Path, label: str, allow_cite_changes: bool, extra_cmds: FrozenSet[str], check_quantifiers: bool, prose_lock: bool, extra_claim_envs: FrozenSet[str]) -> Dict[str, object]:
    b_text = read_text(before_path)
    a_text = read_text(after_path)
    b_toks = extract_protected_tokens(b_text, extra_cmds=extra_cmds)
    a_toks = extract_protected_tokens(a_text, extra_cmds=extra_cmds)
    mismatches = compare_token_lists(b_toks, a_toks, label, allow_cite_changes=allow_cite_changes)

    q_mismatches: List[Dict[str, object]] = []
    if check_quantifiers:
        envs = _claim_envs_for(b_text + "\n" + a_text, extra_claim_envs)
        q_mismatches = compare_quantifier_scopes(extract_claim_scopes(b_text, envs), extract_claim_scopes(a_text, envs), label)
    p_mismatches = compare_prose_locks(b_text, a_text, label) if prose_lock else []

    return {
        "file": label,
        "before_token_count": len(b_toks),
        "after_token_count": len(a_toks),
        "passed": not mismatches and not q_mismatches and not p_mismatches,
        "mismatches": mismatches,
        "quantifier_mismatches": q_mismatches,
        "prose_lock_mismatches": p_mismatches,
    }


def _should_skip_tex(path: Path) -> bool:
    name = path.name.lower()
    return name in {"inventory.tex", "loadbearing.tex"} or name.endswith("_inventory.tex") or name.endswith("_loadbearing.tex")


def collect_tex_files(directory: Path) -> Dict[str, Path]:
    files: Dict[str, Path] = {}
    for p in iter_files([directory], [".tex"]):
        if not _should_skip_tex(p):
            files[str(p.relative_to(directory)).replace("\\", "/")] = p
    return files


def main() -> int:
    utf8_stdio()
    parser = argparse.ArgumentParser(description="Verify LaTeX math, label, ref, cite, macro, logical-word, number and quantifier integrity.")
    parser.add_argument("--before", type=Path, help="Original .tex file before editing")
    parser.add_argument("--after", type=Path, help="Edited .tex file after editing")
    parser.add_argument("--before-dir", type=Path, help="Directory of original .tex files")
    parser.add_argument("--after-dir", type=Path, help="Directory of edited .tex files")
    parser.add_argument("--allow-cite-changes", action="store_true", help="Allow \\cite{...} edits (for citation claim-fidelity stages)")
    parser.add_argument("--extra-protected-cmds", default=",".join(DEFAULT_EXTRA_PROTECTED_CMDS), help="Comma-separated custom macros to lock (default: extref,status,declareexternalref)")
    parser.add_argument("--claim-envs", default="", help="Comma-separated extra claim environments (\\newtheorem ones are detected automatically)")
    parser.add_argument("--check-quantifiers", action="store_true", default=True, help="(default) Verify ordered quantifiers/modalities inside abstract and claim scopes")
    parser.add_argument("--no-check-quantifiers", dest="check_quantifiers", action="store_false", help="Skip the claim-scope quantifier check")
    parser.add_argument("--no-prose-lock", dest="prose_lock", action="store_false", default=True, help="Skip the whole-document negation/strength-word and number lock")
    parser.add_argument("--json-out", type=Path, help="Optional path to write JSON report")
    args = parser.parse_args()

    extra_cmds = frozenset(c.strip().lstrip("\\") for c in args.extra_protected_cmds.split(",") if c.strip())
    extra_envs = frozenset(c.strip() for c in args.claim_envs.split(",") if c.strip())

    results: List[Dict[str, object]] = []
    notes: List[str] = []
    if args.before and args.after:
        if not args.before.is_file() or not args.after.is_file():
            print(json.dumps({"passed": False, "error": f"Input file not found: {args.before if not args.before.is_file() else args.after}"}), file=sys.stderr)
            return 2
        results.append(check_file_pair(args.before, args.after, args.after.name, args.allow_cite_changes, extra_cmds, args.check_quantifiers, args.prose_lock, extra_envs))
    elif args.before_dir and args.after_dir:
        for d in (args.before_dir, args.after_dir):
            if not d.is_dir():
                print(json.dumps({"passed": False, "error": f"Directory not found: {d}"}), file=sys.stderr)
                return 2
        before_map = collect_tex_files(args.before_dir)
        after_map = collect_tex_files(args.after_dir)
        if not before_map:
            print(json.dumps({"passed": False, "error": f"No .tex files under {args.before_dir}"}), file=sys.stderr)
            return 2
        for rel, b_path in before_map.items():
            a_path = after_map.get(rel)
            if a_path is None:
                results.append({"file": rel, "before_token_count": len(extract_protected_tokens(read_text(b_path), extra_cmds=extra_cmds)), "after_token_count": 0, "passed": False,
                                "mismatches": [{"file": rel, "op": "missing_after_file"}], "quantifier_mismatches": [], "prose_lock_mismatches": []})
            else:
                results.append(check_file_pair(b_path, a_path, rel, args.allow_cite_changes, extra_cmds, args.check_quantifiers, args.prose_lock, extra_envs))
        added = sorted(set(after_map) - set(before_map))
        if added:
            notes.append(f"files only present after editing (not checked): {added}")
    else:
        parser.error("Provide either (--before and --after) or (--before-dir and --after-dir).")

    all_passed = bool(results) and all(r["passed"] for r in results)
    payload = {
        "passed": all_passed,
        "allow_cite_changes": args.allow_cite_changes,
        "check_quantifiers": args.check_quantifiers,
        "prose_lock": args.prose_lock,
        "files_checked": len(results),
        "total_tokens_before": sum(int(r["before_token_count"]) for r in results),
        "total_tokens_after": sum(int(r["after_token_count"]) for r in results),
        "mismatches_count": sum(len(r["mismatches"]) for r in results),  # type: ignore[arg-type]
        "quantifier_mismatches_count": sum(len(r["quantifier_mismatches"]) for r in results),  # type: ignore[arg-type]
        "prose_lock_mismatches_count": sum(len(r["prose_lock_mismatches"]) for r in results),  # type: ignore[arg-type]
        "notes": notes,
        "files": results,
    }
    if args.json_out:
        write_new(args.json_out, json.dumps(payload, indent=2, ensure_ascii=False))
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0 if all_passed else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as exc:
        print(json.dumps({"passed": False, "error": f"{type(exc).__name__}: {exc}"}), file=sys.stderr)
        sys.exit(2)
