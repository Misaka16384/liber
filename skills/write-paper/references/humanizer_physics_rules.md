# Physics Prose Hygiene, Few-Shot Cadence & Copy-Edit Guide

This guide governs Stage 2 (`draft`) voice imitation and Stage 5 (`hygiene`) deterministic copy-editing.

> [!IMPORTANT]
> **Hard Policy Shift (v2)**: Destructive `prose_4gram_retention` rewriting and artificial short-sentence splitting (`burstiness` inflation) are **PERMANENTLY DISABLED**. Chasing 4-gram decorrelation fractures rigorous derivations into telegraphic fragments and corrupts logical quantifiers. Instead:
> 1. **Draft with Few-Shot Voice Imitation**: Section drafters read verbatim exemplar paragraphs from `00_style_profile.json` and naturally match the research group's syntax and rhythm on the first pass.
> 2. **Run Pure Positive-ROI Deterministic Hygiene**: Strip invisible watermarks/typography tells, eliminate explicit AI clichés and copy-edit defects, and harmonize *only* Lexicon-Vetted non-scientific connectors/evaluative words.

---

## 1. Absolute Non-Negotiable Guardrails

1. **Zero Math, Label, Citation, or Quantifier Mutation (`check_math_integrity.py --check-quantifiers`)**:
   - NEVER alter any character inside `$...$`, `\(...\)`, `$$...$$`, `\[...\]`, math environments, `\label`, `\ref`, `\eqref`, `\extref`, or `\status{...}`.
   - NEVER rewrite, paraphrase, or swap English logical quantifiers, hypotheses, or modal strength inside the abstract or any theorem/lemma/proposition/corollary/remark (`for all` vs `for some`, `every`, `each`, `unique`, `iff` vs `only if`, `below`/`above`, `predicts` vs fact, `fixing` vs `preserves`, `counted with multiplicity`, `irreducible`, `translation-invariant`, `finite-range`, `bare`).
2. **Science-Neutral Lexicon Vetting Lock**:
   - Automatic/suggested word replacements are strictly limited to **non-scientific connectors and empty evaluative words** (e.g., `furthermore -> moreover`, `utilize -> use`, `important`/`significant`/`crucial` hype) that have been explicitly approved in `00_style_profile.json` (`"lexicon_vetted": true`) by the Lexicon Safety Vetting Agent.
   - NEVER touch verbs, prepositions, or adjectives that carry physical, algebraic, or topological meaning.

---

## 2. LLM Clichés, Staccato Tell & Copy-Edit Blacklist

1. **Banned AI Lexicon (`ai_slop_hits == 0`)**:
   - `delve`, `delves`, `delved`, `delving`, `pivotal`, `tapestry`, `underscore`, `underscoring`, `testament to`, `intricate interplay`, `multifaceted`, `paradigm shift`, `groundbreaking`, `game-changer`, `cornerstone`, `beacon`, `unravel`, `plethora`, `myriad`, `shed light on`, `pave the way for`, `open new avenues`, `with zero ad-hoc assumptions`, `proves mandatory`, `inasmuch as`, `it is worth noting that`, sentence-start `Notably,`/`Importantly,`/`Crucially,`/`Remarkably,`.
   - Avoid mechanical repetition of LLM tic verbs (`locks`, `renders`, `furnishes`, `harbors`, `mandates`).
2. **Anti-Staccato Continuity (`anti_staccato_ok`)**:
   - Keep derivations logically connected with causal clauses. Short `<= 8`-word sentences should stay natural and sparse (`<= 14%` of sentences, never `>= 3` choppy short sentences in a row).
3. **Deterministic Copy-Edit Hygiene (`copyedit_clean`)**:
   - Fix doubled consecutive words (`rank rank`), redundant comparators (`at most <= K`), sentence-initial abbreviations (`Sec.` -> `Section`, `Eq.` -> `Equation`), and repeated acronym expansions (`TQFT` or `MPS` expanded more than once per manuscript).
