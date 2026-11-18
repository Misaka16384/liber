#!/usr/bin/env python3
"""Liber: zero-dependency cross-platform installer for the /write-paper skill.

Installs `skills/write-paper` cleanly into global or project-scoped directories for:
  - Claude Code      (~/.claude/skills/write-paper)
  - OpenAI Codex CLI (~/.codex/skills/write-paper, ~/.agents/skills/write-paper)
  - Antigravity IDE  (~/.gemini/config/skills/write-paper)
  - OpenCode         (~/.config/opencode/skills/write-paper + commands/write-paper.md)
  - Qwen Code        (~/.agents/skills/write-paper)

Pure stdlib Python 3.8+. Supports `--mode copy|link`, `--hosts auto|all|...`,
`--project <dir>`, `--check-deps`, `--list`, and `--uninstall`.
"""

from __future__ import annotations

import argparse
import os
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple


REPO_ROOT = Path(__file__).resolve().parent
SKILL_SRC = REPO_ROOT / "skills" / "write-paper"
SKILL_NAME = "write-paper"


def _config_home() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME", "").strip()
    return Path(xdg) if xdg else (Path.home() / ".config")


def _host_table(project_root: Optional[Path] = None) -> Dict[str, Dict[str, object]]:
    home = Path.home()
    cfg = _config_home()
    if project_root is not None:
        pr = project_root.resolve()
        return {
            "claude": {"label": "Claude Code (project)", "marker": home / ".claude", "dirs": [pr / ".claude" / "skills" / SKILL_NAME]},
            "codex": {"label": "Codex CLI (project)", "marker": home / ".codex", "dirs": [pr / ".agents" / "skills" / SKILL_NAME]},
            "antigravity": {"label": "Antigravity (project)", "marker": home / ".gemini" / "config", "dirs": [pr / ".agents" / "skills" / SKILL_NAME]},
            "opencode": {"label": "OpenCode (project)", "marker": cfg / "opencode", "dirs": [pr / ".opencode" / "skills" / SKILL_NAME], "command": pr / ".opencode" / "commands" / f"{SKILL_NAME}.md"},
            "qwen": {"label": "Qwen Code (project)", "marker": home / ".qwen", "dirs": [pr / ".agents" / "skills" / SKILL_NAME]},
        }
    return {
        "claude": {"label": "Claude Code", "marker": home / ".claude", "dirs": [home / ".claude" / "skills" / SKILL_NAME]},
        "codex": {"label": "Codex CLI", "marker": home / ".codex", "dirs": [home / ".codex" / "skills" / SKILL_NAME, home / ".agents" / "skills" / SKILL_NAME]},
        "antigravity": {"label": "Antigravity (agy)", "marker": home / ".gemini" / "config", "dirs": [home / ".gemini" / "config" / "skills" / SKILL_NAME]},
        "opencode": {"label": "OpenCode", "marker": cfg / "opencode", "dirs": [cfg / "opencode" / "skills" / SKILL_NAME], "command": cfg / "opencode" / "commands" / f"{SKILL_NAME}.md"},
        "qwen": {"label": "Qwen Code", "marker": home / ".qwen", "dirs": [home / ".agents" / "skills" / SKILL_NAME]},
    }


def _is_reparse_or_symlink(path: Path) -> bool:
    if path.is_symlink():
        return True
    try:
        st = os.lstat(path)
        attrs = getattr(st, "st_file_attributes", 0)
        return bool(attrs & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    except OSError:
        return False


def _rmtree_win_safe(dest: Path) -> None:
    def _on_err(func, path, _exc_info):
        try:
            os.chmod(path, stat.S_IWRITE)
        except OSError:
            pass
        func(path)

    for attempt in range(5):
        try:
            shutil.rmtree(dest, onerror=_on_err)
            return
        except PermissionError:
            if attempt == 4:
                raise
            time.sleep(0.15)


def _remove_target(dest: Path) -> None:
    if not dest.exists() and not _is_reparse_or_symlink(dest):
        return
    if dest.is_symlink():
        dest.unlink()
    elif _is_reparse_or_symlink(dest):
        # Safely detach Windows junction without deleting target files
        os.rmdir(dest)
    elif dest.is_dir():
        _rmtree_win_safe(dest)
    else:
        dest.unlink()


def _tree_digest(root: Path) -> str:
    import hashlib

    h = hashlib.sha256()
    for p in sorted(root.rglob("*")):
        if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc":
            h.update(str(p.relative_to(root)).replace("\\", "/").encode())
            h.update(p.read_bytes())
    return h.hexdigest()


def _backup_existing(dest: Path, src: Path) -> Optional[Path]:
    """Keep a copy of an existing *copied* install that differs from the repository (local edits)."""
    if not dest.is_dir() or _is_reparse_or_symlink(dest):
        return None
    if _tree_digest(dest) == _tree_digest(src):
        return None
    # Outside the skills/ folder, so the backup is never loaded as a second skill.
    bdir = dest.parent.parent / ".write-paper-backups"
    bdir.mkdir(parents=True, exist_ok=True)
    target = bdir / f"{dest.name}-{time.strftime('%Y%m%d-%H%M%S')}"
    shutil.copytree(dest, target, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    return target


def _install_dir(src: Path, dest: Path, mode: str) -> str:
    if src.resolve() == dest.resolve():
        return "skipped (destination equals repository source)"
    dest.parent.mkdir(parents=True, exist_ok=True)
    backup = _backup_existing(dest, src)
    if backup is not None:
        print(f"[backup] existing {dest} differed from the repository; saved to {backup}")
    _remove_target(dest)

    if mode == "link":
        try:
            os.symlink(src, dest, target_is_directory=True)
            return "symlinked"
        except OSError:
            if sys.platform.startswith("win"):
                r = subprocess.run(["cmd", "/c", "mklink", "/J", str(dest), str(src)], capture_output=True, text=True)
                if r.returncode == 0:
                    return "junctioned"
            # Fallback to copy when symlinks are disallowed on host
            shutil.copytree(src, dest, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store"))
            return "copied (symlink fallback)"

    shutil.copytree(src, dest, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store"))
    return "copied"


def _write_opencode_command(cmd_path: Path, skill_dir: Path) -> None:
    cmd_path.parent.mkdir(parents=True, exist_ok=True)
    content = (
        f"---\n"
        f"description: Modular theoretical physics LaTeX paper workflow, the author decides the story, AI writes and checks (/write-paper v4)\n"
        f"---\n\n"
        f"Load and follow `{skill_dir / 'SKILL.md'}` with `<SKILL_DIR> = {skill_dir.as_posix()}`.\n"
        f"Run `python {(skill_dir / 'scripts' / 'run_paper.py').as_posix()} --help` or call `scripts/tools/*.py`.\n\n"
        f"$ARGUMENTS\n"
    )
    cmd_path.write_text(content, encoding="utf-8")


def check_dependencies() -> Tuple[bool, Dict[str, object]]:
    py_mods = {}
    for mod in ("pymupdf", "matplotlib", "sympy", "numpy", "PIL"):
        try:
            __import__(mod)
            py_mods[mod] = True
        except ImportError:
            py_mods[mod] = False

    tex_bins = {b: bool(shutil.which(b)) for b in ("pdflatex", "bibtex")}
    cli_bins: Dict[str, bool] = {}
    for b in ("claude", "agy", "codex"):
        found = bool(shutil.which(b))
        if not found and b == "claude":
            found = (Path.home() / ".local" / "bin" / ("claude.exe" if sys.platform.startswith("win") else "claude")).exists()
        cli_bins[b] = found

    all_ok = all(py_mods.values()) and tex_bins["pdflatex"]
    return all_ok, {"python_exe": sys.executable, "python_packages": py_mods, "latex": tex_bins, "agent_clis": cli_bins}


def resolve_hosts(hosts_arg: str, table: Dict[str, Dict[str, object]]) -> List[str]:
    req = hosts_arg.strip().lower()
    if req == "all":
        return list(table.keys())
    if req == "auto":
        detected = [k for k, v in table.items() if Path(v["marker"]).exists() or shutil.which(k)]  # type: ignore[arg-type]
        return detected or ["claude", "codex", "antigravity"]
    out: List[str] = []
    for tok in req.split(","):
        k = tok.strip().lower()
        if not k:
            continue
        if k not in table:
            raise ValueError(f"Unknown host '{k}'. Valid hosts: {', '.join(table.keys())}, auto, all")
        if k not in out:
            out.append(k)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Liber installer for the /write-paper multi-agent skill.")
    ap.add_argument("--hosts", default="auto", help="Comma-separated hosts (claude,codex,antigravity,opencode,qwen), 'auto', or 'all'.")
    ap.add_argument("--project", type=Path, help="Install into project-scoped directories instead of user home (~/.claude, ~/.gemini, etc.).")
    ap.add_argument("--mode", choices=["copy", "link"], default="copy", help="Install via clean copy (default) or symlink/Windows junction.")
    ap.add_argument("--list", action="store_true", help="List detected hosts and target paths without writing files.")
    ap.add_argument("--check-deps", action="store_true", help="Check Python packages (pymupdf, matplotlib, sympy), TeX binaries, and CLIs.")
    ap.add_argument("--uninstall", action="store_true", help="Remove installed write-paper skill directories for selected hosts.")
    args = ap.parse_args()

    if not SKILL_SRC.exists():
        print(f"Error: skill source directory missing at {SKILL_SRC}", file=sys.stderr)
        return 2

    table = _host_table(args.project)
    selected = resolve_hosts(args.hosts, table)

    if args.list:
        print("Detected / selected target installations:")
        for hk in selected:
            info = table[hk]
            marker = Path(info["marker"])  # type: ignore[arg-type]
            dirs = [str(d) for d in info["dirs"]]  # type: ignore[attr-defined]
            print(f"  - {hk:12s} ({info['label']}) | installed={marker.exists()} | paths={dirs}")
        return 0

    installed_paths: Dict[str, str] = {}
    for hk in selected:
        info = table[hk]
        dirs: List[Path] = info["dirs"]  # type: ignore[assignment]
        for target_dir in dirs:
            if args.uninstall:
                backup = _backup_existing(target_dir, SKILL_SRC)
                if backup is not None:
                    print(f"[backup] {target_dir} had local changes; saved to {backup}")
                _remove_target(target_dir)
                print(f"[uninstalled] {hk}: {target_dir}")
                continue
            status = _install_dir(SKILL_SRC, target_dir, mode=args.mode)
            installed_paths[str(target_dir)] = status
            print(f"[ok] {hk:12s} -> {target_dir} ({status})")
        cmd_path: Optional[Path] = info.get("command")  # type: ignore[assignment]
        if cmd_path is not None:
            if args.uninstall and cmd_path.exists():
                cmd_path.unlink()
            elif not args.uninstall:
                _write_opencode_command(cmd_path, dirs[0])
                print(f"[ok] opencode cmd -> {cmd_path}")

    if args.check_deps:
        ok, deps = check_dependencies()
        print("\nEnvironment dependency check:")
        print(f"  Python    : {deps['python_exe']}")
        print(f"  Packages  : {deps['python_packages']}")
        print(f"  LaTeX     : {deps['latex']}")
        print(f"  Agent CLIs: {deps['agent_clis']}")
        if not ok:
            print("  Tip: run `pip install -r requirements.txt` and ensure TeX Live / MiKTeX `pdflatex` is installed.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
