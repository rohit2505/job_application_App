#!/usr/bin/env python3
"""
telegram_poller.py — polls Telegram for a "run" message and, when it sees
one, signals job-pipeline.yml to start (via a GitHub Actions output that
the workflow uses to call the dispatch API).

This replaces resolve_pending.py's old dual role. That script originally
existed to resolve Adzuna postings that needed a real, non-automated click
past their redirect gate -- you'd reply on Telegram with the resolved URL,
and it would finish the application. Adzuna is no longer a job source
(DEFAULT_SOURCES switched to Active Jobs DB via Apify, 2026-09), so that
whole flow is gone. All that's left from resolve_pending.py is the part
that had nothing to do with Adzuna: polling Telegram for a plain "run"
message and dispatching the pipeline.

Requires TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID (without them, there's
nothing to poll -- the script just exits quietly).
"""

import argparse
import json
import os
import sys
import urllib.parse
import urllib.request

TIMEOUT = 30

# Telegram messages that should kick off a full job_search.py -> ... ->
# auto_apply.py pipeline run (job-pipeline.yml). Match is case-insensitive
# and ignores surrounding whitespace; a leading "/" is optional.
TRIGGER_PHRASES = {"run", "run search", "run job search", "run jobs", "runjobs"}


def is_trigger_message(text):
    t = (text or "").strip().lower()
    if t.startswith("/"):
        t = t[1:]
    return t in TRIGGER_PHRASES


def _find_up(fn):
    tried = set()
    for start in (os.getcwd(), os.path.dirname(os.path.abspath(__file__))):
        d = start
        for _ in range(6):
            p = os.path.join(d, fn)
            if p not in tried:
                tried.add(p)
                if os.path.exists(p):
                    return p
            nd = os.path.dirname(d)
            if nd == d:
                break
            d = nd
    return None


def load_keys_json(fn="keys.json"):
    p = _find_up(fn)
    if not p:
        return
    try:
        for k, v in json.load(open(p, encoding="utf-8")).items():
            if not k.startswith("_") and isinstance(v, str) and v.strip():
                os.environ.setdefault(k.strip(), v.strip())
    except Exception as e:
        print(f"  [keys.json] {e}", file=sys.stderr)


def load_dotenv(fn=".env"):
    p = _find_up(fn)
    if not p:
        return
    try:
        for line in open(p, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    except Exception as e:
        print(f"  [.env] {e}", file=sys.stderr)


def cfg(name):
    return os.environ.get(name) or ""


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, type(default)) else default
    except Exception:
        return default


def save_json(path, data):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def telegram_get_updates(offset):
    token = cfg("TELEGRAM_BOT_TOKEN")
    if not token:
        return [], offset
    params = {"timeout": 0, "limit": 50}
    if offset is not None:
        params["offset"] = offset
    url = f"https://api.telegram.org/bot{token}/getUpdates?" + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT) as r:
            data = json.loads(r.read().decode())
    except Exception as e:
        print(f"  [telegram] getUpdates error: {e}", file=sys.stderr)
        return [], offset
    updates = data.get("result", [])
    new_offset = offset
    for u in updates:
        new_offset = max(new_offset or 0, u.get("update_id", 0) + 1)
    return updates, new_offset


def send_telegram(text):
    token, chat_id = cfg("TELEGRAM_BOT_TOKEN"), cfg("TELEGRAM_CHAT_ID")
    if not (token and chat_id):
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
    try:
        urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=TIMEOUT)
        return True
    except Exception as e:
        print(f"  [telegram] send error: {e}", file=sys.stderr)
        return False


def main():
    load_keys_json()
    load_dotenv()
    ap = argparse.ArgumentParser(description="Poll Telegram for a 'run' trigger message")
    ap.add_argument("--offset-file", default=os.environ.get(
        "TELEGRAM_OFFSET_FILE", "state/telegram_update_offset.json"))
    args = ap.parse_args()

    offset_state = load_json(args.offset_file, {})
    offset = offset_state.get("offset")
    updates, new_offset = telegram_get_updates(offset)
    save_json(args.offset_file, {"offset": new_offset})

    if not updates:
        print("  No new Telegram messages.")
        return

    trigger_hit = any(is_trigger_message(u.get("message", {}).get("text", "")) for u in updates)
    if trigger_hit:
        print("  [trigger] got a 'run' message on Telegram — signaling job-pipeline.yml to start")
        gh_out = os.environ.get("GITHUB_OUTPUT")
        if gh_out:
            with open(gh_out, "a", encoding="utf-8") as f:
                f.write("trigger_pipeline=true\n")
        send_telegram("👍 Got it — starting a job search run now.")
    else:
        print(f"  {len(updates)} new Telegram message(s), none of them a 'run' trigger.")


if __name__ == "__main__":
    main()
