"""Wait out a spent `claude -p` quota instead of losing the run to it.

On 20 August 2026 the 9 p.m. Old Seoul post died on `claude -p failed (exit 1):
You've hit your limit · resets 11:40pm (Asia/Seoul)`. The refusal names the
moment it clears, and the gap between that bot's firings is twelve hours, so
the run had every ingredient it needed to simply wait: the post was lost for
want of about two and a half hours of patience.

This is net_guard's argument applied to a quota rather than a network.
Recovering inside a budget costs a late post instead of no post, and running
out of budget is one line in the log rather than a traceback.

The pattern on a post-critical call, where losing the model call means losing
the post:

    if result.returncode != 0:
        err = (result.stderr or result.stdout or '').strip() or '(no output)'
        if limit_guard.is_usage_limit(err) and not limit_waited:
            limit_waited = True
            if limit_guard.wait_for_reset(err):
                continue          # try the call again
            sys.exit(0)           # over budget: skip, don't crash
        raise RuntimeError(...)   # anything else is a real fault

The `limit_waited` flag is not optional. Without it a quota that has not
actually cleared sends the run round the same wait forever.

⚠️ is_usage_limit is the load-bearing part of this module, not the waiting.
A spent quota clears itself; an expired OAuth token never does. scan_filer.py
spent five days in August 2026 filing documents unclassified because a 401 was
absorbed by a fallback that could not tell those two apart. Everything this
does not recognise must keep raising, so that a genuine fault still exits
non-zero and harden_audit.sh check 5 still reports it.

⚠️ Exiting 0 when the budget is blown is only safe because something else
notices a bot that has gone quiet. bot_health_check.py alerts when a bot's
last_success_at is over 26 hours old, and a skipped run never touches that
stamp. Do not copy the exit-0 half of this pattern into a job that has no such
watcher.
"""

import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

# Four hours suits a bot firing twice a day. Keep any budget comfortably
# shorter than the caller's own gap between firings, so a waiting run can never
# collide with its successor.
DEFAULT_BUDGET_S = 4 * 3600

# Used only when the refusal names no reset time. A bounded blind wait beats
# losing the post, but it is a fallback action, not a claim about when the
# quota really clears.
BLIND_WAIT_S = 3600

# How far into the past a stated reset time can be and still mean "just now"
# rather than "tomorrow". Covers clock skew between the CLI and this machine.
_JUST_GONE = timedelta(hours=1)

# Phrases that mean "come back later". Deliberately narrow: a loose search for
# "limit" also matches a context-length error, which is a real fault.
_LIMIT_MARKERS = (
    'hit your limit',
    'usage limit',
    'limit reached',
    'rate limit',
    'too many requests',
)

# `resets 11:40pm (Asia/Seoul)`, and the shapes around it: an optional "at",
# optional minutes, optional bracketed zone.
_RESET_RE = re.compile(
    r'resets?\b[^0-9]{0,12}?(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m\.?'
    r'(?:\s*\(\s*([A-Za-z][A-Za-z0-9_+\-/]*)\s*\))?',
    re.I)


def is_usage_limit(err):
    """True if the CLI refused because a quota is spent, not because something
    is broken. See the warning in the module docstring before widening this."""
    low = err.lower()
    return any(marker in low for marker in _LIMIT_MARKERS)


def parse_reset_time(err, now=None):
    """The wall-clock moment the refusal says the quota clears, or None.

    ⚠️ Wall clock, not a monotonic offset. time.monotonic() does not advance
    while a Mac is asleep, so a monotonic deadline would overshoot by however
    long the lid was shut, and these machines do sleep.
    """
    m = _RESET_RE.search(err)
    if not m:
        return None
    hour, minute = int(m.group(1)), int(m.group(2) or 0)
    if not 1 <= hour <= 12 or minute > 59:
        return None
    if m.group(3).lower() == 'p':
        hour = hour if hour == 12 else hour + 12
    elif hour == 12:
        hour = 0

    tz = None
    if m.group(4):
        try:
            tz = ZoneInfo(m.group(4))
        except Exception:
            tz = None  # An unknown zone name falls back to local time.

    if now is None:
        now = datetime.now(tz) if tz else datetime.now().astimezone()
    elif tz is not None:
        now = now.astimezone(tz)

    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now - _JUST_GONE:
        # Well in the past, so the refusal must mean tomorrow: at 9 p.m.,
        # "resets 3am" is six hours off, not eighteen hours ago.
        target += timedelta(days=1)
    # A time only just gone is left in the past on purpose, so the caller
    # retries at once. Rolling it forward would sleep out a whole day over a
    # minute of clock skew between the CLI's clock and this one.
    return target


def wait_for_reset(err, budget_s=DEFAULT_BUDGET_S, log=print, sleep=None,
                   now_fn=None):
    """Sleep until a spent quota clears. True if it is worth trying again.

    Returns False, without sleeping, when the reset is further off than the
    budget allows: the caller should give up cleanly rather than run long.
    """
    if sleep is None:
        import time as _time
        sleep = _time.sleep
    now_fn = now_fn or (lambda: datetime.now().astimezone())

    target = parse_reset_time(err, now=now_fn())
    if target is None:
        wait_s = min(BLIND_WAIT_S, budget_s)
        log(f'claude -p is out of quota and named no reset time. Waiting '
            f'{wait_s // 60} min before one retry. Message: {err[:120]}')
        sleep(wait_s)
        return True

    # A minute past the stated time, because these are not the same clock and
    # landing a second early wastes the entire wait.
    target += timedelta(minutes=1)
    wait_s = (target - now_fn()).total_seconds()
    if wait_s <= 0:
        return True
    if wait_s > budget_s:
        log(f'claude -p is out of quota until {target:%H:%M}, which is '
            f'{wait_s / 3600:.1f} h away and past the {budget_s / 3600:.0f} h '
            f'budget. Skipping this run; the next firing will try again.')
        return False

    log(f'claude -p is out of quota until {target:%H:%M}. Waiting '
        f'{wait_s / 60:.0f} min, then retrying once.')
    # Sleep in chunks and re-read the clock each time, so a mid-wait sleep/wake
    # is absorbed rather than leaving the run short or long.
    while True:
        remaining = (target - now_fn()).total_seconds()
        if remaining <= 0:
            return True
        sleep(min(60, remaining))
