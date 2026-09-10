#!/usr/bin/env python3
"""Backfill bus_route_history.json from CardBusStatisticsServiceNew.

    python3 bus_route_history_backfill.py --days 63    # from the newest published day back

Saves after every day, so an interrupted run keeps what it fetched and a
re-run skips the days already held (bus_history_add is idempotent). Read-
only against the feed; writes only the history file. About 42 calls a day.
"""
import json
import sys
from datetime import datetime, timedelta

import seoul_index_post as sp


def main():
    n = 63
    for a in sys.argv[1:]:
        if a.startswith('--days='):
            n = int(a.split('=', 1)[1])
    key = json.load(open(sp.CONFIG))['api_key']
    base = f'http://openapi.seoul.go.kr:8088/{key}/json'
    newest, _ = sp._latest_daily(key, 'CardBusStatisticsServiceNew', True)
    if not newest:
        sys.exit('no published day found')
    h = sp.load_bus_history()
    d = datetime.strptime(newest, '%Y%m%d')
    for back in range(n):
        day = (d - timedelta(days=back)).strftime('%Y%m%d')
        if day in h['days'] and day in h.get('stops', {}):
            continue
        d0 = sp.http_get_json(f'{base}/CardBusStatisticsServiceNew/1/1/{day}')
        tot = int(d0['CardBusStatisticsServiceNew']['list_total_count'])
        if tot == 0:
            print(day, 'no rows, skipped', flush=True)
            continue
        sums, stops = {}, {}
        for s in range(1, tot + 1, 1000):
            bd = sp.http_get_json(f'{base}/CardBusStatisticsServiceNew/{s}/{min(s + 999, tot)}/{day}')
            for x in bd.get('CardBusStatisticsServiceNew', {}).get('row', []):
                no = x.get('RTE_NO', '?')
                v = int(x.get('GTON_TNOPE', '0') or 0)
                sums[no] = sums.get(no, 0) + v
                if v > 0:
                    stops[no] = stops.get(no, 0) + 1
        sp.bus_history_add(h, day, sums, stops)
        sp.save_bus_history(h)
        print(day, len(h['days'][day]), 'routes', len(h['stops'][day]), 'with stop counts', flush=True)
    for year in sorted({k[:4] for k in h['days']}):
        if sp.kr_holidays(h, year) is None:
            print('holidays for', year, 'NOT fetched')
    sp.save_bus_history(h)
    print('history holds', len(h['days']), 'days')


if __name__ == '__main__':
    sys.argv.append('--dry-run')   # the poster's argv flags; never posts from here
    main()
