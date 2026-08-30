#!/usr/bin/env python3
"""
apply_notifier.py — stage 4: notify (WhatsApp) + deliver (email), so you apply
from your phone. Runs in the cloud (GitHub); your Mac is not involved.

For each NEW top-scored job it:
  - EMAILS you the apply link + the tailored resume.docx attached (guaranteed
    delivery — no WhatsApp window rules).
  - sends a WhatsApp ping with the apply link (best-effort mobile nudge). If
    WhatsApp can't reach you (24h window closed), you still have the email, and
    the agent emails a "reactivate WhatsApp" note.

--keepalive mode sends a periodic WhatsApp nudge so you reply and keep the 24h
window open (schedule it ~every 20h). If that can't deliver, it emails you to
re-text the bot.

Config via master keys.json / .env:
  TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_WHATSAPP_FROM (e.g. +14155238886),
  WHATSAPP_TO (your mobile, +1...), GMAIL_ADDRESS, GMAIL_APP_PASSWORD, DIGEST_TO.
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


# ---- Twilio WhatsApp ----
def _wa(num):
    num = (num or "").strip()
    return num if num.startswith("whatsapp:") else f"whatsapp:{num}"


def send_whatsapp(body):
    """Return (ok, detail). ok=False if creds missing or WhatsApp rejected it
    (e.g. 24h window closed)."""
    sid, tok = cfg("TWILIO_ACCOUNT_SID"), cfg("TWILIO_AUTH_TOKEN")
    frm, to = cfg("TWILIO_WHATSAPP_FROM"), cfg("WHATSAPP_TO")
    if not all([sid, tok, frm, to]):
        return False, "twilio creds not set"
    url = f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"
    data = urllib.parse.urlencode({"From": _wa(frm), "To": _wa(to), "Body": body}).encode()
    req = urllib.request.Request(url, data=data)
    req.add_header("Authorization", "Basic " + base64.b64encode(f"{sid}:{tok}".encode()).decode())
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT, context=SSL_CTX) as r:
            msg = json.loads(r.read().decode())
        msid = msg.get("sid")
    except Exception as e:
        return False, f"send error: {e}"
    # poll final status briefly — a freeform message outside the 24h window fails
    for _ in range(4):
        time.sleep(4)
        try:
            sreq = urllib.request.Request(url.replace("Messages.json", f"Messages/{msid}.json"))
            sreq.add_header("Authorization", "Basic " + base64.b64encode(f"{sid}:{tok}".encode()).decode())
            with urllib.request.urlopen(sreq, timeout=TIMEOUT, context=SSL_CTX) as r:
                st = json.loads(r.read().decode())
            status = st.get("status")
            if status in ("delivered", "read", "sent"):
                return True, status
            if status in ("failed", "undelivered"):
                return False, f"{status} ({st.get('error_code')})"
        except Exception:
            break
    return True, "queued"


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
    ap.add_argument("--keepalive", action="store_true",
                    help="send a WhatsApp keep-alive nudge (schedule ~every 20h)")
    args = ap.parse_args()

    if args.keepalive:
        ok, detail = send_whatsapp("👋 Reply anything to keep your job alerts flowing on WhatsApp. "
                                   "(No reply needed if you've messaged recently.)")
        print(f"  [keepalive] whatsapp: {'ok' if ok else 'FAILED'} ({detail})")
        if not ok:
            send_email("[Job Agent] Reactivate WhatsApp alerts",
                       "Your WhatsApp job alerts window looks closed. Open WhatsApp and text the "
                       "sandbox 'join <code>' (or just reply to the bot) to resume instant alerts. "
                       "Meanwhile, new matches keep arriving by email.")
        return

    try:
        jobs = json.load(open(args.scored, encoding="utf-8"))
    except Exception as e:
        sys.exit(f"ERROR: could not read {args.scored}: {e}")
    jobs = sorted([j for j in jobs if j.get("score", 0) >= args.min_score],
                  key=lambda j: j.get("score", 0), reverse=True)[:args.top]

    seen = load_seen(args.seen_file)
    already_applied = load_seen(args.success_file)
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
        # 2) WhatsApp ping (best-effort)
        wa_ok, detail = send_whatsapp(f"🧑‍💻 {title} @ {company} (fit {j.get('score')})\n"
                                      f"Apply: {link}\n(resume in your email)")
        if not wa_ok and emailed:
            print(f"  [whatsapp] not delivered ({detail}) — job is in your email.", file=sys.stderr)
        if emailed or wa_ok:
            seen.add(jid)
            sent += 1
            print(f"  sent: [{j.get('score')}] {title[:40]} — {company[:22]}  "
                  f"(email={'y' if emailed else 'n'}, whatsapp={'y' if wa_ok else 'n'})")

    save_seen(args.seen_file, seen)
    print(f"\nNotified {sent} new job(s).")


if __name__ == "__main__":
    main()
