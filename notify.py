#!/usr/bin/env python3
"""
notify.py — drain notifications.jsonl and post each entry to Telegram.

Usage:
    python3 notify.py               # post all queued, clear file
    python3 notify.py --dry-run     # print without posting

Called by prune.py at the end of every run. Also safe to call standalone.

Credentials are read from the macOS Keychain (same place goosed stores them).
Falls back to environment variables TORBOX_TELEGRAM_BOT_TOKEN / TORBOX_TELEGRAM_CHAT_ID.
"""
import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from urllib import request as urlrequest
from urllib.error import HTTPError, URLError

HERE = Path(__file__).resolve().parent
NOTIFICATIONS_PATH = HERE / "notifications.jsonl"

TELEGRAM_API = "https://api.telegram.org"

# ---------------------------------------------------------------------------
# Credential loading
# ---------------------------------------------------------------------------

def _keychain_secret():
    """Read the goose keychain entry and extract Telegram bot token."""
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", "goose", "-g"],
            capture_output=True, text=True
        )
        # password line: password: "..."
        import re
        m = re.search(r'password: "(.+)"', result.stderr + result.stdout)
        if m:
            data = json.loads(m.group(1))
            cfg = data.get("gateway_platform_config_telegram", {})
            return cfg.get("bot_token")
    except Exception:
        pass
    return None


def get_credentials():
    """Return (bot_token, chat_id) or raise if not available."""
    bot_token = os.environ.get("TORBOX_TELEGRAM_BOT_TOKEN") or _keychain_secret()
    chat_id   = os.environ.get("TORBOX_TELEGRAM_CHAT_ID", "")
    if not chat_id:
        raise RuntimeError(
            "TORBOX_TELEGRAM_CHAT_ID not set. See .env.example."
        )
    if not bot_token:
        raise RuntimeError(
            "No Telegram bot token found. Set TORBOX_TELEGRAM_BOT_TOKEN or "
            "ensure goose keychain entry exists."
        )
    return bot_token, chat_id


# ---------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------

def send_message(bot_token, chat_id, text, parse_mode="HTML"):
    """POST a message to Telegram. Returns True on success."""
    url = f"{TELEGRAM_API}/bot{bot_token}/sendMessage"
    payload = json.dumps({
        "chat_id": chat_id,
        "text": text,
        "parse_mode": parse_mode,
    }).encode("utf-8")
    req = urlrequest.Request(url, data=payload, method="POST", headers={
        "Content-Type": "application/json",
        "User-Agent": "torbox-pruner/2.0",
    })
    for attempt in range(1, 4):
        try:
            with urlrequest.urlopen(req, timeout=15) as resp:
                return True
        except HTTPError as e:
            if e.code == 429:
                retry_after = int(e.headers.get("Retry-After", "5"))
                time.sleep(retry_after)
                continue
            body = e.read().decode("utf-8", errors="replace")
            # Telegram rejects HTML with 400 — retry as plain text
            if e.code == 400 and parse_mode == "HTML":
                return send_message(bot_token, chat_id, text, parse_mode="")
            print(f"  ❌ Telegram HTTP {e.code}: {body[:200]}", file=sys.stderr)
            return False
        except URLError as e:
            if attempt < 3:
                time.sleep(2 ** attempt)
                continue
            print(f"  ❌ Telegram network error: {e}", file=sys.stderr)
            return False
    return False


def notification_to_text(entry):
    """Format a notifications.jsonl entry as an HTML Telegram message."""
    title = entry.get("title", "TorBox")
    body  = entry.get("body", "")
    ts    = entry.get("ts", "")[:16].replace("T", " ")  # "2026-06-11 17:00"
    return f"<b>{title}</b>\n{body}\n<i>{ts} UTC</i>"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Post queued TorBox notifications to Telegram")
    ap.add_argument("--dry-run", action="store_true", help="Print without posting")
    args = ap.parse_args()

    if not NOTIFICATIONS_PATH.exists() or NOTIFICATIONS_PATH.stat().st_size == 0:
        print("📭 No notifications to post.")
        return 0

    entries = []
    try:
        with NOTIFICATIONS_PATH.open() as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        entries.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
    except OSError as e:
        print(f"❌ Could not read notifications: {e}", file=sys.stderr)
        return 1

    if not entries:
        print("📭 No valid notifications to post.")
        return 0

    if args.dry_run:
        print(f"[dry-run] Would post {len(entries)} notification(s):")
        for e in entries:
            print(f"  • {e.get('title')} — {e.get('body', '')[:80]}")
        return 0

    try:
        bot_token, chat_id = get_credentials()
    except RuntimeError as e:
        print(f"❌ {e}", file=sys.stderr)
        return 1

    posted = 0
    failed_entries = []
    for entry in entries:
        text = notification_to_text(entry)
        if send_message(bot_token, chat_id, text):
            posted += 1
            print(f"  ✅ Posted: {entry.get('title')}")
        else:
            failed_entries.append(entry)
            print(f"  ❌ Failed: {entry.get('title')}")

    # Rewrite file with only the failed entries (successful ones cleared)
    if failed_entries:
        with NOTIFICATIONS_PATH.open("w") as f:
            for e in failed_entries:
                f.write(json.dumps(e) + "\n")
        print(f"  ⚠️  {len(failed_entries)} notification(s) kept for retry.")
    else:
        NOTIFICATIONS_PATH.unlink(missing_ok=True)
        print(f"  🗑️  notifications.jsonl cleared.")

    print(f"✅ Posted {posted}/{len(entries)} notification(s) to Telegram.")
    return 0 if not failed_entries else 1


if __name__ == "__main__":
    sys.exit(main())
