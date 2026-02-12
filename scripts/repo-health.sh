#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

PYTHON_BIN="python3"
RUFF_BIN="ruff"
PYTEST_BIN="pytest"

if [[ -x "$ROOT_DIR/.venv/bin/python" ]]; then
  PYTHON_BIN="$ROOT_DIR/.venv/bin/python"
fi
if [[ -x "$ROOT_DIR/.venv/bin/ruff" ]]; then
  RUFF_BIN="$ROOT_DIR/.venv/bin/ruff"
fi
if [[ -x "$ROOT_DIR/.venv/bin/pytest" ]]; then
  PYTEST_BIN="$ROOT_DIR/.venv/bin/pytest"
fi

PY_FILES=()
while IFS= read -r line; do
  PY_FILES+=("$line")
done < <(git ls-files "*.py")
if [[ "${#PY_FILES[@]}" -eq 0 ]]; then
  echo "[repo-health] no tracked Python files found"
  exit 1
fi

echo "[repo-health] py_compile (tracked files)"
"$PYTHON_BIN" - "$ROOT_DIR" "${PY_FILES[@]}" <<'PY'
import py_compile
import sys
from pathlib import Path

root = Path(sys.argv[1]).resolve()
files = [Path(item) for item in sys.argv[2:]]
for rel in files:
    py_compile.compile(str(root / rel), doraise=True)
PY

echo "[repo-health] ruff"
"$RUFF_BIN" check "${PY_FILES[@]}"

echo "[repo-health] pytest"
PYTHONPATH=src "$PYTEST_BIN" -q

echo "[repo-health] ok"
