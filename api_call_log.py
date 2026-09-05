"""Shared outbound-API-call counter, copied byte-identical into every repo
that opts in — same shape as net_guard.py/limit_guard.py (see
scripts_tidy.sh check 3c, which compares these copies for drift).

First slice, 5 September 2026: daily_status_digest.py, seoul_index_post.py
and london_index_harvest.py (via london_index_post.py, its real entry
point). Not yet wired into the rest of the ~30 files the outbound-call
survey found — extend one at a time rather than all at once.

Every outbound call in these files goes through `subprocess.run(["curl",
...])` (this Python's own urllib fails certificate verification here — see
reference_py313_ssl_urllib), so install() wraps subprocess.run itself
rather than touching each individual fetch function. That means a NEW curl
call added later is counted automatically, with no second place to
remember to instrument.

⚠️ install() must be called from an `if __name__ == "__main__":` block,
never at bare module level, or importing the script (as every test suite
here does) mutates the process-wide `subprocess.run` for anything else
sharing that interpreter — the exact "a --dry-run guard does NOT stop a
test from filing" trap this codebase already has one documented instance
of (seoul_index_post.py's own `reporting()`), in a new shape.
"""
import json
import subprocess
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

LOG_PATH = Path.home() / "Scripts" / "api_calls.jsonl"


def _target_host(args):
    """The hostname a curl invocation is calling, or None if none of its
    arguments look like a URL. Every curl call in these three files puts
    the URL last (verified by reading every call site, 5 September 2026),
    so that's tried first; a scan over the whole arg list is the fallback
    for anything that doesn't follow the convention, rather than silently
    logging nothing."""
    if args and isinstance(args[-1], str) and args[-1].startswith(("http://", "https://")):
        return urllib.parse.urlparse(args[-1]).hostname
    for a in args:
        if isinstance(a, str) and a.startswith(("http://", "https://")):
            return urllib.parse.urlparse(a).hostname
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


def install(script_name):
    """Wrap subprocess.run so every curl call this process makes is logged
    with its target host before the real call runs. Anything that isn't a
    curl invocation (osascript, security, icalBuddy, wrangler, ...) passes
    through completely unchanged — this never alters behaviour, timing, or
    return values, only observes."""
    real_run = subprocess.run

    def wrapped(args, *a, **kw):
        if isinstance(args, list) and args and args[0] == "curl":
            host = _target_host(args)
            if host:
                _log(script_name, host)
        return real_run(args, *a, **kw)

    subprocess.run = wrapped
