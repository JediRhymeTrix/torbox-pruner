# AGENTS.md — Agent Setup Instructions

This file is for AI agents. It contains everything needed to set up torbox-pruner on a new machine from scratch. A human should be able to point you at this repo and say "set it up" and you should be able to do it with one prompt.

---

## Prerequisites to verify before starting

1. `python3 --version` → must be 3.9 or newer
2. Goose desktop app is installed (`/Applications/Goose.app` on macOS)
3. Goose is paired with Telegram (check `~/.config/goose/config.yaml` for `gateway_pairings` with `platform: telegram`)
4. The user has their TorBox API key (from https://torbox.app → Settings → API)
5. The user has their Telegram chat ID (numeric, e.g. `1474185791` — they can get it from @userinfobot on Telegram)

If any prerequisite is missing, tell the user what they need before proceeding.

---

## Setup steps

### 1. Clone the repo

```bash
git clone https://github.com/JediRhymeTrix/torbox-pruner ~/torbox-pruner
cd ~/torbox-pruner
```

### 2. Run install.sh

```bash
TORBOX_API_KEY=<their-api-key> \
TORBOX_TELEGRAM_CHAT_ID=<their-chat-id> \
  bash scripts/install.sh
```

Or interactively:

```bash
bash scripts/install.sh --interactive
```

`install.sh` handles everything:
- Writes `.env` (chmod 600)
- Copies `config.example.json` → `config.json`
- Installs the Goose recipe to `~/.config/goose/recipes/torbox-pruner.yaml`
  (substitutes `INSTALL_DIR` with the actual install path)
- Installs the Goose schedule to `~/.config/goose/schedules/torbox-pruner.yaml`
- Runs a dry-run to verify the API key works

### 3. Verify the dry-run passed

The last line of `install.sh` output should say:
```
✓ Dry-run passed
✓ Installation complete.
```

If it fails, the most likely causes are:
- Wrong API key → check `~/.config/goose/config.yaml` or the TorBox web UI
- No internet → try `curl -s https://api.torbox.app/v1/api/user/me -H "Authorization: Bearer $TORBOX_API_KEY"`

### 4. Tell the user to open Goose

The Goose desktop app auto-detects new schedules in `~/.config/goose/schedules/`. The user just needs to open Goose (or it may already be running). The scheduler will fire within 10 minutes.

### 5. Test Telegram notifications

```bash
cd ~/torbox-pruner
python3 -c "
from notify import get_credentials, send_message
bot_token, chat_id = get_credentials()
send_message(bot_token, chat_id, '<b>Test</b>\ntorbox-pruner setup complete ✓')
print('Sent!')
"
```

If this fails with "No Telegram bot token found", the Goose keychain entry isn't present. Ask the user to:
1. Open Goose desktop app
2. Make sure the Telegram gateway is connected (Settings → Gateways)
3. Try again — the keychain entry is written when Goose pairs

Alternatively, the user can set `TORBOX_TELEGRAM_BOT_TOKEN` in `.env`.

---

## What each file does

| File | Purpose |
|---|---|
| `prune.py` | Main pruner: fetches TorBox state, classifies, deletes dead, fills slots |
| `notify.py` | Posts `notifications.jsonl` entries to Telegram, clears file after sending |
| `run-prune.sh` | Shell wrapper: sources `.env`, runs `prune.py` (used by launchd if needed) |
| `config.json` | Tunable thresholds (created from `config.example.json` by `install.sh`) |
| `.env` | Secrets: API key + Telegram chat ID (never committed) |
| `queue.jsonl` | Magnet links to auto-add when slots are free |
| `state.json` | Runtime state: stuck_timers, daily add counter, last run summary |
| `notifications.jsonl` | Pending Telegram messages (transient, cleared after each send) |
| `goose/recipe.yaml` | Goose recipe template (INSTALL_DIR substituted at install time) |
| `goose/schedule.yaml` | Goose schedule: run every 10 min |

---

## Config keys (config.json)

All optional — defaults are in `config.example.json`.

| Key | Default | What it controls |
|---|---|---|
| `dead_stalled_no_seeds_min` | 30 | Max minutes in `stalled (no seeds)` before deletion |
| `dead_stalled_no_internet_min` | 10 | Max minutes in `stalled (no internet)` |
| `dead_meta_min` | 15 | Max minutes in `metaDL`/`metadata` |
| `dead_checking_min` | 10 | Max minutes in `checking` |
| `fresh_grace_min` | 15 | New torrents exempt from classification for this long |
| `queue_enabled` | true | Auto-fill from queue.jsonl |
| `queue_daily_add_cap` | 50 | Max queue additions per calendar day |
| `max_deletes_per_run` | 10 | Safety cap: deletions per run |
| `notify_on_deletion` | true | Telegram message on deletion |
| `notify_healthy_full` | true | Telegram message when all slots healthy |
| `notify_cooldown_min` | 60 | Min gap between "all healthy" notifications |
| `dry_run` | false | If true: no TorBox writes, no Telegram |

---

## Adding torrents to the queue

```bash
scripts/add-to-queue.sh "magnet:?xt=urn:btih:..."
```

Items are added to TorBox when a slot is free. Already-present items (matched by infohash) are silently skipped and removed from the queue.

---

## Useful commands

```bash
python3 prune.py --status     # show current bucket counts, no action
python3 prune.py --dry-run    # show what would be deleted, no writes
python3 prune.py --no-notify  # run without Telegram output
python3 notify.py --dry-run   # preview pending notifications
scripts/status.sh             # shortcut for --status
scripts/dry-run.sh            # shortcut for --dry-run
```

---

## Troubleshooting

### API key not working
```bash
curl -s "https://api.torbox.app/v1/api/user/me" \
  -H "Authorization: Bearer $TORBOX_API_KEY" | python3 -m json.tool
```
Should return `"success": true`.

### Telegram not sending
```bash
TORBOX_TELEGRAM_BOT_TOKEN=xxx TORBOX_TELEGRAM_CHAT_ID=yyy python3 notify.py --dry-run
```

### Scheduler not firing
Check `~/.config/goose/schedules/torbox-pruner.yaml` exists and Goose app is running.
Check `~/torbox-pruner/logs/prune-YYYY-MM-DD.log` for recent run entries.

### Check logs
```bash
tail -50 ~/torbox-pruner/logs/prune-$(date +%Y-%m-%d).log
```
