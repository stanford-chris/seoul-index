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


class HostOfTests(unittest.TestCase):
    def test_plain_string_url(self):
        self.assertEqual(acl._host_of("https://api.example.com/x?y=1"), "api.example.com")

    def test_httpx_url_object_stringifies(self):
        import httpx
        self.assertEqual(acl._host_of(httpx.URL("https://bsky.social/xrpc/x")), "bsky.social")

    def test_unparseable_returns_none(self):
        self.assertIsNone(acl._host_of("not a url at all ::::"))


class RequestsInstallTests(unittest.TestCase):
    """requests.Session.request is the one method both Session().get/post(...)
    and the module-level requests.get/post(...) funnel through — the latter
    construct a throwaway Session and call .request() on it."""

    def test_session_get_is_logged_and_still_returns_the_real_response(self):
        import requests
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "api_calls.jsonl"
            real_request = requests.Session.request
            try:
                with mock.patch.object(acl, "LOG_PATH", path), \
                     mock.patch.object(requests.Session, "request", return_value="fake-response"):
                    acl._install_requests("test_script.py")
                    result = requests.Session().get("https://api.trakt.tv/x")
            finally:
                requests.Session.request = real_request
            self.assertEqual(result, "fake-response")
            lines = path.read_text().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0])["service"], "api.trakt.tv")

    def test_noop_when_requests_is_not_importable(self):
        with mock.patch.dict("sys.modules", {"requests": None}):
            acl._install_requests("test_script.py")   # must not raise


class HttpxInstallTests(unittest.TestCase):
    """The atproto SDK's own HTTP client — every Bluesky-posting bot here
    uses Client() from atproto, which calls httpx.Client.request()
    underneath (atproto_client.request.Request._send_request), not curl
    or requests. Without this wrap a bot's own actual post would be
    invisible to this tool."""

    def test_client_get_is_logged_and_still_returns_the_real_response(self):
        import httpx
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "api_calls.jsonl"
            real_request = httpx.Client.request
            try:
                with mock.patch.object(acl, "LOG_PATH", path), \
                     mock.patch.object(httpx.Client, "request", return_value="fake-response"):
                    acl._install_httpx("test_script.py")
                    result = httpx.Client().get("https://bsky.social/xrpc/com.atproto.repo.createRecord")
            finally:
                httpx.Client.request = real_request
            self.assertEqual(result, "fake-response")
            lines = path.read_text().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0])["service"], "bsky.social")

    def test_noop_when_httpx_is_not_importable(self):
        with mock.patch.dict("sys.modules", {"httpx": None}):
            acl._install_httpx("test_script.py")   # must not raise


class InstallAllThreeTests(unittest.TestCase):
    def test_install_wires_up_curl_requests_and_httpx_together(self):
        import httpx
        import requests
        real_run = subprocess.run
        real_requests_request = requests.Session.request
        real_httpx_request = httpx.Client.request
        try:
            with tempfile.TemporaryDirectory() as tmp:
                path = pathlib.Path(tmp) / "api_calls.jsonl"
                with mock.patch.object(acl, "LOG_PATH", path), \
                     mock.patch.object(subprocess, "run",
                                        return_value=subprocess.CompletedProcess([], 0, stdout="ok")), \
                     mock.patch.object(requests.Session, "request", return_value="r1"), \
                     mock.patch.object(httpx.Client, "request", return_value="r2"):
                    acl.install("combo_script.py")
                    subprocess.run(["curl", "-s", "https://a.example.com"])
                    requests.Session().get("https://b.example.com")
                    httpx.Client().get("https://c.example.com")
                hosts = {json.loads(l)["service"] for l in path.read_text().splitlines()}
        finally:
            subprocess.run = real_run
            requests.Session.request = real_requests_request
            httpx.Client.request = real_httpx_request
        self.assertEqual(hosts, {"a.example.com", "b.example.com", "c.example.com"})


if __name__ == "__main__":
    unittest.main()
