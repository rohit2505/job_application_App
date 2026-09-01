#!/usr/bin/env python3
"""
techmap_test.py — standalone diagnostic script, NOT wired into job_search.py.

Hits Techmap.io's "Daily International Job Postings" API (on RapidAPI) with
the same kind of query job_search.py would use, and dumps the raw JSON so we
can eyeball whether it's worth adding as a real source (vs. Adzuna).

Requires:
  - RAPIDAPI_KEY already in keys.json / .env (same key used for JSearch) —
    BUT you must separately subscribe to this specific API on RapidAPI first
    (search "Daily International Job Postings" by Techmap, pick the free
    Basic plan: 250 requests/month). A RapidAPI key only works for APIs
    you've actually subscribed to — JSearch and this one are billed/limited
    independently even though the key is shared.

Usage:
  python3 techmap_test.py "data engineer" --country us --remote
"""
import argparse
import json
import os
import sys
import urllib.parse
import urllib.request

TIMEOUT = 30


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
    ap.add_argument("--country", default="us")
    ap.add_argument("--remote", action="store_true")
    ap.add_argument("--days", type=int, default=3, help="dateCreatedMin = today minus N days")
    ap.add_argument("--out", default="techmap_sample.json")
    args = ap.parse_args()

    import datetime
    date_min = (datetime.datetime.utcnow() - datetime.timedelta(days=args.days)).strftime("%Y-%m-%d")

    params = {
        "title": args.title,
        "countryCode": args.country,
        "dateCreatedMin": date_min,
        "page": 1,
    }
    if args.remote:
        params["workPlace"] = "remote"

    host = "daily-international-job-postings.p.rapidapi.com"
    url = f"https://{host}/api/v2/jobs/search?" + urllib.parse.urlencode(params)
    headers = {"X-RapidAPI-Key": key, "X-RapidAPI-Host": host}
    req = urllib.request.Request(url, headers=headers)

    print(f"GET {url}")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
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

    total = data.get("totalCount")
    results = data.get("result", data.get("results", []))
    print(f"\ntotalCount: {total}")
    print(f"results in this page: {len(results)}")
    for j in results[:5]:
        jl = j.get("jsonLD", {}) or {}
        print("-", j.get("title"), "@", j.get("company"),
              "|", j.get("city"), j.get("countryCode"),
              "|", jl.get("datePosted"), "|", jl.get("url"))


if __name__ == "__main__":
    main()
