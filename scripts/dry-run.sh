#!/bin/bash
# Manual dry-run: show what the pruner would do without writing to TorBox.
set -e
cd "$(dirname "$0")/.."
python3 prune.py --dry-run --no-notify
