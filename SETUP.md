# One-time setup: run this pipeline from GitHub instead of your Mac

## 1. Move the workflow file into place and push

The workflow file couldn't be written directly into `.github/workflows/` for
you, so run this once from the `job application app` folder:

```
mkdir -p .github/workflows
mv _github_setup/job-pipeline.yml .github/workflows/job-pipeline.yml
mv _github_setup/SETUP.md .github/workflows/SETUP.md
rmdir _github_setup

git init                              # skip if already a git repo
git add .
git commit -m "Job search pipeline"
git branch -M main
git remote add origin https://github.com/rohit2505/job_application_App.git
git push -u origin main
```

(If the remote already exists from before, use `git remote set-url origin
https://github.com/rohit2505/job_application_App.git` instead of `add`.)

Double check `keys.json` and any `.env` file are NOT committed — a
`.gitignore` excluding them is already in this folder.

## 2. Add your API keys as GitHub secrets

In your repo on GitHub: **Settings -> Secrets and variables -> Actions -> New
repository secret.** Add each of these (values from your local `keys.json`):

- `ADZUNA_APP_ID`
- `ADZUNA_APP_KEY`
- `RAPIDAPI_KEY`
- `MUSE_API_KEY`
- `ANTHROPIC_API_KEY`
- `GMAIL_ADDRESS`
- `GMAIL_APP_PASSWORD`
- `DIGEST_TO`

## 3. Set up the Gmail label/filter (do this once)

Every email the pipeline sends now starts with the tag `[JobAgent]` in the
subject. Create one Gmail filter so they all land in their own label instead
of your main inbox:

1. In Gmail, click the search bar's filter icon (or go to **Settings -> Filters
   and Blocked Addresses -> Create a new filter**).
2. In **Subject**, enter: `[JobAgent]`
3. Click **Create filter**.
4. Check **Apply the label**, then **New label...** and name it something like
   `Job Applications`.
5. Optionally also check **Skip the Inbox (Archive it)** and **Also apply
   filter to matching conversations** to retroactively label existing ones.
6. Click **Create filter**.

From then on, every fetch/score/tailor run's email (including the tailored
resume + cover letter for each job) lands under that one label automatically.

## 4. Turn on the schedule

`.github/workflows/job-pipeline.yml` runs daily at 13:00 UTC and commits its
state back to the repo (so it won't re-notify you about jobs it already
found). Adjust the cron line or the `JOB_QUERY` / `JOB_LOCATION` /
`MIN_SALARY` env values at the top of that file to taste. You can also
trigger a run immediately from the GitHub UI: **Actions -> Job Search
Pipeline -> Run workflow.**

## What this does NOT do

It does not submit the application to the employer for you. Most job postings
link out to an ATS portal (Workday, Greenhouse, LinkedIn, etc.) with no direct
application email address to send to, so that last click still needs to be
you. What you get automatically: the job found, scored, and a tailored resume
+ cover letter emailed to you (tagged and filed under your label) so it's
ready to submit and archived for interview prep afterward.
