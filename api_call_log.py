"""Shared outbound-API-call counter, copied byte-identical into every repo
that opts in — same shape as net_guard.py/limit_guard.py (see
scripts_tidy.sh check 3c, which compares these copies for drift).

First slice, 5 September 2026: daily_status_digest.py, seoul_index_post.py
and london_index_harvest.py (via london_index_post.py, its real entry
point) — curl-only at that point. Extended the same day to cover the
other two calling conventions the outbound-call survey found: the
`requests` library directly, and `httpx` — the atproto SDK's own HTTP
client, which every Bluesky-posting bot here uses via `Client()` and
which is neither curl nor `requests` under the hood (checked its source,
5 September 2026: `atproto_client.request.Request._send_request` calls
`self._client.request(...)` where `self._client` is an `httpx.Client`).
Without the httpx wrap, a posting bot's own actual Bluesky post — the
single most central call it makes — would be invisible to this tool.

Each of the three wraps patches the ONE method its library funnels every
call through, rather than touching individual fetch functions:
- `subprocess.run`, filtered to invocations whose first argument is
  literally "curl" (this Python's own urllib fails certificate
  verification on this machine — see reference_py313_ssl_urllib — hence
  curl instead of urllib for the scripts that don't use requests/httpx).
- `requests.Session.request` — both `Session().get/post(...)` and the
  module-level `requests.get/post(...)` convenience functions route
  through this, since the latter construct a throwaway Session and call
  `.request()` on it.
- `httpx.Client.request` — same shape, for httpx.

`requests`/`httpx` are optional: a script that doesn't import one simply
never triggers that wrap, and install() doesn't require either to be
present.

⚠️ install() must be called from an `if __name__ == "__main__":` block,
never at bare module level, or importing the script (as every test suite
here does) mutates process-wide state for anything else sharing that
interpreter — the exact "a --dry-run guard does NOT stop a test from
filing" trap this codebase already has one documented instance of
(seoul_index_post.py's own `reporting()`), in a new shape.
"""
import json
import subprocess
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

LOG_PATH = Path.home() / "Scripts" / "api_calls.jsonl"


def _target_host(args):
    """The hostname a curl invocation is calling, or None if none of its
    arguments look like a URL. Every curl call in these files puts the
    URL last (verified by reading every call site, 5 September 2026), so
    that's tried first; a scan over the whole arg list is the fallback
    for anything that doesn't follow the convention, rather than silently
    logging nothing."""
    if args and isinstance(args[-1], str) and args[-1].startswith(("http://", "https://")):
        return urllib.parse.urlparse(args[-1]).hostname
    for a in args:
        if isinstance(a, str) and a.startswith(("http://", "https://")):
            return urllib.parse.urlparse(a).hostname
    return None


def _host_of(url):
    if not isinstance(url, str):
        url = str(url)   # httpx.URL objects stringify to the full URL
    try:
        return urllib.parse.urlparse(url).hostname
    except ValueError:
        return None


def _log(script, host):
    # Logging a call must never be the reason the real call fails — an
    # unwritable log directory or a full disk degrades to "this run wasn't
    # counted," never to "this run broke."
    try:
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps({
                "ts": datetime.now(timezone.utc).isoformat(),
                "script": script,
                "service": host,
            }) + "\n")
    except OSError:
        pass


def _install_curl(script_name):
    real_run = subprocess.run

    def wrapped(args, *a, **kw):
        if isinstance(args, list) and args and args[0] == "curl":
            host = _target_host(args)
            if host:
                _log(script_name, host)
        return real_run(args, *a, **kw)

    subprocess.run = wrapped


def _install_requests(script_name):
    try:
        import requests
    except ImportError:
        return
    real_request = requests.Session.request

    def wrapped(self, method, url, *a, **kw):
        host = _host_of(url)
        if host:
            _log(script_name, host)
        return real_request(self, method, url, *a, **kw)

    requests.Session.request = wrapped


def _install_httpx(script_name):
    try:
        import httpx
    except ImportError:
        return
    real_request = httpx.Client.request

    def wrapped(self, method, url, *a, **kw):
        host = _host_of(url)
        if host:
            _log(script_name, host)
        return real_request(self, method, url, *a, **kw)

    httpx.Client.request = wrapped


def install(script_name):
    """Wraps subprocess.run (curl only), requests.Session.request and
    httpx.Client.request so every outbound call this process makes,
    through any of the three, is logged with its target host before the
    real call runs. Anything else (osascript, security, icalBuddy,
    wrangler, ...) passes through completely unchanged — this never
    alters behaviour, timing, or return values, only observes."""
    _install_curl(script_name)
    _install_requests(script_name)
    _install_httpx(script_name)
