#!/bin/bash
# run-prune.sh — wrapper called by launchd / Goose recipe
# Sources .env if present, then runs prune.py with the system Python.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Load .env if it exists (KEY=VALUE lines, no export needed)
if [ -f "$SCRIPT_DIR/.env" ]; then
  # shellcheck disable=SC2046
  export $(grep -v '^\s*#' "$SCRIPT_DIR/.env" | grep -v '^\s*$' | xargs)
fi

cd "$SCRIPT_DIR"
exec /usr/bin/python3 "$SCRIPT_DIR/prune.py" "$@"
