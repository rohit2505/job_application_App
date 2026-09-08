#!/usr/bin/env python3
"""Unit tests for the Apify cost controls added to
fetch_active_jobs_db_apify(): memory pin, maxTotalChargeUsd cap, and the
lowered default job limit. post_json_with_headers is mocked -- nothing
here makes a real Apify call."""
import os
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

import job_search as js


class TestApifyCostControls(unittest.TestCase):
    def setUp(self):
        # Isolate from any real keys.json / env the module may have loaded.
        js.LOCAL_KEYS = {"APIFY_TOKEN": "test-token"}
        for var in ("APIFY_JOB_LIMIT", "APIFY_RUN_MEMORY_MB", "APIFY_MAX_RUN_USD"):
            os.environ.pop(var, None)

    @patch("job_search._apify_usage_within_budget", return_value=True)
    @patch("job_search.post_json_with_headers", return_value=([], {}))
    def test_default_url_has_memory_and_cost_cap(self, mock_post, _mock_budget):
        js.fetch_active_jobs_db_apify("data engineer", datetime.now(timezone.utc), 2880)
        url = mock_post.call_args[0][0]
        self.assertIn("memory=256", url)
        self.assertIn("maxTotalChargeUsd=0.50", url)

    @patch("job_search._apify_usage_within_budget", return_value=True)
    @patch("job_search.post_json_with_headers", return_value=([], {}))
    def test_default_limit_is_50_not_100(self, mock_post, _mock_budget):
        js.fetch_active_jobs_db_apify("data engineer", datetime.now(timezone.utc), 2880)
        payload = mock_post.call_args[0][1]
        self.assertEqual(payload["limit"], 50)

    @patch("job_search._apify_usage_within_budget", return_value=True)
    @patch("job_search.post_json_with_headers", return_value=([], {}))
    def test_env_overrides_respected(self, mock_post, _mock_budget):
        os.environ["APIFY_RUN_MEMORY_MB"] = "128"
        os.environ["APIFY_MAX_RUN_USD"] = "1.25"
        os.environ["APIFY_JOB_LIMIT"] = "20"
        try:
            js.fetch_active_jobs_db_apify("data engineer", datetime.now(timezone.utc), 2880)
        finally:
            for var in ("APIFY_RUN_MEMORY_MB", "APIFY_MAX_RUN_USD", "APIFY_JOB_LIMIT"):
                os.environ.pop(var, None)
        url = mock_post.call_args[0][0]
        payload = mock_post.call_args[0][1]
        self.assertIn("memory=128", url)
        self.assertIn("maxTotalChargeUsd=1.25", url)
        self.assertEqual(payload["limit"], 20)

    @patch("job_search._apify_usage_within_budget", return_value=False)
    @patch("job_search.post_json_with_headers")
    def test_skips_fetch_when_over_monthly_budget(self, mock_post, _mock_budget):
        result = js.fetch_active_jobs_db_apify("data engineer", datetime.now(timezone.utc), 2880)
        self.assertEqual(result, [])
        mock_post.assert_not_called()

    @patch("job_search._apify_usage_within_budget", return_value=True)
    @patch("job_search.post_json_with_headers", return_value=([], {}))
    def test_multiple_titles_sent_in_one_call(self, mock_post, _mock_budget):
        titles = ["data engineer", "analytics engineer", "etl developer"]
        js.fetch_active_jobs_db_apify(titles, datetime.now(timezone.utc), 2880)
        # Exactly one Apify call for all three titles, not three.
        self.assertEqual(mock_post.call_count, 1)
        payload = mock_post.call_args[0][1]
        self.assertEqual(payload["titleSearch"], titles)

    @patch("job_search._apify_usage_within_budget", return_value=True)
    @patch("job_search.post_json_with_headers", return_value=([], {}))
    def test_single_string_query_wrapped_as_one_item_list(self, mock_post, _mock_budget):
        js.fetch_active_jobs_db_apify("data engineer", datetime.now(timezone.utc), 2880)
        payload = mock_post.call_args[0][1]
        self.assertEqual(payload["titleSearch"], ["data engineer"])

    @patch("job_search._apify_usage_within_budget", return_value=True)
    @patch("job_search.post_json_with_headers", return_value=([], {}))
    def test_blank_titles_in_list_are_dropped(self, mock_post, _mock_budget):
        js.fetch_active_jobs_db_apify(["data engineer", "", "  ", "etl developer"],
                                       datetime.now(timezone.utc), 2880)
        payload = mock_post.call_args[0][1]
        self.assertEqual(payload["titleSearch"], ["data engineer", "etl developer"])


if __name__ == "__main__":
    unittest.main()
