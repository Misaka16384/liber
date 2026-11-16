#!/usr/bin/env sh
set -eu

REPO_ROOT="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"

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
  echo "Error: Python 3.8+ is required." >&2
  exit 1
fi

echo "== Installing /write-paper v2 using $PY_BIN =="
if [ "${INSTALL_DEPS:-0}" = "1" ]; then
  "$PY_BIN" -m pip install -r "$REPO_ROOT/requirements.txt"
fi

"$PY_BIN" "$REPO_ROOT/install.py" --check-deps "$@"
