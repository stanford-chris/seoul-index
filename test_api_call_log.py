#!/usr/bin/env python3
"""Tests for api_call_log.py. Stdlib only, no real subprocess calls, no
real file writes — LOG_PATH is monkeypatched to a tmp file throughout."""

import json
import pathlib
import subprocess
import tempfile
import unittest
from unittest import mock

import api_call_log as acl


class TargetHostTests(unittest.TestCase):
    def test_url_last_is_the_common_case(self):
        args = ["curl", "-s", "--max-time", "10", "https://api.open-meteo.com/v1/forecast?x=1"]
        self.assertEqual(acl._target_host(args), "api.open-meteo.com")

    def test_url_last_with_post_and_headers(self):
        args = ["curl", "-s", "-X", "POST", "-d", "grant_type=refresh_token",
                "https://api.ouraring.com/oauth/token"]
        self.assertEqual(acl._target_host(args), "api.ouraring.com")

    def test_header_values_are_not_mistaken_for_urls(self):
        # A User-Agent or Authorization header value never starts with
        # http(s):// in any real call site here, but the fallback scan
        # must not misfire on one that happened to.
        args = ["curl", "-s", "-A", "MOLIT client (not-a-url)", "https://rt.molit.go.kr/x"]
        self.assertEqual(acl._target_host(args), "rt.molit.go.kr")

    def test_no_url_present_returns_none(self):
        args = ["curl", "-s", "--max-time", "10"]
        self.assertIsNone(acl._target_host(args))

    def test_fallback_scan_when_url_is_not_last(self):
        # Not the convention any real call site here uses, but the scan
        # must still find it rather than silently logging nothing.
        args = ["curl", "https://example.com/x", "-s"]
        self.assertEqual(acl._target_host(args), "example.com")


class LogTests(unittest.TestCase):
    def test_appends_one_json_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "api_calls.jsonl"
            with mock.patch.object(acl, "LOG_PATH", path):
                acl._log("some_script.py", "example.com")
            lines = path.read_text().splitlines()
        self.assertEqual(len(lines), 1)
        row = json.loads(lines[0])
        self.assertEqual(row["script"], "some_script.py")
        self.assertEqual(row["service"], "example.com")
        self.assertIn("ts", row)

    def test_appends_rather_than_overwrites(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "api_calls.jsonl"
            with mock.patch.object(acl, "LOG_PATH", path):
                acl._log("a.py", "one.example.com")
                acl._log("b.py", "two.example.com")
            lines = path.read_text().splitlines()
        self.assertEqual(len(lines), 2)

    def test_unwritable_log_does_not_raise(self):
        # A full disk or a missing directory must never be the reason a
        # real script run fails.
        with mock.patch.object(acl, "LOG_PATH", pathlib.Path("/no/such/dir/api_calls.jsonl")):
            acl._log("a.py", "example.com")   # must not raise


class InstallTests(unittest.TestCase):
    def test_curl_calls_are_logged_and_still_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "api_calls.jsonl"
            real_run = subprocess.run
            try:
                with mock.patch.object(acl, "LOG_PATH", path), \
                     mock.patch.object(subprocess, "run",
                                        return_value=subprocess.CompletedProcess([], 0, stdout="ok")):
                    acl.install("test_script.py")
                    result = subprocess.run(["curl", "-s", "https://example.com/x"],
                                             capture_output=True, text=True)
            finally:
                subprocess.run = real_run
            self.assertEqual(result.stdout, "ok")
            lines = path.read_text().splitlines()
        self.assertEqual(len(lines), 1)
        row = json.loads(lines[0])
        self.assertEqual(row["service"], "example.com")

    def test_non_curl_calls_pass_through_unlogged(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "api_calls.jsonl"
            real_run = subprocess.run
            try:
                with mock.patch.object(acl, "LOG_PATH", path), \
                     mock.patch.object(subprocess, "run",
                                        return_value=subprocess.CompletedProcess([], 0, stdout="done")):
                    acl.install("test_script.py")
                    result = subprocess.run(["osascript", "-e", "return 1"],
                                             capture_output=True, text=True)
            finally:
                subprocess.run = real_run
            self.assertEqual(result.stdout, "done")
            self.assertFalse(path.exists())

    def test_installed_wrapper_never_changes_return_value(self):
        # The wrapper must be pure observation: same args, kwargs and
        # return value as calling the real subprocess.run directly.
        real_run = subprocess.run
        try:
            with mock.patch.object(acl, "_log"), \
                 mock.patch.object(subprocess, "run",
                                    return_value=subprocess.CompletedProcess(["curl"], 0, stdout="x", stderr="y")):
                acl.install("test_script.py")
                result = subprocess.run(["curl", "https://example.com"], capture_output=True, text=True, timeout=5)
        finally:
            subprocess.run = real_run
        self.assertEqual(result.stdout, "x")
        self.assertEqual(result.stderr, "y")
        self.assertEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main()
