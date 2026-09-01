#!/usr/bin/env python3
"""
resolve_pending.py — picks up your Telegram replies to Adzuna jobs that
auto_apply.py couldn't resolve on its own, and finishes the application
using the real URL you got by actually clicking the link yourself.

Why this exists: some Adzuna postings sit behind a redirect that only
completes for a real, non-automated browser (see auto_apply.py's docstring
for the full story) — and separately, some require logging into an Adzuna
account, which this project will never automate. Rather than leave those
jobs fully manual, apply_notifier.py asks you to reply to its Telegram
message with the real destination URL once you've opened it. This script:

  1. Polls Telegram for new replies to messages it's tracking
     (state/pending_resolution.json, written by apply_notifier.py).
  2. Extracts the URL from your reply.
  3. Feeds that URL straight into auto_apply.py's existing Greenhouse/Lever
     fill-and-submit logic — since it's now a plain, already-resolved URL,
     none of the Adzuna-specific handling (or its failure modes) applies.
  4. Emails/Telegrams you the outcome, same as a normal auto-apply run.

You did the one thing that had to be a real human (the click past Adzuna's
gate); everything after that is automated exactly like any other job.

Requires the same keys.json as the other agents, plus TELEGRAM_BOT_TOKEN /
TELEGRAM_CHAT_ID (without which there's nothing to poll).
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import auto_apply as aa  # noqa: E402
import adzuna_queue  # noqa: E402

TIMEOUT = 30
_URL_RE = re.compile(r"https?://[^\s]+")


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


# Telegram messages that should kick off a full job_search.py -> ... ->
# auto_apply.py pipeline run (job-pipeline.yml), independent of any pending
# Adzuna resolution. Match is case-insensitive and ignores surrounding
# whitespace; a leading "/" is optional (Telegram bot commands usually have
# one, but plain text works too).
TRIGGER_PHRASES = {"run", "run search", "run job search", "run jobs", "runjobs"}


def is_trigger_message(text):
    t = (text or "").strip().lower()
    if t.startswith("/"):
        t = t[1:]
    return t in TRIGGER_PHRASES


def telegram_get_updates(offset):
    token = cfg("TELEGRAM_BOT_TOKEN")
    if not token:
        return [], offset
    params = {"timeout": 0, "limit": 50}
    if offset is not None:
        params["offset"] = offset
    url = f"https://api.telegram.org/bot{token}/getUpdates?" + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT, context=aa.SSL_CTX) as r:
            data = json.loads(r.read().decode())
    except Exception as e:
        print(f"  [telegram] getUpdates error: {e}", file=sys.stderr)
        return [], offset
    updates = data.get("result", [])
    new_offset = offset
    for u in updates:
        new_offset = max(new_offset or 0, u.get("update_id", 0) + 1)
    return updates, new_offset


def main():
    load_keys_json()
    load_dotenv()
    ap = argparse.ArgumentParser(description="Resolve Adzuna jobs via your Telegram-replied URL")
    ap.add_argument("--pending-file", default=os.environ.get("PENDING_RESOLUTION_FILE",
                    "state/pending_resolution.json"))
    ap.add_argument("--offset-file", default=os.environ.get("TELEGRAM_OFFSET_FILE",
                    "state/telegram_update_offset.json"))
    ap.add_argument("--profile", default=os.environ.get("PROFILE_FILE", "../profile.json"))
    ap.add_argument("--success-file", default=os.environ.get("APPLIED_SUCCESS_FILE",
                    "state/auto_applied_success.json"))
    ap.add_argument("--queue-file", default=os.environ.get("ADZUNA_QUEUE_FILE",
                    adzuna_queue.QUEUE_FILE_DEFAULT))
    ap.add_argument("--headless", action="store_true", default=False)
    args = ap.parse_args()

    # Poll Telegram FIRST, unconditionally — a "run" trigger message has to
    # be caught even when there's nothing pending, and the offset has to
    # advance either way or we'd reprocess the same messages forever.
    offset_state = load_json(args.offset_file, {})
    offset = offset_state.get("offset")
    updates, new_offset = telegram_get_updates(offset)

    trigger_hit = any(is_trigger_message(u.get("message", {}).get("text", "")) for u in updates)
    if trigger_hit:
        print("  [trigger] got a 'run' message on Telegram — signaling job-pipeline.yml to start")
        gh_out = os.environ.get("GITHUB_OUTPUT")
        if gh_out:
            with open(gh_out, "a", encoding="utf-8") as f:
                f.write("trigger_pipeline=true\n")
        try:
            aa.send_whatsapp("👍 Got it — starting a job search run now.")
        except Exception:
            pass

    pending = load_json(args.pending_file, {})
    if not pending:
        # Nothing outstanding — this is exactly when a queued Adzuna job (if
        # any) should get its ask sent, rather than waiting for the once-a-
        # day apply_notifier.py run.
        q_status, _q_entry = adzuna_queue.send_next_if_idle(
            aa.send_whatsapp, queue_path=args.queue_file, pending_path=args.pending_file)
        print(f"  [adzuna queue] {q_status}")
        pending = load_json(args.pending_file, {})
        if not pending:
            save_json(args.offset_file, {"offset": new_offset})
            print("  Nothing pending — no Adzuna jobs waiting on a resolved URL.")
            return

    if not updates:
        save_json(args.offset_file, {"offset": new_offset})
        print("  No new Telegram messages.")
        return

    try:
        profile = json.load(open(args.profile, encoding="utf-8"))
    except Exception:
        profile = {}
    success = set(load_json(args.success_file, []))

    matched = []
    for u in updates:
        msg = u.get("message", {})
        reply_to = msg.get("reply_to_message", {})
        reply_to_id = str(reply_to.get("message_id", ""))
        if reply_to_id not in pending:
            continue
        text = msg.get("text", "") or ""
        m = _URL_RE.search(text)
        if not m:
            continue
        matched.append((reply_to_id, m.group(0)))

    if not matched:
        save_json(args.offset_file, {"offset": new_offset})
        print("  No replies matched a pending job this poll.")
        return

    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        sys.exit("ERROR: playwright not installed. Run: pip install playwright && playwright install chromium")

    resolved_count = 0
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=args.headless)
        for reply_to_id, resolved_url in matched:
            entry = pending.pop(reply_to_id, None)
            if not entry:
                continue
            job = dict(entry["job"])
            job["url"] = resolved_url  # this is what makes it "direct" from here on
            resume_path, cover_path = entry.get("resume"), entry.get("cover")
            title, company = job.get("title", ""), job.get("company", "")
            resume_text = ""
            if resume_path and os.path.exists(resume_path):
                try:
                    resume_text = aa.read_resume_text(resume_path)
                except Exception:
                    pass

            page = browser.new_page()
            try:
                status, log, screenshot = aa.apply_to_job(page, job, profile, resume_text,
                                                           resume_path, cover_path)
            except Exception as e:
                status, log, screenshot = "error", [{"error": str(e)}], None
            finally:
                try:
                    page.close()
                except Exception:
                    pass

            print(f"  [{status}] {title[:40]} — {company[:22]}  (via your resolved URL)")
            if status == "applied":
                resolved_count += 1
                jid = (entry["job"].get("url") or f"{company}:{title}").strip()
                success.add(jid)
                qa_lines = "\n".join(f"- {q['question']}: {q['answer']} ({q['source']})"
                                     for q in log if q.get("question"))
                body = (f"Auto-applied to {title} @ {company} using the URL you sent.\n\n"
                        f"Screening answers given:\n{qa_lines or '(none)'}\n\n"
                        "Confirmation screenshot, resume, and cover letter attached.")
                aa.send_email(f"Auto-applied: {title} @ {company}", body,
                              [p_ for p_ in (screenshot, resume_path, cover_path) if p_])
                aa.send_whatsapp(f"✅ Auto-applied to {title} @ {company} using the link you sent!")
            else:
                reason = {
                    "fill_failed": "required fields did not verify as filled — refused "
                                    "to submit blank, retryable.",
                    "submit_unconfirmed": "submit button/form was still there after "
                                          "clicking — likely rejected client-side.",
                    "stale_listing": "the company's own posting date is much older than "
                                      "the source claimed — likely stale, skipped.",
                    "not_greenhouse": "that page isn't a Greenhouse or Lever form — "
                                       "not something this can auto-fill, sorry. You'll "
                                       "need to finish this one by hand.",
                    "captcha": "that form has a CAPTCHA — never auto-solved. You'll need "
                               "to finish this one by hand.",
                    "unanswered": "a screening question couldn't be answered confidently "
                                  "and there was no reply in time.",
                    "error": "hit an unexpected error trying to fill/submit.",
                }.get(status, status)
                aa.send_whatsapp(f"⚠️ {title} @ {company}: {reason}")
        browser.close()

    save_json(args.pending_file, pending)
    save_json(args.offset_file, {"offset": new_offset})
    save_json(args.success_file, sorted(success))

    # A reply was just resolved, so the queue may no longer be "busy" —
    # advance to the next queued Adzuna ask right away instead of waiting
    # for the next apply_notifier.py run.
    q_status, _q_entry = adzuna_queue.send_next_if_idle(
        aa.send_whatsapp, queue_path=args.queue_file, pending_path=args.pending_file)
    print(f"  [adzuna queue] {q_status}")

    print(f"\nResolved {len(matched)} repl{'y' if len(matched)==1 else 'ies'}, "
          f"{resolved_count} auto-applied.")


if __name__ == "__main__":
    main()
