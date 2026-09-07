#!/usr/bin/env python3
"""Unit tests for filter_agent.py's salary handling: the new JD-extraction
pass in score_job(), and the format/floor helpers it relies on. Network
(urlopen) is mocked -- nothing here makes a real Anthropic call."""
import json
import sys
import unittest
from unittest.mock import patch, MagicMock

import filter_agent as fa


class TestFormatSalaryRange(unittest.TestCase):
    def test_both(self):
        self.assertEqual(fa.format_salary_range(140000, 170000), "140,000–170,000")

    def test_max_only(self):
        self.assertEqual(fa.format_salary_range(None, 170000), "up to 170,000")

    def test_min_only(self):
        self.assertEqual(fa.format_salary_range(140000, None), "140,000+")

    def test_neither(self):
        self.assertIsNone(fa.format_salary_range(None, None))

    def test_zero_treated_as_unknown(self):
        # Some sources send 0 rather than null for "unknown".
        self.assertIsNone(fa.format_salary_range(0, 0))


class TestSalaryFloorOk(unittest.TestCase):
    def test_no_floor_set_always_ok(self):
        self.assertTrue(fa.salary_floor_ok({"salary_min": 50000, "salary_max": 60000}, None, False))

    def test_known_above_floor(self):
        job = {"salary_min": 150000, "salary_max": 180000}
        self.assertTrue(fa.salary_floor_ok(job, 160000, False))

    def test_known_below_floor(self):
        job = {"salary_min": 120000, "salary_max": 140000}
        self.assertFalse(fa.salary_floor_ok(job, 160000, False))

    def test_uses_max_over_min_when_both_present(self):
        # max=170000 clears a 160000 floor even though min doesn't.
        job = {"salary_min": 120000, "salary_max": 170000}
        self.assertTrue(fa.salary_floor_ok(job, 160000, False))

    def test_unknown_kept_by_default(self):
        job = {"salary_min": None, "salary_max": None}
        self.assertTrue(fa.salary_floor_ok(job, 160000, False))

    def test_unknown_dropped_when_required(self):
        job = {"salary_min": None, "salary_max": None}
        self.assertFalse(fa.salary_floor_ok(job, 160000, True))

    def test_min_only_compared_when_max_absent(self):
        job = {"salary_min": 150000, "salary_max": None}
        self.assertFalse(fa.salary_floor_ok(job, 160000, False))
        job2 = {"salary_min": 165000, "salary_max": None}
        self.assertTrue(fa.salary_floor_ok(job2, 160000, False))


def _mock_response(payload_dict):
    resp = MagicMock()
    resp.__enter__.return_value = resp
    resp.read.return_value = json.dumps({
        "content": [{"text": json.dumps(payload_dict)}]
    }).encode("utf-8")
    return resp


class TestScoreJobSalaryExtraction(unittest.TestCase):
    def _job(self, **overrides):
        base = {"title": "Data Engineer", "company": "Acme", "salary": "",
                "salary_min": None, "salary_max": None,
                "description": "We pay $150,000-$180,000 annually for this role."}
        base.update(overrides)
        return base

    @patch("filter_agent.urllib.request.urlopen")
    def test_extracts_salary_when_unknown(self, mock_urlopen):
        mock_urlopen.return_value = _mock_response({
            "score": 80, "reason": "good fit", "employer_type": "direct",
            "extracted_salary_min": 150000, "extracted_salary_max": 180000,
        })
        score, reason, etype, esmin, esmax = fa.score_job("resume text", self._job(), "model", "key")
        self.assertEqual(score, 80)
        self.assertEqual(esmin, 150000)
        self.assertEqual(esmax, 180000)

        # SALARY_KNOWN must have been sent as false in the request body.
        sent_body = mock_urlopen.call_args[0][0].data
        self.assertIn(b"SALARY_KNOWN: false", sent_body)

    @patch("filter_agent.urllib.request.urlopen")
    def test_does_not_extract_when_already_known(self, mock_urlopen):
        mock_urlopen.return_value = _mock_response({
            "score": 80, "reason": "good fit", "employer_type": "direct",
            "extracted_salary_min": None, "extracted_salary_max": None,
        })
        job = self._job(salary="150,000-180,000", salary_min=150000, salary_max=180000)
        score, reason, etype, esmin, esmax = fa.score_job("resume text", job, "model", "key")
        self.assertIsNone(esmin)
        self.assertIsNone(esmax)
        sent_body = mock_urlopen.call_args[0][0].data
        self.assertIn(b"SALARY_KNOWN: true", sent_body)

    @patch("filter_agent.urllib.request.urlopen")
    def test_ignores_zero_or_negative_extracted_values(self, mock_urlopen):
        mock_urlopen.return_value = _mock_response({
            "score": 70, "reason": "ok", "employer_type": "direct",
            "extracted_salary_min": 0, "extracted_salary_max": -5,
        })
        _, _, _, esmin, esmax = fa.score_job("resume text", self._job(), "model", "key")
        self.assertIsNone(esmin)
        self.assertIsNone(esmax)

    @patch("filter_agent.urllib.request.urlopen")
    def test_no_json_in_response_returns_none_tuple(self, mock_urlopen):
        resp = MagicMock()
        resp.__enter__.return_value = resp
        resp.read.return_value = json.dumps({"content": [{"text": "not json at all"}]}).encode("utf-8")
        mock_urlopen.return_value = resp
        result = fa.score_job("resume text", self._job(), "model", "key")
        self.assertEqual(result, (None, "no JSON from model", "unknown", None, None))


if __name__ == "__main__":
    unittest.main()
