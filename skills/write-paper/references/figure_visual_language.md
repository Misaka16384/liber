# Scientific Visual Design & Typography Language (*APS PRL* & *Nature / Nature Physics*)

This specification governs the `figures` phase and the deterministic `verify_figures.py` gate. Its primary goal is to eliminate "AI infographic / SaaS slide-deck" aesthetics and produce figures that belong indistinguishably alongside flagship *Physical Review Letters* or *Nature Physics* articles.

---

## 1. House Style Specifications (`prl` vs. `nature`)

Use `journal_fig_theme.py` (`apply_house_style(style="prl"|"nature")`, `add_panel_label(...)`). Never rescale finished figure PDFs in LaTeX (`\includegraphics` should embed at `width=\columnwidth` or `width=\textwidth` with matching physical inches).

| Dimension / Rule | *APS Physical Review Letters* (`prl`) | *Nature* / *Nature Physics* (`nature`) |
| :--- | :--- | :--- |
| **Single-Column Width** | `3.375 in` (`8.6 cm`) | `3.504 in` (`89 mm`) |
| **1.5-Column Width** | `4.50 in` (`11.4 cm`, rare) | `4.724 in` (`120 mm`) |
| **Double-Column Width** | `7.008 in` (`17.8 cm`) | `7.205 in` (`183 mm`) |
| **Typeface Family** | **Serif** matching REVTeX: `Times New Roman`, `STIX Two Text`, `STIXGeneral` + `mathtext.fontset="stix"` (never `DejaVu*`) | **Sans-serif strictly mandatory**: `Arial` or `Helvetica` + `mathtext.fontset="stixsans"` (never serif body or `DejaVu*`) |
| **Axis / Body Label Size** | `7.5 – 9.0 pt` at 1:1 print scale | `6.0 – 7.0 pt` (strictly `<= 7.0 pt` ceiling) |
| **Tick / Legend / Annotation** | `6.5 – 8.0 pt` (hard floor `6.0 pt` inside insets; `>= 7.0 pt` main) | `5.0 – 6.5 pt` (hard floor `5.0 pt` everywhere) |
| **Panel Label Style** | `(a)`, `(b)`, `(c)` — `8.5 pt` upright roman (or bold), inside upper-left of data plots or top-left of schematics; **NO descriptive panel titles** | `a`, `b`, `c` — `8.0 pt` **bold upright lowercase without parentheses**, outside top-left; **NO descriptive panel titles** |
| **Data Axis Spines & Ticks** | Closed 4-spine box (`top=True, right=True`), pure `#000000`, inward ticks on all 4 sides with minor ticks ON | Open L-shaped axes (`top=False, right=False`), pure `#000000`, outward ticks on bottom & left only |
| **Stroke / Line Weights** | Spines `0.6–0.75 pt`; data curves `1.0–1.4 pt` | Strict `0.25–1.0 pt` envelope: spines `0.5–0.65 pt`; data curves `0.75–1.0 pt` |
| **Background & Legends** | Pure `#FFFFFF` canvas, zero UI tinted boxes, `legend.frameon=False` (or borderless white mask) | Pure `#FFFFFF` canvas, `legend.frameon=False`, zero background grids |
| **Vector Embedding** | `pdf.fonttype = 42`, `ps.fonttype = 42` (zero Type 3 bitmaps/paths) | `pdf.fonttype = 42`, `ps.fonttype = 42` |

> **Print Legibility Gate**: All multi-character text/tick strings MUST remain `>= 7.0 pt` at target print column width in `prl` (`>= 5.5 pt` inset floor; `>= 5.0 pt` global floor in `nature`), and zero vector strokes, curves, or error bars may cross the interior core of any text bounding box.

---

## 2. Top 10 "AI Infographic Tells" — Banned Patterns

1. **Rounded Callout Cards & Nested Pastel Boxes**: `FancyBboxPatch(boxstyle="round...")` grouping panels, captions, or equations like slide-deck cards.
2. **Solid-Fill SaaS "Status Pills"**: Borderless colored rounded rectangles with bold white text (`[g'(0)=1]`). Quantum circuit / operator boxes in schematics must be crisp sharp rectangles (`fc='#FFFFFF', ec='#000000'`, black text).
3. **`DejaVu Serif` / `DejaVu Sans` Default Bleed**: Matplotlib's bundled wide `DejaVu` family clashes visually with slender `STIX` math symbols and screams unmodified default config.
4. **Tailwind UI Off-Black Ink & Pastel Canvas Fills**: `#F8FAFC` / `#F1F5F9` backgrounds and `#1B263B` navy ink instead of true `#000000` on `#FFFFFF`. Neutral light gray/tinted fills are permitted *only* inside genuine phase-diagram regions at `alpha <= 0.12` (with hatching or distinct boundary curves), never as UI container cards.
5. **Full-Sentence Panel Title Banners (`ax.set_title("(a) ...")`)**: Titles above subplots belong exclusively in LaTeX `\caption{...}`. Leave ONLY bare `(a)` or **`a`**.
6. **Numbered Theorem-Proof Flowcharts Inside Figures**: Flowcharts containing boxed sentences (`"1. T-invertibility -> 2. Unique q_T -> 3. Clash!"`) serialize algebra instead of illustrating physical geometry. Put proofs in `\begin{proof}` prose; draw physical/lattice/topological geometry in figures.
7. **Matplotlib Text Tables & Trivial 2-Bar Charts**: Multi-row text tables belong in LaTeX `ruledtabular` tables, not wasted inside figure PDFs; scalar 2-number comparisons belong in prose or actual multi-point spectrum plots.
8. **Microscopic Crushed Labels (`< 6.0 pt` PRL / `< 5.0 pt` Nature)**: Shrinking text to fit over-stuffed diagrams instead of simplifying the figure and moving run parameters/notes into `\caption`.
9. **Infographic Embellishments on Data Plots**: Unjustified `axhspan` ribbons, lollipop stems hanging from isolated dispersion points, bordered legend boxes, and oversized glowing stars sitting on axis spines.
10. **Manual Math/Aspect Hacks**: Using polynomial arcs to draw matrix parentheses (`draw_mat2`) or `Ellipse` aspect math instead of `ax.set_aspect('equal')` and proper LaTeX/mathtext.

---

## 3. Division of Labor: Canvas vs. `\caption` vs. `Table`

- **Keep on Visual Canvas**: Lattice sites (open/filled circles, `lw=0.65` black bond wires), sharp operator gate boxes, cut lines, interaction range arcs with short symbol tags (`d=1`), clean data curves paired with distinct `linestyle`/`marker` for grayscale legibility, bare `(a)` / `a` identifiers, and axis variables/units.
- **Move to `\caption{...}`**: Full sentence titles for panels, constant run metadata (`t=0.630, \chi=128`), numerical error bounds/spreads (`spread 6\times 10^{-12}`), methodological disclaimers, and color/symbol keys.
- **Move to LaTeX `ruledtabular` Table**: Polynomial classifications, case-by-case parameter matrices, and convergence benchmarks.
- **Color + Monochrome Rule**: Use the Okabe–Ito / Paul Tol palette (`#0072B2` blue, `#D55E00` vermilion, `#009E73` teal, `#AA3377` purple, `#E69F00` amber with dark edge, `#666666` gray) paired with distinct linestyles (`-`, `--`, `-.`, `:`) and markers (`o`, `s`, `^`, `D`, `v`) so grayscale printing preserves 100% curve distinguishability.

> [!IMPORTANT]
> **Hard Context Rule**: Agents inspecting figures MUST NEVER load multi-page `.pdf` manuscripts directly into context (use `verify_figures.py` to render `300 DPI` PNG previews and inspect only `.png` renders and `.json` telemetry). Figure-text notation consistency edits MUST inspect both `macros.tex`/main/SM `.tex` and figures together—never mutate one side to break agreement with the other.
