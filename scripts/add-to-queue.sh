#!/bin/bash
# Add a magnet link or .torrent URL to the queue.
# Usage: scripts/add-to-queue.sh "magnet:?xt=urn:btih:..."
#        scripts/add-to-queue.sh "https://example.com/file.torrent"
set -e
cd "$(dirname "$0")/.."
if [ -z "$1" ]; then
  echo "Usage: $0 <magnet-or-url>" >&2
  exit 1
fi
python3 -c "
import json, sys
from datetime import datetime, timezone
entry = {
    'magnet': sys.argv[1],
    'added': datetime.now(timezone.utc).isoformat()
}
with open('queue.jsonl', 'a') as f:
    f.write(json.dumps(entry) + '\n')
print(f'✓ Queued: {sys.argv[1][:80]}{\"...\" if len(sys.argv[1]) > 80 else \"\"}')" "$1"
