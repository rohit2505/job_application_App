# Getting your keys & moving to GitHub Secrets

This agent needs a few credentials. You can run **locally** now with keys pasted
into the code (or a `.env` file), and switch to **GitHub Secrets** later when you
automate. The variable names are identical in both places, so nothing in the
code changes when you migrate.

## The keys at a glance

| Variable | Needed for | Cost | Required? |
|---|---|---|---|
| `ADZUNA_APP_ID` + `ADZUNA_APP_KEY` | Adzuna source (broad market + salary) | Free | Optional (source skipped if unset) |
| `RAPIDAPI_KEY` | JSearch source (LinkedIn/Indeed/Glassdoor) | Free tier, then ~$10–30/mo | Optional (source skipped if unset) |
| `GMAIL_ADDRESS` + `GMAIL_APP_PASSWORD` | Emailing the digest | Free | Required for email (else it prints only) |
| `DIGEST_TO` | Send digest to a different address | Free | Optional (defaults to `GMAIL_ADDRESS`) |
| `MUSE_API_KEY` | Higher The Muse rate limit | Free | Optional |

Every source is independent — if a key is missing, only that one source is
skipped and the rest still run.

---

## How to get each key

### 1. Adzuna (`ADZUNA_APP_ID`, `ADZUNA_APP_KEY`)
1. Go to https://developer.adzuna.com/ and click **Sign up** (or **Register**).
2. Confirm your email and sign in.
3. Open your dashboard — you'll see an **Application ID** and an **Application Key**.
4. `ADZUNA_APP_ID` = Application ID, `ADZUNA_APP_KEY` = Application Key.

### 2. JSearch / RapidAPI (`RAPIDAPI_KEY`) — the big-board coverage
1. Create a free account at https://rapidapi.com/ .
2. Open the JSearch API page: search "JSearch" (by **OpenWeb Ninja**), or go to
   https://rapidapi.com/OpenWeb-Ninja/api/jsearch .
3. Click **Subscribe to Test** and choose the **Basic (Free)** plan
   (~200 requests/month — enough for a run every ~3 hours).
4. On any endpoint's **Code Snippets** panel, copy the value of the
   `X-RapidAPI-Key` header. That string is your `RAPIDAPI_KEY`.

> The workflow is scheduled every 3 hours to stay within the free tier. To run
> hourly, upgrade JSearch to a paid plan and change the cron in
> `.github/workflows/job-search.yml` to `5 * * * *`.

### 3. Gmail App Password (`GMAIL_ADDRESS`, `GMAIL_APP_PASSWORD`)
This is **not** your normal Gmail password — it's a 16-character app password.
1. Turn on **2-Step Verification**: https://myaccount.google.com/security → 2-Step Verification.
2. Go to **App passwords**: https://myaccount.google.com/apppasswords .
3. Enter a name like `job-agent` and click **Create**.
4. Google shows a 16-character code (e.g. `abcd efgh ijkl mnop`). Copy it and
   remove the spaces → that's `GMAIL_APP_PASSWORD`. `GMAIL_ADDRESS` is your
   normal Gmail address.

---

## Running locally (now)

Two ways — pick one:

**A. `.env` file (recommended — safe).** Copy `.env.example` to `.env`, paste
your keys, and run. `.env` is gitignored, so it can never be pushed by accident.

```bash
cp .env.example .env      # then edit .env and paste your keys
pip install -r requirements.txt
python3 job_search.py "data engineer" --window-min 1440 --no-email   # dry run
```

**B. In-code `LOCAL_KEYS` block.** Open `job_search.py`, find the `LOCAL_KEYS`
block near the top, and paste your keys between the quotes.

> ⚠️ `job_search.py` is tracked by git. If you paste real keys there and push,
> they go public. Blank them back to `""` before `git push`, or just use the
> `.env` method above.

Priority order is: real environment variable → `.env` → `LOCAL_KEYS`.

---

## Full sponsor list (auto-refreshed) — `build_sponsors.py`

`sponsors.txt` ships as a **starter list** of ~150 big sponsors. For complete
coverage, `build_sponsors.py` regenerates it from the official **USCIS H-1B
Employer Data Hub** (the authoritative source that myvisajobs / h1bdata just
repackage — no scraping). It keeps employers with at least one H-1B approval and
unions in a seed of big names.

**One-time: set the source URL.** USCIS changes the download link each fiscal
year, so it isn't hardcoded.

1. Open the **H-1B Employer Data Hub Files** page:
   https://www.uscis.gov/tools/reports-and-studies/h-1b-employer-data-hub-files
2. Copy the direct **.csv** link for the most recent fiscal year.
3. Set it as `SPONSORS_SOURCE_URL` — locally in `.env`, and on GitHub as a
   repository **Secret** of the same name.

**Run it** (replace the placeholder with your real link — don't paste it literally):
```bash
SPONSORS_SOURCE_URL="https://www.uscis.gov/.../h1b_datahubexport-2024.csv" python3 build_sponsors.py --force
```
Or, easiest — **download the CSV in your browser** from the data hub files page,
then point at the local file (no URL needed):
```bash
python3 build_sponsors.py --url ~/Downloads/h1b_datahubexport.csv --force
```

**Self-throttling / daily refresh:** it stamps `sponsors.txt` with a generation
date and only re-downloads if that's older than `--ttl-hours` (default 20). The
main workflow calls it on **every** run, but it actually downloads at most once a
day — effectively the first run of the day — and commits the refreshed
`sponsors.txt` back. If `SPONSORS_SOURCE_URL` is unset or the download fails, the
existing `sponsors.txt` is left untouched (never wiped).

> Note: USCIS updates this data only a few times a year, so a ~daily check is
> already generous — you can raise `SPONSORS_TTL_HOURS` if you want fewer downloads.

## Moving to GitHub Secrets (later)

When you're ready to automate, put the keys in the repo instead of the code:

1. Push the repo (keep it **private**).
2. In GitHub: **Settings → Secrets and variables → Actions → New repository secret**.
3. Add each one, using these **exact names**:
   - `ADZUNA_APP_ID`
   - `ADZUNA_APP_KEY`
   - `RAPIDAPI_KEY`
   - `GMAIL_ADDRESS`
   - `GMAIL_APP_PASSWORD`
   - `DIGEST_TO` (optional)
   - `SPONSORS_SOURCE_URL` (optional — enables the daily sponsor-list refresh)
4. The workflow already reads them (see the `env:` block in
   `.github/workflows/job-search.yml`). No code change needed.
5. **Before pushing**: make sure `LOCAL_KEYS` in `job_search.py` is blank and you
   never committed a real `.env`. (If you ever did commit a key, rotate it — treat
   it as compromised.)
6. Open the **Actions** tab and run the workflow once manually to confirm the
   email arrives; after that it runs on schedule.
