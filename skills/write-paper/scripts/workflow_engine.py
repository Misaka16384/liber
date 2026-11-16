#!/usr/bin/env python3
"""Universal, dual-mode modular orchestrator for /write-paper (v2).

Runs seamlessly inside:
1. Antigravity `run_workflow` (via `paper_workflow.py` -> `run_from_antigravity(globals())`)
2. Standalone Python CLI across Claude Code (`claude`), Antigravity CLI (`agy`),
   or OpenAI Codex CLI (`codex`) (via `run_paper.py` -> `run_from_cli(args_dict)`).

Supports all 10 on-demand atomic phases (`inventory`, `blueprint`, `draft`, `figures`,
`verify`, `citations`, `hygiene`, `build`, `blind_referee`, `repair`) plus composite
pipelines (`draft_and_audit`, `audit_polish`, `full_auto`, `smoke_test`).
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


ENGINE_VERSION = "2.1.0"
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


def resolve_cli_backend(stage_name: str = "") -> Tuple[str, str]:
    """Select active CLI backend and binary path ('claude', 'agy', or 'codex')."""
    if stage_name == "blind_referee" and _STATE.get("referee_backend"):
        req = str(_STATE["referee_backend"]).lower()
    else:
        req = str(_STATE.get("backend") or "auto").lower()

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
#   heavy   - deep reading / auditing / planning (source scans, reverse blueprint audit, 3A/3C reviewers,
#             blind referees, repair planner)
#   builder - multi-step authoring that writes files (ledger synthesis, blueprint, drafting, figures,
#             SymPy scripts, citation and repair edits)
#   fast    - short mechanical fixes (copy-edit, compile fix, JSON repair)
#   probe   - connectivity pings
#
# Precedence: an explicit per-stage model in `models` > routing_policy. Under `uniform_inherit` every call
# uses the host/CLI default, as before tiering existed.
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


def route_call(stage_name: str, tier: str) -> Route:
    runtime = _STATE["runtime"]
    explicit = str((_STATE["models_cfg"] or {}).get(stage_name) or "")
    policy = str(_STATE.get("routing_policy") or "auto_tiered")
    in_plan = _STATE.get("in_plan_mode")
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


_READONLY_WF_FILES = ("00_locks.json", "00_ground_truth_ledger.json", "00_style_profile.json", "01_blueprint.json")


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
) -> List[Any]:
    """Run per-file edit agents in batches; afterwards restore any file an agent touched that was not
    one of that batch's targets (agents get write access to the whole directory)."""
    bsize = max(1, int(_STATE["batch_size"] or 4))
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
        "pre_claim": paper_dir / ".workflow" / "04_pre_claim",
        "pre_polish": paper_dir / ".workflow" / "05_pre_polish",
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
# Stage 0: Ground-Truth Ledger, Hard Locks & Vetted Exemplar Profile
# ==============================================================================

async def run_inventory(paths: Dict[str, Path]) -> Dict[str, Any]:
    wf = paths["wf"]
    args = _wf_args()
    skipped_sources: List[str] = []
    authority_files = collect_source_files(args.get("authority_paths") or [], skipped_sources)
    source_files = collect_source_files(args.get("source_paths") or [], skipped_sources)
    auth_set = {f.resolve() for f in authority_files}
    other_files = [f for f in source_files if f.resolve() not in auth_set]
    exemplar_paths = [str(Path(x)) for x in (args.get("exemplar_paths") or [])]
    if skipped_sources:
        log(f"Stage 0: skipped {len(skipped_sources)} source paths (missing, or too large to be notes): {skipped_sources[:8]}")

    # One scan agent per authority file; the other sources are packed into ~60 KB chunks. Refuse to fan out
    # beyond max_scan_agents instead of silently launching hundreds of paid agents on a data directory.
    scan_jobs: List[Tuple[List[Path], bool]] = [([f], True) for f in authority_files] + [(c, False) for c in chunk_source_files(other_files)]
    max_jobs = int(_STATE.get("max_scan_agents") or 24)
    if len(scan_jobs) > max_jobs:
        raise RuntimeError(
            f"Stage 0 would launch {len(scan_jobs)} scan agents for {len(authority_files)} authority + {len(other_files)} source files "
            f"(limit max_scan_agents={max_jobs}). Narrow --source-paths to the notes/reports that matter (data directories are not "
            f"needed), or raise --max-scan-agents deliberately."
        )

    phase("Stage 0: Ground-Truth Ledger, Structural Locks & Exemplar Vetting")
    profile_path = wf / "00_style_profile.json"
    if exemplar_paths:
        rc, _, _, _ = run_tool("style_metrics.py", ["profile", "--inputs", *exemplar_paths, "--out", str(profile_path), "--no-lm"], allow_fail=True)
        log(f"style_metrics.py profile exited {rc}")
    ensure_default_profile(paths)

    if exemplar_paths:
        vet_res = await dispatch_agent(
            f"Inspect `{profile_path}` and exemplar files {exemplar_paths}.\n"
            f"Task A — Curate 4 to 6 clean verbatim Few-Shot paragraphs covering roles (`opening_hook`, `setup_definition`, "
            f"`theorem_commentary`, `derivation_mechanism`, `discussion`) inside `categorized_exemplars` & `exemplar_paragraphs`.\n"
            f"Task B — LEXICON SAFETY VETTING (CRITICAL): Review `preferred_lexicon.candidate_connector_swaps` and `evaluative_hype_counts` in `{profile_path}`.\n"
            f"Verify that ZERO words carrying scientific, mathematical, logical, or quantifier meaning (`fixing`, `preserves`, `admits`, `realizes`, `for all`, `exists`, `irreducible`, `multiplicity`, etc.) enter replacements.\n"
            f"Write ONLY vetted pure non-scientific connectors/rhetorical swaps into `preferred_lexicon.vetted_replacements`, optional hype warnings into `vetted_style_hints`, and set `preferred_lexicon.lexicon_vetted = true` inside `{profile_path}`.",
            schema={"type": "object", "properties": {"curated_count": {"type": "integer"}, "approved_swaps": {"type": "array", "items": {"type": "string"}}, "rejected_words": {"type": "array", "items": {"type": "string"}}}, "required": ["curated_count", "approved_swaps"]},
            role="Curate Few-Shot & Vet Lexicon",
            stage_name="inventory",
        )
        log(f"Exemplar vetting completed: {vet_res}")

    scan_schema = {
        "type": "object",
        "properties": {
            "file": {"type": "string"},
            "is_authority": {"type": "boolean"},
            "claims": {"type": "array", "items": {"type": "object"}},
            "redlines_and_decisions": {"type": "array", "items": {"type": "string"}},
            "locked_numbers": {"type": "array", "items": {"type": "object"}},
            "notations": {"type": "array", "items": {"type": "string"}},
            "domain_terms": {"type": "array", "items": {"type": "string"}},
            "cited_works": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["file", "claims", "notations"],
    }
    scan_list_schema = {"type": "object", "properties": {"records": {"type": "array", "items": scan_schema}}, "required": ["records"]}
    job_results = await run_batched(
        scan_jobs,
        lambda job: dispatch_agent(
            f"Read each of these files thoroughly (authority_doc={job[1]}): {[str(p) for p in job[0]]}.\n"
            f"Return one record per file. For each file extract:\n"
            f"1. All theorems/lemmas/propositions with exact hypotheses, quantifiers (`with multiplicity`, translation invariance, finite range, boundary conditions), and line spans.\n"
            f"2. User-decided redlines, prohibited claims, verification status grades, and mandatory inclusions.\n"
            f"3. Exact numerical values paired with the EXACT physical Hamiltonian/observable/residual they belong to (never conflate bounds or models).\n"
            f"4. Notations, domain physics terms, and prior papers cited.",
            schema=scan_list_schema,
            role=f"Scan {job[0][0].name}" + (f" (+{len(job[0]) - 1})" if len(job[0]) > 1 else ""),
            stage_name="inventory",
            tier="heavy",
            read_only=True,
        ),
    ) if scan_jobs else []
    scans: List[Any] = []
    for jr in job_results:
        scans.extend(_require_fields(jr, ["records"], "Stage 0 scan")["records"])
    scans_path = wf / "00_scans.json"
    scans_path.write_text(json.dumps(scans, indent=2, ensure_ascii=False), encoding="utf-8")

    ledger_path = wf / "00_ground_truth_ledger.json"
    locks_path = wf / "00_locks.json"
    conflicts_path = wf / "00_conflicts_report.md"
    synth_res = await dispatch_agent(
        f"Synthesize all scanned records in `{scans_path}` ({len(scans)} records; read the whole file) into THREE canonical files:\n"
        f"1. `{ledger_path}`: complete verified claims ledger (IDs, hypotheses, equations, scripts, provenance).\n"
        f"2. `{locks_path}`: immutable machine-readable lock table with keys:\n"
        f"   - `redlines_and_decisions`: list of mandatory user rules & prohibitions (keep exact wording).\n"
        f"   - `locked_numbers`: list of {{id, value, exact_object, allowed_qualifier, forbidden_qualifier, source}}.\n"
        f"   - `locked_theorems`: list of {{id, statement_hypotheses, mandatory_quantifiers, status_grade}}.\n"
        f"   - `sympy_verify_targets`: list of claims needing SymPy/numerical script checks.\n"
        f"3. `{conflicts_path}`: cross-draft convention conflicts (sign conventions, inner-product normalization, generator symbols).\n"
        f"4. Merge extracted physics `domain_terms` into `locked_terms` of `{profile_path}`.\n"
        f"Write `{ledger_path}` as a JSON object whose `claims` key is the list of claim records.",
        schema={
            "type": "object",
            "properties": {
                "claim_count": {"type": "integer"},
                "lock_count": {"type": "integer"},
                "conflict_count": {"type": "integer"},
                "cited_seed_hints": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["claim_count", "lock_count", "conflict_count", "cited_seed_hints"],
        },
        role="Synthesize Ledger & 00_locks.json",
        stage_name="inventory",
    )
    synth_res = _require_fields(synth_res, ["claim_count", "lock_count", "conflict_count"], "Stage 0 synthesis")
    hints = [str(h) for h in (synth_res.get("cited_seed_hints") or [])]
    # Persist the hints: the recommended interactive flow runs `--mode blueprint` as a separate process.
    (wf / "00_cited_seed_hints.json").write_text(json.dumps(hints, indent=2, ensure_ascii=False), encoding="utf-8")
    for must in (ledger_path, locks_path):
        if not must.exists():
            raise RuntimeError(f"Stage 0 synthesis did not write {must}")
    log(f"Stage 0 complete: {synth_res['claim_count']} claims, {synth_res['lock_count']} locks, {synth_res['conflict_count']} conflicts")
    return {"ledger": str(ledger_path), "locks": str(locks_path), "conflicts": str(conflicts_path), "profile": str(profile_path), "cited_seed_hints": hints, "skipped_sources": skipped_sources}


# ==============================================================================
# Stage 1: Narrative Brief, Macros/Blueprint Generation + Reverse GT Audit
# ==============================================================================

async def run_blueprint(paths: Dict[str, Path], inv_info: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    wf = paths["wf"]
    paper_dir = paths["paper"]
    args = _wf_args()
    genre = args.get("genre", "long")
    phase("Stage 1: Prior Literature Boundary, Blueprint Lock & Reverse GT Audit")

    seed_bib_path = wf / "01_verified_seed.bib"
    existing_bib = args.get("existing_bib")
    if existing_bib and Path(existing_bib).exists():
        copy_to_workflow(Path(existing_bib), seed_bib_path)
    elif not seed_bib_path.exists():
        seed_bib_path.write_text("", encoding="utf-8")

    hints_src = (inv_info or {}).get("cited_seed_hints")
    hints_file = wf / "00_cited_seed_hints.json"
    if hints_src is None and hints_file.exists():  # separate `--mode blueprint` run after `--mode inventory`
        try:
            hints_src = json.loads(read_text_safe(hints_file))
        except json.JSONDecodeError:
            hints_src = []
    hints = [str(h) for h in (hints_src or [])][:12]
    if not hints:
        log("Stage 1: no cited_seed_hints from Stage 0; prior-literature check skipped")
    lit_boundary: List[Any] = []
    if hints:
        vc = f"{resolve_python_exe()} {TOOLS_DIR / 'verify_citations.py'} verify-entry"
        lit_boundary = await run_batched(
            hints,
            lambda h: dispatch_agent(
                f"Verify prior work `{h}`.\n"
                f"1. Run `{vc} --query \"<title words, first author, year>\"`. This only lists candidates (verified=false).\n"
                f"2. Pick the candidate that is really this work, then run `{vc} --doi <doi> --title \"<its exact title>\"` (or `--arxiv <id>`).\n"
                f"3. Report verified=true and its BibTeX ONLY if step 2 printed verified=true; otherwise verified=false and an empty bibtex.\n"
                f"Also give a 1-2 sentence boundary: what the prior work did and what gap remains.",
                schema={"type": "object", "properties": {"query": {"type": "string"}, "verified": {"type": "boolean"}, "bibtex": {"type": "string"}, "what_prior_work_did": {"type": "string"}, "remaining_gap": {"type": "string"}}, "required": ["query", "verified", "bibtex", "what_prior_work_did"]},
                role=f"Verify Prior Ref: {h[:26]}",
                stage_name="narrative",
            ),
        )
        verified_bibs = [str(item["bibtex"]).strip() for item in lit_boundary if isinstance(item, dict) and item.get("verified") and item.get("bibtex")]
        if verified_bibs:
            old = read_text_safe(seed_bib_path).strip()
            seed_bib_path.write_text((old + "\n\n" if old else "") + "\n\n".join(verified_bibs) + "\n", encoding="utf-8")

    brief_path = wf / "01_narrative_brief.md"
    genre_doc = read_text_safe(REFS_DIR / "genre_templates.md")
    ledger_path = wf / "00_ground_truth_ledger.json"
    locks_path = wf / "00_locks.json"
    conflicts_path = wf / "00_conflicts_report.md"

    await dispatch_agent(
        f"Write `{brief_path}` strictly obeying `{locks_path}` and `{ledger_path}`.\n"
        f"Sections: (1) Core Results & Exact Locks Summary, (2) Prior Literature Boundary Matrix, (3) 2-3 Candidate Narrative Arcs + Figure Plans.\n"
        f"NEVER extrapolate fits or omit theorem hypotheses. Target genre: `{genre}`.\nRef:\n{genre_doc[:3500]}\nVerified seed lit:\n{json.dumps(lit_boundary, ensure_ascii=False)[:3500]}",
        schema={"type": "string"},
        role="Draft Narrative Decision Brief",
        stage_name="narrative",
    )

    blueprint_path = Path(args["blueprint_path"]) if args.get("blueprint_path") else (wf / "01_blueprint.json")
    macros_path = paper_dir / "macros.tex"
    await dispatch_agent(
        f"Read `{brief_path}`, `{locks_path}`, `{ledger_path}`, and `{conflicts_path}`.\n"
        f"Write:\n"
        f"1. `{macros_path}`: global LaTeX macros resolving all notation conflicts cleanly. IMPORTANT: any run-in heading macro (`\\letterhead`, etc.) MUST begin with `\\par` so headings never collide with the previous paragraph.\n"
        f"2. `{blueprint_path}`: strict JSON blueprint with title, genre, `abstract_lock_ids`, `sections` list (`id`, `file`, `title`, `role_type`, `target_words`, `assigned_ledger_claims`, `assigned_lock_ids`, `must_cover`, `do_not_repeat`, `incoming_bridge`, `outgoing_bridge`, `exported_labels`). Reference IDs in `{locks_path}` instead of writing sloppy paraphrased summaries of numbers or theorems!",
        schema={"type": "string"},
        role="Chief Editor Blueprint & Macros",
        stage_name="narrative",
    )
    await _ensure_valid_blueprint(blueprint_path, "Chief Editor Blueprint")

    # Stage 1C: Mandatory Reverse Blueprint-vs-GT Audit Gate
    auth_list = [str(p) for p in collect_source_files(args.get("authority_paths") or [])]
    rev_res = await dispatch_agent(
        f"YOU ARE THE INDEPENDENT REVERSE BLUEPRINT AUDITOR.\n"
        f"Do NOT trust `01_narrative_brief.md` summaries. Instead, read `{blueprint_path}` and `{macros_path}` sentence by sentence and compare DIRECTLY against raw authority documents {auth_list} and `{locks_path}`:\n"
        f"1. Check EVERY numerical claim: reject ungrounded extrapolations, swapped residuals/errors, tightened inequality bounds, or misattributed models (e.g. assigning an unperturbed model benchmark to a perturbed model).\n"
        f"2. Check EVERY theorem/abstract contract: reject dropped quantifiers (one-site translation, finite-range, multiplicity clauses, exact divisibility conditions, global/pointwise constraints).\n"
        f"3. Check EVERY user redline/decision (required figure elements, canonical domain terminology, forbidden internal references).\n"
        f"4. Verify `{macros_path}` conventions (`\\par` in heading macros, consistent operator symbols) match `{conflicts_path}` uniformly across main text and Supplemental Material.\n"
        f"Patch ALL detected errors directly in `{blueprint_path}` and `{macros_path}`.",
        schema={"type": "object", "properties": {"violations_found": {"type": "integer"}, "fixed_summary": {"type": "string"}}, "required": ["violations_found", "fixed_summary"]},
        role="Reverse Blueprint-vs-GT Audit",
        stage_name="narrative",
        tier="heavy",
    )
    log(f"Stage 1C Reverse Blueprint Audit: {rev_res}")
    await _ensure_valid_blueprint(blueprint_path, "Reverse Blueprint Audit")  # the auditor patched the file in place
    return {"brief": str(brief_path), "blueprint": str(blueprint_path), "macros": str(macros_path), "reverse_audit": rev_res}


def validate_blueprint(bp_path: Path) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Parse 01_blueprint.json and check the fields the draft stage relies on."""
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


async def _ensure_valid_blueprint(bp_path: Path, after_role: str) -> Dict[str, Any]:
    data, err = validate_blueprint(bp_path)
    if err is None and data is not None:
        return data
    log(f"[blueprint] invalid after {after_role}: {err}; asking for a JSON repair")
    await dispatch_agent(
        f"`{bp_path}` must be strict JSON (no comments, no trailing commas) with a non-empty `sections` list whose items each have a "
        f"`file` string. It currently fails validation: {err}. Fix the file in place without changing its content otherwise.",
        schema={"type": "string"},
        role="Blueprint JSON Repair",
        stage_name="narrative",
        tier="fast",
    )
    data, err = validate_blueprint(bp_path)
    if err is not None or data is None:
        raise RuntimeError(f"01_blueprint.json is still invalid after repair: {err}")
    return data


# ==============================================================================
# Stage 2: Few-Shot Parallel Section Drafting & Continuity Assembly
# ==============================================================================

async def run_draft(paths: Dict[str, Path]) -> Dict[str, Any]:
    wf = paths["wf"]
    paper_dir = paths["paper"]
    args = _wf_args()
    bp_path = Path(args["blueprint_path"]) if args.get("blueprint_path") else (wf / "01_blueprint.json")
    blueprint, bp_err = validate_blueprint(bp_path)
    if bp_err is not None or blueprint is None:
        raise RuntimeError(f"Cannot draft: {bp_path} is not a usable blueprint ({bp_err}). Run `--mode blueprint` first or fix the file.")
    macros_txt = read_text_safe(paper_dir / "macros.tex")
    prof_path = ensure_default_profile(paths)
    style_prof = json.loads(read_text_safe(prof_path))
    locks_txt = read_text_safe(wf / "00_locks.json")
    genre_doc = read_text_safe(REFS_DIR / "genre_templates.md")
    seed_bib = read_text_safe(wf / "01_verified_seed.bib")

    refs_bib = paper_dir / "refs.bib"
    if not refs_bib.exists() and seed_bib:
        refs_bib.write_text(seed_bib, encoding="utf-8")

    fewshot_block = json.dumps(style_prof.get("categorized_exemplars") or style_prof.get("exemplar_paragraphs") or [], indent=2, ensure_ascii=False)[:4500]
    sections: List[Dict[str, Any]] = blueprint.get("sections", [])
    tech_sections = [s for s in sections if s.get("role_type", "body") in ("body", "appendix", "supp")] or sections

    phase("Stage 2: Few-Shot Parallel Section Drafting & Continuity Assembly")
    section_schema = {
        "type": "object",
        "properties": {"file": {"type": "string"}, "word_estimate": {"type": "integer"}, "todo_citations": {"type": "array", "items": {"type": "string"}}, "exported_labels_written": {"type": "array", "items": {"type": "string"}}},
        "required": ["file", "todo_citations"],
    }

    draft_results = await run_batched(
        tech_sections,
        lambda sec: dispatch_agent(
            f"Draft section `{sec['file']}` inside `{paper_dir}` following ALL contracts, locks, and Few-Shot voice examples.\n\n"
            f"Section contract:\n{json.dumps(sec, indent=2, ensure_ascii=False)}\n\n"
            f"Hard Locks (`00_locks.json`) — NEVER relax quantifiers or alter locked numbers:\n{locks_txt[:4500]}\n\n"
            f"Mandatory `macros.tex`:\n```latex\n{macros_txt}\n```\n\n"
            f"FEW-SHOT EXEMPLAR VOICE SAMPLES (imitate the group's natural clause rhythm, transitions & restraint; NEVER chop prose into telegraphic <=8-word bullets):\n{fewshot_block}\n\n"
            f"Rules:\n"
            f"1. 4-Part Golden Structure (Motivation -> Definitions in-situ + intuition -> Zero-skip derivation -> Physical verdict).\n"
            f"2. Define EVERY symbol upon first use in main text. Introduce explicit microscopic model equations in the Letter body.\n"
            f"3. NEVER print internal ledger tags (`Lemma L1/L2`, `Grade N`, `Refl-2`) or uncompiled `\\iffalse` comments.\n"
            f"4. NEVER invent BibTeX keys; use `\\cite{{TODO:short_claim}}` if uncertain and list in `todo_citations`.\n"
            f"5. End every `.tex` segment file with an explicit trailing blank line.\n"
            f"6. Write ONLY `{paper_dir / sec['file']}`; other agents are drafting the other sections in parallel.\n\n"
            f"Genre norms:\n{genre_doc[:2500]}",
            schema=section_schema,
            role=f"Draft {sec.get('id', sec['file'])}",
            stage_name="draft",
        ),
    )

    supp_path = resolve_supplement_path(paper_dir) or (paper_dir / "supplement.tex")
    await dispatch_agent(
        f"You are Lead Continuity Editor for `{paper_dir}`.\n"
        f"1. Read drafted files from `{bp_path}` and inspect `{locks_txt[:3000]}`.\n"
        f"2. Finalize Abstract (with explicit definitions, all hypotheses/quantifiers matching main theorems, `<=600` PRL chars), Intro, Discussion, `Acknowledgments`, and `Data Availability`.\n"
        f"3. Ensure `main.tex` (and `{supp_path}` if Supplemental Material sections exist) inputs `macros.tex`, sections in order, and uses formal `\\cite{{SM}}` or synchronized section headers (`Sec. S...` vs `Appendix S...`).\n"
        f"4. Maintain natural connected prose (zero staccato sentence chopping) and match Few-Shot samples.\n"
        f"Draft outputs:\n{json.dumps(draft_results, indent=2, ensure_ascii=False)}",
        schema={"type": "string"},
        role="Lead Continuity Editor & Assembly",
        stage_name="draft",
    )

    snapshot_tex_files(paper_dir, paths["raw_drafts"])
    run_tool("audit_tex_defs.py", ["--main", str(paper_dir / "main.tex"), "--json-out", str(wf / "02_tex_defs_audit.json")], allow_fail=True)
    log(f"Stage 2 drafted {len(draft_results)} sections; raw snapshot saved.")
    return {"drafted_sections": len(draft_results), "raw_snapshot": str(paths["raw_drafts"])}


# ==============================================================================
# Stage 6 / On-Demand Figures Phase: PRL/Nature Visual Language + PyMuPDF Hard Gate
# ==============================================================================

async def run_figures(paths: Dict[str, Path]) -> Dict[str, Any]:
    paper_dir = paths["paper"]
    fig_dir = paths["figures"]
    fig_scripts = paths["fig_scripts"]
    fig_renders = paths["fig_renders"]
    fig_style = str(_wf_args().get("figure_style") or "prl").lower()
    vis_spec = read_text_safe(REFS_DIR / "figure_visual_language.md")
    theme_src = TOOLS_DIR / "journal_fig_theme.py"

    phase(f"Figures Phase ({fig_style.upper()} House Style + PyMuPDF Hard Gate)")
    fig_scripts.mkdir(parents=True, exist_ok=True)
    if theme_src.exists():
        copy_to_workflow(theme_src, fig_scripts / "journal_fig_theme.py")

    specs: List[Dict[str, Any]] = []
    sp_arg = _wf_args().get("figure_specs_path")
    if sp_arg and Path(str(sp_arg)).exists():
        raw_sp = json.loads(read_text_safe(Path(str(sp_arg))))
        specs = raw_sp if isinstance(raw_sp, list) else raw_sp.get("figures", [])
    elif list(fig_scripts.glob("make_fig*.py")):
        for py in sorted(fig_scripts.glob("make_fig*.py")):
            specs.append({"id": py.stem.replace("make_", ""), "script_path": str(py)})
    else:
        bp, bp_err = validate_blueprint(Path(str(_wf_args().get("blueprint_path") or (paths["wf"] / "01_blueprint.json"))))
        if bp is not None:
            specs = [s for s in (bp.get("figures") or []) if isinstance(s, dict)]
        elif (paths["wf"] / "01_blueprint.json").exists():
            log(f"Figures: blueprint unusable ({bp_err}); no figure specs")

    if specs:
        locks_txt = read_text_safe(paths["wf"] / "00_locks.json")
        await run_batched(
            specs,
            lambda sp: dispatch_agent(
                f"Create or refactor figure builder `{sp.get('script_path') or (fig_scripts / ('make_' + sp.get('id', 'fig') + '.py'))}` targeting `{fig_dir}` under `{fig_style}` house style.\n"
                f"MANDATORY VISUAL RULES:\n"
                f"1. Import `journal_fig_theme` from `{fig_scripts}` (`apply_house_style('{fig_style}')`, `add_panel_label`, `PALETTE`, `DIMENSIONS`). NEVER use DejaVu fonts, Tailwind hex cards, `FancyBboxPatch(round)`, status pills, `ax.set_title()`, `ax.table()`, lollipop stems, or proof prose flowcharts.\n"
                f"2. Match ALL notations & numbers strictly against `{paper_dir / 'macros.tex'}` and locks:\n{locks_txt[:2500]}\n"
                f"3. Move descriptive panel titles, constant run parameters, and error bounds into caption text—keep figure canvas clean with >=7pt PRL / >=5pt Nature labels and zero overlapping strokes.\n"
                f"4. After editing the script, execute `{resolve_python_exe()} <script_path>` so updated PDF is written to `{fig_dir}`.\n\n"
                f"Figure Spec:\n{json.dumps(sp, indent=2, ensure_ascii=False)}\n\nVisual Language Reference:\n{vis_spec[:3500]}",
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
            [
                "--pdf-dir", str(fig_dir),
                "--scripts-dir", str(fig_scripts),
                "--tex-dir", str(paper_dir),
                "--style", fig_style,
                "--renders-out", str(fig_renders),
                "--json-out", str(fig_json),
                "--md-out", str(fig_md),
            ],
            allow_fail=True,
        )
        if rc_fig == 0 or rnd == 2 or not fig_rep:
            log(f"verify_figures.py round {rnd + 1} finished: passed={bool(fig_rep and fig_rep.get('passed'))}")
            break
        failing_figs = [f for f in fig_rep.get("figures", []) if not f.get("passed")]
        ast_fails = fig_rep.get("script_ai_findings", [])
        if not failing_figs and not ast_fails:
            break
        log(f"verify_figures.py round {rnd + 1} caught {len(failing_figs)} failing figures and {len(ast_fails)} AST AI tells; running visual repair")
        await run_batched(
            failing_figs,
            lambda ff: dispatch_agent(
                f"Repair figure PDF `{ff['file']}` (its builder script in `{fig_scripts}` and PNG preview `{ff.get('previews', {}).get('rgb_300dpi')}`) to clear ALL hard-gate failures below.\n"
                f"Also inspect `{paper_dir / 'sections'}` if caption/symbol text needs bidirectional harmonization (NEVER break figure-vs-text agreement!).\n"
                f"Inspect ONLY PNG previews (including any `zoom_*.png` in `{fig_renders}`), NEVER load multi-page PDFs.\n"
                f"Failures:\n{json.dumps(ff, indent=2, ensure_ascii=False)[:3500]}\nScript AST issues:\n{json.dumps(ast_fails[:12], indent=2)}",
                schema={"type": "string"},
                role=f"Fix Fig: {Path(ff['file']).name}",
                stage_name="figures",
            ),
        )

    return {"figure_report": str(fig_md), "figure_json": str(fig_json), "renders_dir": str(fig_renders)}


# ==============================================================================
# Stage 3: 3-Way Cross-Verification (Raw Authority + SymPy + Defs Audit)
# ==============================================================================

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


async def run_verify(paths: Dict[str, Path]) -> Dict[str, Any]:
    paper_dir = paths["paper"]
    wf = paths["wf"]
    audit_dir = paths["audit"]
    ledger_path = wf / "00_ground_truth_ledger.json"
    locks_path = wf / "00_locks.json"
    args = _wf_args()
    auth_files = [str(p) for p in collect_source_files(args.get("authority_paths") or args.get("source_paths") or [])]

    phase("Stage 3: 3-Way Raw-Authority Cross-Verification, SymPy & Defs Audit")
    run_tool("audit_tex_defs.py", ["--main", str(paper_dir / "main.tex"), "--json-out", str(wf / "03_tex_defs_audit.json"), "--md-out", str(audit_dir / "00_tex_defs_audit.md")], allow_fail=True)

    tex_files = list_editable_tex(paper_dir)
    issue_schema = {
        "type": "object",
        "properties": {
            "target": {"type": "string"},
            "auto_fixable": {"type": "array", "items": {"type": "object", "properties": {"file": {"type": "string"}, "location": {"type": "string"}, "description": {"type": "string"}, "exact_fix": {"type": "string"}}, "required": ["file", "description", "exact_fix"]}},
            "fundamental_blockers": {"type": "array", "items": {"type": "object", "properties": {"file": {"type": "string"}, "claim_or_eq": {"type": "string"}, "diagnostic": {"type": "string"}, "counterexample_or_script": {"type": "string"}}, "required": ["file", "claim_or_eq", "diagnostic"]}},
        },
        "required": ["target", "auto_fixable", "fundamental_blockers"],
    }

    rev_3a = [
        create_persistent_handle(
            (
                f"Channel 3A Raw-Authority Reviewer: verify `{f}` DIRECTLY against raw source/authority files {auth_files}, `{locks_path}`, and `{ledger_path}` (NOT against blueprint!). "
                f"Verify theorem proof chains have zero missing intermediate lemmas, hypotheses/quantifiers match exactly, and numeric attributions match exact physical Hamiltonians."
            ),
            role=f"3A Trace: {f.name}",
            stage_name="cross_verify",
            read_only=True,
            tier="heavy",
        )
        for f in tex_files
    ]

    claims = _ledger_claims(ledger_path)[:12]
    if not claims:
        log(f"Stage 3: no claim list found in {ledger_path}; 3B runs one generic identity/counterexample check")
        claims = [{"id": "MAIN-CHECK", "summary": "Verify core algebraic identities & counterexamples"}]
    rev_3b = [
        create_persistent_handle(
            f"Channel 3B Independent Python/SymPy Reviewer (use `{resolve_python_exe()}`): write & run standalone check script in `{paths['verify_scripts']}` verifying claim identities & testing boundary counterexamples cleanly.",
            role=f"3B SymPy: {c.get('id', 'Claim')}",
            stage_name="cross_verify",
            read_only=False,
        )
        for c in claims
    ]

    rev_3c = create_persistent_handle(
        f"Channel 3C Global & Definition Alignment Reviewer for `{paper_dir}`: cross-check Abstract/Body vs Supplemental Material, `{paper_dir / 'macros.tex'}`, and `{wf / '03_tex_defs_audit.json'}` to ensure zero undefined symbols, zero leaked internal tags (`Lemma L*`, `Grade N`), and clean proof dependencies.",
        role="3C Global Alignment",
        stage_name="cross_verify",
        read_only=True,
        tier="heavy",
    )

    rounds: List[Tuple[Dict[str, List[Dict[str, Any]]], List[Dict[str, Any]]]] = []
    fixed_after_last_review = False
    for rnd in range(2):
        res_3a = await run_batched(list(zip(rev_3a, tex_files)), lambda pair: call_persistent_handle(pair[0], f"Round {rnd + 1}: Audit `{pair[1]}` directly against raw authority sources & `{locks_path}`.", issue_schema))
        res_3b = await run_batched(list(zip(rev_3b, claims)), lambda pair: call_persistent_handle(pair[0], f"Round {rnd + 1}: Independently check claim {json.dumps(pair[1], ensure_ascii=False)} against `{paper_dir}`.", issue_schema))
        res_3c = await call_persistent_handle(rev_3c, f"Round {rnd + 1}: Global consistency & defs audit across `{paper_dir}`.", issue_schema)

        by_file, blockers = collect_review_round(paper_dir, list(res_3a) + list(res_3b) + [res_3c])
        rounds.append((by_file, blockers))
        if not by_file:
            break
        # one Fix agent per file, keyed by a normalised paper-relative path (reviewers mix absolute/relative paths)
        await run_guarded_edit_batches(
            paper_dir,
            snapshot_project(paper_dir, paths["snapshots"] / f"verify_round{rnd + 1}"),
            sorted(by_file),
            lambda rel: dispatch_agent(
                f"Apply ONLY verified `auto_fixable` corrections to `{paper_dir / rel}`.\nFixes:\n{json.dumps(by_file[rel], indent=2, ensure_ascii=False)}",
                schema={"type": "string"},
                role=f"Fix 3-Way: {Path(rel).name}",
                stage_name="cross_verify",
            ),
        )
        fixed_after_last_review = rnd == 1

    for slot in rev_3a + rev_3b + [rev_3c]:
        slot.pop("handle", None)

    # The last review round supersedes earlier ones: its reviewers re-read the fixed files.
    final_blockers = rounds[-1][1] if rounds else []
    final_keys = {_blocker_key(b) for b in final_blockers}
    resolved = [b for r in rounds[:-1] for b in r[1] if _blocker_key(b) not in final_keys]
    cv_md = ["# Stage 3: 3-Way Cross-Verification Report (v2)", "", f"- **Review rounds**: {len(rounds)}",
             f"- **Fundamental Mathematical Blockers (last round)**: {len(final_blockers)}",
             f"- **Blockers reported earlier and resolved by the fixes**: {len(resolved)}", ""]
    if fixed_after_last_review:
        cv_md += ["> Fixes were applied after the last review round and have not been re-reviewed; run `--mode verify` again to confirm them.", ""]
    if final_blockers:
        cv_md += ["## IMPORTANT: Fundamental Blockers Requiring Author Review", ""]
        for b in final_blockers:
            cv_md.append(f"- **File**: `{b.get('file')}` | **Claim**: `{b.get('claim_or_eq')}`\n  - **Diagnostic**: {b.get('diagnostic')}")
            rel = _paper_rel(paper_dir, b["file"]) if b.get("file") else None  # never write outside the paper
            cand = paper_dir / rel if rel else None
            if cand is not None and cand.is_file() and cand.suffix.lower() == ".tex" and cand.name != "macros.tex":
                marker = f"% [VERIFY-BLOCKER] {str(b.get('claim_or_eq') or 'BLOCKER').strip()}: {str(b.get('diagnostic') or '').replace(chr(10), ' ').strip()}"
                ex = read_text_safe(cand)
                if marker not in ex:
                    cand.write_text(marker + "\n" + ex, encoding="utf-8")
    else:
        cv_md.append("The last review round reported zero fundamental blockers.")
    if resolved:
        cv_md += ["", "## Resolved During This Run", ""] + [f"- `{b.get('file')}`: {b.get('claim_or_eq')}" for b in resolved]
    (audit_dir / "01_cross_verification.md").write_text("\n".join(cv_md) + "\n", encoding="utf-8")
    return {"blockers": len(final_blockers), "resolved": len(resolved), "rounds": len(rounds), "report": str(audit_dir / "01_cross_verification.md")}


def _blocker_key(b: Dict[str, Any]) -> Tuple[str, str]:
    return (str(b.get("file") or ""), re.sub(r"\s+", " ", str(b.get("claim_or_eq") or "")).strip().lower())


def collect_review_round(paper_dir: Path, reports: List[Any]) -> Tuple[Dict[str, List[Dict[str, Any]]], List[Dict[str, Any]]]:
    """Group one round's auto-fixes by paper-relative file and collect its blockers (paths normalised)."""
    by_file: Dict[str, List[Dict[str, Any]]] = {}
    blockers: List[Dict[str, Any]] = []
    for rep in reports:
        if not isinstance(rep, dict):
            raise RuntimeError(f"Stage 3 reviewer returned a non-object result: {str(rep)[:300]}")
        for b in rep.get("fundamental_blockers", []) or []:
            if isinstance(b, dict):
                rel = _paper_rel(paper_dir, b["file"]) if b.get("file") else None
                blockers.append({**b, "file": rel if rel is not None else b.get("file")})
        for fx in rep.get("auto_fixable", []) or []:
            if not isinstance(fx, dict) or not fx.get("file"):
                continue
            rel = _paper_rel(paper_dir, fx["file"])
            if rel is None:
                log(f"[verify] ignored an auto-fix for a file outside the paper directory: {fx.get('file')}")
                continue
            by_file.setdefault(rel, []).append({**fx, "file": rel})
    seen = set()
    unique = []
    for b in blockers:
        k = _blocker_key(b)
        if k not in seen:
            seen.add(k)
            unique.append(b)
    return by_file, unique


# ==============================================================================
# Stage 4: Dual DOI+eprint+.bbl Citations & Lock-Retried Claim Fidelity
# ==============================================================================

async def run_citations(paths: Dict[str, Path]) -> Dict[str, Any]:
    paper_dir = paths["paper"]
    wf = paths["wf"]
    audit_dir = paths["audit"]
    refs_bib = paper_dir / "refs.bib"
    sync_bib = _wf_args().get("sync_bib")
    if sync_bib and Path(str(sync_bib)).exists():
        write_user_file(refs_bib, Path(str(sync_bib)).read_bytes())
    elif not refs_bib.exists():
        refs_bib.write_text("", encoding="utf-8")

    phase("Stage 4: Dual DOI+arXiv+.bbl Citation Audit & Claim Fidelity")
    bib_cache = wf / "04_api_bib_cache.json"
    cite_json = wf / "04_citation_audit.json"
    cite_md = audit_dir / "02_citation_audit.md"

    _, _, _, audit_p1 = run_tool(
        "verify_citations.py",
        ["audit", "--tex-dir", str(paper_dir), "--bib", str(refs_bib), "--cache", str(bib_cache), "--fix-braces", "--upgrade-preprints", "--json-out", str(cite_json), "--md-out", str(cite_md)],
        allow_fail=True,
    )

    todo_list = (audit_p1 or {}).get("todo_placeholders", [])
    newly_added_refs: List[Dict[str, Any]] = []
    if todo_list:
        vc = f"{resolve_python_exe()} {TOOLS_DIR / 'verify_citations.py'} verify-entry"
        resolved = await run_batched(
            todo_list,
            lambda td: dispatch_agent(
                f"Resolve the placeholder citation `{td['key']}` at {td['locations']} inside `{paper_dir}`.\n"
                f"1. `{vc} --query \"...\"` lists candidates only (never verified).\n"
                f"2. Confirm the right one with `{vc} --doi <doi> --title \"<exact title>\"` (or `--arxiv <id>`).\n"
                f"3. ONLY if step 2 printed verified=true: add its BibTeX to `{refs_bib}` (keep the file's other entries, @string and comments untouched) "
                f"and replace the placeholder key in the .tex. Otherwise leave the placeholder and report resolved=false.",
                schema={"type": "object", "properties": {"placeholder": {"type": "string"}, "resolved": {"type": "boolean"}, "new_key": {"type": "string"}, "title_and_id": {"type": "string"}}, "required": ["placeholder", "resolved"]},
                role=f"Resolve {td['key'][:26]}",
                stage_name="citation_check",
            ),
        )
        newly_added_refs = [r for r in resolved if isinstance(r, dict) and r.get("resolved")]

    if audit_p1 is None:
        raise RuntimeError("verify_citations.py audit produced no JSON report (see its stderr); citations stage stopped")
    if audit_p1.get("network_unverified_keys"):
        log(f"Stage 4: {len(audit_p1['network_unverified_keys'])} cited entries could not be checked (network); the citation gate stays FAILED until a run can reach the APIs")
    bad_bib = [e for e in audit_p1.get("entries", []) if e.get("is_cited") and e.get("status") in ("EPRINT_DOI_MISMATCH", "EPRINT_NOT_FOUND", "AUTHOR_ORDER_MISMATCH", "IDENTIFIER_NOT_FOUND", "METADATA_MISMATCH", "UNVERIFIED_NO_MATCH")]
    bbl_iss = audit_p1.get("bbl_issues", []) + audit_p1.get("bib_hygiene_issues", []) + [{"bib_parse_error": pe} for pe in audit_p1.get("bib_parse_errors", [])]
    if bad_bib or bbl_iss:
        await dispatch_agent(
            f"Repair ALL BibTeX metadata, eprint/DOI mismatches, duplicate `arXiv:` notes, and math brace issues inside `{refs_bib}` (verify fixes via `verify_citations.py verify-entry`).\n"
            f"Bad entries:\n{json.dumps(bad_bib, indent=2, ensure_ascii=False)[:3500]}\nHygiene & .bbl issues:\n{json.dumps(bbl_iss, indent=2, ensure_ascii=False)[:2500]}",
            schema={"type": "string"},
            role="Repair refs.bib Metadata",
            stage_name="citation_check",
        )

    snapshot_project(paper_dir, paths["pre_claim"])
    rel_files = [str(f.relative_to(paper_dir)).replace("\\", "/") for f in list_editable_tex(paper_dir)]
    await run_guarded_edit_batches(
        paper_dir,
        paths["pre_claim"],
        rel_files,
        lambda rel: _edit_with_math_lock_retry(
            rel,
            paper_dir,
            paths["pre_claim"],
            lambda attempt, fb: (
                f"Audit citation claim fidelity in `{paper_dir / rel}` against `{bib_cache}` and `{refs_bib}` (Attempt {attempt}).\n"
                f"Only edit if a claim misattributes or overclaims a reference (e.g. conflating author contributions).\n"
                f"GUARDRAILS: You may adjust `\\cite{{...}}` keys, NEVER alter `$...$`, `\\label`, `\\ref`, `\\eqref`, `\\extref`, `\\status`, or logical quantifiers in abstract/theorems.\n"
                + (f"\nPREVIOUS LOCK VIOLATION TO AVOID:\n{fb}" if fb else "")
            ),
            role=f"Claim Fidelity: {Path(rel).name}",
            stage_name="citation_check",
            extra_check_args=["--allow-cite-changes", "--check-quantifiers"],
        ),
    )

    run_tool(
        "verify_citations.py",
        ["audit", "--tex-dir", str(paper_dir), "--bib", str(refs_bib), "--cache", str(bib_cache), "--json-out", str(cite_json), "--md-out", str(cite_md)],
        allow_fail=True,
    )
    if newly_added_refs:
        old_md = read_text_safe(cite_md)
        prep = "\n".join(f"- `{nr.get('new_key')}`: {nr.get('title_and_id', '')}" for nr in newly_added_refs)
        cite_md.write_text(f"## Newly Verified Citations\n\n{prep}\n\n{old_md}", encoding="utf-8")
    return {"citation_md": str(cite_md), "citation_json": str(cite_json)}


# ==============================================================================
# Stage 5: Deterministic Hygiene, Vetted Connectors & Precision Copy-Edit
# ==============================================================================

async def run_hygiene(paths: Dict[str, Path]) -> Dict[str, Any]:
    paper_dir = paths["paper"]
    wf = paths["wf"]
    audit_dir = paths["audit"]
    profile_path = ensure_default_profile(paths)
    rules_txt = read_text_safe(REFS_DIR / "humanizer_physics_rules.md")

    phase("Stage 5: Positive-ROI Watermark Hygiene, Copy-Edit & Anti-Staccato Audit (v2)")
    # Deterministic fixes first, then the snapshot: if a copy-edit agent breaks the math lock, restoring
    # the snapshot must not also undo the watermark/typography clean-up.
    run_tool("clean_watermarks.py", ["--paths", str(paper_dir), "--profile", str(profile_path), "--fix", "--json-out", str(wf / "05_clean_wm.json")], allow_fail=True)
    snapshot_project(paper_dir, paths["pre_polish"])

    stylo_json = wf / "05_stylometrics.json"
    stylo_md = audit_dir / "03_stylometrics_report.md"
    files_now = [str(f) for f in list_editable_tex(paper_dir)]
    _, _, _, sty_res = run_tool(
        "style_metrics.py",
        ["verify", "--inputs", *files_now, "--profile", str(profile_path), "--json-out", str(stylo_json), "--md-out", str(stylo_md), "--no-lm"],
        allow_fail=True,
    )

    _, _, _, wm_scan = run_tool("clean_watermarks.py", ["--paths", str(paper_dir), "--profile", str(profile_path), "--json-out", str(wf / "05_clean_wm.json")], allow_fail=True)
    wm_by_file = {item["file"]: item for item in (wm_scan or {}).get("files", [])}
    sty_by_file = {item["file"]: item for item in (sty_res or {}).get("files", [])}

    flagged_rels: List[str] = []
    for f in list_editable_tex(paper_dir):
        key_p = str(f).replace("\\", "/")
        wi = wm_by_file.get(key_p, {})
        si = sty_by_file.get(key_p, {})
        if wi.get("ai_slop_hits") or wi.get("copyedit_issues") or wi.get("rhetorical_hints") or si.get("staccato_warning"):
            flagged_rels.append(str(f.relative_to(paper_dir)).replace("\\", "/"))

    if flagged_rels:
        log(f"Targeted Copy-Edit triggered ONLY on flagged files: {flagged_rels}")
        await run_guarded_edit_batches(
            paper_dir,
            paths["pre_polish"],
            flagged_rels,
            lambda rel: _edit_with_math_lock_retry(
                rel,
                paper_dir,
                paths["pre_polish"],
                lambda attempt, fb: (
                    f"Precision Copy-Edit `{paper_dir / rel}` (Attempt {attempt}).\n"
                    f"ONLY fix the flagged AI clichés, copy-edit bugs (`rank rank`, `at most <=`, `Sec.` at sentence start, repeated acronym expansions), vetted non-scientific evaluative hype, or choppy `<=8`-word staccato clusters.\n"
                    f"HARD GUARDRAIL: NEVER touch clean paragraphs, math `$...$`, labels/cites, OR any logical quantifiers/hypotheses in abstract/theorems!\n\n"
                    f"Findings:\n{json.dumps(wm_by_file.get(str(paper_dir / rel).replace(chr(92), '/'), {}), indent=2, ensure_ascii=False)[:3000]}\n"
                    f"Staccato warning: {sty_by_file.get(str(paper_dir / rel).replace(chr(92), '/'), {}).get('staccato_warning')}\n"
                    f"Rules:\n{rules_txt[:2000]}"
                    + (f"\nPREVIOUS MATH/QUANTIFIER LOCK VIOLATION:\n{fb}" if fb else "")
                ),
                role=f"Copy-Edit: {Path(rel).name}",
                stage_name="hygiene",
                tier="fast",
                extra_check_args=["--check-quantifiers"],
            ),
        )
        run_tool("style_metrics.py", ["verify", "--inputs", *[str(f) for f in list_editable_tex(paper_dir)], "--profile", str(profile_path), "--json-out", str(stylo_json), "--md-out", str(stylo_md), "--no-lm"], allow_fail=True)
    else:
        log("All files already clean on first hygiene pass; 0 LLM prose rewrites needed.")

    run_tool("audit_tex_defs.py", ["--main", str(paper_dir / "main.tex"), "--json-out", str(wf / "05_tex_defs_audit.json")], allow_fail=True)
    return {"stylometrics_report": str(stylo_md), "clean_wm_json": str(wf / "05_clean_wm.json")}


# ==============================================================================
# Build & Sanitized arXiv Package Stage
# ==============================================================================

async def run_build(paths: Dict[str, Path]) -> Dict[str, Any]:
    paper_dir = paths["paper"]
    wf = paths["wf"]
    main_tex = paper_dir / "main.tex"
    supp_tex = resolve_supplement_path(paper_dir)
    flat_main = paths["submission"] / "main_arxiv.tex"
    flat_supp = paths["submission"] / "supplement_arxiv.tex"
    build_json = wf / "05_build_report.json"

    phase("TeX Dual Build, Layout Hygiene & Sanitized arXiv Flattening")
    for attempt in range(3):
        cli = ["--main", str(main_tex), "--compile", "--flatten-out", str(flat_main), "--inline-bbl", "--json-out", str(build_json)]
        if supp_tex and supp_tex.exists():
            cli.extend(["--supplement", str(supp_tex), "--flatten-supplement-out", str(flat_supp)])
        rc, _, _, bld = run_tool("build_and_flatten_tex.py", cli, allow_fail=True)
        cr = (bld or {}).get("compile_result", {})
        scr = (bld or {}).get("supplement_compile_result", {})
        lc = (bld or {}).get("layout_check", {})
        leaks = (bld or {}).get("submission_leaks", []) + (bld or {}).get("supplement_submission_leaks", [])
        clean = (
            rc == 0
            and cr.get("clean_pass")
            and (not scr or scr.get("clean_pass"))
            and not lc.get("layout_issues")
            and not leaks
            and not cr.get("overfull_gt10pt")
            and not (scr and scr.get("overfull_gt10pt"))
        )
        if clean or attempt == 2:
            log(f"Build finished (attempt {attempt + 1}): clean={clean}, leaks={len(leaks)}, layout_issues={len(lc.get('layout_issues', []))}")
            break
        await dispatch_agent(
            f"Fix TeX compilation, layout (e.g. `\\letterhead` missing `\\par`, orphan page, >10pt overfull, hyperref `\\texorpdfstring`), or arXiv submission leaks (e.g. placeholder authors, missing Acknowledgments/Data Availability) in `{paper_dir}` without altering equations.\n"
            f"Diagnostics:\n{json.dumps(bld, indent=2, ensure_ascii=False)[:4000]}",
            schema={"type": "string"},
            role=f"Build/Layout Fix #{attempt + 1}",
            stage_name="compile_fix",
            tier="fast",
        )
    return {"build_json": str(build_json), "main_arxiv": str(flat_main)}


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
    for name in ("05_tex_defs_audit.json", "04_citation_audit.json", "06_figure_verify.json", "05_build_report.json"):
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
            ("Referee B (Broad Reader)", f"Act as a broad condensed-matter / theoretical-physics Referee reading `{sb / 'main.tex'}`, its `sections/*.tex`, and PNG previews in `{sb / 'renders'}` (do NOT lean on Supplemental Material to rescue missing main-text definitions!). Flag every undefined symbol, missing microscopic formula, math jargon without intuition, staccato prose cluster, and visual clutter.\n{isol_rule}"),
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
    return {"blind_referee_report": str(out_md)}


# ==============================================================================
# On-Demand Repair Phase (Fallback & Modular Patching)
# ==============================================================================

REPAIR_INLINE_CHARS = 12000


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


async def run_repair(paths: Dict[str, Path]) -> Dict[str, Any]:
    paper_dir = paths["paper"]
    wf = paths["wf"]
    audit_dir = paths["audit"]
    args = _wf_args()
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    snap_dir = snapshot_project(paper_dir, paths["snapshots"] / f"{ts}_pre_repair")

    phase(f"Repair Fallback Phase (Checkpoint: {snap_dir.name})")
    if args.get("sync_bib") and Path(str(args["sync_bib"])).exists():
        write_user_file(paper_dir / "refs.bib", Path(str(args["sync_bib"])).read_bytes())

    # The original instructions always live in a file that every repair agent can read verbatim:
    # the planner only routes work, it must never be the only carrier of text to be inserted.
    raw_inst_full = str(args.get("repair_instructions") or "")
    raw_inst = raw_inst_full.strip()
    if raw_inst and _is_existing_file(raw_inst):
        inst_path = Path(raw_inst).resolve()
    elif raw_inst:
        inst_path = wf / f"repair_instructions_{ts}.md"
        with open(inst_path, "w", encoding="utf-8", newline="") as fh:  # byte-for-byte what the user gave
            fh.write(raw_inst_full)
    elif (audit_dir / "05_blind_referee_report.md").exists():
        inst_path = audit_dir / "05_blind_referee_report.md"
    elif (audit_dir / "01_cross_verification.md").exists():
        inst_path = audit_dir / "01_cross_verification.md"
    else:
        raise RuntimeError("repair: no repair_instructions given and no referee / cross-verification report to repair from")
    inst_text = read_text_safe(inst_path)
    if not inst_text.strip():
        raise RuntimeError(f"repair: instructions file {inst_path} is empty")
    truncated = len(inst_text) > REPAIR_INLINE_CHARS
    if truncated:
        log(f"[repair] WARNING instructions are {len(inst_text)} chars; prompts inline only the first {REPAIR_INLINE_CHARS} "
            f"and every agent is told to read the full file {inst_path}")
    inst_excerpt = inst_text[:REPAIR_INLINE_CHARS] + (
        f"\n\n[... TRUNCATED here: {len(inst_text) - REPAIR_INLINE_CHARS} more characters. Read the full file `{inst_path}`.]" if truncated else ""
    )

    locks_txt = read_text_safe(wf / "00_locks.json")
    explicit_targets = [_paper_rel(paper_dir, x) for x in (args.get("repair_files") or [])]
    if any(t is None for t in explicit_targets):
        raise RuntimeError(f"repair_files must point inside {paper_dir}: {args.get('repair_files')}")
    explicit_targets_s: List[str] = [t for t in explicit_targets if t]
    skip_plan = bool(args.get("repair_skip_plan")) or len(explicit_targets_s) == 1
    if args.get("repair_skip_plan") and not explicit_targets_s:
        raise RuntimeError("repair_skip_plan=true needs repair_files (the files to hand the full instructions to)")

    by_file: Dict[str, List[Dict[str, str]]] = {}
    if skip_plan:
        log(f"[repair] planner skipped; each of {explicit_targets_s} receives the full instructions")
        for rel in explicit_targets_s:
            by_file[rel] = [{"issues_to_fix": "Apply every instruction in the instructions file that concerns this file.", "instruction_refs": "(whole file)"}]
    else:
        plan_res = await dispatch_agent(
            f"Plan a surgical repair inside `{paper_dir}`. The complete repair instructions are in `{inst_path}` "
            f"({len(inst_text)} characters{'; the excerpt below is TRUNCATED, read the whole file' if truncated else ''}).\n"
            f"1. Inspect files in `{paper_dir}` (and `macros.tex` / `refs.bib` / `sections/`).\n"
            f"2. Break repairs into per-file work items: `target_file` (relative to `{paper_dir}`), `issues_to_fix` (what to change), "
            f"and `instruction_refs` (where in the instructions file each fix is specified: heading, quoted first words, or line numbers).\n"
            f"3. NEVER paraphrase text that must be inserted or replaced verbatim (acknowledgments, abstract wording, quoted sentences, "
            f"equations, author lists): point to it in `instruction_refs`; the worker copies it from the instructions file.\n"
            f"Explicit target filter (if non-empty, plan only these files): {explicit_targets_s}\n\n"
            f"Current Locks:\n{locks_txt[:2500]}\n\nRepair Instructions (excerpt):\n{inst_excerpt}",
            schema={
                "type": "object",
                "properties": {
                    "work_items": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {"target_file": {"type": "string"}, "issues_to_fix": {"type": "string"}, "instruction_refs": {"type": "string"}},
                            "required": ["target_file", "issues_to_fix"],
                        },
                    },
                },
                "required": ["work_items"],
            },
            role="Plan Surgical Repairs",
            stage_name="repair",
            tier="heavy",
        )
        if not isinstance(plan_res, dict):
            raise RuntimeError(f"Repair planner returned a non-object result: {str(plan_res)[:300]}")
        for it in plan_res.get("work_items", []):
            if not isinstance(it, dict) or not it.get("target_file"):
                continue
            rel = _paper_rel(paper_dir, str(it["target_file"]))
            if rel is None:
                log(f"[repair] skipped work item outside the paper directory: {it.get('target_file')}")
                continue
            if explicit_targets_s and rel not in explicit_targets_s:
                log(f"[repair] skipped work item for {rel}: not in repair_files")
                continue
            by_file.setdefault(rel, []).append({"issues_to_fix": str(it.get("issues_to_fix", "")), "instruction_refs": str(it.get("instruction_refs", ""))})

    def _repair_prompt(rel: str) -> str:
        summary = "\n".join(f"- {w['issues_to_fix']}" + (f"  [see instructions: {w['instruction_refs']}]" if w.get("instruction_refs") else "") for w in by_file[rel])
        return (
            f"Repair `{paper_dir / rel}`.\n"
            f"The complete original repair instructions are in `{inst_path}` ({len(inst_text)} characters). Read the parts that concern "
            f"this file THERE. Any text that must be inserted or replaced (acknowledgments, abstract wording, quoted sentences, equations) "
            f"must be copied exactly from that file; never reconstruct it from the summary below, and do not search other folders for it.\n\n"
            f"Planner summary for this file:\n{summary}\n\n"
            f"Preserve mathematical rigor and the macros in `{paper_dir / 'macros.tex'}`. Snapshot before repair: `{snap_dir}`. "
            f"Edit ONLY `{paper_dir / rel}`."
        )

    rels = sorted(by_file)
    await run_guarded_edit_batches(
        paper_dir,
        snap_dir,
        rels,
        lambda rel: dispatch_agent(_repair_prompt(rel), schema={"type": "string"}, role=f"Repair: {Path(rel).name}", stage_name="repair"),
    )
    items = [{"target_file": r} for r in rels]

    run_tool("clean_watermarks.py", ["--paths", str(paper_dir), "--profile", str(ensure_default_profile(paths)), "--fix"], allow_fail=True)
    run_tool("audit_tex_defs.py", ["--main", str(paper_dir / "main.tex"), "--json-out", str(wf / "repair_tex_defs.json"), "--md-out", str(audit_dir / "00_tex_defs_audit.md")], allow_fail=True)
    if (paper_dir / "refs.bib").exists():
        run_tool("verify_citations.py", ["audit", "--tex-dir", str(paper_dir), "--bib", str(paper_dir / "refs.bib"), "--cache", str(wf / "04_api_bib_cache.json"), "--json-out", str(wf / "04_citation_audit.json"), "--md-out", str(audit_dir / "02_citation_audit.md")], allow_fail=True)
    bld_res = await run_build(paths)

    closeout = audit_dir / "06_repair_closeout.md"
    closeout.write_text(
        f"# Post-Repair Verification Closeout ({ts})\n\n"
        f"- **Checkpoint Snapshot**: `{snap_dir}`\n"
        f"- **Instructions**: `{inst_path}` ({len(inst_text)} chars{', planner skipped' if skip_plan else ''})\n"
        f"- **Files Repaired**: {[it['target_file'] for it in items]}\n"
        f"- **Updated Audits**: `00_tex_defs_audit.md`, `02_citation_audit.md`, `{bld_res['build_json']}`\n",
        encoding="utf-8",
    )
    return {"checkpoint": str(snap_dir), "instructions": str(inst_path), "planner_skipped": skip_plan, "repaired_count": len(items), "closeout": str(closeout), **bld_res}


# ==============================================================================
# Smoke Test & Entry Router
# ==============================================================================

async def run_smoke_test(paths: Dict[str, Path]) -> Dict[str, Any]:
    phase("Smoke Test v2: Python Env Resolution, 7 CLI Tools & Agent Pings")
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


async def execute_mode() -> Dict[str, Any]:
    args = _wf_args()
    mode = str(args.get("mode") or "smoke_test")
    paper_dir = Path(str(args["paper_dir"]))
    paths = ensure_dirs(paper_dir)
    preflight_permissions(paper_dir)

    if mode == "smoke_test":
        return await run_smoke_test(paths)
    if mode == "inventory":
        return await run_inventory(paths)
    if mode == "blueprint":
        return await run_blueprint(paths)
    if mode == "draft":
        return await run_draft(paths)
    if mode == "figures":
        return await run_figures(paths)
    if mode == "verify":
        return await run_verify(paths)
    if mode == "citations":
        return await run_citations(paths)
    if mode == "hygiene":
        return await run_hygiene(paths)
    if mode == "build":
        return await run_build(paths)
    if mode == "blind_referee":
        return await run_blind_referee(paths)
    if mode == "repair":
        return await run_repair(paths)
    if mode == "audit_polish":
        return {
            "verify": await run_verify(paths),
            "citations": await run_citations(paths),
            "hygiene": await run_hygiene(paths),
            "build": await run_build(paths),
        }
    if mode == "draft_and_audit":
        return {
            "draft": await run_draft(paths),
            "verify": await run_verify(paths),
            "citations": await run_citations(paths),
            "hygiene": await run_hygiene(paths),
            "build": await run_build(paths),
        }
    if mode == "full_auto":
        inv = await run_inventory(paths)
        bp = await run_blueprint(paths, inv)
        dr = await run_draft(paths)
        bp_data, _ = validate_blueprint(Path(str(args.get("blueprint_path") or (paths["wf"] / "01_blueprint.json"))))
        wants_figures = bool(args.get("figure_specs_path") or list(paths["fig_scripts"].glob("make_fig*.py")) or (bp_data and bp_data.get("figures")))
        fg = await run_figures(paths) if wants_figures else {"skipped": True, "reason": "no figure specs, scripts, or blueprint figures"}
        vf = await run_verify(paths)
        ct = await run_citations(paths)
        hy = await run_hygiene(paths)
        bl = await run_build(paths)
        br = await run_blind_referee(paths)
        return {"inventory": inv, "blueprint": bp, "draft": dr, "figures": fg, "verify": vf, "citations": ct, "hygiene": hy, "build": bl, "blind_referee": br}
    raise ValueError(f"Unsupported mode: {mode}")


async def run_from_antigravity(host_globals: Dict[str, Any]) -> Dict[str, Any]:
    """Entry point invoked inside Antigravity `run_workflow` sandbox."""
    wf_args = dict(host_globals.get("args") or {})
    workspaces = list(host_globals.get("WORKSPACES") or ())
    if not wf_args.get("paper_dir") and workspaces:
        wf_args["paper_dir"] = workspaces[0]

    _STATE.update({
        "runtime": "antigravity",
        "backend": "antigravity",
        "wf_args": wf_args,
        "models_cfg": dict(wf_args.get("models") or {}),
        "batch_size": max(1, min(8, int(wf_args.get("max_parallel") or 4))),
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
) -> Dict[str, Any]:
    """Entry point invoked by `run_paper.py` under Claude Code, Codex, agy CLI, or terminal."""
    _STATE.update({
        "runtime": "cli",
        "backend": backend,
        "referee_backend": referee_backend,
        "wf_args": dict(args_dict),
        "models_cfg": dict(args_dict.get("models") or {}),
        "batch_size": max(1, min(8, int(args_dict.get("max_parallel") or 4))),
        "inherit_deadlock": False,
        "skip_agent_pings": skip_agent_pings,
        "agent_timeout": int(args_dict.get("agent_timeout_s") or 1800),
        "agent_retries": int(args_dict["agent_retries"]) if args_dict.get("agent_retries") is not None else 2,
        "max_scan_agents": int(args_dict.get("max_scan_agents") or 24),
        "routing_policy": str(args_dict.get("routing_policy") or "auto_tiered"),
        "in_plan_mode": None,
    })
    return await execute_mode()
