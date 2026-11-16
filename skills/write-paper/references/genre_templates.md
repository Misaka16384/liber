# Theoretical & Mathematical Physics Genre Templates & Structural Norms

This guide defines structural templates, word-count budgets, and the mandatory **4-Part Golden Section Structure** for papers drafted under `/write-paper`. All technical sections must adhere to rigorous mathematical-physics exposition standards: motivation before formalism, physical intuition alongside definitions, explicit proof inevitability, and honest scope boundaries.

---

## 1. The 4-Part Golden Section Progression (Mandatory for Every Technical Section)

Every non-trivial technical section (both in the main text and in substantive appendices) MUST follow this progression:

### Part A: Physical Motivation & Logical Bridge
- **Never** open a section by dumping naked matrices, formal definitions, or equations.
- Start with a concise motivation paragraph (2–4 sentences in a long paper; 1–2 punchy sentences in a Letter) answering:
  1. *What open question or debt did the preceding section leave behind?* (e.g., heuristic vs. rigorous proof, one-dimensional vs. higher-dimensional case, clean vs. disordered model).
  2. *What physical mechanism or theorem will this section establish, and why is it needed for the main verdict?*

### Part B: Microscopic Definitions & De-Jargonized Intuition
- **No Unintroduced Symbols**: Define every algebra, Hilbert space, operator, pairing, or polynomial ring parameter *before* writing maps or equations acting on it. All recurring notations MUST use macros from `macros.tex`.
- **Action-Oriented Physical Intuition**: Explain in 1–2 plain sentences what each formal object physically *does* (e.g., a *string operator* in the toric code creates a pair of anyons at its endpoints and moves them apart; the *Jordan–Wigner string* attaches the parity of all sites to the left so that local spin operators become fermions).
- **De-Jargonize Abstract Mathematics**: Whenever introducing an abstract mathematical concept (e.g., cohomology classes, Morita equivalence, a symplectic form, a spectral sequence), immediately translate its physical meaning into plain physicist language. Never conflate a purely algebraic property of the model with a thermodynamic phase of matter.

### Part C: Inevitability & Zero-Skip Derivation
- **Why Forced & What Else Is Feasible**: Whenever specifying a transformation law or ansatz, explicitly prove why physical principles (e.g., locality, symmetry invariance, preservation of the Heisenberg–Weyl symplectic pairing $\Omega$) force its form and relative signs, and state the full feasible symmetry/duality group when constraints are relaxed.
- **Direct Causal Logic**: Keep implication arrows direct (e.g., Kramers–Wannier self-duality of the transverse-field Ising chain together with a unique transition directly places the critical point at $h = J$). Never disguise a circular tautology as a proof.
- **Cross-Section Notation Alignment**: Match column-vector orderings, sign conventions ($\Omega$ vs. $-\Omega$), and physical gap definitions ($\Delta_{\mathrm{TM}}$ vs. $\Delta E$) strictly against `macros.tex` and the blueprint.
- **Main Text vs. Appendix / Supplemental Split**: Keep physical mechanisms, clean logical steps, and sharp theorems in the main text; move long case-by-case algebraic verifications, technical lemmas, and exhaustive parameter/model tables into named appendices with explicit `\ref{app:...}` pointers (never hand-wave "by a tedious calculation" without giving an exact Appendix reference!).

### Part D: Physical Verdict, Concrete Realization & Literature Boundary
- Conclude the section with an unmistakable physical verdict (e.g., solvability criterion $\iff$ anomaly-free gapped phase vs. obstruction forcing gaplessness or spontaneous symmetry breaking).
- Anchor abstract theorems with concrete, independently checkable toy models or explicit parameter tables.
- Explicitly demarcate what is already established in prior literature versus what is genuinely new in this manuscript.

---

## 2. Genre A: Short Letter (`genre = "letter"`, PRL / Nature Physics Letter Style)

### Budgets & Format
- **Documentclass**: `\documentclass[aps,prl,twocolumn,superscriptaddress,longbibliography]{revtex4-2}`
- **APS Length Budget (~3,750 equivalent words max)**:
  - Main-text prose word count target: **2,400 – 2,900 words** (leaving ~850–1,350 word equivalents for display equations at ~16 words/single-column row and 2–3 figures at ~150–300 words each).
  - **Abstract**: $\le 600$ characters (PRL strict guideline), standalone, no undefined acronyms, no `\cite` if avoidable (or minimal explicit citation), state the main result and physical mechanism in 3–4 crisp sentences.
- **Section Organization**:
  - Use bold-italic run-in paragraph headers (`\emph{Introduction.---}`, `\emph{Model and setup.---}`, `\emph{Main result.---}`, `\emph{Examples and phase diagram.---}`, `\emph{Discussion.---}`) rather than heavy numbered sections unless the blueprint requests numbered sections.
  - **First 2 Paragraphs (The Hook)**: Frame the broad physical puzzle in paragraph 1 AND explicitly define core classification terms (e.g., *trivially gapped*) upon first use; state the exact breakthrough, an explicit concrete microscopic Hamiltonian (e.g., spin-chain formula), and physical payoff before any heavy formalism.
  - **No Unintroduced Symbols or Internal Tags**: Define every symbol used in the abstract/main text inside the main text itself (`audit_tex_defs.py`). Never leak internal ledger tags (`Lemma L1/L2/L3`, `Grade N`, `Refl-2`, uncited `Author Condition N`) into visible text; use proper SM theorem/section labels instead.
  - **PRL Run-In Header & SM Citation Discipline**: Any custom heading macro (such as `\letterhead`) MUST begin with `\par` and every section file MUST end with a blank newline so headings never collide onto the previous paragraph. Cite Supplemental Material via formal APS `\cite{SM}` (or explicit `\extref` pointers synced cleanly against `\appendix` / `Sec. S...` numbering—never mix `Appendix S1` headers with `Sec. S1` text links). Never rely on printed Roman section numbers (`Sec. II`) in unnumbered PRL Letters.
  - **Rigorous Proofs & Complete Closing**: Every proof exceeding ~15 lines belongs in `sections/supp_*.tex`. Main text must end with `Acknowledgments` and `Data Availability` statements and strip internal `\status{...}` / `%` draft notes before arXiv flattening. Never cite or critique another group's uncompiled `\iffalse` source comments.
  - **APS Length Budget Calibration**: Track both APS word-equivalent (`<= 3,750` including equations at `16` words/row and aspect-scaled figures) AND actual rendered body page count (`<= 4.0–4.3` pages to end of main prose before Acknowledgments/References).

---

## 3. Genre B: Long Paper (`genre = "long"`, PRB / PRX / JHEP / CMP Style)

### Budgets & Format
- **Documentclass**: `\documentclass[aps,prb,twocolumn,superscriptaddress,longbibliography]{revtex4-2}` (or `aps,prx,twocolumn` / `article` as specified in `blueprint.json`).
- **Length**: Typically **6,000 – 14,000 words** across main sections + detailed appendices; each section targets its `target_words` budget in `01_blueprint.json` ($\pm 15\%$).
- **Standard Architecture**:
  1. **Abstract**: 120–220 words summarizing motivation, formal setup, exact main theorems/verdicts, concrete model realizations, and broader physical implications.
  2. **Sec. I — Introduction**:
     - Broad context & concrete unresolved physical obstruction (no generic textbook filler).
     - **Summary of Main Results**: Bullet list or crisp roadmap explicitly citing `\ref{sec:...}` and `\ref{thm:...}` so a busy referee sees every contribution within 2 minutes.
     - **Relation to Prior Work**: Sharp, fair boundary distinguishing prior scalar/Abelian/stabilizer results from this work's generalizations and new mechanisms.
     - **Organization of the Paper**: Brief 1-paragraph map of main sections and appendices.
  3. **Sec. II — Microscopic Setup & Definitions**: Clean introduction of Hilbert space, Hamiltonians, symmetry/bond algebras, and all symbols in `macros.tex` anchored on a canonical toy model.
  4. **Sec. III–V — Core Derivations, Classification & Physical Obstructions**: Each section tackles one well-scoped pillar in the blueprint (`must_cover` / `do_not_repeat`).
  5. **Concrete Realizations & Numerical/Exact Benchmarks**: Explicit lattice Hamiltonians, small-parameter tables, phase diagrams, and numerical/symbolic checks.
  6. **Discussion & Outlook**: Physical takeaways, experimental or numerical signatures, and genuinely open follow-up questions.
  7. **Appendices (`\appendix`)**: Complete zero-skip proofs, auxiliary lemmas, and extended parameter tables cross-referenced from the main text.

---

## 4. Figure, Table, and Equation Craft

- **Self-Contained Captions**: A referee skimming figures and captions alone should grasp the axis labels, parameters, phase boundaries, and physical takeaway without hunting back through 3 pages of text.
- **Equation Hygiene**: Punctuate all display equations grammatically (commas or periods at the end of equations). Use `\eqref{eq:...}` consistently (never bare `(\ref{eq:...})`). Define every symbol appearing in a display equation in the sentence immediately preceding or following it.
- **No Orphan Floating Citations**: Never write `Ref.~\cite{key} proved X` inside prose when `\cite{key}` suffices, unless emphasizing the reference itself; use `~` non-breaking space before `\cite`, `\ref`, and `\eqref` (`Sec.~\ref{sec:setup}`, `Eq.~\eqref{eq:duality}`, `as shown in Ref.~\cite{Kitaev2003}`).
