"""JobBuddy: the Telegram-facing "agent" that handles screening questions
Claude couldn't confidently answer at fill-time.

Behavior, per explicit product decision:
  - For EVERY question a job application escalates to Telegram, JobBuddy
    first tries to compose a draft answer itself, strictly from your
    resume/profile (the same well-scoped, no-fabrication call the initial
    fill pass already uses) -- you don't have to say anything special to
    trigger this, it's the default.
  - Whatever it composes, it NEVER fills into the live form on its own. It
    always sends the draft back to you on Telegram and waits for your
    approval (a plain "ok") before treating it as final.
  - If you don't like the draft, you can either send the corrected answer
    directly, or address it by name ("hey jobbuddy, <more context>") to
    give it extra facts to redraft with (e.g. "hey jobbuddy, I have 1 yr
    experience in Snowflake") -- it'll compose a new draft and ask again.
  - If it can't compose anything from your resume/profile at all, it falls
    back to just asking you the question directly, same as before -- and
    that raw reply can itself still be a "hey jobbuddy, ..." address if you
    want it to try composing from context you give it right there.
  - A confirmed answer (yours or JobBuddy's, once you've approved it) is
    remembered in the shared qa_cache.json exactly like every other
    confirmed Telegram reply, so the next job with the same/similar
    question skips straight to the cache.
  - JobBuddy never touches CAPTCHA, never guesses which job you mean (it
    only ever runs against the one question actively waiting on a reply
    for the job currently being processed), and never edits your resume
    or profile.json -- only the shared Q&A cache.

Kept as its own file (rather than folded into auto_apply.py) so this
conversational surface can grow independently of the already-large
auto_apply.py fill/escalation logic it's called from.
"""
import time

_CONFIRM_WORDS = {"ok", "okay", "yes", "y", "yep", "sounds good",
                   "use it", "go ahead", "correct", "that's right",
                   "thats right", "looks good", "lgtm"}

_TRIGGER_PREFIXES = ("hey jobbuddy", "hey job buddy", "hi jobbuddy",
                      "jobbuddy", "job buddy")

_MAX_REDRAFT_ROUNDS = 2  # cap on "hey jobbuddy, more context" back-and-forths


def is_jobbuddy_trigger(text):
    """True if a Telegram message addresses JobBuddy by name to give it
    extra context, rather than being an answer/correction itself (e.g.
    "hey jobbuddy, I have 1 yr experience in Snowflake" vs. just typing
    "1 year"). Name-gated on purpose so a literal answer that happens to
    start with an ordinary word is never misread as a command."""
    if not text:
        return False
    return text.strip().lower().startswith(_TRIGGER_PREFIXES)


def _strip_trigger(text):
    """Remove the leading "hey jobbuddy[,:]" address, leaving your actual
    instruction (e.g. "hey jobbuddy, refer to resume and rewrite" ->
    "refer to resume and rewrite"). Empty result means you addressed it
    with no extra instruction -- treated as "just try again"."""
    lower = text.strip().lower()
    for prefix in _TRIGGER_PREFIXES:
        if lower.startswith(prefix):
            return text.strip()[len(prefix):].lstrip(",: ").strip()
    return text.strip()


def _send_draft_for_approval(question, options, draft, send_whatsapp):
    opts_txt = f"\nOptions: {', '.join(options)}" if options else ""
    return send_whatsapp(
        f"Here's what I'd enter for \"{question}\":{opts_txt}\n"
        f"\"{draft}\"\nReply OK to use this, send the corrected answer, or "
        f"tell me more (e.g. \"hey jobbuddy, ...\") and I'll redraft.")


def resolve_unanswered_question(question, options, resume_text, profile, *,
                                 answer_question, ai_polish_answer,
                                 send_whatsapp, wait_for_whatsapp_reply,
                                 remember_answer):
    """Resolve one screening question that fill-time couldn't answer.
    Always tries a resume-based draft first and always requires your
    Telegram approval before treating anything as final. Returns
    (final_answer, source_tag); final_answer is None if nothing was
    confirmed in time, meaning the caller should leave the question
    unanswered exactly as a plain timeout would."""
    draft = answer_question(question, options, resume_text, profile)

    if not draft:
        # Nothing to draft from your resume/profile -- ask you directly,
        # same as before JobBuddy existed.
        opts_txt = f"\nOptions: {', '.join(options)}" if options else ""
        sent, _detail, _msg_id = send_whatsapp(
            f"🧑‍💻 Stuck on this one, and I couldn't draft an answer from "
            f"your resume:\n\"{question}\"{opts_txt}\nReply with your answer.")
        if not sent:
            return None, "unanswered"
        reply = wait_for_whatsapp_reply(time.time())
        if not reply:
            return None, "unanswered"
        if not is_jobbuddy_trigger(reply):
            final = reply.strip()
            remember_answer(question, final)
            return final, "telegram"
        # You addressed JobBuddy with context instead of just answering --
        # fall through into the same redraft loop the "had a draft" path
        # uses, seeded with that context.
        instruction = _strip_trigger(reply)
        draft = ai_polish_answer(question, instruction, resume_text, profile) \
            if instruction else None
        if not draft:
            return None, "unanswered"

    sent, _detail, _msg_id = _send_draft_for_approval(
        question, options, draft, send_whatsapp)
    if not sent:
        return None, "unanswered"

    for _ in range(_MAX_REDRAFT_ROUNDS):
        reply = wait_for_whatsapp_reply(time.time())
        if not reply:
            return None, "unanswered"
        stripped = reply.strip()
        if stripped.lower() in _CONFIRM_WORDS:
            remember_answer(question, draft)
            return draft, "telegram+jobbuddy"
        if is_jobbuddy_trigger(stripped):
            instruction = _strip_trigger(stripped)
            new_draft = (ai_polish_answer(question, instruction, resume_text,
                                           profile) if instruction else None)
            if not new_draft:
                continue  # couldn't redraft -- ask again rather than losing the thread
            draft = new_draft
            sent, _detail, _msg_id = _send_draft_for_approval(
                question, options, draft, send_whatsapp)
            if not sent:
                return None, "unanswered"
            continue
        # Anything else is treated as your own corrected answer, used
        # exactly as typed.
        remember_answer(question, stripped)
        return stripped, "telegram"

    return None, "unanswered"  # too many redraft rounds without a confirm
