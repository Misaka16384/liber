#!/usr/bin/env python3
"""Antigravity `run_workflow` thin entrypoint delegating to `workflow_engine.py`.

`run_workflow` compiles this file under the name '<workflow>' and passes neither `__file__` nor the
ScriptPath, so the engine next to it cannot be found from here. Resolution order: `args.skill_dir` >
this file's own location (only if the host ever provides it) > workspace skill folders > installed skill
folders. To run a checkout that is not installed, pass `Args.skill_dir`. The engine version must match
ENTRY_VERSION; otherwise the run stops instead of silently running another copy of the engine.
"""

from __future__ import annotations

import sys
from pathlib import Path

ENTRY_VERSION = "2.1.0"


def _resolve_scripts_dir() -> Path:
    candidates = []
    wf_args = dict(globals().get("args") or {})
    if wf_args.get("skill_dir"):
        sd = Path(str(wf_args["skill_dir"]))
        candidates.extend([sd / "scripts", sd])
    if "__file__" in globals():
        candidates.append(Path(__file__).resolve().parent)
    try:
        code_file = Path(sys._getframe().f_code.co_filename)
        if code_file.is_file():
            candidates.append(code_file.resolve().parent)
    except (AttributeError, ValueError, OSError):
        pass
    for ws in list(globals().get("WORKSPACES") or ()):
        candidates.append(Path(ws) / "skills" / "write-paper" / "scripts")
        candidates.append(Path(ws) / ".agents" / "skills" / "write-paper" / "scripts")
    home = Path.home()
    candidates.extend([
        home / ".gemini" / "config" / "skills" / "write-paper" / "scripts",
        home / ".claude" / "skills" / "write-paper" / "scripts",
        home / ".agents" / "skills" / "write-paper" / "scripts",
    ])
    for cand in candidates:
        if (cand / "workflow_engine.py").exists():
            return cand.resolve()
    raise RuntimeError("Could not locate `workflow_engine.py`; pass Args.skill_dir pointing at the write-paper skill folder.")


_SCRIPTS_DIR = _resolve_scripts_dir()
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))
# A previous run in the same interpreter may have imported another copy of the engine.
_cached = sys.modules.get("workflow_engine")
if _cached is not None and Path(getattr(_cached, "__file__", "") or "").resolve().parent != _SCRIPTS_DIR:
    del sys.modules["workflow_engine"]

import workflow_engine  # noqa: E402

_engine_version = getattr(workflow_engine, "ENGINE_VERSION", "<2.1 (no ENGINE_VERSION)")
if _engine_version != ENTRY_VERSION:
    raise RuntimeError(
        f"write-paper entry script {ENTRY_VERSION} found workflow_engine {_engine_version} at {_SCRIPTS_DIR}. "
        f"run_workflow does not pass ScriptPath to the script, so the engine next to it cannot be located: "
        f"pass Args.skill_dir=<the write-paper skill folder you want to run>, or reinstall the skill."
    )

await workflow_engine.run_from_antigravity(globals())
