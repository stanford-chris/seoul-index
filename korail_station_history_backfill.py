#!/usr/bin/env python3
"""Fill korail_station_history.json from everything mainLineStationPer still
serves: about a year of daily rows for every Korail station.

    python3 korail_station_history_backfill.py

Pages the whole operation (5,000 rows a page, about 19 pages) and folds the
days in with korail_history_add, the feed's value winning, so a re-run
refreshes rather than duplicates. Read-only against the feed; writes only
the history file. The bot itself keeps the file current from then on, four
pages a run (see KORAIL_HISTORY in seoul_index_post.py).
"""
import json
import sys

import seoul_index_post as sp


def main():
    key = json.load(open(sp.CONFIG))['data_go_kr_key']
    h = sp.load_korail_history()
    before = len(h['days'])
    items, page = [], 1
    while True:
        got = sp._korail_fetch(key, 'mainLineStationPer', sp.KORAIL_PAGE_ROWS, page)
        if not got:
            break
        items += got
        print(f'page {page}: {len(got)} rows', flush=True)
        page += 1
    if not items:
        sys.exit('nothing fetched; history untouched')
    days = sp.korail_days_from_rows(items)
    if sp.korail_history_add(h, days):
        sp.save_korail_history(h)
    print(f'{len(items)} rows, {len(days)} days fetched ({min(days)} to {max(days)}); '
          f'history {before} -> {len(h["days"])} days at {sp.KORAIL_HISTORY}')


if __name__ == '__main__':
    main()
