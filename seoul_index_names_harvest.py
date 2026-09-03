#!/usr/bin/env python3
"""Regenerate seoul_index_names_en.json: Korean -> English names for the
capital-area subway stations and the Seoul districts.

The Seoul open-data feeds the bot uses (CardSubwayStatsNew,
ListAirQualityByDistrictService) return Korean names only, so the English card
needs a lookup. Official English station names are not romanizations
(홍대입구 is "Hongik Univ.", 시청 is "City Hall"), so a table is unavoidable.

Source: OpenStreetMap via Overpass. No API key, ODbL-licensed, and it carries
name:en for essentially the whole capital-area network including the Korail,
AREX and Sinbundang lines that the Seoul Metro datasets leave out.

Run occasionally (new stations open a few times a year), then commit the JSON:

    python3 seoul_index_names_harvest.py
"""

import json
import re
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE / 'seoul_index_names_en.json'

# overpass-api.de rejects a lot of traffic; kumi is the reliable mirror.
ENDPOINTS = [
    'https://overpass.kumi.systems/api/interpreter',
    'https://overpass.private.coffee/api/interpreter',
    'https://overpass-api.de/api/interpreter',
]

# Hand corrections applied after the harvest, so a re-run can't undo them.
# OSM's name:en is community-maintained and occasionally off-style.
STATION_OVERRIDES = {
    '시청.용인대': 'City Hall-Yongin University',  # OSM has "City hall.Yongin Univ."
}

# Capital area, wide enough for every station in CardSubwayStatsNew.
STATION_Q = '''[out:json][timeout:180];
(
  node(36.9,126.4,38.0,127.8)["railway"~"^(station|halt)$"]["name"];
  way(36.9,126.4,38.0,127.8)["railway"~"^(station|halt)$"]["name"];
);
out tags center;'''

DISTRICT_Q = '''[out:json][timeout:180];
relation(37.4,126.7,37.72,127.2)["boundary"="administrative"]["admin_level"~"^(6|7|8)$"]["name"];
out tags;'''


def overpass(query):
    body = urllib.parse.urlencode({'data': query}).encode()
    last = None
    for url in ENDPOINTS:
        try:
            req = urllib.request.Request(
                url, data=body, headers={'User-Agent': 'seoul-index/1.0'})
            with urllib.request.urlopen(req, timeout=240) as r:
                return json.loads(r.read().decode())
        except Exception as e:  # noqa: BLE001 - any failure means try the next mirror
            last = e
            print(f'  {url} failed: {e}')
    raise RuntimeError(f'all Overpass mirrors failed; last error: {last}')


def stations():
    """Korean station name (no 역 suffix) -> official English name."""
    data = overpass(STATION_Q)
    cand = defaultdict(Counter)
    for el in data['elements']:
        tags = el.get('tags', {})
        ko, en = tags.get('name', ''), tags.get('name:en')
        if not en or not re.search(r'[가-힣]', ko):
            continue
        # The Seoul feed's SBWY_STNS_NM drops the 역 suffix; match that.
        base = ko[:-1] if ko.endswith('역') else ko
        # The card row already says "station", so "Seoul Station" -> "Seoul".
        en = re.sub(r'\s*\(Station\)$', '', en.strip())
        en = re.sub(r'\s+Station$', '', en)
        if en:
            cand[base][en] += 1
    out = {ko: c.most_common(1)[0][0] for ko, c in cand.items()}
    for ko, en in STATION_OVERRIDES.items():
        if ko not in out:
            print(f'  note: override station {ko!r} is not in the harvest')
        out[ko] = en
    return out


def districts():
    """Korean gu name -> English, e.g. 강남구 -> Gangnam-gu."""
    data = overpass(DISTRICT_Q)
    out = {}
    for el in data['elements']:
        tags = el.get('tags', {})
        ko, en = tags.get('name', ''), tags.get('name:en')
        if ko.endswith('구') and en:
            out[ko] = en
    return out


def main():
    print('Fetching stations...')
    st = stations()
    print('Fetching districts...')
    gu = districts()
    if len(st) < 500 or len(gu) < 25:
        raise SystemExit(
            f'Refusing to write a thin table (stations {len(st)}, districts {len(gu)}). '
            'Overpass probably returned a partial result; re-run.')
    OUT.write_text(json.dumps({'stations': st, 'districts': gu},
                              ensure_ascii=False, indent=1, sort_keys=True))
    print(f'Wrote {OUT} — {len(st)} stations, {len(gu)} districts.')


if __name__ == '__main__':
    main()
