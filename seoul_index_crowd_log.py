#!/usr/bin/env python3
"""
Hourly crowd sampler for the Seoul Index bot.

citydata_ppltn is a present-and-future endpoint: it gives the crowd right now
and a 12-hour forecast, and nothing at all about the past. So a line like
"typical for a Monday at this hour" cannot be fetched — it has to be
accumulated. This script is that accumulation: run hourly by launchd, it takes
one reading per curated spot and appends it to crowd_history.jsonl.

Hourly from 05:00 to 23:00, not round the clock. Overnight readings are the
least informative (the curves are flat and the bot never posts then), and
skipping them keeps even the monthly sales-scan day inside the only daily call
limit Seoul publishes. The cost is that a spotlight card run between midnight
and 5 a.m. has no baseline to show, which it handles by omitting the line.

Only OBSERVED readings are logged. The forecast is deliberately discarded: a
baseline built from predictions would be a baseline of what the model expected,
not of what happened, and the whole point of the log is to escape the forecast
caveat.

The file is append-only JSONL, one reading per line, so a truncated write can
only ever cost the last line. At 26 spots x 19 hours it grows by ~180k lines a
year (some tens of MB) — small enough that pruning is not worth the risk of throwing
away history we cannot re-fetch.

Usage:
    python3 seoul_index_crowd_log.py           # one sampling pass
    python3 seoul_index_crowd_log.py --stats   # summarise what has accrued
"""

import json
import subprocess
import sys
import urllib.parse
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import net_guard

HERE = Path(__file__).parent
CONFIG = HERE / 'seoul_index_config.json'
HISTORY = HERE / 'crowd_history.jsonl'
# ⚠️ A SEPARATE FILE, not more lines in crowd_history.jsonl. load_history() and
# show_stats() below assume every line is a crowd reading with 'area' and 'mid';
# a second record shape in the same file would not fail, it would quietly skew
# the baselines the crowd cards are built from.
BIKE_HISTORY = HERE / 'bike_history.jsonl'
SEOUL_TZ = ZoneInfo('Asia/Seoul')
STATS = '--stats' in sys.argv

# Kept in step with seoul_index_post.CROWD_SPOTS by importing it, so the log and
# the posts can never drift onto different sets of places.
try:
    from seoul_index_post import CROWD_SPOTS, bike_counts
except ImportError:                                  # stand alone if need be
    CROWD_SPOTS = [{'area': '잠실 관광특구', 'en': 'Jamsil', 'ko': '잠실'}]


def http_get_json(url):
    """GET + parse JSON via curl, matching the rest of the bot (Homebrew py3.13
    fails HTTPS cert verify here, and the Seoul endpoint is plain HTTP anyway)."""
    r = subprocess.run(['curl', '-s', '--max-time', '30', url],
                       capture_output=True, text=True)
    if r.returncode != 0 or not r.stdout.strip():
        raise RuntimeError(f'curl failed (exit {r.returncode})')
    return json.loads(r.stdout)


def sample(api_key):
    """One reading per spot. Returns (records, problems). A spot that fails is
    skipped, never fatal: a sampler that dies on one bad response would leave
    gaps in exactly the history it exists to build."""
    base = f'http://openapi.seoul.go.kr:8088/{api_key}/json/citydata_ppltn'
    stamp = datetime.now(SEOUL_TZ)
    records, problems = [], []
    for spot in CROWD_SPOTS:
        area, en = spot['area'], spot['en']
        try:
            d = http_get_json(f'{base}/1/1/{urllib.parse.quote(area)}')
            # The API reports quota and key problems in RESULT.CODE rather than
            # by failing the request, so surface anything that is not INFO-000:
            # this is how we would find out a daily call limit exists at all.
            code = (d.get('RESULT') or {}).get('CODE')
            if code and code != 'INFO-000':
                problems.append(f'{en}: {code} {(d.get("RESULT") or {}).get("MESSAGE", "")}')
                continue
            r = d['SeoulRtd.citydata_ppltn'][0]
            records.append({
                'at': stamp.strftime('%Y-%m-%d %H:%M'),
                'weekday': stamp.strftime('%a'),
                'hour': stamp.hour,
                'area': en,
                'mid': (int(r['AREA_PPLTN_MIN']) + int(r['AREA_PPLTN_MAX'])) // 2,
                'min': int(r['AREA_PPLTN_MIN']),
                'max': int(r['AREA_PPLTN_MAX']),
                'level': r.get('AREA_CONGEST_LVL', ''),
                # The reading's own timestamp, which lags the wall clock by a
                # few minutes and is what the figure actually describes.
                'ppltn_time': r.get('PPLTN_TIME', ''),
            })
        except (RuntimeError, KeyError, IndexError, ValueError,
                json.JSONDecodeError) as e:
            problems.append(f'{en}: {type(e).__name__} {e}')
    return records, problems


def sample_bikes(api_key):
    """One citywide Ttareungi reading, or None.

    ⚠️⚠️ THIS EXISTS BECAUSE SEOUL STOPPED PUBLISHING THE HISTORY, and the check
    was made rather than assumed (27 August 2026). Every maintained 따릉이
    dataset on data.seoul.go.kr is a FLOW — rentals and returns, daily, monthly,
    per station — or the live snapshot this reads. The only STOCK history,
    "일별 대여소별 거치수량", covers January 2019 to May 2021 and has not been
    added to since. Stock cannot be derived from flow here, because the
    rebalancing trucks move bikes between docks and that movement is never a
    rental. So "how many docks stood empty at six on a Thursday" is, for
    anything after May 2021, only knowable if somebody wrote it down.

    ⚠️ THE AGGREGATE ONLY, and the per-station question is deliberately left
    open rather than silently decided. Per-station stock is what Seoul used to
    publish and is the richer record, at a measured ~225 MB a year against about
    1 MB for this; and ~/Projects now goes to the NAS nightly with no delta
    compression (Matched data: 0 on every run), so that is 225 MB re-sent every
    night. The API cost is identical either way — every row must be fetched to
    total them — so the choice is storage, not calls, and it can be revisited.
    What cannot be revisited is the stretch of history that passes before it is.

    ⚠️ The paging lives in seoul_index_post.bike_counts and is NOT copied here.
    """
    got = bike_counts(api_key)
    if not got:
        return None
    stations, bikes, racks, empty = got
    stamp = datetime.now(SEOUL_TZ)
    return {
        'at': stamp.strftime('%Y-%m-%d %H:%M'),
        'weekday': stamp.strftime('%a'),
        'hour': stamp.hour,
        'stations': stations,
        'bikes': bikes,
        'racks': racks,
        'empty': empty,
    }


def load_history():
    """Every reading logged so far. Skips any malformed line rather than dying,
    so one bad append can never cost us the whole history."""
    if not HISTORY.exists():
        return []
    out = []
    for line in HISTORY.read_text(encoding='utf-8').splitlines():
        if not line.strip():
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def baseline(area, weekday, hour, rows=None, min_samples=3):
    """Typical observed crowd at `area` on a given weekday and hour, or None if
    too little has accrued to say. Returns (mean, number_of_days).

    Averages over DAYS, not readings: each date in the slot contributes one
    value (its own mean), so an hour that happened to get sampled twice — a
    catch-up run, a manual kickstart — does not get double the weight of every
    other Monday.

    min_samples is the honesty gate. With one or two Mondays this would be
    presenting a single Monday as what Mondays are like, so callers get None and
    leave the line out until the history has earned it."""
    rows = load_history() if rows is None else rows
    per_day = defaultdict(list)
    for r in rows:
        if (r.get('area') == area and r.get('weekday') == weekday
                and r.get('hour') == hour and r.get('at')):
            per_day[r['at'][:10]].append(r['mid'])
    if len(per_day) < min_samples:
        return None
    day_means = [sum(v) / len(v) for v in per_day.values()]
    return round(sum(day_means) / len(day_means)), len(day_means)


def show_stats():
    rows = load_history()
    if not rows:
        print(f'No history yet ({HISTORY} does not exist or is empty).')
        return
    by_area = defaultdict(int)
    slots = defaultdict(set)
    for r in rows:
        by_area[r.get('area', '?')] += 1
        slots[(r.get('area'), r.get('weekday'), r.get('hour'))].add((r.get('at') or '')[:10])
    ready = sum(1 for days in slots.values() if len(days) >= 3)
    print(f'{len(rows):,} readings over {len(slots):,} area/weekday/hour slots')
    print(f'first: {rows[0].get("at")}   last: {rows[-1].get("at")}')
    print(f'slots with >=3 distinct days (usable as a baseline): {ready:,} of {len(slots):,}')
    for area, n in sorted(by_area.items(), key=lambda kv: -kv[1]):
        print(f'  {area:<24} {n:,}')


def main():
    if STATS:
        show_stats()
        return

    # Sampling runs hourly, so wait ten minutes at most: an hour of readings is
    # worth saving, but a run must never still be waiting when the next fires.
    # Without this the August 2026 outage wrote 89 hours of "logged 0/26 spots;
    # 26 problem(s)" lines, one per spot, in place of the readings.
    net_guard.require_network(600)

    api_key = json.loads(CONFIG.read_text())['api_key']
    records, problems = sample(api_key)
    if records:
        with HISTORY.open('a', encoding='utf-8') as fh:
            for rec in records:
                fh.write(json.dumps(rec, ensure_ascii=False) + '\n')
    # ⚠️ AFTER the crowd write and independent of it. The two feeds fail
    # separately, and a bikeList outage must not cost the hour's crowd readings
    # (or the reverse). Same reason sample() skips a bad spot instead of dying.
    bike = sample_bikes(api_key)
    if bike:
        with BIKE_HISTORY.open('a', encoding='utf-8') as fh:
            fh.write(json.dumps(bike, ensure_ascii=False) + '\n')

    stamp = datetime.now(SEOUL_TZ).strftime('%Y-%m-%d %H:%M')
    bikenote = (f"; bikes {bike['bikes']:,} at {bike['stations']:,} stations, "
                f"{bike['empty']:,} empty" if bike else '; bikes NOT READ')
    print(f'[{stamp}] logged {len(records)}/{len(CROWD_SPOTS)} spots{bikenote}'
          + (f'; {len(problems)} problem(s): ' + '; '.join(problems) if problems else ''))


if __name__ == '__main__':
    main()
