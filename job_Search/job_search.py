#!/usr/bin/env python3
"""
job_search.py — a compliant, multi-source job-search agent.

Queries official / public job APIs, filters to your criteria, dedups against
jobs it has already seen, and emails you the new ones. No scraping, no
LinkedIn/Indeed logins — only APIs meant to be called this way, so it runs
reliably on free cloud (GitHub Actions).

Sources (all free; Adzuna needs a free key):
  aggregators / boards : Remotive, Arbeitnow, Adzuna, Jobicy, The Muse, RemoteOK
  company ATS boards   : Greenhouse, Lever, Ashby  (edit companies.json)

Freshness model:
  Each run fetches a lookback window (default 60 min interactively; the GitHub
  workflow uses ~25 h) then dedups against state/seen.json, so each job is
  emailed exactly once. A wide lookback + dedup means a skipped run or a coarse
  timestamp can't drop jobs through a crack.

Config via environment (keeps secrets out of the code):
  ADZUNA_APP_ID, ADZUNA_APP_KEY      Adzuna creds (optional; source skipped if unset)
  GMAIL_ADDRESS, GMAIL_APP_PASSWORD  Gmail + app password for the digest email
  DIGEST_TO                          recipient (defaults to GMAIL_ADDRESS)
  SEEN_FILE                          dedup state path (default state/seen.json)
  MUSE_API_KEY                       optional, raises The Muse rate limit

CLI:
  python3 job_search.py "data engineer" --remote-only --location "United States" --exclude-no-sponsorship
  python3 job_search.py "data engineer" --window-min 1440 --no-email          # local test
  python3 job_search.py "data engineer" --sources remotive,adzuna,jobicy      # subset
"""

import argparse
import json
import os
import re
import smtplib
import ssl
import sys
import textwrap
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

TIMEOUT = 20
WINDOW_MINUTES = 60
FUTURE_SKEW_SEC = 120
PRUNE_DAYS = 45
DEFAULT_SEEN_FILE = "state/seen.json"
COMPANIES_FILE = "companies.json"

ALL_SOURCES = ["remotive", "arbeitnow", "adzuna", "jobicy", "muse", "remoteok",
               "jsearch", "himalayas", "activejobsdb", "greenhouse", "lever", "ashby"]

# What runs by default: the aggregators + JSearch. The company ATS boards
# (greenhouse/lever/ashby) are OFF by default — enable with --sources if wanted.
DEFAULT_SOURCES = ["remotive", "arbeitnow", "adzuna", "jobicy", "muse",
                   "remoteok", "jsearch", "himalayas", "activejobsdb"]

# ==========================================================================  #
#  LOCAL TESTING KEYS  —  paste your keys here to run on your own machine.
#
#  !!  SECURITY  !!  This file is tracked by git. If you fill these in and then
#  `git push`, your keys become PUBLIC. For local testing that's fine; before
#  pushing, blank them back to "" (or better, leave these empty and put your
#  keys in a .env file instead — .env is gitignored and uses the same names,
#  so nothing changes when you later move to GitHub Secrets).
#
#  Priority: real environment variable  >  .env file  >  these LOCAL_KEYS.
# ==========================================================================  #
LOCAL_KEYS = {
    "ADZUNA_APP_ID": "",
    "ADZUNA_APP_KEY": "",
    "RAPIDAPI_KEY": "",           # JSearch (LinkedIn/Indeed/Glassdoor)
    "GMAIL_ADDRESS": "",
    "GMAIL_APP_PASSWORD": "",     # 16-char Google App Password (not your login)
    "DIGEST_TO": "",              # optional; defaults to GMAIL_ADDRESS
    "MUSE_API_KEY": "",           # optional
}


def cfg(name):
    """Config value: environment (incl. .env) first, then LOCAL_KEYS."""
    return os.environ.get(name) or LOCAL_KEYS.get(name, "") or ""

# Starter company list for the ATS boards. These are best-effort board slugs —
# edit companies.json to add your targets. Unknown/renamed slugs just 404 and
# are skipped, so a wrong entry never breaks the run.
DEFAULT_COMPANIES = {
    "greenhouse": ["databricks", "stripe", "airbnb", "coinbase", "dropbox"],
    "lever": ["plaid"],
    "ashby": ["ramp", "notion", "openai"],
}


# --------------------------------------------------------------------------- #
# Minimal .env loader (no dependency). Real env vars win over .env.
# --------------------------------------------------------------------------- #
def _find_up(filename):
    # Nearest file by walking UP from the working dir and this script's dir, so a
    # single MASTER config at the project root serves every script.
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
        with open(p, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    except Exception as e:
        print(f"  [.env] warning: {e}", file=sys.stderr)
    return p


def _make_ssl_context():
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


SSL_CTX = _make_ssl_context()


def get_json(url, headers=None):
    resp, _headers = get_json_with_headers(url, headers=headers)
    return resp


def get_json_with_headers(url, headers=None):
    """Same as get_json, but also returns the response headers (as a dict) —
    RapidAPI-fronted APIs (JSearch etc.) report remaining quota via headers
    like X-RateLimit-Requests-Remaining on EVERY response, including 429s, so
    callers that want to track quota need those even on failure."""
    req = urllib.request.Request(url, headers=headers or {"User-Agent": "job-search-agent/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT, context=SSL_CTX) as resp:
            return json.loads(resp.read().decode("utf-8")), dict(resp.headers)
    except urllib.error.HTTPError as e:
        # Surface the API's own message (RapidAPI etc. explain 403/404 in the body).
        try:
            body = e.read().decode("utf-8", "replace")[:300]
        except Exception:
            body = ""
        err = RuntimeError(f"HTTP {e.code} {e.reason} — {body}")
        err.headers = dict(e.headers) if e.headers else {}
        err.code = e.code
        raise err from None


# --------------------------------------------------------------------------- #
# Date helpers
# --------------------------------------------------------------------------- #
def parse_iso(s):
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


def parse_epoch(v, ms=False):
    try:
        v = int(v)
        if ms:
            v //= 1000
        return datetime.fromtimestamp(v, tz=timezone.utc)
    except Exception:
        return None


def in_window(posted, now, window_min):
    if posted is None:
        return False
    return -FUTURE_SKEW_SEC <= (now - posted).total_seconds() <= window_min * 60


def age_str(dt, now):
    if not dt:
        return "date unknown"
    secs = (now - dt).total_seconds()
    if secs < 3600:
        return f"{int(secs // 60)}m ago"
    if secs < 86400:
        return f"{int(secs // 3600)}h ago"
    return f"{int(secs // 86400)}d ago"


# --------------------------------------------------------------------------- #
# Sponsorship — NEGATIVE filter only (drop explicit "no sponsorship" JDs).
# --------------------------------------------------------------------------- #
_TAG_RE = re.compile(r"<[^>]+>")


def strip_html(text):
    return _TAG_RE.sub(" ", text) if text else ""


_NO_SPONSOR_PATTERNS = [
    r"no\s+(?:visa\s+)?sponsorship",
    r"sponsorship\s+(?:is\s+)?not\s+(?:available|offered|provided|possible)",
    r"(?:not|un)\s*able\s+to\s+(?:provide|offer|sponsor|support)\b",
    r"(?:not|n't|no|un)\w*\s+(?:able\s+to\s+|going\s+to\s+)?(?:provide|offer)\s+(?:visa\s+)?sponsorship",
    r"(?:do|does|will|are|can)\s*(?:not|n't)\s+(?:able\s+to\s+)?sponsor",
    r"cannot\s+sponsor",
    r"(?:not|n't|no)\s+(?:require|need)\s+(?:visa\s+)?sponsorship",
    r"must\s+not\s+require\s+(?:visa\s+)?sponsorship",
    r"without\s+(?:the\s+need\s+(?:for|of)\s+)?(?:visa\s+|company\s+|employer\s+|any\s+)?sponsorship",
    r"authoriz\w+\s+to\s+work[^.]{0,60}without\s+sponsorship",
    r"no\s+(?:current\s+or\s+future\s+)?(?:need\s+for\s+)?sponsorship",
    r"u\.?s\.?\s+citizens?\s+only",
    r"must\s+be\s+a\s+u\.?s\.?\s+citizen",
    r"citizenship\s+(?:is\s+)?required",
    r"active\s+security\s+clearance",
    r"security\s+clearance\s+(?:is\s+)?required",
]
_NO_SPONSOR_RE = re.compile("|".join(_NO_SPONSOR_PATTERNS), re.IGNORECASE)


def rejects_sponsorship(*texts):
    blob = re.sub(r"\s+", " ", strip_html(" ".join(t for t in texts if t)))
    return bool(_NO_SPONSOR_RE.search(blob))


# --------------------------------------------------------------------------- #
# Location matching (US-aware) + remote detection
# --------------------------------------------------------------------------- #
_ANYWHERE = ("worldwide", "anywhere", "global")   # remote-open-to-all


_REMOTE_PHRASES = (
    "remote", "work from home", "wfh", "work from anywhere",
    "distributed team", "remote-first", "remote first", "fully remote",
    "100% remote", "telecommute", "virtual position", "work remotely",
)


def _looks_remote(*texts):
    blob = " ".join(t.lower() for t in texts if t)
    return any(p in blob for p in _REMOTE_PHRASES)


# US indicators as whole words: "usa", "u.s.", "u.s.a", bare "us", "united states".
_US_RE = re.compile(r"\b(u\.?s\.?a\.?|usa|us|united\s+states(?:\s+of\s+america)?)\b",
                    re.IGNORECASE)
_US_WANTED = {"us", "usa", "u.s.", "u.s.a", "united states",
              "united states of america", "america"}


def _wants_us(wanted):
    return wanted.strip().lower() in _US_WANTED


def location_ok(j, wanted):
    """Whether a job satisfies the location filter (US-aware)."""
    if not wanted:
        return True
    loc = j["location"].lower()
    # A globally-open remote role is available from anywhere, including the US.
    if any(a in loc for a in _ANYWHERE):
        return True
    if _wants_us(wanted):
        if j.get("us_guaranteed"):          # source already limits to US
            return True
        return bool(_US_RE.search(loc))
    return wanted.strip().lower() in loc


# --------------------------------------------------------------------------- #
# Normalized job record
# --------------------------------------------------------------------------- #
def _num(x):
    """Coerce a salary value (number or numeric string) to float, else None."""
    if x is None:
        return None
    try:
        return float(str(x).replace(",", "").replace("$", "").strip())
    except Exception:
        return None



_SALARY_TEXT_RE = re.compile(
    r'\$\s?(\d{2,3}(?:,\d{3})?)\s*([kK])?\s*(?:-|–|—|to)\s*\$?\s?(\d{2,3}(?:,\d{3})?)\s*([kK])?'
)


def extract_salary_from_text(text):
    """Best-effort ($min - $max) extraction from free-text JD/title when the
    source API doesn't hand us structured salary fields at all."""
    if not text:
        return None, None
    m = _SALARY_TEXT_RE.search(text)
    if not m:
        return None, None
    lo, lo_k, hi, hi_k = m.groups()

    def to_num(v, k):
        v = float(v.replace(",", ""))
        if k or v < 1000:   # bare "120" (with k-suffix) or small number -> thousands
            v *= 1000
        return v

    lo_n, hi_n = to_num(lo, lo_k), to_num(hi, hi_k)
    if lo_n > hi_n:
        lo_n, hi_n = hi_n, lo_n
    return lo_n, hi_n



def format_salary_range(smin, smax):
    """Human-readable salary display. Treats 0 as 'not really given' (some
    APIs use 0 for unknown) rather than a real floor of $0."""
    smin = smin or None
    smax = smax or None
    if smin and smax:
        return f"{smin:,.0f}–{smax:,.0f}"
    if smax:
        return f"up to {smax:,.0f}"
    if smin:
        return f"{smin:,.0f}+"
    return None


JD_MAX = 4000  # cap stored JD length (enough for scoring, keeps JSON small)


def job(source, title, company, location, url, salary=None, tags=None,
        remote=False, posted=None, no_sponsor=False, us_guaranteed=False,
        salary_min=None, salary_max=None, description=None):
    clean_desc = strip_html(description).strip() if description else ""
    smin, smax = _num(salary_min) or None, _num(salary_max) or None
    if not smin and not smax:
        # Source gave us nothing structured — try to pull a range out of the
        # actual JD text (title + description) instead of assuming "no salary".
        smin, smax = extract_salary_from_text(f"{title or ''} {clean_desc}")
    remote_flag = bool(remote) or _looks_remote(title, clean_desc, location)
    return {
        "source": source, "title": (title or "").strip(),
        "company": (company or "").strip(), "location": (location or "").strip() or "—",
        "salary": salary or "—", "url": url or "", "tags": tags or [],
        "remote": remote_flag, "posted": posted, "no_sponsor": bool(no_sponsor),
        "us_guaranteed": bool(us_guaranteed),
        "salary_min": smin, "salary_max": smax,
        "description": clean_desc[:JD_MAX],
    }


def job_id(j):
    return j["url"].strip() or f"{j['source']}::{j['company']}::{j['title']}".lower()


def matches_query(query, *texts):
    q = query.lower()
    return any(q in (t or "").lower() for t in texts)


# Words to ignore when checking title relevance.
_TITLE_STOP = {"a", "an", "the", "of", "for", "and", "or", "in", "to", "with",
               "sr", "jr", "senior", "junior", "staff", "lead", "principal", "i",
               "ii", "iii", "remote"}


def title_relevant(query, title, extra_terms=None):
    """
    True if the TITLE actually matches the search intent — every meaningful word
    of the query must appear in the title (substring, so 'engineer' matches
    'engineering'). `extra_terms` are alternative title keywords that also pass
    (e.g. analytics engineer, etl, data platform), any one of which is enough.
    """
    t = title.lower()
    for term in (extra_terms or []):
        if term.lower() in t:
            return True
    tokens = [w for w in re.findall(r"[a-z0-9+#]+", query.lower())
              if w not in _TITLE_STOP]
    if not tokens:
        return True
    return all(tok in t for tok in tokens)


# --------------------------------------------------------------------------- #
# Sponsorship HISTORY (positive, company-level): keep only employers that have a
# recent H-1B/LCA filing record. Employer names in job feeds differ from legal
# names in DOL data ("Amazon.com Services LLC" vs "Amazon"), so we normalize
# both to a corporate-suffix-stripped key and match on a token-subset basis.
# --------------------------------------------------------------------------- #
_CORP_SUFFIX = {"inc", "incorporated", "llc", "l.l.c", "corp", "corporation",
                "ltd", "limited", "co", "company", "plc", "lp", "llp", "gmbh",
                "technologies", "technology", "services", "solutions", "systems",
                "labs", "group", "holdings", "global", "usa", "na", "the",
                "america", "americas", "us"}


def norm_company(name):
    s = re.sub(r"[^a-z0-9 ]", " ", (name or "").lower())
    toks = [t for t in s.split() if t and t not in _CORP_SUFFIX]
    return " ".join(toks)


def load_sponsors(path):
    """Load an employer sponsor list -> (names_set, first-token index) or None."""
    if not path or not os.path.exists(path):
        return None
    names = set()
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                # accept plain "Name" or CSV where the employer is the first column
                cell = line.split(",")[0].strip().strip('"')
                n = norm_company(cell)
                if n:
                    names.add(n)
    except Exception as e:
        print(f"  [sponsors] {e}", file=sys.stderr)
        return None
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
        return True                      # no list loaded -> don't filter
    names, idx = sponsors
    ec = norm_company(company)
    if not ec:
        return False
    if ec in names:
        return True
    etoks = set(ec.split())
    for tok in etoks:
        for cand in idx.get(tok, ()):    # sponsor names starting with this token
            if all(t in etoks for t in cand):
                return True
    return False


def salary_ok(j, min_salary, require_salary):
    """
    Pass a job against a minimum-salary floor. We compare the job's *upper*
    listed figure (salary_max, else salary_min). Jobs with NO salary data are
    kept by default (most postings don't publish salary — dropping them would
    throw away good roles); pass require_salary=True to drop those too.

    Some source APIs (Adzuna in particular) send 0 rather than null for
    "unknown" — treat 0 the same as missing, not as an actual $0 salary.
    """
    if not min_salary:
        return True
    smax, smin = j.get("salary_max") or None, j.get("salary_min") or None
    top = smax if smax else smin
    if not top:
        return not require_salary
    return top >= min_salary


def post_filter(jobs, location, remote_only, exclude_no_sponsor, limit,
                query=None, loose=False, extra_terms=None,
                min_salary=None, require_salary=False, sponsors=None):
    out = []
    for j in jobs:
        if remote_only and not j["remote"]:
            continue
        if exclude_no_sponsor and j["no_sponsor"]:
            continue
        if not location_ok(j, location):
            continue
        if query and not loose and not title_relevant(query, j["title"], extra_terms):
            continue
        if not salary_ok(j, min_salary, require_salary):
            continue
        if sponsors and not has_sponsor_history(j["company"], sponsors):
            continue
        out.append(j)
    out.sort(key=lambda j: j["posted"] or datetime.min.replace(tzinfo=timezone.utc),
             reverse=True)
    return out if limit is None else out[:limit]


# --------------------------------------------------------------------------- #
# SOURCES.  Each returns normalized jobs already trimmed to the time window.
# Each is wrapped so a single source failing never breaks the run.
# --------------------------------------------------------------------------- #
def _safe(fetch, name):
    try:
        return fetch()
    except Exception as e:
        print(f"  [{name}] error: {e}", file=sys.stderr)
        return []


def fetch_remotive(query, now, window_min):
    url = "https://remotive.com/api/remote-jobs?" + urllib.parse.urlencode(
        {"search": query, "limit": 200})
    out = []
    for j in get_json(url).get("jobs", []):
        posted = parse_iso(j.get("publication_date"))
        if not in_window(posted, now, window_min):
            continue
        title, desc = j.get("title", ""), j.get("description", "")
        out.append(job("remotive", title, j.get("company_name"),
                       j.get("candidate_required_location"), j.get("url"),
                       salary=j.get("salary") or None, tags=j.get("tags", []),
                       remote=True, posted=posted,
                       no_sponsor=rejects_sponsorship(title, desc), description=desc))
    return out


def fetch_arbeitnow(query, now, window_min):
    out = []
    for j in get_json("https://www.arbeitnow.com/api/job-board-api").get("data", []):
        posted = parse_epoch(j.get("created_at"))
        if not in_window(posted, now, window_min):
            continue
        title, desc = j.get("title", ""), j.get("description", "")
        if not matches_query(query, title, desc):
            continue
        loc = j.get("location", "")
        if isinstance(loc, list):
            loc = ", ".join(loc)
        tags = list(j.get("tags", []))
        if j.get("visa_sponsorship"):
            tags = ["visa_sponsorship"] + tags
        out.append(job("arbeitnow", title, j.get("company_name"), loc, j.get("url"),
                       tags=tags, remote=bool(j.get("remote")) or _looks_remote(title, loc),
                       posted=posted, no_sponsor=rejects_sponsorship(title, desc), description=desc))
    return out


def fetch_adzuna(query, now, window_min, country="us", location=None, remote_only=False):
    app_id, app_key = cfg("ADZUNA_APP_ID"), cfg("ADZUNA_APP_KEY")
    if not (app_id and app_key):
        print("  [adzuna] skipped — set ADZUNA_APP_ID and ADZUNA_APP_KEY")
        return []
    params = {
        "app_id": app_id, "app_key": app_key,
        "what_phrase": query,            # exact-phrase => far better relevance
        "results_per_page": 50, "sort_by": "date",
        "max_days_old": 2, "content-type": "application/json",
    }
    if location and not _wants_us(location):
        params["where"] = location       # US is already the country; other -> where
    url = f"https://api.adzuna.com/v1/api/jobs/{country}/search/1?" + urllib.parse.urlencode(params)
    out = []
    for j in get_json(url).get("results", []):
        posted = parse_iso(j.get("created"))
        if not in_window(posted, now, window_min):
            continue
        smin, smax = j.get("salary_min"), j.get("salary_max")
        salary = format_salary_range(smin, smax)
        title, desc = j.get("title", ""), j.get("description", "")
        loc = (j.get("location") or {}).get("display_name", "")
        out.append(job("adzuna", title, (j.get("company") or {}).get("display_name"),
                       loc, j.get("redirect_url"), salary=salary,
                       remote=_looks_remote(title, desc, loc), posted=posted,
                       no_sponsor=rejects_sponsorship(title, desc),
                       us_guaranteed=(country.lower() == "us"),
                       salary_min=smin, salary_max=smax, description=desc))
    return out


def _jsearch_date_posted(window_min):
    if window_min <= 1440:
        return "today"
    if window_min <= 4320:
        return "3days"
    if window_min <= 10080:
        return "week"
    return "month"


def fetch_jsearch(query, now, window_min, country="us", location=None, remote_only=False):
    # JSearch (OpenWeb Ninja via RapidAPI) aggregates Google for Jobs, so results
    # include LinkedIn / Indeed / Glassdoor / ZipRecruiter postings. It's an API,
    # so it runs reliably from the cloud — the scraping burden is on their side.
    key = cfg("RAPIDAPI_KEY")
    if not key:
        print("  [jsearch] skipped — set RAPIDAPI_KEY (free key at rapidapi.com/OpenWeb-Ninja jsearch)")
        return []
    if quota_exhausted("jsearch"):
        print("  [jsearch] skipped — quota was exhausted as of the last call; "
              "not retrying until it's likely reset (see state/rapidapi_quota.json)")
        return []
    # Bias the query itself toward remote when --remote-only, so Google-for-Jobs
    # returns remote roles instead of us filtering most of an onsite page away.
    # US scoping is handled by the country=us param, so we don't add "in USA".
    q = f"remote {query}" if remote_only else query
    if location and not _wants_us(location):
        q = f"{q} in {location}"
    # JSearch v5 param set (confirmed from the RapidAPI playground snippet):
    # query, num_pages, date_posted, country. No `page`, no `work_from_home` —
    # remote is filtered on our side via job_is_remote / work_arrangement.
    params = {
        "query": q,
        "num_pages": 1,           # 1 page (~10 jobs) keeps free-tier usage low
        "date_posted": _jsearch_date_posted(window_min),
        "country": country,
    }
    # JSearch v5 renamed the search endpoint to /search-v2 (classic used /search,
    # which now 404s). Override with JSEARCH_ENDPOINT if it changes again.
    endpoint = os.environ.get("JSEARCH_ENDPOINT", "/search-v2")
    url = f"https://jsearch.p.rapidapi.com{endpoint}?" + urllib.parse.urlencode(params)
    headers = {"X-RapidAPI-Key": key, "X-RapidAPI-Host": "jsearch.p.rapidapi.com",
               "User-Agent": "job-search-agent/1.0"}
    out = []
    # v5 /search-v2 may nest the job list differently than /job-details did.
    # Find the list of job objects wherever it lives, defensively.
    try:
        resp, resp_headers = get_json_with_headers(url, headers=headers)
    except RuntimeError as e:
        record_quota_from_error("jsearch", e)
        raise
    record_quota_from_success("jsearch", resp_headers)
    data = resp.get("data") if isinstance(resp, dict) else None
    if isinstance(data, list):
        rows = data
    elif isinstance(data, dict):
        rows = data.get("jobs") or data.get("results") or data.get("data") or []
    else:
        rows = resp.get("jobs") or resp.get("results") or [] if isinstance(resp, dict) else []
    for j in rows:
        if not isinstance(j, dict):
            continue
        posted = parse_iso(j.get("job_posted_at_datetime_utc")) or parse_epoch(j.get("job_posted_at_timestamp"))
        if not in_window(posted, now, window_min):
            continue
        title, desc = j.get("job_title", ""), j.get("job_description", "")
        loc = ", ".join(x for x in (j.get("job_city"), j.get("job_state"), j.get("job_country")) if x)
        smin, smax = j.get("job_min_salary"), j.get("job_max_salary")
        salary = format_salary_range(smin, smax)
        publisher = j.get("job_publisher", "")   # e.g. LinkedIn / Indeed / Glassdoor
        is_remote = (bool(j.get("job_is_remote"))
                     or str(j.get("work_arrangement", "")).lower() == "remote"
                     or _looks_remote(title, loc))
        out.append(job("jsearch", title, j.get("employer_name"), loc,
                       j.get("job_apply_link"), salary=salary,
                       tags=[publisher] if publisher else [],
                       remote=is_remote,
                       posted=posted, no_sponsor=rejects_sponsorship(title, desc),
                       us_guaranteed=(str(j.get("job_country", "")).lower() in ("us", "usa", "united states")),
                       salary_min=smin, salary_max=smax, description=desc))
    return out


def fetch_active_jobs_db(query, now, window_min, location=None):
    # Active Jobs DB (fantastic.jobs, via RapidAPI) — sources directly from
    # 200k+ employer ATS platforms (Greenhouse, Lever, Workday, Oracle Cloud
    # HCM, Ashby, iCIMS, BambooHR, Comeet, Dayforce, Rippling, ApplyToJob,
    # etc.), NOT aggregated from LinkedIn like most "job board" APIs — a real
    # domain-breakdown test (2026-09) showed 0 LinkedIn links across 100
    # results, vs. 90% LinkedIn for a comparable Techmap.io pull. Free tier
    # is only 250 jobs/month total though, so keep each pull small.
    key = cfg("RAPIDAPI_KEY")
    if not key:
        print("  [activejobsdb] skipped — set RAPIDAPI_KEY (same key as jsearch)")
        return []
    if quota_exhausted("activejobsdb"):
        print("  [activejobsdb] skipped — quota was exhausted as of the last call; "
              "not retrying until it's likely reset (see state/rapidapi_quota.json)")
        return []
    time_frame = "24h" if window_min <= 1440 else ("72h" if window_min <= 4320 else "7d")
    loc_q = f'"{location}"' if location else '"United States"'
    params = {
        "time_frame": time_frame,
        "limit": 50,        # free tier is only 250 jobs/month total — stay modest
        "offset": 0,
        "description_format": "text",
        "title": f'"{query}"',
        "location": loc_q,
    }
    host = "active-jobs-db.p.rapidapi.com"
    url = f"https://{host}/active-ats?" + urllib.parse.urlencode(params)
    headers = {"Content-Type": "application/json",
               "x-rapidapi-key": key, "x-rapidapi-host": host}
    try:
        resp, resp_headers = get_json_with_headers(url, headers=headers)
    except RuntimeError as e:
        record_quota_from_error("activejobsdb", e)
        raise
    record_quota_from_success("activejobsdb", resp_headers)
    rows = resp if isinstance(resp, list) else []
    out = []
    for j in rows:
        if not isinstance(j, dict):
            continue
        posted = parse_iso(j.get("date_posted"))
        if not in_window(posted, now, window_min):
            continue
        title = j.get("title", "")
        desc = j.get("description_text", "")
        loc_str = ", ".join(j.get("locations_derived") or []) or "—"
        # ai_salary_min/max_value come in whatever unit ai_salary_unit_text
        # says (YEAR/MONTH/WEEK/DAY/HOUR) — normalize to annual, or a $24/hr
        # intern rate reads as a $24/year salary and either gets wrongly
        # excluded by --min-salary or (worse, for a high hourly contract
        # rate) wrongly excluded from being flagged as well-paid.
        _UNIT_TO_ANNUAL = {"YEAR": 1, "MONTH": 12, "WEEK": 52, "DAY": 260, "HOUR": 2080}
        unit_mult = _UNIT_TO_ANNUAL.get(str(j.get("ai_salary_unit_text") or "YEAR").upper(), 1)
        smin_raw, smax_raw = j.get("ai_salary_min_value"), j.get("ai_salary_max_value")
        smin = smin_raw * unit_mult if smin_raw else None
        smax = smax_raw * unit_mult if smax_raw else None
        salary = format_salary_range(smin, smax)
        arrangement = (j.get("ai_work_arrangement") or "").lower()
        is_remote = "remote" in arrangement
        visa = str(j.get("ai_visa_sponsorship") or "").strip().lower()
        no_sponsor = visa in ("no", "false", "not offered", "none", "not available")
        out.append(job("activejobsdb", title, j.get("organization"), loc_str,
                       j.get("url"), salary=salary,
                       tags=[j.get("source")] if j.get("source") else [],
                       remote=is_remote, posted=posted,
                       no_sponsor=no_sponsor or rejects_sponsorship(title, desc),
                       us_guaranteed=("United States" in (j.get("countries_derived") or [])),
                       salary_min=smin, salary_max=smax, description=desc))
    return out


def fetch_jobicy(query, now, window_min):
    # Remote-only board with a geo=usa filter — ideal for US-remote roles.
    url = "https://jobicy.com/api/v2/remote-jobs?" + urllib.parse.urlencode(
        {"count": 50, "geo": "usa", "tag": query})
    out = []
    for j in get_json(url).get("jobs", []):
        posted = parse_iso(j.get("pubDate"))
        if not in_window(posted, now, window_min):
            continue
        title, desc = j.get("jobTitle", ""), j.get("jobExcerpt", "") + " " + j.get("jobDescription", "")
        geo = j.get("jobGeo", "")
        out.append(job("jobicy", title, j.get("companyName"), geo, j.get("url"),
                       tags=j.get("jobIndustry", []), remote=True, posted=posted,
                       no_sponsor=rejects_sponsorship(title, desc),
                       us_guaranteed=("usa" in geo.lower() or "united states" in geo.lower()),
                       description=desc))
    return out


def fetch_muse(query, now, window_min, location=None):
    key = cfg("MUSE_API_KEY")
    params = {"page": 0}
    if key:
        params["api_key"] = key
    url = "https://www.themuse.com/api/public/jobs?" + urllib.parse.urlencode(params)
    out = []
    for j in get_json(url).get("results", []):
        posted = parse_iso(j.get("publication_date"))
        if not in_window(posted, now, window_min):
            continue
        title = j.get("name", "")
        desc = j.get("contents", "")
        if not matches_query(query, title):
            continue
        locs = [l.get("name", "") for l in j.get("locations", [])]
        loc = ", ".join(locs)
        company = (j.get("company") or {}).get("name", "")
        out.append(job("muse", title, company, loc, (j.get("refs") or {}).get("landing_page"),
                       remote=_looks_remote(loc, title), posted=posted,
                       no_sponsor=rejects_sponsorship(title, desc), description=desc))
    return out


def fetch_remoteok(query, now, window_min):
    data = get_json("https://remoteok.com/api")
    out = []
    for j in data:
        if not isinstance(j, dict) or not j.get("id"):
            continue  # first element is a legal notice
        posted = parse_iso(j.get("date")) or parse_epoch(j.get("epoch"))
        if not in_window(posted, now, window_min):
            continue
        title = j.get("position", "") or j.get("title", "")
        desc = j.get("description", "")
        tags = j.get("tags", [])
        if not matches_query(query, title, desc, " ".join(tags)):
            continue
        loc = j.get("location", "") or "Remote"
        out.append(job("remoteok", title, j.get("company"), loc, j.get("url"),
                       tags=tags, remote=True, posted=posted,
                       no_sponsor=rejects_sponsorship(title, desc), description=desc))
    return out


def fetch_himalayas(query, now, window_min, max_pages=5):
    # Free, public, no API key/quota — https://himalayas.app/docs/remote-jobs-api
    # applicationLink points to Himalayas' own apply page (not a third-party
    # redirect gate like Adzuna's), so it should be reachable by Playwright.
    # The API returns a small page (~20 jobs) per call regardless of `limit`,
    # newest-first, so we page via `offset` until we're past the time window
    # or hit max_pages — a broad multi-industry board needs real depth to
    # surface enough data-engineering-titled roles to be worth having.
    out = []
    offset = 0
    for _ in range(max_pages):
        url = "https://himalayas.app/jobs/api?" + urllib.parse.urlencode(
            {"limit": 100, "offset": offset})
        data = get_json(url)
        rows = data.get("jobs", []) if isinstance(data, dict) else (data or [])
        if not rows:
            break
        page_had_in_window = False
        for j in rows:
            if not isinstance(j, dict):
                continue
            posted = parse_epoch(j.get("pubDate"))
            # Feed is newest-first: once a job is older than the window, every
            # later job on every later page is older still — stop paging.
            if posted and (now - posted).total_seconds() > window_min * 60:
                continue
            if not in_window(posted, now, window_min):
                continue
            page_had_in_window = True
            title = j.get("title", "")
            desc = j.get("description", "") or j.get("excerpt", "")
            if not matches_query(query, title, desc):
                continue
            loc_list = j.get("locationRestrictions") or []
            loc = ", ".join(loc_list) if loc_list else "Remote (Worldwide)"
            out.append(job("himalayas", title, j.get("companyName"), loc,
                           j.get("applicationLink") or j.get("guid"),
                           tags=j.get("categories", []), remote=True, posted=posted,
                           no_sponsor=rejects_sponsorship(title, desc),
                           salary_min=j.get("minSalary"), salary_max=j.get("maxSalary"),
                           description=desc))
        offset += len(rows)
        if not page_had_in_window:
            break  # this whole page was already outside the window — stop
    return out


def fetch_greenhouse(query, now, window_min, companies):
    out = []
    for slug in companies:
        url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"
        data = _safe(lambda u=url: get_json(u), f"greenhouse:{slug}")
        for j in (data.get("jobs", []) if isinstance(data, dict) else []):
            posted = parse_iso(j.get("updated_at"))
            if not in_window(posted, now, window_min):
                continue
            title = j.get("title", "")
            desc = j.get("content", "")
            if not matches_query(query, title):
                continue
            loc = (j.get("location") or {}).get("name", "")
            out.append(job(f"greenhouse:{slug}", title, slug, loc, j.get("absolute_url"),
                           remote=_looks_remote(title, loc), posted=posted,
                           no_sponsor=rejects_sponsorship(title, desc), description=desc))
    return out


def fetch_lever(query, now, window_min, companies):
    out = []
    for slug in companies:
        url = f"https://api.lever.co/v0/postings/{slug}?mode=json"
        for j in _safe(lambda u=url: get_json(u), f"lever:{slug}"):
            posted = parse_epoch(j.get("createdAt"), ms=True)
            if not in_window(posted, now, window_min):
                continue
            title = j.get("text", "")
            desc = j.get("descriptionPlain", "") or j.get("description", "")
            if not matches_query(query, title):
                continue
            loc = (j.get("categories") or {}).get("location", "")
            out.append(job(f"lever:{slug}", title, slug, loc, j.get("hostedUrl"),
                           remote=_looks_remote(title, loc), posted=posted,
                           no_sponsor=rejects_sponsorship(title, desc), description=desc))
    return out


def fetch_ashby(query, now, window_min, companies):
    out = []
    for slug in companies:
        url = f"https://api.ashbyhq.com/posting-api/job-board/{slug}"
        data = _safe(lambda u=url: get_json(u), f"ashby:{slug}")
        for j in (data.get("jobs", []) if isinstance(data, dict) else []):
            posted = parse_iso(j.get("publishedAt") or j.get("publishedDate"))
            if not in_window(posted, now, window_min):
                continue
            title = j.get("title", "")
            desc = j.get("descriptionPlain", "") or j.get("descriptionHtml", "")
            if not matches_query(query, title):
                continue
            loc = j.get("location", "") or ""
            out.append(job(f"ashby:{slug}", title, slug, loc,
                           j.get("jobUrl") or j.get("applyUrl"),
                           remote=bool(j.get("isRemote")) or _looks_remote(title, loc),
                           posted=posted, no_sponsor=rejects_sponsorship(title, desc), description=desc))
    return out


# --------------------------------------------------------------------------- #
# Dedup state
# --------------------------------------------------------------------------- #
def load_seen(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_seen(path, seen):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(seen, f, indent=0, sort_keys=True)


# --------------------------------------------------------------------------- #
# RapidAPI quota tracking (JSearch etc.) — persisted across runs so we don't
# keep hammering an exhausted monthly quota and printing a 429 every day.
# --------------------------------------------------------------------------- #
QUOTA_STATE_FILE = "state/rapidapi_quota.json"

# Common header spellings RapidAPI providers use for remaining-calls and
# reset-time. Different APIs on RapidAPI use different casings/names, so we
# check a handful rather than assuming one — headers are matched
# case-insensitively by the caller.
_QUOTA_REMAINING_HEADERS = ("x-ratelimit-requests-remaining", "x-ratelimit-remaining")
_QUOTA_RESET_HEADERS = ("x-ratelimit-requests-reset", "x-ratelimit-reset")


def _load_quota_state(service):
    try:
        with open(QUOTA_STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get(service, {}) if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_quota_state(service, remaining=None, reset_epoch=None):
    d = os.path.dirname(QUOTA_STATE_FILE)
    if d:
        os.makedirs(d, exist_ok=True)
    try:
        with open(QUOTA_STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            data = {}
    except Exception:
        data = {}
    entry = data.get(service, {})
    if remaining is not None:
        entry["remaining"] = remaining
    if reset_epoch is not None:
        entry["reset_epoch"] = reset_epoch
    entry["checked_at"] = datetime.now(timezone.utc).isoformat()
    data[service] = entry
    with open(QUOTA_STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=0, sort_keys=True)


def _extract_quota_from_headers(headers):
    """headers: dict of response headers (any casing). Returns
    (remaining_or_None, reset_epoch_or_None)."""
    lower = {k.lower(): v for k, v in (headers or {}).items()}
    remaining = None
    for h in _QUOTA_REMAINING_HEADERS:
        if h in lower:
            try:
                remaining = int(lower[h])
            except (TypeError, ValueError):
                pass
            break
    reset_epoch = None
    for h in _QUOTA_RESET_HEADERS:
        if h in lower:
            try:
                # RapidAPI's reset header is typically "seconds until reset"
                reset_epoch = time.time() + int(lower[h])
            except (TypeError, ValueError):
                pass
            break
    return remaining, reset_epoch


def quota_exhausted(service):
    """True if we already know (from a previous call's response headers)
    that this service's quota is at 0 and hasn't reset yet — skip calling it
    at all this run rather than repeat a guaranteed-to-fail request."""
    state = _load_quota_state(service)
    remaining = state.get("remaining")
    reset_epoch = state.get("reset_epoch")
    if remaining is None or remaining > 0:
        return False
    if reset_epoch and time.time() >= reset_epoch:
        return False  # reset window has passed — worth trying again
    if not reset_epoch:
        # No reset time reported — RapidAPI monthly quotas reset on a
        # billing-cycle date we don't know, so fall back to "worth retrying
        # once a day" rather than blocking indefinitely on a guess.
        checked_at = state.get("checked_at")
        try:
            last = datetime.fromisoformat(checked_at)
            if (datetime.now(timezone.utc) - last) > timedelta(hours=20):
                return False
        except Exception:
            return False
    return True


def record_quota_from_error(service, error):
    """Call on a caught request exception that may carry .headers/.code
    (see get_json_with_headers) to persist quota state for next time."""
    headers = getattr(error, "headers", None) or {}
    remaining, reset_epoch = _extract_quota_from_headers(headers)
    code = getattr(error, "code", None)
    if remaining is None and code == 429:
        # No usable header, but a 429 is itself proof we're at 0 — record
        # that so we back off even without a reset hint (see the 20h
        # fallback in quota_exhausted above).
        remaining = 0
    if remaining is not None or reset_epoch is not None:
        _save_quota_state(service, remaining=remaining, reset_epoch=reset_epoch)


def record_quota_from_success(service, headers):
    remaining, reset_epoch = _extract_quota_from_headers(headers)
    if remaining is not None or reset_epoch is not None:
        _save_quota_state(service, remaining=remaining, reset_epoch=reset_epoch)


def prune_seen(seen, now):
    cutoff = now - timedelta(days=PRUNE_DAYS)
    return {k: v for k, v in seen.items()
            if (parse_iso(v) is None or parse_iso(v) >= cutoff)}


def load_companies():
    if os.path.exists(COMPANIES_FILE):
        try:
            with open(COMPANIES_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            return {k: list(data.get(k, [])) for k in ("greenhouse", "lever", "ashby")}
        except Exception as e:
            print(f"  [companies] {e}; using defaults", file=sys.stderr)
    return DEFAULT_COMPANIES


# --------------------------------------------------------------------------- #
# Email digest
# --------------------------------------------------------------------------- #
def render_text(jobs, now):
    lines = []
    for j in jobs:
        posted = f"{age_str(j['posted'], now)}" if j["posted"] else "—"
        lines += [f"• {j['title']}   [{j['source']}]",
                  f"    {j['company']} — {j['location']} · posted {posted}"]
        if j["salary"] != "—":
            lines.append(f"    Salary: {j['salary']}")
        lines += [f"    {j['url']}", ""]
    return "\n".join(lines)


def render_html(jobs, now):
    rows = []
    for j in jobs:
        posted = age_str(j["posted"], now) if j["posted"] else "—"
        badges = " ".join(
            f'<span style="background:#eef;border-radius:4px;padding:1px 6px;font-size:12px">{b}</span>'
            for b in (["REMOTE"] if j["remote"] else []) +
                     (["VISA-TAGGED"] if "visa_sponsorship" in j["tags"] else []))
        salary = f'<div style="color:#555">💰 {j["salary"]}</div>' if j["salary"] != "—" else ""
        rows.append(f"""
        <div style="border:1px solid #e5e5e5;border-radius:8px;padding:12px 14px;margin:10px 0">
          <div style="font-size:16px;font-weight:600">
            <a href="{j['url']}" style="color:#1a56db;text-decoration:none">{j['title']}</a> {badges}
          </div>
          <div style="color:#333;margin-top:2px">{j['company']} — {j['location']}</div>
          <div style="color:#888;font-size:13px">via {j['source']} · posted {posted}</div>
          {salary}
        </div>""")
    return f"""<html><body style="font-family:-apple-system,Segoe UI,Roboto,sans-serif;max-width:680px;margin:auto">
      <h2 style="margin-bottom:0">New jobs — {len(jobs)}</h2>
      <div style="color:#888;font-size:13px">generated {now.strftime('%Y-%m-%d %H:%M UTC')}</div>
      {''.join(rows)}
      <div style="color:#aaa;font-size:12px;margin-top:16px">Compliant API sources only. No scraping.</div>
    </body></html>"""


def send_digest(jobs, now, query, label="Job Agent"):
    addr, pw = cfg("GMAIL_ADDRESS"), cfg("GMAIL_APP_PASSWORD")
    to = cfg("DIGEST_TO") or addr
    if not (addr and pw):
        print("  [email] skipped — set GMAIL_ADDRESS and GMAIL_APP_PASSWORD")
        return False
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"[{label}] {len(jobs)} new '{query}' job(s)"
    msg["From"], msg["To"] = addr, to
    msg.attach(MIMEText(render_text(jobs, now), "plain", "utf-8"))
    msg.attach(MIMEText(render_html(jobs, now), "html", "utf-8"))
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=SSL_CTX, timeout=TIMEOUT) as s:
            s.login(addr, pw)
            s.sendmail(addr, [to], msg.as_string())
        print(f"  [email] sent {len(jobs)} job(s) to {to}")
        return True
    except Exception as e:
        print(f"  [email] error: {e}", file=sys.stderr)
        return False


def print_jobs(jobs, new_ids, now):
    by_src = {}
    for j in jobs:
        by_src.setdefault(j["source"].split(":")[0], []).append(j)
    for src, items in by_src.items():
        print(f"\n{'=' * 70}\n{src.upper()} — {len(items)} in window\n{'=' * 70}")
        for i, j in enumerate(items, 1):
            new = "  *NEW*" if job_id(j) in new_ids else ""
            badges = ("  [REMOTE]" if j["remote"] else "") + \
                     ("  [VISA]" if "visa_sponsorship" in j["tags"] else "")
            print(f"[{i}] {j['title']}{badges}{new}")
            print(f"    {j['company']} — {j['location']}"
                  + (f" · {age_str(j['posted'], now)}" if j['posted'] else ""))
            if j["salary"] != "—":
                print(f"    Salary: {j['salary']}")
            print(f"    {j['url']}")


# --------------------------------------------------------------------------- #
def env_flag(name):
    return os.environ.get(name, "").lower() in ("1", "true", "yes")


def main():
    load_keys_json()
    load_dotenv()
    ap = argparse.ArgumentParser(description="Compliant multi-source job-search agent")
    ap.add_argument("query", nargs="?",
                    default=os.environ.get("JOB_QUERY", "data engineer,analytics engineer,etl developer"),
                    help="one role, or several comma-separated (each searched, then merged/deduped)")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--country", default=os.environ.get("ADZUNA_COUNTRY", "us"))
    ap.add_argument("--location", default=os.environ.get("JOB_LOCATION", "United States"),
                    help='location filter (default: United States). Pass "" for worldwide.')
    ap.add_argument("--remote-only", action="store_true", default=env_flag("REMOTE_ONLY"))
    ap.add_argument("--exclude-no-sponsorship", action="store_true",
                    default=env_flag("EXCLUDE_NO_SPONSORSHIP"))
    ap.add_argument("--min-salary", type=int,
                    default=int(os.environ["MIN_SALARY"]) if os.environ.get("MIN_SALARY") else None,
                    help="drop jobs whose listed salary is below this (jobs with no "
                         "salary listed are KEPT unless --require-salary)")
    ap.add_argument("--require-salary", action="store_true", default=env_flag("REQUIRE_SALARY"),
                    help="with --min-salary, also drop jobs that list no salary at all")
    ap.add_argument("--window-min", type=int, default=int(os.environ.get("WINDOW_MIN", WINDOW_MINUTES)))
    ap.add_argument("--loose", action="store_true", default=env_flag("LOOSE"),
                    help="disable the title-relevance filter (match anywhere, not just the title)")
    ap.add_argument("--title-terms", default=os.environ.get("TITLE_TERMS", ""),
                    help='extra title keywords that also count as a match, comma-separated '
                         '(e.g. "analytics engineer,etl,data platform,data pipeline")')
    ap.add_argument("--sources", default=os.environ.get("SOURCES", ",".join(DEFAULT_SOURCES)),
                    help="comma-separated subset of: " + ",".join(ALL_SOURCES)
                         + f" (default: {','.join(DEFAULT_SOURCES)} — company boards off)")
    ap.add_argument("--seen-file", default=os.environ.get("SEEN_FILE", DEFAULT_SEEN_FILE))
    ap.add_argument("--sponsors-file", default=os.environ.get("SPONSORS_FILE"),
                    help="keep only employers with H-1B sponsorship history listed in this "
                         "file (one employer per line, or a DOL LCA CSV)")
    ap.add_argument("--label", default=os.environ.get("LABEL", "Job Agent"),
                    help="name for this agent, shown in the email subject and banner")
    ap.add_argument("--no-email", action="store_true")
    ap.add_argument("--json-out", default=os.environ.get("JSON_OUT"),
                    help="write the in-window, filtered jobs to this JSON file "
                         "(feeds the job_filter_agent). Datetimes are ISO strings.")
    ap.add_argument("--seed", action="store_true",
                    help="mark all current jobs seen without emailing (first-run priming)")
    args = ap.parse_args()

    now = datetime.now(timezone.utc)
    win_from = now - timedelta(minutes=args.window_min)
    enabled = [s.strip() for s in args.sources.split(",") if s.strip()]
    companies = load_companies()
    sponsors = load_sponsors(args.sponsors_file)

    bits = [f"last {args.window_min}m"]
    if args.remote_only:
        bits.append("remote-only")
    if args.exclude_no_sponsorship:
        bits.append("exclude-no-sponsorship")
    if args.location:
        bits.append(f'location="{args.location}"')
    if args.min_salary:
        bits.append(f"min-salary=${args.min_salary:,}" + ("+known" if args.require_salary else ""))
    if sponsors:
        bits.append(f"sponsor-history ({len(sponsors[0])} employers)")
    elif args.sponsors_file:
        bits.append("sponsor-history (LIST NOT LOADED)")
    bits.append("title-match" if not args.loose else "loose(title off)")
    print(textwrap.dedent(f"""
        Query   : "{args.query}"
        Window  : {win_from.isoformat()} -> {now.isoformat()} (UTC)
        Filters : {', '.join(bits)}
        Sources : {', '.join(enabled)}
        Seen    : {args.seen_file}
    """).rstrip())

    # Support several comma-separated role titles: search each, title-match each
    # against ITS OWN query, then merge and dedup across queries.
    queries = [t.strip() for t in args.query.split(",") if t.strip()] or [args.query]
    w = args.window_min
    extra_terms = [t.strip() for t in args.title_terms.split(",") if t.strip()]

    def runners_for(q):
        return {
            "remotive": lambda: fetch_remotive(q, now, w),
            "arbeitnow": lambda: fetch_arbeitnow(q, now, w),
            "adzuna": lambda: fetch_adzuna(q, now, w, country=args.country,
                                           location=args.location, remote_only=args.remote_only),
            "jsearch": lambda: fetch_jsearch(q, now, w, country=args.country,
                                             location=args.location, remote_only=args.remote_only),
            "jobicy": lambda: fetch_jobicy(q, now, w),
            "muse": lambda: fetch_muse(q, now, w, location=args.location),
            "remoteok": lambda: fetch_remoteok(q, now, w),
            "himalayas": lambda: fetch_himalayas(q, now, w),
            "activejobsdb": lambda: fetch_active_jobs_db(q, now, w, location=args.location),
            "greenhouse": lambda: fetch_greenhouse(q, now, w, companies["greenhouse"]),
            "lever": lambda: fetch_lever(q, now, w, companies["lever"]),
            "ashby": lambda: fetch_ashby(q, now, w, companies["ashby"]),
        }

    valid = set(runners_for(queries[0]).keys())
    for name in enabled:
        if name not in valid:
            print(f"  [warn] unknown source '{name}'", file=sys.stderr)
    use = [n for n in enabled if n in valid]

    combined = {}
    for q in queries:
        runners = runners_for(q)
        qjobs = []
        for name in use:
            qjobs += _safe(runners[name], name)
        qfiltered = post_filter(qjobs, args.location, args.remote_only,
                                args.exclude_no_sponsorship, None,
                                query=q, loose=args.loose, extra_terms=extra_terms,
                                min_salary=args.min_salary, require_salary=args.require_salary,
                                sponsors=sponsors)
        for j in qfiltered:
            combined.setdefault(job_id(j), j)

    filtered = sorted(combined.values(),
                      key=lambda j: j["posted"] or datetime.min.replace(tzinfo=timezone.utc),
                      reverse=True)
    if args.limit is not None:
        filtered = filtered[:args.limit]

    # Export the filtered jobs for the downstream filter/scorer agent.
    if args.json_out:
        export = []
        for j in filtered:
            d = dict(j)
            d["posted"] = j["posted"].isoformat() if j["posted"] else None
            export.append(d)
        d_ = os.path.dirname(args.json_out)
        if d_:
            os.makedirs(d_, exist_ok=True)
        with open(args.json_out, "w", encoding="utf-8") as f:
            json.dump(export, f, indent=2)
        print(f"  [json] wrote {len(export)} filtered jobs -> {args.json_out}")

    # Dedup against seen.json
    seen = load_seen(args.seen_file)
    new_jobs, new_ids = [], set()
    for j in filtered:
        jid = job_id(j)
        if jid in seen or jid in new_ids:
            continue
        new_jobs.append(j)
        new_ids.add(jid)

    print_jobs(filtered, new_ids, now)
    print(f"\n{len(filtered)} in window · {len(new_jobs)} new (not seen before).")

    if args.seed:
        print("  [seed] priming seen.json — no email sent.")
    elif new_jobs and not args.no_email:
        send_digest(new_jobs, now, args.query, label=args.label)
    elif not new_jobs:
        print("  Nothing new to email.")

    iso_now = now.isoformat()
    for jid in new_ids:
        seen[jid] = iso_now
    save_seen(args.seen_file, prune_seen(seen, now))
    print(f"  [state] seen.json now tracks {len(load_seen(args.seen_file))} job(s).\n")


if __name__ == "__main__":
    main()
