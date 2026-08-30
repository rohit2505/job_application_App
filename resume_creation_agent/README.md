# resume_creation_agent — stage 3: tailor resumes + cover letters

Reads the **scored shortlist** from the filter agent and, for each top job,
produces a **truthfully tailored resume** (`resume.docx`) and a **cover letter**
(`cover_letter.docx`), organized one folder per job under `output/`.

**Truthful by design:** it only reorders, rephrases, and re-emphasises what's
already in your base resume, mirroring each job's terminology for skills you
genuinely have. It never invents skills, tools, employers, dates, or metrics.

## The full pipeline

```bash
# Stage 1 — fetch (in job_Search)
cd ../job_Search
python3 job_search.py "data engineer,analytics engineer,etl developer" \
    --remote-only --location "United States" --window-min 4320 \
    --no-email --json-out ../job_filter_agent/jobs.json

# Stage 2 — filter + score, and EXPORT the shortlist (in job_filter_agent)
cd ../job_filter_agent
python3 filter_agent.py --min-score 75 --model claude-sonnet-4-5-20250929 \
    --no-email --scored-out scored.json

# Stage 3 — tailor resumes + cover letters (here)
cd ../resume_creation_agent
pip install -r requirements.txt
python3 resume_agent.py --top 5
```

Output lands in `output/<company>__<title>/` with `resume.docx`, `resume.md`,
and `cover_letter.docx`.

## Automated mode

**Batch (fully automated, default):** `python3 resume_agent.py --top 5` — silently
audits each job against the base resume, handles gaps conservatively using existing
resume facts, and creates the tailored resume and cover letter without questions.
It uses the editable rules in `recruiter_prompt.md`.

The agent performs its recruiter audit internally and writes the final documents
directly. Edit `recruiter_prompt.md` to change its automated tailoring rules.

## Setup

- Uses the master `keys.json` at the project root (same `ANTHROPIC_API_KEY`).
- Your base resume is **autodetected** from `../job_filter_agent/` (your
  `rohit-nimmakuri-resume.docx`) — or pass `--resume /path/to/resume.docx`.

## Flags

- `--top 5` — tailor at most N top-ranked jobs.
- `--min-score 80` — only tailor jobs at/above this score.
- `--regenerate` — redo everything (default skips jobs already tailored, tracked in `state/done.json`).
- `--model ...` — defaults to `claude-sonnet-4-5-20250929` (better writing).
- `--scored path` / `--out dir` — override the input shortlist / output folder.

## Notes

- Review every tailored resume before sending — it's a strong draft grounded in
  your real resume, but you're the final check on accuracy and tone.
- `output/` and any resume copies are gitignored (personal).
