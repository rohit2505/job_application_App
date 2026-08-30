# job_application_App — compliant job-search agent

A job-search agent that queries **official / public job APIs only** (no
scraping, no LinkedIn/Indeed logins), keeps roles matching your filters, dedups
against jobs it has already emailed, and sends you the new ones. Runs for free
on GitHub Actions.

## Two agents (same engine, different profiles)

One script (`job_search.py`) runs two independent profiles — each with its own
`--seen-file`, email `--label`, and workflow, so they never clash:

1. **Main** (`.github/workflows/job-search.yml`) — US, remote-only,
   sponsorship-JD-friendly `data engineer` roles. Broad daily coverage.
2. **Sponsored DE** (`.github/workflows/job-search-sponsored.yml`) — a
   high-signal stream: US + remote + **salary ≥ 160k** + **employer has H-1B
   sponsorship history** (`sponsors.txt`). Fewer, higher-quality hits.

Run them locally:

```bash
# Main
python3 job_search.py "data engineer" --remote-only --location "United States" --exclude-no-sponsorship --window-min 1440 --no-email
# Sponsored DE
python3 job_search.py "data engineer" --remote-only --location "United States" --exclude-no-sponsorship \
  --min-salary 160000 --sponsors-file sponsors.txt --seen-file state/seen_sponsored.json --label "Sponsored DE" --window-min 1440 --no-email
```

The sponsor filter keeps only employers listed in `sponsors.txt` (a starter list
of ~150 frequent H-1B sponsors). It's not exhaustive — see `SETUP_KEYS.md` →
**Full sponsor list** to load the official DOL LCA data for complete coverage.

## Sources

| Source | Key needed | Notes |
|---|---|---|
| Remotive | no | remote-only board |
| Arbeitnow | no | remote flag + visa tags |
| Adzuna | free key | broad market + salary; phrase-matched for relevance |
| **JSearch** | free RapidAPI key | **big-board coverage — LinkedIn / Indeed / Glassdoor / ZipRecruiter** via Google for Jobs |
| Jobicy | no | remote board with a US geo filter |
| The Muse | no (optional key) | large general board |
| RemoteOK | no | remote-only board |
| Greenhouse / Lever / Ashby | no | apply-direct company boards — edit `companies.json` |

JSearch is what fills the "I only see these on LinkedIn/Indeed" gap. It's an API
(the scraping burden is on their side), so it runs reliably from GitHub. The
free RapidAPI tier is ~200 requests/month — enough for a run every ~3 hours,
which is why the workflow is scheduled every 3h rather than hourly. Each job
shows which board it came from. Every other source still runs regardless of
whether you set up JSearch.

## How it decides what to send

1. **Time window** — each run looks back `--window-min` minutes (the workflow uses
   ~25 h) so a skipped run or coarse timestamps can't lose jobs.
2. **Filters** — `--remote-only`, `--location "United States"` (US-aware: matches
   USA / U.S. / worldwide-remote / Adzuna-US), and `--exclude-no-sponsorship`
   (drops only JDs that *explicitly* rule out sponsorship — never requires a
   positive "we sponsor" statement).
3. **Dedup** — `state/seen.json` guarantees each job is emailed exactly once.
4. **Email** — a Gmail digest of only the new jobs.

## Run locally

```bash
pip install -r requirements.txt
cp .env.example .env      # fill in your keys; .env is gitignored
python3 job_search.py "data engineer" --window-min 1440 --no-email   # dry run, wide window
```

## Run on GitHub (hourly, free)

1. Push this repo (private recommended).
2. Add repository **Secrets** (Settings → Secrets and variables → Actions):
   `ADZUNA_APP_ID`, `ADZUNA_APP_KEY`, `RAPIDAPI_KEY` (JSearch), `GMAIL_ADDRESS`,
   `GMAIL_APP_PASSWORD` (optional: `DIGEST_TO`).
3. The workflow `.github/workflows/job-search.yml` runs every hour, emails new
   jobs, and commits the updated `state/seen.json` back with `[skip ci]`.
4. First run: trigger it manually from the **Actions** tab (workflow_dispatch).
   To avoid a large first email, you can prime the state once locally with
   `python3 job_search.py --seed`.

`GMAIL_APP_PASSWORD` is a 16-char Google **App Password** (needs 2-Step
Verification on), not your normal password.

## Customize

- **Target companies**: edit `companies.json` (Greenhouse/Lever/Ashby slugs).
- **Search terms / filters**: edit the flags in the workflow, or set env vars in `.env`.
- **Sources**: `--sources remotive,adzuna,jobicy` to run a subset.
