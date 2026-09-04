#!/usr/bin/env python3
"""
Seoul Index (@seoul-index.bsky.social) — daily weather forecast card.

A standalone companion to seoul_index_post.py, posting once a day regardless
of the main index's own rotation: today's forecast for Seoul from KMA's
단기예보 (getVilageFcst), on the same VilageFcstInfoService_2.0 key already
approved and in use for the account's air-observation line, so no new
활용신청 was needed.

⚠️ This is the account's first FORECAST card. Every other vein here states a
figure as published or observed (see kma_facts() in seoul_index_post.py,
whose comment on `_kma_air_at` explicitly rejected Open-Meteo for serving "a
model's estimate" rather than a station reading). A forecast is a prediction
by definition, so the same tension applies to KMA's own numbers — the
difference is that KMA's forecast is itself the published figure, not
something this bot computed. The footnote below says so plainly, the same
way seoul_index_post.py's `forecast=True` note tells a reader that KT's
hourly crowd estimates are forecasts too.

Design (deliberately simpler than seoul_index_post.py's Harper's-Index
selector): there is no `claude -p` step here. A forecast card has no
juxtaposition to curate and no witty framing to earn — it is five numbers
in a fixed shape, so Python builds the whole card, in both languages,
without a model call. High/low come from the day's own hourly TMP series
(always present) rather than the TMN/TMX fields, which are not reliably
published for "today" once the day is under way — see _today_summary().

Requires (for actual posting, not --dry-run):
  - seoul_index_config.json with "data_go_kr_key" and "handle" (shared with
    seoul_index_post.py — same account, same KMA key)
  - the bot's Bluesky app password in the Keychain, exactly as
    seoul_index_post.py already requires it

Usage:
  python3 seoul_weather_post.py            # post today's forecast card
  python3 seoul_weather_post.py --dry-run  # fetch, build, print — no post
"""

import collections
import fcntl
import json
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from pathlib import Path

from atproto import Client, client_utils, models

import net_guard
from seoul_index_card import render_card, CardRenderError, curly
from seoul_index_post import (
    KMA_NOW_NX, KMA_NOW_NY, SEOUL_TZ, KEYCHAIN_SERVICE, TAGS,
    http_get_json, keychain_password, write_json_atomic, strip_emoji,
    source_reply, tag_line, LINK_DOMAINS, _wx_rows, _wx_num,
)

HERE = Path(__file__).parent
CONFIG = HERE / 'seoul_index_config.json'
# One JSONL line per posted card, mirroring card_history.jsonl's convention:
# best-effort, written only on a real post, never fatal if it fails.
WEATHER_LOG = HERE / 'weather_history.jsonl'

_KNOWN_ARGS = {'--dry-run'}
if __name__ == '__main__':
    _unknown = set(sys.argv[1:]) - _KNOWN_ARGS
    if _unknown:
        sys.exit(f'Unknown argument(s): {" ".join(sorted(_unknown))}. '
                 f'Known: {" ".join(sorted(_KNOWN_ARGS))}')
    DRY_RUN = '--dry-run' in sys.argv
else:
    DRY_RUN = False

# The eight getVilageFcst announcement times, KST. Each run is ready roughly
# ten minutes after its base time; a wider margin here is cheap insurance
# against asking data.go.kr for a run that technically exists but has not
# landed on their end yet.
BASE_TIMES = ('0200', '0500', '0800', '1100', '1400', '1700', '2000', '2300')
BASE_PUBLISH_LAG_MIN = 20

SKY_TEXT = {'1': ('Clear', '맑음'), '3': ('Partly cloudy', '구름많음'),
            '4': ('Cloudy', '흐림')}
SKY_EMOJI = {'1': '☀️', '3': '⛅', '4': '☁️'}
PTY_TEXT = {'1': ('Rain', '비'), '2': ('Rain/snow', '비/눈'), '3': ('Snow', '눈'),
            '4': ('Showers', '소나기'), '5': ('Drizzle', '빗방울'),
            '6': ('Drizzle/flurries', '빗방울눈날림'), '7': ('Flurries', '눈날림')}
PTY_EMOJI = {'1': '🌧️', '2': '🌨️', '3': '🌨️', '4': '🌦️', '5': '🌧️',
             '6': '🌨️', '7': '❄️'}


def fmt_c(celsius):
    """'31.0' -> '31°C'; '26.5' -> '26.5°C'. KMA's forecast TMP is published
    as whole degrees, unlike the ASOS observations elsewhere on this account
    (seoul_index_post.py's to_f(), which always carries one decimal) — so a
    trailing ".0" here would just be manufactured precision. Kept general
    rather than assuming TMP is always whole, in case KMA ever changes that."""
    return f'{int(celsius)}°C' if celsius == int(celsius) else f'{celsius:.1f}°C'


def fmt_c_en(celsius):
    """Same, with a rounded Fahrenheit conversion alongside — the English
    card's equivalent of to_f()."""
    f = celsius * 9 / 5 + 32
    return f'{fmt_c(celsius)} ({f:.0f}°F)'


def to_in(mm):
    """'34.8' -> '34.8mm (1.37 in)'. English card only, as with to_f() and
    to_mph() in seoul_index_post.py. Two decimal places, not to_f()'s and
    to_mph()'s whole number: a whole-inch rounding would read every rainy
    day here (typically a few mm to a few tens of mm) as 0 or 1 in, which
    is the "$0.00" failure this account's other conversions never have to
    guard against — 25.4mm is worth a whole degree or a whole mph, but
    it's most of a typical day's total rainfall."""
    return f'{mm:.1f}mm ({mm / 25.4:.2f} in)'


def format_ampm(hour, minute):
    """(5, 0) -> '5 a.m.'; (18, 56) -> '6:56 p.m.' — house style is always
    a.m./p.m., lowercase, with periods; never a bare 24-hour clock."""
    period = 'a.m.' if hour < 12 else 'p.m.'
    hour_12 = hour % 12 or 12
    return f'{hour_12} {period}' if minute == 0 else f'{hour_12}:{minute:02d} {period}'


def fmt_hhmm_ampm(hhmm):
    """'0533' -> '5:33 a.m.' — KASI's raw sunrise/sunset field format."""
    return format_ampm(int(hhmm[:2]), int(hhmm[2:]))


def fmt_hhmm_ampm_ko(hhmm):
    """'0533' -> '오전 5시 33분' — the same 오전/오후 convention _ampm_ko()
    already uses in seoul_index_post.py, extended to carry minutes (that
    one is hour-only; sunrise/sunset is almost never exactly on the hour)."""
    hour, minute = int(hhmm[:2]), int(hhmm[2:])
    period = '오전' if hour < 12 else '오후'
    hour_12 = hour % 12 or 12
    return f'{period} {hour_12}시 {minute:02d}분' if minute else f'{period} {hour_12}시'


def fmt_forecast_rain_en(mm, exact):
    """(2.0, True) -> '2.0mm (0.08 in)'; (2.0, False) -> '2.0mm (0.08 in) or
    more'; (0.0, False) -> 'less than 1mm (0.04 in)' — the one case where
    the summed figure is 0 but the day isn't genuinely dry, because the
    only rain KMA forecast was a '1mm 미만' hour that has no exact mm to
    add. See forecast_rain_today()."""
    if mm == 0.0 and not exact:
        return 'less than 1mm (0.04 in)'
    return to_in(mm) if exact else f'{to_in(mm)} or more'


def fmt_forecast_rain_ko(mm, exact):
    """Korean counterpart of fmt_forecast_rain_en(), same three cases."""
    if mm == 0.0 and not exact:
        return '1mm 미만'
    return f'{mm:.1f}mm' if exact else f'{mm:.1f}mm 이상'


def latest_base(now):
    """(base_date, base_time) of the most recent getVilageFcst run that
    should already be published, given `now` (aware, Asia/Seoul).

    Looks at today's and yesterday's eight base times together so a run
    shortly after midnight — before today's own 0200 has landed — correctly
    falls back to yesterday's 2300, which already forecasts today in full."""
    candidates = []
    for day_offset in (0, 1):
        d = (now - timedelta(days=day_offset)).date()
        for t in BASE_TIMES:
            dt = datetime(d.year, d.month, d.day, int(t[:2]), int(t[2:]),
                          tzinfo=now.tzinfo)
            candidates.append((dt, d.strftime('%Y%m%d'), t))
    ready = [c for c in candidates
             if c[0] + timedelta(minutes=BASE_PUBLISH_LAG_MIN) <= now]
    if not ready:
        raise RuntimeError('no published KMA base time found before now')
    _, base_date, base_time = max(ready, key=lambda c: c[0])
    return base_date, base_time


def fetch_forecast(key, base_date, base_time):
    """Raw getVilageFcst items for one run, or None on any failure — the
    caller decides whether that is worth aborting the post over."""
    p = {'serviceKey': key, 'pageNo': '1', 'numOfRows': '1000',
         'dataType': 'JSON', 'base_date': base_date, 'base_time': base_time,
         'nx': str(KMA_NOW_NX), 'ny': str(KMA_NOW_NY)}
    # safe='%' keeps the already-encoded service key from being double-encoded
    # (same reasoning as _kma_air_at in seoul_index_post.py).
    url = ('http://apis.data.go.kr/1360000/VilageFcstInfoService_2.0/'
           'getVilageFcst?' + urllib.parse.urlencode(p, safe='%'))
    try:
        d = http_get_json(url)
    except RuntimeError:
        return None
    try:
        if d['response']['header']['resultCode'] != '00':
            return None
        return d['response']['body']['items']['item']
    except (KeyError, TypeError):
        return None


def fetch_sun_times(key, date_str):
    """(sunrise, sunset) as raw 'HHMM' strings for Seoul on date_str
    (YYYYMMDD), or None on any failure — including "not yet approved for
    this key", which looks identical to any other empty response.

    한국천문연구원_출몰시각 정보 (data.go.kr 15012688) — a separate 활용신청 on
    the same account/key as the forecast call above, so this returns None
    until that approval lands. ⚠️ Unlike every other API this account uses,
    this service has NO JSON option at all (confirmed against its own
    documentation) — it is XML only, so this is the one fetcher here that
    doesn't go through http_get_json."""
    if not key:
        return None
    p = {'serviceKey': key, 'locdate': date_str, 'location': '서울'}
    url = ('http://apis.data.go.kr/B090041/openapi/service/RiseSetInfoService/'
           'getAreaRiseSetInfo?' + urllib.parse.urlencode(p, safe='%'))
    r = subprocess.run(['curl', '-s', '--max-time', '30', url],
                       capture_output=True, text=True)
    if r.returncode != 0 or not r.stdout.strip():
        return None
    try:
        root = ET.fromstring(r.stdout)
    except ET.ParseError:
        return None
    sunrise, sunset = root.findtext('.//sunrise'), root.findtext('.//sunset')
    if not (sunrise and sunset):
        return None
    return sunrise.strip(), sunset.strip()


def yesterday_rain(key, today):
    """Yesterday's ASOS daily total rainfall in mm, or None if the row is
    missing or there was no rain — a genuine dry day (sumRn == 0.0) reads
    the same as an absent row, matching kma_facts()'s own `if rn:` in
    seoul_index_post.py, so both are simply omitted from the card rather
    than shown as a manufactured '0.0mm'."""
    if not key:
        return None
    yday = today - timedelta(days=1)
    rows = _wx_rows(key, f'{yday:%Y%m%d}', f'{yday:%Y%m%d}')
    if not rows:
        return None
    return _wx_num(rows[0], 'sumRn') or None


_PCP_MM_RE = re.compile(r'^(\d+(?:\.\d+)?)mm$')


def forecast_rain_today(items, target_date):
    """Today's forecast rainfall total in mm, summed from the hourly PCP
    (강수량) rows in the SAME getVilageFcst response already fetched for
    today_summary() — no extra call and no separate 활용신청, unlike
    yesterday_rain()'s ASOS observation.

    Returns (mm, exact): mm is the sum of every hour's figure it could
    parse; exact is False if any hour reported a bucketed range instead of
    a plain number. Confirmed live 1 September 2026 (base 0500): ordinary
    hours read '4.0mm', '9.0mm', '1.0mm'; a dry hour reads '강수없음'
    (counted as 0); a trace hour reads '1mm 미만' with no number at all.
    KMA's own published format additionally buckets the heavy-rain end
    ('30.0~50.0mm', '50.0mm 이상'), not observed live but handled the same
    way — anything that isn't a plain 'N.Nmm' or '강수없음' can only push
    the true total up, so the return says "at least this much" rather than
    inventing a number for it. (None, None) if the day has no PCP rows at
    all, so an absent forecast reads the same way as every other
    best-effort figure on this card rather than as a false zero."""
    total, exact, seen = 0.0, True, False
    for i in items:
        if i.get('fcstDate') != target_date or i.get('category') != 'PCP':
            continue
        seen = True
        v = i.get('fcstValue', '')
        if v == '강수없음':
            continue
        m = _PCP_MM_RE.match(v)
        if m:
            total += float(m.group(1))
        else:
            exact = False
    return (total, exact) if seen else (None, None)


def today_summary(items, target_date):
    """Reduce one day's getVilageFcst rows to a card's worth of figures:
    {'hi', 'lo', 'pop', 'sky', 'pty'}, or None if the day has no usable
    hourly temperature data at all (should not happen given how latest_base
    picks its run, but a KMA schema change must never crash the post).

    High/low come from the hourly TMP series rather than the TMN/TMX fields:
    those are only published for "today" in the early-morning base times, and
    are silently absent by mid-morning — deriving from the always-present
    hourly series avoids depending on a field with a fragile availability
    window. Sky is read at the hour of the day's own high (its most
    representative daylight condition); rain chance is the day's own peak
    POP, both published values, not anything this script computes beyond a
    min/max — the same shape as _wx_extremes() already uses in
    seoul_index_post.py."""
    by_hour = collections.defaultdict(dict)
    for i in items:
        if i.get('fcstDate') == target_date:
            by_hour[i['fcstTime']][i['category']] = i['fcstValue']
    temps = {}
    for hr, cats in by_hour.items():
        try:
            temps[hr] = float(cats['TMP'])
        except (KeyError, ValueError):
            continue
    if not temps:
        return None
    hi_hr = max(temps, key=temps.get)
    hi, lo = temps[hi_hr], min(temps.values())

    pops = []
    for cats in by_hour.values():
        v = cats.get('POP')
        if v is not None and v.lstrip('-').isdigit():
            pops.append(int(v))
    pop = max(pops) if pops else None

    # Sky and humidity are read at the SAME hour (the day's high), so
    # together they describe one coherent moment rather than readings
    # stitched from different times of day.
    hi_hour_cats = by_hour.get(hi_hr, {})
    sky = hi_hour_cats.get('SKY')
    humidity = None
    if hi_hour_cats.get('REH', '').lstrip('-').isdigit():
        humidity = int(hi_hour_cats['REH'])

    # Precipitation type: the first hour of the day (chronological) that
    # forecasts anything other than PTY 0. A hot afternoon can still open
    # with an early-morning shower, which the high-temperature hour's own
    # SKY/PTY reading would miss entirely.
    pty = None
    for hr in sorted(by_hour):
        v = by_hour[hr].get('PTY')
        if v and v != '0':
            pty = v
            break

    return {'hi': hi, 'lo': lo, 'pop': pop, 'sky': sky, 'pty': pty,
            'humidity': humidity}


def build_card_lines(summary):
    """(opener, lines, footnote) for each language, from today_summary()'s
    dict. Card text only — dateline and posting are the caller's job."""
    hi, lo, pop, sky, pty = (summary['hi'], summary['lo'], summary['pop'],
                             summary['sky'], summary['pty'])
    humidity = summary['humidity']

    sky_en, sky_ko = SKY_TEXT.get(sky, ('', ''))
    lines_en = [{'emoji': '🔺', 'label': 'High', 'value': fmt_c_en(hi)},
                {'emoji': '🔻', 'label': 'Low', 'value': fmt_c_en(lo)}]
    lines_ko = [{'emoji': '🔺', 'label': '최고기온', 'value': fmt_c(hi)},
                {'emoji': '🔻', 'label': '최저기온', 'value': fmt_c(lo)}]
    pty_en, pty_ko = PTY_TEXT.get(pty, ('', '')) if pty else ('', '')
    # Conditions carries the whole weather judgement in one row: sky, and —
    # when there's anything to say about rain — the day's own chance of it.
    # A day with an active precipitation type names that type as the noun
    # ("...chance of snow") rather than a separate "Precipitation" row, so
    # the sky and the rain figure are never split across two lines.
    cond_emoji = PTY_EMOJI.get(pty, '') if pty_en else SKY_EMOJI.get(sky, '')
    rain_noun_en = pty_en.lower() if pty_en else 'rain'
    if sky_en and pop is not None:
        lines_en.append({'emoji': cond_emoji, 'label': 'Conditions',
                         'value': f'{sky_en} with a {pop}% chance of {rain_noun_en}'})
        lines_ko.append({'emoji': cond_emoji, 'label': '날씨',
                         'value': f'{sky_ko}, 강수확률 {pop}%'})
    elif sky_en and pty_en:
        lines_en.append({'emoji': cond_emoji, 'label': 'Conditions',
                         'value': f'{sky_en} with {rain_noun_en}'})
        lines_ko.append({'emoji': cond_emoji, 'label': '날씨',
                         'value': f'{sky_ko}, {pty_ko}'})
    elif sky_en:
        lines_en.append({'emoji': cond_emoji, 'label': 'Conditions', 'value': sky_en})
        lines_ko.append({'emoji': cond_emoji, 'label': '날씨', 'value': sky_ko})
    elif pop is not None:
        lines_en.append({'emoji': cond_emoji or '☔', 'label': 'Conditions',
                         'value': f'A {pop}% chance of {rain_noun_en}'})
        lines_ko.append({'emoji': cond_emoji or '☔', 'label': '날씨',
                         'value': f'강수확률 {pop}%'})
    elif pty_en:
        lines_en.append({'emoji': cond_emoji, 'label': 'Conditions', 'value': pty_en})
        lines_ko.append({'emoji': cond_emoji, 'label': '날씨', 'value': pty_ko})

    # Rain expected (forecast, today) and yesterday's rainfall (observed)
    # are unrelated readings from two different sources — either can be
    # present without the other — but grouped adjacently rather than
    # merged onto one line: the combined text ("Rain expected 35.0mm or
    # more" beside "Yesterday's rainfall 34.8mm") is too long for one row
    # and wraps badly, unlike the short sunrise/sunset pair above.
    rain_mm, rain_exact = summary.get('rain_forecast_mm'), summary.get('rain_forecast_exact')
    has_forecast = rain_mm is not None and not (rain_mm == 0.0 and rain_exact)
    if has_forecast:
        lines_en.append({'emoji': '☂️', 'label': 'Rain expected',
                         'value': fmt_forecast_rain_en(rain_mm, rain_exact)})
        lines_ko.append({'emoji': '☂️', 'label': '예상 강수량',
                         'value': fmt_forecast_rain_ko(rain_mm, rain_exact)})

    rain_24h = summary.get('rain_24h')
    if rain_24h:
        lines_en.append({'emoji': '🌧️', 'label': "Yesterday's rainfall",
                         'value': to_in(rain_24h)})
        lines_ko.append({'emoji': '🌧️', 'label': '어제 강수량',
                         'value': f'{rain_24h:.1f}mm'})

    if humidity is not None:
        lines_en.append({'emoji': '💧', 'label': 'Humidity', 'value': f'{humidity}%'})
        lines_ko.append({'emoji': '💧', 'label': '습도', 'value': f'{humidity}%'})

    sunrise, sunset = summary.get('sunrise'), summary.get('sunset')
    # fetch_sun_times() only ever returns both or neither (it refuses a
    # response missing either field), so one combined row rather than a
    # sunrise row and a separate sunset row. Sunrise's time bolds via emph
    # (a run inside the otherwise-regular label); Sunset's time bolds
    # because it's the row's actual value — "Sunset" itself sits in
    # value_lead, at regular weight, so only the two clock times bold.
    if sunrise and sunset:
        sr_en, ss_en = fmt_hhmm_ampm(sunrise), fmt_hhmm_ampm(sunset)
        sr_ko, ss_ko = fmt_hhmm_ampm_ko(sunrise), fmt_hhmm_ampm_ko(sunset)
        lines_en.append({'emoji': '☀️', 'label': f'Sunrise {sr_en}', 'emph': sr_en,
                         'value_lead': '🌙 Sunset ', 'value': ss_en,
                         'alt': f'Sunrise {sr_en}, Sunset {ss_en}'})
        lines_ko.append({'emoji': '☀️', 'label': f'일출 {sr_ko}', 'emph': sr_ko,
                         'value_lead': '🌙 일몰 ', 'value': ss_ko,
                         'alt': f'일출 {sr_ko}, 일몰 {ss_ko}'})

    if pty:
        op_emoji = PTY_EMOJI.get(pty, '🌂')
    else:
        op_emoji = SKY_EMOJI.get(sky, '🌤️')
    opener_en = {'emoji': op_emoji, 'text': "Seoul's forecast for today"}
    opener_ko = {'emoji': op_emoji, 'text': '오늘 서울 날씨 예보'}

    return opener_en, lines_en, opener_ko, lines_ko


def footnotes(base_time, has_yesterday_rain=False):
    """The caveat is "not an observed reading" for every figure on the card
    EXCEPT yesterday's rainfall, which is an ASOS observation, not a KMA
    forecast — so a card carrying that row needs the caveat to say so,
    or it flatly misdescribes the one reading that actually is observed."""
    hour, minute = int(base_time[:2]), int(base_time[2:])
    time_en = format_ampm(hour, minute)
    note_en = (f"Korea Meteorological Administration's forecast, issued at "
              f'{time_en} KST — not an observed reading')
    note_ko = f'기상청이 {hour}시에 발표한 예보이며, 실제 관측값이 아닙니다'
    if has_yesterday_rain:
        note_en += ", except yesterday's rainfall, which is"
        note_ko += '. 다만 어제 강수량은 실제 관측값입니다'
    return note_en, note_ko


def build_alt_bodies(opener, lines, footnote):
    """Plain-text rendering of one card's content, for its image ALT text —
    matching seoul_index_post.py's own body/alt convention (curly() applied,
    emoji stripped separately by the caller for the alt itself).

    The default "{label}: {value}" template only reads 'label' and 'value',
    so a row using 'value_lead' (sunrise/sunset packs a second emoji-led
    reading into the value slot) needs its own 'alt' string, or that
    second reading's wording is silently absent from the alt text
    entirely — not just unbolded, but never said."""
    parts = [curly(f"{opener['emoji']} {opener['text']}".strip())]
    for l in lines:
        if 'alt' in l:
            parts.append(curly(l['alt']))
        else:
            parts.append(curly(f"{l['emoji']} {l['label']}: {l['value']}".strip()))
    if footnote:
        parts.append(curly(footnote))
    return '\n'.join(parts)


def already_posted(target_date):
    """True if WEATHER_LOG already holds a successful post for target_date.

    This is what makes the 06:30 safety-net launchd fire (see the plist)
    safe to add alongside the regular 05:25 one: if 05:25 already posted,
    06:30 sees today's target_date logged and exits quietly instead of
    posting a second thread for the same day."""
    if not WEATHER_LOG.exists():
        return False
    with open(WEATHER_LOG) as f:
        for line in f:
            try:
                if json.loads(line).get('target_date') == target_date:
                    return True
            except json.JSONDecodeError:
                continue
    return False


def main():
    lock = open(HERE / '.weather.lock', 'w')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit('Another seoul_weather_post run is in progress; bowing out.')

    now = datetime.now(SEOUL_TZ)
    print(f'--- run at {now:%Y-%m-%d %H:%M:%S} KST ---')

    # Checked before net_guard/config/anything else, so a safety-net rerun
    # after a successful earlier post costs nothing but this.
    target_date = now.strftime('%Y%m%d')
    if not DRY_RUN and already_posted(target_date):
        print(f'Already posted for {target_date}; nothing to do '
              f'(safety-net rerun after an earlier success).')
        return

    # A daily card has no tight window to hit; give the machine up to an hour
    # to find a path out before giving up, matching seoul_index_post.py's
    # own 1800s budget for the same reason.
    net_guard.require_network(1800)

    config = json.loads(CONFIG.read_text())
    gov_key = config.get('data_go_kr_key')
    if not gov_key:
        sys.exit('No data_go_kr_key in seoul_index_config.json.')

    base_date, base_time = latest_base(now)
    items = fetch_forecast(gov_key, base_date, base_time)
    if items is None:
        sys.exit(f'KMA getVilageFcst call failed (base {base_date} {base_time}).')

    summary = today_summary(items, target_date)
    if summary is None:
        sys.exit(f'No hourly temperature data for {target_date} in the '
                 f'{base_date} {base_time} run — KMA response may have '
                 f'changed shape.')

    # All three best-effort: sun times need their own 활용신청 on this key
    # (not yet approved as of 30 August 2026, so this returns None until
    # then), yesterday's rain row is simply absent on a dry day, and the
    # forecast total is None only if KMA's response carries no PCP rows at
    # all. None of the three is worth aborting the post over.
    sun = fetch_sun_times(gov_key, target_date)
    summary['sunrise'], summary['sunset'] = sun if sun else (None, None)
    summary['rain_24h'] = yesterday_rain(gov_key, now.date())
    summary['rain_forecast_mm'], summary['rain_forecast_exact'] = \
        forecast_rain_today(items, target_date)

    print(f'Forecast run {base_date} {base_time} → {target_date}: '
         f'hi {summary["hi"]:.1f}°C, lo {summary["lo"]:.1f}°C, '
         f'pop {summary["pop"]}, sky {summary["sky"]}, pty {summary["pty"]}, '
         f'sunrise {summary["sunrise"]}, sunset {summary["sunset"]}, '
         f'rain_24h {summary["rain_24h"]}, '
         f'rain_forecast {summary["rain_forecast_mm"]} '
         f'(exact={summary["rain_forecast_exact"]})')

    opener_en, lines_en, opener_ko, lines_ko = build_card_lines(summary)
    note_en, note_ko = footnotes(base_time, has_yesterday_rain=bool(summary.get('rain_24h')))
    dateline_en = f'{now:%-d %B %Y}'
    dateline_ko = f'{now.year}년 {now.month}월 {now.day}일'

    en_alt = strip_emoji(build_alt_bodies(opener_en, lines_en, note_en))
    ko_alt = strip_emoji(build_alt_bodies(opener_ko, lines_ko, note_ko))
    print(f'\nEN alt ({len(en_alt)} chars):\n{"-"*46}\n{en_alt}\n{"-"*46}')
    print(f'\nKO alt ({len(ko_alt)} chars):\n{"-"*46}\n{ko_alt}\n{"-"*46}')

    out_dir = Path.cwd() if DRY_RUN else Path(tempfile.mkdtemp())
    try:
        en_path, en_size = render_card(opener_en, lines_en,
                                       out_dir / 'weather_card_en.png',
                                       footnote=note_en, dateline=dateline_en)
        ko_path, ko_size = render_card(opener_ko, lines_ko,
                                       out_dir / 'weather_card_ko.png',
                                       korean=True, footnote=note_ko,
                                       dateline=dateline_ko)
    except CardRenderError as e:
        sys.exit(f'Card render failed ({e}); no plaintext fallback for this '
                 f'bot — re-run once Chrome/Pillow are healthy.')
    en_bytes, ko_bytes = Path(en_path).read_bytes(), Path(ko_path).read_bytes()
    print(f'\nRendered cards — EN {en_size}, KO {ko_size}.')

    if DRY_RUN:
        print(f'\n(dry run — wrote {en_path} and {ko_path}, not posting)')
        return
    shutil.rmtree(out_dir, ignore_errors=True)

    handle = config['handle']
    password = keychain_password(handle, KEYCHAIN_SERVICE)
    bsky = Client()
    bsky.login(handle, password)

    def _reply(parent_ref, root_ref):
        return models.AppBskyFeedPost.ReplyRef(parent=parent_ref, root=root_ref)

    en_ar = models.AppBskyEmbedDefs.AspectRatio(width=en_size[0], height=en_size[1])
    ko_ar = models.AppBskyEmbedDefs.AspectRatio(width=ko_size[0], height=ko_size[1])
    en_src = source_reply(client_utils.TextBuilder(), 'Source: data.kma.go.kr')
    ko_src = source_reply(client_utils.TextBuilder(), '출처: data.kma.go.kr')

    p1 = bsky.send_image(text=tag_line(), image=en_bytes, image_alt=en_alt,
                         langs=['en'], image_aspect_ratio=en_ar)
    root_ref = models.create_strong_ref(p1)
    p2 = bsky.send_post(text=en_src, reply_to=_reply(root_ref, root_ref), langs=['en'])
    p2_ref = models.create_strong_ref(p2)
    p3 = bsky.send_image(text=tag_line(), image=ko_bytes, image_alt=ko_alt,
                         reply_to=_reply(p2_ref, root_ref), langs=['ko'],
                         image_aspect_ratio=ko_ar)
    p3_ref = models.create_strong_ref(p3)
    bsky.send_post(text=ko_src, reply_to=_reply(p3_ref, root_ref), langs=['ko'])
    print('\nPosted (4-post thread: EN card, EN source, KO card, KO source).')

    try:
        with open(WEATHER_LOG, 'a') as f:
            f.write(json.dumps({
                'posted_at': now.isoformat(), 'base_date': base_date,
                'base_time': base_time, 'target_date': target_date,
                'hi': summary['hi'], 'lo': summary['lo'], 'pop': summary['pop'],
                'sky': summary['sky'], 'pty': summary['pty'],
                'uri': p1.uri,
            }, ensure_ascii=False) + '\n')
    except OSError:
        pass  # best-effort — the post is already out


if __name__ == '__main__':
    main()
