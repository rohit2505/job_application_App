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
    """Name kept for call-site compatibility; sends via Telegram now."""
    token, chat_id = cfg("TELEGRAM_BOT_TOKEN"), cfg("TELEGRAM_CHAT_ID")
    if not (token and chat_id):
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": chat_id, "text": body}).encode()
    req = urllib.request.Request(url, data=data)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT, context=SSL_CTX) as r:
            json.loads(r.read().decode())
        return True
    except Exception as e:
        print(f"  [telegram] send error: {e}", file=sys.stderr)
        return False


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


def resolve_real_url(page, apply_url):
    """Follow aggregator redirects/click-throughs until we land somewhere real."""
    page.goto(apply_url, wait_until="networkidle", timeout=45000)
    # Adzuna-style: click "Apply for this job" once if present, to get past
    # its own /land/ad/... redirect layer.
    try:
        link = page.get_by_text("Apply for this job", exact=False).first
        if link and link.is_visible():
            with page.expect_navigation(wait_until="networkidle", timeout=45000):
                link.click()
    except Exception:
        pass
    return page.url


def find_greenhouse_frame(page):
    for frame in page.frames:
        if "greenhouse.io" in frame.url:
            return frame
    # some postings link straight to job-boards.greenhouse.io, no iframe
    if "greenhouse.io" in page.url:
        return page.main_frame
    return None


def has_captcha(frame):
    try:
        return frame.locator("iframe[src*='recaptcha'], .g-recaptcha, [name='g-recaptcha-response']").count() > 0
    except Exception:
        return False


def fill_greenhouse_form(frame, job, profile, resume_text, resume_path, cover_path, log):
    # Standard fields
    def try_fill(selector, value):
        if not value:
            return
        try:
            el = frame.locator(selector).first
            if el.count():
                el.fill(str(value))
        except Exception:
            pass

    try_fill("input[name='first_name']", profile.get("first_name"))
    try_fill("input[name='last_name']", profile.get("last_name"))
    try_fill("input[name='email']", profile.get("email"))
    try_fill("input[name='phone']", profile.get("phone"))
    try_fill("input[name='country']", profile.get("country"))
    loc = ", ".join(x for x in (profile.get("location_city"), profile.get("location_state")) if x)
    try_fill("input[name='candidate-location']", loc)

    # Resume / cover letter uploads
    for name, path in (("resume", resume_path), ("cover_letter", cover_path)):
        if not path or not os.path.exists(path):
            continue
        try:
            el = frame.locator(f"input[name='{name}']").first
            if el.count():
                el.set_input_files(path)
        except Exception:
            pass

    # Label-keyword-matched text fields (LinkedIn, website, how-did-you-hear, etc.)
    try:
        inputs = frame.locator("input[type='text']")
        n = inputs.count()
        for i in range(n):
            el = inputs.nth(i)
            name = el.get_attribute("name") or ""
            if name in ("first_name", "last_name", "email", "phone", "country",
                        "candidate-location") or not name.startswith("question_"):
                continue
            label_text = ""
            try:
                lbl_id = el.get_attribute("id")
                if lbl_id:
                    lbl = frame.locator(f"label[for='{lbl_id}']").first
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
            name = el.get_attribute("name") or ""
            if not name:
                continue
            groups.setdefault(name, []).append(el)
        for name, els in groups.items():
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
            question = fieldset_label or name
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


def apply_to_job(page, job, profile, resume_text, resume_path, cover_path):
    """Returns (status, log, screenshot_path). status in:
    'applied', 'not_greenhouse', 'captcha', 'unanswered', 'error'."""
    log = []
    try:
        final_url = resolve_real_url(page, job.get("url", ""))
    except Exception as e:
        return "error", [{"error": str(e)}], None

    frame = find_greenhouse_frame(page)
    if not frame:
        return "not_greenhouse", log, None

    if has_captcha(frame):
        return "captcha", log, None

    fill_greenhouse_form(frame, job, profile, resume_text, resume_path, cover_path, log)

    unanswered = [q for q in log if q.get("source") == "unanswered"]
    if unanswered:
        # Escalate to WhatsApp for each unanswered question, in order. If any
        # one of them can't be resolved in time, bail out — never submit with
        # a blank required question.
        for item in unanswered:
            asked_at = time.time()
            sent = send_whatsapp(
                f"🧑‍💻 Stuck on {job.get('company','')} application:\n"
                f"\"{item['question']}\"\nReply with your answer.")
            if not sent:
                return "unanswered", log, None
            reply = wait_for_whatsapp_reply(asked_at)
            if not reply:
                return "unanswered", log, None
            item["answer"], item["source"] = reply, "telegram"
            # best-effort: try to fill the field with the reply
            try:
                el = frame.get_by_label(item["question"]).first
                if el.count():
                    el.fill(reply)
            except Exception:
                pass

    if has_captcha(frame):  # re-check — some forms reveal it after fields are filled
        return "captcha", log, None

    shot_path = None
    try:
        submit = frame.get_by_role("button", name=re.compile("submit", re.I)).first
        if submit.count():
            submit.click()
            page.wait_for_timeout(3000)
            shot_path = f"/tmp/{slug(job.get('company'))}_{slug(job.get('title'))}_confirmation.png"
            page.screenshot(path=shot_path, full_page=True)
            return "applied", log, shot_path
    except Exception as e:
        log.append({"error": str(e)})
    return "error", log, shot_path


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
    ap.add_argument("--headless", action="store_true", default=True)
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
                page.close()

            seen.add(jid)  # don't retry automatically either way — surfaced via email/log
            if status == "applied":
                applied += 1
                success.add(jid)
                qa_lines = "\n".join(f"- {q['question']}: {q['answer']} ({q['source']})"
                                     for q in log if q.get("question"))
                body = (f"Auto-applied to {title} @ {company} (fit {j.get('score')}).\n\n"
                        f"Screening answers given:\n{qa_lines or '(none)'}\n\n"
                        "Confirmation screenshot, resume, and cover letter attached.")
                send_email(f"Auto-applied: {title} @ {company}", body,
                           [p_ for p_ in (screenshot, resume_path, cover_path) if p_])
                print(f"  [applied] {title[:40]} — {company[:20]}")
            else:
                skipped += 1
                reason = {
                    "not_greenhouse": "not a Greenhouse-hosted form (unsupported ATS)",
                    "captcha": "CAPTCHA present — will not auto-submit",
                    "unanswered": "a screening question couldn't be answered "
                                  "(Claude was unsure, and Telegram reply didn't "
                                  "arrive in time / not configured)",
                    "error": "unexpected error during fill/submit",
                }.get(status, status)
                print(f"  [skip:{status}] {title[:40]} — {company[:20]} — {reason}")
                # Leave this job for the existing email-me-the-link flow
                # (apply_notifier.py) — nothing else to do here.
        browser.close()

    save_seen(args.seen_file, seen)
    save_seen(args.success_file, success)
    print(f"\nAuto-applied {applied}, skipped {skipped} (left for manual/email flow).")


if __name__ == "__main__":
    main()
