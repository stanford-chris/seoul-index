#!/usr/bin/env python3
"""
Refresh the cached citywide sales aggregation for the Seoul Index bot.

⚠️⚠️ Since 7 October 2026 this reads VwsmMegaSelngW (상권분석서비스 추정매출,
서울시 단위): the city's OWN citywide estimate, one row per industry per
quarter, about 1,400 rows in all. Until then it summed VwsmTrdarSelngQq over
~1,650 commercial districts (상권), which neither covers the city nor
partitions it: measured for 2026 Q2 against the citywide figure, the district
sum was 42 percent of internet cafés, 65 percent of bookshops and 77 percent of
convenience stores, and 118 percent of motels, so the districts overlap as
well as leave gaps. Each industry was off by a different amount, which bent
every dead-heat and gap pair the card drew. The figures are the city's model
estimates, which the card's footnote says.

Output (sales_agg.json), unchanged in shape:
  {
    "generated_at": "<UTC ISO>",
    "source": "VwsmMegaSelngW",
    "latest_quarter": "20262",
    "quarters": {"20211": 63, ...},          # industries per quarter (coverage)
    "by_quarter": {"20262": {"커피-음료": {"amt": ..., "co": ...}, ...}, ...}
  }

Usage:
  python3 seoul_index_sales.py
"""

import json
import subprocess
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import net_guard

HERE = Path(__file__).parent
CONFIG = HERE / 'seoul_index_config.json'
OUT = HERE / 'sales_agg.json'
SERVICE = 'VwsmMegaSelngW'
SEOUL_MEGA_CD = '11'
# A quarter with fewer industries than this is partial and is never written
# over a good file: 63 a quarter in every quarter measured 7 October 2026.
MIN_INDUSTRIES = 50
PAGE = 1000


def http_get_json(url):
    """GET + parse JSON via curl (Homebrew py3.13 urllib fails HTTPS verify here;
    curl also keeps the plain-HTTP Seoul endpoint uniform)."""
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


def main():
    # Monthly, on the 3rd: a skipped run waits a month, so give the network a
    # generous half hour.
    net_guard.require_network(1800)

    api_key = json.loads(CONFIG.read_text())['api_key']
    base = f'http://openapi.seoul.go.kr:8088/{api_key}/json/{SERVICE}'
    total = int(http_get_json(f'{base}/1/1/')[SERVICE]['list_total_count'])
    print(f'{SERVICE}: {total:,} rows')

    quarters = defaultdict(int)
    by_q = defaultdict(dict)
    for start in range(1, total + 1, PAGE):
        end = min(start + PAGE - 1, total)
        for x in http_get_json(f'{base}/{start}/{end}/').get(SERVICE, {}).get('row', []):
            qc = x.get('STDR_YYQU_CD')
            if not qc or str(x.get('MEGA_CD')) != SEOUL_MEGA_CD:
                continue
            quarters[qc] += 1
            by_q[qc][x.get('SVC_INDUTY_CD_NM', '?')] = {
                'amt': float(x.get('THSMON_SELNG_AMT') or 0),
                'co': float(x.get('THSMON_SELNG_CO') or 0)}

    latest = max(quarters) if quarters else None
    if not latest or quarters[latest] < MIN_INDUSTRIES:
        sys.exit(f'Refusing to write: latest quarter {latest} has '
                 f'{quarters.get(latest, 0)} industries (minimum {MIN_INDUSTRIES}).')
    out = {
        'generated_at': datetime.now(timezone.utc).isoformat(),
        'source': SERVICE,
        'latest_quarter': latest,
        'quarters': dict(sorted(quarters.items())),
        'by_quarter': {q: dict(inds) for q, inds in by_q.items()},
    }
    OUT.write_text(json.dumps(out, ensure_ascii=False))
    top = sorted(by_q[latest].items(), key=lambda kv: -kv[1]['amt'])[:3]
    print(f'Latest quarter: {latest}  ({quarters[latest]} industries)')
    print('Top industries: ' + ', '.join(f'{k} ₩{v["amt"]/1e9:.0f}bn' for k, v in top))
    print(f'Wrote {OUT}')


if __name__ == '__main__':
    main()
