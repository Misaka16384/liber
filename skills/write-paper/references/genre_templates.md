# Genre Notes for Physics Papers

These are notes for the author, not a checklist. Write the story first; the `check` stage looks for the
mechanical problems afterwards (lengths, undefined acronyms, unresolved references, heading breaks, leaks),
so none of them needs to be on your mind while drafting.

---

## What a reader needs

A physicist from a neighbouring field should be able to answer four questions after the abstract and the
introduction: **what is the question, why does it matter, how far did earlier work get, and what is new here.**
Most of what follows serves those four answers.

- **Question before formalism.** Open with the physical puzzle, then introduce the objects needed to state it.
- **History as a story.** Earlier work is a sequence of questions and partial answers that leads to this one:
  who asked what, what they found, what they left open. A list of citations is not a history.
- **Words before formulas.** Say what a result means and why it should be true, then give the formula.
- **One concrete model early.** A Hamiltonian the reader can write down anchors every abstract statement.
- **Each new symbol, once, when it first appears**, with a sentence on what it does physically.
- **Honest scope.** State the hypotheses of a theorem where you state the theorem. Do not repeat them in
  every sentence that mentions the result; in the abstract and the introduction, plain words that do not
  claim more than the theorem are enough.
- **Prior work vs. this work.** Say clearly what was known and what is new, and credit others precisely.

---

## Letter (`genre = "letter"`, PRL / Nature Physics style)

**Budget (checked automatically):** about 3,750 words in total, counting display equations (~16 words per line)
and figures (~150-300 words each); the abstract at most **600 characters including spaces**. That leaves
roughly 2,400-2,900 words of prose.

**Shape that often works:**
- **Abstract.** Context and question, the answer in words, why it matters. Every acronym and symbol it uses
  is explained inside it; prefer words to symbols. It is read on its own.
- **Opening paragraphs.** The puzzle, its history in a few sentences, the gap, and the result in words,
  with one concrete model.
- **Body.** Setup, the main result, the mechanism (why it is true), examples or numerics, discussion. Run-in
  headings (`\emph{Model.---}`) instead of numbered sections are usual.
- **Supplemental Material.** Proofs longer than about fifteen lines, case-by-case verifications, long tables.
  Point to it with `\cite{SM}` or explicit section references.
- End with acknowledgments and a data-availability statement (placeholders for the human author are fine).

---

## Long paper (`genre = "long"`, PRB / PRX / JHEP / CMP style)

**Length:** typically 6,000-14,000 words including appendices.

**Shape that often works:**
- **Abstract** (150-220 words): motivation, setting, main results in words, concrete realizations, implications.
- **Introduction:** the problem and its history; a short summary of results pointing to sections and
  theorems; relation to prior work (what was known, what is new); a one-paragraph map of the paper.
- **Setup:** Hilbert space, Hamiltonians, symmetries and notation, anchored on a canonical example.
- **Core sections:** one pillar each; derivations complete, long case checks in appendices.
- **Examples and benchmarks:** explicit models, phase diagrams, numerical or symbolic checks.
- **Discussion:** takeaways, signatures, genuinely open questions.

---

## Craft notes

- **Captions stand alone:** axes, parameters, phase boundaries and the takeaway, without the main text.
- **Equations are part of sentences:** punctuate them; define every symbol next to its first display.
- **References:** `Eq.~\eqref{...}`, `Sec.~\ref{...}`, `Ref.~\cite{...}`; use `\cite` as a parenthesis rather
  than a subject unless the reference itself is the point.
- **Notation:** one symbol per object across main text and supplement; macros live in `macros.tex`.
