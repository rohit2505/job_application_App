#!/usr/bin/env python3
"""
auto_apply.py — stage 5: actually submit applications, end to end, with zero
manual intervention on the happy path.

For each new top-scored job:
  1. Resolve the real destination behind the aggregator link (Adzuna etc. wrap
     the true employer/ATS URL behind a couple of redirects).
  2. Only proceed if it lands on a Greenhouse-hosted application form — the
     only ATS this supports so far. Anything else is left to apply_notifier.py
     (the existing "email me the link + resume" flow) — no attempt is made.
  3. If the form has a CAPTCHA, STOP and fall back to the email flow. This is
     a hard rule, not a per-job judgment call: this script will never attempt
     to solve or bypass a CAPTCHA, for any job, ever.
  4. Fill the standard fields (name, email, phone, location, LinkedIn,
     website, "how did you hear") from profile.json, and upload the tailored
     resume + cover letter for that job.
  5. For posting-specific screening questions, ask Claude to answer using
     ONLY facts already in the resume/profile. If Claude can't answer
     honestly, this pauses and asks YOU on Telegram, and waits (bounded) for
     your reply. If nothing arrives in time, it backs off and leaves that job
     to the manual email flow rather than guessing or submitting incomplete.
  6. Submits, screenshots the confirmation, and emails you that screenshot +
     the exact resume/cover letter used + a full log of every answer it gave,
     tagged [JobAgent], so you have a complete record with nothing to dig for.

Requires: playwright (+ `playwright install chromium` once, done in CI),
the same keys.json as the other agents, plus optionally TELEGRAM_BOT_TOKEN /
TELEGRAM_CHAT_ID for the escalation step (without them, an unanswerable
question just falls back immediately,
no Telegram wait).
"""

import argparse
import base64
import json
import os
import re
import ssl
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

try:
    from playwright.sync_api import Error as PWError, TimeoutError as PWTimeoutError
except Exception:
    # Lets this module be imported (e.g. by tests exercising resolve_real_url
    # with fake page/context doubles) on a machine without playwright
    # installed. Real Playwright errors, when the library is present, are
    # actual subclasses of Exception, so this fallback never masks them.
    class PWError(Exception):
        pass

    class PWTimeoutError(PWError):
        pass

# Only these outcomes are genuinely resolved — a real submission, a
# confirmed non-Greenhouse ATS, or a CAPTCHA gate — and safe to mark
# permanently in state/applied.json. "redirect_failed", "error", and
# "unanswered" are all transient/retry-eligible and must never be added
# there, or a bad run (or a temporary Adzuna hiccup) would silently drop a
# job forever instead of trying again on the next scheduled run.
PERMANENT_STATUSES = {"applied", "not_greenhouse", "captcha", "stale_listing", "escalated_remote"}

TIMEOUT = 30
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
QA_MODEL = "claude-sonnet-4-5-20250929"
WHATSAPP_WAIT_SECONDS = int(os.environ.get("WHATSAPP_WAIT_SECONDS", 600))  # 10 min default
WHATSAPP_POLL_SECONDS = 15


# --------------------------------------------------------------------------- #
# config discovery (same pattern as the sibling agents)
# --------------------------------------------------------------------------- #
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


# --------------------------------------------------------------------------- #
# Telegram escalation — send the question, poll for your reply. Free, no
# message limits, no expiring sandbox (unlike the Twilio WhatsApp sandbox).
# --------------------------------------------------------------------------- #
def send_whatsapp(body):
    """Name kept for call-site compatibility; sends via Telegram now.
    Returns (ok, detail, message_id) — message_id lets a caller track a
    specific outgoing message (e.g. to match a later reply to it)."""
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
        print(f"  [telegram] send error: {e}", file=sys.stderr)
        return False, f"send error: {e}", None


def wait_for_whatsapp_reply(after_ts, timeout_s=WHATSAPP_WAIT_SECONDS):
    """Poll Telegram getUpdates for a message from your chat, sent after
    after_ts. Returns the text, or None if nothing arrives in time / creds
    aren't set."""
    token, chat_id = cfg("TELEGRAM_BOT_TOKEN"), cfg("TELEGRAM_CHAT_ID")
    if not (token and chat_id):
        return None
    url = f"https://api.telegram.org/bot{token}/getUpdates?timeout=0&limit=20"
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        time.sleep(WHATSAPP_POLL_SECONDS)
        req = urllib.request.Request(url)
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT, context=SSL_CTX) as r:
                data = json.loads(r.read().decode())
        except Exception as e:
            print(f"  [telegram] poll error: {e}", file=sys.stderr)
            continue
        for upd in data.get("result", []):
            msg = upd.get("message", {})
            chat = msg.get("chat", {})
            if str(chat.get("id")) != str(chat_id):
                continue
            if msg.get("date", 0) > after_ts and msg.get("text", "").strip():
                return msg["text"].strip()
    return None


# --------------------------------------------------------------------------- #
# email (reuses the same SMTP setup as apply_notifier.py)
# --------------------------------------------------------------------------- #
def send_email(subject, text_body, attach_paths=None):
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText
    from email.mime.application import MIMEApplication
    import smtplib
    addr, pw = cfg("GMAIL_ADDRESS"), cfg("GMAIL_APP_PASSWORD")
    to = cfg("DIGEST_TO") or addr
    if not (addr and pw):
        print("  [email] skipped — set GMAIL_ADDRESS / GMAIL_APP_PASSWORD", file=sys.stderr)
        return False
    if not subject.startswith("[JobAgent]"):
        subject = f"[JobAgent] {subject}"
    msg = MIMEMultipart()
    msg["Subject"], msg["From"], msg["To"] = subject, addr, to
    msg.attach(MIMEText(text_body, "plain", "utf-8"))
    for p in (attach_paths or []):
        if p and os.path.exists(p):
            with open(p, "rb") as f:
                part = MIMEApplication(f.read(), Name=os.path.basename(p))
            part["Content-Disposition"] = f'attachment; filename="{os.path.basename(p)}"'
            msg.attach(part)
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=SSL_CTX, timeout=TIMEOUT) as s:
            s.login(addr, pw)
            s.sendmail(addr, [to], msg.as_string())
        return True
    except Exception as e:
        print(f"  [email] error: {e}", file=sys.stderr)
        return False


# Set by main() while its own `with sync_playwright() as p:` session is
# open, so escalate_to_remote_browser() below can reuse that same instance
# instead of opening a second, nested sync_playwright() context in the same
# thread -- Playwright's sync API does not support that and raises "It
# looks like you are using Playwright Sync API inside the asyncio loop"
# (hit live in CI, 2026-09). None when called standalone (e.g. the manual
# test script), in which case escalate_to_remote_browser() opens its own.
_ACTIVE_PLAYWRIGHT = None


# --------------------------------------------------------------------------- #
# Remote-browser escalation — when a CAPTCHA blocks an otherwise-fillable
# Greenhouse/Lever form, hand the (already-filled) form off to a browser
# running on our own VPS instead of just giving up. The VPS's Chromium is
# reached over an SSH tunnel already opened as a separate CI step (its CDP
# debug port is bound to 127.0.0.1 on the VPS and never exposed publicly).
# We fill everything here, exactly like the normal flow, but never submit
# — you finish (solve the captcha, click submit) yourself from the noVNC
# link this sends to Telegram, from any device including your phone.
#
# This never bypasses or solves the CAPTCHA itself — it only saves you the
# hassle of re-filling the form and re-attaching files on a small screen.
#
# Disabled (falls straight back to the old "captcha" status) unless
# VPS_HOST is configured — so this is fully opt-in and never breaks the
# existing flow if the VPS isn't set up.
# --------------------------------------------------------------------------- #
def _scp_to_vps(local_path, remote_dir, ssh_key, ssh_user, vps_host, port):
    import subprocess
    if not (local_path and os.path.exists(local_path)):
        return None
    remote_name = os.path.basename(local_path)
    remote_path = f"{remote_dir}/{remote_name}"
    cmd = [
        "scp", "-i", ssh_key, "-P", str(port),
        "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=15",
        local_path, f"{ssh_user}@{vps_host}:{remote_path}",
    ]
    try:
        subprocess.run(cmd, check=True, timeout=60, capture_output=True)
        return remote_path
    except Exception as e:
        print(f"  [vps-escalate] scp failed for {local_path}: {e}", file=sys.stderr)
        return None


def _ssh_rm_on_vps(remote_paths, ssh_key, ssh_user, vps_host, port):
    import subprocess
    remote_paths = [p for p in remote_paths if p]
    if not remote_paths:
        return
    cmd = [
        "ssh", "-i", ssh_key, "-p", str(port),
        "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=15",
        f"{ssh_user}@{vps_host}", "rm", "-f", *remote_paths,
    ]
    try:
        subprocess.run(cmd, timeout=30, capture_output=True)
    except Exception as e:
        print(f"  [vps-escalate] cleanup ssh rm failed (non-fatal): {e}", file=sys.stderr)


def escalate_to_remote_browser(url, job, profile, resume_text, resume_path, cover_path, log):
    """Fills the same Greenhouse/Lever form on the VPS's browser instead of
    giving up on CAPTCHA. Returns True if the hand-off succeeded (form
    filled, you were notified) — caller should record status
    'escalated_remote' in that case. Returns False if anything about the
    remote hand-off itself failed, so the caller falls back to the plain
    'captcha' status as before."""
    vps_host = cfg("VPS_HOST")
    if not vps_host:
        return False  # feature not configured — behave exactly as before

    ssh_key = cfg("VPS_SSH_KEY_PATH") or os.path.expanduser("~/.ssh/vps_key")
    ssh_user = cfg("VPS_SSH_USER") or "ubuntu"
    ssh_port = int(cfg("VPS_SSH_PORT") or 22)
    cdp_port = int(cfg("VPS_CDP_LOCAL_PORT") or 9222)  # local end of the already-open tunnel
    remote_dir = cfg("VPS_REMOTE_UPLOAD_DIR") or "/home/ubuntu/uploads"
    novnc_url = cfg("VPS_NOVNC_URL") or f"http://{vps_host}:6080/vnc.html"
    # resize=scale: noVNC scales the whole remote desktop down to fit
    # whatever screen opens the link, so a phone doesn't need horizontal/
    # vertical panning just to see the page — pinch-zoom still works for
    # precise taps.
    novnc_url += ("&" if "?" in novnc_url else "?") + "resize=scale"
    novnc_password = cfg("VPS_NOVNC_PASSWORD") or ""

    title, company = job.get("title", ""), job.get("company", "")

    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:
        print(f"  [vps-escalate] playwright unavailable: {e}", file=sys.stderr)
        return False

    # Make sure the upload dir exists on the VPS, then copy the resume/
    # cover letter over — the remote Chromium needs these files local to
    # its own machine to attach them, not just reachable from the CI runner.
    import subprocess
    try:
        subprocess.run(
            ["ssh", "-i", ssh_key, "-p", str(ssh_port), "-o", "StrictHostKeyChecking=no",
             "-o", "ConnectTimeout=15", f"{ssh_user}@{vps_host}", "mkdir", "-p", remote_dir],
            timeout=20, check=True, capture_output=True,
        )
    except Exception as e:
        print(f"  [vps-escalate] could not prep remote upload dir: {e}", file=sys.stderr)
        return False

    remote_resume = _scp_to_vps(resume_path, remote_dir, ssh_key, ssh_user, vps_host, ssh_port)
    remote_cover = _scp_to_vps(cover_path, remote_dir, ssh_key, ssh_user, vps_host, ssh_port)
    uploaded_remote_paths = [p for p in (remote_resume, remote_cover) if p]

    def _fill_on_playwright(p):
        browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{cdp_port}")
        context = browser.contexts[0] if browser.contexts else browser.new_context()
        rpage = context.new_page()
        rpage.goto(url, timeout=45000)

        filled = False
        filled_frame = None
        gframe = find_greenhouse_frame_with_retry(rpage)
        if gframe:
            fill_greenhouse_form(gframe, job, profile, resume_text,
                                  remote_resume or resume_path,
                                  remote_cover or cover_path, log)
            filled = True
            filled_frame = gframe
        else:
            lframe = find_lever_form(rpage)
            if lframe:
                fill_lever_form(lframe, job, profile, resume_text,
                                 remote_resume or resume_path,
                                 remote_cover or cover_path, log)
                filled = True
                filled_frame = lframe
        # Deliberately no submit here — a human finishes this from
        # the noVNC link below (captcha + final click are theirs).

        # Scroll straight to the captcha (or, failing that, the bottom
        # of the form) so opening the noVNC link on a phone lands right
        # on the thing you need to act on, instead of a long form you'd
        # otherwise have to hunt/scroll through on a small screen.
        if filled_frame is not None:
            try:
                captcha_el = filled_frame.locator(
                    "iframe[src*='recaptcha'], .g-recaptcha, [name='g-recaptcha-response'], "
                    "iframe[src*='hcaptcha'], .h-captcha, [name='h-captcha-response']"
                ).first
                if captcha_el.count():
                    captcha_el.scroll_into_view_if_needed(timeout=5000)
                else:
                    rpage.keyboard.press("End")
            except Exception as e:
                print(f"  [vps-escalate] scroll-to-captcha failed (non-fatal): {e}", file=sys.stderr)
        return filled

    filled_ok = False
    try:
        if _ACTIVE_PLAYWRIGHT is not None:
            # Reuse the caller's already-open Playwright session (see the
            # _ACTIVE_PLAYWRIGHT comment above) instead of nesting a second
            # sync_playwright() context in the same thread.
            filled_ok = _fill_on_playwright(_ACTIVE_PLAYWRIGHT)
        else:
            with sync_playwright() as p:
                filled_ok = _fill_on_playwright(p)
    except Exception as e:
        print(f"  [vps-escalate] remote fill failed: {e}", file=sys.stderr)
        log.append({"vps_escalate_error": str(e)})
    finally:
        # Best-effort cleanup regardless of outcome — never leave the
        # resume/cover letter sitting on the VPS.
        _ssh_rm_on_vps(uploaded_remote_paths, ssh_key, ssh_user, vps_host, ssh_port)

    if not filled_ok:
        return False

    pw_note = f"\nVNC password: {novnc_password}" if novnc_password else ""
    send_whatsapp(
        f"🖥️ Filled but hit a CAPTCHA — {title} @ {company}\n"
        f"Finish it here (solve the captcha, hit submit):\n{novnc_url}{pw_note}"
    )
    send_email(
        f"[Action needed] {title} @ {company}",
        f"Form filled but blocked by a CAPTCHA. Finish it yourself here:\n{novnc_url}\n\n"
        f"Job link: {url}\n\n"
        f"Resume and cover letter attached for your records.",
        [resume_path, cover_path],
    )
    return True



# --------------------------------------------------------------------------- #
# Claude — answer a screening question ONLY from real resume/profile facts
# --------------------------------------------------------------------------- #
def answer_question(question, options, resume_text, profile):
    key = cfg("ANTHROPIC_API_KEY")
    if not key:
        return None
    opts_txt = f"\nOptions: {options}" if options else ""
    prompt = (
        "You are filling in ONE job application field for this candidate. "
        "Answer using ONLY facts explicitly present in the resume or profile below. "
        "Never invent numbers, years, or skills not shown. If the field is a checkbox/"
        "multi-select of options, reply with the exact matching option string(s), "
        "comma-separated. If you cannot answer truthfully and specifically from the "
        "given facts, reply with exactly: UNKNOWN\n\n"
        f"QUESTION: {question}{opts_txt}\n\n"
        f"RESUME:\n{resume_text[:6000]}\n\n"
        f"PROFILE:\n{json.dumps(profile, indent=0)}\n\n"
        "Reply with ONLY the answer text (or UNKNOWN). No explanation."
    )
    body = json.dumps({
        "model": QA_MODEL, "max_tokens": 200,
        "messages": [{"role": "user", "content": prompt}],
    }).encode("utf-8")
    req = urllib.request.Request(ANTHROPIC_URL, data=body, headers={
        "x-api-key": key, "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=60, context=SSL_CTX) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        text = "".join(b.get("text", "") for b in data.get("content", [])).strip()
    except Exception as e:
        print(f"  [claude] error: {e}", file=sys.stderr)
        return None
    return None if text.upper() == "UNKNOWN" or not text else text


def ai_polish_answer(question, raw_notes, resume_text, profile):
    """Used when you reply on Telegram with 'ai: <rough notes>' instead of a
    finished answer — turns your notes into a clear, first-person, properly
    worded application answer, filling in supporting detail from the resume/
    profile where relevant. Unlike answer_question(), this is allowed to use
    whatever you typed in raw_notes even if it's not already in the resume
    (it's YOUR answer, just under-written) — it should not invent facts
    beyond what you or the resume/profile actually say, but it can and
    should turn a few rough words into a proper sentence or two."""
    key = cfg("ANTHROPIC_API_KEY")
    if not key:
        return None
    prompt = (
        "You are helping a candidate answer ONE job application screening "
        "question. They gave you rough notes to work from — turn those "
        "notes into a clear, professional, first-person answer (a few "
        "sentences, no bullet points, no preamble like 'Sure, here's...'). "
        "Use their notes as the primary source of truth; you may pull in "
        "supporting detail from the resume/profile below if it's directly "
        "relevant, but do not invent anything beyond what the notes, "
        "resume, or profile actually say.\n\n"
        f"QUESTION: {question}\n\n"
        f"CANDIDATE'S NOTES: {raw_notes}\n\n"
        f"RESUME:\n{resume_text[:4000]}\n\n"
        f"PROFILE:\n{json.dumps(profile, indent=0)}\n\n"
        "Reply with ONLY the final answer text, nothing else."
    )
    body = json.dumps({
        "model": QA_MODEL, "max_tokens": 300,
        "messages": [{"role": "user", "content": prompt}],
    }).encode("utf-8")
    req = urllib.request.Request(ANTHROPIC_URL, data=body, headers={
        "x-api-key": key, "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=60, context=SSL_CTX) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        text = "".join(b.get("text", "") for b in data.get("content", [])).strip()
    except Exception as e:
        print(f"  [claude] ai_polish_answer error: {e}", file=sys.stderr)
        return None
    return text or None


# Telegram reply phrases that ask the AI to just answer for you, straight
# from your resume/profile (no extra notes given) — same call as the
# original auto-attempt (answer_question), so it's only useful if that
# first attempt was wrong to give up, or you want it to try again.
_AI_ANSWER_PHRASES = {"ai", "ai answer", "ai answer it", "you answer",
                       "you answer it", "answer it", "let ai answer"}


def resolve_telegram_reply(reply, question, resume_text, profile):
    """Reply text as typed on Telegram may itself be the final answer, or it
    may be a request to have the AI answer/polish it:
      - bare phrases like "ai" / "answer it" -> re-run answer_question()
        straight from resume/profile (no new info from you).
      - "ai: <notes>" or "ai <notes>" -> ai_polish_answer() turns your rough
        notes into a proper answer.
      - anything else -> used exactly as typed, unchanged (current behavior).
    Returns (final_answer, source_tag). Falls back to the raw reply (or the
    raw notes, for the 'ai: <notes>' form) if the AI call fails for any
    reason, so a bad/missing ANTHROPIC_API_KEY never blocks the reply."""
    stripped = reply.strip()
    lower = stripped.lower()
    if lower in _AI_ANSWER_PHRASES:
        ai_ans = answer_question(question, None, resume_text, profile)
        if ai_ans:
            return ai_ans, "telegram+ai"
        return stripped, "telegram"  # AI couldn't confidently answer either — use literally what you typed
    if lower.startswith("ai:") or lower.startswith("ai "):
        raw_notes = stripped[3:].lstrip(": ").strip()
        if raw_notes:
            ai_ans = ai_polish_answer(question, raw_notes, resume_text, profile)
            if ai_ans:
                return ai_ans, "telegram+ai"
            return raw_notes, "telegram"
    return stripped, "telegram"


# --------------------------------------------------------------------------- #
# Greenhouse form handling (Playwright)
# --------------------------------------------------------------------------- #
STANDARD_FIELD_MAP = {
    "first_name": "first_name",
    "last_name": "last_name",
    "email": "email",
    "phone": "phone",
    "candidate-location": None,  # filled from location_city/location_state below
    "country": "country",
}

LABEL_KEYWORDS = {
    "linkedin": "linkedin",
    "website": "portfolio_url",
    "portfolio": "portfolio_url",
    "github": "github",
    "how did you hear": "how_did_you_hear",
}


ADZUNA_HOST_SUFFIX = "adzuna.com"

# Text patterns tried in order when looking for the "take me to the job"
# style dismissal on Adzuna's various interstitial widgets/modals. Not an
# exact-text dependency — any of these (case-insensitive substring) is
# accepted, so small wording changes on Adzuna's side don't silently break
# the whole flow.
_SKIP_WIDGET_PATTERNS = (
    "no thanks, take me to the job",
    "no thanks",
    "no, thanks",
    "skip",
    "continue to the job",
    "continue to job",
    "take me to the job",
)

# "Apply" trigger patterns, same reasoning — several ways employers/Adzuna
# phrase the CTA that starts the redirect chain.
_APPLY_PATTERNS = (
    "apply for this job", "apply now", "apply on company site", "apply", "continue",
)


def _hostname(url):
    try:
        return (urllib.parse.urlparse(url).hostname or "").lower()
    except Exception:
        return ""


def _is_adzuna_url(url):
    host = _hostname(url)
    return host == ADZUNA_HOST_SUFFIX or host.endswith("." + ADZUNA_HOST_SUFFIX)


def _dismiss_common_widgets(pg, diag):
    """Best-effort dismissal of cookie-consent banners and Adzuna's inline
    'receive similar jobs by email' widgets. Every attempt is recorded in
    `diag['actions']` (success or failure) — nothing here is silently
    swallowed, even though a missing widget on a given page is expected and
    not itself an error."""
    # generic cookie/consent banners
    for txt in ("decline all", "reject all", "accept all", "accept cookies", "i agree"):
        try:
            btn = pg.get_by_text(txt, exact=False).first
            if btn.is_visible(timeout=800):
                btn.click(timeout=800)
                diag["actions"].append(f"dismissed consent banner: '{txt}'")
                pg.wait_for_timeout(200)
                break
        except Exception:
            pass  # expected: most pages don't show this banner

    for txt in _SKIP_WIDGET_PATTERNS:
        try:
            btn = pg.get_by_text(txt, exact=False).first
            if btn.is_visible(timeout=800):
                btn.click(timeout=800)
                diag["actions"].append(f"dismissed interstitial widget: '{txt}'")
                pg.wait_for_timeout(300)
                return True
        except Exception:
            pass  # expected: not every page shows this widget
    return False


def _click_apply_trigger(pg, context, diag):
    """Click whatever looks like the Apply CTA (link or button), handling
    both same-tab navigation and a new popup tab. Returns the page to keep
    using from here on (may be a new popup page, or the same `pg`)."""
    for txt in _APPLY_PATTERNS:
        for role in ("link", "button"):
            try:
                el = pg.get_by_role(role, name=re.compile(txt, re.I)).first
                if not el.is_visible(timeout=800):
                    continue
            except Exception:
                continue
            try:
                with context.expect_page(timeout=3500) as new_page_info:
                    el.click(timeout=4000)
                target = new_page_info.value
                target.wait_for_load_state("domcontentloaded", timeout=15000)
                diag["actions"].append(f"clicked '{txt}' ({role}) -> opened new tab")
                return target
            except PWTimeoutError:
                # no popup opened — the click (if it landed) navigated the
                # same tab, or did nothing. Either way this isn't an error;
                # record it and move on to settle-polling below.
                diag["actions"].append(f"clicked '{txt}' ({role}) -> same tab (no popup)")
                return pg
            except Exception as e:
                diag["actions"].append(f"click '{txt}' ({role}) failed: {e}")
                continue
    diag["actions"].append("no apply-style CTA found to click")
    return pg


# Adzuna serves a standard no-JS <meta http-equiv="refresh"> fallback tag on
# every /land/ad/... response — always present, identical for every client,
# server-rendered (not gated behind their client-side automation-fingerprint
# check, which only controls whether their JS auto-redirect *timer* fires in
# a live browser). Reading this tag is just following an ordinary HTTP
# redirect, the same as any HTTP client does — not an attempt to evade or
# spoof anything.
_META_REFRESH_RE = re.compile(
    r"""<meta[^>]+http-equiv=['"]refresh['"][^>]*content=['"]\d+;\s*url=([^'"]+)['"]""",
    re.IGNORECASE)


def _fetch_meta_refresh_target(url, timeout_s=15):
    """Plain HTTP GET (no browser, no Playwright) to an Adzuna /land/ad/...
    URL, looking for the static meta-refresh destination.

    Returns (target_url_or_None, reason). `reason` is always a short,
    specific diagnostic string — e.g. "http 403", "urlopen error: <msg>",
    "html fetched (N bytes) but no meta-refresh tag found", "ok" — so a
    failure here is never a silent, unexplained "not found/usable". Callers
    fall back to the click-flow when target is None (e.g. Adzuna's
    account-login-walled 'Easy Apply' postings genuinely have no external
    destination and no meta-refresh tag at all — that's an expected,
    legitimate "no tag found" case, not a bug)."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "job-search-agent/1.0"})
        with urllib.request.urlopen(req, timeout=timeout_s, context=SSL_CTX) as resp:
            status = resp.status
            html = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return None, f"http {e.code} {e.reason}"
    except Exception as e:
        return None, f"request error: {type(e).__name__}: {e}"
    m = _META_REFRESH_RE.search(html)
    if not m:
        return None, f"http {status}, html fetched ({len(html)} bytes) but no meta-refresh tag found"
    return m.group(1), "ok"


def resolve_real_url(page, apply_url, timeout_s=30):
    """Follow aggregator redirects until we land somewhere real, or give up
    with a clear, diagnosable failure.

    Returns (status, final_url, page_to_use_from_here_on, diag) where status
    is one of:
      "direct"          — apply_url was never an Adzuna URL; used as-is.
      "resolved"        — left Adzuna's domain successfully.
      "redirect_failed" — still on an Adzuna hostname after exhausting every
                           attempt; caller should NOT treat this as a
                           confirmed non-Greenhouse ATS, and should NOT mark
                           the job permanently seen — it's eligible for retry.
    `diag` always carries: url, title, frame_urls, actions (list of every
    attempted action, in order — including ones that did nothing), and
    optionally `screenshot` (set by the caller after a failure, since only
    the caller knows the job-specific filename to use).

    Adzuna's "Apply for this job" link is not a plain href to the employer:
    clicking it reveals an inline "Receive similar jobs by email" widget,
    and only dismissing that ("No thanks, take me to the job") fires the
    real client-side redirect through /land/ad/... to the employer site.
    Confirmed live: page.goto() straight to the extracted href stalls
    indefinitely on /land/ad/..., while replicating the click flow reaches
    the employer site within a few seconds.
    """
    diag = {"url": apply_url, "title": "", "frame_urls": [], "actions": []}

    if not _is_adzuna_url(apply_url):
        # Prefer the source's own direct URL when it's already a real
        # employer/ATS link (this is always true for greenhouse/lever/ashby
        # sourced jobs) — nothing to resolve.
        try:
            page.goto(apply_url, wait_until="domcontentloaded", timeout=timeout_s * 1000)
        except PWError as e:
            diag["actions"].append(f"goto(direct url) failed: {e}")
            return "redirect_failed", page.url or apply_url, page, diag
        diag["url"], diag["title"] = page.url, _safe_title(page)
        diag["frame_urls"] = [f.url for f in page.frames]
        return "direct", page.url, page, diag

    # Try the cheap, reliable path first: a plain HTTP GET for the static
    # meta-refresh fallback, no browser/click-flow needed at all.
    meta_target, meta_reason = _fetch_meta_refresh_target(apply_url)
    if meta_target and not _is_adzuna_url(meta_target):
        diag["actions"].append(f"resolved via static meta-refresh (no browser needed) -> {meta_target}")
        try:
            page.goto(meta_target, wait_until="domcontentloaded", timeout=timeout_s * 1000)
        except PWError as e:
            diag["actions"].append(f"goto(meta-refresh target) failed: {e}")
            return "redirect_failed", page.url or meta_target, page, diag
        diag["url"], diag["title"] = page.url, _safe_title(page)
        try:
            diag["frame_urls"] = [f.url for f in page.frames]
        except Exception:
            diag["frame_urls"] = []
        return "resolved", page.url, page, diag

    diag["actions"].append(f"static meta-refresh unavailable ({meta_reason}) — falling back to click-flow")

    try:
        page.goto(apply_url, wait_until="domcontentloaded", timeout=timeout_s * 1000)
    except PWError as e:
        diag["actions"].append(f"goto(adzuna url) failed: {e}")
        return "redirect_failed", page.url or apply_url, page, diag

    _dismiss_common_widgets(page, diag)

    context = page.context
    target = _click_apply_trigger(page, context, diag)

    # clicking Apply reveals the inline widget on whichever page/tab we're
    # now on — dismiss it, that's the actual redirect trigger
    _dismiss_common_widgets(target, diag)

    # settle through any further client-side (JS) redirect hop(s): poll
    # explicitly until the hostname is no longer Adzuna's, not merely until
    # the URL stops changing (an unchanged Adzuna URL is never "resolved").
    deadline = time.time() + timeout_s
    last_url = target.url
    while time.time() < deadline:
        if not _is_adzuna_url(target.url):
            break
        target.wait_for_timeout(1200)
        try:
            target.wait_for_load_state("networkidle", timeout=4000)
        except PWError:
            pass  # fine — some pages keep background connections open
        if target.url != last_url:
            diag["actions"].append(f"url changed: {last_url} -> {target.url}")
            last_url = target.url

    diag["url"], diag["title"] = target.url, _safe_title(target)
    try:
        diag["frame_urls"] = [f.url for f in target.frames]
    except Exception:
        diag["frame_urls"] = []

    if _is_adzuna_url(target.url):
        return "redirect_failed", target.url, target, diag
    return "resolved", target.url, target, diag


def _safe_title(pg):
    try:
        return pg.title()
    except Exception:
        return ""


# Titles used by Cloudflare (and similar) bot-challenge interstitials. Seen in
# practice on some Himalayas company job pages. We never attempt to solve or
# evade these (same hard rule as CAPTCHAs) — we just make sure a page that
# never got past the challenge is diagnosed as "blocked" (retry-eligible),
# never silently misreported as a confirmed "not_greenhouse" result, since we
# never actually saw the real page.
_CHALLENGE_TITLE_PATTERNS = (
    "just a moment", "attention required", "checking your browser",
    "verify you are human", "verifying you are human", "are you a robot",
)


def _is_challenge_page(page):
    title = (_safe_title(page) or "").lower()
    return any(p in title for p in _CHALLENGE_TITLE_PATTERNS)


def _close_extra_page(original_page, ended_up_on):
    """If resolve_real_url moved us onto a popup/new tab, close whichever
    page we're NOT continuing to use, so tabs don't pile up across jobs."""
    if ended_up_on is not original_page:
        try:
            original_page.close()
        except Exception:
            pass


def find_greenhouse_frame(page):
    for frame in page.frames:
        if "greenhouse.io" in frame.url:
            return frame
    # some postings link straight to job-boards.greenhouse.io, no iframe
    if "greenhouse.io" in page.url:
        return page.main_frame
    return None


def find_greenhouse_frame_with_retry(page, attempts=4, wait_ms=1500):
    """Some companies white-label their Greenhouse board under their own
    domain (e.g. jumptrading.com/hr/job?gh_jid=... embedding a
    job-boards.greenhouse.io/embed/job_app iframe) rather than linking
    straight to greenhouse.io — and that iframe can load a beat AFTER the
    page itself, sometimes visibly racing a cookie-consent banner (Osano
    etc.) for load time. Confirmed live (2026-09, Jump Trading): a single
    immediate check of page.frames missed the iframe entirely and this got
    misreported as 'not_greenhouse', even though it's a real, fillable
    Greenhouse form one JS tick later. Poll a few times with a short wait
    before giving up, same pattern as the captcha lazy-load fix."""
    for i in range(attempts):
        frame = find_greenhouse_frame(page)
        if frame:
            return frame
        if i < attempts - 1:
            try:
                page.wait_for_timeout(wait_ms)
            except Exception:
                break
    return None


def has_captcha(frame):
    """Detects both reCAPTCHA (Greenhouse) and hCaptcha (seen on some Lever
    boards) — same hard rule either way: if present, stop and fall back to
    the manual/email flow. Never attempt to solve or bypass either, ever."""
    try:
        return frame.locator(
            "iframe[src*='recaptcha'], .g-recaptcha, [name='g-recaptcha-response'], "
            "iframe[src*='hcaptcha'], .h-captcha, [name='h-captcha-response']"
        ).count() > 0
    except Exception:
        return False


LEVER_HOST_SUFFIX = "lever.co"


def _is_lever_url(url):
    host = _hostname(url)
    return host == LEVER_HOST_SUFFIX or host.endswith("." + LEVER_HOST_SUFFIX)


def find_lever_form(page):
    """Lever's application form is the page itself (not an iframe, unlike
    Greenhouse) — confirmed live against a real Lever board. Detected by
    hostname plus the presence of the standard resume file input.

    IMPORTANT: a lever.co posting resolves to the job DESCRIPTION page
    (jobs.lever.co/<company>/<id>), which never has the form on it — the
    real application form lives at a separate .../apply URL. Confirmed live
    (2026-09) after this silently misreported every un-navigated Lever
    posting as 'not_greenhouse' (not a real ATS-support gap, just never
    reaching the form). So: check the current page first, and if it's a
    lever.co URL without the form, navigate to '<url>/apply' (unless we're
    already there) before giving up."""
    try:
        if not _is_lever_url(page.url):
            return None
        if page.locator("input[name='resume']").count() > 0:
            return page.main_frame
        base_url = page.url.rstrip("/")
        if base_url.endswith("/apply"):
            return None  # already tried the apply page and the form isn't there
        page.goto(base_url + "/apply", wait_until="domcontentloaded", timeout=15000)
        if page.locator("input[name='resume']").count() > 0:
            return page.main_frame
    except Exception:
        pass
    return None


def fill_greenhouse_form(frame, job, profile, resume_text, resume_path, cover_path, log):
    """Fills Greenhouse's standalone job-boards.greenhouse.io application
    form. IMPORTANT: as of 2026-09, this UI's inputs carry the field's
    identity in `id` (first_name, last_name, email, phone, country,
    candidate-location, resume, cover_letter, question_<num>) with `name`
    left EMPTY — confirmed via live DOM inspection after a real submission
    went out completely blank (name-only selectors matched zero elements,
    silently, with no error). Selectors below try id first, name second,
    so this also still works if a posting is on the older name-based
    embed. See _finish_application for the fill-verification gate that
    now refuses to submit unless this actually took."""
    def try_fill(field_id, name_attr, value):
        if not value:
            return False
        for selector in (f"#{field_id}", f"input[name='{name_attr}']"):
            try:
                el = frame.locator(selector).first
                if el.count():
                    el.fill(str(value))
                    return True
            except Exception:
                continue
        return False

    try_fill("first_name", "first_name", profile.get("first_name"))
    try_fill("last_name", "last_name", profile.get("last_name"))
    try_fill("email", "email", profile.get("email"))
    try_fill("phone", "phone", profile.get("phone"))
    try_fill("country", "country", profile.get("country"))
    loc = ", ".join(x for x in (profile.get("location_city"), profile.get("location_state")) if x)
    try_fill("candidate-location", "candidate-location", loc)

    # Resume / cover letter uploads
    for field_id, path in (("resume", resume_path), ("cover_letter", cover_path)):
        if not path or not os.path.exists(path):
            continue
        for selector in (f"#{field_id}", f"input[name='{field_id}']"):
            try:
                el = frame.locator(selector).first
                if el.count():
                    el.set_input_files(path)
                    break
            except Exception:
                continue

    # Label-keyword-matched text fields (LinkedIn, website, how-did-you-hear, etc.)
    # — Greenhouse's custom questions are input#question_<num> with empty
    # name, so match on id, falling back to name for the older UI.
    try:
        inputs = frame.locator("input[type='text']")
        n = inputs.count()
        for i in range(n):
            el = inputs.nth(i)
            el_id = el.get_attribute("id") or ""
            name = el.get_attribute("name") or ""
            identity = el_id or name
            if identity in ("first_name", "last_name", "email", "phone", "country",
                            "candidate-location") or not identity.startswith("question_"):
                continue
            label_text = ""
            try:
                if el_id:
                    lbl = frame.locator(f"label[for='{el_id}']").first
                    if lbl.count():
                        label_text = (lbl.inner_text() or "").strip()
            except Exception:
                pass
            matched = None
            low = label_text.lower()
            for kw, pkey in LABEL_KEYWORDS.items():
                if kw in low:
                    matched = profile.get(pkey)
                    break
            if matched:
                el.fill(str(matched))
                log.append({"question": label_text, "answer": str(matched), "source": "profile"})
                continue
            if label_text:
                ans = answer_question(label_text, None, resume_text, profile)
                if ans:
                    el.fill(ans)
                    log.append({"question": label_text, "answer": ans, "source": "claude"})
                else:
                    log.append({"question": label_text, "answer": None, "source": "unanswered"})
    except Exception as e:
        print(f"  [fill] text-question pass error: {e}", file=sys.stderr)

    # Checkbox-group questions: answer as a group via Claude, then check matches
    try:
        checkboxes = frame.locator("input[type='checkbox']")
        n = checkboxes.count()
        groups = {}
        for i in range(n):
            el = checkboxes.nth(i)
            key = el.get_attribute("name") or el.get_attribute("id") or ""
            if not key:
                continue
            groups.setdefault(key, []).append(el)
        for key, els in groups.items():
            options = []
            for el in els:
                try:
                    lbl_id = el.get_attribute("id")
                    lbl = frame.locator(f"label[for='{lbl_id}']").first if lbl_id else None
                    options.append((lbl.inner_text().strip() if lbl and lbl.count() else "", el))
                except Exception:
                    options.append(("", el))
            fieldset_label = ""
            try:
                fs = els[0].locator("xpath=ancestor::fieldset[1]//legend").first
                if fs.count():
                    fieldset_label = fs.inner_text().strip()
            except Exception:
                pass
            question = fieldset_label or key
            opt_names = [o for o, _ in options if o]
            ans = answer_question(question, opt_names, resume_text, profile)
            if ans:
                chosen = [a.strip().lower() for a in ans.split(",")]
                for label, el in options:
                    if label.strip().lower() in chosen:
                        try:
                            el.check()
                        except Exception:
                            pass
                log.append({"question": question, "answer": ans, "source": "claude"})
            else:
                log.append({"question": question, "answer": None, "source": "unanswered"})
    except Exception as e:
        print(f"  [fill] checkbox pass error: {e}", file=sys.stderr)


def _required_fields_actually_filled(frame, profile):
    """Verify the fields we just tried to fill really hold a value, before
    ever clicking submit. Checks id first (current Greenhouse UI), name
    second (older embed) — same fallback order as try_fill above."""
    checks = [
        ("first_name", "first_name", profile.get("first_name")),
        ("last_name", "last_name", profile.get("last_name")),
        ("email", "email", profile.get("email")),
    ]
    for field_id, name_attr, expected in checks:
        if not expected:
            continue
        got = ""
        for selector in (f"#{field_id}", f"input[name='{name_attr}']"):
            try:
                el = frame.locator(selector).first
                if el.count():
                    got = (el.input_value() or "").strip()
                    if got:
                        break
            except Exception:
                continue
        if not got:
            return False
    return True


def fill_lever_form(frame, job, profile, resume_text, resume_path, cover_path, log):
    """Fills Lever's standard application form — verified live against a
    real Lever posting (jobs.lever.co). Field names are fixed/standard
    across Lever boards: name, email, phone, location, resume (file),
    urls[LinkedIn]/urls[GitHub]/urls[Portfolio], plus custom "cards[<uuid>]
    [fieldN]" questions (radio groups and free-text) that vary per posting.

    Known best-effort limitation: Lever's location field is often a
    location-autocomplete widget backed by a hidden `selectedLocation`
    field — filling the visible text input alone may not populate that
    hidden field the way picking a dropdown suggestion would. If the
    posting requires a resolved location and this isn't enough, the submit
    click below will fail and this correctly falls through to the
    unanswered/error path rather than silently claiming success.
    """
    def try_fill(selector, value):
        if not value:
            return
        try:
            el = frame.locator(selector).first
            if el.count():
                el.fill(str(value))
        except Exception:
            pass

    full_name = " ".join(x for x in (profile.get("first_name"), profile.get("last_name")) if x)
    try_fill("input[name='name']", full_name)
    try_fill("input[name='email']", profile.get("email"))
    try_fill("input[name='phone']", profile.get("phone"))
    loc = ", ".join(x for x in (profile.get("location_city"), profile.get("location_state")) if x)
    try_fill("input[name='location']", loc)
    try_fill("input[name='urls[LinkedIn]']", profile.get("linkedin"))
    try_fill("input[name='urls[GitHub]']", profile.get("github"))
    try_fill("input[name='urls[Portfolio]']", profile.get("portfolio_url"))

    for name, path in (("resume", resume_path),):
        if not path or not os.path.exists(path):
            continue
        try:
            el = frame.locator(f"input[name='{name}']").first
            if el.count():
                el.set_input_files(path)
        except Exception:
            pass

    # Custom per-posting questions: Lever groups these as
    # cards[<uuid>][field0] (radio options) or cards[<uuid>][field1]
    # (free-text). Group by the cards[<uuid>] prefix, same
    # ask-Claude-then-select pattern as Greenhouse's checkbox groups.
    try:
        radios = frame.locator("input[type='radio'][name^='cards[']")
        n = radios.count()
        groups = {}
        for i in range(n):
            el = radios.nth(i)
            name = el.get_attribute("name") or ""
            if name:
                groups.setdefault(name, []).append(el)
        for name, els in groups.items():
            options = []
            for el in els:
                try:
                    lbl_id = el.get_attribute("id")
                    lbl = frame.locator(f"label[for='{lbl_id}']").first if lbl_id else None
                    if not (lbl and lbl.count()):
                        lbl = el.locator("xpath=following-sibling::*[1]").first
                    options.append((lbl.inner_text().strip() if lbl and lbl.count() else "", el))
                except Exception:
                    options.append(("", el))
            question_text = ""
            try:
                fs = els[0].locator("xpath=ancestor::div[contains(@class,'application-question') "
                                     "or contains(@class,'card')][1]//*[self::div or self::span]"
                                     "[contains(@class,'application-label') or "
                                     "contains(@class,'field-label')]").first
                if fs.count():
                    question_text = fs.inner_text().strip()
            except Exception:
                pass
            question = question_text or name
            opt_names = [o for o, _ in options if o]
            ans = answer_question(question, opt_names, resume_text, profile)
            if ans:
                chosen = [a.strip().lower() for a in ans.split(",")]
                for label, el in options:
                    if label.strip().lower() in chosen:
                        try:
                            el.check()
                        except Exception:
                            pass
                log.append({"question": question, "answer": ans, "source": "claude"})
            else:
                log.append({"question": question, "answer": None, "source": "unanswered"})
    except Exception as e:
        print(f"  [fill] lever radio-question pass error: {e}", file=sys.stderr)

    try:
        textareas = frame.locator("textarea[name^='cards[']")
        n = textareas.count()
        for i in range(n):
            el = textareas.nth(i)
            name = el.get_attribute("name") or ""
            question_text = ""
            try:
                fs = el.locator("xpath=ancestor::div[contains(@class,'application-question') "
                                 "or contains(@class,'card')][1]//*[self::div or self::span]"
                                 "[contains(@class,'application-label') or "
                                 "contains(@class,'field-label')]").first
                if fs.count():
                    question_text = fs.inner_text().strip()
            except Exception:
                pass
            question = question_text or name
            ans = answer_question(question, None, resume_text, profile)
            if ans:
                el.fill(ans)
                log.append({"question": question, "answer": ans, "source": "claude"})
            else:
                log.append({"question": question, "answer": None, "source": "unanswered"})
    except Exception as e:
        print(f"  [fill] lever textarea-question pass error: {e}", file=sys.stderr)


def _finish_application(page, frame, job, profile, resume_text, resume_path, cover_path, log):
    """Shared tail end for any ATS we can fill (Greenhouse, Lever, ...):
    escalate unanswered questions to Telegram, re-check for CAPTCHA
    (some forms only reveal it after fields are filled), then submit and
    screenshot. Only 'applied' is a genuine submission; every other exit
    here is a caller-visible non-permanent status."""
    unanswered = [q for q in log if q.get("source") == "unanswered"]
    if unanswered:
        # Escalate to Telegram for each unanswered question, in order. If any
        # one of them can't be resolved in time, bail out — never submit with
        # a blank required question.
        for item in unanswered:
            asked_at = time.time()
            sent, _detail, _msg_id = send_whatsapp(
                f"🧑‍💻 Stuck on {job.get('company','')} application:\n"
                f"\"{item['question']}\"\nReply with your answer.")
            if not sent:
                return "unanswered", log, None
            reply = wait_for_whatsapp_reply(asked_at)
            if not reply:
                return "unanswered", log, None
            final_answer, source_tag = resolve_telegram_reply(
                reply, item["question"], resume_text, profile)
            item["answer"], item["source"] = final_answer, source_tag
            try:
                el = frame.get_by_label(item["question"]).first
                if el.count():
                    el.fill(final_answer)
            except Exception:
                pass

    if has_captcha(frame):  # re-check — some forms reveal it after fields are filled
        if escalate_to_remote_browser(page.url, job, profile, resume_text, resume_path, cover_path, log):
            return "escalated_remote", log, None
        return "captcha", log, None

    # Never submit unless the required fields genuinely took a value — this
    # is what a completely blank real submission to Incident IQ (2026-09-01)
    # taught the hard way: a selector mismatch silently filled nothing, and
    # the old code still clicked submit and called it "applied". Only
    # checked for Greenhouse (has an id-based verifier); Lever's fields are
    # standard `name`-based and were verified live already.
    if find_greenhouse_frame(page) is frame or (frame is page.main_frame and "greenhouse.io" in page.url):
        if not _required_fields_actually_filled(frame, profile):
            log.append({"error": "required fields (first/last name, email) did not "
                                  "verify as filled after fill_greenhouse_form — refusing "
                                  "to submit a blank application"})
            return "fill_failed", log, None

    shot_path = None
    try:
        submit = frame.get_by_role("button", name=re.compile("submit", re.I)).first
        if submit.count():
            submit.click()
            page.wait_for_timeout(3000)
            shot_path = f"/tmp/{slug(job.get('company'))}_{slug(job.get('title'))}_confirmation.png"
            page.screenshot(path=shot_path, full_page=True)
            # Verify the click actually went through — if the same submit
            # button is still visible, the form almost certainly rejected
            # the submission (client-side validation on a missing/invalid
            # required field) and we're still looking at the same page.
            # Never claim "applied" just because a click event fired.
            try:
                still_there = frame.get_by_role("button", name=re.compile("submit", re.I)).first
                if still_there.count() and still_there.is_visible():
                    log.append({"error": "submit button still visible after click — "
                                          "submission was likely rejected client-side "
                                          "(validation error), not confirmed as applied"})
                    return "submit_unconfirmed", log, shot_path
            except Exception:
                pass  # frame/page navigated away entirely — that's the success case
            return "applied", log, shot_path
    except Exception as e:
        log.append({"error": str(e)})
    return "error", log, shot_path


def apply_to_job(page, job, profile, resume_text, resume_path, cover_path):
    """Returns (status, log, screenshot_path). status in:
    'applied', 'not_greenhouse', 'captcha', 'unanswered', 'redirect_failed',
    'blocked', 'error'.

    'redirect_failed' means we never even confirmed which ATS (if any) the
    job uses — still stuck on Adzuna, or a navigation error along the way.
    That's distinct from 'not_greenhouse', which means resolution genuinely
    succeeded and the destination just isn't Greenhouse. Only 'applied',
    'not_greenhouse', and 'captcha' are safe to mark permanently seen —
    'redirect_failed', 'error', and 'unanswered' are all retry-eligible and
    the caller must not add them to the permanent applied-state file."""
    log = []
    original_page = page
    try:
        rstatus, final_url, page, diag = resolve_real_url(page, job.get("url", ""))
    except Exception as e:
        # Never silently swallow this — an unexpected exception here means
        # something in resolve_real_url itself broke, not just a stuck page.
        log.append({"error": f"resolve_real_url raised: {e}"})
        return "redirect_failed", log, None

    log.append({"resolved_status": rstatus, "resolved_url": final_url,
                "page_title": diag.get("title", ""), "frame_urls": diag.get("frame_urls", []),
                "actions": diag.get("actions", [])})

    # From here on everything uses `page` — possibly a popup opened during
    # resolution. Close the original tab now (if different) since it's no
    # longer needed, and make sure `page` itself (popup or not) is closed
    # exactly once when this function is done, on every exit path.
    _close_extra_page(original_page, page)
    try:
        return _apply_to_resolved_page(page, rstatus, job, profile, resume_text,
                                        resume_path, cover_path, log)
    finally:
        if page is not original_page:
            try:
                page.close()
            except Exception:
                pass  # already closed, or never fully opened — fine either way


# ---- freshness cross-check ------------------------------------------------
# Adzuna (and aggregators generally) sometimes report a job as freshly
# posted when the employer's own listing is actually much older — a stale
# re-index, not a lie exactly, but it wastes an application on a posting
# that may already be filled or closed. Most ATS/company job pages embed a
# schema.org JobPosting block (JSON-LD) with a real "datePosted" — read that
# straight off the page we already navigated to (no extra request) and
# compare it against what the source told us.
_JOBPOSTING_DATE_RE = re.compile(
    r'"@type"\s*:\s*"JobPosting".{0,2000}?"datePosted"\s*:\s*"([^"]+)"'
    r'|"datePosted"\s*:\s*"([^"]+)".{0,2000}?"@type"\s*:\s*"JobPosting"',
    re.IGNORECASE | re.DOTALL)

STALE_LISTING_DAYS = 21  # company page's own date this much older than the
                          # source's claimed date => treat as stale, skip


def _parse_iso_loose(s):
    if not s:
        return None
    s = str(s).strip()
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def _extract_company_posted_date(html):
    """Best-effort schema.org JobPosting datePosted from the page's own
    HTML. Returns a timezone-aware datetime, or None if not found/parseable
    (most company pages simply don't include it — that's fine, we just skip
    the check rather than block on it)."""
    if not html:
        return None
    m = _JOBPOSTING_DATE_RE.search(html)
    if not m:
        return None
    raw = m.group(1) or m.group(2)
    return _parse_iso_loose(raw)


def _check_listing_freshness(page, job, log):
    """Returns True if this listing looks stale enough to skip (company's
    own posted date is STALE_LISTING_DAYS+ older than what the source
    claimed), False otherwise (including "couldn't tell" — never block a
    real application just because we couldn't find a date)."""
    source_posted = _parse_iso_loose(job.get("posted"))
    if not source_posted:
        return False
    try:
        html = page.content()
    except Exception:
        return False
    company_posted = _extract_company_posted_date(html)
    if not company_posted:
        return False
    gap_days = (source_posted - company_posted).total_seconds() / 86400
    if gap_days >= STALE_LISTING_DAYS:
        log.append({
            "stale_check": {
                "source_posted": source_posted.isoformat(),
                "company_posted": company_posted.isoformat(),
                "gap_days": round(gap_days, 1),
            }
        })
        return True
    return False


def _captcha_present_with_retry(page, frame, attempts=3, wait_ms=1500):
    """Recaptcha/hCaptcha widgets are sometimes injected into the DOM
    asynchronously by the ATS's own JS a beat after the page finishes
    loading — a single instantaneous check can miss one that only appears
    moments later. Confirmed live (2026-09, MrBeast + DoorDash Greenhouse
    postings): the initial has_captcha() check found nothing, so the form
    got filled and a screening question got escalated to Telegram — only
    for the SECOND has_captcha() check (right before submit) to correctly
    catch the now-loaded widget and block submission. Correct outcome
    (never submits past a captcha), but wastes your time answering a
    question for an application that was never going to go through. Poll a
    few times with a short wait up front so we bail out BEFORE asking you
    anything, instead of after."""
    for i in range(attempts):
        if has_captcha(frame):
            return True
        if i < attempts - 1:
            try:
                page.wait_for_timeout(wait_ms)
            except Exception:
                break
    return False


def _apply_to_resolved_page(page, rstatus, job, profile, resume_text, resume_path, cover_path, log):
    if rstatus == "redirect_failed":
        shot_path = f"/tmp/{slug(job.get('company'))}_{slug(job.get('title'))}_redirect_failed.png"
        try:
            page.screenshot(path=shot_path, full_page=True)
        except Exception as e:
            log.append({"error": f"screenshot on redirect_failed also failed: {e}"})
            shot_path = None
        return "redirect_failed", log, shot_path

    if _check_listing_freshness(page, job, log):
        return "stale_listing", log, None

    frame = find_greenhouse_frame_with_retry(page)
    if frame:
        if _captcha_present_with_retry(page, frame):
            if escalate_to_remote_browser(page.url, job, profile, resume_text, resume_path, cover_path, log):
                return "escalated_remote", log, None
            return "captcha", log, None
        fill_greenhouse_form(frame, job, profile, resume_text, resume_path, cover_path, log)
        return _finish_application(page, frame, job, profile, resume_text, resume_path, cover_path, log)

    lever_frame = find_lever_form(page)
    if lever_frame:
        if _captcha_present_with_retry(page, lever_frame):
            if escalate_to_remote_browser(page.url, job, profile, resume_text, resume_path, cover_path, log):
                return "escalated_remote", log, None
            return "captcha", log, None
        fill_lever_form(lever_frame, job, profile, resume_text, resume_path, cover_path, log)
        return _finish_application(page, lever_frame, job, profile, resume_text, resume_path, cover_path, log)

    if _is_challenge_page(page):
        # We never actually saw the real page — a bot-challenge
        # interstitial (Cloudflare etc.) loaded instead. This is NOT a
        # confirmed "not Greenhouse/Lever" result, so it must stay
        # retry-eligible, not be permanently blacklisted. We do not
        # attempt to solve or evade the challenge, same hard rule as
        # CAPTCHAs — just diagnose it honestly.
        log.append({"blocked_title": _safe_title(page)})
        return "blocked", log, None

    # We DID leave Adzuna (rstatus == "resolved"/"direct") and confirmed
    # this is a real destination that isn't a supported ATS — this one is a
    # legitimate permanent skip, not a transient failure.
    return "not_greenhouse", log, None


# --------------------------------------------------------------------------- #
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


def read_resume_text(path):
    # reuse resume_creation_agent's docx reader if available, else a minimal one
    if path.lower().endswith(".docx"):
        import zipfile, html as _html
        with zipfile.ZipFile(path) as z:
            xml = z.read("word/document.xml").decode("utf-8", "ignore")
        xml = re.sub(r"</w:p>", "\n", xml)
        xml = re.sub(r"<[^>]+>", "", xml)
        return _html.unescape(xml)
    return open(path, encoding="utf-8").read()


def main():
    load_keys_json()
    load_dotenv()
    ap = argparse.ArgumentParser(description="Stage 5: auto-submit Greenhouse applications")
    ap.add_argument("--scored", default=os.environ.get("SCORED_FILE", "../job_filter_agent/scored.json"))
    ap.add_argument("--resumes", default=os.environ.get("RESUME_OUT", "../resume_creation_agent/output"))
    ap.add_argument("--profile", default=os.environ.get("PROFILE_FILE", "../profile.json"))
    ap.add_argument("--min-score", type=int, default=int(os.environ.get("MIN_SCORE", 75)))
    ap.add_argument("--top", type=int, default=int(os.environ.get("TOP", 10)))
    ap.add_argument("--seen-file", default=os.environ.get("APPLIED_SEEN_FILE", "state/applied.json"))
    ap.add_argument("--success-file", default=os.environ.get("APPLIED_SUCCESS_FILE",
                    "state/auto_applied_success.json"),
                    help="jobs actually submitted here — apply_notifier.py reads this "
                         "too, so it never re-notifies you about one already done")
    ap.add_argument("--headless", action="store_true", default=False,
                    help="run headless. Default is headed (needs xvfb-run in CI) because "
                         "Adzuna's own /land/ad/... redirect page does a client-side JS "
                         "redirect that does not fire for headless Chromium — likely "
                         "fingerprint-based anti-scraping on their end, not a bug here.")
    args = ap.parse_args()

    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        sys.exit("ERROR: playwright not installed. Run: pip install playwright && playwright install chromium")

    try:
        jobs = json.load(open(args.scored, encoding="utf-8"))
    except Exception as e:
        sys.exit(f"ERROR: could not read {args.scored}: {e}")
    jobs = sorted([j for j in jobs if j.get("score", 0) >= args.min_score],
                  key=lambda j: j.get("score", 0), reverse=True)[:args.top]

    try:
        profile = json.load(open(args.profile, encoding="utf-8"))
    except Exception:
        profile = {}

    seen = load_seen(args.seen_file)
    success = load_seen(args.success_file)
    applied, skipped = 0, 0

    with sync_playwright() as p:
        global _ACTIVE_PLAYWRIGHT
        _ACTIVE_PLAYWRIGHT = p
        browser = p.chromium.launch(headless=args.headless)
        for j in jobs:
            jid = (j.get("url") or f"{j.get('company')}:{j.get('title')}").strip()
            if jid in seen:
                continue
            title, company = j.get("title", ""), j.get("company", "")
            folder = os.path.join(args.resumes, f"{slug(company)}__{slug(title)}")
            resume_path = os.path.join(folder, "resume.docx")
            cover_path = os.path.join(folder, "cover_letter.docx")
            if not os.path.exists(resume_path):
                print(f"  skip (no tailored resume yet): {title[:40]} — {company[:20]}")
                continue
            resume_text = read_resume_text(resume_path)

            page = browser.new_page()
            try:
                status, log, screenshot = apply_to_job(page, j, profile, resume_text,
                                                        resume_path, cover_path)
            except Exception as e:
                status, log, screenshot = "error", [{"error": str(e)}], None
            finally:
                # apply_to_job already closes any popup/new tab it opened
                # during redirect resolution, and — when it did — also
                # closes this original page itself (see _close_extra_page).
                # Guard against that double-close rather than assume either
                # way; closing an already-closed page just raises harmlessly
                # otherwise.
                try:
                    page.close()
                except Exception:
                    pass

            if status in PERMANENT_STATUSES:
                # Only a genuinely resolved outcome — a real submission, a
                # confirmed non-Greenhouse ATS, or a CAPTCHA gate — earns a
                # permanent skip. A redirect that failed, an unexpected
                # error, or an unanswered screening question is transient:
                # leaving it out of `seen` means a future run retries it
                # instead of silently dropping a job forever because of a
                # bad run.
                seen.add(jid)
            else:
                print(f"  [retry-eligible] {title[:40]} — {company[:20]} "
                      f"(status={status}, not marked permanently seen)")

            if status == "applied":
                applied += 1
                success.add(jid)
                qa_lines = "\n".join(f"- {q['question']}: {q['answer']} ({q['source']})"
                                     for q in log if q.get("question"))
                body = (f"Auto-applied to {title} @ {company} (fit {j.get('score')}).\n\n"
                        f"Screening answers given:\n{qa_lines or '(none)'}\n\n"
                        "Confirmation screenshot, resume, and cover letter attached.")
                emailed = send_email(f"Auto-applied: {title} @ {company}", body,
                                     [p_ for p_ in (screenshot, resume_path, cover_path) if p_])
                print(f"  [applied] {title[:40]} — {company[:20]}"
                      + ("" if emailed else "  [WARNING: confirmation email FAILED to send — "
                                             "this is your only record of this real submission]"))
                if not emailed:
                    # The confirmation email is the only durable evidence a real
                    # submission happened — never let that failure be silent.
                    # Best-effort Telegram fallback so it's not lost entirely.
                    try:
                        send_whatsapp(f"⚠️ Auto-applied to {title} @ {company} but the "
                                      "confirmation EMAIL FAILED to send — check Gmail "
                                      "creds. This was a real submission with no email "
                                      "record; screenshot was at "
                                      f"{screenshot or '(none)'} on the runner (not saved).")
                    except Exception:
                        pass
            else:
                skipped += 1
                reason = {
                    "not_greenhouse": "not a Greenhouse or Lever form (unsupported ATS)",
                    "stale_listing": "the company's own posting date is much older than "
                                      "the source claimed — likely a stale re-index, "
                                      "skipped rather than wasting an application",
                    "fill_failed": "required fields (name/email) did not verify as filled "
                                    "after the form-fill pass — refused to submit blank, "
                                    "retryable (may be a selector mismatch to fix)",
                    "submit_unconfirmed": "clicked submit but the button/form was still "
                                          "there afterward — likely rejected client-side, "
                                          "not confirmed as a real submission",
                    "captcha": "CAPTCHA present — will not auto-submit",
                    "unanswered": "a screening question couldn't be answered "
                                  "(Claude was unsure, and Telegram reply didn't "
                                  "arrive in time / not configured)",
                    "redirect_failed": "couldn't resolve past Adzuna's redirect "
                                        "(or hit a navigation error) — retryable",
                    "blocked": "landed on a bot-challenge page (e.g. Cloudflare) "
                               "instead of the real posting — retryable, not "
                               "attempted to bypass",
                    "error": "unexpected error during fill/submit",
                }.get(status, status)
                print(f"  [skip:{status}] {title[:40]} — {company[:20]} — {reason}")
                if status in ("not_greenhouse", "redirect_failed", "blocked"):
                    for item in log:
                        if "resolved_url" in item:
                            print(f"      resolved_status: {item.get('resolved_status')}")
                            print(f"      resolved to: {item['resolved_url']}")
                            print(f"      page title: {item.get('page_title', '')}")
                            print(f"      frames on page: {item['frame_urls']}")
                            print(f"      actions attempted: {item.get('actions', [])}")
                # Leave this job for the existing email-me-the-link flow
                # (apply_notifier.py) — nothing else to do here.
        browser.close()
    _ACTIVE_PLAYWRIGHT = None

    save_seen(args.seen_file, seen)
    save_seen(args.success_file, success)
    print(f"\nAuto-applied {applied}, skipped {skipped} (left for manual/email flow).")


if __name__ == "__main__":
    main()
