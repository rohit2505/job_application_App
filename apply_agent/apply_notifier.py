#!/usr/bin/env python3
"""
apply_notifier.py — stage 5: notify (Telegram) + deliver (email) for anything
auto_apply.py didn't submit for you. Runs in the cloud (GitHub); your Mac is
not involved.

For each NEW top-scored job not already auto-applied, it:
  - EMAILS you the apply link + the tailored resume/cover-letter attached
    (guaranteed delivery, this is the archival record for interview prep).
  - sends a Telegram ping with the apply link (best-effort mobile nudge).
    Telegram has no expiring session/window like the old Twilio WhatsApp
    sandbox did, so there's no keep-alive nudge needed anymore.

Config via master keys.json / .env:
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, GMAIL_ADDRESS, GMAIL_APP_PASSWORD, DIGEST_TO.
"""

import argparse
import base64
import json
import os
import re
import smtplib
import ssl
import sys
import time
import urllib.parse
import urllib.request
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.application import MIMEApplication

TIMEOUT = 30
LOCAL_KEYS = {}


# ---- config discovery (shared pattern) ----
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
    return os.environ.get(name) or LOCAL_KEYS.get(name, "") or ""


def _ssl():
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


SSL_CTX = _ssl()


def slug(s, n=40):
    s = re.sub(r"[^a-zA-Z0-9]+", "-", (s or "").strip()).strip("-").lower()
    return s[:n] or "job"


# ---- Telegram (replaces the old Twilio WhatsApp sandbox — free, no message
# limits, no 24h window to keep alive) ----
def send_whatsapp(body):
    """Name kept for call-site compatibility; sends via Telegram now.
    Returns (ok, detail, message_id_or_None) — message_id lets a caller
    later match a reply (Telegram's reply_to_message.message_id) back to
    the specific job this message was about."""
    token, chat_id = cfg("TELEGRAM_BOT_TOKEN"), cfg("TELEGRAM_CHAT_ID")
    if not (token and chat_id):
        return False, "telegram creds not set", None
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": chat_id, "text": body}).encode()
    req = urllib.request.Request(url, data=data)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT, context=SSL_CTX) as r:
            resp = json.loads(r.read().decode())
        message_id = (resp.get("result") or {}).get("message_id")
        return True, "sent", message_id
    except Exception as e:
        return False, f"send error: {e}", None


def _multipart_encode(fields, files):
    """Minimal multipart/form-data encoder (stdlib only — no `requests` dep).
    fields: {name: value}. files: {name: (filename, bytes, content_type)}."""
    boundary = "----jobagent" + str(int(time.time() * 1000))
    parts = []
    for name, value in fields.items():
        parts.append(f"--{boundary}\r\n"
                     f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
                     f"{value}\r\n".encode("utf-8"))
    for name, (filename, data, content_type) in files.items():
        parts.append(
            (f"--{boundary}\r\n"
             f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
             f"Content-Type: {content_type}\r\n\r\n").encode("utf-8")
            + data + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode("utf-8"))
    body = b"".join(parts)
    return body, f"multipart/form-data; boundary={boundary}"


def send_telegram_document(path, caption=""):
    """Send a file as a native Telegram document (not a link) — on mobile
    this lands in the chat's own cached files, so the file-upload dialog on
    a job's application page can pick it straight from 'Recent'/'Files'
    without the Gmail download-then-reupload round trip. Best-effort:
    returns (ok, detail); callers should not treat a failure here as fatal
    since the email attachment is still the guaranteed-delivery path."""
    token, chat_id = cfg("TELEGRAM_BOT_TOKEN"), cfg("TELEGRAM_CHAT_ID")
    if not (token and chat_id):
        return False, "telegram creds not set"
    if not path or not os.path.exists(path):
        return False, "file not found"
    try:
        with open(path, "rb") as f:
            data = f.read()
        docx_type = ("application/vnd.openxmlformats-officedocument"
                     ".wordprocessingml.document")
        body, content_type = _multipart_encode(
            {"chat_id": chat_id, "caption": caption[:1024]},
            {"document": (os.path.basename(path), data, docx_type)})
        url = f"https://api.telegram.org/bot{token}/sendDocument"
        req = urllib.request.Request(url, data=body,
                                      headers={"Content-Type": content_type})
        with urllib.request.urlopen(req, timeout=TIMEOUT, context=SSL_CTX) as r:
            json.loads(r.read().decode())
        return True, "sent"
    except Exception as e:
        return False, f"send error: {e}"


# ---- email ----
SUBJECT_TAG = "[JobAgent]"  # every email this script sends carries this tag — set up ONE
                             # Gmail filter matching it to file everything into one label


def send_email(subject, text_body, attach_paths=None):
    addr, pw = cfg("GMAIL_ADDRESS"), cfg("GMAIL_APP_PASSWORD")
    to = cfg("DIGEST_TO") or addr
    if not (addr and pw):
        print("  [email] skipped — set GMAIL_ADDRESS / GMAIL_APP_PASSWORD", file=sys.stderr)
        return False
    if not subject.startswith(SUBJECT_TAG):
        subject = f"{SUBJECT_TAG} {subject}"
    msg = MIMEMultipart()
    msg["Subject"], msg["From"], msg["To"] = subject, addr, to
    msg.attach(MIMEText(text_body, "plain", "utf-8"))
    for attach_path in (attach_paths or []):
        if attach_path and os.path.exists(attach_path):
            with open(attach_path, "rb") as f:
                part = MIMEApplication(f.read(), Name=os.path.basename(attach_path))
            part["Content-Disposition"] = f'attachment; filename="{os.path.basename(attach_path)}"'
            msg.attach(part)
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=SSL_CTX, timeout=TIMEOUT) as s:
            s.login(addr, pw)
            s.sendmail(addr, [to], msg.as_string())
        return True
    except Exception as e:
        print(f"  [email] error: {e}", file=sys.stderr)
        return False


# ---- state ----
def load_seen(p):
    try:
        return set(json.load(open(p, encoding="utf-8")))
    except Exception:
        return set()


def save_seen(p, s):
    d = os.path.dirname(p)
    if d:
        os.makedirs(d, exist_ok=True)
    json.dump(sorted(s), open(p, "w", encoding="utf-8"), indent=0)


ADZUNA_HOST_SUFFIX = "adzuna.com"


def _is_adzuna_url(url):
    try:
        host = (urllib.parse.urlparse(url).hostname or "").lower()
    except Exception:
        return False
    return host == ADZUNA_HOST_SUFFIX or host.endswith("." + ADZUNA_HOST_SUFFIX)


PENDING_RESOLUTION_FILE_DEFAULT = "state/pending_resolution.json"


def load_pending(path):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_pending(path, data):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def _folder_for(job, out_root):
    return os.path.join(out_root, f"{slug(job.get('company'))}__{slug(job.get('title'))}")


def resume_for(job, out_root):
    p = os.path.join(_folder_for(job, out_root), "resume.docx")
    return p if os.path.exists(p) else None


def cover_letter_for(job, out_root):
    p = os.path.join(_folder_for(job, out_root), "cover_letter.docx")
    return p if os.path.exists(p) else None


def main():
    load_keys_json()
    load_dotenv()
    ap = argparse.ArgumentParser(description="Stage 4: notify (WhatsApp) + deliver (email)")
    ap.add_argument("--scored", default=os.environ.get("SCORED_FILE", "../job_filter_agent/scored.json"))
    ap.add_argument("--resumes", default=os.environ.get("RESUME_OUT", "../resume_creation_agent/output"))
    ap.add_argument("--min-score", type=int, default=int(os.environ.get("MIN_SCORE", 75)))
    ap.add_argument("--top", type=int, default=int(os.environ.get("TOP", 10)))
    ap.add_argument("--seen-file", default=os.environ.get("SEEN_FILE", "state/notified.json"))
    ap.add_argument("--success-file", default=os.environ.get("APPLIED_SUCCESS_FILE",
                    "state/auto_applied_success.json"),
                    help="jobs auto_apply.py already submitted — skip these, no double-notify")
    ap.add_argument("--pending-file", default=os.environ.get("PENDING_RESOLUTION_FILE",
                    PENDING_RESOLUTION_FILE_DEFAULT),
                    help="tracks Telegram message_id -> Adzuna job, so a reply with the "
                         "resolved employer URL can be picked up by resolve_pending.py")
    ap.add_argument("--keepalive", action="store_true",
                    help="(legacy no-op) Telegram has no expiring session, unlike the old "
                         "Twilio WhatsApp sandbox, so there's nothing to keep alive")
    args = ap.parse_args()

    if args.keepalive:
        print("  [keepalive] no-op — Telegram doesn't need this, kept only for compatibility.")
        return

    try:
        jobs = json.load(open(args.scored, encoding="utf-8"))
    except Exception as e:
        sys.exit(f"ERROR: could not read {args.scored}: {e}")
    jobs = sorted([j for j in jobs if j.get("score", 0) >= args.min_score],
                  key=lambda j: j.get("score", 0), reverse=True)[:args.top]

    seen = load_seen(args.seen_file)
    already_applied = load_seen(args.success_file)
    pending = load_pending(args.pending_file)
    sent = 0
    for j in jobs:
        jid = (j.get("url") or f"{j.get('company')}:{j.get('title')}").strip()
        if jid in already_applied:
            continue  # auto_apply.py already submitted this one and emailed you
        if jid in seen:
            continue
        title, company = j.get("title", ""), j.get("company", "")
        link = j.get("url", "")
        resume = resume_for(j, args.resumes)
        cover = cover_letter_for(j, args.resumes)
        attachments = [p for p in (resume, cover) if p]
        # 1) email — guaranteed delivery, with the exact tailored resume + cover
        # letter attached, so it's archived under one label for interview prep
        # once you've applied on the employer's site.
        body = (f"{title} @ {company}   (fit {j.get('score')})\n"
                f"{j.get('reason','')}\n\nApply here: {link}\n\n"
                "Tailored resume" + (" and cover letter" if cover else "") +
                " attached: this is the exact version to submit, and your "
                "record to reference later if you get an interview." if attachments else
                "No tailored resume/cover letter found — run the resume agent for this job first.")
        emailed = send_email(f"{title} @ {company}", body, attachments)
        # 2) Telegram ping (best-effort) — plus the actual files as native
        # Telegram documents, so applying is possible entirely from a phone:
        # tap the link, and when the job site asks for a resume upload, the
        # file is already sitting in this chat's cache, no Gmail round trip.
        is_adzuna = _is_adzuna_url(link)
        if is_adzuna:
            # Adzuna's redirect can't always be resolved automatically (see
            # auto_apply.py) — but a real, non-automated click from your own
            # phone/browser always gets past it. Ask for that one click back,
            # via Telegram's native "reply" so we know exactly which job it's
            # for, and hand the resulting real URL straight to auto-apply.
            tg_text = (f"🧑‍💻 {title} @ {company} (fit {j.get('score')})\n"
                       f"Open: {link}\n\n"
                       "This one needs a real click to get past Adzuna's redirect — "
                       "open it, let it land on the real company/ATS page, then "
                       "REPLY to THIS message with that final URL and I'll take it "
                       "from there (fill + submit automatically where supported).\n"
                       + ("(resume + cover letter below)" if attachments else "(resume in your email)"))
        else:
            tg_text = (f"🧑‍💻 {title} @ {company} (fit {j.get('score')})\n"
                       f"Apply: {link}\n"
                       + ("(resume + cover letter below)" if attachments else "(resume in your email)"))
        tg_ok, detail, tg_message_id = send_whatsapp(tg_text)
        if not tg_ok and emailed:
            print(f"  [telegram] not delivered ({detail}) — job is in your email.", file=sys.stderr)
        if tg_ok and is_adzuna and tg_message_id:
            pending[str(tg_message_id)] = {
                "job": j, "resume": resume, "cover": cover,
                "notified_at": time.time(),
            }
        if tg_ok:
            for path, label in ((resume, "Resume"), (cover, "Cover letter")):
                if not path:
                    continue
                doc_ok, doc_detail = send_telegram_document(
                    path, caption=f"{label} — {title} @ {company}")
                if not doc_ok:
                    print(f"  [telegram] {label.lower()} not delivered ({doc_detail}) "
                          "— it's in your email.", file=sys.stderr)
        if emailed or tg_ok:
            seen.add(jid)
            sent += 1
            print(f"  sent: [{j.get('score')}] {title[:40]} — {company[:22]}  "
                  f"(email={'y' if emailed else 'n'}, telegram={'y' if tg_ok else 'n'})")

    save_seen(args.seen_file, seen)
    save_pending(args.pending_file, pending)
    print(f"\nNotified {sent} new job(s).")


if __name__ == "__main__":
    main()
