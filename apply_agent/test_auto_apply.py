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
    def __init__(self, visible=False, raises_on_click=None):
        self._visible = visible
        self._raises_on_click = raises_on_click
        self.clicked = False

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

    def goto(self, url, wait_until=None, timeout=None):
        if self._on_goto:
            self._on_goto(self, url)
        else:
            self.url = url
            self.frames = [FakeFrame(url)]

    def title(self):
        return self._title

    def get_by_text(self, text, exact=False):
        return FakeLocator(visible=False)

    def get_by_role(self, role, name=None):
        return FakeLocator(visible=False)

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


class AppliedStateTests(unittest.TestCase):
    """Status -> permanent-seen mapping (item 8/9 of the fix request)."""

    def test_permanent_statuses_are_exactly_the_confirmed_outcomes(self):
        self.assertEqual(aa.PERMANENT_STATUSES, {"applied", "not_greenhouse", "captcha"})

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


if __name__ == "__main__":
    unittest.main()
