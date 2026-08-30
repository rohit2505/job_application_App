#!/usr/bin/env python3
"""
filter_agent.py — the second stage of the pipeline.

Reads the jobs exported by job_search.py, then:
  1) SPONSORSHIP: drops jobs whose JD rules out sponsorship, and (if a sponsor
     list is given) keeps only employers with an H-1B sponsorship history.
  2) SCORE: asks Claude to score each remaining job 0-100 against YOUR resume,
     with a one-line reason.
  3) Keeps score >= --min-score, ranks best-first, and emails / prints the shortlist.

It dedups against its own state (state/seen_scored.json) and stores each job's
score, so it never re-pays to score the same job twice.

Pipeline:
    # stage 1 — fetch (in ../job_Search)
    python3 job_search.py "data engineer" --remote-only --location "United States" \
        --window-min 1440 --no-email --json-out ../job_filter_agent/jobs.json
    # stage 2 — filter + score (here)
    python3 filter_agent.py --jobs jobs.json --resume resume.md --min-score 70

Config via env / .env / the LOCAL_KEYS block below:
    ANTHROPIC_API_KEY   required — for scoring
    ANTHROPIC_MODEL     optional — defaults to claude-3-5-haiku-latest
    GMAIL_ADDRESS, GMAIL_APP_PASSWORD, DIGEST_TO   for the email digest
"""

import argparse
import json
import os
import re
import smtplib
import ssl
import sys
import urllib.request
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

TIMEOUT = 60
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
DEFAULT_MODEL = "claude-haiku-4-5-20251001"   # cheapest/fastest; good for scoring
RESUME_MAX = 8000   # cap resume chars sent to the model

# ==========================================================================  #
#  LOCAL TESTING KEYS — paste here to run locally. Env / .env take precedence.
#  This file is git-tracked: blank these before pushing, or use a .env file.
# ==========================================================================  #
LOCAL_KEYS = {
    "ANTHROPIC_API_KEY": "",
    "ANTHROPIC_MODEL": "",
    "GMAIL_ADDRESS": "",
    "GMAIL_APP_PASSWORD": "",
    "DIGEST_TO": "",
}


def _find_up(filename):
    tried = set()
    for start in (os.getcwd(), os.path.dirname(os.path.abspath(__file__))):
        d = start
        for _ in range(6):
            p = os.path.join(d, filename)
            if p not in tried:
                tried.add(p)
                if os.path.exists(p):
                    return p
            nd = os.path.dirname(d)
            if nd == d:
                break
            d = nd
    return None


def load_keys_json(filename="keys.json"):
    """Master JSON key file (same format as LOCAL_KEYS). Values load into the
    environment; real env vars still win. Keys starting with '_' are ignored."""
    p = _find_up(filename)
    if not p:
        return None
    try:
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
        for k, v in data.items():
            if not k.startswith("_") and isinstance(v, str) and v.strip():
                os.environ.setdefault(k.strip(), v.strip())
        return p
    except Exception as e:
        print(f"  [keys.json] {e}", file=sys.stderr)
        return None


def load_dotenv(filename=".env"):
    p = _find_up(filename)
    if not p:
        return None
    try:
        with open(p, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    except Exception as e:
        print(f"  [.env] {e}", file=sys.stderr)
    return p


def cfg(name):
    return os.environ.get(name) or LOCAL_KEYS.get(name, "") or ""


def _ssl():
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


SSL_CTX = _ssl()


# --------------------------------------------------------------------------- #
# Sponsorship history (same logic as the fetcher, kept standalone here)
# --------------------------------------------------------------------------- #
_CORP_SUFFIX = {"inc", "incorporated", "llc", "corp", "corporation", "ltd",
                "limited", "co", "company", "plc", "lp", "llp", "gmbh",
                "technologies", "technology", "services", "solutions", "systems",
                "labs", "group", "holdings", "global", "usa", "na", "the",
                "america", "americas", "us"}


_REMOTE_PHRASES = (
    "remote", "work from home", "wfh", "work from anywhere",
    "distributed team", "remote-first", "remote first", "fully remote",
    "100% remote", "telecommute", "virtual position", "work remotely",
)
_ONSITE_PHRASES = (
    "on-site", "onsite", "in-office", "in office", "hybrid", "relocation required",
)


def remote_signal(job):
    """Local, free (no LLM call) best-effort read of remote-friendliness from
    the JD text itself, since source APIs under- and mis-tag this constantly.
    Returns 'remote' | 'possibly-onsite/hybrid' | 'unclear' — a signal to show
    you, not a hard filter (so we never silently drop a real job over it)."""
    blob = " ".join(str(job.get(f, "")) for f in ("title", "location", "description")).lower()
    looks_remote = any(p in blob for p in _REMOTE_PHRASES)
    looks_onsite = any(p in blob for p in _ONSITE_PHRASES)
    if looks_remote and not looks_onsite:
        return "remote"
    if looks_onsite and not looks_remote:
        return "possibly-onsite/hybrid"
    if looks_remote and looks_onsite:
        return "hybrid-or-mixed-signal"
    return "unclear"


def norm_company(name):
    s = re.sub(r"[^a-z0-9 ]", " ", (name or "").lower())
    return " ".join(t for t in s.split() if t and t not in _CORP_SUFFIX)


def load_sponsors(path):
    if not path or not os.path.exists(path):
        return None
    names = set()
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                n = norm_company(line.split(",")[0].strip().strip('"'))
                if n:
                    names.add(n)
    if not names:
        return None
    idx = {}
    for n in names:
        toks = n.split()
        if toks:
            idx.setdefault(toks[0], []).append(toks)
    return (names, idx)


def has_sponsor_history(company, sponsors):
    if not sponsors:
        return True
    names, idx = sponsors
    ec = norm_company(company)
    if not ec:
        return False
    if ec in names:
        return True
    etoks = set(ec.split())
    for tok in etoks:
        for cand in idx.get(tok, ()):
            if all(t in etoks for t in cand):
                return True
    return False


# --------------------------------------------------------------------------- #
# Resume loading
# --------------------------------------------------------------------------- #
def find_resume(explicit):
    if explicit:
        return explicit if os.path.exists(explicit) else None
    for name in ("resume.md", "resume.txt", "resume.docx", "resume_base.md", "resume.pdf"):
        if os.path.exists(name):
            return name
    # Fuzzy: any file with "resume" in the name (e.g. rohit-nimmakuri-resume.docx).
    import glob
    for ext in ("docx", "pdf", "md", "txt"):
        for pat in (f"*resume*.{ext}", f"*Resume*.{ext}", f"*RESUME*.{ext}", f"*CV*.{ext}"):
            hits = sorted(glob.glob(pat))
            if hits:
                return hits[0]
    # Last resort: a single lone .docx / .pdf in the folder.
    for ext in ("docx", "pdf"):
        hits = sorted(glob.glob(f"*.{ext}"))
        if len(hits) == 1:
            return hits[0]
    return None


def _read_docx(path):
    # A .docx is a zip; the body text lives in word/document.xml. Extract text
    # runs with paragraph breaks — no external dependency needed.
    import zipfile
    import html as _html
    with zipfile.ZipFile(path) as z:
        xml = z.read("word/document.xml").decode("utf-8", "ignore")
    xml = re.sub(r"</w:p>", "\n", xml)                 # paragraph -> newline
    xml = re.sub(r"<w:tab\b[^>]*/>", "\t", xml)        # tabs
    xml = re.sub(r"<[^>]+>", "", xml)                  # drop all tags, keep text
    return _html.unescape(xml)


def read_resume(path):
    low = path.lower()
    if low.endswith(".pdf"):
        try:
            from pypdf import PdfReader
        except Exception:
            try:
                from PyPDF2 import PdfReader
            except Exception:
                raise RuntimeError("PDF resume needs pypdf: pip install pypdf "
                                   "(or save your resume as resume.md / .txt / .docx)")
        text = "\n".join((pg.extract_text() or "") for pg in PdfReader(path).pages)
    elif low.endswith(".docx"):
        text = _read_docx(path)
    else:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text[:RESUME_MAX]


# --------------------------------------------------------------------------- #
# Claude scoring
# --------------------------------------------------------------------------- #
_SYS = ("You are a precise technical recruiter. Do TWO things for the given job.\n"
        "1) SCORE resume-to-job fit (0-100) with a CAREER-DIRECTION lens. The "
        "candidate is transitioning FROM legacy PL/SQL / Oracle ETL INTO the "
        "MODERN data-engineering stack: Python, PySpark / Spark, Airflow & "
        "orchestration, cloud warehouses (Snowflake / BigQuery / Redshift), dbt, "
        "cloud (AWS / GCP / Azure), Kafka, Databricks, lakehouse. Judge fit by BOTH "
        "his transferable foundations AND whether the role MOVES HIM FORWARD:\n"
        "- His FOUNDATIONS transfer and are a strong base: SQL, PL/SQL, data "
        "modeling, warehousing, ETL/ELT concepts, pipelines.\n"
        "- REWARD roles built on the MODERN stack above — they advance the "
        "transition. Modern tools he hasn't used yet are LEARNABLE gaps; do NOT "
        "heavily penalize.\n"
        "- A HEAVILY LEGACY role (pure Informatica / DataStage / SSIS / Ab Initio, "
        "mainframe, or PL/SQL-maintenance only) is a LOWER fit EVEN IF it matches "
        "his current tools, because it does not advance the transition — cap such "
        "roles around 55-65.\n"
        "- Domain/industry unfamiliarity is a MINOR factor.\n"
        "- Penalize hard only for genuine mismatch (frontend, pure ML research, "
        "management-only, non-data) or far-off seniority.\n"
        "Bands: 85-100 modern data-engineering role his foundations fit well; "
        "70-84 solid modern/data role with learnable gaps; 55-69 adjacent OR "
        "legacy-heavy; <50 wrong field.\n"
        "2) CLASSIFY the employer as one of: \"direct\" (the company is hiring "
        "for its OWN product/team), \"staffing\" (a staffing/recruiting agency "
        "placing you at an unnamed client), \"consulting\" (an IT-services / "
        "consulting / outsourcing firm that bills you out to clients, e.g. "
        "system integrators and bodyshops), or \"unknown\". Signals of NOT direct: "
        "phrases like 'our client', 'contract', 'contract-to-hire', 'W2', "
        "'multiple positions', staffing/consulting/solutions/resourcing in the "
        "company name.\n"
        "Reply with ONLY compact JSON: "
        '{"score": <int 0-100>, "reason": "<=18 words", '
        '"employer_type": "direct|staffing|consulting|unknown"}. No other text.')


def score_job(resume, job, model, key):
    user = (f"RESUME:\n{resume}\n\n"
            f"JOB\nTitle: {job.get('title','')}\n"
            f"Company: {job.get('company','')}\n"
            f"Salary: {job.get('salary','')}\n"
            f"Description:\n{job.get('description','')[:4000]}\n\n"
            "Score fit and classify the employer. JSON only.")
    body = json.dumps({
        "model": model,
        "max_tokens": 150,
        "system": _SYS,
        "messages": [{"role": "user", "content": user}],
    }).encode("utf-8")
    req = urllib.request.Request(ANTHROPIC_URL, data=body, headers={
        "x-api-key": key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    })
    with urllib.request.urlopen(req, timeout=TIMEOUT, context=SSL_CTX) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    text = "".join(b.get("text", "") for b in data.get("content", []))
    mobj = re.search(r"\{.*\}", text, re.DOTALL)
    if not mobj:
        return None, "no JSON from model", "unknown"
    obj = json.loads(mobj.group(0))
    score = max(0, min(100, int(obj.get("score"))))
    reason = str(obj.get("reason", ""))[:200]
    etype = str(obj.get("employer_type", "unknown")).strip().lower()
    if etype not in ("direct", "staffing", "consulting", "outsourcing", "agency", "unknown"):
        etype = "unknown"
    return score, reason, etype


# --------------------------------------------------------------------------- #
# State
# --------------------------------------------------------------------------- #
def load_seen(path):
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def save_seen(path, seen):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(seen, f, indent=0, sort_keys=True)


def job_key(j):
    return (j.get("url") or f"{j.get('source')}:{j.get('company')}:{j.get('title')}").strip()


# --------------------------------------------------------------------------- #
# Email
# --------------------------------------------------------------------------- #
def render_html(scored, label):
    def card(j):
        s = j["score"]
        color = "#137333" if s >= 85 else "#8a6d00" if s >= 65 else "#b3261e"
        return f"""
        <div style="border:1px solid #e5e5e5;border-radius:8px;padding:12px 14px;margin:10px 0">
          <div style="display:flex;justify-content:space-between;align-items:baseline">
            <div style="font-size:16px;font-weight:600">
              <a href="{j.get('url','')}" style="color:#1a56db;text-decoration:none">{j.get('title','')}</a>
            </div>
            <div style="font-size:20px;font-weight:700;color:{color}">{s}</div>
          </div>
          <div style="color:#333;margin-top:2px">{j.get('company','')} — {j.get('location','')}</div>
          <div style="color:#555;font-size:13px;margin-top:2px"><i>{j.get('reason','')}</i></div>
          <div style="color:#888;font-size:12px;margin-top:2px">via {j.get('source','')}"""\
               + (f" · 💰 {j['salary']}" if j.get("salary") and j["salary"] != "—" else "") + "</div></div>"
    rows = "".join(card(j) for j in scored)
    return (f'<html><body style="font-family:-apple-system,Segoe UI,Roboto,sans-serif;max-width:680px;margin:auto">'
            f'<h2 style="margin-bottom:0">{label}: {len(scored)} scored match(es)</h2>'
            f'<div style="color:#888;font-size:13px">ranked by resume-fit score</div>{rows}'
            '<div style="color:#aaa;font-size:12px;margin-top:16px">Scored by Claude against your resume.</div>'
            '</body></html>')


def render_text(scored, label):
    lines = [f"{label}: {len(scored)} scored match(es)\n"]
    for j in scored:
        lines.append(f"[{j['score']}] {j.get('title','')} — {j.get('company','')}")
        lines.append(f"      {j.get('reason','')}")
        if j.get("salary") and j["salary"] != "—":
            lines.append(f"      Salary: {j['salary']}")
        lines.append(f"      {j.get('url','')}\n")
    return "\n".join(lines)


def send_digest(scored, label):
    addr, pw = cfg("GMAIL_ADDRESS"), cfg("GMAIL_APP_PASSWORD")
    to = cfg("DIGEST_TO") or addr
    if not (addr and pw):
        print("  [email] skipped — set GMAIL_ADDRESS and GMAIL_APP_PASSWORD")
        return
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"[{label}] {len(scored)} scored job(s)"
    msg["From"], msg["To"] = addr, to
    msg.attach(MIMEText(render_text(scored, label), "plain", "utf-8"))
    msg.attach(MIMEText(render_html(scored, label), "html", "utf-8"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=SSL_CTX, timeout=TIMEOUT) as s:
        s.login(addr, pw)
        s.sendmail(addr, [to], msg.as_string())
    print(f"  [email] sent {len(scored)} scored job(s) to {to}")


# --------------------------------------------------------------------------- #
def main():
    load_keys_json()
    load_dotenv()
    ap = argparse.ArgumentParser(description="Filter + Claude-score fetched jobs against your resume")
    ap.add_argument("--jobs", default=os.environ.get("JOBS_FILE", "jobs.json"),
                    help="jobs JSON exported by job_search.py --json-out (default: ./jobs.json)")
    ap.add_argument("--resume", default=os.environ.get("RESUME_FILE"),
                    help="resume file (.md/.txt/.pdf). Autodetects resume.md/.txt if omitted.")
    ap.add_argument("--sponsors-file", default=os.environ.get("SPONSORS_FILE", "../job_Search/sponsors.txt"),
                    help="employer sponsor list; keep only these companies (blank to skip)")
    ap.add_argument("--any-company", action="store_true",
                    help="do NOT require sponsorship history (skip the sponsor-list filter)")
    ap.add_argument("--keep-no-sponsor", action="store_true",
                    help="keep jobs whose JD explicitly rules out sponsorship (default drops them)")
    ap.add_argument("--staffing-file", default=os.environ.get("STAFFING_FILE", "staffing_blocklist.txt"),
                    help="known staffing/consulting/outsourcing firms to drop (direct employers only)")
    ap.add_argument("--allow-staffing", action="store_true",
                    help="do NOT drop staffing/consulting/outsourcing employers")
    ap.add_argument("--strict-direct", action="store_true",
                    help="also drop employers Claude can't confirm are direct (employer_type unknown)")
    ap.add_argument("--model", default=None,
                    help="Anthropic model id (overrides ANTHROPIC_MODEL / the default). "
                         "List yours: curl https://api.anthropic.com/v1/models "
                         "-H \"x-api-key: $KEY\" -H \"anthropic-version: 2023-06-01\"")
    ap.add_argument("--min-score", type=int, default=int(os.environ.get("MIN_SCORE", 70)))
    ap.add_argument("--max-calls", type=int, default=int(os.environ.get("MAX_CALLS", 60)),
                    help="cap the number of Claude scoring calls per run (cost guard)")
    ap.add_argument("--seen-file", default=os.environ.get("SEEN_FILE", "state/seen_scored.json"))
    ap.add_argument("--rescore", action="store_true",
                    help="ignore the seen list and re-score every job (for testing/calibration)")
    ap.add_argument("--label", default=os.environ.get("LABEL", "Scored DE"))
    ap.add_argument("--scored-out", default=os.environ.get("SCORED_OUT"),
                    help="write the ranked, kept jobs (with score/reason/employer_type) "
                         "to this JSON — feeds the resume creation agent.")
    ap.add_argument("--no-email", action="store_true")
    args = ap.parse_args()

    key = cfg("ANTHROPIC_API_KEY")
    if not key:
        sys.exit("ERROR: set ANTHROPIC_API_KEY (in LOCAL_KEYS, .env, or the environment).")
    model = args.model or cfg("ANTHROPIC_MODEL") or DEFAULT_MODEL

    resume_path = find_resume(args.resume)
    if not resume_path:
        sys.exit("ERROR: no resume found. Add resume.md (or resume.txt/.pdf), or pass --resume.")
    resume = read_resume(resume_path)

    try:
        with open(args.jobs, encoding="utf-8") as f:
            jobs = json.load(f)
    except Exception as e:
        sys.exit(f"ERROR: could not read jobs file {args.jobs}: {e}")

    sponsors = None if args.any_company else load_sponsors(args.sponsors_file)
    staffing = None if args.allow_staffing else load_sponsors(args.staffing_file)
    seen = load_seen(args.seen_file)
    INTERMEDIARY = {"staffing", "consulting", "outsourcing", "agency"}

    print(f"Resume  : {resume_path} ({len(resume)} chars)")
    print(f"Jobs    : {len(jobs)} from {args.jobs}")
    print(f"Model   : {model}")
    print(f"Filters : min-score={args.min_score}, "
          + ("sponsor-history ON" if sponsors else "sponsor-history OFF")
          + (", keep-no-sponsor" if args.keep_no_sponsor else ", drop-no-sponsor")
          + (f", direct-only (blocklist {len(staffing[0])})" if staffing else ", staffing ALLOWED")
          + (", strict-direct" if args.strict_direct else "")
          + f", max-calls={args.max_calls}")

    kept, calls, skipped_seen, dropped_spon, dropped_staff = [], 0, 0, 0, 0
    for j in jobs:
        k = job_key(j)
        company = j.get("company", "")
        if k in seen and not args.rescore:
            skipped_seen += 1
            continue
        if not args.keep_no_sponsor and j.get("no_sponsor"):
            seen[k] = {"drop": "no_sponsor"}
            dropped_spon += 1
            continue
        if sponsors and not has_sponsor_history(company, sponsors):
            seen[k] = {"drop": "no_sponsor_history"}
            dropped_spon += 1
            continue
        # Deterministic staffing/consulting blocklist — drop before spending a call.
        if staffing and has_sponsor_history(company, staffing):
            seen[k] = {"drop": "staffing_blocklist"}
            dropped_staff += 1
            continue
        if calls >= args.max_calls:
            print(f"  [cap] hit --max-calls={args.max_calls}; stopping scoring early")
            break
        calls += 1
        try:
            score, reason, etype = score_job(resume, j, model, key)
        except Exception as e:
            print(f"  [score] error on '{j.get('title','')[:40]}': {e}", file=sys.stderr)
            continue
        if score is None:
            continue
        # Claude-detected intermediary (catches staffing firms not on the blocklist).
        if not args.allow_staffing and (etype in INTERMEDIARY
                                        or (args.strict_direct and etype == "unknown")):
            seen[k] = {"drop": f"employer_type:{etype}", "score": score}
            dropped_staff += 1
            continue
        seen[k] = {"score": score, "employer_type": etype}
        if score >= args.min_score:
            jj = dict(j)
            jj["score"], jj["reason"], jj["employer_type"] = score, reason, etype
            jj["remote_signal"] = remote_signal(j)
            kept.append(jj)

    kept.sort(key=lambda j: j["score"], reverse=True)
    save_seen(args.seen_file, seen)

    if args.scored_out:
        d_ = os.path.dirname(args.scored_out)
        if d_:
            os.makedirs(d_, exist_ok=True)
        with open(args.scored_out, "w", encoding="utf-8") as f:
            json.dump(kept, f, indent=2)
        print(f"  [scored] wrote {len(kept)} ranked jobs -> {args.scored_out}")

    print(f"\nScored {calls} new job(s) · {len(kept)} >= {args.min_score} · "
          f"{dropped_spon} dropped (sponsorship) · {dropped_staff} dropped (staffing/consulting) · "
          f"{skipped_seen} already seen.\n")
    for j in kept:
        print(f"[{j['score']}] {j.get('title','')} — {j.get('company','')} "
              f"({j.get('employer_type','')}) · remote-signal: {j.get('remote_signal','unclear')}")
        print(f"      {j.get('reason','')}")
        print(f"      {j.get('url','')}")

    if kept and not args.no_email:
        send_digest(kept, args.label)
    elif not kept:
        print("\nNothing cleared the score threshold — nothing to email.")


if __name__ == "__main__":
    main()
