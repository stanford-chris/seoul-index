#!/usr/bin/env python3
"""
Seoul Index (@seoul-index.bsky.social) — "Seoul by the numbers".

A Harper's-Index-style bot: each post is a short set of statistics drawn from
Seoul Open Data, arranged so two numbers sit next to each other for a double-take
(a near-equal "dead heat", or a wide gap). Posts as a thread: an English index,
then a Korean translation as a threaded reply.

Design principle — accuracy over wit:
  Python owns every NUMBER. It harvests the data, formats each value, and detects
  the sharp juxtapositions. The `claude -p` step only CURATES (which lines, what
  order, an opener), lightly rewords English labels for wit, and TRANSLATES labels
  to Korean. Claude never emits a numeric value; the poster reuses Python's exact
  value string in both languages, and rejects any Claude label that contains a
  digit. So a hallucinated figure cannot reach a post.

Freshness:
  Live facts (crowds, air) are pulled at post time. Daily facts (subway/bus) are
  computed at post time but cached per-day in state so the second daily post is
  cheap. Quarterly sales come from sales_agg.json (refreshed monthly by
  seoul_index_sales.py).

Requires (for actual posting, not --dry-run):
  - seoul_index_config.json with {"api_key": "...", "handle": "seoul-index.bsky.social"}
  - the bot's Bluesky app password in the Keychain:
      security add-generic-password -a "seoul-index.bsky.social" -s "seoulindex-bluesky" -w
  - a long-lived claude setup-token in the Keychain (shared, account 'seoulbot')

Usage:
  python3 seoul_index_post.py            # post one index (English -> Korean thread)
  python3 seoul_index_post.py --dry-run  # harvest, select, compose, print — no post
  python3 seoul_index_post.py --spotlight --dry-run   # force the single-place card
"""

import collections
import csv
import fcntl
import io
import json
import os
import random
import re
import subprocess
import sys
import tempfile
import time
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

from atproto import Client, client_utils, models

import limit_guard
import net_guard
from seoul_index_card import render_card, CardRenderError, curly

HERE = Path(__file__).parent
CONFIG = HERE / 'seoul_index_config.json'
STATE = HERE / 'seoul_index_state.json'
SALES_AGG = HERE / 'sales_agg.json'
NAMES_EN = HERE / 'seoul_index_names_en.json'
# One JSONL line per posted card, so a week's output can be skimmed to catch
# duds. Written only on a real post (never a --dry-run), and best-effort: the
# post is already out by the time it is written, so a logging hiccup must not
# surface as a failure. See log_card().
CARD_LOG = HERE / 'card_history.jsonl'
# One JSONL line per label checked, the rejected ones included. Those are the
# records worth keeping: a rejected label never reaches a card, so this file is
# the only place a false alarm is ever visible. See check_labels.
LABEL_LOG = HERE / 'label_checks.jsonl'
# Whether compose() ends by checking its labels. Only ever False in tests: the
# composition suites promise no network and no model call, and they disable the
# CALL rather than replacing check_labels itself, which would also silence the
# checker's own tests — unittest discover imports every test module into one
# process, so a stubbed function stays stubbed for the whole run.
CHECK_LABELS = True
# The estate's shared notebook, read by a weekly review. Optional: this
# repository is public and the bot runs without it.
OBSERVE = Path.home() / 'Scripts' / 'observe.py'
KEYCHAIN_SERVICE = 'seoulindex-bluesky'
CLAUDE_TOKEN_ACCOUNT = 'seoulbot'
CLAUDE_TOKEN_SERVICE = 'claude-oauth-token'
CLAUDE_MODEL = 'claude-sonnet-5'  # wit + Korean; easy to change if unavailable
# Hard cap on one `claude -p` selector call. Healthy calls finish well inside
# this; without a cap the call hung indefinitely twice on 21 Jul 2026, and a
# hung selector is a post that silently never happens. A timeout retries once
# (like invalid JSON), then raises so the failure lands in the launchd log.
CLAUDE_TIMEOUT = 300

# Refuse anything unrecognised: with membership tests instead of argparse, an
# unknown flag would silently run LIVE (`--help` published a real thread on
# 20 Jul 2026). Fail before doing anything at all — but only when THIS file is
# the program being run: importers (seoul_index_methodology.py) have their own
# flags, and validating their argv here rejected `--pin` on 23 Jul 2026.
_KNOWN_ARGS = {'--dry-run', '--spotlight', '--show-cross', '--tail'}
_ONLY_PREFIX = '--only='       # --only=<cat>: build the card from one vein


def _tail_n(argv):
    """N for `--tail [N]` (read the card log and exit), or None if absent.
    N defaults to 10 and a bare integer right after --tail overrides it."""
    if '--tail' not in argv:
        return None
    i = argv.index('--tail')
    if i + 1 < len(argv) and argv[i + 1].isdigit():
        return max(1, int(argv[i + 1]))
    return 10


if __name__ == '__main__':
    # --tail may be followed by a bare integer (its count); skip that token so
    # it is not mistaken for an unknown argument.
    _skip = None
    if '--tail' in sys.argv:
        _t = sys.argv.index('--tail')
        if _t + 1 < len(sys.argv) and sys.argv[_t + 1].isdigit():
            _skip = _t + 1
    _unknown = [a for j, a in enumerate(sys.argv[1:], 1)
                if a not in _KNOWN_ARGS and j != _skip
                and not a.startswith(_ONLY_PREFIX)]
    if _unknown:
        sys.exit(f'Unknown argument(s): {" ".join(_unknown)}. '
                 f'Recognised: {" ".join(sorted(_KNOWN_ARGS))} [N], '
                 f'{_ONLY_PREFIX}<cat>. '
                 f'Refusing to run (a bare run posts live).')

DRY_RUN = '--dry-run' in sys.argv
FORCE_SPOTLIGHT = '--spotlight' in sys.argv   # for testing the single-place card
SHOW_CROSS = '--show-cross' in sys.argv       # print cross-vein collisions, then exit
TAIL_N = _tail_n(sys.argv)                    # read the card log, then exit
# --only=<cat> builds the card from ONE vein, the way the vein floor does when
# it promotes a starved one. It exists because there was no way to show a
# particular vein on demand: the floor picks whichever vein has waited longest,
# so asking to see a new one meant either waiting for its turn or posting the
# wrong card to find out. Takes the promoted path (strict=False), because the
# overlap rule would reject every card a single small vein can build.
ONLY_CAT = next((a[len(_ONLY_PREFIX):] for a in sys.argv
                 if a.startswith(_ONLY_PREFIX)), None) or None
MAX_POST_CHARS = 285  # buffer under Bluesky's 300-grapheme limit
SEOUL_TZ = ZoneInfo('Asia/Seoul')
SOURCE_URL = 'https://data.seoul.go.kr/'

# How many recently-used line ids / categories to keep off the next post.
RECENT_IDS_KEEP = 24
RECENT_CATS_KEEP = 2

# recent_ids is advisory: it goes to the selector as AVOID_IDS, the last and
# weakest rule in a long prompt, and the selector demonstrably ignores it when a
# frozen PAIR is more attractive. So the same card can come back the moment its
# ids age out. It did: the spending card below went out line-for-line identical
# on 29 Jul and 6, 10 and 17 Aug 2026 —
#     Karaoke rooms ₩87.8bn / Bookshops ₩77.8bn / Fried chicken ₩77.7bn / Motels ₩61.7bn
# and its top THREE lines were the same on all six spending cards in that
# fortnight, only the fourth line moving. An exact-match guard would have caught
# the four but not the six, so the rule is an OVERLAP one: a new card may share
# at most CARD_OVERLAP_MAX line ids with any of the last RECENT_CARDS_KEEP
# cards. Enforced in Python by reselecting (see select_fresh), because a rule the
# selector is merely asked to follow is the thing that failed.
#
# 12 cards is about four days at three posts a day. The small veins (bike and
# transport have 4 facts each) cannot clear an overlap of 2 against their own
# last card, so in effect they post at most once per window — which is the
# intended outcome: their four lines are fixed and only the values move.
RECENT_CARDS_KEEP = 12
CARD_OVERLAP_MAX = 2
# Each retry is another claude -p call, so the guard gives up rather than
# spending forever; a card that survives three rejections posts anyway.
SELECT_RETRIES = 3

# The floor under the neglected veins. Measured 17 Aug 2026 over the 50 logged
# cards: seven of the sixteen harvested veins had NEVER posted (world 29 facts,
# weather 19, health 12, infra 7, national 5, culture 4, air 2 — 78 of 202
# facts, 39% of everything harvested), while six veins took the whole feed. The
# cause is the prompt itself: it tells the selector to STRONGLY prefer a
# pre-detected PAIR and calls CROSS_PAIRS "the account's sharpest move", and
# pairs cluster in the live and quarterly veins. The annual-vintage veins lose
# every time they are offered.
#
# So the same remedy as the cooldowns, pointed the other way: when a vein has
# not been the primary category for this many days, the pool is narrowed TO that
# vein for one card and the selector has nothing else to choose. Promotions never
# land back to back, so a burst of starved veins airs over alternating posts
# rather than putting the feed on rails for two days.
STARVE_DAYS = 5
# A promoted vein must be able to fill a card on its own. 'air' has only 2 facts
# and so can never be promoted — it can only ever ride along on someone else's
# card, which is worth knowing rather than silently working around.
STARVE_MIN_FACTS = 3

# Categories whose lines keep the HARVESTER'S order instead of being sorted by
# value, like a spotlight card. Sorting a river-level card by size buried "the
# river now" at the bottom under three thresholds it is nowhere near, which
# inverts what the card is about: the reading leads, then the tiers ascend away
# from it. Add a category here only when its lines are a SEQUENCE rather than a
# ranking.
# 'complaint' and 'infant' label their lines with BARE YEARS, which are a
# sequence and not a ranking: value-sorted, a complaints card read 2023, 2024,
# 2022, 2025, which a reader takes for a bug before they take it for an
# ordering. The infant card escaped that only because its fall happens to be
# monotonic — luck, not design, and it scrambles the moment a band ticks up.
# 'boxhist' is three years of one date, newest first: a sequence, like the
# complaint and infant year lines, and value-sorting it would scramble the
# years the moment a middle one came out highest.
ORDERED_CATS = {'level', 'complaint', 'infant', 'boxhist'}

# Veins whose lines are all the same KIND of thing, where a partial set of line
# emoji reads as an oversight rather than a judgement. See even_out_emoji.
EMOJI_ALL_OR_NONE = {'boxoffice', 'boxhist'}

# Curated live-crowd locations (citydata_ppltn AREA_NM, all verified to resolve).
# A mix of packed / quiet / touristy / young so contrasts are available.
# 'area' is the API's own AREA_NM, which often carries an administrative suffix
# (관광특구, "special tourist zone") that nobody says out loud, so 'en' and 'ko'
# are what a card calls the place. 'wiki_en'/'wiki_ko' are the article a
# spotlight card links to on its source line — every one of them checked against
# the Wikipedia API rather than assembled from a title that looks plausible,
# because a URL that 404s is a wrong fact like any other. Korean articles are
# not always the mirror of the English one (there is no ko article for the
# Gangseo riverbank, for instance), so the two are resolved independently.
CROWD_SPOTS = [
    {'area': '잠실 관광특구', 'en': 'Jamsil', 'ko': '잠실',
     'wiki_en': 'https://en.wikipedia.org/wiki/Jamsil-dong',
     'wiki_ko': 'https://ko.wikipedia.org/wiki/%EC%9E%A0%EC%8B%A4%EB%8F%99'},
    {'area': '홍대 관광특구', 'en': 'Hongdae', 'ko': '홍대',
     'wiki_en': 'https://en.wikipedia.org/wiki/Hongdae_(area)',
     'wiki_ko': 'https://ko.wikipedia.org/wiki/%ED%99%8D%EB%8C%80_(%EC%A7%80%EC%97%AD)'},
    {'area': '강남역', 'en': 'Gangnam Station', 'ko': '강남역',
     'wiki_en': 'https://en.wikipedia.org/wiki/Gangnam_station',
     'wiki_ko': 'https://ko.wikipedia.org/wiki/%EA%B0%95%EB%82%A8%EC%97%AD'},
    {'area': '광화문·덕수궁', 'en': 'Gwanghwamun', 'ko': '광화문',
     'wiki_en': 'https://en.wikipedia.org/wiki/Gwanghwamun',
     'wiki_ko': 'https://ko.wikipedia.org/wiki/%EA%B4%91%ED%99%94%EB%AC%B8'},
    {'area': '여의도한강공원', 'en': 'the Yeouido riverbank', 'ko': '여의도 한강공원',
     'wiki_en': 'https://en.wikipedia.org/wiki/Yeouido',
     'wiki_ko': 'https://ko.wikipedia.org/wiki/%EC%97%AC%EC%9D%98%EB%8F%84'},
    {'area': '명동 관광특구', 'en': 'Myeongdong', 'ko': '명동',
     'wiki_en': 'https://en.wikipedia.org/wiki/Myeong-dong',
     'wiki_ko': 'https://ko.wikipedia.org/wiki/%EB%AA%85%EB%8F%99'},
    {'area': '이태원 관광특구', 'en': 'Itaewon', 'ko': '이태원',
     'wiki_en': 'https://en.wikipedia.org/wiki/Itaewon-dong',
     'wiki_ko': 'https://ko.wikipedia.org/wiki/%EC%9D%B4%ED%83%9C%EC%9B%90%EB%8F%99'},
    {'area': '경복궁', 'en': 'Gyeongbokgung', 'ko': '경복궁',
     'wiki_en': 'https://en.wikipedia.org/wiki/Gyeongbokgung',
     'wiki_ko': 'https://ko.wikipedia.org/wiki/%EA%B2%BD%EB%B3%B5%EA%B6%81'},
    {'area': '북촌한옥마을', 'en': 'Bukchon Hanok Village', 'ko': '북촌한옥마을',
     'wiki_en': 'https://en.wikipedia.org/wiki/Bukchon_Hanok_Village',
     'wiki_ko': 'https://ko.wikipedia.org/wiki/%EB%B6%81%EC%B4%8C_%ED%95%9C%EC%98%A5%EB%A7%88%EC%9D%84'},
    {'area': '인사동', 'en': 'Insadong', 'ko': '인사동',
     'wiki_en': 'https://en.wikipedia.org/wiki/Insa-dong',
     'wiki_ko': 'https://ko.wikipedia.org/wiki/%EC%9D%B8%EC%82%AC%EB%8F%99'},
    {'area': '광장(전통)시장', 'en': 'Gwangjang Market', 'ko': '광장시장',
     'wiki_en': 'https://en.wikipedia.org/wiki/Gwangjang_Market',
     'wiki_ko': 'https://ko.wikipedia.org/wiki/%EA%B4%91%EC%9E%A5%EC%8B%9C%EC%9E%A5'},
    {'area': '남대문시장', 'en': 'Namdaemun Market', 'ko': '남대문시장',
     'wiki_en': 'https://en.wikipedia.org/wiki/Namdaemun_Market',
     'wiki_ko': 'https://ko.wikipedia.org/wiki/%EB%82%A8%EB%8C%80%EB%AC%B8%EC%8B%9C%EC%9E%A5'},
    {'area': '서울역', 'en': 'Seoul Station', 'ko': '서울역',
     'wiki_en': 'https://en.wikipedia.org/wiki/Seoul_Station',
     'wiki_ko': 'https://ko.wikipedia.org/wiki/%EC%84%9C%EC%9A%B8%EC%97%AD'},
    {'area': '고속터미널역', 'en': 'the Express Bus Terminal', 'ko': '고속터미널역',
     'wiki_en': 'https://en.wikipedia.org/wiki/Express_Bus_Terminal_station',
     'wiki_ko': 'https://ko.wikipedia.org/wiki/%EA%B3%A0%EC%86%8D%ED%84%B0%EB%AF%B8%EB%84%90%EC%97%AD'},
    {'area': '김포공항', 'en': 'Gimpo Airport', 'ko': '김포공항',
     'wiki_en': 'https://en.wikipedia.org/wiki/Gimpo_International_Airport',
     'wiki_ko': 'https://ko.wikipedia.org/wiki/%EA%B9%80%ED%8F%AC%EA%B5%AD%EC%A0%9C%EA%B3%B5%ED%95%AD'},
    {'area': '가산디지털단지역', 'en': 'Gasan Digital Complex', 'ko': '가산디지털단지역',
     'wiki_en': 'https://en.wikipedia.org/wiki/Gasan_Digital_Complex_station',
     'wiki_ko': 'https://ko.wikipedia.org/wiki/%EA%B0%80%EC%82%B0%EB%94%94%EC%A7%80%ED%84%B8%EB%8B%A8%EC%A7%80%EC%97%AD'},
    {'area': '신림역', 'en': 'Sillim Station', 'ko': '신림역',
     'wiki_en': 'https://en.wikipedia.org/wiki/Sillim_station',
     'wiki_ko': 'https://ko.wikipedia.org/wiki/%EC%8B%A0%EB%A6%BC%EC%97%AD'},
    {'area': '사당역', 'en': 'Sadang Station', 'ko': '사당역',
     'wiki_en': 'https://en.wikipedia.org/wiki/Sadang_station',
     'wiki_ko': 'https://ko.wikipedia.org/wiki/%EC%82%AC%EB%8B%B9%EC%97%AD'},
    {'area': '성수카페거리', 'en': 'the Seongsu cafe strip', 'ko': '성수카페거리',
     'wiki_en': 'https://en.wikipedia.org/wiki/Seongsu-dong',
     'wiki_ko': 'https://ko.wikipedia.org/wiki/%EC%84%B1%EC%88%98%EB%8F%99'},
    {'area': '연남동', 'en': 'Yeonnam-dong', 'ko': '연남동',
     'wiki_en': 'https://en.wikipedia.org/wiki/Yeonnam-dong',
     'wiki_ko': 'https://ko.wikipedia.org/wiki/%EC%97%B0%EB%82%A8%EB%8F%99'},
    {'area': '해방촌·경리단길', 'en': 'Haebangchon', 'ko': '해방촌',
     'wiki_en': 'https://en.wikipedia.org/wiki/Haebangchon',
     'wiki_ko': 'https://ko.wikipedia.org/wiki/%ED%95%B4%EB%B0%A9%EC%B4%8C'},
    {'area': '남산공원', 'en': 'Namsan Park', 'ko': '남산공원',
     'wiki_en': 'https://en.wikipedia.org/wiki/Namsan',
     'wiki_ko': 'https://ko.wikipedia.org/wiki/%EB%82%A8%EC%82%B0_(%EC%84%9C%EC%9A%B8)'},
    {'area': '서울숲공원', 'en': 'Seoul Forest', 'ko': '서울숲',
     'wiki_en': 'https://en.wikipedia.org/wiki/Seoul_Forest',
     'wiki_ko': 'https://ko.wikipedia.org/wiki/%EC%84%9C%EC%9A%B8%EC%88%B2'},
    {'area': '노들섬', 'en': 'Nodeul Island', 'ko': '노들섬',
     'wiki_en': 'https://en.wikipedia.org/wiki/Nodeulseom',
     'wiki_ko': 'https://ko.wikipedia.org/wiki/%EB%85%B8%EB%93%A4%EC%84%AC'},
    {'area': '강서한강공원', 'en': 'the Gangseo riverbank', 'ko': '강서한강공원',
     'wiki_en': 'https://en.wikipedia.org/wiki/Gangseo_District,_Seoul',
     'wiki_ko': 'https://ko.wikipedia.org/wiki/%EA%B0%95%EC%84%9C%EA%B5%AC_(%EC%84%9C%EC%9A%B8%ED%8A%B9%EB%B3%84%EC%8B%9C)'},
    {'area': '잠실롯데타워·석촌호수', 'en': 'Lotte World Tower', 'ko': '롯데월드타워',
     'wiki_en': 'https://en.wikipedia.org/wiki/Lotte_World_Tower',
     'wiki_ko': 'https://ko.wikipedia.org/wiki/%EB%A1%AF%EB%8D%B0%EC%9B%94%EB%93%9C%ED%83%80%EC%9B%8C'},
]

# One post in every SPOTLIGHT_EVERY, on average, drills into a single place
# instead of setting places against each other. Chosen by coin flip rather
# than a fixed cadence (see main()), so the spotlight does not land in the
# same slot every day.
#
# Was 3 until 17 Aug 2026, which measured out at 17 of the 50 logged cards —
# a third of the feed spent on one card type that never reaches the selector,
# and therefore a third of the feed that no other vein can ever occupy. Seven
# veins (world, weather, health, infra, national, culture, air: 78 of 202
# facts) had gone unposted for the whole three weeks the card log covers. At 5
# the spotlight is about a fifth of the feed and those slots go back into the
# pool.
SPOTLIGHT_EVERY = 5

# A spotlight card is four readings of ONE place, so unlike an index card it has
# no contrast to fall back on: if the readings are all the same number, the card
# says nothing four times. The KT population buckets are coarse (they arrive
# pre-rounded, and the quieter spots round to the same value hour after hour),
# so this is not rare — Yeonnam-dong on 7 Aug 2026 posted 11,000 / 11,000 /
# 11,000 / 6,250 and Haebangchon on 10 Aug posted 3,250 / 3,250 / 3,750 /
# 3,250. Both cleared the old "a flat forecast says nothing" check, which
# compares the peak and trough HOURS and never looks at the values.
#
# A card must therefore carry at least SPOTLIGHT_MIN_DISTINCT different values,
# spread at least SPOTLIGHT_MIN_SPREAD of the largest. Replayed against the 17
# logged spotlight cards, the pair rejects exactly the two dead cards above and
# keeps the other 15. The distinct-values floor is what does the work: eight of
# the 15 keepers sit exactly on it (three values from four lines is the normal
# shape, since 'now' and either the weekday average or the peak often round
# together), while the closest keeper on spread is Bukchon Hanok Village at 46%,
# comfortably clear of 25%. Both dead cards above fail on distinct values alone,
# so on this sample the spread bar rejects nothing the distinct floor does not.
# It is kept as the backstop for the shape the distinct floor cannot see: three
# or four different values that are all nearly the same size (11,000 / 11,050 /
# 11,100), which is one number with a wobble and reads as flat on the card.
#
# A rejected card falls through to a normal index card, which is the existing
# behaviour when a place answers with too few lines.
SPOTLIGHT_MIN_DISTINCT = 3
SPOTLIGHT_MIN_SPREAD = 0.25

# The world vein is a quarter of the pool and holds the widest gaps in it
# (Seoul's density is 4x Amsterdam's), so the selector reaches for it whenever
# it is offered. It therefore gets a cooldown the other categories do not need:
# after a world post, world facts leave the pool entirely until this many days
# have passed. At three posts a day, 3 days is about one world card in nine.
WORLD_COOLDOWN_DAYS = 3

# Spending gets a cooldown for the opposite reason to world's. Its figures come
# from a QUARTERLY aggregate (sales_agg.json), so the dead-heat pair that
# sales_facts() pre-detects is not merely attractive, it is FROZEN: the same two
# categories are handed to the selector under PAIRS on every run for months at a
# time, and the prompt tells the selector to strongly prefer building around a
# pair. The result was the identical card — bookshops against fried chicken —
# recurring as fast as recent_ids would release it (4 posts in the fortnight to
# 17 Aug 2026, on 4, 6, 10 and 17 August, three of them line-for-line the same).
# recent_ids alone cannot fix this: at RECENT_IDS_KEEP=24 it only spaces repeats
# about six posts apart. At three posts a day, 3 days is about one spending card
# in nine. 'avgbill' is deliberately NOT cooled — it is the same source data
# under a different framing ("Average bill in Seoul"), and it is not what
# repeats.
SPENDING_COOLDOWN_DAYS = 3

# Rotating openers offered to the selector (it may also write its own). Kept
# deliberately neutral — time/place framings, never a punchline. The house style
# is Harper's: the arrangement carries the joke, the opener never gives it away.
OPENERS = [
    ('Seoul by the numbers', '숫자로 보는 서울'),
    ('Seoul, right now', '지금 서울은'),
    ('Seoul today', '오늘의 서울'),
    ('The city, as it stands', '지금 이 도시는'),
    ('Last quarter in Seoul', '지난 분기의 서울'),
    ('Spent last quarter in Seoul', '지난 분기 서울의 지출'),
    # "Average bill", not "per visit": the figure is sales / number of
    # TRANSACTIONS, so one Korean-restaurant line is a shared table, not one
    # diner. "Per visit" invited the reader to compare it with a coffee, which
    # really is one person paying for themselves.
    ('Average bill in Seoul', '서울의 평균 결제액'),
    ("20-somethings in Seoul's crowds, right now", '지금 서울 인파의 20대'),
    ('Seoul on the move', '움직이는 서울'),
    ('From the city’s data', '서울시 데이터에서'),
    ('Seoul and the nation', '서울과 전국'),
    ('Seoul among world cities', '세계 도시 속의 서울'),
    ('The apartment market, one month', '한 달의 아파트 시장'),
    ('50 years apart', '50년의 간격'),
    ('Seoul, yesterday', '어제의 서울'),
    ('Through Gimpo airport', '김포공항에서'),
    ("A year in Seoul's clinics", '서울 진료실의 1년'),
    ("A year at Seoul's museums", '서울 박물관의 1년'),
    ('Through the turnstiles', '개찰구를 지나서'),
    ('Green space per person', '1인당 녹지 면적'),
    ('Within a five-minute walk of transit', '도보 5분 내 대중교통'),
    ('Summer nights, hotter than the countryside', '여름밤, 도시가 더 더운 만큼'),
    ('People per square kilometre', '1제곱킬로미터당 인구'),
    ('Births per woman', '여성 1명당 출생아 수'),
]

TAGS = [('Seoul', 'seoul'), ('서울', '서울')]

# Set by sales_facts() so compose() can add quarter context to the source line
# instead of repeating it on every spending row.
SALES_Q = {'en': None, 'ko': None}


# --- small utilities -------------------------------------------------------

def write_json_atomic(path, data, **dumps_kwargs):
    """Write JSON via a sibling temp file and an atomic rename, so a crash
    mid-write can never leave a truncated state or cache file behind."""
    tmp = path.with_name(path.name + '.tmp')
    tmp.write_text(json.dumps(data, **dumps_kwargs))
    os.replace(tmp, path)


def http_get_json(url):
    for _ in range(3):
        r = subprocess.run(['curl', '-s', '--max-time', '30', url],
                           capture_output=True, text=True)
        if r.returncode == 0 and r.stdout.strip():
            try:
                return json.loads(r.stdout)
            except json.JSONDecodeError:
                pass
    raise RuntimeError(f'Request failed: {url}')


def keychain_password(account, service):
    r = subprocess.run(['security', 'find-generic-password', '-a', account,
                        '-s', service, '-w'], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(
            f'No Keychain password for account="{account}" service="{service}".\n'
            f'Add it with:\n'
            f'  security add-generic-password -a "{account}" -s "{service}" -w')
    return r.stdout.strip()


def claude_env():
    env = os.environ.copy()
    r = subprocess.run(['security', 'find-generic-password', '-a', CLAUDE_TOKEN_ACCOUNT,
                        '-s', CLAUDE_TOKEN_SERVICE, '-w'], capture_output=True, text=True)
    if r.returncode == 0 and r.stdout.strip():
        env['CLAUDE_CODE_OAUTH_TOKEN'] = r.stdout.strip()
    return env


def grouped(n):
    return f'{int(round(n)):,}'


def to_f(celsius):
    """'26.5°C' -> '26.5°C (80°F)'. ENGLISH CARD ONLY: Korea is metric and a
    Korean card carrying °F would be noise, which is exactly what the two
    value strings on every fact are for."""
    return f'{celsius:.1f}°C ({celsius * 9 / 5 + 32:.0f}°F)'


def to_f_delta(celsius):
    """Same, for a temperature DIFFERENCE rather than a temperature.

    ⚠️ A difference converts with x9/5 and NO +32: the urban heat island runs
    about 2.0°C, which is 3.6°F of extra warmth, not 35.6°F. Using the
    temperature formula on it would publish a number roughly ten times too
    large and perfectly plausible-looking."""
    return f'{celsius:.1f}°C ({celsius * 9 / 5:.1f}°F)'


def to_mph(kmh):
    """'26 km/h' -> '26 km/h (16 mph)'. English card only, as with to_f."""
    return f'{kmh} km/h ({kmh * 0.621371:.0f} mph)'


def won_ko(amount):
    """Korean currency: 653,500,000,000 -> '6,535억 원'; 3.67e12 -> '3조 6,701억 원';
    1천만–1억 (a cheap apartment) -> '6,500만 원'; small per-visit amounts -> '5,441원'."""
    if amount < 1e7:
        return f'{grouped(amount)}원'
    if amount < 1e8:
        return f'{int(round(amount / 1e4)):,}만 원'
    eok = int(round(amount / 1e8))  # 억 = 10^8
    if eok >= 10000:
        jo, rem = divmod(eok, 10000)
        return f'{jo}조 {rem:,}억 원' if rem else f'{jo}조 원'
    return f'{eok:,}억 원'


def won_en(amount):
    """English currency: 6.535e11 -> '₩653.5bn'; 7.79e9 -> '₩7.8bn'; small -> '₩Xm'."""
    if amount >= 1e12:
        return f'₩{amount / 1e12:.2f}tn'
    if amount >= 1e9:
        return f'₩{amount / 1e9:.1f}bn'
    if amount >= 1e6:
        return f'₩{amount / 1e6:.0f}m'
    return f'₩{grouped(amount)}'


@lru_cache(maxsize=1)
def _names_en():
    """Korean -> English names for stations and districts. The Seoul feeds return
    Korean only, and the English card should be English throughout. Regenerate
    the table with seoul_index_names_harvest.py."""
    try:
        return json.loads(NAMES_EN.read_text())
    except (OSError, ValueError) as e:
        print(f'Warning: {NAMES_EN.name} unreadable ({e}); labels stay in Korean.')
        return {'stations': {}, 'districts': {}}


# Official English station names are abbreviated on the signage; the card has
# room to spell them out. Expansions longer than MAX_EXPANDED stay short: past
# roughly 53 characters of label plus value the row wraps, and a wrapped row
# loses its dotted leader entirely, which breaks the house style. The station
# list breaks cleanly at 28 — expansions run to "Sookmyung Women's University"
# and then jump to 36+ — so the cap only holds back a handful of monsters.
ABBREV = ((r'\bUniv\.', 'University'), (r"\bNat'l\b", 'National'),
          (r"\bInt'l\b", 'International'))
MAX_EXPANDED = 28


def spell_out(name):
    full = name
    for pattern, word in ABBREV:
        full = re.sub(pattern, word, full)
    return full if len(full) <= MAX_EXPANDED else name


def en_lookup(korean, kind):
    """The mapped English name, or None. Never warns, never invents.

    ⚠️ The boardings feeds append a landmark in brackets that the name table does
    not carry: 신정(은행정), 광화문(세종문화회관), 삼성(무역센터), 동대문역사문화공원(DDP).
    58 of the 72 station names CardSubwayTime returned unmapped in July 2026
    resolve on the bare name. Trying it can only turn a miss into a hit, since an
    exact key is looked up first and wins.
    ⚠️ The 14 it does not fix are almost all outside Seoul (천안, 연천, 직산), plus
    서울역, which the table holds as 서울: a real gap, and the reason callers that
    need a guaranteed English name must check rather than assume."""
    table = _names_en().get(kind, {})
    got = table.get(korean)
    if not got:
        got = table.get(re.sub(r'\s*\(.*?\)\s*$', '', korean or '').strip())
    return spell_out(got) if got else None


def en_name(korean, kind):
    """English name for a station/district, or the Korean original if unmapped.
    Official names are not romanisations (홍대입구 is 'Hongik Univ.', 시청 is
    'City Hall'), so an unmapped name has no safe English form — fall back and
    say so rather than invent one."""
    if not korean:
        return korean
    got = en_lookup(korean, kind)
    if not got:
        print(f'Warning: no English name for {kind[:-1]} {korean!r} — '
              f'using Korean on the English card.')
        return korean
    return got


def fact(fid, cat, label_en, value_en, value_ko, estimated=False, pair=None,
         year=None, forecast=False, label_ko=None, pin=False,
         num=None, unit=None):
    """One candidate line. `label_ko` is normally left None so the selector
    translates the label; spotlight lines set it because their labels carry
    clock times, and a translated time is a number Python no longer owns.

    `pin` extends that to English: the selector may reword a label, which is
    usually an improvement but silently drops anything it reads as ornament.
    It shortened "Subway boardings on 18 Jul" to "Subway", leaving a figure
    with no date attached to it. Pin a label whose wording is load-bearing:
    a date, a place, a named standard.

    `num` + `unit` make a fact eligible for a cross-vein collision (see
    cross_vein_pairs): `num` is the raw magnitude and `unit` its class
    ('won' or 'people'). Left None, a fact is never set against another vein's.
    Only the DETECTOR reads `num`; the posted value is still value_en, so
    Python owns every number as before. Collide like with like only — a ₩
    figure never against a head-count, people never against a count of things
    (flights, filings, museums stay un-collidable, i.e. unit left None)."""
    return {'id': fid, 'cat': cat, 'label_en': label_en, 'value_en': value_en,
            'value_ko': value_ko, 'estimated': estimated, 'pair': pair,
            'year': year, 'forecast': forecast, 'label_ko': label_ko,
            'pin': pin, 'num': num, 'unit': unit}


# --- harvesters ------------------------------------------------------------

CROWD_WINDOW = 10   # places an index card considers per post (see crowd_window)
CROWD_STRIDE = 7    # coprime with len(CROWD_SPOTS), so the walk covers them all


def crowd_window(state):
    """The places this index card will consider, as a rotating sample.

    All of CROWD_SPOTS is sampled hourly for the history log, but offering every
    one to the selector would cost an API call each for lines only three or four
    of which can be used, and would swell the prompt enough to slow the selector
    noticeably. A window keeps that cost flat while the mix changes every post.

    It STRIDES through the list rather than taking a contiguous slice, because
    the list is grouped by kind of place: a slice would hand the selector ten
    palaces one post and ten subway stations the next, when the contrast between
    a packed station and an empty riverbank is the whole point. A stride coprime
    with the list length visits every place equally often."""
    i, n = int(state.get('crowd_i', 0)), len(CROWD_SPOTS)
    state['crowd_i'] = (i + 1) % n
    return [CROWD_SPOTS[(i + k * CROWD_STRIDE) % n] for k in range(min(CROWD_WINDOW, n))]


def crowd_facts(api_key, spots=None):
    """Live crowd estimates for the given spots + a fullest/quietest contrast."""
    spots = CROWD_SPOTS if spots is None else spots
    base = f'http://openapi.seoul.go.kr:8088/{api_key}/json/citydata_ppltn'
    got = []
    for spot in spots:
        area, en = spot['area'], spot['en']
        try:
            d = http_get_json(f'{base}/1/1/{_url(area)}')
            r = d['SeoulRtd.citydata_ppltn'][0]
            mid = (int(r['AREA_PPLTN_MIN']) + int(r['AREA_PPLTN_MAX'])) // 2
            got.append({'en': en, 'mid': mid,
                        'visitor': r['NON_RESNT_PPLTN_RATE'],
                        'female': r['FEMALE_PPLTN_RATE'],
                        'twenties': r['PPLTN_RATE_20']})
        except (RuntimeError, KeyError, IndexError, ValueError):
            continue
    facts = []
    for g in got:
        facts.append(fact(f'crowd_{g["en"]}', 'crowd',
                          f'Estimated crowd in {g["en"]} right now',
                          grouped(g['mid']), grouped(g['mid']), estimated=True,
                          num=g['mid'], unit='people'))
        facts.append(fact(f'visitor_{g["en"]}', 'crowd',
                          f'Estimated share in {g["en"]} who don’t live there',
                          f'{g["visitor"]}%', f'{g["visitor"]}%', estimated=True))
        facts.append(fact(f'twenties_{g["en"]}', 'crowd',
                          f'Share of the {g["en"].removeprefix("the ")} crowd in their twenties',
                          f'{g["twenties"]}%', f'{g["twenties"]}%', estimated=True))
        facts.append(fact(f'female_{g["en"]}', 'crowd',
                          f'Women’s share of the {g["en"].removeprefix("the ")} crowd',
                          f'{g["female"]}%', f'{g["female"]}%', estimated=True))
    # Contrast pair: fullest vs quietest sampled spot.
    if len(got) >= 2:
        full = max(got, key=lambda g: g['mid'])
        quiet = min(got, key=lambda g: g['mid'])
        facts.append(fact('crowd_fullest', 'crowd',
                          f'Estimated crowd packed into {full["en"]} now',
                          grouped(full['mid']), grouped(full['mid']),
                          estimated=True, pair='crowd_gap'))
        facts.append(fact('crowd_quietest', 'crowd',
                          f'Estimated crowd at {quiet["en"]} the same minute',
                          grouped(quiet['mid']), grouped(quiet['mid']),
                          estimated=True, pair='crowd_gap'))
    # Age contrast: youngest vs oldest sampled crowd, by share in their twenties.
    def _tw(g):
        try:
            return float(g['twenties'])
        except (TypeError, ValueError):
            return -1.0
    ages = [g for g in got if _tw(g) >= 0]
    if len(ages) >= 2:
        young = max(ages, key=_tw)
        old = min(ages, key=_tw)
        for g in (young, old):
            facts.append(fact(f'agegap_{g["en"]}', 'crowd',
                              f'Share of the {g["en"].removeprefix("the ")} crowd in their twenties',
                              f'{g["twenties"]}%', f'{g["twenties"]}%',
                              estimated=True, pair='age_gap'))
    return facts


def _ampm_en(h):
    if h == 0:
        return 'midnight'
    if h == 12:
        return 'noon'
    return f'{h % 12} {"a.m." if h < 12 else "p.m."}'


def _ampm_ko(h):
    if h == 0:
        return '자정'
    if h == 12:
        return '정오'
    return f'{"오전" if h < 12 else "오후"} {h % 12}시'


WEEKDAY_EN = {'Mon': 'Monday', 'Tue': 'Tuesday', 'Wed': 'Wednesday',
              'Thu': 'Thursday', 'Fri': 'Friday', 'Sat': 'Saturday',
              'Sun': 'Sunday'}
WEEKDAY_KO = {'Mon': '월요일', 'Tue': '화요일', 'Wed': '수요일', 'Thu': '목요일',
              'Fri': '금요일', 'Sat': '토요일', 'Sun': '일요일'}


def spotlight_facts(api_key, spot):
    """One place, over time, rather than places against each other.

    Everything here comes from a single citydata_ppltn call plus the bot's own
    accumulated log. The endpoint knows the present and the next 12 hours and
    nothing else, so the peak and trough lines are the busiest and quietest
    hours AHEAD, not of the day: the morning that already happened is not in the
    data, and calling this "today" would claim otherwise. The typical-for-this-
    weekday line comes from crowd_history.jsonl and simply does not appear until
    three separate weeks have been observed.

    Returns facts in reading order (compose keeps it), or [] if the place did
    not answer well enough for a card."""
    area, en = spot['area'], spot['en']
    try:
        d = http_get_json(
            f'http://openapi.seoul.go.kr:8088/{api_key}/json/citydata_ppltn/1/1/{_url(area)}')
        r = d['SeoulRtd.citydata_ppltn'][0]
        now_mid = (int(r['AREA_PPLTN_MIN']) + int(r['AREA_PPLTN_MAX'])) // 2
    except (RuntimeError, KeyError, IndexError, ValueError):
        return []

    stamp = r.get('PPLTN_TIME') or ''
    try:                                   # the reading's own clock, not ours
        now_h = int(stamp[11:13])
    except (ValueError, IndexError):
        now_h = datetime.now(SEOUL_TZ).hour
    wd = datetime.now(SEOUL_TZ).strftime('%a')

    facts = [fact(f'spot_now_{en}', 'spotlight',
                  f'Estimated crowd right now ({_ampm_en(now_h)})',
                  grouped(now_mid), grouped(now_mid), estimated=True,
                  label_ko=f'지금 추정 인구 ({_ampm_ko(now_h)})')]
    # Raw magnitudes, kept alongside the facts purely so the flat-card check at
    # the end can see them. They are never posted: the published strings are
    # the grouped() values already inside each fact, so Python still owns every
    # number exactly as before.
    nums = [now_mid]

    # Typical for this weekday and hour, from our own observations. Sits second
    # so it lands next to the live figure it gives meaning to.
    try:
        from seoul_index_crowd_log import baseline
        base = baseline(en, wd, now_h)
    except Exception:                      # no log yet, or unreadable — skip
        base = None
    if base:
        mean, days = base
        facts.append(fact(f'spot_usual_{en}', 'spotlight',
                          f'Usual for a {WEEKDAY_EN.get(wd, wd)} at {_ampm_en(now_h)}',
                          grouped(mean), grouped(mean), estimated=True,
                          label_ko=f'{WEEKDAY_KO.get(wd, wd)} {_ampm_ko(now_h)} 평균'))
        nums.append(mean)

    pts = []
    for x in (r.get('FCST_PPLTN') or []):
        try:
            pts.append((int(x['FCST_TIME'][11:13]),
                        (int(x['FCST_PPLTN_MIN']) + int(x['FCST_PPLTN_MAX'])) // 2))
        except (KeyError, ValueError, IndexError):
            continue
    if len(pts) >= 2:
        hi = max(pts, key=lambda p: p[1])
        lo = min(pts, key=lambda p: p[1])
        if hi[0] != lo[0]:                 # a flat forecast says nothing
            facts.append(fact(f'spot_peak_{en}', 'spotlight',
                              f'Busiest hour ahead ({_ampm_en(hi[0])})',
                              grouped(hi[1]), grouped(hi[1]),
                              estimated=True, forecast=True,
                              label_ko=f'가장 붐빌 시간 ({_ampm_ko(hi[0])})'))
            facts.append(fact(f'spot_quiet_{en}', 'spotlight',
                              f'Quietest hour ahead ({_ampm_en(lo[0])})',
                              grouped(lo[1]), grouped(lo[1]),
                              estimated=True, forecast=True,
                              label_ko=f'가장 한산할 시간 ({_ampm_ko(lo[0])})'))
            nums += [hi[1], lo[1]]
    if len(facts) < 3:
        return []
    # Reject a card whose readings are the same number wearing different labels
    # (see SPOTLIGHT_MIN_DISTINCT). Returning [] puts the run on the normal
    # index path, which is what already happens when a place answers thinly.
    distinct = len(set(nums))
    hi_n = max(nums)
    spread = (hi_n - min(nums)) / hi_n if hi_n else 0.0
    if distinct < SPOTLIGHT_MIN_DISTINCT or spread < SPOTLIGHT_MIN_SPREAD:
        print(f'Spotlight on {en} is flat ({distinct} distinct value(s), '
              f'{spread:.0%} spread; needs {SPOTLIGHT_MIN_DISTINCT} and '
              f'{SPOTLIGHT_MIN_SPREAD:.0%}) — no card in it.')
        return []
    return facts


def spotlight_sel(spot, facts):
    """The selector's job on a spotlight card is already done: the lines are
    fixed, in order, and their labels carry clock times that must not be
    reworded or re-translated. So build its answer in Python instead of asking,
    which also spares a claude -p call. The opener names the place in each
    language from CROWD_SPOTS, so nothing needs translating at all."""
    en, ko = spot['en'], spot['ko']
    place_en = en[0].upper() + en[1:]
    # The source reply also points at the place itself, so a reader who does not
    # know Jamsil can go and find out. Anchor text is the name the card used;
    # the article behind it may be titled differently (Jamsil-dong).
    # The heading capitalises ("The Gangseo riverbank, hour by hour"), but the
    # link sits mid-sentence, where a leading article reads as a mistake either
    # capitalised or not — so the anchor drops it.
    anchor_en = en[4:] if en.startswith('the ') else place_en
    wiki = {}
    if spot.get('wiki_en'):
        wiki['wiki_en'] = (' · Wikipedia: ', anchor_en, spot['wiki_en'])
    if spot.get('wiki_ko'):
        wiki['wiki_ko'] = (' · 위키백과: ', ko, spot['wiki_ko'])
    return {
        'opener_en': f'{place_en}, hour by hour',
        'opener_ko': f'{ko}, 시간대별',
        'opener_emoji': '📍',
        'note': f'single-place spotlight: {en}',
        'picks': [{'id': f['id'], 'label_en': f['label_en'],
                   'label_ko': f['label_ko'], 'emoji': ''} for f in facts],
        **wiki,
    }


def air_facts(api_key):
    try:
        d = http_get_json(
            f'http://openapi.seoul.go.kr:8088/{api_key}/json/ListAirQualityByDistrictService/1/25/')
        rows = [v for v in d.values() if isinstance(v, dict) and 'row' in v][0]['row']
        vals = [(x.get('MSRSTE_NM') or x.get('SAREA_NM') or x.get('MSRSTN_NM'),
                 float(x['FPM'])) for x in rows
                if str(x.get('FPM', '')).replace('.', '', 1).isdigit()]
        if not vals:
            return []
        worst = max(vals, key=lambda t: t[1])
        # FPM is PM2.5, not PM10. The service documents its measured values as
        # 미세먼지(PM-10), 오존, 이산화질소, 일산화탄소, 아황산가스, and OZON/NTDX/CBMX/SPDX
        # take four of those, leaving PM as the documented PM-10 and FPM as the
        # fine fraction — PM >= FPM in 23 of 25 districts, and CRST_SBSTN names
        # "PM-2.5" in its own right. So the Korean term is 초미세먼지; 미세먼지 would
        # be PM10, a different number. English names the standard outright rather
        # than saying "fine dust", which is the ambiguity that caused this.
        return [fact('air_monitors', 'air', 'Air-quality monitors reporting live across Seoul',
                     str(len(vals)), str(len(vals)), pin=True),
                fact('air_worst', 'air',
                     f'Worst PM2.5 right now ({en_name(worst[0], "districts")})',
                     f'{worst[1]:.0f} µg/m³', f'{worst[1]:.0f} µg/m³', pin=True,
                     label_ko=f'지금 초미세먼지가 가장 심한 곳 ({worst[0]})')]
    except (RuntimeError, KeyError, IndexError, ValueError):
        return []


def _latest_daily(api_key, service, day_field_ok):
    """Walk back from today (KST) to the most recent date the service has rows for."""
    base = f'http://openapi.seoul.go.kr:8088/{api_key}/json/{service}'
    today = datetime.now(SEOUL_TZ).date()
    for back in range(2, 10):
        day = (today - timedelta(days=back)).strftime('%Y%m%d')
        try:
            d = http_get_json(f'{base}/1/1/{day}')
            body = d.get(service, {})
            if body.get('list_total_count'):
                return day, int(body['list_total_count'])
        except RuntimeError:
            continue
    return None, 0


def transport_facts(api_key, state):
    """Subway + bus daily totals for the latest available date. Cached per-day in
    state so the second post of the day doesn't re-sum ~42 bus pages."""
    day, sub_total_rows = _latest_daily(api_key, 'CardSubwayStatsNew', True)
    if not day:
        return []
    cache = state.get('transport_cache', {})
    if cache.get('date') == day:
        c = cache
    else:
        base = f'http://openapi.seoul.go.kr:8088/{api_key}/json'
        # Subway: one page holds all ~617 stations.
        sd = http_get_json(f'{base}/CardSubwayStatsNew/1/{max(sub_total_rows, 700)}/{day}')
        srows = [x for x in sd['CardSubwayStatsNew']['row'] if x['GTON_TNOPE'].isdigit()]
        sub_total = sum(int(x['GTON_TNOPE']) for x in srows)
        # Busiest station, and quietest *sane* one (drop sub-handful feed artifacts
        # at major stations by ignoring boardings < 10).
        srows.sort(key=lambda x: int(x['GTON_TNOPE']))
        busiest = srows[-1]
        sane = [x for x in srows if int(x['GTON_TNOPE']) >= 10]
        quietest = sane[0] if sane else srows[0]
        # Bus: page through the day.
        bd0 = http_get_json(f'{base}/CardBusStatisticsServiceNew/1/1/{day}')
        btot_rows = int(bd0['CardBusStatisticsServiceNew']['list_total_count'])
        bus_total = 0
        route = {}
        for s in range(1, btot_rows + 1, 1000):
            bd = http_get_json(f'{base}/CardBusStatisticsServiceNew/{s}/{min(s + 999, btot_rows)}/{day}')
            for x in bd.get('CardBusStatisticsServiceNew', {}).get('row', []):
                v = int(x.get('GTON_TNOPE', '0') or 0)
                bus_total += v
                route[x.get('RTE_NM', '?')] = route.get(x.get('RTE_NM', '?'), 0) + v
        top_route = max(route.items(), key=lambda kv: kv[1]) if route else ('?', 0)
        c = {'date': day, 'sub_total': sub_total, 'bus_total': bus_total,
             'busiest_st': busiest['SBWY_STNS_NM'], 'busiest_v': int(busiest['GTON_TNOPE']),
             'quietest_st': quietest['SBWY_STNS_NM'], 'quietest_v': int(quietest['GTON_TNOPE']),
             'top_route': top_route[0], 'top_route_v': top_route[1]}
        state['transport_cache'] = c

    dt = datetime.strptime(c['date'], '%Y%m%d')
    d = dt.strftime('%-d %B')
    d_ko = f'{dt.month}월 {dt.day}일'
    # All four are pinned: the date says which day the count belongs to, and the
    # station names are the ones looked up from the English name table, so
    # neither is the selector's to reword away.
    facts = [
        fact('sub_total', 'transport', f'Subway boardings on {d}',
             grouped(c['sub_total']), grouped(c['sub_total']), pair='modes',
             pin=True, label_ko=f'{d_ko} 지하철 승차 인원',
             num=c['sub_total'], unit='people'),
        fact('bus_total', 'transport', f'Bus boardings the same day',
             grouped(c['bus_total']), grouped(c['bus_total']), pair='modes',
             pin=True, label_ko='같은 날 버스 승차 인원',
             num=c['bus_total'], unit='people'),
        # The station name is set in both languages here rather than left to the
        # selector: it would otherwise carry "Hongik Univ." across to the Korean
        # card in Latin script.
        # "subway station", not just "station": these two lines are often
        # selected alongside the bus total with no "Subway boardings" row to lean
        # on (see the card that pairs a bus figure straight above them), so each
        # line must name its own mode or the reader reads them as bus stops. The
        # cost is width: the card row fits about 53 characters of label plus value
        # before it wraps and drops its dotted leader, and station names run long
        # (46 chars for "Gyeonggi Provincial Government Northern Office"). The
        # busiest is always a major hub with a short name, so it fits; only a
        # long-named quietest station wraps, which is a graceful loss of the
        # leader, not a break.
        fact('sub_busiest', 'transport',
             f'Busiest subway station, {en_name(c["busiest_st"], "stations")}',
             grouped(c['busiest_v']), grouped(c['busiest_v']), pair='station_gap', pin=True,
             label_ko=f'가장 붐빈 지하철역, {c["busiest_st"]}',
             num=c['busiest_v'], unit='people'),
        fact('sub_quietest', 'transport',
             f'Quietest subway station, {en_name(c["quietest_st"], "stations")}',
             grouped(c['quietest_v']), grouped(c['quietest_v']), pair='station_gap', pin=True,
             label_ko=f'가장 한산한 지하철역, {c["quietest_st"]}',
             num=c['quietest_v'], unit='people'),
    ]
    return facts


# --- rush hour -------------------------------------------------------------
# Added 25 August 2026. transport_facts() above counts a whole DAY through the
# turnstiles; this reads the same turnstiles along a CLOCK, from CardSubwayTime:
# monthly, per line x station, 24 hourly boarding columns, published back to
# January 2015 and never used here until now.
#
# The card it exists to make is ONE STATION AT TWO HOURS. 종각 took 10,872
# boardings at 8 in the morning in July 2026 and 227,972 at 6 in the evening,
# twenty-one times as many: nobody boards at 종각 in the morning because nobody
# sleeps there. Set against a station of the opposite kind (신정, 77,726 against
# 15,585, the other way about) the four figures say where Seoul sleeps and where
# it works with no comment attached, which is the house style exactly.
#
# ⚠️ CARDSUBWAYTIME SERVES EVERY ROW TWICE, BYTE-IDENTICAL. July 2026 returns
# 1,242 rows that are 621 real ones: same line, same station, same 48 figures,
# not one field differing. Summed naively every figure here is exactly double,
# and nothing looks wrong — the ratios hold, the ranking holds, the card reads
# perfectly, and only the numbers lie. It was caught by checking a month against
# the DAILY feed transport_facts() already reads: de-duplicated, subway and bus
# both come to 0.87 of a single weekday, which is what a month including
# weekends should be; doubled, the subway alone came to 1.74x a weekday while
# the bus stayed right, so the two feeds disagreed. ⚠️ CardBusTimeNew does NOT
# do this (5,000 rows, 5,000 distinct), so it cannot be fixed once for both.
#
# ⚠️ Whole-row identity is the de-duplication test on purpose: keying on
# (line, station) would silently drop a genuinely different second row, and that
# would be real data thrown away.
#
# ⚠️ EVERY FIGURE IS A WHOLE MONTH OF THAT HOUR, NEVER ONE DAY'S. A bare
# "종각, 6 p.m.: 227,972" under a month dateline reads as one evening, which is
# wrong by a factor of about thirty. compose() puts that in the card footnote
# and SELECT_PROMPT forbids the selector implying otherwise.
RUSH_M = {'en': None, 'ko': None}
RUSH_AM, RUSH_PM = 8, 18    # the two peaks of the citywide profile, every month
RUSH_FLOOR = 300_000        # monthly boardings a station must clear to be
                            # offered. Without it the extremes of the ratio are
                            # tiny stations where a few dozen people decide the
                            # whole finding.
RUSH_STATIONS = 2           # one of each kind, each offered as its two hours


def _rush_month(api_key, month):
    """{station: [24 hourly boardings]} for one month, or {} if not published."""
    base = f'http://openapi.seoul.go.kr:8088/{api_key}/json/CardSubwayTime'
    try:
        head = http_get_json(f'{base}/1/1/{month}')
    except RuntimeError:
        return {}
    body = head.get('CardSubwayTime') or {}
    total = int(body.get('list_total_count') or 0)
    if not total:
        return {}
    rows = []
    for start in range(1, total + 1, 1000):
        try:
            d = http_get_json(f'{base}/{start}/{min(start + 999, total)}/{month}')
        except RuntimeError:
            return {}
        rows += d.get('CardSubwayTime', {}).get('row', [])
    if len(rows) != total:
        return {}      # a short read must not become a quieter city
    seen, agg = set(), {}
    for r in rows:
        sig = tuple(sorted((k, str(v)) for k, v in r.items()))
        if sig in seen:
            continue   # the exact-duplicate row; see the warning above
        seen.add(sig)
        hours = agg.setdefault(r.get('STTN', ''), [0] * 24)
        for h in range(24):
            try:
                hours[h] += int(float(r.get(f'HR_{h}_GET_ON_NOPE') or 0))
            except (TypeError, ValueError):
                return {}
    agg.pop('', None)
    return agg


def rush_facts(api_key, state):
    """One station at its morning hour and its evening hour, both ends of the
    city. Cached per month in state: the source publishes monthly, so a second
    post the same day must not re-fetch it."""
    now = datetime.now(SEOUL_TZ).date().replace(day=1)
    cache = state.get('rush_cache') or {}
    month, picks = cache.get('month'), cache.get('picks')
    if not picks:
        agg = {}
        for _ in range(6):
            month = f'{now:%Y%m}'
            agg = _rush_month(api_key, month)
            if agg:
                break
            now = (now - timedelta(days=1)).replace(day=1)
        if not agg:
            return []
        scored, unnamed = [], []
        for st, h in agg.items():
            am, pm = h[RUSH_AM], h[RUSH_PM]
            if sum(h) < RUSH_FLOOR or not am or not pm:
                continue
            # ⚠️ A station with no English name is SKIPPED, not romanised and not
            # printed in Korean on the English card — the same rule the box
            # office vein applies to a film KOFIC has no English title for. The
            # skip is logged rather than silent, or a name-table gap looks like a
            # vein nobody picks.
            if not en_lookup(st, 'stations'):
                unnamed.append(st)
                continue
            scored.append(((am - pm) / (am + pm), st, am, pm))
        if unnamed:
            print(f'rush: {len(unnamed)} station(s) skipped for want of an '
                  f'English name: {", ".join(sorted(unnamed)[:6])}')
        if len(scored) < RUSH_STATIONS * 2:
            return []
        scored.sort()
        # The two ends of the same axis: the most evening-boarded station and
        # the most morning-boarded one. Measured every month, never hardcoded —
        # a named station would rot the first time the city changed shape.
        picks = [list(scored[0][1:]), list(scored[-1][1:])]
        state['rush_cache'] = {'month': month, 'picks': picks}
    dt = datetime.strptime(month, '%Y%m')
    RUSH_M['en'] = f'{MONTHS_EN[dt.month - 1]} {dt.year}'
    RUSH_M['ko'] = f'{dt.year}년 {dt.month}월'
    facts = []
    for i, (st, am, pm) in enumerate(picks):
        en = en_name(st, 'stations')
        for h, v in ((RUSH_AM, am), (RUSH_PM, pm)):
            # Pinned in both languages: the label is a station and a clock time,
            # and a translated time is a number Python no longer owns. Same rule
            # as spotlight_facts.
            facts.append(fact(f'rush_{i}_{h}', 'rush',
                              f'{en}, {_ampm_en(h)}',
                              grouped(v), grouped(v), pair=f'rush_{i}',
                              pin=True, label_ko=f'{st}, {_ampm_ko(h)}',
                              num=v, unit='people'))
    return facts


def count_facts(api_key):
    """Cheap structural counts + cheapest listed cultural event."""
    base = f'http://openapi.seoul.go.kr:8088/{api_key}/json'
    out = []

    def total(service):
        d = http_get_json(f'{base}/{service}/1/1/')
        body = [v for v in d.values() if isinstance(v, dict) and 'list_total_count' in v]
        return int(body[0]['list_total_count']) if body else None

    # 4th tuple element (label_ko) is documentation only — count facts leave
    # label_ko None so the selector translates, exactly as the pre-existing rows
    # already do. Service names all verified live (INFO-000 + a count) 28 Jul 2026.
    specs = [('wifi', 'TbPublicWifiInfo', 'Public Wi-Fi hotspots the city runs', '공공 와이파이 수'),
             ('library', 'SeoulPublicLibraryInfo', 'Public libraries', None),
             # Seoul Library's catalogue, added 21 Aug 2026 from the portal
             # sweep. It is ONE library's holdings, not the city's, which is
             # why the label names it rather than saying 'Books in Seoul'.
             ('holdings', 'SeoulLibraryBookSearchInfo',
              'Items in Seoul Library’s catalogue', '서울도서관 소장자료 수'),
             ('park', 'SearchParkInfoService', 'Major parks', None),
             ('busstop', 'busStopLocationXyInfo', 'Bus stops citywide', None),
             ('events', 'culturalEventInfo', 'Cultural events on the city’s listings', None),
             ('culture_space', 'culturalSpaceInfo',
              'Cultural spaces: museums, galleries, halls', '박물관·미술관·공연장 등 문화공간 수'),
             ('carpark', 'GetParkInfo',
              'Car parks in the city’s parking system', '주차 정보에 등록된 주차장 수')]
    for fid, service, label, _ in specs:
        try:
            n = total(service)
            if n:
                out.append(fact(f'count_{fid}', 'infra', label, grouped(n), grouped(n),
                                pair='infra' if fid in ('busstop', 'library') else None))
        except (RuntimeError, KeyError, IndexError, ValueError):
            continue
    return out


def bike_facts(api_key):
    """Live Ttareungi (public-bike) numbers, citywide, right now.

    The bikeList service returns one row per docking station — bikes currently
    parked (parkingBikeTotCnt), rack capacity (rackTotCnt) and occupancy — and
    refreshes continuously (data.seoul.go.kr 갱신주기 '수시'). ~2,700 stations in
    pages of 1,000. Unlike CardBus/CardSubway, bikeList's list_total_count only
    echoes the page size, so it is NO USE as a grand total: page until a short
    page instead (verified live on the Mini 28 Jul 2026 — 1000+1000+742 rows).

    Aggregate-only, by design: station names come back as messy Korean strings
    ('102. 망원역 1번출구 앞') that the English name table does not carry, so a
    named line would fall back to Korean on the English card. The citywide totals
    carry the story without names, and stay fully owned by Python."""
    base = f'http://openapi.seoul.go.kr:8088/{api_key}/json/bikeList'
    stations = bikes = racks = empty = 0
    start, page = 1, 1000
    while start <= 20000:   # safety cap, far above the ~2,700 real stations
        try:
            d = http_get_json(f'{base}/{start}/{start + page - 1}/')
        except RuntimeError:
            return []   # a network failure would misreport the citywide totals
        # bikeList's list_total_count just echoes the page size, so it can't be
        # used as a grand total; page until a short page instead. Past the last
        # station the response simply omits 'rentBikeStatus', so .get() -> {} ->
        # no rows -> stop cleanly.
        body = d.get('rentBikeStatus') or {}
        rows = body.get('row') or []
        if not rows:
            break
        for x in rows:
            b = int(float(x.get('parkingBikeTotCnt', 0) or 0))
            racks += int(float(x.get('rackTotCnt', 0) or 0))
            stations += 1
            bikes += b
            if b == 0:
                empty += 1
        if len(rows) < page:
            break   # last (partial) page
        start += page
    if not stations:
        return []
    # pin the two "right now" labels: the selector would otherwise trim "right
    # now" as ornament and leave a live count reading like a fixed total.
    return [
        fact('bike_avail', 'bike', 'Ttareungi bikes waiting at a dock right now',
             grouped(bikes), grouped(bikes), pair='bike_stock', pin=True,
             label_ko='지금 거치대에 있는 따릉이 수'),
        fact('bike_racks', 'bike', 'Ttareungi docking points across Seoul',
             grouped(racks), grouped(racks), pair='bike_stock',
             label_ko='서울 전역 따릉이 거치대 수'),
        fact('bike_stations', 'bike', 'Ttareungi stations citywide',
             grouped(stations), grouped(stations), pair='bike_reach'),
        fact('bike_empty', 'bike', 'Ttareungi stations with no bike left right now',
             grouped(empty), grouped(empty), pair='bike_reach', pin=True,
             label_ko='지금 따릉이가 한 대도 없는 대여소 수'),
    ]


TRAFFIC_LINKS = HERE / 'traffic_links.json'


def _traffic_speed(api_key, link_id):
    """Current speed (km/h) on one TOPIS road link, or None.

    TrafficInfo is keyed by a single 표준링크 id and returns prcs_spd +
    prcs_trv_time for it; there is no citywide listing, which is why the links
    are curated by hand (see traffic_facts / traffic_links.json). Verified live
    28 Jul 2026: xml/TrafficInfo/1/1/1220003800 -> prcs_spd 26. The service
    rejected json under the shared sample key, so ask for xml and parse with ET
    (both already used elsewhere in this file)."""
    url = f'http://openapi.seoul.go.kr:8088/{api_key}/xml/TrafficInfo/1/1/{link_id}'
    for _ in range(3):
        r = subprocess.run(['curl', '-s', '--max-time', '30', url],
                           capture_output=True, text=True)
        if r.returncode == 0 and r.stdout.strip():
            try:
                root = ET.fromstring(r.stdout)
            except ET.ParseError:
                continue
            if (root.findtext('.//CODE') or '') != 'INFO-000':
                return None
            spd = root.findtext('.//row/prcs_spd')
            if spd and spd.strip().isdigit():
                return int(spd)
    return None


def traffic_facts(api_key):
    """Live road speeds on a curated set of Seoul's signature arteries.

    TrafficInfo gives speed per 표준링크 id only, so seoul-index carries its own
    name -> link_id table (traffic_links.json). Fill that table by harvesting real
    link ids on the Mini with the live key: the shared sample key returns nothing
    for the listing services, and a road's link id comes from TOPIS
    (topis.seoul.go.kr) or the Seoul standard node-link dataset, not from this
    service. A single speed is not a Harper's set, so the vein stays inert until
    at least two links resolve. Rows whose name_en begins with '_' are skipped,
    which is how the seed row (one verified link, road name unknown) sits in the
    file without ever being posted."""
    try:
        links = json.loads(TRAFFIC_LINKS.read_text())
    except (OSError, ValueError):
        return []
    out = []
    for entry in links:
        lid = str(entry.get('link_id', '')).strip()
        name_en = entry.get('name_en') or ''
        if not lid or not name_en or name_en.startswith('_'):
            continue   # placeholder / unharvested row
        spd = _traffic_speed(api_key, lid)
        if spd is None:
            continue
        # Bare road names, like the OECD 'world' lines: the opener must name the
        # metric ("How fast Seoul is driving right now"), so pin the label to keep
        # the road name and let the selector supply that framing.
        out.append(fact(f'traffic_{lid}', 'traffic', name_en,
                        to_mph(spd), f'{spd}km/h', pair='traffic_speed',
                        pin=True, label_ko=entry.get('name_ko') or name_en))
    return out if len(out) >= 2 else []


# --- river -----------------------------------------------------------------
# Absorbed from the Han River bot (@hanrivernow) on 21 Aug 2026. That bot ran on
# this same Seoul key from 18 July and never published a thing: it was held back
# for a CCTV still whose ITS key took 34 days to approve, by which point a ninth
# Bluesky account was the wrong answer. Its readings became a vein here instead.
#
# ⚠️ The vein is water AGAINST AIR, not river against river. On 21 Aug 2026 the
# four stations sat within 1.7°C of each other (안양천 28.8, 탄천 28.2, 선유 28.1,
# 중랑천 27.1), which is four numbers that nearly match: no double-take, no card.
# The contrast that carries a card is the water disagreeing with the sky, which
# it does by several degrees for most of the year. So the air reading is part of
# the vein rather than a companion to it, and a card must include it.
#
# ⚠️ Every line in a card shares ONE reading hour, and that is load-bearing.
# 선유 is the ONLY Han main-stem station and publishes about five hours behind
# the three tributaries — over the ten days to 21 Aug 2026 it logged 246 valid
# readings against their 251, the shortfall being exactly the most recent hours.
# Taking each station's newest reading would set a 1 p.m. river beside a 6 p.m.
# sky and call the result "right now", which is the Overcast-offset mistake in
# another costume. So the harvester picks the newest hour the stations agree on
# and asks KMA for the air AT THAT HOUR. 초단기실황 serves any hour in the last
# 24 ("최근 1일 간의 자료만 제공합니다"), which always covers 선유's lag.
WPOS_STATIONS = [('선유', 'The Han at Seonyu', '한강(선유)'),
                 ('탄천', 'The Tancheon', '탄천'),
                 ('중랑천', 'The Jungnangcheon', '중랑천'),
                 ('안양천', 'The Anyangcheon', '안양천')]
WPOS_ROWS = 120          # ~30 hours over the four stations, newest first
WPOS_MIN_STATIONS = 3    # an hour fewer stations agree on is not a card
RIVER_MIN_SPREAD_C = 3.0  # warmest reading minus coolest; below this the
                         # vein stays inert (see river_facts)
HAN_STATION = '선유'      # the only main-stem station; a card without it is not
                         # a card about the Han (see river_facts)

# 종로구 on the KMA forecast grid: central Seoul, the same 예보구역 as ASOS 108.
KMA_NOW_NX, KMA_NOW_NY = 60, 127

# Set by river_facts() so compose() can footnote the reading hour beside the
# figures, the same split books, sales and property make.
RIVER_PERIOD = {'en': None, 'ko': None}


def _wpos_grid(api_key):
    """(YMD, HR) -> {station: °C}, from the hourly water-quality readings."""
    url = (f'http://openapi.seoul.go.kr:8088/{api_key}/json/'
           f'WPOSInformationTime/1/{WPOS_ROWS}/')
    try:
        d = http_get_json(url)
    except RuntimeError:
        return {}
    body = d.get('WPOSInformationTime') or {}
    if ((body.get('RESULT') or {}).get('CODE') or '') != 'INFO-000':
        return {}
    grid = {}
    for r in body.get('row') or []:
        # WATT is the water temperature. It reads '점검중' while a station is
        # under maintenance, and is sometimes blank; both must be skipped
        # rather than coerced.
        try:
            v = float((r.get('WATT') or '').strip())
        except ValueError:
            continue
        grid.setdefault((r.get('YMD'), r.get('HR')), {})[r.get('MSRSTN_NM')] = v
    return grid


def _kma_air_at(key, ymd, hhmm):
    """Air temperature °C from 초단기실황 at one hour, or None.

    This is an observation (the 예보구역's representative AWS reading), not a
    forecast, which is what lets it sit on a card that promises figures as
    published. Open-Meteo was the alternative and was rejected for that reason:
    its 'current' is a model's estimate and its 'feels like' plainly derived."""
    if not key:
        return None
    p = {'serviceKey': key, 'pageNo': '1', 'numOfRows': '10',
         'dataType': 'JSON', 'base_date': ymd, 'base_time': hhmm,
         'nx': str(KMA_NOW_NX), 'ny': str(KMA_NOW_NY)}
    # safe='%' keeps the already-encoded service key from being double-encoded.
    url = ('http://apis.data.go.kr/1360000/VilageFcstInfoService_2.0/'
           'getUltraSrtNcst?' + urllib.parse.urlencode(p, safe='%'))
    try:
        d = http_get_json(url)
    except RuntimeError:
        return None
    try:
        items = d['response']['body']['items']['item']
    except (KeyError, TypeError):
        return None
    for it in items:
        if it.get('category') == 'T1H':
            try:
                return float(it.get('obsrValue'))
            except (TypeError, ValueError):
                return None
    return None


def river_facts(api_key, gov_key):
    """Water temperatures in Seoul's rivers, and the air above them, at one hour."""
    grid = _wpos_grid(api_key)
    if not grid:
        return []
    # 선유 is REQUIRED, not merely counted. It is the only Han main-stem
    # station, and an hour without it yields a card about the Tancheon, the
    # Jungnangcheon and the Anyangcheon — three tributaries, no Han, on an
    # account whose readers came for the river. Because 선유 lags the others by
    # about five hours, the newest hour that satisfies this is usually not the
    # newest hour in the grid; that is the trade, and the footnote carries the
    # hour so the staleness is stated rather than hidden.
    stamp = next((s for s in sorted(grid, reverse=True)
                  if HAN_STATION in grid[s] and len(grid[s]) >= WPOS_MIN_STATIONS),
                 None)
    if stamp is None:
        return []
    ymd, hr = stamp
    cells = grid[stamp]
    air = _kma_air_at(gov_key, ymd, (hr or '').replace(':', ''))
    if air is None:
        # Without the air line the remaining readings are four near-identical
        # numbers (see the note above), so the vein stays inert rather than
        # offering the selector a flat card.
        return []

    try:
        stamp_dt = datetime.strptime(f'{ymd} {hr}', '%Y%m%d %H:%M')
    except ValueError:
        return []
    # The hour alone would mislead on a reading taken yesterday, which a
    # late run plus 선유's lag can produce, so THE DATE ALWAYS RIDES ALONG.
    # Until 23 August 2026 it appeared only when the reading was not from today,
    # which left the ordinary card headed by a bare "noon" — a dateline that
    # dates nothing and quietly asks the reader to assume today, on a vein whose
    # whole hazard is that 선유 lags the other stations by about five hours.
    #
    # .capitalize() rather than a noon/midnight special case: it is a no-op on
    # the numeral hours ("3 p.m." stays "3 p.m.") and lifts exactly the two
    # word-hours, which are the only ones that read as lowercase prose at the
    # head of a card. ⚠️ Capitalise HERE, not in _ampm_en: that helper also
    # feeds spotlight LABELS, where the hour sits mid-phrase inside brackets
    # ("Estimated crowd right now (noon)") and a capital would be wrong.
    RIVER_PERIOD['en'] = (_ampm_en(stamp_dt.hour).capitalize()
                          + f', {stamp_dt.day} {MONTHS_EN[stamp_dt.month - 1]}')
    RIVER_PERIOD['ko'] = (_ampm_ko(stamp_dt.hour)
                          + f', {stamp_dt.month}월 {stamp_dt.day}일')

    facts = []
    for ko_name, en_label, ko_label in WPOS_STATIONS:
        v = cells.get(ko_name)
        if v is None:
            continue
        facts.append(fact(f'river_watt_{ko_name}', 'river', en_label,
                          to_f(v), f'{v:.1f}°C', pair='river_now',
                          pin=True, label_ko=ko_label))
    # Bare labels like the traffic and world lines: the opener carries the
    # metric and the hour, so every label here is pinned to survive rewording.
    facts.append(fact('river_air', 'river', 'The air', to_f(air),
                      f'{air:.1f}°C', pair='river_now', pin=True,
                      label_ko='기온'))
    # One water reading plus the air is a contrast, not an index; three lines
    # is the floor a card is built from.
    if len(facts) < 3:
        return []

    # ⚠️ SPREAD GUARD, and it is the difference between a card and a shrug.
    # Measured on 21 Aug 2026 at 1 p.m.: Han 28.1, Anyangcheon 28.1, Tancheon
    # 27.4, Jungnangcheon 26.5, air 26.5 — a 1.6°C spread with the air landing
    # exactly on a tributary. Five numbers that nearly match are not a Harper's
    # index, they are a shrug, and the vein floor would have promoted that card
    # anyway because nothing here was empty or errored.
    #
    # In high summer the river and the sky converge, so this vein is SUPPOSED to
    # sleep for part of the year and wake when the gap opens: by October the air
    # runs several degrees under the water, and in winter the gap is close to
    # ten. The same shape of rule as the spotlight card's "at least three
    # distinct values, spread at least a quarter of the largest", which exists
    # because that card, too, once went out saying one thing four times.
    # Read the KOREAN value: it stays bare metric, while the English one now
    # carries an imperial conversion that no float() will survive.
    vals = [float(f['value_ko'].rstrip('°C')) for f in facts]
    if max(vals) - min(vals) < RIVER_MIN_SPREAD_C:
        print(f'river: spread {max(vals) - min(vals):.1f}°C is under '
              f'{RIVER_MIN_SPREAD_C}°C — vein inert this run')
        return []
    return facts


# --- river level (conditional) ---------------------------------------------
# The other half of what the Han River bot measured, and the half that nearly
# did not make it in. Three framings were tried and two were thrown away:
#
#   · Seoul's 16 HRFCO gauges side by side. WRONG, and confidently so. Every
#     gauge reads from its own datum, and those run from -0.068 m at 잠수교 to
#     47.946 m at 신림5교 (checked 21 Aug 2026): two gauges both showing 3 m are
#     describing water surfaces forty-eight metres apart. A card setting them in
#     a column presents a comparison that does not exist — the avgbill mistake
#     with a bigger drop.
#   · Gauge reading plus datum, to get elevation above sea level. Comparable,
#     but computed, and all it discloses is that water runs downhill.
#   · ONE gauge against ITS OWN published tiers. Kept: every number is published
#     by HRFCO, nothing is derived, and the arrangement asks the only question
#     worth asking of a river level — how far is it from trouble.
#
# ⚠️ It is CONDITIONAL, and that is the whole design. Four of the five lines are
# thresholds that never move, so as a routine vein it would post the same card
# forever and the repeat guard would (rightly) block it. So the vein sleeps
# until the river is actually doing something, and the vein floor then promotes
# it on its first appearance because it has "never posted". A quiet river is
# silence, which is the same contract harden_audit.sh and the roster check keep.
#
# ⚠️ Never write that 잠수교 is closed or submerged. These are 홍수특보 tiers, not
# the level at which the walkway goes under, and the bot must not translate one
# into the other. Name the tiers as HRFCO names them.
HRFCO_BASE = 'http://api.hrfco.go.kr'
JAMSU_OBS = '1018680'          # 서울시(잠수교)
# Mirror of the published 관심 tier, used only as the cheap gate so an ordinary
# river costs one small request. The tiers actually PRINTED on the card are read
# live from the info endpoint, so a revision by HRFCO reaches the card even if
# this constant lags. Verified against the published value 21 Aug 2026.
JAMSU_GATE_M = 3.9
LEVEL_TIERS = [('attwl', 'Attention', '관심'),
               ('wrnwl', 'Caution', '주의'),
               ('almwl', 'Alert', '경계'),
               ('srswl', 'Serious', '심각')]

# Set by level_facts() so compose() can footnote the reading time.
LEVEL_PERIOD = {'en': None, 'ko': None}


def _hrfco_latest_level(key):
    """(level_m, reading_datetime) at 잠수교, or None.

    ⚠️ HRFCO returns the range NEWEST-FIRST, so the last row is the OLDEST.
    Sort before taking the newest rather than indexing off the end; reading
    rows[-1] silently yields a figure hours stale and looks perfectly fine.

    ⚠️ The 10M grid aligns to the START minute of the window, so an unfloored
    'now' asks for :X6 slots the station never reports and every wl comes back
    blank. Floor the window to a ten-minute boundary."""
    now = datetime.now(SEOUL_TZ)
    base = now.replace(minute=now.minute // 10 * 10, second=0, microsecond=0)
    url = (f'{HRFCO_BASE}/{key}/waterlevel/list/10M/{JAMSU_OBS}/'
           f'{(base - timedelta(hours=2)):%Y%m%d%H%M}/{base:%Y%m%d%H%M}.json')
    try:
        d = http_get_json(url)
    except RuntimeError:
        return None
    pts = []
    for r in (d.get('content') or []):
        raw = str(r.get('wl') or '').strip()
        if not raw:
            continue        # the most recent slot or two are routinely blank
        try:
            pts.append((str(r.get('ymdhm')), float(raw)))
        except ValueError:
            continue
    if not pts:
        return None
    pts.sort()
    stamp, wl = pts[-1]
    try:
        when = datetime.strptime(stamp, '%Y%m%d%H%M')
    except ValueError:
        return None
    return wl, when


def _hrfco_tiers(key):
    """The four published warning tiers for 잠수교, or None."""
    try:
        d = http_get_json(f'{HRFCO_BASE}/{key}/waterlevel/info.json')
    except RuntimeError:
        return None
    row = next((r for r in (d.get('content') or [])
                if r.get('wlobscd') == JAMSU_OBS), None)
    if not row:
        return None
    tiers = []
    for field, en, ko in LEVEL_TIERS:
        try:
            tiers.append((float(str(row.get(field)).strip()), en, ko))
        except (TypeError, ValueError):
            return None     # a partial tier set would misdescribe the river
    return tiers


def level_facts(hrfco_key):
    """The Han at 잠수교 against its own flood tiers — only when it is high."""
    if not hrfco_key:
        return []
    latest = _hrfco_latest_level(hrfco_key)
    if latest is None:
        return []
    wl, when = latest
    if wl < JAMSU_GATE_M:
        return []           # an ordinary river is not news; stay silent
    tiers = _hrfco_tiers(hrfco_key)
    if not tiers:
        return []

    # Dated and capitalised on the same rule as the river card's hour, and for
    # a sharper reason: this scope entry is the card's only datable period, so
    # it is always the one lifted to the masthead dateline, where a bare
    # lowercase "3 p.m." headed a card about a river in flood without saying
    # which day it was in flood. A card that may be read back weeks later, and
    # the one card on this account where the reader's question is when.
    # ⚠️ Capitalise HERE, not in _ampm_en, which also feeds the spotlight
    # labels that read "... (noon)" mid-phrase. See river_facts.
    LEVEL_PERIOD['en'] = (_ampm_en(when.hour).capitalize()
                          + f', {when.day} {MONTHS_EN[when.month - 1]}')
    LEVEL_PERIOD['ko'] = (_ampm_ko(when.hour)
                          + f', {when.month}월 {when.day}일')
    # Deliberately NOT "The Han at Jamsu Bridge": the opener already names the
    # river and the gauge, and a pinned label repeating it word for word gets
    # past dedupe_labels (pins are exempt) and onto the card twice. Sorted by
    # value, this line lands in its true place among the tiers, which is the
    # whole point of the card.
    facts = [fact('level_now', 'level', 'The river now',
                  f'{wl:.2f} m', f'{wl:.2f}m', pair='level_tiers', pin=True,
                  label_ko='현재 수위')]
    for value, en, ko in tiers:
        facts.append(fact(f'level_{en.lower()}', 'level',
                          f'{en} level ({ko})', f'{value:.2f} m',
                          f'{value:.2f}m', pair='level_tiers', pin=True,
                          label_ko=f'{ko} 수위'))
    return facts


# --- market prices ---------------------------------------------------------
# Found 21 Aug 2026 by sweeping data.seoul.go.kr properly for the first time
# (3,401 datasets; see reference_seoul_portal_sweep). ~700k observations back to
# Jan 2025, refreshed roughly weekly, and it is the best-shaped source the
# account has taken on: the SAME item priced at named shops across the city, so
# a card is two published prices and the gap between them, with nothing computed
# and nothing modelled. The same shape as the property vein's dearest/cheapest.
#
# ⚠️ The feed is NEWEST-FIRST. Reading from the end returns January 2025 and
# looks like a dead archive; row 1 is a few days old. This cost a wrong
# conclusion during the build, and it is the second time in one evening a Seoul
# API's ordering did that (see the HRFCO note in level_facts).
#
# ⚠️ Shops are labelled by DISTRICT and KIND, never by name. The feed carries
# real shop names (뚝도시장, 이마트(미아점)) and the English card has no English
# for them, so a named line would fall back to Korean exactly as the bike vein's
# station names would have. District names come from the curated table the
# subway lines already use. The kind — traditional market against supermarket —
# is a published field and is the more interesting half anyway: it changes sides
# from item to item, which a card should let the reader notice unremarked.
PRICE_SVC = 'ListNecessariesPricesService'
PRICE_ROWS = 1000        # newest-first, ~2 days of observations
PRICE_MIN_LINES = 3      # a spread needs three quoted shops to be an index
PRICE_MIN_RATIO = 1.5    # dearest/cheapest below this is not worth a card
PRICE_STRIDE = 7         # coprime with the item list, so the walk covers it

# Curated because the feed's 93 product/unit pairs include several a reader
# outside Korea cannot place, and because the unit belongs in the opener rather
# than on every line. Korean labels are the feed's own wording.
PRICE_ITEMS = [
    ('배추',   '1포기',  'a napa cabbage',      '배추 1포기'),
    ('수박',   '',       'a watermelon',        '수박 한 통'),
    ('계란',   '',       'a tray of eggs',      '계란 한 판'),
    ('사과',   '',       'apples',              '사과'),
    ('삼겹살', '100g',   'pork belly, 100g',    '삼겹살 100g'),
    ('돼지고기','100g',  'pork, 100g',          '돼지고기 100g'),
    ('소고기(국산)','100g','Korean beef, 100g',  '국산 소고기 100g'),
    ('고등어', '대',     'a large mackerel',    '고등어(대)'),
    ('갈치',   '대',     'a large hairtail',    '갈치(대)'),
    ('쌀',     '10kg',   'rice, 10kg',          '쌀 10kg'),
    ('양파',   '1kg',    'onions, 1kg',         '양파 1kg'),
    ('마늘',   '1kg',    'garlic, 1kg',         '마늘 1kg'),
    ('풋고추', '100g',   'green chillies, 100g','풋고추 100g'),
    ('상추',   '100g',   'lettuce, 100g',       '상추 100g'),
    ('두부',   '380g',   'a block of tofu',     '두부 380g'),
    ('콩나물', '340g',   'bean sprouts, 340g',  '콩나물 340g'),
    ('우유',   '1L',     'milk, 1L',            '우유 1L'),
    ('소주',   '360ml',  'a bottle of soju',    '소주 360ml'),
    ('맥주',   '500ml',  'a can of beer',       '맥주 500ml'),
    ('라면',   '5개입',  'instant noodles, 5-pack', '라면 5개입'),
    ('식용유', '1.8L',   'cooking oil, 1.8L',   '식용유 1.8L'),
    ('참기름', '320ml',  'sesame oil, 320ml',   '참기름 320ml'),
    ('고추장', '1kg',    'gochujang, 1kg',      '고추장 1kg'),
    ('김치',   '3.3kg',  'kimchi, 3.3kg',       '김치 3.3kg'),
]
PRICE_KIND = {'전통시장': ('a traditional market', '전통시장'),
              '대형마트': ('a supermarket', '대형마트')}

# Set by price_facts() so compose() can date the prices and name the item.
PRICE_PERIOD = {'en': None, 'ko': None}
PRICE_LABEL = {'en': None, 'ko': None}


def _price_rows(api_key):
    """The newest page of price observations, or []."""
    url = (f'http://openapi.seoul.go.kr:8088/{api_key}/json/'
           f'{PRICE_SVC}/1/{PRICE_ROWS}/')
    try:
        d = http_get_json(url)
    except RuntimeError:
        return []
    body = d.get(PRICE_SVC) or {}
    if ((body.get('RESULT') or {}).get('CODE') or '') != 'INFO-000':
        return []
    return body.get('row') or []


def price_window(state):
    """Which item this card considers, striding so every item comes round."""
    i, n = int(state.get('price_i', 0)), len(PRICE_ITEMS)
    state['price_i'] = (i + 1) % n
    return [PRICE_ITEMS[(i + k * PRICE_STRIDE) % n] for k in range(n)]


def price_facts(api_key, state):
    """One everyday item, priced at shops across Seoul, on the newest day."""
    rows = _price_rows(api_key)
    if not rows:
        return []
    newest = max(r.get('P_DATE') or '' for r in rows)
    if not newest:
        return []
    day = [r for r in rows if r.get('P_DATE') == newest]

    for ko_name, unit, en_label, ko_label in price_window(state):
        seen = {}
        for r in day:
            if r.get('PRDLST_NM') != ko_name or (r.get('UNIT') or '') != unit:
                continue
            kind = PRICE_KIND.get(r.get('M_TYPE_NAME') or '')
            gu_ko = r.get('M_GU_NAME') or ''
            if not kind or not gu_ko:
                continue
            gu_en = en_name(gu_ko, 'districts')
            if gu_en == gu_ko:
                continue        # unmapped: en_name says so, and a Korean
                                # district on the English card is not a line
            try:
                price = float(r.get('A_PRICE'))
            except (TypeError, ValueError):
                continue
            if price <= 0:
                continue
            # One line per (district, kind): several shops of the same kind in
            # one district would otherwise put the same label on the card twice.
            key = (gu_ko, r['M_TYPE_NAME'])
            if key not in seen or price < seen[key][0]:
                seen[key] = (price, kind, gu_en, gu_ko)
        if len(seen) < PRICE_MIN_LINES:
            continue
        vals = sorted(seen.values())
        if vals[-1][0] / max(vals[0][0], 1) < PRICE_MIN_RATIO:
            continue            # too flat to be worth a reader's attention

        # Keep the two ends and, if room, the widest-apart middles: the card is
        # the SPREAD, so the extremes must both survive the selector's trim.
        # ⚠️ THE LABEL LEADS WITH WHAT THE NUMBER MEANS. "A traditional market
        # in Dongjak-gu" puts the unplaceable part first and never says the
        # thing that matters — that this is the dearest in the city that day —
        # leaving a reader who cannot place Dongjak with four prices and no way
        # to read them. The two ends therefore carry their rank, exactly as the
        # property vein's "Most paid for an apartment (Yongsan-gu)" does. The
        # rank is true of the whole city, not of the card, so it stays true
        # whichever companions the selector keeps.
        picks = [vals[0], vals[-1]] + vals[1:-1][:2]
        rank = {id(vals[0]): ('Cheapest', '가장 싼'),
                id(vals[-1]): ('Dearest', '가장 비싼')}
        try:
            d = datetime.strptime(newest, '%Y-%m-%d')
            PRICE_PERIOD['en'] = f'{d.day} {MONTHS_EN[d.month - 1]}'
            PRICE_PERIOD['ko'] = f'{d.month}월 {d.day}일'
        except ValueError:
            PRICE_PERIOD['en'] = PRICE_PERIOD['ko'] = newest
        PRICE_LABEL['en'] = en_label
        PRICE_LABEL['ko'] = ko_label
        facts = []
        for entry in picks:
            price, kind, gu_en, gu_ko = entry
            lead = rank.get(id(entry))
            label_en = f'{kind[0].capitalize()} in {gu_en}'
            label_ko = f'{gu_ko}의 {kind[1]}'
            if lead:
                label_en = f'{lead[0]}, {kind[0]} ({gu_en})'
                label_ko = f'{lead[1]} {kind[1]} ({gu_ko})'
            facts.append(fact(f'price_{ko_name}_{gu_ko}_{kind[1]}', 'price',
                              label_en, won_en(price), won_ko(price),
                              pair='price_spread', pin=True, label_ko=label_ko,
                              num=price, unit='won'))
        return facts
    return []


# --- waterworks ------------------------------------------------------------
# WoWcbsDayStatic, found in the 21 Aug 2026 portal sweep. Daily, and genuinely
# daily: it carried yesterday's figures when built. Two kinds of site report —
# 정수센터 (purification centres, measuring 취수 intake and 송수 transmission) and
# 수도사업소 (district waterworks offices, measuring 공급량 supplied) — and the
# vein uses ONE measure at a time so the lines are actually comparable. Setting
# an intake figure beside a supply figure would look like a ranking of places
# and be a comparison of two different things.
WATER_SVC = 'WoWcbsDayStatic'
WATER_ROWS = 300
WATER_MIN_LINES = 3
# The five purification centres, curated rather than romanised: 뚝도 is not the
# 뚝섬 in the station table and would take its name wrongly.
WATER_SITES = {'암사': 'Amsa', '강북': 'Gangbuk', '뚝도': 'Ttukdo',
               '구의': 'Guui', '영등포': 'Yeongdeungpo'}
WATER_PERIOD = {'en': None, 'ko': None}


def water_facts(api_key):
    """Water drawn at each of Seoul's purification centres, on one day."""
    try:
        d = http_get_json(f'http://openapi.seoul.go.kr:8088/{api_key}/json/'
                          f'{WATER_SVC}/1/{WATER_ROWS}/')
    except RuntimeError:
        return []
    body = d.get(WATER_SVC) or {}
    if ((body.get('RESULT') or {}).get('CODE') or '') != 'INFO-000':
        return []
    rows = body.get('row') or []
    if not rows:
        return []
    newest = max(r.get('YMD') or '' for r in rows)
    facts = []
    for r in rows:
        if r.get('YMD') != newest or r.get('ROF_SE_NM') != '취수':
            continue            # 취수 only: see the note above
        en = WATER_SITES.get(r.get('BUSNP_NM') or '')
        if not en:
            continue
        try:
            v = float(r.get('MSRMT_VL'))
        except (TypeError, ValueError):
            continue
        if v <= 0:
            continue
        # ⚠️ These labels stay BARE PLACE NAMES on purpose. de1028e gave the
        # price and daynight extremes a leading "what this means" phrase
        # ("Dearest, a traditional market (Gangdong-gu)") because their cards
        # buried the point in the sort order — and doing the same here ("Most
        # drawn (Amsa)") is the obvious next step for consistency. It was
        # considered on 22 Aug 2026 and DECLINED: this vein's own rule is that
        # a centre is never called bigger or busier than another, and a leading
        # superlative is exactly that claim. The card is legible without it,
        # because the dateline names what the places are and the arrangement
        # carries the rest. Do not "finish" de1028e by extending it here.
        facts.append(fact(f'water_{r["BUSNP_NM"]}', 'water', en,
                          f'{grouped(v)} m³', f'{grouped(v)}m³',
                          pair='water_intake', pin=True,
                          label_ko=f'{r["BUSNP_NM"]} 정수센터'))
    if len(facts) < WATER_MIN_LINES:
        return []
    try:
        dt = datetime.strptime(newest, '%Y%m%d')
        # The dateline names the KIND of place as well as the day: the lines
        # are bare names (Amsa, Ttukdo) and nothing else on the card said they
        # were waterworks rather than districts or rivers.
        WATER_PERIOD['en'] = f'Purification centres, {dt.day} {MONTHS_EN[dt.month - 1]}'
        WATER_PERIOD['ko'] = f'정수센터, {dt.month}월 {dt.day}일'
    except ValueError:
        WATER_PERIOD['en'] = WATER_PERIOD['ko'] = newest
    return facts


# --- day and night ---------------------------------------------------------
# SPOP_DAILYSUM_JACHI_250: Seoul's OWN district-level daily aggregate of the
# 생활인구 series. The 250m-cell tables carry more (21 nationalities), but a
# citywide or district figure from those would be us summing cells, i.e. us
# computing. This table publishes the district totals already, so the card
# quotes rather than calculates — the distinction the account rests on.
#
# ⚠️ 생활인구 is KT-modelled, not counted, exactly like the crowd vein, so these
# facts are flagged estimated=True and the card carries that caveat.
DAYNIGHT_SVC = 'SPOP_DAILYSUM_JACHI_250'
DAYNIGHT_ROWS = 200
DAYNIGHT_MIN_LINES = 3
DAYNIGHT_PERIOD = {'en': None, 'ko': None}


def daynight_facts(api_key, state):
    """How far a district's daytime population runs above its night-time one."""
    try:
        d = http_get_json(f'http://openapi.seoul.go.kr:8088/{api_key}/json/'
                          f'{DAYNIGHT_SVC}/1/{DAYNIGHT_ROWS}/')
    except RuntimeError:
        return []
    body = d.get(DAYNIGHT_SVC) or {}
    if ((body.get('RESULT') or {}).get('CODE') or '') != 'INFO-000':
        return []
    rows = body.get('row') or []
    if not rows:
        return []
    newest = max(r.get('STDR_DE_ID') or '' for r in rows)
    day = [r for r in rows if r.get('STDR_DE_ID') == newest]

    def num(r, k):
        try:
            return float(r.get(k))
        except (TypeError, ValueError):
            return None

    # Alternate between the two published halves rather than mixing them: a
    # daytime figure and a night-time one for DIFFERENT districts in one column
    # reads as a ranking of places and is nothing of the sort.
    which = 'DAY_LVPOP_CO' if int(state.get('daynight_i', 0)) % 2 == 0 else 'NIGHT_LVPOP_CO'
    state['daynight_i'] = int(state.get('daynight_i', 0)) + 1
    when_en, when_ko = (('by day', '낮') if which.startswith('DAY')
                        else ('by night', '밤'))
    facts = []
    for r in day:
        gu_ko = r.get('SIGNGU_NM') or ''
        v = num(r, which)
        # ⚠️ '서울시' is a CITYWIDE row sitting among the 25 districts. Left in,
        # it would tower over every district line and read as one of them.
        if not gu_ko or gu_ko == '서울시' or v is None or v <= 0:
            continue
        gu_en = en_name(gu_ko, 'districts')
        if gu_en == gu_ko:
            continue
        facts.append(fact(f'daynight_{which[:3]}_{gu_ko}', 'daynight', gu_en,
                          grouped(v), grouped(v), pair='daynight_spread',
                          pin=True, label_ko=gu_ko, estimated=True,
                          num=v, unit='people'))
    if len(facts) < DAYNIGHT_MIN_LINES:
        return []
    facts.sort(key=lambda f: -f['num'])
    facts = facts[:3] + facts[-3:]      # the fullest and the emptiest
    # The ends lead with what they mean, as the price vein does: a reader who
    # cannot place Songpa-gu can still read "Fullest by day". facts[0] and
    # facts[-1] are the extremes of the WHOLE CITY, not of the card, so the
    # claim holds whichever companions the selector keeps.
    for f, (en, ko) in ((facts[0], (f'Fullest {when_en}', f'{when_ko} 가장 많은 곳')),
                        (facts[-1], (f'Emptiest {when_en}', f'{when_ko} 가장 적은 곳'))):
        f['label_en'] = f'{en} ({f["label_en"]})'
        f['label_ko'] = f'{ko} ({f["label_ko"]})'
    try:
        dt = datetime.strptime(newest, '%Y%m%d')
        # Just the date: the opener is required to say which half of the day
        # it is, and a dateline reading "by day, 17 August" under an opener
        # reading "Seoul by day" said it twice.
        DAYNIGHT_PERIOD['en'] = f'{dt.day} {MONTHS_EN[dt.month - 1]}'
        DAYNIGHT_PERIOD['ko'] = f'{dt.month}월 {dt.day}일'
    except ValueError:
        DAYNIGHT_PERIOD['en'] = DAYNIGHT_PERIOD['ko'] = newest
    return facts


# --- the youngest ----------------------------------------------------------
# statInfantNumInfo: Seoul's count of children at each age, one column per year,
# 2016 to 2025. The account already sets Seoul's fertility rate against the
# country's; this is the same story told in whole children rather than a rate,
# which is the more legible half.
#
# ⚠️ Row GBCODE '00' is a HEADER, not data: its YEAR01..YEAR10 hold the year
# labels ('2016'…'2025'), and reading it as a row would publish the year as a
# population. The year labels are taken FROM it, which is why it is read first
# rather than skipped.
INFANT_SVC = 'statInfantNumInfo'
INFANT_MIN_LINES = 3
# ⚠️⚠️ KEYED ON GBCODE, NEVER ON THE LABEL, because this feed's labels lie.
# Row '어린이집,계,수' ("count") holds "44.1%" and '어린이집,계,비율' ("ratio")
# holds 131,081: the two are swapped. Row 12 also misspells 유치원 as 유지원.
# Only these four rows were checked to be unambiguous whole-number counts, and
# anything not on this list is left alone rather than trusted.
# ⚠️ '0세' is NOT "aged 0", which an English reader takes to mean newborns. It
# is the first year of life, 0 to 11 months. The feed is on 만 나이
# (international age) — Korean counting age starts at 1 and has no 0세 at all,
# so a 0세 row can only be the international reckoning — which makes the honest
# English "under 1". Same reasoning turns '영아(0~2)세' into "under 3".
INFANT_SERIES = {'01': ('Children under 1', '0세 인구'),
                 '04': ('Children under 6', '영유아 인구'),
                 '05': ('Children under 3', '영아(0~2세) 인구'),
                 '06': ('Children aged 3 to 5', '유아(3~5세) 인구')}
INFANT_PERIOD = {'en': None, 'ko': None}


def infant_facts(api_key, state):
    """Seoul's children at one age, a decade apart."""
    try:
        d = http_get_json(f'http://openapi.seoul.go.kr:8088/{api_key}/json/'
                          f'{INFANT_SVC}/1/50/')
    except RuntimeError:
        return []
    body = d.get(INFANT_SVC) or {}
    if ((body.get('RESULT') or {}).get('CODE') or '') != 'INFO-000':
        return []
    rows = body.get('row') or []
    header = next((r for r in rows if r.get('GBCODE') == '00'), None)
    if not header:
        return []
    cols = [f'YEAR{i:02d}' for i in range(1, 11)]
    years = {c: (header.get(c) or '').strip() for c in cols}

    ages = [r for r in rows if r.get('GBCODE') in INFANT_SERIES]
    if not ages:
        return []
    i = int(state.get('infant_i', 0))
    state['infant_i'] = (i + 1) % len(ages)
    r = ages[i % len(ages)]
    en_age, ko_age = INFANT_SERIES[r['GBCODE']]

    facts = []
    for c in cols:
        yr = years.get(c)
        raw = (r.get(c) or '').replace(',', '').strip()
        if not yr or not raw.isdigit():
            continue
        v = int(raw)
        if v <= 0:
            continue
        facts.append(fact(f'infant_{r["GBCODE"]}_{yr}', 'infant', yr,
                          grouped(v), grouped(v), pair='infant_decade',
                          pin=True, label_ko=f'{yr}년', num=v, unit='people'))
    if len(facts) < INFANT_MIN_LINES:
        return []
    # The ends of the decade carry it; the middle years only pad the card.
    facts = [facts[0], facts[len(facts) // 2], facts[-1]]
    INFANT_PERIOD['en'] = en_age
    INFANT_PERIOD['ko'] = ko_age
    return facts


# --- library membership ----------------------------------------------------
# SeoulLibraryMemberInfo: registered members of 서울도서관 by year of birth,
# which the vein sums into the decade bands the feed itself declares
# (AGE_RANGE). Summing published rows is counting, which the account allows;
# nothing here is modelled or averaged.
#
# ⚠️ The sibling loans service (SeoulLibraryBookRentNumInfo) is deliberately NOT
# used, though it is live and carries real checkout counts. It publishes NO date
# or period field, so nobody can say what window a count of 31 covers, and an
# unlabelled count cannot go on a card. Revisit only if a period appears.
# ⚠️ This is 서울도서관, the city's flagship library, NOT Seoul's 215 public
# libraries. The opener must say so or the figures read as citywide.
LIBRARY_SVC = 'SeoulLibraryMemberInfo'
LIBRARY_MIN_LINES = 3
# Numerals, not words: the card is a column of age bands and "In their
# thirties" spends four words on what "30s" says, which is also the rule the
# crowd vein already follows for its age lines.
# Numerals from 20 up, but 'Teens' for the youngest: nobody says "in their
# 10s", and the column still reads as numerals with one short word at the head.
LIBRARY_BANDS = {'10': ('Teens', '10대'), '20': ('20s', '20대'),
                 '30': ('30s', '30대'), '40': ('40s', '40대'),
                 '50': ('50s', '50대'), '60': ('60s', '60대'),
                 '70': ('70s', '70대'), '80': ('80s', '80대')}

# The "1 in N" denominator: Seoul's registered population of each decade, from
# KOSIS DT_1B04005N (주민등록인구, five-year bands). Two published bands make one
# decade — 10-14세 plus 15-19세 is the teens — and summing published rows is
# counting, which the account allows. A bare count says how many teens hold a
# card; it does not say whether that is many, and the city's own teen population
# is the only honest thing to set it against.
#
# ⚠️⚠️ THE NUMERATOR IS NOT A SUBSET OF THIS DENOMINATOR, which is why the value
# reads "1 in 65" and the footnote says members need not live in Seoul. Read off
# 서울도서관's own 회원증 발급 page, 23 Aug 2026: 준회원 is open to ANY 대한민국 국민
# or registered foreign resident of Korea, with no Seoul connection required at
# all, and even 정회원 covers people who merely work or study in Seoul while
# living outside it. The API returns AGE_RANGE, BRDT and MBR_CNT only — no
# member class — so the two cannot be separated. The card may therefore state
# the RATIO and must never state a SHARE: "1 in 65" is honest, "1.5% of Seoul's
# teens hold a card" is a claim the data cannot carry.
# ⚠️ 주민등록인구 counts Korean nationals on the resident register; registered
# foreign residents are a separate count. Hence "registered population" in the
# footnote rather than the bare word.
LIBRARY_POP_TBL = 'DT_1B04005N'
LIBRARY_POP_ITM = 'T2'                  # 총인구수
LIBRARY_POP_SEOUL = '11'                # 행정구역: 서울특별시
# KOSIS names a five-year band by the age its NEXT band starts at: '15' is
# 10-14세 and '20' is 15-19세, so a decade is (d+5, d+10). Verified against the
# service's own meta (type=ITM, OBJ B) on 23 Aug 2026 rather than assumed — an
# off-by-one here would divide by the wrong generation and nothing in the output
# would look wrong.
LIBRARY_POP_BANDS = {d: (str(int(d) + 5), str(int(d) + 10)) for d in LIBRARY_BANDS}
LIBRARY_POP_MIN = 2     # "1 in 1" is not a ratio; "1 in 0" is a bug
# The population month, set by library_facts() only when a ratio actually
# reached a line. Read by compose() for the footnote: empty means bare counts
# went out and no scope note may claim otherwise.
LIBRARY_POP = {'en': '', 'ko': ''}


def library_pop(kosis_key):
    """({decade: Seoul's registered population}, 'July 2026', '2026년 7월').

    ({}, '', '') on any failure, which costs the ratio and nothing else: the
    vein falls back to bare member counts and the footnote note falls with it."""
    if not kosis_key:
        return {}, '', ''
    from urllib.parse import quote
    codes = sorted({c for pair in LIBRARY_POP_BANDS.values() for c in pair}, key=int)
    url = ('https://kosis.kr/openapi/Param/statisticsParameterData.do'
           f'?method=getList&apiKey={quote(kosis_key, safe="")}&format=json'
           f'&jsonVD=Y&orgId=101&tblId={LIBRARY_POP_TBL}&itmId={LIBRARY_POP_ITM}'
           f'&objL1={LIBRARY_POP_SEOUL}&objL2={"+".join(codes)}'
           '&prdSe=M&newEstPrdCnt=1')
    try:
        rows = http_get_json(url)
    except RuntimeError:
        return {}, '', ''
    # KOSIS reports its own errors as a DICT ({"err":"30", ...}) and its data as
    # a list, so the type is the error check: a dict here is an outage, a bad
    # key or a table that has moved, never a population.
    if not isinstance(rows, list) or not rows:
        return {}, '', ''
    by_code, prd = {}, ''
    for r in rows:
        if not isinstance(r, dict):
            continue
        try:
            by_code[str(r['C2'])] = int(r['DT'])
        except (KeyError, TypeError, ValueError):
            continue
        prd = prd or str(r.get('PRD_DE') or '')
    pop = {d: by_code[lo] + by_code[hi]
           for d, (lo, hi) in LIBRARY_POP_BANDS.items()
           if by_code.get(lo) and by_code.get(hi)}
    if not pop or not (len(prd) == 6 and prd.isdigit() and 1 <= int(prd[4:]) <= 12):
        return {}, '', ''       # a ratio whose vintage cannot be stated is not postable
    y, m = int(prd[:4]), int(prd[4:])
    return pop, f'{MONTHS_EN[m - 1]} {y}', f'{y}년 {m}월'


def library_facts(api_key, kosis_key=None):
    """Who holds a card at Seoul Library, by decade of life."""
    try:
        d = http_get_json(f'http://openapi.seoul.go.kr:8088/{api_key}/json/'
                          f'{LIBRARY_SVC}/1/200/')
    except RuntimeError:
        return []
    body = d.get(LIBRARY_SVC) or {}
    if ((body.get('RESULT') or {}).get('CODE') or '') != 'INFO-000':
        return []
    tally = {}
    for r in (body.get('row') or []):
        band = (r.get('AGE_RANGE') or '').strip()
        if band not in LIBRARY_BANDS:
            continue        # '0' and '90' are real but tiny; they would read as
                            # errors beside a band of 70,000
        try:
            tally[band] = tally.get(band, 0) + int(str(r.get('MBR_CNT')).strip())
        except (TypeError, ValueError):
            continue
    pop, pop_en, pop_ko = library_pop(kosis_key)
    facts, ratios = [], 0
    for band, total in sorted(tally.items(), key=lambda kv: -kv[1]):
        if total <= 0:
            continue
        en, ko = LIBRARY_BANDS[band]
        v_en = v_ko = grouped(total)
        # "1 in 65", not "1.5%": the ratio is the Harper's form and it is also
        # the honest one here (see the ⚠️ above LIBRARY_POP_TBL). It rides in a
        # TRAILING PARENTHETICAL on purpose — _sortkey() strips one before
        # reading the magnitude, so the card still orders these lines by member
        # count, and cross_vein_pairs still collides on `num`. A "10,921 · 1 in
        # 65" form would have made every value unparseable and silently dropped
        # the size sort on this card.
        n = round(pop[band] / total) if pop.get(band) else 0
        if n >= LIBRARY_POP_MIN:
            v_en, v_ko = f'{v_en} (1 in {grouped(n)})', f'{v_ko} ({grouped(n)}명 중 1명)'
            ratios += 1
        facts.append(fact(f'library_{band}', 'library', en, v_en, v_ko,
                          pair='library_ages', pin=True,
                          label_ko=ko, num=total, unit='people'))
    facts = facts if len(facts) >= LIBRARY_MIN_LINES else []
    # Only claim the ratio in the footnote if one actually went out on a line,
    # and only alongside lines that survived the minimum-count cut.
    LIBRARY_POP['en'] = pop_en if (facts and ratios) else ''
    LIBRARY_POP['ko'] = pop_ko if (facts and ratios) else ''
    return facts


# --- complaints ------------------------------------------------------------
# SmartUncomfStatMonth: how many times Seoul's residents reported something
# wrong, by month, back to 2012. A whole year against a whole year is the card.
#
# ⚠️⚠️ THE CURRENT YEAR'S ROW IS NOT SAFE TO READ AS MONTHS. In the row for the
# running year, the CURRENT month's slot holds the YEAR-TO-DATE TOTAL, not that
# month: on 21 Aug 2026 MON_07 read 435,518, which is exactly MON_TOTAL and
# exactly the sum of January to June. In 2025 and 2024 the same field is an
# ordinary month. Publishing MON_07 as July would have been six times too large.
# So the vein uses COMPLETE PRIOR YEARS ONLY, and the check is arithmetic rather
# than a date comparison: a year whose months do not sum to its own MON_TOTAL is
# not a finished year.
COMPLAINT_SVC = 'SmartUncomfStatMonth'
COMPLAINT_MIN_LINES = 3


def complaint_facts(api_key):
    """Reports to Seoul's fault-reporting service, by complete year."""
    try:
        d = http_get_json(f'http://openapi.seoul.go.kr:8088/{api_key}/json/'
                          f'{COMPLAINT_SVC}/1/30/')
    except RuntimeError:
        return []
    body = d.get(COMPLAINT_SVC) or {}
    if ((body.get('RESULT') or {}).get('CODE') or '') != 'INFO-000':
        return []
    years = []
    for r in (body.get('row') or []):
        yr = (r.get('YEAR') or '').strip()
        try:
            months = [float(r.get(f'MON_{i:02d}') or 0) for i in range(1, 13)]
            total = float(r.get('MON_TOTAL') or 0)
        except (TypeError, ValueError):
            continue
        if not yr.isdigit() or total <= 0:
            continue
        # The running year fails this: its months sum to about twice its total,
        # because the year-to-date figure is sitting in a month's slot.
        if abs(sum(months) - total) > 1:
            continue
        if min(months) <= 0:
            continue        # a year with an empty month is not complete either
        years.append((yr, total))
    if len(years) < COMPLAINT_MIN_LINES:
        return []
    years.sort(key=lambda t: t[0], reverse=True)
    facts = []
    for yr, total in years[:5]:
        facts.append(fact(f'complaint_{yr}', 'complaint', yr, grouped(total),
                          grouped(total), pair='complaint_years', pin=True,
                          label_ko=f'{yr}년'))
    return facts


BOOKS_AGG = HERE / 'books_agg.json'
# Set by books_facts() so compose() can footnote the loan window on the card (the
# publisher stays a clickable credit in the reply; the window and the library are
# keys to the figures and ride beside them). Both are read from the harvest
# rather than written here, because the library publishes the window and can
# change it.
#
# ⚠️ **This vein carries NO dateline, unlike the other scoped ones, and that is
# deliberate.** It briefly showed the harvest date at the top while the footnote
# said "last 60 days", and the two read as contradicting each other. Everywhere
# else on this bot the dateline slot means "the period these figures cover" — a
# month, a quarter, an hour. Here the data's period IS the rolling 60 days in the
# footnote, and the harvest date is only when it was read, so putting it in that
# slot said something the slot does not mean. The post's own timestamp answers
# "when", and BOOKS_MAX_AGE_DAYS keeps the cache from ever being old enough for
# the question to matter.
# 'records' is pre-formatted here rather than in compose(), where a LOCAL named
# `grouped` (the cross-pair layout flag) shadows the grouped() helper.
# 'loans' is the total across every subject, pre-formatted like 'records' and
# for the same reason. It is the denominator behind each line's "(1 in N)" and
# the footnote states it, because a ratio whose denominator is not on the card
# is a number the reader cannot check.
BOOKS_WINDOW = {'days': None, 'scope_en': None, 'scope_ko': None,
                'records': None, 'loans': None}
# A ratio needs a denominator at least twice its numerator; below that "1 in 1"
# is not a ratio at all. Applied to the WHOLE card rather than line by line (see
# books_facts): every line divides by the same total, so either the device works
# for all of them or the card drops it and shows bare counts.
BOOKS_RATIO_MIN = 2
# A rolling 60-day window read months ago is not a fact about now. The harvest is
# monthly, so anything older than this means the job has stopped and the vein
# should go quiet rather than keep posting a window it no longer knows the end of.
BOOKS_MAX_AGE_DAYS = 45


def books_facts():
    """What Seoul Library lent over the last 60 days, by KDC subject, from the
    cached scan (books_agg.json, refreshed weekly by
    seoul_index_books_harvest.py). Silent until that file exists — the same
    safe-by-default pattern as traffic_facts.

    ⚠️ ONE library, the city's flagship, not Seoul's 215 public libraries. The
    source changed on 22 August 2026 from data4library (citywide, calendar
    months, and never activated) to Seoul's own SeoulLibraryBookRentNumInfo. A
    reader who takes these for citywide figures has been misled, which is why the
    opener is required to name the library and the footnote carries the scope.

    ⚠️ **This counted the top ten books until 22 August 2026, and it was a dull
    card**: the most-borrowed book (32), the tenth (20) and the ten combined
    (245) are three numbers off one short list, none of which tells a reader
    anything they had not assumed. The same records carry a subject on every one,
    and summed by subject they say something — literature outruns 어학 eight to
    one. Titles are still never posted: they are Korean proper nouns that would
    strand mixed script on the English card.

    The subject NAMES come from the harvest rather than from a table here, so
    the words on the card and the words in the file cannot drift apart; a subject
    arriving without a name is dropped rather than guessed at. Labels are pinned
    because they are the library's own classification: the selector rewording
    'Applied sciences' to 'Tech' would mislabel a class whose most-borrowed books
    are medicine."""
    try:
        agg = json.loads(BOOKS_AGG.read_text())
        subs = [s for s in agg['subjects']
                if isinstance(s.get('loans'), int) and s['loans'] > 0
                and s.get('name_en') and s.get('name_ko')]
        days = int(agg['window_days'])
        records = int(agg['records'])
        # ⚠️ The subtraction lives INSIDE the try: a timezone-NAIVE stamp parses
        # fine and then raises TypeError here, so catching only the parse would
        # crash the whole run on a hand-edited cache file.
        age = (datetime.now(timezone.utc)
               - datetime.fromisoformat(agg['generated_at'])).days
    except (OSError, ValueError, KeyError, TypeError):
        return []
    # Four is what a card needs to be a spread rather than a pair of numbers.
    if len(subs) < 4:
        return []
    # ⚠️ With no dateline on this vein, the footnote's window is the ONLY thing
    # on the card saying what period these counts cover. A missing or nonsense
    # window would leave compose() with nothing to footnote and the card would
    # publish ten subject totals over no stated period at all.
    if days <= 0:
        return []
    # ⚠️ And the size of the set matters just as much, for the same reason: see
    # BOOKS_WINDOW['records'] and the footnote in compose(). Without it the card
    # would claim every loan the library made.
    if records <= 0:
        return []
    # See BOOKS_MAX_AGE_DAYS: a stale cache goes quiet rather than dating a
    # rolling window by a run nobody ran.
    if age > BOOKS_MAX_AGE_DAYS:
        return []
    BOOKS_WINDOW['days'] = days
    BOOKS_WINDOW['records'] = grouped(records)
    BOOKS_WINDOW['scope_en'] = agg.get('scope_en') or 'Seoul Library'
    BOOKS_WINDOW['scope_ko'] = agg.get('scope_ko') or '서울도서관'

    subs.sort(key=lambda s: (-s['loans'], s['code']))
    # Each subject's share of the checkouts counted, as "1 in 3" rather than
    # "32%" — the same form the membership vein uses, and the reason a card
    # showing FOUR of the ten subjects can still say what the other six weigh.
    #
    # ⚠️ "of the checkouts COUNTED", never "of all checkouts". The feed is a cut
    # at the 3,000 most-borrowed items and the truncation need not fall evenly
    # across subjects, so it bends the shares exactly as it bends the totals.
    # Both halves of this ratio come from that same cut, which is what makes it
    # honest: unlike the membership ratio, there is no second population
    # involved and so no caveat beyond the scope the footnote already carries.
    #
    # All-or-nothing, and deliberately so. One total divides every line, so a
    # per-line guard could leave the largest subject bare while the rest carried
    # a ratio — one card, two forms, for no reason a reader could see.
    total = sum(s['loans'] for s in subs)
    ratio = {}
    # Cleared before it is set, never merely overwritten: a run that computes no
    # ratio must not inherit the last run's denominator and footnote a card
    # whose values carry none.
    BOOKS_WINDOW['loans'] = None
    if total > 0 and all(round(total / s['loans']) >= BOOKS_RATIO_MIN
                         for s in subs):
        ratio = {s['code']: round(total / s['loans']) for s in subs}
        BOOKS_WINDOW['loans'] = grouped(total)

    def _vals(s):
        # (English, Korean). Trailing parenthetical, as on the membership lines:
        # _sortkey() strips one before reading a magnitude, so the card keeps its
        # size order. ⚠️ The two languages are built separately here because they
        # were once built once and used twice, which put "(1 in 3)" on the KOREAN
        # card — the counter is 건, a checkout, and Korean counts "1 of every 3"
        # head-final. Every value on this bot is two strings for exactly this.
        v = grouped(s['loans'])
        if s['code'] not in ratio:
            return v, v
        n = grouped(ratio[s['code']])
        return f'{v} (1 in {n})', f'{v} ({n}건 중 1건)'

    facts = [fact(f'book_{s["code"]}', 'books', s['name_en'],
                  *_vals(s), pin=True, label_ko=s['name_ko'])
             for s in subs]
    # Dead heat and widest gap, the same two detectors the sales vein carries:
    # what is worth posting about a ranking is where it is level and where it is
    # not, and neither is visible from the list order alone.
    best = None
    for i in range(len(subs)):
        for j in range(i + 1, len(subs)):
            a, b = subs[i]['loans'], subs[j]['loans']
            gap = abs(a - b) / max(a, b)
            if gap <= 0.02 and (best is None or gap < best[0]):
                best = (gap, subs[i], subs[j])
    if best:
        for s in (best[1], best[2]):
            facts.append(fact(f'bookheat_{s["code"]}', 'books', s['name_en'],
                              *_vals(s),
                              pair='book_heat', pin=True, label_ko=s['name_ko']))
    for s in (subs[-1], subs[0]):
        facts.append(fact(f'bookgap_{s["code"]}', 'books', s['name_en'],
                          *_vals(s),
                          pair='book_gap', pin=True, label_ko=s['name_ko']))
    return facts


# Industry categories worth surfacing (Korean name -> English gloss).
SALES_LABELS = {
    '커피-음료': ('coffee shops', '커피-음료'),
    '호프-간이주점': ('pubs and beer halls', '호프-간이주점'),
    '노래방': ('karaoke rooms', '노래방'),
    '치킨전문점': ('fried-chicken shops', '치킨전문점'),
    '서적': ('bookshops', '서적'),
    'PC방': ('internet cafés', 'PC방'),
    '당구장': ('billiard halls', '당구장'),
    '여관': ('motels', '여관'),
    '한식음식점': ('Korean restaurants', '한식음식점'),
    '제과점': ('bakeries', '제과점'),
    '분식전문점': ('snack bars', '분식전문점'),
    '화장품': ('cosmetics shops', '화장품'),
    '편의점': ('convenience stores', '편의점'),
    '애완동물': ('pet shops', '애완동물'),
    '예술학원': ('art academies', '예술학원'),
}


def sales_facts():
    """Latest-quarter industry sales from the cached full scan, with the sharp
    near-equal ('dead heat') pairs pre-detected."""
    if not SALES_AGG.exists():
        return []
    agg = json.loads(SALES_AGG.read_text())
    q = agg.get('latest_quarter')
    inds = agg.get('by_quarter', {}).get(q, {})
    if not inds:
        return []
    SALES_Q['en'] = f'{q[:4]} Q{q[4]}'          # 20261 -> 2026 Q1
    SALES_Q['ko'] = f'{q[:4]}년 {q[4]}분기'      # -> 2026년 1분기
    facts = []
    # Single-industry sales lines for the curated categories. Quarter context
    # lives on the source line (see compose), not repeated on every row.
    for ko, (en, ko_gloss) in SALES_LABELS.items():
        cell = inds.get(ko)
        if not cell:
            continue
        facts.append(fact(f'sales_{ko}', 'spending',
                          f'{en.capitalize()}',
                          won_en(cell['amt']), won_ko(cell['amt']),
                          num=cell['amt'], unit='won'))
    # Dead-heat detector: any two curated categories within 2% of each other.
    curated = [(ko, inds[ko]['amt']) for ko in SALES_LABELS if ko in inds]
    best = None
    for i in range(len(curated)):
        for j in range(i + 1, len(curated)):
            a, b = curated[i][1], curated[j][1]
            if max(a, b) == 0:
                continue
            gap = abs(a - b) / max(a, b)
            if gap <= 0.02 and (best is None or gap < best[0]):
                best = (gap, curated[i][0], curated[j][0])
    if best:
        _, koa, kob = best
        for ko in (koa, kob):
            en = SALES_LABELS[ko][0]
            facts.append(fact(f'heat_{ko}', 'spending',
                              f'{en.capitalize()}',
                              won_en(inds[ko]['amt']), won_ko(inds[ko]['amt']),
                              pair='dead_heat'))
    # Average-bill (per-transaction spend) facts + the widest gap. A distinct
    # 'avgbill' category so rotation and openers treat it apart from totals.
    avg_list = []
    for ko, (en, ko_gloss) in SALES_LABELS.items():
        cell = inds.get(ko)
        if not cell or not cell.get('co'):
            continue
        avg = cell['amt'] / cell['co']
        avg_list.append((ko, en, avg))
        facts.append(fact(f'avg_{ko}', 'avgbill', en.capitalize(),
                          won_en(avg), won_ko(avg), num=avg, unit='won'))
    if len(avg_list) >= 2:
        hi = max(avg_list, key=lambda t: t[2])
        lo = min(avg_list, key=lambda t: t[2])
        for ko, en, avg in (lo, hi):
            facts.append(fact(f'avggap_{ko}', 'avgbill', en.capitalize(),
                              won_en(avg), won_ko(avg), pair='avg_gap'))
    return facts


# --- apartment market (MOLIT 실거래가) --------------------------------------
# 국토교통부's real-transaction filings, via data.go.kr (RTMSDataSvcAptTrade /
# RTMSDataSvcAptRent, one 활용신청 each, 자동승인). A different publisher from
# the city, so compose() credits rt.molit.go.kr — the ministry's own 실거래가
# portal — on its own source line.
#
# Editorial rule (the one that stopped the KTO vein): every line must be a
# PUBLISHED figure or a plain count of published rows. The highest sale is an
# actual filed row; "sales filed" is counting; no medians, no averages.
#
# The gateway rejects curl's default User-Agent (bare "Forbidden"), so
# _molit_items sends a browser one. Amounts arrive in 만원 with comma grouping.
# Cancelled sales stay in the feed with cdealType set — they are filtered out.
#
# DEAL_YMD is the CONTRACT month, and filings are due within 30 days of the
# contract, so the newest complete month is two calendar months back. That
# month's figures are frozen, which is what makes MOLIT_AGG safe to cache:
# one harvest (~50 calls) serves the whole month of posts.

MOLIT_AGG = HERE / 'molit_agg.json'
MOLIT_BASE = 'http://apis.data.go.kr/1613000'
MOLIT_UA = ('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
            'AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15')

# Seoul's 25 자치구 by 법정동 code prefix (LAWD_CD). Verified 22 Jul 2026
# against the API itself: each code's May-2026 rows majority-report the same
# district in estateAgentSggNm.
SEOUL_LAWD = {
    '11110': '종로구', '11140': '중구', '11170': '용산구', '11200': '성동구',
    '11215': '광진구', '11230': '동대문구', '11260': '중랑구', '11290': '성북구',
    '11305': '강북구', '11320': '도봉구', '11350': '노원구', '11380': '은평구',
    '11410': '서대문구', '11440': '마포구', '11470': '양천구', '11500': '강서구',
    '11530': '구로구', '11545': '금천구', '11560': '영등포구', '11590': '동작구',
    '11620': '관악구', '11650': '서초구', '11680': '강남구', '11710': '송파구',
    '11740': '강동구',
}

MONTHS_EN = ('January', 'February', 'March', 'April', 'May', 'June', 'July',
             'August', 'September', 'October', 'November', 'December')

# Set by molit_facts() so compose() can put the filing month on the card
# instead of repeating it on every row (same device as SALES_Q).
MOLIT_M = {'en': None, 'ko': None}


def _molit_month():
    """Newest complete contract month, as 'YYYYMM'. Filings are due within 30
    days of the contract, so two months back is the newest month that can no
    longer grow."""
    first = datetime.now(SEOUL_TZ).date().replace(day=1)
    for _ in range(2):
        first = (first - timedelta(days=1)).replace(day=1)
    return f'{first.year}{first.month:02d}'


def _molit_items(service, lawd, ym, key):
    """All <item> rows for one district-month, paginated on totalCount."""
    rows, page = [], 1
    while True:
        url = (f'{MOLIT_BASE}/{service}/get{service}?serviceKey={key}'
               f'&LAWD_CD={lawd}&DEAL_YMD={ym}&numOfRows=1000&pageNo={page}')
        r = subprocess.run(['curl', '-s', '--max-time', '60', '-A', MOLIT_UA, url],
                           capture_output=True, text=True)
        try:
            root = ET.fromstring(r.stdout)
        except ET.ParseError:
            raise RuntimeError(f'MOLIT {service} {lawd}/{ym}: not XML: '
                               f'{r.stdout[:80]!r}')
        if root.findtext('.//resultCode') != '000':
            raise RuntimeError(f'MOLIT {service} {lawd}/{ym}: '
                               f'{root.findtext(".//resultMsg")!r}')
        rows += list(root.iter('item'))
        total = int(root.findtext('.//totalCount') or 0)
        if len(rows) >= total:
            return rows
        page += 1


def _manwon(s):
    """'197,000' (만원) -> 1_970_000_000 (원), or None."""
    s = (s or '').replace(',', '').strip()
    return int(s) * 10_000 if s.isdigit() else None


def _molit_harvest(key, ym):
    """One month's citywide aggregates, computed from every filed row. All 25
    districts or nothing: a partial harvest would sell a partial city as
    'citywide', so any district failing fails the month."""
    trade_n, by_gu = 0, {}
    top = low = None    # [amount, 구, 아파트명]
    jeonse_n = wolse_n = 0
    top_dep = None      # [amount, 구]
    for lawd, gu in SEOUL_LAWD.items():
        for it in _molit_items('RTMSDataSvcAptTrade', lawd, ym, key):
            if (it.findtext('cdealType') or '').strip():
                continue    # cancelled sale, retracted but still in the feed
            amt = _manwon(it.findtext('dealAmount'))
            if amt is None:
                continue
            trade_n += 1
            by_gu[gu] = by_gu.get(gu, 0) + 1
            apt = (it.findtext('aptNm') or '').strip()
            if top is None or amt > top[0]:
                top = [amt, gu, apt]
            if low is None or amt < low[0]:
                low = [amt, gu, apt]
        for it in _molit_items('RTMSDataSvcAptRent', lawd, ym, key):
            dep = _manwon(it.findtext('deposit'))
            if dep is None:
                continue
            if _manwon(it.findtext('monthlyRent')):
                wolse_n += 1
            else:
                jeonse_n += 1
                if top_dep is None or dep > top_dep[0]:
                    top_dep = [dep, gu]
    # A real month has thousands of each; zeros mean the feed (or a field
    # name) changed under us, and caching them would freeze the mistake.
    if not trade_n or not (jeonse_n + wolse_n):
        raise RuntimeError(f'MOLIT harvest for {ym} looks empty '
                           f'(trade={trade_n}, leases={jeonse_n + wolse_n})')
    return {'month': ym, 'trade_n': trade_n, 'by_gu': by_gu, 'top': top,
            'low': low, 'jeonse_n': jeonse_n, 'wolse_n': wolse_n,
            'top_deposit': top_dep}


def molit_facts(molit_key):
    """Apartment-market lines from the newest complete month's filings."""
    if not molit_key:
        return []
    ym = _molit_month()
    agg = None
    if MOLIT_AGG.exists():
        try:
            cached = json.loads(MOLIT_AGG.read_text())
            if cached.get('month') == ym:
                agg = cached
        except (OSError, ValueError):
            pass
    if agg is None:
        try:
            agg = _molit_harvest(molit_key, ym)
        except (RuntimeError, OSError, ValueError) as e:
            print(f'Warning: MOLIT harvest failed ({e}); no property lines.')
            return []
        write_json_atomic(MOLIT_AGG, agg, ensure_ascii=False, indent=1)
    y, m = int(ym[:4]), int(ym[4:])
    MOLIT_M['en'], MOLIT_M['ko'] = f'{MONTHS_EN[m - 1]} {y}', f'{y}년 {m}월'
    facts = []
    if agg.get('top') and agg.get('low') and agg['top'] != agg['low']:
        for fid, (amt, gu, _apt), en_word, ko_word in (
                ('apt_top_sale', agg['top'], 'Most', '가장 비싸게 팔린'),
                ('apt_low_sale', agg['low'], 'Least', '가장 싸게 팔린')):
            facts.append(fact(fid, 'property',
                              f'{en_word} paid for an apartment '
                              f'({en_name(gu, "districts")})',
                              won_en(amt), won_ko(amt), pair='apt_price_gap',
                              pin=True, label_ko=f'{ko_word} 아파트, {gu}',
                              num=amt, unit='won'))
    if agg.get('trade_n'):
        n = agg['trade_n']
        facts.append(fact('apt_sales_n', 'property',
                          'Apartment sales filed citywide',
                          grouped(n), grouped(n)))
    by_gu = agg.get('by_gu') or {}
    if len(by_gu) >= 2:
        busy = max(by_gu, key=by_gu.get)
        quiet = min(by_gu, key=by_gu.get)
        for fid, gu in (('apt_busy_gu', busy), ('apt_quiet_gu', quiet)):
            n = by_gu[gu]
            facts.append(fact(fid, 'property',
                              f'Sales filed in {en_name(gu, "districts")}',
                              grouped(n), grouped(n), pair='apt_count_gap',
                              pin=True, label_ko=f'{gu} 매매 신고'))
    if agg.get('top_deposit'):
        amt, gu = agg['top_deposit']
        facts.append(fact('apt_top_jeonse', 'property',
                          f'Largest jeonse deposit ({en_name(gu, "districts")})',
                          won_en(amt), won_ko(amt), pin=True,
                          label_ko=f'최고 전세 보증금, {gu}', num=amt, unit='won'))
    if agg.get('jeonse_n') and agg.get('wolse_n'):
        for fid, label, n in (
                ('lease_jeonse_n', 'Jeonse leases filed', agg['jeonse_n']),
                ('lease_wolse_n', 'Monthly-rent leases filed', agg['wolse_n'])):
            facts.append(fact(fid, 'property', label, grouped(n), grouped(n),
                              pair='lease_split'))
    return facts


# --- weather (KMA ASOS, the official Seoul station) ------------------------
# 기상청's daily surface observations for station 108 — the Seoul reference
# station, observing since 1904 — via data.go.kr (자동승인, approved 22 Jul
# 2026). The service publishes through YESTERDAY only (D-1), so the freshest
# line is yesterday's, and the monthly lines use the last full month.
#
# Same editorial rule as the property vein: a line is a published daily
# reading (the hottest day IS a row) or a count of rows against a criterion
# stated in the label. No monthly means or totals — those are computations.
#
# The half-century pair is the vein's reason to exist: the same calendar
# month, WX_YEARS_BACK years apart, each side a published reading from the
# same station. The archive answered June 1976 in full when probed; a month
# with no rows (there are wartime gaps) simply drops its side of the pair.

WX_BASE = ('http://apis.data.go.kr/1360000/AsosDalyInfoService/getWthrDataList'
           '?serviceKey={key}&dataType=JSON&dataCd=ASOS&dateCd=DAY&stnIds=108'
           '&numOfRows={rows}&pageNo=1&startDt={start}&endDt={end}')
WX_YEARS_BACK = 50


def _wx_rows(key, start, end, rows=31):
    """Daily rows for one date span, [] on any failure (the vein just thins).

    A month fits in the 31-row default; the summer-to-date span crosses
    months, so its caller asks for enough rows to cover the whole window in
    one page (the API returns the full range when numOfRows spans it)."""
    url = WX_BASE.format(key=key, start=start, end=end, rows=rows)
    r = subprocess.run(['curl', '-s', '--max-time', '30', '-A', MOLIT_UA, url],
                       capture_output=True, text=True)
    try:
        body = json.loads(r.stdout)['response']['body']
    except (ValueError, KeyError, TypeError):
        return []
    items = body.get('items')
    return items.get('item') or [] if isinstance(items, dict) else []


def _wx_num(row, field):
    """A reading as float, or None — old rows leave some fields blank."""
    try:
        return float((row.get(field) or '').strip())
    except ValueError:
        return None


def _wx_extremes(rows):
    """The published extreme days of a month, plus criterion counts."""
    temps = [r for r in rows if _wx_num(r, 'maxTa') is not None]
    wets = [r for r in rows if (_wx_num(r, 'sumRn') or 0) > 0]
    return {
        'hot': max(temps, key=lambda r: _wx_num(r, 'maxTa'), default=None),
        'wet': max(wets, key=lambda r: _wx_num(r, 'sumRn'), default=None),
        'swelter': sum(1 for r in temps if _wx_num(r, 'maxTa') >= 33),
        'tropical': sum(1 for r in rows
                        if (_wx_num(r, 'minTa') or -99) >= 25),
        'freeze': sum(1 for r in temps if _wx_num(r, 'maxTa') < 0),
    }


def kma_facts(key):
    """Weather lines: yesterday's readings, and the last full month set
    against the same month fifty years earlier."""
    if not key:
        return []
    today = datetime.now(SEOUL_TZ).date()
    facts = []

    yday = today - timedelta(days=1)
    rows = _wx_rows(key, f'{yday:%Y%m%d}', f'{yday:%Y%m%d}')
    if rows:
        r = rows[0]
        hi, lo, rn = (_wx_num(r, 'maxTa'), _wx_num(r, 'minTa'),
                      _wx_num(r, 'sumRn'))
        if hi is not None and lo is not None:
            facts.append(fact('wx_yday_hi', 'weather', "Seoul's high yesterday",
                              to_f(hi), f'{hi:.1f}°C', pair='wx_yday',
                              pin=True, label_ko='어제 서울 최고기온'))
            facts.append(fact('wx_yday_lo', 'weather', "Seoul's low yesterday",
                              to_f(lo), f'{lo:.1f}°C', pair='wx_yday',
                              pin=True, label_ko='어제 서울 최저기온'))
        if rn:
            facts.append(fact('wx_yday_rain', 'weather',
                              'Rain on Seoul yesterday',
                              f'{rn:.1f}mm', f'{rn:.1f}mm', pair='wx_yday',
                              pin=True, label_ko='어제 서울에 내린 비'))

    # Last FULL month against the same month fifty years back: both sides
    # are complete, and neither can still grow.
    m_end = today.replace(day=1) - timedelta(days=1)
    m_start = m_end.replace(day=1)
    mon, mon_en = m_start.month, MONTHS_EN[m_start.month - 1]
    then_y = m_start.year - WX_YEARS_BACK
    then_end = (date(then_y, mon, 28) + timedelta(days=4)).replace(day=1) \
        - timedelta(days=1)
    now = _wx_extremes(_wx_rows(key, f'{m_start:%Y%m%d}', f'{m_end:%Y%m%d}'))
    then = _wx_extremes(_wx_rows(key, f'{then_y}{mon:02d}01',
                                 f'{then_end:%Y%m%d}'))

    for side, ex, y in (('now', now, m_start.year), ('then', then, then_y)):
        if ex['hot']:
            v = _wx_num(ex['hot'], 'maxTa')
            facts.append(fact(f'wx_hot_{side}', 'weather',
                              f'Hottest day, {mon_en} {y}',
                              to_f(v), f'{v:.1f}°C', pair='wx_heat_then',
                              pin=True, label_ko=f'가장 더웠던 날, {y}년 {mon}월'))
        if ex['wet']:
            v = _wx_num(ex['wet'], 'sumRn')
            facts.append(fact(f'wx_wet_{side}', 'weather',
                              f'Wettest day, {mon_en} {y}',
                              f'{v:.1f}mm', f'{v:.1f}mm', pair='wx_rain_then',
                              pin=True,
                              label_ko=f'비가 가장 많이 온 날, {y}년 {mon}월'))
    # Criterion counts, offered only in the season where they bite: a pair
    # of zeros is not a fact.
    for fid, kind, en_label, ko_label in (
            ('wx_swelter', 'swelter', 'Days of 33°C (91°F) or more',
             '최고기온 33°C 이상인 날'),
            ('wx_tropical', 'tropical', 'Nights never below 25°C (77°F)',
             '최저기온 25°C 이상인 날'),
            ('wx_freeze', 'freeze', 'Days never above freezing',
             '종일 영하였던 날')):
        if now[kind] or then[kind]:
            for side, ex, y in (('now', now, m_start.year),
                                ('then', then, then_y)):
                facts.append(fact(f'{fid}_{side}', 'weather',
                                  f'{en_label}, {mon_en} {y}',
                                  grouped(ex[kind]), grouped(ex[kind]),
                                  pair=f'{fid}_then', pin=True,
                                  label_ko=f'{ko_label}, {y}년 {mon}월'))

    # Season-to-date: the vein's one present-tense frame. Everything above is
    # settled (yesterday, or a closed month); this counts from 1 June through
    # YESTERDAY, a window still growing, against the SAME span fifty years
    # back. The running swelter tally (days of 33°C or more) is the point of
    # it, and it rides with the season's other extremes so far — hottest day,
    # wettest day, tropical nights — exactly as the monthly frame bundles its
    # own. Still published rows and counts of rows, so the vein's rule holds.
    # Offered only in summer, and only once swelter days have landed: the
    # tally is the reason the frame exists, so a 0/0 swelter count means no
    # frame (a pair of zeros is not a fact).
    if today.month in (6, 7, 8, 9):
        s_start = date(today.year, 6, 1)
        then_start = date(today.year - WX_YEARS_BACK, 6, 1)
        then_yday = date(yday.year - WX_YEARS_BACK, yday.month, yday.day)
        s_now = _wx_extremes(_wx_rows(key, f'{s_start:%Y%m%d}',
                                      f'{yday:%Y%m%d}', rows=200))
        s_then = _wx_extremes(_wx_rows(key, f'{then_start:%Y%m%d}',
                                       f'{then_yday:%Y%m%d}', rows=200))
        if s_now['swelter'] or s_then['swelter']:
            span_en = f'1 June–{yday.day} {MONTHS_EN[yday.month - 1]}'
            span_ko = f'6월 1일–{yday.month}월 {yday.day}일'
            sides = (('now', s_now, yday.year),
                     ('then', s_then, yday.year - WX_YEARS_BACK))
            for side, ex, y in sides:
                if ex['hot']:
                    v = _wx_num(ex['hot'], 'maxTa')
                    facts.append(fact(f'wx_s_hot_{side}', 'weather',
                                      f'Hottest day, {span_en} {y}',
                                      to_f(v), f'{v:.1f}°C', pair='wx_s_hot',
                                      pin=True,
                                      label_ko=f'가장 더웠던 날, {y}년 {span_ko}'))
                if ex['wet']:
                    v = _wx_num(ex['wet'], 'sumRn')
                    facts.append(fact(f'wx_s_wet_{side}', 'weather',
                                      f'Wettest day, {span_en} {y}',
                                      f'{v:.1f}mm', f'{v:.1f}mm', pair='wx_s_wet',
                                      pin=True,
                                      label_ko=f'비가 가장 많이 온 날, {y}년 {span_ko}'))
            for fid, kind, en_label, ko_label in (
                    ('wx_s_swelter', 'swelter', 'Days of 33°C (91°F) or more',
                     '최고기온 33°C 이상인 날'),
                    ('wx_s_tropical', 'tropical', 'Nights never below 25°C (77°F)',
                     '최저기온 25°C 이상인 날')):
                if s_now[kind] or s_then[kind]:
                    for side, ex, y in sides:
                        facts.append(fact(f'{fid}_{side}', 'weather',
                                          f'{en_label}, {span_en} {y}',
                                          grouped(ex[kind]), grouped(ex[kind]),
                                          pair=fid, pin=True,
                                          label_ko=f'{ko_label}, {y}년 {span_ko}'))
    return facts


# --- Gimpo airport (Korea Airports Corporation) ----------------------------
# 한국공항공사's monthly transport statistics via data.go.kr (자동승인,
# approved 22 Jul 2026). One row per airport per month; the bot reads the
# 김포 row — Seoul's airport — and nothing else. A month publishes from the
# 5th business day of the next; records run from 2006, so the then-and-now
# device here spans twenty years, not weather's fifty. The 국내선/국제선
# split comes from the routeBe filter — each side is a published row, never
# a subtraction.

KAC_BASE = ('http://apis.data.go.kr/B551178/airport-transport-stats/info'
            '?serviceKey={key}&startDePd={ym}&endDePd={ym}{extra}')
KAC_YEARS_BACK = 20


def _kac_month(key, y, m, route=None):
    """김포's row for one month as {'pax': int, 'flights': int}, or None."""
    extra = f'&routeBe={route}' if route is not None else ''
    url = KAC_BASE.format(key=key, ym=f'{y}{m:02d}', extra=extra)
    r = subprocess.run(['curl', '-s', '--max-time', '30', '-A', MOLIT_UA, url],
                       capture_output=True, text=True)
    try:
        root = ET.fromstring(r.stdout)
    except ET.ParseError:
        return None
    for it in root.iter('item'):
        if (it.findtext('Airport') or '').strip() == '김포':
            try:
                return {'pax': int(float(it.findtext('subpassenger'))),
                        'flights': int(float(it.findtext('Subflgt')))}
            except (TypeError, ValueError):
                return None
    return None


def kac_facts(key):
    """Gimpo's newest published month, its twenty-years-ago shadow, and the
    domestic/international split."""
    if not key:
        return []
    today = datetime.now(SEOUL_TZ).date()
    # Newest published month: last month from the ~5th business day, the
    # month before until then.
    m_first = today.replace(day=1)
    now = None
    for _ in range(2):
        m_first = (m_first - timedelta(days=1)).replace(day=1)
        now = _kac_month(key, m_first.year, m_first.month)
        if now:
            break
    if not now:
        return []
    y, m = m_first.year, m_first.month
    mon_en = MONTHS_EN[m - 1]
    facts = [fact('kac_pax_now', 'airport',
                  f'Passengers through Gimpo, {mon_en} {y}',
                  grouped(now['pax']), grouped(now['pax']), pair='gimpo_then',
                  pin=True, label_ko=f'김포공항 이용객, {y}년 {m}월',
                  num=now['pax'], unit='people'),
             fact('kac_flights_now', 'airport',
                  f'Flights in and out, {mon_en} {y}',
                  grouped(now['flights']), grouped(now['flights']),
                  pin=True, label_ko=f'운항 편수, {y}년 {m}월')]
    then = _kac_month(key, y - KAC_YEARS_BACK, m)
    if then:
        facts.append(fact('kac_pax_then', 'airport',
                          f'Passengers through Gimpo, {mon_en} {y - KAC_YEARS_BACK}',
                          grouped(then['pax']), grouped(then['pax']),
                          pair='gimpo_then', pin=True,
                          label_ko=f'김포공항 이용객, {y - KAC_YEARS_BACK}년 {m}월'))
    dom = _kac_month(key, y, m, route=0)
    intl = _kac_month(key, y, m, route=1)
    if dom and intl:
        for fid, row, en, ko in (
                ('kac_dom', dom, 'Domestic passengers', '국내선 이용객'),
                ('kac_intl', intl, 'International passengers', '국제선 이용객')):
            facts.append(fact(fid, 'airport', f'{en}, {mon_en} {y}',
                              grouped(row['pax']), grouped(row['pax']),
                              pair='gimpo_split', pin=True,
                              label_ko=f'{ko}, {y}년 {m}월',
                              num=row['pax'], unit='people'))
    return facts


# --- a year in the clinics (HIRA) ------------------------------------------
# 건강보험심사평가원's per-disease statistics via data.go.kr (자동승인,
# approved 22 Jul 2026): patients per 3-character KCD code per 시도, from
# adjudicated health-insurance claims. Two provisos, both on the card
# footnote: the region is where the INSTITUTION is, not where the patient
# lives, and the counts are insurance claims only. The year is the newest
# COMPLETE published care year (today - 2): the API answers for later years,
# but their semantics are unverified and their figures may still be growing.
#
# The conditions are curated for recognisability AND for surviving a
# playful adjacency: androgenic alopecia was cut because insurance treats
# so little hair loss that the true figure (9,413 in 2024) reads as wrong
# to anyone who knows Seoul — the avgbill lesson again.

HIRA_BASE = ('http://apis.data.go.kr/B551182/diseaseInfoService1/'
             'getDissByAreaStats1?serviceKey={key}&year={year}&sickType=1'
             '&medTp=1&sickCd={code}&numOfRows=30&pageNo=1')

HEALTH_CONDS = [   # (3-char KCD, EN gloss, KO gloss)
    ('J00', 'The common cold', '감기'),
    ('J30', 'Allergic rhinitis', '알레르기 비염'),
    ('I10', 'High blood pressure', '고혈압'),
    ('K02', 'Tooth decay', '충치'),
    ('E11', 'Type 2 diabetes', '2형 당뇨병'),
    ('H52', 'Refractive eye problems', '굴절 이상(근시·원시)'),
    ('G47', 'Sleep disorders', '수면장애'),
    ('M10', 'Gout', '통풍'),
]

# Set by hira_facts() so compose() can put the claims year on the card.
HEALTH_Y = {'y': None}


def hira_facts(key):
    """Patient counts at Seoul institutions, one condition per line."""
    if not key:
        return []
    year = datetime.now(SEOUL_TZ).year - 2
    got = []
    for code, en, ko in HEALTH_CONDS:
        url = HIRA_BASE.format(key=key, year=year, code=code)
        r = subprocess.run(['curl', '-s', '--max-time', '30', '-A', MOLIT_UA,
                            url], capture_output=True, text=True)
        try:
            root = ET.fromstring(r.stdout)
        except ET.ParseError:
            continue
        for it in root.iter('item'):
            if (it.findtext('lcName') or '').strip() == '서울':
                try:
                    got.append((code, en, ko, int(it.findtext('ptntCnt'))))
                except (TypeError, ValueError):
                    pass
                break
    if len(got) < 3:
        return []
    HEALTH_Y['y'] = year
    facts = [fact(f'sick_{code}', 'health', en, grouped(n), grouped(n),
                  label_ko=ko)
             for code, en, ko, n in got]
    # Same detectors as the sales vein: any two conditions within 2% are a
    # dead heat, and the widest spread is a gap pair.
    best = None
    for i in range(len(got)):
        for j in range(i + 1, len(got)):
            a, b = got[i][3], got[j][3]
            gap = abs(a - b) / max(a, b)
            if gap <= 0.02 and (best is None or gap < best[0]):
                best = (gap, got[i], got[j])
    if best:
        for code, en, ko, n in (best[1], best[2]):
            facts.append(fact(f'sickheat_{code}', 'health', en,
                              grouped(n), grouped(n), pair='sick_heat',
                              label_ko=ko))
    hi = max(got, key=lambda t: t[3])
    lo = min(got, key=lambda t: t[3])
    if hi[3] and lo[3] and hi[3] / max(lo[3], 1) >= 3:
        for code, en, ko, n in (hi, lo):
            facts.append(fact(f'sickgap_{code}', 'health', en,
                              grouped(n), grouped(n), pair='sick_gap',
                              label_ko=ko))
    return facts


# --- museums and galleries (문화기반시설총람) --------------------------------
# 문화체육관광부's annual culture-facility survey, served by 한국문화정보원
# via data.go.kr (자동승인, approved 22 Jul 2026). Per-facility rows carry
# the year's visitor total (fyerVwngNope), so the busiest houses are
# published rows and the city counts are counting. The survey year lags:
# the 2024 edition carries 2023 figures, which the card footnote says.

CULTURE_BASE = ('http://apis.data.go.kr/B553457/rgnCltrFcltExmnv1/{op}'
                '?serviceKey={key}&pblshYr={yr}&numOfRows=2000&pageNo=1'
                '&resultType=json')

# Official English names for the houses likely to top the tables. An
# unmapped name falls back to Korean with a warning, same as en_name().
CULTURE_EN = {
    '국립중앙박물관': 'the National Museum of Korea',
    '전쟁기념관': 'the War Memorial of Korea',
    '국립민속박물관': 'the National Folk Museum',
    '국립고궁박물관': 'the National Palace Museum',
    '석조전 대한제국역사관': 'Seokjojeon Hall',
    '서울역사박물관': 'the Seoul Museum of History',
    '서울시립미술관': 'the Seoul Museum of Art',
    '국립현대미술관(서울관)': 'MMCA Seoul',
    '리움미술관': 'the Leeum Museum of Art',
    '한가람미술관': 'the Hangaram Art Museum',
}

# Set by culture_facts() so compose() can put the survey year on the card.
CULTURE_Y = {'y': None}


def _culture_rows(key, op, yr):
    """One facility table's Seoul rows (시군구 codes 11xxx)."""
    url = CULTURE_BASE.format(op=op, key=key, yr=yr)
    r = subprocess.run(['curl', '-s', '--max-time', '60', '-A', MOLIT_UA, url],
                       capture_output=True, text=True)
    try:
        rows = json.loads(r.stdout)['response']['body']['data']
    except (ValueError, KeyError, TypeError):
        return []
    return [x for x in rows if str(x.get('sggCd', '')).startswith('11')]


def _culture_top(rows, name_field):
    """The most-visited facility as (korean_name, visitors), or None."""
    best = max(rows, key=lambda x: x.get('fyerVwngNope') or 0, default=None)
    if not best or not best.get('fyerVwngNope'):
        return None
    return (best.get(name_field) or '').strip(), int(best['fyerVwngNope'])


def culture_facts(key):
    """Seoul's museums and galleries: the counts, and the busiest houses."""
    if not key:
        return []
    this_year = datetime.now(SEOUL_TZ).year
    museums = []
    for yr in (this_year, this_year - 1, this_year - 2):
        museums = _culture_rows(key, 'clifMsmv1', yr)
        if museums:
            break
    if not museums:
        return []
    galleries = _culture_rows(key, 'clifArglv1', yr)
    crtr = str(museums[0].get('crtrYr') or yr - 1)
    CULTURE_Y['y'] = crtr
    facts = [fact('culture_msm_n', 'culture', 'Museums in Seoul',
                  grouped(len(museums)), grouped(len(museums)),
                  pair='culture_count', label_ko='서울의 박물관 수')]
    if galleries:
        facts.append(fact('culture_argl_n', 'culture', 'Art galleries in Seoul',
                          grouped(len(galleries)), grouped(len(galleries)),
                          pair='culture_count', label_ko='서울의 미술관 수'))
    for fid, rows, field, ko_kind in (
            ('culture_top_msm', museums, 'msmNm', '박물관'),
            ('culture_top_argl', galleries, 'arglNm', '미술관')):
        top = _culture_top(rows, field)
        if not top:
            continue
        ko_name, n = top
        en = CULTURE_EN.get(ko_name)
        if not en:
            print(f'Warning: no English name for {ko_name!r} — '
                  f'using Korean on the English card.')
            en = ko_name
        facts.append(fact(fid, 'culture', f"A year's visitors to {en}",
                          grouped(n), grouped(n), pair='top_house', pin=True,
                          label_ko=f'{ko_name} 연간 관람객'))
    return facts


# --- through the turnstiles (관광자원통계) -----------------------------------
# 한국문화관광연구원's monthly visitor counts for paid-admission sites, via
# data.go.kr (자동승인, approved 22 Jul 2026; the openapi.tour.go.kr gateway
# registered the key overnight, unlike the apis.data.go.kr veins which
# unlocked in minutes). One row per attraction per month: csNatCnt Koreans,
# csForCnt foreigners, csMvCnt the total. Publication runs MONTHS behind —
# December 2025 was the newest month on 23 July 2026 — so the harvester
# walks back from last month until rows appear, and the month rides on the
# card footnote.
#
# The rows are curated to a whitelist of recognisable attractions with
# official English names. That is not just naming: the December feed
# carried a memorial hall with EIGHT visitors — a closure artefact in all
# likelihood, and a punchline built on it would mislead (the avgbill
# lesson). An unlisted attraction is simply not offered.

TOUR_BASE = ('http://openapi.tour.go.kr/openapi/service/'
             'TourismResourceStatsService/getPchrgTrrsrtVisitorList'
             '?serviceKey={key}&YM={ym}&SIDO=%EC%84%9C%EC%9A%B8%ED%8A%B9'
             '%EB%B3%84%EC%8B%9C&numOfRows=100&pageNo=1')

TOUR_EN = {
    '경복궁': 'Gyeongbokgung',
    '창덕궁': 'Changdeokgung',
    '창경궁': 'Changgyeonggung',
    '덕수궁': 'Deoksugung',
    '종묘': 'Jongmyo',
    '롯데월드': 'Lotte World',
    '서울스카이': 'Seoul Sky (Lotte World Tower)',
    '아쿠아리움': 'the Lotte World Aquarium',
    '서대문형무소역사관': 'Seodaemun Prison History Hall',
    '서대문자연사박물관': 'the Seodaemun Museum of Natural History',
}

# Wikipedia articles for the whitelisted attractions, verified 23 Jul 2026:
# every entry checked against both wikis (ko 종묘 is the Seoul shrine, not the
# generic rite; both 서울스카이 wiki pages exist and were re-verified 29 Jul
# 2026). 아쿠아리움 is absent on purpose — the Lotte World Aquarium has no
# standalone article on either wiki. The EN anchor is the plain article name
# (no leading "the", unlike TOUR_EN); the KO anchor is the feed name itself.
# 서울스카이 is displayed as "Seoul Sky (Lotte World Tower)" in TOUR_EN — the
# observation deck named first, the building it crowns in parentheses (per
# preference, 29 Jul 2026) — but its wiki anchors stay the deck's own articles.
TOUR_WIKI = {
    '경복궁': ('Gyeongbokgung',
               'https://en.wikipedia.org/wiki/Gyeongbokgung',
               'https://ko.wikipedia.org/wiki/%EA%B2%BD%EB%B3%B5%EA%B6%81'),
    '창덕궁': ('Changdeokgung',
               'https://en.wikipedia.org/wiki/Changdeokgung',
               'https://ko.wikipedia.org/wiki/%EC%B0%BD%EB%8D%95%EA%B6%81'),
    '창경궁': ('Changgyeonggung',
               'https://en.wikipedia.org/wiki/Changgyeonggung',
               'https://ko.wikipedia.org/wiki/%EC%B0%BD%EA%B2%BD%EA%B6%81'),
    '덕수궁': ('Deoksugung',
               'https://en.wikipedia.org/wiki/Deoksugung',
               'https://ko.wikipedia.org/wiki/%EB%8D%95%EC%88%98%EA%B6%81'),
    '종묘': ('Jongmyo',
             'https://en.wikipedia.org/wiki/Jongmyo',
             'https://ko.wikipedia.org/wiki/%EC%A2%85%EB%AC%98'),
    '롯데월드': ('Lotte World',
                 'https://en.wikipedia.org/wiki/Lotte_World',
                 'https://ko.wikipedia.org/wiki/%EB%A1%AF%EB%8D%B0%EC%9B%94%EB%93%9C'),
    '서울스카이': ('Seoul Sky',
                   'https://en.wikipedia.org/wiki/Seoul_Sky',
                   'https://ko.wikipedia.org/wiki/%EC%84%9C%EC%9A%B8%EC%8A%A4%EC%B9%B4%EC%9D%B4'),
    '서대문형무소역사관': ('Seodaemun Prison History Hall',
                           'https://en.wikipedia.org/wiki/Seodaemun_Prison_History_Hall',
                           'https://ko.wikipedia.org/wiki/%EC%84%9C%EB%8C%80%EB%AC%B8%ED%98%95%EB%AC%B4%EC%86%8C%EC%97%AD%EC%82%AC%EA%B4%80'),
    '서대문자연사박물관': ('Seodaemun Museum of Natural History',
                           'https://en.wikipedia.org/wiki/Seodaemun_Museum_of_Natural_History',
                           'https://ko.wikipedia.org/wiki/%EC%84%9C%EB%8C%80%EB%AC%B8%EC%9E%90%EC%97%B0%EC%82%AC%EB%B0%95%EB%AC%BC%EA%B4%80'),
}

# Set by tour_facts() so compose() can put the month on the card.
TOUR_M = {'en': None, 'ko': None}


def tour_facts(key):
    """One month through Seoul's turnstiles: total visitors per attraction,
    and the foreigner counts as their own frame."""
    if not key:
        return []
    first = datetime.now(SEOUL_TZ).date().replace(day=1)
    items = []
    for _ in range(12):
        first = (first - timedelta(days=1)).replace(day=1)
        url = TOUR_BASE.format(key=key, ym=f'{first:%Y%m}')
        r = subprocess.run(['curl', '-s', '--max-time', '30', '-A', MOLIT_UA,
                            url], capture_output=True, text=True)
        try:
            root = ET.fromstring(r.stdout)
        except ET.ParseError:
            return []
        items = list(root.iter('item'))
        if items:
            break
    if not items:
        return []
    TOUR_M['en'] = f'{MONTHS_EN[first.month - 1]} {first.year}'
    TOUR_M['ko'] = f'{first.year}년 {first.month}월'
    facts = []
    totals = []
    for it in items:
        ko_name = (it.findtext('resNm') or '').strip()
        en = TOUR_EN.get(ko_name)
        if not en:
            continue
        def _n(tag, it=it):
            try:
                return int(it.findtext(tag))
            except (TypeError, ValueError):
                return None
        total, forgn = _n('csMvCnt'), _n('csForCnt')
        if total:
            totals.append((ko_name, en, total))
            facts.append(fact(f'tour_{ko_name}', 'tourism',
                              f'Visitors to {en}',
                              grouped(total), grouped(total),
                              num=total, unit='people'))
        if forgn:
            facts.append(fact(f'tourfor_{ko_name}', 'tourism',
                              f'Foreign visitors to {en}',
                              grouped(forgn), grouped(forgn),
                              pair='tour_foreign'))
    if len(totals) < 3:
        return []
    # The sales detectors again: near-equals are a dead heat, and the widest
    # spread between two named attractions is a gap pair.
    best = None
    for i in range(len(totals)):
        for j in range(i + 1, len(totals)):
            a, b = totals[i][2], totals[j][2]
            gap = abs(a - b) / max(a, b)
            if gap <= 0.02 and (best is None or gap < best[0]):
                best = (gap, totals[i], totals[j])
    if best:
        for ko_name, en, n in (best[1], best[2]):
            facts.append(fact(f'tourheat_{ko_name}', 'tourism',
                              f'Visitors to {en}', grouped(n), grouped(n),
                              pair='tour_heat'))
    hi = max(totals, key=lambda t: t[2])
    lo = min(totals, key=lambda t: t[2])
    if hi[2] and lo[2] and hi[2] / max(lo[2], 1) >= 3:
        for ko_name, en, n in (hi, lo):
            facts.append(fact(f'tourgap_{ko_name}', 'tourism',
                              f'Visitors to {en}', grouped(n), grouped(n),
                              pair='tour_gap'))
    return facts


# --- box office (KOBIS / 영화진흥위원회) -------------------------------------
# SEOUL'S admissions, not the country's. wideAreaCd=0105001 is 서울시 in KOBIS's
# own region table (searchCodeList, comCode=0105000000), and the cut is a real
# one rather than a relabelled national figure: on 22 Aug 2026 오디세이 took
# 132,555 admissions in Seoul against 557,345 nationwide, and the orders differ
# too, 인시디어스 ranking 3rd in Seoul and 4th nationally.
#
# ⚠️ Drop the wideAreaCd and the call still succeeds, returning national rows in
# the identical shape. Nothing errors, nothing looks wrong, and the card would
# claim Seoul while printing the country. That is what the vein test guards.
#
# Every posted figure is a published row (관객수), never a share or an average.
KOBIS_BASE = 'https://www.kobis.or.kr/kobisopenapi/webservice/rest'
KOBIS_SEOUL = '0105001'
BOXOFFICE_N = 4        # films on the card, and English-title calls per run
SMALL_NUMBERS_EN = {3: 'three', 4: 'four', 5: 'five', 6: 'six', 7: 'seven'}
# Native Korean numerals, which is what 편 takes: 다섯 편, never 오 편.
SMALL_NUMBERS_KO = {3: '세', 4: '네', 5: '다섯', 6: '여섯', 7: '일곱'}
BOXOFFICE_LOOKBACK = 3   # days to walk back before giving up on a day's rows
# The screens frame: the top film's screen count on this date, this many years
# back. ⚠️ NOT twenty. The ticketing network was still being rolled out in the
# 2000s — about half of screens in 2005, 86% in 2006, 95% in 2007, 98% by 2008
# — and the API's own numbers show it, the top film sitting on 27 Seoul screens
# in Aug 2004 and 89 in 2006. A 2006 figure is a smaller share of cinemas
# reporting, not a smaller audience, so setting it against today would compare
# coverage while looking like it compares cinemas. Both years here are inside
# the ≥98% era. Twenty years becomes honest in 2028 on its own.
SCREENS_YEARS = (5, 10)
# Set by boxoffice_facts() so compose() can date the card.
BOXOFFICE_D = {'en': None, 'ko': None}


def _kobis_title_en(key, movie_cd):
    """The English title KOFIC itself publishes for a film, or ''.

    The box office rows carry Korean titles only, and the English card carries
    no Hangul. Romanising mechanically is the exact mistake
    seoul_index_names_en.json exists to prevent: 오디세이 is "The Odyssey", not
    "Odisei". One extra call per film named; a film with no English title on
    file is dropped rather than guessed at.
    """
    try:
        d = http_get_json(f'{KOBIS_BASE}/movie/searchMovieInfo.json'
                          f'?key={key}&movieCd={movie_cd}')
        return (d['movieInfoResult']['movieInfo'].get('movieNmEn') or '').strip()
    except (RuntimeError, KeyError, TypeError):
        return ''


def boxoffice_facts(kobis_key):
    """What Seoul watched yesterday: admissions per film, on Seoul screens."""
    if not kobis_key:
        return []
    day = datetime.now(SEOUL_TZ).date()
    rows = []
    # A day publishes the following morning, so the newest day with rows is
    # normally yesterday. Walk back rather than assume it: a run at 08:30 can
    # land before KOFIC has posted, and a silent vein beats a wrong date.
    for _ in range(BOXOFFICE_LOOKBACK):
        day -= timedelta(days=1)
        try:
            d = http_get_json(f'{KOBIS_BASE}/boxoffice/searchDailyBoxOfficeList.json'
                              f'?key={kobis_key}&targetDt={day:%Y%m%d}'
                              f'&wideAreaCd={KOBIS_SEOUL}')
            rows = d['boxOfficeResult']['dailyBoxOfficeList']
        except (RuntimeError, KeyError, TypeError):
            return []
        if rows:
            break
    if not rows:
        return []

    films, dropped = [], []
    for r in rows[:BOXOFFICE_N]:
        try:
            audi = int(r['audiCnt'])
        except (KeyError, TypeError, ValueError):
            continue
        ko_name = (r.get('movieNm') or '').strip()
        en_name = _kobis_title_en(kobis_key, r.get('movieCd', ''))
        if not (audi and ko_name and en_name):
            dropped.append(ko_name or r.get('movieCd', '?'))
            continue
        films.append((r.get('movieCd'), ko_name, en_name, audi))
    # ⚠️ All four or nothing. The card IS the day's top four in order, so a
    # short set is not a smaller card, it is a different and misleading one: a
    # reader takes four films with a hole in the ranking for the ranking. The
    # tempting repair, filling the gap from the fifth film, is the same fault
    # wearing a full card's clothes.
    #
    # Say WHY out loud. The likeliest cause is a film with no English title on
    # file at KOFIC, and a vein that goes quiet for days without explaining
    # itself is how the books vein spent 34 days dead. If this line turns up
    # often, the answer is a title table like seoul_index_names_en.json, not a
    # silent fallback to the next rank.
    if len(films) < BOXOFFICE_N:
        if dropped:
            print(f'boxoffice: no English title for {", ".join(dropped)} '
                  f'— vein silent this run (needs {BOXOFFICE_N} of the top '
                  f'{BOXOFFICE_N})')
        return []

    # No year: the card is always yesterday, and the post's own timestamp
    # answers "when" the way it does for the books vein's window. The only
    # ambiguous case is a card posted on 1 January carrying 31 December, where
    # the timestamp still settles it, one day out.
    BOXOFFICE_D['en'] = f'{day.day} {MONTHS_EN[day.month - 1]}'
    BOXOFFICE_D['ko'] = f'{day.month}월 {day.day}일'

    facts = []
    for cd, ko_name, en_name, audi in films:
        # label_ko is set rather than left for the selector: a film's Korean
        # title is not a translation of its English one, it is the other title
        # the distributor registered. pin keeps both ends as published.
        facts.append(fact(f'bo_{cd}', 'boxoffice', en_name,
                          grouped(audi), grouped(audi), label_ko=ko_name,
                          pin=True, num=audi, unit='people'))
    # No dead-heat or gap pairs here, unlike sales and tourism. Those veins
    # offer ten-odd candidates and a pair is a reason to choose two of them;
    # this vein offers exactly the four that go on the card, so a pair would
    # only be the same films a second time under another id. The arrangement
    # is already the point: on 22 Aug 2026 Spider-Man alone outsold the three
    # films beneath it combined, and the four lines in order say so without a
    # detector pointing at it.
    return facts + screens_facts(kobis_key, day, rows[0])


def _same_date(day, years_back):
    """This date, N years ago. 29 February falls back to the 28th, which is
    what a leap day has to do rather than raise."""
    try:
        return day.replace(year=day.year - years_back)
    except ValueError:
        return day.replace(year=day.year - years_back, day=28)


def screens_facts(kobis_key, day, today_row):
    """How many Seoul screens the day's top film is on, against the same date
    five and ten years ago.

    A different card from the admissions one and a different kind of figure, so
    a different category: this is about the cinemas rather than the audience,
    and on 22 August it runs 382 screens against 224 five years back and 161
    ten. Each line is a published scrnCnt for a published number-one film.

    Three lines or none. Two years is a comparison, not a card, and a year
    whose top film has no English title on file cannot be shown at all.
    """
    rows = [(day, today_row)]
    for back in SCREENS_YEARS:
        d = _same_date(day, back)
        try:
            r = http_get_json(f'{KOBIS_BASE}/boxoffice/searchDailyBoxOfficeList.json'
                              f'?key={kobis_key}&targetDt={d:%Y%m%d}'
                              f'&wideAreaCd={KOBIS_SEOUL}')
            got = r['boxOfficeResult']['dailyBoxOfficeList']
        except (RuntimeError, KeyError, TypeError):
            continue
        if got:
            rows.append((d, got[0]))

    facts = []
    for d, r in rows:
        try:
            scrn = int(r['scrnCnt'])
        except (KeyError, TypeError, ValueError):
            continue
        ko_name = (r.get('movieNm') or '').strip()
        en_name = _kobis_title_en(kobis_key, r.get('movieCd', ''))
        if not (scrn and ko_name and en_name):
            continue
        # The year rides IN the label, not on a dateline: each line is a
        # different year, so no single period covers the card, and the year is
        # the thing being compared rather than a caption for the whole. It
        # LEADS the label, and the renderer bolds a leading "YYYY:", because a
        # reader scanning this card is scanning the years and a title is a name
        # they may not know.
        facts.append(fact(f'boxscrn_{d:%Y}', 'boxhist',
                          f'{d.year}: {en_name}', grouped(scrn), grouped(scrn),
                          label_ko=f'{d.year}: {ko_name}', pin=True))
    return facts if len(facts) >= 3 else []


def _url(s):
    from urllib.parse import quote
    return quote(s)


# --- national contrast (KOSIS / Statistics Korea) --------------------------
# KOSIS is a separate source from data.seoul.go.kr, so compose() credits it on
# its own source line. orgId 101 = Statistics Korea; C1/objL1 00 = 전국 (whole
# country), 11 = 서울특별시. prdSe=Y (annual); newEstPrdCnt=1 takes the latest
# year only. The apiKey must be URL-encoded ('=' -> %3D).

def _kosis_row(key_enc, tbl, itm, obj, prd_se='Y'):
    url = ('https://kosis.kr/openapi/Param/statisticsParameterData.do?method=getList'
           f'&apiKey={key_enc}&format=json&jsonVD=Y&orgId=101&tblId={tbl}'
           f'&itmId={itm}&objL1={obj}&prdSe={prd_se}&newEstPrdCnt=1')
    d = http_get_json(url)
    if isinstance(d, list) and d:
        return d[0]
    raise RuntimeError(f'KOSIS returned no data for {tbl} objL1={obj}: {d!r:.120}')


def kosis_facts(kosis_key):
    """National-vs-Seoul figures from KOSIS: Seoul's share of the country's
    population, and the total-fertility-rate gap (Seoul is the lowest in Korea).
    Annual figures; a KOSIS outage just yields an empty list, never a crash."""
    if not kosis_key:
        return []
    from urllib.parse import quote
    enc = quote(kosis_key, safe='')
    facts = []
    try:
        pop_kr = _kosis_row(enc, 'DT_1B040A3', 'T20', '00')
        pop_se = _kosis_row(enc, 'DT_1B040A3', 'T20', '11')
        n_kr, n_se = int(pop_kr['DT']), int(pop_se['DT'])
        py = pop_se.get('PRD_DE') or None
        facts.append(fact('pop_seoul', 'national', 'People who live in Seoul',
                          grouped(n_se), grouped(n_se), pair='share_gap', year=py,
                          num=n_se, unit='people'))
        facts.append(fact('pop_korea', 'national', 'People who live in South Korea',
                          grouped(n_kr), grouped(n_kr), pair='share_gap', year=py))
        if n_kr:
            share = 100 * n_se / n_kr
            facts.append(fact('pop_share', 'national',
                              'Share of all South Koreans who live in Seoul',
                              f'{share:.1f}%', f'{share:.1f}%', year=py))
    except (RuntimeError, KeyError, IndexError, ValueError, ZeroDivisionError):
        pass
    try:
        fert_kr = _kosis_row(enc, 'DT_1B81A21', 'T1', '00')
        fert_se = _kosis_row(enc, 'DT_1B81A21', 'T1', '11')
        v_kr, v_se = str(fert_kr['DT']), str(fert_se['DT'])
        fy = fert_kr.get('PRD_DE') or None
        facts.append(fact('fert_korea', 'national',
                          'Births the average South Korean woman will have',
                          v_kr, v_kr, pair='fertility_gap', year=fy))
        facts.append(fact('fert_seoul', 'national',
                          'Births the average Seoul woman will have',
                          v_se, v_se, pair='fertility_gap', year=fy))
    except (RuntimeError, KeyError, IndexError, ValueError):
        pass
    return facts


# --- world cities (OECD functional urban areas) ----------------------------
# The OECD's SDMX service publishes its FUA ("functional urban area") database:
# one publisher measuring every metro area the same way, which is the only kind
# of source a Seoul-vs-other-cities line can honestly be built on. A FUA is the
# built-up core plus its commuting belt, so KOR01F is the whole Seoul capital
# region (~24m people), NOT the 9.6m of Seoul city that the KOSIS lines use.
# compose() therefore puts the metric, the FUA caveat and the year on the
# source line of every comparison post.
#
# No API key. CSV comes back with one row per (city, measure, year); the key is
# positional, so a dataflow needs exactly as many dots as it has dimensions
# after REF_AREA — _sdmx_csv() reads the count out of the service's own error
# message if the DSD ever gains a dimension.

OECD_BASE = 'https://sdmx.oecd.org/public/rest/data/OECD.CFE.EDS'
OECD_DOMAIN = 'data-explorer.oecd.org'

# Peers chosen to be recognisable to both an English and a Korean reader. Any
# city missing from a given year is simply dropped, so this list is safe to grow.
WORLD_CITIES = [
    ('KOR01F', 'Seoul'),
    ('JPN01F', 'Tokyo'),
    ('JPN02F', 'Osaka'),
    ('FR001F', 'Paris'),
    ('UK001F', 'London'),
    ('USA01F', 'New York'),
    ('DE001F', 'Berlin'),
    ('ES001F', 'Madrid'),
    ('NL001F', 'Amsterdam'),
]

# (key, dataflow, dots after REF_AREA, row filter, metric label EN/KO, formatter)
WORLD_MEASURES = [
    ('green', 'DSD_FUA_ENV@DF_GREEN_AREA', 6,
     {'MEASURE': 'GREEN_AREA', 'UNIT_MEASURE': 'M2_PS'},
     ('Green space per person', '1인당 녹지 면적'),
     lambda v: f'{v:,.0f}m²'),
    ('transit', 'DSD_FUA_TRAN@DF_PT_ACCESS', 7,
     {'MEASURE': 'POP_WITH_ACCESS', 'TRAVEL_TIME': 'MN_LE5', 'SERVICE': 'PT_STOP'},
     ('Share of people within a 5-minute walk of a transit stop',
      '도보 5분 내 대중교통 정류장 이용 가능 인구 비율'),
     lambda v: f'{v:.1f}%'),
    ('heat', 'DSD_FUA_ENV@DF_UHI', 6,
     {'MEASURE': 'UHI', 'TIME_SEASON': 'NIGHT_SUMMER'},
     ('Urban heat island, summer nights', '여름밤 도시 열섬 강도'),
     # ⚠️ A DIFFERENCE, not a temperature: to_f_delta, never to_f.
     lambda v: (to_f_delta(v), f'{v:.1f}°C')),
    ('density', 'DSD_FUA_TERR@DF_DENSITY', 4,
     {'MEASURE': 'POP_DEN'},
     ('People per square kilometre', '1제곱킬로미터당 인구'),
     lambda v: f'{v:,.0f}/km²'),
]

WORLD_METRICS = {key: labels for key, _, _, _, labels, _ in WORLD_MEASURES}

# A world post needs Seoul plus at least this many peers, all in the SAME year.
# Mixed vintages are the trap here: the OECD's latest density figure is 2020 for
# London and 2024 for Amsterdam, and setting those side by side would be a
# comparison of survey dates dressed up as a comparison of cities.
WORLD_MIN_PEERS = 2


def _sdmx_csv(flow, ndots, codes, start_period):
    """One OECD SDMX-REST query, returned as a list of dict rows (or [])."""
    codes = '+'.join(codes)
    for _ in range(3):
        url = (f'{OECD_BASE},{flow},/{codes}{"." * ndots}'
               f'?startPeriod={start_period}')
        r = subprocess.run(['curl', '-s', '--max-time', '60', '-H',
                            'Accept: application/vnd.sdmx.data+csv', url],
                           capture_output=True, text=True)
        text = r.stdout if r.returncode == 0 else ''
        if text.lstrip().startswith('DATAFLOW'):
            return list(csv.DictReader(io.StringIO(text)))
        # "Not enough key values in query, expecting 9 got 8" — the DSD gained or
        # lost a dimension; take the service's own count and retry once.
        m = re.search(r'expecting (\d+) got (\d+)', text)
        if m:
            want = int(m.group(1)) - 1
            if want == ndots or not 0 <= want <= 20:
                return []
            ndots = want
    return []


def _world_latest_common_year(rows, filt, names):
    """Newest year in which Seoul and enough peers all report. Returns
    (year, {code: float}) or (None, {})."""
    by_year = {}
    for r in rows:
        if any(r.get(k) != v for k, v in filt.items()):
            continue
        code = r.get('REF_AREA')
        if code not in names:
            continue
        try:
            by_year.setdefault(r['TIME_PERIOD'], {})[code] = float(r['OBS_VALUE'])
        except (KeyError, TypeError, ValueError):
            continue
    for year in sorted(by_year, reverse=True):
        vals = by_year[year]
        if 'KOR01F' in vals and len(vals) >= WORLD_MIN_PEERS + 1:
            return year, vals
    return None, {}


def world_facts():
    """Seoul against peer metro areas, one OECD measure at a time.

    Every measure yields its own pair (city_green, city_transit, ...) so the
    selector builds a post around a single metric: the lines are bare city
    names, and it is the opener that says what is being counted."""
    names = dict(WORLD_CITIES)
    codes = [c for c, _ in WORLD_CITIES]
    out = []
    for key, flow, ndots, filt, _labels, fmt in WORLD_MEASURES:
        try:
            rows = _sdmx_csv(flow, ndots, codes, 2015)
            year, vals = _world_latest_common_year(rows, filt, names)
            if not year:
                continue
            for code, v in vals.items():
                # A metric may return one string for both languages, or an
                # (english, korean) pair where only the English card carries an
                # imperial conversion.
                value = fmt(v)
                value_en, value_ko = (value if isinstance(value, tuple)
                                      else (value, value))
                # Bare city name: dedupe_labels() strips a shared trailing run
                # from all but the FIRST label, so "... metro area" on every line
                # would survive only on line one and read as though that city
                # alone were a metro figure. The caveat rides on the source line,
                # which compose() always writes, and on the pinned card.
                out.append(fact(f'world_{key}_{code}', 'world',
                                names[code], value_en, value_ko,
                                pair=f'city_{key}', year=year))
        except (RuntimeError, KeyError, IndexError, ValueError):
            continue
    return out


# --- Seoul against whole countries (World Bank for the countries, KOSIS for
# --- Seoul) ----------------------------------------------------------------
# Re-anchored to LEAD with Seoul, so every card carries a Seoul figure and reads
# as a Seoul card, not a bare nations table. The peers are WHOLE COUNTRIES, so
# Seoul (a 605 km² city) is set against the likes of Korea, Japan, the US — and
# that is the point of the density line especially: Seoul is denser than entire
# nations. Seoul's own figure is computed live from KOSIS (its population over
# the city's area, or its published rate); the countries come from the World
# Bank. The year is the newest one BOTH sources share, the same latest-common-
# year rule world_facts uses; the source line names the metric and the footnote
# flags the Seoul-vs-countries scope. https is required: the http host
# Cloudflare-redirects to https.
WB_BASE = 'https://api.worldbank.org/v2'
WB_DOMAIN = 'data.worldbank.org'
NATION_COOLDOWN_DAYS = 3   # like WORLD_COOLDOWN_DAYS: keep nation posts occasional
SEOUL_AREA_KM2 = 605.21    # Seoul Metropolitan Government official city area

# Peer countries recognisable to an English and a Korean reader; any country
# missing a given indicator-year is simply dropped, so this list is safe to
# grow. Korea stays in: Seoul -> Korea -> the world is the intended reading.
WB_COUNTRIES = [
    ('KOR', 'South Korea'),
    ('JPN', 'Japan'),
    ('USA', 'United States'),
    ('GBR', 'United Kingdom'),
    ('FRA', 'France'),
    ('DEU', 'Germany'),
    ('CHN', 'China'),
]

# Each measure sets Seoul against the countries on one metric. 'wb' is the World
# Bank indicator (countries); 'seoul_*' locate Seoul's own annual figure in
# KOSIS (orgId 101, objL1 11 = 서울); 'seoul' transforms that raw KOSIS value
# into the metric (density = population / area). Values are language-neutral
# (numbers + symbols) so value_en == value_ko and the selector translates only
# the bare place-name labels, exactly as in world_facts.
WB_MEASURES = [
    {'key': 'density', 'wb': 'EN.POP.DNST',
     'seoul_tbl': 'DT_1B040A3', 'seoul_itm': 'T20', 'seoul_obj': '11',
     'seoul': lambda pop: pop / SEOUL_AREA_KM2,
     'label': ('People per square kilometre', '1제곱킬로미터당 인구'),
     'fmt': lambda v: f'{v:,.0f}/km²'},
    {'key': 'fertility', 'wb': 'SP.DYN.TFRT.IN',
     'seoul_tbl': 'DT_1B81A21', 'seoul_itm': 'T1', 'seoul_obj': '11',
     'seoul': lambda x: x,
     'label': ('Births per woman', '여성 1명당 출생아 수'),
     'fmt': lambda v: f'{v:.2f}'},
]
WB_METRICS = {m['key']: m['label'] for m in WB_MEASURES}
WB_MIN_PEERS = 2   # this many peer countries (beyond Korea) must share the year


def _wb_indicator(indicator, iso_list):
    """One World Bank call for every country on one indicator; returns
    {iso3: {year: value}} over non-null values, or {} on failure."""
    ctry = ';'.join(iso_list)
    url = (f'{WB_BASE}/country/{ctry}/indicator/{indicator}'
           f'?format=json&mrv=6&per_page=400')
    for _ in range(3):
        r = subprocess.run(['curl', '-s', '--max-time', '25', url],
                           capture_output=True, text=True)
        try:
            d = json.loads(r.stdout)
        except (ValueError, TypeError):
            continue
        if isinstance(d, list) and len(d) > 1 and d[1]:
            out = {}
            for row in d[1]:
                if row.get('value') is None:
                    continue
                iso, yr = row.get('countryiso3code'), row.get('date')
                if iso and yr:
                    out.setdefault(iso, {})[yr] = row['value']
            return out
    return {}


def _kosis_series(key_enc, tbl, itm, obj, n=10):
    """{'YYYY': value} for the latest n annual periods of one KOSIS series, so a
    Seoul figure can be aligned to whatever year the World Bank last reported.
    Empty on any failure — the vein just goes silent, never crashes."""
    url = ('https://kosis.kr/openapi/Param/statisticsParameterData.do?method=getList'
           f'&apiKey={key_enc}&format=json&jsonVD=Y&orgId=101&tblId={tbl}'
           f'&itmId={itm}&objL1={obj}&prdSe=Y&newEstPrdCnt={n}')
    try:
        d = http_get_json(url)
    except (RuntimeError, ValueError, OSError):
        return {}
    out = {}
    if isinstance(d, list):
        for r in d:
            try:
                out[r['PRD_DE']] = float(r['DT'])
            except (KeyError, TypeError, ValueError):
                continue
    return out


def worldbank_facts(state, kosis_key):
    """Seoul against whole countries, one metric at a time — the World Bank for
    the countries, KOSIS for Seoul.

    Re-anchored to LEAD with Seoul: every card carries a Seoul figure computed
    live (its KOSIS population over the city's 605.21 km², or its KOSIS rate),
    so the card reads as a Seoul card, not a bare nations table. Each measure
    yields its own pair (nation_density, nation_fertility) so the selector
    builds a post around one metric; the labels are bare place names, the opener
    names the metric, and the year is the newest one BOTH sources share.

    Gated by its own cooldown here (rather than in main() like world) so a
    cooled nation vein makes NO network calls."""
    if not kosis_key:
        return []
    last = state.get('last_nation_at')
    if last:
        try:
            if datetime.now(timezone.utc) - datetime.fromisoformat(last) \
                    < timedelta(days=NATION_COOLDOWN_DAYS):
                return []
        except ValueError:
            pass
    from urllib.parse import quote
    enc = quote(kosis_key, safe='')
    names = dict(WB_COUNTRIES)
    isos = [c for c, _ in WB_COUNTRIES]
    out = []
    for m in WB_MEASURES:
        try:
            wb_by_year = {}
            for iso, yv in _wb_indicator(m['wb'], isos).items():
                for yr, v in yv.items():
                    wb_by_year.setdefault(yr, {})[iso] = v
            seoul_raw = _kosis_series(enc, m['seoul_tbl'], m['seoul_itm'], m['seoul_obj'])
            seoul_by_year = {yr: m['seoul'](v) for yr, v in seoul_raw.items()}
            # Newest year Seoul and >= WB_MIN_PEERS+1 countries (Korea + peers)
            # all report: mixed vintages would be a comparison of survey dates.
            year = next((yr for yr in sorted(set(wb_by_year) & set(seoul_by_year),
                                             reverse=True)
                         if len(wb_by_year[yr]) >= WB_MIN_PEERS + 1), None)
            if not year:
                continue
            fmt = m['fmt']
            sv = fmt(seoul_by_year[year])
            out.append(fact(f"nation_{m['key']}_SEOUL", 'nation', 'Seoul',
                            sv, sv, pair=f"nation_{m['key']}", year=year))
            for iso, v in wb_by_year[year].items():
                fv = fmt(v)
                out.append(fact(f"nation_{m['key']}_{iso}", 'nation', names[iso],
                                fv, fv, pair=f"nation_{m['key']}", year=year))
        except (RuntimeError, KeyError, ValueError, TypeError):
            continue
    return out


# --- selection + composition ----------------------------------------------

def build_pool(api_key, state, kosis_key=None, gov_key=None, hrfco_key=None,
               kobis_key=None):
    # gov_key is the shared data.go.kr key: one key, per-API 활용신청, so the
    # property, weather, airport, health and culture veins all ride on it.
    pool = []
    pool += crowd_facts(api_key, crowd_window(state))
    pool += air_facts(api_key)
    pool += transport_facts(api_key, state)
    pool += rush_facts(api_key, state)
    pool += count_facts(api_key)
    pool += bike_facts(api_key)
    pool += traffic_facts(api_key)
    pool += river_facts(api_key, gov_key)
    pool += level_facts(hrfco_key)
    pool += price_facts(api_key, state)
    pool += water_facts(api_key)
    pool += daynight_facts(api_key, state)
    pool += infant_facts(api_key, state)
    # kosis_key is the library ratio's denominator (Seoul's registered
    # population that age); without it the vein still posts bare counts.
    pool += library_facts(api_key, kosis_key)
    pool += complaint_facts(api_key)
    pool += books_facts()
    pool += sales_facts()
    pool += kosis_facts(kosis_key)
    pool += world_facts()
    # Re-anchored 30 Jul 2026 to LEAD with Seoul (World Bank for the countries,
    # KOSIS for Seoul), then held off pending a look at the card. Card previewed
    # and approved by the user 17 Aug 2026, so LIVE from this point:
    #     People per square kilometre
    #     Seoul 15,509/km² · South Korea 530/km² · United States 37/km²
    # The metric used to be repeated on the source reply as well as the opener;
    # see unsaid_metrics(), fixed in the same pass.
    pool += worldbank_facts(state, kosis_key)
    pool += molit_facts(gov_key)
    pool += kma_facts(gov_key)
    pool += kac_facts(gov_key)
    pool += hira_facts(gov_key)
    pool += culture_facts(gov_key)
    pool += tour_facts(gov_key)
    # KOFIC issues its own key, like HRFCO: not a data.go.kr one.
    pool += boxoffice_facts(kobis_key)
    return pool


SELECT_PROMPT = """You are the editor of "Seoul by the numbers", a Bluesky account in the style of Harper's Index: a short list of real statistics arranged so two numbers sit next to each other and make the reader do a double-take.

You are given a POOL of candidate lines (each already has an exact value you must NOT change) and some PAIRS that already form a sharp juxtaposition (a near-equal "dead heat", or a wide gap). Build ONE post.

Rules:
- Choose 3 to 4 lines that form a coherent set. STRONGLY prefer building around one PAIR (a dead heat or a wide gap) — that is the joke.
- CROSS_PAIRS (may be empty) are the account's sharpest move: two figures from DIFFERENT veins that happen to land on nearly the same number — a coincidence worth a double-take. Each side shares one unit (two ₩ figures, or two head-counts). You MAY build ONE post around a single CROSS_PAIR, and choosing one OVERRIDES the "own post, never mixed" rule below — but only for the two veins that pair names. When you do:
  · Use BOTH of the pair's lines. Add 1 or 2 companion lines drawn ONLY from those same two veins, and EVERY line in the post must share the pair's unit (all ₩, or all head-counts) — never add a percentage, a count of things, or a line from any third vein.
  · Companion lines must be CLOSE IN SIZE to the pair — the same order of magnitude, never several times larger or smaller. The joke is that the pair's two numbers are nearly equal; a companion that towers over them (a national total above a city-scale pair) flattens that near-equality and buries the double-take. If no same-scale companion exists in those two veins, prefer a different pair.
  · The opener MUST be neutral and give nothing away — "Seoul by the numbers" / "숫자로 보는 서울", or a short neutral time/place framing. NEVER use a vein-specific opener (not "Spent last quarter", not "The apartment market", not "Through the turnstiles"): it would falsely frame the other vein's line.
  · Let the coincidence sit there unremarked, exactly as with any pair — never write a line, opener or note that points out that the two numbers match.
  · Only reach for a CROSS_PAIR when the two SUBJECTS make a genuinely interesting, tasteful pair (one apartment's deposit against a whole industry's quarter; a month's visitors against a crowd right now). If a pair's two subjects are dull or jarring together, ignore it and build a normal single-vein post. Never force it. NEVER build a cross pair that involves illness or patients.
- House style is Harper's Index: let the arrangement carry the joke. NEVER add a line that explains or points out the juxtaposition, and never editorialise. Just the labelled numbers.
- Punctuation: NEVER write an em dash (—) in anything you produce: not in an opener, not in a label, not in the note. Use a colon or a comma instead. House style has no em dashes anywhere.
- Do NOT worry about line order: when the lines share a unit (e.g. an all-₩ post) they are automatically sorted by value, largest first. A near-equal "dead heat" still lands because near-equal values end up next to each other. Just choose a coherent set.
- Each line is a bare "Label: value". Do NOT repeat a shared verb or metric on every line — put it once in the opener. For spending posts (₩ amounts), pick an opener that carries the verb, e.g. "Spent last quarter in Seoul", so lines read "Coffee shops: ₩651.4bn", never "Spent at coffee shops: ...". This matters for live "right now" lines too: the pool labels repeat the whole phrase ("Estimated crowd in Jamsil right now"), and a post that copies them four times reads like a form. Name the metric on ONE line and leave the others bare ("Estimated crowd in Jamsil", then "Hongdae", "Gangnam Station"), and let the opener carry the time frame.
- Wording shared by EVERY line is trimmed automatically after you answer, so a label you leave repetitive will be cut back rather than posted as-is. Write the labels you want and do not pad them to match each other.
- Some ₩ lines are average BILLS (category "avgbill"), not quarterly totals: sales divided by the number of transactions, i.e. what one payment came to. One bill is not one person — a Korean-restaurant bill covers a shared table, while a coffee is one person paying for themselves. So use an average-bill opener like "Average bill in Seoul" (never the "Spent last quarter" one, and never wording like "per visit" or "per person", which would claim a per-head figure the data does not give). Never mix avgbill lines with quarterly-total spending lines in one post.
- For age-group crowd posts, write the age band as a numeral: "20-somethings" (never "Twentysomethings"). Opener e.g. "20-somethings in Seoul's crowds, right now"; lines are bare place names.
- Do not mix unrelated live "right now" lines with quarterly spending lines in a way that breaks a single frame, unless the contrast itself is the point.
- "national" lines (Seoul set against the whole country: its share of the population, the fertility-rate gap) are annual figures from a different source. Build them into their own "Seoul and the nation" post — never mix a national line with a live "right now" line or a spending line. The fertility pair is only two lines, so pair it with the population-share line to make a set of three.
- "world" lines set Seoul's metro area against other cities' metro areas, from the OECD. Their labels are BARE CITY NAMES, so the opener MUST say what is being measured (e.g. "Green space per person", "Within a five-minute walk of transit") — this is the one case where the opener names the metric. Build them into their own post: every world line in a post must come from the SAME pair (all city_green, or all city_transit, never a mix), and a world line NEVER appears alongside a Seoul-only line of any other category. Always include the Seoul line.
- "nation" lines set SEOUL against whole countries, on one metric, from the World Bank (countries) and KOSIS (Seoul). Seoul leads the card; the peers are whole nations (Korea, Japan, the US…), which is the point — e.g. Seoul is denser than entire countries. Labels are BARE PLACE NAMES (Seoul, then countries), so the opener MUST name the metric (e.g. "People per square kilometre", "Births per woman") — the same rule as the world lines. Do NOT reach for the generic "Seoul and the nation" / "서울과 전국" opener here: that framing belongs to the Seoul-vs-Korea "national" lines, and on a nation card it names no metric, leaving the countries measuring nothing — make the metric itself the opener. Build them into their own post: every nation line must come from the SAME pair (all nation_density, or all nation_fertility, never a mix), ALWAYS include the Seoul line, and a nation line NEVER appears alongside a Seoul-only line of any other category or a world (city) line. The pair is the point: Seoul against the country that most sharpens it (the widest gap, or a near dead heat).
- "property" lines are one month's apartment-market filings from the national land ministry: actual sale prices (the dearest and cheapest single sales), a record jeonse deposit, and counts of filings. Build them into their own post — never alongside a live "right now" line, a spending line, a national line or a world line. The pairs are the point: the price gap (dearest vs cheapest sale) or the jeonse/monthly-rent split. Never put a month or date in a property label — the filing month rides on the card automatically.
- "weather" lines are published readings from Seoul's official weather station: yesterday's high/low/rain, the last full month set against the SAME month FIFTY YEARS earlier, and (in summer) a season-to-date swelter tally — days of 33°C or more counted from 1 June through yesterday — likewise against the same span fifty years back (each label already carries its dates and year — do not reword those labels). Build them into their own post, never mixed with any other category, and pick ONE frame: the yesterday set, the then-and-now monthly set, OR the season-to-date set (never blend the three). A season-to-date post is built around the swelter tally ("Days of 33°C or more, 1 Jun–…") — always include that pair; the hottest/wettest/tropical season-to-date pairs are its companions. In any then-and-now or season-to-date post every pair must keep BOTH its sides, every pair must put its two years in the SAME order, and the arrangement carries the half-century — never point it out. Open both fifty-year weather frames with "50 years apart" / "50년의 간격" (the numeral, not "Fifty").
- "tourism" lines are one month's visitor counts at named paid-admission Seoul attractions (the palaces, Lotte World, Seoul Sky…). Own post; ONE frame per post — total visitors OR foreign visitors, never both; the month rides on the card automatically. The pairs are the point: a dead heat or the widest gap between two named attractions.
- "river" lines are readings taken at ONE hour: the water temperature in the Han (at Seonyu) and in three tributaries, plus the AIR temperature over central Seoul at that same hour. Build them into their own post, never mixed with any other category, and ALWAYS INCLUDE "The air" line — it is the whole point. Four river temperatures alone sit within about a degree of each other and say nothing; the contrast is the water disagreeing with the sky. Labels are BARE NAMES ("The Han at Seonyu", "The air"), so the opener MUST carry the metric and nothing more, e.g. "Water and air in Seoul" (ℹ️ whatever you write here is REPLACED in compose(): the opener names air or water first to match whichever the sort puts on the top line, which is a fact about the readings rather than a choice of words) — the same case as the world, traffic and books lines. ⚠️ Do NOT put the hour, the time or the words "one hour" in the opener: the reading hour rides on the card automatically as its dateline, and an opener repeating it spends the line saying nothing. Do NOT write "right now" either: that hour can be several hours old. Never point out that the water is warmer or cooler than the air; let the arrangement do it.
- "level" lines appear ONLY when the Han is running high, and they are one gauge (잠수교) set against its own published flood-warning tiers: the level right now, then the 관심/주의/경계/심각 levels. Build them into their own post, never mixed with any other category, and include the current level plus at least two tiers — the arrangement IS the story, which is how far the river is from each tier. The opener must name the river and the gauge, e.g. "The Han at Jamsu Bridge". ⚠️ NEVER write or imply that the bridge is closed, submerged, flooded or about to be: these are flood-WARNING tiers set by 한강홍수통제소, not the level at which the walkway goes under, and the two are different things. Do not add alarm, urgency or commentary of any kind — state the levels and stop. Never call the situation dangerous.
- "price" lines are ONE everyday item priced at shops across Seoul on one day. Each label is a bare shop KIND and DISTRICT ("A traditional market in Dongjak-gu", "A supermarket in Nowon-gu"), so the opener MUST name the item and that these are its prices — e.g. "What a watermelon costs in Seoul" — the same case as the world, traffic and books lines. Build them into their own post, never mixed with any other category, and ALWAYS keep the cheapest and dearest lines: the card IS the spread. Never point out that markets are dearer than supermarkets or the reverse — it changes from item to item, and noticing it is the reader's job. Never call a price high, low, cheap or a bargain.
- "water" lines are the raw water drawn at each of Seoul's purification centres on one day. Labels are BARE PLACE NAMES (Amsa, Ttukdo), and the card's dateline says they are purification centres, so the opener MUST name the METRIC and nothing more ("Water drawn for Seoul"). ⚠️ Do NOT put the day, the date or the words "one day" in the opener: the date rides on the card automatically and an opener that repeats it spends the line saying nothing. Own post, never mixed. Every line is an intake figure at the same measure — never say one centre is bigger or busier than another.
- "daynight" lines are HOW MANY PEOPLE ARE PRESENT in each district, either by DAY or by NIGHT, never both in one card. Labels are BARE DISTRICT NAMES, so the opener MUST name BOTH what is counted — people, a population — AND which half of the day: "Seoul's daytime population", "How many people are in Seoul after dark". ⚠️ An opener naming only the time ("Seoul by day") is NOT enough and leaves the reader guessing whether the figures are people, money or anything else. The measure is 생활인구: everyone present at that hour, residents and workers and visitors together — so never call it the district's population in the sense of who LIVES there, and never call it a crowd. Own post, never mixed. The KT-estimate caveat rides on the card already: do not restate it in a line.
- "infant" lines count Seoul's children in ONE age band, one line per year across a decade. Labels are BARE YEARS. ⚠️ The card already names the age band on its own line, and YOU ARE NOT TOLD WHICH BAND IT IS — so the opener must NEVER state an age or an age range. Writing "Children aged 0" over the under-six figures is the exact mistake this rule exists to stop. Give a neutral opener that says only that these are Seoul's children over time: "Seoul's children, a decade apart", "Fewer every year in Seoul". Own post, never mixed, and keep the first and last years: the fall between them is the card. State it and stop — never call it a decline, a crisis, or a collapse, and never mention birth rates.
- "library" lines are the registered members of Seoul Library by decade of life. Labels are BARE AGE BANDS, so the opener MUST name the library and what is counted ("Who holds a card at Seoul Library"). Own post, never mixed. It is ONE library, not the city's 215 — never imply otherwise. ⚠️ The value may carry a trailing "(1 in N)" — that is Python's, and it sets the members of that band against Seoul's registered population of that age. Leave it exactly where it is and NEVER restate it, convert it to a percentage, explain it, or build the opener or a label on it: the card footnote says what it is, and members need not live in Seoul, so the opener must never call it a share of Seoul's teens or of any other age.
- "complaint" lines are how many faults Seoul's residents reported in a whole year, one line per year. Labels are BARE YEARS, so the opener MUST name what is counted ("Things reported broken in Seoul"). Own post, never mixed, and never characterise a year as better or worse than another.
- "airport", "health" and "culture" lines are single-source sets like "property" and "weather": each builds its OWN post, never mixed with another category. An airport post is Gimpo's newest month — pick ONE frame, the twenty-year pair or the domestic/international split (labels carry their months). A health post is patient counts at Seoul care institutions in one year: the labels are bare condition names, so the opener must carry the "a year in Seoul's clinics" framing. These are real illnesses — arrange the numbers, never joke about them, and drop any set that reads as a punchline at patients' expense. A culture post is the city's museums and galleries: the counts and the year's most-visited houses.
- "bike" lines are the public-bike system (Ttareungi) counted live, citywide, right now: bikes waiting at a dock, docking points, stations, and stations standing empty. These are live "right now" figures like the crowd and air lines — build them into their own post, and the opener MUST carry the "right now" framing so the bare counts read as a live snapshot, not fixed totals. The pair is the point: bikes waiting against docking points, or empty stations against all stations. Never mix a bike line with a spending, national, world or other single-source line.
- "traffic" lines are live road speeds (km/h) on named Seoul arteries, right now. Like the "world" lines, the labels are BARE ROAD NAMES, so the opener MUST name the metric and the time ("How fast Seoul is driving right now", or a neutral live-speed framing) — this is the other case where the opener names the metric. Build them into their own post; the pair is the gap between the fastest-moving and slowest-moving road. Never mix a traffic line with any other category.
- "books" lines are checkouts at SEOUL LIBRARY over the last 60 days, counted by SUBJECT: literature, philosophy, 어학 and the rest, in the library's own classification. Labels are BARE SUBJECT NAMES, so the opener MUST name the library and say these are loans, exactly as the "library" membership lines do — and MUST NOT settle on one wording: "What Seoul Library lent, by subject", "Seoul Library's loans, by subject", "Borrowing at Seoul Library, by subject" and "What went out of Seoul Library" are four of many, so write a fresh one rather than reusing the last. ⚠️ It is ONE library, the city's flagship, NOT Seoul's 215 public libraries — never imply otherwise. ⚠️ Do NOT put the date or the window in the opener: both ride on the card automatically. Own post, never mixed with any other category. ⚠️ The value may carry a trailing "(1 in N)" — that is Python's, and it is the subject's share of every checkout counted, which is why four lines can still say what the other six weigh. Leave it exactly where it is and NEVER restate it, convert it to a percentage, explain it, or build the opener or a label on it; the card footnote gives the total it divides by. ⚠️ TEN subjects are offered and a card takes four, so there is no one right card and THE EXTREMES ARE NOT COMPULSORY. Do not reach for the biggest subject at the top and the smallest at the bottom every time: four subjects from the middle of the list is a card, the four smallest is a card, and a set leaving out the largest number altogether is a card. The two pairs are two arrangements among many rather than the default — a "book_heat" pair is two subjects that came out level, a "book_gap" pair is the least- and most-borrowed of the ten; use at most ONE of them on a card, and prefer neither if the plain four you have chosen already say something. Deliberately vary which subjects appear from post to post and lean hard on AVOID_IDS here: with only ten subjects this vein repeats itself faster than any other. Never say which way the gap runs, never call a subject popular or neglected, and never draw a conclusion about what Seoul reads — set the numbers down and let the reader do it.
- "rush" lines are SUBWAY BOARDINGS at one named station in ONE HOUR of the day. Labels are a station and a clock time ("City Hall, 6 p.m."), so the opener MUST say IN WORDS that these are subway boardings, e.g. "Boarding the Seoul subway", "Through the turnstiles, by the hour" — the same case as the world, traffic, price and books lines — and MUST NOT settle on one wording, so write a fresh one rather than reusing the last. ⚠️ EVERY figure is a WHOLE MONTH of that hour: never write or imply that one is a single day's, a single evening's, an average, or "in an hour". ⚠️ Do NOT put the month in the opener: it rides on the card as its dateline. The PAIR offered is the SAME station at its morning hour and its evening hour, and that contrast IS the joke: use both halves and let it sit there unremarked. Never point out that one is larger, never call a station busy, quiet, dead or booming, and never label a place residential, commercial, a business district or a dormitory suburb: the four numbers say all of it, and saying it as well is the one thing this account never does. Own post, never mixed with any other category.
- "boxoffice" lines are cinema ADMISSIONS on SEOUL screens for ONE day, film by film, from the Korean Film Council's ticketing network. Labels are BARE FILM TITLES, so the opener MUST say IN WORDS that the figures are admissions or tickets, and that they are Seoul's: a title and a bare number leave the reader to guess whether it is people, screens or won. "Seoul at the cinema" is NOT enough on its own and neither is "What Seoul watched" — write e.g. "Cinema admissions in Seoul", "Tickets sold in Seoul's cinemas", "Seats filled in Seoul's cinemas" (관객수 / 티켓 in the Korean), the same case as the world, traffic, price and books lines — and MUST NOT settle on one wording, so write a fresh one rather than reusing the last. ⚠️ These are SEOUL's admissions, NOT the country's: never write "nationwide", "across Korea" or any national framing, and never imply the figures are a film's total. ⚠️ Do NOT put the date in the opener: the day rides on the card automatically as its dateline. ⚠️ Titles are printed exactly as they come, in each language: never translate, shorten or reword a film title. ⚠️ EVERY film on this card gets an "emoji", with no exceptions: the general rule above lets you leave one blank where nothing obvious fits, and that is right for an abstract line but wrong here, since a film is always ABOUT something. Take it from the subject, the genre or the title itself: 🕷 for a Spider-Man film, 👻 for a horror, 🕵 for a detective story, 🐋 for a whale, 🏛 or ⛵ for an ancient epic, 🎞 or 🍿 as a last resort. If a card would go out with one film tagged and another bare, every emoji on it is stripped instead, so a lazy blank costs the whole card its emoji rather than just that line. Own post, never mixed with any other category. ⚠️ The four films offered are the day's FOUR most-watched in Seoul, and you must use ALL FOUR, every time: this card is the complete top four in order, not a selection from a longer list, and dropping one leaves a hole in a ranking that a reader will take for the ranking. Do not number the lines (they are already sorted by value) and do not write an opener that ranks them ("the day's winners", "Seoul's biggest"): the footnote says what the set is, and the arrangement does the rest. Never call a film a hit, a flop or a winner, never say which is beating which, and never remark on the gap between them.
- "boxhist" lines are a DIFFERENT card from the box office one and never share a post with it: how many SEOUL SCREENS the day's number-one film was on, this date, against the same date five and ten years ago. Each label is a YEAR, then a colon, then the film title (the renderer bolds the year), and each value is a screen count, so the opener MUST say the figures are screens in Seoul and that the years are the same date (e.g. "Screens for Seoul's most-watched film, the same date", "What the most-watched film was playing on"). ⚠️ Say MOST-WATCHED, never "top" or "number one" on their own: the value on this card is a count of SCREENS, so an unqualified "top film" reads as top BY screens and makes the card circular. The ranking is by admissions, and MUST NOT settle on one wording. ⚠️ The lines are a SEQUENCE, newest first, and are never reordered: they are years, not a ranking. ⚠️ Titles and years are printed exactly as given: never translate a title, never drop a year. Every line gets an emoji or the card loses them all, as with the other film card. Never say cinemas grew, shrank, recovered or collapsed, never mention the pandemic, and never explain the change: three numbers and their years are the whole card, and the reader is better at drawing the conclusion than you are.
- Keep the opener neutral (a time or place framing), EXCEPT on a world post, where it must name the metric as described above. Pick one from OPENERS, or write a short neutral one (max ~5 words) — it must NOT give away or hint at the pairing. Provide it in English and Korean.
- You may lightly reword an English label for wit, but keep its meaning and DO NOT put any digit in a label.
- Translate every chosen label to natural Korean (labels only — never restate the number in the label).
- Emoji: give "opener_emoji" one topic emoji that fits the whole set. For each pick, give an "emoji" ONLY where an obvious, tasteful one exists (a food, a shop, a place, a clear object). Leave "emoji" as "" for abstract lines (shares, rates, counts of people, air readings) — a forced emoji looks worse than none. One emoji each, the same emoji works for both languages, and never repeat the opener_emoji on a line — the opener already said it. NEVER use a number/keycap emoji (0-9, #) — numbers only ever come from the data.
- Avoid the ids in AVOID_IDS.

Return ONLY JSON:
{"opener_en":"...","opener_ko":"...","opener_emoji":"<one emoji or ''>","note":"one line: what the juxtaposition is","picks":[{"id":"<pool id>","label_en":"<optional reword or copy>","label_ko":"<korean label>","emoji":"<one emoji or ''>"}]}
"""


# --- cross-vein collisions -------------------------------------------------
# The sharpest Harper's-Index juxtaposition is a coincidence: two figures from
# UNRELATED datasets that land on nearly the same number, so the reader does a
# double-take. Within a single vein the pairs are only ever the two ends of one
# distribution (dearest vs cheapest flat) — a range, not a collision. This pass
# scans the whole pool for two facts from DIFFERENT veins whose magnitudes
# match, and hands the selector that pair as a sanctioned exception to the
# never-mix rule. Safety lives in fact(): only 'won' and 'people' facts carry a
# `num`, so a ₩ figure is never set against a head-count and people never
# against a count of things. The detector reads `num` but never posts it — the
# published value is still value_en, so Python owns every number as before.

# Two figures "collide" when their magnitudes are within this fraction of the
# larger. Looser than the 2% a within-vein dead heat uses: across veins the
# surprise is that two unrelated things are even the same size, so a rough
# match still reads as coincidence rather than sameness.
CROSS_HEAT_MAX = 0.15
# At most this many collisions are offered to the selector, closest first. It
# builds at most one post around one of them; the rest are there so a slow-
# moving dataset does not surface the identical pair every single day.
CROSS_MAX = 6


def cross_vein_pairs(pool):
    """Cross-vein 'dead heat' collisions: pairs of facts from different
    categories whose magnitudes nearly match, closest first. Advisory input for
    the selector (see SELECT_PROMPT) — it references the two ids like any other
    pick, and compose() already credits every vein a post touches, so no
    special handling is needed downstream."""
    elig = [f for f in pool if f.get('unit') in ('won', 'people')
            and isinstance(f.get('num'), (int, float)) and f['num'] > 0]
    found = []
    for i in range(len(elig)):
        for j in range(i + 1, len(elig)):
            a, b = elig[i], elig[j]
            if a['cat'] == b['cat'] or a['unit'] != b['unit']:
                continue
            hi = max(a['num'], b['num'])
            gap = (hi - min(a['num'], b['num'])) / hi
            if gap <= CROSS_HEAT_MAX:
                found.append((gap, a, b))
    found.sort(key=lambda t: t[0])
    out = []
    for gap, a, b in found[:CROSS_MAX]:
        out.append({
            'unit': a['unit'],
            'a': a['id'], 'a_cat': a['cat'],
            'a_label': a['label_en'], 'a_value': a['value_en'],
            'b': b['id'], 'b_cat': b['cat'],
            'b_label': b['label_en'], 'b_value': b['value_en'],
        })
    return out


def apply_cooldown(pool, state, stamp_key, cat, days, label):
    """Drop `cat` from the pool if its last post is younger than `days`.

    Shared by the world and spending veins, which both get reached for far more
    often than their share of the pool warrants. Two deliberate refusals to
    fire: an unrecoverable or missing stamp means no cooldown, and the filter is
    abandoned if it would leave fewer than 5 facts. A guard should never be the
    thing that empties the pool and skips a post.

    TypeError is caught alongside ValueError because a timezone-NAIVE stamp
    (a hand-edited state file, or one written before the stamps carried an
    offset) is unparseable against an aware now() and would otherwise raise
    mid-run and skip the post. main() writes aware stamps, so this is the
    unhappy path only.
    """
    stamp = state.get(stamp_key)
    if not stamp:
        return pool
    try:
        age = datetime.now(timezone.utc) - datetime.fromisoformat(stamp)
    except (ValueError, TypeError):
        return pool
    if age >= timedelta(days=days):
        return pool
    cooled = [f for f in pool if f['cat'] != cat]
    if len(cooled) < 5:
        return pool
    hours = int(age.total_seconds() // 3600)
    print(f'{label} on cooldown ({hours}h of {days * 24}h) - '
          f'{len(pool) - len(cooled)} facts withheld.')
    return cooled


def promote_starved(pool, state):
    """Give a long-unposted vein one card to itself (see STARVE_DAYS).

    The cooldowns hold back veins the selector over-reaches for; this is the
    same mechanism aimed at the ones it never reaches for at all. When a vein
    has gone STARVE_DAYS without being a card's primary category, the pool is
    narrowed to that vein alone, so the selector's preference for a juicy
    cross-vein pair cannot outvote it.

    Never-posted veins go first, then the longest-neglected. Returns
    (pool, promoted_cat), with promoted_cat None when nothing is promoted and
    the pool handed back untouched.
    """
    # Two promotions running would put the feed on rails. last_cat is the record
    # of what the previous card WAS, so comparing it with the cat we promoted
    # last time needs no second flag to fall out of step with it (a duplicate
    # flag is exactly what put two spotlights back to back on 22 Jul 2026).
    if state.get('last_cat') and state.get('last_cat') == state.get('last_promoted_cat'):
        return pool, None

    counts = collections.Counter(f['cat'] for f in pool)
    seen = state.get('cat_last_at') or {}
    now = datetime.now(timezone.utc)
    starved = []
    for cat, n in counts.items():
        if n < STARVE_MIN_FACTS:
            continue
        stamp = seen.get(cat)
        age = None                          # None = never posted at all
        if stamp:
            try:
                age = now - datetime.fromisoformat(stamp)
            except (ValueError, TypeError):
                age = None
        if age is None or age >= timedelta(days=STARVE_DAYS):
            starved.append((age, cat, n))
    if not starved:
        return pool, None

    # Never-posted first; then oldest-seen first.
    starved.sort(key=lambda t: (t[0] is not None,
                                -(t[0].total_seconds() if t[0] else 0)))
    age, cat, n = starved[0]
    waited = 'never posted' if age is None else f'{age.days}d since last card'
    others = ', '.join(c for _, c, _ in starved[1:]) or 'none'
    print(f'Vein floor: promoting {cat} ({waited}); this card is built from its '
          f'{n} facts alone. Also starved: {others}.')
    return [f for f in pool if f['cat'] == cat], cat


def select(pool, state):
    avoid = state.get('recent_ids', [])[-RECENT_IDS_KEEP:]
    slim = [{'id': f['id'], 'cat': f['cat'], 'label_en': f['label_en'],
             'value_en': f['value_en'], 'estimated': f['estimated'], 'pair': f['pair']}
            for f in pool]
    pairs = {}
    for f in pool:
        if f['pair']:
            pairs.setdefault(f['pair'], []).append(f['id'])
    payload = {'POOL': slim, 'PAIRS': pairs,
               'CROSS_PAIRS': cross_vein_pairs(pool),
               'OPENERS': [list(o) for o in OPENERS], 'AVOID_IDS': avoid}
    prompt = SELECT_PROMPT + '\n\n' + json.dumps(payload, ensure_ascii=False)
    attempts = 4
    attempt = 0
    limit_waited = False
    while True:
        last = attempt == attempts - 1
        try:
            r = subprocess.run(['claude', '-p', '--model', CLAUDE_MODEL, prompt],
                               capture_output=True, text=True, env=claude_env(),
                               timeout=CLAUDE_TIMEOUT)
        except subprocess.TimeoutExpired:
            if last:
                raise RuntimeError(
                    f'claude -p timed out after {CLAUDE_TIMEOUT}s, {attempts} times')
            attempt += 1
            continue
        if r.returncode != 0:
            err = (r.stderr or r.stdout or '').strip() or '(no output)'
            # ⚠️ A spent quota is checked BEFORE the backoff below, and its
            # retry deliberately does not count as an attempt. The backoff is
            # 5s, 10s and 15s: against a quota that clears in hours it burns
            # all four attempts in half a minute and loses the post, which is
            # how Old Seoul lost its 9 p.m. post on 20 August 2026.
            if limit_guard.is_usage_limit(err) and not limit_waited:
                limit_waited = True
                if limit_guard.wait_for_reset(err):
                    continue
                # Exit 0 rather than raise: a spent quota is not this bot's
                # fault, and a run that skips itself is still caught by
                # bot_health_check.py, which alerts when last_success_at goes
                # over 26 hours old.
                sys.exit(0)
            # A transient network blip at fire time (EHOSTUNREACH etc.) surfaces
            # as a nonzero exit here — back off and retry rather than crashing
            # the run and skipping the post.
            if last:
                raise RuntimeError(f'claude -p failed (exit {r.returncode}): {err}')
            time.sleep(5 * (attempt + 1))
            attempt += 1
            continue
        text = re.sub(r'^```[a-z]*\n?|\n?```$', '', r.stdout.strip()).strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            if last:
                raise RuntimeError(f'claude -p returned invalid JSON: {text[:200]!r}')
            attempt += 1
            continue


def card_signature(picks):
    """A card's identity as posted: its set of line ids, order-independent.
    Order is not part of it because compose() sorts the lines by magnitude
    anyway, so the same three facts in a different order are the same card."""
    return sorted(p['id'] for p in picks if p.get('id'))


def select_fresh(pool, state, strict=True):
    """select(), but reject a card that repeats a recent one and ask again.

    On a rejection the offending ids are dropped from the pool before
    reselecting, which is what forces a genuinely different card rather than a
    reshuffle of the same lines. Gives up after SELECT_RETRIES and posts the
    last answer: a slightly repetitive card is better than no post.

    strict=False falls back to rejecting only verbatim repeats. It is for a
    promoted vein (see promote_starved), where the pool has deliberately been
    narrowed to one small vein and the overlap rule would reject every card it
    can possibly build.
    """
    recent = [set(s) for s in (state.get('recent_cards') or [])]
    banned, sel = set(), None
    for attempt in range(SELECT_RETRIES):
        sub = [f for f in pool if f['id'] not in banned] if banned else pool
        if len(sub) < 3:                    # nothing left to build a card from
            sub = pool
        sel = select(sub, state)
        sig = set(card_signature(sel.get('picks', [])))
        if not sig:
            return sel                      # malformed; downstream will handle it
        limit = CARD_OVERLAP_MAX if strict else len(sig) - 1
        worst = max((len(sig & prev) for prev in recent), default=0)
        if worst <= limit:
            return sel
        kind = 'overlaps' if strict else 'repeats'
        print(f'Reselecting: card {kind} a recent one ({worst} of '
              f'{len(sig)} lines shared, max {limit}); '
              f'attempt {attempt + 1} of {SELECT_RETRIES}.')
        banned |= sig
    print('Repeat guard gave up; posting the last selection.')
    return sel


def clean_label(label, fallback, value):
    """Accept Claude's label unless it restates the statistic. A label may carry a
    date or year (e.g. 'on 3 Aug'), but if it contains the VALUE's digits it means
    Claude injected the number into the label — reject and use the pool's own
    label so the only source of numbers stays Python."""
    if not label or not label.strip():
        return fallback
    ldigits = re.sub(r'\D', '', label)
    vdigits = re.sub(r'\D', '', value)
    if vdigits and vdigits in ldigits:
        return fallback
    return label.strip()


OPENER_MAX = 56


def clean_opener(text, fallback):
    """Openers carry no statistic, so only sanitise length; a year is fine.

    ⚠️ Trim on a word boundary. The cap used to be a hard slice at 48, which
    cut mid-phrase and shipped the result: the screens card went out reading
    "Screens for Seoul's most-watched film, the same :" on 23 Aug 2026, four
    characters short of "date" and with the comma left dangling. A truncated
    opener is worse than a long one, because it reads as a bug rather than as
    a choice, and nothing was watching for it.

    The cap itself is real (the title wraps and a runaway opener unbalances the
    card), so it stays, a little higher: this vein's opener has to name the
    metric, the city and the comparison, which 48 could not hold.
    """
    if not text or not text.strip():
        return fallback
    s = text.strip()
    if len(s) <= OPENER_MAX:
        return s
    cut = s[:OPENER_MAX]
    if ' ' in cut:
        cut = cut[:cut.rindex(' ')]
    return cut.rstrip(' ,;:·-') or fallback


def _valid_emoji(s):
    """Return a single tasteful emoji if `s` is one, else ''. The card design
    lets the selector tag lines with an emoji, but numbers must stay Python's
    alone: reject anything carrying a digit or a keycap (0-9, #, *) so a figure
    can never reach a post through an emoji. Also reject non-emoji text so a
    stray label word can't slip in."""
    if not s or not s.strip():
        return ''
    s = s.strip()
    if any(ch.isdigit() for ch in s):
        return ''
    cps = [ord(ch) for ch in s]
    if 0x20E3 in cps or ord('#') in cps or ord('*') in cps or len(cps) > 8:
        return ''

    def emoji_ish(o):
        return (0x1F000 <= o <= 0x1FAFF or 0x2600 <= o <= 0x27BF or
                0x2B00 <= o <= 0x2BFF or 0x2190 <= o <= 0x21FF or
                0x1F1E6 <= o <= 0x1F1FF or 0x1F3FB <= o <= 0x1F3FF or
                o in (0x200D, 0xFE0F, 0x2122, 0x2139, 0x203C, 0x2049))

    def pictograph(o):
        return (0x1F000 <= o <= 0x1FAFF or 0x2600 <= o <= 0x27BF or
                0x2B00 <= o <= 0x2BFF or 0x1F1E6 <= o <= 0x1F1FF)

    if not all(emoji_ish(o) for o in cps) or not any(pictograph(o) for o in cps):
        return ''
    return s


def complete_boxoffice(picks, pool):
    """A box office card carries the day's top four films, or it is not one.

    The vein offers exactly four and the guidance asks for all four, but a
    selector that returns three would produce a card with a hole in its
    ranking, which a reader takes for the ranking: that is the fault the
    "of the day's five most-watched" footnote was invented to admit to, and it
    is better prevented than admitted.

    Own-vein cards only. A cross pair legitimately puts one or two films beside
    another vein's line, and completing the chart there would wreck the pairing.
    A film added back here carries no emoji, so even_out_emoji strips the rest:
    a complete card with no emoji, rather than a partial one that looks styled.
    """
    by_id = {f['id']: f for f in pool}
    if not picks or any(by_id[p['id']]['cat'] != 'boxoffice' for p in picks):
        return picks
    have = {p['id'] for p in picks}
    return picks + [{'id': f['id'], 'emoji': ''} for f in pool
                    if f['cat'] == 'boxoffice' and f['id'] not in have]


def even_out_emoji(lines, cats):
    """All the films carry an emoji, or none of them do.

    The prompt tells the selector to tag a line ONLY where an obvious emoji
    exists, which is right for a mixed card: a spending card can carry ☕ beside
    an abstract share with nothing. A box office card is not mixed. Every line
    is the same KIND of thing, a film, so an emoji on three of four reads as an
    oversight rather than a judgement, and that is exactly what the second live
    preview looked like: 🕷 Spider-Man, 👻 Insidious, 🕵️ Conan, and The Odyssey
    bare at the top.

    Deterministic rather than another sentence in the prompt: the selector is
    being asked for a judgement per line, and consistency across lines is not a
    judgement, it is a rule. Applied per category so a genuine mixed card is
    left alone.
    """
    for cat in EMOJI_ALL_OR_NONE & cats:
        ours = [l for l in lines if l.get('cat') == cat]
        if ours and not all(l['emoji'] for l in ours):
            for l in ours:
                l['emoji'] = ''
    return lines


def strip_emoji(text):
    """`text` with emoji removed, for use as image ALT text.

    Emoji earn their place on the card and in the plaintext fallback post, but
    in alt text they are announced aloud by name, putting a spoken glyph in
    front of the content on the opener and on every tagged line. Uses the same
    ranges _valid_emoji() accepts, so what the selector can put in is exactly
    what comes back out, and tidies the space each one leaves behind.
    """
    def is_emoji(ch):
        o = ord(ch)
        return (0x1F000 <= o <= 0x1FAFF or 0x2600 <= o <= 0x27BF or
                0x2B00 <= o <= 0x2BFF or 0x1F1E6 <= o <= 0x1F1FF or
                0x1F3FB <= o <= 0x1F3FF or o in (0x200D, 0xFE0F))

    out = ''.join('' if is_emoji(ch) else ch for ch in text)
    # Collapse the gap a stripped leading emoji leaves, per line, without
    # touching the blank lines that separate the card's blocks.
    return '\n'.join(ln.strip() if ln.strip() else ln for ln in out.split('\n'))


def _sortkey(value_en):
    """(unit_class, magnitude) for a formatted value, or None if unparseable.
    Lets compose() order a post's lines by size, but only among lines that share
    a unit (so a ₩ post sorts, a mixed count+% narrative post is left alone).

    ⚠️ A trailing imperial conversion is stripped first. '26.5°C (80°F)' does
    not match the unit pattern below — the parenthetical contains digits — so
    without this every temperature and speed card silently stopped sorting the
    moment conversions were added."""
    s = re.sub(r'\s*\([^)]*\)\s*$', '', value_en.strip())
    if s.startswith('₩'):
        num, mult = s[1:], 1.0
        for suf, m in (('tn', 1e12), ('bn', 1e9), ('m', 1e6)):
            if num.endswith(suf):
                num, mult = num[:-len(suf)], m
                break
        try:
            return ('won', float(num.replace(',', '')) * mult)
        except ValueError:
            return None
    if s.endswith('%'):
        try:
            return ('pct', float(s[:-1]))
        except ValueError:
            return None
    if 'µg' in s:
        try:
            return ('air', float(s.split()[0]))
        except ValueError:
            return None
    # A number with a trailing unit ('46m²', '2.2°C', '3,611/km²'): sortable
    # against other lines carrying the SAME unit, which is what a world post is.
    m = re.fullmatch(r'([\d,]+(?:\.\d+)?)\s*(\D+)', s)
    if m:
        try:
            return (f'u:{m.group(2).strip()}', float(m.group(1).replace(',', '')))
        except ValueError:
            return None
    try:
        return ('num', float(s.replace(',', '')))
    except ValueError:
        return None


# --- label de-duplication --------------------------------------------------

# Words that must not be left stranded at the end of a trimmed English label.
_EN_DANGLERS = {'in', 'at', 'on', 'of', 'for', 'to', 'per', 'the', 'a', 'an',
                'from', 'by', 'with', 'and', 'who', 'that',
                # Prepositions that take a place, and so sit immediately before
                # the word an opener saying "Seoul" invites us to trim. Without
                # them "Air-quality monitors reporting live across Seoul"
                # trims to "...reporting live across", which strands the
                # preposition exactly as the docstring says it must not.
                'across', 'around', 'near', 'within', 'throughout', 'outside',
                'inside', 'between', 'over', 'under', 'into', 'about'}


def _common_run(seqs, from_end):
    """Length of the longest run of identical tokens shared by EVERY sequence,
    counted from the start or the end. Never consumes a whole sequence, so every
    label keeps at least one token."""
    n = 0
    while all(len(s) > n for s in seqs):
        pos = -1 - n if from_end else n
        if len({s[pos] for s in seqs}) != 1:
            break
        n += 1
    return n


def _opener_covers(tokens, opener):
    """True if the opener already says all of `tokens`. When it does, repeating
    them on the lines is pure redundancy and the run can go from the first line
    too; when it doesn't, the run stays on the first line so the framing is
    stated once rather than lost."""
    if not tokens:
        return False
    have = set(re.sub(r'[^\w\s]', ' ', opener.lower()).split())
    return all(re.sub(r'\W', '', t.lower()) in have for t in tokens)


def dedupe_labels(labels, opener, korean=False):
    """Trim wording that every label in a post repeats, so the card reads the way
    a Harper's Index does: the metric is named once, and each later line carries
    only what actually differs.

        Estimated crowd in Jamsil right now     Estimated crowd in Jamsil
        Estimated crowd in Hongdae right now -> In Hongdae
        Estimated crowd at the Yeouido riverbank right now
                                                At the Yeouido riverbank

    Both the leading and the trailing shared run are dropped from every line but
    the first. A run the OPENER already carries ("right now" under the opener
    "Seoul, right now") is dropped from the first line as well, since the reader
    has just read it. Nothing that appears nowhere else is ever discarded.

    English is head-initial, so the metric leads and the time frame trails;
    Korean is head-final, so the two swap. The rule is symmetric, so the same
    code handles both: `korean` only suppresses re-capitalisation.

    Returns the labels untouched whenever there is nothing safe to trim."""
    if len(labels) < 3:
        return labels
    toks = [l.split() for l in labels]
    shortest = min(len(t) for t in toks)
    n_pre = _common_run(toks, from_end=False)
    # Leave at least one token that belongs to the line itself.
    n_suf = min(_common_run(toks, from_end=True), shortest - n_pre - 1)
    n_suf = max(n_suf, 0)
    # A trim that strands a preposition ("Coffee shops in") is worse than the
    # repetition it removes, so drop that end rather than mangle the line.
    if n_suf and not korean:
        if any(t[-1 - n_suf].lower().strip(',') in _EN_DANGLERS for t in toks):
            n_suf = 0
    pre, suf = toks[0][:n_pre], (toks[0][len(toks[0]) - n_suf:] if n_suf else [])
    first_drops_pre = _opener_covers(pre, opener)
    first_drops_suf = _opener_covers(suf, opener)

    out = []
    for i, t in enumerate(toks):
        cut_pre = n_pre if (i or first_drops_pre) else 0
        cut_suf = n_suf if (i or first_drops_suf) else 0
        rest = t[cut_pre:len(t) - cut_suf]
        if not rest:                      # nothing left to say — keep the original
            out = list(labels)
            break
        s = ' '.join(rest)
        if cut_pre and not korean and s[:1].islower():
            s = s[0].upper() + s[1:]
        out.append(s)
    # Runs the whole post shares are gone; the first line may still echo the
    # opener on its own (nothing shared it, so nothing above caught it).
    out[0] = _drop_opener_echo(out[0], opener, korean)
    return out


def _drop_opener_echo(label, opener, korean):
    """Trim the framing off the one line that still carries it.

    When the selector has already written the later lines bare, there is no run
    shared by every line for dedupe_labels() to catch, and the first line keeps
    its full pool label: "Estimated crowd in Myeongdong right now" under the
    opener "Seoul, right now". Strip the longest run of framing words the opener
    already says, from the end in English and the start in Korean. Refuses any
    trim that would strand a preposition or empty the label."""
    t = label.split()
    n = 0
    while n < len(t) - 1 and _opener_covers([t[-1 - n] if not korean else t[n]], opener):
        n += 1
    if not n:
        return label
    rest = t[:len(t) - n] if not korean else t[n:]
    if not korean and rest[-1].lower().strip(',') in _EN_DANGLERS:
        return label
    return ' '.join(rest)


# --- cross-pair grouping ---------------------------------------------------

# Veins counted live, "right now". When one shares a card with a single dated
# vein (a month's visitors, a quarter's spending), the card reads two time-
# frames at once. Rather than fly the dated month as a lone masthead dateline
# while the live line dates itself inline, the card splits into two labelled
# groups — the dated month over its lines, "Right now" over the live ones. The
# masthead dateline stays the design for single-frame dated cards. See compose().
LIVE_CATS = {'crowd', 'air', 'bike', 'traffic'}
# Veins that carry a liftable month/quarter period (they set a dateline). Same
# four the dateline logic promotes; used early, before scope is built, to spot a
# groupable live+dated cross pair while ordering the lines.
DATED_PERIOD_CATS = {'tourism', 'property', 'spending', 'avgbill', 'boxoffice',
                     'rush'}
# Veins whose lines are BARE LABELS ("60s", "2019") explained by a DESCRIPTOR
# rather than by a date. On an own post the opener carries that meaning, so this
# is only a reminder in the footnote — but a cross pair OVERRIDES "own post,
# never mixed" (see SELECT_PROMPT) and takes the opener generic, and then the
# descriptor is the only thing on the card saying what is counted.
# ⚠️ Single source of truth: the scope section in compose() reads these same
# strings. Adding a vein here without its scope entry, or the reverse, puts the
# words in one place and not the other.
DESCRIPTOR_SCOPES = {
    'library': ('Members of Seoul Library', '서울도서관 등록 회원'),
    'complaint': ('Reports to Seoul, complete years', '서울시 접수 신고, 연도별'),
}
# Non-live veins carrying a scope of their own, by either route. When one of
# these crosses with a live vein the card reads two frames at once, so the scope
# heads its OWN group instead of flying as a masthead over the whole card: a
# masthead is a claim about every line under it, and "Members of Seoul Library"
# over a live crowd line is simply false. The four period veins added here
# (infant, daynight, water, price) were doing exactly that — their period lifted
# to the masthead because nothing marked them groupable.
SCOPED_CATS = (DATED_PERIOD_CATS | set(DESCRIPTOR_SCOPES)
               | {'infant', 'daynight', 'water', 'price', 'books'})


def _strip_live_frame(label, korean):
    """Drop the framing a "Right now" subhead makes redundant on a live line, plus
    the "Estimated"/"추정" the KT-estimated footnote already carries. English is
    head-initial ("Estimated crowd in the Seongsu cafe strip right now" ->
    "Crowd in the Seongsu cafe strip"); Korean is head-final, so the same words
    lead ("지금 성수동 카페거리 추정 인구" -> "성수동 카페거리 인구"). Guards against
    stranding a preposition or emptying the label; recapitalises English."""
    t = label.split()
    if not t:
        return label
    if korean:
        keep = [w for w in t if w not in {'지금', '추정'}]
        return ' '.join(keep) if keep else label
    while len(t) > 1 and t[0].lower().strip(',') == 'estimated':
        t = t[1:]
    while len(t) > 1 and t[-1].lower().strip(',') in {'right', 'now'} \
            and t[-1].lower().strip(',') not in _EN_DANGLERS:
        t = t[:-1]
    if not t:
        return label
    s = ' '.join(t)
    return s[0].upper() + s[1:] if s[:1].islower() else s


def unsaid_metrics(opener, metrics):
    """Those of `metrics` the opener does not already say.

    The world and nation source lines name the metric because their labels are
    bare place names — "Seoul, South Korea, United States" measures nothing on
    its own, so something has to say what the figures are. That was written
    before SELECT_PROMPT required those openers to name the metric themselves,
    and once both did it, the same phrase went out twice one post apart:

        card    ## People per square kilometre
        reply   Source: data.worldbank.org · World Bank · People per square kilometre

    which is precisely the duplication the KT-estimate caveat was moved off the
    source reply to avoid (whatever the card says, the reply must not repeat).
    Naming only what the opener left out keeps the safety net for a vague or
    reworded opener without repeating a good one. Compared on letters and digits
    alone, so punctuation, case and the opener's emoji do not defeat the match;
    each language is judged against its own opener.
    """
    def norm(s):
        return re.sub(r'[^0-9a-z가-힣]+', '', (s or '').lower())

    op = norm(opener)
    return [m for m in metrics if norm(m) and norm(m) not in op]


# The same model that writes the labels, asked a different question. A separate
# focused call is the point rather than a stronger model: the selector is
# optimising for wit and arrangement across a whole card, and a reader asked
# only "does this label still say what the figure is" catches what that one
# does not stop to look at.
CHECK_MODEL = CLAUDE_MODEL


def _ask_json(prompt, model=CLAUDE_MODEL):
    """One `claude -p` call returning a JSON object. Raises on any failure.

    Deliberately thinner than the selector\'s own call, which retries four
    times, waits out a spent quota and exits 0 rather than lose the post. This
    one is for the CHECK, where every failure means the same thing: the card
    goes out unchecked, as every card did before the check existed. Waiting
    hours for a quota to clear so a second opinion can be had would turn a
    best-effort check into the thing that delayed the post.
    """
    r = subprocess.run(['claude', '-p', '--model', model, prompt],
                       capture_output=True, text=True, env=claude_env(),
                       timeout=CLAUDE_TIMEOUT)
    if r.returncode != 0:
        raise RuntimeError(((r.stderr or r.stdout or '').strip() or
                            '(no output)')[:200])
    text = re.sub(r'^```[a-z]*\n?|\n?```$', '', r.stdout.strip()).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r'\{.*\}', text, re.S)
        if m:
            return json.loads(m.group(0))
        raise RuntimeError(f'invalid JSON: {text[:150]}')


def _label_check_prompt(rows, opener_en, opener_ko):
    body = '\n'.join(
        f'{i}. category={r["cat"]}\n'
        f'   source label: {r["pool_en"]}\n'
        f'   published EN: {r["label_en"]}\n'
        f'   published KO: {r["label_ko"]}\n'
        f'   value: {r["value_en"]}'
        for i, r in enumerate(rows))
    return (
        f'You are checking the labels on one card of "Seoul by the numbers", a '
        f'Bluesky account in the style of Harper\'s Index, BEFORE it is '
        f'published. Each line is a bare "Label: value".\n\n'
        f'Python owns every number, so the values are correct by construction '
        f'and are NOT what you are checking. A model wrote the published '
        f'labels, by rewording the source label for wit and translating it into '
        f'Korean. Nothing else checks that work.\n\n'
        f'English opener: {opener_en}\n'
        f'Korean opener: {opener_ko}\n\n'
        f'LINES:\n{body}\n\n'
        f'Report a problem ONLY in these three cases:\n'
        f'- the published English no longer says what the SOURCE label says: a '
        f'qualifier dropped or changed, or what is counted quietly widened or '
        f'narrowed. One library reported as the city\'s libraries, people '
        f'present in a district reported as its residents, Seoul\'s figures '
        f'reported as the country\'s\n'
        f'- the Korean says something different from the published English\n'
        f'- read WITH THE OPENER, the label leaves its number meaning nothing '
        f'or meaning something else. The opener is supposed to carry the '
        f'metric, so "Teens: 10,921" is right under "Members of Seoul Library" '
        f'and empty under "Seoul by the numbers". ⚠️ Judge each line on its '
        f'own: a card may name the metric on one line and leave the rest bare, '
        f'and the lines are re-sorted by value afterwards, so a reader can '
        f'meet a bare one first and must not be left guessing\n\n'
        f'NEVER report:\n'
        f'- a Korean label that drops a unit conversion: the English carries °F '
        f'and $, the Korean does not, by design\n'
        f'- style, tone, wit, word choice, or a translation you would have '
        f'phrased differently. A label reworded for wit is the house style and '
        f'is only a problem if the wit changed the meaning\n'
        f'- the arrangement, which lines were chosen, or anything about the '
        f'numbers\n'
        f'- a label shorter than its source label. Brevity is the format\n\n'
        f'If you are unsure, PASS it. A false alarm costs the card its wit and '
        f'a card goes out three times a day.\n\n'
        f'Return JSON only, listing ONLY the lines with a problem:\n'
        f'{{"problems": [{{"i": <line number>, "lang": "en"|"ko", '
        f'"problem": "<one short sentence>"}}]}}')


def check_labels(lines, rows, opener_en, opener_ko, log=print):
    """Check the written labels against the pool\'s own before the card is drawn.

    Repairs in place: a flagged label falls back to the source label the fact
    was built with, which is the one thing on the card guaranteed to say what
    its number is. That fallback already existed for a label that injected a
    digit (see clean_label); this widens what can send a label back to it.

    ⚠️ A Korean flag falls back to the ENGLISH source label, which is what the
    card already does when the selector returns no Korean at all. A line of
    English on a Korean card reads oddly; a Korean line saying the figures were
    concluded when the source says they were filed is wrong, and wrong is worse.

    ⚠️ A check that cannot run is NOT a failure. The card goes out unchecked,
    exactly as every card did before this existed, and the fallback is recorded
    so a checker broken for a week is visible in the log rather than silently
    absent.
    """
    if not rows:
        return
    try:
        out = _ask_json(_label_check_prompt(rows, opener_en, opener_ko),
                        model=CHECK_MODEL)
    except Exception as exc:                # noqa: BLE001
        log(f'  (label check did not run: {exc})')
        _log_labels(rows, opener_en, [], error=str(exc))
        return
    problems = [q for q in (out.get('problems') or [])
                if isinstance(q, dict) and isinstance(q.get('i'), int)
                and 0 <= q['i'] < len(rows)]
    for q in problems:
        i, lang = q['i'], ('ko' if q.get('lang') == 'ko' else 'en')
        row, line = rows[i], lines[i]
        log(f'  !! label {i} ({lang}) failed the check: {q.get("problem")}')
        if row['pin']:
            # A pinned label was never the model\'s to reword, so a flag on one
            # is the checker misreading the card, not a label to repair.
            log('     (pinned label — left alone)')
            continue
        line[f'label_{lang}'] = row['pool_en']
        log(f'     -> falling back to: {row["pool_en"]}')
    _log_labels(rows, opener_en, problems)


def _log_labels(rows, opener, problems, error=''):
    """One line per card checked, carrying every label and every verdict."""
    rec = {
        'at': datetime.now(timezone.utc).astimezone().strftime('%Y-%m-%d %H:%M:%S'),
        'opener': opener,
        'labels': [{'cat': r['cat'], 'pool': r['pool_en'], 'en': r['label_en'],
                    'ko': r['label_ko']} for r in rows],
        'problems': problems,
        'error': error,
        'dry': DRY_RUN,
    }
    try:
        with LABEL_LOG.open('a', encoding='utf-8') as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + '\n')
    except OSError as exc:
        print(f'(label log failed: {exc})')
    _observe_labels(problems, error)


def _observe_labels(problems, error):
    """Tell the estate\'s shared log, so a check firing three weeks running
    reads as one recurring condition rather than three unrelated events.

    ⚠️ Dry runs report nothing: a test filing itself with the weekly review as
    a rejected label is a fault invented by the reporting of it.
    """
    if DRY_RUN or not OBSERVE.exists():
        return
    if error:
        kind, key = 'finding', 'seoul-index-label-check-unavailable'
        text = f'label check did not run: {error[:120]}'
    elif problems:
        kind, key = 'finding', 'seoul-index-label-rejected'
        text = (f'{len(problems)} label(s) fell back to the source wording: '
                f'{problems[0].get("problem", "")[:100]}')
    else:
        kind, key = 'ok', 'seoul-index-label-ok'
        text = 'card labels checked, none rejected'
    try:
        subprocess.run(
            ['python3', str(OBSERVE), 'add', '--source', 'seoul-index-labels',
             '--kind', kind, '--key', key, '--quiet', text],
            capture_output=True, timeout=20)
    except Exception:                       # noqa: BLE001
        pass


def compose(sel, pool):
    by_id = {f['id']: f for f in pool}
    picks = [p for p in sel.get('picks', []) if p.get('id') in by_id]
    picks = complete_boxoffice(picks, pool)
    if len(picks) < 3:
        raise RuntimeError(f'selector returned too few valid picks: {len(picks)}')
    spotlight = any(by_id[p['id']]['cat'] == 'spotlight' for p in picks)
    precats = {by_id[p['id']]['cat'] for p in picks}
    # A live "right now" vein beside a scoped one reads two frames at once, so it
    # groups: the scoped lines first under their own subhead (their month, or the
    # descriptor that says what they count), live lines under "Right now".
    # Whether a head can actually be resolved is settled later, once the dateline
    # logic has run; grouped is confirmed there. This early flag only steers the
    # ordering below.
    maybe_grouped = (not spotlight and bool(precats & LIVE_CATS)
                     and bool(precats & SCOPED_CATS))

    def _val(p):
        k = _sortkey(by_id[p['id']]['value_en'])
        return k[1] if k else 0.0

    # A spotlight card is one place read along a clock — now, then the usual for
    # this hour, then the hours ahead. Sorting that by size would scramble the
    # sequence into nonsense, so it keeps the harvester's order instead.
    if spotlight or precats & ORDERED_CATS:
        order = {f['id']: i for i, f in enumerate(pool)}
        picks = sorted(picks, key=lambda p: order.get(p['id'], 0))
    elif maybe_grouped:
        # Dated group first, live group second; each largest-first, so the two
        # near-equal values that make the cross pair land either side of the
        # divide and sit next to each other across it.
        picks = sorted(picks, key=lambda p: (
            by_id[p['id']]['cat'] in LIVE_CATS, -_val(p)))
    else:
        # Order the lines by value, largest first, but only when every line shares a
        # unit (an all-₩ or all-% post). Mixed-unit posts (e.g. a national post's two
        # population counts then a share %) keep the selector's narrative order.
        keys = [_sortkey(by_id[p['id']]['value_en']) for p in picks]
        if all(k is not None for k in keys) and len({k[0] for k in keys}) == 1:
            picks = [p for _, p in sorted(zip(keys, picks),
                                          key=lambda kp: kp[0][1], reverse=True)]
    lines, used, cats, estimated, forecast = [], [], set(), False, False
    for p in picks:
        f = by_id[p['id']]
        label_en = (f['label_en'] if f.get('pin')
                    else clean_label(p.get('label_en'), f['label_en'], f['value_en']))
        # A fact that ships its own Korean label keeps it: those labels carry
        # clock times, and a time is a number Python does not hand over.
        label_ko = (f['label_ko'] if f.get('label_ko')
                    else clean_label(p.get('label_ko'), f['label_en'], f['value_ko']))
        lines.append({'emoji': _valid_emoji(p.get('emoji')),
                      'label_en': label_en, 'label_ko': label_ko,
                      'value_en': f['value_en'], 'value_ko': f['value_ko'],
                      'live': f['cat'] in LIVE_CATS, 'cat': f['cat'],
                      'pin': bool(f.get('pin') or f.get('label_ko'))})
        used.append(f['id'])
        cats.add(f['cat'])
        estimated = estimated or f['estimated']
        forecast = forecast or f.get('forecast')

    even_out_emoji(lines, cats)

    opener_en = clean_opener(sel.get('opener_en'), 'Seoul by the numbers')
    opener_ko = clean_opener(sel.get('opener_ko'), '숫자로 보는 서울')
    opener_emoji = _valid_emoji(sel.get('opener_emoji'))

    # The transport card mixes two systems - a bus total and subway stations -
    # so a single transit emoji chosen by the selector can contradict the top
    # line, e.g. a metro glyph sitting above a bus figure. When the first line
    # is a transport fact, match the opener emoji to THAT line's mode instead of
    # trusting the selector: subway, bus, or a generic car as the catch-all.
    # picks[0] is the first line (lines are built from picks in order below).
    first_fact = by_id[picks[0]['id']]
    if first_fact['cat'] == 'transport':
        fid = first_fact['id']
        if fid.startswith('sub'):
            opener_emoji = '🚇'
        elif fid.startswith('bus'):
            opener_emoji = '🚌'
        else:
            opener_emoji = '🚗'

    # The river card names its two subjects in the order the card shows them.
    # These lines are sorted hottest-first (see the same-unit sort above), so on
    # a hot day the air leads and an opener reading "Water and air" contradicts
    # the arrangement printed directly beneath it. Which of the five readings is
    # warmest is a fact about the data, not a writing choice, so it is settled
    # here rather than left to the selector — whose river opener is discarded.
    # Requested 23 August 2026, after the noon card led with the air at 31.3°C.
    if first_fact['cat'] == 'river':
        air_first = first_fact['id'] == 'river_air'
        opener_en = ('Air and water in Seoul' if air_first
                     else 'Water and air in Seoul')
        opener_ko = ('서울의 공기와 물' if air_first
                     else '서울의 물과 공기')

    # Say the shared part once: the selector is asked for bare labels, but it
    # often copies a pool label verbatim onto every line, so trim deterministically
    # rather than trust the prompt. PINNED labels are exempt: a pin declares the
    # wording load-bearing, and trimming a shared "June" off "Hottest day,
    # June 1976" turned a month's record into a claim about the whole year.
    if maybe_grouped:
        # The group subheads, not the opener, carry the framing on a grouped card:
        # "Right now" over the live lines lets each shed its "right now" (and the
        # "Estimated" the footnote already owns), while the dated lines keep their
        # wording so the two "Visitors to ..." reads stay parallel under the month.
        for l in lines:
            if l['live'] and not l['pin']:
                for lang, ko in (('en', False), ('ko', True)):
                    l[f'label_{lang}'] = _strip_live_frame(l[f'label_{lang}'], ko)
    else:
        for lang, ko in (('en', False), ('ko', True)):
            opener = opener_en if lang == 'en' else opener_ko
            trimmed = dedupe_labels([l[f'label_{lang}'] for l in lines], opener, korean=ko)
            for l, t in zip(lines, trimmed):
                if not l['pin']:
                    l[f'label_{lang}'] = t

    # Check the written labels against the pool's own, LAST: after the trim,
    # after _strip_live_frame and after the river and transport openers are
    # rewritten, so what is checked is the wording the card will actually draw
    # rather than a draft of it. See check_labels.
    if CHECK_LABELS:
        check_labels(
            lines,
            [{'cat': by_id[q['id']]['cat'],
              'pool_en': by_id[q['id']]['label_en'],
              'label_en': l['label_en'], 'label_ko': l['label_ko'],
              'value_en': l['value_en'], 'pin': l['pin']}
             for q, l in zip(picks, lines)],
            opener_en, opener_ko)

    # Source line credits every distinct source used. Seoul Open Data covers
    # everything except the KOSIS 'national' figures, which get their own credit.
    # Categories whose figures come from a publisher other than Seoul Open
    # Data; anything outside this set is credited to data.seoul.go.kr.
    # 'books' is deliberately NOT here: since 22 August 2026 the loan counts come
    # from Seoul's own portal (SeoulLibraryBookRentNumInfo), not data4library.
    non_seoul = {'national', 'world', 'nation', 'property', 'weather', 'airport',
                 'health', 'culture', 'tourism', 'level', 'boxoffice',
                 'boxhist'}
    uses_seoul = any(c not in non_seoul for c in cats)
    uses_kosis = 'national' in cats
    # The library "1 in N" divides by KOSIS's registered population, so a card
    # carrying the ratio credits KOSIS exactly as a national card does. Guarded
    # on LIBRARY_POP rather than on the category, because a KOSIS outage leaves
    # the same library lines on the card with no ratio and nothing to credit.
    lib_ratio = 'library' in cats and bool(LIBRARY_POP['en'])
    uses_oecd = 'world' in cats
    uses_wb = 'nation' in cats
    uses_molit = 'property' in cats
    # The river vein spans two publishers: the water is Seoul Open Data
    # (so 'river' stays out of non_seoul above) and the air is KMA.
    uses_kma = 'weather' in cats or 'river' in cats
    uses_kac = 'airport' in cats
    uses_hira = 'health' in cats
    uses_mcst = 'culture' in cats
    uses_tour = 'tourism' in cats
    uses_books = 'books' in cats
    uses_kobis = bool({'boxoffice', 'boxhist'} & cats)
    srcs = (['data.seoul.go.kr'] if uses_seoul else []) + \
           (['kosis.kr'] if uses_kosis or lib_ratio else []) + \
           ([OECD_DOMAIN] if uses_oecd else []) + \
           (['rt.molit.go.kr'] if uses_molit else []) + \
           (['data.kma.go.kr'] if uses_kma else []) + \
           (['airport.co.kr'] if uses_kac else []) + \
           (['opendata.hira.or.kr'] if uses_hira else []) + \
           (['mcst.go.kr'] if uses_mcst else []) + \
           (['know.tour.go.kr'] if uses_tour else []) + \
           (['hrfco.go.kr'] if 'level' in cats else []) + \
           (['kobis.or.kr'] if uses_kobis else []) + \
           ([WB_DOMAIN] if uses_wb else [])
    if not srcs:
        srcs = ['data.seoul.go.kr']
    joined = ', '.join(srcs)
    label = 'Sources' if len(srcs) > 1 else 'Source'
    src_en, src_ko = f'{label}: {joined}', f'출처: {joined}'
    # Which dataset, and from when, are keys to the figures rather than credits
    # for them, so they belong on the card beside the numbers - the same split
    # the OECD branch below already makes with its metro-area scope. Only the
    # credit stays in the reply, where the domain can be a real clickable link.
    # Scope entries qualify the figures ("which dataset, from when") and ride the
    # card, not the source reply. Each is a (descriptor, period) pair: the four
    # month/quarter veins carry a real date as their period, everything else has
    # period None. When exactly ONE datable period covers the whole card, it is
    # lifted to a dateline under the title (masthead-style) and the footnote keeps
    # only the descriptor; otherwise the period stays inline as "<desc>, <period>".
    # Live veins (crowds, bikes) carry no period and date themselves in their own
    # labels ("right now"), so a card pairing a live line with a dated vein still
    # shows the dated month up top while the live line reads against it.
    scope_en, scope_ko = [], []
    # Metric to surface on the card face (masthead subtitle) for bare-place-name
    # cards (nation) when the opener did not name it. Empty means the opener
    # already said it, so nothing is added. Set by the uses_wb branch below and
    # lifted onto the dateline once the period/grouping logic has settled.
    card_metric_en = card_metric_ko = ''
    if ('spending' in cats or 'avgbill' in cats) and SALES_Q['en']:
        scope_en.append(('Commercial districts', SALES_Q['en']))
        scope_ko.append(('상권', SALES_Q['ko']))
    if uses_kosis or lib_ratio:
        src_en += ' · Statistics Korea'
        src_ko += ' · 통계청'
    if uses_kosis:
        years = sorted({by_id[p['id']].get('year') for p in picks
                        if by_id[p['id']]['cat'] == 'national' and by_id[p['id']].get('year')})
        if years:
            scope_en.append((f'{"/".join(years)} figures', None))
            scope_ko.append((f'{"/".join(years)}년 자료', None))
    if uses_molit:
        # Same split as KOSIS: the ministry is the credit, the filing month is
        # a key to the figures and rides on the card footnote.
        src_en += ' · MOLIT'
        src_ko += ' · 국토교통부'
        if MOLIT_M['en']:
            scope_en.append(('Apartment filings', MOLIT_M['en']))
            scope_ko.append(('아파트 실거래 신고', MOLIT_M['ko']))
    if uses_kma:
        # Which station the readings come from is a key to the figures, so
        # it rides on the card; the labels already carry their months.
        src_en += ' · KMA'
        src_ko += ' · 기상청'
        # ⚠️ The two KMA veins read DIFFERENT instruments, and saying so wrongly
        # is worse than not saying it. 'weather' is ASOS station 108, Seoul's
        # reference station, daily. 'river' is 초단기실황, the representative AWS
        # value for the 종로구 예보구역, hourly. Crediting the river card to 108
        # would name an instrument that did not take the reading — which the
        # first version of this vein did, on 21 Aug 2026, until the card was
        # actually read.
        if 'weather' in cats:
            scope_en.append(('Official Seoul station (108)', None))
            scope_ko.append(('서울 대표 관측소(108) 기준', None))
        if 'river' in cats:
            # What kind of thing an Anyangcheon IS. The labels are bare names by
            # design (the opener owns the metric), which leaves an English
            # reader five temperatures and no idea that three of them are
            # waterways feeding the river they have heard of — the same gap the
            # water card closes by naming 정수센터 on its dateline, and the same
            # reasoning as the README's "labels lead with what the number
            # means". The river card cannot use its dateline for this: the
            # reading hour is already there.
            #
            # "Tributaries", not "streams" or "rivers": it is accurate for all
            # three (Seoul's own English calls them Streams, but Wikipedia has
            # the Anyangcheon as a river), and it is the more useful word, since
            # what makes the card worth reading is that these feed the Han.
            #
            # Built from the lines actually on THIS card, never from the station
            # table: a station under maintenance drops out of the card, and a
            # footnote naming a river the reader cannot see is worse than none.
            # Placed above the air caveat because the footnote joins in append
            # order and this one explains three lines rather than one.
            tribs = [by_id[p['id']] for p in picks
                     if by_id[p['id']]['id'].startswith('river_watt_')
                     and by_id[p['id']]['id'] != f'river_watt_{HAN_STATION}']
            if tribs:
                names = [f['label_en'].removeprefix('The ') for f in tribs]
                lead = (' and '.join([', '.join(names[:-1]), names[-1]])
                        if len(names) > 1 else names[0])
                verb = 'are tributaries' if len(names) > 1 else 'is a tributary'
                scope_en.append((f'The {lead} {verb} of the Han', None))
                # Every 천 ends in a consonant, so the topic particle is 은
                # whether the list is one name or three.
                scope_ko.append(
                    ('·'.join(f['label_ko'] for f in tribs) + '은 한강 지류',
                     None))
            scope_en.append(('Air: Seoul forecast-zone reading', None))
            scope_ko.append(('기온: 서울 예보구역 관측값', None))
    if 'level' in cats:
        # ⚠️ These are 홍수특보 tiers, NOT the level at which the walkway goes
        # under. The card must never say the bridge is closed or submerged.
        src_en += ' · HRFCO'
        src_ko += ' · 한강홍수통제소'
        scope_en.append(('Flood-warning tiers at this gauge', LEVEL_PERIOD['en']))
        scope_ko.append(('이 지점의 홍수특보 기준수위', LEVEL_PERIOD['ko']))
    if 'rush' in cats and RUSH_M['en']:
        # ⚠️ Without this the card says "City Hall, 6 p.m.: 347,582" under a
        # month dateline, which reads as one evening and is out by about
        # thirtyfold. The dateline says WHICH month; only this says that each
        # figure is the whole of it.
        scope_en.append(('Boardings in that hour, summed over the month',
                         RUSH_M['en']))
        scope_ko.append(('해당 시간대 승차 인원, 한 달 합계', RUSH_M['ko']))
    if uses_kac:
        src_en += ' · Korea Airports Corporation'
        src_ko += ' · 한국공항공사'
    if uses_hira:
        # Both provisos are keys to the figures: the region is where the
        # institution is, and the counts are insurance claims.
        src_en += ' · HIRA'
        src_ko += ' · 건강보험심사평가원'
        if HEALTH_Y['y']:
            scope_en.append((f'Insured patients at Seoul institutions, '
                             f'{HEALTH_Y["y"]}', None))
            scope_ko.append((f'서울 소재 요양기관 건강보험 환자 수, '
                             f'{HEALTH_Y["y"]}년', None))
    if uses_mcst:
        src_en += ' · MCST'
        src_ko += ' · 문화체육관광부'
        if CULTURE_Y['y']:
            scope_en.append((f'Culture-facility survey, {CULTURE_Y["y"]} figures', None))
            scope_ko.append((f'문화기반시설총람, {CULTURE_Y["y"]}년 기준', None))
    if uses_tour:
        # Paid-admission scope and the (months-old) data month are keys to
        # the figures; both ride the card.
        src_en += ' · KCTI'
        src_ko += ' · 한국문화관광연구원'
        if TOUR_M['en']:
            scope_en.append(('Paid-admission sites', TOUR_M['en']))
            scope_ko.append(('유료 관광지 입장객', TOUR_M['ko']))
    if uses_kobis:
        # The scope is doing real work here, not decoration: admissions on
        # SEOUL screens are a different number from the national ones every
        # outlet reports, and a card of bare film titles gives a reader no way
        # to know which they are looking at. The day rides as the dateline.
        src_en += ' · KOFIC'
        src_ko += ' · 영화진흥위원회'
        if 'boxhist' in cats:
            # No dateline: every line is a different year and carries its own.
            # What the footnote must supply is what the number counts, since
            # the labels are titles and the values are bare counts.
            # "Most-watched", never "top". The VALUE on this card is a screen
            # count, so "the top film" invites the reader to take the ranking
            # as a ranking by screens, which would make the card circular: the
            # film with the most screens is on 382 screens. It is ranked by
            # ADMISSIONS — verified 23 Aug 2026 over 20 sampled days, where
            # KOBIS's own rank matched the admissions order every time and the
            # sales order on 2 of 20. The Korean says 관객수 1위 for the same
            # reason: a bare 1위 does not say what it won.
            scope_en.append(('Screens showing each year’s most-watched film', None))
            scope_ko.append(('각 연도 관객수 1위 영화의 상영 스크린 수', None))
        elif BOXOFFICE_D['en']:
            # The card is the day's top four, in order, always. It said "of the
            # day's five most-watched" for an hour on 23 Aug 2026, while the
            # selector was free to choose four of five: that was honest about a
            # card with a hole in its ranking, but a hole in a ranking is a
            # worse card than a fixed one. What the vein lost is variety
            # between days, which the chart itself supplies as it moves.
            # Spelled out, as the card spells out months: "the day's 5
            # most-watched" is prose, not a figure, and the only numerals on
            # this card should be the ones being reported.
            scope_en.append((f'Seoul screens, the day’s '
                             f'{SMALL_NUMBERS_EN.get(BOXOFFICE_N, BOXOFFICE_N)} '
                             f'most-watched', BOXOFFICE_D['en']))
            # 편 is a counter and takes a space after a spelled-out numeral:
            # 다섯 편, not 다섯편, which the first render produced.
            scope_ko.append((f'서울 지역 상영, 그날 관객수 상위 '
                             f'{SMALL_NUMBERS_KO.get(BOXOFFICE_N, BOXOFFICE_N)} 편',
                             BOXOFFICE_D['ko']))
    if uses_books and BOOKS_WINDOW['days']:
        # ⚠️ The window is the whole reason this vein is publishable and it is
        # not in the API: 서울도서관 states it on its own page and the harvester
        # re-reads it every run (see seoul_index_books_harvest.py). Period slot
        # None on purpose — see BOOKS_WINDOW for why this vein has no dateline.
        #
        # ⚠️ **"3,000 most-borrowed items" is not padding: the feed is a CUT and
        # the footnote said so wrongly for one evening.** list_total_count is
        # exactly 3,000, and the record counts per loan-count are 590 at two
        # loans against 1,248 at three — a natural tail has the MOST records at
        # the lowest count, so that inversion is the list being truncated
        # part-way through the two-loan books. Everything borrowed once, and
        # most of what was borrowed twice, is missing. A footnote reading
        # "Loans at Seoul Library" therefore claimed every loan the library
        # made, and the truncation need not fall evenly across subjects, so it
        # bends the comparison between the lines as well as their totals.
        scope_en.append((f'{BOOKS_WINDOW["scope_en"]}\u2019s '
                         f'{BOOKS_WINDOW["records"]} most-borrowed '
                         f'items, last {BOOKS_WINDOW["days"]} days', None))
        scope_ko.append((f'{BOOKS_WINDOW["scope_ko"]} 대출 상위 자료 '
                         f'{BOOKS_WINDOW["records"]}건, '
                         f'최근 {BOOKS_WINDOW["days"]}일', None))
        if BOOKS_WINDOW['loans']:
            # The denominator goes ON the card, not just into the arithmetic: a
            # "1 in 3" whose total the reader cannot see is a number they cannot
            # check. "Counted", never "all", for the truncation reason above —
            # and the period slot stays None, as everywhere in this vein, which
            # carries no dateline at all.
            scope_en.append((f'Ratio is to all {BOOKS_WINDOW["loans"]} '
                             f'checkouts counted', None))
            scope_ko.append((f'집계된 대출 {BOOKS_WINDOW["loans"]}건 전체 대비',
                             None))
    if 'water' in cats and WATER_PERIOD['en']:
        # Not "Raw water drawn": that only repeats an opener already required
        # to name the metric. What the reader cannot know from the card is that
        # this is 취수 — water taken from the river BEFORE treatment — and not
        # the 송수/공급량 figures the same feed also carries.
        scope_en.append(('Raw intake, before treatment', WATER_PERIOD['en']))
        scope_ko.append(('취수량 (정수 전)', WATER_PERIOD['ko']))
    if 'daynight' in cats and DAYNIGHT_PERIOD['en']:
        # No descriptor: these facts are estimated=True, so the card already
        # carries the KT caveat, and adding "Estimated population" beside it
        # printed the same warning twice. Only the date is missing, so only
        # the date is added.
        scope_en.append((None, DAYNIGHT_PERIOD['en']))
        scope_ko.append((None, DAYNIGHT_PERIOD['ko']))
    if 'infant' in cats and INFANT_PERIOD['en']:
        # ⚠️ The AGE BAND rides here, in the dateline slot, and the opener is
        # forbidden from naming one. The lines are bare years, so nothing else
        # on the card says which of the four series it is — and when this was
        # briefly left to the opener, the selector wrote "Children aged 0 in
        # Seoul" over the under-SIX figures. Python owns which band it is,
        # exactly as it owns every value.
        scope_en.append((None, INFANT_PERIOD['en']))
        scope_ko.append((None, INFANT_PERIOD['ko']))
    # ⚠️ library: Seoul Library, not Seoul's 215 public libraries. Both of these
    # read from DESCRIPTOR_SCOPES so the words a cross pair promotes to a group
    # subhead and the words in the footnote can never be two different things.
    for _c in ('library', 'complaint'):
        if _c in cats:
            scope_en.append((DESCRIPTOR_SCOPES[_c][0], None))
            scope_ko.append((DESCRIPTOR_SCOPES[_c][1], None))
    if lib_ratio:
        # ⚠️ The population month rides in the DESCRIPTOR, not the period slot,
        # for the same reason the OECD vintage does: a period here would be the
        # card's only one, lift to the masthead dateline, and date the
        # MEMBERSHIP figures — which carry no date at all, the service
        # publishing neither a period nor a stamp.
        scope_en.append((f'Ratio is to Seoul’s registered population that age, '
                         f'{LIBRARY_POP["en"]}', None))
        scope_ko.append((f'서울 해당 연령 주민등록인구 대비, {LIBRARY_POP["ko"]}', None))
        # ⚠️ Not decoration and not a hedge: 준회원 needs no Seoul connection
        # whatever, so the members counted are NOT a subset of the population
        # divided by. Without this line the card reads as "one Seoul teen in 65
        # holds a card", which is a claim the data cannot carry. If the ratio
        # ever ships without this note, the vein is misreporting.
        scope_en.append(('Members need not live in Seoul', None))
        scope_ko.append(('회원 자격은 서울 거주자에 한정되지 않음', None))
    if 'price' in cats and PRICE_PERIOD['en']:
        # The date and the item are keys to the figures: a price means
        # nothing without what was bought and when.
        scope_en.append((f'Price of {PRICE_LABEL["en"]}', PRICE_PERIOD['en']))
        scope_ko.append((f'{PRICE_LABEL["ko"]} 가격', PRICE_PERIOD['ko']))
    if 'river' in cats and RIVER_PERIOD['en']:
        # The hour is a key to the figures, not a credit: a water
        # temperature means nothing without the hour it was read.
        # The period slot becomes the card's dateline (as with books and
        # property), which is where the hour belongs: it heads the card rather
        # than trailing it. The descriptor is left empty because a scope note
        # reading "Water and air readings" only restates the card; the caveat
        # worth the space is which instrument the air came from, added below.
        scope_en.append((None, RIVER_PERIOD['en']))
        scope_ko.append((None, RIVER_PERIOD['ko']))
    if uses_oecd:
        # Name the metric here rather than trusting the opener. The metro-area
        # scope and the year are NOT put here: they qualify the numbers rather
        # than crediting them, so they belong on the card beside the figures
        # (in the footnote), by the same reasoning as the crowd caveat. The year
        # rides in the descriptor (not the period slot), so it stays a footnote
        # caveat rather than a masthead dateline — it is a vintage, not a month.
        wf = [by_id[p['id']] for p in picks if by_id[p['id']]['cat'] == 'world']
        keys = sorted({f['id'].split('_')[1] for f in wf})
        years = sorted({f['year'] for f in wf if f.get('year')})
        met_en = ', '.join(unsaid_metrics(
            opener_en, [WORLD_METRICS[k][0] for k in keys if k in WORLD_METRICS]))
        met_ko = ', '.join(unsaid_metrics(
            opener_ko, [WORLD_METRICS[k][1] for k in keys if k in WORLD_METRICS]))
        yr = f', {"/".join(years)}' if years else ''
        # No bare "OECD" here: the domain already carries it, and the pinned
        # methodology card is where the publisher gets named in full.
        if met_en:
            src_en += f' · {met_en}'
            src_ko += f' · {met_ko}'
        scope_en.append((f'Metro areas{yr}', None))
        scope_ko.append((f'광역도시권{yr}', None))
    if uses_wb:
        # The labels are bare place names (Seoul, then whole countries), so the
        # metric must be stated somewhere or the numbers measure nothing. When the
        # opener already names it, unsaid_metrics() returns empty and we say it
        # nowhere else. When it does NOT (e.g. the generic "Seoul and the nation"
        # opener), the metric rides the card's masthead subtitle — above the rows,
        # not in the source reply — so a reader of the card sees what is measured
        # without hunting the credit line. The scope and year still qualify the
        # numbers, so they stay in the footnote ("Seoul against whole countries,
        # <year>") with a None period slot, exactly like metro.
        nf = [by_id[p['id']] for p in picks if by_id[p['id']]['cat'] == 'nation']
        keys = sorted({f['id'].split('_')[1] for f in nf})
        years = sorted({f['year'] for f in nf if f.get('year')})
        card_metric_en = ', '.join(unsaid_metrics(
            opener_en, [WB_METRICS[k][0] for k in keys if k in WB_METRICS]))
        card_metric_ko = ', '.join(unsaid_metrics(
            opener_ko, [WB_METRICS[k][1] for k in keys if k in WB_METRICS]))
        yr = f', {"/".join(years)}' if years else ''
        src_en += ' · World Bank'
        src_ko += ' · 세계은행'
        scope_en.append((f'Seoul against whole countries{yr}', None))
        scope_ko.append((f'서울 대 각국(국가 전체){yr}', None))
    # The dateline is the single datable period shared across the card. Only the
    # month/quarter veins carry one; if two dated veins disagree (a rare cross of
    # different months) no single date is true, so none is lifted and both stay
    # inline in the footnote.
    per_pairs = [(pe, pk) for (_, pe), (_, pk) in zip(scope_en, scope_ko) if pe]
    dateline_en = dateline_ko = ''
    if per_pairs and len({pe for pe, _ in per_pairs}) == 1:
        dateline_en, dateline_ko = per_pairs[0]

    # Confirm the grouped layout now the period is settled: a live+dated cross
    # pair groups only if a single month was actually lifted. When it groups, the
    # month heads its own group instead of flying as a lone masthead dateline, so
    # the masthead is suppressed (see _card_payload) and the period rides the
    # dated group's subhead. The footnote already dropped it via _scope_strs.
    grouped = maybe_grouped and bool(dateline_en)
    # The subheads a grouped card draws. A period that lifted is the head (the
    # dated case); otherwise the descriptor of the single scoped vein on the
    # card. Two scoped veins would leave no one true head, so the card simply
    # does not group and every scope stays in the footnote, as before.
    group_en, group_ko = (dateline_en, dateline_ko) if grouped else ('', '')
    if maybe_grouped and not grouped:
        descs = sorted(precats & set(DESCRIPTOR_SCOPES))
        if len(descs) == 1:
            group_en, group_ko = DESCRIPTOR_SCOPES[descs[0]]
            grouped = True

    # Bare-place-name cards (nation) with an opener that did not name the metric
    # lift it onto the masthead subtitle — under the title, above the rows. This
    # runs after the period/grouping logic so it can never be mistaken for a
    # datable period or flip `grouped`: a nation card carries no liftable period,
    # so dateline_en is empty here, and it is never a grouped cross pair.
    if card_metric_en and not grouped and not dateline_en:
        dateline_en, dateline_ko = card_metric_en, card_metric_ko

    def _scope_strs(entries, promoted):
        # A promoted period is dropped from its entry (it now rides the dateline
        # or a group subhead); a whole entry goes when the descriptor itself was
        # promoted, or the footnote repeats the subhead two lines below it.
        out = []
        for desc, per in entries:
            if promoted and desc == promoted and not per:
                continue
            out.append(f'{desc}, {per}' if per and per != promoted else desc)
        return out
    scope_en = _scope_strs(scope_en, group_en or dateline_en)
    scope_ko = _scope_strs(scope_ko, group_ko or dateline_ko)
    # NOTE: the KT-estimate caveat is deliberately NOT added to the source line.
    # It is a caveat, not a credit, and it already rides on the card footnote
    # below; putting it in both made the reply repeat what the card had just
    # said, one post above it. Anything that appears in the footnote should be
    # left off the source reply for the same reason.
    # How the crowd figures are arrived at is a caveat on the numbers themselves,
    # not a credit, so it rides on the card beside them rather than in the source
    # reply. It carries no link, so nothing is lost by taking it off the reply.
    # A spotlight card's later lines are predictions, and saying so is the whole
    # reason it is not headed "today".
    if forecast:
        note_en = 'Hours ahead are forecasts; crowds are KT-estimated'
        note_ko = '이후 시간대는 예측치 · 인구는 KT 추정'
    elif 'daynight' in cats:
        # ⚠️ NOT "crowds". These are 생활인구: everyone present in a district at
        # that hour — residents, people at work, people visiting — which is a
        # different thing from the crowd vein's estimate of a named place, and
        # from the district's registered population. Borrowing the crowd vein's
        # wording said the wrong thing about the figures.
        note_en = 'Population present, KT-estimated' if estimated else ''
        note_ko = '생활인구는 KT 추정' if estimated else ''
    else:
        note_en = 'Crowds are KT-estimated' if estimated else ''
        note_ko = '인구는 KT 추정' if estimated else ''
    # Caveat first, then scope: a warning about the numbers outranks a key to
    # them. Everything here is deliberately absent from the source reply, which
    # sits one post below and would otherwise repeat the card verbatim.
    note_en = ' · '.join([p for p in [note_en, *scope_en] if p])
    note_ko = ' · '.join([p for p in [note_ko, *scope_ko] if p])

    cat_list = [by_id[p['id']]['cat'] for p in picks]
    primary = max(set(cat_list), key=cat_list.count)

    # Plaintext bodies (opener + lines + source), used as the card's alt text and
    # as the whole post if card rendering fails. Emoji sit ahead of the label, as
    # on the card; the card's "##" markdown token is card-only decoration.
    def _pl(emoji, label, value):
        return f'{emoji} {label}: {value}' if emoji else f'{label}: {value}'
    op_en = f'{opener_emoji} {opener_en}' if opener_emoji else opener_en
    op_ko = f'{opener_emoji} {opener_ko}' if opener_emoji else opener_ko
    # The trailing Wikipedia links are real links in the posted reply, so they
    # stay out of src_* (which add_tags renders as text) and are spelled out
    # only in the plaintext body that serves as alt text and as the fallback
    # post. Spotlights set a single (prefix, anchor, url) tuple; normalise to a
    # list so tourism posts can carry one link per attraction.
    wiki_en, wiki_ko = sel.get('wiki_en'), sel.get('wiki_ko')
    wiki_en = [wiki_en] if isinstance(wiki_en, tuple) else list(wiki_en or [])
    wiki_ko = [wiki_ko] if isinstance(wiki_ko, tuple) else list(wiki_ko or [])
    if uses_tour and not wiki_en:
        # Every attraction on the card gets its article, in card (value) order.
        # 아쿠아리움 has no article (see TOUR_WIKI) and is simply skipped.
        seen = []
        for p in picks:
            f = by_id[p['id']]
            ko_name = f['id'].split('_', 1)[1]
            if f['cat'] == 'tourism' and ko_name in TOUR_WIKI \
                    and ko_name not in seen:
                seen.append(ko_name)
        for i, ko_name in enumerate(seen):
            en_anchor, en_url, ko_url = TOUR_WIKI[ko_name]
            wiki_en.append((' · Wikipedia: ' if i == 0 else ', ',
                            en_anchor, en_url))
            wiki_ko.append((' · 위키백과: ' if i == 0 else ', ',
                            ko_name, ko_url))
    tail_en = ''.join(f'{p}{a}' for p, a, _ in wiki_en)
    tail_ko = ''.join(f'{p}{a}' for p, a, _ in wiki_ko)

    # The ordered elements the card draws, per language. A grouped cross pair puts
    # a date subhead over the dated lines and a "Right now" subhead over the live
    # ones; otherwise just the rows (an ungrouped dated card flies its month as a
    # masthead dateline, passed separately). The same list feeds the alt-text twin
    # below, so card and alt text never drift.
    def _items(lang):
        rows = [{'emoji': l['emoji'], 'label': l[f'label_{lang}'],
                 'value': l[f'value_{lang}']} for l in lines]
        if not grouped:
            return rows
        scoped_head = group_en if lang == 'en' else group_ko
        live_head = 'Right now' if lang == 'en' else '지금'
        scoped = [r for r, l in zip(rows, lines) if not l['live']]
        live = [r for r, l in zip(rows, lines) if l['live']]
        return [{'subhead': scoped_head}, *scoped, {'subhead': live_head}, *live]
    items_en, items_ko = _items('en'), _items('ko')

    # curly() so the alt text / plaintext fallback matches the card, which the
    # renderer curls via _esc. A subhead item is its own bare line. The period
    # survives in the plaintext either as the masthead line under the opener
    # (ungrouped) or as the dated group's subhead (grouped) — never both.
    def _body(op, items, note, src, tail, masthead):
        parts = [it['subhead'] if 'subhead' in it
                 else _pl(it['emoji'], it['label'], it['value']) for it in items]
        return curly(op + ':\n' + (f'{masthead}\n' if masthead else '')
                     + '\n'.join(parts)
                     + (f'\n{note}' if note else '') + '\n' + src + tail)
    en_body = _body(op_en, items_en, note_en, src_en, tail_en,
                    '' if grouped else dateline_en)
    ko_body = _body(op_ko, items_ko, note_ko, src_ko, tail_ko,
                    '' if grouped else dateline_ko)

    return {
        'opener': {'emoji': opener_emoji, 'en': opener_en, 'ko': opener_ko},
        'lines': lines, 'items_en': items_en, 'items_ko': items_ko,
        'grouped': grouped, 'src_en': src_en, 'src_ko': src_ko,
        'note_en': note_en, 'note_ko': note_ko,
        'dateline_en': dateline_en, 'dateline_ko': dateline_ko,
        'wiki_en': wiki_en, 'wiki_ko': wiki_ko,
        'en_body': en_body, 'ko_body': ko_body,
        'used': used, 'cats': list(cats), 'primary': primary,
    }


LINK_DOMAINS = [('data.seoul.go.kr', 'https://data.seoul.go.kr'),
                ('kosis.kr', 'https://kosis.kr'),
                (OECD_DOMAIN, f'https://{OECD_DOMAIN}'),
                ('rt.molit.go.kr', 'https://rt.molit.go.kr'),
                ('data.kma.go.kr', 'https://data.kma.go.kr'),
                ('airport.co.kr', 'https://www.airport.co.kr'),
                ('opendata.hira.or.kr', 'https://opendata.hira.or.kr'),
                ('mcst.go.kr', 'https://www.mcst.go.kr'),
                ('know.tour.go.kr', 'https://know.tour.go.kr'),
                ('hrfco.go.kr', 'https://www.hrfco.go.kr'),
                ('kobis.or.kr', 'https://www.kobis.or.kr'),
                (WB_DOMAIN, f'https://{WB_DOMAIN}')]


def source_reply(tb, body, extra=None):
    """Build the source reply: the body with its source domains hyperlinked,
    then optional trailing links — a (prefix, anchor, url) tuple or a list of
    them, used to point at Wikipedia articles (the spotlight's place, or every
    attraction on a tourism card). No hashtags: tags ride the card posts only."""
    # Hyperlink every source domain that appears on the source line.
    hits = sorted((body.find(dom), dom, url) for dom, url in LINK_DOMAINS
                  if body.find(dom) != -1)
    pos = 0
    for i, dom, url in hits:
        if i < pos:  # a later domain nested inside an earlier match — skip
            continue
        tb.text(body[pos:i]).link(dom, url)
        pos = i + len(dom)
    tb.text(body[pos:])
    if extra:
        for prefix, anchor, url in ([extra] if isinstance(extra, tuple) else extra):
            tb.text(prefix)
            tb.link(anchor, url)
    return tb


def tag_line():
    """Tags-only caption for the card posts: just the hashtag facets, no
    headline, so the card stays visually first but the top-level post is
    still discoverable via the tags. The card posts are the only place the
    tags live — the source replies carry none."""
    tb = client_utils.TextBuilder()
    for i, (tag, label) in enumerate(TAGS):
        if i:
            tb.text(' ')
        tb.tag(f'#{tag}', label)
    return tb


# --- card rendering --------------------------------------------------------

def _card_payload(c, lang):
    """Pull the card's opener, render items, footnote and dateline for one
    language out of compose()'s output. The items are rows, optionally split by
    group subheads on a grouped cross pair (see compose()._items). On a grouped
    card the date rides a group subhead, so the masthead dateline is suppressed."""
    opener = {'emoji': c['opener']['emoji'], 'text': c['opener'][lang]}
    dateline = '' if c.get('grouped') else c.get(f'dateline_{lang}', '')
    return opener, c[f'items_{lang}'], c[f'note_{lang}'], dateline


def render_pair(c, out_dir):
    """Render the EN and KO cards into out_dir. Returns ((path,size),(path,size))."""
    en_op, en_lines, en_note, en_dl = _card_payload(c, 'en')
    ko_op, ko_lines, ko_note, ko_dl = _card_payload(c, 'ko')
    en = render_card(en_op, en_lines, Path(out_dir) / 'card_en.png', footnote=en_note,
                     dateline=en_dl)
    ko = render_card(ko_op, ko_lines, Path(out_dir) / 'card_ko.png', korean=True,
                     footnote=ko_note, dateline=ko_dl)
    return en, ko


def log_card(c, sel, primary, post_uri, handle, fallback):
    """Append one JSONL line describing the card just posted, so a week's
    output can be skimmed later to catch duds. Best-effort by design: the
    thread is already live when this runs, so a logging failure is warned
    about and swallowed, never raised."""
    try:
        rkey = post_uri.rsplit('/', 1)[-1] if post_uri else None
        url = f'https://bsky.app/profile/{handle}/post/{rkey}' if rkey else None
        rec = {
            'at': f'{datetime.now(SEOUL_TZ):%Y-%m-%d %H:%M:%S}',
            'primary': primary,
            'cats': c.get('cats', []),
            'opener': c['opener']['en'],
            'note': sel.get('note', ''),
            'lines': [{'label': l['label_en'], 'value': l['value_en']}
                      for l in c['lines']],
            'fallback': fallback,
            'url': url,
        }
        with CARD_LOG.open('a', encoding='utf-8') as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + '\n')
    except Exception as e:
        print(f'(card log failed: {e})')


def tail_cards(n):
    """Print the last n posted cards from card_history.jsonl, newest last, for
    eyeballing recent output. Read-only: no harvest, no selector, no post, no
    lock — safe to run any time, including while a scheduled post is composing."""
    if not CARD_LOG.exists():
        print(f'No card log yet at {CARD_LOG} — written after the first real post.')
        return
    recs = []
    for ln in CARD_LOG.read_text(encoding='utf-8').splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            recs.append(json.loads(ln))
        except json.JSONDecodeError:
            continue  # a torn final line from a crash mid-write is not fatal
    if not recs:
        print(f'Card log {CARD_LOG} has no readable entries yet.')
        return
    shown = recs[-n:]
    print(f'Last {len(shown)} of {len(recs)} card(s):')
    for r in shown:
        cats = ', '.join(r.get('cats', []))
        head = f'\n{r.get("at", "?")}  [{r.get("primary", "?")}'
        head += f' · {cats}]' if cats else ']'
        if r.get('fallback'):
            head += '  (plaintext fallback)'
        print(head)
        if r.get('opener'):
            print(f'  {r["opener"]}')
        for l in r.get('lines', []):
            print(f'  {l.get("label", "")}: {l.get("value", "")}')
        if r.get('note'):
            print(f'  ↳ {r["note"]}')
        if r.get('url'):
            print(f'  {r["url"]}')
    print()


# --- main ------------------------------------------------------------------

def main():
    # --tail is a read-only log viewer: print recent cards and exit before the
    # lock, config or any network — never touches state, never posts.
    if TAIL_N is not None:
        tail_cards(TAIL_N)
        return

    # One run at a time. launchd fires a slot the machine slept through as
    # soon as it wakes, which can land right beside the next scheduled
    # firing; two interleaved runs then race over the state file, and the
    # loser's stale state overwrites the winner's. The lock is held for the
    # whole run (the fd must stay referenced); a second instance bows out.
    lock = open(HERE / '.post.lock', 'w')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit('Another seoul_index_post run is in progress; bowing out.')
    # Timestamp every run: the log had none, which made the back-to-back
    # spotlight incident of 22 Jul 2026 needlessly hard to reconstruct.
    print(f'--- run at {datetime.now(SEOUL_TZ):%Y-%m-%d %H:%M:%S} KST ---')

    # Wait for the machine to have a path out before harvesting or selecting.
    # Posts fire 8:30/12:30/20:30, so half an hour of waiting still lands the
    # post and cannot collide with the next firing. Inside the lock, so a run
    # that is waiting turns its successor away rather than stacking up.
    net_guard.require_network(1800)

    config = json.loads(CONFIG.read_text())
    api_key = config['api_key']
    kosis_key = config.get('kosis_key')
    gov_key = config.get('data_go_kr_key')
    # HRFCO issues its own key (not a data.go.kr one): it is the level
    # vein's only source, and that vein is silent without it.
    hrfco_key = config.get('hrfco_api_key')
    # KOFIC issues its own key too (kobis.or.kr, not data.go.kr); the box
    # office vein is silent without it.
    kobis_key = config.get('kobis_key')
    state = json.loads(STATE.read_text()) if STATE.exists() else {}

    # Inspect the cross-vein collisions the detector finds for the live pool,
    # then exit — no selector call, no post. A deterministic look at what the
    # selector will be offered (unlike a --dry-run, whose picks the model makes).
    if SHOW_CROSS:
        pool = build_pool(api_key, state, kosis_key, gov_key, hrfco_key,
                          kobis_key)
        elig = [f for f in pool if f.get('unit')]
        print(f'{len(pool)} facts, {len(elig)} collidable:')
        for f in sorted(elig, key=lambda f: (f['unit'], -f['num'])):
            print(f'  [{f["unit"]:6}] {f["num"]:>15,.0f}  {f["cat"]:9} {f["id"]}')
        cross = cross_vein_pairs(pool)
        print(f'\n{len(cross)} cross-vein collision(s), closest first:')
        for c in cross:
            print(f'  {c["unit"]}: {c["a_label"]} ({c["a_value"]}, {c["a_cat"]})'
                  f'  ⟷  {c["b_label"]} ({c["b_value"]}, {c["b_cat"]})')
        return

    # One post in SPOTLIGHT_EVERY, on average, drills into one place instead of
    # setting places against each other, cycling through the curated spots.
    # These are interspersed with the usual index cards, not a replacement for
    # them, and a place that does not answer with enough lines simply falls
    # back to one. Chosen by coin flip, not post_n % N: a fixed cadence puts
    # the spotlight in the same clock slot every day once the posting schedule
    # is a multiple of N. A 1/(N-1) chance after each non-spotlight post, with
    # back-to-back spotlights barred, works out to the same 1-in-N long-run
    # rate without the rhythm.
    post_n = int(state.get('post_n', 0)) + 1
    state['post_n'] = post_n
    # A category never repeats, and the spotlight is a category like any
    # other. The bar reads last_cat — the record of what the previous post
    # actually WAS — not a separate boolean: a duplicate flag can fall out
    # of step with the record it shadows, and on 22 Jul 2026 it did, putting
    # two spotlights back to back.
    # ONLY_CAT suppresses the spotlight draw. --only exists to show ONE vein on
    # demand, and without this a coin flip could answer a request to see the box
    # office with a spotlight card instead: the flag looked broken rather than
    # overruled, which is how it read the first time a new vein was previewed.
    # An explicit --spotlight still wins, being the more specific instruction.
    want_spotlight = FORCE_SPOTLIGHT or (
        ONLY_CAT is None
        and state.get('last_cat') != 'spotlight'
        and random.random() < 1 / (SPOTLIGHT_EVERY - 1))
    if want_spotlight:
        i = int(state.get('spotlight_i', 0))
        spot = CROWD_SPOTS[i % len(CROWD_SPOTS)]
        facts = spotlight_facts(api_key, spot)
        if facts:
            state['spotlight_i'] = (i + 1) % len(CROWD_SPOTS)
            print(f'Spotlight post #{post_n}: {spot["en"]} ({len(facts)} lines, '
                  f'no selector call).')
            sel, pool = spotlight_sel(spot, facts), facts
        else:
            print(f'Spotlight on {spot["en"]} returned too little; normal index instead.')
            want_spotlight = False

    # last_cat, written after posting, is the single record of what this
    # post was (a spotlight that fell back to a normal card records its
    # normal category). The old last_spotlight boolean is retired.
    state.pop('last_spotlight', None)
    promoted = None                        # set by the vein floor, below

    if not want_spotlight:
        pool = build_pool(api_key, state, kosis_key, gov_key, hrfco_key,
                          kobis_key)
        if len(pool) < 5:
            sys.exit(f'Pool too small ({len(pool)} facts) — data sources may be down.')

        # Vein cooldowns (see WORLD_COOLDOWN_DAYS, SPENDING_COOLDOWN_DAYS).
        # Applied before the rotation below, so that a post held back here is
        # dropped from the running rather than merely deferred to the next post.
        pool = apply_cooldown(pool, state, 'last_world_at', 'world',
                              WORLD_COOLDOWN_DAYS, 'World')
        pool = apply_cooldown(pool, state, 'last_spending_at', 'spending',
                              SPENDING_COOLDOWN_DAYS, 'Spending')

        # The floor under the veins the selector never reaches for. Applied
        # after the cooldowns so a promoted vein is never one the cooldown has
        # just withheld, and instead of the rotation below rather than before
        # it: the promoted vein IS the card, so there is nothing to rotate away
        # from (and a promoted vein cannot be last_cat anyway — it has not
        # posted for STARVE_DAYS).
        if ONLY_CAT:
            only = [f for f in pool if f['cat'] == ONLY_CAT]
            if len(only) < 3:
                sys.exit(f'--only={ONLY_CAT}: {len(only)} fact(s) in that vein, '
                         f'need at least 3 to build a card. Pool has: '
                         f'{", ".join(sorted({f["cat"] for f in pool}))}.')
            pool, promoted = only, ONLY_CAT
        else:
            pool, promoted = promote_starved(pool, state)

        if promoted:
            print(f'Harvested {len(pool)} candidate facts (vein floor: {promoted}).')
        else:
            # Category rotation: don't lead with the same metric two posts running.
            last_cat = state.get('last_cat')
            if last_cat:
                rotated = [f for f in pool if f['cat'] != last_cat]
                if len(rotated) >= 5:
                    pool = rotated
            print(f'Harvested {len(pool)} candidate facts (rotated away from: {last_cat}).')
        sel = select_fresh(pool, state, strict=not promoted)

    c = compose(sel, pool)
    used, primary = c['used'], c['primary']

    # Each card posts as an image with NO caption, so the card sits at the very
    # top of its post; the source line + hashtags follow as their own threaded
    # reply, which keeps data.seoul.go.kr a real clickable link. The full
    # plaintext body is the card's alt text, and the whole post if rendering fails.
    en_source = source_reply(client_utils.TextBuilder(), c['src_en'], c.get('wiki_en'))
    ko_source = source_reply(client_utils.TextBuilder(), c['src_ko'], c.get('wiki_ko'))
    # The body doubles as the cards' ALT text and, if rendering fails, as the
    # plaintext post. Emoji are right for the post and wrong for the alt: a
    # screen reader announces them by name, so the opener reads out as "round
    # pushpin Seoul Forest, hour by hour" and every tagged line carries a
    # spoken glyph before its label. Strip them from the alt only; the cards
    # and the fallback post keep theirs.
    en_alt, ko_alt = strip_emoji(c['en_body']), strip_emoji(c['ko_body'])

    print(f'\nNote: {sel.get("note", "")}')
    print(f'\nEN alt / fallback ({len(en_alt)} chars):\n{"-"*46}\n{en_alt}\n{"-"*46}')
    print(f'\nKO alt / fallback ({len(ko_alt)} chars):\n{"-"*46}\n{ko_alt}\n{"-"*46}')
    print(f'\nEN source post: {en_source.build_text()!r}\nKO source post: {ko_source.build_text()!r}')

    # No length guard here. In the normal path en_alt/ko_alt ride as the cards'
    # image ALT text, which Bluesky does not length-limit (there is no maxLength
    # or maxGraphemes on the embed's alt field). Only the plaintext fallback
    # below posts the body AS a post, so the ~300-grapheme cap is enforced there
    # instead: putting it here aborted otherwise-fine card posts whenever the
    # alt text ran a little long.

    # Render both cards; any failure drops us to a plaintext thread so a post
    # never fails to go out over a rendering hiccup.
    cards = None
    try:
        out_dir = Path.cwd() if DRY_RUN else tempfile.mkdtemp()
        (en_path, en_size), (ko_path, ko_size) = render_pair(c, out_dir)
        cards = {'en': (Path(en_path).read_bytes(), en_size),
                 'ko': (Path(ko_path).read_bytes(), ko_size)}
        print(f'\nRendered cards — EN {en_size}, KO {ko_size}.')
        if not DRY_RUN:
            import shutil
            shutil.rmtree(out_dir, ignore_errors=True)
    except CardRenderError as e:
        print(f'\nCard render failed ({e}); falling back to a plaintext thread.')

    if DRY_RUN:
        if cards:
            print(f'\nCard caption (both langs): {tag_line().build_text()!r}')
            print(f'\n(dry run — wrote {out_dir}/card_en.png and card_ko.png, not posting)')
        else:
            print('\n(dry run — not posting)')
        return

    handle = config['handle']
    password = keychain_password(handle, KEYCHAIN_SERVICE)
    bsky = Client()
    bsky.login(handle, password)
    posted_uri = None
    if cards:
        (en_bytes, en_size), (ko_bytes, ko_size) = cards['en'], cards['ko']
        en_ar = models.AppBskyEmbedDefs.AspectRatio(width=en_size[0], height=en_size[1])
        ko_ar = models.AppBskyEmbedDefs.AspectRatio(width=ko_size[0], height=ko_size[1])

        def _reply(parent_ref, root_ref):
            return models.AppBskyFeedPost.ReplyRef(parent=parent_ref, root=root_ref)

        # 4-post chain: EN card → EN source → KO card → KO source. Card posts
        # carry only the hashtags (see tag_line) so the card stays visually
        # first; each source reply carries the clickable link + tags. Every
        # reply's root stays the first (EN card) post.
        p1 = bsky.send_image(text=tag_line(), image=en_bytes, image_alt=en_alt,
                             langs=['en'], image_aspect_ratio=en_ar)
        posted_uri = p1.uri
        root_ref = models.create_strong_ref(p1)
        p2 = bsky.send_post(text=en_source, reply_to=_reply(root_ref, root_ref), langs=['en'])
        p2_ref = models.create_strong_ref(p2)
        p3 = bsky.send_image(text=tag_line(), image=ko_bytes, image_alt=ko_alt,
                             reply_to=_reply(p2_ref, root_ref), langs=['ko'],
                             image_aspect_ratio=ko_ar)
        p3_ref = models.create_strong_ref(p3)
        bsky.send_post(text=ko_source, reply_to=_reply(p3_ref, root_ref), langs=['ko'])
        print('\nPosted (4-post thread: EN card, EN source, KO card, KO source).')
    else:
        # Plaintext fallback (card render failed): there are no card posts here,
        # so these full-text posts must carry the hashtags themselves — the tags
        # live on the cards in the normal path, and there is no card to ride.
        en_full = source_reply(client_utils.TextBuilder(), c['en_body'], c.get('wiki_en'))
        ko_full = source_reply(client_utils.TextBuilder(), c['ko_body'], c.get('wiki_ko'))
        for tb in (en_full, ko_full):
            if TAGS:
                tb.text('\n')
                for i, (tag, label) in enumerate(TAGS):
                    if i:
                        tb.text(' ')
                    tb.tag(f'#{tag}', label)
        # Only this fallback posts the body AS a post, so Bluesky's ~300-grapheme
        # cap applies here alone (the normal path renders the body onto a card and
        # carries it as unbounded alt text). Measured on the built post text,
        # which includes the source tail and hashtags the body string itself omits.
        en_len, ko_len = len(en_full.build_text()), len(ko_full.build_text())
        if en_len > MAX_POST_CHARS or ko_len > MAX_POST_CHARS:
            sys.exit(f'Plaintext-fallback post too long (EN {en_len}, KO {ko_len}; '
                     f'max {MAX_POST_CHARS}); card render failed. Re-run to reselect.')
        root = bsky.send_post(text=en_full, langs=['en'])
        posted_uri = root.uri
        root_ref = models.create_strong_ref(root)
        reply_ref = models.AppBskyFeedPost.ReplyRef(parent=root_ref, root=root_ref)
        bsky.send_post(text=ko_full, reply_to=reply_ref, langs=['ko'])
        print('\nPosted (English + Korean thread, plaintext fallback).')

    recent_ids = (state.get('recent_ids', []) + used)[-RECENT_IDS_KEEP:]
    state['recent_ids'] = recent_ids
    # The card's own identity, for the repeat guard (see select_fresh). Written
    # for spotlight cards too, so the same place cannot come round twice in a
    # window either.
    state['recent_cards'] = ((state.get('recent_cards') or [])
                             + [sorted(used)])[-RECENT_CARDS_KEEP:]
    # Per-vein clock for the starvation floor (see promote_starved). 'primary'
    # is the category the card was actually built on, which is what the floor
    # measures — a vein that merely rode along on a cross-pair card has not had
    # its turn.
    state['last_cat'] = primary
    state['last_success_at'] = datetime.now(timezone.utc).isoformat()
    state.setdefault('cat_last_at', {})[primary] = state['last_success_at']
    if promoted:
        state['last_promoted_cat'] = promoted
    if primary == 'world':
        state['last_world_at'] = state['last_success_at']
    if primary == 'spending':
        state['last_spending_at'] = state['last_success_at']
    if primary == 'nation':
        state['last_nation_at'] = state['last_success_at']
    write_json_atomic(STATE, state, ensure_ascii=False, indent=2)

    log_card(c, sel, primary, posted_uri, handle, fallback=cards is None)


if __name__ == '__main__':
    main()
