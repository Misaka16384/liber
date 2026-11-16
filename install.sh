#!/usr/bin/env sh
# Liber installer: installs the /write-paper skill into every agent found on this machine.
#
#   One line (macOS / Linux / Git Bash):
#     curl -fsSL https://raw.githubusercontent.com/Misaka16384/liber/master/install.sh | sh
#     curl -fsSL https://raw.githubusercontent.com/Misaka16384/liber/master/install.sh | sh -s -- --hosts claude
#   From a clone:
#     sh install.sh [install.py options, e.g. --hosts claude,antigravity --mode link]
#
#   Environment: INSTALL_DEPS=1 also pip-installs the Python dependencies;
#                LIBER_REPO / LIBER_REF select another fork or branch; WRITE_PAPER_PYTHON picks the interpreter.
set -eu

LIBER_REPO="${LIBER_REPO:-https://github.com/Misaka16384/liber}"
LIBER_REF="${LIBER_REF:-master}"

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" 2>/dev/null && pwd || pwd)"
TMP_DIR=""
cleanup() {
  if [ -n "$TMP_DIR" ]; then rm -rf "$TMP_DIR"; fi
}
trap cleanup EXIT INT TERM

find_python() {
  for cand in "${WRITE_PAPER_PYTHON:-}" python3 python; do
    [ -n "$cand" ] || continue
    if command -v "$cand" >/dev/null 2>&1; then
      if "$cand" -c "import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)" 2>/dev/null; then
        printf '%s' "$cand"
        return 0
      fi
    fi
  done
  return 1
}

PY_BIN="$(find_python || true)"
if [ -z "$PY_BIN" ]; then
  echo "Error: Python 3.8+ is required (set WRITE_PAPER_PYTHON to choose one)." >&2
  exit 1
fi

BOOTSTRAP=0
if [ -f "$SCRIPT_DIR/skills/write-paper/SKILL.md" ] && [ -f "$SCRIPT_DIR/install.py" ]; then
  REPO_ROOT="$SCRIPT_DIR"
else
  # Piped from curl: fetch the repository into a temporary folder first.
  BOOTSTRAP=1
  TMP_DIR="$(mktemp -d 2>/dev/null || mktemp -d -t liber)"
  echo "== Downloading Liber ($LIBER_REF) from $LIBER_REPO =="
  if command -v git >/dev/null 2>&1; then
    git clone --quiet --depth 1 --branch "$LIBER_REF" "$LIBER_REPO.git" "$TMP_DIR/liber"
  elif command -v curl >/dev/null 2>&1; then
    curl -fsSL "$LIBER_REPO/archive/refs/heads/$LIBER_REF.tar.gz" | tar -xz -C "$TMP_DIR"
    mv "$TMP_DIR"/liber-* "$TMP_DIR/liber"
  elif command -v wget >/dev/null 2>&1; then
    wget -qO- "$LIBER_REPO/archive/refs/heads/$LIBER_REF.tar.gz" | tar -xz -C "$TMP_DIR"
    mv "$TMP_DIR"/liber-* "$TMP_DIR/liber"
  else
    echo "Error: need git, curl or wget to download Liber." >&2
    exit 1
  fi
  REPO_ROOT="$TMP_DIR/liber"
fi

echo "== Installing the /write-paper skill (Liber) using $PY_BIN =="
if [ "${INSTALL_DEPS:-0}" = "1" ]; then
  "$PY_BIN" -m pip install -r "$REPO_ROOT/requirements.txt"
fi

if [ "$BOOTSTRAP" = "1" ]; then
  # The download is deleted afterwards, so links would dangle: always copy (a later --mode wins in argparse).
  "$PY_BIN" "$REPO_ROOT/install.py" --check-deps "$@" --mode copy
else
  "$PY_BIN" "$REPO_ROOT/install.py" --check-deps "$@"
fi
echo "Done. Start a new agent session and use /write-paper (Claude Code plugin users: /liber:write-paper)."
