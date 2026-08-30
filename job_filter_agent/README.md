# job_filter_agent — filter + resume-score stage

Stage 2 of the job pipeline. The fetcher (`../job_Search/job_search.py`) pulls
jobs and filters only on **remote / recency / role / salary**, then exports them
as JSON. This agent takes that JSON and does the heavier, personal filtering:

1. **Sponsorship** — drops jobs whose JD rules out sponsorship, and keeps only
   employers with an H-1B sponsorship history (`../job_Search/sponsors.txt`).
2. **Direct employers only** — drops staffing / consulting / outsourcing firms
   (bodyshops & agencies), both via a `staffing_blocklist.txt` and via Claude's
   employer-type classification (catches "our client / contract" postings). This
   intentionally overrides the sponsor list, so e.g. Cognizant/Infosys are dropped
   even though they sponsor.
3. **Resume fit** — asks **Claude** to score each remaining job 0–100 against
   your resume, with a one-line reason.
4. Keeps `score >= --min-score`, ranks best-first, and emails / prints the shortlist.

It dedups against its own `state/seen_scored.json` and records each score, so it
never pays to score the same job twice.

## Setup

1. `pip install -r requirements.txt`
2. Put your resume here as **`resume.docx`** (also supports .md / .txt / .pdf).
   It's autodetected and gitignored, so it never gets committed. `.docx` is read
   natively — no extra install. (`.pdf` needs `pip install pypdf`.)
3. Put your Anthropic key in a `.env` (copy `.env.example`) or the `LOCAL_KEYS`
   block at the top of `filter_agent.py`.

## Run (two stages)

```bash
# Stage 1 — fetch (in ../job_Search), export jobs, no filtering beyond remote/recency/role
cd ../job_Search
python3 job_search.py "data engineer" --remote-only --location "United States" \
    --window-min 1440 --no-email --json-out ../job_filter_agent/jobs.json

# Stage 2 — filter + score (here)
cd ../job_filter_agent
python3 filter_agent.py --min-score 70 --no-email          # prints ranked shortlist
```

Drop `--no-email` to get the Gmail digest (scored + ranked). The email subject is
`[Scored DE] …`, separate from the fetcher's digests.

## Useful flags

- `--min-score 80` — stricter fit bar.
- `--any-company` — skip the sponsor-history filter (score everything).
- `--keep-no-sponsor` — keep jobs whose JD rules out sponsorship.
- `--max-calls 40` — cap Claude calls per run (cost guard; default 60).
- `--resume path` / `--jobs path` / `--sponsors-file path` — override locations.

## Cost

One Claude call per *newly-seen* job that passes the sponsorship filters
(dedup + the sponsor prefilter keep this small). The default model
(`claude-3-5-haiku-latest`) is inexpensive; `--max-calls` caps spend per run.

## Notes

- Sponsorship history uses the same `sponsors.txt` as the fetcher (a starter list
  of ~150 big sponsors). Load the full DOL list for complete coverage — see
  `../job_Search/SETUP_KEYS.md` → "Full sponsor list".
- `jobs.json` is transient (gitignored). `state/seen_scored.json` is kept so
  dedup survives across runs.
