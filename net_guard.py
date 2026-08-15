"""Gate a run on the machine actually having a working path out.

Between 11 and 15 August 2026 the Mac mini stayed associated to Wi-Fi with a
valid DHCP lease and no working uplink for 89 hours. Every bot on the machine
died the same way, every firing: `claude -p` raised EHOSTUNREACH, curl exited
7, and launchd recorded a traceback instead of a post. The retries already in
the scripts did not help, because they all sat inside a single run and gave up
after seconds.

wait_for_network() goes at the top of main(). When there is no path out it
backs off and keeps probing up to a budget the caller sets from its own
schedule. Recovering inside that budget costs a late post instead of no post;
missing it exits 0 with one line rather than a traceback, so the launchd log
stays readable and the next firing starts clean.

Probes are TCP connects to named hosts, deliberately not pings: the failure
mode was an associated interface with no route, a state where ICMP and a
cached DNS answer can both still look healthy. Connecting by name proves
resolution and routing together, which is what a run actually needs.

This never hides a long outage. bot_health_check.py alerts on how long it has
been since each account last posted, so runs that skip themselves still raise
the alarm if the network stays down.
"""

import socket
import sys
import time

# Two independent operators, so one provider's bad day does not read as the
# machine being offline. Port 443 rather than 53: what broke in August 2026
# was outbound TCP routing, and 443 is the port the runs themselves need.
DEFAULT_PROBES = (('one.one.one.one', 443), ('dns.google', 443))

PROBE_TIMEOUT = 5.0     # seconds per probe, so one check costs at most ~10s
FIRST_DELAY = 15        # seconds; doubles per failed check
MAX_DELAY = 120


def _mins(seconds):
    return f'{seconds // 60} min' if seconds >= 60 else f'{seconds}s'


def network_is_up(probes=DEFAULT_PROBES, timeout=PROBE_TIMEOUT):
    """True as soon as any probe completes a TCP handshake."""
    for host, port in probes:
        try:
            socket.create_connection((host, port), timeout=timeout).close()
            return True
        except OSError:
            continue
    return False


def wait_for_network(budget_s, probes=DEFAULT_PROBES, timeout=PROBE_TIMEOUT,
                     log=print):
    """Block until the machine has a path out, or until budget_s is spent.

    Returns True if the network is up, immediately or after waiting, and False
    if the budget ran out. Keep budget_s comfortably shorter than the gap to
    the caller's next scheduled firing, so a waiting run never collides with
    its own successor: the last probe can overrun the budget by up to one
    round of timeouts (~10s at the defaults).
    """
    if network_is_up(probes, timeout):
        return True

    log(f'No network path out. Retrying for up to {_mins(budget_s)}.')
    deadline = time.monotonic() + budget_s
    delay = FIRST_DELAY
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(delay, remaining))
        if network_is_up(probes, timeout):
            waited = int(budget_s - (deadline - time.monotonic()))
            log(f'Network back after {waited}s. Continuing.')
            return True
        delay = min(delay * 2, MAX_DELAY)

    log(f'Still no network after {_mins(budget_s)}. Skipping this run; '
        f'the next scheduled firing will try again.')
    return False


def require_network(budget_s, **kwargs):
    """wait_for_network(), exiting 0 instead of returning False.

    Exit 0 rather than raising: an unreachable network is not this run's bug,
    and it was a traceback per firing that made the August 2026 outage
    unreadable in the launchd logs.
    """
    if not wait_for_network(budget_s, **kwargs):
        sys.exit(0)
