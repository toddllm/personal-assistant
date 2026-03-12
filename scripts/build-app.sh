#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
APP_DIR="$ROOT_DIR/app"
BUNDLE_APP="$APP_DIR/src-tauri/target/release/bundle/macos/Personal Assistant.app"
INSTALL_DIR="/Applications"

cd "$APP_DIR"

echo "==> Installing frontend dependencies..."
pnpm install --frozen-lockfile 2>/dev/null || pnpm install

echo "==> Building Tauri app..."
pnpm tauri build --bundles app

echo "==> Stopping running instance..."
osascript -e 'quit app "Personal Assistant"' 2>/dev/null || true
pkill -f "Personal Assistant" 2>/dev/null || true
sleep 1

echo "==> Copying to $INSTALL_DIR..."
rm -rf "$INSTALL_DIR/Personal Assistant.app"
cp -R "$BUNDLE_APP" "$INSTALL_DIR/"

echo "==> Launching..."
open "$INSTALL_DIR/Personal Assistant.app"

echo "==> Done."
