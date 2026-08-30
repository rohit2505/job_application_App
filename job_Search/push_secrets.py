#!/usr/bin/env python3
"""
push_secrets.py — import keys.json into GitHub Actions Secrets (via the gh CLI).

This is the SECURE way to get your keys onto GitHub: keys.json stays LOCAL and
gitignored (never committed), and this pushes each value straight into the repo's
encrypted Secrets. So there is no secret file living in the repo to delete later.

Prereqs:
  - gh CLI installed and authenticated:  gh auth login
  - run this from inside the repo (job_Search), OR pass --repo owner/name
Usage:
  python3 push_secrets.py                 # infer repo from the current git remote
  python3 push_secrets.py --repo rohit2505/job_application_App
  python3 push_secrets.py --dry-run       # show what would be set, set nothing
"""

import argparse
import json
import os
import subprocess
import sys


def find_keys():
    for p in ("keys.json", os.path.join("..", "keys.json"),
              os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "keys.json")):
        if os.path.exists(p):
            return p
    return None


def main():
    ap = argparse.ArgumentParser(description="Import keys.json into GitHub Secrets")
    ap.add_argument("--repo", default=None, help="owner/name (default: infer from git remote)")
    ap.add_argument("--keys", default=None, help="path to keys.json (default: auto-find)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if subprocess.run(["which", "gh"], capture_output=True).returncode != 0:
        sys.exit("ERROR: GitHub CLI 'gh' not found. Install it and run 'gh auth login'.")

    path = args.keys or find_keys()
    if not path:
        sys.exit("ERROR: keys.json not found (looked in ./ and ../).")
    with open(path, encoding="utf-8") as f:
        data = json.load(f)

    items = [(k, v) for k, v in data.items()
             if not k.startswith("_") and isinstance(v, str) and v.strip()]
    if not items:
        sys.exit("Nothing to push — every value in keys.json is empty.")

    print(f"Importing {len(items)} secret(s) from {path}"
          + (f" into {args.repo}" if args.repo else " (repo inferred from git remote)") + ":")
    for k, v in items:
        base = ["gh", "secret", "set", k]
        if args.repo:
            base += ["--repo", args.repo]
        if args.dry_run:
            print(f"  [dry-run] would set {k}  ({len(v)} chars)")
            continue
        r = subprocess.run(base, input=v.encode("utf-8"), capture_output=True)
        if r.returncode == 0:
            print(f"  set {k}")
        else:
            print(f"  FAILED {k}: {r.stderr.decode().strip()}", file=sys.stderr)
    if not args.dry_run:
        print("Done. Your workflows will read these as env vars — no secret file in the repo.")


if __name__ == "__main__":
    main()
