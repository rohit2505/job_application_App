#!/usr/bin/env python3
"""
activejobsdb_test.py — standalone diagnostic script (NOT wired into
job_search.py) for fantastic.jobs' "Active Jobs DB" API on RapidAPI.

Unlike Techmap (dominated by LinkedIn re-posts — see techmap_test.py's
findings), this one claims to source directly from ATS platforms
(Greenhouse, Lever, Workday, Ashby, SmartRecruiters, Workable, etc.) —
exactly the kind of direct-apply links this project actually needs.
Verify that claim with real data before adding it as a real source or
spending money on it.

Requires:
  - RAPIDAPI_KEY already in keys.json / .env (same key used for JSearch) —
    BUT you must separately subscribe to THIS specific API on RapidAPI
    first (search "Active Jobs DB" by fantastic.jobs). Free trial: 7 days,
    500 jobs/week, 50 requests/week, no card required.

Usage:
  python3 activejobsdb_test.py "data engineer" --location "United States" --domains
"""
import argparse
import json
import os
import ssl
import sys
import urllib.parse
import urllib.request

TIMEOUT = 30


def _ssl():
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


SSL_CTX = _ssl()


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
    for k, v in json.load(open(p, encoding="utf-8")).items():
        if not k.startswith("_") and isinstance(v, str) and v.strip():
            os.environ.setdefault(k.strip(), v.strip())


def load_dotenv(fn=".env"):
    p = _find_up(fn)
    if not p:
        return
    for line in open(p, encoding="utf-8"):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def main():
    load_keys_json()
    load_dotenv()
    key = os.environ.get("RAPIDAPI_KEY", "").strip()
    if not key:
        sys.exit("ERROR: RAPIDAPI_KEY not set in keys.json/.env")

    ap = argparse.ArgumentParser()
    ap.add_argument("title")
    ap.add_argument("--location", default="United States")
    ap.add_argument("--remote", action="store_true")
    ap.add_argument("--limit", type=int, default=50)
    ap.add_argument("--out", default="activejobsdb_sample.json")
    ap.add_argument("--domains", action="store_true",
                     help="print a tally of apply-URL hostnames across the pulled results")
    args = ap.parse_args()

    params = {
        "title_filter": args.title,
        "location_filter": args.location,
        "limit": args.limit,
        "offset": 0,
        "description_type": "text",
    }
    if args.remote:
        params["ai_work_arrangement_filter"] = "remote"

    host = "active-jobs-db.p.rapidapi.com"
    endpoint = "/active-ats-7d"  # last-7-days feed of active ATS postings
    url = f"https://{host}{endpoint}?" + urllib.parse.urlencode(params)
    headers = {"X-RapidAPI-Key": key, "X-RapidAPI-Host": host}
    req = urllib.request.Request(url, headers=headers)

    print(f"GET {url}")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT, context=SSL_CTX) as r:
            status = r.status
            raw = r.read().decode()
    except urllib.error.HTTPError as e:
        print(f"HTTP {e.code} {e.reason}")
        print(e.read().decode(errors="replace")[:2000])
        sys.exit(1)
    except Exception as e:
        sys.exit(f"request error: {type(e).__name__}: {e}")

    print(f"status: {status}, bytes: {len(raw)}")
    data = json.loads(raw)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    print(f"full response saved to {args.out}")

    # response shape isn't confirmed yet — try the common possibilities
    results = data if isinstance(data, list) else (
        data.get("result") or data.get("results") or data.get("jobs") or [])
    print(f"\nresults returned: {len(results)}")
    for j in results[:8]:
        if not isinstance(j, dict):
            continue
        print("-", j.get("title") or j.get("job_title"), "@",
              j.get("organization") or j.get("company") or j.get("company_name"),
              "|", j.get("location") or j.get("locations_derived") or j.get("location_filter"),
              "|", j.get("date_posted") or j.get("date_created") or j.get("posted_at"),
              "|", j.get("url") or j.get("application_url") or j.get("apply_url") or j.get("job_url"))

    if args.domains and results:
        from collections import Counter
        hosts = Counter()
        for j in results:
            if not isinstance(j, dict):
                continue
            u = (j.get("url") or j.get("application_url") or j.get("apply_url")
                 or j.get("job_url") or "")
            try:
                host_name = urllib.parse.urlparse(u).hostname or "(none)"
            except Exception:
                host_name = "(unparseable)"
            hosts[host_name] += 1
        print("\nhostname breakdown:")
        for h, c in hosts.most_common(30):
            print(f"  {c:4d}  {h}")


if __name__ == "__main__":
    main()
