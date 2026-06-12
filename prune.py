#!/usr/bin/env python3
"""
TorBox Pruner v2.0 — state-driven, comprehensive, time-efficient
================================================================
Goal: maximize # of completed torrents per unit time.

Approach:
  - Use TorBox's own download_state field directly (no heuristics)
  - If state == "stalled (no seeds)" for >N min, it's dead — delete
  - If state == "metaDL" / "checking" / "error" / "stalled (no internet)" past
    their thresholds, delete
  - Free slots are filled from queue.jsonl
  - New additions get a fresh_grace_min protection window

No reannounce, no slot-counting gates, no progress/age ratios.
TorBox is the source of truth. We just act on what it tells us.
"""
import argparse
import json
import os
import random
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib import request as urlrequest
from urllib.error import HTTPError, URLError

# ---------------------------------------------------------------------------
# Paths + constants
# ---------------------------------------------------------------------------
HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "config.json"

# Auto-load .env from the project directory so the script works when called
# directly (python3 prune.py) as well as via run-prune.sh.
_env_path = HERE / ".env"
if _env_path.exists():
    with _env_path.open() as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _, _v = _line.partition("=")
                os.environ.setdefault(_k.strip(), _v.strip())
STATE_PATH = HERE / "state.json"
LOGS_DIR = HERE / "logs"
QUEUE_PATH = HERE / "queue.jsonl"
NOTIFICATIONS_PATH = HERE / "notifications.jsonl"

API_BASE = "https://api.torbox.app"
API_KEY = os.environ.get("TORBOX_API_KEY", "")
REQUEST_TIMEOUT = 20

# Concurrent slot counts by TorBox plan ID
# Source: torrentFunctions-4cbb0a76.js (official TorBox web app, 2026-06-11)
#   case 0: w=1  (Free)
#   case 1: w=3+additional  (Essential)
#   case 2: w=10+additional (Pro)
#   case 3: w=5+additional  (Unknown/legacy plan — present in app source)
#   default: w=1
PLAN_BASE_SLOTS = {0: 1, 1: 3, 2: 10, 3: 5}
MAX_ACTIVE_SLOTS = 10  # overwritten at startup by fetch_max_slots()

# ---------------------------------------------------------------------------
# Config — every threshold is a per-state death-age in minutes
# ---------------------------------------------------------------------------
DEFAULT_CONFIG = {
    # Death thresholds — time IN THE BAD STATE (via stuck_timers), not torrent lifetime.
    # Applied only to TorBox's own reported state strings. We never infer state ourselves.
    "dead_stalled_no_seeds_min": 30,       # "stalled (no seeds)" for this long → dead
    "dead_stalled_no_internet_min": 10,    # "stalled (no internet)" for this long → dead
    "dead_meta_min": 15,                   # "metaDL" / "metadata" for this long → dead
    "dead_checking_min": 10,               # "checking" for this long → dead
    # "error": always dead immediately, no threshold

    # New additions: don't classify for this many minutes
    "fresh_grace_min": 15,

    # Queue auto-fill
    "queue_enabled": True,
    "queue_daily_add_cap": 50,
    # Fill priority: "local" = queue.jsonl first, then TorBox queue (default)
    #                "torbox" = TorBox internal queue first, then queue.jsonl
    "queue_priority": "local",

    # Safety
    "max_deletes_per_run": 10,

    # Notifications
    "notify_on_deletion": True,
    "notify_healthy_full": True,
    "notify_cooldown_min": 60,

    "dry_run": False,
}


# ---------------------------------------------------------------------------
# Config + state
# ---------------------------------------------------------------------------
def load_config():
    if CONFIG_PATH.exists():
        try:
            with CONFIG_PATH.open() as f:
                user_cfg = json.load(f)
            return {**DEFAULT_CONFIG, **user_cfg}
        except (OSError, json.JSONDecodeError) as e:
            log(f"⚠️  bad config.json: {e}, using defaults", "warn")
    return DEFAULT_CONFIG.copy()


def load_state():
    base = {
        "fresh_ids": {},
        "stuck_timers": {},
        "queue_consumed_today": 0,
        "queue_consumed_date": "",
        "last_run": None,
        "last_run_summary": None,
        "run_count": 0,
        "total_deleted": 0,
        "last_healthy_full_notified": None,
    }
    if STATE_PATH.exists():
        try:
            with STATE_PATH.open() as f:
                base.update(json.load(f))
        except (OSError, json.JSONDecodeError):
            pass
    return base


def save_state(state):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        with STATE_PATH.open("w") as f:
            json.dump(state, f, indent=2)
    except OSError as e:
        log(f"⚠️  couldn't save state: {e}", "error")


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
def _log_path():
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    return LOGS_DIR / f"prune-{datetime.now(timezone.utc).date().isoformat()}.log"


def log(msg, level="info"):
    line = f"[{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}] [{level.upper():5s}] {msg}"
    try:
        print(line)
    except UnicodeError:
        print(line.encode("ascii", "replace").decode())
    try:
        with _log_path().open("a") as f:
            f.write(line + "\n")
    except OSError:
        pass


# ---------------------------------------------------------------------------
# TorBox API
# ---------------------------------------------------------------------------
def _compute_backoff(attempt, base=2, cap_s=300):
    """Exponential backoff with full jitter (AWS-style).
    attempt: 1-indexed retry number (1, 2, 3, ...)
    Returns: seconds to wait, in [0, min(cap, base^attempt)]
    Full jitter formula: random.uniform(0, min(cap, base^attempt))
    This decorrelates retries across clients and avoids thundering-herd.
    """
    upper = min(cap_s, base ** attempt)
    return random.uniform(0, upper)


def _api_call_with_retry(method, path, body=None, max_attempts=8, op_label=""):
    """
    Single HTTP call with full-jitter exponential backoff and 429/Retry-After.
    
    Retryable errors:
      - 429 (rate-limited) — honor Retry-After header if present
      - 5xx (server error) — exponential backoff with jitter
      - URLError (network) — exponential backoff with jitter
    
    Non-retryable:
      - 4xx except 429 (client errors — bad request, auth, etc.) — raise immediately
    
    max_attempts: max total attempts (default 8, giving ~2-5 minutes of total wait
                  at typical backoff rates)
    """
    url = f"{API_BASE}{path}"
    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "User-Agent": "torbox-pruner/2.0",
    }
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    last_err = None
    for attempt in range(1, max_attempts + 1):
        try:
            req = urlrequest.Request(url, data=data, method=method, headers=headers)
            with urlrequest.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
                if attempt > 1:
                    log(f"  ✓ {op_label or method} {path} succeeded on attempt {attempt}/{max_attempts}")
                return json.loads(resp.read().decode("utf-8"))
        except HTTPError as e:
            # 429: respect Retry-After (TorBox's own throttle)
            if e.code == 429:
                retry_after = e.headers.get("Retry-After", "")
                try:
                    wait = int(retry_after)
                except ValueError:
                    # No Retry-After header → use full-jitter backoff
                    wait = _compute_backoff(attempt, base=2, cap_s=120)
                log(f"  ⏳ {op_label or method} {path} → 429 rate-limited "
                    f"(attempt {attempt}/{max_attempts}), waiting {wait:.1f}s")
                time.sleep(wait)
                last_err = e
                continue
            # 5xx: server error, retry with backoff
            if 500 <= e.code < 600:
                if attempt < max_attempts:
                    wait = _compute_backoff(attempt, base=2, cap_s=120)
                    log(f"  ⏳ {op_label or method} {path} → HTTP {e.code} "
                        f"(attempt {attempt}/{max_attempts}), backoff {wait:.1f}s")
                    time.sleep(wait)
                    last_err = e
                    continue
                log(f"  ❌ {op_label or method} {path} → HTTP {e.code} "
                    f"after {max_attempts} attempts, giving up")
                raise
            # 4xx (other): client error, don't retry
            log(f"  ❌ {op_label or method} {path} → HTTP {e.code} (client error, not retrying)")
            raise
        except URLError as e:
            # Network error: retry with backoff
            if attempt < max_attempts:
                wait = _compute_backoff(attempt, base=2, cap_s=60)
                log(f"  ⏳ {op_label or method} {path} → URLError {e.reason} "
                    f"(attempt {attempt}/{max_attempts}), backoff {wait:.1f}s")
                time.sleep(wait)
                last_err = e
                continue
            log(f"  ❌ {op_label or method} {path} → URLError after {max_attempts} attempts")
            raise
    if last_err:
        raise last_err


def api_get(path, params=None):
    if params:
        from urllib.parse import urlencode
        path = f"{path}?{urlencode(params)}"
    return _api_call_with_retry("GET", path)


def api_post(path, body):
    return _api_call_with_retry("POST", path, body)


def fetch_max_slots():
    """
    Fetch the real concurrent-slot count for the authenticated account.
    Plan IDs per torbox.app/pricing: 0=Free(1), 1=Essential(3), 2=Pro(10).
    additional_concurrent_slots can be purchased on top.
    Falls back to the hardcoded default if the call fails.
    """
    global MAX_ACTIVE_SLOTS
    try:
        r = api_get("/v1/api/user/me")
        if r.get("success"):
            u = r.get("data") or {}
            plan = u.get("plan", 0)
            base = PLAN_BASE_SLOTS.get(plan, 10)
            extra = u.get("additional_concurrent_slots", 0) or 0
            MAX_ACTIVE_SLOTS = base + extra
            log(f"Plan {plan}: {base} base + {extra} extra = {MAX_ACTIVE_SLOTS} concurrent slots")
        else:
            log(f"⚠️  user/me returned success=False, keeping default {MAX_ACTIVE_SLOTS} slots", "warn")
    except Exception as e:
        log(f"⚠️  could not fetch plan info: {e}, keeping default {MAX_ACTIVE_SLOTS} slots", "warn")


def list_torrents():
    """
    Fetch ALL torrents, merging the standard list with ?queued=true.

    Without queued=true, TorBox omits torrents in its internal queue
    (items awaiting a free slot). These are invisible to the pruner,
    so it can't dedup against them and never counts them as occupying
    slots. ?queued=true returns ONLY the queued items, so we fetch both
    and union by ID.
    """
    def _paginate(extra_params):
        results = []
        page = 1
        while True:
            params = {"limit": 1000, "offset": (page - 1) * 1000, **extra_params}
            r = api_get("/v1/api/torrents/mylist", params=params)
            if not r.get("success"):
                raise RuntimeError(f"API error: {r.get('detail') or r}")
            batch = r.get("data") or []
            if not isinstance(batch, list):
                break
            results.extend(batch)
            if len(batch) < 1000:
                break
            page += 1
            if page > 50:
                break
        return results

    # Fetch active list and queued list in parallel would need threading;
    # do sequential for simplicity — queued list is usually small.
    active = _paginate({"bypass_cache": "true"})
    queued = _paginate({"queued": "true", "bypass_cache": "true"})

    # Merge by ID — queued items may overlap with active list on some API versions
    seen = {t["id"] for t in active}
    extras = [t for t in queued if t["id"] not in seen]
    if extras:
        log(f"  +{len(extras)} queued-only item(s) from ?queued=true (total hidden from plain list)")
    all_t = active + extras
    log(f"  list_torrents: {len(active)} active + {len(extras)} queued-only = {len(all_t)} total")
    return all_t


def control_torrent(torrent_id, operation):
    return api_post("/v1/api/torrents/controltorrent", {
        "torrent_id": torrent_id,
        "operation": operation,
    })


def list_torbox_queue():
    """
    Fetch TorBox's own internal download queue via /v1/api/queued/getqueued.
    These are items waiting for a free slot — separate from the active mylist.
    Returns list of dicts with keys: id, hash, name, magnet, created_at, type.
    """
    try:
        r = api_get("/v1/api/queued/getqueued")
        if r.get("success"):
            return r.get("data") or []
        log(f"  ⚠ getqueued returned success=False: {r.get('detail','')}", "warn")
    except Exception as e:
        log(f"  ⚠ getqueued failed: {e}", "warn")
    return []


def control_queued_torrent(queued_id, operation):
    """
    Control a TorBox queued item.
    Valid operations: 'start' (promote to active slot), 'delete'.
    queued_id is the id from /v1/api/queued/getqueued, NOT the torrent_id.
    """
    return api_post("/v1/api/queued/controlqueued", {
        "queued_id": queued_id,
        "operation": operation,
    })


def add_torrent(magnet_or_url, as_queued=False):
    """
    Add a torrent to TorBox via /v1/api/torrents/createtorrent.
    
    NOTE: This endpoint requires MULTIPART/FORM-DATA, not JSON.
    The previous code used /v1/api/torrents/addtorrent with JSON body,
    which always returns 404 (that endpoint doesn't exist).
    
    The official OpenAPI spec (https://api.torbox.app/openapi.json) shows:
      POST /v1/api/torrents/createtorrent  (multipart/form-data)
        - file: optional .torrent file
        - magnet: optional magnet link
        - seed: optional int (default 1)
        - allow_zip: optional bool (default true)
        - name: optional str
        - as_queued: bool (default false) — add to queue instead of active
        - add_only_if_cached: bool (default false) — only add if already cached
    
    We use seed=1 to seed back, allow_zip=true to allow zip extraction.
    Use as_queued=True if you want to add to TorBox's queue (rather than
    starting immediately). We default as_queued=False for the auto-fill
    use case so the pruner can detect if the torrent is dead early.
    """
    from urllib.parse import quote
    from urllib.request import Request as UReq
    
    url = f"{API_BASE}/v1/api/torrents/createtorrent"
    boundary = "----TorboxPrunerBoundary7MA4YWxkTrZu0gW"
    
    def field(name, value):
        return (
            f"--{boundary}\r\n"
            f"Content-Disposition: form-data; name=\"{name}\"\r\n\r\n"
            f"{value}\r\n"
        ).encode("utf-8")
    
    body = b""
    if magnet_or_url.startswith("magnet:"):
        body += field("magnet", magnet_or_url)
    else:
        # URL or .torrent — TorBox can fetch it
        body += field("magnet", magnet_or_url)  # magnet field also accepts URLs
    body += field("seed", "1")
    body += field("allow_zip", "true")
    if as_queued:
        body += field("as_queued", "true")
    body += f"--{boundary}--\r\n".encode("utf-8")
    
    req = UReq(url, data=body, method="POST", headers={
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": f"multipart/form-data; boundary={boundary}",
        "User-Agent": "torbox-pruner/2.0",
    })
    last_err = None
    for attempt in range(1, 6):
        try:
            with urlrequest.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except HTTPError as e:
            if e.code == 429:
                retry_after = e.headers.get("Retry-After", "60")
                try: wait = int(retry_after)
                except ValueError: wait = 60
                log(f"  ⏳ addtorrent 429, waiting {wait}s")
                _time.sleep(wait)
                last_err = e
                continue
            if 500 <= e.code < 600 and attempt < 5:
                _time.sleep(2 ** attempt)
                last_err = e
                continue
            raise
        except URLError as e:
            if attempt < 5:
                _time.sleep(2 ** attempt)
                last_err = e
                continue
            raise
    if last_err: raise last_err


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def parse_ts(s):
    if not s:
        return datetime.now(timezone.utc)
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def minutes_since(ts_str):
    if not ts_str:
        return 0.0
    return (datetime.now(timezone.utc) - parse_ts(ts_str)).total_seconds() / 60.0


# ---------------------------------------------------------------------------
# Fresh protection
# ---------------------------------------------------------------------------
def is_fresh(t, state, cfg):
    tid = str(t.get("id"))
    fresh = state.get("fresh_ids") or {}
    if tid not in fresh:
        return False
    try:
        added = parse_ts(fresh[tid])
        age_min = (datetime.now(timezone.utc) - added).total_seconds() / 60
        return age_min < cfg.get("fresh_grace_min", 15)
    except (TypeError, ValueError):
        return False


def cleanup_fresh(state, current_torrent_ids):
    """Drop fresh_ids entries that are gone or older than 1 hour."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=1)
    fresh = state.get("fresh_ids") or {}
    state["fresh_ids"] = {
        k: v for k, v in fresh.items()
        if k in current_torrent_ids and parse_ts(v) > cutoff
    }


# ---------------------------------------------------------------------------
# Notification queue (Goose recipe drains this and posts to Telegram)
# ---------------------------------------------------------------------------
def notify(title, body, urgent=False):
    log(f"📣 QUEUE NOTIFY: {title} | {body}")
    NOTIFICATIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "title": title,
        "body": body,
        "urgent": bool(urgent),
    }
    try:
        with NOTIFICATIONS_PATH.open("a") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError as e:
        log(f"Failed to queue notification: {e}", "error")


# ---------------------------------------------------------------------------
# Classification — uses TorBox's own state strings directly
# ---------------------------------------------------------------------------
def _stuck_min(tid, bad_state_key, state):
    """
    Return how many minutes this torrent has been continuously in its current
    bad state, using stuck_timers in persisted state.

    stuck_timers[tid] = {"state": <state_key>, "since": <iso timestamp>}

    If the torrent is in a NEW bad state (different from what we last saw),
    we reset the timer to now — giving it a fresh grace window from this moment.
    This is the fix for the created_at age flaw: we measure time in the bad
    state, not total torrent age.
    """
    timers = state.setdefault("stuck_timers", {})
    now = datetime.now(timezone.utc)
    entry = timers.get(tid)
    if entry and entry.get("state") == bad_state_key:
        # Same bad state as before — how long have we been here?
        try:
            since = parse_ts(entry["since"])
            return (now - since).total_seconds() / 60.0
        except (TypeError, ValueError):
            pass
    # New bad state (or first time we see it) — start the clock
    timers[tid] = {"state": bad_state_key, "since": now.isoformat()}
    return 0.0


def _clear_stuck(tid, state):
    """Remove stuck_timer for a torrent that is no longer in a bad state."""
    state.get("stuck_timers", {}).pop(tid, None)


def classify(torrents, state, cfg):
    """
    Bucket every torrent using ONLY TorBox's reported download_state.

    We never infer or override state from speed/progress/eta — that is
    TorBox's job. If TorBox says "downloading" we treat it as downloading,
    full stop. Our only job is to apply configurable grace windows via
    stuck_timers so that a torrent new to a bad state always gets its
    full threshold before we kill it.

    State → bucket mapping (authoritative):
      cached / completed                → done
      downloading                       → healthy  (TorBox's word, we trust it)
      paused                            → paused
      stalled (no seeds)                → stalled_watching → dead after threshold
      stalled (no internet)             → stalled_watching → dead after threshold
      metaDL / metadata                 → stalled_watching → dead after threshold
      checking                          → stalled_watching → dead after threshold
      error                             → dead (immediate)
      uploading / seeding / compressing
        / extracting / moving /
        reannouncing / initializing /
        queued / waiting                → other (transient, count slots, no prune)
      anything else TorBox may add      → other (safe default)
    """
    buckets = {k: [] for k in [
        "done", "healthy", "stalled_watching",
        "dead", "fresh", "paused", "other"
    ]}

    for t in torrents:
        tid = str(t.get("id"))

        # 1. Fresh — highest priority, skip all classification
        if is_fresh(t, state, cfg):
            _clear_stuck(tid, state)
            buckets["fresh"].append(t)
            continue

        ds = (t.get("download_state") or "").lower().strip()
        progress = t.get("progress") or 0

        # 2. Done
        if ds in ("cached", "completed"):
            _clear_stuck(tid, state)
            buckets["done"].append(t)
            continue

        # 3. Paused — user action, never touch
        if ds == "paused":
            _clear_stuck(tid, state)
            buckets["paused"].append(t)
            continue

        # 4. Actively downloading — TorBox says so, we trust it
        if ds == "downloading":
            _clear_stuck(tid, state)
            buckets["healthy"].append(t)
            continue

        # 5. Error — always dead immediately
        if ds == "error":
            t["_death_reason"] = "error state"
            _clear_stuck(tid, state)
            buckets["dead"].append(t)
            continue

        # 6. Bad states with configurable grace windows (stuck_timers)
        #    Each maps to a threshold key. If within threshold → stalled_watching.
        #    If past threshold → dead.
        #
        #    State string provenance (all lowercase, as returned by API):
        #      "stalled (no seeds)"  — confirmed in Dashboard.js (resume optimistic state)
        #                             and in historical pruner logs
        #      "stalled (no internet)" — NOT seen in Dashboard.js or live data; included
        #                             conservatively from old pruner code; uses its own
        #                             threshold in case it ever appears
        #      "metadl"              — confirmed in historical pruner logs ("stuck in metaDL")
        #      "metadata"            — alias for metadl; included defensively
        #      "checking"            — confirmed in historical pruner logs ("stuck in checking")
        dead_states = {
            "stalled (no seeds)":    ("stalled_no_seeds",    "dead_stalled_no_seeds_min"),
            "stalled (no internet)": ("stalled_no_internet", "dead_stalled_no_internet_min"),
            "metadl":                ("metadl",              "dead_meta_min"),
            "metadata":              ("metadl",              "dead_meta_min"),
            "checking":              ("checking",            "dead_checking_min"),
        }

        if ds in dead_states:
            timer_key, cfg_key = dead_states[ds]
            stuck = _stuck_min(tid, timer_key, state)
            threshold = cfg[cfg_key]
            if stuck >= threshold:
                t["_death_reason"] = f"{ds} for {stuck:.0f}min (threshold {threshold}min)"
                _clear_stuck(tid, state)
                buckets["dead"].append(t)
            else:
                buckets["stalled_watching"].append(t)
            continue

        # 7. Any other stalled variant TorBox might report (e.g. "stalled")
        if ds.startswith("stalled"):
            stuck = _stuck_min(tid, f"stalled_other_{ds}", state)
            threshold = cfg["dead_stalled_no_seeds_min"]
            if stuck >= threshold:
                t["_death_reason"] = f"{ds} for {stuck:.0f}min"
                _clear_stuck(tid, state)
                buckets["dead"].append(t)
            else:
                buckets["stalled_watching"].append(t)
            continue

        # 8. Transient / post-download states — count slots, don't prune.
        #    "uploading" seen in Dashboard.js filter labels and as a display string.
        #    The rest ("seeding", "compressing", etc.) are not confirmed in app source
        #    or live data but are plausible BitTorrent states; kept as safe fallback.
        #    All unknown states also land in 'other' via step 9 below.
        if ds in ("uploading", "seeding", "compressing", "extracting", "moving",
                  "reannouncing", "initializing", "queued", "waiting"):
            _clear_stuck(tid, state)
            buckets["other"].append(t)
            continue

        # 9. Unknown state — log it, treat as other (safe: don't prune unknown things)
        log(f"  ⚠ unknown download_state '{ds}' for id={tid} — treating as other", "warn")
        _clear_stuck(tid, state)
        buckets["other"].append(t)

    return buckets


# ---------------------------------------------------------------------------
# Status report
# ---------------------------------------------------------------------------
def report_status(buckets):
    log("=" * 60)
    log("STATUS SNAPSHOT")
    log("=" * 60)
    log(f"  done            : {len(buckets['done'])}")
    log(f"  healthy         : {len(buckets['healthy'])}")
    log(f"  stalled_watching: {len(buckets['stalled_watching'])}")
    log(f"  fresh (grace)   : {len(buckets['fresh'])}")
    log(f"  dead            : {len(buckets['dead'])}")
    log(f"  paused          : {len(buckets['paused'])}")
    log(f"  other           : {len(buckets['other'])}")
    slots_used = (
        len(buckets["healthy"])
        + len(buckets["stalled_watching"]) + len(buckets["dead"])
        + len(buckets["fresh"]) + len(buckets["paused"])
        + len(buckets["other"])
    )
    slots_free = max(0, MAX_ACTIVE_SLOTS - slots_used)
    log(f"  active slots used: {slots_used}/{MAX_ACTIVE_SLOTS}  (FREE: {slots_free})")


# ---------------------------------------------------------------------------
# Plan + execute deletions — just the `dead` bucket
# ---------------------------------------------------------------------------
def plan_deletions(buckets, cfg):
    deletions = []
    cap = cfg.get("max_deletes_per_run", 10)
    for t in buckets["dead"]:
        if len(deletions) >= cap:
            break
        deletions.append({
            "id": t["id"],
            "name": t.get("name", "?"),
            "reason": t.get("_death_reason", "dead"),
            "age_min": minutes_since(t.get("created_at") or ""),
        })
    return deletions


def execute_deletions(deletions, dry_run):
    """
    Two-pass deletion with retry-with-backoff.
    
    Pass 1: Try each delete via control_torrent (which has its own retry via
            _api_call_with_retry's max_attempts=8, full-jitter backoff).
    Pass 2: Collect any that failed in pass 1, then retry them ONCE with
            a longer backoff — this catches transient 5xx that lasted long
            enough to exhaust the per-call retries.
    
    Reports retry statistics in the final summary.
    """
    results = []
    failed = []
    
    if not deletions:
        return results
    
    log(f"  Pass 1: deleting {len(deletions)} item(s) with per-call retry-with-backoff...")
    
    for d in deletions:
        if dry_run:
            log(f"  🟡 DRY-RUN would delete id={d['id']} | {d['reason']} | name={d['name'][:60]}")
            results.append({**d, "status": "dry-run"})
            continue
        try:
            r = control_torrent(d["id"], "delete")
            if r.get("success"):
                log(f"  🗑️  DELETED id={d['id']} | {d['reason']} | name={d['name'][:60]}")
                results.append({**d, "status": "deleted", "response": r})
            else:
                # API returned success=False — not a network error, don't retry
                log(f"  ❌ FAILED id={d['id']} (API returned success=False): {r.get('detail')}")
                results.append({**d, "status": "failed", "response": r})
        except (HTTPError, URLError) as e:
            # Network/5xx error after all per-call retries exhausted
            log(f"  ⚠️  FAILED id={d['id']} after retries: {e}", "warn")
            failed.append(d)
            results.append({**d, "status": "retry-pending", "error": str(e)})
    
    # Pass 2: retry any items that exhausted their per-call retries
    if failed and not dry_run:
        log(f"  Pass 2: retrying {len(failed)} failed delete(s) with longer backoff...")
        # Wait a bit before the second pass to let any server-side issue resolve
        backoff = _compute_backoff(2, base=3, cap_s=60)
        log(f"  ⏳  Waiting {backoff:.1f}s before retry pass...")
        time.sleep(backoff)
        
        still_failing = []
        for d in failed:
            try:
                r = control_torrent(d["id"], "delete")
                if r.get("success"):
                    log(f"  🗑️  DELETED (retry) id={d['id']} | {d['reason']} | name={d['name'][:60]}")
                    # Update the result entry
                    for res in results:
                        if res.get("id") == d["id"]:
                            res["status"] = "deleted"
                            res["response"] = r
                            break
                else:
                    log(f"  ❌ FAILED (retry) id={d['id']}: {r.get('detail')}")
                    still_failing.append(d)
            except (HTTPError, URLError) as e:
                log(f"  ❌ FAILED (retry) id={d['id']}: {e}", "error")
                still_failing.append(d)
        
        if still_failing:
            log(f"  ⚠️  {len(still_failing)} item(s) still failing after 2 passes — will retry next cycle")
            for d in still_failing:
                # Save to state for next-cycle retry visibility
                pass  # already logged
    
    # Retry statistics
    succeeded = sum(1 for r in results if r["status"] in ("deleted", "dry-run"))
    failed_count = sum(1 for r in results if r["status"] not in ("deleted", "dry-run"))
    if failed_count > 0:
        log(f"  📊 Delete summary: {succeeded} succeeded, {failed_count} failed/persistent")
    else:
        log(f"  📊 Delete summary: {succeeded}/{len(results)} succeeded (clean)")
    
    return results


# ---------------------------------------------------------------------------
# Auto-fill from queue
# ---------------------------------------------------------------------------
def load_queue():
    if not QUEUE_PATH.exists():
        return []
    items = []
    try:
        with QUEUE_PATH.open() as f:
            for ln in f:
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    items.append(json.loads(ln))
                except json.JSONDecodeError:
                    continue
    except OSError as e:
        log(f"⚠️  couldn't read queue: {e}", "warn")
    return items


def save_queue(items):
    """Rewrite queue.jsonl with the given list (used to remove consumed items)."""
    try:
        with QUEUE_PATH.open("w") as f:
            for item in items:
                f.write(json.dumps(item) + "\n")
    except OSError as e:
        log(f"⚠️  couldn't save queue: {e}", "warn")


def _extract_hash(magnet):
    """Return lowercase infohash from a magnet URI, or None."""
    import re
    m = re.search(r'xt=urn:btih:([0-9a-fA-F]{40})', magnet or "")
    return m.group(1).lower() if m else None


def _extract_dn(magnet):
    """Return decoded display name (dn=) from a magnet URI, or None."""
    import re
    from urllib.parse import unquote_plus
    m = re.search(r'[?&]dn=([^&]+)', magnet or "")
    return unquote_plus(m.group(1)) if m else None


def fill_slots(slots_free, state, cfg, live_torrents=None):
    """
    Fill free slots from two sources. Order controlled by cfg["queue_priority"]:

      "local"  (default) — queue.jsonl first, then TorBox internal queue.
                           Manually-added items jump ahead of the backlog.
      "torbox"           — TorBox internal queue first, then queue.jsonl.
                           Drains existing TorBox backlog before adding new items.

    TorBox internal queue: promoted via POST /v1/api/queued/controlqueued {start}.
    Local queue.jsonl:     added via createtorrent, deduped by infohash.
    Both sources share the daily cap (queue_daily_add_cap).
    """
    if not cfg.get("queue_enabled", True) or slots_free <= 0:
        return [], []

    today = datetime.now(timezone.utc).date().isoformat()
    if state.get("queue_consumed_date") != today:
        state["queue_consumed_date"] = today
        state["queue_consumed_today"] = 0
    daily_cap = cfg.get("queue_daily_add_cap", 50)
    if state.get("queue_consumed_today", 0) >= daily_cap:
        log(f"  ⏭  daily cap ({daily_cap}) reached")
        return [], []

    added = []
    remaining_slots = slots_free
    priority = cfg.get("queue_priority", "local")  # "local" | "torbox"

    # Fetch both sources up-front (needed for dedup regardless of order)
    torbox_queue = list_torbox_queue()
    local_queue  = load_queue()

    # Build live hash set for dedup (active list + TorBox internal queue)
    live_hashes = set()
    for t in (live_torrents or []):
        h = (t.get("hash") or "").lower()
        if h:
            live_hashes.add(h)
        for alt in (t.get("alternative_hashes") or []):
            live_hashes.add(alt.lower())
    for t in torbox_queue:
        h = (t.get("hash") or "").lower()
        if h:
            live_hashes.add(h)

    # ── Inner: promote items from TorBox's internal queue ────────────────────
    def _fill_torbox():
        nonlocal remaining_slots
        if not torbox_queue:
            log(f"  📭 TorBox internal queue is empty")
            return
        log(f"  📋 TorBox internal queue: {len(torbox_queue)} item(s) waiting")
        for item in torbox_queue:
            if remaining_slots <= 0 or state["queue_consumed_today"] >= daily_cap:
                break
            qid    = item.get("id")
            # TorBox returns "Unknown Torrent Name" for most queued items —
            # try to get a real name from the magnet's dn= parameter instead.
            raw_name = item.get("name") or ""
            dn_name  = _extract_dn(item.get("magnet") or "")
            if dn_name:
                name = dn_name[:60]
            elif raw_name and raw_name.lower() != "unknown torrent name":
                name = raw_name[:60]
            else:
                name = (item.get("hash") or "?")[:16]  # fall back to hash prefix
            try:
                r = control_queued_torrent(qid, "start")
                if r.get("success"):
                    state["queue_consumed_today"] += 1
                    remaining_slots -= 1
                    added.append({"source": "torbox_queue", "queued_id": qid,
                                  "name": name, "hash": (item.get("hash") or "").lower()})
                    log(f"  ✓ started torbox queued id={qid} '{name}' "
                        f"({state['queue_consumed_today']}/{daily_cap} today)")
                else:
                    log(f"  ✗ start queued id={qid} failed: {r.get('detail','')[:80]}")
            except (HTTPError, URLError) as e:
                log(f"  ✗ start queued id={qid} error: {e}")

    # ── Inner: add items from local queue.jsonl ───────────────────────────────
    def _fill_local():
        nonlocal remaining_slots
        if not local_queue:
            log(f"  📭 local queue.jsonl is empty")
            return
        log(f"  📂 local queue.jsonl: {len(local_queue)} item(s)")
        remaining_local = []
        for item in local_queue:
            if remaining_slots <= 0 or state["queue_consumed_today"] >= daily_cap:
                remaining_local.append(item)
                continue
            magnet = item.get("magnet") or item.get("url") or item.get("link")
            if not magnet or not isinstance(magnet, str):
                log(f"  ⚠ dropping invalid local queue item: {str(item)[:80]}")
                continue
            if not (magnet.startswith("magnet:?") or magnet.startswith("http")):
                log(f"  ⚠ dropping local queue item with unrecognised scheme: {str(item)[:80]}")
                continue
            item_hash = _extract_hash(magnet)
            if item_hash and item_hash in live_hashes:
                log(f"  ⏭  skipping local item (already in TorBox): "
                    f"{item.get('name', item_hash[:12])}")
                continue  # drop from queue — already present
            try:
                r = add_torrent(magnet)
                if r.get("success"):
                    data = r.get("data")
                    tid = data.get("torrent_id") if isinstance(data, dict) else None
                    if tid is None and isinstance(data, int):
                        tid = data
                    if tid is None:
                        log(f"  ⚠ add succeeded but no torrent_id: {r}")
                        remaining_local.append(item)
                        continue
                    state.setdefault("fresh_ids", {})[str(tid)] = (
                        datetime.now(timezone.utc).isoformat()
                    )
                    state["queue_consumed_today"] += 1
                    remaining_slots -= 1
                    name = item.get("name", magnet[:40])
                    added.append({"source": "local_queue", "torrent_id": tid, "name": name,
                                  "hash": (item_hash or "")})
                    log(f"  ✓ added local id={tid} '{name}' "
                        f"({state['queue_consumed_today']}/{daily_cap} today)")
                else:
                    log(f"  ✗ local add failed: {r.get('detail','')[:80]}")
                    remaining_local.append(item)
            except (HTTPError, URLError) as e:
                log(f"  ✗ local add error: {e}")
                remaining_local.append(item)
        if len(remaining_local) != len(local_queue):
            save_queue(remaining_local)
            removed = len(local_queue) - len(remaining_local)
            log(f"  🗂  queue.jsonl: removed {removed} item(s), "
                f"{len(remaining_local)} remaining")

    # ── Dispatch in configured priority order ─────────────────────────────────
    if priority == "torbox":
        log(f"  🔀 fill priority: TorBox queue → local queue")
        _fill_torbox()
        if remaining_slots > 0:
            _fill_local()
    else:
        log(f"  🔀 fill priority: local queue → TorBox queue")
        _fill_local()
        if remaining_slots > 0:
            _fill_torbox()

    return added, []


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="TorBox pruner v2.0 (state-driven)")
    ap.add_argument("--dry-run", action="store_true", help="no API writes")
    ap.add_argument("--status", action="store_true", help="snapshot + exit")
    ap.add_argument("--no-notify", action="store_true", help="skip notifications")
    args = ap.parse_args()

    # ── Lockfile: exit immediately if another instance is already running ─────
    # The Goose scheduler fires multiple concurrent recipe sessions when a
    # previous session times out without completing. This ensures only one
    # instance runs at a time; subsequent ones exit cleanly (code 0).
    import fcntl
    LOCK_PATH = HERE / ".prune.lock"
    lock_fh = open(LOCK_PATH, "w")
    try:
        fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log("⏩ Another instance is already running — exiting.")
        lock_fh.close()
        return 0
    # Lock held for the lifetime of this process; released on exit automatically

    cfg = load_config()
    if not API_KEY:
        log("❌ TORBOX_API_KEY env var not set. See .env.example.", "error")
        return 1
    if args.dry_run:
        cfg["dry_run"] = True

    log("=" * 60)
    log(f"TorBox Pruner v2.0 starting (dry_run={cfg['dry_run']})")
    log("=" * 60)

    # 1a. Fetch plan info → sets MAX_ACTIVE_SLOTS
    fetch_max_slots()

    # 1b. Fetch full torrent list (active + queued)
    try:
        torrents = list_torrents()
    except Exception as e:
        log(f"❌ Failed to fetch torrents: {e}", "error")
        return 1
    log(f"Fetched {len(torrents)} torrents from TorBox")

    state = load_state()
    cleanup_fresh(state, {str(t["id"]) for t in torrents})

    # 2. Classify
    buckets = classify(torrents, state, cfg)
    report_status(buckets)

    if args.status:
        return 0

    state["run_count"] = state.get("run_count", 0) + 1
    state["last_run"] = datetime.now(timezone.utc).isoformat()

    # 3. Plan + execute deletions (just the `dead` bucket)
    deletions = plan_deletions(buckets, cfg)
    log(f"Plan: {len(deletions)} deletion(s) (from `dead` bucket)")

    if deletions:
        results = execute_deletions(deletions, dry_run=cfg["dry_run"])
        deleted = sum(1 for r in results if r["status"] in ("deleted", "dry-run"))
        state["total_deleted"] = state.get("total_deleted", 0) + deleted
        if cfg.get("notify_on_deletion") and not args.no_notify and deleted > 0:
            lines = [
                f"• {r['name'][:50]}  ({r['reason']})"
                for r in results
                if r["status"] in ("deleted", "dry-run")
            ]
            body = f"Removed {deleted} dead item(s):\n" + "\n".join(lines[:5])
            if len(lines) > 5:
                body += f"\n…and {len(lines)-5} more"
            notify(f"🗑️  TorBox: Pruned {deleted}", body, urgent=False)

    # 4. Auto-fill from queue
    # Subtract actually-deleted items so freed slots are immediately visible.
    # buckets["dead"] still contains items we tried to delete; only subtract
    # those that succeeded (status="deleted").
    actually_deleted = sum(1 for r in (results if deletions else []) if r.get("status") == "deleted")
    slots_used = (
        len(buckets["healthy"])
        + len(buckets["stalled_watching"])
        + (len(buckets["dead"]) - actually_deleted)   # freed slots subtracted
        + len(buckets["fresh"]) + len(buckets["paused"])
        + len(buckets["other"])
    )
    slots_free = max(0, MAX_ACTIVE_SLOTS - slots_used)

    if slots_free > 0 and cfg.get("queue_enabled", True) and not cfg["dry_run"]:
        log(f"📥 {slots_free} free slot(s) — filling from TorBox queue then local queue...")
        added, _ = fill_slots(slots_free, state, cfg, live_torrents=torrents)
        if added:
            new_torrents = []
            try:
                new_torrents = list_torrents()
                buckets = classify(new_torrents, state, cfg)
                report_status(buckets)
            except Exception as e:
                log(f"⚠️  re-classify after queue fill failed: {e}", "warn")
            if cfg.get("notify_on_deletion") and not args.no_notify:
                # Build a hash→name lookup from the freshly-fetched torrent list
                # so we can substitute real names for anything that was just started.
                hash_to_name = {
                    (t.get("hash") or "").lower(): t.get("name") or ""
                    for t in new_torrents
                    if t.get("name") and t.get("name") != "Unknown Torrent Name"
                }
                def _resolve_name(it):
                    # 1. Try fresh name from just-fetched active list (by hash)
                    h = (it.get("hash") or "").lower()
                    if h and hash_to_name.get(h):
                        return hash_to_name[h]
                    # 2. Use the name we stored at start time (may be dn= or hash prefix)
                    n = it.get("name") or ""
                    if n and n.lower() != "unknown torrent name":
                        return n
                    # 3. Hash prefix as last resort
                    return h[:16] if h else "?"

                names = [_resolve_name(it) for it in added[:5]]
                names_str = "\n".join(f"• {n}" for n in names)
                if len(added) > 5:
                    names_str += f"\n…and {len(added) - 5} more"
                body = f"Started {len(added)} item(s) from queue:\n{names_str}"
                notify("📥 TorBox: Queue Filled", body, urgent=False)
    elif slots_free <= 0:
        log("  no free slots, skipping queue fill")

    # 5. Save state + final log
    state["last_run_summary"] = (
        f"{len(torrents)} total | "
        f"{len(buckets['healthy'])} healthy | "
        f"{len(buckets['stalled_watching'])} watching | "
        f"{len(buckets['dead'])} dead | "
        f"deleted: {len(deletions)}"
    )
    save_state(state)
    log(f"Run complete: {state['last_run_summary']}")

    # 6. Post notifications directly to Telegram (no Goose relay needed)
    if not cfg["dry_run"] and not args.no_notify:
        try:
            import subprocess as _sp
            _sp.run(
                [sys.executable, str(HERE / "notify.py")],
                check=False, timeout=30
            )
        except Exception as e:
            log(f"⚠️  notify.py failed: {e}", "warn")

    return 0


if __name__ == "__main__":
    sys.exit(main())
