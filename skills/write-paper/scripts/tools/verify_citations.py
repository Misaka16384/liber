#!/usr/bin/env python3
r"""Deterministic LaTeX, BibTeX & rendered-.bbl citation auditor backed by INSPIRE-HEP, Crossref, doi.org & arXiv.

Design rules (v2.1):
1. Fail closed. An entry that could not be checked (network error, rate limit, no match) fails the
   gate unless `--allow-offline` is given; an empty or unreadable input fails the gate.
2. Never rewrite refs.bib wholesale. `--fix-braces` / `--upgrade-preprints` edit only the field
   values they change, keep @string/@comment/@preamble/comments/macros/`#` concatenation, keep the
   file's encoding and newlines, and leave a backup in `.wp_backups/`.
3. `verify-entry --query` only returns candidates. Only an identifier lookup (`--doi` / `--arxiv`,
   optionally cross-checked with `--title`) can return `verified: true`.
4. Bidirectional DOI + eprint cross-checking, full author-order verification, math-case bracing,
   rendered `*.bbl` audit, companion-work exemption (`COMPANION_EXEMPT`).

Exit codes: 0 = pass / verified, 1 = issues found / not verified, 2 = usage or tool error.
"""

from __future__ import annotations

import argparse
import difflib
import html
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _texio import collect_tex_tree, iter_files, read_text, read_text_file, strip_comments_text, utf8_stdio, write_back, write_new  # noqa: E402


USER_AGENT = "write-paper-citation-auditor/2.1 (opensource-academic-tool)"
CACHE_TTL_DAYS_DEFAULT = 30

# Only tokens that contain an uppercase letter are ever protected; bracing never changes letter case.
PROPER_NOUN_BRACES: List[str] = [
    "MPS", "PEPS", "MERA", "SPT", "CFT", "QFT", "TQFT", "QCD", "QED", "LSM", "KW", "KPZ",
    "RG", "DMRG", "TEBD", "VUMPS", "GNS", "AKLT", "SSH", "XY", "XXZ", "TFIM", "CSS",
    "Kitaev", "Lieb", "Schultz", "Mattis", "Kramers", "Wannier", "Frobenius", "Hensel",
    "Ising", "Heisenberg", "Weyl", "Majorana", "Dirac", "Pauli", "Hilbert", "Fock",
    "Floquet", "Markov", "Gaussian", "Abelian", "Yang", "Baxter", "Bethe", "Onsager",
    "Symplectic", "Lagrangian", "Hamiltonian", "Matsui", "Jordan", "Wigner", "Anyon",
    "QCA", "Arf", "Levi", "Levin", "Gu", "Bratteli", "Robinson", "Jones",
    "Chern", "Simons", "Goldstone", "Noether", "Schwinger", "Bogoliubov", "Thouless",
    "Haldane", "Berry", "Lindblad", "Calabi", "Yau", "Rydberg", "Gauss", "Stokes",
    "Poincare", "Kac", "Moody", "Virasoro", "Wess", "Zumino", "Witten", "1D", "2D", "3D",
]

MONTH_MACROS = {m: m for m in ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")}

BAD_STATUSES = {
    "IDENTIFIER_NOT_FOUND",
    "METADATA_MISMATCH",
    "EPRINT_DOI_MISMATCH",
    "EPRINT_NOT_FOUND",
    "AUTHOR_ORDER_MISMATCH",
    "UNVERIFIED_NO_MATCH",
}


# ---------------------------------------------------------------------------------------------
# HTTP with retries
# ---------------------------------------------------------------------------------------------

def _http_get_text(url: str, accept: str = "application/json", timeout: float = 20.0, attempts: int = 3) -> Tuple[int, str]:
    """GET with retries on timeouts, connection errors, 429 and 5xx. Returns (status, body); status -1 = network failure."""
    last: Tuple[int, str] = (-1, "")
    for k in range(attempts):
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": accept})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            if e.code == 429 or e.code >= 500:
                last = (e.code, "")
                retry_after = e.headers.get("Retry-After") if e.headers else None
                delay = min(10.0, float(retry_after)) if (retry_after and retry_after.isdigit()) else 1.5 * (2 ** k)
                time.sleep(delay)
                continue
            return e.code, ""
        except Exception as e:  # timeout, DNS, TLS, proxy ...
            last = (-1, str(e))
            time.sleep(1.5 * (2 ** k))
    return last


# ---------------------------------------------------------------------------------------------
# Normalisation helpers
# ---------------------------------------------------------------------------------------------

def _normalize_arxiv(raw: str) -> str:
    s = raw.strip()
    s = re.sub(r"^https?://arxiv\.org/(?:abs|pdf)/", "", s, flags=re.I)
    s = re.sub(r"^arXiv:", "", s, flags=re.I)
    s = re.sub(r"\.pdf$", "", s, flags=re.I)
    s = re.sub(r"v\d+$", "", s, flags=re.I)
    return s.strip()


def _normalize_doi(raw: str) -> str:
    return re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi:)", "", raw.strip(), flags=re.I).strip()


def _normalize_title(title: str) -> str:
    t = html.unescape(re.sub(r"<[^>]+>", " ", title))  # Crossref/Elsevier titles may carry JATS/MathML tags
    t = re.sub(r"\$[^$]*\$", " ", t)
    t = re.sub(r"\\[A-Za-z]+\{([^}]*)\}", r"\1", t)
    t = re.sub(r"\\[A-Za-z]+", " ", t)
    t = t.replace("{", "").replace("}", "")
    t = re.sub(r"[^A-Za-z0-9\s]", " ", t.lower())
    return re.sub(r"\s+", " ", t).strip()


def _title_similarity(a: str, b: str) -> float:
    na, nb = _normalize_title(a), _normalize_title(b)
    if not na or not nb:
        return 0.0
    seq = difflib.SequenceMatcher(None, na, nb).ratio()
    sa, sb = set(na.split()), set(nb.split())
    jacc = len(sa & sb) / max(1, len(sa | sb))
    return round(max(seq, jacc), 4)


def _query_title_containment(query: str, title: str) -> float:
    """Fraction of the candidate title's words that appear in a free-text query."""
    nq, nt = set(_normalize_title(query).split()), _normalize_title(title).split()
    if not nt:
        return 0.0
    return round(sum(1 for w in nt if w in nq) / len(nt), 4)


def _all_author_surnames(author_field: str) -> List[str]:
    if not author_field:
        return []
    out: List[str] = []
    for part in re.split(r"\s+and\s+", author_field.strip(), flags=re.I):
        clean = part.replace("{", "").replace("}", "").strip()
        if not clean or clean.lower() in ("others", "et al.", "et al"):
            continue
        if "," in clean:
            out.append(clean.split(",", 1)[0].strip().lower())
        else:
            toks = clean.split()
            if toks:
                out.append(toks[-1].strip().lower())
    return out


def _first_author_surname(author_field: str) -> str:
    surs = _all_author_surnames(author_field)
    return surs[0] if surs else ""


def _strip_tex_accents(s: str) -> str:
    import unicodedata
    t = re.sub(r"\\(?:[\"'\^`~=.ucvdHkrtb])\{?([A-Za-z])\}?", r"\1", s)
    t = unicodedata.normalize("NFKD", t).encode("ascii", "ignore").decode("ascii")
    return t


def _surname_norms(s: str) -> List[str]:
    full = re.sub(r"[^a-z]", "", _strip_tex_accents(s).lower())
    toks = [re.sub(r"[^a-z]", "", t) for t in re.split(r"[\s\-]+", _strip_tex_accents(s).lower())]
    toks = [t for t in toks if t]
    forms = {full}
    if toks:
        forms.add(toks[-1])  # "van der Waals" vs "Waals"
    forms |= {f.replace("ue", "u").replace("oe", "o").replace("ae", "a") for f in list(forms)}
    return [f for f in forms if f]


def _surname_match(a: str, b: str) -> bool:
    """Whole-name equality after accent/particle normalisation (never substring: Li != Alicea)."""
    if not a or not b:
        return False
    fa, fb = _surname_norms(a), _surname_norms(b)
    if not fa or not fb:  # non-Latin scripts: compare the raw strings
        return a.strip().lower() == b.strip().lower()
    return any(x == y and len(x) >= 2 for x in fa for y in fb)


# ---------------------------------------------------------------------------------------------
# Span-preserving BibTeX parser
# ---------------------------------------------------------------------------------------------

_ENTRY_START_RE = re.compile(r"@([A-Za-z]+)\s*([({])")
_NEXT_ENTRY_RE = re.compile(r"[ \t]*@[A-Za-z]+\s*[({]")


def _match_brace(text: str, i: int, end: int) -> int:
    """`text[i] == '{'`; index just past its matching `}` (or -1)."""
    d = 0
    j = i
    while j < end:
        c = text[j]
        if c == "\\":
            j += 2
            continue
        if c == "{":
            d += 1
        elif c == "}":
            d -= 1
            if d == 0:
                return j + 1
        j += 1
    return -1


def _scan_value(text: str, pos: int, end: int) -> Optional[Tuple[List[Tuple[str, str]], int]]:
    parts: List[Tuple[str, str]] = []
    i = pos
    while True:
        while i < end and text[i] in " \t\n":
            i += 1
        if i >= end:
            return None
        c = text[i]
        if c == "{":
            j = _match_brace(text, i, end)
            if j == -1:
                return None
            parts.append(("brace", text[i + 1 : j - 1]))
            i = j
        elif c == '"':
            j = i + 1
            d = 0
            while j < end:
                if text[j] == "\\":
                    j += 2
                    continue
                if text[j] == "{":
                    d += 1
                elif text[j] == "}":
                    d -= 1
                elif text[j] == '"' and d == 0:
                    break
                j += 1
            if j >= end:
                return None
            parts.append(("quote", text[i + 1 : j]))
            i = j + 1
        else:
            m = re.compile(r"[A-Za-z0-9_.:+\-/']+").match(text, i, end)
            if not m:
                return None
            parts.append(("bare", m.group(0)))
            i = m.end()
        k = i
        while k < end and text[k] in " \t\n":
            k += 1
        if k < end and text[k] == "#":
            i = k + 1
            continue
        return parts, i


def _expand_parts(parts: List[Tuple[str, str]], strings: Dict[str, str]) -> str:
    out = []
    for kind, val in parts:
        if kind == "bare":
            low = val.lower()
            out.append(val if val.isdigit() else strings.get(low, MONTH_MACROS.get(low, val)))
        else:
            out.append(val)
    return "".join(out)


def parse_bibtex(bib_text: str) -> Tuple[List[Dict[str, object]], List[str], List[Dict[str, object]]]:
    """Parse BibTeX with absolute spans for every field value. Returns (entries, duplicate_keys, parse_errors)."""
    entries: List[Dict[str, object]] = []
    strings: Dict[str, str] = {}
    seen_keys: Dict[str, int] = {}
    duplicate_keys: List[str] = []
    parse_errors: List[Dict[str, object]] = []
    n = len(bib_text)
    i = 0
    while True:
        m = _ENTRY_START_RE.search(bib_text, i)
        if not m:
            break
        at = m.start()
        etype_raw = m.group(1)
        etype = etype_raw.lower()
        open_c = m.group(2)
        close_c = "}" if open_c == "{" else ")"
        j = m.end()
        depth = 1
        broken = False
        while j < n and depth > 0:
            ch = bib_text[j]
            if ch == "\\":
                j += 2
                continue
            if ch == "\n" and etype not in ("comment", "preamble") and _NEXT_ENTRY_RE.match(bib_text, j + 1):
                broken = True  # a new entry starts while this one is still open: unbalanced braces
                break
            if ch == open_c or (open_c == "(" and ch == "{"):
                depth += 1
            elif ch == close_c or (open_c == "(" and ch == "}"):
                depth -= 1
            j += 1
        entry_end = j
        i = max(entry_end, at + 1)
        if etype in ("comment", "preamble"):
            continue
        body_start = m.end()
        body_end = entry_end - 1 if not broken else entry_end
        if etype == "string":
            sm = re.match(r"\s*([A-Za-z][\w\-:]*)\s*=\s*", bib_text[body_start:body_end])
            if sm:
                sv = _scan_value(bib_text, body_start + sm.end(), body_end)
                if sv:
                    strings[sm.group(1).lower()] = _expand_parts(sv[0], strings)
            continue

        comma = bib_text.find(",", body_start, body_end)
        if comma == -1:
            parse_errors.append({"at": bib_text.count("\n", 0, at) + 1, "detail": f"@{etype_raw} entry without key/fields"})
            continue
        key = bib_text[body_start:comma].strip()
        seen_keys[key.lower()] = seen_keys.get(key.lower(), 0) + 1
        if seen_keys[key.lower()] == 2:
            duplicate_keys.append(key)
        if broken:
            parse_errors.append({"key": key, "line": bib_text.count("\n", 0, at) + 1, "detail": "unbalanced braces: entry is not closed before the next @entry"})

        fields: Dict[str, str] = {}
        spans: Dict[str, Tuple[int, int, str]] = {}
        pos = comma + 1
        while pos < body_end:
            while pos < body_end and bib_text[pos] in " \t\n,":
                pos += 1
            if pos >= body_end:
                break
            fm = re.compile(r"([A-Za-z][A-Za-z0-9_:\-]*)\s*=\s*").match(bib_text, pos, body_end)
            if not fm:
                parse_errors.append({"key": key, "detail": f"cannot parse field near: {bib_text[pos:pos + 40]!r}"})
                break
            fname = fm.group(1).lower()
            sv = _scan_value(bib_text, fm.end(), body_end)
            if sv is None:
                parse_errors.append({"key": key, "field": fname, "detail": "unterminated field value"})
                break
            parts, vend = sv
            kind = parts[0][0] if len(parts) == 1 else "concat"
            fields[fname] = re.sub(r"\s+", " ", _expand_parts(parts, strings)).strip()
            spans[fname] = (fm.end(), vend, kind)
            pos = vend

        entries.append({
            "entry_type": etype,
            "key": key,
            "fields": fields,
            "raw": bib_text[at:entry_end],
            "start": at,
            "end": entry_end,
            "type_span": (at + 1, at + 1 + len(etype_raw)),
            "close_pos": None if broken else entry_end - 1,
            "spans": spans,
            "broken": broken,
        })
    return entries, duplicate_keys, parse_errors


def _field_edit(bib_text: str, entry: Dict[str, object], name: str, value: str) -> Optional[Tuple[int, int, str]]:
    spans: Dict[str, Tuple[int, int, str]] = entry["spans"]  # type: ignore[assignment]
    if name in spans:
        vs, ve, kind = spans[name]
        if kind == "quote":
            return vs, ve, '"' + value + '"'
        return vs, ve, "{" + value + "}"
    return None


def _insert_fields(bib_text: str, entry: Dict[str, object], new_fields: Dict[str, str]) -> Optional[Tuple[int, int, str]]:
    cp = entry.get("close_pos")
    if cp is None or not new_fields:
        return None
    k = int(cp) - 1
    while k > int(entry["start"]) and bib_text[k] in " \t\n":  # type: ignore[arg-type]
        k -= 1
    lead = "" if bib_text[k] == "," else ","
    ins = lead + "".join(f"\n  {f} = {{{v}}}," for f, v in new_fields.items())
    ins = ins.rstrip(",") + "\n"
    return k + 1, int(cp), ins


def apply_edits(text: str, edits: List[Tuple[int, int, str]]) -> str:
    for s, e, r in sorted(edits, key=lambda x: (x[0], x[1]), reverse=True):
        text = text[:s] + r + text[e:]
    return text


def format_bibtex_entry(etype: str, key: str, fields: Dict[str, str]) -> str:
    """Used only to print a brand-new entry for `verify-entry` (never to rewrite an existing .bib)."""
    order = ["author", "title", "journal", "volume", "number", "pages", "year", "month", "publisher", "booktitle", "eprint", "archiveprefix", "primaryclass", "doi", "url", "note"]
    lines = [f"@{etype}{{{key},"]
    written = set()
    for f in order:
        if f in fields and fields[f]:
            lines.append(f"  {f:<13} = {{{fields[f]}}},")
            written.add(f)
    for f, v in fields.items():
        if f not in written and v:
            lines.append(f"  {f:<13} = {{{v}}},")
    if len(lines) > 1:
        lines[-1] = lines[-1].rstrip(",")
    lines.append("}")
    return "\n".join(lines)


def _brace_depth_at(s: str, pos: int) -> int:
    d = 0
    i = 0
    while i < pos:
        if s[i] == "\\" and i + 1 < len(s):
            i += 2
            continue
        if s[i] == "{":
            d += 1
        elif s[i] == "}":
            d = max(0, d - 1)
        i += 1
    return d


def check_and_fix_title_braces(title: str) -> Tuple[str, List[str]]:
    """Brace unprotected capitalised physics proper nouns, acronyms & uppercase $math$ (letters are never changed)."""
    unprotected: List[str] = []
    fixed = title

    def _math_repl(m: re.Match) -> str:
        inner = m.group(0)
        if not re.search(r"[A-Z]", inner):
            return inner
        if _brace_depth_at(fixed, m.start()) > 0:
            return inner
        unprotected.append(inner)
        return "{" + inner + "}"

    fixed = re.sub(r"\$[^$\n]+\$", _math_repl, fixed)

    for token in PROPER_NOUN_BRACES:
        if not re.search(r"[A-Z]", token):
            continue
        pat = re.compile(rf"(?<!\{{)\b({re.escape(token)})\b(?!\}})")
        if pat.search(fixed):
            def _repl(m: re.Match) -> str:
                if _brace_depth_at(fixed, m.start()) > 0:
                    return m.group(0)
                unprotected.append(m.group(1))
                return "{" + m.group(1) + "}"

            fixed = pat.sub(_repl, fixed)

    return fixed, sorted(set(unprotected))


# ---------------------------------------------------------------------------------------------
# Remote lookups
# ---------------------------------------------------------------------------------------------

def _crossref_message_to_meta(msg: Dict[str, object], source: str, doi_fallback: str = "") -> Dict[str, object]:
    title = (msg.get("title") or [""])[0] if isinstance(msg.get("title"), list) else str(msg.get("title") or "")  # type: ignore[index]
    authors = [f"{a.get('family', '')}, {a.get('given', '')}".strip(", ") for a in (msg.get("author") or [])]  # type: ignore[union-attr]
    ct = msg.get("short-container-title") or msg.get("container-title") or [""]
    journal = ct[0] if isinstance(ct, list) and ct else str(ct or "")
    year = ""
    for date_key in ("published-print", "published-online", "issued", "created"):
        parts = (msg.get(date_key) or {}).get("date-parts", [[]])  # type: ignore[union-attr]
        if parts and parts[0] and parts[0][0]:
            year = str(parts[0][0])
            break
    abstract = re.sub(r"<[^>]+>", " ", str(msg.get("abstract", "") or ""))
    return {
        "source": source,
        "type": str(msg.get("type", "") or ""),
        "title": html.unescape(re.sub(r"<[^>]+>", "", title)),
        "authors": authors,
        "journal": journal,
        "volume": str(msg.get("volume", "") or ""),
        "pages": str(msg.get("page", "") or msg.get("article-number", "") or ""),
        "year": year,
        "doi": str(msg.get("DOI", "") or doi_fallback),
        "dois": [str(msg.get("DOI", "") or doi_fallback)],
        "abstract": re.sub(r"\s+", " ", abstract).strip(),
    }


def query_doi_org(doi: str) -> Tuple[str, Optional[Dict[str, object]]]:
    """Registration-agency-neutral lookup (DataCite/Zenodo/mEDRA/...) via doi.org content negotiation."""
    url = f"https://doi.org/{urllib.parse.quote(doi, safe='/')}"
    code, body = _http_get_text(url, accept="application/vnd.citationstyles.csl+json")
    if code == 404:
        return "IDENTIFIER_NOT_FOUND", None
    if code != 200:
        return "NETWORK_ERROR", None
    try:
        csl = json.loads(body)
        issued = (csl.get("issued") or {}).get("date-parts", [[None]])
        return "OK", _crossref_message_to_meta({
            "title": csl.get("title", ""),
            "author": csl.get("author", []),
            "container-title": [csl.get("container-title", "")] if csl.get("container-title") else [""],
            "volume": csl.get("volume", ""),
            "page": csl.get("page", ""),
            "issued": {"date-parts": issued},
            "DOI": csl.get("DOI", doi),
            "type": csl.get("type", ""),
        }, "doi.org", doi)
    except Exception:
        return "NETWORK_ERROR", None


def query_crossref_doi(doi: str) -> Tuple[str, Optional[Dict[str, object]]]:
    doi = _normalize_doi(doi)
    url = f"https://api.crossref.org/works/{urllib.parse.quote(doi)}"
    code, body = _http_get_text(url)
    if code == 404:
        return query_doi_org(doi)  # not a Crossref DOI (DataCite, Zenodo, arXiv's 10.48550, ...)
    if code != 200:
        return "NETWORK_ERROR", None
    try:
        return "OK", _crossref_message_to_meta(json.loads(body)["message"], "crossref", doi)
    except Exception:
        return "NETWORK_ERROR", None


def query_inspire(query_str: str, size: int = 1) -> Tuple[str, List[Dict[str, object]]]:
    url = f"https://inspirehep.net/api/literature?q={urllib.parse.quote(query_str)}&size={size}"
    code, body = _http_get_text(url)
    if code != 200:
        return "NETWORK_ERROR", []
    try:
        hits = json.loads(body).get("hits", {}).get("hits", [])
        if not hits:
            return "IDENTIFIER_NOT_FOUND", []
        out: List[Dict[str, object]] = []
        for h in hits:
            md = h.get("metadata", {})
            title = (md.get("titles") or [{}])[0].get("title", "")
            authors = [a.get("full_name", "") for a in md.get("authors", [])]
            dois = [d.get("value", "") for d in md.get("dois", []) if d.get("value")]
            arxivs = [e.get("value", "") for e in md.get("arxiv_eprints", []) if e.get("value")]
            pub = (md.get("publication_info") or [{}])[0]
            abstracts = [ab.get("value", "") for ab in md.get("abstracts", []) if ab.get("value")]
            year = str(pub.get("year") or md.get("preprint_date", "")[:4] or "")
            out.append({
                "source": "inspire",
                "title": title,
                "authors": authors,
                "journal": pub.get("journal_title", ""),
                "volume": str(pub.get("journal_volume", "") or ""),
                "pages": str(pub.get("page_start", "") or pub.get("artid", "") or ""),
                "year": year,
                "doi": dois[0] if dois else "",
                "dois": dois,
                "arxiv": arxivs[0] if arxivs else "",
                "abstract": re.sub(r"\s+", " ", abstracts[0]).strip() if abstracts else "",
            })
        return "OK", out
    except Exception:
        return "NETWORK_ERROR", []


def query_arxiv(arxiv_id: str) -> Tuple[str, Optional[Dict[str, object]]]:
    norm_id = _normalize_arxiv(arxiv_id)
    url = f"https://export.arxiv.org/api/query?id_list={urllib.parse.quote(norm_id)}"
    code, body = _http_get_text(url, accept="application/atom+xml")
    if code != 200:
        return ("IDENTIFIER_NOT_FOUND", None) if code in (400, 404) else ("NETWORK_ERROR", None)
    try:
        ns = {"atom": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}
        root = ET.fromstring(body)
        entries = root.findall("atom:entry", ns)
        if not entries:
            return "IDENTIFIER_NOT_FOUND", None
        e = entries[0]
        eid = (e.findtext("atom:id", default="", namespaces=ns) or "").strip()
        title_el = e.find("atom:title", ns)
        raw_title = (title_el.text or "").strip() if title_el is not None else ""
        if not raw_title or "/api/errors" in eid or raw_title == "Error":
            return "IDENTIFIER_NOT_FOUND", None
        title = re.sub(r"\s+", " ", raw_title)
        authors = [a.findtext("atom:name", default="", namespaces=ns).strip() for a in e.findall("atom:author", ns)]
        summary = re.sub(r"\s+", " ", e.findtext("atom:summary", default="", namespaces=ns)).strip()
        published = e.findtext("atom:published", default="", namespaces=ns)
        doi = e.findtext("arxiv:doi", default="", namespaces=ns).strip()
        jref = e.findtext("arxiv:journal_ref", default="", namespaces=ns).strip()
        return "OK", {
            "source": "arxiv",
            "title": title,
            "authors": authors,
            "journal": "",  # arXiv's free-text journal_ref is never used as a journal name
            "journal_ref": jref,
            "volume": "",
            "pages": "",
            "year": published[:4] if len(published) >= 4 else "",
            "doi": doi,
            "dois": [doi] if doi else [],
            "arxiv": norm_id,
            "abstract": summary,
        }
    except Exception:
        return "NETWORK_ERROR", None


def query_crossref_bib(query_text: str, rows: int = 3) -> Tuple[str, List[Dict[str, object]]]:
    url = f"https://api.crossref.org/works?query.bibliographic={urllib.parse.quote(query_text)}&rows={rows}"
    code, body = _http_get_text(url)
    if code != 200:
        return "NETWORK_ERROR", []
    try:
        items = json.loads(body).get("message", {}).get("items", [])
        return "OK", [_crossref_message_to_meta(msg, "crossref_search") for msg in items]
    except Exception:
        return "NETWORK_ERROR", []


def _extract_arxiv_from_fields(fields: Dict[str, str]) -> str:
    if fields.get("eprint"):
        return _normalize_arxiv(fields["eprint"])
    dm = re.match(r"^10\.48550/arxiv\.(.+)$", _normalize_doi(fields.get("doi", "")), re.I)
    if dm:
        return _normalize_arxiv(dm.group(1))
    for k in ("journal", "note", "url"):
        val = fields.get(k, "")
        m = re.search(r"arXiv:([0-9]{4}\.[0-9]{4,5}|[a-z-]+/[0-9]{7})", val, re.I)
        if m:
            return _normalize_arxiv(m.group(1))
    return ""


def _stamp(meta: Dict[str, object]) -> Dict[str, object]:
    meta = dict(meta)
    meta["_cached_at"] = time.time()
    return meta


def _fetch_doi_cached(doi: str, cache: Dict[str, Dict[str, object]]) -> Tuple[str, Optional[Dict[str, object]]]:
    cid = f"doi:{doi.lower()}"
    if cid in cache:
        return "OK", cache[cid]
    st, meta = query_crossref_doi(doi)
    if st == "OK" and meta:
        if not meta.get("abstract") or not meta.get("arxiv"):
            ins_st, ins_hits = query_inspire(f"doi:{doi}")
            if ins_st == "OK" and ins_hits:
                ins_meta = ins_hits[0]
                if ins_meta.get("abstract"):
                    meta["abstract"] = ins_meta["abstract"]
                if ins_meta.get("arxiv"):
                    meta["arxiv"] = ins_meta["arxiv"]
                meta["dois"] = sorted({*(meta.get("dois") or []), *(ins_meta.get("dois") or [])})  # type: ignore[misc]
        cache[cid] = _stamp(meta)
    time.sleep(0.10)
    return st, meta


def _fetch_arxiv_cached(arxiv_id: str, cache: Dict[str, Dict[str, object]]) -> Tuple[str, Optional[Dict[str, object]]]:
    cid = f"arxiv:{arxiv_id.lower()}"
    if cid in cache:
        return "OK", cache[cid]
    st, hits = query_inspire(f"arxiv:{arxiv_id}")
    meta = hits[0] if (st == "OK" and hits and hits[0].get("title")) else None
    if meta is None:
        st, meta = query_arxiv(arxiv_id)
    if st == "OK" and meta:
        cache[cid] = _stamp(meta)
    time.sleep(0.10)
    return st, meta


def _is_companion_exempt(entry: Dict[str, object]) -> bool:
    etype = str(entry.get("entry_type", "")).lower()
    key = str(entry.get("key", "")).lower()
    fields: Dict[str, str] = entry["fields"]  # type: ignore[assignment]
    raw = str(entry.get("raw", "")).lower()
    note = fields.get("note", "").lower()
    journal = fields.get("journal", "").lower()
    if etype == "unpublished":
        return True
    if "[unpublished-companion]" in raw:
        return True
    if any(tok in note for tok in ("in preparation", "companion", "unpublished", "to appear")) or (
        re.match(r"^paper[_-]?(?:[a-z]|[ivx]+|\d+)$", key) and not fields.get("doi") and not fields.get("eprint") and ("prep" in journal or "companion" in note or not journal)
    ):
        return True
    return False


def _result(key: str, status: str, sim: float, author_ok: bool, order_ok: bool, hygiene: List[str], api_meta: Optional[Dict[str, object]], **extra: object) -> Dict[str, object]:
    out: Dict[str, object] = {"key": key, "status": status, "title_similarity": sim, "author_match": author_ok, "author_order_ok": order_ok, "bib_hygiene": hygiene, "api_meta": api_meta}
    out.update(extra)
    return out


def verify_single_bib_entry(entry: Dict[str, object], cache: Dict[str, Dict[str, object]]) -> Dict[str, object]:
    fields: Dict[str, str] = entry["fields"]  # type: ignore[assignment]
    key = str(entry["key"])
    etype = str(entry.get("entry_type", "")).lower()
    doi = _normalize_doi(fields.get("doi", ""))
    arxiv_id = _extract_arxiv_from_fields(fields)
    if re.match(r"^10\.48550/arxiv\.", doi, re.I):
        doi = ""  # arXiv-issued DOI: verify through the arXiv record
    title = fields.get("title", "")
    author = fields.get("author", "")
    year = fields.get("year", "").strip()

    hygiene: List[str] = []
    if fields.get("eprint", "").strip() and re.search(r"v\d+$", fields["eprint"].strip(), re.I):
        hygiene.append("EPRINT_HAS_VERSION")
    if "arxiv" in fields.get("journal", "").lower():
        hygiene.append("PSEUDO_ARXIV_JOURNAL")
    if fields.get("eprint") and re.search(r"arXiv:", fields.get("note", ""), re.I):
        hygiene.append("DUPLICATE_ARXIV_FIELD")

    if _is_companion_exempt(entry):
        return _result(key, "COMPANION_EXEMPT", 1.0, True, True, hygiene, None)

    api_meta: Optional[Dict[str, object]] = None
    fetch_status = "NONE"
    if doi:
        fetch_status, api_meta = _fetch_doi_cached(doi, cache)
    elif arxiv_id:
        fetch_status, api_meta = _fetch_arxiv_cached(arxiv_id, cache)
    else:
        q = f"{title} {_first_author_surname(author)} {year}".strip()
        cid = f"title:{_normalize_title(title)}"
        if cid in cache:
            fetch_status, api_meta = "OK", cache[cid]
        else:
            st, cands = query_crossref_bib(q)
            fetch_status = st
            if st == "OK" and cands:
                best = max(cands, key=lambda c: _title_similarity(title, str(c.get("title", ""))))
                if _title_similarity(title, str(best.get("title", ""))) >= 0.72:
                    api_meta = best
                    cache[cid] = _stamp(best)
            time.sleep(0.10)

    if fetch_status == "IDENTIFIER_NOT_FOUND":
        return _result(key, "IDENTIFIER_NOT_FOUND", 0.0, False, False, hygiene, None)
    if fetch_status == "NETWORK_ERROR":
        return _result(key, "NETWORK_ERROR", 0.0, False, False, hygiene, None)
    if api_meta is None:
        return _result(key, "UNVERIFIED_NO_MATCH", 0.0, False, False, hygiene, None, diagnostic="no DOI/eprint and no search hit with a matching title")

    sim = _title_similarity(title, str(api_meta.get("title", ""))) if title else 1.0
    local_surnames = _all_author_surnames(author)
    local_first = local_surnames[0] if local_surnames else ""
    api_surnames = _all_author_surnames(" and ".join(api_meta.get("authors", [])))  # type: ignore[arg-type]

    author_ok = (not local_first) or any(_surname_match(local_first, s) for s in api_surnames[:4])
    author_order_ok = True
    if len(local_surnames) >= 2 and len(api_surnames) >= 2:
        k_cmp = min(4, len(local_surnames), len(api_surnames))
        same_first = _surname_match(local_surnames[0], api_surnames[0])
        same_set = sum(1 for ls in local_surnames[:k_cmp] if any(_surname_match(ls, as_) for as_ in api_surnames[:k_cmp])) >= min(2, k_cmp)
        if same_set and not same_first:
            author_order_ok = False
        elif same_set and not all(_surname_match(local_surnames[idx], api_surnames[idx]) for idx in range(k_cmp)):
            author_order_ok = False

    if doi and arxiv_id:
        api_arxiv = _normalize_arxiv(str(api_meta.get("arxiv") or ""))
        if api_arxiv and api_arxiv.lower() != arxiv_id.lower():
            return _result(key, "EPRINT_DOI_MISMATCH", sim, False, author_order_ok, hygiene, api_meta, diagnostic=f"DOI {doi} corresponds to arXiv:{api_arxiv}, not arXiv:{arxiv_id}")
        ax_st, ax_meta = _fetch_arxiv_cached(arxiv_id, cache)
        if ax_st == "IDENTIFIER_NOT_FOUND":
            return _result(key, "EPRINT_NOT_FOUND", sim, author_ok, author_order_ok, hygiene, api_meta, diagnostic=f"DOI {doi} verified, but arXiv:{arxiv_id} was not found.")
        if ax_st == "NETWORK_ERROR":
            hygiene.append("EPRINT_UNVERIFIED_NETWORK")
        if ax_st == "OK" and ax_meta:
            ax_dois = [_normalize_doi(str(d)).lower() for d in (ax_meta.get("dois") or ([ax_meta.get("doi")] if ax_meta.get("doi") else [])) if d]  # type: ignore[union-attr]
            if ax_dois and doi.lower() not in ax_dois:
                return _result(key, "EPRINT_DOI_MISMATCH", sim, False, author_order_ok, hygiene, api_meta, diagnostic=f"arXiv:{arxiv_id} is linked to DOI(s) {ax_dois}, not DOI {doi}", arxiv_meta=ax_meta)
            ax_title = str(ax_meta.get("title", ""))
            doi_title = str(api_meta.get("title", ""))
            cross_sim = _title_similarity(doi_title, ax_title)
            local_vs_ax = _title_similarity(title, ax_title) if title else cross_sim
            ax_surnames = _all_author_surnames(" and ".join(ax_meta.get("authors", [])))  # type: ignore[arg-type]
            ax_author_ok = (not local_first) or any(_surname_match(local_first, s) for s in ax_surnames[:4])
            if (cross_sim < 0.65 and local_vs_ax < 0.65) or not ax_author_ok:
                return _result(key, "EPRINT_DOI_MISMATCH", sim, False, author_order_ok, hygiene, api_meta, diagnostic=f"DOI title ('{doi_title[:80]}') != arXiv:{arxiv_id} title ('{ax_title[:80]}')", arxiv_meta=ax_meta)

    if (sim < 0.72 or (not author_ok and sim < 0.90)) and etype == "book":
        st_b, cands_b = query_crossref_bib(f"{title} {local_first} {year}".strip())
        if st_b == "OK" and cands_b:
            best_b = max(cands_b, key=lambda c: _title_similarity(title, str(c.get("title", ""))))
            if _title_similarity(title, str(best_b.get("title", ""))) >= 0.75:
                return _result(key, "BOOK_CHAPTER_DOI_WARNING", _title_similarity(title, str(best_b.get("title", ""))), True, author_order_ok, hygiene, best_b,
                               diagnostic=f"@book DOI {doi} resolves to a sub-unit/chapter ('{str(api_meta.get('title', ''))[:60]}'), while book title verified via search.")

    if sim < 0.72 or (not author_ok and sim < 0.90):
        return _result(key, "METADATA_MISMATCH", sim, author_ok, author_order_ok, hygiene, api_meta)
    if not author_order_ok:
        return _result(key, "AUTHOR_ORDER_MISMATCH", sim, author_ok, False, hygiene, api_meta)

    is_preprint_only = (not doi) and bool(arxiv_id) and (not fields.get("journal") or "arxiv" in fields.get("journal", "").lower())
    status = "PREPRINT_UPGRADABLE" if (is_preprint_only and api_meta.get("doi") and not re.match(r"^10\.48550/", str(api_meta.get("doi")), re.I)) else "VERIFIED_EXACT"
    return _result(key, status, sim, author_ok, True, hygiene, api_meta)


# ---------------------------------------------------------------------------------------------
# TeX side
# ---------------------------------------------------------------------------------------------

_CITE_RE = re.compile(r"\\([A-Za-z]*cite[A-Za-z]*|nocite)\*?(?:\s*\[[^\]]*\]){0,2}\s*\{([^{}]*)\}")
_NOT_CITE_CMDS = {"bibcite", "citation", "citenamefont", "citestyle", "citeform", "citepunct", "citeleft", "citeright", "citeindextrue", "citeindexfalse"}


def _should_skip_tex(path: Path) -> bool:
    name = path.name.lower()
    return name in {"inventory.tex", "loadbearing.tex"} or name.endswith("_inventory.tex") or name.endswith("_loadbearing.tex")


def manuscript_tex_files(tex_dir: Path, mains: Optional[List[Path]] = None) -> List[Path]:
    """Prefer the \\input tree of main.tex (+ supplement.tex); fall back to every .tex under tex_dir."""
    entry = [p for p in (mains or [tex_dir / "main.tex", tex_dir / "supplement.tex"]) if p.is_file()]
    files = collect_tex_tree(entry) if entry else iter_files([tex_dir], [".tex"])
    return [p for p in files if not _should_skip_tex(p)]


def extract_tex_citations(tex_files: List[Path], tex_dir: Path) -> Tuple[List[str], List[str], Dict[str, List[str]], List[str], bool]:
    cited: List[str] = []
    todos: List[str] = []
    locations: Dict[str, List[str]] = {}
    bare_sm_mentions: List[str] = []
    nocite_all = False

    for p in tex_files:
        try:
            rel = str(p.relative_to(tex_dir)).replace("\\", "/")
        except ValueError:
            rel = str(p).replace("\\", "/")
        is_supp = "supp" in p.name.lower()
        code = strip_comments_text(read_text(p))
        for m in _CITE_RE.finditer(code):
            if m.group(1) in _NOT_CITE_CMDS:
                continue
            line_no = code.count("\n", 0, m.start()) + 1
            for raw_k in m.group(2).split(","):
                k = raw_k.strip()
                if not k or "#" in k:
                    continue
                if k == "*":
                    nocite_all = True
                    continue
                locations.setdefault(k, []).append(f"{rel}:{line_no}")
                if k.upper().startswith("TODO:"):
                    if k not in todos:
                        todos.append(k)
                elif k not in cited:
                    cited.append(k)
        for idx, line in enumerate(code.split("\n"), 1):
            if (not is_supp) and not _CITE_RE.search(line) and re.search(r"\b(?:SM\s+Sec\.|Supplemental\s+Material)\b", line):
                bare_sm_mentions.append(f"{rel}:{idx}")
    return cited, todos, locations, bare_sm_mentions, nocite_all


def audit_bbl_files(tex_dir: Path, entry_map: Dict[str, Dict[str, object]]) -> List[Dict[str, object]]:
    """Scan generated *.bbl files for rendered citation defects (duplicate arXiv:, math downcasing, lowercase nouns)."""
    issues: List[Dict[str, object]] = []
    bbl_paths = [p for p in sorted(tex_dir.glob("*.bbl")) if p.is_file()]
    bibitem_re = re.compile(r"\\bibitem\s*(?:\[[\s\S]*?\])?\s*\{([^}]+)\}([\s\S]*?)(?=\\bibitem\b|\\end\{thebibliography\}|\Z)")

    for bbl in bbl_paths:
        txt = read_text(bbl)
        for m in bibitem_re.finditer(txt):
            key = m.group(1).strip()
            body = re.sub(r"\s+", " ", m.group(2)).strip()
            ax_hits = re.findall(r"arXiv:\s*(?:[0-9]{4}\.[0-9]{4,5}|[a-z-]+/[0-9]{7}|[0-9]{7})", body, flags=re.I)
            if len(ax_hits) >= 2:
                issues.append({"bbl": bbl.name, "key": key, "kind": "BBL_DUPLICATE_ARXIV", "detail": f"Found {len(ax_hits)} arXiv strings: {ax_hits}"})
            bib_entry = entry_map.get(key.lower())
            if bib_entry:
                bib_title = str(bib_entry["fields"].get("title", ""))  # type: ignore[index]
                bib_maths = re.findall(r"\$[^$]+\$", bib_title)
                bbl_maths = re.findall(r"\$[^$]+\$", body)
                for bm in bib_maths:
                    if re.search(r"[A-Z]", bm) and bm not in bbl_maths and bm.lower() in [m_.lower() for m_ in bbl_maths]:
                        issues.append({"bbl": bbl.name, "key": key, "kind": "BBL_MATH_CASE_COLLAPSE", "detail": f".bib '{bm}' rendered lowercased in .bbl"})
            low_noun_m = re.search(r"\b(?:hilbert|fock|kramers|wannier|kitaev|matsui|heisenberg|majorana)\b", body)
            if low_noun_m:
                issues.append({"bbl": bbl.name, "key": key, "kind": "BBL_UNBRACED_PROPER_NOUN", "detail": f"Lowercased proper noun '{low_noun_m.group(0)}' in rendered .bbl"})
            part_m = re.search(r"\bPart\s+[IVX]+\.\s+[a-z]", body)
            if part_m:
                issues.append({"bbl": bbl.name, "key": key, "kind": "BBL_PART_LOWERCASE", "detail": f"Lowercased post-Part title '{part_m.group(0)}'"})
    return issues


# ---------------------------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------------------------

def load_cache(path: Optional[Path], ttl_days: float) -> Tuple[Dict[str, Dict[str, object]], List[str]]:
    notes: List[str] = []
    if not path or not path.exists():
        return {}, notes
    try:
        raw = json.loads(read_text(path))
        if not isinstance(raw, dict):
            raise ValueError("cache is not a JSON object")
    except Exception as exc:
        notes.append(f"ignored unreadable cache {path}: {exc}")
        return {}, notes
    horizon = time.time() - ttl_days * 86400.0
    fresh = {k: v for k, v in raw.items() if isinstance(v, dict) and float(v.get("_cached_at", 0) or 0) >= horizon}
    if len(fresh) < len(raw):
        notes.append(f"dropped {len(raw) - len(fresh)} cache entries older than {ttl_days} days")
    return fresh, notes


def save_cache(path: Optional[Path], cache: Dict[str, Dict[str, object]]) -> None:
    if path:
        write_new(path, json.dumps(cache, indent=2, ensure_ascii=False))


# ---------------------------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------------------------

def _meta_to_bibtex(meta: Dict[str, object]) -> str:
    first_sur = _first_author_surname((meta.get("authors") or [""])[0]) or "ref"  # type: ignore[index]
    first_sur = re.sub(r"[^A-Za-z]", "", _strip_tex_accents(first_sur)) or "ref"
    yr = str(meta.get("year") or "XXXX")
    skey = f"{first_sur.capitalize()}{yr}"
    etype = "article" if meta.get("journal") else "misc"
    fields = {
        "author": " and ".join(meta.get("authors", [])),  # type: ignore[arg-type]
        "title": str(meta.get("title", "")),
        "journal": str(meta.get("journal", "")),
        "volume": str(meta.get("volume", "")),
        "pages": str(meta.get("pages", "")),
        "year": yr,
        "doi": str(meta.get("doi", "")),
        "eprint": str(meta.get("arxiv", "")),
        "archiveprefix": "arXiv" if meta.get("arxiv") else "",
    }
    fields["title"], _ = check_and_fix_title_braces(fields["title"])
    return format_bibtex_entry(etype, skey, fields)


def cmd_verify_entry(args: argparse.Namespace) -> int:
    cache, _ = load_cache(args.cache, args.cache_ttl_days)
    out: Dict[str, object]
    if args.doi or args.arxiv:
        if args.doi:
            dnorm = _normalize_doi(args.doi)
            am = re.match(r"^10\.48550/arxiv\.(.+)$", dnorm, re.I)
            status, meta = _fetch_arxiv_cached(_normalize_arxiv(am.group(1)), cache) if am else _fetch_doi_cached(dnorm, cache)
        else:
            status, meta = _fetch_arxiv_cached(_normalize_arxiv(args.arxiv), cache)
        verified = status == "OK" and meta is not None
        sim = None
        if verified and args.title and meta is not None:
            sim = _title_similarity(args.title, str(meta.get("title", "")))
            if sim < 0.72:
                verified = False
                status = "METADATA_MISMATCH"
        out = {
            "verified": verified,
            "status": "VERIFIED_EXACT" if verified else status,
            "title_similarity": sim,
            "metadata": meta,
            "bibtex": _meta_to_bibtex(meta) if (verified and meta) else "",
        }
    elif args.query:
        st_i, hits_i = query_inspire(args.query, size=3)
        st_c, hits_c = query_crossref_bib(args.query, rows=3)
        cands: List[Dict[str, object]] = []
        for h in (hits_i if st_i == "OK" else []) + (hits_c if st_c == "OK" else []):
            score = _title_similarity(args.title, str(h.get("title", ""))) if args.title else _query_title_containment(args.query, str(h.get("title", "")))
            cands.append({
                "title": h.get("title"),
                "authors": (h.get("authors") or [])[:4],  # type: ignore[index]
                "year": h.get("year"),
                "doi": h.get("doi"),
                "arxiv": h.get("arxiv", ""),
                "source": h.get("source"),
                "match_score": score,
            })
        cands.sort(key=lambda c: float(c["match_score"] or 0), reverse=True)  # type: ignore[arg-type]
        network = st_i == "NETWORK_ERROR" and st_c == "NETWORK_ERROR"
        out = {
            "verified": False,
            "status": "NETWORK_ERROR" if network else ("CANDIDATES_ONLY" if cands else "UNVERIFIED_NO_MATCH"),
            "candidates": cands[:5],
            "next_step": "A search hit is not a verification. Pick the candidate whose title/authors/year match the claim, "
                         "then re-run with --doi <doi> (or --arxiv <id>) --title \"<exact title>\" and use its bibtex only if verified=true.",
            "bibtex": "",
        }
    else:
        print(json.dumps({"verified": False, "error": "Provide --doi, --arxiv, or --query"}), file=sys.stderr)
        return 2

    save_cache(args.cache, cache)
    if args.json_out:
        write_new(args.json_out, json.dumps(out, indent=2, ensure_ascii=False))
    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0 if out.get("verified") else 1


def cmd_audit(args: argparse.Namespace) -> int:
    errors: List[str] = []
    if not args.tex_dir.is_dir():
        print(json.dumps({"passed": False, "error": f"--tex-dir not found: {args.tex_dir}"}), file=sys.stderr)
        return 2
    tex_files = manuscript_tex_files(args.tex_dir, [Path(m) for m in (args.main or [])] or None)
    if not tex_files:
        errors.append(f"no .tex files found under {args.tex_dir}")
    cited_keys, todo_placeholders, cite_locs, bare_sm_mentions, nocite_all = extract_tex_citations(tex_files, args.tex_dir)

    bib_tf = None
    bib_text = ""
    if args.bib.exists():
        bib_tf = read_text_file(args.bib)
        bib_text = bib_tf.text
    elif cited_keys:
        errors.append(f"bibliography file not found: {args.bib}")
    entries, dup_keys, parse_errors = parse_bibtex(bib_text)
    entry_map = {str(e["key"]).lower(): e for e in entries}

    if nocite_all:
        for e in entries:
            if str(e["key"]) not in cited_keys:
                cited_keys.append(str(e["key"]))
    cited_lower = {k.lower() for k in cited_keys}

    doi_seen: Dict[str, List[str]] = {}
    arxiv_seen: Dict[str, List[str]] = {}
    unprotected_braces: Dict[str, List[str]] = {}
    edits: List[Tuple[int, int, str]] = []
    skipped_edits: List[str] = []
    for e in entries:
        f: Dict[str, str] = e["fields"]  # type: ignore[assignment]
        k = str(e["key"])
        if f.get("doi"):
            doi_seen.setdefault(_normalize_doi(f["doi"]).lower(), []).append(k)
        ax = _extract_arxiv_from_fields(f)
        if ax:
            arxiv_seen.setdefault(ax.lower(), []).append(k)
        spans: Dict[str, Tuple[int, int, str]] = e["spans"]  # type: ignore[assignment]
        if "title" in spans:
            raw_title = bib_text[spans["title"][0] : spans["title"][1]]
            kind = spans["title"][2]
            literal = raw_title[1:-1] if kind in ("brace", "quote") else raw_title
            fixed_t, unp = check_and_fix_title_braces(literal)
            if unp:
                unprotected_braces[k] = unp
                if args.fix_braces:
                    if e["broken"] or kind not in ("brace", "quote"):
                        skipped_edits.append(f"{k}: title is not a single literal value (macro/concatenation) or entry is malformed; not rewritten")
                    else:
                        fe = _field_edit(bib_text, e, "title", fixed_t)
                        if fe:
                            edits.append(fe)

    dup_dois = {d: ks for d, ks in doi_seen.items() if len(ks) > 1}
    dup_arxiv = {a: ks for a, ks in arxiv_seen.items() if len(ks) > 1}
    undefined_keys = [k for k in cited_keys if k.lower() not in entry_map]
    case_mismatched = [k for k in cited_keys if k.lower() in entry_map and str(entry_map[k.lower()]["key"]) != k]
    unused_keys = [str(e["key"]) for e in entries if str(e["key"]).lower() not in cited_lower]

    cache, cache_notes = load_cache(args.cache, args.cache_ttl_days)

    verified_entries: List[Dict[str, object]] = []
    upgraded_keys: List[str] = []
    for e in entries:
        k = str(e["key"])
        res = verify_single_bib_entry(e, cache)
        res["is_cited"] = k.lower() in cited_lower
        res["cite_locations"] = cite_locs.get(k, [])
        verified_entries.append(res)
        if args.upgrade_preprints and res["status"] == "PREPRINT_UPGRADABLE" and res.get("api_meta"):
            am: Dict[str, object] = res["api_meta"]  # type: ignore[assignment]
            if e["broken"]:
                skipped_edits.append(f"{k}: malformed entry, preprint not upgraded")
                continue
            # Only publisher metadata (resolved from the DOI) may fill journal/volume/pages/year.
            st_p, pub = _fetch_doi_cached(_normalize_doi(str(am.get("doi", ""))), cache)
            if st_p != "OK" or not pub or not pub.get("journal"):
                skipped_edits.append(f"{k}: published DOI {am.get('doi')} could not be resolved to journal metadata; not upgraded")
                continue
            new_vals = {"journal": str(pub.get("journal", "")), "volume": str(pub.get("volume", "")), "pages": str(pub.get("pages", "")), "year": str(pub.get("year", "")), "doi": str(pub.get("doi", ""))}
            spans = e["spans"]  # type: ignore[assignment]
            inserts: Dict[str, str] = {}
            for fname, fval in new_vals.items():
                if not fval:
                    continue
                if fname in spans:  # type: ignore[operator]
                    fe = _field_edit(bib_text, e, fname, fval)
                    if fe:
                        edits.append(fe)
                else:
                    inserts[fname] = fval
            ins = _insert_fields(bib_text, e, inserts)
            if ins:
                edits.append(ins)
            if str(e["entry_type"]) in ("misc", "unpublished", "online"):
                ts, te = e["type_span"]  # type: ignore[misc]
                edits.append((ts, te, "article"))
            upgraded_keys.append(k)

    bib_backup = None
    if edits and bib_tf is not None:
        new_text = apply_edits(bib_text, edits)
        bib_backup = write_back(bib_tf, new_text)

    save_cache(args.cache, cache)

    bbl_issues = audit_bbl_files(args.tex_dir, entry_map)
    bib_hygiene_issues = [{"key": r["key"], "issues": r["bib_hygiene"]} for r in verified_entries if r.get("bib_hygiene") and r.get("is_cited")]

    bad = set(BAD_STATUSES)
    if not args.allow_offline:
        bad.add("NETWORK_ERROR")
    bad_cited = [r for r in verified_entries if r["is_cited"] and r["status"] in bad]
    network_unverified = [r["key"] for r in verified_entries if r["is_cited"] and r["status"] == "NETWORK_ERROR"]
    all_ok = (
        not errors
        and not parse_errors
        and not undefined_keys
        and not case_mismatched
        and not todo_placeholders
        and not bad_cited
        and not dup_keys
        and not bbl_issues
        and not bib_hygiene_issues
    )

    payload = {
        "passed": all_ok,
        "errors": errors,
        "tex_files_scanned": [str(p).replace("\\", "/") for p in tex_files],
        "cited_keys_count": len(cited_keys),
        "bib_entries_count": len(entries),
        "bib_parse_errors": parse_errors,
        "todo_placeholders": [{"key": t, "locations": cite_locs.get(t, [])} for t in todo_placeholders],
        "undefined_keys": [{"key": u, "locations": cite_locs.get(u, [])} for u in undefined_keys],
        "case_mismatched_keys": case_mismatched,
        "unused_bib_keys": unused_keys,
        "duplicate_bib_keys": dup_keys,
        "duplicate_dois": dup_dois,
        "duplicate_arxiv_ids": dup_arxiv,
        "unprotected_title_braces": unprotected_braces,
        "upgraded_preprints": upgraded_keys,
        "skipped_bib_edits": skipped_edits,
        "bib_backup": str(bib_backup).replace("\\", "/") if bib_backup else None,
        "network_unverified_keys": network_unverified,
        "offline_allowed": bool(args.allow_offline),
        "cache_notes": cache_notes,
        "bib_hygiene_issues": bib_hygiene_issues,
        "bbl_issues": bbl_issues,
        "bare_sm_mentions": bare_sm_mentions,
        "entries": verified_entries,
    }

    if args.json_out:
        write_new(args.json_out, json.dumps(payload, indent=2, ensure_ascii=False))

    if args.md_out:
        lines = [
            "# Citation & Rendered `.bbl` Verification Audit Report (v2.1)",
            "",
            f"- **Audit Status**: `{'PASS' if all_ok else 'ISSUES_FOUND'}`",
            f"- **Cited Keys**: {len(cited_keys)} | **BibTeX Entries**: {len(entries)} | **TeX files scanned**: {len(tex_files)}",
            f"- **Critical Bad Citations**: {len(bad_cited)} | **Unverifiable (network)**: {len(network_unverified)} | **`.bbl` Render Defects**: {len(bbl_issues)} | **BibTeX Hygiene Issues**: {len(bib_hygiene_issues)}",
            "",
        ]
        if errors or parse_errors:
            lines += ["## Input Errors", ""] + [f"- {x}" for x in errors] + [f"- BibTeX parse: {pe}" for pe in parse_errors] + [""]
        lines += [
            "| BibTeX Key | Cited? | API Status | Title Sim | Author Match | Order OK | Identifier |",
            "| :--- | :---: | :--- | :---: | :---: | :---: | :--- |",
        ]
        for r in verified_entries:
            am = r.get("api_meta") or {}
            ident = am.get("doi") or am.get("arxiv") or "—"  # type: ignore[union-attr]
            lines.append(f"| `{r['key']}` | {'Yes' if r['is_cited'] else 'No'} | `{r['status']}` | {r['title_similarity']} | {'Yes' if r['author_match'] else 'No'} | {'Yes' if r.get('author_order_ok', True) else 'No'} | `{ident}` |")
        if bad_cited:
            lines += ["", "## Critical Suspect / Mismatched / Unverifiable Citations", ""]
            for r in bad_cited:
                lines.append(f"- `{r['key']}` (`{r['status']}`) at {r['cite_locations']} {r.get('diagnostic', '')}")
        if undefined_keys or case_mismatched:
            lines += ["", "## Undefined / Case-Mismatched Keys", ""] + [f"- `{u}` at {cite_locs.get(u, [])}" for u in undefined_keys] + [f"- `{c}` (case differs from .bib)" for c in case_mismatched]
        if bbl_issues:
            lines += ["", "## Rendered `.bbl` Defects", ""] + [f"- `{b['bbl']}` `{b['key']}` (`{b['kind']}`): {b['detail']}" for b in bbl_issues]
        if bib_hygiene_issues:
            lines += ["", "## BibTeX Hygiene Warnings (Fix Before `.bbl` Build)", ""] + [f"- `{bh['key']}`: {bh['issues']}" for bh in bib_hygiene_issues]
        if todo_placeholders:
            lines += ["", "## Unresolved TODO Citations", ""] + [f"- `{t}` at {cite_locs.get(t, [])}" for t in todo_placeholders]
        if skipped_edits or bib_backup:
            lines += ["", "## refs.bib Edits", ""] + ([f"- backup of the previous refs.bib: `{bib_backup}`"] if bib_backup else []) + [f"- skipped: {s}" for s in skipped_edits]
        write_new(args.md_out, "\n".join(lines) + "\n")

    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0 if all_ok else 1


def main() -> int:
    utf8_stdio()
    parser = argparse.ArgumentParser(description="Verify LaTeX citations, .bib & .bbl against INSPIRE/Crossref/doi.org/arXiv (v2.1).")
    sub = parser.add_subparsers(dest="cmd", required=True)

    pa = sub.add_parser("audit", help="Audit all .tex citations, .bib entries & rendered .bbl files")
    pa.add_argument("--tex-dir", type=Path, required=True)
    pa.add_argument("--bib", type=Path, required=True)
    pa.add_argument("--main", nargs="*", help="Entry .tex files whose \\input tree is the manuscript (default: main.tex + supplement.tex in --tex-dir)")
    pa.add_argument("--cache", type=Path)
    pa.add_argument("--cache-ttl-days", type=float, default=CACHE_TTL_DAYS_DEFAULT)
    pa.add_argument("--fix-braces", action="store_true", help="Brace-protect proper nouns/uppercase math in titles (in-place field edits + backup)")
    pa.add_argument("--upgrade-preprints", action="store_true", help="Add journal/volume/pages/DOI from publisher metadata to arXiv-only entries (in-place + backup)")
    pa.add_argument("--allow-offline", action="store_true", help="Do not fail the gate on entries that could not be checked because of network errors")
    pa.add_argument("--json-out", type=Path)
    pa.add_argument("--md-out", type=Path)

    pv = sub.add_parser("verify-entry", help="Verify a single reference by identifier, or list candidates for a free-text query")
    pv.add_argument("--doi")
    pv.add_argument("--arxiv")
    pv.add_argument("--query", help="Free-text search: returns candidates only, never verified=true")
    pv.add_argument("--title", help="Expected title; an identifier lookup only verifies if the record's title matches")
    pv.add_argument("--cache", type=Path)
    pv.add_argument("--cache-ttl-days", type=float, default=CACHE_TTL_DAYS_DEFAULT)
    pv.add_argument("--json-out", type=Path)

    args = parser.parse_args()
    if args.cmd == "audit":
        return cmd_audit(args)
    return cmd_verify_entry(args)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as exc:
        print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}), file=sys.stderr)
        sys.exit(2)
