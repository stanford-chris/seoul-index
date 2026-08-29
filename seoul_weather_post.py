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
import shutil
import sys
import tempfile
import urllib.parse
from datetime import datetime, timedelta
from pathlib import Path

from atproto import Client, client_utils, models

import net_guard
from seoul_index_card import render_card, CardRenderError, curly
from seoul_index_post import (
    KMA_NOW_NX, KMA_NOW_NY, SEOUL_TZ, KEYCHAIN_SERVICE, TAGS,
    http_get_json, keychain_password, write_json_atomic, strip_emoji,
    source_reply, tag_line, LINK_DOMAINS,
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
    if sky_en:
        lines_en.append({'emoji': SKY_EMOJI.get(sky, ''), 'label': 'Sky',
                         'value': sky_en})
        lines_ko.append({'emoji': SKY_EMOJI.get(sky, ''), 'label': '하늘상태',
                         'value': sky_ko})
    # One row for rain, not two: a "Chance of rain" line beside a
    # "Precipitation" line said the same thing twice whenever both were
    # present. Dry days keep the plain percentage; a day with a precipitation
    # type folds it and the percentage into one row instead. Sits right after
    # Sky — the two are one weather judgement, not separated by humidity.
    pty_en, pty_ko = PTY_TEXT.get(pty, ('', '')) if pty else ('', '')
    if pty_en and pop is not None:
        lines_en.append({'emoji': PTY_EMOJI.get(pty, ''), 'label': 'Precipitation',
                         'value': f'{pty_en} ({pop}%)'})
        lines_ko.append({'emoji': PTY_EMOJI.get(pty, ''), 'label': '강수형태',
                         'value': f'{pty_ko} ({pop}%)'})
    elif pty_en:
        lines_en.append({'emoji': PTY_EMOJI.get(pty, ''), 'label': 'Precipitation',
                         'value': pty_en})
        lines_ko.append({'emoji': PTY_EMOJI.get(pty, ''), 'label': '강수형태',
                         'value': pty_ko})
    elif pop is not None:
        lines_en.append({'emoji': '☔', 'label': 'Chance of rain',
                         'value': f'{pop}%'})
        lines_ko.append({'emoji': '☔', 'label': '강수확률',
                         'value': f'{pop}%'})
    if humidity is not None:
        lines_en.append({'emoji': '💧', 'label': 'Humidity', 'value': f'{humidity}%'})
        lines_ko.append({'emoji': '💧', 'label': '습도', 'value': f'{humidity}%'})

    if pty:
        op_emoji = PTY_EMOJI.get(pty, '🌂')
    else:
        op_emoji = SKY_EMOJI.get(sky, '🌤️')
    opener_en = {'emoji': op_emoji, 'text': "Seoul's forecast for today"}
    opener_ko = {'emoji': op_emoji, 'text': '오늘 서울 날씨 예보'}

    return opener_en, lines_en, opener_ko, lines_ko


def footnotes(base_time):
    hhmm = f'{base_time[:2]}:{base_time[2:]}'
    hour_ko = int(base_time[:2])
    note_en = (f"Korea Meteorological Administration's forecast, issued "
              f'{hhmm} KST — not an observed reading')
    note_ko = f'기상청이 {hour_ko}시에 발표한 예보이며, 실제 관측값이 아닙니다'
    return note_en, note_ko


def build_alt_bodies(opener, lines, footnote):
    """Plain-text rendering of one card's content, for its image ALT text —
    matching seoul_index_post.py's own body/alt convention (curly() applied,
    emoji stripped separately by the caller for the alt itself)."""
    parts = [curly(f"{opener['emoji']} {opener['text']}".strip())]
    for l in lines:
        parts.append(curly(f"{l['emoji']} {l['label']}: {l['value']}".strip()))
    if footnote:
        parts.append(curly(footnote))
    return '\n'.join(parts)


def main():
    lock = open(HERE / '.weather.lock', 'w')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit('Another seoul_weather_post run is in progress; bowing out.')

    now = datetime.now(SEOUL_TZ)
    print(f'--- run at {now:%Y-%m-%d %H:%M:%S} KST ---')

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

    target_date = now.strftime('%Y%m%d')
    summary = today_summary(items, target_date)
    if summary is None:
        sys.exit(f'No hourly temperature data for {target_date} in the '
                 f'{base_date} {base_time} run — KMA response may have '
                 f'changed shape.')

    print(f'Forecast run {base_date} {base_time} → {target_date}: '
         f'hi {summary["hi"]:.1f}°C, lo {summary["lo"]:.1f}°C, '
         f'pop {summary["pop"]}, sky {summary["sky"]}, pty {summary["pty"]}')

    opener_en, lines_en, opener_ko, lines_ko = build_card_lines(summary)
    note_en, note_ko = footnotes(base_time)
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
