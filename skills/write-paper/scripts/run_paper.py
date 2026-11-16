#!/usr/bin/env python3
"""Standalone multi-agent CLI entrypoint for /write-paper (v3).

Runs the full pipeline or any single phase from Claude Code, OpenAI Codex CLI, Antigravity CLI (`agy`),
or a standard terminal:

  python run_paper.py --mode full_auto --paper-dir ./paper --source-paths ./notes --genre letter
  python run_paper.py --mode draft_and_check --paper-dir ./paper --backend agy --check-backend claude
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Dict


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import workflow_engine  # noqa: E402


MODES = [
    "inventory",
    "story",
    "draft",
    "figures",
    "check",
    "revise",
    "cleanup",
    "build",
    "blind_referee",
    "draft_and_check",
    "check_and_revise",
    "full_auto",
    "smoke_test",
    # v2 names, still accepted
    "blueprint",
    "verify",
    "citations",
    "hygiene",
    "repair",
    "draft_and_audit",
    "audit_polish",
]

ALL_STAGES = ["inventory", "story", "draft", "figures", "check", "revise", "compile_fix", "blind_referee"]


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Standalone CLI runner for /write-paper v3 across Claude Code, Codex, and Antigravity.")
    ap.add_argument("--args-json", type=Path, help="Optional JSON file matching workflow.json schema; CLI flags override its values.")
    ap.add_argument("--mode", choices=MODES, help="On-demand modular phase or composite pipeline.")
    ap.add_argument("--paper-dir", type=Path, help="Target LaTeX paper project root directory.")
    ap.add_argument("--source-paths", nargs="*", help="Core derivation notes, reports, verification scripts, and data directories.")
    ap.add_argument("--authority-paths", nargs="*", help="Highest-authority scope, redline, and benchmark documents.")
    ap.add_argument("--exemplar-paths", nargs="*", help="Well-written published papers (.tex or .pdf) whose paragraphs the author reads as voice samples.")
    ap.add_argument("--existing-bib", type=str, help="Optional seed .bib file.")
    ap.add_argument("--genre", choices=["letter", "long"], help="Target format: 'letter' (PRL/Nature Physics) or 'long' (PRB/PRX/CMP).")
    ap.add_argument("--figure-style", choices=["prl", "nature"], help="Journal figure style preset (default: prl).")
    ap.add_argument("--plan-path", type=str, help="Optional explicit path to 01_plan.json (default: <paper-dir>/.workflow/01_plan.json).")
    ap.add_argument("--blueprint-path", type=str, help="v2 workspaces only: 01_blueprint.json, read for its figure list.")
    ap.add_argument("--no-pause", dest="pause_after_story", action="store_false", default=None, help="full_auto: do not stop after the story stage for the author to read 01_story.md.")
    ap.add_argument("--max-numeric-checks", type=int, default=None, help="check: how many core results get a scripted numeric check (default 12).")
    ap.add_argument("--supplement-tex", type=str, help="Optional path to Supplemental Material .tex.")
    ap.add_argument("--figure-specs-path", type=str, help="Optional JSON file describing target figures & captions.")
    ap.add_argument("--repair-instructions", type=str, help="--mode revise/repair: a referee or audit report path, or the instructions as text (default: the latest check report).")
    ap.add_argument("--repair-files", nargs="*", help="Optional target file subset for --mode revise with instructions.")
    ap.add_argument("--repair-skip-plan", action="store_true", default=None, help="--mode revise with instructions: skip the planner and hand each --repair-files file the full instructions (automatic for a single file).")
    ap.add_argument("--sync-bib", type=str, help="Optional external .bib to sync into refs.bib.")
    ap.add_argument("--max-parallel", type=int, default=None, help="Maximum concurrent agent subprocesses (default: 4, or the value in --args-json).")
    ap.add_argument("--agent-timeout", type=int, default=None, help="Seconds before one agent call is killed together with its process tree (default: 1800).")
    ap.add_argument("--agent-retries", type=int, default=None, help="Retries for a failed or timed-out agent call (default: 2).")
    ap.add_argument("--max-scan-agents", type=int, default=None, help="Upper bound on Stage-0 scan agents (default: 24); the run stops instead of fanning out further.")
    ap.add_argument("--backend", choices=["auto", "claude", "agy", "codex"], default="auto", help="Primary headless agent CLI backend (default: auto-detect).")
    ap.add_argument("--referee-backend", choices=["", "claude", "agy", "codex"], default="", help="Optional separate CLI backend for cross-vendor blind_referee.")
    ap.add_argument("--check-backend", choices=["", "claude", "agy", "codex"], default="", help="Optional separate CLI backend for the check stage (e.g. agy writes, claude checks).")
    ap.add_argument("--routing-policy", choices=["auto_tiered", "uniform_inherit"], default=None, help="auto_tiered (default): route each call by task tier (heavy/builder/fast/probe); uniform_inherit: CLI default model everywhere. --model/--models-json always win.")
    ap.add_argument("--model", type=str, default="", help="Optional uniform model override across all stages (e.g. opus, sonnet); disables tiered routing.")
    ap.add_argument("--models-json", type=str, default="", help="Optional JSON string mapping per-stage models.")
    ap.add_argument("--skip-agent-pings", action="store_true", help="Used with --mode smoke_test for offline tool/runtime verification without CLI LLM calls.")
    return ap.parse_args()


def main() -> int:
    ns = parse_args()
    cfg: Dict[str, Any] = {}
    if ns.args_json:
        cfg.update(json.loads(ns.args_json.read_text(encoding="utf-8")))

    if ns.mode:
        cfg["mode"] = ns.mode
    elif "mode" not in cfg:
        cfg["mode"] = "smoke_test"

    if ns.paper_dir:
        cfg["paper_dir"] = str(ns.paper_dir)
    if "paper_dir" not in cfg or not cfg["paper_dir"]:
        print("Error: --paper-dir (or paper_dir inside --args-json) is required.", file=sys.stderr)
        return 2

    for attr, key in (
        ("source_paths", "source_paths"),
        ("authority_paths", "authority_paths"),
        ("exemplar_paths", "exemplar_paths"),
        ("existing_bib", "existing_bib"),
        ("genre", "genre"),
        ("figure_style", "figure_style"),
        ("plan_path", "plan_path"),
        ("blueprint_path", "blueprint_path"),
        ("pause_after_story", "pause_after_story"),
        ("max_numeric_checks", "max_numeric_checks"),
        ("supplement_tex", "supplement_tex"),
        ("figure_specs_path", "figure_specs_path"),
        ("repair_instructions", "repair_instructions"),
        ("repair_files", "repair_files"),
        ("repair_skip_plan", "repair_skip_plan"),
        ("sync_bib", "sync_bib"),
        ("max_parallel", "max_parallel"),
        ("agent_timeout", "agent_timeout_s"),
        ("agent_retries", "agent_retries"),
        ("max_scan_agents", "max_scan_agents"),
        ("routing_policy", "routing_policy"),
    ):
        val = getattr(ns, attr)
        if val is not None:
            cfg[key] = val

    models_cfg = dict(cfg.get("models") or {})
    if ns.model:
        for st in ALL_STAGES:
            models_cfg[st] = ns.model
    if ns.models_json:
        models_cfg.update(json.loads(ns.models_json))
    if models_cfg:
        cfg["models"] = models_cfg

    result = asyncio.run(
        workflow_engine.run_from_cli(
            cfg,
            backend=ns.backend,
            referee_backend=ns.referee_backend,
            skip_agent_pings=ns.skip_agent_pings,
            check_backend=ns.check_backend,
        )
    )
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
