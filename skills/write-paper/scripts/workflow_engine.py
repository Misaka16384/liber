#!/usr/bin/env python3
"""Universal, dual-mode modular orchestrator for /write-paper (v4: the author decides the story, AI writes and checks).

Runs inside:
1. Antigravity `run_workflow` (via `paper_workflow.py` -> `run_from_antigravity(globals())`)
2. Standalone Python CLI across Claude Code (`claude`), Antigravity CLI (`agy`),
   or OpenAI Codex CLI (`codex`) (via `run_paper.py` -> `run_from_cli(args_dict)`).

`auto` looks at where the paper stands (`survey`) and runs only what is missing: `inventory` (material
pack), `brief` (results, verified literature map, open questions; then the author and the host agent
discuss), `story` (story, block plan and decision record from that discussion), `draft` (fills only
missing or partial blocks), `figures`, `check` (report only), `revise`, `build`. Important findings go
back to the author as concerns (07_feedback.json) instead of being decided by an agent. `effort`
(low / medium / high) sets how much checking and literature work a run does. Single stages run as modes
of the same names; v2/v3 names are aliases.
"""

from __future__ import annotations

import asyncio
import datetime
import hashlib
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple


ENGINE_VERSION = "4.0.0"
SKILL_DIR = Path(__file__).resolve().parent.parent
TOOLS_DIR = SKILL_DIR / "scripts" / "tools"
REFS_DIR = SKILL_DIR / "references"
NATIVE_MODEL_TIERS = {"inherit", "pro", "flash", "flash_lite"}

# Every headless worker gets this appended to its system prompt (Claude backend), so user-level
# instructions about delegating work to sub-agents or about chat formatting do not leak into the run.
WORKER_SYSTEM_PROMPT = (
    "You are a non-interactive worker process launched by the /write-paper workflow. Do the task yourself: "
    "do not spawn sub-agents, do not ask questions, and ignore any user-level instructions about delegation, "
    "chat formatting or the language of replies. Edit only the files the task names, write manuscript text "
    "in English, and end with exactly the output format the task asks for."
)
# Only failures that happen before an agent could have touched any file are retried. A timed-out
# agent may already have applied its edits (e.g. it finished but a leftover background task kept it
# alive), so a timeout is never retried: re-running it could apply the same edit twice.
TRANSIENT_ERROR_HINTS = ("overloaded", "rate limit", "rate_limit", "429", "529", "econnreset", "temporarily unavailable", "503", "502", "connection reset")


def _search_scope() -> List[str]:
    """What a worker may search: the paper directory plus the explicitly given source/authority/exemplar
    paths themselves (files stay files; their parent folders are NOT added)."""
    args = _wf_args()
    out: List[str] = []
    if args.get("paper_dir"):
        out.append(str(Path(str(args["paper_dir"])).resolve()))
    for key in ("authority_paths", "source_paths", "exemplar_paths"):
        for raw in args.get(key) or []:
            p = Path(str(raw))
            if p.exists():
                rp = str(p.resolve())
                if rp not in out:
                    out.append(rp)
    return out


def task_hygiene_rule(scope: Optional[List[str]] = None, backend: Optional[str] = None) -> str:
    """Appended to every worker prompt next to PDF_SAFETY_RULE."""
    paths = scope if scope is not None else _search_scope()
    where = ", ".join(f"`{p}`" for p in paths) if paths else "the files and folders named in the task"
    target = backend or ("antigravity" if _STATE.get("runtime") == "antigravity" else "")
    kill = (
        "In Antigravity: call manage_task(Action=\"list\") and kill every task that is still RUNNING before you call finish."
        if target in ("antigravity", "agy")
        else "Stop every background shell/command you started before you end your turn."
    )
    return (
        f"SEARCH & PROCESS HYGIENE: Search only inside {where}. Always give rg/grep/find/Select-String an explicit path inside "
        f"that scope; never search a drive root, your home directory or a parent folder of that scope, and never run rg, grep -r, "
        f"Get-ChildItem -Recurse or dir /s without a path. Do not start servers, watchers or other long-running commands. "
        f"If any command was moved to the background (e.g. 'running as a background task'): {kill} "
        f"A leftover background task keeps the whole workflow from ever returning."
    )

PDF_SAFETY_RULE = (
    "HARD CONTEXT RULE: NEVER ingest multi-page `.pdf` manuscripts directly into context "
    "(do not call read_file/viewer on `main.pdf` or `supplement.pdf`). "
    "Read `.tex` / `.bbl` source files for text, or inspect rendered `.png` images and `.json` telemetry only."
)

# Runtime state configured either by run_from_antigravity() or run_from_cli()
_STATE: Dict[str, Any] = {
    "runtime": "cli",            # "antigravity" | "cli"
    "backend": "auto",           # "auto" | "claude" | "agy" | "codex"
    "referee_backend": "",       # optional cross-vendor backend for blind_referee
    "check_backend": "",         # optional cross-vendor backend for the check stage
    "wf_args": {},
    "models_cfg": {},
    "batch_size": 4,
    "inherit_deadlock": False,
    "resolved_python": None,
    "ag_agent": None,
    "ag_parallel": None,
    "ag_phase": None,
    "ag_log": None,
    "skip_agent_pings": False,
    "agent_timeout": 1800,
    "agent_retries": 2,
    "max_scan_agents": 24,
    "routing_policy": "auto_tiered",
    "in_plan_mode": None,       # Antigravity only: True = called from a Planning Turn, False = Execution Turn, None = unknown
}


def _wf_args() -> Dict[str, Any]:
    return _STATE["wf_args"]


def log(msg: str) -> None:
    if _STATE["runtime"] == "antigravity" and callable(_STATE["ag_log"]):
        _STATE["ag_log"](msg)
    else:
        print(f"[log] {msg}", flush=True)


def phase(title: str) -> None:
    if _STATE["runtime"] == "antigravity" and callable(_STATE["ag_phase"]):
        _STATE["ag_phase"](title)
    else:
        print(f"\n=== PHASE: {title} ===", flush=True)


def resolve_python_exe() -> str:
    """Locate a Python interpreter containing pymupdf + matplotlib + sympy cleanly across OSes."""
    if _STATE["resolved_python"] is not None:
        return str(_STATE["resolved_python"])

    candidates: List[str] = []
    env_py = os.environ.get("WRITE_PAPER_PYTHON", "").strip()
    if env_py:
        candidates.append(env_py)
    if sys.executable:
        candidates.append(sys.executable)
    for name in ("python3", "python"):
        found = shutil.which(name)
        if found and found not in candidates:
            candidates.append(found)

    home = Path.home()
    if sys.platform.startswith("win"):
        for p in sorted((home / "AppData" / "Local" / "Programs" / "Python").glob("Python3*/python.exe"), reverse=True):
            candidates.append(str(p))
        for conda_dir in ("miniforge3", "mambaforge", "miniconda3", "anaconda3"):
            cp = home / conda_dir / "python.exe"
            if cp.exists():
                candidates.append(str(cp))
    else:
        for p_str in ("/opt/homebrew/bin/python3", "/usr/local/bin/python3", "/usr/bin/python3"):
            if Path(p_str).exists():
                candidates.append(p_str)
        for conda_dir in ("miniforge3", "mambaforge", "miniconda3", "anaconda3"):
            cp = home / conda_dir / "bin" / "python"
            if cp.exists():
                candidates.append(str(cp))

    dedup: List[str] = []
    for c in candidates:
        if c and c not in dedup and Path(c).exists():
            dedup.append(c)

    for probe in ("import pymupdf, matplotlib, sympy", "import fitz, matplotlib, sympy", "import pymupdf", "import fitz"):
        for cand in dedup:
            try:
                r = subprocess.run([cand, "-c", probe], capture_output=True, text=True, timeout=10)
                if r.returncode == 0:
                    _STATE["resolved_python"] = cand
                    return cand
            except Exception:
                continue

    _STATE["resolved_python"] = dedup[0] if dedup else sys.executable
    return str(_STATE["resolved_python"])


def _find_cli_binary(backend: str) -> Optional[str]:
    names = {
        "claude": ["claude", str(Path.home() / ".local" / "bin" / ("claude.exe" if sys.platform.startswith("win") else "claude"))],
        "agy": ["agy", str(Path.home() / "AppData" / "Local" / "agy" / "bin" / "agy.EXE")],
        "codex": ["codex"],
    }.get(backend, [backend])
    for cand in names:
        resolved = shutil.which(cand) if os.sep not in cand and "/" not in cand else (cand if Path(cand).exists() else None)
        if resolved:
            return resolved
    return None


def stage_backend_override(stage_name: str) -> str:
    """A different vendor for checking or refereeing (e.g. Gemini writes, Claude checks); "" = no override."""
    key = {"blind_referee": "referee_backend", "check": "check_backend"}.get(stage_name)
    return str(_STATE.get(key) or "").lower() if key else ""


def resolve_cli_backend(stage_name: str = "") -> Tuple[str, str]:
    """Select active CLI backend and binary path ('claude', 'agy', or 'codex')."""
    req = stage_backend_override(stage_name) or str(_STATE.get("backend") or "auto").lower()

    if req != "auto":
        bin_p = _find_cli_binary(req)
        if not bin_p:
            raise RuntimeError(f"Requested backend '{req}' CLI binary was not found on PATH.")
        return req, bin_p

    for cand in ("claude", "agy", "codex"):
        bin_p = _find_cli_binary(cand)
        if bin_p:
            return cand, bin_p
    raise RuntimeError("No supported agent CLI found on PATH (tried: claude, agy, codex).")


def _allowed_dirs() -> List[str]:
    dirs: List[str] = []
    args = _wf_args()
    if args.get("paper_dir"):
        dirs.append(str(Path(str(args["paper_dir"])).resolve()))
    for key in ("source_paths", "authority_paths", "exemplar_paths"):
        for raw in (args.get(key) or []):
            p = Path(str(raw))
            d = p if p.is_dir() else p.parent
            if d.exists():
                dirs.append(str(d.resolve()))
    seen: List[str] = []
    for d in dirs:
        if d not in seen:
            seen.append(d)
    return seen


def _clean_subprocess_env(backend: str) -> Dict[str, str]:
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    if backend == "claude":
        for k in list(env):
            if k.startswith("CLAUDE") or k == "ANTHROPIC_BASE_URL":
                env.pop(k, None)
    return env


def _parse_json_result(raw_text: str, schema: Dict[str, Any]) -> Any:
    if schema.get("type") == "string":
        return str(raw_text)
    txt = str(raw_text).strip()
    try:
        return json.loads(txt)
    except Exception:
        pass
    m = re.search(r"```(?:json)?\s*([\s\S]*?)```", txt)
    if m:
        try:
            return json.loads(m.group(1).strip())
        except Exception:
            pass
    m2 = re.search(r"(\{[\s\S]*\}|\[[\s\S]*\])", txt)
    if m2:
        try:
            return json.loads(m2.group(1))
        except Exception:
            pass
    return txt


def _map_model_for_cli(backend: str, raw_model: str) -> str:
    m = (raw_model or "").strip()
    if not m or m == "inherit":
        return ""
    if backend == "claude":
        return {"pro": "opus", "flash": "sonnet", "flash_lite": "haiku"}.get(m, m)
    if backend in ("agy", "codex") and m in NATIVE_MODEL_TIERS:
        return ""
    return m


TURN_FIELD = "_turn"


def with_turn_nonce(schema: Dict[str, Any], prompt: str, nonce: str) -> Tuple[Dict[str, Any], str]:
    """Ask the agent to stamp its final answer with this turn's nonce (object answers: a required `_turn`
    field; free-text answers: a last line `TURN <nonce>`)."""
    if schema and schema.get("type") == "object":
        props = dict(schema.get("properties") or {})
        props[TURN_FIELD] = {"type": "string"}
        new_schema = {**schema, "properties": props, "required": list(schema.get("required") or []) + [TURN_FIELD]}
        return new_schema, f"{prompt}\n\nIn your final JSON answer set the field `{TURN_FIELD}` to exactly \"{nonce}\"."
    if schema and schema.get("type") == "array":
        return schema, prompt
    return schema, f"{prompt}\n\nEnd your final answer with a separate last line: TURN {nonce}"


def _strip_turn(value: Any, nonce: Optional[str]) -> Any:
    if isinstance(value, dict):
        return {k: v for k, v in value.items() if k != TURN_FIELD}
    if isinstance(value, str) and nonce:
        return re.sub(rf"\n?[ \t]*TURN {re.escape(nonce)}[ \t]*$", "", value.rstrip()).rstrip()
    return value


def _has_turn(value: Any, nonce: str) -> bool:
    if isinstance(value, dict):
        return value.get(TURN_FIELD) == nonce
    if isinstance(value, str):
        return f"TURN {nonce}" in value
    return False


def parse_cli_json(backend: str, data: Dict[str, Any], schema: Dict[str, Any], err_s: str = "", nonce: Optional[str] = None) -> Tuple[Any, Optional[str]]:
    """Turn a claude/agy `--output-format json` payload into (result, session_id).

    Both CLIs put the schema-validated object in a top-level `structured_output` field when
    `--json-schema` is given; the free-text answer is only a fallback. `nonce` (agy) is this turn's stamp:
    a non-SUCCESS payload is only accepted if its answer carries it.
    """
    wants_object = bool(schema) and schema.get("type") != "string"
    if backend == "claude":
        if data.get("is_error"):
            raise RuntimeError(f"claude CLI error: {data.get('result') or err_s}")
        sid = data.get("session_id")
        if wants_object and data.get("structured_output") is not None:
            return data["structured_output"], sid
        return _parse_json_result(str(data.get("result") or ""), schema), sid
    if backend == "agy":
        cid = data.get("conversation_id")
        if data.get("status") == "SUCCESS":
            if wants_object and data.get("structured_output") is not None:
                return _strip_turn(data["structured_output"], nonce), cid
            return _strip_turn(_parse_json_result(str(data.get("response") or ""), schema), nonce), cid
        # agy reports status=ERROR for the whole run if any intermediate step failed, e.g. a stream that was
        # interrupted and auto-resumed, even when the agent went on to finish with a complete answer. In a
        # resumed conversation it also reports ERROR (and re-emits the PREVIOUS turn's answer) when this turn
        # failed before finishing: the nonce tells the two apart.
        recovered = _recover_agy_result(data, schema)
        if recovered is not _NO_RESULT and nonce is not None and not _has_turn(recovered, nonce):
            log(f"[agy] status={data.get('status')} and the returned answer lacks this turn's stamp (stale answer from an earlier turn?); rejecting it")
            recovered = _NO_RESULT
        if recovered is not _NO_RESULT:
            recovered = _strip_turn(recovered, nonce)
            log(f"[agy] status={data.get('status')} but the run returned a complete result; accepting it "
                f"(finished_reason={data.get('finished_reason')!r}, error={str(data.get('error') or '')[:120]!r})")
            return recovered, cid
        detail = data.get("error") or data.get("response") or err_s
        raise RuntimeError(f"agy CLI error ({data.get('status')}, finished_reason={data.get('finished_reason')!r}): {str(detail)[:1500]}")
    raise RuntimeError(f"no JSON parser for backend {backend}")


_NO_RESULT = object()


def _matches_schema_shape(value: Any, schema: Dict[str, Any]) -> bool:
    t = schema.get("type")
    if t == "object":
        return isinstance(value, dict) and all(k in value for k in (schema.get("required") or []))
    if t == "array":
        return isinstance(value, list)
    if t == "string":
        return isinstance(value, str) and bool(value.strip())
    return value is not None


def _recover_agy_result(data: Dict[str, Any], schema: Dict[str, Any]) -> Any:
    """A non-SUCCESS agy payload is still usable when it carries a complete final answer.

    Object/array schemas: the schema-shaped answer (structured_output, or a response that parses and has every
    required key) proves the agent finished. Free-text answers cannot prove that, so they are accepted only when
    agy reports no error at all. A print timeout is never accepted.
    """
    if data.get("finished_reason") == "print_timeout" or str(data.get("status") or "").upper() in ("CANCELLED", "CANCELED", "TIMEOUT"):
        return _NO_RESULT
    if schema and schema.get("type") in ("object", "array"):
        so = data.get("structured_output")
        if so is not None and _matches_schema_shape(so, schema):
            return so
        parsed = _parse_json_result(str(data.get("response") or ""), schema)
        return parsed if _matches_schema_shape(parsed, schema) else _NO_RESULT
    resp = str(data.get("response") or "")
    if resp.strip() and not data.get("error"):
        return resp
    return _NO_RESULT


def _kill_process_tree(proc: "asyncio.subprocess.Process") -> None:
    """Kill an agent CLI and everything it spawned (tool shells, python checks, ...)."""
    if proc.returncode is not None:
        return
    try:
        if sys.platform.startswith("win"):
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, timeout=30)
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


async def _run_cli_turn(
    backend: str,
    bin_path: str,
    prompt: str,
    schema: Dict[str, Any],
    model: str = "",
    session_id: Optional[str] = None,
    read_only: bool = False,
    add_dirs_override: Optional[List[str]] = None,
    cwd: Optional[str] = None,
    persist: bool = True,
    allow_bash_read_only: bool = False,
    extra_args: Sequence[str] = (),
) -> Tuple[Any, Optional[str]]:
    """Execute one headless turn via claude, agy, or codex CLI asynchronously (bounded by agent_timeout)."""
    add_dirs = add_dirs_override if add_dirs_override is not None else _allowed_dirs()
    eff_model = _map_model_for_cli(backend, model)
    timeout_s = float(_STATE.get("agent_timeout") or 1800)
    turn_nonce: Optional[str] = None
    env = _clean_subprocess_env(backend)
    tmp_files: List[Path] = []
    stdin_bytes: Optional[bytes] = None

    try:
        if backend == "claude":
            cmd: List[str] = [bin_path, "-p", "--output-format", "json"]
            if schema and schema.get("type") != "string":
                cmd += ["--json-schema", json.dumps(schema)]
            if eff_model:
                cmd += ["--model", eff_model]
            if session_id:
                cmd += ["--resume", session_id]
            elif not persist:
                cmd += ["--no-session-persistence"]
            # Isolation from the operator's own Claude setup: no auto-memory, no user-level plugins/hooks/settings,
            # no MCP servers, no sub-agents. (User-level CLAUDE.md cannot be disabled without --bare, which needs an
            # API key, so the worker system prompt overrides its delegation rules instead.)
            cmd += ["--settings", json.dumps({"autoMemoryEnabled": False})]
            cmd += ["--setting-sources", os.environ.get("WRITE_PAPER_CLAUDE_SETTING_SOURCES", "project,local")]
            cmd += ["--strict-mcp-config", "--append-system-prompt", WORKER_SYSTEM_PROMPT]
            for ad in add_dirs:
                cmd += ["--add-dir", ad]
            if read_only:
                cmd += ["--disallowedTools", "Edit,Write,NotebookEdit,Agent,Task"]
                if allow_bash_read_only:
                    cmd += ["--allowedTools", "Bash,Read,Glob,Grep"]
            else:
                cmd += ["--permission-mode", "acceptEdits", "--allowedTools", "Bash,Read,Edit,Write,Glob,Grep", "--disallowedTools", "Agent,Task"]
            cmd += list(extra_args)
            stdin_bytes = prompt.encode("utf-8")

        elif backend == "agy":
            # A per-turn nonce proves that an answer belongs to THIS turn: when a resumed agy conversation fails
            # before `finish`, agy re-emits the previous turn's final answer with status=ERROR.
            turn_nonce = uuid.uuid4().hex[:12]
            schema, prompt = with_turn_nonce(schema, prompt, turn_nonce)
            # `--mode auto` (sandbox + reviewer agent) needs a personal account and a terminal sandbox, otherwise
            # it warns and falls back. `accept-edits` only auto-approves file edits; terminal commands (figure
            # scripts, SymPy checks) would still wait for a confirmation nobody can give in print mode unless the
            # user happens to run with toolPermission=always-proceed. `--settings` applies to this session only
            # and never touches the user's settings.json. Read-only workers get neither, except a referee that
            # must run scripts inside its throw-away sandbox.
            cmd = [bin_path, "--output-format", "json", "--print-timeout", f"{int(timeout_s)}s"]
            if not read_only:
                cmd += ["--mode", "accept-edits"]
            if not read_only or allow_bash_read_only:
                cmd += ["--settings", json.dumps({"toolPermission": "always-proceed"})]
            if schema and schema.get("type") != "string":
                cmd += ["--json-schema", json.dumps(schema)]
            if eff_model:
                cmd += ["--model", eff_model]
            if session_id:
                cmd += ["--conversation", session_id]
            for ad in add_dirs:
                cmd += ["--add-dir", ad]
            cmd += list(extra_args)
            # the prompt (-p) must stay the last argument
            if len(prompt) > 14000:
                tf = Path(tempfile.mkstemp(prefix="wp_prompt_", suffix=".txt")[1])
                tf.write_text(prompt, encoding="utf-8")
                tmp_files.append(tf)
                cmd += ["-p", f"Read the full task prompt in `{tf}` and execute all instructions faithfully."]
            else:
                cmd += ["-p", prompt]

        elif backend == "codex":
            tf_out = Path(tempfile.mkstemp(prefix="wp_codex_out_", suffix=".json")[1])
            tmp_files.append(tf_out)
            sandbox_mode = "read-only" if read_only else "workspace-write"
            cmd = [bin_path, "exec", "-", "--sandbox", sandbox_mode, "-o", str(tf_out)]
            if schema and schema.get("type") != "string":
                tf_sc = Path(tempfile.mkstemp(prefix="wp_codex_schema_", suffix=".json")[1])
                tf_sc.write_text(json.dumps(schema), encoding="utf-8")
                tmp_files.append(tf_sc)
                cmd += ["--output-schema", str(tf_sc)]
            if eff_model:
                cmd += ["-m", eff_model]
            for ad in add_dirs:
                cmd += ["--add-dir", ad]
            cmd += list(extra_args)
            stdin_bytes = prompt.encode("utf-8")
            codex_out_path: Optional[Path] = tf_out

        else:
            raise RuntimeError(f"Unsupported standalone backend: {backend}")

        popen_kw: Dict[str, Any] = {}
        if not sys.platform.startswith("win"):
            popen_kw["start_new_session"] = True  # own process group, so the whole tree can be killed
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE if stdin_bytes is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            cwd=cwd,
            **popen_kw,
        )
        try:
            out_b, err_b = await asyncio.wait_for(proc.communicate(input=stdin_bytes), timeout=timeout_s)
        except asyncio.TimeoutError:
            _kill_process_tree(proc)
            raise RuntimeError(f"{backend} agent timed out after {int(timeout_s)}s")
        except asyncio.CancelledError:
            _kill_process_tree(proc)  # a sibling failed or the run was interrupted: do not leave paid workers running
            raise
        out_s = out_b.decode("utf-8", errors="replace").strip()
        err_s = err_b.decode("utf-8", errors="replace").strip()

        if backend == "codex":
            raw_ans = codex_out_path.read_text(encoding="utf-8", errors="replace").strip() if (codex_out_path and codex_out_path.exists()) else out_s
            if proc.returncode != 0 and not raw_ans:
                raise RuntimeError(f"codex exec failed ({proc.returncode}): {err_s or out_s}")
            return _parse_json_result(raw_ans, schema), None

        if proc.returncode != 0 and not out_s:
            raise RuntimeError(f"{backend} CLI failed ({proc.returncode}): {err_s[-2000:]}")

        try:
            data = json.loads(out_s)
        except json.JSONDecodeError:
            raise RuntimeError(f"{backend} CLI returned non-JSON output: {out_s[:500]}")
        return parse_cli_json(backend, data, schema, err_s, nonce=turn_nonce)
    finally:
        for tf in tmp_files:
            try:
                if tf.exists():
                    tf.unlink()
            except Exception:
                pass


def get_model(stage_name: str) -> str:
    val = (_STATE["models_cfg"].get(stage_name) or "inherit").strip()
    return val or "inherit"


def resolve_native_model(stage_name: str) -> str:
    m = get_model(stage_name)
    if m == "inherit" and _STATE["inherit_deadlock"]:
        return "pro"
    return m


# ==============================================================================
# Model routing: task tiers x runtime/backend
# ==============================================================================
#
# Every agent call site declares how much reasoning it needs (independent of whether it may write files,
# which is the separate `read_only` flag):
#   heavy   - deep reading / judging / planning (source scans, lineage, story and plan, checkers, the
#             reader check, blind referees, revision planner)
#   builder - multi-step authoring that writes files (material synthesis, the author, SM blocks, figures,
#             scripted numeric checks, revisions, citation fixes)
#   fast    - short mechanical fixes (compile fix, JSON repair)
#   probe   - connectivity pings
#
# Precedence: an explicit per-stage model in `models` > routing_policy. Under `uniform_inherit` every call
# uses the host/CLI default, as before tiering existed. `check_backend` / `referee_backend` send those stages
# to another vendor's CLI in both runtimes.
TASK_TIERS = ("heavy", "builder", "fast", "probe")
ROUTING_POLICIES = ("auto_tiered", "uniform_inherit")

TIER_MODELS: Dict[str, Dict[str, Any]] = {
    # Antigravity run_workflow `agent(...)` tiers. `inherit` keeps the parent's Carbon->Hydrogen pipeline,
    # which only works when run_workflow is called from a Planning Turn (see routing below).
    "antigravity": {"heavy": "inherit", "builder": "inherit", "fast": "flash", "probe": "flash"},
    # Standalone agy CLI: preference lists, checked against `agy models` (first available wins; none -> CLI default).
    "agy": {
        "heavy": ["gemini-4-carbon-high", "gemini-pro-orchestrator"],
        "builder": ["gemini-pro-orchestrator"],
        "fast": ["gemini-4-hydrogen-high", "gemini-3.8-flash-high"],
        "probe": ["gemini-3.8-flash-high", "gemini-3.8-flash-medium"],
    },
    # Claude Code aliases are stable across releases; "" = the user's CLI default model.
    "claude": {"heavy": "opus", "builder": "", "fast": "sonnet", "probe": "haiku"},
    # Codex: no tiering, always the user's default configuration.
    "codex": {"heavy": "", "builder": "", "fast": "", "probe": ""},
}


@dataclass(frozen=True)
class Route:
    kind: str  # "native" = Antigravity agent(...), "cli" = headless CLI subprocess
    backend: str  # "antigravity" | "claude" | "agy" | "codex"
    model: str  # native tier, CLI model id, or "" for the CLI default
    reason: str = ""


def _pick_available(prefs: Sequence[str], available: Optional[Sequence[str]]) -> str:
    """First preferred model the CLI actually offers; "" (CLI default) when none is, or the list is unknown."""
    if not available:
        return ""
    for p in prefs:
        if p in available:
            return p
    return ""


def decide_route(
    *,
    runtime: str,
    cli_backend: str,
    tier: str,
    explicit_model: str,
    policy: str,
    in_plan_mode: Optional[bool],
    agy_available: bool,
    agy_models: Optional[Sequence[str]],
) -> Route:
    """Pure routing decision (unit-tested). `explicit_model` is the user's per-stage setting ("" if none)."""
    tier = tier if tier in TASK_TIERS else "builder"
    policy = policy if policy in ROUTING_POLICIES else "auto_tiered"
    explicit = (explicit_model or "").strip()
    if explicit == "inherit" and policy == "auto_tiered":
        explicit = ""  # "inherit" is the documented default value, not a deliberate per-stage choice

    if runtime == "antigravity":
        if explicit:
            if explicit in NATIVE_MODEL_TIERS:
                return Route("native", "antigravity", explicit, "explicit stage model")
            return Route("cli", "agy", explicit, "explicit non-native model -> agy CLI")
        native = "inherit" if policy == "uniform_inherit" else TIER_MODELS["antigravity"][tier]
        if native == "inherit" and in_plan_mode is False:
            # Called from an Execution Turn: an `inherit` sub-agent starts with plan mode off and no plan, and
            # deadlocks on "[plan review due]". Use an independent top-level agy CLI session instead.
            if agy_available:
                model = "" if policy == "uniform_inherit" else _pick_available(TIER_MODELS["agy"][tier], agy_models)
                return Route("cli", "agy", model, "execution turn: inherit would deadlock -> standalone agy CLI")
            return Route("native", "antigravity", "pro", "execution turn and no agy CLI -> pro")
        turn = {True: " (planning turn)", False: " (execution turn)", None: " (plan mode unknown)"}[in_plan_mode]
        return Route("native", "antigravity", native, f"{policy}/{tier}{turn}")

    backend = cli_backend
    if explicit:
        return Route("cli", backend, explicit, "explicit stage model")
    if policy == "uniform_inherit":
        return Route("cli", backend, "", "uniform_inherit: CLI default")
    spec = TIER_MODELS.get(backend, {}).get(tier, "")
    if isinstance(spec, (list, tuple)):
        return Route("cli", backend, _pick_available(spec, agy_models if backend == "agy" else None), f"auto_tiered/{tier}")
    return Route("cli", backend, str(spec), f"auto_tiered/{tier}")


def _agy_models() -> Optional[List[str]]:
    """`agy models`, run once per process and cached; None if it cannot be listed (then no --model is passed)."""
    if "agy_models" in _STATE and _STATE["agy_models"] is not _UNSET:
        return _STATE["agy_models"]
    models: Optional[List[str]] = None
    bin_p = _find_cli_binary("agy")
    if bin_p:
        try:
            r = subprocess.run([bin_p, "models"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60, stdin=subprocess.DEVNULL)
            ids = [ln.split()[0] for ln in r.stdout.splitlines() if ln.strip() and re.match(r"^[a-z0-9][\w.\-]*\s", ln.strip() + " ") and not ln.lower().startswith("fetching")]
            models = ids or None
        except Exception as exc:
            log(f"[routing] `agy models` failed ({exc}); agy calls will use the CLI default model")
    _STATE["agy_models"] = models
    return models


_UNSET = object()


_LEGACY_STAGE_NAMES = {"story": ("narrative",), "check": ("cross_verify", "citation_check"), "revise": ("repair",)}


def explicit_stage_model(stage_name: str) -> str:
    """`models[stage]`, also under its v2 name (narrative, cross_verify, citation_check, repair)."""
    cfg = _STATE.get("models_cfg") or {}
    for key in (stage_name, *_LEGACY_STAGE_NAMES.get(stage_name, ())):
        val = str(cfg.get(key) or "").strip()
        if val:
            return val
    return ""


def route_call(stage_name: str, tier: str) -> Route:
    runtime = _STATE["runtime"]
    explicit = explicit_stage_model(stage_name)
    policy = str(_STATE.get("routing_policy") or "auto_tiered")
    in_plan = _STATE.get("in_plan_mode")
    override = stage_backend_override(stage_name)
    if override and runtime == "antigravity":
        # Cross-vendor stage from inside Antigravity: a standalone CLI of the other vendor.
        model = "" if explicit in NATIVE_MODEL_TIERS else explicit
        route = decide_route(runtime="cli", cli_backend=override, tier=tier, explicit_model=model, policy=policy, in_plan_mode=None,
                             agy_available=False, agy_models=_agy_models() if override == "agy" else None)
        return Route(route.kind, route.backend, route.model, f"{stage_name} on {override} (cross-vendor); {route.reason}")
    cli_backend = ""
    agy_available = False
    if runtime == "antigravity":
        agy_available = _find_cli_binary("agy") is not None
    else:
        cli_backend = resolve_cli_backend(stage_name)[0]
    needs_agy_list = (cli_backend == "agy") or (runtime == "antigravity" and in_plan is False and agy_available)
    route = decide_route(
        runtime=runtime, cli_backend=cli_backend, tier=tier, explicit_model=explicit, policy=policy,
        in_plan_mode=in_plan, agy_available=agy_available, agy_models=_agy_models() if needs_agy_list else None,
    )
    # A native `inherit` that already deadlocked once in this run is not tried again.
    if route.kind == "native" and route.model == "inherit" and _STATE.get("inherit_deadlock"):
        route = Route("native", "antigravity", "pro", "inherit deadlocked earlier in this run -> pro")
    return route


async def _bounded(awaitable: Any, label: str) -> Any:
    """Per-call wall-clock limit for every agent await (Antigravity `agent(...)` handles and CLI turns alike)."""
    timeout_s = float(_STATE.get("agent_timeout") or 1800)
    try:
        return await asyncio.wait_for(awaitable, timeout=timeout_s)
    except asyncio.TimeoutError:
        raise RuntimeError(
            f"{label}: agent timed out after {int(timeout_s)}s (in Antigravity this is typically a leftover background "
            f"task keeping the sub-agent alive after finish; check its task list)"
        )


async def _with_retries(label: str, factory: Callable[[], Any]) -> Any:
    """Await factory() with bounded retries on failures that happen before any work (rate limit, overload,
    the CLI not starting). Timeouts and other errors are raised at once: the agent may have edited files."""
    retries = max(0, int(_STATE.get("agent_retries", 2)))
    for attempt in range(retries + 1):
        try:
            return await factory()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            msg = str(exc).lower()
            transient = (isinstance(exc, OSError) or any(h in msg for h in TRANSIENT_ERROR_HINTS)) and "timed out" not in msg
            if attempt >= retries or not transient:
                raise
            delay = 15.0 * (2 ** attempt)
            log(f"[retry] {label}: {str(exc)[:200]} -> retry {attempt + 1}/{retries} in {int(delay)}s")
            await asyncio.sleep(delay)
    raise RuntimeError(f"{label}: retries exhausted")  # unreachable


def _require_fields(res: Any, required: Sequence[str], label: str) -> Dict[str, Any]:
    """Structured agent results are model output: check their shape before indexing into them."""
    if not isinstance(res, dict):
        raise RuntimeError(f"{label}: expected a JSON object, got {type(res).__name__}: {str(res)[:300]}")
    missing = [k for k in required if k not in res]
    if missing:
        raise RuntimeError(f"{label}: result is missing fields {missing}: {json.dumps(res, ensure_ascii=False)[:300]}")
    return res


async def dispatch_agent(
    prompt: str,
    schema: Dict[str, Any],
    role: str,
    stage_name: str,
    workspace: str = "inherit",
    read_only: bool = False,
    add_dirs: Optional[List[str]] = None,
    cwd: Optional[str] = None,
    allow_bash_read_only: bool = False,
    tier: str = "builder",
) -> Any:
    """Dispatch a single agent call (with retries), routed by `tier` (see decide_route), through Antigravity
    primitives or a standalone CLI backend."""
    snap = _readonly_snapshot() if read_only else None
    try:
        return await _with_retries(role, lambda: _dispatch_agent_once(prompt, schema, role, stage_name, workspace, read_only, add_dirs, cwd, allow_bash_read_only, tier))
    finally:
        _readonly_restore(snap, role)


_READONLY_WF_FILES = (
    "00_ground_truth_ledger.json", "00_redlines.md", "00_story_material.md", "00_style_profile.json",
    "01_lineage.md", "01_literature_map.md", "01_briefing.md", "01_discussion.md", "01_story.md", "01_plan.json",
    "01_decisions.json", "01_lock.json", "07_feedback.json", "00_locks.json", "01_blueprint.json",
)


def _readonly_snapshot() -> Optional[Dict[str, Any]]:
    """In-memory copy of the manuscript and the workflow's ground-truth files before a read-only call.

    Only the Claude CLI can deny write tools; Antigravity `agent(...)` and the agy CLI cannot (agy's
    `toolPermission=strict` also denies reads, and a global `always-proceed` overrides `--mode plan`), so
    read-only is enforced afterwards for every backend: whatever such a call changed is put back.
    """
    paper = _wf_args().get("paper_dir")
    if not paper:
        return None
    pdir = Path(str(paper))
    if not pdir.is_dir():
        return None
    tex = list_editable_tex(pdir)
    files = tex + [pdir / n for n in ("macros.tex", "refs.bib")] + [pdir / ".workflow" / n for n in _READONLY_WF_FILES]
    contents: Dict[str, bytes] = {}
    for f in files:
        if f.is_file():
            try:
                contents[str(f.resolve())] = f.read_bytes()
            except OSError:
                pass
    return {"paper": pdir, "contents": contents, "tex": {str(f.resolve()) for f in tex}}


def _readonly_restore(snap: Optional[Dict[str, Any]], role: str) -> None:
    if not snap:
        return
    restored: List[str] = []
    moved: List[str] = []
    pdir: Path = snap["paper"]
    for path_s, data in snap["contents"].items():
        p = Path(path_s)
        try:
            if not p.exists() or p.read_bytes() != data:
                write_user_file(p, data)
                restored.append(os.path.relpath(path_s, pdir))
        except OSError as exc:
            log(f"[read-only guard] could not restore {path_s}: {exc}")
    for f in list_editable_tex(pdir):
        if str(f.resolve()) not in snap["tex"]:
            dest = pdir / ".workflow" / "readonly_violations" / f"{datetime.datetime.now().strftime('%Y%m%d_%H%M%S_%f')}_{f.name}"
            dest.parent.mkdir(parents=True, exist_ok=True)
            try:
                shutil.move(str(f), str(dest))
                moved.append(os.path.relpath(str(f), pdir))
            except OSError as exc:
                log(f"[read-only guard] could not move new file {f}: {exc}")
    if restored or moved:
        log(f"[read-only guard] {role} changed files it may only read: restored {restored}; moved new .tex files {moved} to .workflow/readonly_violations/")


def _log_route(role: str, route: Route) -> None:
    log(f"[route] {role}: {route.kind}:{route.backend} model={route.model or '(default)'} ({route.reason})")


async def _dispatch_agent_once(
    prompt: str,
    schema: Dict[str, Any],
    role: str,
    stage_name: str,
    workspace: str = "inherit",
    read_only: bool = False,
    add_dirs: Optional[List[str]] = None,
    cwd: Optional[str] = None,
    allow_bash_read_only: bool = False,
    tier: str = "builder",
) -> Any:
    route = route_call(stage_name, tier)
    _log_route(role, route)
    full_prompt = f"{prompt}\n\n{PDF_SAFETY_RULE}\n\n{task_hygiene_rule(add_dirs, route.backend)}"

    if route.kind == "native":
        ag_agent = _STATE["ag_agent"]
        eff_m = route.model
        try:
            return await _bounded(ag_agent(full_prompt, schema=schema, role=role, workspace=workspace, model=eff_m), role)
        except Exception as exc:
            if eff_m == "inherit" and "ended without calling finish" in str(exc):
                # Only reachable when the turn type could not be detected (in_plan_mode is None).
                _STATE["inherit_deadlock"] = True
                log("[Orchestrator execution-turn fallback] model='inherit' hit [plan review due] deadlock; retrying with model='pro'")
                return await _bounded(ag_agent(full_prompt, schema=schema, role=role, workspace=workspace, model="pro"), role)
            raise

    # Headless CLI (standalone runtime, or an Antigravity call routed to the agy CLI);
    # _run_cli_turn applies its own timeout and kills the CLI process tree on expiry.
    bin_p = _find_cli_binary(route.backend) if _STATE["runtime"] == "antigravity" else resolve_cli_backend(stage_name)[1]
    if not bin_p:
        raise RuntimeError(f"{role}: routed to the '{route.backend}' CLI ({route.reason}) but its binary was not found.")
    res, _ = await _run_cli_turn(route.backend, bin_p, full_prompt, schema, model=route.model, read_only=read_only,
                                 add_dirs_override=add_dirs, cwd=cwd, persist=False, allow_bash_read_only=allow_bash_read_only)
    return res


def create_persistent_handle(sys_prompt: str, role: str, stage_name: str, read_only: bool = False, tier: str = "builder") -> Dict[str, Any]:
    """Create a multi-turn agent handle (Antigravity native handle, or CLI session resumed with --resume/--conversation)."""
    route = route_call(stage_name, tier)
    _log_route(role, route)
    if route.kind == "native":
        ag_agent = _STATE["ag_agent"]
        return {
            "mode": "antigravity",
            "handle": ag_agent(sys_prompt, role=role, model=route.model),
            "sys_prompt": sys_prompt,
            "role": role,
            "model": route.model,
            "stage_name": stage_name,
            "read_only": read_only,
        }
    bin_p = _find_cli_binary(route.backend) if _STATE["runtime"] == "antigravity" else resolve_cli_backend(stage_name)[1]
    if not bin_p:
        raise RuntimeError(f"{role}: routed to the '{route.backend}' CLI ({route.reason}) but its binary was not found.")
    return {
        "mode": "cli",
        "backend": route.backend,
        "bin_path": bin_p,
        "sys_prompt": sys_prompt,
        "role": role,
        "model": route.model,
        "stage_name": stage_name,
        "session_id": None,
        "read_only": read_only,
    }


async def call_persistent_handle(slot: Dict[str, Any], prompt: str, schema: Dict[str, Any]) -> Any:
    """Invoke a persistent multi-turn slot (read-only slots are guarded like read-only dispatches)."""
    snap = _readonly_snapshot() if slot.get("read_only") else None
    try:
        return await _call_persistent_handle(slot, prompt, schema)
    finally:
        _readonly_restore(snap, slot["role"])


async def _call_persistent_handle(slot: Dict[str, Any], prompt: str, schema: Dict[str, Any]) -> Any:
    full_p = f"{prompt}\n\n{PDF_SAFETY_RULE}\n\n{task_hygiene_rule(None, 'antigravity' if slot['mode'] == 'antigravity' else slot.get('backend'))}"
    if slot["mode"] == "antigravity":
        ag_agent = _STATE["ag_agent"]

        async def _ag_turn() -> Any:
            try:
                return await _bounded(slot["handle"](full_p, schema), slot["role"])
            except Exception as exc:
                if slot["model"] == "inherit" and "ended without calling finish" in str(exc):
                    _STATE["inherit_deadlock"] = True
                    log(f"[Orchestrator execution-turn fallback] reviewer '{slot['role']}' hit deadlock; switching to 'pro'")
                    slot.pop("handle", None)
                    slot["handle"] = ag_agent(slot["sys_prompt"], role=slot["role"], model="pro")
                    slot["model"] = "pro"
                    return await _bounded(slot["handle"](full_p, schema), slot["role"])
                raise

        return await _with_retries(slot["role"], _ag_turn)

    async def _turn() -> Any:
        sent_p = full_p if slot.get("session_id") else f"SYSTEM INSTRUCTIONS:\n{slot['sys_prompt']}\n\nTURN 1:\n{full_p}"
        res, new_sid = await _run_cli_turn(
            slot["backend"],
            slot["bin_path"],
            sent_p,
            schema,
            model=slot["model"],
            session_id=slot.get("session_id"),
            read_only=bool(slot.get("read_only")),
        )
        if new_sid:
            slot["session_id"] = new_sid
        return res

    return await _with_retries(slot["role"], _turn)


async def _gather_fail_fast(coros: List[Any]) -> List[Any]:
    """Like gather(), but on the first failure cancel the siblings (their CLI process trees are killed) and re-raise."""
    tasks = [asyncio.ensure_future(c) for c in coros]
    try:
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
        failed = [t for t in done if not t.cancelled() and t.exception() is not None]
        if failed:
            for t in pending:
                t.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            raise failed[0].exception()  # type: ignore[misc]
        return [t.result() for t in tasks]
    except asyncio.CancelledError:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise


async def run_batched(items: Sequence[Any], worker_coro_fn: Callable[[Any], Any], batch_size: Optional[int] = None) -> List[Any]:
    """Run parallel agent batches capped at <= batch_size to prevent RPC subscriber or API throttling."""
    bsize = max(1, int(batch_size or _STATE["batch_size"] or 4))
    results: List[Any] = []
    seq = list(items)
    for i in range(0, len(seq), bsize):
        chunk = seq[i : i + bsize]
        if _STATE["runtime"] == "antigravity" and callable(_STATE["ag_parallel"]):
            res_chunk = await _STATE["ag_parallel"](chunk, worker_coro_fn)
        else:
            res_chunk = await _gather_fail_fast([worker_coro_fn(it) for it in chunk])
        results.extend(res_chunk)
    return results


def _file_hashes(paths: Sequence[Path]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for p in paths:
        try:
            out[str(p.resolve())] = hashlib.sha256(p.read_bytes()).hexdigest()
        except OSError:
            pass
    return out


async def run_guarded_edit_batches(
    paper_dir: Path,
    snapshot_dir: Path,
    rel_files: List[str],
    worker: Callable[[str], Any],
    batch_size: Optional[int] = None,
) -> List[Any]:
    """Run per-file edit agents in batches (batch_size=1: one file at a time, in order); afterwards restore any
    file an agent touched that was not one of that batch's targets (agents get write access to the whole directory)."""
    bsize = max(1, int(batch_size or _STATE["batch_size"] or 4))
    results: List[Any] = []
    for i in range(0, len(rel_files), bsize):
        chunk = rel_files[i : i + bsize]
        all_files = list_editable_tex(paper_dir) + [p for p in (paper_dir / "macros.tex", paper_dir / "refs.bib") if p.exists()]
        before = _file_hashes(all_files)
        results.extend(await run_batched(chunk, worker))
        targets = {str((paper_dir / r).resolve()) for r in chunk}
        after = _file_hashes(all_files)
        for path_s, h in before.items():
            if path_s in targets or after.get(path_s) == h:
                continue
            rel = os.path.relpath(path_s, paper_dir).replace("\\", "/")
            if (snapshot_dir / rel).exists():
                restore_tex_file(paper_dir, snapshot_dir, rel)
                log(f"[edit guard] restored {rel}: changed by an agent that was assigned other files")
            else:
                log(f"[edit guard] WARNING {rel} changed outside its assigned agent and has no snapshot; left as is")
    return results


def run_tool(script_name: str, cli_args: List[str], allow_fail: bool = True, timeout_s: int = 1800) -> Tuple[int, str, str, Optional[Dict[str, Any]]]:
    script_path = TOOLS_DIR / script_name
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    py_exe = resolve_python_exe()
    try:
        res = subprocess.run([py_exe, str(script_path), *cli_args], capture_output=True, text=True, encoding="utf-8", errors="replace", env=env, timeout=timeout_s)
    except subprocess.TimeoutExpired:
        if not allow_fail:
            raise RuntimeError(f"{script_name} timed out after {timeout_s}s")
        return 124, "", f"{script_name} timed out after {timeout_s}s", None
    parsed: Optional[Dict[str, Any]] = None
    out = res.stdout.strip()
    if out.startswith("{") or out.startswith("["):
        try:
            parsed = json.loads(out)
        except Exception:
            parsed = None
    if res.returncode != 0 and not allow_fail:
        raise RuntimeError(f"{script_name} failed ({res.returncode}): {res.stderr or out}")
    return res.returncode, res.stdout, res.stderr, parsed


def ensure_dirs(paper_dir: Path) -> Dict[str, Path]:
    paths = {
        "paper": paper_dir,
        "wf": paper_dir / ".workflow",
        "raw_drafts": paper_dir / ".workflow" / "02_raw_drafts",
        "verify_scripts": paper_dir / ".workflow" / "verify_scripts",
        "refs": paper_dir / ".workflow" / "refs",
        "fig_scripts": paper_dir / ".workflow" / "figures",
        "fig_renders": paper_dir / ".workflow" / "figures" / "renders",
        "snapshots": paper_dir / ".workflow" / "snapshots",
        "sections": paper_dir / "sections",
        "figures": paper_dir / "figures",
        "audit": paper_dir / "audit_reports",
        "submission": paper_dir / "submission",
    }
    for p in paths.values():
        p.mkdir(parents=True, exist_ok=True)
    return paths


def read_text_safe(p: Path) -> str:
    return p.read_text(encoding="utf-8", errors="replace") if p.exists() else ""


_SOURCE_EXTS = {".tex", ".md", ".txt", ".py", ".jl", ".nb", ".bib", ".json"}
_SOURCE_SKIP_DIRS = {".git", "__pycache__", ".workflow", ".wp_backups", "node_modules", ".venv", ".ipynb_checkpoints"}
_MAX_SOURCE_FILE_BYTES = 1_000_000  # larger files are data dumps, not notes
_MAX_JSON_SOURCE_BYTES = 200_000


def collect_source_files(raw_paths: List[str], skipped: Optional[List[str]] = None) -> List[Path]:
    files: List[Path] = []
    for raw in raw_paths:
        p = Path(raw)
        if p.is_file():
            files.append(p)  # explicitly named files are always taken
        elif p.is_dir():
            for sub in sorted(p.rglob("*")):
                if not sub.is_file() or sub.suffix.lower() not in _SOURCE_EXTS:
                    continue
                if any(part in _SOURCE_SKIP_DIRS for part in sub.relative_to(p).parts[:-1]):
                    continue
                size = sub.stat().st_size
                limit = _MAX_JSON_SOURCE_BYTES if sub.suffix.lower() == ".json" else _MAX_SOURCE_FILE_BYTES
                if size > limit:
                    if skipped is not None:
                        skipped.append(f"{sub} ({size // 1024} KB)")
                    continue
                files.append(sub)
        elif skipped is not None:
            skipped.append(f"{raw} (not found)")
    return files


def chunk_source_files(files: List[Path], budget_bytes: int = 60_000) -> List[List[Path]]:
    """Pack small files together so one scan agent reads several of them (authority files stay alone)."""
    chunks: List[List[Path]] = []
    cur: List[Path] = []
    cur_size = 0
    for f in files:
        size = f.stat().st_size if f.exists() else 0
        if cur and cur_size + size > budget_bytes:
            chunks.append(cur)
            cur, cur_size = [], 0
        cur.append(f)
        cur_size += size
    if cur:
        chunks.append(cur)
    return chunks


def _is_skipped_generated_tex(p: Path) -> bool:
    nl = p.name.lower()
    return nl in {"macros.tex", "inventory.tex", "loadbearing.tex"} or nl.endswith(("_inventory.tex", "_loadbearing.tex"))


def list_editable_tex(paper_dir: Path) -> List[Path]:
    skip_dirs = {".workflow", "submission", ".git", "__pycache__", ".wp_backups"}
    out: List[Path] = []
    for p in sorted(paper_dir.rglob("*.tex")):
        if any(part in skip_dirs for part in p.relative_to(paper_dir).parts):
            continue
        if _is_skipped_generated_tex(p):
            continue
        out.append(p)
    return out


# ------------------------------------------------------------------------------
# Read-only-safe file operations (Windows: the read-only attribute blocks delete and replace;
# shutil.copy2 copies that attribute, so snapshots of read-only sources used to become undeletable)
# ------------------------------------------------------------------------------

def is_readonly(p: Path) -> bool:
    try:
        st = os.stat(p)
    except OSError:
        return False
    attrs = getattr(st, "st_file_attributes", None)
    if attrs is not None:  # Windows: the attribute, also on directories
        return bool(attrs & getattr(stat, "FILE_ATTRIBUTE_READONLY", 1))
    return not (st.st_mode & stat.S_IWUSR)


def clear_readonly(p: Path) -> None:
    try:
        os.chmod(p, stat.S_IMODE(os.stat(p).st_mode) | stat.S_IWRITE | stat.S_IWUSR)
    except OSError:
        pass


def force_rmtree(path: Path) -> None:
    """rmtree for workflow-owned folders that tolerates read-only files and directories."""
    if not Path(path).exists():
        return

    def _retry(func: Callable[..., Any], p: str, _info: Any) -> None:
        clear_readonly(Path(p))
        clear_readonly(Path(p).parent)
        func(p)

    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=_retry)  # type: ignore[call-arg]
    else:
        shutil.rmtree(path, onerror=_retry)


def copy_to_workflow(src: Path, dst: Path) -> None:
    """Copy into a workflow-owned location; the copy is always writable (never inherits read-only)."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists() and is_readonly(dst):
        clear_readonly(dst)
    shutil.copy2(src, dst)
    clear_readonly(dst)


def write_user_file(dst: Path, data: bytes) -> None:
    """Rewrite a user file's content while keeping its attributes (a read-only file stays read-only)."""
    mode = stat.S_IMODE(os.stat(dst).st_mode) if dst.exists() else None
    was_ro = dst.exists() and is_readonly(dst)
    if was_ro:
        clear_readonly(dst)
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(data)
    finally:
        if was_ro and mode is not None:
            try:
                os.chmod(dst, mode)
            except OSError:
                pass


def snapshot_tex_files(paper_dir: Path, dest_dir: Path) -> None:
    force_rmtree(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    for f in list_editable_tex(paper_dir):
        copy_to_workflow(f, dest_dir / f.relative_to(paper_dir))


def snapshot_project(paper_dir: Path, dest_dir: Path) -> Path:
    """Full checkpoint snapshot including macros.tex, refs.bib & all editable .tex files."""
    force_rmtree(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    for f in list_editable_tex(paper_dir):
        copy_to_workflow(f, dest_dir / f.relative_to(paper_dir))
    for extra_name in ("macros.tex", "refs.bib"):
        ep = paper_dir / extra_name
        if ep.exists():
            copy_to_workflow(ep, dest_dir / extra_name)
    return dest_dir


def restore_tex_file(paper_dir: Path, snapshot_dir: Path, rel_path: str) -> None:
    src = snapshot_dir / rel_path
    dst = paper_dir / rel_path
    if src.exists():
        write_user_file(dst, src.read_bytes())


def preflight_permissions(paper_dir: Path) -> Dict[str, Any]:
    """Clear read-only attributes inside workflow-owned folders; report read-only manuscript files."""
    cleared: List[str] = []
    for owned in (paper_dir / ".workflow", paper_dir / "submission"):
        if not owned.exists():
            continue
        for p in [owned, *owned.rglob("*")]:
            if is_readonly(p):
                clear_readonly(p)
                cleared.append(os.path.relpath(p, paper_dir))
    blocked: List[str] = []
    skip = {".git", ".workflow", "submission", ".wp_backups", "__pycache__"}
    for p in paper_dir.rglob("*"):
        if any(part in skip for part in p.relative_to(paper_dir).parts):
            continue
        if p.is_file() and p.suffix.lower() in (".tex", ".bib", ".sty", ".cls", ".bst", ".py", ".json") and is_readonly(p):
            blocked.append(str(p.relative_to(paper_dir)).replace("\\", "/"))
    if cleared:
        log(f"[preflight] cleared the read-only attribute on {len(cleared)} workflow-owned paths (.workflow/, submission/)")
    if blocked:
        fix = f'attrib -R /S /D "{paper_dir}\\*"' if sys.platform.startswith("win") else f'chmod -R u+w "{paper_dir}"'
        log(f"[preflight] WARNING {len(blocked)} manuscript files are read-only, so agents cannot edit them: {blocked[:10]}"
            f"{' ...' if len(blocked) > 10 else ''}. They are left as they are; to allow edits run: {fix}")
    return {"cleared_workflow_paths": len(cleared), "readonly_manuscript_files": blocked}


def resolve_supplement_path(paper_dir: Path) -> Optional[Path]:
    args = _wf_args()
    if args.get("supplement_tex"):
        p = Path(str(args["supplement_tex"]))
        return p if p.exists() else None
    cand = paper_dir / "supplement.tex"
    return cand if cand.exists() else None


def ensure_default_profile(paths: Dict[str, Path]) -> Path:
    prof_path = paths["wf"] / "00_style_profile.json"
    if not prof_path.exists():
        prof_path.write_text(
            json.dumps({
                "exemplar_files": [],
                "baseline_metrics": {"mean_sentence_len": 20.0, "short_le8_ratio": 0.08},
                "preferred_lexicon": {"lexicon_vetted": False, "vetted_replacements": {}, "vetted_style_hints": {}},
                "locked_terms": [],
                "exemplar_paragraphs": [],
                "categorized_exemplars": {},
            }, indent=2),
            encoding="utf-8",
        )
    return prof_path


async def _edit_with_math_lock_retry(
    rel_file: str,
    paper_dir: Path,
    snapshot_dir: Path,
    instruction_fn: Callable[[int, str], str],
    role: str,
    stage_name: str,
    extra_check_args: List[str],
    tier: str = "builder",
) -> bool:
    """Apply an agent edit to rel_file, checking math/quantifier lock with diff-guided retry."""
    target_path = paper_dir / rel_file
    orig_snap = snapshot_dir / rel_file
    if not target_path.exists() or not orig_snap.exists():
        return True

    feedback = ""
    for attempt in range(2):
        await dispatch_agent(
            instruction_fn(attempt + 1, feedback) + f"\n\nEdit ONLY `{target_path}`; do not modify any other file.",
            schema={"type": "string"},
            role=f"{role} (try {attempt + 1})" if attempt > 0 else role,
            stage_name=stage_name,
            tier=tier,
        )
        rc, _, _, chk = run_tool(
            "check_math_integrity.py",
            ["--before", str(orig_snap), "--after", str(target_path), *extra_check_args],
            allow_fail=True,
        )
        if rc == 0:
            return True
        feedback = json.dumps(chk or {"error": "check_math_integrity failed"}, indent=2, ensure_ascii=False)[:2500]
        log(f"[Lock Retry] {rel_file} violated math/quantifier lock on try {attempt + 1}; restoring & feeding diff")
        restore_tex_file(paper_dir, snapshot_dir, rel_file)
    log(f"[Lock Escalation] {rel_file} failed lock twice; preserved snapshot cleanly to prevent corruption.")
    return False


# ==============================================================================
# v3 pipeline
#   inventory (material pack) -> story (lineage, story, block plan; full_auto pauses here for the author)
#   -> draft (one author writes the main text block by block; SM blocks in parallel)
#   -> check (report only) -> revise (the author fixes) -> check -> build (deterministic clean-up + compile)
# Writers get material and the story, not rule lists. Whatever a program can check (length limits, acronyms
# in the abstract, heading breaks, leaks, word budget) is checked after writing instead of being demanded up front.
# ==============================================================================

ABSTRACT_LIMITS = {"letter": 600}  # rendered characters including spaces (APS Letters); long papers: no hard limit
SEVERITIES = ("high", "medium", "low")
MODE_ALIASES = {
    "blueprint": "story",
    "verify": "check",
    "citations": "check",
    "hygiene": "cleanup",
    "repair": "revise",
    "draft_and_audit": "draft_and_check",
    "audit_polish": "check_and_revise",
    "full_auto": "auto",
}
# Pipelines of v3 are `auto` started at a stage.
PIPELINE_STARTS = {"draft_and_check": "draft", "check_and_revise": "check"}
AUTO_STARTS = ("", "brief", "story", "draft", "check", "revise", "build")

# How much reading, checking and literature work a run does. The writing itself is the same at every level;
# explicit arguments (max_parallel, max_numeric_checks, revise_low) override the preset.
EFFORT_PRESETS: Dict[str, Dict[str, Any]] = {
    "low": {"max_parallel": 6, "lineage_steps": "3 to 5", "neighbour_clusters": "", "max_numeric_checks": 0,
            "separate_reader": False, "check_group_bytes": 120_000, "revise_low": False, "referee": False,
            "cite_context_batch": 0, "fulltext_works": 0},
    "medium": {"max_parallel": 4, "lineage_steps": "4 to 8", "neighbour_clusters": "2 to 3", "max_numeric_checks": 4,
               "separate_reader": True, "check_group_bytes": 60_000, "revise_low": False, "referee": False,
               "cite_context_batch": 40, "fulltext_works": 8},
    "high": {"max_parallel": 4, "lineage_steps": "5 to 10", "neighbour_clusters": "3 to 4", "max_numeric_checks": 10,
             "separate_reader": True, "check_group_bytes": 40_000, "revise_low": True, "referee": True,
             "cite_context_batch": 20, "fulltext_works": 20},
}


def effort_level(args: Optional[Dict[str, Any]] = None) -> str:
    lvl = str((args if args is not None else _wf_args()).get("effort") or "medium").lower()
    return lvl if lvl in EFFORT_PRESETS else "medium"


def effort() -> Dict[str, Any]:
    return EFFORT_PRESETS[effort_level()]


def effort_setting(key: str, arg_name: Optional[str] = None) -> Any:
    """An explicit argument wins over the effort preset."""
    val = _wf_args().get(arg_name or key)
    return effort()[key] if val is None else val


def initial_batch_size(args: Dict[str, Any]) -> int:
    return max(1, min(8, int(args.get("max_parallel") or EFFORT_PRESETS[effort_level(args)]["max_parallel"])))


def only_files(paper_dir: Path) -> List[str]:
    """`only_files`: limit draft, check and revise to these files (paper-relative)."""
    out: List[str] = []
    for raw in _wf_args().get("only_files") or []:
        rel = resolve_paper_file(paper_dir, raw)
        if rel is None:
            raise RuntimeError(f"only_files must point inside {paper_dir}: {raw}")
        if rel not in out:
            out.append(rel)
    return out


# ------------------------------------------------------------------------------
# Concerns for the author (human in the loop)
#
# The author's decisions are the current best judgement, not rules: they may rest on information that turns
# out to be incomplete. An agent that finds such a case neither obeys silently nor overrides silently; it files
# a concern with evidence and options. Blocking concerns stop `auto` at the next stage boundary; the others
# wait in the list while the run goes on with the decision as it stands.
# ------------------------------------------------------------------------------

FEEDBACK_KINDS = ("evidence", "literature", "feasibility", "conflict")
CONCERN_ITEM = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "enum": list(FEEDBACK_KINDS)}, "blocking": {"type": "boolean"},
        "finding": {"type": "string"}, "evidence": {"type": "string"}, "decision_id": {"type": "string"},
        "options": {"type": "array", "items": {"type": "string"}}, "recommendation": {"type": "string"},
        "affected_blocks": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["kind", "finding", "evidence"],
}
CONCERN_RULE = (
    "Raise a concern only when the agreed story or one of the author's decisions may rest on something that does not hold: "
    "evidence weaker than the decision assumed, a reference that changes what is new, a part that cannot be written as planned, "
    "or two decisions (or a decision and a red line) in conflict. Give the evidence, the options (keeping the current decision is "
    "always one) and your recommendation; mark it blocking only if the paper cannot go on correctly without the author's answer. "
    "Wording, numbers, citations and length are not concerns: handle them yourself."
)
MAX_BLOCKING_SHOWN = 3


def _feedback_path(wf: Path) -> Path:
    return wf / "07_feedback.json"


def load_feedback(wf: Path) -> Dict[str, Any]:
    try:
        data = json.loads(read_text_safe(_feedback_path(wf)) or "{}")
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{_feedback_path(wf)} is not valid JSON ({exc.msg} at line {exc.lineno}); fix the answers there and run again")
    items = data.get("items") if isinstance(data, dict) else None
    return {"items": [i for i in (items or []) if isinstance(i, dict)]}


def load_decisions(wf: Path) -> List[Dict[str, Any]]:
    try:
        data = json.loads(read_text_safe(wf / "01_decisions.json") or "{}")
    except json.JSONDecodeError:
        return []
    items = data.get("decisions") if isinstance(data, dict) else data
    return [d for d in (items or []) if isinstance(d, dict)]


def open_blocking(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [i for i in data["items"] if i.get("status") == "open" and i.get("blocking")]


def save_feedback(paths: Dict[str, Path], data: Dict[str, Any]) -> None:
    wf = paths["wf"]
    _feedback_path(wf).write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    md = ["# Concerns for the author", "",
          f"Answer in `{_feedback_path(wf)}`: set `answer` (the author's words), `status` to \"answered\", and `keep` to true when the "
          "author keeps the decision as it is. Then run mode=auto again.", ""]
    for state in ("open", "answered", "applied"):
        items = [i for i in data["items"] if i.get("status") == state]
        if not items:
            continue
        md += [f"## {state.capitalize()} ({len(items)})", ""]
        for i in sorted(items, key=lambda x: not x.get("blocking")):
            md.append(f"### {i['id']} [{i.get('kind', '')}{', blocking' if i.get('blocking') else ''}] from {i.get('stage', '')}")
            md.append(f"- Finding: {i.get('finding', '')}")
            md.append(f"- Evidence: {i.get('evidence', '')}")
            if i.get("decision_id"):
                md.append(f"- Decision {i['decision_id']}: {i.get('decision_choice', '')} (rested on: {i.get('decision_basis', '') or 'not recorded'})")
            for o in i.get("options") or []:
                md.append(f"- Option: {o}")
            if i.get("recommendation"):
                md.append(f"- Recommendation: {i['recommendation']}")
            if i.get("answer"):
                md.append(f"- Answer: {i['answer']}")
            md.append("")
    (paths["audit"] / "07_feedback.md").write_text("\n".join(md), encoding="utf-8")


def add_feedback(paths: Dict[str, Path], items: Sequence[Any], stage: str) -> List[Dict[str, Any]]:
    """File concerns raised by agents; duplicates of a concern that is still open are dropped."""
    wf = paths["wf"]
    data = load_feedback(wf)
    decisions = {str(d.get("id")): d for d in load_decisions(wf)}

    def key(i: Dict[str, Any]) -> Tuple[str, str, str]:
        return (str(i.get("kind") or ""), str(i.get("decision_id") or ""), re.sub(r"\s+", " ", str(i.get("finding") or "")).strip().lower()[:80])

    seen = {key(i) for i in data["items"] if i.get("status") in ("open", "answered")}
    added: List[Dict[str, Any]] = []
    merged = False
    n = len(data["items"])
    for raw in items:
        if not isinstance(raw, dict) or not str(raw.get("finding") or "").strip():
            continue
        if key(raw) in seen:
            continue
        seen.add(key(raw))
        # The same kind of concern about the same decision from the same stage is one question for the author:
        # the new finding joins the open item instead of becoming another one.
        twin = next((i for i in data["items"] if i.get("status") == "open" and i.get("stage") == stage and str(raw.get("decision_id") or "")
                     and i.get("decision_id") == str(raw.get("decision_id")) and i.get("kind") == str(raw.get("kind") or "evidence")), None)
        if twin is not None:
            twin["evidence"] = f"{twin.get('evidence', '')} | Also: {raw.get('finding')} ({raw.get('evidence') or ''})".strip(" |")
            twin["blocking"] = bool(twin.get("blocking")) or bool(raw.get("blocking"))
            merged = True
            continue
        n += 1
        kind = str(raw.get("kind") or "evidence")
        it = {
            "id": f"F{n}", "stage": stage, "kind": kind if kind in FEEDBACK_KINDS else "evidence", "blocking": bool(raw.get("blocking")),
            "finding": str(raw.get("finding")), "evidence": str(raw.get("evidence") or ""), "decision_id": str(raw.get("decision_id") or ""),
            "options": [str(o) for o in raw.get("options") or []], "recommendation": str(raw.get("recommendation") or ""),
            "affected_blocks": [str(b) for b in raw.get("affected_blocks") or []], "status": "open", "answer": "",
            "created": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
        }
        d = decisions.get(it["decision_id"])
        if d:
            it["decision_choice"] = str(d.get("choice") or "")
            it["decision_basis"] = str(d.get("basis") or "")
        data["items"].append(it)
        added.append(it)
    if added or merged:
        save_feedback(paths, data)
    if added:
        log(f"[concerns] {len(added)} new concern(s) for the author from {stage} "
            f"({sum(1 for i in added if i['blocking'])} blocking) -> {paths['audit'] / '07_feedback.md'}")
    return added


def concerns_from_issues(issues: Sequence[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Split checker issues: those marked with `feedback_kind` go to the author (a high one blocks), the rest to revise."""
    concerns: List[Dict[str, Any]] = []
    rest: List[Dict[str, Any]] = []
    for it in issues:
        if str(it.get("feedback_kind") or "") in FEEDBACK_KINDS:
            where = f"{it.get('file', '')}:{it.get('location', '')}".strip(":")
            concerns.append({"kind": it["feedback_kind"], "blocking": it.get("severity") == "high", "finding": it.get("problem", ""),
                             "evidence": "; ".join(x for x in (where, str(it.get("quote") or ""), str(it.get("evidence") or "")) if x),
                             "decision_id": it.get("decision_id", ""), "recommendation": it.get("suggested_fix", ""),
                             "options": ["keep the current text and decision"] + ([str(it["suggested_fix"])] if it.get("suggested_fix") else [])})
        else:
            rest.append(it)
    return concerns, rest


def decisions_note(wf: Path) -> str:
    p = wf / "01_decisions.json"
    return (f"The author's decisions, each with what it rests on, are in `{p}`: a choice recorded there is deliberate. " if p.exists() else "")


def hand_edit_note(wf: Path) -> str:
    try:
        info = json.loads(read_text_safe(wf / "05_hand_edits.json") or "{}")
    except json.JSONDecodeError:
        return ""
    files = info.get("files") or []
    if not files:
        return ""
    return (f"The author edited {', '.join(files)} by hand since the last run (diff: `{info.get('diff', '')}`). Those edits are intended: "
            "never undo them; if one conflicts with the ledger or a decision, say so instead of changing it back. ")


def stage_refs(paths: Dict[str, Path]) -> Dict[str, Path]:
    """Copy the reference notes into .workflow/refs/ and return their paths.

    Agents get these paths and read the files themselves. CLI workers may be refused reads outside the paper
    directory, and the v2 habit of inlining excerpts cut at a fixed length silently dropped most of each file.
    """
    out: Dict[str, Path] = {}
    for name in ("genre_templates.md", "figure_visual_language.md"):
        src = REFS_DIR / name
        if src.exists():
            dst = paths["wf"] / "refs" / name
            copy_to_workflow(src, dst)
            out[name] = dst
    return out


def _ledger_claims(ledger_path: Path) -> List[Dict[str, Any]]:
    """The ledger is agent-written: accept {"claims": [...]}, close variants, or a bare list."""
    try:
        data = json.loads(read_text_safe(ledger_path) or "null")
    except json.JSONDecodeError as exc:
        log(f"[ledger] {ledger_path} is not valid JSON ({exc.msg} at line {exc.lineno})")
        return []
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        items = next((data[k] for k in ("claims", "verified_claims", "ledger", "entries", "items") if isinstance(data.get(k), list)), [])
    else:
        items = []
    out: List[Dict[str, Any]] = []
    for i, it in enumerate(items):
        out.append(it if isinstance(it, dict) else {"id": f"C{i + 1}", "summary": str(it)})
    return out


def _ledger_index(ledger_path: Path) -> Dict[str, Dict[str, Any]]:
    return {str(c["id"]): c for c in _ledger_claims(ledger_path) if c.get("id") is not None}


def ledger_records(index: Dict[str, Dict[str, Any]], ids: Sequence[Any]) -> str:
    """The exact ledger records a block needs, joined by the program: the plan only names their ids, so no
    agent ever works from a paraphrase of a result (v2's blueprint paraphrases were the main error source)."""
    recs = [index[str(i)] for i in ids if str(i) in index]
    return json.dumps(recs, indent=1, ensure_ascii=False) if recs else "(none assigned)"


def _plan_path() -> Path:
    args = _wf_args()
    return Path(str(args.get("plan_path") or (Path(str(args["paper_dir"])) / ".workflow" / "01_plan.json")))


def validate_plan(plan_path: Path, known_ids: Optional[Sequence[str]] = None) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Parse 01_plan.json. Blocks name results only by ledger id; ids missing from the ledger are an error."""
    if not plan_path.exists():
        return None, f"{plan_path} does not exist (run mode=story first)"
    try:
        data = json.loads(read_text_safe(plan_path))
    except json.JSONDecodeError as exc:
        return None, f"invalid JSON at line {exc.lineno} col {exc.colno}: {exc.msg}"
    if not isinstance(data, dict):
        return None, "top level must be a JSON object"
    blocks = data.get("blocks")
    if not isinstance(blocks, list) or not blocks:
        return None, "`blocks` must be a non-empty list"
    known = {str(k) for k in known_ids} if known_ids else None
    ids: set = set()
    files: set = set()
    unknown: List[str] = []
    for i, b in enumerate(blocks):
        if not isinstance(b, dict):
            return None, f"blocks[{i}] is not an object"
        bid = str(b.get("id") or "").strip()
        f = str(b.get("file") or "").strip().replace("\\", "/")
        if not bid or bid in ids:
            return None, f"blocks[{i}] needs a unique `id`"
        if b.get("part") not in ("main", "sm"):
            return None, f'blocks[{i}] `part` must be "main" or "sm"'
        if not f.endswith(".tex") or f.startswith("/") or ":" in f or ".." in f.split("/"):
            return None, f"blocks[{i}] `file` must be a relative .tex path inside the paper directory"
        if f in files:
            return None, f"blocks[{i}] reuses the file {f}"
        if not str(b.get("purpose") or "").strip():
            return None, f"blocks[{i}] has no `purpose`"
        cids = b.get("claim_ids") or []
        if not isinstance(cids, list):
            return None, f"blocks[{i}] `claim_ids` must be a list"
        if known is not None:
            unknown += [str(c) for c in cids if str(c) not in known]
        ids.add(bid)
        files.add(f)
        b["file"] = f
    if not any(b.get("part") == "main" for b in blocks):
        return None, "the plan has no main-text block"
    if unknown:
        return None, f"claim_ids not in the ledger: {sorted(set(unknown))[:20]}"
    figs = data.get("figures") or []
    if not isinstance(figs, list) or not all(isinstance(x, dict) and str(x.get("id") or "").strip() for x in figs):
        return None, "`figures` must be a list of objects with an `id`"
    return data, None


def validate_blueprint(bp_path: Path) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """A v2 `01_blueprint.json`; v3 reads it only for the figure list of old workspaces."""
    if not bp_path.exists():
        return None, f"{bp_path} does not exist"
    try:
        data = json.loads(read_text_safe(bp_path))
    except json.JSONDecodeError as exc:
        return None, f"invalid JSON at line {exc.lineno} col {exc.colno}: {exc.msg}"
    if not isinstance(data, dict):
        return None, "top level must be a JSON object"
    sections = data.get("sections")
    if not isinstance(sections, list) or not sections:
        return None, "`sections` must be a non-empty list"
    for idx, sec in enumerate(sections):
        if not isinstance(sec, dict):
            return None, f"sections[{idx}] is not an object"
        if not str(sec.get("file") or "").strip():
            return None, f"sections[{idx}] has no `file`"
    return data, None


async def _ensure_valid_plan(plan_path: Path, ledger_path: Path) -> Dict[str, Any]:
    known = list(_ledger_index(ledger_path))
    data, err = validate_plan(plan_path, known)
    if err is None and data is not None:
        return data
    if not plan_path.exists():
        raise RuntimeError(err)
    log(f"[plan] invalid: {err}; asking for a repair")
    await dispatch_agent(
        f"`{plan_path}` fails validation: {err}. Fix it in place: strict JSON; every block needs a unique `id`, `part` \"main\" or \"sm\", "
        f"a unique relative `file` ending in .tex, a one-sentence `purpose`, and `claim_ids` that exist in `{ledger_path}`. Change nothing else.",
        schema={"type": "string"},
        role="Plan JSON Repair",
        stage_name="story",
        tier="fast",
    )
    data, err = validate_plan(plan_path, known)
    if err is not None or data is None:
        raise RuntimeError(f"01_plan.json is still invalid after repair: {err}")
    return data


# ------------------------------------------------------------------------------
# Survey: where the paper stands (programs only)
#
# The workflow can start on a paper at any stage of completion. It looks at what exists, derives what is
# missing upstream from what is there (a block plan from the .tex files, the material pack from the paper's
# own text when no notes are given), writes only missing or partial blocks, and treats the author's hand
# edits since the last run as intended.
# ------------------------------------------------------------------------------

_PLACEHOLDER_RE = re.compile(r"\\todo\s*\{|\bTODO\b|\[TODO|\bTBD\b|\bXXX\b")
MIN_BLOCK_WORDS = 40


def _strip_tex_comments(text: str) -> str:
    return "\n".join(re.split(r"(?<!\\)%", ln, maxsplit=1)[0] for ln in text.splitlines())


def _body(text: str) -> str:
    """The part after \\begin{document}, if any (the skeleton preamble defines \\todo with a [TODO ...] text)."""
    i = text.find("\\begin{document}")
    return text[i:] if i >= 0 else text


def prose_words(text: str) -> int:
    t = re.sub(r"\$\$.*?\$\$|\$[^$]*\$", " ", text, flags=re.S)
    t = re.sub(r"\\begin\{(equation|align|eqnarray|gather|multline)(\*?)\}.*?\\end\{\1\2\}", " ", t, flags=re.S)
    t = re.sub(r"\\[A-Za-z@]+\*?", " ", t)
    return len(re.findall(r"[A-Za-z]{2,}", t))


def block_status(paper_dir: Path, rel: str) -> str:
    """empty (missing or under MIN_BLOCK_WORDS words of prose), partial (has \\todo / TODO / TBD), or full."""
    p = paper_dir / rel
    if not p.exists():
        return "empty"
    text = _body(_strip_tex_comments(read_text_safe(p)))
    if prose_words(text) < MIN_BLOCK_WORDS:
        return "empty"
    return "partial" if _PLACEHOLDER_RE.search(text) else "full"


def abstract_missing(paper_dir: Path) -> bool:
    m = re.search(r"\\begin\{abstract\}(.*?)\\end\{abstract\}", _strip_tex_comments(read_text_safe(paper_dir / "main.tex")), flags=re.S)
    return not m or m.group(1).strip() in ("", "ABSTRACT") or prose_words(m.group(1)) < 15


def derive_plan_from_tex(paper_dir: Path) -> Optional[Dict[str, Any]]:
    """A block plan for a paper that exists without one: one block per input file (or main.tex itself)."""
    main = paper_dir / "main.tex"
    if not main.exists():
        return None
    blocks: List[Dict[str, Any]] = []
    used: set = set()

    def add(rel: str, part: str) -> None:
        bid = re.sub(r"[^A-Za-z0-9]+", "-", Path(rel).stem).strip("-") or "block"
        while bid in used:
            bid += "b"
        used.add(bid)
        m = re.search(r"\\(?:section|subsection)\*?\{([^}]*)\}", read_text_safe(paper_dir / rel))
        blocks.append({"id": bid, "part": part, "file": rel, "claim_ids": [],
                       "purpose": f"existing text: {m.group(1).strip()}" if m else f"existing text of {rel}"})

    main_inputs = main_part_files(paper_dir)[:-1]
    for rel in main_inputs:
        add(rel, "main")
    if not main_inputs:
        add("main.tex", "main")
    supp = resolve_supplement_path(paper_dir)
    supp_rel = _paper_rel(paper_dir, supp) if supp else None
    if supp_rel:
        sm_inputs = [r for r in part_files(paper_dir, supp_rel)[:-1] if r not in main_inputs]
        for rel in sm_inputs:
            add(rel, "sm")
        if not sm_inputs:
            add(supp_rel, "sm")
    t = re.search(r"\\title\{([^}]*)\}", read_text_safe(main))
    return {"title": t.group(1).strip() if t else "", "blocks": blocks, "figures": [], "derived_from_tex": True}


def current_plan(paths: Dict[str, Path], save: bool = False) -> Optional[Dict[str, Any]]:
    """The block plan, or one derived from the .tex files (saved when asked, so later stages can use it)."""
    plan, _ = validate_plan(_plan_path())
    if plan is not None:
        return plan
    derived = derive_plan_from_tex(paths["paper"])
    if derived is not None and save:
        _plan_path().write_text(json.dumps(derived, indent=2, ensure_ascii=False), encoding="utf-8")
        log(f"[survey] no block plan: derived one from the .tex files ({len(derived['blocks'])} blocks) -> {_plan_path()}")
    return derived


def tracked_text_files(paper_dir: Path) -> List[Path]:
    return list_editable_tex(paper_dir) + [p for p in (paper_dir / "macros.tex", paper_dir / "refs.bib") if p.exists()]


def text_hashes(paper_dir: Path) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for p in tracked_text_files(paper_dir):
        try:
            out[str(p.relative_to(paper_dir)).replace("\\", "/")] = hashlib.sha256(p.read_bytes()).hexdigest()
        except OSError:
            pass
    return out


def detect_hand_edits(paths: Dict[str, Path]) -> List[str]:
    """Files changed since the workflow last finished (by the author, by hand). The diff goes to the agents."""
    paper_dir, wf = paths["paper"], paths["wf"]
    try:
        before = json.loads(read_text_safe(wf / "05_ai_hashes.json") or "{}")
    except json.JSONDecodeError:
        before = {}
    info: Dict[str, Any] = {"files": [], "diff": ""}
    if before:
        import difflib
        now = text_hashes(paper_dir)
        changed = sorted(r for r, h in now.items() if before.get(r) != h)
        chunks: List[str] = []
        for rel in changed:
            old = read_text_safe(wf / "05_ai_copy" / rel).splitlines(keepends=True)
            new = read_text_safe(paper_dir / rel).splitlines(keepends=True)
            chunks.append("".join(difflib.unified_diff(old, new, fromfile=f"workflow/{rel}", tofile=f"author/{rel}")))
        if changed:
            diff_path = wf / "05_hand_edits.diff"
            diff_path.write_text("\n".join(chunks), encoding="utf-8")
            info = {"files": changed, "diff": str(diff_path)}
            log(f"[survey] hand edits since the last run (treated as intended): {changed}")
    (wf / "05_hand_edits.json").write_text(json.dumps(info, indent=2, ensure_ascii=False), encoding="utf-8")
    return info["files"]


def remember_text(paths: Dict[str, Path], before: Optional[Dict[str, str]] = None) -> None:
    """Record what the workflow itself changed in this run (files whose hash differs from `before`), so the next
    run's remaining differences are the author's hand edits. A file the run did not touch keeps its old record:
    a hand edit stays visible to later runs until an agent has worked on that file. The first run records all."""
    paper_dir, wf = paths["paper"], paths["wf"]
    copy_dir = wf / "05_ai_copy"
    hashes_path = wf / "05_ai_hashes.json"
    try:
        stored = json.loads(read_text_safe(hashes_path) or "{}")
    except json.JSONDecodeError:
        stored = {}
    now = text_hashes(paper_dir)
    if not stored or before is None:
        force_rmtree(copy_dir)
        stored, changed, removed = {}, set(now), set()
    else:
        changed = {r for r, h in now.items() if before.get(r) != h}
        removed = {r for r in before if r not in now}
    for rel in changed:
        copy_to_workflow(paper_dir / rel, copy_dir / rel)
        stored[rel] = now[rel]
    for rel in removed:
        stored.pop(rel, None)
        if (copy_dir / rel).exists():
            clear_readonly(copy_dir / rel)
            (copy_dir / rel).unlink()
    hashes_path.write_text(json.dumps(stored, indent=1), encoding="utf-8")


def _lock_hashes(wf: Path) -> Dict[str, str]:
    return {n: _file_sha(wf / n) for n in ("01_story.md", "01_plan.json", "01_decisions.json") if (wf / n).exists()}


def lock_valid(wf: Path) -> bool:
    try:
        lock = json.loads(read_text_safe(wf / "01_lock.json") or "{}")
    except json.JSONDecodeError:
        return False
    return bool(lock.get("hashes")) and lock.get("hashes") == _lock_hashes(wf)


def _load_rewrite(wf: Path) -> Dict[str, str]:
    try:
        data = json.loads(read_text_safe(wf / "01_rewrite.json") or "{}")
    except json.JSONDecodeError:
        return {}
    return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}


def mark_rewrite(wf: Path, items: Sequence[Any]) -> Dict[str, str]:
    cur = _load_rewrite(wf)
    for it in items:
        if isinstance(it, dict) and str(it.get("block") or "").strip():
            cur[str(it["block"])] = str(it.get("why") or "the story changed")
    (wf / "01_rewrite.json").write_text(json.dumps(cur, indent=2, ensure_ascii=False), encoding="utf-8")
    return cur


async def lock_story(paths: Dict[str, Path]) -> Dict[str, Any]:
    """The author confirmed the story, plan and decisions. Plan blocks whose job changed since the last lock are
    marked for rewriting (a block that is new or empty is written anyway)."""
    wf = paths["wf"]
    plan = await _ensure_valid_plan(_plan_path(), wf / "00_ground_truth_ledger.json")
    if not (wf / "01_decisions.json").exists():
        (wf / "01_decisions.json").write_text(json.dumps({"decisions": []}, indent=2), encoding="utf-8")
    try:
        old = json.loads(read_text_safe(wf / "01_lock.json") or "{}")
    except json.JSONDecodeError:
        old = {}
    old_blocks = {b.get("id"): b for b in (old.get("plan_blocks") or []) if isinstance(b, dict)}
    changed = [{"block": b["id"], "why": f"its job in the plan changed: {b['purpose']}"} for b in plan["blocks"]
               if b["id"] in old_blocks and any(old_blocks[b["id"]].get(k) != b.get(k) for k in ("purpose", "claim_ids", "file", "part"))]
    if changed:
        mark_rewrite(wf, changed)
    (wf / "01_lock.json").write_text(json.dumps({"locked": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"), "hashes": _lock_hashes(wf),
                                                 "plan_blocks": plan["blocks"]}, indent=2, ensure_ascii=False), encoding="utf-8")
    log(f"[story] locked{f'; {len(changed)} block(s) to rewrite because their job changed' if changed else ''}")
    return {"locked": True, "rewrite": [c["block"] for c in changed]}


def run_survey(paths: Dict[str, Path]) -> Dict[str, Any]:
    """Where the paper stands and what `auto` would do next. Programs only; no agent is called."""
    paper_dir, wf = paths["paper"], paths["wf"]
    plan = current_plan(paths)
    blocks = [{"id": b["id"], "part": b["part"], "file": b["file"], "status": block_status(paper_dir, b["file"])} for b in (plan or {}).get("blocks", [])]
    fb = load_feedback(wf)
    hand = json.loads(read_text_safe(wf / "05_hand_edits.json") or "{}").get("files") or []
    try:
        checked = json.loads(read_text_safe(wf / "06_checked_hashes.json") or "{}")
    except json.JSONDecodeError:
        checked = {}
    has_text = any(b["status"] != "empty" for b in blocks)
    st = {
        "effort": effort_level(),
        "sources": len(collect_source_files(list(_wf_args().get("source_paths") or []) + list(_wf_args().get("authority_paths") or []))),
        "plan": "none" if plan is None else ("derived from the .tex files" if plan.get("derived_from_tex") else "01_plan.json"),
        "blocks": blocks,
        "abstract_missing": abstract_missing(paper_dir) if (paper_dir / "main.tex").exists() else True,
        "artifacts": {n: (wf / n).exists() for n in ("00_ground_truth_ledger.json", "01_briefing.md", "01_discussion.md", "01_story.md", "01_decisions.json")},
        "story_locked": lock_valid(wf),
        "rewrite": _load_rewrite(wf),
        "hand_edits": hand,
        "concerns": {s: sum(1 for i in fb["items"] if i.get("status") == s) for s in ("open", "answered", "applied")},
        "blocking": len(open_blocking(fb)),
        "checked_since_last_change": bool(checked) and checked == text_hashes(paper_dir),
    }
    to_write = [b["file"] for b in blocks if b["status"] != "full"] + list(st["rewrite"])
    if st["concerns"]["answered"]:
        nxt = "apply the author's answers to the concerns"
    elif st["blocking"]:
        nxt = "wait for the author's answers to blocking concerns"
    elif not has_text and not st["artifacts"]["01_story.md"]:
        nxt = "briefing, then the story discussion with the author"
    elif not st["story_locked"] and (to_write or st["abstract_missing"]) and st["artifacts"]["01_story.md"]:
        nxt = "confirm and lock the story, then write the missing blocks"
    elif not st["story_locked"] and (to_write or st["abstract_missing"]):
        nxt = "briefing (with the story the draft tells now), then the story discussion with the author"
    elif to_write or st["abstract_missing"]:
        nxt = f"write {len(to_write)} missing or partial block(s)" + (" and the abstract" if st["abstract_missing"] else "")
    elif not st["checked_since_last_change"]:
        nxt = "check, revise and build"
    else:
        nxt = "nothing: the text has not changed since the last check"
    st["next"] = nxt
    (wf / "00_survey.json").write_text(json.dumps(st, indent=2, ensure_ascii=False), encoding="utf-8")
    md = ["# Where the paper stands", "", f"- Effort: {st['effort']}", f"- Source files: {st['sources']}", f"- Block plan: {st['plan']}",
          f"- Story locked: {st['story_locked']}", f"- Abstract missing: {st['abstract_missing']}",
          f"- Hand edits since the last run: {', '.join(hand) or 'none'}",
          f"- Concerns: {st['concerns']['open']} open ({st['blocking']} blocking), {st['concerns']['answered']} answered",
          f"- Next: {nxt}", "", "| block | part | file | status |", "|---|---|---|---|"]
    md += [f"| {b['id']} | {b['part']} | {b['file']} | {b['status']}{' (rewrite)' if b['id'] in st['rewrite'] else ''} |" for b in blocks]
    (paths["audit"] / "00_survey.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    return st


# ------------------------------------------------------------------------------
# Stage 0: material pack
# ------------------------------------------------------------------------------

SCAN_VERSION = "3.1"  # bump when the scan prompt changes, so cached scans are redone


def _file_sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _match_record_file(rec_file: str, files: Sequence[Path]) -> Optional[Path]:
    """Which of a scan job's files a returned record belongs to (agents name files in different ways)."""
    s = str(rec_file or "").replace("\\", "/").strip()
    for f in files:
        fp = str(f).replace("\\", "/")
        if s and (fp == s or fp.endswith("/" + s.lstrip("./")) or s.endswith("/" + f.name) or s == f.name):
            return f
    return files[0] if len(files) == 1 else None


def build_ledger(records: Sequence[Any]) -> Dict[str, Any]:
    """Merge scan records into the ledger by program: ids from the source file, statements copied as scanned.
    (In v3.0 an agent rewrote everything into one JSON; for a real paper that was tens of thousands of output
    tokens, the slowest call of the whole run, and another place where a result could be paraphrased.)"""
    claims: List[Dict[str, Any]] = []
    cited: List[str] = []
    notations: List[str] = []
    used: set = set()
    for rec in records:
        if not isinstance(rec, dict):
            continue
        src = str(rec.get("file") or "notes")
        stem = re.sub(r"[^A-Za-z0-9]+", "-", Path(src.replace("\\", "/")).stem).strip("-")[:24] or "src"
        for k, c in enumerate(rec.get("claims") or [], 1):
            c = c if isinstance(c, dict) else {"statement": str(c)}
            cid = f"{stem}-{k}"
            while cid in used:
                cid += "b"
            used.add(cid)
            lines = str(c.get("lines") or "").strip()
            claims.append({
                "id": cid,
                "kind": str(c.get("kind") or "result"),
                "statement": str(c.get("statement") or c.get("text") or json.dumps(c, ensure_ascii=False)),
                "hypotheses": c.get("hypotheses") or "",
                "source": f"{src}:{lines}" if lines else src,
                "tier": "support",
            })
        for x in rec.get("cited_works") or []:
            if str(x) not in cited:
                cited.append(str(x))
        for x in rec.get("notations") or []:
            if str(x) not in notations:
                notations.append(str(x))
    return {"claims": claims, "cited_works": cited, "notations": notations}


def apply_judgement(ledger: Dict[str, Any], judgement: Dict[str, Any]) -> Dict[str, Any]:
    """Tiers and duplicates decided by the judging agent, applied by program (statements are never rewritten)."""
    claims = list(ledger.get("claims") or [])
    by_id = {c["id"]: c for c in claims}
    core = {str(x) for x in judgement.get("core") or [] if str(x) in by_id}
    drop: Dict[str, str] = {}
    for grp in judgement.get("duplicates") or []:
        ids = [str(x) for x in (grp or []) if str(x) in by_id]
        for d in ids[1:]:
            if d != ids[0] and ids[0] not in drop:
                drop[d] = ids[0]
    for d, keep in drop.items():
        by_id[keep].setdefault("also_in", []).append(by_id[d]["source"])
        if d in core:
            core.add(keep)
    kept = [c for c in claims if c["id"] not in drop]
    for c in kept:
        c["tier"] = "core" if c["id"] in core else "support"
    return {**ledger, "claims": kept}


async def run_inventory(paths: Dict[str, Path]) -> Dict[str, Any]:
    wf = paths["wf"]
    args = _wf_args()
    skipped_sources: List[str] = []
    authority_files = collect_source_files(args.get("authority_paths") or [], skipped_sources)
    source_files = collect_source_files(args.get("source_paths") or [], skipped_sources)
    if not authority_files and not source_files:
        # A paper that exists without its notes: its own text is the material (results only it states are then
        # checked against nothing else, so the checkers say so).
        source_files = list_editable_tex(paths["paper"])
        if not source_files:
            raise RuntimeError("inventory: nothing to work from; give source_paths (the notes) or a paper directory that has text")
        log(f"Stage 0: no notes given; the material pack is built from the paper's own {len(source_files)} .tex files")
    auth_set = {f.resolve() for f in authority_files}
    other_files = [f for f in source_files if f.resolve() not in auth_set]
    exemplar_paths = [str(Path(x)) for x in (args.get("exemplar_paths") or [])]
    if skipped_sources:
        log(f"Stage 0: skipped {len(skipped_sources)} source paths (missing, or too large to be notes): {skipped_sources[:8]}")

    # Unchanged files are not scanned again: their records come from the cache.
    cache_path = wf / "00_scan_cache.json"
    try:
        cache = json.loads(read_text_safe(cache_path) or "{}")
    except json.JSONDecodeError:
        cache = {}
    if cache.get("version") != SCAN_VERSION:
        cache = {"version": SCAN_VERSION, "files": {}}
    all_inputs = authority_files + other_files
    shas = {str(f.resolve()): _file_sha(f) for f in all_inputs}
    cache["files"] = {k: v for k, v in (cache.get("files") or {}).items() if k in shas}

    def fresh(f: Path) -> bool:
        ent = cache["files"].get(str(f.resolve()))
        return bool(ent) and ent.get("sha") == shas[str(f.resolve())]

    stale_auth = [f for f in authority_files if not fresh(f)]
    stale_other = [f for f in other_files if not fresh(f)]

    # One scan agent per changed authority file; other changed sources are packed into ~60 KB chunks. Refuse to fan
    # out beyond max_scan_agents instead of silently launching hundreds of paid agents on a data directory.
    scan_jobs: List[Tuple[List[Path], bool]] = [([f], True) for f in stale_auth] + [(c, False) for c in chunk_source_files(stale_other)]
    max_jobs = int(_STATE.get("max_scan_agents") or 24)
    if len(scan_jobs) > max_jobs:
        raise RuntimeError(
            f"Stage 0 would launch {len(scan_jobs)} scan agents for {len(stale_auth)} authority + {len(stale_other)} source files "
            f"(limit max_scan_agents={max_jobs}). Narrow --source-paths to the notes/reports that matter (data directories are not "
            f"needed), or raise --max-scan-agents deliberately."
        )

    phase("Stage 0: Material Pack (results ledger, red lines, story material, voice samples)")
    if len(all_inputs) > len(stale_auth) + len(stale_other):
        log(f"Stage 0: {len(all_inputs) - len(stale_auth) - len(stale_other)} unchanged source files reuse their cached scans")
    profile_path = wf / "00_style_profile.json"
    ex_key = sorted([str(Path(x).resolve()), _file_sha(Path(x))] for x in exemplar_paths if Path(x).is_file())
    try:
        has_samples = bool(json.loads(read_text_safe(profile_path) or "{}").get("categorized_exemplars"))
    except json.JSONDecodeError:
        has_samples = False
    if exemplar_paths and not (cache.get("exemplars") == ex_key and has_samples):
        rc, _, _, _ = run_tool("style_metrics.py", ["profile", "--inputs", *exemplar_paths, "--out", str(profile_path), "--no-lm"], allow_fail=True)
        log(f"style_metrics.py profile exited {rc}")
        ensure_default_profile(paths)
        vet_res = await dispatch_agent(
            f"Read the exemplar papers {exemplar_paths}. Copy verbatim 5 to 8 paragraphs worth imitating into `{profile_path}`, under "
            f"`categorized_exemplars`: an object with the keys `opening`, `history`, `result_statement`, `mechanism` and `discussion`, each a "
            f"list of paragraphs (leave a list empty when the papers have no good example). Leave the other keys of the file as they are.",
            schema={"type": "object", "properties": {"curated_count": {"type": "integer"}}, "required": ["curated_count"]},
            role="Curate Voice Samples",
            stage_name="inventory",
        )
        log(f"Voice samples curated: {vet_res}")
        cache["exemplars"] = ex_key
    ensure_default_profile(paths)

    claim_item = {
        "type": "object",
        "properties": {"statement": {"type": "string"}, "hypotheses": {"type": "string"}, "kind": {"type": "string"}, "lines": {"type": "string"}},
        "required": ["statement"],
    }
    scan_schema = {
        "type": "object",
        "properties": {
            "file": {"type": "string"},
            "claims": {"type": "array", "items": claim_item},
            "redlines_and_decisions": {"type": "array", "items": {"type": "string"}},
            "story_material": {"type": "array", "items": {"type": "string"}},
            "notations": {"type": "array", "items": {"type": "string"}},
            "cited_works": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["file", "claims"],
    }
    scan_list_schema = {"type": "object", "properties": {"records": {"type": "array", "items": scan_schema}}, "required": ["records"]}
    job_results = await run_batched(
        scan_jobs,
        lambda job: dispatch_agent(
            f"Read each of these files thoroughly (authority_doc={job[1]}): {[str(p) for p in job[0]]}.\n"
            f"Return one record per file (`file` = its path as given) with:\n"
            f"1. `claims`: every result (theorem, lemma, proposition, numerical value or finding), each with its exact `statement`, its "
            f"`hypotheses` and quantifiers (`with multiplicity`, translation invariance, finite range, boundary conditions), a `kind` "
            f"(theorem / proposition / number / definition / observation) and its `lines`. A number's statement names the exact Hamiltonian, "
            f"observable or residual it belongs to (never conflate bounds or models).\n"
            f"2. `redlines_and_decisions`: the author's decisions and prohibitions (what must not be claimed or mentioned), verbatim.\n"
            f"3. `story_material`: why the author cared, physical pictures and analogies, surprises, history and prior work mentioned, open questions (with line spans).\n"
            f"4. `notations` and `cited_works`.",
            schema=scan_list_schema,
            role=f"Scan {job[0][0].name}" + (f" (+{len(job[0]) - 1})" if len(job[0]) > 1 else ""),
            stage_name="inventory",
            tier="heavy",
            read_only=True,
        ),
    ) if scan_jobs else []
    unmatched: List[Any] = []
    for job, jr in zip(scan_jobs, job_results):
        matched: Dict[str, List[Any]] = {}
        for rec in _require_fields(jr, ["records"], "Stage 0 scan")["records"]:
            f = _match_record_file(str(rec.get("file") or ""), job[0]) if isinstance(rec, dict) else None
            if f is None:
                unmatched.append(rec)  # used now; its files stay uncached and are scanned again next time
            else:
                matched.setdefault(str(f.resolve()), []).append(rec)
        for key, recs in matched.items():
            cache["files"][key] = {"sha": shas[key], "records": recs}
    cache["judged"] = cache.get("judged") if not scan_jobs else False
    cache_path.write_text(json.dumps(cache, indent=1, ensure_ascii=False), encoding="utf-8")

    scans = [r for f in all_inputs for r in (cache["files"].get(str(f.resolve())) or {}).get("records", [])] + unmatched
    scans_path = wf / "00_scans.json"
    scans_path.write_text(json.dumps(scans, indent=2, ensure_ascii=False), encoding="utf-8")

    ledger_path = wf / "00_ground_truth_ledger.json"
    redlines_path = wf / "00_redlines.md"
    story_path = wf / "00_story_material.md"
    conflicts_path = wf / "00_conflicts_report.md"
    hints_path = wf / "00_cited_seed_hints.json"
    outputs = (ledger_path, redlines_path, story_path, conflicts_path, hints_path)
    if cache.get("judged") and all(p.exists() for p in outputs):
        log("Stage 0: sources unchanged since the last run; the material pack is reused as it is")
        hints = json.loads(read_text_safe(hints_path) or "[]")
        return {"ledger": str(ledger_path), "redlines": str(redlines_path), "story_material": str(story_path), "conflicts": str(conflicts_path),
                "profile": str(profile_path), "cited_seed_hints": hints, "skipped_sources": skipped_sources, "rescanned_jobs": 0, "reused": True}

    ledger = build_ledger(scans)
    ledger_path.write_text(json.dumps(ledger, indent=2, ensure_ascii=False), encoding="utf-8")
    judgement = await dispatch_agent(
        f"The workflow merged {len(ledger['claims'])} results from the notes into `{ledger_path}` (ids assigned, statements copied as "
        f"scanned). Read it and the scan records in `{scans_path}`. Do not rewrite the ledger. Write three short files:\n"
        f"1. `{redlines_path}`: the author's decisions and prohibitions, one per line, exact wording, with the source. Keep it short: it is "
        f"the only rule list the writers see.\n"
        f"2. `{story_path}`: the story material (motivation, physical pictures, surprises, history and prior work as the notes give it, open "
        f"questions), with source pointers.\n"
        f"3. `{conflicts_path}`: notation and convention conflicts between the sources (signs, normalisations, symbols).\n"
        f"Then answer with `core` (ids of the results that could headline the paper or belong in its main text), `duplicates` (groups of ids "
        f"that state the same result, the most complete statement first) and `cited_seed_hints` (works the notes cite, as title words, first "
        f"author, year).",
        schema={
            "type": "object",
            "properties": {
                "core": {"type": "array", "items": {"type": "string"}},
                "duplicates": {"type": "array", "items": {"type": "array", "items": {"type": "string"}}},
                "cited_seed_hints": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["core", "duplicates", "cited_seed_hints"],
        },
        role="Judge Material Pack",
        stage_name="inventory",
        tier="heavy",
    )
    judgement = _require_fields(judgement, ["core", "duplicates"], "Stage 0 judgement")
    ledger = apply_judgement(ledger, judgement)
    ledger_path.write_text(json.dumps(ledger, indent=2, ensure_ascii=False), encoding="utf-8")
    hints = [str(h) for h in (judgement.get("cited_seed_hints") or [])]
    # Persisted: the story stage usually runs as a separate process after the author looked at the pack.
    hints_path.write_text(json.dumps(hints, indent=2, ensure_ascii=False), encoding="utf-8")
    for must in (redlines_path, story_path):
        if not must.exists():
            raise RuntimeError(f"Stage 0 judgement did not write {must}")
    if not conflicts_path.exists():
        conflicts_path.write_text("No notation conflicts reported.\n", encoding="utf-8")
    cache["judged"] = True
    cache_path.write_text(json.dumps(cache, indent=1, ensure_ascii=False), encoding="utf-8")
    n_core = sum(1 for c in ledger["claims"] if c["tier"] == "core")
    log(f"Stage 0 complete: {len(ledger['claims'])} results ({n_core} core) from {len(scans)} records; {len(scan_jobs)} scan jobs this run")
    return {"ledger": str(ledger_path), "redlines": str(redlines_path), "story_material": str(story_path), "conflicts": str(conflicts_path),
            "profile": str(profile_path), "cited_seed_hints": hints, "skipped_sources": skipped_sources, "rescanned_jobs": len(scan_jobs), "reused": False}


# ------------------------------------------------------------------------------
# Stage 1: lineage, story, block plan
# ------------------------------------------------------------------------------

LINEAGE_SCHEMA = {
    "type": "object",
    "properties": {
        "steps": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "year": {"type": "string"}, "who": {"type": "string"}, "question": {"type": "string"}, "found": {"type": "string"},
                    "left_open": {"type": "string"}, "doi": {"type": "string"}, "arxiv": {"type": "string"}, "title": {"type": "string"},
                },
                "required": ["who", "question", "found"],
            },
        },
        "this_paper": {"type": "string"},
    },
    "required": ["steps"],
}


def verify_lineage_refs(steps: List[Dict[str, Any]], seed_bib: Path, cache: Path) -> int:
    """Verify each step's reference with verify_citations.py (identifier plus title). The workflow runs the
    check itself instead of trusting an agent's word; only verified BibTeX enters the seed .bib."""
    existing = read_text_safe(seed_bib)
    have = set(re.findall(r"@\w+\s*\{\s*([^,\s]+)\s*,", existing))
    new_entries: List[str] = []
    for s in steps:
        s["verified"] = False
        ident, flag = str(s.get("doi") or "").strip(), "--doi"
        if not ident:
            ident, flag = str(s.get("arxiv") or "").strip(), "--arxiv"
        if not ident:
            continue
        cli = ["verify-entry", flag, ident, "--cache", str(cache)]
        if str(s.get("title") or "").strip():
            cli += ["--title", str(s["title"])]
        _, _, _, res = run_tool("verify_citations.py", cli, allow_fail=True, timeout_s=300)
        if not (res and res.get("verified") and res.get("bibtex")):
            continue
        s["verified"] = True
        md = res.get("metadata") or {}
        authors = md.get("authors") if isinstance(md.get("authors"), list) else []
        s["meta"] = {"title": str(md.get("title") or s.get("title") or ""), "year": str(md.get("year") or s.get("year") or ""),
                     "authors": [str(a) for a in authors[:3]], "abstract": str(md.get("abstract") or ""),
                     "arxiv": str(md.get("arxiv") or s.get("arxiv") or "")}
        bib = str(res["bibtex"]).strip()
        km = re.match(r"@\w+\s*\{\s*([^,\s]+)", bib)
        if km:
            s["cite_key"] = km.group(1)
            if km.group(1) in have:
                continue
            have.add(km.group(1))
        new_entries.append(bib)
    if new_entries:
        seed_bib.write_text((existing.strip() + "\n\n" if existing.strip() else "") + "\n\n".join(new_entries) + "\n", encoding="utf-8")
    return len(new_entries)


def render_lineage(steps: List[Dict[str, Any]], this_paper: str) -> str:
    lines = ["# Lineage", "", "Each reference was checked with verify_citations.py. Cite verified ones by their key; an unverified "
             "step may be told, but its citation stays \\cite{TODO:...} until someone verifies it.", ""]
    for i, s in enumerate(steps, 1):
        ref = _ref_tag(s)
        lines.append(f"{i}. **{s.get('year', '')} {s.get('who', '')}** {ref}")
        lines.append(f"   - Question: {s.get('question', '')}")
        lines.append(f"   - Found: {s.get('found', '')}")
        if s.get("left_open"):
            lines.append(f"   - Left open: {s['left_open']}")
    if this_paper:
        lines += ["", f"**This paper:** {this_paper}"]
    return "\n".join(lines) + "\n"


NEIGHBOUR_WORK = {
    "type": "object",
    "properties": {"who": {"type": "string"}, "year": {"type": "string"}, "title": {"type": "string"}, "doi": {"type": "string"},
                   "arxiv": {"type": "string"}, "note": {"type": "string"}, "relation": {"type": "string"}},
    "required": ["title"],
}
NEIGHBOUR_SCHEMA = {
    "type": "object",
    "properties": {
        "clusters": {"type": "array", "items": {"type": "object", "properties": {
            "name": {"type": "string"}, "what_they_do": {"type": "string"}, "limitation": {"type": "string"},
            "relation": {"type": "string"}, "works": {"type": "array", "items": NEIGHBOUR_WORK}}, "required": ["name", "works"]}},
        "concurrent": {"type": "array", "items": NEIGHBOUR_WORK},
    },
    "required": ["clusters"],
}
STORY_SCHEMA = {
    "type": "object",
    "properties": {"rewrite": {"type": "array", "items": {"type": "object", "properties": {"block": {"type": "string"}, "why": {"type": "string"}}, "required": ["block"]}}},
    "required": ["rewrite"],
}


def _ref_tag(w: Dict[str, Any]) -> str:
    ident = w.get("doi") or w.get("arxiv") or "no identifier"
    check = f" {w['check']}" if w.get("check") else ""
    if w.get("verified"):
        return (f"[verified: {w['cite_key']}]" if w.get("cite_key") else "[verified]") + check
    return f"[unverified: {ident}]"


def render_literature_map(steps: List[Dict[str, Any]], this_paper: str, clusters: List[Dict[str, Any]], concurrent: List[Dict[str, Any]]) -> str:
    lines = [render_lineage(steps, this_paper).rstrip(), ""]
    if clusters:
        lines += ["# Neighbouring work", ""]
        for c in clusters:
            lines.append(f"## {c.get('name', '')}")
            for k, lab in (("what_they_do", "What they do"), ("limitation", "Where they stop"), ("relation", "Relation to this paper")):
                if c.get(k):
                    lines.append(f"- {lab}: {c[k]}")
            for w in c.get("works") or []:
                lines.append(f"  - {w.get('who', '')} {w.get('year', '')}: {w.get('title', '')} {_ref_tag(w)}" + (f" ({w['note']})" if w.get("note") else ""))
            lines.append("")
    if concurrent:
        lines += ["# Concurrent or later work (never prior work)", ""]
        for w in concurrent:
            lines.append(f"- {w.get('who', '')} {w.get('year', '')}: {w.get('title', '')} {_ref_tag(w)}" + (f" ({w['relation']})" if w.get("relation") else ""))
    return "\n".join(lines) + "\n"


def _seed_and_hints(paths: Dict[str, Path], inv_info: Optional[Dict[str, Any]]) -> Tuple[Path, List[str]]:
    wf = paths["wf"]
    seed_bib_path = wf / "01_verified_seed.bib"
    existing_bib = _wf_args().get("existing_bib")
    if existing_bib and Path(existing_bib).exists():
        copy_to_workflow(Path(existing_bib), seed_bib_path)
    elif not seed_bib_path.exists():
        seed_bib_path.write_text("", encoding="utf-8")
    hints_src = (inv_info or {}).get("cited_seed_hints")
    hints_file = wf / "00_cited_seed_hints.json"
    if hints_src is None and hints_file.exists():
        try:
            hints_src = json.loads(read_text_safe(hints_file))
        except json.JSONDecodeError:
            hints_src = []
    return seed_bib_path, [str(h) for h in (hints_src or [])][:20]


def _work_from_meta(w: Dict[str, Any]) -> Dict[str, Any]:
    md = w.get("meta") or {}
    return {"key": w["cite_key"], "title": md.get("title") or w.get("title", ""), "year": md.get("year") or w.get("year", ""),
            "authors": md.get("authors") or [], "abstract": md.get("abstract") or "(no abstract available)",
            "arxiv": md.get("arxiv") or w.get("arxiv", ""), "literature_map": ""}


async def check_literature_claims(paths: Dict[str, Path], steps: List[Dict[str, Any]], clusters: List[Dict[str, Any]],
                                  concurrent: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """What the map says each verified work did is checked like a citation (abstract first, the lineage steps and
    concurrent works in full when the abstract cannot settle it), so the author decides on checked facts."""
    if not effort()["cite_context_batch"]:
        return None
    ctx: List[Dict[str, Any]] = []
    owners: List[Dict[str, Any]] = []
    for i, s in enumerate(steps, 1):
        if s.get("verified") and s.get("cite_key"):
            ctx.append({"file": "01_literature_map.md", "line": f"lineage {i}", "key_context": True, "works": [_work_from_meta(s)],
                        "sentence": f"{s.get('who', '')} ({s.get('year', '')}) asked: {s.get('question', '')} Found: {s.get('found', '')}"
                                    + (f" Left open: {s['left_open']}" if s.get("left_open") else "")})
            owners.append(s)
    for c in clusters:
        for w in c.get("works") or []:
            if w.get("verified") and w.get("cite_key"):
                ctx.append({"file": "01_literature_map.md", "line": f"cluster {c.get('name', '')}", "works": [_work_from_meta(w)],
                            "sentence": f"{c.get('name', '')}: {c.get('what_they_do', '')} Where they stop: {c.get('limitation', '')}"
                                        + (f" This work: {w['note']}" if w.get("note") else "")})
                owners.append(w)
    for w in concurrent:
        if w.get("verified") and w.get("cite_key"):
            ctx.append({"file": "01_literature_map.md", "line": "concurrent", "key_context": True, "works": [_work_from_meta(w)],
                        "sentence": f"Concurrent with or later than this paper's results: {w.get('relation', '')}"})
            owners.append(w)
    if not ctx:
        return None
    jobs, state = coarse_citation_jobs(paths, ctx, role="Literature Check")
    res = await finish_citation_check(paths, state, await run_batched(jobs, lambda j: j[1]()) if jobs else [], "08_literature_map_check.md")
    by_line = {f"c{i}:{c['works'][0]['key']}": owner for i, (c, owner) in enumerate(zip(ctx, owners), 1)}
    for v in res["verdicts"]:
        owner = by_line.get(v["id"])
        if owner is None:
            continue
        where = f", {v['location']}" if v.get("location") else ""
        if v["verdict"] == "supported":
            owner["check"] = f"[claim confirmed: {v['level']}{where}]"
        elif v["verdict"] == "unclear":
            owner["check"] = f"[claim unclear: {v['level']}{', full text unavailable' if v.get('fulltext') == 'unavailable' else ''}]"
        else:
            owner["check"] = f"[claim not supported: {v['verdict'].replace('_', ' ')}: {v.get('problem', '')}]"
    return {k: res[k] for k in ("reused", "fulltext_read", "fulltext_unavailable", "report")}


async def run_brief(paths: Dict[str, Path], inv_info: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """What the author needs to decide the story: results with their evidence, a verified literature map and the
    open questions. Information only: no storyline and no recommendation (the author speaks first)."""
    wf = paths["wf"]
    paper_dir = paths["paper"]
    genre = str(_wf_args().get("genre") or "long")
    sm_word = "Supplemental Material" if genre == "letter" else "appendices"
    phase("Briefing: results, literature map and open questions for the author")
    refs = stage_refs(paths)
    seed_bib_path, hints = _seed_and_hints(paths, inv_info)
    ledger_path = wf / "00_ground_truth_ledger.json"
    material_path = wf / "00_story_material.md"
    if not ledger_path.exists():
        raise RuntimeError(f"brief: {ledger_path} is missing; run mode=inventory first")
    vc_query = f"{resolve_python_exe()} {TOOLS_DIR / 'verify_citations.py'} verify-entry --query"
    hint_txt = f" Works the notes cite: {json.dumps(hints, ensure_ascii=False)}." if hints else ""
    find_rule = (f"For each work, find it with `{vc_query} \"<title words, first author, year>\"` (this only lists candidates) and report the DOI "
                 f"or arXiv id and the exact title of the right candidate; the workflow verifies them itself. Leave out what you cannot identify "
                 f"that way, and never invent a reference.")
    eff = effort()
    jobs: List[Tuple[str, Callable[[], Any]]] = [("lineage", lambda: dispatch_agent(
        f"Trace the history that leads to this paper's question. Read `{material_path}` and `{ledger_path}`.{hint_txt}\n"
        f"Give {eff['lineage_steps']} steps of earlier work in time order (who, when, what question they asked, what they found, what they left "
        f"open), and put the step this paper takes in `this_paper`, not among the steps. {find_rule}",
        schema=LINEAGE_SCHEMA, role="Lineage", stage_name="story", tier="heavy"))]
    if eff["neighbour_clusters"]:
        jobs.append(("neighbours", lambda: dispatch_agent(
            f"Map the work around this paper's question. Read `{material_path}` and `{ledger_path}`.{hint_txt}\n"
            f"1. `clusters`: {eff['neighbour_clusters']} groups of neighbouring work a referee would compare this paper with. For each: what "
            f"they do, where they stop (the limitation that matters here), how this paper relates to them, and its key works.\n"
            f"2. `concurrent`: work that appeared at the same time as these results or later and overlaps them; it is concurrent work, never "
            f"prior work.\n{find_rule}",
            schema=NEIGHBOUR_SCHEMA, role="Neighbouring Work", stage_name="story", tier="heavy")))
    results = dict(zip([j[0] for j in jobs], await run_batched(jobs, lambda j: j[1]())))
    lineage = _require_fields(results["lineage"], ["steps"], "Lineage")
    steps = [s for s in (lineage.get("steps") or []) if isinstance(s, dict)]
    nb = results.get("neighbours") if isinstance(results.get("neighbours"), dict) else {}
    clusters = [c for c in (nb.get("clusters") or []) if isinstance(c, dict)]
    concurrent = [w for w in (nb.get("concurrent") or []) if isinstance(w, dict)]
    works = [w for c in clusters for w in (c.get("works") or []) if isinstance(w, dict)] + concurrent
    cache = wf / "04_api_bib_cache.json"
    added = verify_lineage_refs(steps, seed_bib_path, cache) + verify_lineage_refs(works, seed_bib_path, cache)
    lit_check = await check_literature_claims(paths, steps, clusters, concurrent)
    this_paper = str(lineage.get("this_paper") or "")
    lineage_path = wf / "01_lineage.md"
    lineage_path.write_text(render_lineage(steps, this_paper), encoding="utf-8")
    litmap_path = wf / "01_literature_map.md"
    litmap_path.write_text(render_literature_map(steps, this_paper, clusters, concurrent), encoding="utf-8")
    n_ver = sum(1 for x in steps + works if x.get("verified"))
    log(f"Literature map: {len(steps)} lineage steps, {len(clusters)} neighbouring clusters, {len(concurrent)} concurrent works; "
        f"{n_ver} references verified, {added} added to the seed .bib")

    plan = current_plan(paths)
    written = [(b["file"], block_status(paper_dir, b["file"])) for b in (plan or {}).get("blocks", [])]
    written = [(f, s) for f, s in written if s != "empty"]
    draft_txt = (f" The paper already has text ({', '.join(f'{f}: {s}' for f, s in written)}; main file `{paper_dir / 'main.tex'}`)." if written else "")
    brief_path = wf / "01_briefing.md"
    sections = [
        "Results: the core results in logical order (what rests on what). For each: its ledger id, one plain sentence, the evidence "
        "(proven, numerical or conjectured, with the source), its scope, and what is new against the literature map (with the references).",
        "Literature: the lineage and the neighbouring work in a few lines each; concurrent work separately.",
    ]
    if written:
        sections.append("The draft as it stands: the story the existing text tells now (abstract, introduction, order of sections), quoted briefly.")
    sections += [
        "Limits: the venue's limits for this genre and the author's red lines.",
        f"Open questions for the author: the decisions the paper needs (the headline, the framing and the terms, main text versus {sm_word}, "
        "what to leave out, anything the material leaves undecided), as questions only.",
    ]
    res = await dispatch_agent(
        f"Write `{brief_path}`: the briefing the author reads before deciding the story and the structure of the paper (genre: {genre}). It gives "
        "information, not advice: do not propose a storyline, do not rank the results by importance, and do not recommend. The author says "
        "what they think first; the AI's own view comes afterwards, in conversation.\n"
        f"Read the ledger `{ledger_path}`, the story material `{material_path}`, the red lines `{wf / '00_redlines.md'}`, the notation "
        f"conflicts `{wf / '00_conflicts_report.md'}`, the literature map `{litmap_path}` and the genre notes "
        f"`{refs.get('genre_templates.md', REFS_DIR / 'genre_templates.md')}`.{draft_txt} In the map, a claim about a work marked "
        "[claim not supported ...] or [claim unclear ...] is not a fact: leave it out or say it is unconfirmed.\nSections:\n"
        + "\n".join(f"{k}. {s}" for k, s in enumerate(sections, 1))
        + "\nPlain language, readable in ten minutes. Reply with the number of open questions.",
        schema={"type": "object", "properties": {"open_questions": {"type": "integer"}}, "required": ["open_questions"]},
        role="Briefing", stage_name="story", tier="heavy",
    )
    if not brief_path.exists():
        raise RuntimeError(f"brief: the briefing agent did not write {brief_path}")
    return {"brief": str(brief_path), "literature_map": str(litmap_path), "lineage_steps": len(steps), "clusters": len(clusters),
            "concurrent": len(concurrent), "verified_refs": n_ver, "literature_check": lit_check,
            "open_questions": res.get("open_questions") if isinstance(res, dict) else None}


async def run_story(paths: Dict[str, Path], inv_info: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Story, block plan and decision record, written from the author's discussion of the briefing (or, in an
    unattended run, chosen by the agent and marked as such)."""
    wf = paths["wf"]
    paper_dir = paths["paper"]
    genre = str(_wf_args().get("genre") or "long")
    sm_word = "Supplemental Material" if genre == "letter" else "appendices"
    phase("Story, block plan and decision record")
    refs = stage_refs(paths)
    ledger_path = wf / "00_ground_truth_ledger.json"
    if not ledger_path.exists():
        raise RuntimeError(f"story: {ledger_path} is missing; run mode=inventory first")
    litmap_path = wf / "01_literature_map.md"
    if not litmap_path.exists():
        log("[story] no literature map yet; writing the briefing first")
        await run_brief(paths, inv_info)
    brief_path = wf / "01_briefing.md"
    disc_path = wf / "01_discussion.md"
    story_path = wf / "01_story.md"
    decisions_path = wf / "01_decisions.json"
    plan_path = _plan_path()
    macros_path = paper_dir / "macros.tex"
    plan_now = current_plan(paths)
    written = [f"{b['file']} ({block_status(paper_dir, b['file'])})" for b in (plan_now or {}).get("blocks", [])
               if block_status(paper_dir, b["file"]) != "empty"]
    disc_txt = (
        f"The author discussed the briefing with the AI; the agreed points are in `{disc_path}`. Follow them. Where they say nothing, choose "
        "yourself and list those choices in the story under the heading 'Filled in by the AI', so the author sees them.\n"
        if disc_path.exists() else
        "Nobody discussed the briefing (an unattended run): choose yourself, and give every decision the source \"ai\".\n"
    )
    existing_txt = (f"The paper already has text: {', '.join(written)}. Keep a block's existing file name where the block still serves the "
                    "plan. A complete block is not rewritten unless you list it under `rewrite` with the reason.\n" if written else "")
    res = await dispatch_agent(
        f"Write the story of this paper. Read the briefing `{brief_path}`, the ledger `{ledger_path}`, the story material "
        f"`{wf / '00_story_material.md'}`, the literature map `{litmap_path}`, the red lines `{wf / '00_redlines.md'}`, the notation "
        f"conflicts `{wf / '00_conflicts_report.md'}` and the genre notes `{refs.get('genre_templates.md', REFS_DIR / 'genre_templates.md')}` "
        f"(genre: {genre}).\n{disc_txt}{existing_txt}Write four files.\n"
        f"1. `{story_path}`: one page in plain language: the question in one sentence; the history that leads to it; why it is still open; "
        f"the answer in plain words; why it matters; what goes into the main text, what goes to the {sm_word} and what is left out (by "
        f"ledger id); the block plan in prose; two or three title candidates.\n"
        f"2. `{plan_path}`: the same plan as strict JSON: {{\"title\": str, \"blocks\": [{{\"id\": str, \"part\": \"main\" or \"sm\", "
        f"\"file\": \"sections/<name>.tex\", \"purpose\": one sentence, \"claim_ids\": [ledger ids], \"figure_ids\": [figure ids], "
        f"\"target_words\": int}}], \"figures\": [{{\"id\": str, \"purpose\": str, \"claim_ids\": [ledger ids]}}]}}. Main-text blocks follow "
        f"the logical beats of the story in reading order, the question and its history first; {sm_word} blocks hold the proofs and technical "
        f"detail. Name results only by ledger id; never restate them in the plan.\n"
        f"3. `{decisions_path}`: the decision record as strict JSON: {{\"decisions\": [{{\"id\": \"D1\", \"topic\": str, \"choice\": str, "
        f"\"reason\": str, \"basis\": what it rests on (ledger ids, references, the author's words), \"unknown_then\": what was not known "
        f"when it was made, \"affects\": [block ids], \"source\": \"author\" or \"ai\", \"version\": 1}}]}}, one entry per real choice (the "
        f"headline, the framing and the terms, main text versus {sm_word}, what is left out).\n"
        f"4. `{macros_path}`: LaTeX macros for the notation, resolving the conflicts (if the file exists, keep it and add only what is "
        f"missing); a run-in heading macro must begin with `\\par`.\n"
        "Reply with `rewrite`: existing blocks whose text must be rewritten for this story (block id and why); an empty list when none.",
        schema=STORY_SCHEMA,
        role="Story and Plan",
        stage_name="story",
        tier="heavy",
    )
    plan = await _ensure_valid_plan(plan_path, ledger_path)
    ids = {b["id"] for b in plan["blocks"]}
    rewrite = [r for r in ((res or {}).get("rewrite") if isinstance(res, dict) else None) or [] if isinstance(r, dict) and str(r.get("block")) in ids]
    if rewrite:
        mark_rewrite(wf, rewrite)
    if not decisions_path.exists():
        decisions_path.write_text(json.dumps({"decisions": []}, indent=2), encoding="utf-8")
    n_main = sum(1 for b in plan["blocks"] if b["part"] == "main")
    log(f"Story stage complete: {n_main} main-text blocks, {len(plan['blocks']) - n_main} {sm_word} blocks, "
        f"{len(load_decisions(wf))} decisions recorded{f', {len(rewrite)} existing block(s) to rewrite' if rewrite else ''}.")
    return {"story": str(story_path), "plan": str(plan_path), "decisions": str(decisions_path), "macros": str(macros_path),
            "main_blocks": n_main, "sm_blocks": len(plan["blocks"]) - n_main, "rewrite": [r["block"] for r in rewrite],
            "from_discussion": disc_path.exists()}


async def apply_feedback(paths: Dict[str, Path]) -> Dict[str, Any]:
    """The author answered concerns. Answers that keep a decision change nothing; the others update the story,
    plan and decision record (one agent call), and the blocks they touch are rewritten by the next draft."""
    wf = paths["wf"]
    data = load_feedback(wf)
    answered = [i for i in data["items"] if i.get("status") == "answered"]
    if not answered:
        return {"applied": 0}
    changes = [i for i in answered if not i.get("keep")]
    rewrite: List[Dict[str, Any]] = []
    if changes:
        phase(f"Apply the author's answers to {len(changes)} concern(s)")
        current_plan(paths, save=True)
        ledger_path = wf / "00_ground_truth_ledger.json"
        brief = [{k: i.get(k) for k in ("id", "finding", "evidence", "decision_id", "options", "answer")} for i in changes]
        res = await dispatch_agent(
            f"The author answered these concerns about the paper:\n{json.dumps(brief, indent=1, ensure_ascii=False)}\n"
            f"Update the story `{wf / '01_story.md'}` (if it exists), the block plan `{_plan_path()}` and the decision record "
            f"`{wf / '01_decisions.json'}` (create it if missing, as {{\"decisions\": [...]}}) to follow the answers. For a changed decision raise "
            f"its `version`, update `choice`, `reason` and `basis`, and add a short `history` entry (date, what changed, why). Change nothing "
            f"the answers do not touch; keep the JSON strict and every claim id in the ledger `{ledger_path}`.\n"
            "Reply with `rewrite`: the blocks whose text must change (block id and why, naming the answer); an empty list when none.",
            schema=STORY_SCHEMA, role="Apply Answers", stage_name="story", tier="heavy",
        )
        plan = await _ensure_valid_plan(_plan_path(), ledger_path)
        ids = {b["id"] for b in plan["blocks"]}
        rewrite = [r for r in ((res or {}).get("rewrite") if isinstance(res, dict) else None) or [] if isinstance(r, dict) and str(r.get("block")) in ids]
        mark_rewrite(wf, rewrite)
        if (wf / "01_lock.json").exists():
            await lock_story(paths)  # the author's answer confirms the updated story
    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    for i in answered:
        i["status"], i["applied"] = "applied", stamp
    save_feedback(paths, data)
    log(f"[concerns] applied {len(answered)} answer(s); {len(changes)} changed the story or a decision; blocks to rewrite: {[r['block'] for r in rewrite]}")
    return {"applied": len(answered), "changed": len(changes), "rewrite": [r["block"] for r in rewrite]}


# ------------------------------------------------------------------------------
# Stage 2: drafting (one author for the main text, block by block; SM blocks in parallel)
# ------------------------------------------------------------------------------

BLOCK_SCHEMA = {
    "type": "object",
    "properties": {"file": {"type": "string"}, "words": {"type": "integer"}, "todos": {"type": "array", "items": {"type": "string"}},
                   "concerns": {"type": "array", "items": CONCERN_ITEM}},
    "required": ["file"],
}


def write_skeleton(tex_path: Path, files: Sequence[str], genre: str, title: str, supplement: bool) -> bool:
    """Write main.tex / supplement.tex when absent: preamble, placeholders and the block inputs in plan order."""
    if tex_path.exists():
        return False
    opts = "aps,prl,twocolumn,superscriptaddress,longbibliography" if (genre == "letter" and not supplement) else "aps,prb,twocolumn,superscriptaddress,longbibliography"
    shown_title = ("Supplemental Material: " + title) if (supplement and title) else (title or "TITLE")
    text = (
        "\\documentclass[" + opts + "]{revtex4-2}\n\\usepackage{amsmath,amssymb,graphicx}\n\\input{macros.tex}\n"
        "\\providecommand{\\todo}[1]{\\textbf{[TODO: #1]}}\n\n\\begin{document}\n\n"
        "\\title{" + shown_title + "}\n\\author{[Authors]}\n\\date{\\today}\n\n"
    )
    if not supplement:
        text += "\\begin{abstract}\nABSTRACT\n\\end{abstract}\n\n"
    text += "\\maketitle\n\n" + "\n".join("\\input{" + f + "}" for f in files) + "\n\n"
    if not supplement:
        text += "\\begin{acknowledgments}\n[Acknowledgments]\n\\end{acknowledgments}\n\n"
    text += "\\bibliography{refs}\n\n\\end{document}\n"
    tex_path.parent.mkdir(parents=True, exist_ok=True)
    tex_path.write_text(text, encoding="utf-8")
    return True


def author_system_prompt(m: Dict[str, str], exemplars: Sequence[str]) -> str:
    ex = f" and the exemplar papers {list(exemplars)}" if exemplars else ""
    return (
        "You are the author of the main text of a physics paper. You write it block by block over several turns, one block per turn. "
        f"Before the first block read the story `{m['story']}`, the block plan `{m['plan']}`, the notation `{m['macros']}`, the genre notes "
        f"`{m['genre']}`, the voice samples under `categorized_exemplars` in `{m['profile']}`{ex}, and the author's red lines `{m['redlines']}`. "
        "Write for a physicist outside the subfield: say why before how, give each result in words before its formula, and keep one story "
        "running through all blocks. Three bottom lines: (1) state no fact that is not in the material (ledger records, story, literature "
        "map); where something is missing write \\todo{...}; (2) a theorem, lemma or proposition keeps the hypotheses and quantifiers of its "
        "ledger record (reformat it freely, never weaken or strengthen it); (3) nothing the red lines forbid. "
        f"Cite only keys that exist in `{m['bib']}`; otherwise write \\cite{{TODO:short description}}. "
        f"The literature map `{m['litmap']}` says what each cited work did; metadata and abstracts of verified works are in `{m['bib_cache']}`. "
        + m.get("decisions_note", "") + m.get("hand_note", "")
        + "If writing shows that the story or a decision does not hold, write the block as well as you can and raise a concern in your reply. "
        + CONCERN_RULE
    )


def _block_task(b: Dict[str, Any], mode: str, why: str, paper_dir: Path) -> str:
    path = paper_dir / b["file"]
    if mode == "complete":
        return f"complete `{path}`: keep its existing text and fill what it marks as missing (\\todo, TODO) or leaves unfinished"
    if mode == "rewrite":
        return f"rewrite `{path}` ({why}); keep what still serves the story"
    return f"write `{path}`"


def main_block_prompt(b: Dict[str, Any], i: int, n: int, done: Sequence[str], paper_dir: Path, index: Dict[str, Dict[str, Any]],
                      mode: str = "write", why: str = "", after: Sequence[str] = ()) -> str:
    done_s = ", ".join(f"`{paper_dir / f}`" for f in done)
    prev = f"Already written (continue the story from where they end): {done_s}" if done else "This is the opening block."
    nxt = (f"\nAlready written after this block (lead into them; do not edit them): {', '.join(f'`{paper_dir / f}`' for f in after)}"
           if after else "")
    fig_ids = [str(x) for x in (b.get("figure_ids") or [])]
    figs = f"Figures to place in this block: {', '.join(fig_ids)}.\n" if fig_ids else ""
    return (
        f"Block {i}/{n} `{b['id']}`: {_block_task(b, mode, why, paper_dir)} (about {int(b.get('target_words') or 400)} words; end the file "
        f"with a blank line).\nIts job in the story: {b['purpose']}\n{prev}{nxt}\n{figs}"
        f"Ledger records for this block (exact):\n{ledger_records(index, b.get('claim_ids') or [])}\n"
        "Write only this block's file. Reply with the file path, a word count, any \\todo items you left and any concerns."
    )


def finish_prompt(main_tex: Path, main_files: Sequence[str], paper_dir: Path, genre: str, has_abstract: bool = False) -> str:
    """The author's last turn: read the whole main text through, then write the title and the abstract for what is there."""
    limit = ABSTRACT_LIMITS.get(genre)
    lim = f"The venue allows at most {limit} characters including spaces (counted automatically afterwards)." if limit else "Aim for 150 to 220 words."
    files = ", ".join(f"`{paper_dir / f}`" for f in main_files)
    abstract_task = (
        f"2. Then check the title and the abstract in `{main_tex}` against the text as it now stands, and write the title and the abstract "
        "anew only where the text now tells something they do not."
        if has_abstract else
        f"2. Then write the title and the abstract in `{main_tex}`, replacing the placeholders, for the text as it now stands."
    )
    return (
        f"The main-text blocks are written. 1. Read the whole main text in order ({files}). Smooth the transitions, remove repetition, "
        "and make the introduction promise exactly what the body delivers; keep these edits small and add no new results. "
        f"{abstract_task} The abstract tells the story in miniature: the context and the question, the answer in words, and why it matters. "
        f"Explain inside the abstract every symbol and acronym it uses, and prefer words to symbols. {lim} "
        f"Make sure `{main_tex}` inputs `macros.tex` and the block files in this order: {', '.join(main_files)}. "
        "Leave the author list and the acknowledgments placeholders for the human author. Reply with a short list of what you changed."
    )


def sm_block_prompt(b: Dict[str, Any], m: Dict[str, str], paper_dir: Path, index: Dict[str, Dict[str, Any]], genre: str,
                    mode: str = "write", why: str = "") -> str:
    where = "Supplemental Material" if genre == "letter" else "appendix"
    return (
        f"{where[0].upper() + where[1:]} block `{b['id']}`: {_block_task(b, mode, why, paper_dir)} (about {int(b.get('target_words') or 800)} "
        f"words; end the file with a blank line).\nIts job: {b['purpose']}\n"
        f"Read the notation `{m['macros']}`, the plan `{m['plan']}` (to see what the main text says) and the red lines `{m['redlines']}`.\n"
        "Rigour first: complete proofs and derivations with every step, and every hypothesis stated. State no fact that is not in the "
        f"material; write \\todo{{...}} where something is missing; cite only keys in `{m['bib']}`, otherwise \\cite{{TODO:short description}}.\n"
        + m.get("decisions_note", "") + m.get("hand_note", "")
        + f"\nLedger records for this block (exact):\n{ledger_records(index, b.get('claim_ids') or [])}\n"
        "Write only this file. Reply with the file path, a word count, any \\todo items you left and any concerns. " + CONCERN_RULE
    )


async def run_draft(paths: Dict[str, Path]) -> Dict[str, Any]:
    """Write what is missing: empty blocks, the gaps of partial blocks, blocks marked for rewriting (or the files
    named in `only_files`). Complete blocks are kept and read for continuity."""
    wf = paths["wf"]
    paper_dir = paths["paper"]
    args = _wf_args()
    genre = str(args.get("genre") or "long")
    ledger_path = wf / "00_ground_truth_ledger.json"
    current_plan(paths, save=True)
    plan = await _ensure_valid_plan(_plan_path(), ledger_path)
    index = _ledger_index(ledger_path)
    refs = stage_refs(paths)
    profile_path = ensure_default_profile(paths)

    refs_bib = paper_dir / "refs.bib"
    seed_bib = read_text_safe(wf / "01_verified_seed.bib")
    if not refs_bib.exists() and seed_bib.strip():
        refs_bib.write_text(seed_bib, encoding="utf-8")

    main_blocks = [b for b in plan["blocks"] if b["part"] == "main"]
    sm_blocks = [b for b in plan["blocks"] if b["part"] == "sm"]
    main_files = [b["file"] for b in main_blocks]
    main_tex = paper_dir / "main.tex"
    supp_tex = resolve_supplement_path(paper_dir) or (paper_dir / "supplement.tex")
    title = str(plan.get("title") or "")
    if write_skeleton(main_tex, main_files, genre, title, supplement=False):
        log(f"[draft] wrote the skeleton {main_tex}")
    if sm_blocks and write_skeleton(supp_tex, [b["file"] for b in sm_blocks], genre, title, supplement=True):
        log(f"[draft] wrote the skeleton {supp_tex}")

    rewrite = _load_rewrite(wf)
    only = only_files(paper_dir)

    def mode_of(b: Dict[str, Any]) -> Optional[str]:
        status = block_status(paper_dir, b["file"])
        if only:
            return ("rewrite" if status == "full" else ("complete" if status == "partial" else "write")) if b["file"] in only else None
        if b["id"] in rewrite:
            return "rewrite" if status != "empty" else "write"
        return {"empty": "write", "partial": "complete"}.get(status)

    modes = {b["id"]: mode_of(b) for b in plan["blocks"]}
    main_todo = [b for b in main_blocks if modes[b["id"]]]
    sm_todo = [b for b in sm_blocks if modes[b["id"]]]
    has_abstract = not abstract_missing(paper_dir)
    finish = bool(main_todo) or not has_abstract
    kept = [b["id"] for b in plan["blocks"] if not modes[b["id"]]]
    if not main_todo and not sm_todo and not finish:
        log("[draft] every block is complete and nothing is marked for rewriting; nothing to write")
        return {"skipped": True, "reason": "nothing missing", "kept": kept}
    if kept:
        log(f"[draft] keeping {len(kept)} complete block(s): {kept}")

    m = {
        "story": str(wf / "01_story.md"), "plan": str(_plan_path()), "macros": str(paper_dir / "macros.tex"),
        "genre": str(refs.get("genre_templates.md", REFS_DIR / "genre_templates.md")), "profile": str(profile_path),
        "redlines": str(wf / "00_redlines.md"), "bib": str(refs_bib), "litmap": str(wf / "01_literature_map.md"),
        "bib_cache": str(wf / "04_api_bib_cache.json"), "decisions_note": decisions_note(wf), "hand_note": hand_edit_note(wf),
    }
    exemplars = [str(x) for x in (args.get("exemplar_paths") or [])]

    phase(f"Draft: {len(main_todo)} main-text block(s) by one author, {len(sm_todo)} SM block(s) in parallel")
    results: Dict[str, List[Any]] = {"main": [], "sm": []}

    async def write_main() -> None:
        if not finish:
            return
        author = create_persistent_handle(author_system_prompt(m, exemplars), role="Author", stage_name="draft", tier="builder")
        try:
            for i, b in enumerate(main_todo, 1):
                k = main_files.index(b["file"])
                results["main"].append(await call_persistent_handle(
                    author, main_block_prompt(b, i, len(main_todo), main_files[:k], paper_dir, index, modes[b["id"]], rewrite.get(b["id"], ""),
                                              [f for f in main_files[k + 1:] if block_status(paper_dir, f) == "full"]), BLOCK_SCHEMA))
            await call_persistent_handle(author, finish_prompt(main_tex, main_files, paper_dir, genre, has_abstract), {"type": "string"})
        finally:
            author.pop("handle", None)

    async def write_sm() -> None:
        if sm_todo:
            bs = max(1, int(_STATE["batch_size"] or 4) - 1)  # the author already uses one slot
            results["sm"] = await run_batched(
                sm_todo,
                lambda b: dispatch_agent(sm_block_prompt(b, m, paper_dir, index, genre, modes[b["id"]], rewrite.get(b["id"], "")),
                                         schema=BLOCK_SCHEMA, role=f"SM {b['id']}", stage_name="draft"),
                batch_size=bs,
            )

    await _gather_fail_fast([write_main(), write_sm()])
    snapshot_tex_files(paper_dir, paths["raw_drafts"])
    done_ids = {b["id"] for b in main_todo + sm_todo}
    left = {k: v for k, v in rewrite.items() if k not in done_ids}
    (wf / "01_rewrite.json").write_text(json.dumps(left, indent=2, ensure_ascii=False), encoding="utf-8")
    replies = [r for r in results["main"] + results["sm"] if isinstance(r, dict)]
    todos = [t for r in replies for t in (r.get("todos") or [])]
    concerns = add_feedback(paths, [c for r in replies for c in (r.get("concerns") or [])], "draft")
    return {"main_blocks": len(main_todo), "sm_blocks": len(sm_todo), "kept": kept, "main_tex": str(main_tex), "todos": todos,
            "concerns": len(concerns)}


# ------------------------------------------------------------------------------
# Figures
# ------------------------------------------------------------------------------

def _figure_specs(paths: Dict[str, Path]) -> List[Dict[str, Any]]:
    args = _wf_args()
    sp_arg = args.get("figure_specs_path")
    if sp_arg and Path(str(sp_arg)).exists():
        raw_sp = json.loads(read_text_safe(Path(str(sp_arg))))
        return raw_sp if isinstance(raw_sp, list) else raw_sp.get("figures", [])
    scripts = sorted(paths["fig_scripts"].glob("make_fig*.py"))
    if scripts:
        return [{"id": py.stem.replace("make_", ""), "script_path": str(py)} for py in scripts]
    plan, _ = validate_plan(_plan_path())
    if plan is not None:
        return [f for f in (plan.get("figures") or []) if isinstance(f, dict)]
    bp, _ = validate_blueprint(Path(str(args.get("blueprint_path") or (paths["wf"] / "01_blueprint.json"))))  # v2 workspaces
    return [f for f in ((bp or {}).get("figures") or []) if isinstance(f, dict)]


def _figure_missing(paths: Dict[str, Path], sp: Dict[str, Any]) -> bool:
    fid = str(sp.get("id") or "").strip().lower()
    return not fid or not any(fid in f.stem.lower() for f in paths["figures"].glob("*.pdf"))


async def run_figures(paths: Dict[str, Path], only_missing: bool = False) -> Dict[str, Any]:
    paper_dir = paths["paper"]
    fig_dir = paths["figures"]
    fig_scripts = paths["fig_scripts"]
    fig_renders = paths["fig_renders"]
    fig_style = str(_wf_args().get("figure_style") or "prl").lower()
    theme_src = TOOLS_DIR / "journal_fig_theme.py"
    refs = stage_refs(paths)
    index = _ledger_index(paths["wf"] / "00_ground_truth_ledger.json")

    phase(f"Figures Phase ({fig_style.upper()} House Style + PyMuPDF Hard Gate)")
    fig_scripts.mkdir(parents=True, exist_ok=True)
    if theme_src.exists():
        copy_to_workflow(theme_src, fig_scripts / "journal_fig_theme.py")

    specs = _figure_specs(paths)
    if only_missing:
        specs = [sp for sp in specs if _figure_missing(paths, sp)]
    if specs:
        await run_batched(
            specs,
            lambda sp: dispatch_agent(
                f"Create or refactor the figure builder `{sp.get('script_path') or (fig_scripts / ('make_' + str(sp.get('id', 'fig')) + '.py'))}` "
                f"writing into `{fig_dir}` in the `{fig_style}` house style.\n"
                f"Read the visual rules `{refs.get('figure_visual_language.md', REFS_DIR / 'figure_visual_language.md')}` and the notation `{paper_dir / 'macros.tex'}`.\n"
                f"1. Import `journal_fig_theme` from `{fig_scripts}` (`apply_house_style('{fig_style}')`, `add_panel_label`, `PALETTE`, `DIMENSIONS`).\n"
                f"2. Every number and label must match the ledger records below (exact values and the objects they belong to) and `macros.tex`.\n"
                f"3. Keep the canvas clean: panel titles, run parameters and error bounds go into the caption; labels at least 7 pt (PRL) / 5 pt (Nature); no overlapping strokes.\n"
                f"4. After editing the script, run `{resolve_python_exe()} <script_path>` so the PDF is written to `{fig_dir}`.\n\n"
                f"Figure spec:\n{json.dumps(sp, indent=2, ensure_ascii=False)}\n\n"
                f"Ledger records for this figure (exact):\n{ledger_records(index, sp.get('claim_ids') or [])}",
                schema={"type": "string"},
                role=f"Figure Design: {sp.get('id', 'fig')}",
                stage_name="figures",
            ),
        )

    fig_json = paths["wf"] / "06_figure_verify.json"
    fig_md = paths["audit"] / "04_figure_design_review.md"
    for rnd in range(3):
        rc_fig, _, _, fig_rep = run_tool(
            "verify_figures.py",
            ["--pdf-dir", str(fig_dir), "--scripts-dir", str(fig_scripts), "--tex-dir", str(paper_dir), "--style", fig_style,
             "--renders-out", str(fig_renders), "--json-out", str(fig_json), "--md-out", str(fig_md)],
            allow_fail=True,
        )
        if rc_fig == 0 or rnd == 2 or not fig_rep:
            log(f"verify_figures.py round {rnd + 1} finished: passed={bool(fig_rep and fig_rep.get('passed'))}")
            break
        failing_figs = [f for f in fig_rep.get("figures", []) if not f.get("passed")]
        ast_fails = fig_rep.get("script_ai_findings", [])
        if not failing_figs and not ast_fails:
            break
        log(f"verify_figures.py round {rnd + 1} caught {len(failing_figs)} failing figures and {len(ast_fails)} AST findings; repairing")
        await run_batched(
            failing_figs,
            lambda ff: dispatch_agent(
                f"Repair figure PDF `{ff['file']}` (its builder script in `{fig_scripts}` and PNG preview `{ff.get('previews', {}).get('rgb_300dpi')}`) to clear every failure below.\n"
                f"If a caption or symbol must change, keep figure and text in agreement (`{paper_dir / 'sections'}`).\n"
                f"Inspect only PNG previews (including any `zoom_*.png` in `{fig_renders}`), never multi-page PDFs.\n"
                f"Failures:\n{json.dumps(ff, indent=2, ensure_ascii=False)}\nScript findings:\n{json.dumps(ast_fails, indent=2)}",
                schema={"type": "string"},
                role=f"Fix Fig: {Path(ff['file']).name}",
                stage_name="figures",
            ),
        )
    return {"figure_report": str(fig_md), "figure_json": str(fig_json), "renders_dir": str(fig_renders)}


def _wants_figures(paths: Dict[str, Path]) -> bool:
    try:
        return bool(_figure_specs(paths))
    except (OSError, ValueError):
        return False


# ------------------------------------------------------------------------------
# Stage 3: check (report only)
# ------------------------------------------------------------------------------

ISSUE_SCHEMA = {
    "type": "object",
    "properties": {
        "issues": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "file": {"type": "string"}, "location": {"type": "string"}, "quote": {"type": "string"}, "problem": {"type": "string"},
                    "evidence": {"type": "string"}, "severity": {"type": "string", "enum": list(SEVERITIES)}, "suggested_fix": {"type": "string"},
                    "feedback_kind": {"type": "string", "enum": list(FEEDBACK_KINDS)}, "decision_id": {"type": "string"},
                },
                "required": ["file", "problem", "severity"],
            },
        },
    },
    "required": ["issues"],
}
ISSUE_FORMAT = (
    "Do not edit any file. For each issue give `file` (path relative to the paper directory), `location` (line number or the first words), "
    "`quote` (the exact text), `problem`, `evidence` (source file:line, ledger id or script), `severity` (high = wrong or overclaimed, "
    "medium = imprecise or unclear, low = minor) and, if you have one, `suggested_fix`. An empty list is a valid answer."
)
_BAD_BIB_STATUSES = ("EPRINT_DOI_MISMATCH", "EPRINT_NOT_FOUND", "AUTHOR_ORDER_MISMATCH", "IDENTIFIER_NOT_FOUND", "METADATA_MISMATCH", "UNVERIFIED_NO_MATCH")


def resolve_paper_file(paper_dir: Path, raw: Any) -> Optional[str]:
    """Map a file named by an agent to an existing paper-relative path. Agents mix paths relative to the paper,
    relative to their own working directory (`paper/sections/x.tex`) and absolute ones; two spellings of one file
    must not become two work items (the edit guard would then revert one agent's edits). None = outside the paper."""
    s = str(raw or "").strip().replace("\\", "/")
    if not s:
        return None
    rel = _paper_rel(paper_dir, s)
    if rel is not None and (paper_dir / rel).exists():
        return rel
    p = Path(s)
    if not p.is_absolute():
        try:
            via_cwd = str((Path.cwd() / p).resolve().relative_to(paper_dir.resolve())).replace("\\", "/")
            if (paper_dir / via_cwd).exists():
                return via_cwd
        except ValueError:
            pass
    if rel is None:
        return None
    known = [str(f.relative_to(paper_dir)).replace("\\", "/") for f in list_editable_tex(paper_dir)]
    known += [n for n in ("macros.tex", "refs.bib") if (paper_dir / n).exists()]
    hits = [k for k in known if rel == k or rel.endswith("/" + k)]
    return max(hits, key=len) if hits else rel


def collect_issues(paper_dir: Path, reports: Sequence[Any], source: str) -> List[Dict[str, Any]]:
    """Normalise checker output: paper-relative paths, known severities, a source tag; drop issues for files outside the paper."""
    out: List[Dict[str, Any]] = []
    for rep in reports:
        if not isinstance(rep, dict):
            raise RuntimeError(f"{source}: checker returned a non-object result: {str(rep)[:300]}")
        for it in rep.get("issues") or []:
            if not isinstance(it, dict) or not str(it.get("problem") or "").strip():
                continue
            raw_file = str(it.get("file") or "").strip()
            rel = resolve_paper_file(paper_dir, raw_file) if raw_file else ""
            if rel is None:
                log(f"[check] dropped an issue for a file outside the paper directory: {raw_file}")
                continue
            sev = str(it.get("severity") or "medium").lower()
            out.append({**it, "file": rel, "severity": sev if sev in SEVERITIES else "medium", "source": it.get("source") or source})
    return out


def dedupe_issues(issues: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen: set = set()
    out: List[Dict[str, Any]] = []
    for it in issues:
        key = (it.get("file", ""), re.sub(r"\s+", " ", str(it.get("quote") or it.get("problem") or "")).strip().lower()[:200])
        if key in seen:
            continue
        seen.add(key)
        out.append(it)
    order = {s: i for i, s in enumerate(SEVERITIES)}
    return sorted(out, key=lambda i: (order.get(i["severity"], 1), str(i.get("file", "")), str(i.get("location", ""))))


def _det_issue(paper_dir: Path, loc: Any, problem: str, severity: str, kind: str, source: str, default_file: str = "") -> Dict[str, Any]:
    m = re.search(r"([^:]+\.(?:tex|bib)):(\d+)\s*$", str(loc or ""))
    rel = (resolve_paper_file(paper_dir, m.group(1)) or m.group(1).replace("\\", "/")) if m else default_file
    return {"file": rel, "location": m.group(2) if m else "", "problem": problem, "severity": severity, "kind": kind, "source": source}


def issues_from_defs(paper_dir: Path, rep: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not rep:
        return []
    out: List[Dict[str, Any]] = []
    for t in rep.get("internal_tag_leaks") or []:
        out.append(_det_issue(paper_dir, t.get("location"), f"internal tag in the text: {t.get('match')}", "high", "leak", "audit_tex_defs"))
    for u in rep.get("unresolved_refs") or []:
        out.append(_det_issue(paper_dir, u.get("location"), f"{u.get('kind')}: {u.get('target')}", "high", "structure", "audit_tex_defs"))
    for s in rep.get("section_mismatches") or []:
        out.append(_det_issue(paper_dir, s.get("location"), f"{s.get('kind')}: {s.get('excerpt', '')}", "low", "structure", "audit_tex_defs", "main.tex"))
    for s in rep.get("undefined_main_symbols") or []:
        where = f" (first use: {s['first_use_excerpt']})" if s.get("first_use_excerpt") else ""
        out.append(_det_issue(paper_dir, "", f"{s.get('kind')}: {s.get('symbol')}{where}", "medium", "reader", "audit_tex_defs", "main.tex"))
    for a in rep.get("abstract_issues") or []:
        if a.get("kind") == "abstract_too_long":
            out.append(_det_issue(paper_dir, "", f"the abstract is about {a.get('chars')} characters; the venue limit is {a.get('limit')}", "high", "length", "audit_tex_defs", "main.tex"))
        else:
            out.append(_det_issue(paper_dir, "", f"the abstract uses the acronym {a.get('acronym')} without expanding it", "medium", "reader", "audit_tex_defs", "main.tex"))
    for d in rep.get("dual_symbol_conflicts") or []:
        out.append(_det_issue(paper_dir, "", str(d.get("detail") or d), "low", "wording", "audit_tex_defs", "main.tex"))
    return out


def issues_from_citations(paper_dir: Path, rep: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not rep:
        return [_det_issue(paper_dir, "", "the citation audit produced no report", "high", "citation", "verify_citations", "refs.bib")]
    out: List[Dict[str, Any]] = []
    for e in rep.get("entries") or []:
        if e.get("is_cited") and e.get("status") in _BAD_BIB_STATUSES:
            out.append(_det_issue(paper_dir, "", f"reference {e.get('key')}: {e.get('status')}", "high", "citation", "verify_citations", "refs.bib"))
    for t in rep.get("todo_placeholders") or []:
        for loc in (t.get("locations") or [""]):
            out.append(_det_issue(paper_dir, loc, f"unresolved citation placeholder {t.get('key')}", "medium", "citation", "verify_citations", "main.tex"))
    net = rep.get("network_unverified_keys") or []
    if net:
        out.append(_det_issue(paper_dir, "", f"{len(net)} cited entries could not be checked (network): {net[:10]}", "medium", "citation", "verify_citations", "refs.bib"))
    return out


def issues_from_scans(paper_dir: Path, wm: Optional[Dict[str, Any]], sty: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for fr in (wm or {}).get("files") or []:
        rel = resolve_paper_file(paper_dir, fr.get("file") or "") or ""
        for h in (fr.get("copyedit_issues") or []) + (fr.get("ai_slop_hits") or []):
            out.append({"file": rel, "location": str(h.get("line", "")) if isinstance(h, dict) else "",
                        "problem": "copy-edit: " + json.dumps(h, ensure_ascii=False)[:240], "severity": "low", "kind": "copyedit", "source": "clean_watermarks"})
    for fr in (sty or {}).get("files") or []:
        if fr.get("staccato_warning"):
            rel = resolve_paper_file(paper_dir, fr.get("file") or "") or ""
            out.append({"file": rel, "location": "", "problem": f"runs of very short sentences: {json.dumps(fr.get('staccato_warning'), ensure_ascii=False)[:240]}",
                        "severity": "low", "kind": "flow", "source": "style_metrics"})
    return out


async def citation_pass(paths: Dict[str, Path], agents: bool = True) -> Optional[Dict[str, Any]]:
    """Reference metadata only: audit the .bib, resolve `\\cite{TODO:...}` placeholders that can be verified, repair
    wrong metadata in refs.bib. Prose is never rewritten here. agents=False: the audit alone (cached API answers)."""
    paper_dir = paths["paper"]
    wf = paths["wf"]
    refs_bib = paper_dir / "refs.bib"
    sync_bib = _wf_args().get("sync_bib")
    if sync_bib and Path(str(sync_bib)).exists():
        write_user_file(refs_bib, Path(str(sync_bib)).read_bytes())
    elif not refs_bib.exists():
        refs_bib.write_text("", encoding="utf-8")
    bib_cache = wf / "04_api_bib_cache.json"
    cite_json = wf / "04_citation_audit.json"
    cite_md = paths["audit"] / "02_citation_audit.md"
    audit_cli = ["audit", "--tex-dir", str(paper_dir), "--bib", str(refs_bib), "--cache", str(bib_cache), "--json-out", str(cite_json), "--md-out", str(cite_md)]
    _, _, _, rep = run_tool("verify_citations.py", audit_cli + (["--fix-braces", "--upgrade-preprints"] if agents else []), allow_fail=True)
    if rep is None or not agents:
        return rep
    vc = f"{resolve_python_exe()} {TOOLS_DIR / 'verify_citations.py'} verify-entry"
    todo_list = rep.get("todo_placeholders") or []
    if todo_list:
        # One agent for all placeholders: each is a short lookup, and one agent per placeholder was mostly start-up time.
        await dispatch_agent(
            f"Resolve these placeholder citations inside `{paper_dir}`: {json.dumps(todo_list, ensure_ascii=False)}.\n"
            f"For each one: 1. `{vc} --query \"...\"` lists candidates only (never verified). 2. Confirm the right candidate with "
            f"`{vc} --doi <doi> --title \"<exact title>\"` (or `--arxiv <id>`). 3. ONLY if step 2 printed verified=true: add its BibTeX to "
            f"`{refs_bib}` (keep the other entries, @string and comments untouched) and replace the placeholder key in the .tex. Otherwise "
            f"leave that placeholder. Change no other text.",
            schema={"type": "object", "properties": {"resolved": {"type": "array", "items": {"type": "string"}}, "unresolved": {"type": "array", "items": {"type": "string"}}},
                    "required": ["resolved", "unresolved"]},
            role="Resolve Citation Placeholders",
            stage_name="check",
        )
    bad_bib = [e for e in rep.get("entries", []) if e.get("is_cited") and e.get("status") in _BAD_BIB_STATUSES]
    bbl_iss = rep.get("bbl_issues", []) + rep.get("bib_hygiene_issues", []) + [{"bib_parse_error": pe} for pe in rep.get("bib_parse_errors", [])]
    if bad_bib or bbl_iss:
        await dispatch_agent(
            f"Repair the BibTeX metadata in `{refs_bib}`: eprint/DOI mismatches, duplicate `arXiv:` notes and math braces (check each fix with "
            f"`{vc}`). Edit only `{refs_bib}`.\nBad entries:\n{json.dumps(bad_bib, indent=2, ensure_ascii=False)}\n"
            f"Hygiene and .bbl issues:\n{json.dumps(bbl_iss, indent=2, ensure_ascii=False)}",
            schema={"type": "string"},
            role="Repair refs.bib Metadata",
            stage_name="check",
        )
    if todo_list or bad_bib or bbl_iss:
        _, _, _, rep = run_tool("verify_citations.py", audit_cli, allow_fail=True)
    return rep


_CITE_CTX_RE = re.compile(r"\\([A-Za-z]*cite[A-Za-z]*)\*?(?:\s*\[[^\]]*\]){0,2}\s*\{([^{}]*)\}")


def citation_contexts(paper_dir: Path, audit: Optional[Dict[str, Any]], litmap_text: str, files: Sequence[str]) -> List[Dict[str, Any]]:
    """Each \\cite with the sentence around it and, per cited key, title, year, authors and abstract (from the citation
    audit's API metadata) plus what the literature map says the work did. Joined by the program: the checker judges
    the fit of each citation, it does not look the works up."""
    meta = {str(e.get("key")): (e.get("api_meta") or {}) for e in (audit or {}).get("entries") or [] if isinstance(e, dict)}
    notes: Dict[str, str] = {}
    lm_lines = litmap_text.splitlines()
    for i, line in enumerate(lm_lines):
        m = re.search(r"\[verified: ([^\]]+)\]", line)
        if m:
            parts = [line.strip().lstrip("-").strip()]
            for nxt in lm_lines[i + 1:i + 4]:  # a lineage step's "Question / Found / Left open" lines follow it, indented
                if not re.match(r"\s{2,}- (Question|Found|Left open):", nxt):
                    break
                parts.append(nxt.strip().lstrip("-").strip())
            notes[m.group(1).strip()] = " ".join(parts)
    out: List[Dict[str, Any]] = []
    for rel in files:
        code = _strip_tex_comments(read_text_safe(paper_dir / rel))
        for m in _CITE_CTX_RE.finditer(code):
            if m.group(1) == "nocite":
                continue
            keys = [k.strip() for k in m.group(2).split(",") if k.strip() and not k.strip().upper().startswith("TODO:")]
            if not keys:
                continue
            s = max(code.rfind(". ", 0, m.start()), code.rfind("\n\n", 0, m.start()))
            ends = [x for x in (code.find(". ", m.end()), code.find("\n\n", m.end())) if x >= 0]
            e = min(ends) if ends else len(code)
            sentence = re.sub(r"\s+", " ", code[(s + 1 if s >= 0 else 0):e + 1]).strip()[:800]
            works = []
            for k in keys:
                md = meta.get(k) or {}
                authors = md.get("authors") or []
                works.append({"key": k, "title": str(md.get("title") or ""), "year": str(md.get("year") or ""),
                              "authors": [str(a) for a in authors[:3]] if isinstance(authors, list) else str(authors),
                              "arxiv": str(md.get("arxiv") or ""), "abstract": str(md.get("abstract") or "") or "(no abstract available)",
                              "literature_map": notes.get(k, "")})
            out.append({"file": rel, "line": code.count("\n", 0, m.start()) + 1, "sentence": sentence, "works": works})
    return out


# ------------------------------------------------------------------------------
# Citation claims, coarse to fine
#
# Every (sentence, cited work) pair is first judged from the work's abstract, its literature-map note and any
# facts verified earlier from its full text (coarse). Key citations the coarse pass cannot settle are read in
# full (fine; at effort high, every key citation). Verdicts and per-work reading notes are kept in the citation
# notes folder (`citation_notes_dir`, default .workflow/citation_notes): an unchanged sentence is not checked
# again, and a work read once is not read again, here or in a later paper that points to the same folder.
# ------------------------------------------------------------------------------

CITE_VERDICTS = ("supported", "not_supported", "unclear", "wrong_kind", "timeline")
_CITE_VERDICT_ITEM = {
    "type": "object",
    "properties": {
        "id": {"type": "string"}, "verdict": {"type": "string", "enum": list(CITE_VERDICTS)},
        "importance": {"type": "string", "enum": ["key", "background"]}, "problem": {"type": "string"},
        "suggested_fix": {"type": "string"}, "novelty": {"type": "boolean"}, "evidence": {"type": "string"}, "location": {"type": "string"},
    },
    "required": ["id", "verdict"],
}
CITE_COARSE_SCHEMA = {"type": "object", "properties": {"verdicts": {"type": "array", "items": _CITE_VERDICT_ITEM}}, "required": ["verdicts"]}
CITE_FINE_SCHEMA = {
    "type": "object",
    "properties": {
        "verdicts": {"type": "array", "items": _CITE_VERDICT_ITEM},
        "facts": {"type": "array", "items": {"type": "object", "properties": {"statement": {"type": "string"}, "location": {"type": "string"},
                                                                               "quote": {"type": "string"}}, "required": ["statement", "location"]}},
    },
    "required": ["verdicts", "facts"],
}
CITE_JUDGE_RULES = (
    "supported (the work establishes what the sentence credits it with); not_supported (it does not: another result, model or method, "
    "or less than the sentence claims); wrong_kind (a priority or 'first shown by' statement citing a review, a textbook or a later paper "
    "instead of the original, or a result credited to a work that only uses it); timeline (a work concurrent with or later than the "
    "present results cited as earlier work); unclear (the text you have cannot settle it). Set `novelty` when the work contradicts a "
    "novelty claim of the manuscript ('for the first time', 'new', 'unlike previous work'). Set `importance` to key when the sentence "
    "relies on the work for a specific result, number, method, priority or novelty comparison, background otherwise."
)


def _notes_dir(wf: Path) -> Path:
    d = Path(str(_wf_args().get("citation_notes_dir") or (wf / "citation_notes")))
    d.mkdir(parents=True, exist_ok=True)
    return d


def _safe_key(key: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", key)[:80] or "work"


def claim_id(key: str, sentence: str) -> str:
    return hashlib.sha1((key + "|" + re.sub(r"\s+", " ", sentence).strip().lower()).encode("utf-8")).hexdigest()[:16]


def _load_json_file(path: Path, default: Any) -> Any:
    try:
        return json.loads(read_text_safe(path) or "null") or default
    except json.JSONDecodeError:
        return default


def reading_note(nd: Path, key: str) -> Dict[str, Any]:
    return _load_json_file(nd / f"{_safe_key(key)}.json", {})


def needs_fulltext(v: Dict[str, Any]) -> bool:
    if v.get("level") == "fulltext" or v.get("importance") != "key":
        return False
    return v.get("verdict") != "supported" or effort_level() == "high"


def fetch_full_text(nd: Path, work: Dict[str, Any]) -> Optional[Path]:
    """The work's text (pages marked), from `reference_pdfs/<key>.pdf` if given, else from arXiv; cached per work."""
    txt = nd / f"{_safe_key(work['key'])}.txt"
    if txt.exists() and txt.stat().st_size > 500:
        return txt
    pdf_dir = _wf_args().get("reference_pdfs")
    cand = Path(str(pdf_dir)) / f"{work['key']}.pdf" if pdf_dir else None
    if cand is not None and cand.is_file():
        cli = ["--pdf", str(cand)]
    elif work.get("arxiv"):
        cli = ["--arxiv", str(work["arxiv"])]
    else:
        return None
    rc, _, _, _ = run_tool("fetch_fulltext.py", cli + ["--out", str(txt)], allow_fail=True, timeout_s=300)
    return txt if rc == 0 and txt.exists() else None


def _cite_record(pr: Dict[str, Any], v: Dict[str, Any], level: str) -> Dict[str, Any]:
    verdict = str(v.get("verdict") or "unclear")
    imp = "key" if pr.get("forced_key") else str(v.get("importance") or "background")
    return {"id": pr["id"], "claim": pr["claim"], "key": pr["key"], "sentence": pr["sentence"], "file": pr["file"], "line": pr["line"],
            "verdict": verdict if verdict in CITE_VERDICTS else "unclear", "importance": imp if imp in ("key", "background") else "background",
            "problem": str(v.get("problem") or ""), "suggested_fix": str(v.get("suggested_fix") or ""), "novelty": bool(v.get("novelty")),
            "evidence": str(v.get("evidence") or ""), "location": str(v.get("location") or ""), "level": level,
            "checked": datetime.datetime.now().strftime("%Y-%m-%d")}


def coarse_citation_jobs(paths: Dict[str, Path], contexts: Sequence[Dict[str, Any]], role: str = "Citation Check") -> Tuple[List[Tuple[str, Callable[[], Any]]], Dict[str, Any]]:
    """The coarse pass as jobs for the caller's agent pool; verdicts stored for an unchanged sentence are reused."""
    wf = paths["wf"]
    nd = _notes_dir(wf)
    store = _load_json_file(nd / "verdicts.json", {})
    pairs: List[Dict[str, Any]] = []
    for i, c in enumerate(contexts, 1):
        for w in c.get("works") or []:
            pairs.append({"id": f"c{i}:{w['key']}", "claim": claim_id(w["key"], c["sentence"]), "key": w["key"], "sentence": c["sentence"],
                          "file": c.get("file", ""), "line": c.get("line", ""), "work": w, "forced_key": bool(c.get("key_context"))})
    state: Dict[str, Any] = {"pairs": pairs, "store": store, "nd": nd, "verdicts": {}, "todo": []}
    for pr in pairs:
        old = store.get(pr["claim"])
        if old:
            state["verdicts"][pr["id"]] = {**old, "id": pr["id"], "file": pr["file"], "line": pr["line"], "reused": True}
        else:
            state["todo"].append(pr)
    batch = int(effort()["cite_context_batch"] or 0)
    jobs: List[Tuple[str, Callable[[], Any]]] = []
    if not batch:
        state["todo"] = []
        return jobs, state
    for k in range(0, len(state["todo"]), batch):
        items = []
        for pr in state["todo"][k:k + batch]:
            w = pr["work"]
            items.append({"id": pr["id"], "file": pr["file"], "line": pr["line"], "sentence": pr["sentence"],
                          "work": {x: w.get(x) for x in ("key", "title", "year", "authors", "abstract", "literature_map")},
                          "facts_verified_from_full_text": (reading_note(nd, pr["key"]).get("facts") or [])[:30]})
        n = k // batch + 1
        bf = wf / f"04_citation_pairs_{role.split()[0].lower()}_{n}.json"
        bf.write_text(json.dumps(items, indent=1, ensure_ascii=False), encoding="utf-8")
        jobs.append((f"cite-coarse:{n}", lambda bf=bf, n=n: dispatch_agent(
            f"Judge whether each citation fits the sentence that makes it. `{bf}` lists (sentence, cited work) pairs; each work comes with "
            "its title, year, authors and abstract, what the literature map says it did, and any facts verified earlier from its full text. "
            "Judge from those only: this is the coarse pass, and key citations you cannot settle are read in full afterwards. For each "
            f"pair give its `id` and a verdict: {CITE_JUDGE_RULES} For a verdict other than supported say what is wrong in `problem` and, "
            "if you can, give a `suggested_fix`. Report nothing about style.",
            schema=CITE_COARSE_SCHEMA, role=f"{role} {n}", stage_name="check", read_only=True, tier="heavy")))
    return jobs, state


async def finish_citation_check(paths: Dict[str, Path], state: Dict[str, Any], coarse_reports: Sequence[Any],
                                report_name: str = "08_citation_check.md") -> Dict[str, Any]:
    """Take the coarse verdicts, read the doubtful key works in full, store verdicts and reading notes, write the report."""
    nd: Path = state["nd"]
    by_id = {pr["id"]: pr for pr in state["pairs"]}
    for rep_ in coarse_reports:
        for v in (rep_.get("verdicts") if isinstance(rep_, dict) else None) or []:
            pr = by_id.get(str(v.get("id") or ""))
            if pr is not None and not state["verdicts"].get(pr["id"], {}).get("reused"):
                state["verdicts"][pr["id"]] = _cite_record(pr, v, "abstract")
    for pr in state["todo"]:
        state["verdicts"].setdefault(pr["id"], _cite_record(pr, {"verdict": "unclear", "problem": "no verdict from the coarse pass"}, "abstract"))

    need = [v for v in state["verdicts"].values() if needs_fulltext(v)]
    rank = {"not_supported": 0, "timeline": 0, "wrong_kind": 1, "unclear": 1, "supported": 2}
    keys: List[str] = []
    for v in sorted(need, key=lambda x: rank.get(x["verdict"], 1)):
        if v["key"] not in keys:
            keys.append(v["key"])
    cap = int(effort()["fulltext_works"] or 0)
    if len(keys) > cap:
        log(f"[citations] {len(keys)} works would be read in full; reading the first {cap} (effort {effort_level()}), the rest stay at the abstract level")
        keys = keys[:cap]
    works = {pr["key"]: pr["work"] for pr in state["pairs"]}
    texts: Dict[str, Path] = {}
    unavailable: List[str] = []
    for k in keys:
        txt = fetch_full_text(nd, works[k])
        if txt is None:
            unavailable.append(k)
        else:
            texts[k] = txt
    for v in need:
        if v["key"] in unavailable:
            v["fulltext"] = "unavailable"

    async def fine(k: str) -> Tuple[str, Any]:
        items = [{"id": v["id"], "sentence": v["sentence"], "coarse_verdict": v["verdict"], "coarse_problem": v["problem"]} for v in need if v["key"] == k]
        facts = reading_note(nd, k).get("facts") or []
        w = works[k]
        res = await dispatch_agent(
            f"Read the full text of `{k}` ({w.get('title', '')}, {w.get('year', '')}) in `{texts[k]}` (pages are marked `=== page N ===`)."
            + (f" Facts verified from it earlier: {json.dumps(facts, ensure_ascii=False)}." if facts else "")
            + f"\nFor each sentence below that cites it, give its `id` and a verdict from the full text: {CITE_JUDGE_RULES} Give the "
            "`location` (section, equation, theorem or page) and, in `evidence`, a quote of at most 25 words. Then list `facts`: what the "
            "work establishes that these sentences rely on, each with its location and a short quote, so later citations of it can be "
            f"checked without reading it again.\nSentences:\n{json.dumps(items, indent=1, ensure_ascii=False)}",
            schema=CITE_FINE_SCHEMA, role=f"Citation Full Text {k}", stage_name="check", read_only=True, tier="heavy")
        return k, res

    fine_results = await run_batched([k for k in keys if k in texts], fine) if texts else []
    stamp = datetime.datetime.now().strftime("%Y-%m-%d")
    for k, res in fine_results:
        if not isinstance(res, dict):
            continue
        for v in res.get("verdicts") or []:
            pid = str(v.get("id") or "")
            old = state["verdicts"].get(pid)
            if old is not None and old["key"] == k:
                state["verdicts"][pid] = {**_cite_record(by_id[pid], v, "fulltext"), "importance": old["importance"]}
        new_facts = [f for f in res.get("facts") or [] if isinstance(f, dict) and f.get("statement")]
        if new_facts:
            note = reading_note(nd, k)
            seen = {(f.get("statement"), f.get("location")) for f in note.get("facts") or []}
            merged = list(note.get("facts") or []) + [f for f in new_facts if (f.get("statement"), f.get("location")) not in seen]
            (nd / f"{_safe_key(k)}.json").write_text(json.dumps({
                "key": k, "title": works[k].get("title", ""), "year": works[k].get("year", ""), "arxiv": works[k].get("arxiv", ""),
                "text": str(texts[k]), "facts": merged, "updated": stamp}, indent=1, ensure_ascii=False), encoding="utf-8")

    store = state["store"]
    for v in state["verdicts"].values():
        store[v["claim"]] = {x: y for x, y in v.items() if x not in ("id", "reused")}
    (nd / "verdicts.json").write_text(json.dumps(store, indent=1, ensure_ascii=False), encoding="utf-8")
    verdicts = list(state["verdicts"].values())
    report = write_citation_report(paths, verdicts, nd, report_name)
    n_reused = sum(1 for v in verdicts if v.get("reused"))
    log(f"[citations] {len(verdicts)} citations: {len(verdicts) - n_reused} judged now from abstracts, {n_reused} reused; "
        f"{len(texts)} works read in full{f', {len(unavailable)} without an accessible full text' if unavailable else ''} -> {report}")
    return {"verdicts": verdicts, "reused": n_reused, "fulltext_read": sorted(texts), "fulltext_unavailable": unavailable, "report": str(report)}


def write_citation_report(paths: Dict[str, Path], verdicts: Sequence[Dict[str, Any]], nd: Path, name: str) -> Path:
    counts = {k: sum(1 for v in verdicts if v["verdict"] == k) for k in CITE_VERDICTS}
    md = ["# Citation check (coarse to fine)", "",
          f"Abstracts first; key citations the abstract could not settle are read in full. Verdicts and reading notes are kept in `{nd}` "
          "(verdicts.json, one note per work read in full) and reused while a sentence is unchanged.", "",
          "- " + ", ".join(f"{k}: {n}" for k, n in counts.items()), ""]
    by_key: Dict[str, List[Dict[str, Any]]] = {}
    for v in verdicts:
        by_key.setdefault(v["key"], []).append(v)
    for key in sorted(by_key):
        note = reading_note(nd, key)
        md.append(f"## {key}" + (f" (read in full; {len(note.get('facts') or [])} verified facts in `{_safe_key(key)}.json`)" if note.get("facts") else ""))
        for v in by_key[key]:
            tags = [v.get("level", ""), "key" if v.get("importance") == "key" else "", "earlier run" if v.get("reused") else "",
                    "full text unavailable" if v.get("fulltext") == "unavailable" else ""]
            line = f"- **{v['verdict']}** ({', '.join(x for x in tags if x)}) {v.get('file', '')}:{v.get('line', '')}: \"{str(v.get('sentence', ''))[:220]}\""
            if v.get("location"):
                line += f" [at {v['location']}]"
            if v.get("problem"):
                line += f" {v['problem']}"
            if v.get("evidence"):
                line += f" Evidence: {v['evidence']}"
            md.append(line)
        md.append("")
    path = paths["audit"] / name
    path.write_text("\n".join(md), encoding="utf-8")
    return path


def issues_from_citation_verdicts(verdicts: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for v in verdicts:
        sev = {"not_supported": "high", "timeline": "high", "wrong_kind": "medium"}.get(v["verdict"])
        if sev:
            problem = f"citation {v['key']}: {v['verdict'].replace('_', ' ')}" + (f": {v['problem']}" if v.get("problem") else "")
        elif v["verdict"] == "unclear" and v.get("importance") == "key":
            sev = "low"
            why = "its full text was not accessible" if v.get("fulltext") == "unavailable" else f"checked at the {v.get('level', 'abstract')} level"
            problem = f"could not confirm that {v['key']} supports this sentence ({why}; a PDF named {v['key']}.pdf in reference_pdfs allows a full reading)"
        else:
            continue
        it = {"file": v.get("file", ""), "location": str(v.get("line", "")), "quote": str(v.get("sentence", ""))[:300], "problem": problem,
              "severity": sev, "evidence": "; ".join(x for x in (v.get("location", ""), v.get("evidence", "")) if x),
              "suggested_fix": v.get("suggested_fix", ""), "kind": "citation", "source": f"citation-check ({v.get('level', 'abstract')})"}
        if v.get("novelty"):
            it["feedback_kind"] = "literature"
        out.append(it)
    return out


def _scripted_check_guarded(prompt: str, role: str) -> Any:
    """A checker that may write scripts (under .workflow/verify_scripts) but must not change the manuscript."""
    async def _go() -> Any:
        snap = _readonly_snapshot()
        try:
            return await dispatch_agent(prompt, schema=ISSUE_SCHEMA, role=role, stage_name="check", tier="builder")
        finally:
            _readonly_restore(snap, role)
    return _go()


DEFAULT_NUMERIC_CHECKS = 4
CHECK_GROUP_BYTES = 60_000


def _pick_numeric_claims(ledger_path: Path, plan: Optional[Dict[str, Any]], limit: int) -> List[Dict[str, Any]]:
    """Core results that the main text uses come first (in reading order), then the other core results."""
    claims = _ledger_claims(ledger_path)
    by_id = {str(c.get("id")): c for c in claims}
    core_ids = {str(c.get("id")) for c in claims if str(c.get("tier") or "").lower() == "core"}
    main_ids = [str(i) for b in (plan or {}).get("blocks", []) if b.get("part") == "main" for i in (b.get("claim_ids") or [])]
    order: List[str] = []
    for cid in main_ids + [str(c.get("id")) for c in claims]:
        if cid in by_id and cid not in order and (not core_ids or cid in core_ids):
            order.append(cid)
    if len(order) > limit:
        log(f"[check] {len(order)} candidate results; scripted checks run on the first {limit} (max_numeric_checks)")
    return [by_id[c] for c in order[:limit]]


def check_groups(paper_dir: Path, budget: Optional[int] = None) -> List[Tuple[str, List[str]]]:
    """The main text is one group, read in order by one checker (a story is checked as a whole, and an issue is
    pinned to the right file). Every other .tex file is packed, in reading order, into groups of about `budget` bytes."""
    budget = int(budget or effort()["check_group_bytes"] or CHECK_GROUP_BYTES)
    main = part_files(paper_dir, "main.tex")
    groups: List[Tuple[str, List[str]]] = [("main text", main)] if main else []
    supp = resolve_supplement_path(paper_dir)
    supp_order = part_files(paper_dir, supp.name) if supp else []
    rest = [str(f.relative_to(paper_dir)).replace("\\", "/") for f in list_editable_tex(paper_dir)]
    rest = [r for r in rest if r not in main]
    rest.sort(key=lambda r: (supp_order.index(r) if r in supp_order else len(supp_order), r))
    packs: List[List[str]] = []
    cur: List[str] = []
    size = 0
    for r in rest:
        s = (paper_dir / r).stat().st_size if (paper_dir / r).exists() else 0
        if cur and size + s > budget:
            packs.append(cur)
            cur, size = [], 0
        cur.append(r)
        size += s
    if cur:
        packs.append(cur)
    for k, pack in enumerate(packs, 1):
        groups.append((f"supplement {k}" if len(packs) > 1 else "supplement", pack))
    return groups


def _write_check_report(paths: Dict[str, Path], issues: List[Dict[str, Any]], suffix: str, title: str, note: str) -> Tuple[Path, Path, Dict[str, int]]:
    counts = {s: sum(1 for i in issues if i["severity"] == s) for s in SEVERITIES}
    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    issues_path = paths["wf"] / f"03_issues{suffix}.json"
    issues_path.write_text(json.dumps({"generated": stamp, "counts": counts, "issues": issues}, indent=2, ensure_ascii=False), encoding="utf-8")
    md = [f"# {title}", "", f"- Generated: {stamp}", f"- Issues: {len(issues)} (high {counts['high']}, medium {counts['medium']}, low {counts['low']})", f"- {note}", ""]
    by_file: Dict[str, List[Dict[str, Any]]] = {}
    for it in issues:
        by_file.setdefault(it.get("file") or "(no file)", []).append(it)
    for f, items in by_file.items():
        md += [f"## {f}", ""]
        for it in items:
            quote = f" — \"{str(it['quote'])[:160]}\"" if it.get("quote") else ""
            md.append(f"- **{it['severity']}** [{it.get('source', '')}] {it.get('location', '')}{quote}: {it['problem']}")
        md.append("")
    report = paths["audit"] / f"01_check_report{suffix}.md"
    report.write_text("\n".join(md), encoding="utf-8")
    return issues_path, report, counts


async def run_check(paths: Dict[str, Path], llm: bool = True) -> Dict[str, Any]:
    """All checks in one pass over the finished text. Nothing here rewrites prose (the citation step only fixes
    refs.bib metadata and verifiable `\\cite{TODO:...}` keys). llm=False: the deterministic checks alone, used
    after revise instead of a second full check."""
    paper_dir = paths["paper"]
    wf = paths["wf"]
    audit_dir = paths["audit"]
    args = _wf_args()
    genre = str(args.get("genre") or "long")
    ledger_path = wf / "00_ground_truth_ledger.json"
    plan, _ = validate_plan(_plan_path())
    main_tex = paper_dir / "main.tex"
    auth_files = [str(p) for p in collect_source_files(args.get("authority_paths") or args.get("source_paths") or [])]
    phase("Stage 3: Check (one pass over the whole text; reports only)" if llm else "Deterministic check after revise")

    defs_cli = ["--main", str(main_tex), "--json-out", str(wf / "03_tex_defs_audit.json"), "--md-out", str(audit_dir / "00_tex_defs_audit.md")]
    if ABSTRACT_LIMITS.get(genre):
        defs_cli += ["--abstract-max-chars", str(ABSTRACT_LIMITS[genre])]
    _, _, _, defs = run_tool("audit_tex_defs.py", defs_cli, allow_fail=True)
    issues: List[Dict[str, Any]] = issues_from_defs(paper_dir, defs)
    cite_rep = await citation_pass(paths, agents=llm)
    issues += issues_from_citations(paper_dir, cite_rep)
    tex_files = list_editable_tex(paper_dir)
    _, _, _, wm = run_tool("clean_watermarks.py", ["--paths", str(paper_dir), "--json-out", str(wf / "05_clean_wm.json")], allow_fail=True)
    sty = None
    if tex_files:
        _, _, _, sty = run_tool("style_metrics.py", ["verify", "--inputs", *[str(f) for f in tex_files], "--profile", str(ensure_default_profile(paths)),
                                                     "--json-out", str(wf / "05_stylometrics.json"), "--md-out", str(audit_dir / "03_stylometrics_report.md"), "--no-lm"], allow_fail=True)
    issues += issues_from_scans(paper_dir, wm, sty)

    if not llm:
        issues = dedupe_issues(issues)
        issues_path, report, counts = _write_check_report(paths, issues, "_after_revise", "Deterministic Check After Revise",
                                                          "Programs only (definitions, abstract read alone, citations, copy-edit scans); run mode=check for a full check.")
        log(f"Deterministic check after revise: {len(issues)} issues (high {counts['high']}) -> {report}")
        return {"issues": len(issues), **counts, "issues_json": str(issues_path), "report": str(report)}

    if auth_files:
        truth = (f"the ledger `{ledger_path}` and the original notes and authority files {auth_files}. Do not use `01_story.md`, `01_plan.json` "
                 f"or the old `01_blueprint.json` as evidence")
    else:
        truth = (f"the ledger `{ledger_path}`, which was built from the paper itself because no notes were given: check the mathematics and the "
                 f"internal consistency (a result stated differently in two places is an issue). Do not use `01_story.md`, `01_plan.json` or the "
                 f"old `01_blueprint.json` as evidence")
    scope_rules = (
        f"Report every place where the text (a) states a result with a wider scope, or weaker or stronger hypotheses or quantifiers, than "
        f"the source; (b) attributes something to the wrong work or misdescribes a cited work (metadata and abstracts of cited works: "
        f"`{wf / '04_api_bib_cache.json'}`); (c) gives a number that differs from the source or attaches it to the wrong model or observable; "
        f"(d) contains a mathematical error. Report nothing about style or wording. In the abstract and the introduction a result may be "
        f"told in words with its technical hypotheses left to the body: report only what is false or stronger than the source there, "
        f"not a missing qualifier."
    )
    concern_txt = (
        decisions_note(wf) + hand_edit_note(wf)
        + "If an issue cannot be fixed by a local edit because the story, the structure, a headline or one of the author's decisions would "
        "have to change, or if it shows that a decision rests on something that does not hold, give it `feedback_kind` (evidence, "
        "literature, feasibility or conflict) and the `decision_id` if one applies: the author decides those."
    )
    only = only_files(paper_dir)
    report_only = f" Report issues only in: {', '.join(only)}." if only else ""
    intro = next((b["file"] for b in (plan or {}).get("blocks", []) if b.get("part") == "main"), None)
    intro_txt = f" and the introduction `{paper_dir / intro}`" if intro else " and its introduction (the first main-text section it inputs)"
    math_txt = f" The abstract uses these math expressions: {json.dumps((defs or {}).get('abstract_math') or [], ensure_ascii=False)}." if (defs or {}).get("abstract_math") else ""
    reader_task = (
        f"Read the abstract in `{main_tex}`{intro_txt} as a physicist from a neighbouring field would.{math_txt}\n"
        f"Report: (1) every sentence of the abstract or the introduction that is false or claims more than the results support (compare "
        f"with the ledger `{ledger_path}`), severity high. These parts tell results in words and leave technical hypotheses (system size, "
        f"boundary conditions, parameter ranges) to the body; a missing qualifier is not an issue unless the sentence is false without it. "
        f"(2) every symbol or acronym in the abstract that the abstract itself does not explain, severity medium; (3) one issue for each of "
        f"these questions the introduction leaves unanswered, severity medium: what is the question, why does it matter, how far did "
        f"earlier work get, what is new here."
    )
    want_reader = not only or "main.tex" in only or (intro is not None and intro in only)
    separate_reader = bool(effort()["separate_reader"])

    jobs: List[Tuple[str, Callable[[], Any]]] = []
    for label, files in check_groups(paper_dir):
        if only and not any(f in only for f in files):
            continue
        listing = ", ".join(f"`{paper_dir / f}`" for f in files)
        second = f"\nThen, as a second pass: {reader_task}" if (label == "main text" and want_reader and not separate_reader) else ""
        jobs.append((f"scope:{label}", lambda label=label, files=files, listing=listing, second=second: dispatch_agent(
            f"Check the {label} of the manuscript in `{paper_dir}`: read {listing} in this order, as one text, against {truth}.\n{scope_rules}\n"
            f"{concern_txt}{report_only}{second}\nGive each issue the file it is in (one of: {', '.join(files)}).\n{ISSUE_FORMAT}",
            schema=ISSUE_SCHEMA, role=f"Check {label}", stage_name="check", read_only=True, tier="heavy")))

    picks = _pick_numeric_claims(ledger_path, plan, int(effort_setting("max_numeric_checks")))  # 0 = none
    if only and plan:
        only_ids = {str(c) for b in plan.get("blocks", []) if b.get("file") in only for c in (b.get("claim_ids") or [])}
        picks = [c for c in picks if str(c.get("id")) in only_ids]
    for c in picks:
        jobs.append((f"numeric:{c.get('id', '?')}", lambda c=c: _scripted_check_guarded(
            f"Independently check this ledger result as the manuscript in `{paper_dir}` states it:\n{json.dumps(c, ensure_ascii=False)}\n"
            f"Write and run a standalone script with `{resolve_python_exe()}` in `{paths['verify_scripts']}` that tests the identity or number "
            f"and its boundary cases. Do not edit manuscript files. Report an issue only where the manuscript's statement fails your check, "
            f"and cite the script as evidence.\n{ISSUE_FORMAT}",
            f"Numeric {c.get('id', '?')}")))

    cite_state: Optional[Dict[str, Any]] = None
    if effort()["cite_context_batch"] and cite_rep:
        ctx_files = [str(f.relative_to(paper_dir)).replace("\\", "/") for f in list_editable_tex(paper_dir)]
        ctx = citation_contexts(paper_dir, cite_rep, read_text_safe(wf / "01_literature_map.md"),
                                [f for f in ctx_files if not only or f in only])
        cite_jobs, cite_state = coarse_citation_jobs(paths, ctx)
        jobs += cite_jobs
        log(f"[check] citations: {len(cite_state['pairs'])} (sentence, work) pairs, {len(cite_state['pairs']) - len(cite_state['todo'])} "
            f"settled in an earlier run, {len(cite_jobs)} coarse checker(s)")
    elif effort()["cite_context_batch"]:
        log("[check] no citation audit report (offline?): the citation check is skipped")

    if want_reader and separate_reader:
        jobs.append(("reader", lambda: dispatch_agent(
            f"{reader_task}\n{concern_txt}\n{ISSUE_FORMAT}",
            schema=ISSUE_SCHEMA, role="Reader Check", stage_name="check", read_only=True, tier="heavy")))

    # One pool for every checker: the wall clock is ceil(jobs / max_parallel) rounds, not one round set per kind of check.
    reports = await run_batched(jobs, lambda job: job[1]())
    for (source, _), rep in zip(jobs, reports):
        if not source.startswith("cite-coarse:"):
            issues += collect_issues(paper_dir, [rep], source)
    cite_out: Optional[Dict[str, Any]] = None
    if cite_state is not None:
        cite_out = await finish_citation_check(paths, cite_state, [r for (s, _), r in zip(jobs, reports) if s.startswith("cite-coarse:")])
        issues += issues_from_citation_verdicts(cite_out["verdicts"])
    if only:
        issues = [i for i in issues if not i.get("file") or i.get("file") in only]

    issues = dedupe_issues(issues)
    concerns, issues = concerns_from_issues(issues)
    filed = add_feedback(paths, concerns, "check")
    issues_path, report, counts = _write_check_report(paths, issues, "", "Check Report", "Nothing was rewritten here; `mode=revise` hands these issues to the author.")
    log(f"Check: {len(issues)} issues (high {counts['high']}, medium {counts['medium']}, low {counts['low']}) from {len(jobs)} checkers"
        f"{f'; {len(filed)} concern(s) for the author' if filed else ''} -> {report}")
    return {"issues": len(issues), **counts, "checkers": len(jobs), "concerns": len(filed), "issues_json": str(issues_path), "report": str(report),
            "citations": {k: cite_out[k] for k in ("reused", "fulltext_read", "fulltext_unavailable", "report")} if cite_out else None}


# ------------------------------------------------------------------------------
# Stage 4: revise (the author fixes reported issues; `repair` is an alias)
# ------------------------------------------------------------------------------

REPAIR_INLINE_CHARS = 12000
REVISE_SCHEMA = {
    "type": "object",
    "properties": {
        "fixed": {"type": "array", "items": {"type": "string"}},
        "declined": {"type": "array", "items": {"type": "object", "properties": {"issue": {"type": "string"}, "reason": {"type": "string"},
                                                                                "decision_id": {"type": "string"}}, "required": ["issue", "reason"]}},
    },
    "required": ["fixed", "declined"],
}


def _is_existing_file(s: str) -> bool:
    """`repair_instructions` may be a path or the instructions themselves; long text is not a path
    (and on Linux Path(long_text).is_file() raises ENAMETOOLONG instead of returning False)."""
    if "\n" in s or len(s) > 1024:
        return False
    try:
        return Path(s).is_file()
    except (OSError, ValueError):
        return False


def _paper_rel(paper_dir: Path, raw: Any) -> Optional[str]:
    """Normalise a file reference (relative to paper_dir, './x', or absolute) to a paper-relative path; None if outside."""
    p = Path(str(raw).strip().replace("\\", "/"))
    cand = p if p.is_absolute() else paper_dir / p
    try:
        return str(cand.resolve().relative_to(paper_dir.resolve())).replace("\\", "/")
    except ValueError:
        return None


def part_files(paper_dir: Path, root_name: str) -> List[str]:
    """`root_name`'s own inputs in reading order, then the root file itself (main.tex holds the abstract: revised last)."""
    root = paper_dir / root_name
    if not root.exists():
        return []
    out: List[str] = []
    for line in read_text_safe(root).splitlines():
        code = re.split(r"(?<!\\)%", line, maxsplit=1)[0]
        for m in re.finditer(r"\\(?:input|include)\s*\{([^}]+)\}", code):
            name = m.group(1).strip()
            rel = name if name.lower().endswith(".tex") else name + ".tex"
            norm = _paper_rel(paper_dir, rel)
            if norm and not _is_skipped_generated_tex(Path(norm)) and (paper_dir / norm).exists() and norm not in out:
                out.append(norm)
    return out + [root_name]


def main_part_files(paper_dir: Path) -> List[str]:
    return part_files(paper_dir, "main.tex")


def _issue_lines(items: List[Dict[str, Any]]) -> List[str]:
    lines: List[str] = []
    for k, it in enumerate(items, 1):
        if "issues_to_fix" in it:  # routed from free-form instructions by the planner
            ref = f"  [see instructions: {it['instruction_refs']}]" if it.get("instruction_refs") else ""
            lines.append(f"{k}. {it['issues_to_fix']}{ref}")
            continue
        parts = [f"{k}. [{it.get('severity', 'medium')}] {it.get('location', '')}: {it.get('problem', '')}"]
        if it.get("quote"):
            parts.append(f"text: \"{it['quote']}\"")
        if it.get("evidence"):
            parts.append(f"evidence: {it['evidence']}")
        if it.get("suggested_fix"):
            parts.append(f"suggestion: {it['suggested_fix']}")
        lines.append(" | ".join(parts))
    return lines


def _revise_rules(paper_dir: Path, genre: str, story_path: Path, inst_path: Optional[Path], inst_len: int) -> Tuple[str, str]:
    story = f"The paper's story is in `{story_path}`; keep it intact.\n" if story_path.exists() else ""
    inst = (f"The full instructions are in `{inst_path}` ({inst_len} characters). Read the parts that concern your files there; text that must be "
            f"inserted or replaced verbatim (acknowledgments, abstract wording, quoted sentences, equations) must be copied exactly from that file, "
            f"never reconstructed from the summary below, and do not search other folders for it.\n") if inst_path else ""
    limit = ABSTRACT_LIMITS.get(genre)
    rules = (
        "Fix each issue with the smallest edit that makes the text right, and keep the story and the flow. In the abstract and the "
        "introduction, fix an overclaim by rewording the sentence or dropping the claim, never by adding qualifiers or hypotheses: those "
        "belong in the theorem statement or the Supplemental Material"
        + (f", and the abstract must stay within {limit} characters including spaces. " if limit else ". ")
        + "If an issue is mistaken, or its text is not where it says, leave the text and say why. If fixing it would go against one of "
        "the author's decisions, do not fix it: decline it and give the `decision_id`. "
        f"Keep the macros in `{paper_dir / 'macros.tex'}`."
    )
    return story + inst + decisions_note(story_path.parent) + hand_edit_note(story_path.parent), rules


def _revise_file_prompt(paper_dir: Path, rel: str, items: List[Dict[str, Any]], context: str, rules: str) -> str:
    return (
        f"Revise `{paper_dir / rel}`. You own this text.\n{context}"
        "Issues for this file (low severity = optional):\n" + "\n".join(_issue_lines(items)) + "\n"
        + rules + " Edit only this file. Reply with what you fixed and what you declined (with reasons)."
    )


def _revise_main_prompt(paper_dir: Path, order: List[str], by_file: Dict[str, List[Dict[str, Any]]], context: str, rules: str) -> str:
    targets = [r for r in order if r in by_file]
    reading = ", ".join(f"`{paper_dir / r}`" for r in order)
    blocks = "\n".join(f"### {r}\n" + "\n".join(_issue_lines(by_file[r])) for r in targets)
    return (
        f"You are the author of the main text and revise it as one piece. First read it whole, in reading order: {reading} "
        f"(main.tex holds the title and the abstract).\n{context}"
        "Then fix the issues below file by file in that order, the abstract last, so that the text still reads as one story "
        "(low severity = optional):\n" + blocks + "\n"
        + rules + f" Edit only these files: {', '.join(targets)}. Reply with what you fixed and what you declined (with reasons)."
    )


async def guarded_group_edit(paper_dir: Path, snap_dir: Path, targets: Sequence[str], call: Callable[[], Any]) -> Any:
    """Run one edit agent that may change several files; any other file it touched is restored from the checkpoint."""
    all_files = list_editable_tex(paper_dir) + [p for p in (paper_dir / "macros.tex", paper_dir / "refs.bib") if p.exists()]
    before = _file_hashes(all_files)
    res = await call()
    after = _file_hashes(all_files)
    allowed = {str((paper_dir / r).resolve()) for r in targets}
    for path_s, h in before.items():
        if path_s in allowed or after.get(path_s) == h:
            continue
        rel = os.path.relpath(path_s, paper_dir).replace("\\", "/")
        if (snap_dir / rel).exists():
            restore_tex_file(paper_dir, snap_dir, rel)
            log(f"[edit guard] restored {rel}: changed by an agent that was assigned other files")
    return res


async def run_revise(paths: Dict[str, Path], build_after: bool = True, prefer_referee: bool = False) -> Dict[str, Any]:
    paper_dir = paths["paper"]
    wf = paths["wf"]
    audit_dir = paths["audit"]
    args = _wf_args()
    genre = str(args.get("genre") or "long")
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    snap_dir = snapshot_project(paper_dir, paths["snapshots"] / f"{ts}_pre_revise")
    phase(f"Revise: the author fixes the reported issues (checkpoint {snap_dir.name})")
    if args.get("sync_bib") and Path(str(args["sync_bib"])).exists():
        write_user_file(paper_dir / "refs.bib", Path(str(args["sync_bib"])).read_bytes())

    raw_inst_full = str(args.get("repair_instructions") or "")
    raw_inst = raw_inst_full.strip()
    issues_path = wf / "03_issues.json"
    referee = audit_dir / "05_blind_referee_report.md"
    inst_path: Optional[Path] = None
    if raw_inst and _is_existing_file(raw_inst):
        inst_path = Path(raw_inst).resolve()
    elif raw_inst:
        # Inline text is saved byte-for-byte, so every worker can read the original rather than a planner's paraphrase.
        inst_path = wf / f"repair_instructions_{ts}.md"
        with open(inst_path, "w", encoding="utf-8", newline="") as fh:
            fh.write(raw_inst_full)
    elif prefer_referee and referee.exists():
        inst_path = referee
    elif not issues_path.exists() and referee.exists():
        inst_path = referee
    elif not issues_path.exists():
        raise RuntimeError("revise: nothing to work from. Run mode=check first, or pass repair_instructions (a report path or the text).")

    by_file: Dict[str, List[Dict[str, Any]]] = {}
    inst_len = 0
    skip_plan = False
    skipped_low: List[str] = []
    if inst_path is None:
        data = json.loads(read_text_safe(issues_path) or "{}")
        for it in data.get("issues", []) if isinstance(data, dict) else []:
            rel = resolve_paper_file(paper_dir, it.get("file") or "main.tex")
            if rel:
                by_file.setdefault(rel, []).append(it)
        source = str(issues_path)
        if not effort_setting("revise_low"):
            # A file whose issues are all low severity (copy-edit and flow hints) is not worth an agent call by default.
            skipped_low = sorted(r for r, items in by_file.items() if all(str(i.get("severity") or "medium") == "low" for i in items))
            for r in skipped_low:
                by_file.pop(r)
            if skipped_low:
                log(f"[revise] skipped {len(skipped_low)} files with only low-severity issues (revise_low=true includes them): {skipped_low}")
    else:
        inst_text = read_text_safe(inst_path)
        if not inst_text.strip():
            raise RuntimeError(f"revise: instructions file {inst_path} is empty")
        inst_len = len(inst_text)
        source = str(inst_path)
        truncated = inst_len > REPAIR_INLINE_CHARS
        if truncated:
            log(f"[revise] instructions are {inst_len} chars; the planner sees the first {REPAIR_INLINE_CHARS} and is told to read the full file {inst_path}")
        inst_excerpt = inst_text[:REPAIR_INLINE_CHARS] + (
            f"\n\n[... TRUNCATED here: {inst_len - REPAIR_INLINE_CHARS} more characters. Read the full file `{inst_path}`.]" if truncated else "")
        explicit = [_paper_rel(paper_dir, x) for x in (args.get("repair_files") or [])]
        if any(t is None for t in explicit):
            raise RuntimeError(f"repair_files must point inside {paper_dir}: {args.get('repair_files')}")
        explicit_s = [t for t in explicit if t]
        skip_plan = bool(args.get("repair_skip_plan")) or len(explicit_s) == 1
        if args.get("repair_skip_plan") and not explicit_s:
            raise RuntimeError("repair_skip_plan=true needs repair_files (the files to hand the full instructions to)")
        if skip_plan:
            log(f"[revise] planner skipped; each of {explicit_s} receives the full instructions")
            for rel in explicit_s:
                by_file[rel] = [{"issues_to_fix": "Apply every instruction in the instructions file that concerns this file.", "instruction_refs": "(whole file)"}]
        else:
            plan_res = await dispatch_agent(
                f"Route the revision instructions in `{inst_path}` ({inst_len} characters{'; the excerpt below is TRUNCATED, read the whole file' if truncated else ''}) "
                f"to the files of the paper in `{paper_dir}` (`macros.tex`, `refs.bib`, `sections/`, `main.tex`, `supplement.tex`).\n"
                f"Return per-file work items: `target_file` (relative to `{paper_dir}`), `issues_to_fix` (what to change) and `instruction_refs` "
                f"(where in the instructions each fix is specified: heading, quoted first words, or line numbers). NEVER paraphrase text that must "
                f"be inserted or replaced verbatim; point to it in `instruction_refs`.\n"
                f"Explicit target filter (if non-empty, plan only these files): {explicit_s}\n\nInstructions (excerpt):\n{inst_excerpt}",
                schema={
                    "type": "object",
                    "properties": {"work_items": {"type": "array", "items": {"type": "object", "properties": {
                        "target_file": {"type": "string"}, "issues_to_fix": {"type": "string"}, "instruction_refs": {"type": "string"}},
                        "required": ["target_file", "issues_to_fix"]}}},
                    "required": ["work_items"],
                },
                role="Plan Revisions",
                stage_name="revise",
                tier="heavy",
            )
            if not isinstance(plan_res, dict):
                raise RuntimeError(f"Revision planner returned a non-object result: {str(plan_res)[:300]}")
            for it in plan_res.get("work_items", []):
                if not isinstance(it, dict) or not it.get("target_file"):
                    continue
                rel = resolve_paper_file(paper_dir, str(it["target_file"]))
                if rel is None:
                    log(f"[revise] skipped work item outside the paper directory: {it.get('target_file')}")
                    continue
                if explicit_s and rel not in explicit_s:
                    log(f"[revise] skipped work item for {rel}: not in repair_files")
                    continue
                by_file.setdefault(rel, []).append({"issues_to_fix": str(it.get("issues_to_fix", "")), "instruction_refs": str(it.get("instruction_refs", ""))})

    only = only_files(paper_dir)
    if only:
        by_file = {k: v for k, v in by_file.items() if k in only}
    story_path = wf / "01_story.md"
    context, rules = _revise_rules(paper_dir, genre, story_path, inst_path, inst_len)
    main_order = main_part_files(paper_dir)
    main_targets = [r for r in main_order if r in by_file]
    others = [r for r in sorted(by_file) if r not in main_order]  # SM, macros, refs.bib: technical, revised in parallel

    def worker(rel: str) -> Any:
        return dispatch_agent(_revise_file_prompt(paper_dir, rel, by_file[rel], context, rules),
                              schema=REVISE_SCHEMA, role=f"Revise: {Path(rel).name}", stage_name="revise")

    results: Dict[str, Any] = dict(zip(others, await run_guarded_edit_batches(paper_dir, snap_dir, others, worker)))
    if main_targets:
        # The main text is one story: one author reads all of it and fixes its issues in one pass, abstract last.
        results["main text"] = await guarded_group_edit(
            paper_dir, snap_dir, main_targets,
            lambda: dispatch_agent(_revise_main_prompt(paper_dir, main_order, by_file, context, rules),
                                   schema=REVISE_SCHEMA, role="Revise: main text", stage_name="revise"),
        )

    # Math changes are reported, not blocked: some issues are wrong formulas that must change.
    math_changes: Dict[str, Any] = {}
    for rel in main_targets + others:
        before, after = snap_dir / rel, paper_dir / rel
        if rel.endswith(".tex") and before.exists() and after.exists() and before.read_bytes() != after.read_bytes():
            rc, _, _, chk = run_tool("check_math_integrity.py", ["--before", str(before), "--after", str(after), "--allow-cite-changes"], allow_fail=True)
            if rc != 0 and chk:
                math_changes[rel] = {k: chk.get(k) for k in ("mismatches_count", "quantifier_mismatches_count", "prose_lock_mismatches_count")}

    lines = [f"# Revise Report ({ts})", "", f"- **Checkpoint**: `{snap_dir}`", f"- **Worked from**: `{source}`{' (planner skipped)' if skip_plan else ''}"]
    if skipped_low:
        lines.append(f"- **Skipped (only low-severity issues; revise_low=true includes them)**: {', '.join(skipped_low)}")
    lines.append("")
    declined_total = 0
    for key in (["main text"] if main_targets else []) + others:
        r = results.get(key)
        lines += [f"## {key}" + (f" ({', '.join(main_targets)})" if key == "main text" else ""), ""]
        if isinstance(r, dict):
            lines += [f"- fixed: {x}" for x in r.get("fixed") or []]
            for d in r.get("declined") or []:
                declined_total += 1
                lines.append(f"- declined: {d.get('issue', '') if isinstance(d, dict) else d} \u2014 {d.get('reason', '') if isinstance(d, dict) else ''}")
        else:
            lines.append(f"- {str(r)[:400]}")
        lines.append("")
    if math_changes:
        lines += ["## Math changed (review it)", ""] + [f"- {rel}: {v}" for rel, v in math_changes.items()] + [""]
    report = audit_dir / "06_revise_report.md"
    report.write_text("\n".join(lines), encoding="utf-8")
    conflicts = [{"kind": "conflict", "blocking": False, "finding": f"not fixed because it goes against a decision: {d.get('issue', '')}",
                  "evidence": str(d.get("reason") or ""), "decision_id": str(d.get("decision_id") or ""),
                  "options": ["keep the decision and the text", "change the decision so the issue can be fixed"]}
                 for r in results.values() if isinstance(r, dict) for d in (r.get("declined") or [])
                 if isinstance(d, dict) and str(d.get("decision_id") or "").strip()]
    add_feedback(paths, conflicts, "revise")
    out: Dict[str, Any] = {"checkpoint": str(snap_dir), "instructions": source, "planner_skipped": skip_plan,
                           "revised_files": main_targets + others, "skipped_low": skipped_low, "agent_calls": len(others) + (1 if main_targets else 0),
                           "declined": declined_total, "math_changed_files": sorted(math_changes), "report": str(report)}
    if build_after:
        out["after_revise"] = await run_check(paths, llm=False)
        out.update(await run_build(paths))
    return out


# ------------------------------------------------------------------------------
# Clean-up and build (deterministic; the only LLM call fixes compile errors)
# ------------------------------------------------------------------------------

def run_cleanup_tools(paths: Dict[str, Path]) -> Dict[str, Any]:
    """Invisible characters, typography, copy-edit slips. No profile is passed, so no connector is ever swapped."""
    _, _, _, wm = run_tool("clean_watermarks.py", ["--paths", str(paths["paper"]), "--fix", "--json-out", str(paths["wf"] / "05_clean_wm.json")], allow_fail=True)
    return wm or {}


async def run_cleanup(paths: Dict[str, Path]) -> Dict[str, Any]:
    phase("Clean-up (deterministic only; v3 has no LLM polishing)")
    run_cleanup_tools(paths)
    files = [str(f) for f in list_editable_tex(paths["paper"])]
    stylo_md = paths["audit"] / "03_stylometrics_report.md"
    if files:
        run_tool("style_metrics.py", ["verify", "--inputs", *files, "--profile", str(ensure_default_profile(paths)), "--json-out",
                                      str(paths["wf"] / "05_stylometrics.json"), "--md-out", str(stylo_md), "--no-lm"], allow_fail=True)
    return {"clean_wm_json": str(paths["wf"] / "05_clean_wm.json"), "stylometrics_report": str(stylo_md)}


async def run_build(paths: Dict[str, Path]) -> Dict[str, Any]:
    paper_dir = paths["paper"]
    wf = paths["wf"]
    main_tex = paper_dir / "main.tex"
    supp_tex = resolve_supplement_path(paper_dir)
    flat_main = paths["submission"] / "main_arxiv.tex"
    flat_supp = paths["submission"] / "supplement_arxiv.tex"
    build_json = wf / "05_build_report.json"

    phase("Clean-up, TeX Dual Build, Layout Check and Sanitized arXiv Flattening")
    run_cleanup_tools(paths)
    clean, missing = False, []
    for attempt in range(3):
        cli = ["--main", str(main_tex), "--compile", "--flatten-out", str(flat_main), "--inline-bbl", "--json-out", str(build_json)]
        if supp_tex and supp_tex.exists():
            cli.extend(["--supplement", str(supp_tex), "--flatten-supplement-out", str(flat_supp)])
        rc, _, _, bld = run_tool("build_and_flatten_tex.py", cli, allow_fail=True)
        cr = (bld or {}).get("compile_result", {})
        scr = (bld or {}).get("supplement_compile_result", {})
        lc = (bld or {}).get("layout_check", {})
        leaks = (bld or {}).get("submission_leaks", []) + (bld or {}).get("supplement_submission_leaks", [])
        missing, other_errors = missing_tex_files(cr, scr)
        rest_clean = (
            not other_errors
            and not lc.get("layout_issues")
            and not leaks
            and not cr.get("overfull_gt10pt")
            and not (scr and scr.get("overfull_gt10pt"))
        )
        clean = bool(rc == 0 and cr.get("clean_pass") and (not scr or scr.get("clean_pass")) and rest_clean)
        if missing:
            log(f"[build] missing files {missing}: figures come from mode=figures (which also checks them); the compile fixer never makes them")
        if clean or attempt == 2 or (missing and rest_clean):
            log(f"Build finished (attempt {attempt + 1}): clean={clean}, missing={len(missing)}, leaks={len(leaks)}, layout_issues={len(lc.get('layout_issues', []))}")
            break
        fig_before = set(paths["figures"].rglob("*")) if paths["figures"].exists() else set()
        await dispatch_agent(
            f"Fix TeX compilation, layout (e.g. `\\letterhead` missing `\\par`, orphan page, >10pt overfull, hyperref `\\texorpdfstring`), or arXiv "
            f"submission leaks in `{paper_dir}` without altering equations or the wording of the prose. Placeholders meant for the human author "
            f"(author list, acknowledgments) stay as they are. Never create, regenerate or edit figure files or figure scripts: a missing figure "
            f"is made by the figures stage, which also checks it, so leave those errors in place"
            + (f" (missing now: {missing})" if missing else "") + f".\nDiagnostics:\n{json.dumps(bld, indent=2, ensure_ascii=False)}",
            schema={"type": "string"},
            role=f"Build/Layout Fix #{attempt + 1}",
            stage_name="compile_fix",
            tier="fast",
        )
        for f in sorted(set(paths["figures"].rglob("*")) - fig_before):
            if f.is_file():  # made by the compile fixer despite the rule: moved aside, never used unchecked
                dest = wf / "readonly_violations" / f"{datetime.datetime.now().strftime('%Y%m%d_%H%M%S_%f')}_{f.name}"
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(f), str(dest))
                log(f"[build] the compile fixer created {f.name} in figures/; moved it to {dest} (figures come from mode=figures)")
    return {"build_json": str(build_json), "main_arxiv": str(flat_main), "clean": clean, "missing_files": missing}


def missing_tex_files(*compile_results: Optional[Dict[str, Any]]) -> Tuple[List[str], List[str]]:
    """Split LaTeX errors into missing input/graphics files and everything else."""
    missing: set = set()
    other: List[str] = []
    for res in compile_results:
        for err in (res or {}).get("latex_errors") or []:
            m = re.search(r"File [`']([^'`]+)' not found", str(err))
            if m:
                missing.add(m.group(1))
            else:
                other.append(str(err))
    return sorted(missing), other


# ==============================================================================
# On-Demand Blind Referee Panel Stage (Strict Context Isolation & Cross-Vendor)
# ==============================================================================

def _build_referee_sandbox(paper_dir: Path, wf: Path, fig_renders: Path, sandbox: Path) -> List[str]:
    copied: List[str] = []

    def _cp(src: Path, rel: str) -> None:
        copy_to_workflow(src, sandbox / rel)
        copied.append(rel)

    for f in list_editable_tex(paper_dir):
        _cp(f, str(f.relative_to(paper_dir)))
    for pat in ("macros.tex", "*.bib", "*.bbl", "*.bst", "*.cls", "*.sty"):
        for f in paper_dir.glob(pat):
            if f.is_file() and str(f.relative_to(paper_dir)) not in copied:
                _cp(f, f.name)
    for name in ("main_arxiv.tex", "supplement_arxiv.tex"):
        f = paper_dir / "submission" / name
        if f.exists():
            _cp(f, f"submission/{name}")
    if fig_renders.exists():
        for f in sorted(fig_renders.glob("*.png")):
            _cp(f, f"renders/{f.name}")
    for name in ("05_tex_defs_audit.json", "03_tex_defs_audit.json", "04_citation_audit.json", "06_figure_verify.json", "05_build_report.json"):
        f = wf / name
        if f.exists():
            _cp(f, f"audit/{name}")
    (sandbox / "verify_scripts").mkdir(exist_ok=True)
    return copied


async def run_blind_referee(paths: Dict[str, Path]) -> Dict[str, Any]:
    paper_dir = paths["paper"]
    wf = paths["wf"]
    audit_dir = paths["audit"]
    fig_dir = paths["figures"]
    fig_renders = paths["fig_renders"]
    fig_style = str(_wf_args().get("figure_style") or "prl").lower()

    phase("Blind Referee Panel Stage (Context-Isolated Specialist + Broad Reader + Editor)")
    if fig_dir.exists() and list(fig_dir.glob("*.pdf")):
        run_tool("verify_figures.py", ["--pdf-dir", str(fig_dir), "--style", fig_style, "--renders-out", str(fig_renders), "--json-out", str(wf / "06_figure_verify.json")], allow_fail=True)
    run_tool("audit_tex_defs.py", ["--main", str(paper_dir / "main.tex"), "--json-out", str(wf / "05_tex_defs_audit.json")], allow_fail=True)
    if (paper_dir / "refs.bib").exists():
        run_tool("verify_citations.py", ["audit", "--tex-dir", str(paper_dir), "--bib", str(paper_dir / "refs.bib"), "--cache", str(wf / "04_api_bib_cache.json"), "--json-out", str(wf / "04_citation_audit.json")], allow_fail=True)

    # Real isolation for CLI backends: the panel runs inside a throw-away copy that contains only the
    # manuscript, its bibliography, the rendered figure PNGs and the deterministic audit JSONs. The agents'
    # working directory and only readable directory is that copy, so notes, ledgers and .workflow/ are
    # out of reach (not merely forbidden by the prompt), and anything they write lands in the copy.
    sandbox = Path(tempfile.mkdtemp(prefix="wp_referee_"))
    try:
        _build_referee_sandbox(paper_dir, wf, fig_renders, sandbox)
        sb = sandbox
        isol_rule = (
            f"BLIND REVIEW: everything you may use is in `{sb}` (manuscript sources, .bib/.bbl, `renders/*.png`, `audit/*.json`, "
            f"`submission/`). Do not look for author notes or workflow files elsewhere. {PDF_SAFETY_RULE}"
        )
        roles = [
            ("Referee A (Specialist)", f"Act as a strict narrow-field specialist Referee for the manuscript in `{sb}`. Audit EVERY theorem hypothesis, quantifier, proof step (`Lemma` completeness, boundary conditions, counterexamples via `{resolve_python_exe()}` scripts you write in `{sb / 'verify_scripts'}`), and numerical claim across main + Supplemental `.tex`. Rank findings strictly into P0/P1/P2/P3 with file:line.\n{isol_rule}"),
            ("Referee B (Broad Reader)", f"Act as a broad condensed-matter / theoretical-physics Referee reading `{sb / 'main.tex'}`, its `sections/*.tex`, and PNG previews in `{sb / 'renders'}` (do NOT lean on Supplemental Material to rescue missing main-text definitions). First answer, from the abstract and the introduction alone: what is the question, why does it matter, how far did earlier work get, and what is new here; say plainly where the text does not tell you. Then flag every undefined symbol, missing microscopic formula, math jargon without intuition, choppy prose, and visual clutter.\n{isol_rule}"),
            ("Associate Editor (Format & Policy)", f"Act as Journal Associate Editor inspecting `{sb / 'main.tex'}`, `*.bbl`, `{sb / 'submission'}` and the audit JSONs in `{sb / 'audit'}` (build report, citation audit, figure verification). Check abstract char/page budgets, run-in heading `\\par` breaks, SM `\\cite` formatting, `.bbl` duplicate `arXiv:`/math downcasing, arXiv leak sanitization, and print pt legibility.\n{isol_rule}"),
        ]
        panel_reports = await run_batched(
            roles,
            lambda rp: dispatch_agent(
                rp[1], schema={"type": "string"}, role=rp[0], stage_name="blind_referee", read_only=True, tier="heavy",
                add_dirs=[str(sb)], cwd=str(sb), allow_bash_read_only=rp[0].startswith("Referee A"),
            ),
        )
    finally:
        try:
            force_rmtree(sandbox)
        except OSError as exc:
            log(f"[referee] could not remove the sandbox copy {sandbox}: {exc}")

    out_md = audit_dir / "05_blind_referee_report.md"
    out_md.write_text(
        "# Blind Three-Role Referee & Format Editor Report\n\n"
        + "\n\n---\n\n".join(f"## {rp[0]}\n\n{rep}" for rp, rep in zip(roles, panel_reports))
        + "\n",
        encoding="utf-8",
    )
    return {"blind_referee_report": str(out_md), "next": "mode=revise with repair_instructions set to this report hands it to the author"}


# ==============================================================================
# Smoke Test & Entry Router
# ==============================================================================

async def run_smoke_test(paths: Dict[str, Path]) -> Dict[str, Any]:
    phase("Smoke Test v3: Python Env Resolution, 7 CLI Tools & Agent Pings")
    py_exe = resolve_python_exe()
    expected_tools = [
        "check_math_integrity.py",
        "clean_watermarks.py",
        "style_metrics.py",
        "verify_citations.py",
        "build_and_flatten_tex.py",
        "verify_figures.py",
        "audit_tex_defs.py",
    ]
    tool_checks = {}
    for t in expected_tools:
        rc, _, err, _ = run_tool(t, ["--help"], allow_fail=True)
        tool_checks[t] = (rc == 0)
        if rc != 0:
            raise RuntimeError(f"Tool {t} failed --help using {py_exe}: {err}")

    if _STATE.get("skip_agent_pings"):
        pings = ["skipped"]
    else:
        pings = await run_batched(
            ["ping-1", "ping-2"],
            lambda label: dispatch_agent(
                f"Reply immediately with '{label}:ok'.",
                schema={"type": "string"},
                role=f"Smoke {label}",
                stage_name="draft",
                tier="probe",
                read_only=True,
            ),
        )
    log(f"Runtime={_STATE['runtime']} Backend={_STATE['backend']} Python={py_exe}; Tools OK={tool_checks}; Pings={pings}")
    return {"runtime": _STATE["runtime"], "python_exe": py_exe, "tools_ok": tool_checks, "pings": pings}


def _pause_enabled() -> bool:
    """`pause_after_story` (default true): stop for the author at the briefing, the story confirmation and blocking
    concerns. False = an unattended run (the agent chooses the story; concerns wait in the list)."""
    v = _wf_args().get("pause_after_story")
    return True if v is None else bool(v)


def _paused(out: Dict[str, Any], kind: str, paths: Dict[str, Path]) -> Dict[str, Any]:
    wf = paths["wf"]
    if kind == "discuss":
        msg = (
            f"Briefing ready: {wf / '01_briefing.md'} (literature map: {wf / '01_literature_map.md'}). Talk it through with the author in "
            "this order: (1) give the core information of the briefing (results with their evidence, literature, limits, open questions) "
            "without proposing a storyline or recommending anything; (2) ask for the author's idea and let them speak first; (3) only then "
            "give your own view (what supports or weakens their idea, what history the introduction would need, likely referee objections, "
            f"alternatives you see); (4) discuss until you agree. Write the agreed points, including what was decided against and why, to "
            f"{wf / '01_discussion.md'}, then run mode=auto again."
        )
    elif kind == "confirm":
        msg = (
            f"Story, block plan and decision record written from the discussion: {wf / '01_story.md'}, {_plan_path()}, "
            f"{wf / '01_decisions.json'}. Show the author the story, including the choices under 'Filled in by the AI'. When the author "
            "confirms (or has edited these files), run mode=auto again: the story is locked and writing starts."
        )
    else:
        blk = open_blocking(load_feedback(wf))
        top = "\n".join(f"- {i['id']} [{i.get('kind', '')}]: {i.get('finding', '')}" for i in blk[:MAX_BLOCKING_SHOWN])
        more = f"\n(and {len(blk) - MAX_BLOCKING_SHOWN} more)" if len(blk) > MAX_BLOCKING_SHOWN else ""
        msg = (
            f"{len(blk)} concern(s) need the author's answer before the run goes on:\n{top}{more}\nFull list: "
            f"{paths['audit'] / '07_feedback.md'}. For each, tell the author the finding, the evidence, the decision it touches and what "
            "that decision rested on, the options and the recommendation. Record each answer in "
            f"{_feedback_path(wf)} (`answer`, `status`: \"answered\", and `keep`: true when the author keeps the decision), then run mode=auto again."
        )
    log(msg)
    return {**out, "paused": kind, "next": msg}


def _gate(paths: Dict[str, Path], out: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if _pause_enabled() and open_blocking(load_feedback(paths["wf"])):
        return _paused(out, "concerns", paths)
    return None


def _story_needed(paths: Dict[str, Path]) -> bool:
    """Text is missing (not merely marked for rewriting): only then does an unlocked paper go through the story."""
    paper_dir = paths["paper"]
    blocks = (current_plan(paths) or {}).get("blocks", [])
    return not blocks or any(block_status(paper_dir, b["file"]) != "full" for b in blocks) or abstract_missing(paper_dir)


def _writing_needed(paths: Dict[str, Path]) -> bool:
    paper_dir = paths["paper"]
    blocks = (current_plan(paths) or {}).get("blocks", [])
    return (not blocks or any(block_status(paper_dir, b["file"]) != "full" for b in blocks)
            or bool(_load_rewrite(paths["wf"])) or abstract_missing(paper_dir) or bool(only_files(paper_dir)))


async def run_auto(paths: Dict[str, Path], start: str = "") -> Dict[str, Any]:
    """Run what the paper still needs, from wherever it stands. Stops for the author at the briefing, at the story
    confirmation and at blocking concerns (unless pause_after_story=false)."""
    wf, paper_dir = paths["wf"], paths["paper"]
    start = start or str(_wf_args().get("start") or "")
    if start not in AUTO_STARTS:
        raise ValueError(f"start must be one of {AUTO_STARTS[1:]}, not {start!r}")
    pause = _pause_enabled()
    st = run_survey(paths)
    out: Dict[str, Any] = {"effort": effort_level(), "survey": {"next": st["next"], "hand_edits": st["hand_edits"],
                                                               "blocks": {b["id"]: b["status"] for b in st["blocks"]}}}
    log(f"[survey] effort={out['effort']}; next: {st['next']} (details: {paths['audit'] / '00_survey.md'})")
    if st["concerns"]["answered"]:
        out["answers"] = await apply_feedback(paths)
    gate = _gate(paths, out)
    if gate:
        return gate

    late = start in ("check", "revise", "build")
    if not late or not (wf / "00_ground_truth_ledger.json").exists():
        out["inventory"] = await run_inventory(paths)

    # The story: briefing -> the author's discussion -> story, plan and decisions -> the author confirms -> lock.
    story_p, brief_p, disc_p = wf / "01_story.md", wf / "01_briefing.md", wf / "01_discussion.md"
    if start == "brief" and (wf / "01_lock.json").exists():
        (wf / "01_lock.json").unlink()
    if not late and start != "draft" and (start in ("brief", "story") or (not lock_valid(wf) and _story_needed(paths))):
        if start == "brief" or (not story_p.exists() and not brief_p.exists()):
            out["brief"] = await run_brief(paths, out.get("inventory"))
            if pause:
                return _paused(out, "discuss", paths)
        if not story_p.exists() or start in ("brief", "story") or (disc_p.exists() and disc_p.stat().st_mtime > story_p.stat().st_mtime):
            if pause and not disc_p.exists() and not story_p.exists():
                return _paused(out, "discuss", paths)
            out["story"] = await run_story(paths, out.get("inventory"))
            if pause:
                return _paused(out, "confirm", paths)
        out["lock"] = await lock_story(paths)

    # Writing: only what is missing, partial or marked for rewriting.
    if not late and (start == "draft" or _writing_needed(paths)):
        out["draft"] = await run_draft(paths)
        gate = _gate(paths, out)
        if gate:
            return gate
        specs = _figure_specs(paths) if _wants_figures(paths) else []
        if any(_figure_missing(paths, sp) for sp in specs):
            out["figures"] = await run_figures(paths, only_missing=True)

    # Check, revise, build: once per change of the text.
    if start == "build":
        out["build"] = await run_build(paths)
    elif start == "revise":
        out["revise"] = await run_revise(paths, build_after=False)
        gate = _gate(paths, out)
        if gate:
            return gate
        out["after_revise"] = await run_check(paths, llm=False)
        out["build"] = await run_build(paths)
    elif start == "check" or not run_survey(paths)["checked_since_last_change"]:
        out["check"] = await run_check(paths)
        gate = _gate(paths, out)
        if gate:
            return gate
        if out["check"].get("issues"):
            out["revise"] = await run_revise(paths, build_after=False)
            gate = _gate(paths, out)
            if gate:
                return gate
            out["after_revise"] = await run_check(paths, llm=False)
        out["build"] = await run_build(paths)
    else:
        out["check"] = {"skipped": True, "reason": "the text has not changed since the last check"}
    if "build" in out:
        (wf / "06_checked_hashes.json").write_text(json.dumps(text_hashes(paper_dir), indent=1), encoding="utf-8")
        if effort()["referee"]:
            out["blind_referee"] = await run_blind_referee(paths)

    open_items = [i for i in load_feedback(wf)["items"] if i.get("status") == "open"]
    out["concerns_open"] = len(open_items)
    out["next"] = (
        f"{len(open_items)} concern(s) wait for the author: {paths['audit'] / '07_feedback.md'}. Answer them in {_feedback_path(wf)} and "
        "run mode=auto again." if open_items else
        "Done." + ("" if effort()["referee"] else " Optional: mode=blind_referee for an isolated referee panel, then mode=revise with its report.")
    )
    return out


async def _run_mode(mode: str, raw: str, paths: Dict[str, Path]) -> Dict[str, Any]:
    if mode == "auto" or mode in PIPELINE_STARTS:
        return await run_auto(paths, PIPELINE_STARTS.get(mode, ""))
    if mode == "survey":
        return run_survey(paths)
    if mode == "inventory":
        return await run_inventory(paths)
    if mode == "brief":
        return await run_brief(paths)
    if mode == "story":
        return await run_story(paths)
    if mode == "draft":
        return await run_draft(paths)
    if mode == "figures":
        return await run_figures(paths)
    if mode == "check":
        return await run_check(paths)
    if mode == "revise":
        return await run_revise(paths, build_after=True, prefer_referee=(raw == "repair"))
    if mode == "cleanup":
        return await run_cleanup(paths)
    if mode == "build":
        return await run_build(paths)
    if mode == "blind_referee":
        return await run_blind_referee(paths)
    raise ValueError(f"Unsupported mode: {raw}")


async def execute_mode() -> Dict[str, Any]:
    args = _wf_args()
    raw = str(args.get("mode") or "smoke_test")
    mode = MODE_ALIASES.get(raw, raw)
    if mode != raw:
        log(f"[mode] '{raw}' is called '{mode}' since v4" if raw == "full_auto" else f"[mode] '{raw}' is called '{mode}' since v3")
    paper_dir = Path(str(args["paper_dir"]))
    paths = ensure_dirs(paper_dir)
    preflight_permissions(paper_dir)
    if mode == "smoke_test":
        return await run_smoke_test(paths)
    detect_hand_edits(paths)
    before = text_hashes(paper_dir)
    try:
        return await _run_mode(mode, raw, paths)
    finally:
        # What the workflow changed in this run (also in a run that stopped) is its own text, not a hand edit.
        remember_text(paths, before)


async def run_from_antigravity(host_globals: Dict[str, Any]) -> Dict[str, Any]:
    """Entry point invoked inside Antigravity `run_workflow` sandbox."""
    wf_args = dict(host_globals.get("args") or {})
    workspaces = list(host_globals.get("WORKSPACES") or ())
    if not wf_args.get("paper_dir") and workspaces:
        wf_args["paper_dir"] = workspaces[0]

    _STATE.update({
        "runtime": "antigravity",
        "backend": "antigravity",
        "referee_backend": str(wf_args.get("referee_backend") or ""),
        "check_backend": str(wf_args.get("check_backend") or ""),
        "wf_args": wf_args,
        "models_cfg": dict(wf_args.get("models") or {}),
        "batch_size": initial_batch_size(wf_args),
        "inherit_deadlock": False,
        "ag_agent": host_globals.get("agent"),
        "ag_parallel": host_globals.get("parallel"),
        "ag_phase": host_globals.get("phase"),
        "ag_log": host_globals.get("log"),
        "skip_agent_pings": False,
        "agent_timeout": int(wf_args.get("agent_timeout_s") or 1800),
        "agent_retries": int(wf_args["agent_retries"]) if wf_args.get("agent_retries") is not None else 2,
        "max_scan_agents": int(wf_args.get("max_scan_agents") or 24),
        "routing_policy": str(wf_args.get("routing_policy") or "auto_tiered"),
    })
    log(f"[engine] write-paper workflow_engine {ENGINE_VERSION} loaded from {Path(__file__).resolve().parent} "
        f"(pass Args.skill_dir to run a different copy)")
    _STATE["in_plan_mode"] = await resolve_plan_mode(str(wf_args.get("plan_mode") or "auto"), host_globals.get("trajectory"))
    log(f"[routing] policy={_STATE['routing_policy']} in_plan_mode={_STATE['in_plan_mode']} "
        f"(False = Execution Turn: inherit-tier calls go to the agy CLI if available, else to model='pro')")
    res = await execute_mode()
    print(json.dumps(res, indent=2, ensure_ascii=False))
    return res


def _step_field(step: Any, name: str) -> Any:
    if isinstance(step, dict):
        return step.get(name)
    return getattr(step, name, None)


async def detect_plan_mode(trajectory: Any) -> Optional[bool]:
    """Is the parent Antigravity session in a Planning Turn right now?

    Every USER_INPUT switches the host back to plan mode without recording an `enter_plan_mode` call, so
    plan mode is on iff the latest USER_INPUT or completed enter_plan_mode comes after the latest completed
    exit_plan_mode. Returns None when the trajectory API is unavailable or fails (routing then falls back to
    trying `inherit` once and switching to 'pro' on the deadlock error).
    """
    if not callable(trajectory):
        return None
    try:
        u_steps = list(await trajectory(types=["USER_INPUT"], last=1) or [])
        t_steps = list(await trajectory(tools=["exit_plan_mode", "enter_plan_mode"], last=5) or [])
    except Exception as exc:
        log(f"[routing] trajectory() unavailable ({type(exc).__name__}: {exc}); plan mode unknown")
        return None

    def _idx(s: Any) -> int:
        try:
            return int(_step_field(s, "index"))
        except (TypeError, ValueError):
            return -1

    last_u = max((_idx(s) for s in u_steps), default=-1)
    done = [s for s in t_steps if str(_step_field(s, "status") or "").upper() == "DONE"]
    last_enter = max((_idx(s) for s in done if _step_field(s, "tool") == "enter_plan_mode"), default=-1)
    last_exit = max((_idx(s) for s in done if _step_field(s, "tool") == "exit_plan_mode"), default=-1)
    if last_u < 0 and last_enter < 0 and last_exit < 0:
        return None
    return max(last_u, last_enter) > last_exit


async def resolve_plan_mode(setting: str, trajectory: Any) -> Optional[bool]:
    s = (setting or "auto").lower()
    if s == "on":
        return True
    if s == "off":
        return False
    return await detect_plan_mode(trajectory)


async def run_from_cli(
    args_dict: Dict[str, Any],
    backend: str = "auto",
    referee_backend: str = "",
    skip_agent_pings: bool = False,
    check_backend: str = "",
) -> Dict[str, Any]:
    """Entry point invoked by `run_paper.py` under Claude Code, Codex, agy CLI, or terminal."""
    _STATE.update({
        "runtime": "cli",
        "backend": backend,
        "referee_backend": referee_backend or str(args_dict.get("referee_backend") or ""),
        "check_backend": check_backend or str(args_dict.get("check_backend") or ""),
        "wf_args": dict(args_dict),
        "models_cfg": dict(args_dict.get("models") or {}),
        "batch_size": initial_batch_size(args_dict),
        "inherit_deadlock": False,
        "skip_agent_pings": skip_agent_pings,
        "agent_timeout": int(args_dict.get("agent_timeout_s") or 1800),
        "agent_retries": int(args_dict["agent_retries"]) if args_dict.get("agent_retries") is not None else 2,
        "max_scan_agents": int(args_dict.get("max_scan_agents") or 24),
        "routing_policy": str(args_dict.get("routing_policy") or "auto_tiered"),
        "in_plan_mode": None,
    })
    return await execute_mode()
