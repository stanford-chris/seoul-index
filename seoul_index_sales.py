#!/usr/bin/env python3
"""
Refresh the cached commercial-district sales aggregation for the Seoul Index bot.

The Seoul Open Data sales dataset (VwsmTrdarSelngQq — 상권 추정매출) has ~460k rows
and is NOT ordered by quarter (it interleaves by district), so the only honest way
to total a quarter by industry is a full scan. That is slow (~460 API calls, a few
minutes), and the underlying data only changes quarterly, so this runs on its own
MONTHLY launchd schedule (3rd at 05:00) and writes sales_agg.json for the poster
to read cheaply. It was weekly until 20 July 2026; a weekly full scan spent ~460
calls recomputing a figure that moves four times a year, and collided with the
hourly crowd sampler's daily budget.

Output (sales_agg.json):
  {
    "generated_at": "<UTC ISO>",
    "latest_quarter": "20261",              # e.g. 2026 Q1
    "quarters": {"20211": 22239, ...},       # row count per quarter (coverage)
    "by_quarter": {                          # per-quarter, per-industry totals
      "20261": {"커피-음료": {"amt": 651.4e9, "co": 77764522.0}, ...},
      ...
    }
  }

Usage:
  python3 seoul_index_sales.py            # full refresh
  python3 seoul_index_sales.py --quick    # latest 2 quarters only (faster sanity run)
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
SERVICE = 'VwsmTrdarSelngQq'
PAGE = 1000
QUICK = '--quick' in sys.argv


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
    # generous half hour before giving up on the scan.
    net_guard.require_network(1800)

    api_key = json.loads(CONFIG.read_text())['api_key']
    base = f'http://openapi.seoul.go.kr:8088/{api_key}/json/{SERVICE}'

    # Total row count from a 1-row probe.
    probe = http_get_json(f'{base}/1/1/')
    total = int(probe[SERVICE]['list_total_count'])
    print(f'{SERVICE}: {total:,} rows to scan (page size {PAGE})')

    quarters = defaultdict(int)
    by_q = defaultdict(lambda: defaultdict(lambda: {'amt': 0.0, 'co': 0.0}))

    # Quarters are interleaved across the row range, so we always scan every row
    # (that is what makes a quarter total honest). --quick only trims the written
    # output to the latest two quarters afterwards; it does not skip the scan.
    for start in range(1, total + 1, PAGE):
        end = min(start + PAGE - 1, total)
        rows = http_get_json(f'{base}/{start}/{end}/').get(SERVICE, {}).get('row', [])
        for x in rows:
            qc = x.get('STDR_YYQU_CD')
            if not qc:
                continue
            quarters[qc] += 1
            ind = x.get('SVC_INDUTY_CD_NM', '?')
            cell = by_q[qc][ind]
            cell['amt'] += float(x.get('THSMON_SELNG_AMT') or 0)
            cell['co'] += float(x.get('THSMON_SELNG_CO') or 0)
        if (start - 1) % 50000 == 0:
            print(f'  ...scanned to row {start:,}', flush=True)

    latest = max(quarters) if quarters else None
    if QUICK and latest:
        # Trim to the latest two quarters to keep the quick run's output small.
        keep2 = sorted(quarters)[-2:]
        by_q = {q: by_q[q] for q in keep2}

    out = {
        'generated_at': datetime.now(timezone.utc).isoformat(),
        'latest_quarter': latest,
        'quarters': dict(sorted(quarters.items())),
        'by_quarter': {q: {i: v for i, v in inds.items()}
                       for q, inds in by_q.items()},
    }
    OUT.write_text(json.dumps(out, ensure_ascii=False))
    top = sorted(by_q.get(latest, {}).items(),
                 key=lambda kv: -kv[1]['amt'])[:3] if latest else []
    print(f'Latest quarter: {latest}  ({quarters.get(latest, 0):,} rows)')
    print('Top industries: ' + ', '.join(f'{k} ₩{v["amt"]/1e9:.0f}bn' for k, v in top))
    print(f'Wrote {OUT}')


if __name__ == '__main__':
    main()
