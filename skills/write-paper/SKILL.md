---
name: write-paper
description: >-
  Modular, re-entrant theoretical & mathematical physics LaTeX paper workflow
  (/write-paper v4: the author decides the story, AI writes and checks). Starts from
  any stage on a paper at any completion: a survey of where it stands, a material
  pack from notes (or from the paper itself), a briefing (results with their
  evidence, a citation-verified literature map, open questions) for a story
  discussion in which the author speaks first, a story/plan/decision record the
  author confirms, one author drafting only missing or partial blocks, report-only
  checks (scope, quantifiers, attribution, numbers, whether each citation fits its
  sentence, the abstract read alone), revision, deterministic clean-up and build.
  Findings that touch the story go back to the author as concerns. Effort levels
  (low/medium/high), PRL/Nature figure gates, cross-vendor checking, blind referees.
---

# Theoretical Physics Paper Workflow (`/write-paper` v4)

Turns derivation notes, authority documents (scope, red lines, numerical protocols), numerical reports and good published papers into a LaTeX manuscript, or picks up a manuscript that is already half or fully written. Works with **Claude Code**, **OpenAI Codex CLI**, **Google Antigravity**, **OpenCode**, **Qwen** and a standalone Python CLI.

**Who does what.** The author owns the story and the structure; agents do the repetitive work (drafting, checking, citations, build). Agents present information before the author decides, and the author's decisions are the current best judgement, not rules: when an agent finds that a decision rests on something that does not hold, it neither obeys silently nor overrides silently, it files a **concern** for the author. Writers get material (exact results, the story, the literature map, notation) and three bottom lines, not rule lists; everything a program can check is checked after writing.

> [!IMPORTANT]
> **Hard context and environment rules**
> 1. **Never load multi-page `.pdf` files into context** (`main.pdf`, `supplement.pdf`). Read `.tex`/`.bbl`/`.log` sources or single-page `.png` previews (`verify_figures.py --renders-out`).
> 2. `workflow_engine.py` finds a Python with `pymupdf + matplotlib + sympy` by itself (override with `WRITE_PAPER_PYTHON`).
> 3. Results are never paraphrased between agents: the plan names them by ledger id and the program pastes the exact ledger records into each writer's and checker's prompt.

---

## `auto`: run what the paper still needs

`--mode auto` first surveys the paper (`audit_reports/00_survey.md`: each block empty / partial / full, the plan, hand edits since the last run, open concerns, what comes next) and then runs only what is missing. It stops for the author at three points: the **briefing** (story discussion), the **story confirmation**, and **blocking concerns**. Run `auto` again after each stop. `--no-pause` (`pause_after_story=false`) is an unattended run: the agent chooses the story itself (marked `source: ai`) and concerns wait in the list.

| Paper as it is | What `auto` does |
| :--- | :--- |
| Notes only | material pack → briefing → *you discuss* → story → *you confirm* → draft → figures → check → revise → build |
| Half written | material pack → briefing that also says what story the draft tells now → *you discuss* → story (keeps existing files) → *you confirm* → writes only missing/partial blocks → check → revise → build |
| Fully written | material pack (from the notes, or from the paper itself when none are given) → check → revise → build; the story is not reopened unless a concern requires it |
| Text unchanged since the last check | nothing (`--from check` forces a check) |

`--from brief|story|draft|check|revise|build` starts at that stage and runs to the end (`brief` reopens the story; `check` skips writing). `--only <files>` limits draft, check and revise to those files. The author's **hand edits** between runs are detected (`.workflow/05_hand_edits.diff`), passed to every agent as intended changes, and never reverted.

### Talking with the author (host agent)

At the **briefing stop** (`.workflow/01_briefing.md`, `01_literature_map.md`), in the author's language:
1. Give the core information of the briefing: results with their evidence and scope, the literature (lineage, neighbouring work, concurrent work), limits and red lines, the open questions. **No storyline, no recommendation yet.**
2. Ask for the author's idea and let them speak first.
3. Then give your own view: what supports or weakens their idea, what history the introduction would need, likely referee objections, alternatives you see.
4. Discuss until you agree. Write the agreed points, including what was decided against and why, to `.workflow/01_discussion.md`, and run `auto` again.

At the **confirmation stop**, show the story (`01_story.md`, including the choices under "Filled in by the AI") and the decision record (`01_decisions.json`: each decision with what it rests on). When the author confirms or has edited them, run `auto` again: they are locked and writing starts.

At a **concerns stop** (`audit_reports/07_feedback.md`), give each concern: the finding, the evidence, the decision it touches and what that decision rested on, the options (keeping the decision is always one) and the recommendation. Record each answer in `.workflow/07_feedback.json` (`answer` in the author's words, `status: "answered"`, `keep: true` when the author keeps the decision) and run `auto` again: answers that change something update the story, plan and decision record, and only the blocks they touch are rewritten.

### Effort

`--effort low|medium|high` (default medium) sets how much checking and literature work a run does; the writing is the same. Explicit `--max-parallel`, `--max-numeric-checks`, `--revise-low` win.

| | low | medium | high |
| :--- | :--- | :--- | :--- |
| Agents in parallel | 6 | 4 | 4 |
| Literature map | lineage (3-5 steps) | lineage + 2-3 neighbouring clusters + concurrent work | lineage + 3-4 clusters |
| Scripted numeric checks | 0 | 4 | 10 |
| Reader check (abstract, introduction) | merged into the main-text check | separate | separate |
| Citation-context check | off | 40 sentences per checker | 20 per checker; may read arXiv full text |
| Revise files with only low-severity issues | no | no | yes |
| Blind referee panel at the end | no | no | yes |

---

## The stages

| Stage (`--mode`) | What happens | Output |
| :--- | :--- | :--- |
| `survey` | Programs only: where the paper stands and what `auto` would do next. | `00_survey.json`, `audit_reports/00_survey.md` |
| `inventory` | Scan notes and authority files (unchanged files reuse their cached scan; with no notes, the paper's own `.tex`). The program merges the scans into the ledger; one agent judges core results, duplicates, red lines, story material. | `00_ground_truth_ledger.json`, `00_redlines.md`, `00_story_material.md`, `00_conflicts_report.md`; voice samples from `exemplar_paths` |
| `brief` | Lineage and neighbouring-work agents in parallel; the workflow verifies every reference itself with `verify_citations.py`; a briefing agent writes information only (no storyline, no ranking, no advice). | `01_literature_map.md` (lineage, neighbouring clusters with where they stop, concurrent work marked as never prior), `01_briefing.md` |
| `story` | From `01_discussion.md` (or chosen by the agent in an unattended run): the story, the block plan, the decision record and macros. Existing block files are kept; existing blocks that must change are listed for rewriting. | `01_story.md`, `01_plan.json`, `01_decisions.json`, `macros.tex` |
| `draft` | One persistent author writes the main-text blocks that are missing, partial or marked for rewriting, in reading order, reading the complete ones for continuity; a last turn reads the main text through and writes (or updates) the title and abstract. SM blocks in parallel, rigour first. Writers may raise concerns. | `sections/*.tex`, `main.tex`, `supplement.tex` |
| `figures` | Builds figures with `journal_fig_theme.py` (`prl` / `nature`); `verify_figures.py` PyMuPDF/AST gate with repair rounds (`auto` makes only missing figures). | `figures/*.pdf` |
| `check` | Report only. Programs: definitions, abstract read alone (acronyms, venue length), references, copy-edit scans, citation metadata. Agents, in one parallel pool: the main text read whole by one checker and the SM in groups (scope, quantifiers, attribution, numbers, math; never style), scripted numeric checks of core results, the reader check, and the **citation-context check**: every citing sentence is joined by the program with the cited works' title, year, authors, abstract and literature-map note, and a checker judges whether the citation fits (misattribution, a review cited for a priority claim, later work cited as prior, a novelty claim a cited work contradicts). Issues that would need a change of story, structure or decision become concerns, not revise items. `check_backend` runs it on another vendor. | `03_issues.json`, `audit_reports/01_check_report.md`, concerns in `07_feedback.json` |
| `revise` | SM files in parallel, then one author fixes the main text in reading order, abstract last; smallest edit; an issue that goes against a decision is declined and becomes a concern. Also takes a referee report or free-text instructions (`repair_instructions`). Afterwards only the deterministic checks run again. | `audit_reports/06_revise_report.md` |
| `build` | Deterministic clean-up, dual compile, layout and leak checks, arXiv flattening; an LLM only fixes compile errors and never makes figures. | `submission/` |
| `blind_referee` | Context-isolated panel in a throw-away copy holding only the manuscript, bibliography, figure PNGs and audit JSONs. `referee_backend` runs it on another vendor. | `05_blind_referee_report.md` (feed it to `revise`) |

**Older names still work:** `full_auto`→`auto`, `draft_and_check`→`auto --from draft`, `check_and_revise`→`auto --from check`, `blueprint`→`story`, `verify`/`citations`→`check`, `hygiene`→`cleanup`, `repair`→`revise`, `draft_and_audit`, `audit_polish`. A v3 workspace with `01_story.md` and `01_plan.json` is locked as it is on the next `auto` run.

---

## Choosing your execution path

Let `<SKILL_DIR>` be this skill's directory (typically `~/.claude/skills/write-paper`, `~/.gemini/config/skills/write-paper`, `~/.codex/skills/write-paper`, or `<project>/.agents/skills/write-paper`).

### Path A: orchestrated CLI (Claude Code, Codex, OpenCode, terminal)
`scripts/run_paper.py` dispatches `claude -p`, `agy -p` or `codex exec` workers:
```bash
# where does the paper stand?
python <SKILL_DIR>/scripts/run_paper.py --mode survey --paper-dir ./paper
# run what it needs; repeat after each stop (briefing, confirmation, concerns)
python <SKILL_DIR>/scripts/run_paper.py --mode auto --paper-dir ./paper \
  --source-paths ./notes ./numerics --authority-paths ./SCOPE.md \
  --exemplar-paths ./a_well_written_prl.tex --genre letter --figure-style prl
# a finished draft, quick pass, checks on another vendor
python <SKILL_DIR>/scripts/run_paper.py --mode auto --from check --effort low --paper-dir ./paper \
  --source-paths ./notes --backend agy --check-backend claude
```

### Path B: native Antigravity `run_workflow`
Invoke `run_workflow` with `ScriptPath="<SKILL_DIR>/scripts/paper_workflow.py"` and `Args` matching `scripts/workflow.json`. `run_workflow` compiles the script as `'<workflow>'` and passes neither `__file__` nor `ScriptPath`, so the entry script finds the engine only through `Args.skill_dir`, the workspace, or the installed copy. **When running a checkout that is not installed, pass `Args.skill_dir = "<SKILL_DIR>"`.** The entry script refuses to start if the engine it found has a different version, and the log line `[engine] ... loaded from ...` shows which copy ran.

> [!IMPORTANT]
> **Call `run_workflow` from the Planning Turn.** If the session shows `Plan mode is on`, call `run_workflow` right there, even for modes that edit files. Do **not** call `exit_plan_mode` first: from an Execution Turn, `model="inherit"` sub-agents start without a plan and deadlock on `[plan review due]`.

The engine also guards against this: at start-up it reads the session trajectory (the latest `USER_INPUT` or completed `enter_plan_mode` versus the latest completed `exit_plan_mode`) to tell a Planning Turn from an Execution Turn (override with `Args.plan_mode = "on" | "off"`). In an Execution Turn, calls that would use `inherit` go to an independent `agy` CLI session (persistent agents resume it with `--conversation`), or to `model="pro"` when no `agy` binary is installed. `Args.check_backend` / `Args.referee_backend` send those stages to another vendor's CLI from inside Antigravity too.

**Model routing (`routing_policy`, default `auto_tiered`).** Each call site declares a task tier; an explicit `models[stage]` always wins (older stage names map to current ones), and `uniform_inherit` restores "host/CLI default everywhere":

| Tier | Call sites | Antigravity (planning turn) | agy CLI | claude CLI |
| :--- | :--- | :--- | :--- | :--- |
| `heavy` | source scans, lineage, neighbouring work, briefing, story and plan, applying answers, checkers, citation-context and reader checks, blind referees, revision planner | `inherit` | `gemini-4-carbon-high` → `gemini-pro-orchestrator` | `opus` |
| `builder` | material synthesis, the author, SM blocks, figures, scripted numeric checks, revisions, citation fixes | `inherit` | `gemini-pro-orchestrator` | CLI default |
| `fast` | compile fix, plan JSON repair | `flash` | `gemini-4-hydrogen-high` → `gemini-3.8-flash-high` | `sonnet` |
| `probe` | smoke-test pings | `flash` | `gemini-3.8-flash-high` → `gemini-3.8-flash-medium` | `haiku` |

agy model ids are checked once per run against `agy models`; if none of a tier's ids is offered, no `--model` is passed. Codex always uses its default configuration. Tiers set the model only; whether a worker may write is decided per call (`read_only`).

### Path C: host-native sub-agents plus the CLI gates
Driving the stages yourself with your host's sub-agents (`Task` in Claude Code, `swarm`/`subagent` in Codex/Antigravity): keep the same order (material → briefing → the author speaks first, then you → story the author confirms → one author drafts what is missing → report-only checks → revision; concerns back to the author), read `references/genre_templates.md` and `references/figure_visual_language.md` as notes, and run the deterministic tools below as the checks.

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

**Worker hygiene and safety (all runtimes).** Reference notes are copied into `.workflow/refs/` and handed to agents as paths (never as excerpts cut at a fixed length). Every worker prompt carries a search scope (the paper directory plus the exact source/authority/exemplar paths, never their parent folders) and must kill its background tasks before finishing (Antigravity: `manage_task` list + kill). Every agent await is bounded by `agent_timeout_s` (default 1800 s); a timed-out call is reported and never retried, because it may already have edited files. Read-only is enforced for every backend after the fact: the manuscript, `macros.tex`, `refs.bib` and the ledger, red-line, briefing, discussion, story, plan, decision and concern files are copied in memory before each read-only call (and each scripted check) and restored if changed; new `.tex` files go to `.workflow/readonly_violations/`. Edit workers are guarded too: a file changed by an agent assigned to another file is restored from the checkpoint. Headless `agy` workers that may write run with `--mode accept-edits --settings '{"toolPermission":"always-proceed"}'` (session-only); read-only `agy` workers get neither. Every `agy` answer carries a per-turn nonce, so a resumed conversation that fails before `finish` cannot pass off the previous turn's answer. With `repair_instructions`, the full text is always available to each worker as a file (inline text is saved to `.workflow/repair_instructions_<ts>.md`); the planner only routes work. Orchestrator limits: `--agent-timeout` (1800 s), `--agent-retries` (2), `--max-scan-agents` (24); `--max-parallel`, `--max-numeric-checks` and `--revise-low` default from `--effort`.
