#!/usr/bin/env python3
"""Exemplar Few-Shot curator, science-neutral lexicon profiler, and positive-ROI prose hygiene verifier.

Subcommands:
  profile  Extract baseline cadence metrics, categorized verbatim Few-Shot exemplar
           paragraphs, science-neutral connector preferences (requiring agent safety
           vetting before activation), and staccato/tic-word baselines.
  verify   Audit manuscript against AI clichés, copy-edit bugs (`rank rank`,
           `at most <=`), telegraphic short-sentence fragmentation (`<= 8` words),
           and mechanical LLM tic-word overuse — WITHOUT enforcing harmful 4-gram
           destruction or artificial short-sentence burstiness targets.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import shutil
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _texio import iter_files, read_text, strip_comments_text, utf8_stdio, write_new  # noqa: E402

try:
    from clean_watermarks import (
        AI_SLOP_PATTERNS,
        LEXICON_FORMS,
        SAFE_CONNECTOR_WORDS,
        _compute_protected_ranges,
        _scan_copyedit_issues,
        _should_skip_tex,
    )
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from clean_watermarks import (
        AI_SLOP_PATTERNS,
        LEXICON_FORMS,
        SAFE_CONNECTOR_WORDS,
        _compute_protected_ranges,
        _scan_copyedit_issues,
        _should_skip_tex,
    )


DEFAULT_LOCKED_PHYSICS_TERMS: List[str] = [
    "ground state", "ground states", "excited state", "excited states",
    "Hilbert space", "Fock space", "transfer matrix", "partition function",
    "bond algebra", "operator algebra", "correlation length", "entanglement entropy",
    "area law", "symmetry breaking", "spontaneous symmetry breaking",
    "gapped phase", "gapped phases", "gapless phase", "gapless phases",
    "trivially gapped", "critical point", "fixed point", "phase diagram",
    "order parameter", "gauge field", "anomaly inflow", "exact sequence",
    "short exact sequence", "Frobenius", "Hensel", "Lagrangian", "Hamiltonian", "symplectic",
    "Heisenberg", "Weyl", "Kramers", "Wannier", "Lieb", "Schultz", "Mattis",
    "Majorana", "Kitaev", "toric code", "cluster state", "matrix product state",
    "stabilizer", "renormalization group", "conformal field theory",
    "topological order", "spectral gap", "energy gap", "dual lattice",
    "unitary circuit", "fixing", "preserves", "admits", "realizes", "irreducible", "multiplicity",
]

# Strictly science-neutral clusters (connectors + rhetorical/evaluative words ONLY).
SYNONYM_CLUSTERS: Dict[str, List[str]] = {
    "consequence_connector": ["hence", "therefore", "thus", "consequently", "accordingly"],
    "addition_connector": ["moreover", "furthermore", "additionally"],
    "contrast_connector": ["however", "nevertheless", "nonetheless"],
    "sequence_connector": ["subsequently", "thereafter"],
    "similarity_connector": ["analogously", "similarly", "likewise"],
    "usage_rhetorical": ["use", "utilize", "leverage"],
    "evaluative_hype": ["important", "crucial", "vital", "essential", "significant", "remarkable", "notable"],
}

EXTENDED_FORMS: Dict[str, str] = {lemma: lemma for lemmas in SYNONYM_CLUSTERS.values() for lemma in lemmas}
for form_w, (lem, _) in LEXICON_FORMS.items():
    EXTENDED_FORMS[form_w] = lem

# Mechanical LLM tic words flagged only on heavy overuse
TIC_WORDS = ("locks", "renders", "furnishes", "harbors", "mandates")

_GPT2_CACHE: Optional[Tuple[object, object, str]] = None
_GPT2_STATUS: Optional[str] = None  # set once a load attempt failed, so it is never retried per file
_ALLOW_LM_DOWNLOAD = False


def _read_input_text(path: Path) -> str:
    suf = path.suffix.lower()
    if suf in {".tex", ".md", ".txt", ".rst"}:
        return read_text(path)
    if suf == ".pdf":
        pdftotext = shutil.which("pdftotext")
        if pdftotext:
            res = subprocess.run([pdftotext, str(path), "-"], capture_output=True, text=True, encoding="utf-8", errors="replace")
            if res.returncode == 0 and res.stdout.strip():
                return res.stdout
        try:
            import pypdf  # type: ignore
            reader = pypdf.PdfReader(str(path))
            return "\n\n".join(page.extract_text() or "" for page in reader.pages)
        except Exception:
            pass
        raise RuntimeError(f"Cannot extract text from PDF {path}; please provide .tex exemplar files or install pdftotext/pypdf.")
    return read_text(path)


def _strip_tex_document_shell(text: str) -> str:
    s = strip_comments_text(text)

    if "\\begin{document}" in s:
        s = s.split("\\begin{document}", 1)[1]
    for end_marker in ("\\end{document}", "\\begin{thebibliography}", "\\bibliography{"):
        if end_marker in s:
            s = s.split(end_marker, 1)[0]

    s = re.sub(r"\\begin\{(?:figure|table)\*?\}[\s\S]*?\\end\{(?:figure|table)\*?\}", "\n\n", s)
    s = re.sub(r"\\(?:maketitle|tableofcontents|clearpage|newpage|appendix|onecolumngrid|twocolumngrid)\b", "", s)
    return s


def _split_sentences(prose: str) -> List[str]:
    protected = prose
    protected = re.sub(r"\b(Eq|Eqs|Fig|Figs|Sec|Secs|Ref|Refs|Thm|Lem|Prop|Cor|Def|App|No|cf|vs)\.", r"\1<DOT>", protected)
    protected = re.sub(r"\b(e\.g|i\.e)\.", lambda m: m.group(0).replace(".", "<DOT>"), protected, flags=re.I)
    protected = re.sub(r"\bet\s+al\.", "et al<DOT>", protected, flags=re.I)
    protected = re.sub(r"([A-Z])\.", r"\1<DOT>", protected)

    raw_sents = re.split(r"(?<=[.!?])\s+(?=[A-Z0-9`\"(\[])|(?:\n\s*\n)+", protected)
    sents: List[str] = []
    for chunk in raw_sents:
        c = chunk.replace("<DOT>", ".").strip()
        words = re.findall(r"[A-Za-z0-9']+", c)
        if len(words) >= 3:
            sents.append(re.sub(r"\s+", " ", c))
    return sents


def extract_prose_views(raw_text: str, locked_terms: Sequence[str], is_tex: bool = True) -> Dict[str, object]:
    """Extract clean prose sentences, tokenized connector words, punctuation & copy-edit views."""
    s = _strip_tex_document_shell(raw_text) if is_tex else raw_text

    em_dash_count = s.count("---")
    semicolon_count = s.count(";")
    colon_count = s.count(":")
    paren_count = s.count("(")

    lm_s = s
    shingle_s = s

    display_envs = (
        "equation", "equation*", "align", "align*", "alignat", "alignat*",
        "gather", "gather*", "multline", "multline*", "flalign", "flalign*",
        "eqnarray", "eqnarray*", "tikzpicture", "tikzcd",
    )

    def _display_repl(m: "re.Match[str]") -> str:
        # A displayed equation is part of its sentence. It only ends the sentence when the
        # equation itself ends with a period ("... = 0.\n\\end{equation}").
        body = re.sub(r"\\end\{[^}]*\}\s*$|\\\]\s*$|\$\$\s*$", "", m.group(0)).rstrip()
        body = re.sub(r"(?:\\\\|\\nonumber|\\notag|\\label\{[^}]*\})\s*$", "", body).rstrip()
        return " quantity. " if body.endswith(".") else " quantity "

    for env in display_envs:
        pat = re.compile(rf"\\begin\{{{re.escape(env)}\}}[\s\S]*?\\end\{{{re.escape(env)}\}}")
        lm_s = pat.sub(_display_repl, lm_s)
        shingle_s = pat.sub(" <MATH> ", shingle_s)

    for pat_str in (r"\\\[[\s\S]*?\\\]", r"\$\$[\s\S]*?\$\$"):
        pat = re.compile(pat_str)
        lm_s = pat.sub(_display_repl, lm_s)
        shingle_s = pat.sub(" <MATH> ", shingle_s)

    for ip in (re.compile(r"\\\([\s\S]*?\\\)"), re.compile(r"(?<!\\)\$[^$\n]+?(?<!\\)\$")):
        lm_s = ip.sub(" quantity ", lm_s)
        shingle_s = ip.sub(" <MATH> ", shingle_s)

    # Strip \status{...} and \declareexternalref metadata before prose inspection
    for cmd in ("status", "declareexternalref", "label", "cite[a-z]*"):
        lm_s = re.sub(rf"\\{cmd}(?:\[[^\]]*\])?(?:\{{[^}}]*\}})+", "", lm_s)

    lm_s = re.sub(r"\\(?:eq|ext)?ref\{[^}]*\}", "1", lm_s)
    lm_s = re.sub(r"\\(?:emph|textbf|textit|textrm|textsc|underline)\{([^}]*)\}", r"\1", lm_s)
    lm_s = re.sub(r"\\(?:sub)*section\*?(?:\[[^\]]*\])?\{([^}]*)\}", r"\n\n\1.\n\n", lm_s)
    lm_s = re.sub(r"\\letterhead\{([^}]*)\}\{[^}]*\}", r"\n\n\1.\n\n", lm_s)
    lm_s = re.sub(r"\\(?:begin|end)\{[^}]*\}(?:\[[^\]]*\])?", "\n", lm_s)
    lm_s = re.sub(r"\\[A-Za-z@]+\*?(?:\[[^\]]*\])?\{([^}]*)\}", r"\1", lm_s)
    lm_s = re.sub(r"\\[A-Za-z@]+", " ", lm_s)
    lm_s = lm_s.replace("~", " ").replace("``", '"').replace("''", '"').replace("---", ", ").replace("--", "-")
    lm_s = re.sub(r"[{}]", " ", lm_s)

    shingle_s = re.sub(r"\\(?:label|status|cite[a-z]*|(?:eq|ext)?ref|pageref|autoref|cref|Cref)(?:\{[^}]*\})+", " <MATH> ", shingle_s)
    shingle_s = re.sub(r"\\(?:emph|textbf|textit|textrm|textsc)\{([^}]*)\}", r"\1", shingle_s)
    shingle_s = re.sub(r"\\(?:sub)*section\*?(?:\[[^\]]*\])?\{([^}]*)\}", "\n\n", shingle_s)
    shingle_s = re.sub(r"\\letterhead\{[^}]*\}\{[^}]*\}", "\n\n", shingle_s)
    shingle_s = re.sub(r"\\(?:begin|end)\{[^}]*\}(?:\[[^\]]*\])?", "\n", shingle_s)
    shingle_s = re.sub(r"\\[A-Za-z@]+\*?(?:\[[^\]]*\])?\{([^}]*)\}", r" \1 ", shingle_s)
    shingle_s = re.sub(r"\\[A-Za-z@]+", " ", shingle_s)
    shingle_s = shingle_s.replace("~", " ").replace("``", " ").replace("''", " ").replace("---", " ").replace("--", " ")
    shingle_s = re.sub(r"[{}]", " ", shingle_s)

    shingle_masked = shingle_s
    for term in sorted(set(locked_terms), key=len, reverse=True):
        if term:
            shingle_masked = re.sub(r"\b" + re.escape(term) + r"\b", " <TERM> ", shingle_masked, flags=re.IGNORECASE)

    # Verbatim clean paragraphs for Few-Shot voice learning (preserve original TeX formulas if any)
    raw_src_paras = [re.sub(r"\s+", " ", p).strip() for p in re.split(r"\n\s*\n+", s)]
    verbatim_paras = [
        p for p in raw_src_paras
        if 40 <= len(p.split()) <= 260
        and not p.lstrip().startswith(("\\begin{table", "\\begin{figure", "\\includegraphics"))
    ]

    sentences = _split_sentences(lm_s)
    words = re.findall(r"[A-Za-z]+(?:'[A-Za-z]+)?", shingle_s.lower())

    synonym_words = [
        tok.lower()
        for tok in re.findall(r"__MATH__|__TERM__|[A-Za-z]+", shingle_masked.replace("<MATH>", " __MATH__ ").replace("<TERM>", " __TERM__ "))
        if tok not in ("__MATH__", "__TERM__")
    ]

    # Raw text prose-masked copy for _scan_copyedit_issues
    raw_ranges = _compute_protected_ranges(raw_text, protect_claim_envs=False) if is_tex else []
    masked_chars = list(raw_text)
    for st, en in raw_ranges:
        for k in range(st, min(en, len(masked_chars))):
            if masked_chars[k] != "\n":
                masked_chars[k] = " "
    raw_prose_only = "".join(masked_chars)

    return {
        "sentences": sentences,
        "verbatim_paragraphs": verbatim_paras,
        "words": words,
        "synonym_words": synonym_words,
        "punctuation_counts": {
            "em_dash": em_dash_count,
            "semicolon": semicolon_count,
            "colon": colon_count,
            "paren": paren_count,
        },
        "ai_scan_prose": lm_s,
        "raw_text": raw_text,
        "raw_prose_only": raw_prose_only,
    }


def _load_gpt2(disable_lm: bool) -> Optional[Tuple[object, object, str]]:
    """Optional GPT-2 perplexity (informational only; never part of the pass/fail checks).

    Uses the local Hugging Face cache only, unless `--lm-download` was given. A failed load is
    remembered, so a missing model costs one attempt per run instead of one per file.
    """
    global _GPT2_CACHE, _GPT2_STATUS
    if disable_lm:
        return None
    if _GPT2_CACHE is not None:
        return _GPT2_CACHE
    if _GPT2_STATUS is not None:
        return None
    try:
        import torch  # type: ignore
        from transformers import AutoModelForCausalLM, AutoTokenizer  # type: ignore

        local_only = not _ALLOW_LM_DOWNLOAD
        tok = AutoTokenizer.from_pretrained("gpt2", local_files_only=local_only)
        mdl = AutoModelForCausalLM.from_pretrained("gpt2", local_files_only=local_only)
        mdl.eval()
        device = "cuda" if torch.cuda.is_available() else "cpu"
        mdl.to(device)
        _GPT2_CACHE = (tok, mdl, device)
        return _GPT2_CACHE
    except Exception as exc:
        _GPT2_STATUS = f"unavailable: {type(exc).__name__}"
        return None


def _compute_lm_stats(sentences: List[str], disable_lm: bool) -> Dict[str, object]:
    if not sentences:
        return {"lm_backend": "none", "mean_ppl": 0.0, "median_ppl": 0.0, "sentence_logloss_std": 0.0}
    loaded = _load_gpt2(disable_lm)
    if loaded is not None:
        import torch  # type: ignore
        tok, mdl, device = loaded
        losses: List[float] = []
        with torch.no_grad():
            for sent in sentences[:250]:
                enc = tok(sent, return_tensors="pt", truncation=True, max_length=512)
                input_ids = enc["input_ids"].to(device)
                if input_ids.shape[1] < 3:
                    continue
                out = mdl(input_ids, labels=input_ids)
                loss_val = float(out.loss.detach().cpu().item())
                if math.isfinite(loss_val):
                    losses.append(loss_val)
        if losses:
            mean_loss = sum(losses) / len(losses)
            sorted_l = sorted(losses)
            med_loss = sorted_l[len(sorted_l) // 2]
            var_l = sum((x - mean_loss) ** 2 for x in losses) / max(1, len(losses))
            return {
                "lm_backend": "transformers:gpt2",
                "scored_sentences": len(losses),
                "mean_ppl": round(math.exp(min(mean_loss, 12.0)), 3),
                "median_ppl": round(math.exp(min(med_loss, 12.0)), 3),
                "sentence_logloss_std": round(math.sqrt(var_l), 4),
            }
    return {"lm_backend": "disabled" if disable_lm else (_GPT2_STATUS or "unavailable"), "scored_sentences": 0, "mean_ppl": 0.0, "median_ppl": 0.0, "sentence_logloss_std": 0.0}


def _compute_text_metrics(views: Dict[str, object], disable_lm: bool) -> Dict[str, object]:
    sentences: List[str] = views["sentences"]  # type: ignore[assignment]
    words: List[str] = views["words"]  # type: ignore[assignment]
    punct: Dict[str, int] = views["punctuation_counts"]  # type: ignore[assignment]

    sent_lens = [len(re.findall(r"[A-Za-z0-9']+", s)) for s in sentences]
    n_sents = len(sent_lens)
    n_words = len(words)

    if n_sents == 0:
        mean_len, std_len, burst, short_r, le8_r, long_r, max_consec_short = 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0
    else:
        mean_len = sum(sent_lens) / n_sents
        var_len = sum((x - mean_len) ** 2 for x in sent_lens) / n_sents
        std_len = math.sqrt(var_len)
        burst = std_len / mean_len if mean_len > 0 else 0.0
        short_r = sum(1 for x in sent_lens if x < 12) / n_sents
        le8_r = sum(1 for x in sent_lens if x <= 8) / n_sents
        long_r = sum(1 for x in sent_lens if x > 28) / n_sents
        cur_run = 0
        max_consec_short = 0
        for x in sent_lens:
            if x <= 9:
                cur_run += 1
                max_consec_short = max(max_consec_short, cur_run)
            else:
                cur_run = 0

    word_counts = Counter(words)
    tic_counts = {tw: int(word_counts.get(tw, 0)) for tw in TIC_WORDS}
    per_1k = 1000.0 / max(1, n_words)
    tic_rates = {tw: round(cnt * per_1k, 2) for tw, cnt in tic_counts.items()}

    w_win = 50
    if n_words == 0:
        mattr = 0.0
    elif n_words <= w_win:
        mattr = len(set(words)) / n_words
    else:
        ttrs = [len(set(words[i : i + w_win])) / w_win for i in range(n_words - w_win + 1)]
        mattr = sum(ttrs) / len(ttrs)

    lm_stats = _compute_lm_stats(sentences, disable_lm)

    return {
        "sentence_count": n_sents,
        "word_count": n_words,
        "mean_sentence_len": round(mean_len, 2),
        "std_sentence_len": round(std_len, 2),
        "burstiness": round(burst, 4),
        "short_sentence_ratio": round(short_r, 4),
        "short_le8_ratio": round(le8_r, 4),
        "max_consecutive_short_le9": max_consec_short,
        "long_sentence_ratio": round(long_r, 4),
        "mattr_50": round(mattr, 4),
        "tic_word_counts": tic_counts,
        "tic_word_per_1k": tic_rates,
        "punctuation_per_1k": {
            "em_dash": round(punct["em_dash"] * per_1k, 2),
            "semicolon": round(punct["semicolon"] * per_1k, 2),
            "colon": round(punct["colon"] * per_1k, 2),
            "paren": round(punct["paren"] * per_1k, 2),
        },
        **lm_stats,
    }


def _extract_rhetorical_lexicon_candidates(synonym_words: List[str], n_words: int) -> Dict[str, object]:
    """Extract ONLY science-neutral rhetorical candidates; requires agent safety vetting before activation."""
    lemma_counts: Counter = Counter()
    for w in synonym_words:
        lem = EXTENDED_FORMS.get(w)
        if lem:
            lemma_counts[lem] += 1

    cluster_counts: Dict[str, Dict[str, int]] = {}
    candidate_connector_swaps: Dict[str, str] = {}
    preferred_by_cluster: Dict[str, str] = {}
    evaluative_counts: Dict[str, int] = {}

    for cname, lemmas in SYNONYM_CLUSTERS.items():
        counts = {lem: int(lemma_counts[lem]) for lem in lemmas}
        cluster_counts[cname] = counts
        if cname == "evaluative_hype":
            evaluative_counts = counts
            continue
        total = sum(counts.values())
        if total < 2:
            continue
        sorted_items = sorted(counts.items(), key=lambda kv: (-kv[1], lemmas.index(kv[0])))
        top_lem, top_cnt = sorted_items[0]
        if top_cnt < 2:
            continue
        preferred_by_cluster[cname] = top_lem
        if cname.endswith("_connector") or cname == "usage_rhetorical":
            for lem, cnt in sorted_items[1:]:
                if cnt == 0 or (top_cnt >= 5 and cnt < 0.10 * top_cnt):
                    if lem in SAFE_CONNECTOR_WORDS and top_lem in SAFE_CONNECTOR_WORDS:
                        candidate_connector_swaps[lem] = top_lem
                    elif cname == "usage_rhetorical" and lem in ("utilize", "leverage") and top_lem == "use":
                        candidate_connector_swaps[lem] = top_lem

    per_1k = 1000.0 / max(1, n_words)
    return {
        "lexicon_vetted": False,
        "vetting_note": "Must be audited by Lexicon Safety Vetting Agent before copying approved swaps into vetted_replacements and setting lexicon_vetted=true.",
        "cluster_counts": cluster_counts,
        "preferred_by_cluster": preferred_by_cluster,
        "candidate_connector_swaps": candidate_connector_swaps,
        "vetted_replacements": {},
        "vetted_style_hints": {},
        "evaluative_hype_counts": evaluative_counts,
        "evaluative_hype_per_1k": round(sum(evaluative_counts.values()) * per_1k, 2),
    }


def _categorize_exemplars(paras: List[str]) -> Tuple[Dict[str, List[str]], List[str]]:
    buckets: Dict[str, List[str]] = {
        "opening_hook": [],
        "setup_definition": [],
        "theorem_commentary": [],
        "derivation_mechanism": [],
        "discussion": [],
    }
    n = max(1, len(paras))
    for idx, p in enumerate(paras):
        low = p.lower()
        if idx < max(2, int(0.18 * n)) and any(k in low for k in ("question", "recent", "consider", "intrigu", "puzzle", "long-standing")):
            buckets["opening_hook"].append(p)
        elif any(k in low for k in ("theorem", "proposition", "corollary", "remark", "if and only if", "criterion")):
            buckets["theorem_commentary"].append(p)
        elif any(k in low for k in ("define", "hamiltonian", "hilbert", "bond algebra", "generated by", "operator")):
            buckets["setup_definition"].append(p)
        elif any(k in low for k in ("outlook", "discuss", "open question", "conclude", "experiment", "simulator")) or idx >= int(0.82 * n):
            buckets["discussion"].append(p)
        else:
            buckets["derivation_mechanism"].append(p)

    curated_by_role: Dict[str, List[str]] = {}
    flat: List[str] = []
    for role in ("opening_hook", "setup_definition", "theorem_commentary", "derivation_mechanism", "discussion"):
        chosen = buckets[role][:1]
        if chosen:
            curated_by_role[role] = chosen
            flat.extend(chosen)
    if len(flat) < 4:
        for p in paras:
            if p not in flat:
                flat.append(p)
                if len(flat) >= 6:
                    break
    return curated_by_role, flat[:6]


def _collect_input_files(paths: Sequence[Path], dir_suffixes: Sequence[str]) -> List[Path]:
    """Explicit files are taken as given; directories contribute only `dir_suffixes` (reports/PDF builds are skipped)."""
    out: List[Path] = []
    for p in paths:
        if p.is_file():
            if not _should_skip_tex(p):
                out.append(p)
        elif p.is_dir():
            for sub in iter_files([p], dir_suffixes):
                rel_parts = sub.relative_to(p).parts
                if "audit_reports" in rel_parts or _should_skip_tex(sub):
                    continue
                out.append(sub)
    return out


def cmd_profile(args: argparse.Namespace) -> int:
    locked = list(DEFAULT_LOCKED_PHYSICS_TERMS)
    if args.extra_locked_terms and args.extra_locked_terms.exists():
        extra = json.loads(args.extra_locked_terms.read_text(encoding="utf-8"))
        if isinstance(extra, list):
            locked.extend(str(x) for x in extra)
    locked = sorted(set(locked))

    missing = [str(p) for p in args.inputs if not p.exists()]
    if missing:
        print(json.dumps({"error": f"input path(s) not found: {missing}"}), file=sys.stderr)
        return 2
    files = _collect_input_files(args.inputs, [".tex", ".md", ".txt", ".pdf"])
    if not files:
        print(json.dumps({"error": "No valid exemplar files found"}), file=sys.stderr)
        return 2

    combined_views: Dict[str, object] = {
        "sentences": [],
        "verbatim_paragraphs": [],
        "words": [],
        "synonym_words": [],
        "punctuation_counts": {"em_dash": 0, "semicolon": 0, "colon": 0, "paren": 0},
    }
    skipped: List[str] = []
    used: List[Path] = []
    for f in files:
        try:
            txt = _read_input_text(f)
        except RuntimeError as exc:
            skipped.append(str(exc))
            continue
        used.append(f)
        v = extract_prose_views(txt, locked, is_tex=(f.suffix.lower() == ".tex"))
        combined_views["sentences"].extend(v["sentences"])  # type: ignore[attr-defined]
        combined_views["verbatim_paragraphs"].extend(v["verbatim_paragraphs"])  # type: ignore[attr-defined]
        combined_views["words"].extend(v["words"])  # type: ignore[attr-defined]
        combined_views["synonym_words"].extend(v["synonym_words"])  # type: ignore[attr-defined]
        for k, cnt in v["punctuation_counts"].items():  # type: ignore[attr-defined]
            combined_views["punctuation_counts"][k] += cnt  # type: ignore[index]

    if not used:
        print(json.dumps({"error": "No readable exemplar text", "skipped": skipped}), file=sys.stderr)
        return 2
    files = used
    metrics = _compute_text_metrics(combined_views, args.no_lm)
    lexicon = _extract_rhetorical_lexicon_candidates(
        combined_views["synonym_words"],  # type: ignore[arg-type]
        int(metrics["word_count"]),
    )
    by_role, flat_paras = _categorize_exemplars(combined_views["verbatim_paragraphs"])  # type: ignore[arg-type]

    profile = {
        "exemplar_files": [str(f).replace("\\", "/") for f in files],
        "baseline_metrics": metrics,
        "preferred_lexicon": lexicon,
        "locked_terms": locked,
        "exemplar_paragraphs": flat_paras,
        "categorized_exemplars": by_role,
        "skipped_inputs": skipped,
    }

    write_new(args.out, json.dumps(profile, indent=2, ensure_ascii=False))
    print(json.dumps(profile, indent=2, ensure_ascii=False))
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    if not args.profile.exists():
        print(json.dumps({"passed": False, "error": f"profile not found: {args.profile}"}), file=sys.stderr)
        return 2
    missing = [str(p) for p in args.inputs if not p.exists()]
    if missing:
        print(json.dumps({"passed": False, "error": f"input path(s) not found: {missing}"}), file=sys.stderr)
        return 2
    prof = json.loads(read_text(args.profile))
    locked = prof.get("locked_terms", DEFAULT_LOCKED_PHYSICS_TERMS)
    base = prof.get("baseline_metrics", {})

    files = _collect_input_files(args.inputs, [".tex"])
    if not files:
        print(json.dumps({"passed": False, "error": "No input files to verify"}), file=sys.stderr)
        return 2

    file_reports: List[Dict[str, object]] = []
    agg_views: Dict[str, object] = {
        "sentences": [],
        "words": [],
        "punctuation_counts": {"em_dash": 0, "semicolon": 0, "colon": 0, "paren": 0},
    }
    acronym_files: Dict[str, List[str]] = {}

    for f in files:
        txt = _read_input_text(f)
        v = extract_prose_views(txt, locked, is_tex=(f.suffix.lower() == ".tex"))
        m = _compute_text_metrics(v, args.no_lm)

        ai_hits = []
        for label, pat in AI_SLOP_PATTERNS:
            for hit in pat.finditer(v["ai_scan_prose"]):  # type: ignore[arg-type]
                ai_hits.append({"kind": label, "match": hit.group(0)})

        copyedit_issues = _scan_copyedit_issues(
            str(v["raw_text"]),
            str(v["raw_prose_only"]),
        ) if f.suffix.lower() == ".tex" else []

        # Track cross-file repeated acronym expansions like "... (MPS)"
        for am in re.finditer(r"\b[A-Za-z][A-Za-z-]+(?:\s+[A-Za-z][A-Za-z-]+){1,5}\s*\(([A-Z]{2,8})\)", str(v["raw_prose_only"])):
            acronym_files.setdefault(am.group(1), []).append(f.name)

        file_staccato_warn = (
            int(m["sentence_count"]) >= 8
            and (float(m["short_le8_ratio"]) > 0.22 or int(m["max_consecutive_short_le9"]) >= 3)
        )

        file_reports.append({
            "file": str(f).replace("\\", "/"),
            "metrics": m,
            "ai_slop_hits": ai_hits,
            "copyedit_issues": copyedit_issues,
            "staccato_warning": file_staccato_warn,
        })

        agg_views["sentences"].extend(v["sentences"])  # type: ignore[attr-defined]
        agg_views["words"].extend(v["words"])  # type: ignore[attr-defined]
        for k, cnt in v["punctuation_counts"].items():  # type: ignore[attr-defined]
            agg_views["punctuation_counts"][k] += cnt  # type: ignore[index]

    cross_file_acronyms = [
        {"kind": "cross_file_repeated_acronym", "acronym": acr, "occurrences": len(flist), "files": flist}
        for acr, flist in acronym_files.items()
        if len(flist) >= 3
    ]

    agg_metrics = _compute_text_metrics(agg_views, args.no_lm)
    total_ai_slop = sum(len(r["ai_slop_hits"]) for r in file_reports)  # type: ignore[arg-type]
    total_copyedit = sum(len(r["copyedit_issues"]) for r in file_reports) + len(cross_file_acronyms)  # type: ignore[arg-type]

    # Anti-staccato threshold: trigger only once meaningful sample >= 8 sentences exists
    ref_le8 = float(base.get("short_le8_ratio", 0.08))
    max_le8_threshold = max(0.14, 1.5 * ref_le8)
    n_sents = int(agg_metrics["sentence_count"])
    if n_sents >= 8:
        anti_staccato_ok = (
            float(agg_metrics["short_le8_ratio"]) <= max_le8_threshold
            and int(agg_metrics["max_consecutive_short_le9"]) < 3
            and not any(r["staccato_warning"] for r in file_reports)
        )
    else:
        anti_staccato_ok = int(agg_metrics["max_consecutive_short_le9"]) < 3

    # Check LLM tic word overuse
    base_tic_rates: Dict[str, float] = base.get("tic_word_per_1k", {}) if isinstance(base.get("tic_word_per_1k"), dict) else {}
    cur_tic_counts: Dict[str, int] = agg_metrics["tic_word_counts"]  # type: ignore[assignment]
    cur_tic_rates: Dict[str, float] = agg_metrics["tic_word_per_1k"]  # type: ignore[assignment]
    overused_tics: List[Dict[str, object]] = []
    for tw, cnt in cur_tic_counts.items():
        ref_r = max(0.6, float(base_tic_rates.get(tw, 0.5)))
        if cnt >= 8 and cur_tic_rates.get(tw, 0.0) > 3.5 * ref_r:
            overused_tics.append({"word": tw, "count": cnt, "per_1k": cur_tic_rates[tw], "exemplar_per_1k": base_tic_rates.get(tw, 0.0)})

    checks = {
        "ai_slop_zero": total_ai_slop == 0,
        "copyedit_clean": total_copyedit == 0,
        "anti_staccato_ok": anti_staccato_ok,
        "no_tic_word_overuse": len(overused_tics) == 0,
    }
    overall_pass = all(checks.values())

    report = {
        "passed": overall_pass,
        "checks": checks,
        "thresholds": {
            "max_ai_slop_hits": 0,
            "max_copyedit_issues": 0,
            "max_short_le8_ratio": round(max_le8_threshold, 4),
            "max_consecutive_short_le9": 2,
        },
        "exemplar_baseline": base,
        "current_metrics": agg_metrics,
        "total_ai_slop_hits": total_ai_slop,
        "total_copyedit_issues": total_copyedit,
        "cross_file_repeated_acronyms": cross_file_acronyms,
        "overused_tic_words": overused_tics,
        "policy_note": (
            "v2 policy: destructive prose_4gram_retention and forced burstiness inflation are disabled. "
            "Voice matches exemplar papers via Few-Shot imitation; deterministic checks enforce zero AI slop, "
            "clean copy-editing, anti-staccato continuity, and no mechanical tic-word overuse. "
            "GPT-2 perplexity (only with --lm) is informational and never affects `passed`."
        ),
        "files": file_reports,
    }

    if args.json_out:
        write_new(args.json_out, json.dumps(report, indent=2, ensure_ascii=False))

    if args.md_out:
        md = [
            "# Prose Hygiene, Anti-Staccato & Copy-Edit Audit Report (v2)",
            "",
            f"- **Overall Verdict**: `{'PASS' if overall_pass else 'NEEDS_REVISION'}`",
            f"- **Policy**: {report['policy_note']}",
            "",
            "| Metric | Exemplar Baseline | Current Manuscript | Threshold / Status |",
            "| :--- | :---: | :---: | :--- |",
            f"| AI Cliché Hits | 0 | {total_ai_slop} | `== 0` ({'PASS' if checks['ai_slop_zero'] else 'FAIL'}) |",
            f"| Copy-Edit Defects | 0 | {total_copyedit} | `== 0` ({'PASS' if checks['copyedit_clean'] else 'FAIL'}) |",
            f"| `<=8`-Word Short-Sentence Ratio | {base.get('short_le8_ratio', 'N/A')} | {agg_metrics['short_le8_ratio']} | `<= {max_le8_threshold:.3f}` ({'PASS' if checks['anti_staccato_ok'] else 'FAIL'}) |",
            f"| Mean Sentence Length (words) | {base.get('mean_sentence_len', 'N/A')} | {agg_metrics['mean_sentence_len']} | Reference continuity |",
            f"| Tic-Word Overuse Alerts | 0 | {len(overused_tics)} | `== 0` ({'PASS' if checks['no_tic_word_overuse'] else 'FAIL'}) |",
            "",
        ]
        write_new(args.md_out, "\n".join(md))

    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0 if overall_pass else 1


def main() -> int:
    global _ALLOW_LM_DOWNLOAD
    utf8_stdio()
    parser = argparse.ArgumentParser(description="Exemplar Few-Shot profiler and positive-ROI prose hygiene verifier (v2).")
    sub = parser.add_subparsers(dest="cmd", required=True)

    def _lm_flags(p: argparse.ArgumentParser) -> None:
        p.add_argument("--lm", action="store_true", help="Also compute GPT-2 perplexity (informational; needs torch+transformers and a cached gpt2)")
        p.add_argument("--lm-download", action="store_true", help="With --lm: allow downloading gpt2 from the Hugging Face hub")
        p.add_argument("--no-lm", action="store_true", help="(default) Skip GPT-2 scoring; kept for backward compatibility")

    p_prof = sub.add_parser("profile", help="Extract exemplar cadence, Few-Shot samples, and connector candidates")
    p_prof.add_argument("--inputs", nargs="+", type=Path, required=True)
    p_prof.add_argument("--out", type=Path, required=True)
    p_prof.add_argument("--extra-locked-terms", type=Path)
    _lm_flags(p_prof)

    p_ver = sub.add_parser("verify", help="Verify manuscript prose hygiene, anti-staccato continuity & copy-editing")
    p_ver.add_argument("--inputs", nargs="+", type=Path, required=True)
    p_ver.add_argument("--profile", type=Path, required=True)
    p_ver.add_argument("--pre-polish-dir", type=Path, help="(Accepted for backward CLI compatibility; no longer enforces 4-gram destruction)")
    p_ver.add_argument("--json-out", type=Path)
    p_ver.add_argument("--md-out", type=Path)
    _lm_flags(p_ver)

    args = parser.parse_args()
    args.no_lm = args.no_lm or not args.lm  # LM scoring is opt-in
    _ALLOW_LM_DOWNLOAD = bool(args.lm_download)
    if args.cmd == "profile":
        return cmd_profile(args)
    return cmd_verify(args)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as exc:
        print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}), file=sys.stderr)
        sys.exit(2)
