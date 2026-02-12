#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 1 ]]; then
  echo "Usage: scripts/create-github-epic.sh <owner/repo>"
  exit 1
fi

REPO="$1"
TITLE="EPIC: Productionize Personal Assistant Capture Stack"
BODY_FILE="docs/epics/productionize-audio-assist-epic.md"

if [[ ! -f "$BODY_FILE" ]]; then
  echo "Missing epic body file: $BODY_FILE"
  exit 1
fi

gh issue create \
  --repo "$REPO" \
  --title "$TITLE" \
  --label "epic" \
  --body-file "$BODY_FILE"
