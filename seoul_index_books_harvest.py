#!/usr/bin/env python3
"""
Refresh the cached "most-borrowed books in Seoul" aggregation for the bot.

Source: 도서관 정보나루 / data4library.kr — the national library big-data portal,
a DIFFERENT publisher from data.seoul.go.kr, so the books vein carries its own
credit (data4library.kr) on the card. The popular-loans endpoint (loanItemSrch)
returns the most-loaned titles for a region and period, with a loan count each.

Like seoul_index_sales.py this writes a small JSON the poster reads cheaply, and
the underlying data moves at most monthly, so it belongs on its OWN monthly
launchd schedule (not the per-post path). Until this has run once, books_facts()
in the poster finds no file and stays silent — the same safe-by-default pattern
as the traffic vein.

VERIFIED 28 Jul 2026: the endpoint honours &format=json and wraps results in
{"response": {...}}; a bad key returns {"response":{"errCode":"authErr",...}}.
NOT yet verified end to end (needs the real data4library_key, which lives on the
Mini): the exact field names inside a SUCCESSFUL response.docs[].doc. They follow
data4library's documented schema — bookname, authors, loan_count, ranking — and
are read defensively below; confirm them on the first live run and adjust the
_FIELD_* constants if the portal ever renames one.

Output (books_agg.json):
  {
    "generated_at": "<UTC ISO>",
    "region": "11",                          # 서울 (standard sido code)
    "period": {"startDt": "2026-06-01", "endDt": "2026-06-30",
               "label_en": "June 2026", "label_ko": "2026년 6월"},
    "books": [ {"ranking": 1, "bookname": "...", "authors": "...",
                "loan_count": 12345}, ... ]   # top TOP_N, ranking order
  }

Usage:
  python3 seoul_index_books_harvest.py            # refresh for last full month
  python3 seoul_index_books_harvest.py --dry-run  # fetch + print, do not write
"""

import json
import subprocess
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path

import net_guard

HERE = Path(__file__).parent
CONFIG = HERE / 'seoul_index_config.json'
OUT = HERE / 'books_agg.json'

# data4library popular-loans API. region 11 = 서울특별시 (the standard sido code
# these government feeds share — the same '11' the MOLIT vein uses for Seoul).
API = 'http://data4library.kr/api/loanItemSrch'
REGION = '11'
TOP_N = 10

# Response field names inside response.docs[].doc — read via these so a portal
# rename is a one-line fix, not a scavenger hunt (see the module docstring).
_FIELD_NAME = 'bookname'
_FIELD_AUTHORS = 'authors'
_FIELD_LOANS = 'loan_count'
_FIELD_RANK = 'ranking'

DRY_RUN = '--dry-run' in sys.argv
_KNOWN_ARGS = {'--dry-run'}
_unknown = [a for a in sys.argv[1:] if a not in _KNOWN_ARGS]
if _unknown:
    sys.exit(f'Unknown argument(s): {" ".join(_unknown)}. '
             f'Recognised: {" ".join(sorted(_KNOWN_ARGS))}.')

_MONTHS_EN = ['', 'January', 'February', 'March', 'April', 'May', 'June',
              'July', 'August', 'September', 'October', 'November', 'December']


def http_get_json(url):
    """GET + parse JSON via curl, matching the rest of the project (Homebrew
    py3.13 urllib fails HTTPS verify here; curl keeps the transport uniform)."""
    for _ in range(3):
        r = subprocess.run(['curl', '-s', '--max-time', '40', url],
                           capture_output=True, text=True)
        if r.returncode == 0 and r.stdout.strip():
            try:
                return json.loads(r.stdout)
            except json.JSONDecodeError:
                pass
        time.sleep(1)
    raise RuntimeError(f'Request failed after retries: {url}')


def last_full_month(today):
    """First and last day of the most recent COMPLETE calendar month, KST-ish
    (the harvester runs early in a month for the month just ended). Returns
    (start_date, end_date)."""
    first_of_this = today.replace(day=1)
    end = first_of_this - _one_day()
    start = end.replace(day=1)
    return start, end


def _one_day():
    from datetime import timedelta
    return timedelta(days=1)


def main():
    # Monthly, on the 4th: a skipped run waits a month, so give the network a
    # generous half hour before giving up on the harvest.
    net_guard.require_network(1800)

    try:
        cfg = json.loads(CONFIG.read_text())
    except (OSError, ValueError) as e:
        sys.exit(f'Cannot read {CONFIG.name}: {e}')
    key = cfg.get('data4library_key')
    if not key or key.startswith('YOUR_'):
        sys.exit('No data4library_key in config. Register a free authKey at '
                 'https://data4library.kr and put it in seoul_index_config.json.')

    today = date.today()
    start, end = last_full_month(today)
    start_s, end_s = start.strftime('%Y-%m-%d'), end.strftime('%Y-%m-%d')
    label_en = f'{_MONTHS_EN[start.month]} {start.year}'
    label_ko = f'{start.year}년 {start.month}월'

    url = (f'{API}?authKey={key}&format=json&region={REGION}'
           f'&startDt={start_s}&endDt={end_s}&pageNo=1&pageSize={TOP_N}')
    data = http_get_json(url)
    resp = data.get('response', {})
    if resp.get('errCode') or 'error' in resp:
        sys.exit(f'API error: {resp.get("errCode")} {resp.get("error")}')

    # The poster publishes loan COUNTS only (titles are not posted — they would
    # strand mixed script on the English card), so loan_count is the one required
    # field; bookname/authors are kept best-effort for the operator's dry-run
    # print. A title-field rename therefore cannot nuke the whole harvest.
    books = []
    for d in resp.get('docs', []):
        doc = d.get('doc', d)
        loans = doc.get(_FIELD_LOANS)
        if loans in (None, ''):
            continue
        try:
            loans = int(str(loans).replace(',', ''))
        except ValueError:
            continue
        try:
            rank = int(str(doc.get(_FIELD_RANK, len(books) + 1)))
        except ValueError:
            rank = len(books) + 1
        books.append({'ranking': rank,
                      'bookname': (doc.get(_FIELD_NAME) or '').strip(),
                      'authors': (doc.get(_FIELD_AUTHORS) or '').strip(),
                      'loan_count': loans})

    if len(books) < 2:
        sys.exit(f'Only {len(books)} book(s) parsed — refusing to write a set too '
                 f'small to post. Check the field names against a live response '
                 f'(see _FIELD_* in this file).')

    books.sort(key=lambda b: b['ranking'])
    out = {
        'generated_at': datetime.now(timezone.utc).isoformat(),
        'region': REGION,
        'period': {'startDt': start_s, 'endDt': end_s,
                   'label_en': label_en, 'label_ko': label_ko},
        'books': books,
    }

    print(f'Most-borrowed in Seoul, {label_en} ({start_s}..{end_s}):')
    for b in books[:5]:
        print(f'  #{b["ranking"]:>2}  {b["loan_count"]:>7,}  {b["bookname"]}')
    if DRY_RUN:
        print('\n(dry run — parsed, not writing books_agg.json)')
        return
    tmp = OUT.with_name(OUT.name + '.tmp')
    tmp.write_text(json.dumps(out, ensure_ascii=False))
    tmp.replace(OUT)
    print(f'Wrote {OUT} ({len(books)} books).')


if __name__ == '__main__':
    main()
