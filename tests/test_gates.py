#!/usr/bin/env python3
"""Offline regression suite for the deterministic gates and the orchestrator's safety logic.

No network and no LLM calls. Run with `python tests/test_gates.py` (or pytest). Tests that need
pdflatex or matplotlib are skipped when those are missing.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "skills" / "write-paper" / "scripts"
TOOLS = SCRIPTS / "tools"
PY = sys.executable
OFFLINE_ENV = {"HTTPS_PROXY": "http://127.0.0.1:9", "HTTP_PROXY": "http://127.0.0.1:9", "NO_PROXY": ""}


class Skip(Exception):
    pass


def tool(name, *args, env_extra=None, timeout=600):
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", **(env_extra or {})}
    r = subprocess.run([PY, str(TOOLS / name), *map(str, args)], capture_output=True, text=True, encoding="utf-8", errors="replace", env=env, timeout=timeout)
    try:
        js = json.loads(r.stdout)
    except Exception:
        js = None
    return r.returncode, js, r.stderr


def tmpdir(name: str) -> Path:
    d = Path(tempfile.mkdtemp(prefix=f"wp_{name}_"))
    return d


PRE = "\\documentclass[aps,prl,reprint]{revtex4-2}\n\\usepackage{url}\n\\begin{document}\n"
END = "\n\\begin{acknowledgments}Thanks.\\end{acknowledgments}\n\\section*{Data Availability}None.\n\\end{document}\n"


# ------------------------------------------------------------------------------------- build tool

def test_flatten_preserves_typesetting():
    if not shutil.which("pdflatex"):
        raise Skip("pdflatex not on PATH")
    d = tmpdir("flat")
    (d / "main.tex").write_text(
        PRE + "\\newcommand{\\foo}{%\n  bar%\n}\nX[A\\foo B]X glue%\nd.\n\nSee \\url{http://example.org/a%20b}.\n\n"
        "\\begin{tabular}{cc} a & b \\\\% TODO internal: ask referee 2\n\\end{tabular}\n\n\\iffalse SECRET-IFFALSE \\fi\n" + END,
        encoding="utf-8")
    rc, js, err = tool("build_and_flatten_tex.py", "--main", d / "main.tex", "--compile", "--flatten-out", d / "flat.tex")
    flat = (d / "flat.tex").read_text(encoding="utf-8")
    assert "\\newcommand{\\foo}{%\n  bar%\n}" in flat, "end-of-line % must survive as a bare %"
    assert "\\url{http://example.org/a%20b}" in flat, "% inside \\url must be kept"
    assert "ask referee 2" not in flat and "SECRET-IFFALSE" not in flat, "comment after \\\\ and \\iffalse block must be removed"
    fd = d / "compile_flat"
    fd.mkdir()
    shutil.copy2(d / "flat.tex", fd / "main.tex")
    r = subprocess.run(["pdflatex", "-interaction=nonstopmode", "-halt-on-error", "main.tex"], cwd=fd, capture_output=True, timeout=180)
    assert r.returncode == 0, "flattened file must compile"
    assert rc == 0 and js["passed"], f"clean document should pass (rc={rc})"


def test_build_detects_errors_and_wrapped_undefined_refs():
    if not shutil.which("pdflatex"):
        raise Skip("pdflatex not on PATH")
    d = tmpdir("err")
    (d / "main.tex").write_text(PRE + "Broken $x^2}$ here.\n" + END, encoding="utf-8")
    rc, js, _ = tool("build_and_flatten_tex.py", "--main", d / "main.tex", "--compile")
    cr = js["compile_result"]
    assert rc == 1 and not cr["clean_pass"] and cr["latex_errors"], "a LaTeX error must be reported and exit 1"
    d2 = tmpdir("ref")
    lab = "sec:" + "a_very_long_label_name_that_wraps" * 3
    (d2 / "main.tex").write_text(PRE + f"See Sec.~\\ref{{{lab}}}.\n" + END, encoding="utf-8")
    rc, js, _ = tool("build_and_flatten_tex.py", "--main", d2 / "main.tex", "--compile")
    assert rc == 1 and js["compile_result"]["undefined_references"], "an undefined \\ref wrapped across log lines must be caught"


# ------------------------------------------------------------------------------------- citations

def _cite_project() -> Path:
    d = tmpdir("cite")
    (d / "main.tex").write_text("\\documentclass{article}\n\\begin{document}\nAnyons~\\cite{kitaev2003,\n  other}.\n"
                                "\\newcommand{\\mycite}[1]{\\cite{#1}}\n\\bibliography{refs}\n\\end{document}\n", encoding="utf-8")
    (d / "refs.bib").write_text(
        '@string{prl = "Phys. Rev. Lett."}\n@comment{keep me}\n% a comment line\n'
        "@article{kitaev2003,\n  author = {Kitaev, A. Yu.},\n  title = {Majorana chains and Kitaev models},\n"
        "  journal = prl # { (x)},\n  month = jan,\n  year = {2003},\n  doi = {10.1016/S0003-4916(02)00018-0}\n}\n", encoding="utf-8")
    return d


def test_citations_fail_closed_offline():
    d = _cite_project()
    rc, js, _ = tool("verify_citations.py", "audit", "--tex-dir", d, "--bib", d / "refs.bib", env_extra=OFFLINE_ENV)
    assert rc == 1 and not js["passed"] and "kitaev2003" in js["network_unverified_keys"], "offline must not pass"
    assert any(u["key"] == "other" for u in js["undefined_keys"]), "key on the continuation line of a multi-line \\cite must be seen"
    assert not any(u["key"] == "#1" for u in js["undefined_keys"]), "\\cite{#1} in a macro body is not a citation"
    rc, js, _ = tool("verify_citations.py", "audit", "--tex-dir", tmpdir("empty"), "--bib", d / "refs.bib", "--allow-offline", env_extra=OFFLINE_ENV)
    assert rc == 1 and not js["passed"] and js["errors"], "an empty --tex-dir must fail"


def test_bib_fix_is_in_place_with_backup():
    d = _cite_project()
    tool("verify_citations.py", "audit", "--tex-dir", d, "--bib", d / "refs.bib", "--fix-braces", "--allow-offline", env_extra=OFFLINE_ENV)
    after = (d / "refs.bib").read_text(encoding="utf-8")
    for keep in ('@string{prl = "Phys. Rev. Lett."}', "@comment{keep me}", "% a comment line", "prl # { (x)}", "month = jan"):
        assert keep in after, f"{keep!r} must survive --fix-braces"
    assert "{{Majorana} chains and {Kitaev} models}" in after, "title must be brace-protected in place"
    assert list((d / ".wp_backups").glob("refs.bib.*.bak")), "a backup of refs.bib must be written"


def test_bib_stray_brace_is_reported_not_swallowed():
    d = _cite_project()
    (d / "broken.bib").write_text("@article{a,\n  title = {Unclosed {brace},\n  year = {2001}\n}\n\n@article{b,\n  title = {B},\n  year = {2002}\n}\n", encoding="utf-8")
    rc, js, _ = tool("verify_citations.py", "audit", "--tex-dir", d, "--bib", d / "broken.bib", "--fix-braces", "--allow-offline", env_extra=OFFLINE_ENV)
    assert js["bib_parse_errors"] and [e["key"] for e in js["entries"]] == ["a", "b"], "entry b must survive a stray brace in entry a"
    assert "@article{b," in (d / "broken.bib").read_text(encoding="utf-8")


# ------------------------------------------------------------------------------------- math lock

LOCK_BASE = r"""\documentclass{revtex4-2}
\newtheorem{conj}{Conjecture}
\newcommand{\Zt}{\mathbb{Z}_2}
\begin{document}
\begin{abstract}
For every $L$ the code is not fault-tolerant, and the threshold is at least 4 percent.
\end{abstract}
The distance grows exponentially in $L$ with 162 samples over 10 minutes. See Eqs.~\crefrange{a}{b}.
This condition is necessary and sufficient. The symmetry is $\Zt$ and the inline
math $x \text{for each
set}$ is wrapped.
\begin{theorem*} For all $n$ there exists a unique ground state. \end{theorem*}
\begin{conj} Every gapped phase is trivial. \end{conj}
\end{document}
"""

LOCK_CASES = {
    "identical": (lambda s: s, True),
    "rewrap inline math": (lambda s: s.replace("$x \\text{for each\nset}$", "$x \\text{for each set}$"), True),
    "10 -> ten": (lambda s: s.replace("10 minutes", "ten minutes"), True),
    "drop not": (lambda s: s.replace("is not fault", "is fault"), False),
    "at least 4 -> 5": (lambda s: s.replace("at least 4", "at least 5"), False),
    "162 -> 1620": (lambda s: s.replace("162 samples", "1620 samples"), False),
    "nec. and suff. -> suff.": (lambda s: s.replace("necessary and sufficient", "sufficient"), False),
    "exponentially -> polynomially": (lambda s: s.replace("exponentially", "polynomially"), False),
    "macro body": (lambda s: s.replace("\\mathbb{Z}_2}", "\\mathbb{R}_2}"), False),
    "theorem* quantifier": (lambda s: s.replace("a unique ground", "a ground"), False),
    "newtheorem env quantifier": (lambda s: s.replace("Every gapped", "Some gapped"), False),
    "crefrange target": (lambda s: s.replace("\\crefrange{a}{b}", "\\crefrange{a}{c}"), False),
}


def test_math_lock_cases():
    d = tmpdir("lock")
    (d / "before.tex").write_text(LOCK_BASE, encoding="utf-8")
    for name, (fn, should_pass) in LOCK_CASES.items():
        (d / "after.tex").write_text(fn(LOCK_BASE), encoding="utf-8")
        rc, js, err = tool("check_math_integrity.py", "--before", d / "before.tex", "--after", d / "after.tex")
        assert js is not None, f"{name}: no JSON ({err[-200:]})"
        assert js["passed"] == should_pass and rc == (0 if should_pass else 1), f"{name}: expected passed={should_pass}, got {js['passed']} rc={rc}"
    rc, _, _ = tool("check_math_integrity.py", "--before-dir", d / "missing", "--after-dir", d)
    assert rc == 2, "a mistyped directory is an error, not a pass"


# ------------------------------------------------------------------------------------- definitions audit

def test_audit_defs():
    a = tmpdir("defs")
    (a / "sections").mkdir()
    (a / "main.tex").write_text(
        "\\documentclass[aps,prl]{revtex4-2}\n\\newcommand{\\lemA}{Lemma~L2}\n\\begin{document}\n\\begin{abstract}We study a chain.\\end{abstract}\n"
        "Intro \\input{sections/sec.1} and \\input{sections/sec2} end.\n\\input{sections/missing}\n"
        "As in Sec.~II of Ref.~\\cite{x}, and Theorem~A of Ref.~\\cite{y}, the class $H_2(M;Z_2)$ is fine.\n"
        "Status \\status{draft}{line one\nline two Grade 3} done. Ref \\cref{sec:nowhere}.\n\\end{document}\n", encoding="utf-8")
    (a / "sections" / "sec.1.tex").write_text("The parameter $\\kappa_{\\mathrm{mix}}$ controls the crossover, see \\lemA.\n", encoding="utf-8")
    (a / "sections" / "sec2.tex").write_text("Here $E_{\\mathrm{gap}} = 2J$ is the gap. Also R44 is noted.\n", encoding="utf-8")
    (a / ".wp_internal_tags.txt").write_text("\\bR\\d{2}\\b\n", encoding="utf-8")
    rc, js, err = tool("audit_tex_defs.py", "--main", a / "main.tex")
    assert js is not None, err
    tags = {t["match"] for t in js["internal_tag_leaks"]}
    assert {"R44", "Lemma~L2"} <= tags and not any("Grade" in t for t in tags), tags
    kinds = {(u["kind"], u["target"].split("\\")[-1] if u["kind"] == "missing_input_file" else u["target"]) for u in js["unresolved_refs"]}
    assert ("unresolved_cref", "sec:nowhere") in kinds and any(k == "missing_input_file" for k, _ in kinds), kinds
    assert not js["section_mismatches"], "a section of a cited paper is not a ghost section"
    undefined = {u["symbol"] for u in js["undefined_main_symbols"]}
    assert "\\kappa_{\\mathrm{mix}}" in undefined and not any("E_{" in s or "H_2" in s for s in undefined), undefined
    assert rc == 1


# ------------------------------------------------------------------------------------- hygiene / style

def test_clean_watermarks_is_safe():
    d = tmpdir("wm")
    src = ("中文破折号——这里，省略号……结束。\u3000缩进“中文引号”。\nEnglish “quoted”—dash and an\u00a0NBSP.\n"
           "\\begin{verbatim}\nx -- y  “quoted”\n\\end{verbatim}\nSee \\url{http://x.org/a%20b} and \\verb|“v”|.\nThus we proceed. However, it fails.\n")
    (d / "a.tex").write_bytes(src.encode("utf-8"))
    prof = d / "prof.json"
    prof.write_text(json.dumps({"preferred_lexicon": {"lexicon_vetted": False, "vetted_replacements": {"thus": "hence"}}}), encoding="utf-8")
    rc, _, _ = tool("clean_watermarks.py", "--paths", d / "a.tex", "--profile", prof, "--fix")
    out = (d / "a.tex").read_bytes()
    text = out.decode("utf-8")
    assert rc == 0 and b"\r\n" not in out, "LF files stay LF"
    assert "——" in text and "……" in text and "\u3000" in text and "“中文引号”" in text, "CJK punctuation must be untouched"
    assert "``quoted''---dash" in text and "an~NBSP" in text, "English typography still normalised; NBSP becomes a tie"
    assert "x -- y  “quoted”" in text and "a%20b" in text and "\\verb|“v”|" in text, "verbatim/url/verb untouched"
    assert "Thus we proceed" in text, "an un-vetted lexicon is never applied"
    assert list((d / ".wp_backups").glob("a.tex.*.bak")), "backup written"
    (d / "b.tex").write_text("The values of $J$ and $h$ and the gap fix the rank rank of the map.\n", encoding="utf-8")
    rc, js, _ = tool("clean_watermarks.py", "--paths", d / "b.tex")
    doubled = [i["match"] for f in js["files"] for i in f["copyedit_issues"] if i["kind"] == "doubled_word"]
    assert doubled == ["rank rank"], f"words separated by math are not doubled: {doubled}"
    rc, _, _ = tool("clean_watermarks.py", "--paths", d / "missing.tex")
    assert rc == 2


def test_style_metrics_equations_are_not_sentence_breaks():
    d = tmpdir("sty")
    para = ("We consider the Hamiltonian\n\\begin{equation}\nH = -J\\sum_j Z_j Z_{j+1} - h\\sum_j X_j,\n\\end{equation}\n"
            "where $J$ is the coupling and $h$ is the transverse field acting on every site of the chain.\n") * 6
    (d / "p.tex").write_text(para, encoding="utf-8")
    (d / "notes.md").write_text("Short. Very. Short. Bits.", encoding="utf-8")
    rc, js, _ = tool("style_metrics.py", "profile", "--inputs", d / "p.tex", "--out", d / "prof.json")
    assert rc == 0 and js["baseline_metrics"]["lm_backend"] == "disabled", "LM scoring is opt-in"
    rc, js, _ = tool("style_metrics.py", "verify", "--inputs", d, "--profile", d / "prof.json")
    assert js["checks"]["anti_staccato_ok"] and [Path(f["file"]).name for f in js["files"]] == ["p.tex"]


# ------------------------------------------------------------------------------------- figures

def test_verify_figures_scripts_widths_and_corrupt_pdf():
    try:
        import matplotlib  # noqa: F401
        import pymupdf  # noqa: F401
    except ImportError:
        raise Skip("matplotlib/pymupdf missing")
    d = tmpdir("fig")
    figdir, scripts = d / "figures", d / "scripts"
    figdir.mkdir()
    scripts.mkdir()
    shutil.copy2(TOOLS / "journal_fig_theme.py", scripts / "journal_fig_theme.py")
    (scripts / "make_fig1.py").write_text(
        "import sys; sys.path.insert(0, r'%s')\nimport numpy as np, matplotlib; matplotlib.use('Agg'); import matplotlib.pyplot as plt\n"
        "from journal_fig_theme import apply_house_style, get_figsize, add_panel_label\napply_house_style('prl')\n"
        "fig, ax = plt.subplots(figsize=get_figsize('prl', 'single', 2.4)); x = np.logspace(-3, 0, 30)\n"
        "ax.loglog(x, x**2, label=r'$J_{xy}$'); ax.set_xlabel(r'$h/J$'); ax.set_ylabel(r'$\\Delta$'); ax.legend(); add_panel_label(ax, 'a')\n"
        "fig.tight_layout(); fig.savefig(r'%s')\n"
        "fig2, ax2 = plt.subplots(figsize=get_figsize('prl', 'one_half', 2.4)); ax2.plot([0, 1], [0, 1]); ax2.set_xlabel('t'); ax2.set_title('(b)', loc='left')\n"
        "fig2.tight_layout(); fig2.savefig(r'%s')\n" % (scripts, figdir / "fig1.pdf", figdir / "fig2.pdf"), encoding="utf-8")
    (scripts / "plot_phase.py").write_text("import matplotlib.pyplot as plt\nfig, ax = plt.subplots()\nax.axvspan(0, 1)\nax.set_title('Phase diagram of the model')\n", encoding="utf-8")
    r = subprocess.run([PY, str(scripts / "make_fig1.py")], capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stderr[-400:]
    (figdir / "fig9_corrupt.pdf").write_bytes(b"%PDF-1.4 garbage")
    rc, js, err = tool("verify_figures.py", "--pdf-dir", figdir, "--scripts-dir", scripts, "--style", "prl")
    by = {Path(f["file"]).name: f for f in js["figures"]}
    assert by["fig1.pdf"]["passed"] and by["fig2.pdf"]["passed"], {k: v.get("checks") for k, v in by.items()}
    assert not by["fig9_corrupt.pdf"]["passed"] and by["fig9_corrupt.pdf"].get("error"), "a corrupt PDF fails on its own"
    assert "plot_phase.py" in js["scripts_scanned"] and js["script_ai_findings_count"] == 1 and js["script_warnings_count"] == 1
    rc, _, _ = tool("verify_figures.py", "--pdfs", figdir / "missing.pdf")
    assert rc == 2


# ------------------------------------------------------------------------------------- orchestrator

def _engine(paper: Path):
    sys.path.insert(0, str(SCRIPTS))
    import workflow_engine as we  # noqa: E402
    we._STATE.update({"runtime": "cli", "backend": "claude", "wf_args": {"paper_dir": str(paper)}, "batch_size": 3, "agent_retries": 0, "max_scan_agents": 24})
    return we


def test_engine_blueprint_ledger_and_scan_cap():
    p = tmpdir("eng")
    we = _engine(p)
    paths = we.ensure_dirs(p)
    bp = paths["wf"] / "01_blueprint.json"
    bp.write_text('{"sections": [{"file": "a.tex"},]}', encoding="utf-8")
    assert we.validate_blueprint(bp)[1] and "JSON" in we.validate_blueprint(bp)[1]
    bp.write_text('{"sections": [{"id": "x"}]}', encoding="utf-8")
    assert "file" in we.validate_blueprint(bp)[1]
    lg = paths["wf"] / "00_ground_truth_ledger.json"
    lg.write_text('[{"id": "X"}, "plain"]', encoding="utf-8")
    assert [c["id"] for c in we._ledger_claims(lg)] == ["X", "C2"]
    src = p / "src"
    src.mkdir()
    for i in range(30):
        (src / f"n{i}.md").write_text("x" * 5000, encoding="utf-8")
    we._STATE["wf_args"] = {"paper_dir": str(p), "source_paths": [str(src)]}
    we._STATE["max_scan_agents"] = 1
    try:
        asyncio.run(we.run_inventory(paths))
        raise AssertionError("scan cap not enforced")
    except RuntimeError as exc:
        assert "max_scan_agents" in str(exc)
    finally:
        we._STATE["max_scan_agents"] = 24


def test_engine_fail_fast_and_edit_guard():
    p = tmpdir("eng2")
    we = _engine(p)
    paths = we.ensure_dirs(p)
    cancelled = []

    async def slow(i):
        try:
            await asyncio.sleep(5)
        except asyncio.CancelledError:
            cancelled.append(i)
            raise

    async def boom(i):
        await asyncio.sleep(0.05)
        raise RuntimeError("worker failed")

    t0 = time.time()
    try:
        asyncio.run(we.run_batched([0, 1, 2], lambda i: boom(i) if i == 1 else slow(i)))
        raise AssertionError("failure not propagated")
    except RuntimeError as exc:
        assert "worker failed" in str(exc) and time.time() - t0 < 3 and sorted(cancelled) == [0, 2]

    (p / "sections").mkdir(exist_ok=True)
    for n in ("a", "b", "c"):
        (p / "sections" / f"{n}.tex").write_text(f"orig {n}\n", encoding="utf-8")
    snap = we.snapshot_project(p, paths["snapshots"] / "guard")

    async def rogue(rel):
        (p / rel).write_text("edited\n", encoding="utf-8")
        (p / "sections" / "c.tex").write_text("ROGUE\n", encoding="utf-8")

    asyncio.run(we.run_guarded_edit_batches(p, snap, ["sections/a.tex", "sections/b.tex"], rogue))
    assert (p / "sections" / "a.tex").read_text() == "edited\n" and (p / "sections" / "c.tex").read_text() == "orig c\n"


def test_engine_kills_process_tree():
    p = tmpdir("eng3")
    we = _engine(p)
    pidfile = p / "gc.pid"
    code = ("import subprocess,sys,time;"
            f"q=subprocess.Popen([sys.executable,'-c','import time; time.sleep(120)']);open(r'{pidfile}','w').write(str(q.pid));time.sleep(120)")

    async def go():
        kw = {} if sys.platform.startswith("win") else {"start_new_session": True}
        proc = await asyncio.create_subprocess_exec(PY, "-c", code, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, **kw)
        try:
            await asyncio.wait_for(proc.communicate(), timeout=3)
        except asyncio.TimeoutError:
            we._kill_process_tree(proc)
        await proc.wait()

    asyncio.run(go())
    time.sleep(1)
    gpid = pidfile.read_text().strip()
    if sys.platform.startswith("win"):
        alive = gpid in subprocess.run(["tasklist", "/FI", f"PID eq {gpid}"], capture_output=True, text=True).stdout
    else:
        alive = os.path.exists(f"/proc/{gpid}")
    assert not alive, "grandchild survived the tree kill"


class _FakeCall:
    """What `agent(...)` returns: awaitable (a one-shot call) and callable (the next turn of a persistent handle)."""

    def __init__(self, fake, prompt, schema, role, model):
        self.fake, self.prompt, self.schema, self.role, self.model = fake, prompt, schema, role, model

    def __await__(self):
        return self.fake._answer(self.prompt, self.schema, self.role, self.model).__await__()

    def __call__(self, prompt, schema=None):
        return self.fake._answer(prompt, schema, self.role, self.model)


class FakeAgy:
    """Stand-in for the Antigravity `agent` primitive: records prompts, answers by role (prefix match in `answers`)."""

    def __init__(self, hang_roles=(), planner_items=None, answers=None):
        self.calls = []
        self.hang_roles = set(hang_roles)
        self.planner_items = planner_items or []
        self.answers = answers or {}

    def __call__(self, prompt, schema=None, role=None, workspace=None, model=None):
        return _FakeCall(self, prompt, schema, role, model)

    async def _answer(self, prompt, schema, role, model):
        self.calls.append({"role": role, "prompt": prompt, "model": model})
        if role in self.hang_roles:
            await asyncio.sleep(3600)
        if role == "Plan Revisions":
            return {"work_items": self.planner_items}
        for prefix, ans in self.answers.items():
            if role and role.startswith(prefix):
                return ans(prompt) if callable(ans) else ans
        return "done"


def _antigravity_engine(paper: Path, fake: FakeAgy, **wf_args):
    we = _engine(paper)
    we._STATE.update({"runtime": "antigravity", "ag_agent": fake, "ag_parallel": None, "ag_phase": None, "ag_log": None,
                      "wf_args": {"paper_dir": str(paper), **wf_args}, "models_cfg": {}, "inherit_deadlock": False,
                      "agent_timeout": 1, "agent_retries": 2, "routing_policy": "auto_tiered",
                      "in_plan_mode": True})  # planning turn: everything stays on the fake native agent
    we._STATE.pop("agy_models", None)
    return we


def test_antigravity_timeout_is_bounded_and_not_retried():
    p = tmpdir("agy1")
    fake = FakeAgy(hang_roles={"Hung worker"})
    we = _antigravity_engine(p, fake)
    t0 = time.time()
    try:
        asyncio.run(we.dispatch_agent("do it", schema={"type": "string"}, role="Hung worker", stage_name="repair"))
        raise AssertionError("a hung agent must raise")
    except RuntimeError as exc:
        assert "timed out" in str(exc)
    finally:
        we._STATE["runtime"] = "cli"
    assert len(fake.calls) == 1 and time.time() - t0 < 10, f"timed-out call must not be retried ({len(fake.calls)} calls)"


def test_worker_prompts_carry_search_scope_and_task_cleanup():
    p = tmpdir("agy2")
    notes_parent = tmpdir("agy2_notes")
    auth = notes_parent / "SCOPE.md"
    auth.write_text("scope", encoding="utf-8")
    fake = FakeAgy()
    we = _antigravity_engine(p, fake, authority_paths=[str(auth)])
    try:
        asyncio.run(we.dispatch_agent("do it", schema={"type": "string"}, role="Worker", stage_name="draft"))
    finally:
        we._STATE["runtime"] = "cli"
    prompt = fake.calls[0]["prompt"]
    assert "SEARCH & PROCESS HYGIENE" in prompt and "manage_task" in prompt
    assert str(p.resolve()) in prompt and str(auth.resolve()) in prompt
    assert f"`{notes_parent.resolve()}`" not in prompt, "a file's parent folder must not become search scope"


def test_revise_hands_full_instructions_to_workers():
    p = tmpdir("agy3")
    (p / "sections").mkdir()
    (p / "main.tex").write_text("\\documentclass{article}\\begin{document}\\input{sections/a}\\end{document}\n", encoding="utf-8")
    (p / "sections" / "a.tex").write_text("text\n", encoding="utf-8")
    long_inst = "Insert this acknowledgment verbatim: We thank X.\n" + ("filler line\n" * 1500)
    fake = FakeAgy(planner_items=[{"target_file": "main.tex", "issues_to_fix": "add acknowledgment", "instruction_refs": "line 1"},
                                  {"target_file": "./main.tex", "issues_to_fix": "second fix"},
                                  {"target_file": "../outside.tex", "issues_to_fix": "must be dropped"}])
    we = _antigravity_engine(p, fake, repair_instructions=long_inst)
    orig_tool, orig_build = we.run_tool, we.run_build
    we.run_tool = lambda *a, **k: (0, "", "", {})

    async def _no_build(paths):
        return {"build_json": "b.json", "main_arxiv": "m.tex"}

    we.run_build = _no_build
    try:
        res = asyncio.run(we.run_revise(we.ensure_dirs(p)))
        inst = Path(res["instructions"])
        assert inst.exists() and inst.read_bytes() == long_inst.encode("utf-8"), "inline instructions must be saved byte-for-byte"
        planner = [c for c in fake.calls if c["role"] == "Plan Revisions"][0]["prompt"]
        assert "TRUNCATED" in planner and str(inst) in planner
        workers = [c for c in fake.calls if c["role"].startswith("Revise:")]
        assert [c["role"] for c in workers] == ["Revise: main.tex"], "items for one file are merged; items outside paper_dir dropped"
        assert str(inst) in workers[0]["prompt"] and "second fix" in workers[0]["prompt"] and "line 1" in workers[0]["prompt"]

        fake.calls.clear()
        we._STATE["wf_args"] = {"paper_dir": str(p), "repair_instructions": str(inst), "repair_files": ["sections/a.tex"]}
        res = asyncio.run(we.run_revise(we.ensure_dirs(p)))
        assert res["planner_skipped"] and [c["role"] for c in fake.calls] == ["Revise: a.tex"], "single repair file skips the planner"
    finally:
        we._STATE["runtime"] = "cli"
        we.run_tool, we.run_build = orig_tool, orig_build


AGY_LIST = ["gemini-4-carbon-high", "gemini-pro-orchestrator", "gemini-4-hydrogen-high", "gemini-3.8-flash-high"]


def test_routing_decisions():
    p = tmpdir("route")
    we = _engine(p)

    def r(**kw):
        base = dict(runtime="cli", cli_backend="claude", tier="builder", explicit_model="", policy="auto_tiered",
                    in_plan_mode=None, agy_available=False, agy_models=None)
        base.update(kw)
        x = we.decide_route(**base)
        return (x.kind, x.backend, x.model)

    # Antigravity, planning turn: native inherit / flash
    assert r(runtime="antigravity", tier="heavy", in_plan_mode=True) == ("native", "antigravity", "inherit")
    assert r(runtime="antigravity", tier="builder", in_plan_mode=True) == ("native", "antigravity", "inherit")
    assert r(runtime="antigravity", tier="fast", in_plan_mode=True) == ("native", "antigravity", "flash")
    # Antigravity, execution turn: inherit tiers move to the agy CLI (or pro without it); flash stays native
    assert r(runtime="antigravity", tier="heavy", in_plan_mode=False, agy_available=True, agy_models=AGY_LIST) == ("cli", "agy", "gemini-4-carbon-high")
    assert r(runtime="antigravity", tier="builder", in_plan_mode=False, agy_available=True, agy_models=AGY_LIST) == ("cli", "agy", "gemini-pro-orchestrator")
    assert r(runtime="antigravity", tier="heavy", in_plan_mode=False, agy_available=False) == ("native", "antigravity", "pro")
    assert r(runtime="antigravity", tier="fast", in_plan_mode=False, agy_available=True, agy_models=AGY_LIST) == ("native", "antigravity", "flash")
    # unknown turn type keeps inherit (the engine then falls back to pro on the deadlock error)
    assert r(runtime="antigravity", tier="heavy", in_plan_mode=None) == ("native", "antigravity", "inherit")
    # CLI backends
    assert [r(tier=t)[2] for t in ("heavy", "builder", "fast", "probe")] == ["opus", "", "sonnet", "haiku"]
    assert [r(cli_backend="agy", tier=t, agy_models=AGY_LIST)[2] for t in ("heavy", "builder", "fast", "probe")] == \
        ["gemini-4-carbon-high", "gemini-pro-orchestrator", "gemini-4-hydrogen-high", "gemini-3.8-flash-high"]
    assert r(cli_backend="codex", tier="heavy") == ("cli", "codex", "")
    # model-name rot: preferred ids missing -> next fallback, then CLI default; unknown list -> CLI default
    assert r(cli_backend="agy", tier="heavy", agy_models=["gemini-pro-orchestrator"])[2] == "gemini-pro-orchestrator"
    assert r(cli_backend="agy", tier="heavy", agy_models=["some-future-model"])[2] == ""
    assert r(cli_backend="agy", tier="heavy", agy_models=None)[2] == ""
    # explicit stage models always win; "inherit" counts as "not set" under auto_tiered
    assert r(explicit_model="haiku", tier="heavy") == ("cli", "claude", "haiku")
    assert r(runtime="antigravity", explicit_model="pro", tier="heavy", in_plan_mode=True) == ("native", "antigravity", "pro")
    assert r(runtime="antigravity", explicit_model="gemini-4-argon-high", in_plan_mode=True) == ("cli", "agy", "gemini-4-argon-high")
    assert r(explicit_model="inherit", tier="heavy") == ("cli", "claude", "opus")
    # uniform_inherit: defaults everywhere, but the execution-turn deadlock is still avoided
    assert r(policy="uniform_inherit", tier="heavy") == ("cli", "claude", "")
    assert r(runtime="antigravity", policy="uniform_inherit", tier="fast", in_plan_mode=True) == ("native", "antigravity", "inherit")
    assert r(runtime="antigravity", policy="uniform_inherit", in_plan_mode=False, agy_available=True, agy_models=AGY_LIST) == ("cli", "agy", "")


def test_plan_mode_detection_from_trajectory():
    p = tmpdir("plan")
    we = _engine(p)

    def traj(steps):
        async def trajectory(types=None, tools=None, last=None):
            if types:
                sel = [s for s in steps if s.get("type") in types]
            else:
                sel = [s for s in steps if s.get("tool") in (tools or [])]
            return sel[-last:] if last else sel
        return trajectory

    U = lambda i: {"type": "USER_INPUT", "index": i}  # noqa: E731
    T = lambda i, tool, st="DONE": {"type": "TOOL", "tool": tool, "index": i, "status": st}  # noqa: E731
    run = lambda steps: asyncio.run(we.detect_plan_mode(traj(steps)))  # noqa: E731
    assert run([U(1)]) is True, "a fresh user message puts the host in plan mode"
    assert run([U(1), T(5, "exit_plan_mode")]) is False, "after exit_plan_mode: execution turn"
    assert run([U(1), T(5, "exit_plan_mode"), U(9)]) is True, "a new USER_INPUT resets to plan mode without an enter_plan_mode call"
    assert run([U(1), T(5, "exit_plan_mode"), T(7, "enter_plan_mode")]) is True
    assert run([U(1), T(5, "exit_plan_mode", "RUNNING")]) is True, "only completed tool calls count"
    assert asyncio.run(we.detect_plan_mode(None)) is None

    async def broken(**kw):
        raise RuntimeError("no trajectory here")
    assert asyncio.run(we.detect_plan_mode(broken)) is None
    assert asyncio.run(we.resolve_plan_mode("off", traj([U(1)]))) is False


def test_agy_cli_flags_and_structured_output():
    p = tmpdir("agyflags")
    we = _engine(p)
    seen = []

    class FakeProc:
        returncode = 0
        pid = 0

        async def communicate(self, input=None):
            payload = {"status": "SUCCESS", "conversation_id": "c1", "response": "not json", "structured_output": {"ok": 1}}
            return json.dumps(payload).encode(), b""

    async def fake_exec(*cmd, **kw):
        seen.append(list(cmd))
        return FakeProc()

    orig = we.asyncio.create_subprocess_exec
    we.asyncio.create_subprocess_exec = fake_exec
    try:
        schema = {"type": "object", "properties": {"ok": {"type": "integer"}}}
        res, cid = asyncio.run(we._run_cli_turn("agy", "agy", "write it", schema, model="gemini-pro-orchestrator", read_only=False))
        assert res == {"ok": 1} and cid == "c1", "structured_output must win over the free-text response"
        w = seen[-1]
        assert "--mode" in w and w[w.index("--mode") + 1] == "accept-edits" and "auto" not in w
        assert "--settings" in w and json.loads(w[w.index("--settings") + 1]) == {"toolPermission": "always-proceed"}
        assert w[-2] == "-p" and w[-1].startswith("write it"), "the prompt must stay the last argument"
        sent_schema = json.loads(w[w.index("--json-schema") + 1])
        assert "_turn" in sent_schema["required"] and "set the field `_turn`" in w[-1], "agy answers carry a per-turn stamp"
        asyncio.run(we._run_cli_turn("agy", "agy", "read it", schema, read_only=True))
        assert "--mode" not in seen[-1] and "--settings" not in seen[-1], "read-only agy workers get no auto-approval"
        asyncio.run(we._run_cli_turn("agy", "agy", "referee", schema, read_only=True, allow_bash_read_only=True))
        assert "--mode" not in seen[-1] and "--settings" in seen[-1], "a sandboxed referee may run scripts"
    finally:
        we.asyncio.create_subprocess_exec = orig


def test_execution_turn_routes_inherit_calls_to_agy_cli():
    p = tmpdir("exec")
    fake = FakeAgy()
    we = _antigravity_engine(p, fake)
    we._STATE["in_plan_mode"] = False
    we._STATE["agy_models"] = AGY_LIST
    cli_calls = []

    async def fake_cli(backend, bin_path, prompt, schema, model="", **kw):
        cli_calls.append((backend, model, "manage_task" in prompt))
        return "cli-done", "cid"

    orig_cli, orig_find = we._run_cli_turn, we._find_cli_binary
    we._run_cli_turn = fake_cli
    we._find_cli_binary = lambda name: "agy" if name == "agy" else None
    try:
        out = asyncio.run(we.dispatch_agent("audit", schema={"type": "string"}, role="Reviewer", stage_name="cross_verify", tier="heavy"))
        out2 = asyncio.run(we.dispatch_agent("polish", schema={"type": "string"}, role="Copy-Edit", stage_name="hygiene", tier="fast"))
    finally:
        we._run_cli_turn, we._find_cli_binary = orig_cli, orig_find
        we._STATE["runtime"] = "cli"
        we._STATE["in_plan_mode"] = None
        we._STATE.pop("agy_models", None)
    assert out == "cli-done" and cli_calls == [("agy", "gemini-4-carbon-high", True)], cli_calls
    assert out2 == "done" and fake.calls[-1]["model"] == "flash", "fast tier stays native flash"


def test_agy_error_status_with_complete_answer():
    p = tmpdir("agyerr")
    we = _engine(p)
    obj = {"type": "object", "properties": {"target": {"type": "string"}}, "required": ["target"]}
    txt = {"type": "string"}
    base = {"status": "ERROR", "conversation_id": "c9"}
    # interrupted-and-resumed stream: schema-complete answer in response or structured_output is accepted
    assert we.parse_cli_json("agy", {**base, "response": '{"target": "main.tex"}'}, obj) == ({"target": "main.tex"}, "c9")
    assert we.parse_cli_json("agy", {**base, "structured_output": {"target": "x"}, "error": "stream interrupted"}, obj)[0] == {"target": "x"}
    rejected = [
        ({**base, "response": '{"other": 1}'}, obj),                                    # missing required key
        ({**base, "response": '{"target": "a"}', "finished_reason": "print_timeout"}, obj),  # timed out
        ({**base, "response": "partial text", "error": "stream interrupted"}, txt),     # free text with an error
        ({**base, "response": ""}, txt),
    ]
    for payload, schema in rejected:
        try:
            we.parse_cli_json("agy", payload, schema)
            raise AssertionError(f"should have raised: {payload}")
        except RuntimeError:
            pass
    assert we.parse_cli_json("agy", {**base, "response": "done"}, txt)[0] == "done", "free text without any error is accepted"
    r = we.decide_route(runtime="antigravity", cli_backend="", tier="fast", explicit_model="", policy="auto_tiered",
                        in_plan_mode=False, agy_available=True, agy_models=None)
    assert "execution turn" in r.reason and "planning" not in r.reason


def test_read_only_calls_cannot_change_the_manuscript():
    p = tmpdir("ro")
    (p / "sections").mkdir()
    (p / "main.tex").write_text("orig main\n", encoding="utf-8")
    (p / "sections" / "a.tex").write_text("orig a\n", encoding="utf-8")
    fake = FakeAgy()
    we = _antigravity_engine(p, fake)
    paths = we.ensure_dirs(p)
    (paths["wf"] / "00_locks.json").write_text('{"lock": 1}', encoding="utf-8")

    async def vandal(prompt, schema=None, role=None, workspace=None, model=None):
        (p / "main.tex").write_text("VANDALISED\n", encoding="utf-8")
        (p / "sections" / "new.tex").write_text("new section\n", encoding="utf-8")
        (paths["wf"] / "00_locks.json").write_text("{}", encoding="utf-8")
        return "done"

    we._STATE["ag_agent"] = vandal
    try:
        asyncio.run(we.dispatch_agent("audit only", schema={"type": "string"}, role="Reader", stage_name="cross_verify", read_only=True, tier="heavy"))
        assert (p / "main.tex").read_text(encoding="utf-8") == "orig main\n"
        assert (paths["wf"] / "00_locks.json").read_text(encoding="utf-8") == '{"lock": 1}'
        assert not (p / "sections" / "new.tex").exists() and list((paths["wf"] / "readonly_violations").glob("*new.tex"))
        asyncio.run(we.dispatch_agent("write", schema={"type": "string"}, role="Writer", stage_name="draft", read_only=False))
        assert (p / "main.tex").read_text(encoding="utf-8") == "VANDALISED\n", "writers are not reverted"
    finally:
        we._STATE["runtime"] = "cli"


def test_paper_workflow_entry_without_dunder_file():
    """Mimic run_workflow: exec the entry script without __file__ and with host globals."""
    import ast

    src_path = SCRIPTS / "paper_workflow.py"
    code = compile(src_path.read_text(encoding="utf-8"), str(src_path), "exec", flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT)
    p = tmpdir("pw")
    fake = FakeAgy()
    logs = []

    async def traj(types=None, tools=None, last=None):
        return [{"index": 3, "type": "USER_INPUT", "status": "DONE"}] if types else []

    g = {"__name__": "__wf__", "args": {"mode": "smoke_test", "paper_dir": str(p)}, "agent": fake, "log": logs.append,
         "phase": lambda t: None, "trajectory": traj, "WORKSPACES": []}
    try:
        asyncio.run(eval(code, g))
    finally:
        we = sys.modules["workflow_engine"]
        we._STATE.update({"runtime": "cli", "ag_agent": None, "ag_log": None, "ag_phase": None, "in_plan_mode": None})
    loaded = [m for m in logs if m.startswith("[engine]")]
    assert loaded and str(SCRIPTS.resolve()) in loaded[0], f"the checkout next to the script must be loaded: {loaded}"
    assert any("in_plan_mode=True" in m for m in logs)
    assert [c["model"] for c in fake.calls] == ["flash", "flash"], "probe pings run on native flash"


def test_agy_turn_nonce_rejects_stale_answers():
    p = tmpdir("nonce")
    we = _engine(p)
    obj = {"type": "object", "properties": {"target": {"type": "string"}}, "required": ["target"]}
    sent, prompt = we.with_turn_nonce(obj, "audit", "NEW1")
    assert sent["required"] == ["target", "_turn"] and "NEW1" in prompt and obj["required"] == ["target"], "original schema untouched"
    err = {"status": "ERROR", "conversation_id": "c", "finished_reason": "error"}
    # a resumed turn that failed before finish re-emits the previous turn's answer: rejected
    try:
        we.parse_cli_json("agy", {**err, "response": '{"target": "a", "_turn": "OLD0"}'}, sent, nonce="NEW1")
        raise AssertionError("stale answer accepted")
    except RuntimeError:
        pass
    assert we.parse_cli_json("agy", {**err, "response": '{"target": "a", "_turn": "NEW1"}'}, sent, nonce="NEW1")[0] == {"target": "a"}
    assert we.parse_cli_json("agy", {"status": "SUCCESS", "structured_output": {"target": "b", "_turn": "NEW1"}}, sent, nonce="NEW1")[0] == {"target": "b"}
    txt = {"type": "string"}
    assert we.parse_cli_json("agy", {**err, "response": "edited sec1.tex\nTURN NEW1"}, txt, nonce="NEW1")[0] == "edited sec1.tex"
    try:
        we.parse_cli_json("agy", {**err, "response": "edited sec1.tex\nTURN OLD0"}, txt, nonce="NEW1")
        raise AssertionError("stale free-text answer accepted")
    except RuntimeError:
        pass
    assert we.parse_cli_json("agy", {"status": "SUCCESS", "response": "ok\nTURN NEW1"}, txt, nonce="NEW1")[0] == "ok"


def test_check_issue_collection():
    p = tmpdir("issues")
    (p / "sections").mkdir()
    (p / "sections" / "sec1.tex").write_text("x\n", encoding="utf-8")
    we = _engine(p)
    reports = [
        {"issues": [{"file": str((p / "sections" / "sec1.tex").resolve()), "problem": "sign", "quote": "E = +4", "severity": "HIGH"}]},
        {"issues": [{"file": "./sections\\sec1.tex", "problem": "same", "quote": "E  =  +4", "severity": "high"},
                    {"file": "sections/sec1.tex", "problem": "vague", "severity": "bogus"},
                    {"file": "../outside.tex", "problem": "outside", "severity": "low"},
                    {"file": "sections/sec1.tex", "problem": "", "severity": "low"},
                    {"file": f"{p.name}/sections/sec1.tex", "problem": "path relative to the agent's cwd", "severity": "low"}]},
    ]
    issues = we.dedupe_issues(we.collect_issues(p, reports, "scope"))
    assert [(i["file"], i["severity"]) for i in issues] == [("sections/sec1.tex", "high"), ("sections/sec1.tex", "medium"), ("sections/sec1.tex", "low")], issues
    assert all(i["source"] == "scope" for i in issues)
    assert we.resolve_paper_file(p, "/somewhere/else/sections/sec1.tex") is None, "an absolute path outside the paper stays outside"
    assert we.resolve_paper_file(p, "sections/new.tex") == "sections/new.tex", "a new file inside the paper keeps its path"
    try:
        we.collect_issues(p, ["not a dict"], "scope")
        raise AssertionError("non-object checker result accepted")
    except RuntimeError:
        pass
    det = we.issues_from_defs(p, {"internal_tag_leaks": [{"location": "sections/sec1.tex:3", "match": "Lemma L2"}],
                                  "abstract_issues": [{"kind": "abstract_too_long", "chars": 700, "limit": 600},
                                                      {"kind": "abstract_unexpanded_acronym", "acronym": "MBL"}]})
    assert (det[0]["file"], det[0]["location"], det[0]["severity"]) == ("sections/sec1.tex", "3", "high")
    assert {d["file"] for d in det[1:]} == {"main.tex"} and "MBL" in det[2]["problem"]


def test_entry_refuses_mismatched_engine():
    import ast

    fake_dir = tmpdir("oldengine")
    (fake_dir / "workflow_engine.py").write_text("ENGINE_VERSION = '1.0'\nasync def run_from_antigravity(g):\n    return {}\n", encoding="utf-8")
    src_path = SCRIPTS / "paper_workflow.py"
    code = compile(src_path.read_text(encoding="utf-8"), "<workflow>", "exec", flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT)
    saved_mod = sys.modules.get("workflow_engine")
    saved_path = list(sys.path)
    g = {"__name__": None, "args": {"mode": "smoke_test", "paper_dir": str(fake_dir), "skill_dir": str(fake_dir)}, "WORKSPACES": []}
    try:
        asyncio.run(eval(code, g))
        raise AssertionError("an engine of another version must be refused")
    except RuntimeError as exc:
        assert "skill_dir" in str(exc) and "1.0" in str(exc)
    finally:
        sys.path[:] = saved_path
        if saved_mod is not None:
            sys.modules["workflow_engine"] = saved_mod
        else:
            sys.modules.pop("workflow_engine", None)


def test_readonly_attributes_do_not_break_snapshots():
    import stat as _stat

    p = tmpdir("rattr")
    we = _engine(p)
    paths = we.ensure_dirs(p)
    (p / "sections").mkdir(exist_ok=True)
    a = p / "sections" / "a.tex"
    a.write_text("orig a\n", encoding="utf-8")
    (p / "refs.bib").write_text("@misc{x, title={X}}\n", encoding="utf-8")
    ro = _stat.S_IREAD
    os.chmod(a, ro)
    os.chmod(p / "refs.bib", ro)
    # a stale snapshot from an earlier run that inherited the read-only bits (the reported failure)
    old = paths["snapshots"] / "pre" / "sections"
    old.mkdir(parents=True)
    (old / "a.tex").write_text("stale\n", encoding="utf-8")
    os.chmod(old / "a.tex", ro)
    os.chmod(old, ro)
    try:
        snap = we.snapshot_project(p, paths["snapshots"] / "pre")  # used to raise PermissionError on Windows
        assert (snap / "sections" / "a.tex").read_text(encoding="utf-8") == "orig a\n"
        assert not we.is_readonly(snap / "sections" / "a.tex") and not we.is_readonly(snap / "refs.bib"), "snapshot copies must be writable"
        we.write_user_file(a, b"agent edit\n")
        we.restore_tex_file(p, snap, "sections/a.tex")
        assert a.read_text(encoding="utf-8") == "orig a\n" and we.is_readonly(a), "restore keeps the user's read-only attribute"

        sys.path.insert(0, str(TOOLS))
        import _texio
        tf = _texio.read_text_file(p / "refs.bib")
        bak = _texio.write_back(tf, "@misc{x, title={{X}}}\n")
        assert (p / "refs.bib").read_text(encoding="utf-8") == "@misc{x, title={{X}}}\n" and we.is_readonly(p / "refs.bib")
        assert bak is not None and not we.is_readonly(bak), "backups are deletable"

        os.chmod(paths["wf"] / "00_locks.json" if (paths["wf"] / "00_locks.json").exists() else snap / "refs.bib", ro)
        report = we.preflight_permissions(p)
        assert set(report["readonly_manuscript_files"]) == {"sections/a.tex", "refs.bib"}
        assert not any(we.is_readonly(x) for x in [paths["wf"], *paths["wf"].rglob("*")]), "workflow-owned paths are made writable"
    finally:
        for x in [p, *p.rglob("*")]:
            we.clear_readonly(x)


# ------------------------------------------------------------------------------------- v3: story first, check afterwards

LONG_STATEMENT = "For every finite-range chain " + "with hypothesis h_k, " * 400 + "the gap closes."


def _v3_workspace(name):
    p = tmpdir(name)
    wf = p / ".workflow"
    wf.mkdir(parents=True)
    (wf / "00_ground_truth_ledger.json").write_text(json.dumps({"claims": [
        {"id": "THM-1", "statement": LONG_STATEMENT, "tier": "core"},
        {"id": "NUM-2", "statement": "E0 = -1.2732 for H_7", "tier": "core"},
        {"id": "AUX-3", "statement": "a support lemma", "tier": "support"},
    ]}), encoding="utf-8")
    plan = {"title": "A question about chains", "blocks": [
        {"id": "question", "part": "main", "file": "sections/s1.tex", "purpose": "the puzzle and its history", "claim_ids": ["THM-1"]},
        {"id": "result", "part": "main", "file": "sections/s2.tex", "purpose": "the answer", "claim_ids": ["THM-1", "NUM-2"]},
        {"id": "proof", "part": "sm", "file": "sections/sm1.tex", "purpose": "the proof", "claim_ids": ["AUX-3"]},
    ], "figures": []}
    (wf / "01_plan.json").write_text(json.dumps(plan), encoding="utf-8")
    for n in ("01_story.md", "00_redlines.md", "00_story_material.md"):
        (wf / n).write_text("x\n", encoding="utf-8")
    (p / "macros.tex").write_text("\\newcommand{\\Hsym}{H_{\\mathrm{sym}}}\n", encoding="utf-8")
    return p, plan


def test_plan_validation_names_results_by_ledger_id():
    p, plan = _v3_workspace("plan")
    we = _engine(p)
    pp = p / ".workflow" / "01_plan.json"
    data, err = we.validate_plan(pp, ["THM-1", "NUM-2", "AUX-3"])
    assert err is None and [b["id"] for b in data["blocks"]] == ["question", "result", "proof"]
    bad = [
        ({**plan, "blocks": plan["blocks"] + [{"id": "x", "part": "main", "file": "sections/x.tex", "purpose": "p", "claim_ids": ["THM-99"]}]}, "THM-99"),
        ({**plan, "blocks": plan["blocks"] + [{"id": "y", "part": "main", "file": "sections/s1.tex", "purpose": "p"}]}, "reuses"),
        ({**plan, "blocks": [{"id": "z", "part": "main", "file": "../out.tex", "purpose": "p"}]}, "relative"),
        ({**plan, "blocks": [{"id": "z", "part": "sm", "file": "s.tex", "purpose": "p"}]}, "no main-text"),
        ({**plan, "blocks": [{"id": "z", "part": "body", "file": "s.tex", "purpose": "p"}]}, "part"),
    ]
    for obj, needle in bad:
        pp.write_text(json.dumps(obj), encoding="utf-8")
        assert needle in (we.validate_plan(pp, ["THM-1", "NUM-2", "AUX-3"])[1] or ""), needle


def test_draft_one_author_writes_blocks_in_order_with_exact_records():
    p, plan = _v3_workspace("draft")
    fake = FakeAgy()
    we = _antigravity_engine(p, fake, genre="letter")
    try:
        res = asyncio.run(we.run_draft(we.ensure_dirs(p)))
    finally:
        we._STATE["runtime"] = "cli"
    author = [c for c in fake.calls if c["role"] == "Author"]
    assert len(author) == 4, [c["role"] for c in fake.calls]
    assert "Block 1/2 `question`" in author[0]["prompt"] and "This is the opening block." in author[0]["prompt"]
    assert "Block 2/2 `result`" in author[1]["prompt"] and "sections/s1.tex" in author[1]["prompt"].replace("\\", "/")
    assert "abstract" in author[2]["prompt"] and "600 characters" in author[2]["prompt"]
    assert "Read the whole main text" in author[3]["prompt"]
    assert LONG_STATEMENT in author[0]["prompt"], "ledger records are joined in full, never truncated or paraphrased"
    assert "E0 = -1.2732 for H_7" in author[1]["prompt"] and "E0 = -1.2732" not in author[0]["prompt"]
    sm = [c for c in fake.calls if c["role"] == "SM proof"]
    assert len(sm) == 1 and "Rigour first" in sm[0]["prompt"] and "a support lemma" in sm[0]["prompt"]
    main = (p / "main.tex").read_text(encoding="utf-8")
    assert main.index("\\input{sections/s1.tex}") < main.index("\\input{sections/s2.tex}") and "aps,prl" in main
    assert "\\input{sections/sm1.tex}" in (p / "supplement.tex").read_text(encoding="utf-8")
    assert res["main_blocks"] == 2 and res["sm_blocks"] == 1
    assert (p / ".workflow" / "refs" / "genre_templates.md").exists(), "reference notes are copied for the agents to read"


def test_check_reports_only_and_writes_issue_file():
    p, plan = _v3_workspace("check")
    (p / "sections").mkdir()
    for f in ("s1", "s2", "sm1"):
        (p / "sections" / f"{f}.tex").write_text(f"text of {f}\n", encoding="utf-8")
    (p / "main.tex").write_text("\\begin{document}\\begin{abstract}A.\\end{abstract}\\input{sections/s1}\\input{sections/s2}\\end{document}\n", encoding="utf-8")

    def vandal_check(prompt):
        (p / "sections" / "s1.tex").write_text("REWRITTEN BY A CHECKER\n", encoding="utf-8")
        return {"issues": [{"file": "sections/s1.tex", "location": "1", "quote": "text of s1", "problem": "overclaims THM-1", "severity": "high"}]}

    fake = FakeAgy(answers={"Check s1.tex": vandal_check, "Check": {"issues": []}, "Numeric": {"issues": []},
                            "Reader Check": {"issues": [{"file": "main.tex", "problem": "the introduction never says why it matters", "severity": "medium"}]}})
    we = _antigravity_engine(p, fake, genre="letter")
    orig_tool = we.run_tool

    def fake_tool(name, cli, allow_fail=True, timeout_s=1800):
        if name == "verify_citations.py":
            return 0, "", "", {"entries": [], "todo_placeholders": []}
        if name == "audit_tex_defs.py":
            assert "--abstract-max-chars" in cli and "600" in cli, "letters get the 600-character check"
            return 1, "", "", {"abstract_issues": [{"kind": "abstract_unexpanded_acronym", "acronym": "MBL"}], "abstract_math": ["\\eta"]}
        return 0, "", "", {}

    we.run_tool = fake_tool
    try:
        res = asyncio.run(we.run_check(we.ensure_dirs(p)))
    finally:
        we.run_tool = orig_tool
        we._STATE["runtime"] = "cli"
    assert (p / "sections" / "s1.tex").read_text(encoding="utf-8") == "text of s1\n", "checkers cannot change the manuscript"
    data = json.loads((p / ".workflow" / "03_issues.json").read_text(encoding="utf-8"))
    problems = [i["problem"] for i in data["issues"]]
    assert data["issues"][0]["severity"] == "high" and "overclaims THM-1" in problems
    assert any("MBL" in x for x in problems) and any("why it matters" in x for x in problems)
    assert res["high"] == 1 and (p / "audit_reports" / "01_check_report.md").exists()
    numeric = [c["role"] for c in fake.calls if c["role"].startswith("Numeric")]
    assert sorted(numeric) == ["Numeric NUM-2", "Numeric THM-1"], "scripted checks go to core results, not the first N"
    reader = [c for c in fake.calls if c["role"] == "Reader Check"][0]["prompt"]
    assert "\\eta" in reader and "what is new here" in reader
    checker = [c for c in fake.calls if c["role"] == "Check s2.tex"][0]["prompt"]
    assert "Do not use `01_story.md`" in checker and "Report nothing about style" in checker


def test_revise_works_from_issues_in_reading_order():
    p, plan = _v3_workspace("revise")
    (p / "sections").mkdir()
    for f in ("s1", "s2", "sm1"):
        (p / "sections" / f"{f}.tex").write_text(f"text of {f}\n", encoding="utf-8")
    (p / "main.tex").write_text("\\input{sections/s1} % \\input{sections/old}\n\\input{sections/s2}\n", encoding="utf-8")
    issues = [{"file": f, "problem": f"fix {f}", "severity": "medium"} for f in ("main.tex", "sections/s2.tex", "sections/sm1.tex", "sections/s1.tex")]
    issues.append({"file": f"{p.name}/sections/s2.tex", "problem": "same file, spelled from the agent's cwd", "severity": "low"})
    (p / ".workflow" / "03_issues.json").write_text(json.dumps({"issues": issues}), encoding="utf-8")
    fake = FakeAgy(answers={"Revise": {"fixed": ["done"], "declined": []}})
    we = _antigravity_engine(p, fake, genre="letter")
    try:
        res = asyncio.run(we.run_revise(we.ensure_dirs(p), build_after=False))
    finally:
        we._STATE["runtime"] = "cli"
    assert [c["role"] for c in fake.calls] == ["Revise: sm1.tex", "Revise: s1.tex", "Revise: s2.tex", "Revise: main.tex"], \
        "SM first, then the main text in reading order with the abstract (main.tex) last; one work item per file"
    assert "never by adding qualifiers" in fake.calls[1]["prompt"] and "600 characters" in fake.calls[1]["prompt"]
    assert "fix sections/s1.tex" in fake.calls[1]["prompt"] and "spelled from the agent's cwd" in fake.calls[2]["prompt"]
    assert res["instructions"].endswith("03_issues.json") and (p / "audit_reports" / "06_revise_report.md").exists()
    assert we.main_part_files(p) == ["sections/s1.tex", "sections/s2.tex", "main.tex"], "commented-out inputs are ignored"


def test_cross_vendor_check_routing_and_legacy_stage_models():
    p = tmpdir("xv")
    fake = FakeAgy()
    we = _antigravity_engine(p, fake, check_backend="claude")
    we._STATE["check_backend"] = "claude"
    try:
        r = we.route_call("check", "heavy")
        assert (r.kind, r.backend, r.model) == ("cli", "claude", "opus") and "cross-vendor" in r.reason
        assert we.route_call("draft", "heavy").kind == "native", "only the check stage moves"
        we._STATE["models_cfg"] = {"cross_verify": "sonnet", "narrative": "pro"}
        assert we.explicit_stage_model("check") == "sonnet" and we.explicit_stage_model("story") == "pro"
    finally:
        we._STATE.update({"runtime": "cli", "check_backend": "", "models_cfg": {}})
    we._STATE.update({"backend": "agy", "check_backend": "claude"})
    orig = we._find_cli_binary
    we._find_cli_binary = lambda name: name
    try:
        assert we.resolve_cli_backend("check")[0] == "claude" and we.resolve_cli_backend("draft")[0] == "agy"
    finally:
        we._find_cli_binary = orig
        we._STATE.update({"backend": "claude", "check_backend": ""})


def test_lineage_references_are_verified_by_the_workflow():
    p = tmpdir("lineage")
    we = _engine(p)
    seed = p / "seed.bib"
    seed.write_text("@article{Old2000,\n  title={Old}\n}\n", encoding="utf-8")
    steps = [{"who": "Kramers and Wannier", "question": "q", "found": "f", "doi": "10.1103/PhysRev.60.252", "title": "Statistics of the 2D ferromagnet"},
             {"who": "Made up", "question": "q", "found": "f", "doi": "10.0000/fake", "title": "Fake"},
             {"who": "Old", "question": "q", "found": "f", "arxiv": "0001.0001", "title": "Old"},
             {"who": "No id", "question": "q", "found": "f"}]
    calls = []

    def fake_tool(name, cli, allow_fail=True, timeout_s=1800):
        calls.append(cli)
        ident = cli[2]
        if ident == "10.1103/PhysRev.60.252":
            return 0, "", "", {"verified": True, "bibtex": "@article{Kramers1941,\n  title={Statistics}\n}"}
        if ident == "0001.0001":
            return 0, "", "", {"verified": True, "bibtex": "@article{Old2000,\n  title={Old}\n}"}
        return 1, "", "", {"verified": False, "bibtex": ""}

    orig = we.run_tool
    we.run_tool = fake_tool
    try:
        added = we.verify_lineage_refs(steps, seed, p / "cache.json")
    finally:
        we.run_tool = orig
    text = seed.read_text(encoding="utf-8")
    assert added == 1 and "Kramers1941" in text and text.count("Old2000") == 1
    assert [s["verified"] for s in steps] == [True, False, True, False] and len(calls) == 3
    assert all("--title" in c for c in calls), "an identifier only counts when its title matches"
    md = we.render_lineage(steps, "this paper")
    assert "[verified: Kramers1941]" in md and "[unverified: 10.0000/fake]" in md


def test_abstract_is_checked_as_a_standalone_text():
    a = tmpdir("abs")
    long_tail = " The construction is explicit and the numerics agree." * 12
    (a / "main.tex").write_text(
        "\\documentclass[aps,prl]{revtex4-2}\n\\begin{document}\n\\begin{abstract}\n"
        "We classify palindromic MBL symmetries of Kramers--Wannier (KW) self-dual chains with symmetry-protected "
        "topological (SPT) order, using $\\eta$ and MPS methods." + long_tail + "\n\\end{abstract}\nBody.\n\\end{document}\n", encoding="utf-8")
    rc, js, err = tool("audit_tex_defs.py", "--main", a / "main.tex", "--abstract-max-chars", "600")
    assert js is not None, err
    kinds = {(i["kind"], i.get("acronym")) for i in js["abstract_issues"]}
    assert ("abstract_unexpanded_acronym", "MBL") in kinds and ("abstract_unexpanded_acronym", "MPS") in kinds
    assert not any(k[1] in ("KW", "SPT") for k in kinds), "acronyms expanded inside the abstract are fine"
    assert ("abstract_too_long", None) in kinds and js["abstract_chars_est"] > 600 and js["abstract_math"] == ["\\eta"]
    assert rc == 1 and not js["checks"]["abstract_reads_alone"]



def test_build_fixer_never_makes_figures():
    p = tmpdir("buildfig")
    (p / "main.tex").write_text("x\n", encoding="utf-8")

    def fixer(prompt):
        (p / "figures" / "fig_gap.pdf").write_bytes(b"%PDF-1.4 made up by the fixer")
        return "fixed the $ error and drew the missing figure"

    fake = FakeAgy(answers={"Build/Layout Fix": fixer})
    we = _antigravity_engine(p, fake)
    missing_err = "./sections/r.tex:3: LaTeX Error: File `figures/fig_gap' not found."
    builds = [[missing_err, "./sections/r.tex:4: Missing $ inserted."], [missing_err], [missing_err]]
    orig_tool = we.run_tool

    def fake_tool(name, cli, allow_fail=True, timeout_s=1800):
        if name == "build_and_flatten_tex.py":
            errs = builds.pop(0)
            return 1, "", "", {"compile_result": {"clean_pass": False, "latex_errors": errs}, "layout_check": {}}
        return 0, "", "", {}

    we.run_tool = fake_tool
    try:
        res = asyncio.run(we.run_build(we.ensure_dirs(p)))
    finally:
        we.run_tool = orig_tool
        we._STATE["runtime"] = "cli"
    fixes = [c for c in fake.calls if c["role"].startswith("Build/Layout Fix")]
    assert len(fixes) == 1 and "Never create, regenerate or edit figure files" in fixes[0]["prompt"], "one fix for the $ error, none for the missing figure"
    assert len(builds) == 1, "a build whose only problem is a missing figure is not retried"
    assert not (p / "figures" / "fig_gap.pdf").exists() and list((p / ".workflow" / "readonly_violations").glob("*fig_gap.pdf"))
    assert res["missing_files"] == ["figures/fig_gap"] and res["clean"] is False


def main() -> int:
    tests =[(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in tests:
        t0 = time.time()
        try:
            fn()
            print(f"[PASS] {name} ({time.time() - t0:.1f}s)")
        except Skip as exc:
            print(f"[SKIP] {name}: {exc}")
        except Exception as exc:
            failed += 1
            print(f"[FAIL] {name}: {type(exc).__name__}: {exc}")
    print("ALL_GATE_TESTS_PASSED" if not failed else f"{failed} FAILED")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
