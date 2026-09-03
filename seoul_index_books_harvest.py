#!/usr/bin/env python3
"""
Refresh the cached "what Seoul Library lent, by subject" aggregation.

Source: Seoul Open Data, `SeoulLibraryBookRentNumInfo` (OA-15475), read with the
SAME key the rest of the bot uses. Like seoul_index_sales.py this writes a small
JSON the poster reads cheaply, on its own monthly launchd schedule rather than on
the per-post path. Until it has run once, books_facts() in the poster finds no
file and stays silent — the same safe-by-default pattern as the traffic vein.

⚠️ **This replaced 도서관 정보나루 (data4library.kr) on 22 August 2026, and the swap
cost more than a URL.** The data4library key was issued 19 July 2026 and returned
`vitalizationErr` ("API 활성화 상태가아닙니다") on every call for the 34 days that
followed, so the vein never produced a single card. A bogus key of the same shape
gets `authErr` and that one does not, so the key IS recognized: what was never
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

⚠️ **The vein counts SUBJECTS, not the top ten, and that was a second pass.**
The first version published the checkouts of the most-borrowed book (32), of the
tenth (20) and of the ten combined (245) — three numbers off one short list, none
of which told a reader anything they had not already assumed. The same 3,000
records carry a KDC class on every one, so the interesting figure was there all
along and was being thrown away: literature outruns 어학 eight to one.

⚠️ **The classification is KDC, not DDC, and the two disagree exactly where it
would hurt.** Under DDC a 4 is language and a 7 is the arts; under KDC a 4 is
natural science and a 7 is language. Getting it backwards would file 이기적 유전자
under language and 여행영어 under the arts, and the card would read perfectly.
Verified 22 August 2026 against 서울도서관's own category filter
(`favorLoan?category=N00`, whose tabs name 총류 … 역사, 지리 in KDC order): for
each of the ten classes, the API's top five titles in that class appear on the
library's page for that same class. The labels below are therefore the library's
own words, not a translation table from memory.

⚠️ **Every record must land in a class.** A row whose CLASS_NO is not a digit is
counted as `unclassified` and its loans appear in no subject, which quietly
understates every line without changing their relative order — the shape of
error that reads as a smaller library rather than as a bug. The harvest ABORTS
above UNCLASSIFIED_MAX, and the count rides in the output either way. It was 0 of
3,000 on 22 August 2026.

Counts are per RECORD, not per title: 모순 appears twice with different ISBNs at
30 and 9 and the library's own list keeps them separate. That matters less now
that the vein sums whole classes, but it is why no de-duplication happens here.

⛔ **Do NOT try to manufacture a calendar month by diffing two snapshots.** The
window is ROLLING, so old loans fall out of it and a delta is net change, not
checkouts — it can even go negative. Take the window the library publishes.

Output (books_agg.json):
  {
    "generated_at": "<UTC ISO>",
    "source": "SeoulLibraryBookRentNumInfo",
    "scope_en": "Seoul Library", "scope_ko": "서울도서관",
    "window_days": 60,                       # read from the library, not assumed
    "records": 3000, "unclassified": 0,   # no "period": the card carries no
                                          # dateline — see BOOKS_WINDOW in the
                                          # poster for why
    "subjects": [ {"code": "8", "name_en": "Literature", "name_ko": "문학",
                   "loans": 3625, "titles": 631}, ... ]   # loans desc
  }

The labels live HERE rather than in the poster, so the words on the card and the
words in this file can never be two different things — the same reason scope_en
travels with the figures. A subject with no name is dropped by the poster.

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
SEOUL_TZ = ZoneInfo('Asia/Seoul')

# KDC main classes, in the library's own words (see the docstring: taken from the
# category tabs on lib.seoul.go.kr/statistics/favorLoan, not from a translation
# table). '기술과학' is glossed 'Applied sciences' rather than 'Technology'
# because its most-borrowed titles are medicine and health, which 'Technology'
# beside them would misdescribe.
KDC = {
    '0': ('General works', '총류'),
    '1': ('Philosophy', '철학'),
    '2': ('Religion', '종교'),
    '3': ('Social sciences', '사회과학'),
    '4': ('Natural sciences', '자연과학'),
    '5': ('Applied sciences', '기술과학'),
    '6': ('Arts', '예술'),
    '7': ('Language', '어학'),
    '8': ('Literature', '문학'),
    '9': ('History and geography', '역사·지리'),
}
# Share of records allowed to carry no readable class before the run aborts. Any
# is a smell; a schema change would push it to 100%.
UNCLASSIFIED_MAX = 0.05
# How many of the API's top titles verify_window() checks against the library's
# page. They are no longer published, but they are still the proof that the two
# are the same table, which is what the 60-day window rests on.
TOP_FOR_CHECK = 3

# 서울도서관's own rendering of this same table, and the only place the period is
# stated. See the module docstring: this is a load-bearing dependency, not a
# nicety, and an unreadable page aborts the run.
WINDOW_PAGE = 'https://lib.seoul.go.kr/statistics/favorLoan'
_WINDOW_RE = re.compile(r'최근\s*(\d+)\s*일\s*자료집계')
# How many of the API's top titles must appear on that page for the two to count
# as the same aggregate. Three is enough to be decisive and loose enough to
# survive one title falling off the page's TOP 100 between the extract and now.
CORRESPONDENCE_N = TOP_FOR_CHECK

DRY_RUN = '--dry-run' in sys.argv
_KNOWN_ARGS = {'--dry-run'}
_unknown = [a for a in sys.argv[1:] if a not in _KNOWN_ARGS]
if _unknown:
    sys.exit(f'Unknown argument(s): {" ".join(_unknown)}. '
             f'Recognized: {" ".join(sorted(_KNOWN_ARGS))}.')

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


def tally(parsed):
    """Sum (title, loans, class) triples into KDC subjects.

    Returns (subjects, unclassified) and raises ValueError rather than writing
    anything the card would misreport. Split out from main() so both refusals
    can be tested without a network call — they are the two that would
    otherwise fail silently and plausibly."""
    loans = {c: 0 for c in KDC}
    titles = {c: 0 for c in KDC}
    unclassified = 0
    for _title, cnt, cls in parsed:
        if cls not in KDC:
            unclassified += 1
            continue
        loans[cls] += cnt
        titles[cls] += 1
    share = unclassified / len(parsed) if parsed else 1.0
    if share > UNCLASSIFIED_MAX:
        raise ValueError(
            f'{unclassified} of {len(parsed)} records ({share:.1%}) carry no KDC '
            f'class — above the {UNCLASSIFIED_MAX:.0%} ceiling. Their loans would '
            f'go missing from every subject without changing the order, so the '
            f'shares would read fine and be wrong. Refusing to write; check '
            f'CLASS_NO against a live response.')
    subjects = [{'code': c, 'name_en': KDC[c][0], 'name_ko': KDC[c][1],
                 'loans': loans[c], 'titles': titles[c]}
                for c in KDC if titles[c]]
    subjects.sort(key=lambda s: (-s['loans'], s['code']))
    if len(subjects) < 4:
        raise ValueError(f'Only {len(subjects)} subject(s) have any loans — '
                         f'refusing to write a set too small to build a card from.')
    return subjects, unclassified


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

    parsed = []
    for x in rows:
        try:
            cnt = int(str(x.get('CNT', '')).replace(',', ''))
        except ValueError:
            continue
        title = (x.get('TITLE') or '').strip()
        if not title:
            continue
        parsed.append((title, cnt, (x.get('CLASS_NO') or '').strip()[:1]))

    if len(parsed) < 2:
        sys.exit(f'Only {len(parsed)} record(s) parsed from {len(rows)} row(s) — '
                 f'refusing to write a set too small to post.')

    try:
        subjects, unclassified = tally(parsed)
    except ValueError as e:
        sys.exit(str(e))

    # Ties broken by title so the check is deterministic; these titles are used
    # ONLY to prove the API is still serving the library's own list (see
    # verify_window) and are never published.
    top = sorted(parsed, key=lambda t: (-t[1], t[0]))[:TOP_FOR_CHECK]
    days = verify_window([t[0] for t in top])

    # For the operator's line below only: the card carries no date, because the
    # figures' period is the rolling window and not the day they were read.
    label_en = datetime.now(SEOUL_TZ).strftime('%-d %B')
    out = {
        'generated_at': datetime.now(timezone.utc).isoformat(),
        'source': SERVICE,
        'scope_en': 'Seoul Library',
        'scope_ko': '서울도서관',
        'window_days': days,
        'records': len(parsed),
        'unclassified': unclassified,
        'subjects': subjects,
    }

    print(f'Seoul Library loans by subject, last {days} days '
          f'(as of {label_en}; {total:,} records scanned, '
          f'{unclassified} unclassified):')
    for s in subjects:
        print(f'  {s["name_ko"]:<6} {s["name_en"]:<22} {s["loans"]:>6}  '
              f'({s["titles"]} titles)')
    if DRY_RUN:
        print('\n(dry run — parsed, not writing books_agg.json)')
        return
    tmp = OUT.with_name(OUT.name + '.tmp')
    tmp.write_text(json.dumps(out, ensure_ascii=False))
    tmp.replace(OUT)
    print(f'Wrote {OUT} ({len(subjects)} subjects, '
          f'{sum(s["loans"] for s in subjects):,} loans).')


if __name__ == '__main__':
    main()
