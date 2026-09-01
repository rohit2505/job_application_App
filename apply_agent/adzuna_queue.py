"""adzuna_queue.py — keeps exactly one Adzuna "please reply with the
resolved URL" Telegram ask outstanding at a time.

Why: if apply_notifier.py fires off a reply-needed ask for every new Adzuna
job in the same run, several can land in Telegram together and a reply is
then ambiguous about which job it's answering. This module makes Adzuna
jobs join a queue instead of being asked about immediately; send_next_if_idle
only sends the next one once nothing is currently awaiting a reply
(state/pending_resolution.json is empty).

Two call sites:
  - apply_notifier.py: enqueue() each new Adzuna job instead of asking right
    away, then call send_next_if_idle() once per run so the queue drains
    over time even if nothing new comes in.
  - resolve_pending.py: call send_next_if_idle() right after resolving (and
    removing) a pending entry, so the next queued job gets asked about
    immediately rather than waiting for the next scheduled apply_notifier
    run.
"""

import json
import os
import time

QUEUE_FILE_DEFAULT = "state/adzuna_queue.json"
PENDING_FILE_DEFAULT = "state/pending_resolution.json"


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


def enqueue(job, resume, cover, text, queue_path=QUEUE_FILE_DEFAULT):
    """Add one Adzuna job to the queue, unless it's already queued (dedup by
    job URL). Doesn't send anything — the actual Telegram ask happens later,
    one at a time, via send_next_if_idle. Returns True if added."""
    queue = load_json(queue_path, [])
    url = (job.get("url") or "").strip()
    if url and any((e.get("job", {}).get("url") or "").strip() == url for e in queue):
        return False
    queue.append({
        "job": job, "resume": resume, "cover": cover, "text": text,
        "queued_at": time.time(),
    })
    save_json(queue_path, queue)
    return True


def send_next_if_idle(send_fn, queue_path=QUEUE_FILE_DEFAULT, pending_path=PENDING_FILE_DEFAULT):
    """If nothing is currently awaiting a reply, send the next queued ask via
    send_fn (a send_whatsapp-style callable returning (ok, detail,
    message_id)) and record it as pending. If something IS still
    outstanding, do nothing — one at a time.

    Returns (status_str, sent_entry_or_None). sent_entry lets the caller
    also deliver that job's resume/cover-letter documents, since this
    module doesn't know about Telegram document uploads."""
    pending = load_json(pending_path, {})
    if pending:
        return f"busy — {len(pending)} still awaiting a reply, not sending another yet", None

    queue = load_json(queue_path, [])
    if not queue:
        return "queue empty — nothing waiting to be asked", None

    entry = queue.pop(0)
    ok, detail, message_id = send_fn(entry["text"])
    if not ok or not message_id:
        queue.insert(0, entry)
        save_json(queue_path, queue)
        return f"send failed ({detail}) — left at front of queue", None

    pending[str(message_id)] = {
        "job": entry["job"], "resume": entry.get("resume"), "cover": entry.get("cover"),
        "notified_at": time.time(),
    }
    save_json(queue_path, queue)
    save_json(pending_path, pending)
    title = entry["job"].get("title", "")
    company = entry["job"].get("company", "")
    return f"sent ask for {title} @ {company}", entry
