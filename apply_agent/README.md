# apply_agent — stage 4: notify + deliver (you apply from your phone)

Runs in the cloud (GitHub). For each **new top-scored** job it:

- **Emails** you the apply link + the **tailored resume attached** (guaranteed —
  no WhatsApp window rules).
- Sends a **WhatsApp** ping with the apply link (best-effort mobile nudge).

You tap the link and apply from your phone. Human effort = the apply itself; the
system does everything up to it.

## Why email is the backbone

WhatsApp only lets a business send free-form messages within **24h of your last
message to it**. So WhatsApp is the *convenience* channel; **email guarantees you
never miss a job**. If WhatsApp can't deliver (window closed), the job is already
in your email and the agent emails a "reactivate WhatsApp" note.

## Keep WhatsApp alerts live

```bash
python3 apply_notifier.py --keepalive   # schedule ~every 20h; reply to it to reset the window
```
Reply to the nudge and the 24h window resets. Forget, and nothing breaks — you
still get everything by email.

## Run

```bash
# after fetch -> filter (--scored-out) -> resume agent (tailor)
pip install -r requirements.txt
python3 apply_notifier.py --min-score 75
```

Reads `../job_filter_agent/scored.json` and the tailored resumes in
`../resume_creation_agent/output/`. Dedups via `state/notified.json`.

## Setup (Twilio WhatsApp sandbox)

1. twilio.com → Messaging → **Try WhatsApp** → note the sandbox number + `join <code>`.
2. From your phone's WhatsApp, send `join <code>` to that number once.
3. Add to the master `keys.json` (paste; don't overwrite the file):
   ```json
   "TWILIO_ACCOUNT_SID": "AC...",
   "TWILIO_AUTH_TOKEN": "...",
   "TWILIO_WHATSAPP_FROM": "+14155238886",
   "WHATSAPP_TO": "+1YOURMOBILE"
   ```
Uses the same Gmail keys as the other agents for email.
