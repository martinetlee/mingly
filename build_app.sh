#!/usr/bin/env bash
# Build Mingly.app for macOS
set -e

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$REPO_DIR"

echo "==> Building frontend…"
cd frontend
npm run build
cd ..

echo "==> Running PyInstaller…"
python3 -m PyInstaller mingly.spec --noconfirm

echo ""
echo "Done! App bundle is at: dist/Mingly.app"
echo "Run it with:  open dist/Mingly.app"
