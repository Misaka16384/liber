#!/usr/bin/env python3
"""Shared text I/O helpers for the /write-paper tools.

Every tool that rewrites a user file goes through `write_back`, which
- keeps the file's original encoding (UTF-8, UTF-8 with BOM, cp1252 or Latin-1),
- keeps its original newline convention (LF or CRLF),
- copies the previous version into `<dir>/.wp_backups/` before touching it,
- writes atomically (temp file + replace),
- does nothing at all when the content did not change.

Generated outputs (reports, flattened arXiv sources) go through `write_new`, which
always writes UTF-8 with LF newlines on every platform.
"""

from __future__ import annotations

import codecs
import datetime
import os
import re
import shutil
import stat
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional

BACKUP_DIR_NAME = ".wp_backups"
SKIP_DIR_NAMES = frozenset({".git", ".workflow", "submission", "__pycache__", BACKUP_DIR_NAME, ".venv", "node_modules"})
_DECODE_ORDER = ("utf-8", "cp1252", "latin-1")


@dataclass
class TextFile:
    path: Path
    text: str  # newline-normalised to "\n"
    encoding: str
    newline: str  # "\n" or "\r\n"
    bom: bool


def read_text_file(path: Path) -> TextFile:
    p = Path(path)
    raw = p.read_bytes()
    bom = raw.startswith(codecs.BOM_UTF8)
    if bom:
        raw = raw[len(codecs.BOM_UTF8):]
    text = None
    encoding = "utf-8"
    for enc in _DECODE_ORDER:
        try:
            text = raw.decode(enc)
            encoding = enc
            break
        except UnicodeDecodeError:
            continue
    if text is None:  # latin-1 never fails; this is only for type checkers
        text = raw.decode("latin-1")
        encoding = "latin-1"
    crlf = raw.count(b"\r\n")
    lf_only = raw.count(b"\n") - crlf
    newline = "\r\n" if crlf > lf_only else "\n"
    text = text.replace("\r\n", "\n")
    return TextFile(path=p, text=text, encoding=encoding, newline=newline, bom=bom)


def read_text(path: Path) -> str:
    """Read any TeX/BibTeX/Markdown file without ever raising on its encoding (newlines -> "\\n")."""
    return read_text_file(path).text


def _make_writable(p: Path) -> None:
    try:
        os.chmod(p, stat.S_IMODE(os.stat(p).st_mode) | stat.S_IWRITE | stat.S_IWUSR)
    except OSError:
        pass


def backup_file(path: Path) -> Path:
    p = Path(path)
    bdir = p.parent / BACKUP_DIR_NAME
    bdir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    dest = bdir / f"{p.name}.{stamp}.bak"
    shutil.copy2(p, dest)
    _make_writable(dest)  # a backup must stay deletable even if its source is read-only
    return dest


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    """Replace the file's content; its permissions (including a Windows read-only attribute) are kept."""
    p = Path(path)
    fd, tmp = tempfile.mkstemp(prefix=f".{p.name}.", suffix=".tmp", dir=str(p.parent))
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        if p.exists():
            try:
                shutil.copymode(p, tmp)  # the new file gets the old mode, read-only included
            except OSError:
                pass
            if not os.access(p, os.W_OK):
                _make_writable(p)  # Windows cannot replace a read-only file
        os.replace(tmp, p)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_back(tf: TextFile, new_text: str, backup: bool = True) -> Optional[Path]:
    """Write `new_text` over `tf.path` preserving encoding/newlines. Returns the backup path (or None if unchanged)."""
    new_norm = new_text.replace("\r\n", "\n")
    if new_norm == tf.text:
        return None
    bak = backup_file(tf.path) if backup and tf.path.exists() else None
    data = new_norm.replace("\n", tf.newline) if tf.newline != "\n" else new_norm
    try:
        payload = data.encode(tf.encoding)
    except UnicodeEncodeError:
        # The edit introduced characters the legacy encoding cannot hold: fall back to UTF-8.
        payload = data.encode("utf-8")
        tf.encoding = "utf-8"
    if tf.bom and tf.encoding == "utf-8":
        payload = codecs.BOM_UTF8 + payload
    _atomic_write_bytes(tf.path, payload)
    tf.text = new_norm
    return bak


def write_new(path: Path, text: str) -> None:
    """Write a generated artefact as UTF-8 + LF (no backup; not a user-authored file)."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_bytes(p, text.replace("\r\n", "\n").encode("utf-8"))


def utf8_stdio() -> None:
    """Make print() safe for non-ASCII JSON on consoles with legacy code pages (e.g. cp936)."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            pass


def iter_files(roots: Iterable[Path], suffixes: Iterable[str]) -> List[Path]:
    """Collect files with the given suffixes under roots, skipping workflow/backup/VCS directories."""
    sfx = {s.lower() for s in suffixes}
    out: List[Path] = []
    for root in roots:
        r = Path(root)
        if r.is_file():
            if r.suffix.lower() in sfx:
                out.append(r)
            continue
        if not r.is_dir():
            continue
        for p in sorted(r.rglob("*")):
            if not p.is_file() or p.suffix.lower() not in sfx:
                continue
            rel_parts = p.relative_to(r).parts[:-1]
            if any(part in SKIP_DIR_NAMES for part in rel_parts):
                continue
            out.append(p)
    return out


_INPUT_RE = re.compile(r"\\(input|include|subfile)(?:\s*\{([^{}]+)\}|\s+([^\s{}%\\]+))")


def resolve_tex_input(target: str, tex_dir: Path, root_dir: Path) -> Optional[Path]:
    """Resolve `\\input{target}` the way TeX does: try `target`, then `target.tex`, relative to both dirs."""
    t = target.strip().strip('"')
    if not t or "#" in t or "\\" in t:
        return None
    for base in (tex_dir, root_dir):
        for cand in (base / t, base / f"{t}.tex"):
            if cand.is_file():
                return cand
    return None


def collect_tex_tree(entry_files: Iterable[Path], missing: Optional[List[str]] = None) -> List[Path]:
    """Files actually reached from the entry files through \\input/\\include/\\subfile (outside comments)."""
    order: List[Path] = []
    seen: set = set()

    def walk(p: Path, root: Path) -> None:
        rp = p.resolve()
        if rp in seen:
            return
        seen.add(rp)
        order.append(p)
        code = strip_comments_text(read_text(p))
        for m in _INPUT_RE.finditer(code):
            tgt = m.group(2) if m.group(2) is not None else m.group(3)
            child = resolve_tex_input(tgt or "", p.parent, root)
            if child is None:
                if missing is not None and tgt and "#" not in tgt and "\\" not in tgt:
                    missing.append(f"{p.name}: \\{m.group(1)}{{{tgt}}}")
                continue
            walk(child, root)

    for ef in entry_files:
        ef = Path(ef)
        if ef.is_file():
            walk(ef, ef.parent)
    return order


# --------------------------------------------------------------------------------------
# TeX-aware comment detection shared by several tools
# --------------------------------------------------------------------------------------

_CS_RE = re.compile(r"\\([A-Za-z@]+)(\*?)")
_URL_LIKE = frozenset({"url", "path", "nolinkurl", "href"})
_VERB_LIKE = frozenset({"verb", "lstinline", "mintinline", "Verb"})


def find_comment_start(line: str) -> int:
    """Index of the first `%` that starts a TeX comment on this line, or -1.

    Handles `\\%` (escaped), `\\\\%` (line break followed by a real comment), `\\verb|..%..|`,
    and `%` inside `\\url{..}` / `\\href{..}` / `\\path{..}` arguments.
    """
    i, n = 0, len(line)
    while i < n:
        c = line[i]
        if c == "\\":
            m = _CS_RE.match(line, i)
            if not m:
                i += 2  # control symbol such as \% \\ \{ \$
                continue
            name = m.group(1)
            j = m.end()
            if name in _VERB_LIKE:
                if name == "mintinline":  # \mintinline{lang}<delim>...<delim>
                    while j < n and line[j] == " ":
                        j += 1
                    if j < n and line[j] == "{":
                        close = line.find("}", j)
                        j = close + 1 if close != -1 else n
                if j < n:
                    delim = line[j]
                    close_ch = "}" if delim == "{" else delim
                    end = line.find(close_ch, j + 1)
                    i = end + 1 if end != -1 else n
                    continue
            if name in _URL_LIKE:
                k = j
                while k < n and line[k] == " ":
                    k += 1
                if k < n and line[k] == "{":
                    depth = 0
                    while k < n:
                        if line[k] == "{":
                            depth += 1
                        elif line[k] == "}":
                            depth -= 1
                            if depth == 0:
                                break
                        k += 1
                    i = k + 1
                    continue
            i = j
            continue
        if c == "%":
            return i
        i += 1
    return -1


def strip_comment(line: str) -> str:
    """The code part of a line (everything before the comment `%`)."""
    k = find_comment_start(line)
    return line if k == -1 else line[:k]


_VERBATIM_ENVS = ("verbatim", "verbatim*", "Verbatim", "lstlisting", "minted", "comment", "alltt")
_BEGIN_VERB_RE = re.compile(r"\\begin\{(" + "|".join(re.escape(e) for e in _VERBATIM_ENVS) + r")\}")


def strip_comments_text(text: str) -> str:
    """Remove comment tails from every line outside verbatim-like environments (keeps line count)."""
    out: List[str] = []
    in_env: Optional[str] = None
    for ln in text.split("\n"):
        if in_env is not None:
            # `comment` environments are not typeset, so their body is dropped like a comment.
            out.append("" if in_env == "comment" else ln)
            if f"\\end{{{in_env}}}" in ln:
                in_env = None
            continue
        code = strip_comment(ln)
        out.append(code)
        mb = _BEGIN_VERB_RE.search(code)
        if mb and f"\\end{{{mb.group(1)}}}" not in code[mb.end():]:
            in_env = mb.group(1)
    return "\n".join(out)
