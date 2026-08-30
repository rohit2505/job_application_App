#!/usr/bin/env python3
"""
build_sponsors.py — regenerate sponsors.txt from the official USCIS H-1B
Employer Data Hub (the authoritative, downloadable source that sites like
myvisajobs / h1bdata just repackage). No scraping.

SELF-THROTTLING: it stamps sponsors.txt with a generation date and only
re-downloads if that stamp is older than --ttl-hours (default 20). So a
scheduler can call it every run and it will actually refresh at most once a day
("first run of the day"). The stamp lives in the committed file, so it works
across GitHub's fresh checkouts (filesystem mtime would not).

Source URL: USCIS updates the data hub file periodically; the download link
changes by fiscal year, so it is NOT hardcoded. Set it once:
    SPONSORS_SOURCE_URL=<direct .csv link>   (env or --url)
Get the current link from:
    https://www.uscis.gov/tools/reports-and-studies/h-1b-employer-data-hub-files
The CSV has an employer column plus Initial/Continuing Approval counts; we keep
employers with at least one approval. If the URL is unset or the download fails,
the existing sponsors.txt is left untouched (never wiped).

Usage:
    python3 build_sponsors.py                 # refresh if stale
    python3 build_sponsors.py --force         # refresh now
    SPONSORS_SOURCE_URL=... python3 build_sponsors.py
"""

import argparse
import csv
import io
import os
import ssl
import sys
import tempfile
import urllib.request
from datetime import datetime, timezone

OUT_DEFAULT = "sponsors.txt"
STAMP = "# generated_utc:"
MIN_ROWS_OK = 500          # sanity floor; below this we assume a bad download
CHUNK = 1 << 16

# Always-include big sponsors, so the list is sane even before/without a download.
SEED = [
    "Google", "Alphabet", "Amazon", "Microsoft", "Meta", "Apple", "Netflix",
    "Nvidia", "Intel", "IBM", "Oracle", "Salesforce", "Adobe", "Cisco",
    "Qualcomm", "Uber", "Lyft", "Airbnb", "LinkedIn", "PayPal", "Snowflake",
    "Databricks", "Stripe", "Coinbase", "Robinhood", "Plaid", "Atlassian",
    "Datadog", "MongoDB", "Palantir", "Walmart", "JPMorgan Chase",
    "Goldman Sachs", "Morgan Stanley", "Citigroup", "Capital One", "Visa",
    "Mastercard", "BlackRock", "Bloomberg", "Two Sigma", "Citadel",
]

EMP_COLS = ("employer", "employer (petitioner) name", "employer name",
            "petitioner", "employer_name", "company", "petitioner_name")
INIT_COLS = ("initial approval", "initial approvals", "new employment approval")
CONT_COLS = ("continuing approval", "continuing approvals")


def ssl_ctx():
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def read_stamp(path):
    try:
        with open(path, encoding="utf-8") as f:
            for _ in range(5):
                line = f.readline()
                if line.startswith(STAMP):
                    return datetime.fromisoformat(line[len(STAMP):].strip())
    except Exception:
        pass
    return None


def is_fresh(path, ttl_hours):
    stamp = read_stamp(path)
    if not stamp:
        return False
    age_h = (datetime.now(timezone.utc) - stamp).total_seconds() / 3600
    return age_h < ttl_hours


def existing_names(path):
    names = set()
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    names.add(line)
    except Exception:
        pass
    return names


def download(url, dest):
    req = urllib.request.Request(url, headers={"User-Agent": "sponsors-builder/1.0"})
    with urllib.request.urlopen(req, timeout=120, context=ssl_ctx()) as resp, open(dest, "wb") as out:
        while True:
            chunk = resp.read(CHUNK)
            if not chunk:
                break
            out.write(chunk)


def _find(cols, candidates):
    for i, c in enumerate(cols):
        if c.strip().lower() in candidates:
            return i
    return None


def parse_csv(path):
    names = set()
    with open(path, newline="", encoding="utf-8", errors="ignore") as f:
        reader = csv.reader(f)
        header = next(reader, None)
        if not header:
            return names
        low = [h.strip().lower() for h in header]
        ei = _find(low, EMP_COLS)
        if ei is None:                      # fall back to first column
            ei = 0
        ii = _find(low, INIT_COLS)
        ci = _find(low, CONT_COLS)
        for row in reader:
            if ei >= len(row):
                continue
            emp = row[ei].strip().strip('"')
            if not emp:
                continue
            if ii is not None or ci is not None:
                def _n(idx):
                    if idx is None or idx >= len(row):
                        return 0
                    try:
                        return int(float(str(row[idx]).replace(",", "") or 0))
                    except Exception:
                        return 0
                if _n(ii) + _n(ci) <= 0:
                    continue
            names.add(emp)
    return names


def write_sponsors(path, names):
    now = datetime.now(timezone.utc).isoformat()
    lines = [f"{STAMP} {now}",
             "# H-1B sponsor employers (USCIS H-1B Employer Data Hub + seed).",
             "# Regenerated by build_sponsors.py. Matching ignores corp suffixes.",
             ""]
    lines += sorted(names, key=str.lower)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def main():
    ap = argparse.ArgumentParser(description="Rebuild sponsors.txt from USCIS H-1B data")
    ap.add_argument("--url", default=os.environ.get("SPONSORS_SOURCE_URL"),
                    help="a direct .csv URL from the USCIS H-1B Employer Data Hub Files "
                         "page, OR a path to a CSV you downloaded yourself "
                         "(e.g. ~/Downloads/h1b_datahubexport.csv)")
    ap.add_argument("--out", default=os.environ.get("SPONSORS_OUT", OUT_DEFAULT))
    ap.add_argument("--ttl-hours", type=float, default=float(os.environ.get("SPONSORS_TTL_HOURS", 20)))
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    if not args.force and is_fresh(args.out, args.ttl_hours):
        print(f"  [sponsors] {args.out} is fresh (< {args.ttl_hours}h) — skipping refresh.")
        return

    if not args.url:
        print("  [sponsors] SPONSORS_SOURCE_URL not set — keeping existing "
              f"{args.out}. Get the CSV link from the USCIS H-1B Employer Data "
              "Hub Files page and set SPONSORS_SOURCE_URL to enable auto-refresh.")
        return

    src = args.url
    is_remote = "://" in src
    tmp = os.path.join(tempfile.gettempdir(), "uscis_h1b_datahub.csv")
    try:
        if is_remote:
            print(f"  [sponsors] downloading {src} ...")
            download(src, tmp)
            path = tmp
        else:
            path = os.path.expanduser(src)   # a local CSV you downloaded yourself
            if not os.path.exists(path):
                raise FileNotFoundError(path)
            print(f"  [sponsors] reading local CSV {path} ...")
        names = parse_csv(path)
    except Exception as e:
        print(f"  [sponsors] download/parse failed ({e}) — keeping existing {args.out}.",
              file=sys.stderr)
        return
    finally:
        if is_remote:
            try:
                os.remove(tmp)
            except Exception:
                pass

    if len(names) < MIN_ROWS_OK:
        print(f"  [sponsors] only {len(names)} employers parsed (< {MIN_ROWS_OK}) — "
              f"looks wrong, keeping existing {args.out}.", file=sys.stderr)
        return

    names |= set(SEED)
    names |= existing_names(args.out)   # never lose anything already curated
    write_sponsors(args.out, names)
    print(f"  [sponsors] wrote {len(names):,} employers -> {args.out}")


if __name__ == "__main__":
    main()
