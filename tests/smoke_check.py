#!/usr/bin/env python3
"""Comprehensive repository hygiene, CLI portability, and tool regression suite."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
SKILL_SCRIPTS = REPO_ROOT / "skills" / "write-paper" / "scripts"
TOOLS_DIR = SKILL_SCRIPTS / "tools"
PY = sys.executable

# Generic patterns only. Personal or project-specific patterns (names, folder names, unpublished
# terminology) belong in tests/leak_patterns.local.txt (git-ignored, one regex per line) or in the file
# named by $WRITE_PAPER_LEAK_PATTERNS_FILE, so that the leak list itself never leaks.
FORBIDDEN_PATTERNS = [
    r"C:\\\\Users",
    r"C:/Users",
    r"/Users/[A-Za-z]",
    r"/home/[a-z]",
]
LOCAL_PATTERN_FILES = [REPO_ROOT / "tests" / "leak_patterns.local.txt"]
if os.environ.get("WRITE_PAPER_LEAK_PATTERNS_FILE"):
    LOCAL_PATTERN_FILES.append(Path(os.environ["WRITE_PAPER_LEAK_PATTERNS_FILE"]))


def load_forbidden_patterns() -> list:
    pats = list(FORBIDDEN_PATTERNS)
    for f in LOCAL_PATTERN_FILES:
        if f.is_file():
            pats += [ln.strip() for ln in f.read_text(encoding="utf-8").splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    return pats


def run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")


def check_zero_leaks() -> None:
    hits = []
    skip_dirs = {".git", "__pycache__", ".venv", "dist", "build", ".wp_backups"}
    patterns = load_forbidden_patterns()
    local_files = {f.resolve() for f in LOCAL_PATTERN_FILES if f.exists()}
    for p in sorted(REPO_ROOT.rglob("*")):
        if not p.is_file() or any(part in skip_dirs for part in p.parts):
            continue
        if p.resolve() == Path(__file__).resolve() or p.resolve() in local_files:
            continue
        if p.suffix.lower() in {".png", ".jpg", ".pdf", ".pyc"}:
            continue
        txt = p.read_text(encoding="utf-8", errors="replace")
        for pat in patterns:
            for m in re.finditer(pat, txt):
                line = txt.count("\n", 0, m.start()) + 1
                hits.append(f"{p.relative_to(REPO_ROOT)}:{line}: matched /{pat}/ -> {m.group(0)}")
    assert not hits, "Found personal or single-project leaks:\n" + "\n".join(hits)
    print(f"[PASS] zero personal/project leak scan ({len(patterns)} patterns, {len(patterns) - len(FORBIDDEN_PATTERNS)} from local files)")


def check_cli_helps() -> None:
    targets = [
        [PY, "-W", "error", str(REPO_ROOT / "install.py"), "--list"],
        [PY, "-W", "error", str(SKILL_SCRIPTS / "run_paper.py"), "--help"],
    ]
    for tool_name in (
        "check_math_integrity.py",
        "clean_watermarks.py",
        "style_metrics.py",
        "verify_citations.py",
        "build_and_flatten_tex.py",
        "verify_figures.py",
        "audit_tex_defs.py",
    ):
        targets.append([PY, "-W", "error", str(TOOLS_DIR / tool_name), "--help"])

    for cmd in targets:
        r = run(cmd)
        assert r.returncode == 0, f"Command failed: {cmd}\nstderr={r.stderr}"
    print("[PASS] all CLI --help & -W error checks (9/9)")


def check_offline_smoke_and_audit_defs() -> None:
    with tempfile.TemporaryDirectory(prefix="wp_smoke_") as td:
        tmp = Path(td)
        # 1. Offline run_paper.py smoke_test
        proj = tmp / "proj"
        r = run([PY, str(SKILL_SCRIPTS / "run_paper.py"), "--mode", "smoke_test", "--paper-dir", str(proj), "--skip-agent-pings"])
        assert r.returncode == 0, f"run_paper.py smoke_test failed: {r.stderr}\n{r.stdout}"
        idx = r.stdout.rfind("\n{")
        assert idx != -1, f"Missing JSON block in stdout:\n{r.stdout}"
        out_json = json.loads(r.stdout[idx + 1 :])
        assert all(out_json["tools_ok"].values()), f"Unexpected tools_ok={out_json['tools_ok']}"

        # 2. Generalized audit_tex_defs.py negative & positive fixtures
        bad_dir = tmp / "bad_tex"
        bad_dir.mkdir()
        (bad_dir / "main.tex").write_text(
            r"""
\documentclass[aps,prl,twocolumn]{revtex4-2}
\begin{document}
\begin{abstract}We study operators $A(\alpha_j)$ along a chain.\end{abstract}
\maketitle
The witness state has generator $\chi_3$, factor $c_{\mathrm{bad}}$, and obeys Smith Condition 2.
\end{document}
""",
            encoding="utf-8",
        )
        (bad_dir / "supplement.tex").write_text(
            r"""
\documentclass[aps,prl]{revtex4-2}
\begin{document}
\section{Details}
Here $\chi_3 = \prod_k X_k$.
\end{document}
""",
            encoding="utf-8",
        )
        rb = run([PY, "-W", "error", str(TOOLS_DIR / "audit_tex_defs.py"), "--main", str(bad_dir / "main.tex")])
        assert rb.returncode == 1, f"Expected audit_tex_defs failure, got {rb.returncode}"
        rb_j = json.loads(rb.stdout)
        undef_syms = {x["symbol"] for x in rb_j["undefined_main_symbols"]}
        assert any("A(\\alpha_j)" in s for s in undef_syms), f"Missed abstract undefined op: {undef_syms}"
        assert r"\chi_3" in undef_syms, f"Missed SM-only symbol: {undef_syms}"
        assert r"c_{\mathrm{bad}}" in undef_syms, f"Missed bespoke subscript: {undef_syms}"
        assert any(x["kind"] == "internal_condition_jargon" for x in rb_j["internal_tag_leaks"]), "Missed uncited Author Condition leak"

        clean_dir = tmp / "clean_tex"
        clean_dir.mkdir()
        (clean_dir / "main.tex").write_text(
            r"""
\documentclass[aps,prl,twocolumn]{revtex4-2}
\begin{document}
\begin{abstract}We study a finite-range Hamiltonian $H = -\sum_j Z_j Z_{j+1}$.\end{abstract}
\maketitle
Let $\chi_3 = \prod_k X_k$ define the parity generator (see Ref.~\cite{SM}).
\end{document}
""",
            encoding="utf-8",
        )
        rc = run([PY, "-W", "error", str(TOOLS_DIR / "audit_tex_defs.py"), "--main", str(clean_dir / "main.tex")])
        assert rc.returncode == 0, f"Clean fixture failed audit_tex_defs: {rc.stdout}\n{rc.stderr}"

    print("[PASS] offline run_paper.py smoke_test & generalized audit_tex_defs fixtures")


if __name__ == "__main__":
    check_zero_leaks()
    check_cli_helps()
    check_offline_smoke_and_audit_defs()
    print("ALL_CHECKS_PASSED")
