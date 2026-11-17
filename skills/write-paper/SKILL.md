---
name: write-paper
description: >-
  Modular, re-entrant theoretical & mathematical physics LaTeX paper workflow
  (/write-paper v3, story first): builds a material pack from notes (results ledger,
  red lines, story material), traces a citation-verified lineage, writes a one-page
  story and block plan for the author to approve, lets one author draft the main
  text block by block (Supplemental Material in parallel), then checks afterwards in
  report-only passes (scope, quantifiers, attribution, numbers, the abstract read on
  its own) and hands the issues back to the author; PRL/Nature figure gates,
  DOI/arXiv/.bbl citation audit, deterministic clean-up and build, optional
  cross-vendor checking and blind refereeing.
---

# Theoretical Physics Paper Workflow (`/write-paper` v3)

Turns derivation notes, authority documents (scope, red lines, numerical protocols), numerical reports and good published papers into a LaTeX manuscript. Works with **Claude Code**, **OpenAI Codex CLI**, **Google Antigravity**, **OpenCode**, **Qwen** and a standalone Python CLI. Every stage runs on demand, end to end or on an existing manuscript.

**The idea: write first, check afterwards.** Writers get material (exact results, the story, the lineage, notation) and three bottom lines, not rule lists. Everything a program can check (length limits, acronyms in the abstract, unresolved references, heading breaks, leaks) is checked after writing. Checkers only report; the author fixes, with the smallest edit that keeps the story.

> [!IMPORTANT]
> **Hard context and environment rules**
> 1. **Never load multi-page `.pdf` files into context** (`main.pdf`, `supplement.pdf`). Read `.tex`/`.bbl`/`.log` sources or single-page `.png` previews (`verify_figures.py --renders-out`).
> 2. `workflow_engine.py` finds a Python with `pymupdf + matplotlib + sympy` by itself (override with `WRITE_PAPER_PYTHON`).
> 3. Results are never paraphrased between agents: the plan names them by ledger id and the program pastes the exact ledger records into each writer's and checker's prompt.

---

## The pipeline

| Stage (`--mode`) | What happens | Output |
| :--- | :--- | :--- |
| `inventory` | Scan notes and authority files (unchanged files reuse their cached scan). The program merges the scans into the ledger; one agent only judges: which results are core, which are duplicates, the red lines, the story material. | `00_ground_truth_ledger.json` (every result with exact statement, hypotheses, source, tier core/support), `00_redlines.md` (the author's prohibitions, the only rule list writers see), `00_story_material.md` (motivation, pictures, surprises, history in the notes), `00_conflicts_report.md`; voice samples from `exemplar_paths` |
| `story` | A lineage agent traces 4-8 historical steps; the workflow verifies each reference itself with `verify_citations.py`. A story agent writes the plan. | `01_lineage.md`, **`01_story.md`** (one page for the author: question, lineage, why open, answer in words, why it matters, what goes to the main text / SM / nowhere, block plan, title ideas), `01_plan.json` (blocks naming results only by ledger id), `macros.tex` |
| *checkpoint* | `full_auto` stops here (`pause_after_story`, default on). The author reads `01_story.md`, edits it or `01_plan.json` if needed, then runs `draft_and_check`. | |
| `draft` | One persistent author writes the main text block by block in reading order; a last turn reads it all through and then writes the title and abstract for the text as it stands. SM blocks are written in parallel, rigour first. `main.tex` / `supplement.tex` skeletons are created if missing. | `sections/*.tex`, `main.tex`, `supplement.tex` |
| `figures` | Builds figures with `journal_fig_theme.py` (`prl` / `nature`) from the plan's figure list; `verify_figures.py` PyMuPDF/AST gate with repair rounds. | `figures/*.pdf` |
| `check` | One pass over the finished text; report only. Deterministic: definitions, abstract read alone (acronyms, venue length), references, copy-edit and short-sentence scans, citation metadata (refs.bib repairs and verifiable `\cite{TODO:...}` placeholders are the only edits). LLM, all in one parallel pool: the whole main text read by one checker, the SM in groups of about 60 KB (scope, quantifiers, attribution, numbers, math; never style; against the ledger and the original files), scripted SymPy/numeric checks of up to `max_numeric_checks` (default 4) core results, main-text ones first, and a reader check of the abstract and introduction (overclaims, unexplained symbols, the four questions: what, why, how far earlier work got, what is new). `check_backend` runs it on another vendor. | `03_issues.json`, `audit_reports/01_check_report.md` |
| `revise` | The author fixes the issues: SM files in parallel, then one author reads the whole main text and fixes its issues in one pass, in reading order, abstract last. Smallest edit; extra hypotheses go into the theorem or the SM, not the abstract or introduction; mistaken issues may be declined with a reason. Files with only low-severity issues are skipped unless `revise_low`. Math changes are listed for review, not blocked. Also takes a referee report or free-text instructions (`repair_instructions`). Afterwards only the deterministic checks run again. | `audit_reports/06_revise_report.md`, `01_check_report_after_revise.md` |
| `build` | Deterministic clean-up (invisible characters, typography), dual compile, layout and leak checks, `\status{...}`-free arXiv flattening; an LLM only fixes compile errors. | `submission/` |
| `blind_referee` | Context-isolated panel (specialist, broad reader who first answers the four questions, editor) in a throw-away copy holding only the manuscript, bibliography, figure PNGs and audit JSONs. `referee_backend` runs it on another vendor. | `05_blind_referee_report.md` (feed it to `revise`) |

**Pipelines.** The text is written whole first, then checked once: `full_auto` = inventory → story → *pause* → (draft → figures → check → revise → deterministic re-check → build). `draft_and_check` = the part in brackets. `check_and_revise` = check → revise → deterministic re-check → build. A second LLM check and the referee panel run only when asked (`check`, `blind_referee`). `cleanup` = deterministic clean-up only. For one paper, `inventory` and `story` usually run once; `draft_and_check` can be repeated.

**v2 names still work:** `blueprint`→`story`, `verify`/`citations`→`check`, `hygiene`→`cleanup`, `repair`→`revise`, `draft_and_audit`→`draft_and_check`, `audit_polish`→`check_and_revise`. v2 workspaces need `inventory` + `story` once before `draft` (v3 drafts from `01_plan.json`).

---

## Choosing your execution path

Let `<SKILL_DIR>` be this skill's directory (typically `~/.claude/skills/write-paper`, `~/.gemini/config/skills/write-paper`, `~/.codex/skills/write-paper`, or `<project>/.agents/skills/write-paper`).

### Path A: orchestrated CLI (Claude Code, Codex, OpenCode, terminal)
`scripts/run_paper.py` dispatches `claude -p`, `agy -p` or `codex exec` workers:
```bash
# 1. material, lineage, story; stops for you to read .workflow/01_story.md
python <SKILL_DIR>/scripts/run_paper.py --mode full_auto --paper-dir ./paper \
  --source-paths ./notes ./numerics --authority-paths ./SCOPE.md \
  --exemplar-paths ./a_well_written_prl.tex --genre letter --figure-style prl
# 2. after reading (and editing) the story: write, check, revise, build; checks on another vendor
python <SKILL_DIR>/scripts/run_paper.py --mode draft_and_check --paper-dir ./paper --genre letter \
  --source-paths ./notes --authority-paths ./SCOPE.md --backend agy --check-backend claude
```

### Path B: native Antigravity `run_workflow`
Invoke `run_workflow` with `ScriptPath="<SKILL_DIR>/scripts/paper_workflow.py"` and `Args` matching `scripts/workflow.json`. `run_workflow` compiles the script as `'<workflow>'` and passes neither `__file__` nor `ScriptPath`, so the entry script finds the engine only through `Args.skill_dir`, the workspace, or the installed copy. **When running a checkout that is not installed, pass `Args.skill_dir = "<SKILL_DIR>"`.** The entry script refuses to start if the engine it found has a different version, and the log line `[engine] ... loaded from ...` shows which copy ran.

> [!IMPORTANT]
> **Call `run_workflow` from the Planning Turn.** If the session shows `Plan mode is on`, call `run_workflow` right there, even for modes that edit files. Do **not** call `exit_plan_mode` first: from an Execution Turn, `model="inherit"` sub-agents start without a plan and deadlock on `[plan review due]`.

The engine also guards against this: at start-up it reads the session trajectory (the latest `USER_INPUT` or completed `enter_plan_mode` versus the latest completed `exit_plan_mode`) to tell a Planning Turn from an Execution Turn (override with `Args.plan_mode = "on" | "off"`). In an Execution Turn, calls that would use `inherit` go to an independent `agy` CLI session (persistent agents resume it with `--conversation`), or to `model="pro"` when no `agy` binary is installed. `Args.check_backend` / `Args.referee_backend` send those stages to another vendor's CLI from inside Antigravity too.

**Model routing (`routing_policy`, default `auto_tiered`).** Each call site declares a task tier; an explicit `models[stage]` always wins (v2 stage names map to v3 ones), and `uniform_inherit` restores "host/CLI default everywhere":

| Tier | Call sites | Antigravity (planning turn) | agy CLI | claude CLI |
| :--- | :--- | :--- | :--- | :--- |
| `heavy` | source scans, lineage, story and plan, checkers, reader check, blind referees, revision planner | `inherit` | `gemini-4-carbon-high` → `gemini-pro-orchestrator` | `opus` |
| `builder` | material synthesis, the author, SM blocks, figures, scripted numeric checks, revisions, citation fixes | `inherit` | `gemini-pro-orchestrator` | CLI default |
| `fast` | compile fix, plan JSON repair | `flash` | `gemini-4-hydrogen-high` → `gemini-3.8-flash-high` | `sonnet` |
| `probe` | smoke-test pings | `flash` | `gemini-3.8-flash-high` → `gemini-3.8-flash-medium` | `haiku` |

agy model ids are checked once per run against `agy models`; if none of a tier's ids is offered, no `--model` is passed. Codex always uses its default configuration. Tiers set the model only; whether a worker may write is decided per call (`read_only`).

### Path C: host-native sub-agents plus the CLI gates
Driving the stages yourself with your host's sub-agents (`Task` in Claude Code, `swarm`/`subagent` in Codex/Antigravity): keep the same order (material → story the author approves → one author drafts block by block → report-only checks → the author revises), read `references/genre_templates.md` and `references/figure_visual_language.md` as notes, and run the deterministic tools below as the checks.

---

## Deterministic CLI tools (`<SKILL_DIR>/scripts/tools/`)

1. **Math, label, citation and quantifier diff**: `check_math_integrity.py --before <orig.tex> --after <edited.tex> [--allow-cite-changes]`
2. **Figures** (`prl` / `nature`): `verify_figures.py --pdf-dir <figures_dir> --scripts-dir <fig_scripts_dir> --tex-dir <paper_dir> --style prl --renders-out <renders_dir>` (theme: `journal_fig_theme.py`; notes: `references/figure_visual_language.md`)
3. **Definitions, internal tags, references, the abstract read alone**: `audit_tex_defs.py --main <main.tex> [--supplement <supplement.tex>] [--abstract-max-chars 600] [--abstract-allowed-acronyms ...]`
4. **Invisible characters and typography**: `clean_watermarks.py --paths <dir> --fix` (connector swaps only with a vetted lexicon profile; the workflow never passes one)
5. **Voice-sample profile and short-sentence report**: `style_metrics.py profile --inputs <exemplars...> --out <profile.json>` / `style_metrics.py verify --inputs <tex_files...> --profile <profile.json> --no-lm`
6. **Citations**: `verify_citations.py audit --tex-dir <paper_dir> --bib <refs.bib> [--fix-braces] [--upgrade-preprints] [--allow-offline]`; `verify_citations.py verify-entry --query "..."` lists candidates only; confirm with `--doi <doi> --title "<title>"` (or `--arxiv <id>`) and use the BibTeX only when `verified: true`.
7. **Build**: `build_and_flatten_tex.py --main <main.tex> [--supplement <supplement.tex>] --compile --flatten-out <submission/main_arxiv.tex> --inline-bbl`

**Tool contract (all seven):** exit `0` = pass, `1` = issues found (details in the JSON), `2` = usage/tool error. Gates fail closed (unverifiable citations, empty inputs, missing paths). Every in-place fix keeps encoding and line endings and leaves the previous version in `.wp_backups/`.

**Worker hygiene and safety (all runtimes).** Reference notes are copied into `.workflow/refs/` and handed to agents as paths (never as excerpts cut at a fixed length). Every worker prompt carries a search scope (the paper directory plus the exact source/authority/exemplar paths, never their parent folders) and must kill its background tasks before finishing (Antigravity: `manage_task` list + kill). Every agent await is bounded by `agent_timeout_s` (default 1800 s); a timed-out call is reported and never retried, because it may already have edited files. Read-only is enforced for every backend after the fact: the manuscript, `macros.tex`, `refs.bib` and the ledger, red-line, story and plan files are copied in memory before each read-only call (and each scripted check) and restored if changed; new `.tex` files go to `.workflow/readonly_violations/`. Edit workers are guarded too: a file changed by an agent assigned to another file is restored from the checkpoint. Headless `agy` workers that may write run with `--mode accept-edits --settings '{"toolPermission":"always-proceed"}'` (session-only); read-only `agy` workers get neither. Every `agy` answer carries a per-turn nonce, so a resumed conversation that fails before `finish` cannot pass off the previous turn's answer. With `repair_instructions`, the full text is always available to each worker as a file (inline text is saved to `.workflow/repair_instructions_<ts>.md`); the planner only routes work. Orchestrator limits: `--agent-timeout` (1800 s), `--agent-retries` (2), `--max-scan-agents` (24), `--max-numeric-checks` (4), `--max-parallel` (4; raise it to 6-8 if the API allows, every parallel phase gets faster).
