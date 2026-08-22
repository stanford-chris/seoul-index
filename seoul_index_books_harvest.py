#!/usr/bin/env python3
"""
Refresh the cached "most-borrowed books at Seoul Library" aggregation.

Source: Seoul Open Data, `SeoulLibraryBookRentNumInfo` (OA-15475), read with the
SAME key the rest of the bot uses. Like seoul_index_sales.py this writes a small
JSON the poster reads cheaply, on its own monthly launchd schedule rather than on
the per-post path. Until it has run once, books_facts() in the poster finds no
file and stays silent — the same safe-by-default pattern as the traffic vein.

⚠️ **This replaced 도서관 정보나루 (data4library.kr) on 22 August 2026, and the swap
cost more than a URL.** The data4library key was issued 19 July 2026 and returned
`vitalizationErr` ("API 활성화 상태가아닙니다") on every call for the 34 days that
followed, so the vein never produced a single card. A bogus key of the same shape
gets `authErr` and that one does not, so the key IS recognised: what was never
switched on is the API, which is an account matter (libdata@korea.kr, 02-595-6131)
and not something code can fix. What the two sources cover is NOT the same thing,
and the card must not pretend otherwise:

  data4library    all of Seoul's 215 public libraries, by calendar month
  this            서울도서관 alone — the city's flagship — rolling 60 days

So every label here says Seoul Library, never "Seoul's public libraries". The
membership vein already carries that same scope, from the same building.

⚠️ **The 60-day window is the whole reason this source is usable, and it is NOT
in the API.** The payload has eight fields (CONTROLNO, TITLE, AUTHOR, PUBLISHER,
PUBLISHER_YEAR, ISBN, CLASS_NO, CNT) and not one of them says what period a count
of 32 covers — which is why this dataset was assessed on 21 August 2026 and
rejected. The period is published by 서울도서관 itself, on the page that serves the
identical table:

    lib.seoul.go.kr/statistics/favorLoan  →  "TOP 100 목록 (최근 60일 자료집계)"

Verified 22 August 2026 by matching the API's top 12 titles against that page:
12 of 12 present, counts identical or within 2 (the API extract runs about a day
behind the live page). So `verify_window()` below re-reads that heading on EVERY
harvest and takes the number from it. Two consequences, both deliberate:
  - The window is never hardcoded. If the library changes it to 30 or 90 days,
    the card follows on the next run instead of publishing a stale claim.
  - **A page that cannot be read, or whose top titles no longer match the API's,
    aborts the harvest.** The entire period claim rests on that correspondence,
    so losing it means we no longer know what the counts cover, and a figure
    whose period is unknown is not publishable. Failing loudly is the point: the
    job exits non-zero and harden_audit.sh check 5 reports it.

⚠️ **Counts are per RECORD, not per title, and that follows the library.** 모순
appears twice with different ISBNs (two editions) at 30 and 9, and the library's
own TOP 100 lists them separately rather than summing to 39. Aggregating would
give a truer answer to "which book" and would break the one public cross-check
this vein has, so it is left alone. It affects 22 of 2,971 ISBN keys and does not
touch the top four.

⛔ **Do NOT try to manufacture a calendar month by diffing two snapshots.** The
window is ROLLING, so old loans fall out of it and a delta is net change, not
checkouts — it can even go negative. Take the window the library publishes.

Output (books_agg.json):
  {
    "generated_at": "<UTC ISO>",
    "source": "SeoulLibraryBookRentNumInfo",
    "scope_en": "Seoul Library", "scope_ko": "서울도서관",
    "window_days": 60,                       # read from the library, not assumed
    "period": {"label_en": "22 August", "label_ko": "8월 22일"},   # as-of date
    "books": [ {"ranking": 1, "bookname": "...", "authors": "...",
                "loan_count": 32}, ... ]     # top TOP_N, ranking order
  }

Usage:
  python3 seoul_index_books_harvest.py            # refresh
  python3 seoul_index_books_harvest.py --dry-run  # fetch + print, do not write
"""

import json
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import net_guard

HERE = Path(__file__).parent
CONFIG = HERE / 'seoul_index_config.json'
OUT = HERE / 'books_agg.json'

SERVICE = 'SeoulLibraryBookRentNumInfo'
PAGE = 1000                     # Seoul's per-call row cap
TOP_N = 10
SEOUL_TZ = ZoneInfo('Asia/Seoul')

# 서울도서관's own rendering of this same table, and the only place the period is
# stated. See the module docstring: this is a load-bearing dependency, not a
# nicety, and an unreadable page aborts the run.
WINDOW_PAGE = 'https://lib.seoul.go.kr/statistics/favorLoan'
_WINDOW_RE = re.compile(r'최근\s*(\d+)\s*일\s*자료집계')
# How many of the API's top titles must appear on that page for the two to count
# as the same aggregate. Three is enough to be decisive and loose enough to
# survive one title falling off the page's TOP 100 between the extract and now.
CORRESPONDENCE_N = 3

DRY_RUN = '--dry-run' in sys.argv
_KNOWN_ARGS = {'--dry-run'}
_unknown = [a for a in sys.argv[1:] if a not in _KNOWN_ARGS]
if _unknown:
    sys.exit(f'Unknown argument(s): {" ".join(_unknown)}. '
             f'Recognised: {" ".join(sorted(_KNOWN_ARGS))}.')

_MONTHS_KO = ['', '1', '2', '3', '4', '5', '6', '7', '8', '9', '10', '11', '12']


def http_get(url):
    """GET as text via curl, matching the rest of the project (Homebrew py3.13
    urllib fails HTTPS verify here; curl keeps the transport uniform)."""
    for _ in range(3):
        r = subprocess.run(['curl', '-s', '--max-time', '40', url],
                           capture_output=True, text=True)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout
        time.sleep(1)
    raise RuntimeError(f'Request failed after retries: {url}')


def http_get_json(url):
    for _ in range(3):
        try:
            return json.loads(http_get(url))
        except json.JSONDecodeError:
            time.sleep(1)
    raise RuntimeError(f'Response was not JSON after retries: {url}')


def verify_window(top_titles):
    """Read the loan window from 서울도서관's own page, and confirm that page is
    still showing the same table the API just served.

    Returns the window in days. Raises on anything less than a confident answer:
    the period is the one thing the API cannot tell us, so a guess here would put
    an unverifiable claim on the card."""
    page = http_get(WINDOW_PAGE)
    m = _WINDOW_RE.search(page)
    if not m:
        raise RuntimeError(
            f'{WINDOW_PAGE} no longer states its aggregation window '
            f'("최근 N일 자료집계"). The API publishes no period of its own, so '
            f'there is nothing left to date these counts by — refusing to write.')
    days = int(m.group(1))
    if not 1 <= days <= 400:
        raise RuntimeError(f'Implausible window read from {WINDOW_PAGE}: {days} days.')
    # The window belongs to the library's list; it is only ours to borrow while
    # the API is serving that same list.
    hits = [t for t in top_titles[:CORRESPONDENCE_N] if t and t in page]
    if len(hits) < len(top_titles[:CORRESPONDENCE_N]):
        missing = [t for t in top_titles[:CORRESPONDENCE_N] if t not in hits]
        raise RuntimeError(
            f'The API\'s top titles are not on {WINDOW_PAGE} '
            f'({len(hits)}/{CORRESPONDENCE_N} matched; missing: '
            f'{"; ".join(t[:40] for t in missing)}). The two are no longer the '
            f'same aggregate, so the page\'s {days}-day window cannot be claimed '
            f'for these counts — refusing to write.')
    return days


def main():
    # Monthly: a skipped run waits a month, so give the network a generous half
    # hour before giving up on the harvest.
    net_guard.require_network(1800)

    try:
        cfg = json.loads(CONFIG.read_text())
    except (OSError, ValueError) as e:
        sys.exit(f'Cannot read {CONFIG.name}: {e}')
    key = cfg.get('api_key')
    if not key or key.startswith('YOUR_'):
        sys.exit('No api_key in seoul_index_config.json (Seoul Open Data key).')

    base = f'http://openapi.seoul.go.kr:8088/{key}/json/{SERVICE}'
    probe = http_get_json(f'{base}/1/1/')
    body = probe.get(SERVICE)
    if not body:
        sys.exit(f'Unexpected response from {SERVICE}: {json.dumps(probe)[:200]}')
    code = body.get('RESULT', {}).get('CODE')
    if code and code != 'INFO-000':
        sys.exit(f'API error {code}: {body.get("RESULT", {}).get("MESSAGE")}')
    total = int(body['list_total_count'])

    # The rows are NOT ordered by loan count (row 1 came back at 2 checkouts on
    # 22 August 2026 while row 2 had 32), so the top N can only be had by reading
    # every row. At 3,000 rows that is three calls.
    rows = []
    for start in range(1, total + 1, PAGE):
        end = min(start + PAGE - 1, total)
        rows += http_get_json(f'{base}/{start}/{end}/').get(SERVICE, {}).get('row', [])

    books = []
    for x in rows:
        try:
            cnt = int(str(x.get('CNT', '')).replace(',', ''))
        except ValueError:
            continue
        title = (x.get('TITLE') or '').strip()
        if not title:
            continue
        books.append({'bookname': title,
                      'authors': (x.get('AUTHOR') or '').strip(),
                      'loan_count': cnt})
    # Ties are broken by title so the same day's harvest always ranks the same
    # way: several records share a count well inside the top ten.
    books.sort(key=lambda b: (-b['loan_count'], b['bookname']))
    books = books[:TOP_N]
    for i, b in enumerate(books, 1):
        b['ranking'] = i

    if len(books) < 2:
        sys.exit(f'Only {len(books)} book(s) parsed from {len(rows)} row(s) — '
                 f'refusing to write a set too small to post.')

    days = verify_window([b['bookname'] for b in books])

    now = datetime.now(SEOUL_TZ)
    label_en = now.strftime('%-d %B')
    label_ko = f'{_MONTHS_KO[now.month]}월 {now.day}일'
    out = {
        'generated_at': datetime.now(timezone.utc).isoformat(),
        'source': SERVICE,
        'scope_en': 'Seoul Library',
        'scope_ko': '서울도서관',
        'window_days': days,
        'period': {'label_en': label_en, 'label_ko': label_ko},
        'books': books,
    }

    print(f'Most-borrowed at Seoul Library, last {days} days '
          f'(as of {label_en}; {total:,} records scanned):')
    for b in books:
        print(f'  #{b["ranking"]:>2}  {b["loan_count"]:>5}  {b["bookname"][:60]}')
    if DRY_RUN:
        print('\n(dry run — parsed, not writing books_agg.json)')
        return
    tmp = OUT.with_name(OUT.name + '.tmp')
    tmp.write_text(json.dumps(out, ensure_ascii=False))
    tmp.replace(OUT)
    print(f'Wrote {OUT} ({len(books)} books).')


if __name__ == '__main__':
    main()
