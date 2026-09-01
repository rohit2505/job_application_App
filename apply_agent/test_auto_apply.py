#!/usr/bin/env python3
"""
Focused tests for resolve_real_url() / apply_to_job()'s redirect handling and
applied-state behavior. Uses lightweight fakes standing in for Playwright's
Page/BrowserContext/Locator/Frame — no real browser, no network — so these
run anywhere with just `python3 -m pytest apply_agent/test_auto_apply.py`
(or plain `python3 -m unittest`).

Covers the scenarios called out in the fix request:
  1. Direct non-Adzuna Greenhouse URL
  2. Adzuna details page with same-tab redirect
  3. Adzuna details page opening a popup
  4. Adzuna land/ad page with delayed redirect
  5. Redirect that remains stuck on Adzuna
  6. Temporary navigation timeout
  7. Successful redirect to a non-Greenhouse ATS
  8. Correct applied-state (seen.json) behavior for each result status
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import auto_apply as aa  # noqa: E402


def _no_meta_refresh(url, timeout_s=15):
    """Test default: no static meta-refresh available, forcing the
    click-flow fallback path — matches every existing scenario below, which
    is testing that click-flow logic specifically."""
    return None, "test stub: no meta-refresh"


# --------------------------------------------------------------------------- #
# Minimal fakes standing in for the small slice of the Playwright API this
# module actually touches.
# --------------------------------------------------------------------------- #
class FakeLocator:
    def __init__(self, visible=False, raises_on_click=None, count_override=None,
                 hide_after_click=False):
        self._visible = visible
        self._raises_on_click = raises_on_click
        self.clicked = False
        self._count_override = count_override
        self._hide_after_click = hide_after_click
        self._value = ""

    @property
    def first(self):
        # real Playwright's .first returns a Locator scoped to the first
        # match; for this fake, the fake itself already represents "the"
        # element, so returning self is an accurate-enough stand-in.
        return self

    def is_visible(self, timeout=0):
        return self._visible

    def click(self, timeout=0):
        self.clicked = True
        if self._raises_on_click:
            raise self._raises_on_click
        if self._hide_after_click:
            # simulates a successful submit navigating away / the form
            # disappearing — as opposed to a rejected submit where the
            # button stays put (see _finish_application's post-click check)
            self._visible = False

    def count(self):
        return self._count_override if self._count_override is not None else (1 if self._visible else 0)

    def fill(self, value):
        self._value = value

    def input_value(self):
        return self._value

    def set_input_files(self, path):
        pass

    def get_attribute(self, name):
        return None

    def inner_text(self):
        return ""

    def nth(self, i):
        return self

    def check(self):
        pass

    def get_by_label(self, text):
        return FakeLocator(visible=False, count_override=0)


class FakeFrame:
    def __init__(self, url):
        self.url = url


class FakePage:
    """Fake Playwright Page. `url` is a mutable attribute so a test can
    script a page "navigating" partway through a call by having a locator's
    click() flip self.url before returning."""

    def __init__(self, start_url, context, on_goto=None, redirect_after_n_polls=None,
                 stuck=False, closed_tracker=None):
        self.url = start_url
        self.context = context
        self._on_goto = on_goto
        self._redirect_after_n_polls = redirect_after_n_polls
        self._poll_count = 0
        self._stuck = stuck
        self.frames = [FakeFrame(start_url)]
        self._closed = False
        self._closed_tracker = closed_tracker if closed_tracker is not None else []
        self._title = ""
        self._content = ""

    def goto(self, url, wait_until=None, timeout=None):
        if self._on_goto:
            self._on_goto(self, url)
        else:
            self.url = url
            self.frames = [FakeFrame(url)]

    def title(self):
        return self._title

    def content(self):
        return self._content

    def get_by_text(self, text, exact=False):
        return FakeLocator(visible=False)

    def get_by_role(self, role, name=None):
        return FakeLocator(visible=False)

    def locator(self, selector):
        # Test-configurable: self._locator_counts maps selector substrings
        # to a count, defaulting to 0 (element not present) for anything
        # unconfigured — safe for fill routines that best-effort skip
        # absent optional fields. Cached by exact selector string so a
        # later .locator(same selector).input_value() sees what an earlier
        # .locator(same selector).fill(...) actually set — real Playwright
        # locators aren't the same object either, but they resolve to the
        # same live DOM element, which is the behavior that matters here.
        cache = self.__dict__.setdefault("_locator_cache", {})
        if selector in cache:
            return cache[selector]
        counts = getattr(self, "_locator_counts", {})
        loc = FakeLocator(visible=False, count_override=0)
        for key, n in counts.items():
            if key in selector:
                loc = FakeLocator(visible=n > 0, count_override=n)
                break
        cache[selector] = loc
        return loc

    @property
    def main_frame(self):
        return self

    def wait_for_timeout(self, ms):
        # simulate the passage of time advancing a delayed/stuck redirect
        self._poll_count += 1
        if self._stuck:
            return
        if self._redirect_after_n_polls is not None and self._poll_count >= self._redirect_after_n_polls:
            self.url = "https://boards.greenhouse.io/acme/jobs/123"
            self.frames = [FakeFrame(self.url)]

    def wait_for_load_state(self, state=None, timeout=None):
        pass

    def screenshot(self, path=None, full_page=True):
        pass

    def close(self):
        self._closed = True
        self._closed_tracker.append(self)


class FakeExpectPageCtx:
    """Fakes context.expect_page(...) as a context manager whose .value is
    the popup page — or raises PWTimeoutError if no popup opens, matching
    real Playwright behavior."""

    def __init__(self, popup_page, raises=None):
        self._popup_page = popup_page
        self._raises = raises

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    @property
    def value(self):
        if self._raises:
            raise self._raises
        return self._popup_page


class FakeContext:
    def __init__(self, popup_page=None, no_popup=False):
        self._popup_page = popup_page
        self._no_popup = no_popup

    def expect_page(self, timeout=None):
        if self._no_popup or self._popup_page is None:
            return FakeExpectPageCtx(None, raises=aa.PWTimeoutError("no popup"))
        return FakeExpectPageCtx(self._popup_page)


# --------------------------------------------------------------------------- #
class ResolveRealUrlTests(unittest.TestCase):
    def setUp(self):
        self._orig_meta = aa._fetch_meta_refresh_target
        aa._fetch_meta_refresh_target = _no_meta_refresh

    def tearDown(self):
        aa._fetch_meta_refresh_target = self._orig_meta

    def test_1_direct_non_adzuna_url_used_as_is(self):
        """A greenhouse/lever/ashby-sourced job's URL is already real — no
        click-through dance, no Adzuna handling, just navigate and return."""
        ctx = FakeContext(no_popup=True)
        gh_url = "https://boards.greenhouse.io/acme/jobs/999"
        page = FakePage(start_url="about:blank", context=ctx)

        status, final_url, out_page, diag = aa.resolve_real_url(page, gh_url)

        self.assertEqual(status, "direct")
        self.assertEqual(final_url, gh_url)
        self.assertIs(out_page, page)
        self.assertFalse(aa._is_adzuna_url(final_url))

    def test_2_adzuna_details_same_tab_redirect(self):
        """Clicking through resolves within the same tab (no popup)."""
        ctx = FakeContext(no_popup=True)
        page = FakePage(start_url="https://www.adzuna.com/details/123", context=ctx,
                         redirect_after_n_polls=1)

        status, final_url, out_page, diag = aa.resolve_real_url(page, page.url, timeout_s=5)

        self.assertEqual(status, "resolved")
        self.assertFalse(aa._is_adzuna_url(final_url))
        self.assertIs(out_page, page)

    def test_3_adzuna_details_opens_popup(self):
        """Clicking Apply opens a new tab; resolution must continue on the
        popup, and the original tab must get closed."""
        closed = []
        ctx = FakeContext()
        popup = FakePage(start_url="https://www.adzuna.com/land/ad/123", context=ctx,
                          redirect_after_n_polls=1, closed_tracker=closed)
        ctx._popup_page = popup
        original = FakePage(start_url="https://www.adzuna.com/details/123", context=ctx,
                             closed_tracker=closed)

        # force _click_apply_trigger to find a "visible" CTA so it attempts
        # the popup path
        original.get_by_role = lambda role, name=None: FakeLocator(visible=True)

        status, final_url, out_page, diag = aa.resolve_real_url(original, original.url, timeout_s=5)

        self.assertEqual(status, "resolved")
        self.assertIs(out_page, popup)
        self.assertFalse(aa._is_adzuna_url(final_url))

    def test_4_land_ad_delayed_redirect(self):
        """The redirect fires only after a few polls — must not bail out
        early just because the URL looked unchanged on the first check."""
        ctx = FakeContext(no_popup=True)
        page = FakePage(start_url="https://www.adzuna.com/land/ad/456", context=ctx,
                         redirect_after_n_polls=3)

        status, final_url, out_page, diag = aa.resolve_real_url(page, page.url, timeout_s=10)

        self.assertEqual(status, "resolved")
        self.assertFalse(aa._is_adzuna_url(final_url))

    def test_5_stuck_on_adzuna_is_redirect_failed_not_not_greenhouse(self):
        """This is the core bug fix: a page that never leaves Adzuna must be
        reported as redirect_failed, never silently treated as a confirmed
        non-Greenhouse ATS."""
        ctx = FakeContext(no_popup=True)
        page = FakePage(start_url="https://www.adzuna.com/land/ad/789", context=ctx,
                         stuck=True)

        status, final_url, out_page, diag = aa.resolve_real_url(page, page.url, timeout_s=2)

        self.assertEqual(status, "redirect_failed")
        self.assertTrue(aa._is_adzuna_url(final_url))
        self.assertIn("actions", diag)

    def test_6_temporary_navigation_timeout_is_recorded_not_swallowed(self):
        """A Playwright timeout on the initial goto() must be surfaced in
        diag['actions'], not silently caught and hidden."""
        ctx = FakeContext(no_popup=True)

        def raise_timeout(pg, url):
            raise aa.PWTimeoutError("Timeout 45000ms exceeded")

        page = FakePage(start_url="https://www.adzuna.com/details/999", context=ctx,
                         on_goto=raise_timeout)

        status, final_url, out_page, diag = aa.resolve_real_url(page, "https://www.adzuna.com/details/999")

        self.assertEqual(status, "redirect_failed")
        self.assertTrue(any("failed" in a for a in diag["actions"]))

    def test_7_resolves_to_non_greenhouse_ats(self):
        """Successful resolution to a real, non-Greenhouse destination is a
        genuine 'not_greenhouse' — distinct from a failed resolution."""
        ctx = FakeContext(no_popup=True)

        def goto_to_lever(pg, url):
            pg.url = "https://jobs.lever.co/acme/abc-123"
            pg.frames = [FakeFrame(pg.url)]

        page = FakePage(start_url="https://www.adzuna.com/details/321", context=ctx,
                         on_goto=goto_to_lever)

        status, final_url, out_page, diag = aa.resolve_real_url(page, "https://www.adzuna.com/details/321")

        self.assertEqual(status, "resolved")
        self.assertIn("lever.co", final_url)
        frame = aa.find_greenhouse_frame(out_page)
        self.assertIsNone(frame)


class ChallengePageTests(unittest.TestCase):
    def test_cloudflare_challenge_page_is_blocked_not_not_greenhouse(self):
        """A bot-challenge interstitial (e.g. Cloudflare's 'Just a moment...')
        must never be misreported as a confirmed non-Greenhouse result — that
        would permanently blacklist a job we never actually saw."""
        ctx = FakeContext(no_popup=True)
        page = FakePage(start_url="https://example.com/companies/acme/jobs/1", context=ctx)
        page._title = "Just a moment..."
        job = {"url": page.url, "company": "Acme Co", "title": "Data Engineer"}

        status, log, screenshot = aa.apply_to_job(page, job, {}, "", "/tmp/does-not-exist.docx",
                                                   "/tmp/does-not-exist.docx")

        self.assertEqual(status, "blocked")
        self.assertNotIn("blocked", aa.PERMANENT_STATUSES)


class MetaRefreshResolutionTests(unittest.TestCase):
    """The primary, preferred path: a plain HTTP GET finds Adzuna's static
    no-JS meta-refresh fallback and we skip the click-flow entirely."""

    def setUp(self):
        self._orig_meta = aa._fetch_meta_refresh_target

    def tearDown(self):
        aa._fetch_meta_refresh_target = self._orig_meta

    def test_meta_refresh_target_resolves_without_click_flow(self):
        aa._fetch_meta_refresh_target = lambda url, timeout_s=15: ("https://boards.greenhouse.io/acme/jobs/1", "ok")
        ctx = FakeContext(no_popup=True)
        page = FakePage(start_url="https://www.adzuna.com/land/ad/999", context=ctx)

        status, final_url, out_page, diag = aa.resolve_real_url(page, page.url, timeout_s=5)

        self.assertEqual(status, "resolved")
        self.assertEqual(final_url, "https://boards.greenhouse.io/acme/jobs/1")
        self.assertTrue(any("meta-refresh" in a for a in diag["actions"]))

    def test_meta_refresh_extraction_regex_matches_real_shape(self):
        html = ('<meta http-equiv="refresh" content="5; '
                'url=https://www.ziprecruiter.com/kn/abc?tsid=1&utm_source=adzuna">')
        m = aa._META_REFRESH_RE.search(html)
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), "https://www.ziprecruiter.com/kn/abc?tsid=1&utm_source=adzuna")

    def test_no_meta_refresh_falls_back_to_click_flow(self):
        aa._fetch_meta_refresh_target = _no_meta_refresh
        ctx = FakeContext(no_popup=True)
        page = FakePage(start_url="https://www.adzuna.com/land/ad/456", context=ctx,
                         redirect_after_n_polls=1)

        status, final_url, out_page, diag = aa.resolve_real_url(page, page.url, timeout_s=5)

        self.assertEqual(status, "resolved")
        self.assertTrue(any("falling back to click-flow" in a for a in diag["actions"]))


class ListingFreshnessTests(unittest.TestCase):
    """Cross-check the source's claimed posting date against the company
    page's own schema.org JobPosting datePosted — catches Adzuna (or any
    aggregator) reporting a job as new when the employer's own listing is
    actually much older."""

    def _jsonld(self, date_posted):
        return (
            '<html><head><script type="application/ld+json">'
            '{"@context":"https://schema.org/","@type":"JobPosting",'
            f'"title":"Data Engineer","datePosted":"{date_posted}"'
            '}</script></head><body></body></html>'
        )

    def test_fresh_listing_not_flagged(self):
        job = {"posted": "2026-08-30T00:00:00+00:00"}
        html = self._jsonld("2026-08-29T00:00:00+00:00")  # 1 day gap
        page = FakePage(start_url="https://boards.greenhouse.io/acme/jobs/1", context=FakeContext())
        page._content = html
        log = []
        self.assertFalse(aa._check_listing_freshness(page, job, log))
        self.assertEqual(log, [])

    def test_stale_listing_flagged(self):
        job = {"posted": "2026-08-30T00:00:00+00:00"}
        html = self._jsonld("2026-07-01T00:00:00+00:00")  # ~60 day gap
        page = FakePage(start_url="https://boards.greenhouse.io/acme/jobs/1", context=FakeContext())
        page._content = html
        log = []
        self.assertTrue(aa._check_listing_freshness(page, job, log))
        self.assertTrue(any("stale_check" in item for item in log))

    def test_no_jsonld_date_never_blocks(self):
        job = {"posted": "2026-08-30T00:00:00+00:00"}
        page = FakePage(start_url="https://boards.greenhouse.io/acme/jobs/1", context=FakeContext())
        page._content = "<html><body>no structured data here</body></html>"
        log = []
        self.assertFalse(aa._check_listing_freshness(page, job, log))

    def test_missing_source_date_never_blocks(self):
        job = {}  # no "posted" field at all
        page = FakePage(start_url="https://boards.greenhouse.io/acme/jobs/1", context=FakeContext())
        page._content = self._jsonld("2026-01-01T00:00:00+00:00")
        log = []
        self.assertFalse(aa._check_listing_freshness(page, job, log))

    def test_stale_listing_short_circuits_apply_to_resolved_page(self):
        job = {"posted": "2026-08-30T00:00:00+00:00"}
        page = FakePage(start_url="https://boards.greenhouse.io/acme/jobs/1", context=FakeContext())
        page._content = self._jsonld("2026-06-01T00:00:00+00:00")
        status, log, shot = aa._apply_to_resolved_page(
            page, "resolved", job, {}, "", None, None, [])
        self.assertEqual(status, "stale_listing")
        self.assertIn("stale_listing", aa.PERMANENT_STATUSES)


class GreenhouseFillVerificationTests(unittest.TestCase):
    """Regression coverage for the 2026-09-01 incident: a real submission
    to Incident IQ went out completely blank. Root cause was Greenhouse's
    current job-boards.greenhouse.io UI using id-based fields with an
    EMPTY name attribute — old name-only selectors matched nothing,
    silently, and the code still clicked submit and called it "applied"."""

    def _gh_page(self, submit_visible=True, id_selectors_present=True):
        ctx = FakeContext(no_popup=True)
        # job-boards.greenhouse.io's form is NOT in an iframe (unlike the
        # classic embed) — model that with an on_goto that leaves frames
        # empty (resolve_real_url calls page.goto() even for an
        # already-direct URL), so find_greenhouse_frame falls through to
        # its page.main_frame fallback, same as a real standalone page.
        def _on_goto(pg, url):
            pg.url = url
            pg.frames = []
        page = FakePage(start_url="https://job-boards.greenhouse.io/acme/jobs/1",
                         context=ctx, on_goto=_on_goto)
        page.frames = []
        if id_selectors_present:
            # Only id-based selectors "exist" — models the real current UI
            # where input[name='first_name'] etc. match zero elements.
            page._locator_counts = {"#first_name": 1, "#last_name": 1, "#email": 1}
        else:
            page._locator_counts = {}
        submit_locator = FakeLocator(visible=submit_visible, hide_after_click=True)
        page.get_by_role = lambda role, name=None: submit_locator
        return page

    def test_id_based_fields_get_filled_and_verified(self):
        page = self._gh_page()
        frame = page.main_frame
        profile = {"first_name": "Jane", "last_name": "Doe", "email": "jane@example.com"}
        aa.fill_greenhouse_form(frame, {}, profile, "", None, None, [])
        self.assertTrue(aa._required_fields_actually_filled(frame, profile))

    def test_name_only_selectors_fail_verification_not_silently_applied(self):
        # This is the exact bug: id selectors don't exist on this fake page
        # (simulating the case where fill matched nothing), so verification
        # must catch it rather than let a blank submit through.
        page = self._gh_page(id_selectors_present=False)
        frame = page.main_frame
        profile = {"first_name": "Jane", "last_name": "Doe", "email": "jane@example.com"}
        aa.fill_greenhouse_form(frame, {}, profile, "", None, None, [])
        self.assertFalse(aa._required_fields_actually_filled(frame, profile))

    def test_blank_form_never_reaches_applied_status(self):
        page = self._gh_page(id_selectors_present=False)
        job = {"url": page.url, "company": "Acme", "title": "Engineer"}
        profile = {"first_name": "Jane", "last_name": "Doe", "email": "jane@example.com"}
        status, log, screenshot = aa.apply_to_job(page, job, profile, "", None, None)
        self.assertEqual(status, "fill_failed")
        self.assertNotIn("fill_failed", aa.PERMANENT_STATUSES)  # must stay retry-eligible

    def test_properly_filled_form_reaches_applied_status(self):
        page = self._gh_page(id_selectors_present=True)
        job = {"url": page.url, "company": "Acme", "title": "Engineer"}
        profile = {"first_name": "Jane", "last_name": "Doe", "email": "jane@example.com"}
        status, log, screenshot = aa.apply_to_job(page, job, profile, "", None, None)
        self.assertEqual(status, "applied")

    def test_submit_button_still_visible_after_click_is_unconfirmed(self):
        # Fields filled fine, but the submit button never disappears after
        # click — models Greenhouse's client-side validation silently
        # rejecting the submission and keeping the same page up.
        page = self._gh_page(id_selectors_present=True, submit_visible=True)
        # override with a locator that does NOT hide after click
        stuck_submit = FakeLocator(visible=True, hide_after_click=False)
        page.get_by_role = lambda role, name=None: stuck_submit
        job = {"url": page.url, "company": "Acme", "title": "Engineer"}
        profile = {"first_name": "Jane", "last_name": "Doe", "email": "jane@example.com"}
        status, log, screenshot = aa.apply_to_job(page, job, profile, "", None, None)
        self.assertEqual(status, "submit_unconfirmed")
        self.assertNotIn("submit_unconfirmed", aa.PERMANENT_STATUSES)


class LeverSupportTests(unittest.TestCase):
    """Lever detection/fill/CAPTCHA-block routing (destination-side — fires
    on a lever.co URL regardless of which source found the job)."""

    def setUp(self):
        self._orig_meta = aa._fetch_meta_refresh_target
        aa._fetch_meta_refresh_target = _no_meta_refresh

    def tearDown(self):
        aa._fetch_meta_refresh_target = self._orig_meta

    def _lever_page(self, extra_locator_counts=None, submit_visible=True):
        ctx = FakeContext(no_popup=True)
        page = FakePage(start_url="https://jobs.lever.co/acme/apply", context=ctx)
        page._locator_counts = {"input[name='resume']": 1}
        if extra_locator_counts:
            page._locator_counts.update(extra_locator_counts)
        # Same instance on every get_by_role call (not a fresh one) so that
        # clicking it can be observed on the re-check right after —
        # hide_after_click=True models a successful submit navigating away.
        submit_locator = FakeLocator(visible=submit_visible, hide_after_click=True)
        page.get_by_role = lambda role, name=None: submit_locator
        return page

    def test_find_lever_form_detects_by_url_and_resume_field(self):
        page = self._lever_page()
        frame = aa.find_lever_form(page)
        self.assertIsNotNone(frame)

    def test_find_lever_form_none_without_resume_field(self):
        page = self._lever_page(extra_locator_counts={"input[name='resume']": 0})
        frame = aa.find_lever_form(page)
        self.assertIsNone(frame)

    def test_hcaptcha_on_lever_blocks_submission_never_solved(self):
        page = self._lever_page(extra_locator_counts={"h-captcha": 1})
        job = {"url": page.url, "company": "Acme", "title": "Engineer"}
        status, log, screenshot = aa.apply_to_job(page, job, {}, "", "/tmp/x.docx", "/tmp/x.docx")
        self.assertEqual(status, "captcha")
        self.assertIsNone(screenshot)

    def test_lever_form_with_no_captcha_and_submit_button_applies(self):
        page = self._lever_page()
        job = {"url": page.url, "company": "Acme", "title": "Engineer"}
        status, log, screenshot = aa.apply_to_job(page, job, {"first_name": "Jane"}, "",
                                                   "/tmp/x.docx", "/tmp/x.docx")
        self.assertEqual(status, "applied")

    def test_find_lever_form_navigates_to_apply_page_when_description_page_has_no_form(self):
        """Regression for the 2026-09 bug: a Lever posting resolves to the
        job DESCRIPTION page (.../<id>), which never has the form on it —
        the real form lives at a separate .../<id>/apply URL. This was
        previously silently misreported as 'not_greenhouse'."""
        ctx = FakeContext(no_popup=True)

        def _on_goto(pg, url):
            pg.url = url
            pg.frames = [FakeFrame(url)]
            pg._locator_cache = {}  # fresh "page" — don't reuse the old count
            pg._locator_counts = {"input[name='resume']": 1 if url.endswith("/apply") else 0}

        page = FakePage(start_url="https://jobs.lever.co/acme/xyz", context=ctx, on_goto=_on_goto)
        page._locator_counts = {"input[name='resume']": 0}  # description page: no form yet

        frame = aa.find_lever_form(page)
        self.assertIsNotNone(frame)
        self.assertTrue(page.url.endswith("/apply"), page.url)

    def test_find_lever_form_returns_none_if_apply_page_also_has_no_form(self):
        ctx = FakeContext(no_popup=True)

        def _on_goto(pg, url):
            pg.url = url
            pg.frames = [FakeFrame(url)]
            pg._locator_cache = {}
            pg._locator_counts = {"input[name='resume']": 0}

        page = FakePage(start_url="https://jobs.lever.co/acme/xyz", context=ctx, on_goto=_on_goto)
        page._locator_counts = {"input[name='resume']": 0}

        frame = aa.find_lever_form(page)
        self.assertIsNone(frame)


class AppliedStateTests(unittest.TestCase):
    """Status -> permanent-seen mapping (item 8/9 of the fix request)."""

    def test_permanent_statuses_are_exactly_the_confirmed_outcomes(self):
        self.assertEqual(aa.PERMANENT_STATUSES, {"applied", "not_greenhouse", "captcha", "stale_listing"})

    def test_retry_eligible_statuses_excluded(self):
        for status in ("redirect_failed", "error", "unanswered"):
            self.assertNotIn(status, aa.PERMANENT_STATUSES,
                              f"{status} must be retry-eligible, not permanently seen")


class ApplyToJobRedirectFailedTests(unittest.TestCase):
    def setUp(self):
        self._orig_meta = aa._fetch_meta_refresh_target
        aa._fetch_meta_refresh_target = _no_meta_refresh

    def tearDown(self):
        aa._fetch_meta_refresh_target = self._orig_meta

    def test_redirect_failed_captures_diagnostics_and_screenshot_path(self):
        ctx = FakeContext(no_popup=True)
        page = FakePage(start_url="https://www.adzuna.com/land/ad/1", context=ctx, stuck=True)
        job = {"url": page.url, "company": "Acme Co", "title": "Data Engineer"}

        status, log, screenshot = aa.apply_to_job(page, job, {}, "", "/tmp/does-not-exist.docx",
                                                   "/tmp/does-not-exist.docx")

        self.assertEqual(status, "redirect_failed")
        self.assertTrue(screenshot and screenshot.endswith("_redirect_failed.png"))
        diag_entries = [e for e in log if "resolved_status" in e]
        self.assertTrue(diag_entries)
        self.assertEqual(diag_entries[0]["resolved_status"], "redirect_failed")
        self.assertIn("frame_urls", diag_entries[0])
        self.assertIn("actions", diag_entries[0])


class CaptchaRetryTimingTests(unittest.TestCase):
    """Regression for the 2026-09 MrBeast/DoorDash incidents: a recaptcha
    widget injected into the DOM a beat after page load must be caught
    BEFORE any screening question gets escalated to Telegram, not just on
    the later pre-submit re-check."""

    def test_captcha_appearing_on_second_check_is_still_caught(self):
        calls = {"n": 0}

        class StubFrame:
            def locator(self, selector):
                calls["n"] += 1
                present = calls["n"] >= 2  # missing on check 1, present on check 2
                return FakeLocator(visible=present, count_override=1 if present else 0)

        class StubPage:
            def wait_for_timeout(self, ms):
                pass

        self.assertTrue(aa._captcha_present_with_retry(StubPage(), StubFrame(),
                                                         attempts=3, wait_ms=0))
        self.assertEqual(calls["n"], 2)

    def test_no_captcha_across_all_attempts_returns_false(self):
        class StubFrame:
            def locator(self, selector):
                return FakeLocator(visible=False, count_override=0)

        class StubPage:
            def wait_for_timeout(self, ms):
                pass

        self.assertFalse(aa._captcha_present_with_retry(StubPage(), StubFrame(),
                                                          attempts=3, wait_ms=0))


class ResolveTelegramReplyTests(unittest.TestCase):
    """Lets a Telegram reply ask the AI to answer/polish instead of the
    candidate typing the final answer themselves."""

    def setUp(self):
        self._orig_answer = aa.answer_question
        self._orig_polish = aa.ai_polish_answer

    def tearDown(self):
        aa.answer_question = self._orig_answer
        aa.ai_polish_answer = self._orig_polish

    def test_bare_ai_phrase_reruns_answer_question(self):
        aa.answer_question = lambda q, o, r, p: "Yes, I have."
        final, source = aa.resolve_telegram_reply("ai", "Have you done X?", "resume", {})
        self.assertEqual(final, "Yes, I have.")
        self.assertEqual(source, "telegram+ai")

    def test_bare_ai_phrase_falls_back_to_literal_text_if_ai_cannot_answer(self):
        aa.answer_question = lambda q, o, r, p: None
        final, source = aa.resolve_telegram_reply("answer it", "Have you done X?", "resume", {})
        self.assertEqual(final, "answer it")
        self.assertEqual(source, "telegram")

    def test_ai_colon_notes_gets_polished(self):
        aa.ai_polish_answer = lambda q, notes, r, p: f"Polished: {notes}"
        final, source = aa.resolve_telegram_reply(
            "ai: built the onboarding flow from scratch at my last job",
            "Have you designed a feature from scratch?", "resume", {})
        self.assertEqual(final, "Polished: built the onboarding flow from scratch at my last job")
        self.assertEqual(source, "telegram+ai")

    def test_ai_space_notes_also_works(self):
        aa.ai_polish_answer = lambda q, notes, r, p: f"Polished: {notes}"
        final, source = aa.resolve_telegram_reply(
            "ai built the onboarding flow", "Have you designed a feature from scratch?",
            "resume", {})
        self.assertEqual(final, "Polished: built the onboarding flow")
        self.assertEqual(source, "telegram+ai")

    def test_ai_colon_falls_back_to_raw_notes_if_polish_fails(self):
        aa.ai_polish_answer = lambda q, notes, r, p: None
        final, source = aa.resolve_telegram_reply(
            "ai: built the onboarding flow", "Have you designed a feature from scratch?",
            "resume", {})
        self.assertEqual(final, "built the onboarding flow")
        self.assertEqual(source, "telegram")

    def test_plain_reply_used_verbatim(self):
        final, source = aa.resolve_telegram_reply(
            "Yes, at my last company I built the whole reporting pipeline.",
            "Have you done X?", "resume", {})
        self.assertEqual(final, "Yes, at my last company I built the whole reporting pipeline.")
        self.assertEqual(source, "telegram")


if __name__ == "__main__":
    unittest.main()
