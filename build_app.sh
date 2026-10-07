#!/usr/bin/env bash
# Build Mingly.app for macOS
set -e

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$REPO_DIR"

echo "==> Checking backend (undefined names, tests)…"
# Fail on pyflakes errors that are always bugs (undefined names, use before
# assignment); plain style warnings like unused imports are not gated.
if ! python3 -c "import pyflakes" 2>/dev/null; then
  echo "pyflakes is not installed (pip install -r requirements-dev.txt)" >&2
  exit 1
fi
if python3 -m pyflakes backend run_dev.py | grep -E "undefined name|referenced before assignment|undefined local"; then
  echo "pyflakes found errors above" >&2
  exit 1
fi
if ! python3 -c "import mypy" 2>/dev/null; then
  echo "mypy is not installed (pip install -r requirements-dev.txt)" >&2
  exit 1
fi
# Gate on attribute/name/signature errors only (e.g. msg.telegram_id after a rename)
if python3 -m mypy backend --no-error-summary | grep -E "\[(attr-defined|name-defined|call-arg|no-redef)\]"; then
  echo "mypy found errors above" >&2
  exit 1
fi
python3 -m pytest -p no:deepeval -q tests backend

echo "==> Building frontend (type check + tests)…"
cd frontend
npm run typecheck
npx vitest run
npm run build
cd ..

echo "==> Running PyInstaller…"
python3 -m PyInstaller mingly.spec --noconfirm

echo ""
echo "Done! App bundle is at: dist/Common Defense.app"
echo "Run it with:  open 'dist/Common Defense.app'"
