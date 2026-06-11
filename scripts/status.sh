#!/bin/bash
# One-shot status snapshot — bucket counts, no actions, no notifications.
set -e
cd "$(dirname "$0")/.."
python3 prune.py --status --no-notify
