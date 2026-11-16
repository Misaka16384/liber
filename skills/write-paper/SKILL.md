---
name: write-paper
description: >-
  Modular, re-entrant theoretical & mathematical physics LaTeX paper workflow
  (/write-paper v2): builds GT/scope lock tables (`00_locks.json`), reverse-checks
  blueprints against raw authorities, curates Few-Shot exemplar voice & agent-vetted
  non-scientific connector styles, drafts sections in capped batches, designs &
  geometrically verifies PRL/Nature figures via PyMuPDF + AST, runs 3-way raw-authority
  SymPy verification, audits BibTeX + dual DOI/arXiv + rendered .bbl outputs, performs
  positive-ROI hygiene (zero 4-gram destruction), simulates context-isolated 3-role
  blind refereeing (optionally cross-vendor), and supports checkpointed `repair` reentry.
---

# Theoretical Physics Paper Workflow (`/write-paper` v2)

Cross-agent modular workflow (compatible with **Claude Code**, **OpenAI Codex CLI**, **Google Antigravity**, **OpenCode**, **Qwen**, and standalone Python CLI) turning derivation notes, authoritative scope/redline documents, numerical reports, and prior group papers into verified, publication-ready LaTeX manuscripts. Every stage runs **on demand**—end-to-end or plugged straight into an existing manuscript (`figures`, `verify`, `citations`, `hygiene`, `build`, `blind_referee`, `repair`).

> [!IMPORTANT]
> **Hard Context & Environment Rules**:
> 1. **NEVER load multi-page `.pdf` files directly into context** (`main.pdf`, `supplement.pdf`). Always inspect `.tex`/`.bbl`/`.log` source files or rendered single-page `.png` previews (`verify_figures.py --renders-out`).
> 2. **Scientific Python Auto-Resolution**: `workflow_engine.py` automatically locates a system Python interpreter containing `pymupdf + matplotlib + sympy` (override via `WRITE_PAPER_PYTHON`).
> 3. **Anti-SynthID / 4-Gram Destruction Disabled**: Voice naturalness comes from **Few-Shot exemplar imitation** during `draft` plus **positive-ROI hygiene** (no short-sentence chopping, no unvetted verb replacements).

---

## Choosing Your Execution Path (Cross-Agent Compatible)

Let `<SKILL_DIR>` be this skill's directory (typically `~/.claude/skills/write-paper`, `~/.gemini/config/skills/write-paper`, `~/.codex/skills/write-paper`, or `<project>/.agents/skills/write-paper`).

### Path A: Universal Orchestrated CLI (`Claude Code`, `Codex`, `OpenCode`, Terminal)
Run the automated orchestrator cleanly via `scripts/run_paper.py` (automatically dispatches `claude -p`, `agy -p`, or `codex exec`, including optional **cross-vendor blind refereeing** via `--referee-backend`):
```bash
python <SKILL_DIR>/scripts/run_paper.py \
  --mode draft_and_audit \
  --paper-dir ./paper \
  --source-paths ./notes ./numerics \
  --authority-paths ./SCOPE_AND_TRUTH.md \
  --exemplar-paths ./prior_group_paper.tex \
  --genre letter \
  --figure-style prl \
  --backend auto \
  --referee-backend agy
```

### Path B: Native Antigravity `run_workflow`
Invoke `run_workflow` with `ScriptPath="<SKILL_DIR>/scripts/paper_workflow.py"`, `Args` matching `scripts/workflow.json`. `run_workflow` compiles the script as `'<workflow>'` and passes neither `__file__` nor `ScriptPath` into it, so the entry script finds the engine only through `Args.skill_dir`, the workspace, or the installed copy. **When running a checkout that is not installed, pass `Args.skill_dir = "<SKILL_DIR>"`.** The entry script refuses to start if the engine it found has a different version (instead of silently running a stale install), and the log line `[engine] ... loaded from ...` shows which copy ran.

> [!IMPORTANT]
> **Call `run_workflow` from the Planning Turn.** If the session shows `Plan mode is on`, call `run_workflow` right there, even for modes that edit files. Do **not** call `exit_plan_mode` first: from an Execution Turn, `model="inherit"` sub-agents start without a plan and deadlock on `[plan review due]`.

The engine also guards against this itself: at start-up it reads the session trajectory (the latest `USER_INPUT` or completed `enter_plan_mode` versus the latest completed `exit_plan_mode`) to tell a Planning Turn from an Execution Turn (override with `Args.plan_mode = "on" | "off"`). In an Execution Turn, calls that would use `inherit` go to an independent `agy` CLI session (multi-turn reviewers resume it with `--conversation`), or to `model="pro"` when no `agy` binary is installed.

**Model routing (`routing_policy`, default `auto_tiered`).** Each call site declares a task tier; an explicit `models[stage]` always wins, and `uniform_inherit` restores "host/CLI default everywhere":

| Tier | Call sites | Antigravity (planning turn) | agy CLI | claude CLI |
| :--- | :--- | :--- | :--- | :--- |
| `heavy` | source scans, reverse blueprint audit, 3A/3C reviewers, blind referees, repair planner | `inherit` | `gemini-4-carbon-high` → `gemini-pro-orchestrator` | `opus` |
| `builder` | ledger synthesis, lexicon vetting, blueprint, drafting, figures, 3B SymPy + fixes, citation fixes, repair edits | `inherit` | `gemini-pro-orchestrator` | CLI default |
| `fast` | copy-edit, compile fix, blueprint JSON repair | `flash` | `gemini-4-hydrogen-high` → `gemini-3.8-flash-high` | `sonnet` |
| `probe` | smoke-test pings | `flash` | `gemini-3.8-flash-high` → `gemini-3.8-flash-medium` | `haiku` |

agy model ids are checked once per run against `agy models`; if none of a tier's ids is offered, no `--model` is passed (the CLI default is used). Codex always uses its default configuration. Tiers set the model only; whether a worker may write is decided separately per call (`read_only`).

### Path C: Host-Native Subagent Orchestration + CLI Gates
If driving stages step-by-step with your host's native subagents (`Task` in Claude Code, `swarm`/`subagent` in Codex/Antigravity):
1. Follow the contracts in `references/genre_templates.md`, `references/humanizer_physics_rules.md`, and `references/figure_visual_language.md`.
2. Enforce every deterministic gate directly using the standalone tools in `<SKILL_DIR>/scripts/tools/` below.

---

## On-Demand Modes & Stages

### Composite Pipelines
- **Interactive Two-Stage (Recommended)**: `inventory` -> `blueprint` (extracts `00_locks.json`, vets Few-Shot lexicon, reverse-audits `01_blueprint.json` against raw authorities, pauses for user sign-off) -> `draft_and_audit` -> optional `figures` / `blind_referee`.
- **Interactive Three-Stage**: Pause after `draft` + `build` before running `audit_polish`.
- **Full-Auto**: `mode="full_auto"` (end-to-end through `blind_referee`).

### Standalone Atomic Phases (Plug Into Any Existing Paper)
| Mode | Purpose |
| :--- | :--- |
| `inventory` | Build `00_ground_truth_ledger.json`, hard `00_locks.json`, Few-Shot `00_style_profile.json` + **Lexicon Safety Vetting Agent** sign-off. |
| `blueprint` | Literature seed check + `01_narrative_brief.md` + `macros.tex` + `01_blueprint.json` + **Reverse Blueprint-vs-GT Audit Gate**. |
| `draft` | Capped (`<=4`) parallel section drafting using Few-Shot exemplar paragraphs + continuity assembly + early `audit_tex_defs.py`. |
| `figures` | Build/refactor figures with `journal_fig_theme.py` (`prl` \| `nature`), run `verify_figures.py` PyMuPDF/AST hard gate + PNG review + bidirectional caption sync. |
| `verify` | `audit_tex_defs.py` + 3-way verification directly against raw `authority_paths`/`00_locks.json` + SymPy verification scripts. |
| `citations` | Dual DOI+arXiv cross-verification + `.bbl` render audit + Claim Fidelity guarded by `--allow-cite-changes --check-quantifiers` with diff-guided retry. |
| `hygiene` | Watermark/typography scrubbing + copy-edit scan + precision fixes ONLY on flagged sections (`ai_slop`, `copyedit_issues`, or staccato `<=8`-word clusters). |
| `build` | Dual `main.tex` + `supplement.tex` build, log + `\letterhead \par` + APS page/word-eq inspection, and `\status{...}`-free arXiv flattening. |
| `blind_referee` | **Context-isolated** 3-role panel (Specialist Referee, Broad Reader, Associate Editor) run inside a throw-away copy holding only `.tex`/`.bib`/`.bbl`/`.png`/tool JSONs -> `05_blind_referee_report.md`. |
| `repair` | **Fallback & patch mode**: full project checkpoint -> ingest `repair_instructions` + optional `repair_files` + `sync_bib` -> patch -> full deterministic verification + build. |

---

## Standalone Deterministic CLI Tools (`<SKILL_DIR>/scripts/tools/`)

1. **Math, Label, Citation & Quantifier Lock**:
   `python <SKILL_DIR>/scripts/tools/check_math_integrity.py --before <orig.tex> --after <edited.tex> --check-quantifiers [--allow-cite-changes]`
2. **Figure Style Helper & PyMuPDF Geometric/AST Hard Gate (`prl` / `nature`)**:
   `python <SKILL_DIR>/scripts/tools/verify_figures.py --pdf-dir <figures_dir> --scripts-dir <fig_scripts_dir> --tex-dir <paper_dir> --style prl --renders-out <renders_dir>`
   (Theme library: `scripts/tools/journal_fig_theme.py`; rules: `references/figure_visual_language.md`)
3. **Knowledge-Curse, Internal Tag & Symbol Definition Audit**:
   `python <SKILL_DIR>/scripts/tools/audit_tex_defs.py --main <main.tex> [--supplement <supplement.tex>] [--extra-symbols ...]`
4. **Watermark, Typography & Copy-Edit Hygiene (`rank rank`, `at most <=`, `Sec.`)**:
   `python <SKILL_DIR>/scripts/tools/clean_watermarks.py --paths <dir> --profile <00_style_profile.json> --fix`
5. **Exemplar Few-Shot Profiling & Anti-Staccato Audit**:
   `python <SKILL_DIR>/scripts/tools/style_metrics.py profile --inputs <exemplars...> --out <00_style_profile.json>`
   `python <SKILL_DIR>/scripts/tools/style_metrics.py verify --inputs <tex_files...> --profile <00_style_profile.json> --no-lm`
6. **Dual DOI+arXiv & Rendered `.bbl` Citation Audit**:
   `python <SKILL_DIR>/scripts/tools/verify_citations.py audit --tex-dir <paper_dir> --bib <refs.bib> [--fix-braces] [--upgrade-preprints] [--allow-offline]`
   `python <SKILL_DIR>/scripts/tools/verify_citations.py verify-entry --query "..."` lists candidates only; confirm with `--doi <doi> --title "<title>"` (or `--arxiv <id>`) and use the BibTeX only when `verified: true`.
7. **Dual Compile, Layout Audit & arXiv Leak-Stripped Flatten**:
   `python <SKILL_DIR>/scripts/tools/build_and_flatten_tex.py --main <main.tex> [--supplement <supplement.tex>] --compile --flatten-out <submission/main_arxiv.tex> --inline-bbl`

**Worker hygiene (all runtimes):** every sub-agent prompt carries a search scope (the paper directory plus the exact source/authority/exemplar paths, never their parent folders) and must kill its background tasks before finishing (Antigravity: `manage_task` list + kill). Every agent await, including Antigravity `agent(...)` handles, is bounded by `agent_timeout_s` (default 1800 s); a timed-out call is reported and never retried, because it may already have edited files. In `repair`, the full instructions are always available to each worker as a file (inline text is saved to `.workflow/repair_instructions_<ts>.md`); the planner only routes work, and `repair_skip_plan` (automatic for a single `repair_files` entry) skips it.

**Tool contract (all seven):** exit `0` = pass, `1` = issues found (details in the JSON), `2` = usage/tool error. Gates fail closed (unverifiable citations, empty inputs, missing paths). Every in-place fix keeps encoding and line endings and leaves the previous version in `.wp_backups/`. Orchestrator limits: `--agent-timeout` (1800 s), `--agent-retries` (2), `--max-scan-agents` (24); routing: `--routing-policy auto_tiered|uniform_inherit` (a uniform `--model` disables tiering). Headless `agy` workers that may write run with `--mode accept-edits --settings '{"toolPermission":"always-proceed"}'` (session-only; needed so scripts run without a confirmation prompt); read-only `agy` workers get neither. Only the Claude CLI can deny write tools, so read-only is enforced for every backend after the fact: the manuscript, `macros.tex`, `refs.bib` and the ground-truth files are copied in memory before each read-only call and restored if it changed them (new `.tex` files go to `.workflow/readonly_violations/`). An `agy` run that reports `status: ERROR` after an interrupted-and-resumed stream is still accepted when it returned a complete, schema-shaped answer stamped with this turn's nonce (every `agy` answer carries a per-turn `_turn` field or `TURN <nonce>` line, because a resumed conversation that fails before `finish` re-emits the previous turn's answer). In `verify`, auto-fixes are grouped by normalised paper-relative path (one Fix agent per file), and only the last review round's blockers are reported and marked in the sources; blockers that the fixes resolved are listed separately.
