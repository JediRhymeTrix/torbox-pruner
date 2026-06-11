# torbox-pruner

Autonomous download manager for [TorBox](https://torbox.app). Runs every 10 minutes via the Goose GUI scheduler, keeps your active slots filled with healthy work, evicts stuck/dead torrents, and sends Telegram notifications — all with zero manual intervention.

## What it does

Every 10 minutes:

1. **Fetches** all torrents from TorBox (active list + queued list merged)
2. **Classifies** each torrent using TorBox's own `download_state` field — no heuristics
3. **Deletes** anything in a bad state past its grace window (stalled, stuck-in-metaDL, error, etc.)
4. **Backfills** freed + idle slots from `queue.jsonl`
5. **Notifies** you on Telegram for every deletion and queue fill

## Classification logic

| TorBox `download_state` | Bucket | Action |
|---|---|---|
| `cached` / `completed` | done | none |
| `downloading` | healthy | none — TorBox said so, we trust it |
| `paused` | paused | none — user action |
| `stalled (no seeds)` | watching → dead | dead after 30 min in this state |
| `stalled (no internet)` | watching → dead | dead after 10 min |
| `metaDL` / `metadata` | watching → dead | dead after 15 min |
| `checking` | watching → dead | dead after 10 min |
| `error` | dead | immediate |
| `uploading`, `seeding`, etc. | other | count slots, never pruned |
| anything unknown | other | warned + counted, never pruned |

**Key invariant:** thresholds measure time *in the bad state* (tracked via `stuck_timers` in `state.json`), not total torrent age. A torrent that was downloading fine for 2 hours then stalled gets a fresh 30-minute window from the moment it stalled.

## Requirements

- Python 3.9+ (system `/usr/bin/python3` works)
- [Goose](https://github.com/block/goose) desktop app (for the scheduler)
- A [TorBox](https://torbox.app) account (Free / Essential / Pro)
- A Telegram account paired with Goose (for notifications)

## Quick setup (new machine)

```bash
git clone https://github.com/JediRhymeTrix/torbox-pruner ~/torbox-pruner
cd ~/torbox-pruner

TORBOX_API_KEY=your-api-key \
TORBOX_TELEGRAM_CHAT_ID=your-chat-id \
  bash scripts/install.sh
```

`install.sh` will:
- Write `.env` with your credentials
- Copy `config.example.json` → `config.json`
- Install the Goose recipe to `~/.config/goose/recipes/`
- Install the Goose schedule to `~/.config/goose/schedules/`
- Run a dry-run to verify everything works

Then open the Goose desktop app — the scheduler picks up the new schedule automatically.

### Interactive mode

```bash
bash scripts/install.sh --interactive
# prompts for API key and chat ID
```

### Environment variables

All credentials live in `.env` (never committed). See `.env.example`:

| Variable | Required | Description |
|---|---|---|
| `TORBOX_API_KEY` | ✅ | TorBox API key (Settings → API) |
| `TORBOX_TELEGRAM_CHAT_ID` | ✅ | Your Telegram numeric chat ID |
| `TORBOX_TELEGRAM_BOT_TOKEN` | optional | Only if Goose keychain detection fails |

## Configuration (`config.json`)

Copy `config.example.json` → `config.json` and adjust. All keys are optional — defaults are shown.

| Key | Default | Description |
|---|---|---|
| `dead_stalled_no_seeds_min` | `30` | Time in `stalled (no seeds)` before deletion |
| `dead_stalled_no_internet_min` | `10` | Time in `stalled (no internet)` before deletion |
| `dead_meta_min` | `15` | Time in `metaDL`/`metadata` before deletion |
| `dead_checking_min` | `10` | Time in `checking` before deletion |
| `fresh_grace_min` | `15` | Newly-added torrents are exempt from classification for this long |
| `queue_enabled` | `true` | Enable auto-fill from `queue.jsonl` |
| `queue_daily_add_cap` | `50` | Max torrents added from queue per calendar day |
| `max_deletes_per_run` | `10` | Safety cap on deletions per run |
| `notify_on_deletion` | `true` | Telegram notification on each deletion |
| `notify_healthy_full` | `true` | Notify when all slots are full of healthy downloads |
| `notify_cooldown_min` | `60` | Minimum minutes between "all slots healthy" notifications |
| `dry_run` | `false` | When true, no TorBox API writes |

## Adding torrents to the queue

```bash
scripts/add-to-queue.sh "magnet:?xt=urn:btih:..."
scripts/add-to-queue.sh "https://example.com/file.torrent"
```

Items in `queue.jsonl` are added to TorBox whenever slots are free. Successfully added items are removed from the queue automatically. Items already in TorBox (matched by infohash) are silently dropped.

## Manual runs

```bash
cd ~/torbox-pruner

python3 prune.py              # live run
python3 prune.py --status     # snapshot only, no actions
python3 prune.py --dry-run    # show plan, no API writes
python3 prune.py --no-notify  # suppress Telegram notifications

python3 notify.py             # drain notifications.jsonl manually
python3 notify.py --dry-run   # preview what would be sent

# helpers
scripts/status.sh
scripts/dry-run.sh
```

## Repository layout

```
torbox-pruner/
├── prune.py              # Main pruner (classify, delete, backfill)
├── notify.py             # Telegram notification sender
├── run-prune.sh          # Wrapper: sources .env, calls prune.py
├── config.example.json   # All config keys with defaults
├── .env.example          # Required environment variables
├── goose/
│   ├── recipe.yaml       # Goose recipe template (INSTALL_DIR substituted by install.sh)
│   └── schedule.yaml     # Goose schedule (every 10 min)
├── scripts/
│   ├── install.sh        # Full from-scratch bootstrap
│   ├── add-to-queue.sh   # Add a magnet/URL to queue.jsonl
│   ├── status.sh         # One-shot status snapshot
│   └── dry-run.sh        # Non-destructive test run
├── README.md
└── AGENTS.md             # Agent-readable setup instructions
```

Runtime files (not committed):

```
├── .env                  # Your credentials
├── config.json           # Your config (from config.example.json)
├── state.json            # Persisted run state (timers, counters)
├── queue.jsonl           # Magnet links to add
├── notifications.jsonl   # Pending Telegram notifications (transient)
└── logs/                 # Daily log files
```

## How notifications work

`prune.py` writes events to `notifications.jsonl`. At the end of every run, it calls `notify.py` which reads the bot token from the macOS Keychain (the same entry the Goose Telegram gateway uses) and posts directly to the Telegram Bot API. No Goose relay session is needed — works correctly from the headless scheduler context.

## Slot counting

```
slots_used = healthy + stalled_watching + (dead - actually_deleted)
           + fresh + paused + other
slots_free = max(0, MAX_ACTIVE_SLOTS - slots_used)
```

`MAX_ACTIVE_SLOTS` is fetched live from `/v1/api/user/me` at startup:

| TorBox plan | Concurrent slots |
|---|---|
| Free (0) | 1 |
| Essential (1) | 3 |
| Pro (2) | 10 |
| Legacy plan (3) | 5 |

Additional purchased slots (`additional_concurrent_slots`) are added on top.

## Scheduling

The Goose GUI scheduler fires the `torbox-pruner` recipe every 10 minutes. The recipe runs in a headless session — `notify.py` posts to Telegram directly so no active gateway is required.

To adjust the interval, edit `goose/schedule.yaml` and re-run `install.sh` (or copy the file to `~/.config/goose/schedules/torbox-pruner.yaml` manually).
