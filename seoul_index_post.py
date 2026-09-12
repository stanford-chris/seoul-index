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
import math
import re
import statistics
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
from seoul_index_card import (render_card, render_bus_route_map, render_station_map,
                              CardRenderError, curly, RED)

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


def reporting():
    """May this process file into the estate's shared observation log?

    ⚠️ ONLY A REAL RUN MAY, and "real" means this file was executed, not
    imported. Every `_observe_*` below already refused a `--dry-run`, and every
    one of them still filed from the TEST SUITE, because DRY_RUN reads
    `sys.argv` and a test process's argv says nothing about --dry-run. Measured
    27 August 2026: 148 `seoul-index-korean` findings in
    `memory/observations.jsonl`, six per suite run, every one of them a
    synthetic label from a fixture — and the Sunday `estate-review` reads that
    key as a fault recurring for weeks. The check whose whole job is to keep
    that log honest was the thing filling it with noise.

    ⚠️ It is checked HERE and not by each caller, because a rule every author
    has to remember is a rule the next one forgets. That is the same reasoning
    that put check_masthead in compose() rather than in the airport vein.

    ⚠️ `__name__` is the right test and cannot fail quiet in production: the
    launchd job runs this file directly (verified in
    com.chrisstanford.seoulindex.plist), and the ONLY importers are the six
    test suites. If a real caller ever imports compose() it will report nothing
    — so give it an explicit opt-in then rather than loosening this.
    """
    return __name__ == '__main__' and not DRY_RUN and OBSERVE.exists()
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
_KNOWN_ARGS = {'--dry-run', '--spotlight', '--show-cross', '--tail', '--force'}
_ONLY_PREFIX = '--only='       # --only=<cat>: build the card from one vein
_DAILY_PREFIX = '--daily='     # --daily=<cat>: --only for a vein's own launchd slot


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
                and not a.startswith(_ONLY_PREFIX)
                and not a.startswith(_DAILY_PREFIX)]
    if _unknown:
        sys.exit(f'Unknown argument(s): {" ".join(_unknown)}. '
                 f'Recognized: {" ".join(sorted(_KNOWN_ARGS))} [N], '
                 f'{_ONLY_PREFIX}<cat>, {_DAILY_PREFIX}<cat>. '
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
# --only skips its vein's own cooldown, but not a post made in the last
# ONLY_MIN_HOURS: on 10 September 2026 the 20:30 scheduled run chose the new
# stations vein by itself and a hand-run --only=stations at 21:01 put the
# identical card out again. --force overrides, for a deliberate repost.
FORCE = '--force' in sys.argv
ONLY_MIN_HOURS = 6
# --daily=<cat>: the scheduled form of --only=<cat>, for a vein that has a
# launchd slot of its own. busroutes has one at 10:30 KST since 12 September
# 2026, his call ("I want to make this a daily post"): with the vein in the
# rotation alone it posted twice in three days and then sat behind a 3-day
# cooldown and six debuts. Same one-vein card as --only; the differences are
# what an UNATTENDED run needs. A day the feed has not advanced since the
# last post exits 0 and says so rather than reposting the same day's card
# (daily_already_posted); a withheld or thin vein exits 0 with its reason,
# where a hand run gets exit 1, so a stalled feed is not a red job in the
# morning digest. The rotation never picks a daily vein by itself: every post
# through here refreshes the vein's own cooldown stamp, so the four scheduled
# runs always find it inside its cooldown.
DAILY_CAT = next((a[len(_DAILY_PREFIX):] for a in sys.argv
                  if a.startswith(_DAILY_PREFIX)), None) or None
if DAILY_CAT:
    ONLY_CAT = DAILY_CAT
DAILY = DAILY_CAT is not None
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
# vein for one card and the selector has nothing else to choose. Promotions do
# not land back to back, so a burst of starved veins airs over alternating posts
# rather than putting the feed on rails for two days: exceptions are a vein that
# has never led a card at all, and a vein stuck past SEVERE_STARVE_DAYS (see
# below), both of which may run on. See promote_starved.
STARVE_DAYS = 5
# A promoted vein must be able to fill a card on its own. 'air' had only 2 facts
# until 11 September 2026 and so could never be promoted — it could only ride
# along on someone else's card. It carries 4 now (see air_facts).
STARVE_MIN_FACTS = 3

# Openers Python writes when a card is ONE of these veins and nothing else,
# applied in main() beside the rush card's. The ranked cards carry theirs in
# RANKED_CARD_INFO instead, since those change with the day.
FIXED_OPENERS = {'air': ('Seoul’s air quality, right now', '지금 서울의 대기질')}

# The back-to-back bar in promote_starved trades a hard per-vein guarantee for
# an even-looking feed: with the roster now at 30 veins, the floor's own
# throughput (at most one promotion every two posts, once no debut is waiting)
# is arithmetically short of what a strict 5-day ceiling for every vein would
# need, so some veins queue behind others even when the mechanism is working
# exactly as designed. Measured 2 Sep 2026: price and water both sat at 11 days
# since their one and only post, more than double STARVE_DAYS, with the guard
# still refusing to run two promotions in a row because nothing was making its
# FIRST debut that day. SEVERE_STARVE_DAYS is the second, narrower exception:
# once the worst-waiting vein has gone this long, the bar lifts for it alone,
# the same way it already lifts for an undebuted vein. It is deliberately an
# AGE test on the single most-overdue vein, not a queue-depth test — a depth
# test ("N veins are currently starved") was tried in reasoning and rejected
# right above promote_starved's back_to_back check, because with 26+ veins
# sharing a 5-day floor, most of them sit starved most of the time by simple
# arithmetic, which would make a depth trigger permanently true and the guard
# dead code. An age test does not have that failure mode: promoting the worst
# vein resets its own age to zero, so the trigger is self-correcting rather
# than standing open once tripped.
SEVERE_STARVE_DAYS = STARVE_DAYS * 2

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
# 'busroutes' is busiest/2nd-busiest/quietest THEN the total — a ranking with
# its sum tacked on the end, not four comparable magnitudes. Value-sorted, the
# total (in the millions) would jump to the top of a card whose whole point is
# the top-to-bottom rank of the three routes above it.
ORDERED_CATS = {'level', 'complaint', 'infant', 'boxhist', 'busroutes', 'stations',
                'busmovers', 'nightbus', 'busweekend', 'busstops', 'railstations', 'seoulstation',
                'stationgap', 'wxday', 'rescue', 'kopis', 'kepco', 'kepcohouse', 'railcommuter'}

# Every vein's lines are all-or-nothing on emoji, not just a chosen few: a
# partial set reads as an oversight rather than a judgement, whatever the
# category. See even_out_emoji. Started as a per-vein allowlist (boxoffice,
# boxhist, then 'culture' on 28 Aug 2026, after a museum card mixed a
# flag/museum emoji with bare visitor-count lines — the user's call, on the
# 3mu3yywrhdj2x post) and generalised to every category on 31 Aug 2026,
# also the user's call. A genuinely mixed CARD is still fine — a cross-vein
# pair post can show one vein's lines all tagged and the other vein's lines
# all bare — this only forces consistency WITHIN a single vein's own lines.

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
# behavior when a place answers with too few lines.
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

# Bike, traffic and transport get the same cooldown for the same reason as
# spending, just never named until a card_history.jsonl audit on 31 Aug 2026
# found it: each vein's facts() function hands the selector essentially one
# fixed structural pairing every single call, so whenever the category is
# reached for at all, it is nearly always the same card. bike_facts() always
# pairs bike_avail/bike_racks as 'bike_stock' and bike_stations/bike_empty as
# 'bike_reach' — there is no other bike comparison to draw. traffic_facts()
# draws from the same curated road list (traffic_links.json) every run, so
# "fastest vs slowest" keeps landing on the same two roads. transport_facts()
# always pairs sub_total/bus_total as 'modes'. The audit found bike identical
# in substance on 5 of 5 posts since 5 Aug, traffic on 3 of 3 since 30 Jul and
# transport on 3 of 3 since 28 Jul — this is what spending looked like before
# 17 Aug. Same fix, same 3-day value.
BIKE_COOLDOWN_DAYS = 3
TRAFFIC_COOLDOWN_DAYS = 3
TRANSPORT_COOLDOWN_DAYS = 3

# Tourism gets the same cooldown for the same frozen-pair reason as
# bike/traffic/transport above: tour_facts() pre-detects the closest-matched
# pair of attractions each month, and against a small, fixed roster of
# paid-admission venues that closest pair does not move around much. The
# aquarium-against-Lotte-World-Tower "dead heat" shipped seven times between
# 1 August and 9 September 2026 (bot-scout, 9 Sep) — a real near-tie in the
# data, reported as a fresh surprise on every run that reached for it.
TOURISM_COOLDOWN_DAYS = 3

# The infrastructure counts (bus stops, car parks, libraries, parks, wifi,
# cultural spaces: count_facts) are registry sizes, and a registry barely
# moves. The card posted 18 Aug, 30 Aug and 10 Sep 2026 and read 11,239 /
# 11,239 / 11,236 bus stops and 2,189 / — / 2,186 car parks: three posts,
# one figure. His call, 11 Sep 2026: once a month or so is enough.
INFRA_COOLDOWN_DAYS = 30

# Same reasoning again: the busiest bus route is Route 143 on every one of
# the 10 days measured 9 Sep 2026 (never close, usually 15-30% ahead of
# whoever's second), so busroutes() hands back the same top line almost
# every time it is offered. The two- and three-line ranks below it are the
# part that actually varies day to day.
BUSROUTES_COOLDOWN_DAYS = 3
# Veins held out of the pool entirely, by his instruction, until he decides:
# busroutes from 10 to 11 September 2026 (the per-stop rebuild, previewed
# for a day and released once the layout was settled: measure on the
# dateline, bare rows, stop counts in the footnote). Applied after the
# cooldowns and BEFORE the vein floor, so a held vein can be neither selected
# nor promoted as starved. A hand-run --only=<cat> still shows it (with
# --dry-run for a preview, or --force past the six-hour guard), since that is
# how a decision gets made. Empty the set to release a vein; nothing else
# needs touching. Empty since 11 September 2026.
HELD_CATS = set()   # kepcohouse and railcommuter held for his look and released 12 Sep 2026
# And once more for the station card: 서울역 was the busiest station on every
# one of the 7 days measured 10 Sep 2026 (122k-150k, summed across its five
# platforms' rows), Jamsil or Hongik Univ. second.
STATIONS_COOLDOWN_DAYS = 3
# And the bus stops card, for the same reason: 홍대입구역 (stop 14015) was the
# busiest stop on all four days measured, 4-7 September 2026.
BUSSTOPS_COOLDOWN_DAYS = 3
# And the rail stations card: Seoul Station will lead it every day.
RAILSTATIONS_COOLDOWN_DAYS = 3

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


# Categories that ever post a won_en() value. compose() reads this to decide
# whether a card needs the "$1 ≈ ₩N" footnote at all.
WON_CATS = {'price', 'spending', 'avgbill', 'property', 'healthcost', 'kopis', 'kepco', 'kepcohist',
            'kepcohouse'}

# Set once per run by refresh_usd_rate(), read by compose() for the footnote.
# ⚠️ Deliberately a single rate on the FOOTNOTE, not a per-value "(~$18.3M)"
# suffix on every won_en() line. That was the first design, and it was
# rejected on measurement, not on taste: the flagship property line, "Most
# paid for an apartment (Yongsan-gu)  ₩25.0bn", is already 47 characters —
# adding "(~$18.3M)" pushes the row past the ~53-character budget documented
# above transport_facts() and wraps the exact line most worth anchoring. One
# rate converts every won figure on the card without touching any of their
# widths, at the cost of asking the reader to do the multiplication once.
USD_RATE = {'rate': None}


def refresh_usd_rate(state):
    """KRW->USD from the European Central Bank's daily reference rate, via
    api.frankfurter.dev (no key, no signup). Cached once per KST calendar day
    in state, exactly as transport_cache/molit_agg cache their own daily
    lookups, since the bot posts several times a day and the rate does not.

    ⚠️ The project's old domain, api.frankfurter.app, now 301-redirects here —
    verified 28 August 2026 — and http_get_json() shells out to curl with no
    -L, so it silently receives Cloudflare's redirect HTML, fails to parse it
    as JSON, retries twice more the same way, then raises. Point at the
    canonical api.frankfurter.dev directly rather than relying on a redirect
    that itself could one day stop resolving.

    ECB publishes on weekdays only, so this can be serving Friday's rate on a
    Sunday — no worse than the KMA weather lines already do, and won->dollar
    movement over a couple of days is well inside the "$1 ≈ ₩N" footnote's
    own rounding. On a failed fetch, yesterday's cached rate is reused rather
    than dropped: a rate is an approximate anchor, never a precise claim, so a
    day or two of extra staleness costs far less than losing the anchor
    outright for a whole run. Only a HARD failure (nothing ever cached, and
    today's fetch also failed) leaves USD_RATE empty, which compose() reads as
    "say nothing" rather than showing a wrong figure — same rule as every
    other unmakeable check in this file."""
    today = datetime.now(SEOUL_TZ).strftime('%Y-%m-%d')
    cache = state.get('usd_rate_cache') or {}
    if cache.get('fetched') == today and cache.get('rate'):
        USD_RATE['rate'] = cache['rate']
        return
    try:
        d = http_get_json('https://api.frankfurter.dev/v1/latest?from=KRW&to=USD')
        rate = float(d['rates']['USD'])
        # Sanity floor/ceiling: KRW/USD has sat between roughly 1,000 and
        # 1,600 won per dollar for two decades. A schema change or a bad
        # response would otherwise post a nonsense conversion with total
        # confidence.
        if not (1 / 2000 < rate < 1 / 500):
            raise ValueError(f'implausible KRW->USD rate: {rate}')
    # RuntimeError is http_get_json()'s own "all 3 retries failed" — caught
    # here alongside a malformed or short-of-schema response, so a network
    # blip and a changed API shape both fall back the same safe way.
    except (RuntimeError, OSError, ValueError, KeyError, TypeError) as e:
        print(f'Warning: USD rate fetch failed ({e})'
              + (' — reusing cached rate.' if cache.get('rate')
                 else ' — won lines carry no $ footnote this run.'))
        USD_RATE['rate'] = cache.get('rate')
        return
    state['usd_rate_cache'] = {'fetched': today, 'rate': rate,
                               'ecb_date': d.get('date')}
    USD_RATE['rate'] = rate


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
    Official names are not romanizations (홍대입구 is 'Hongik Univ.', 시청 is
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
         num=None, unit=None, head_en=None, head_ko=None,
         period_en=None, period_ko=None, place_en=None, place_ko=None):
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
    (flights, filings, museums stay un-collidable, i.e. unit left None).

    `head_*` + `period_*` split a then-and-now label into the metric and the
    period it covers ("Days of 33°C (91°F) or more" / "Summer 1976"). BOTH
    halves stay inside `label_en`, which remains the whole self-describing
    string: it is what the selector is shown, what check_labels judges and what
    a card falls back to. The split is an ADDITION the card may use to draw the
    metric once as a group subhead with the periods bolded beneath it — see
    _metric_groups() in compose(). Set them only on a fact that genuinely has a
    sibling differing in nothing but the period, or the subhead is a heading
    over one row."""
    return {'id': fid, 'cat': cat, 'label_en': label_en, 'value_en': value_en,
            'value_ko': value_ko, 'estimated': estimated, 'pair': pair,
            'year': year, 'forecast': forecast, 'label_ko': label_ko,
            'pin': pin, 'num': num, 'unit': unit,
            'head_en': head_en, 'head_ko': head_ko,
            'period_en': period_en, 'period_ko': period_ko,
            'place_en': place_en, 'place_ko': place_ko}


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
            got.append({'en': en, 'ko': spot.get('ko') or area, 'mid': mid,
                        'visitor': r['NON_RESNT_PPLTN_RATE'],
                        'female': r['FEMALE_PPLTN_RATE'],
                        'twenties': r['PPLTN_RATE_20']})
        except (RuntimeError, KeyError, IndexError, ValueError):
            continue
    facts = []
    for g in got:
        # ⚠️ PINNED, and that is the change of 26 August 2026. The selector used
        # to reword this line per row, and on a four-place card that produced
        # three different sentences saying one thing: "Estimated crowd, Gangnam
        # Station", "Estimated crowd in Seoul Station right now", "Estimated
        # crowd at Nodeul Island the same minute". Variety is usually an
        # improvement and here it is pure noise: every row is the same metric,
        # the only real difference is the place, and the rewording buried it in
        # the middle of a sentence of a different length each time — the last
        # row wrapped because of it. One shape down the card also lets the place
        # bold (see place_en), which cannot work while the surrounding wording
        # moves. The place goes LAST so the four names line up.
        # ⚠️ The KOREAN label is pinned too, and it has to be. `pin` covers
        # English alone; with label_ko left None the selector translates each
        # row on its own and the Korean card gets the four different shapes
        # this change exists to remove — so the KO twin would keep both faults,
        # the wrap and the unfindable place, while the EN card was fixed. The
        # wording is the model's own, lifted from the 24 August card
        # (강남역 추정 인파); Korean is head-final, so the place leads.
        facts.append(fact(f'crowd_{g["en"]}', 'crowd',
                          f'Estimated crowd, {g["en"]}',
                          grouped(g['mid']), grouped(g['mid']), estimated=True,
                          num=g['mid'], unit='people', pin=True,
                          label_ko=f'{g["ko"]} 추정 인파',
                          place_en=g['en'], place_ko=g['ko']))
        facts.append(fact(f'visitor_{g["en"]}', 'crowd',
                          f'Estimated share in {g["en"]} who don’t live there',
                          f'{g["visitor"]}%', f'{g["visitor"]}%', estimated=True,
                          place_en=g['en'], place_ko=g['ko']))
        # The place is carried STRIPPED here, exactly as the label renders it:
        # these two say "the Han riverside crowd", not "the the Han riverside".
        facts.append(fact(f'twenties_{g["en"]}', 'crowd',
                          f'Share of the {g["en"].removeprefix("the ")} crowd in their twenties',
                          f'{g["twenties"]}%', f'{g["twenties"]}%', estimated=True,
                          place_en=g['en'].removeprefix('the '),
                          place_ko=g['ko']))
        facts.append(fact(f'female_{g["en"]}', 'crowd',
                          f'Women’s share of the {g["en"].removeprefix("the ")} crowd',
                          f'{g["female"]}%', f'{g["female"]}%', estimated=True,
                          place_en=g['en'].removeprefix('the '),
                          place_ko=g['ko']))
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


def air_readings(api_key):
    """[(district, pm25)] for every reporting monitor, or None.

    ⚠️ Extracted 27 August 2026 so air_facts() and the hourly archive read the
    endpoint through one function. Same reason as bike_counts: two copies of the
    field names drift, and FPM in particular is easy to get wrong.

    ⚠️ FPM is PM2.5, NOT PM10. The service documents 미세먼지(PM-10), 오존,
    이산화질소, 일산화탄소, 아황산가스; OZON/NTDX/CBMX/SPDX take four of those,
    leaving PM as the documented PM-10 and FPM as the fine fraction. PM >= FPM in
    23 of 25 districts and CRST_SBSTN names "PM-2.5" outright.

    ⚠️ The station key is MSRSTN_NM. MSRSTE_NM and SAREA_NM are tried first for
    historical reasons and BOTH are absent from the live response — measured
    27 August 2026, when reading them alone collapsed all 25 districts to one
    null key and made a 373-byte record look like 61.

    ⚠️ None, not [], on a failed read: a partial city and a clean one must not
    look alike to an archive.
    """
    rows = air_rows(api_key)
    if rows is None:
        return None
    vals = [(x.get('MSRSTE_NM') or x.get('SAREA_NM') or x.get('MSRSTN_NM'),
             float(x['FPM'])) for x in rows
            if str(x.get('FPM', '')).replace('.', '', 1).isdigit()]
    return vals or None


def air_rows(api_key):
    """The 25 district rows as served, or None on a failed read. One fetch
    behind both air_readings() (the archive's shape) and air_facts(), which
    since 11 September 2026 also reads the official grade (CAI_GRD)."""
    try:
        d = http_get_json(
            f'http://openapi.seoul.go.kr:8088/{api_key}/json/ListAirQualityByDistrictService/1/25/')
        return [v for v in d.values() if isinstance(v, dict) and 'row' in v][0]['row']
    except (RuntimeError, KeyError, IndexError, ValueError):
        return None


def kma_now(key):
    """Every 초단기실황 observation at the city point right now, or None.

    _kma_air_at() takes one variable (T1H) at one stated hour, which is what a
    card needs. This takes the whole reading at the current hour, which is what
    an archive needs: temperature, humidity, wind, precipitation.

    ⚠️ The endpoint keeps ONE DAY. Verified 27 August 2026 by asking for older
    dates: anything past 24 hours answers "최근 1일 간의 자료만 제공합니다".
    The same measurements live permanently in ASOS (AsosHourlyInfoService,
    station 108), reachable on this same key, so this archive is a convenience
    rather than the only copy.
    """
    if not key:
        return None
    now = datetime.now(SEOUL_TZ)
    p = {'serviceKey': key, 'pageNo': '1', 'numOfRows': '20', 'dataType': 'JSON',
         'base_date': now.strftime('%Y%m%d'), 'base_time': now.strftime('%H00'),
         'nx': str(KMA_NOW_NX), 'ny': str(KMA_NOW_NY)}
    url = ('http://apis.data.go.kr/1360000/VilageFcstInfoService_2.0/'
           'getUltraSrtNcst?' + urllib.parse.urlencode(p, safe='%'))
    try:
        d = http_get_json(url)
        items = d['response']['body']['items']['item']
    except (RuntimeError, KeyError, TypeError):
        return None
    out = {i['category']: i['obsrValue'] for i in items if i.get('category')}
    return out or None


# The air title's glyph: KMA's index colours, the worst grade present.
AIR_GRADE_EMOJI = {'좋음': '🟢', '보통': '🟡', '나쁨': '🟠', '매우나쁨': '🔴'}
AIR_SCALE_NOTE_EN = 'PM2.5: good to 15, bad from 36; PM10: good to 30, bad from 81 µg/m³'
AIR_SCALE_NOTE_KO = '초미세먼지: 15까지 좋음, 36부터 나쁨; 미세먼지: 30까지 좋음, 81부터 나쁨 (µg/m³)'
AIR_NOW = {'emoji': None}


def air_facts(api_key):
    """Four live lines. Two facts until 11 September 2026, which kept the vein
    under STARVE_MIN_FACTS: it could ride along on another card but never
    lead one, so the floor never promoted it. The two added come off the
    same rows: the best PM2.5 district (the other end of the spread the
    worst line opens) and how many districts the city's own index rates
    좋음 right now, a count of rows against a published grade. A monitor
    under maintenance reports 점검중 for its readings and no grade (3 of 25
    on the morning this was built) and is left out of both.
    """
    try:
        rows = air_rows(api_key)
        if not rows:
            return []
        vals = [(x.get('MSRSTE_NM') or x.get('SAREA_NM') or x.get('MSRSTN_NM'),
                 float(x['FPM'])) for x in rows
                if str(x.get('FPM', '')).replace('.', '', 1).isdigit()]
        if not vals:
            return []
        worst = max(vals, key=lambda t: t[1])
        best = min(vals, key=lambda t: t[1])
        graded = [x for x in rows if (x.get('CAI_GRD') or '').strip()]
        good = sum(1 for x in graded if x['CAI_GRD'].strip() == '좋음')
        bad = sum(1 for x in graded if x['CAI_GRD'].strip() in ('나쁨', '매우나쁨'))
        pm10 = [(x.get('MSRSTN_NM'), float(x['PM'])) for x in rows
                if str(x.get('PM', '')).replace('.', '', 1).isdigit()]
        # The title glyph is KMA's own index colour for the WORST grade any
        # district is in right now, his call, 11 September 2026.
        AIR_NOW['emoji'] = next((AIR_GRADE_EMOJI[g] for g in ('매우나쁨', '나쁨', '보통', '좋음')
                                 if any(x['CAI_GRD'].strip() == g for x in graded)), None)
        # FPM is PM2.5, not PM10. The service documents its measured values as
        # 미세먼지(PM-10), 오존, 이산화질소, 일산화탄소, 아황산가스, and OZON/NTDX/CBMX/SPDX
        # take four of those, leaving PM as the documented PM-10 and FPM as the
        # fine fraction — PM >= FPM in 23 of 25 districts, and CRST_SBSTN names
        # "PM-2.5" in its own right. So the Korean term is 초미세먼지; 미세먼지 would
        # be PM10, a different number. English names the standard outright rather
        # than saying "fine dust", which is the ambiguity that caused this.
        # label_ko pinned since 11 September 2026: left to the selector, one
        # run dropped "live" (실시간) and the label check put the English
        # line on the Korean card. The wording is the selector's own from a
        # run it got right.
        # "across Seoul" and then "right now" dropped 11 September 2026, his
        # call: the opener names the city and the moment, so the lines need not
        # (feedback_no_word_the_title_already_carries).
        return [fact('air_monitors', 'air', 'Air-quality monitors reporting live',
                     str(len(vals)), str(len(vals)), pin=True,
                     label_ko='실시간으로 보고하는 대기질 측정소'),
                fact('air_worst', 'air',
                     f'Worst PM2.5 ({en_name(worst[0], "districts")})',
                     f'{worst[1]:.0f} µg/m³', f'{worst[1]:.0f} µg/m³', pin=True,
                     label_ko=f'초미세먼지가 가장 심한 곳 ({worst[0]})'),
                fact('air_best', 'air',
                     f'Cleanest PM2.5 ({en_name(best[0], "districts")})',
                     f'{best[1]:.0f} µg/m³', f'{best[1]:.0f} µg/m³', pin=True,
                     label_ko=f'초미세먼지가 가장 낮은 곳 ({best[0]})')] + ([
                # Short enough not to wrap against "19 of 22" (the row fits
                # about 53 characters of label and value before it loses its
                # leader; the first wording did, on the first render).
                fact('air_good', 'air', 'Districts with “good” air',
                     f'{good} of {len(graded)}', f'{len(graded)}곳 중 {good}곳', pin=True,
                     label_ko='대기질 등급이 “좋음”인 자치구')] if graded else []) + ([
                # Only when there is one: the line people look up on a dust day.
                fact('air_bad', 'air', 'Districts with “bad” or worse air',
                     f'{bad} of {len(graded)}', f'{len(graded)}곳 중 {bad}곳', pin=True,
                     label_ko='대기질 등급이 “나쁨” 이상인 자치구')] if bad else []) + ([
                # Yellow-dust days are PM10 events, which PM2.5 alone would miss.
                fact('air_pm10', 'air',
                     f'Worst PM10 ({en_name(max(pm10, key=lambda t: t[1])[0], "districts")})',
                     f'{max(v for _, v in pm10):.0f} µg/m³', f'{max(v for _, v in pm10):.0f} µg/m³',
                     pin=True,
                     label_ko=f'미세먼지가 가장 심한 곳 ({max(pm10, key=lambda t: t[1])[0]})')]
                if pm10 else [])
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


# The busroutes card since 10 September 2026 ranks by BOARDINGS PER STOP
# SERVED, not raw boardings: his call, after 63 days of history showed the
# raw ranking fixed at both ends (143 busiest and 1226 quietest on all 63)
# because 143 is the longest route, not the most crowded. Per stop, 143 was
# seventh of 326 on 7 September, behind 2211 (455 a stop on 40 stops), 5515
# and 5621; 1226 was last on both measures. Routes with fewer than
# BUS_MIN_STOPS stops served are left out, so a short shuttle cannot lead on
# two stops. The fourth line is the citywide average on the same measure,
# so the card stays one unit. Built from bus_route_history.json (totals and
# stop counts per route per day), like the other history cards, which is
# also what lets both streaks below be read straight from the record.
BUS_MIN_STOPS = 10
# A 1- or 2-day streak is not yet a pattern — every route has a quiet run
# sometimes. Below this the footnote says nothing about it, same reasoning
# as KBO_ORDER_SINCE and SEVERE_STARVE_DAYS elsewhere in this codebase.
BUS_ROUTE_STREAK_MIN = 3

# The ranking is over TRUNK AND BRANCH ROUTES ONLY — Seoul's own 간선 (blue,
# three digits 100-799) and 지선 (green, four digits 1000-7999) classes, the
# ordinary all-day network — his call, 10 September 2026, in two steps the
# same evening. Step one dropped Hangul route numbers (village buses "마포01",
# rush-hour variants "8442퇴근"), because on 1-7 September's data '8442퇴근'
# was the quietest on EVERY weekday and the all-or-nothing withhold (still
# below, as the belt) was refusing the card five days in seven. Step two,
# after he asked whether busiest and quietest were apples to apples: with
# Hangul out, the quietest on every one of those seven days was still an
# 8000-series 맞춤버스 — 8333A runs 06:35-08:05, eight trips, weekdays only —
# against a trunk route running all day. So the 8000s (tailored), 9000s
# (express, into Gyeonggi), N-prefixed night routes and letter-suffixed loop
# halves are out of the ranking too; the footnote says so; and the day's
# TOTAL still counts every route. Under this rule 1226 (a 04:30-22:55 branch
# route, two buses) was the quietest on all seven days measured, and the top
# two never changed. Letter-suffixed routes never came within six of the
# bottom, so excluding them changed no result.
_RANKED_ROUTE_RE = re.compile(r'^[1-7]\d{2,3}$')
# The measure itself ("Boardings per stop served") moved up to the dateline
# on 11 September 2026, his layout: the footnote keeps the eligibility rule,
# then the stop counts, then any streak, as sentences.
BUS_ROUTE_CAVEAT_EN = 'Trunk and branch routes with 10 or more stops.'
# The opener is Python's since 12 September 2026, his wording ("just On the
# buses"): the selector wrote "On the buses today" and "Seoul's bus routes,
# today" over a card whose day is four days behind the feed, so "today" was
# a false claim on every post. The dateline carries the day; the title
# carries none. Same arrangement as BUSSTOP_OPENER_EN.
BUSROUTES_OPENER_EN = 'On the buses'
BUSROUTES_OPENER_KO = '서울의 버스'
# The night-bus card's opener is Python's for the same reason since
# 12 September 2026, when it got its own 18:30 slot: the selector wrote
# "Seoul's night buses, route by route" and could as easily write "tonight"
# over a day four days behind. Same shape as the day card's.
NIGHTBUS_OPENER_EN = 'On the night buses'
NIGHTBUS_OPENER_KO = '서울의 심야버스'
# The stations card's opener, Python's since 12 September 2026 when it got
# its own 14:30 slot ("can we add a daily subway post?"): same reason and
# same shape as the bus cards above. The card's day rides the dateline.
STATIONS_OPENER_EN = 'On the subway'
STATIONS_OPENER_KO = '서울의 지하철'
BUS_ROUTE_CAVEAT_KO = '정류장 10곳 이상 간선·지선.'
# Stamped into transport_cache; a same-day cache stamped with another rule
# (or none) is refetched rather than served with a route the current rule
# would not rank at the bottom.
BUS_RANK_RULE = 'trunk-branch'


# --- the stations card -------------------------------------------------------
# The subway analogue of busroutes, his call, 10 September 2026 ("build the
# subway card", "including a map"). Same four-line shape: busiest, second-
# busiest, quietest station and the day's total subway boardings, own post,
# a map of the three as a fifth reply. Two rules make it apples to apples,
# both measured before deciding:
#
# ⚠️ ROWS ARE SUMMED PER STATION ACROSS LINES. CardSubwayStatsNew is one row
# per station PER LINE (616 rows, 528 stations on 7 Sep 2026), so 서울역 is
# split five ways (1호선, 경부선, 4호선, 공항철도, 경의선) and ranks third or lower
# on any single row while Gangnam, with one row, ranks first. Summed, 서울역
# led every one of the 7 days measured (122k-150k) with 잠실 or 홍대입구 second.
# The transport vein's own "Busiest subway station" line is per-row and says
# Gangnam; it is left as it was. The same fold closes the other end: an
# interchange's minor-line row can read 1-9 boardings (연신내 3, 충무로 4) because
# the gates count on the other line, and summing folds those in.
#
# ⚠️ STATIONS INSIDE SEOUL ONLY. The feed reaches Paju, Gapyeong, Yangpyeong
# and Cheonan, so the raw quietest is a Korail halt (운천 2-13, 임진강 9-87,
# 지평 48-69). Membership is decided by distance to the nearest Seoul-
# registered bus stop (ids beginning '1', the convention the bus map already
# uses): measured over all 655 stations in subwayStationMaster, 311 sit
# within 300 m and the next three are 광명사거리 at 389 m, 지축 at 453 m and
# 갈매 at 734 m — all three in Gyeonggi — with everything else beyond 2 km.
# 남태령 (223 m) and 마천 (147 m), both in Seoul, land inside. So the cut is
# STATION_IN_SEOUL_KM with a measured gap on either side of it, and the
# footnote says what the card counts.
STATION_DAY = {'en': None, 'ko': None}
STATION_MAP_INFO = {'day': None, 'stations': None}   # [(label_en, lon, lat)] ×3
STATION_CAVEAT_EN = 'Stations inside Seoul, all lines combined'
STATION_CAVEAT_KO = '서울 시내 역, 전 노선 합산'
STATION_IN_SEOUL_KM = 0.3
STATION_QUIET_FLOOR = 10     # the transport vein's own feed-artifact floor
# Stamped into transport_cache beside bus_rank_rule, same reasoning.
STATION_RANK_RULE = 'seoul-summed-2'   # -2: the transport lines follow it too


# --- the bus stops card ------------------------------------------------------
# His call, 11 September 2026 ("Build the busiest bus stop card"), after a
# review of data the bot fetches and never reads: CardBusStatisticsServiceNew
# is one row per ROUTE AND STOP, and transport_facts() only ever summed it to
# routes. Four lines, own post, a pin map as a fifth reply through the same
# registry as the other ranked cards: the day's busiest, second- and third-
# busiest stops, then how many stops took a boarding at all. No quietest
# line: on 7 September 2026, 179 Seoul stops took no boarding and 888 took
# under ten, so "quietest" is a thousand-way tie on a figure that says
# nothing. Measured on 4-7 September 2026 before any of this was written:
#
# ⚠️ THE UNIT IS ONE STOP, ONE ARS NUMBER, and a name is not a stop. 고속터미널
# is two stops facing each other (22019 and 22020, 9,616 and 8,879 on
# 7 September), 구로디지털단지역 is a median-lane stop (구로디지털단지역(중),
# 10,502) and a kerbside one (9,032), and 청량리역환승센터 is a whole transfer
# centre of bays under one name, which is why it LEADS a ranking summed by
# name (20,517) and appears nowhere in one by stop. Ranked by stop, 홍대입구역
# (14015) led all four days. So the ranking is per stop id, Seoul stops only
# (ids beginning '1', the convention the maps and the stations card use;
# Gyeonggi stops on Seoul routes took 255,008 of 5.48m boardings that day),
# and a name appears ONCE: where two stops share a name, the busier side.
# The footnote says so.
#
# ⚠️ ENGLISH NAMES ARE DERIVED FROM THE STATION TABLE, NEVER INVENTED. No Seoul
# feed carries an English stop name; OpenStreetMap has one for under half of
# Seoul's stops, not for 14015, and unevenly ("Hongdae Entrance Station",
# "Suyuyeokgangbuk Office"), so it was rejected. The busiest stops are named
# after the station they stand at, and the official English of that station
# is already in seoul_index_names_en.json: 홍대입구역 → "Hongik University
# Station", 지하철2호선강남역 → "Gangnam Station", 고속터미널 → "Express Bus
# Terminal". A stop whose name is not a station's (수서역6번출구,
# 신림사거리.신림역, 남산서울타워) gets no English, and if it ranks in the top
# three the whole card is withheld and the log says which name, exactly the
# stations card's rule: a fourth stop promoted into a "top three" would be
# a ranking that never happened. On the four measured days every top-three
# stop resolved.
#
# ⚠️ THE KOREAN LINE CARRIES THE SAME NAME THE ENGLISH DOES. The feed spells
# a stop 구로디지털단지역(중) and 지하철2호선강남역; the English, borrowed from the
# station, is Guro Digital Complex Station and Gangnam Station, and the label
# check flagged both on the first dry run (11 September 2026) for a
# qualifier the other language lacked. So the Korean shows the folded name
# (stop_display_ko): the one-per-name rule already makes the name, not the
# side of the road or the line prefix, the unit the card counts.
BUSSTOP_RANK_RULE = 'seoul-per-stop-2'   # stamped into transport_cache; -2 added seoul_stop_count
BUSSTOP_KEEP = 12                        # ranked stops kept in the day cache
# The footnote is his wording, 11 September 2026, with the count of Seoul's
# registered stops (the coordinate feed's '1'-prefixed ids) read live. The
# first draft ("Stops inside Seoul, one per name: the busier side of the
# road") packed two rules into one clause and he found it hard to follow.
# The opener and dateline are Python's too, so the card says "bus stops"
# whatever the selector writes: the first dry run's "Where Seoul catches the
# bus" left rows reading "... Station" under a title that never said stop.
BUSSTOP_OPENER_EN = 'Seoul’s bus stops'
BUSSTOP_OPENER_KO = '서울의 버스 정류장'


def busstop_note(n_stops):
    return (f'There are {grouped(n_stops)} bus stops in Seoul. '
            f'Where two stops share a name, the busier is shown.',
            f'서울 시내 버스 정류장은 {grouped(n_stops)}곳. 같은 이름의 정류장이 둘이면 승차가 많은 쪽을 표시.')


def stop_name(raw):
    """'명륜3가.성대입구(00030)' → '명륜3가.성대입구': the feed's own name with
    its bracketed stop-sequence number dropped. Any other bracket, like the
    (중) marking a median-lane stop, is part of the published name and stays."""
    return re.sub(r'\s*\(\d{4,}\)\s*$', '', raw or '').strip()


def stop_key(name):
    """The name a stop shares with its twin across the road: brackets
    dropped ('구로디지털단지역(중)' → '구로디지털단지역'), so the two sides of one
    named stop, and a median-lane stop beside a kerbside one, fold to one
    key for the one-per-name rule."""
    return re.sub(r'\s*\(.*?\)\s*$', '', name or '').strip()


def stop_display_ko(name):
    """The Korean name the card prints: brackets and a 지하철N호선 prefix
    dropped, so it names the same stop the English line does."""
    return re.sub(r'^지하철\d+호선', '', stop_key(name)) or name


def stop_en(name):
    """The official English of the station a stop is named after, with
    'Station' appended where the Korean carries 역, or None. A leading
    지하철N호선 is dropped first (지하철2호선강남역). Never a romanization."""
    base = re.sub(r'^지하철\d+호선', '', stop_key(name))
    if not base:
        return None
    if base.endswith('역'):
        en = en_lookup(base[:-1], 'stations')
        if en:
            return en if en.endswith('Station') else f'{en} Station'
    return en_lookup(base, 'stations')


def seoul_bus_stop_coord_map(api_key):
    """{stop id: (lon, lat)} for every Seoul-registered bus stop (id beginning
    '1'). ~11,000 entries, a dozen calls; the maps' silhouette, the stations
    card's membership test and the bus stops card's pins all read it."""
    base = f'http://openapi.seoul.go.kr:8088/{api_key}/json'
    tot = int(http_get_json(f'{base}/busStopLocationXyInfo/1/1')
              ['busStopLocationXyInfo']['list_total_count'])
    out = {}
    for s in range(1, tot + 1, 1000):
        d = http_get_json(f'{base}/busStopLocationXyInfo/{s}/{min(s + 999, tot)}')
        for r in d.get('busStopLocationXyInfo', {}).get('row', []):
            sid = str(r.get('STOPS_NO', ''))
            if not sid.startswith('1'):
                continue
            try:
                out[sid] = (float(r['XCRD']), float(r['YCRD']))
            except (KeyError, TypeError, ValueError):
                continue
    return out


def rank_bus_stops(stop_sum, stop_nm):
    """[(stop id, published name, boardings)] largest first, Seoul stops
    only, every stop its own entry (the one-per-name fold happens when the
    card is built, so the cache keeps the raw ranking)."""
    return sorted(((sid, stop_nm.get(sid, ''), v) for sid, v in stop_sum.items()
                   if str(sid).startswith('1') and v > 0),
                  key=lambda t: (-t[2], t[0]))


def bus_stops_facts(c, d, d_ko):
    """The busstops card for the cached day: the three busiest stops, one
    per name, each with a derived English name and coordinates, plus the
    count of stops that took a boarding. Fills RANKED_CARD_INFO['busstops']
    when built; prints why when withheld."""
    RANKED_CARD_INFO.pop('busstops', None)
    if c.get('stop_rule') != BUSSTOP_RANK_RULE:
        return []
    chosen, seen = [], set()
    for sid, name, v in c.get('stop_ranked') or []:
        en = stop_en(name)
        key = en or stop_key(name)
        if key in seen:
            continue
        seen.add(key)
        chosen.append((sid, name, int(v), en))
        if len(chosen) == 3:
            break
    if len(chosen) < 3:
        if c.get('stop_ranked'):
            print(f'Bus stops card withheld for {d}: fewer than three rankable stops.')
        return []
    missing = [name for _, name, _, en in chosen if not en]
    if missing:
        print(f'Bus stops card withheld for {d}: no English name for '
              f'{", ".join(repr(m) for m in missing)}.')
        return []
    coords = c.get('stop_coords') or {}
    if any(sid not in coords for sid, _, _, _ in chosen):
        print(f'Bus stops card withheld for {d}: no coordinates for '
              f'{", ".join(repr(name) for sid, name, _, _ in chosen if sid not in coords)}.')
        return []
    ranks = (('Busiest', '가장 붐빔'), ('2nd-busiest', '두 번째로 붐빔'), ('3rd-busiest', '세 번째로 붐빔'))
    note_en, note_ko = busstop_note(c.get('seoul_stop_count') or 0)
    RANKED_CARD_INFO['busstops'] = {
        'day_en': d, 'day_ko': d_ko,
        'opener_en': BUSSTOP_OPENER_EN, 'opener_ko': BUSSTOP_OPENER_KO,
        'dateline_en': f'Bus boardings on {d}', 'dateline_ko': f'{d_ko} 버스 승차',
        'note_en': note_en, 'note_ko': note_ko,
        'map_day': c['date'],
        # The map says "bus stops" in its title: with the bare date and
        # legend lines ending "Station" it read as the subway map (his
        # observation, 11 Sep 2026). The caption describes the drawing, his
        # wording; the busier-side rule stays on the card, one post above.
        'map_title': f'Bus boardings for {d}',   # his wording, 11 Sep 2026
        'map_caption': (f'The map is composed of gray dots that represent each of '
                        f'Seoul’s {grouped(c.get("seoul_stop_count") or 0)} bus stops.'),
        # The pins are the stations map's own shape, drawn by the same renderer.
        'map_pins': [(f'{ranks[i][0]}: {en}', MAP_COLOURS[i], tuple(coords[sid]))
                     for i, (sid, _, _, en) in enumerate(chosen)],
        'map_alt': (f'Map of the three busiest Seoul bus stops on {d}: '
                    + ', '.join(f'{ranks[i][0].lower()} ({en})' for i, (_, _, _, en) in enumerate(chosen))
                    + f', each marked and named. The map is composed of gray dots that '
                      f'represent each of Seoul’s {grouped(c.get("seoul_stop_count") or 0)} bus '
                      f'stops. Where two stops share a name, the busier is shown.')}
    facts = []
    for i, (sid, name, v, en) in enumerate(chosen):
        rank_en, rank_ko = ranks[i]
        # Bare stop names, no "stop" word: the opener names bus stops (the
        # selector rule requires it), as the stations card's lines are bare.
        facts.append(fact(f'busstop_{("busiest", "second", "third")[i]}', 'busstops',
                          f'{rank_en}: {en}', grouped(v), grouped(v), pin=True,
                          label_ko=f'{rank_ko}: {stop_display_ko(name)}',
                          place_en=rank_en, place_ko=rank_ko,
                          num=v, unit='people'))
    facts.append(fact('busstop_used', 'busstops', 'Stops with at least one boarding',
                      grouped(c['stops_used']), grouped(c['stops_used']), pin=True,
                      label_ko='승차가 1명 이상 있었던 정류장 수'))
    return facts


def fold_station(name):
    """'잠실(송파구청)' → '잠실': the bracketed landmark the feed appends and the
    name table does not carry (see en_lookup), and the key two lines' rows
    are summed on."""
    return re.sub(r'\s*\(.*?\)\s*$', '', name or '').strip()


def seoul_bus_stop_coords(api_key):
    """[(lon, lat)] for every Seoul-registered bus stop (id beginning '1').
    The background silhouette of both maps and the membership test for the
    stations card. ~11,000 points, a dozen calls."""
    return list(seoul_bus_stop_coord_map(api_key).values())


def station_coords(api_key):
    """{folded station name: (lon, lat)} from subwayStationMaster, first row
    per name (a transfer station repeats once per line; its platforms are
    within a couple of hundred metres of each other)."""
    base = f'http://openapi.seoul.go.kr:8088/{api_key}/json'
    tot = int(http_get_json(f'{base}/subwayStationMaster/1/1')
              ['subwayStationMaster']['list_total_count'])
    out = {}
    for s in range(1, tot + 1, 1000):
        d = http_get_json(f'{base}/subwayStationMaster/{s}/{min(s + 999, tot)}')
        for r in d.get('subwayStationMaster', {}).get('row', []):
            name = fold_station(r.get('BLDN_NM'))
            if not name or name in out:
                continue
            try:
                out[name] = (float(r['LOT']), float(r['LAT']))
            except (KeyError, TypeError, ValueError):
                continue
    return out


def _km(a, b):
    k = math.cos(math.radians((a[1] + b[1]) / 2))
    return math.hypot((a[0] - b[0]) * k, a[1] - b[1]) * 111.2


def stations_in_seoul(coords, seoul_stops, within_km=STATION_IN_SEOUL_KM):
    """The subset of `coords` (name → (lon, lat)) within `within_km` of any
    Seoul-registered bus stop. A coarse grid keeps it to a few hundred
    distance checks per station rather than 11,000."""
    cell = 0.01   # ~1 km; every candidate within 300 m is in this or a neighbour cell
    grid = {}
    for lon, lat in seoul_stops:
        grid.setdefault((int(lon / cell), int(lat / cell)), []).append((lon, lat))
    inside = set()
    for name, (lon, lat) in coords.items():
        gx, gy = int(lon / cell), int(lat / cell)
        if any(_km((lon, lat), q) <= within_km
               for dx in (-1, 0, 1) for dy in (-1, 0, 1)
               for q in grid.get((gx + dx, gy + dy), ())):
            inside.add(name)
    return inside


# --- the station gap card ----------------------------------------------------
# His call, 11 September 2026 ("Build No. 1"), after measuring the feed's
# alighting column over thirteen days: ranked by people getting off, the
# stations card repeats itself (Seoul Station and Jamsil lead both ways,
# and only the third slot swaps Gangnam for Hongik Univ.), but the GAP
# between the two columns is new. Saturday 5 September 2026 at 여의나루:
# 73,991 got off and 21,074 got on, an event on the river that no boardings
# ranking would show; every weekday 역삼 takes 6,000-8,000 more off than on
# and 김포공항 the reverse. So: the rush card's shape, ONE station, two
# lines, both figures published, the larger first, the day's widest
# ABSOLUTE gap (a ratio would hand the card to a halt with 50 on and 500
# off). Summed per station across lines and inside Seoul, the stations
# card's own two rules. A computed difference is never printed: the two
# figures sit together and the arrangement does the rest.
STATIONGAP_COOLDOWN_DAYS = 7     # 역삼 leads most weekdays
STATIONGAP_RULE = 'seoul-abs-1'  # stamped into transport_cache beside st_rule
STATIONGAP_OPENER_EN = 'Seoul’s subway, one station'
STATIONGAP_OPENER_KO = '서울 지하철, 역 하나'
STATIONGAP_NOTE_EN = ('The station with the day’s widest gap between getting off and getting on. '
                      'Stations inside Seoul, all lines combined.')
STATIONGAP_NOTE_KO = '하차와 승차의 차이가 그날 가장 큰 역. 서울 시내 역, 전 노선 합산.'


def station_gap(srows, in_seoul):
    """(name, on, off) for the Seoul station with the widest |off - on|,
    summed across its lines, or None. Rows without a digit off-count are
    left out, so a feed that dropped the column withholds rather than
    reading every station as all-boardings."""
    on, off = {}, {}
    for x in srows:
        name = fold_station(x.get('SBWY_STNS_NM'))
        a, b = str(x.get('GTON_TNOPE', '')), str(x.get('GTOFF_TNOPE', ''))
        if not name or name not in in_seoul or not (a.isdigit() and b.isdigit()):
            continue
        on[name] = on.get(name, 0) + int(a)
        off[name] = off.get(name, 0) + int(b)
    if not on:
        return None
    name = max(on, key=lambda n: (abs(off[n] - on[n]), n))
    return name, on[name], off[name]


def station_gap_facts(c, d, d_ko):
    """The stationgap card for the cached day. Fills RANKED_CARD_INFO when
    built; prints why when withheld."""
    RANKED_CARD_INFO.pop('stationgap', None)
    if c.get('gap_rule') != STATIONGAP_RULE or not c.get('gap'):
        return []
    name, on, off = c['gap']
    en = en_lookup(name, 'stations')
    if not en:
        print(f'Station gap card withheld for {d}: no English name for {name!r}.')
        return []
    RANKED_CARD_INFO['stationgap'] = {
        'day_en': d, 'day_ko': d_ko,
        'opener_en': STATIONGAP_OPENER_EN, 'opener_ko': STATIONGAP_OPENER_KO,
        'dateline_en': d, 'dateline_ko': d_ko,
        'note_en': STATIONGAP_NOTE_EN, 'note_ko': STATIONGAP_NOTE_KO}
    # The station is the emphasised run (place_en), as the rush card bolds
    # its station; the larger figure leads.
    off_line = fact('st_gap_off', 'stationgap', f'Got off at {en}', grouped(off), grouped(off),
                    pin=True, label_ko=f'{name} 하차', place_en=en, place_ko=name,
                    num=off, unit='people')
    on_line = fact('st_gap_on', 'stationgap', f'Got on at {en}', grouped(on), grouped(on),
                   pin=True, label_ko=f'{name} 승차', place_en=en, place_ko=name,
                   num=on, unit='people')
    return [off_line, on_line] if off >= on else [on_line, off_line]


def rank_stations(srows, in_seoul):
    """Sum boardings per folded station name, keep stations inside Seoul,
    and return (ranked largest-first, quietest at or above the floor)."""
    sums = {}
    for x in srows:
        name = fold_station(x.get('SBWY_STNS_NM'))
        v = x.get('GTON_TNOPE', '0')
        if not name or not str(v).isdigit():
            continue
        sums[name] = sums.get(name, 0) + int(v)
    ranked = sorted(((n, v) for n, v in sums.items() if n in in_seoul),
                    key=lambda kv: -kv[1])
    sane = [p for p in ranked if p[1] >= STATION_QUIET_FLOOR]
    return ranked, (sane[-1] if sane else None)


# --- per-route daily history -----------------------------------------------
# One file, bus_route_history.json (gitignored, regenerable from the feed by
# bus_route_history_backfill.py), holding every ASCII-numbered route's day
# total for every day transport_facts() has fetched, plus a cache of Korean
# public holidays. Added 10 September 2026 for the three cards that need a
# route's own past rather than a single day's ranking: busmovers (a day
# against the route's own same-weekday median), busweekend (weekend against
# weekday) and nightbus (which only needs one day but shares the file).
# ⚠️ Tests must point BUS_HISTORY at a temp path: transport_facts() writes it.
BUS_HISTORY = Path(__file__).with_name('bus_route_history.json')
STOPS_RULE = 'distinct-stop-ids'
HOLIDAYS_URL = 'https://date.nager.at/api/v3/PublicHolidays/{year}/KR'


def load_bus_history():
    try:
        h = json.loads(BUS_HISTORY.read_text())
    except (OSError, ValueError):
        h = {}
    h.setdefault('days', {})
    h.setdefault('holidays', {})
    # Stop counts are versioned: the first fill counted feed ROWS, which
    # over-counts routes that carry a stop twice; a file stamped with an
    # older rule drops its stop counts so the backfill rebuilds them.
    if h.get('stops_rule') != STOPS_RULE:
        h['stops'] = {}
        h['stops_rule'] = STOPS_RULE
    h.setdefault('stops', {})
    return h


def save_bus_history(h):
    write_json_atomic(BUS_HISTORY, h, ensure_ascii=False)


def bus_history_add(h, day, route_sums, route_stops=None):
    """Record a day's per-route totals (ASCII route numbers only; the Hangul
    ones rank in no card) and, since 10 September 2026, each route's count
    of stops that saw a boarding (`h['stops'][day]`), for the per-stop
    ranking. Idempotent per key: a day already held is left alone, so a
    re-fetch under a new ranking rule cannot rewrite history; a day whose
    totals are held but whose stop counts are not gains the stop counts."""
    ascii_ = lambda no: no and re.match(r'^[A-Za-z0-9-]+$', str(no))
    added = False
    if day not in h['days']:
        h['days'][day] = {no: int(v) for no, v in route_sums.items() if ascii_(no)}
        added = True
    if route_stops is not None and day not in h.setdefault('stops', {}):
        h['stops'][day] = {no: int(n) for no, n in route_stops.items() if ascii_(no)}
        added = True
    return added


def kr_holidays(h, year):
    """Set of 'YYYYMMDD' public holidays for `year`, from the history file's
    cache or nager.at. None when it cannot be had: a caller comparing
    weekdays must then withhold rather than compare a holiday Monday with
    ordinary ones (17 August 2026, the 광복절 substitute day, had Sunday-
    shaped figures)."""
    cache = h.setdefault('holidays', {})
    if str(year) in cache:
        return set(cache[str(year)])
    try:
        rows = http_get_json(HOLIDAYS_URL.format(year=year))
        dates = sorted({r['date'].replace('-', '') for r in rows if r.get('date')})
    except (RuntimeError, KeyError, TypeError, AttributeError):
        return None
    if not dates:
        return None
    cache[str(year)] = dates
    return set(dates)


def kr_holiday_names(h, year):
    """{'YYYYMMDD': (local name, English name)} for `year`'s public holidays,
    from the history file's cache or nager.at; None when it cannot be had.
    Kept beside kr_holidays()'s date list rather than folded into it, so
    that list's shape (which the movers and weekend cards read as a set)
    never changes. The Seoul Station card's dateline reads it: "September 25
    was a holiday, Chuseok" / "9월 25일 추석", his wording, 11 Sep 2026."""
    cache = h.setdefault('holiday_names', {})
    if str(year) in cache:
        return {d: tuple(v) for d, v in cache[str(year)].items()}
    try:
        rows = http_get_json(HOLIDAYS_URL.format(year=year))
        names = {r['date'].replace('-', ''): [r.get('localName') or '', r.get('name') or '']
                 for r in rows if r.get('date')}
    except (RuntimeError, KeyError, TypeError, AttributeError):
        return None
    if not names:
        return None
    cache[str(year)] = names
    return {d: tuple(v) for d, v in names.items()}


WEEKDAY_EN = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
WEEKDAY_KO = ['월요일', '화요일', '수요일', '목요일', '금요일', '토요일', '일요일']


# --- the three history cards ---------------------------------------------------
# His call, 10 September 2026, after 28 days showed the busroutes ranking
# fixed at both ends (143 busiest and 1226 quietest on all 28): a card whose
# answer changes. Three of them, all read from bus_route_history.json:
#   busmovers  — the day's biggest rise and biggest fall against each
#                route's own median of the same weekday in prior weeks (two
#                of each until 11 September 2026).
#   busweekend — the latest complete week's route that holds up best at the
#                weekend and the one that falls most, Saturday+Sunday per day
#                against Monday-Friday per day (two of each until 11 Sept 2026).
#   nightbus   — busiest, second and quietest N route plus the night total:
#                the one class the trunk-and-branch rule leaves out, ranked
#                within itself where it IS like for like.
# All three: trunk and branch routes only (nightbus: N routes only), a
# median or weekday floor of BUS_HIST_FLOOR so a 300-boarding route cannot
# swing 40% on a school trip, public holidays excluded from every baseline
# and never themselves compared (kr_holidays; a missing table withholds).
BUS_HIST_FLOOR = 1000
BUSMOVERS_MIN_PRIOR = 3
BUSMOVERS_MAX_PRIOR = 8
NIGHTBUS_COOLDOWN_DAYS = 7
BUSWEEKEND_COOLDOWN_DAYS = 7
# What the harvester tells compose() and main() about a history card it built
# this run: {cat: {'day_en', 'day_ko', 'note_en', 'note_ko', 'map_day',
# 'map_routes': [(label, colour, route_no)], 'map_caption'}}. Reset every run.
RANKED_CARD_INFO = {}
RANKED_CATS = ('busroutes', 'stations', 'busmovers', 'nightbus', 'busweekend', 'busstops',
               'railstations', 'seoulstation', 'stationgap', 'wxday', 'rescue', 'kopis',
               'kepco', 'kepcohist', 'kepcohouse', 'railcommuter')
# The veins whose card is TWO lines by design: rush (one station at two
# hours) and, since 11 September 2026, busmovers (one rise, one fall) and
# busweekend (one holds up best, one falls most). Every other vein needs
# three facts to be a card, and the starvation floor and --only both read
# this set rather than naming rush alone.
TWO_LINE_CATS = frozenset({'rush', 'busmovers', 'busweekend', 'stationgap'})
MAP_COLOURS = ('#d70000', '#e08a1e', '#000000', '#7a7a7a')


def _pct(ratio):
    """'+40%' / '−7%' (a real minus sign), rounded to whole percent."""
    v = round((ratio - 1) * 100)
    return f'+{v}%' if v >= 0 else f'−{-v}%'


def _day_dt(day):
    return datetime.strptime(day, '%Y%m%d')


def bus_movers(h, day, holidays):
    """(result, reason): result is {'ups': [(no, v, med, ratio)],
    'downs': [...], 'n_prior': n, 'wd': weekday} or None with the reason.
    One of each since 11 September 2026."""
    if holidays is None:
        return None, 'holiday table unavailable'
    if day in holidays:
        return None, f'{day} is a public holiday, not compared'
    if day not in h['days']:
        return None, f'{day} not in history'
    wd = _day_dt(day).weekday()
    prior = sorted(k for k in h['days'] if k < day and _day_dt(k).weekday() == wd
                   and k not in holidays)[-BUSMOVERS_MAX_PRIOR:]
    if len(prior) < BUSMOVERS_MIN_PRIOR:
        return None, f'only {len(prior)} prior {WEEKDAY_EN[wd]}s in history, need {BUSMOVERS_MIN_PRIOR}'
    rows = []
    for no, v in h['days'][day].items():
        if not ranked_route_no(no):
            continue
        hist = [h['days'][k][no] for k in prior if no in h['days'][k]]
        if len(hist) < BUSMOVERS_MIN_PRIOR:
            continue
        med = statistics.median(hist)
        if med < BUS_HIST_FLOOR:
            continue
        rows.append((no, v, med, v / med))
    # The two largest and two smallest ratios, whatever their sign: on a
    # day when the whole city fell, all four can be falls, and the card says
    # so line by line rather than withholding.
    rows.sort(key=lambda r: -r[3])
    # One up and one down, his call on 11 September 2026 (two of each until
    # then): the second rise and second fall said little the first did not.
    if len(rows) < 2:
        return None, f'only {len(rows)} routes with a baseline'
    return {'ups': rows[:1], 'downs': rows[-1:],
            'n_prior': len(prior), 'wd': wd}, None


def night_bus_rank(h, day):
    """(result, reason): N routes on `day` — ranked largest-first, plus the
    night total. Needs at least three N routes with a boarding."""
    if day not in h['days']:
        return None, f'{day} not in history'
    rows = sorted(((no, v) for no, v in h['days'][day].items()
                   if re.match(r'^N\d+$', no) and v > 0), key=lambda r: -r[1])
    if len(rows) < 3:
        return None, f'only {len(rows)} N routes with boardings'
    return {'ranked': rows, 'total': sum(v for _, v in rows)}, None


def weekend_swing(h, day, holidays):
    """(result, reason): the latest complete Monday-Sunday week ending on or
    before `day` with no holiday in it, and each trunk/branch route's
    Saturday+Sunday per-day boardings against its Monday-Friday per-day."""
    if holidays is None:
        return None, 'holiday table unavailable'
    dt = _day_dt(day)
    sunday = dt - timedelta(days=(dt.weekday() + 1) % 7)   # the Sunday on or before day
    for _ in range(4):
        week = [(sunday - timedelta(days=6 - i)).strftime('%Y%m%d') for i in range(7)]
        if all(k in h['days'] for k in week) and not any(k in holidays for k in week):
            break
        sunday -= timedelta(days=7)
    else:
        return None, 'no complete holiday-free week in history'
    wk, we = week[:5], week[5:]
    rows = []
    for no in h['days'][we[0]]:
        if not ranked_route_no(no):
            continue
        try:
            wk_avg = sum(h['days'][k][no] for k in wk) / 5
            we_avg = sum(h['days'][k][no] for k in we) / 2
        except KeyError:
            continue
        if wk_avg < BUS_HIST_FLOOR:
            continue
        rows.append((no, we_avg, wk_avg, we_avg / wk_avg))
    # Measured 1-7 September 2026: exactly ONE trunk/branch route over the
    # floor gained boardings at the weekend. So the card is not "gainers and
    # losers" but the two that hold up best and the two that fall most —
    # the two ends of the same ranking, whatever the sign at the top.
    rows.sort(key=lambda r: -r[3])
    # One that holds up best and one that falls most, his call on
    # 11 September 2026 (two of each until then, and the second of each
    # said little the first did not).
    if len(rows) < 2:
        return None, f'only {len(rows)} routes with a weekday baseline'
    return {'ups': rows[:1], 'downs': rows[-1:], 'week': week}, None


def _span_en(a, b):
    """'September 1 to 7' or 'August 31 to September 6' (month first,
    see en_date)."""
    da, db = _day_dt(a), _day_dt(b)
    if da.month == db.month:
        return f'{MONTHS_EN[db.month - 1]} {da.day} to {db.day}'
    return f'{en_date(da)} to {en_date(db)}'


def _span_ko(a, b):
    da, db = _day_dt(a), _day_dt(b)
    if da.month == db.month:
        return f'{da.month}월 {da.day}일~{db.day}일'
    return f'{da.month}월 {da.day}일~{db.month}월 {db.day}일'


def history_bus_facts(h, day, d, d_ko):
    """Build the busmovers, nightbus and busweekend facts for `day` from the
    history, filling RANKED_CARD_INFO for each card actually built and
    printing why for each one withheld."""
    for cat in ('busmovers', 'nightbus', 'busweekend'):
        RANKED_CARD_INFO.pop(cat, None)
    facts = []
    years = {day[:4], str(int(day[:4]) - 1)}
    hol = set()
    for y in sorted(years):
        got = kr_holidays(h, y)
        if got is None:
            hol = None
            break
        hol |= got
    # "Stops on each route", not "stops where each route saw a boarding":
    # bus_route_map_stops() draws one direction's stops from the day's feed
    # without filtering on boardings, so the old caption claimed a filter the
    # map does not apply (5515: 33 boarded stops on the card, 31 drawn).
    # Reworded 11 September 2026 rather than filtering, his call: a reader
    # cannot count stops on a drawn line, and a boarded-only filter would
    # only make a quiet route's path patchier.
    caption = f'Stops on each route, {d}: not necessarily its full path'

    mv, why = bus_movers(h, day, hol)
    if mv is None:
        print(f'Bus movers withheld for {d}: {why}.')
    else:
        wd_en, wd_ko = WEEKDAY_EN[mv['wd']], WEEKDAY_KO[mv['wd']]
        n = mv['n_prior']
        RANKED_CARD_INFO['busmovers'] = {
            'day_en': d, 'day_ko': d_ko,
            # Opener and dateline are Python's, his wording, 11 September
            # 2026: the selector's own openers ("Where Seoul's buses swung
            # today") never said the card is each route against its own
            # usual figure. Same fixed-opener arrangement as rush; applied
            # in main() where rush's is.
            'opener_en': f'Seoul’s bus routes, against their usual {wd_en}',
            'opener_ko': f'서울의 버스 노선, 평소 {wd_ko} 대비',
            'dateline_en': f'Boardings on {d}', 'dateline_ko': f'{d_ko} 승차',
            'note_en': (f'Against each route’s median of its previous {n} {wd_en}s · '
                        f'trunk and branch routes over 1,000 boardings'),
            'note_ko': f'각 노선의 이전 {wd_ko} {n}일 중앙값 대비 · 승차 1,000명 이상 간선·지선',
            'map_day': day, 'map_caption': caption,
            # Legend matches the rows ("Up the most"), his call, 11 September
            # 2026; it keeps "Route", having no opener to lean on.
            'map_routes': [(f'{"Up the most" if r > 1 else "Down the most"}: Route {no} ({_pct(r)})',
                            MAP_COLOURS[i], no)
                           for i, (no, v, m, r) in enumerate(mv['ups'] + mv['downs'])]}
        for no, v, m, r in mv['ups'] + mv['downs']:
            up = r > 1
            # "Up the most" / "Down the most": his wording, 11 September 2026,
            # so the row says it is the day's extreme, not one rise among many.
            rank_en = 'Up the most' if up else 'Down the most'
            rank_ko = '가장 많이 증가' if up else '가장 많이 감소'
            facts.append(fact(
                f'busmv_{"up" if up else "down"}1', 'busmovers',
                f'{rank_en}: {no}, {grouped(v)} boardings',
                _pct(r), _pct(r), pin=True,
                label_ko=f'{rank_ko}: {no}번, {grouped(v)}명 승차',
                place_en=rank_en, place_ko=rank_ko))

    nb, why = night_bus_rank(h, day)
    if nb is None:
        print(f'Night bus withheld for {d}: {why}.')
    else:
        top, second, bottom = nb['ranked'][0], nb['ranked'][1], nb['ranked'][-1]
        RANKED_CARD_INFO['nightbus'] = {
            'day_en': d, 'day_ko': d_ko,
            # The dateline says what the figures ARE, his wording, 11 September
            # 2026: "Boardings on 7 September" rather than a bare date. day_en
            # stays the bare date, since the map's title and alt read it.
            'dateline_en': f'Boardings on {d}', 'dateline_ko': f'{d_ko} 승차',
            'opener_en': NIGHTBUS_OPENER_EN, 'opener_ko': NIGHTBUS_OPENER_KO,
            # The footnote says the dateline is the newest day the feed has,
            # his call, 12 September 2026, since the card cites a day several
            # days behind: "September 8 is the latest date for which data is
            # available." A rule, not a lag count, so it is true on the morning
            # the feed stalls too.
            'note_en': f'Night routes only. {d} is the latest date for which data is available.',
            'note_ko': f'심야 노선만. {d_ko}은 데이터가 공개된 가장 최근 날짜.',
            'map_day': day, 'map_caption': caption,
            'map_routes': [(f'Busiest: Route {top[0]}', MAP_COLOURS[0], top[0]),
                           (f'2nd-busiest: Route {second[0]}', MAP_COLOURS[1], second[0]),
                           (f'Quietest: Route {bottom[0]}', MAP_COLOURS[2], bottom[0])]}
        facts += [
            fact('busnight_busiest', 'nightbus', f'Busiest: {top[0]}',
                 grouped(top[1]), grouped(top[1]), pin=True,
                 label_ko=f'가장 붐빔: {top[0]}번', place_en='Busiest', place_ko='가장 붐빔',
                 num=top[1], unit='people'),
            fact('busnight_second', 'nightbus', f'2nd-busiest: {second[0]}',
                 grouped(second[1]), grouped(second[1]), pin=True,
                 label_ko=f'두 번째로 붐빔: {second[0]}번', place_en='2nd-busiest',
                 place_ko='두 번째로 붐빔', num=second[1], unit='people'),
            fact('busnight_quietest', 'nightbus', f'Quietest: {bottom[0]}',
                 grouped(bottom[1]), grouped(bottom[1]), pin=True,
                 label_ko=f'가장 한산함: {bottom[0]}번', place_en='Quietest',
                 place_ko='가장 한산함', num=bottom[1], unit='people'),
            fact('busnight_total', 'nightbus', 'Total night-bus boardings',
                 grouped(nb['total']), grouped(nb['total']), pin=True,
                 label_ko='심야버스 전체 승차 인원', num=nb['total'], unit='people'),
        ]

    ws, why = weekend_swing(h, day, hol)
    if ws is None:
        print(f'Weekend swing withheld for {d}: {why}.')
    else:
        week = ws['week']
        RANKED_CARD_INFO['busweekend'] = {
            'day_en': _span_en(week[0], week[6]), 'day_ko': _span_ko(week[0], week[6]),
            # Fixed opener, Python's, same arrangement as busmovers (applied in
            # main()): his call, 11 September 2026, so the title says what the
            # two rows compare rather than whatever the selector reaches for.
            'opener_en': 'Seoul’s bus routes, weekend against weekday',
            'opener_ko': '서울의 버스 노선, 주말 대 평일',
            'note_en': ('Saturday and Sunday per day against Monday to Friday per day · '
                        'trunk and branch routes over 1,000 weekday boardings'),
            'note_ko': '토·일 하루 평균 대 월~금 하루 평균 · 평일 승차 1,000명 이상 간선·지선',
            'map_day': week[5],
            'map_caption': (f'Stops on each route on Saturday '
                            f'{en_date(_day_dt(week[5]))}: not necessarily its full path'),
            'map_routes': [(f'Holds up best: Route {no} ({_pct(r)})', MAP_COLOURS[i], no)
                           for i, (no, we, wk, r) in enumerate(ws['ups'])]
                          + [(f'Falls most: Route {no} ({_pct(r)})', MAP_COLOURS[2 + i], no)
                             for i, (no, we, wk, r) in enumerate(ws['downs'])]}
        # "Route" on these lines, unlike the other bus cards, his call on
        # 11 September 2026: the opener here is about weekends rather than
        # routes, so the line has to say what the number is. The Korean
        # carries 번 already.
        for i, (no, we, wk, r) in enumerate(ws['ups'] + ws['downs']):
            top = i < 1
            facts.append(fact(
                f'buswk_{"top" if top else "bottom"}1', 'busweekend',
                f'{"Holds up best" if top else "Falls most"}: Route {no}, {grouped(round(we))} a day',
                _pct(r), _pct(r), pin=True,
                label_ko=f'{"주말 강세" if top else "주말 약세"}: {no}번, 하루 {grouped(round(we))}명',
                place_en='Holds up best' if top else 'Falls most',
                place_ko='주말 강세' if top else '주말 약세'))
    return facts


def ranked_route_no(no):
    """True for a route number the ranking may use: a trunk (100-799) or
    branch (1000-7999) route. False for night, express, tailored, village
    and letter-suffixed routes, and for anything carrying Hangul."""
    return bool(no) and bool(_RANKED_ROUTE_RE.match(str(no)))


def bus_intensity_rank(h, day):
    """(result, reason): trunk/branch routes on `day` with at least
    BUS_MIN_STOPS stops served, ranked by boardings per stop served,
    largest first, plus the citywide average on the same measure."""
    if day not in h.get('days', {}) or day not in h.get('stops', {}):
        return None, f'{day} has no stop counts in history'
    rows = []
    for no, v in h['days'][day].items():
        if not ranked_route_no(no):
            continue
        n = h['stops'][day].get(no, 0)
        if n < BUS_MIN_STOPS:
            continue
        rows.append((no, v / n, v, n))
    if len(rows) < 3:
        return None, f'only {len(rows)} routes with {BUS_MIN_STOPS}+ stops served'
    rows.sort(key=lambda r: -r[1])
    return {'ranked': rows,
            'avg': sum(r[2] for r in rows) / sum(r[3] for r in rows)}, None


def bus_rank_streaks(h, day):
    """How long the day's busiest and quietest (per stop) have held those
    places, read from the history: (top, top_days, bottom, bottom_days,
    recorded, first_day). `recorded` is the run of consecutive days ending
    at `day` that the history can rank at all, and `first_day` its start;
    a streak equal to `recorded` is "every day recorded", which is all the
    record can say. A missing day ends the walk rather than being bridged."""
    r, _ = bus_intensity_rank(h, day)
    if r is None:
        return None
    top, bottom = r['ranked'][0][0], r['ranked'][-1][0]
    top_days = bottom_days = recorded = 0
    top_alive = bottom_alive = True
    d = day
    first = day
    while True:
        rr, _ = bus_intensity_rank(h, d)
        if rr is None:
            break
        recorded += 1
        first = d
        if top_alive and rr['ranked'][0][0] == top:
            top_days += 1
        else:
            top_alive = False
        if bottom_alive and rr['ranked'][-1][0] == bottom:
            bottom_days += 1
        else:
            bottom_alive = False
        d = (datetime.strptime(d, '%Y%m%d') - timedelta(days=1)).strftime('%Y%m%d')
    return top, top_days, bottom, bottom_days, recorded, first


def _streak_note(h, day):
    """(en, ko) footnote clause for the busroutes card, or ('', '')."""
    st = bus_rank_streaks(h, day)
    if not st:
        return '', ''
    top, td, bottom, bd, recorded, first = st
    fd = datetime.strptime(first, '%Y%m%d')
    first_en, first_ko = en_date(fd), f'{fd.month}월 {fd.day}일'
    # Sentences, with "Route" on them (the footnote has no opener to lean
    # on): his wording, 11 September 2026.
    if td >= BUS_ROUTE_STREAK_MIN and bd >= BUS_ROUTE_STREAK_MIN and td == bd == recorded:
        return (f'Route {top} was the busiest and Route {bottom} the quietest on every day '
                f'recorded, {recorded} since {first_en}.',
                f'기록된 {recorded}일({first_ko}부터) 내내 {top}번이 1위, {bottom}번이 최하위.')
    en, ko = [], []
    if td >= BUS_ROUTE_STREAK_MIN:
        if td == recorded:
            en.append(f'Route {top} has led on every day recorded, {recorded} since {first_en}.')
            ko.append(f'{top}번은 기록된 {recorded}일({first_ko}부터) 내내 1위.')
        else:
            en.append(f'Route {top} has led for the past {td} days.')
            ko.append(f'{top}번은 최근 {td}일간 매일 1위.')
    if bd >= BUS_ROUTE_STREAK_MIN:
        if bd == recorded:
            en.append(f'Route {bottom} was the quietest on every day recorded, {recorded} since {first_en}.')
            ko.append(f'{bottom}번은 기록된 {recorded}일({first_ko}부터) 내내 최하위.')
        else:
            en.append(f'Route {bottom} was the quietest for the past {bd} days.')
            ko.append(f'{bottom}번은 최근 {bd}일간 매일 최하위.')
    return ' '.join(en), ' '.join(ko)


def bus_routes_facts(h, day, d, d_ko):
    """The busroutes card for `day`, from the history (see BUS_MIN_STOPS's
    block): busiest, second-busiest and quietest route by boardings per stop
    served, and the citywide average on the same measure. Fills
    RANKED_CARD_INFO['busroutes'] when built; prints why when withheld."""
    RANKED_CARD_INFO.pop('busroutes', None)
    r, why = bus_intensity_rank(h, day)
    if r is None:
        print(f'Bus routes withheld for {d}: {why}.')
        return []
    top, second, bottom = r['ranked'][0], r['ranked'][1], r['ranked'][-1]
    streak_en, streak_ko = _streak_note(h, day)
    # His layout, 11 September 2026: the measure rides the dateline, the
    # rows are bare, and the stop counts go to the footnote as one sentence
    # between the eligibility rule and any streak. day_en stays the bare
    # date for the map's title and alt.
    stops_en = (f'Number of stops: Route {top[0]} ({top[3]}), {second[0]} ({second[3]}), '
                f'{bottom[0]} ({bottom[3]}).')
    stops_ko = f'정류장 수: {top[0]}번 ({top[3]}), {second[0]}번 ({second[3]}), {bottom[0]}번 ({bottom[3]}).'
    RANKED_CARD_INFO['busroutes'] = {
        'day_en': d, 'day_ko': d_ko,
        'dateline_en': f'Boardings per stop served on {d}',
        'dateline_ko': f'{d_ko} 정류장 1곳당 승차 인원',
        'opener_en': BUSROUTES_OPENER_EN, 'opener_ko': BUSROUTES_OPENER_KO,
        'note_en': ' '.join(x for x in (BUS_ROUTE_CAVEAT_EN, stops_en, streak_en) if x),
        'note_ko': ' '.join(x for x in (BUS_ROUTE_CAVEAT_KO, stops_ko, streak_ko) if x),
        'map_day': day,
        # Same wording and reason as history_bus_facts()'s caption.
        'map_caption': f'Stops on each route, {d}: not necessarily its full path',
        'map_routes': [(f'Busiest: Route {top[0]}', MAP_COLOURS[0], top[0]),
                       (f'2nd-busiest: Route {second[0]}', MAP_COLOURS[1], second[0]),
                       (f'Quietest: Route {bottom[0]}', MAP_COLOURS[2], bottom[0])]}
    # Bare route numbers, no "Route" on the line: his instruction, 10
    # September 2026, since the opener already says these are bus routes.
    # The stop count rode the label until 11 September 2026 and now sits in
    # the footnote, his call: it qualified every row where it belongs to
    # the method. The map legends keep "Route", having no opener to lean
    # on; the selector rule REQUIRES an opener naming buses.
    def line(fid, rank_en, rank_ko, row):
        no, per, tot, n = row
        return fact(fid, 'busroutes', f'{rank_en}: {no}',
                    grouped(round(per)), grouped(round(per)), pin=True,
                    label_ko=f'{rank_ko}: {no}번',
                    place_en=rank_en, place_ko=rank_ko)
    return [line('bus_busiest_route', 'Busiest', '가장 붐빔', top),
            line('bus_second_route', '2nd-busiest', '두 번째로 붐빔', second),
            line('bus_quietest_route', 'Quietest', '가장 한산함', bottom),
            # "per stop" on the line itself: the label check flagged the first
            # wording ("Average across all routes: 138") as saying nothing
            # about stops, and this line can be met alone in a hashtag feed.
            fact('bus_route_total', 'busroutes', 'Average per stop, all routes',
                 grouped(round(r['avg'])), grouped(round(r['avg'])), pin=True,
                 label_ko='정류장당 평균, 전체 노선')]


def transport_facts(api_key, state):
    """Subway + bus daily totals for the latest available date. Cached per-day in
    state so the second post of the day doesn't re-sum ~42 bus pages."""
    day, sub_total_rows = _latest_daily(api_key, 'CardSubwayStatsNew', True)
    if not day:
        return []
    hist = load_bus_history()
    cache = state.get('transport_cache', {})
    if (cache.get('date') == day and cache.get('bus_rank_rule') == BUS_RANK_RULE
            and cache.get('st_rule') == STATION_RANK_RULE
            and cache.get('stop_rule') == BUSSTOP_RANK_RULE
            and cache.get('gap_rule') == STATIONGAP_RULE):
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
        # ⚠️ Per-ROW busiest/quietest until 10 September 2026, which put
        # Gangnam (one row) above Seoul Station (five rows, one per line) and
        # a Korail halt in Paju at the bottom. Now summed per station, and
        # inside Seoul when the stations card's membership test is
        # available (see STATION_DAY's block); all-network summed when it is
        # not. Set below, once the stations block has run, from the same
        # ranking the stations card uses, so the two cards cannot disagree.
        busiest = srows[-1]
        sane = [x for x in srows if int(x['GTON_TNOPE']) >= 10]
        quietest = sane[0] if sane else srows[0]
        # Stations card: summed per station, Seoul only (see STATION_DAY's
        # block above). Its two coordinate feeds are its own dependency: a
        # failure there withholds THIS card and says so, and touches nothing
        # the transport and busroutes facts need.
        st_ranked, st_bottom, st_coords, st_count, gap = [], None, {}, 0, None
        # Fetched once here for both cards: the stations card's membership
        # test and the bus stops card's pins. A failure withholds both and
        # says so; the transport and busroutes facts do not need it.
        stop_xy = {}
        try:
            stop_xy = seoul_bus_stop_coord_map(api_key)
        except RuntimeError as e:
            print(f'Bus stop coordinates unavailable ({e}): the stations and bus stops cards are withheld.')
        try:
            coords = station_coords(api_key)
            if not stop_xy:
                raise RuntimeError('bus stop coordinates unavailable')
            in_seoul = stations_in_seoul(coords, list(stop_xy.values()))
            st_count = len(in_seoul)
            st_ranked, st_bottom = rank_stations(srows, in_seoul)
            gap = station_gap(srows, in_seoul)
            st_ranked = st_ranked[:2]
            st_coords = {n: coords[n] for n, _ in st_ranked + ([st_bottom] if st_bottom else [])
                         if n in coords}
        except RuntimeError as e:
            print(f'Stations card withheld: coordinate feeds unavailable ({e}).')
        # The transport vein's own busiest/quietest station lines: summed
        # per station, Seoul-only when membership is known, else all-network.
        if st_ranked and st_bottom:
            tr_busiest, tr_quietest = st_ranked[0], st_bottom
        else:
            all_ranked, all_bottom = rank_stations(srows, {fold_station(x['SBWY_STNS_NM']) for x in srows})
            tr_busiest = all_ranked[0] if all_ranked else (fold_station(busiest['SBWY_STNS_NM']), int(busiest['GTON_TNOPE']))
            tr_quietest = all_bottom or (fold_station(quietest['SBWY_STNS_NM']), int(quietest['GTON_TNOPE']))
        # Bus: page through the day.
        bd0 = http_get_json(f'{base}/CardBusStatisticsServiceNew/1/1/{day}')
        btot_rows = int(bd0['CardBusStatisticsServiceNew']['list_total_count'])
        bus_total = 0
        route = {}
        # DISTINCT stop ids per route, not rows: 62 of 326 trunk/branch routes
        # carry more rows than stops on a given day (2211: 51 rows, 40 stops,
        # 7 September 2026), and counting rows halved its boardings per stop.
        route_stop_ids = {}
        # Per STOP as well, for the bus stops card (see BUSSTOP_RANK_RULE's
        # block): the same rows, summed on the stop id across every route
        # that calls there, Seoul stops only.
        stop_sum, stop_nm = {}, {}
        for s in range(1, btot_rows + 1, 1000):
            bd = http_get_json(f'{base}/CardBusStatisticsServiceNew/{s}/{min(s + 999, btot_rows)}/{day}')
            for x in bd.get('CardBusStatisticsServiceNew', {}).get('row', []):
                v = int(x.get('GTON_TNOPE', '0') or 0)
                bus_total += v
                sid = str(x.get('STOPS_ID') or '')
                if sid.startswith('1'):
                    stop_sum[sid] = stop_sum.get(sid, 0) + v
                    if sid not in stop_nm:
                        stop_nm[sid] = stop_name(x.get('SBWY_STNS_NM') or '')
                if v > 0:
                    route_stop_ids.setdefault(x.get('RTE_NO', '?'), set()).add(x.get('STOPS_ID'))
                # Keyed on RTE_NO, not RTE_NM: a night ("N") route is split
                # across two RTE_IDs, one per direction, sharing one RTE_NO
                # (verified 9 Sep 2026, e.g. N13 = ids 11110363/11110364) — a
                # rider means both directions by "Route N13", so summing on
                # the number they'd recognise is the more honest total, not
                # a coincidence to guard against.
                no = x.get('RTE_NO', '?')
                route[no] = route.get(no, 0) + v
        # Every route's total goes into the history file (idempotent), for the
        # three history cards.
        if bus_history_add(hist, day, route, {no: len(ids) for no, ids in route_stop_ids.items()}):
            save_bus_history(hist)
        stop_ranked = rank_bus_stops(stop_sum, stop_nm)[:BUSSTOP_KEEP]
        c = {'date': day, 'sub_total': sub_total, 'bus_total': bus_total,
             'busiest_st': tr_busiest[0], 'busiest_v': tr_busiest[1],
             'quietest_st': tr_quietest[0], 'quietest_v': tr_quietest[1],
             # Which ranking rule built this cache; see BUS_RANK_RULE.
             'bus_rank_rule': BUS_RANK_RULE,
             'st_ranked': st_ranked, 'st_bottom': st_bottom, 'st_coords': st_coords,
             'st_in_seoul': st_count, 'st_rule': STATION_RANK_RULE,
             # The station gap card's day cache (see STATIONGAP_RULE).
             'gap': gap, 'gap_rule': STATIONGAP_RULE,
             # The bus stops card's day cache: the raw top BUSSTOP_KEEP stops,
             # how many stops took a boarding, and the pins' coordinates.
             'stop_ranked': stop_ranked,
             'stops_used': sum(1 for v in stop_sum.values() if v > 0),
             'stop_coords': {sid: stop_xy[sid] for sid, _, _ in stop_ranked if sid in stop_xy},
             'seoul_stop_count': len(stop_xy),   # the footnote's "There are N bus stops"
             'stop_rule': BUSSTOP_RANK_RULE}
        state['transport_cache'] = c

    dt = datetime.strptime(c['date'], '%Y%m%d')
    d = en_date(dt)
    d_ko = f'{dt.month}월 {dt.day}일'
    # All of these are pinned: the date says which day the count belongs to, and the
    # station/route identifiers are either looked up from the English name table or
    # need no lookup at all, so none of it is the selector's to reword away.
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
        # ⚠️ The date is now IN these two labels too, since 30 August 2026.
        # They are the only 'transport' facts with num/unit set (sub_total and
        # bus_total are not cross-eligible), so a CROSS_PAIR can post one of
        # them alone, with neither sibling total present to carry the date. A
        # live card did exactly that on 23 August 2026: a bare "Busiest subway
        # station, Gangnam: 90,066" sat under a tourism vein's "June 2026"
        # opener with nothing to say the figure was one day, not the month.
        # Pinned, so the selector cannot reword the date back out.
        fact('sub_busiest', 'transport',
             f'Busiest subway station, {en_name(c["busiest_st"], "stations")}, {d}',
             grouped(c['busiest_v']), grouped(c['busiest_v']), pair='station_gap', pin=True,
             label_ko=f'가장 붐빈 지하철역, {c["busiest_st"]} ({d_ko})',
             num=c['busiest_v'], unit='people'),
        fact('sub_quietest', 'transport',
             f'Quietest subway station, {en_name(c["quietest_st"], "stations")}, {d}',
             grouped(c['quietest_v']), grouped(c['quietest_v']), pair='station_gap', pin=True,
             label_ko=f'가장 한산한 지하철역, {c["quietest_st"]} ({d_ko})',
             num=c['quietest_v'], unit='people'),
    ]
    # The busroutes card, from the history (see bus_routes_facts()). A
    # separate category from 'transport' above, own post, never mixed.
    facts += bus_routes_facts(hist, c['date'], d, d_ko)

    # The stations card. Same all-or-nothing rule as busroutes: every one of
    # the three needs an official English name (en_lookup never invents one),
    # or the card is withheld and the log says which name was missing.
    st_top = c['st_ranked'][0] if c.get('st_ranked') else None
    st_second = c['st_ranked'][1] if len(c.get('st_ranked') or []) >= 2 else None
    st_bottom = c.get('st_bottom')
    STATION_DAY['en'] = STATION_DAY['ko'] = None
    STATION_MAP_INFO['day'] = STATION_MAP_INFO['stations'] = None
    RANKED_CARD_INFO.pop('stations', None)
    if st_top and st_second and st_bottom:
        st_en = {n: en_lookup(n, 'stations') for n, _ in (st_top, st_second, st_bottom)}
        missing = [n for n, e in st_en.items() if not e]
        coords = c.get('st_coords') or {}
        if missing:
            print(f'Stations card withheld for {d}: no English name for '
                  f'{", ".join(repr(m) for m in missing)}.')
        elif any(n not in coords for n in st_en):
            print(f'Stations card withheld for {d}: no coordinates for '
                  f'{", ".join(repr(n) for n in st_en if n not in coords)}.')
        else:
            STATION_DAY['en'], STATION_DAY['ko'] = d, d_ko
            STATION_MAP_INFO['day'] = c['date']
            # A registry entry for the fixed opener and the --daily guard's
            # data day only: no map_routes or map_pins, so the generic map
            # reply is not triggered; the station map keeps its own path
            # (STATION_MAP_INFO), unchanged.
            RANKED_CARD_INFO['stations'] = {
                'map_day': c['date'],
                'opener_en': STATIONS_OPENER_EN, 'opener_ko': STATIONS_OPENER_KO,
            }
            STATION_MAP_INFO['stations'] = [
                (f'Busiest: {st_en[st_top[0]]}', *coords[st_top[0]]),
                (f'2nd-busiest: {st_en[st_second[0]]}', *coords[st_second[0]]),
                (f'Quietest: {st_en[st_bottom[0]]}', *coords[st_bottom[0]]),
            ]
            facts += [
                fact('st_busiest', 'stations', f'Busiest: {st_en[st_top[0]]}',
                     grouped(st_top[1]), grouped(st_top[1]), pin=True,
                     label_ko=f'가장 붐빔: {st_top[0]}', place_en='Busiest',
                     place_ko='가장 붐빔', num=st_top[1], unit='people'),
                fact('st_second', 'stations', f'2nd-busiest: {st_en[st_second[0]]}',
                     grouped(st_second[1]), grouped(st_second[1]), pin=True,
                     label_ko=f'두 번째로 붐빔: {st_second[0]}', place_en='2nd-busiest',
                     place_ko='두 번째로 붐빔', num=st_second[1], unit='people'),
                fact('st_quietest', 'stations', f'Quietest: {st_en[st_bottom[0]]}',
                     grouped(st_bottom[1]), grouped(st_bottom[1]), pin=True,
                     label_ko=f'가장 한산함: {st_bottom[0]}', place_en='Quietest',
                     place_ko='가장 한산함', num=st_bottom[1], unit='people'),
                fact('st_total', 'stations', 'Total subway boardings',
                     grouped(c['sub_total']), grouped(c['sub_total']), pin=True,
                     label_ko='전체 지하철 승차 인원', num=c['sub_total'], unit='people'),
            ]
    elif c.get('st_rule') == STATION_RANK_RULE and c.get('st_in_seoul'):
        print(f'Stations card withheld for {d}: fewer than three rankable stations.')

    # The bus stops card, from the same day's rows (see BUSSTOP_RANK_RULE).
    facts += bus_stops_facts(c, d, d_ko)
    # The station gap card, from the same subway rows (see STATIONGAP_RULE).
    facts += station_gap_facts(c, d, d_ko)

    # The three history cards. kr_holidays() may have fetched a year into the
    # history's cache; keep it.
    before = json.dumps(hist.get('holidays', {}), sort_keys=True)
    facts += history_bus_facts(hist, c['date'], d, d_ko)
    if json.dumps(hist.get('holidays', {}), sort_keys=True) != before:
        save_bus_history(hist)
    return facts


def bus_route_map_stops(api_key, day, route_nos, with_served=False):
    """{route_no: [(lon, lat), ...]} ordered stop coordinates for `route_nos`
    on `day`, plus every Seoul-prefixed stop's own coordinates for the map's
    background silhouette. Returns (routes, seoul_stops), or with
    `with_served` a third value: {route_no: {'points': [(lon, lat), ...],
    'count': n}}, the stops the route SERVED that day (a boarding, either
    direction: transport_facts()' rule, the footnote's count) as coordinates
    where Seoul's stop table has them, and the full served count beside
    them, so the caption can say when one could not be placed.

    ⚠️ The LINE is not filtered on boardings: every stop the day's feed lists
    for the route's longest direction is drawn, boarded or not, so its stop
    count is not the card's "stops served". Until 12 September 2026 that was
    the only thing drawn, and a reader who counted found the map a stop or
    two off the footnote (2211: 40 drawn, 39 served); the served stops are
    drawn as dots on the line now, his ask, so the dots are the footnote.

    Fetched at POSTING time, not harvest time: transport_facts() already
    pages the whole day's CardBusStatisticsServiceNew once to find the day's
    ranking, but only keeps the aggregated totals, not each stop's own row —
    keeping all ~40,000 of those in state just in case this vein posts would
    bloat a JSON file written every run for data needed on the rare run
    (3-day cooldown) that actually does. A second full-day pass here is the
    simpler trade, paid only on that rare run.

    The stop order comes from the same trick seoul-transit-art/harvest.py's
    route_paths() verified against real coordinates: each row's own stop-name
    field ends in a bracketed sequence number, "명륜3가.성대입구(00030)" — real
    published order, not a guess.
    """
    base = f'http://openapi.seoul.go.kr:8088/{api_key}/json'
    stop_total = int(http_get_json(f'{base}/busStopLocationXyInfo/1/1')
                     ['busStopLocationXyInfo']['list_total_count'])
    stops = {}
    for s in range(1, stop_total + 1, 1000):
        d = http_get_json(f'{base}/busStopLocationXyInfo/{s}/{min(s + 999, stop_total)}')
        for r in d.get('busStopLocationXyInfo', {}).get('row', []):
            try:
                stops[r['STOPS_NO']] = (float(r['XCRD']), float(r['YCRD']))
            except (KeyError, TypeError, ValueError):
                continue
    seoul_stops = [c for sid, c in stops.items() if sid.startswith('1')]

    bd0 = http_get_json(f'{base}/CardBusStatisticsServiceNew/1/1/{day}')
    btot = int(bd0['CardBusStatisticsServiceNew']['list_total_count'])
    wanted = set(route_nos)
    # ⚠️ Keyed on (route number, RTE_ID), not the route number alone. A route
    # can run as two RTE_IDs, one per direction (every N night route does —
    # N13 is 11110363/11110364), and each direction numbers its stops from
    # 1, so sorting the two sequences together interleaves outbound stop 1,
    # inbound stop 1, outbound 2 … and draws a zigzag across the city. Seen
    # on the first night-bus map, 10 September 2026: N61 and N15 came out as
    # scribbles. The longest direction is drawn; the other retraces it.
    seq = {}
    served_ids = {no: set() for no in wanted}
    for s in range(1, btot + 1, 1000):
        bd = http_get_json(f'{base}/CardBusStatisticsServiceNew/{s}/{min(s + 999, btot)}/{day}')
        for x in bd.get('CardBusStatisticsServiceNew', {}).get('row', []):
            no = x.get('RTE_NO')
            if no not in wanted:
                continue
            if int(x.get('GTON_TNOPE', '0') or 0) > 0:
                served_ids[no].add(x.get('STOPS_ID'))
            m = re.search(r'\((\d+)\)\s*$', x.get('SBWY_STNS_NM') or '')
            sid = x.get('STOPS_ID')
            if not m or sid not in stops:
                continue
            seq.setdefault((no, x.get('RTE_ID')), []).append((int(m.group(1)), sid))

    routes = {no: [] for no in wanted}
    for (no, _rid), pairs in seq.items():
        pairs.sort()
        ids, seen = [], set()
        for _, sid in pairs:
            if sid not in seen and sid.startswith('1'):
                seen.add(sid)
                ids.append(sid)
        if len(ids) > len(routes[no]):
            routes[no] = [stops[sid] for sid in ids]
    if not with_served:
        return routes, seoul_stops
    served = {no: {'points': [stops[sid] for sid in sorted(ids)
                              if sid in stops and str(sid).startswith('1')],
                   'count': len(ids)}
              for no, ids in served_ids.items()}
    return routes, seoul_stops, served


# --- rush hour -------------------------------------------------------------
# Added 25 August 2026. transport_facts() above counts a whole DAY through the
# turnstiles; this reads the same turnstiles along a CLOCK, from CardSubwayTime:
# monthly, per line x station, 24 hourly boarding columns, published back to
# January 2015 and never used here until now.
#
# The card it exists to make is ONE STATION AT TWO HOURS. 종각 took 10,872
# boardings at 8 in the morning in July 2026 and 227,972 at 6 in the evening,
# twenty-one times as many: nobody boards at 종각 in the morning because nobody
# sleeps there. No comment attached, which is the house style exactly.
#
# ⚠️ REWORKED 1 September 2026, at the user's request, from two stations (one
# of each kind, four lines) to one. A second station of the opposite kind
# (신정, 77,726 against 15,585, the sleeping side of the same axis) was shown
# alongside it until this date; dropped because the single swing already
# carries the point on its own, and a reader does not need a second place to
# read it. Which station is shown is still measured every run, never
# hardcoded — whichever end of the axis swings harder that month, so the vein
# does not always surface the same kind of place.
#
# ⚠️ CARDSUBWAYTIME SOMETIMES SERVES EVERY ROW TWICE, BYTE-IDENTICAL. July 2026
# returns 1,242 rows that are 621 real ones: same line, same station, same 48
# figures, not one field differing. ⚠️ It is NOT every month, which is why this
# is a de-duplication and must never be turned into a divide-by-two: measured
# 25 August 2026, May returns 620 rows, June 621, July 1,242. Summed naively every figure here is exactly double,
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
RUSH_STATIONS = 2           # minimum qualifying stations before either end of
                            # the axis is trusted as a real extreme
# Held out of the pool from 25 August 2026, the evening it was built, until
# 1 September 2026. In that window it was reworked from two stations to one
# (see the block comment above), given a fixed opener and bolded station name,
# and the side alternates rather than always favoring the more dramatic one
# — all reviewed against real dry-run cards before this was armed.
RUSH_LIVE = True


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
            # ⚠️ A station with no English name is SKIPPED, not romanized and not
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
        # ⚠️ ALTERNATE the side, do not pick whichever swings harder. Measured
        # 1 Sept 2026: the top 12 stations by |ratio| were ALL evening-heavy
        # office stops — not one residential morning station came close — so
        # "most dramatic wins" would have shown the same KIND of place every
        # single time. User's call, same day. Which STATION wins within a side
        # is still measured fresh every month, never hardcoded — only which
        # side gets a turn is state-driven, the same last-vs-this rule
        # promote_starved() already uses to stop two of anything running in a
        # row.
        side = 'am' if state.get('rush_last_side') == 'pm' else 'pm'
        extreme = scored[-1] if side == 'am' else scored[0]
        state['rush_last_side'] = side
        picks = [list(extreme[1:])]
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
                              num=v, unit='people',
                              place_en=en, place_ko=st))
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
             # Seoul Library's catalog, added 21 Aug 2026 from the portal
             # sweep. It is ONE library's holdings, not the city's, which is
             # why the label names it rather than saying 'Books in Seoul'.
             ('holdings', 'SeoulLibraryBookSearchInfo',
              'Items in Seoul Library’s catalog', '서울도서관 소장자료 수'),
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


def bike_counts(api_key):
    """Citywide Ttareungi totals right now: (stations, bikes, racks, empty),
    or None if the sweep could not be completed.

    ⚠️ ONE IMPLEMENTATION, TWO CONSUMERS. bike_facts() builds a card from this
    and seoul_index_crowd_log.py archives it hourly. It was extracted on
    27 August 2026 rather than copied, because a second copy of the paging
    below would drift from this one and scripts_tidy.sh check 3b exists
    precisely because that has already happened elsewhere in this estate.

    The bikeList service returns one row per docking station — bikes currently
    parked (parkingBikeTotCnt), rack capacity (rackTotCnt) and occupancy — and
    refreshes continuously (data.seoul.go.kr 갱신주기 '수시'). ~2,700 stations in
    pages of 1,000. Unlike CardBus/CardSubway, bikeList's list_total_count only
    echoes the page size, so it is NO USE as a grand total: page until a short
    page instead (verified live on the Mini 28 Jul 2026 — 1000+1000+742 rows).

    ⚠️ NONE, NOT ZEROS, on a failed sweep. A half-read city looks exactly like a
    quiet one — plausible totals, all of them too low — and an archive of those
    is worse than a gap, because a gap announces itself and a wrong reading does
    not. The caller decides what to do about it.
    """
    base = f'http://openapi.seoul.go.kr:8088/{api_key}/json/bikeList'
    stations = bikes = racks = empty = 0
    start, page = 1, 1000
    while start <= 20000:   # safety cap, far above the ~2,700 real stations
        try:
            d = http_get_json(f'{base}/{start}/{start + page - 1}/')
        except RuntimeError:
            return None   # a network failure would misreport the citywide totals
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
    return (stations, bikes, racks, empty) if stations else None


def bike_facts(api_key):
    """Live Ttareungi (public-bike) numbers, citywide, right now.

    Aggregate-only, by design: station names come back as messy Korean strings
    ('102. 망원역 1번출구 앞') that the English name table does not carry, so a
    named line would fall back to Korean on the English card. The citywide totals
    carry the story without names, and stay fully owned by Python."""
    got = bike_counts(api_key)
    if not got:
        return []
    stations, bikes, racks, empty = got
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
                          + f', {en_date(stamp_dt)}')
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
#     describing water surfaces forty-eight meters apart. A card setting them in
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
                          + f', {en_date(when)}')
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
# and nothing modeled. The same shape as the property vein's dearest/cheapest.
#
# ⚠️ The feed is NEWEST-FIRST. Reading from the end returns January 2025 and
# looks like a dead archive; row 1 is a few days old. This cost a wrong
# conclusion during the build, and it is the second time in one evening a Seoul
# API's ordering did that (see the HRFCO note in level_facts).
#
# ⚠️ Shops are labeled by DISTRICT and KIND, never by name. The feed carries
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
            PRICE_PERIOD['en'] = f'{en_date(d)}'
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
# 정수센터 (purification centers, measuring 취수 intake and 송수 transmission) and
# 수도사업소 (district waterworks offices, measuring 공급량 supplied) — and the
# vein uses ONE measure at a time so the lines are actually comparable. Setting
# an intake figure beside a supply figure would look like a ranking of places
# and be a comparison of two different things.
WATER_SVC = 'WoWcbsDayStatic'
WATER_ROWS = 300
WATER_MIN_LINES = 3
# The five purification centers, curated rather than romanized: 뚝도 is not the
# 뚝섬 in the station table and would take its name wrongly.
WATER_SITES = {'암사': 'Amsa', '강북': 'Gangbuk', '뚝도': 'Ttukdo',
               '구의': 'Guui', '영등포': 'Yeongdeungpo'}
WATER_PERIOD = {'en': None, 'ko': None}


def water_facts(api_key):
    """Water drawn at each of Seoul's purification centers, on one day."""
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
        # a center is never called bigger or busier than another, and a leading
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
        WATER_PERIOD['en'] = f'Purification centers, {en_date(dt)}'
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
# ⚠️ 생활인구 is KT-modeled, not counted, exactly like the crowd vein, so these
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
        DAYNIGHT_PERIOD['en'] = f'{en_date(dt)}'
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
# nothing here is modeled or averaged.
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
        # count. A "10,921 · 1 in 65" form would have made every value
        # unparseable and silently dropped the size sort on this card.
        n = round(pop[band] / total) if pop.get(band) else 0
        if n >= LIBRARY_POP_MIN:
            v_en, v_ko = f'{v_en} (1 in {grouped(n)})', f'{v_ko} ({grouped(n)}명 중 1명)'
            ratios += 1
        # ⚠️ NO num/unit here, deliberately, since 30 August 2026. Library
        # membership is a static, undated running total, unlike every other
        # cross-eligible vein, which is bound to a day, month, quarter or year.
        # cross_vein_pairs() used to collide it against anything else in the
        # 10,000-20,000 range regardless of what that other figure counted, and
        # it produced two live posts pairing library members by age band
        # against unrelated live crowd counts: a numeric coincidence between
        # two unrelated groups of people, not a real juxtaposition. Two
        # magnitudes landing close together is not, on its own, interesting.
        facts.append(fact(f'library_{band}', 'library', en, v_en, v_ko,
                          pair='library_ages', pin=True, label_ko=ko))
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


def en_date(dt):
    """'September 7': month first on this bot's English cards, his call on
    11 September 2026. House style everywhere else is day first ("7
    September"); here the Korean card beside it runs 9월 7일, month then day,
    and Seoul's own English site writes "September 1", so the English follows
    the order a Korean reader of English already meets. Every English date
    the bot writes goes through here (or _span_en), so the order is one edit."""
    return f'{MONTHS_EN[dt.month - 1]} {dt.day}'

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


# The three fact ids whose label uses the word "jeonse" without explaining it.
# compose() glosses the term once, in the footnote, when any of these is picked.
JEONSE_IDS = {'apt_top_jeonse', 'lease_jeonse_n', 'lease_wolse_n'}
JEONSE_NOTE_EN = ('Jeonse is a lump-sum deposit paid instead of monthly rent, '
                  'refunded when the lease ends')


# --- weather (KMA ASOS, the official Seoul station) ------------------------
# 기상청's daily surface observations for station 108 — the Seoul reference
# station, observing since 1907 — via data.go.kr (자동승인, approved 22 Jul
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

# ⚠️ 1907, not 1904. This read "since 1904" until 26 August 2026, which is the
# year Korea's modern observation network began (the 임시관측소 at 인천, 부산,
# 목포), NOT the year Seoul's station started. 경성측후소 opened in 낙원동 in 1907
# and moved to the present site in 1933. Settled against the bot's OWN source
# rather than a history page: station 108's first daily row in this API is
# 1907-10-01, and every span before it returns NO_DATA. The figure is now
# printed on the source reply, so getting it wrong would publish it.
WX_OBSERVING_SINCE = 1907

# The season-to-date span, filled in by kma_facts when it offers that frame.
# The rows say "Summer 2026" and "Summer 1976"; only this says which days that
# actually counts, and the window is still growing, so it is not decoration.
# Empty when no season frame is on the card. Same shape as MOLIT_M / SALES_Q.
WX_SEASON = {'en': None, 'ko': None}

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


# --- yesterday at the weather station -----------------------------------------
# His call, 11 September 2026 ("Fix ... yesterday's rainfall"). Three lines
# from station 108's row for yesterday, published by KMA the next morning:
# high, low and rain, as its own post with a fixed opener and the date on
# the dateline. ⚠️ The AVERAGE line was cut on 12 September 2026, his call
# ("we don't need the average temperature line going forward"); the row's
# avgTa is no longer read. ⚠️ The rain field is BLANK on a day with no
# precipitation, not 0.0: measured on the finalised rows for 25 August to
# 10 September 2026, dry days (Open-Meteo 0.0 mm) read '' and a trace day
# reads '0.0'. So a blank prints "None" / "없음", which is a reading, not a
# missing one; a missing high or low withholds the card. Three-day
# cooldown like the other daily cards.
WXDAY_COOLDOWN_DAYS = 3
# His wording, 11 September 2026: the first opener ("Yesterday at Seoul's
# weather station") implied the city has one station; it has one REFERENCE
# station and a network of automatic ones, and the footnote names which.
WXDAY_OPENER_EN = 'Seoul’s weather yesterday'
WXDAY_OPENER_KO = '어제 서울의 날씨'


def wx_day_emoji(row):
    """The title glyph for yesterday, from the row's own published fields,
    his call, 11 September 2026. Snow (ddMefs, fresh snow depth) beats rain
    (sumRn, a millimetre or more; a trace 0.0 or 0.3 is not a rainy day),
    then KMA's own cloud-cover bands on avgTca in tenths: 맑음 to 5, 구름많음
    to 8, 흐림 above. Measured 25 August to 10 September 2026: 31 August
    (34.8 mm) reads 🌧, 3 to 8 September (0.3 to 2.3 tenths, 11 hours of
    sun) ☀️, 10 September (7.1 tenths, 0.2 hours) ⛅. A row with no cloud
    field falls back to the neutral 🌤."""
    if (_wx_num(row, 'ddMefs') or 0) > 0:
        return '🌨'
    if (_wx_num(row, 'sumRn') or 0) >= 1.0:
        return '🌧'
    cloud = _wx_num(row, 'avgTca')
    if cloud is None:
        return '🌤'
    return '☀️' if cloud <= 5 else '⛅' if cloud <= 8 else '☁️'


def wx_day_facts(key):
    """The wxday card: yesterday's published readings from station 108.
    Fills RANKED_CARD_INFO when built; prints why when withheld."""
    RANKED_CARD_INFO.pop('wxday', None)
    if not key:
        return []
    yday = datetime.now(SEOUL_TZ).date() - timedelta(days=1)
    rows = _wx_rows(key, f'{yday:%Y%m%d}', f'{yday:%Y%m%d}')
    d, d_ko = en_date(yday), f'{yday.month}월 {yday.day}일'
    if not rows:
        print(f'Weather-day card withheld: no row yet for {d}.')
        return []
    r = rows[0]
    hi, lo, rn = _wx_num(r, 'maxTa'), _wx_num(r, 'minTa'), _wx_num(r, 'sumRn')
    if hi is None or lo is None:
        print(f'Weather-day card withheld: high or low missing for {d}.')
        return []
    RANKED_CARD_INFO['wxday'] = {
        'day_en': d, 'day_ko': d_ko,
        'opener_en': WXDAY_OPENER_EN, 'opener_ko': WXDAY_OPENER_KO,
        'dateline_en': d, 'dateline_ko': d_ko,
        'note_en': f'Seoul’s reference station, observing since {WX_OBSERVING_SINCE}',
        'note_ko': f'서울 대표 관측소, {WX_OBSERVING_SINCE}년 관측 개시',
        # One glyph per line, his call; the rain line keeps its glyph on a
        # dry day, since the line is still about rain. The title's is the
        # day's own weather (wx_day_emoji), also his call.
        'line_emoji': {'High': '🔺', 'Low': '🔻', 'Rain': '🌧',
                       'Sunshine': '🌞', 'Snow': '❄️'},
        'emoji': wx_day_emoji(r)}
    facts = [fact('wxday_hi', 'wxday', 'High', to_f(hi), f'{hi:.1f}°C', pin=True, label_ko='최고기온'),
             fact('wxday_lo', 'wxday', 'Low', to_f(lo), f'{lo:.1f}°C', pin=True, label_ko='최저기온')]
    rain_en, rain_ko = (f'{rn:.1f}mm', f'{rn:.1f}mm') if rn is not None else ('None', '없음')
    facts.append(fact('wxday_rain', 'wxday', 'Rain', rain_en, rain_ko, pin=True, label_ko='강수량'))
    # Sunshine hours, his call, 11 September 2026: on a dry day it says what
    # "Rain: None" cannot (3 September 11.0 hours, 10 September 0.2). And
    # fresh snow, only on a day it fell, the seasonal-line pattern.
    ss = _wx_num(r, 'sumSsHr')
    if ss is not None:
        facts.append(fact('wxday_sun', 'wxday', 'Sunshine', f'{ss:.1f} hours', f'{ss:.1f}시간',
                          pin=True, label_ko='일조시간'))
    sn = _wx_num(r, 'ddMefs')
    if sn:
        facts.append(fact('wxday_snow', 'wxday', 'Snow', f'{sn:.1f}cm', f'{sn:.1f}cm',
                          pin=True, label_ko='신적설'))
    return facts


def kma_facts(key):
    """Weather lines: the last full month set against the same month fifty
    years earlier, and in summer the season to date. Yesterday's readings
    are wx_day_facts()'s card since 11 September 2026."""
    if not key:
        return []
    today = datetime.now(SEOUL_TZ).date()
    facts = []

    # Yesterday's readings were the vein's third frame until 11 September
    # 2026 and never once reached a card: the selector is told to pick one
    # frame and picked the fifty-year one every time (zero cards with a
    # "yesterday" line in the whole history), so the rain line, which needs
    # a wet day AND that frame, could not fire. They are their own card now:
    # wx_day_facts() below.
    # The date itself still bounds the season-to-date window further down.
    yday = today - timedelta(days=1)

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
        per_en, per_ko = f'{mon_en} {y}', f'{y}년 {mon}월'
        if ex['hot']:
            v = _wx_num(ex['hot'], 'maxTa')
            facts.append(fact(f'wx_hot_{side}', 'weather',
                              f'Hottest day, {mon_en} {y}',
                              to_f(v), f'{v:.1f}°C', pair='wx_heat_then',
                              pin=True, label_ko=f'가장 더웠던 날, {y}년 {mon}월',
                              head_en='Hottest day', head_ko='가장 더웠던 날',
                              period_en=per_en, period_ko=per_ko))
        if ex['wet']:
            v = _wx_num(ex['wet'], 'sumRn')
            facts.append(fact(f'wx_wet_{side}', 'weather',
                              f'Wettest day, {mon_en} {y}',
                              f'{v:.1f}mm', f'{v:.1f}mm', pair='wx_rain_then',
                              pin=True,
                              label_ko=f'비가 가장 많이 온 날, {y}년 {mon}월',
                              head_en='Wettest day',
                              head_ko='비가 가장 많이 온 날',
                              period_en=per_en, period_ko=per_ko))
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
                                  label_ko=f'{ko_label}, {y}년 {mon}월',
                                  head_en=en_label, head_ko=ko_label,
                                  period_en=f'{mon_en} {y}',
                                  period_ko=f'{y}년 {mon}월'))

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
            span_en = f'June 1–{en_date(yday)}'
            span_ko = f'6월 1일–{yday.month}월 {yday.day}일'
            # Published for the source reply. The rows carry "Summer 2026", a
            # word the exact window has to stand behind — and the window is
            # still growing, so it cannot be left to the reader to assume.
            WX_SEASON['en'], WX_SEASON['ko'] = span_en, span_ko
            sides = (('now', s_now, yday.year),
                     ('then', s_then, yday.year - WX_YEARS_BACK))
            for side, ex, y in sides:
                per_en, per_ko = f'Summer {y}', f'{y}년 여름'
                if ex['hot']:
                    v = _wx_num(ex['hot'], 'maxTa')
                    facts.append(fact(f'wx_s_hot_{side}', 'weather',
                                      f'Hottest day, {span_en} {y}',
                                      to_f(v), f'{v:.1f}°C', pair='wx_s_hot',
                                      pin=True,
                                      label_ko=f'가장 더웠던 날, {y}년 {span_ko}',
                                      head_en='Hottest day',
                                      head_ko='가장 더웠던 날',
                                      period_en=per_en, period_ko=per_ko))
                if ex['wet']:
                    v = _wx_num(ex['wet'], 'sumRn')
                    facts.append(fact(f'wx_s_wet_{side}', 'weather',
                                      f'Wettest day, {span_en} {y}',
                                      f'{v:.1f}mm', f'{v:.1f}mm', pair='wx_s_wet',
                                      pin=True,
                                      label_ko=f'비가 가장 많이 온 날, {y}년 {span_ko}',
                                      head_en='Wettest day',
                                      head_ko='비가 가장 많이 온 날',
                                      period_en=per_en, period_ko=per_ko))
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
                                          label_ko=f'{ko_label}, {y}년 {span_ko}',
                                          head_en=en_label, head_ko=ko_label,
                                          period_en=f'Summer {y}',
                                          period_ko=f'{y}년 여름'))
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
    # ⚠️ Every label here carries its own month AND the fact carries it again as
    # a period. Both are needed and neither is redundant: on the twenty-year
    # pair the month is the DISCRIMINATOR ("July 2026" against "July 2006") and
    # has to stay on the row, while on a single-month card it is the same on
    # every row and belongs on the masthead. compose() lifts the period to the
    # dateline and strips it back off the labels only when the whole card is one
    # month, so the two frames each get the layout they need.
    per_en, per_ko = f'{mon_en} {y}', f'{y}년 {m}월'
    facts = [fact('kac_pax_now', 'airport',
                  f'Passengers through Gimpo, {per_en}',
                  grouped(now['pax']), grouped(now['pax']), pair='gimpo_then',
                  pin=True, label_ko=f'김포공항 이용객, {per_ko}',
                  period_en=per_en, period_ko=per_ko,
                  num=now['pax'], unit='people'),
             fact('kac_flights_now', 'airport',
                  f'Flights in and out, {per_en}',
                  grouped(now['flights']), grouped(now['flights']),
                  pin=True, label_ko=f'운항 편수, {per_ko}',
                  period_en=per_en, period_ko=per_ko)]
    then = _kac_month(key, y - KAC_YEARS_BACK, m)
    if then:
        then_en = f'{mon_en} {y - KAC_YEARS_BACK}'
        then_ko = f'{y - KAC_YEARS_BACK}년 {m}월'
        facts.append(fact('kac_pax_then', 'airport',
                          f'Passengers through Gimpo, {then_en}',
                          grouped(then['pax']), grouped(then['pax']),
                          pair='gimpo_then', pin=True,
                          label_ko=f'김포공항 이용객, {then_ko}',
                          period_en=then_en, period_ko=then_ko))
    dom = _kac_month(key, y, m, route=0)
    intl = _kac_month(key, y, m, route=1)
    if dom and intl:
        for fid, row, en, ko in (
                ('kac_dom', dom, 'Domestic passengers', '국내선 이용객'),
                ('kac_intl', intl, 'International passengers', '국제선 이용객')):
            facts.append(fact(fid, 'airport', f'{en}, {per_en}',
                              grouped(row['pax']), grouped(row['pax']),
                              pair='gimpo_split', pin=True,
                              label_ko=f'{ko}, {per_ko}',
                              period_en=per_en, period_ko=per_ko,
                              num=row['pax'], unit='people'))
    return facts


# --- the world through Incheon (IIAC) ---------------------------------------
# 인천국제공항공사's airline/route transport performance, via data.go.kr
# (자동승인, approved 2 Sep 2026). One row per airline/route/direction/
# regularity/month: paid/unpaid/transfer passengers, flight counts, and
# cargo/mail/baggage weight. The API always serves whatever month it most
# recently published — no year/month parameter — so there is no then/now
# comparison here, only a snapshot, unlike kac_facts()'s twenty-year pair.
# ⚠️ A near-identical-looking sibling API, AviationStatsByAirport (also
# B551177), states in its own description that it stopped updating after
# October 2024 and was dropped from IIAC's managed dataset list. This vein
# is operationPerformanceByRoute, the one still current — see
# [[project_seoul_data_source_candidates]] for how the two were told apart.

IIAC_BASE = ('http://apis.data.go.kr/B551177/operationPerformanceByRoute/'
             'getOperationPerformanceByRoute'
             '?serviceKey={key}&pageNo=1&numOfRows=1000&type=json')

# Official English names for the country field the tables are most likely to
# surface. An unmapped name falls back to Korean with a warning, same as
# CULTURE_EN above.
IIAC_COUNTRY_EN = {
    '일본': 'Japan', '중국': 'China', '베트남': 'Vietnam',
    '미국': 'the United States', '대만': 'Taiwan', '홍콩': 'Hong Kong',
    '태국': 'Thailand', '필리핀': 'the Philippines', '싱가포르': 'Singapore',
    '말레이시아': 'Malaysia', '인도네시아': 'Indonesia', '몽골': 'Mongolia',
    '독일': 'Germany', '프랑스': 'France', '영국': 'the United Kingdom',
    '캐나다': 'Canada', '호주': 'Australia', '터키': 'Turkey',
    '카타르': 'Qatar', '아랍에미리트': 'the United Arab Emirates',
    '우즈베키스탄': 'Uzbekistan', '캄보디아': 'Cambodia', '인도': 'India',
}


def _iiac_rows(key):
    """This month's passenger rows (paxCode == '여객'), or []."""
    url = IIAC_BASE.format(key=key)
    r = subprocess.run(['curl', '-s', '--max-time', '30', '-A', MOLIT_UA, url],
                       capture_output=True, text=True)
    try:
        items = json.loads(r.stdout)['response']['body']['items']
    except (ValueError, KeyError, TypeError):
        return []
    if not isinstance(items, list):
        return []
    return [x for x in items if x.get('paxCode') == '여객']


def iiac_facts(key):
    """Incheon's newest published month: total traffic and the busiest
    destination country. Counting, not modelling: every figure here is a
    straight sum of the API's own per-row passenger/flight counts."""
    if not key:
        return []
    rows = _iiac_rows(key)
    if not rows:
        return []
    ym = str(rows[0].get('yearMonth') or '')
    if len(ym) != 6:
        return []
    y, m = int(ym[:4]), int(ym[4:])
    per_en, per_ko = f'{MONTHS_EN[m - 1]} {y}', f'{y}년 {m}월'

    total_pax = total_flights = 0
    by_country = {}
    for it in rows:
        try:
            pax = int(it.get('totalEff') or 0)
            flights = int(it.get('flightCount') or 0)
        except (TypeError, ValueError):
            continue
        total_pax += pax
        total_flights += flights
        nation = (it.get('nationName') or '').strip()
        if nation:
            by_country[nation] = by_country.get(nation, 0) + pax
    if not total_pax:
        return []

    facts = [fact('iiac_pax_total', 'incheon',
                  f'Passengers through Incheon, {per_en}',
                  grouped(total_pax), grouped(total_pax), pin=True,
                  label_ko=f'인천공항 이용객, {per_ko}',
                  period_en=per_en, period_ko=per_ko,
                  num=total_pax, unit='people'),
             fact('iiac_flights_total', 'incheon',
                  f'Flights in and out, {per_en}',
                  grouped(total_flights), grouped(total_flights), pin=True,
                  label_ko=f'운항 편수, {per_ko}',
                  period_en=per_en, period_ko=per_ko)]

    top = max(by_country.items(), key=lambda kv: kv[1], default=None)
    if top:
        ko_name, n = top
        en = IIAC_COUNTRY_EN.get(ko_name)
        if not en:
            print(f'Warning: no English name for {ko_name!r} — '
                  f'using Korean on the English card.')
            en = ko_name
        facts.append(fact('iiac_top_country', 'incheon',
                          f'Passengers to {en}, {per_en}',
                          grouped(n), grouped(n), pin=True,
                          label_ko=f'{ko_name} 노선 이용객, {per_ko}',
                          period_en=per_en, period_ko=per_ko,
                          num=n, unit='people'))
    return facts


# --- Rescued animals (국가동물보호정보시스템) --------------------------------
# The Animal and Plant Quarantine Agency's rescue-animal register via
# data.go.kr (15098931, 자동승인, approved 11 September 2026): one row per
# rescue notice, with the rescue date (happenDt), the species (upKindNm: 개,
# 고양이 or 기타), the district that filed it and what became of the animal.
# Counting, not modelling: the card is a straight count of Seoul's rows
# over one week, split by species.
#
# ⚠️ A notice lags its rescue. Measured on 349 Seoul rows for 12 August to
# 10 September 2026: 20% are filed the same day, 54% the next, 15% two days
# on, and the tail runs to a week (94% within three days). A week ending
# yesterday would undercount its last days on every card, so the window
# ends RESCUE_LAG_DAYS ago and the count is what the register held on the
# day the card was built. Rolling seven days rather than Monday-to-Sunday,
# so the card is never nine days stale on a Monday; the cooldown stops two
# cards overlapping.
# ⚠️ The Seoul filter is upr_cd=6110000, the register's own code for the
# city (sido_v2), and bgnde/endde filter on happenDt, the rescue date, not
# the notice date (measured: the happenDt range of a window is exactly the
# window; noticeSdt spills past its end).
# ⚠️ A zero-row week is an outage, not a quiet week: the smallest weekly
# count in six weeks of history was 31, so an empty answer withholds.
RESCUE_BASE = ('https://apis.data.go.kr/1543061/abandonmentPublicService_v2/'
               'abandonmentPublic_v2?serviceKey={key}&_type=json&numOfRows=1000'
               '&pageNo={page}&upr_cd=6110000&bgnde={a}&endde={b}')
RESCUE_SEOUL_CODE = '6110000'
RESCUE_LAG_DAYS = 3
RESCUE_WINDOW_DAYS = 7
RESCUE_COOLDOWN_DAYS = 7
RESCUE_MAX_PAGES = 5
RESCUE_OPENER_EN = 'Animals rescued in Seoul'
RESCUE_OPENER_KO = '서울에서 구조된 동물'
RESCUE_NOTE_EN = ('Rescue notices filed by Seoul’s 25 districts on the national '
                  'animal protection register, counted by the day of rescue')
RESCUE_NOTE_KO = '서울 25개 자치구가 국가동물보호정보시스템에 올린 구조 공고, 구조일 기준'
RESCUE_SPECIES = [('고양이', 'Cats', '고양이', '🐈'),
                  ('개', 'Dogs', '개', '🐕'),
                  ('기타', 'Other', '그 밖의 동물', '🐢')]


def _rescue_rows(key, a, b):
    """Every Seoul row rescued between a and b (YYYYMMDD, inclusive), paged
    to the register's own totalCount, or None when the read could not be
    trusted: a failed call, a short page, or more pages than RESCUE_MAX_PAGES
    (a week is under a hundred rows; five pages is five thousand)."""
    rows, total = [], None
    for page in range(1, RESCUE_MAX_PAGES + 1):
        url = RESCUE_BASE.format(key=key, page=page, a=a, b=b)
        r = subprocess.run(['curl', '-s', '--max-time', '30', '-A', MOLIT_UA, url],
                           capture_output=True, text=True)
        try:
            body = json.loads(r.stdout)['response']['body']
            total = int(body['totalCount'])
            items = body['items']['item'] if body.get('items') else []
        except (ValueError, KeyError, TypeError):
            return None
        if not isinstance(items, list):
            return None
        rows += items
        if len(rows) >= total:
            break
    else:
        return None
    if total is None or len(rows) != total:
        return None
    return rows


def rescue_facts(key):
    """The rescue card: one week of Seoul's rescue notices, the total and
    the split by species. Fills RANKED_CARD_INFO when built; prints why
    when withheld."""
    RANKED_CARD_INFO.pop('rescue', None)
    if not key:
        return []
    end = datetime.now(SEOUL_TZ).date() - timedelta(days=RESCUE_LAG_DAYS)
    start = end - timedelta(days=RESCUE_WINDOW_DAYS - 1)
    a, b = f'{start:%Y%m%d}', f'{end:%Y%m%d}'
    rows = _rescue_rows(key, a, b)
    if rows is None:
        print(f'Rescue card withheld: the register could not be read for {a}-{b}.')
        return []
    if not rows:
        print(f'Rescue card withheld: zero rows for {a}-{b}, which reads as an outage.')
        return []
    by_kind = {}
    for r in rows:
        k = (r.get('upKindNm') or '').strip()
        by_kind[k] = by_kind.get(k, 0) + 1
    known = {k for k, _, _, _ in RESCUE_SPECIES}
    stray = {k: n for k, n in by_kind.items() if k not in known}
    if stray:
        # A species the register never used before is folded into Other
        # rather than dropped, and said so: the total must still be the sum
        # of the lines under it.
        print(f'Rescue card: unexpected upKindNm {stray} counted as Other.')
        by_kind['기타'] = by_kind.get('기타', 0) + sum(stray.values())
    span_en, span_ko = _span_en(a, b), _span_ko(a, b)
    RANKED_CARD_INFO['rescue'] = {
        'day_en': span_en, 'day_ko': span_ko,
        'opener_en': RESCUE_OPENER_EN, 'opener_ko': RESCUE_OPENER_KO,
        'dateline_en': span_en, 'dateline_ko': span_ko,
        'note_en': RESCUE_NOTE_EN, 'note_ko': RESCUE_NOTE_KO,
        'line_emoji': {'All animals': '🐾', **{en: e for _, en, _, e in RESCUE_SPECIES}},
        'emoji': '🐾'}
    facts = [fact('rescue_total', 'rescue', 'All animals', grouped(len(rows)),
                  grouped(len(rows)), pin=True, label_ko='전체')]
    for ko_key, en, ko, _ in RESCUE_SPECIES:
        n = by_kind.get(ko_key, 0)
        facts.append(fact(f'rescue_{en.lower()}', 'rescue', en, grouped(n), grouped(n),
                          pin=True, label_ko=ko))
    return facts


# --- Performances (KOPIS, 공연예술통합전산망) -----------------------------------
# The national box-office register for the performing arts (theater,
# musicals, classical, dance, circus…), run by 예술경영지원센터. Its own key,
# issued on application through a public form (no account) on 11 September
# 2026; not a data.go.kr one. One call: prfstsArea, one row per region for a
# span of up to 31 days. Counting, not modelling: every line is the
# register's own figure for the 서울 row.
# Field names from the API page's own 출력결과 필드 table: prfcnt 공연건수,
# prfprocnt 개막편수, prfdtcnt 상연횟수, nmrs 판매수, nmrcancl 취소수,
# totnmrs 총 티켓판매수 (= nmrs - nmrcancl, checked on 1-7 September 2026:
# 417,075 - 171,251 = 245,824), amount 총 티켓판매액.
# ⚠️ The gateway answers a bare curl with 0 bytes and www. with a bodiless
# 301: send a browser User-Agent and follow redirects.
# ⚠️ The day rows are additive on showings and tickets (the seven days of
# 1-7 September sum to the week's 1,724 showings and 417,075 sales) but
# NOT on performances: a run playing all week is one performance in the
# week's row and seven across the days. Always ask for the week in one call.
# ⚠️ How far a day's figures settle after it is NOT measured: on the first
# probe (11 September) the previous day's row was present and full-sized.
# The window ends yesterday until a later comparison says otherwise.
KOPIS_BASE = ('https://kopis.or.kr/openApi/restful/prfstsArea'
              '?service={key}&stdate={a}&eddate={b}')
KOPIS_WINDOW_DAYS = 7
KOPIS_COOLDOWN_DAYS = 7
KOPIS_OPENER_EN = 'On stage in Seoul'
KOPIS_OPENER_KO = '서울의 공연'
KOPIS_NOTE_EN = ('Theater, musicals, classical, dance and more, from the national '
                 'box-office register; tickets net of cancellations')
KOPIS_NOTE_KO = '연극·뮤지컬·클래식·무용 등, 공연예술통합전산망 집계, 티켓은 취소분 제외'


def _kopis_seoul(key, a, b):
    """The 서울 row of prfstsArea for a..b (YYYYMMDD), as {tag: int}, or
    None when the call failed, parsed to nothing, or carried no 서울 row."""
    url = KOPIS_BASE.format(key=key, a=a, b=b)
    r = subprocess.run(['curl', '-s', '-L', '--max-time', '30', '-A', MOLIT_UA, url],
                       capture_output=True, text=True)
    try:
        root = ET.fromstring(r.stdout)
    except ET.ParseError:
        return None
    for row in root.findall('prfst'):
        if (row.findtext('area') or '').strip() == '서울':
            out = {}
            for c in row:
                if c.tag == 'area':
                    continue
                try:
                    out[c.tag] = int(c.text)
                except (TypeError, ValueError):
                    return None
            return out
    return None


def kopis_facts(key):
    """The kopis card: one week on Seoul's stages, the register's own five
    figures. Fills RANKED_CARD_INFO when built; prints why when withheld."""
    RANKED_CARD_INFO.pop('kopis', None)
    if not key:
        return []
    end = datetime.now(SEOUL_TZ).date() - timedelta(days=1)
    start = end - timedelta(days=KOPIS_WINDOW_DAYS - 1)
    a, b = f'{start:%Y%m%d}', f'{end:%Y%m%d}'
    row = _kopis_seoul(key, a, b)
    if row is None:
        print(f'Performances card withheld: no Seoul row could be read for {a}-{b}.')
        return []
    need = ('prfcnt', 'prfprocnt', 'prfdtcnt', 'totnmrs', 'amount')
    if any(k not in row for k in need) or not row['prfdtcnt']:
        print(f'Performances card withheld: a field is missing or the week shows '
              f'no showings for {a}-{b}: {row}.')
        return []
    span_en, span_ko = _span_en(a, b), _span_ko(a, b)
    RANKED_CARD_INFO['kopis'] = {
        'day_en': span_en, 'day_ko': span_ko,
        'opener_en': KOPIS_OPENER_EN, 'opener_ko': KOPIS_OPENER_KO,
        'dateline_en': span_en, 'dateline_ko': span_ko,
        'note_en': KOPIS_NOTE_EN, 'note_ko': KOPIS_NOTE_KO,
        'line_emoji': {'Productions': '🎭', 'Opened this week': '🎬', 'Performances': '🎟',
                       'Tickets sold': '🎫', 'Box office': '💰'},
        'emoji': '🎭'}
    return [fact('kopis_productions', 'kopis', 'Productions', grouped(row['prfcnt']),
                 grouped(row['prfcnt']), pin=True, label_ko='공연 건수'),
            fact('kopis_opened', 'kopis', 'Opened this week', grouped(row['prfprocnt']),
                 grouped(row['prfprocnt']), pin=True, label_ko='개막 편수'),
            fact('kopis_showings', 'kopis', 'Performances', grouped(row['prfdtcnt']),
                 grouped(row['prfdtcnt']), pin=True, label_ko='상연 횟수'),
            fact('kopis_tickets', 'kopis', 'Tickets sold', grouped(row['totnmrs']),
                 grouped(row['totnmrs']), pin=True, label_ko='티켓 판매'),
            fact('kopis_box', 'kopis', 'Box office', won_en(row['amount']),
                 won_ko(row['amount']), pin=True, label_ko='티켓 판매액',
                 num=row['amount'], unit='won')]


# --- Electricity (KEPCO, 전력데이터 개방 포털) --------------------------------
# Korea Electric Power Corporation's monthly sales by district and contract
# type, from its own data portal (bigdata.kepco.co.kr): its own key, issued
# on the spot on 11 September 2026 after an EN:TER 개인 회원가입 with phone
# 본인인증, so not a data.go.kr one. One call per month: 25 districts × 7
# contract types (주택용 households, 일반용 shops and offices, 산업용, 교육용,
# 심야 off-peak, 가로등 street lighting, 농사용), each with customers,
# kWh, the bill in won, the average price and contracted capacity.
# Counting, not modelling: every line is a sum of the portal's own rows.
# ⚠️ About two months behind (June was newest on 11 September 2026), and an
# unpublished month answers HTTP 404 with {errCd, errMsg}, so the newest
# month is found by walking back from last month. History runs to 2002
# (173-175 rows for June of every year sampled) and stops before 2000,
# which is what makes the twenty-year card possible.
# ⚠️ Per-household kWh is NOT on either card: dividing the 주택용 kWh by its
# customers is our arithmetic, not a published figure (the editorial test).
# KEPCO's separate 가구평균 API publishes that number itself.
KEPCO_BASE = ('https://bigdata.kepco.co.kr/openapi/v1/powerUsage/contractType.do'
              '?year={y}&month={m:02d}&metroCd=11&apiKey={key}&returnType=json')
KEPCO_LOOKBACK_MONTHS = 5     # how far back from last month to look for the newest
KEPCO_YEARS_BACK = 20
KEPCO_COOLDOWN_DAYS = 28      # a new month arrives monthly, like the infra counts
KEPCO_MIN_ROWS = 150          # 25 districts × 7 types is 173-175; a short answer is a partial month
KEPCO_OPENER_EN = 'Electricity in Seoul'
KEPCO_OPENER_KO = '서울의 전기'
KEPCO_HIST_OPENER_EN = 'Seoul’s electricity, 20 years apart'
KEPCO_HIST_OPENER_KO = '서울의 전기, 20년 전과 지금'
KEPCO_NOTE_EN = ('Korea Electric Power Corporation’s billing for the month, all contract '
                 'types; households are its residential tariff, shops and offices its '
                 'general one')
KEPCO_NOTE_KO = '한국전력 월별 판매 실적, 전체 계약종별 합계; 가정은 주택용, 상가·사무실은 일반용'
KEPCO_HIST_NOTE_EN = 'Korea Electric Power Corporation’s billing for the month, all contract types'
KEPCO_HIST_NOTE_KO = '한국전력 월별 판매 실적, 전체 계약종별 합계'
_KEPCO_CACHE = {}
# KEPCO's household-average endpoint (가구평균, houseAve.do): per district, the
# month's households on the residential tariff, the AVERAGE kWh per household
# and the AVERAGE bill, all published by KEPCO itself. That is the per-household
# figure the kepco card deliberately does not compute (see kepco_facts): here
# it is the publisher's own number. Added 12 September 2026, his call. ⚠️
# The month MUST be zero-padded ('06', not '6'): a bare single digit answers
# 404 NotFound exactly like an unpublished month, which on 12 September
# 2026 read for an hour as "only October to December are published". Padded,
# every month answers and the newest is about two months behind, like the
# contract card, so the same five-month lookback serves.
KEPCO_HOUSE_BASE = ('https://bigdata.kepco.co.kr/openapi/v1/powerUsage/houseAve.do'
                    '?year={y}&month={m:02d}&metroCd=11&apiKey={key}&returnType=json')
KEPCO_HOUSE_LOOKBACK_MONTHS = KEPCO_LOOKBACK_MONTHS
KEPCO_HOUSE_MIN_ROWS = 25      # one row per district; fewer is a partial answer
KEPCO_HOUSE_COOLDOWN_DAYS = 28
KEPCO_HOUSE_OPENER_EN = 'Household electricity in Seoul'
KEPCO_HOUSE_OPENER_KO = '서울 가정의 전기'
KEPCO_HOUSE_NOTE_EN = ('Korea Electric Power Corporation’s average per household on its '
                       'residential tariff, by district')
KEPCO_HOUSE_NOTE_KO = '한국전력 주택용 가구당 평균, 자치구별'
_KEPCO_HOUSE_CACHE = {}


def gwh(kwh):
    """kWh -> a GWh string, both languages: 4,155,959,477 -> '4,156 GWh';
    18,801,140 -> '18.8 GWh'. Whole numbers from 100 GWh up."""
    g = kwh / 1e6
    return f'{g:,.0f} GWh' if g >= 100 else f'{g:,.1f} GWh'


def _kepco_rows(key, y, m):
    """The month's Seoul rows, or None: a failed call, an unpublished month
    (404 with errCd), or fewer rows than a whole month has."""
    if (y, m) in _KEPCO_CACHE:
        return _KEPCO_CACHE[(y, m)]
    url = KEPCO_BASE.format(key=key, y=y, m=m)
    r = subprocess.run(['curl', '-s', '-L', '--max-time', '30', '-A', MOLIT_UA, url],
                       capture_output=True, text=True)
    try:
        d = json.loads(r.stdout)
        rows = d['data']
    except (ValueError, KeyError, TypeError):
        _KEPCO_CACHE[(y, m)] = None
        return None
    if not isinstance(rows, list) or len(rows) < KEPCO_MIN_ROWS:
        _KEPCO_CACHE[(y, m)] = None
        return None
    _KEPCO_CACHE[(y, m)] = rows
    return rows


def _kepco_totals(rows):
    """Sum the month: {'cust', 'kwh', 'bill', 'house_kwh', 'general_kwh'},
    or None when a row's numbers do not parse."""
    t = {'cust': 0, 'kwh': 0, 'bill': 0, 'house_kwh': 0, 'general_kwh': 0}
    for r in rows:
        try:
            c, u, b = int(r['custCnt']), int(r['powerUsage']), int(r['bill'])
        except (KeyError, TypeError, ValueError):
            return None
        t['cust'] += c
        t['kwh'] += u
        t['bill'] += b
        k = (r.get('cntr') or '').replace(' ', '')
        if k == '주택용':
            t['house_kwh'] += u
        elif k == '일반용':
            t['general_kwh'] += u
    return t if t['kwh'] else None


def _kepco_newest(key):
    """(year, month, rows) for the newest published month, walking back from
    last month, or None."""
    first = datetime.now(SEOUL_TZ).date().replace(day=1)
    for _ in range(KEPCO_LOOKBACK_MONTHS):
        first = (first - timedelta(days=1)).replace(day=1)
        rows = _kepco_rows(key, first.year, first.month)
        if rows:
            return first.year, first.month, rows
    return None


def kepco_facts(key):
    """The kepco card: the newest month's electricity in Seoul, five lines.
    Fills RANKED_CARD_INFO when built; prints why when withheld."""
    RANKED_CARD_INFO.pop('kepco', None)
    if not key:
        return []
    got = _kepco_newest(key)
    if not got:
        print('Electricity card withheld: no month could be read from KEPCO.')
        return []
    y, m, rows = got
    t = _kepco_totals(rows)
    if not t:
        print(f'Electricity card withheld: the rows for {y}-{m:02d} did not sum.')
        return []
    per_en, per_ko = f'{MONTHS_EN[m - 1]} {y}', f'{y}년 {m}월'
    RANKED_CARD_INFO['kepco'] = {
        'day_en': per_en, 'day_ko': per_ko,
        'opener_en': KEPCO_OPENER_EN, 'opener_ko': KEPCO_OPENER_KO,
        'dateline_en': per_en, 'dateline_ko': per_ko,
        'note_en': KEPCO_NOTE_EN, 'note_ko': KEPCO_NOTE_KO,
        'line_emoji': {'Customers': '🔌', 'Electricity used': '⚡️', 'Households': '🏠',
                       'Shops and offices': '🏢', 'Billed': '💰'},
        'emoji': '⚡️'}
    return [fact('kepco_cust', 'kepco', 'Customers', grouped(t['cust']), grouped(t['cust']),
                 pin=True, label_ko='고객 호수'),
            fact('kepco_kwh', 'kepco', 'Electricity used', gwh(t['kwh']), gwh(t['kwh']),
                 pin=True, label_ko='전력 사용량'),
            fact('kepco_house', 'kepco', 'Households', gwh(t['house_kwh']), gwh(t['house_kwh']),
                 pin=True, label_ko='가정'),
            fact('kepco_general', 'kepco', 'Shops and offices', gwh(t['general_kwh']),
                 gwh(t['general_kwh']), pin=True, label_ko='상가·사무실'),
            fact('kepco_bill', 'kepco', 'Billed', won_en(t['bill']), won_ko(t['bill']),
                 pin=True, label_ko='전기 요금', num=t['bill'], unit='won')]


def kepco_hist_facts(key):
    """The kepcohist card: the newest month against the same month twenty
    years earlier, three metrics as period pairs (compose() groups them by
    metric and bolds the periods, the fifty-year weather card's shape)."""
    RANKED_CARD_INFO.pop('kepcohist', None)
    if not key:
        return []
    got = _kepco_newest(key)
    if not got:
        return []
    y, m, rows = got
    then_rows = _kepco_rows(key, y - KEPCO_YEARS_BACK, m)
    if not then_rows:
        print(f'Electricity then-and-now withheld: no rows for {y - KEPCO_YEARS_BACK}-{m:02d}.')
        return []
    now, then = _kepco_totals(rows), _kepco_totals(then_rows)
    if not now or not then:
        return []
    RANKED_CARD_INFO['kepcohist'] = {
        'opener_en': KEPCO_HIST_OPENER_EN, 'opener_ko': KEPCO_HIST_OPENER_KO,
        'note_en': KEPCO_HIST_NOTE_EN, 'note_ko': KEPCO_HIST_NOTE_KO,
        'emoji': '⚡️'}
    mon_en = MONTHS_EN[m - 1]
    facts = []
    for head_en, head_ko, fid, fmt_en, fmt_ko, unit in (
            ('Electricity used', '전력 사용량', 'kwh', gwh, gwh, None),
            ('Billed', '전기 요금', 'bill', won_en, won_ko, 'won'),
            ('Customers', '고객 호수', 'cust', grouped, grouped, None)):
        for side, t, yy in (('now', now, y), ('then', then, y - KEPCO_YEARS_BACK)):
            per_en, per_ko = f'{mon_en} {yy}', f'{yy}년 {m}월'
            v = t['kwh' if fid == 'kwh' else fid]
            facts.append(fact(f'kepcohist_{fid}_{side}', 'kepcohist',
                              f'{head_en}, {per_en}', fmt_en(v), fmt_ko(v),
                              pair=f'kepcohist_{fid}_then', pin=True,
                              label_ko=f'{head_ko}, {per_ko}',
                              head_en=head_en, head_ko=head_ko,
                              period_en=per_en, period_ko=per_ko,
                              num=v if unit else None, unit=unit))
    return facts


def _kepco_house_rows(key, y, m):
    """The month's 25 district rows from houseAve.do, or None: a failed call,
    an unpublished month (404 with errCd) or fewer rows than districts."""
    if (y, m) in _KEPCO_HOUSE_CACHE:
        return _KEPCO_HOUSE_CACHE[(y, m)]
    url = KEPCO_HOUSE_BASE.format(key=key, y=y, m=m)
    r = subprocess.run(['curl', '-s', '-L', '--max-time', '30', '-A', MOLIT_UA, url],
                       capture_output=True, text=True)
    try:
        rows = json.loads(r.stdout)['data']
    except (ValueError, KeyError, TypeError):
        rows = None
    if not isinstance(rows, list) or len(rows) < KEPCO_HOUSE_MIN_ROWS:
        rows = None
    _KEPCO_HOUSE_CACHE[(y, m)] = rows
    return rows


def _kepco_house_newest(key):
    """(year, month, rows) for the newest published household month,
    walking back from last month over KEPCO_HOUSE_LOOKBACK_MONTHS, or None."""
    first = datetime.now(SEOUL_TZ).date().replace(day=1)
    for _ in range(KEPCO_HOUSE_LOOKBACK_MONTHS):
        first = (first - timedelta(days=1)).replace(day=1)
        rows = _kepco_house_rows(key, first.year, first.month)
        if rows:
            return first.year, first.month, rows
    return None


def kwh_avg(v):
    """An average kWh figure: 209.26 -> '209 kWh'."""
    return f'{v:,.0f} kWh'


def kepco_house_facts(key):
    """The kepcohouse card: the newest published month's household
    averages by district, four lines in a fixed order, the two ends of use
    and the two ends of the bill. Every figure is a row KEPCO published;
    nothing is summed or divided here. Fills RANKED_CARD_INFO when built;
    prints why when withheld."""
    RANKED_CARD_INFO.pop('kepcohouse', None)
    if not key:
        return []
    got = _kepco_house_newest(key)
    if not got:
        print('Household electricity card withheld: no month could be read from KEPCO.')
        return []
    y, m, rows = got
    parsed = []
    for r in rows:
        try:
            parsed.append((str(r['city']).strip(), float(r['powerUsage']), int(r['bill'])))
        except (KeyError, TypeError, ValueError):
            print(f'Household electricity card withheld: a row for {y}-{m:02d} did not parse.')
            return []
    en = {gu: en_name(gu, 'districts') for gu, _, _ in parsed}
    missing = [gu for gu, e in en.items() if not e]
    if missing:
        print(f'Household electricity card withheld: no English name for '
              f'{", ".join(repr(g) for g in missing)}.')
        return []
    by_use = sorted(parsed, key=lambda t: (-t[1], t[0]))
    by_bill = sorted(parsed, key=lambda t: (-t[2], t[0]))
    per_en, per_ko = f'{MONTHS_EN[m - 1]} {y}', f'{y}년 {m}월'
    RANKED_CARD_INFO['kepcohouse'] = {
        'day_en': per_en, 'day_ko': per_ko,
        'opener_en': KEPCO_HOUSE_OPENER_EN, 'opener_ko': KEPCO_HOUSE_OPENER_KO,
        'dateline_en': f'Average per household, {per_en}',
        'dateline_ko': f'{per_ko} 가구당 평균',
        'note_en': KEPCO_HOUSE_NOTE_EN, 'note_ko': KEPCO_HOUSE_NOTE_KO,
        'emoji': '⚡️'}
    hi_u, lo_u, hi_b, lo_b = by_use[0], by_use[-1], by_bill[0], by_bill[-1]
    return [fact('kepcohouse_use_hi', 'kepcohouse', f'Most used: {en[hi_u[0]]}',
                 kwh_avg(hi_u[1]), kwh_avg(hi_u[1]), pin=True,
                 label_ko=f'가장 많이 씀: {hi_u[0]}', place_en=en[hi_u[0]], place_ko=hi_u[0]),
            fact('kepcohouse_use_lo', 'kepcohouse', f'Least used: {en[lo_u[0]]}',
                 kwh_avg(lo_u[1]), kwh_avg(lo_u[1]), pin=True,
                 label_ko=f'가장 적게 씀: {lo_u[0]}', place_en=en[lo_u[0]], place_ko=lo_u[0]),
            fact('kepcohouse_bill_hi', 'kepcohouse', f'Highest bill: {en[hi_b[0]]}',
                 won_en(hi_b[2]), won_ko(hi_b[2]), pin=True,
                 label_ko=f'가장 비싼 요금: {hi_b[0]}', place_en=en[hi_b[0]], place_ko=hi_b[0],
                 num=hi_b[2], unit='won'),
            fact('kepcohouse_bill_lo', 'kepcohouse', f'Lowest bill: {en[lo_b[0]]}',
                 won_en(lo_b[2]), won_ko(lo_b[2]), pin=True,
                 label_ko=f'가장 싼 요금: {lo_b[0]}', place_en=en[lo_b[0]], place_ko=lo_b[0],
                 num=lo_b[2], unit='won')]


# --- Korea by rail (KORAIL) --------------------------------------------------
# 한국철도공사's ticketing/movement-type statistics via data.go.kr (자동승인,
# approved 2 Sep 2026): ten operations, covering intercity trains (간선열차:
# KTX/새마을/무궁화) and commuter rail (광역철도) separately. This vein reads
# two of the ten — mainLineRoutePer (intercity, by route) and
# wideRailloadRoutePer (commuter, by line) — and reports the busiest of each,
# one month.
#
# ⚠️ Both operations return roughly a YEAR of history when no month is
# specified — there is no query parameter to select one — so the newest
# run_ym present has to be found and filtered to client-side, or "one
# month's ridership" is actually eleven or twelve months summed together.
# Measured 4 September 2026 building the sample cards for this vein: an
# unfiltered sum overstated a single month by roughly 12x.

KORAIL_BASE = 'http://apis.data.go.kr/B551457/issueStatistics/{op}'

# Official English names for the route/line fields most likely to top the
# tables. An unmapped name falls back to Korean with a warning, same as
# CULTURE_EN and IIAC_COUNTRY_EN above. Intercity and commuter lines can
# share a Korean name (e.g. 경부선 names both a KTX trunk line and a separate
# commuter corridor) — that's fine, each is a distinct fact with its own
# "intercity"/"commuter" framing in label_en, not a lookup collision.
KORAIL_LINE_EN = {
    '경부선': 'the Gyeongbu Line', '호남선': 'the Honam Line',
    '전라선': 'the Jeolla Line', '동해선': 'the Donghae Line',
    '경전선': 'the Gyeongjeon Line', '강릉선': 'the Gangneung Line',
    '장항선': 'the Janghang Line', '중앙선': 'the Jungang Line',
    '충북선': 'the Chungbuk Line', '분당선': 'the Bundang Line',
    '경인선': 'the Gyeongin Line', '경원선': 'the Gyeongwon Line',
    '일산선': 'the Ilsan Line', '경의선': 'the Gyeongui Line',
    '안산선': 'the Ansan Line', '과천선': 'the Gwacheon Line',
    '수인선': 'the Suin Line', '경춘선': 'the Gyeongchun Line',
    '서해선': 'the Seohae Line', '경강선': 'the Gyeonggang Line',
    '대경선': 'the Daegyeong Line',
    # The rest of mainLineRoutePer's July 2026 roster, added 12 September
    # 2026 for the commuter-rail card's sibling work; kept, harmless.
    '태백선': 'the Taebaek Line', '중부내륙선': 'the Jungbu Naeryuk Line',
    '영동선': 'the Yeongdong Line', '경북선': 'the Gyeongbuk Line',
    '대구선': 'the Daegu Line', '공항철도': 'the Airport Railroad',
}


def _korail_fetch(key, op, numofrows, page=1):
    """One page of one Korail operation, as its item list, or []."""
    url = (KORAIL_BASE.format(op=op) +
           f'?serviceKey={key}&pageNo={page}&numOfRows={numofrows}&type=json')
    r = subprocess.run(['curl', '-s', '--max-time', '45', '-A', MOLIT_UA, url],
                       capture_output=True, text=True)
    try:
        items = json.loads(r.stdout)['response']['body']['items']['item']
    except (ValueError, KeyError, TypeError):
        return []
    return items if isinstance(items, list) else []


def _korail_newest(items, field):
    """The rows carrying the newest value of `field` (run_ym or opr_ymd),
    as (rows, newest), or ([], None)."""
    newest = max((x.get(field) for x in items if x.get(field)), default=None)
    if not newest:
        return [], None
    return [x for x in items if x.get(field) == newest], newest


def _korail_newest_rows(key, op, numofrows):
    """One operation's rows for its OWN newest run_ym, as (rows, 'YYYYMM'),
    or ([], None). numofrows must cover the operation's full history, since
    the API's own row order is not guaranteed newest-first."""
    return _korail_newest(_korail_fetch(key, op, numofrows), 'run_ym')


# --- Korail per-station history -------------------------------------------------
# His call, 11 September 2026 ("Can we start saving data so we have more
# than a year's worth?"): the feed prunes at about a year, so anything older
# exists nowhere unless kept here. korail_station_history.json holds every
# station's boardings and alightings for every day the bot has seen,
# {'days': {'YYYYMMDD': {'서울': [ride, goff], ...}}}, about 2.5 MB a year.
# Gitignored like crowd_history.jsonl (public data, but not source), and
# swept to the NAS and C2 nightly with the rest of ~/Projects. Once per run
# the bot fetches KORAIL_FETCH_PAGES pages (about 77 days) and folds them
# in, the feed's value winning for any day it still serves, so a month of
# missed runs heals itself; korail_station_history_backfill.py pages the
# whole feed for the initial fill. ⚠️ Both Korail station cards read the
# NEWEST day from what was FETCHED this run, never from the file: a failed
# fetch withholds them rather than posting the file's last day as new.
KORAIL_HISTORY = Path(__file__).with_name('korail_station_history.json')
KORAIL_FETCH_PAGES = 4
KORAIL_PAGE_ROWS = 5000
_KORAIL_RUN = {}     # one fetch per process: {'history': days, 'fetched': days}


def load_korail_history():
    try:
        h = json.loads(KORAIL_HISTORY.read_text())
    except (OSError, ValueError):
        h = {}
    h.setdefault('days', {})
    return h


def save_korail_history(h):
    write_json_atomic(KORAIL_HISTORY, h, ensure_ascii=False)


def korail_days_from_rows(items):
    """{day: {station: [ride, goff]}} from mainLineStationPer rows."""
    out = {}
    for x in items:
        day, name = x.get('opr_ymd') or '', (x.get('stn_nm') or '').strip()
        if len(day) != 8 or not name:
            continue
        try:
            out.setdefault(day, {})[name] = [int(x.get('ride_nope') or 0), int(x.get('goff_nope') or 0)]
        except (TypeError, ValueError):
            continue
    return out


def korail_history_add(h, days):
    """Fold fetched days into the history; the feed wins for a day it still
    serves (a revised figure replaces the stored one). True if anything changed."""
    changed = False
    for day, stations in days.items():
        if h['days'].get(day) != stations:
            h['days'][day] = stations
            changed = True
    return changed


def korail_station_days(key):
    """(history days, fetched days), fetching once per process. fetched is
    {} when nothing came back, and both station cards withhold on that."""
    if 'history' in _KORAIL_RUN:
        return _KORAIL_RUN['history'], _KORAIL_RUN['fetched']
    items = []
    for page in range(1, KORAIL_FETCH_PAGES + 1):
        got = _korail_fetch(key, 'mainLineStationPer', KORAIL_PAGE_ROWS, page)
        if not got:
            break
        items += got
    fetched = korail_days_from_rows(items)
    h = load_korail_history()
    if fetched and korail_history_add(h, fetched):
        save_korail_history(h)
    _KORAIL_RUN['history'], _KORAIL_RUN['fetched'] = h['days'], fetched
    return h['days'], fetched


# --- the rail stations card ---------------------------------------------------
# His call, 11 September 2026 ("Now the Korail per-station card"), the
# second of the two Korail operations this bot reads that carry Seoul by
# name. mainLineStationPer is DAILY, not monthly: one row per station per
# day, boardings (ride_nope) and alightings (goff_nope), station names in
# the row, about 258 stations a day, newest day first, roughly a year of
# history and no date parameter (93,814 rows on 11 September 2026, back to
# 10 September 2025). The newest day was three days back. The rows come
# through korail_station_days() above, shared with the Seoul Station card.
#
# ⚠️ WHICH STATIONS ARE IN SEOUL WAS MEASURED, NOT ASSUMED. The 258 names on
# 8 September 2026 were matched to subwayStationMaster and passed through
# the stations card's own inside-Seoul test (within 300 m of a Seoul bus
# stop): eight matched, and they are the roster below, with that day's
# boardings: 서울 49,068, 용산 17,925, 청량리 7,549, 영등포 7,310, 상봉 381,
# 왕십리 268, 옥수 32, 양원 1. The card shows the four busiest of them. 수서
# is absent from the feed altogether: it is SRT's terminal, a different
# operator, which the footnote says. Re-measure with the same join if a
# Korail station opens in Seoul; a name not on the roster cannot appear.
#
# English is the station table's, tried with 역 first so 서울 reads "Seoul
# Station" and not "Seoul", the city: a bare "Seoul: 49,068" on a card
# about stations would say the wrong thing.
KORAIL_SEOUL_STATIONS = ('서울', '용산', '청량리', '영등포', '상봉', '왕십리', '옥수', '양원')
RAILSTATIONS_OPENER_EN = 'Seoul’s railway stations'
RAILSTATIONS_OPENER_KO = '서울의 기차역'
# The footnote is his wording, 11 September 2026, and its count is the
# ROSTER's, not the day's rows: "if there are 8 stations there are 8
# stations, regardless of whether there's service" (his call, same day).
RAILSTATIONS_NOTE_EN = (f'The four busiest of Seoul’s {len(KORAIL_SEOUL_STATIONS)} Korail stations. '
                        f'Korail trains only, KTX to Mugunghwa; SRT is a separate operator.')
RAILSTATIONS_NOTE_KO = (f'서울의 코레일 역 {len(KORAIL_SEOUL_STATIONS)}곳 중 승차가 많은 네 곳. '
                        f'코레일 열차 기준(KTX~무궁화호), SRT는 별도 운영사.')


# --- the Seoul Station card ---------------------------------------------------
# His call, 11 September 2026, a separate vein from the rail stations card
# above: one station, its passengers that day, and something to read the
# figure against. Measured over the 364 days the feed held that morning:
# 서울 led every one of them, in Seoul and nationally, so the vein never has
# to ask which station. Its median day is about 113,000 boardings and
# alightings together, but Monday to Wednesday run near 100,000 and Friday
# to Sunday near 135,000, so the comparison is the SAME WEEKDAY, as the
# movers card does: the median of the previous SEOULSTATION_MAX_PRIOR such
# days, at least SEOULSTATION_MIN_PRIOR. Boardings and alightings are two
# lines rather than one, because they part only around the holidays and
# then carry the whole story: 14 February 2026, the Saturday before
# Seollal, 76,119 boarded and 44,605 got off; on the 18th it reversed,
# 43,102 against 78,103. On an ordinary day the two are within a few
# hundred. Rejected as the comparison: a year ago (the feed holds exactly a
# year, so that day sits on the edge and drops out), the nation's share
# (a computed share, which the pinned card says this account does not
# publish), and the year's busiest day (it would repeat until beaten).
#
# The baseline reads korail_station_history.json (see KORAIL_HISTORY), so
# it outlives the feed's year; the day itself is always this run's fetch. A
# holiday is not excluded from the day or the baseline: the median absorbs
# one or two among eight, and a holiday IS the day worth comparing.
SEOULSTATION_NAME = '서울'
SEOULSTATION_MIN_PRIOR = 3
SEOULSTATION_MAX_PRIOR = 8
SEOULSTATION_COOLDOWN_DAYS = 7


def seoul_station_facts(key):
    """The seoulstation card: boarded, got off, passengers, and a typical
    same weekday, for the newest fetched day, the baseline from the history
    file. Fills RANKED_CARD_INFO when built; prints why when withheld."""
    RANKED_CARD_INFO.pop('seoulstation', None)
    if not key:
        return []
    history, fetched = korail_station_days(key)
    if not fetched:
        return []
    day = max(fetched)
    if SEOULSTATION_NAME not in fetched[day]:
        print(f'Seoul Station card withheld: no {SEOULSTATION_NAME} row for {day}.')
        return []
    ride, goff = fetched[day][SEOULSTATION_NAME]
    series = {k: v[SEOULSTATION_NAME] for k, v in history.items() if SEOULSTATION_NAME in v}
    dt = _day_dt(day)
    d, d_ko = en_date(dt), f'{dt.month}월 {dt.day}일'
    wd = dt.weekday()
    prior = sorted(k for k in series if k < day and _day_dt(k).weekday() == wd)[-SEOULSTATION_MAX_PRIOR:]
    if len(prior) < SEOULSTATION_MIN_PRIOR:
        print(f'Seoul Station card withheld for {d}: only {len(prior)} prior '
              f'{WEEKDAY_EN[wd]}s in the feed, need {SEOULSTATION_MIN_PRIOR}.')
        return []
    typical = round(statistics.median(series[k][0] + series[k][1] for k in prior))
    wd_en, wd_ko = WEEKDAY_EN[wd], WEEKDAY_KO[wd]
    n = len(prior)
    # A public holiday is named on the dateline, his wording, 11 September
    # 2026. Only the official day: the Saturday before Seollal carried the
    # year's biggest outbound swing and was not one, so it reads bare and
    # the boarded/got-off split speaks for itself. A lookup that cannot be
    # made leaves the plain date; it is a note, not a figure.
    dateline_en, dateline_ko = d, d_ko
    hist = load_bus_history()
    before = json.dumps(hist.get('holiday_names', {}), sort_keys=True)
    names = kr_holiday_names(hist, dt.year)
    if json.dumps(hist.get('holiday_names', {}), sort_keys=True) != before:
        save_bus_history(hist)
    if names is None:
        print(f'Seoul Station card: holiday table unavailable, {d} shown as a plain date.')
    elif day in names and all(names[day]):
        dateline_en = f'{d} was a holiday, {names[day][1]}'
        dateline_ko = f'{d_ko} {names[day][0]}'
    RANKED_CARD_INFO['seoulstation'] = {
        'day_en': d, 'day_ko': d_ko,
        'opener_en': 'Seoul Station', 'opener_ko': '서울역',
        'dateline_en': dateline_en, 'dateline_ko': dateline_ko,
        'note_en': (f'Typical: the median of the previous {n} {wd_en}s. '
                    f'Korail trains only; SRT is a separate operator.'),
        'note_ko': f'평소: 이전 {wd_ko} {n}회의 중앙값. 코레일 열차 기준, SRT는 별도 운영사.'}
    return [
        fact('railss_boarded', 'seoulstation', 'Boarded', grouped(ride), grouped(ride), pin=True,
             label_ko='승차', num=ride, unit='people'),
        fact('railss_alighted', 'seoulstation', 'Got off', grouped(goff), grouped(goff), pin=True,
             label_ko='하차', num=goff, unit='people'),
        fact('railss_total', 'seoulstation', 'Passengers', grouped(ride + goff), grouped(ride + goff),
             pin=True, label_ko='이용객', num=ride + goff, unit='people'),
        fact('railss_typical', 'seoulstation', f'A typical {wd_en}', grouped(typical), grouped(typical),
             pin=True, label_ko=f'평소 {wd_ko}', num=typical, unit='people'),
    ]


def rail_station_en(ko_name):
    """The station table's English, 역 form first ('서울역' → Seoul Station),
    then the bare name ('용산' → Yongsan). None rather than a romanization."""
    return en_lookup(f'{ko_name}역', 'stations') or en_lookup(ko_name, 'stations')


def rail_stations_facts(key):
    """The railstations card: Seoul's four busiest Korail stations by
    boardings on the newest published day. Fills RANKED_CARD_INFO when
    built; prints why when withheld."""
    RANKED_CARD_INFO.pop('railstations', None)
    if not key:
        return []
    _, fetched = korail_station_days(key)
    if not fetched:
        return []
    day = max(fetched)
    seoul = [(name, v[0]) for name, v in fetched[day].items() if name in KORAIL_SEOUL_STATIONS]
    seoul.sort(key=lambda t: (-t[1], t[0]))
    top = seoul[:4]
    dt = _day_dt(day)
    d, d_ko = en_date(dt), f'{dt.month}월 {dt.day}일'
    if len(top) < 3:
        print(f'Rail stations card withheld for {d}: only {len(top)} Seoul stations in the feed.')
        return []
    en = {name: rail_station_en(name) for name, _ in top}
    missing = [n for n, e in en.items() if not e]
    if missing:
        print(f'Rail stations card withheld for {d}: no English name for '
              f'{", ".join(repr(m) for m in missing)}.')
        return []
    RANKED_CARD_INFO['railstations'] = {
        'day_en': d, 'day_ko': d_ko,
        'opener_en': RAILSTATIONS_OPENER_EN, 'opener_ko': RAILSTATIONS_OPENER_KO,
        'dateline_en': f'Intercity rail boardings on {d}', 'dateline_ko': f'{d_ko} 열차 승차',
        'note_en': RAILSTATIONS_NOTE_EN, 'note_ko': RAILSTATIONS_NOTE_KO}
    # Bare station names, sorted by the day's boardings; the name itself is
    # the emphasised run (place_en), as the rush card bolds its station.
    return [fact(f'railst_{i}', 'railstations', en[name], grouped(v), grouped(v), pin=True,
                 label_ko=f'{name}역', place_en=en[name], place_ko=f'{name}역',
                 num=v, unit='people')
            for i, (name, v) in enumerate(top)]


RAILCOMMUTER_COOLDOWN_DAYS = 28    # a new month arrives monthly
RAILCOMMUTER_OPENER_EN = 'Seoul’s commuter rail'
RAILCOMMUTER_OPENER_KO = '서울의 광역철도'
RAILCOMMUTER_NOTE_EN = ('Korail’s commuter lines only: its stretches of lines 1, 3 and 4, and the '
                        'Gyeongui-Jungang, Gyeongchun, Suin-Bundang, Gyeonggang and Seohae lines. '
                        'Stations inside Seoul.')
RAILCOMMUTER_NOTE_KO = ('코레일 광역철도만: 1·3·4호선 코레일 구간과 경의중앙·경춘·수인분당·경강·서해선. '
                        '서울 시내 역.')
RAILCOMMUTER_TOP = 3
RAILCOMMUTER_MIN_STATIONS = 20   # 62 inside Seoul in July 2026; far fewer is a broken join
# The membership join (Korail name → subwayStationMaster → within 300 m of a
# Seoul bus stop) costs a dozen coordinate calls, so it is cached per month
# here, gitignored: {run_ym: {'rides': {folded station: boardings},
# 'coords': {folded station: [lon, lat]}, 'stops': N}}. The coordinates feed
# the threaded pin map without a refetch; 'stops' is the backdrop's count
# for the map's caption. An entry without those keys is recomputed.
RAILCOMMUTER_CACHE = Path(__file__).with_name('railcommuter_cache.json')
# ⚠️ A bracket naming ANOTHER CITY excludes the row; folding it off would
# hand Busan's 교대 and 송정 to Seoul's stations of the same name, which the
# first join did (12 September 2026, 교대(부산) 151,849 credited to Seoul).
# A bracket naming a line ((경의선), (서해), (분당)) is folded as usual.
RAILCOMMUTER_OTHER_CITY = re.compile(r'\((부산|대구|인천|대전|광주|울산)\)\s*$')


def rail_commuter_month(gov_key, api_key):
    """({'rides': {folded station: boardings}, 'coords': {station: [lon, lat]},
    'stops': N} for stations inside Seoul, 'YYYYMM') for the newest month of
    wideRailloadStationPer, or (None, None). The per-station boardings are
    the API's own rows, summed only where two rows fold to one station (서울
    and 서울(경의선) are one Seoul Station, the subway card's own rule).
    Cached per month."""
    items = _korail_fetch(gov_key, 'wideRailloadStationPer', KORAIL_PAGE_ROWS)
    rows, ym = _korail_newest(items, 'run_ym')
    if not rows or not ym:
        return None, None
    try:
        cache = json.loads(RAILCOMMUTER_CACHE.read_text())
    except (OSError, ValueError):
        cache = {}
    got = cache.get(ym)
    if (isinstance(got, dict) and isinstance(got.get('rides'), dict)
            and len(got['rides']) >= RAILCOMMUTER_MIN_STATIONS and got.get('coords')):
        return got, ym
    try:
        coords = station_coords(api_key)
        stop_map = seoul_bus_stop_coord_map(api_key)
        inside = stations_in_seoul(coords, list(stop_map.values()))
    except RuntimeError as e:
        print(f'Commuter rail card withheld: coordinate feeds unavailable ({e}).')
        return None, None
    seoul = {}
    for r in rows:
        raw = r.get('stn_nm') or ''
        if RAILCOMMUTER_OTHER_CITY.search(raw):
            continue
        name = fold_station(raw)
        if name not in inside:
            continue
        try:
            v = int(r.get('ride_nope') or 0)
        except (TypeError, ValueError):
            print(f'Commuter rail card withheld: a row for {ym} did not parse.')
            return None, None
        seoul[name] = seoul.get(name, 0) + v
    if len(seoul) < RAILCOMMUTER_MIN_STATIONS:
        print(f'Commuter rail card withheld: only {len(seoul)} stations joined for {ym}.')
        return None, None
    month = {'rides': seoul,
             'coords': {n: [coords[n][0], coords[n][1]] for n in seoul if n in coords},
             'stops': len(stop_map)}
    cache[ym] = month
    write_json_atomic(RAILCOMMUTER_CACHE, cache, ensure_ascii=False)
    return month, ym


def rail_commuter_facts(gov_key, api_key):
    """The railcommuter card: the newest month's boardings at Korail's
    commuter-rail stations inside Seoul, the busiest three and the total
    for all of them. Replaced a Korea-wide lines card the same day it was
    built (12 September 2026): a whole-line figure to Busan is not Seoul's,
    and this table is the one Korail operation nothing here read."""
    RANKED_CARD_INFO.pop('railcommuter', None)
    if not gov_key or not api_key:
        return []
    month, ym = rail_commuter_month(gov_key, api_key)
    if not month:
        return []
    seoul, coords = month['rides'], month.get('coords') or {}
    ranked = sorted(seoul.items(), key=lambda kv: (-kv[1], kv[0]))
    top = ranked[:RAILCOMMUTER_TOP]
    # The Korail roster's own English first ('서울' → 'Seoul Station', as the
    # rail stations card prints it; bare 'Seoul' on a station line reads as
    # the city), then the subway table's official name.
    en = {name: rail_station_en(name) or en_lookup(name, 'stations') for name, _ in top}
    missing = [n for n, e in en.items() if not e]
    if missing:
        print(f'Commuter rail card withheld for {ym}: no English name for '
              f'{", ".join(repr(m) for m in missing)}.')
        return []
    if any(name not in coords for name, _ in top):
        print(f'Commuter rail card withheld for {ym}: no coordinates for '
              f'{", ".join(repr(n) for n, _ in top if n not in coords)}.')
        return []
    y, m = int(ym[:4]), int(ym[4:])
    per_en, per_ko = f'{MONTHS_EN[m - 1]} {y}', f'{y}년 {m}월'
    ranks = (('Busiest', '가장 붐빔'), ('2nd-busiest', '두 번째로 붐빔'),
             ('3rd-busiest', '세 번째로 붐빔'))
    dots = (f'The map is composed of gray dots that represent each of Seoul’s '
            f'{grouped(month.get("stops") or 0)} bus stops.')
    RANKED_CARD_INFO['railcommuter'] = {
        'day_en': per_en, 'day_ko': per_ko,
        'opener_en': RAILCOMMUTER_OPENER_EN, 'opener_ko': RAILCOMMUTER_OPENER_KO,
        'dateline_en': f'Boardings in {per_en}', 'dateline_ko': f'{per_ko} 승차',
        'note_en': RAILCOMMUTER_NOTE_EN, 'note_ko': RAILCOMMUTER_NOTE_KO,
        # The threaded pin map, the bus stops card's own shape (his call,
        # 12 September 2026): three stations named on the bus-stop silhouette.
        'map_title': f'Commuter rail boardings for {per_en}',
        'map_caption': dots,
        'map_pins': [(f'{ranks[i][0]}: {en[name]}', MAP_COLOURS[i], tuple(coords[name]))
                     for i, (name, _) in enumerate(top)],
        'map_alt': (f'Map of the three busiest Korail commuter-rail stations in Seoul, {per_en}: '
                    + ', '.join(f'{ranks[i][0].lower()} ({en[name]})' for i, (name, _) in enumerate(top))
                    + f', each marked and named. {dots}')}
    facts = [fact(f'railcom_{i}', 'railcommuter', f'{ranks[i][0]}: {en[name]}',
                  grouped(v), grouped(v), pin=True,
                  # '역' on every Korean name, as the rail stations card does: a bare
                  # '서울' on a Korean card is the city, not the station.
                  label_ko=f'{ranks[i][1]}: {name}역', place_en=en[name], place_ko=f'{name}역',
                  num=v, unit='people')
             for i, (name, v) in enumerate(top)]
    total = sum(seoul.values())
    facts.append(fact('railcom_total', 'railcommuter', 'All Seoul stations', grouped(total),
                      grouped(total), pin=True, label_ko='서울 시내 역 전체', num=total, unit='people'))
    return facts


def _korail_top_line(rows, name_field, value_field):
    """The busiest route/line as (korean_name, total), summed across every
    row sharing that name (e.g. one row per train model on a route)."""
    totals = {}
    for x in rows:
        name = (x.get(name_field) or '').strip()
        try:
            v = int(x.get(value_field) or 0)
        except (TypeError, ValueError):
            continue
        if name:
            totals[name] = totals.get(name, 0) + v
    return max(totals.items(), key=lambda kv: kv[1], default=None)


def _korail_line_en(ko_name):
    en = KORAIL_LINE_EN.get(ko_name)
    if not en:
        print(f'Warning: no English name for {ko_name!r} — '
              f'using Korean on the English card.')
        en = ko_name
    return en


def rail_facts(key):
    """Korea's busiest intercity route and Seoul's busiest commuter line,
    one month each. Counting, not modelling: each figure is the API's own
    published ridership total for the busiest named route/line, summed
    across whichever rows (train models) share that name."""
    if not key:
        return []
    facts = []

    inter_rows, inter_ym = _korail_newest_rows(key, 'mainLineRoutePer', 800)
    if inter_rows and inter_ym:
        y, m = int(inter_ym[:4]), int(inter_ym[4:])
        per_en, per_ko = f'{MONTHS_EN[m - 1]} {y}', f'{y}년 {m}월'
        top = _korail_top_line(inter_rows, 'rte_nm', 'utztn_nope')
        if top:
            ko_name, n = top
            en = _korail_line_en(ko_name)
            facts.append(fact('korail_inter_top', 'rail',
                              f'Riders on {en}, {per_en}',
                              grouped(n), grouped(n), pin=True,
                              label_ko=f'{ko_name} 이용객, {per_ko}',
                              period_en=per_en, period_ko=per_ko,
                              num=n, unit='people'))

    comm_rows, comm_ym = _korail_newest_rows(key, 'wideRailloadRoutePer', 300)
    if comm_rows and comm_ym:
        y, m = int(comm_ym[:4]), int(comm_ym[4:])
        per_en, per_ko = f'{MONTHS_EN[m - 1]} {y}', f'{y}년 {m}월'
        top = _korail_top_line(comm_rows, 'sbwy_ln_nm', 'ride_nope')
        if top:
            ko_name, n = top
            en = _korail_line_en(ko_name)
            facts.append(fact('korail_comm_top', 'rail',
                              f'Boardings on {en}, {per_en}',
                              grouped(n), grouped(n), pin=True,
                              label_ko=f'{ko_name} 승차인원, {per_ko}',
                              period_en=per_en, period_ko=per_ko,
                              num=n, unit='people'))
        # A third line, on top of the two "top line" facts above, so the vein
        # can stand on its own (build_pool()'s solo-card floor is 3 lines) and
        # not only ever ride along inside a bigger card. A straight sum of
        # every commuter line's own published boardings — still counting.
        comm_total = sum(int(x.get('ride_nope') or 0) for x in comm_rows)
        if comm_total:
            # ⚠️ '승차인원' here, not '이용객' — matching korail_comm_top's own
            # word for "boardings". check_labels() caught these disagreeing
            # (both pinned, so neither is rewritten) on the first live
            # --dry-run: same English word, two different Korean ones.
            facts.append(fact('korail_comm_total', 'rail',
                              f'Commuter rail boardings, {per_en}',
                              grouped(comm_total), grouped(comm_total),
                              pin=True, label_ko=f'광역철도 승차인원, {per_ko}',
                              period_en=per_en, period_ko=per_ko,
                              num=comm_total, unit='people'))
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
# The conditions are curated for recognizability AND for surviving a
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


# --- disease cost (3단상병별 시도별) ------------------------------------------
# A DIFFERENT HIRA dataset from the one hira_facts() reads above: that one is
# a live query API (diseaseInfoService1) capped at 8 curated conditions and
# patient COUNTS only. This one is data.go.kr's auto-converted odcloud wrapper
# around a file dump (dataset 15089587) — 26,750 rows, every 3-char KCD code
# x every 시도 x one year — and it carries actual WON figures
# (요양급여비용총액), which the other endpoint never does.
#
# ⚠️ Project memory called this "odcloud auto-API, uddi:00aaa3bc-..." and
# that uddi is real, but the memory was otherwise wrong: it is not a live
# queryable service with a year param, it is data.go.kr's auto-conversion of
# a FILE dump (the page is /data/15089587/fileData.do; openapi.do 404s — the
# "오픈 API" tab is an in-page SPA toggle on that same page, not a separate
# URL). Confirmed live 2 Sept 2026: api.odcloud.kr accepts the shared gov_key
# with no separate 활용신청, and `cond[COLUMN::EQ]=value` filters server-side.
#
# ⚠️ EACH YEAR IS A SEPARATE, UNGUESSABLE ENDPOINT. Unlike every other
# data.go.kr vein here, there is no year query param — data.go.kr mints a new
# opaque uddi per annual file, so HIRA_COST_UDDI below is a hardcoded table,
# not a formula, and it WILL go stale. When the next year's file lands
# (업데이트 주기: 연간; 차기 등록 예정일 shown on the page), add its entry by
# opening https://www.data.go.kr/data/15089587/fileData.do, clicking "오픈
# API", then "활용명세", and reading the newest GET row's uddi. The year used
# is read off this table's own max key, so a stale table means a stale
# footnote year, never a crash or a silent wrong year.
HIRA_COST_UDDI = {
    2018: 'beeada27-ea11-4969-a580-d763e05fb8c9',
    2020: '30d03588-fddc-44c5-81ab-9de6aa4ae0d6',
    2021: '1a69597e-840e-4f3f-89bc-abbd56392acf',
    2022: '1dcdd991-1451-4358-b68e-ffa7774d0f72',
    2023: '37939755-007a-48e1-89d5-dcb2e53841ab',
    2024: '00aaa3bc-9a0d-45b6-995f-8d65e3437702',
    2025: 'd443a743-6656-40fa-a753-b508c69209fa',
}
HIRA_COST_BASE = 'https://api.odcloud.kr/api/15089587/v1/uddi:{uddi}'

# Ten of the costliest conditions in Seoul (measured live 2 Sept 2026, top of
# 1,650 Seoul rows for 2025 by 요양급여비용총액) with verified plain-English
# glosses — same shape as HEALTH_CONDS above, a fixed named set ranked fresh
# every run, so an unnamed code never reaches a card. Overlap with
# HEALTH_CONDS (I10) is fine: same condition, a different published number
# each time (cost here, patient count there), never the same fact.
HEALTH_COST_CONDS = [   # (3-char KCD, EN gloss, KO gloss)
    ('C50', 'Breast cancer', '유방암'),
    ('K05', 'Gum disease', '잇몸병'),
    ('N18', 'Chronic kidney disease', '만성 콩팥병'),
    ('C34', 'Lung cancer', '폐암'),
    ('M17', 'Knee arthritis', '무릎 관절염'),
    ('M54', 'Back pain', '요통'),
    ('C22', 'Liver cancer', '간암'),
    ('I63', 'Stroke', '뇌경색'),
    ('C16', 'Stomach cancer', '위암'),
    ('I10', 'High blood pressure', '고혈압'),
]

# Set by hira_cost_facts() so compose() can put the claims year on the card.
HEALTH_COST_Y = {'y': None}


def hira_cost_facts(key):
    """Two framings of Seoul treatment cost, one condition per line: the raw
    total, and the average per patient (총액 ÷ 환자수 — HIRA's own 환자수 is
    already deduped within the condition, so this is a genuine per-person
    figure, the same reasoning avgbill's sales ÷ transactions relies on).
    Added 2 Sept 2026 at the user's request, after the vein shipped
    cost-only: 환자수 rides in the SAME response row, so this costs no extra
    calls. Two frames the selector must not mix on one card — same rule as
    kma_facts()'s yesterday-vs-fifty-years-ago split, because a card pairing
    a total with a per-patient figure compares two different units under one
    word, 'cost'. See SELECT_PROMPT."""
    if not key:
        return []
    year = max(HIRA_COST_UDDI)
    url = HIRA_COST_BASE.format(uddi=HIRA_COST_UDDI[year])
    got = []
    for code, en, ko in HEALTH_COST_CONDS:
        params = {'page': '1', 'perPage': '1',
                  'cond[시도구분::EQ]': '서울', 'cond[주상병코드::EQ]': code,
                  'serviceKey': key, 'returnType': 'JSON'}
        full = url + '?' + urllib.parse.urlencode(params)
        r = subprocess.run(['curl', '-s', '--max-time', '30', '-A', MOLIT_UA,
                            full], capture_output=True, text=True)
        try:
            row = json.loads(r.stdout)['data'][0]
            cost = int(row['요양급여비용총액(선별포함)'])
            patients = int(row['환자수'])
        except (json.JSONDecodeError, KeyError, IndexError, TypeError, ValueError):
            continue
        if not patients:
            continue
        got.append((code, en, ko, cost, patients))
    if len(got) < 3:
        return []
    HEALTH_COST_Y['y'] = year

    def frame(values, id_prefix, pair_name):
        """values: [(code, en, ko, n)]. One line per condition, plus a dead
        heat and a gap pair — hira_facts()'s own detector shape, run once
        per frame so the total and the per-patient figures never compete
        for the same pair."""
        lines = [fact(f'{id_prefix}_{code}', 'healthcost', en, won_en(n),
                      won_ko(n), label_ko=ko) for code, en, ko, n in values]
        best = None
        for i in range(len(values)):
            for j in range(i + 1, len(values)):
                a, b = values[i][3], values[j][3]
                gap = abs(a - b) / max(a, b)
                if gap <= 0.02 and (best is None or gap < best[0]):
                    best = (gap, values[i], values[j])
        if best:
            for code, en, ko, n in (best[1], best[2]):
                lines.append(fact(f'{id_prefix}heat_{code}', 'healthcost', en,
                                  won_en(n), won_ko(n), pair=f'{pair_name}_heat',
                                  label_ko=ko))
        hi = max(values, key=lambda t: t[3])
        lo = min(values, key=lambda t: t[3])
        if hi[3] and lo[3] and hi[3] / max(lo[3], 1) >= 3:
            for code, en, ko, n in (hi, lo):
                lines.append(fact(f'{id_prefix}gap_{code}', 'healthcost', en,
                                  won_en(n), won_ko(n), pair=f'{pair_name}_gap',
                                  label_ko=ko))
        return lines

    totals = [(code, en, ko, cost) for code, en, ko, cost, pts in got]
    per_patient = [(code, en, ko, round(cost / pts))
                   for code, en, ko, cost, pts in got]
    return (frame(totals, 'sickcost', 'sickcost')
            + frame(per_patient, 'sickcostpp', 'sickcostpp'))


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
        # No "A year's" here: the opener already says "A year at Seoul's
        # museums" (서울 박물관의 1년) and the footnote carries the survey year,
        # so the per-line prefix was saying it a third time. See the 28 Aug
        # 2026 post at 3mu3yywrhdj2x.
        facts.append(fact(fid, 'culture', f'Visitors to {en}',
                          grouped(n), grouped(n), pair='top_house', pin=True,
                          label_ko=f'{ko_name} 관람객'))
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
# The rows are curated to a whitelist of recognizable attractions with
# official English names. That is not just naming: the December feed
# carried a memorial hall with EIGHT visitors — a closure artifact in all
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

# Set by tour_facts() so compose() can put the month on the card. month_en/
# month_ko are the bare month name (no year) — added for the period-grouped
# cross-pair subhead (see PERIOD_GROUPED_CATS in compose()), which needs "June"
# on its own rather than re-parsing it out of "June 2026".
TOUR_M = {'en': None, 'ko': None, 'month_en': None, 'month_ko': None}


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
    TOUR_M['month_en'] = MONTHS_EN[first.month - 1]
    TOUR_M['month_ko'] = f'{first.month}월'
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
                              num=total, unit='people',
                              place_en=en, place_ko=ko_name))
        if forgn:
            facts.append(fact(f'tourfor_{ko_name}', 'tourism',
                              f'Foreign visitors to {en}',
                              grouped(forgn), grouped(forgn),
                              pair='tour_foreign',
                              place_en=en, place_ko=ko_name))
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
                              pair='tour_heat', place_en=en, place_ko=ko_name))
    hi = max(totals, key=lambda t: t[2])
    lo = min(totals, key=lambda t: t[2])
    if hi[2] and lo[2] and hi[2] / max(lo[2], 1) >= 3:
        for ko_name, en, n in (hi, lo):
            facts.append(fact(f'tourgap_{ko_name}', 'tourism',
                              f'Visitors to {en}', grouped(n), grouped(n),
                              pair='tour_gap', place_en=en, place_ko=ko_name))
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
# Set by boxoffice_facts() so compose() can date the card. month_en/month_ko
# mirror TOUR_M's — the bare month name, for the period-grouped cross-pair
# subhead.
BOXOFFICE_D = {'en': None, 'ko': None, 'month_en': None, 'month_ko': None}


_TITLE_MINOR_WORDS = {
    'a', 'an', 'and', 'as', 'at', 'but', 'by', 'for', 'from', 'in',
    'into', 'nor', 'of', 'off', 'on', 'onto', 'or', 'per', 'so', 'the',
    'to', 'up', 'via', 'vs', 'with', 'yet',
}


def _title_case_word(word):
    """Capitalize one word, honoring an internal hyphen: SPIDER-MAN -> Spider-Man."""
    return '-'.join(p[:1] + p[1:].lower() for p in word.split('-'))


def _fix_shouted_title(name):
    """KOFIC's own movieNmEn is inconsistently cased: most titles are properly
    styled ("The Odyssey") but some are published in full caps ("THE END OF
    OAK STREET" — confirmed straight off the API, movieCd 20264557, 27 Aug
    2026). isupper() catches only the shouted ones, and re-title-cases them
    with minor words lowercase except first and last, matching house style.
    A title that is upper case because it is genuinely all-initials (an
    acronym with no lowercase letters) would read the same way and get
    title-cased too; none has come up yet, and fixing that would need a named
    override in seoul_index_names_en.json rather than a blanket rule.
    """
    if not name or not name.isupper():
        return name
    words = name.split(' ')
    last = len(words) - 1
    return ' '.join(
        w.lower() if 0 < i < last and w.strip('.,:;!?()"‘’').lower()
        in _TITLE_MINOR_WORDS else _title_case_word(w)
        for i, w in enumerate(words))


def _kobis_title_en(key, movie_cd):
    """The English title KOFIC itself publishes for a film, or ''.

    The box office rows carry Korean titles only, and the English card carries
    no Hangul. Romanizing mechanically is the exact mistake
    seoul_index_names_en.json exists to prevent: 오디세이 is "The Odyssey", not
    "Odisei". One extra call per film named; a film with no English title on
    file is dropped rather than guessed at.
    """
    try:
        d = http_get_json(f'{KOBIS_BASE}/movie/searchMovieInfo.json'
                          f'?key={key}&movieCd={movie_cd}')
        en = (d['movieInfoResult']['movieInfo'].get('movieNmEn') or '').strip()
        return _fix_shouted_title(en)
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
    BOXOFFICE_D['en'] = f'{en_date(day)}'
    BOXOFFICE_D['ko'] = f'{day.month}월 {day.day}일'
    BOXOFFICE_D['month_en'] = MONTHS_EN[day.month - 1]
    BOXOFFICE_D['month_ko'] = f'{day.month}월'

    facts = []
    for cd, ko_name, en_name, audi in films:
        # label_ko is set rather than left for the selector: a film's Korean
        # title is not a translation of its English one, it is the other title
        # the distributor registered. pin keeps both ends as published.
        # Quoted (house style: titles of works in quotation marks, not bare) —
        # curly() turns the straight marks written here into curly ones at
        # render time, exactly as every other quote in this file is written.
        facts.append(fact(f'bo_{cd}', 'boxoffice', f'"{en_name}"',
                          grouped(audi), grouped(audi), label_ko=f'"{ko_name}"',
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
        # they may not know. The title itself is quoted (house style), which
        # sits fine with the bold-year regex: _YEAR_LEAD only matches the
        # leading "YYYY:" and leaves everything after the colon, quotes
        # included, exactly as given.
        facts.append(fact(f'boxscrn_{d:%Y}', 'boxhist',
                          f'{d.year}: "{en_name}"', grouped(scrn), grouped(scrn),
                          label_ko=f'{d.year}: "{ko_name}"', pin=True))
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

# Peers chosen to be recognizable to both an English and a Korean reader. Any
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

# NOT a typo for the constant above: 'nation' (World Bank, this section) and
# 'national' (KOSIS, kosis_facts()) are two distinct categories that happen to
# share the opener "Seoul and the nation", which is exactly how this one went
# unnoticed — a card_history.jsonl audit on 31 Aug 2026, prompted by a reader
# spotting the same fertility-rate card twice in 12 days, found 'national' had
# no cooldown of its own at all. It needs one more than most: kosis_facts()
# returns at most five facts, annual and near-static for months, forming only
# two possible pairs ('share_gap' population, 'fertility_gap' fertility) — an
# even smaller, more frozen pool than spending's quarterly pair.
NATIONAL_COOLDOWN_DAYS = 3
SEOUL_AREA_KM2 = 605.21    # Seoul Metropolitan Government official city area

# Peer countries recognizable to an English and a Korean reader; any country
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
               kobis_key=None, kopis_key=None, kepco_key=None):
    # gov_key is the shared data.go.kr key: one key, per-API 활용신청, so the
    # property, weather, airport, health and culture veins all ride on it.
    # Harvested here alongside everything else, once per run, though nothing
    # below reads it: compose() (called later, on whichever facts the selector
    # picks) is USD_RATE's only reader, for the card's "$1 ≈ ₩N" footnote.
    refresh_usd_rate(state)
    pool = []
    pool += crowd_facts(api_key, crowd_window(state))
    pool += air_facts(api_key)
    pool += transport_facts(api_key, state)
    # Held: see RUSH_LIVE. Gated here rather than by deleting the call, so the
    # vein cannot rot unnoticed while it waits and the switch is one word.
    if RUSH_LIVE:
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
    pool += wx_day_facts(gov_key)
    pool += kac_facts(gov_key)
    pool += iiac_facts(gov_key)
    pool += rail_facts(gov_key)
    pool += rail_stations_facts(gov_key)
    pool += seoul_station_facts(gov_key)
    pool += hira_facts(gov_key)
    pool += hira_cost_facts(gov_key)
    pool += culture_facts(gov_key)
    pool += tour_facts(gov_key)
    pool += rescue_facts(gov_key)
    # KOPIS issues its own key too, on application; the performances card is
    # silent without it.
    pool += kopis_facts(kopis_key)
    # KEPCO's own key as well (bigdata.kepco.co.kr); both electricity cards
    # are silent without it. One fetch per month per run, cached.
    _KEPCO_CACHE.clear()
    _KEPCO_HOUSE_CACHE.clear()
    pool += kepco_facts(kepco_key)
    pool += kepco_hist_facts(kepco_key)
    pool += kepco_house_facts(kepco_key)
    pool += rail_commuter_facts(gov_key, api_key)
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
  · ℹ️ A "tourism" + "boxoffice" CROSS_PAIR carries two different SPANS of time (a whole month against one day), and Python draws that itself: it groups the card, a subhead over each vein's lines reading its span ("August 30" / "The entire month of June"), so you do not need to and must not mention either span or the mismatch in a label, opener or note.
- House style is Harper's Index: let the arrangement carry the joke. NEVER add a line that explains or points out the juxtaposition, and never editorialize. Just the labeled numbers.
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
- "weather" lines are published readings from Seoul's official weather station: the last full month set against the SAME month FIFTY YEARS earlier, and (in summer) a season-to-date swelter tally — days of 33°C or more counted from 1 June through yesterday — likewise against the same span fifty years back (each label already carries its dates and year — do not reword those labels). Build them into their own post, never mixed with any other category, and pick ONE frame: the then-and-now monthly set OR the season-to-date set (never blend the two). A season-to-date post is built around the swelter tally ("Days of 33°C or more, June 1–…") — always include that pair; the hottest/wettest/tropical season-to-date pairs are its companions. In any then-and-now or season-to-date post every pair must keep BOTH its sides, and the arrangement carries the half-century — never point it out. ℹ️ Python owns the LAYOUT of these cards: it groups the lines by metric, draws each metric once as a subhead, and puts the newer year first in every group, so you do not have to order them and cannot get the two pairs out of step. Choose a coherent set of complete pairs and leave the rest alone. Open both fifty-year weather frames with "50 years apart" / "50년의 간격" (the numeral, not "Fifty").
- "wxday" lines are YESTERDAY's published readings at Seoul's reference weather station: the high, the low and the rain (a rain value of "None" means none was recorded, and it is a reading, not a gap) — own post, never mixed with any other category, including "weather". All the lines offered are compulsory, in that order. Its opener is FIXED and written by Python ("Seoul's weather yesterday"), so whatever opener you write for this card is replaced; the dateline carries the date. Never call the day hot, cold, wet or dry, and never compare it with anything.
- "rescue" lines are ONE WEEK of rescue notices filed by Seoul's districts on the national animal protection register: every animal, then cats, dogs and other animals — own post, never mixed with any other category. All FOUR lines are compulsory, used together, in that order. Its opener is FIXED and written by Python ("Animals rescued in Seoul"), so whatever opener you write for this card is replaced; the dateline carries the week and the footnote says who files the notices. Never call the week busy or quiet, never remark on the split between cats and dogs, and never mention shelters, adoption or what became of any animal.
- "kopis" lines are ONE WEEK on Seoul's stages from the national box-office register: productions, productions that opened, performances given, tickets sold and box office — own post, never mixed with any other category, including "boxoffice" (that is cinema). All FIVE lines are compulsory, used together, in that order. Its opener is FIXED and written by Python ("On stage in Seoul"), so whatever opener you write for this card is replaced; the dateline carries the week and the footnote names the register. Never call the week busy or quiet, never name a show, and never compare the figures with anything.
- "kepco" lines are ONE MONTH of electricity in Seoul from Korea Electric Power Corporation: customers, electricity used, the households' share, the shops-and-offices share, and the bill — own post, never mixed with any other category, including "kepcohist". All FIVE lines are compulsory, used together, in that order. Its opener is FIXED and written by Python ("Electricity in Seoul"), so whatever opener you write for this card is replaced; the dateline carries the month and the footnote says which tariffs the two shares are. Never call the month heavy or light, and never compare it with anything.
- "kepcohist" lines set ONE MONTH of Seoul's electricity against the SAME month TWENTY YEARS earlier: electricity used, the bill and customers, each as a pair (each label already carries its month and year — do not reword those labels) — own post, never mixed with any other category, including "kepco". Use ALL SIX lines, every pair with BOTH its sides. Its opener is FIXED and written by Python, so whatever you write is replaced. The arrangement carries the twenty years — never point out that the bill rose faster than the use, or that anything rose or fell at all.
- "kepcohouse" lines are ONE MONTH's household electricity by Seoul district from Korea Electric Power Corporation: the district with the most used per household, the least, the highest average bill and the lowest — own post, never mixed with any other category, including "kepco". All FOUR lines are compulsory, used together, in that order. Its opener is FIXED and written by Python ("Household electricity in Seoul"), so whatever opener you write is replaced; the dateline carries the month. Never call a district rich or poor, hot or cold, and never explain the gap.
- "railcommuter" lines are ONE MONTH's boardings at Korail's commuter-rail stations inside Seoul from the Korea Railroad Corporation: the busiest, second and third stations and the total for all Seoul stations — own post, never mixed with any other category, including "rail", "railstations" and "stations". All FOUR lines are compulsory, used together, in that order. Its opener is FIXED and written by Python ("Seoul's commuter rail"), so whatever opener you write is replaced; the dateline carries the month and the footnote says which lines these are. The lines carry BARE STATION NAMES; never call a station busy or quiet.
- "tourism" lines are one month's visitor counts at named paid-admission Seoul attractions (the palaces, Lotte World, Seoul Sky…). Own post; ONE frame per post — total visitors OR foreign visitors, never both; the month rides on the card automatically. The pairs are the point: a dead heat or the widest gap between two named attractions.
- "river" lines are readings taken at ONE hour: the water temperature in the Han (at Seonyu) and in three tributaries, plus the AIR temperature over central Seoul at that same hour. Build them into their own post, never mixed with any other category, and ALWAYS INCLUDE "The air" line — it is the whole point. Four river temperatures alone sit within about a degree of each other and say nothing; the contrast is the water disagreeing with the sky. Labels are BARE NAMES ("The Han at Seonyu", "The air"), so the opener MUST carry the metric and nothing more, e.g. "Water and air in Seoul" (ℹ️ whatever you write here is REPLACED in compose(): the opener names air or water first to match whichever the sort puts on the top line, which is a fact about the readings rather than a choice of words) — the same case as the world, traffic and books lines. ⚠️ Do NOT put the hour, the time or the words "one hour" in the opener: the reading hour rides on the card automatically as its dateline, and an opener repeating it spends the line saying nothing. Do NOT write "right now" either: that hour can be several hours old. Never point out that the water is warmer or cooler than the air; let the arrangement do it.
- "level" lines appear ONLY when the Han is running high, and they are one gauge (잠수교) set against its own published flood-warning tiers: the level right now, then the 관심/주의/경계/심각 levels. Build them into their own post, never mixed with any other category, and include the current level plus at least two tiers — the arrangement IS the story, which is how far the river is from each tier. The opener must name the river and the gauge, e.g. "The Han at Jamsu Bridge". ⚠️ NEVER write or imply that the bridge is closed, submerged, flooded or about to be: these are flood-WARNING tiers set by 한강홍수통제소, not the level at which the walkway goes under, and the two are different things. Do not add alarm, urgency or commentary of any kind — state the levels and stop. Never call the situation dangerous.
- "price" lines are ONE everyday item priced at shops across Seoul on one day. Each label is a bare shop KIND and DISTRICT ("A traditional market in Dongjak-gu", "A supermarket in Nowon-gu"), so the opener MUST name the item and that these are its prices — e.g. "What a watermelon costs in Seoul" — the same case as the world, traffic and books lines. Build them into their own post, never mixed with any other category, and ALWAYS keep the cheapest and dearest lines: the card IS the spread. Never point out that markets are dearer than supermarkets or the reverse — it changes from item to item, and noticing it is the reader's job. Never call a price high, low, cheap or a bargain.
- "water" lines are the raw water drawn at each of Seoul's purification centers on one day. Labels are BARE PLACE NAMES (Amsa, Ttukdo), and the card's dateline says they are purification centers, so the opener MUST name the METRIC and nothing more ("Water drawn for Seoul"). ⚠️ Do NOT put the day, the date or the words "one day" in the opener: the date rides on the card automatically and an opener that repeats it spends the line saying nothing. Own post, never mixed. Every line is an intake figure at the same measure — never say one center is bigger or busier than another.
- "daynight" lines are HOW MANY PEOPLE ARE PRESENT in each district, either by DAY or by NIGHT, never both in one card. Labels are BARE DISTRICT NAMES, so the opener MUST name BOTH what is counted — people, a population — AND which half of the day: "Seoul's daytime population", "How many people are in Seoul after dark". ⚠️ An opener naming only the time ("Seoul by day") is NOT enough and leaves the reader guessing whether the figures are people, money or anything else. The measure is 생활인구: everyone present at that hour, residents and workers and visitors together — so never call it the district's population in the sense of who LIVES there, and never call it a crowd. Own post, never mixed. The KT-estimate caveat rides on the card already: do not restate it in a line.
- "infant" lines count Seoul's children in ONE age band, one line per year across a decade. Labels are BARE YEARS. ⚠️ The card already names the age band on its own line, and YOU ARE NOT TOLD WHICH BAND IT IS — so the opener must NEVER state an age or an age range. Writing "Children aged 0" over the under-six figures is the exact mistake this rule exists to stop. Give a neutral opener that says only that these are Seoul's children over time: "Seoul's children, a decade apart", "Fewer every year in Seoul". Own post, never mixed, and keep the first and last years: the fall between them is the card. State it and stop — never call it a decline, a crisis, or a collapse, and never mention birth rates.
- "library" lines are the registered members of Seoul Library by decade of life. Labels are BARE AGE BANDS, so the opener MUST name the library and what is counted ("Who holds a card at Seoul Library"). Own post, never mixed. It is ONE library, not the city's 215 — never imply otherwise. ⚠️ The value may carry a trailing "(1 in N)" — that is Python's, and it sets the members of that band against Seoul's registered population of that age. Leave it exactly where it is and NEVER restate it, convert it to a percentage, explain it, or build the opener or a label on it: the card footnote says what it is, and members need not live in Seoul, so the opener must never call it a share of Seoul's teens or of any other age.
- "complaint" lines are how many faults Seoul's residents reported in a whole year, one line per year. Labels are BARE YEARS, so the opener MUST name what is counted ("Things reported broken in Seoul"). Own post, never mixed, and never characterize a year as better or worse than another.
- "airport", "health", "healthcost" and "culture" lines are single-source sets like "property" and "weather": each builds its OWN post, never mixed with another category. An airport post is Gimpo's newest month — pick ONE frame, the twenty-year pair or the domestic/international split. ⚠️ Do NOT put the month in the opener: on the split frame it rides on the card automatically as its dateline, and on the twenty-year pair each label carries its own year, which is the whole point of that frame. A health post is patient counts at Seoul care institutions in one year: the labels are bare condition names, so the opener must carry the "a year in Seoul's clinics" framing. A healthcost post is the SAME shape but treatment COST, not patient counts, and it comes in TWO FRAMES you must not blend on one card: the raw total cost per condition (treat it like "spending"/"property" for tone — a citywide sum, never implied per-person), OR the average cost PER PATIENT (like avgbill: the opener must say "average" plainly, e.g. "What treating each condition costs, per patient", so a reader never mistakes it for the total or for what one patient actually pays out of pocket — insurance covers most of it). Pick one frame, not lines from both. Both health and healthcost: these are real illnesses — arrange the numbers, never joke about them, and drop any set that reads as a punchline at patients' expense. A culture post is the city's museums and galleries: the counts and the year's most-visited houses.
- "bike" lines are the public-bike system (Ttareungi) counted live, citywide, right now: bikes waiting at a dock, docking points, stations, and stations standing empty. These are live "right now" figures like the crowd and air lines — build them into their own post, and the opener MUST carry the "right now" framing so the bare counts read as a live snapshot, not fixed totals. The pair is the point: bikes waiting against docking points, or empty stations against all stations. Never mix a bike line with a spending, national, world or other single-source line.
- "traffic" lines are live road speeds (km/h) on named Seoul arteries, right now. Like the "world" lines, the labels are BARE ROAD NAMES, so the opener MUST name the metric and the time ("How fast Seoul is driving right now", or a neutral live-speed framing) — this is the other case where the opener names the metric. Build them into their own post; the pair is the gap between the fastest-moving and slowest-moving road. Never mix a traffic line with any other category.
- "transport" lines are Seoul's total subway and bus boardings for the most recently published day, plus that day's busiest and quietest subway stations. The subway and bus TOTAL labels already carry the date in the label itself ("Subway boardings on August 26", "Bus boardings the same day") — there is no separate dateline to lean on here, so do NOT put a date anywhere in the opener, and do NOT write a second, different date of your own: a neutral opener with no date at all is enough, e.g. "Through the turnstiles", "Seoul on the move". Never call a station busy, quiet, packed or empty — the four numbers say it.
- "busroutes" lines are that day's busiest, second-busiest and quietest Seoul bus routes by plain route number ("Busiest: 143"), plus the day's total bus boardings — own post, never mixed with any other category, including "transport" above (that vein's own bus/subway totals are a different card). All FOUR lines are compulsory and must be used together, in that order: this is a complete small ranking, not a selection from it, the same rule "boxoffice" uses for its top four films. The dateline carries the date, so do NOT put a date anywhere in the opener and do NOT write a second one of your own — the opener MUST name buses or bus routes, because the lines carry BARE ROUTE NUMBERS with no "Route" word ("Busiest: 143"); like "busstops" its opener is FIXED and written by Python ("On the buses") and whatever opener you write for this card is replaced, and it must never say "today", since the day shown is several days behind. Never call a route busy, quiet, packed or empty, and never remark on the gap between the busiest and quietest lines: the numbers say it. If the footnote already names a route's winning streak, do not repeat or rephrase that fact in the opener — it would say the same thing twice on one card.
- "stations" lines are that day's busiest, second-busiest and quietest Seoul SUBWAY stations by official English name ("Busiest: Seoul Station"), plus the day's total subway boardings — own post, never mixed with any other category, including "transport" and "busroutes" above. Exactly the same rules as "busroutes": all FOUR lines are compulsory, used together, in that order; the dateline carries the date, so do NOT put a date in the opener; the opener MUST name the subway or its stations, because the lines carry BARE STATION NAMES with no "station" word ("Busiest: Seoul Station", "Quietest: Dorimcheon"); like "busstops" its opener is FIXED and written by Python ("On the subway") and whatever opener you write for this card is replaced, and it must never say "today", since the day shown is several days behind; and it MUST NOT settle on one wording; never call a station busy, quiet, packed or empty, and never remark on the gap between the busiest and quietest lines.
- "busstops" lines are that day's busiest, second-busiest and third-busiest Seoul BUS STOPS ("Busiest: Hongik University Station", one stop per name, the busier side of the road), plus how many stops took at least one boarding that day — own post, never mixed with any other category, including "transport", "busroutes" and "stations" above. Exactly the same rules as "stations": all FOUR lines are compulsory, used together, in that order; the dateline carries the date, so do NOT put a date in the opener; the lines carry BARE STOP NAMES with no "stop" word, and a stop named after a station reads as the station unless the title says these are bus stops, so like "busmovers" its opener is FIXED and written by Python ("Seoul's bus stops") and whatever opener you write for this card is replaced; never call a stop busy, quiet, packed or empty, and never remark on the gap between the lines. There is no quietest line on this card, by design.
- "railstations" lines are the four busiest of Seoul's Korail stations by boardings on one day ("Seoul Station", "Yongsan"), from the Korea Railroad Corporation — own post, never mixed with any other category, including "rail" and "stations" above (the subway card is a different thing). All FOUR lines are compulsory, used together, in that order; the dateline carries the date and the footnote the operator note, so do NOT put either in the opener. The lines are BARE STATION NAMES, so like "busmovers" its opener is FIXED and written by Python ("Seoul's railway stations") and whatever opener you write for this card is replaced. Never call a station busy or quiet, and never remark on the gap between the lines.
- "seoulstation" lines are ONE station, Seoul Station, on one day: how many boarded Korail trains there, how many got off, the two together ("Passengers"), and "A typical Tuesday", the median of the previous same weekdays — own post, never mixed with any other category, including "rail", "railstations" and "stations". All FOUR lines are compulsory, used together, in that order. Its opener is FIXED and written by Python ("Seoul Station"), so whatever opener you write for this card is replaced; the dateline carries the date and the footnote says what typical means. Never guess WHY the day differs from a typical one, and never call the station busy or quiet.
- "stationgap" lines are ONE Seoul subway station on one day, the station with the widest gap between people getting off and getting on: "Got off at Yeouinaru" and "Got on at Yeouinaru", the larger first — own post, never mixed with any other category. BOTH lines are compulsory, in the order given: a two-line card by design, like "rush". Its opener is FIXED and written by Python ("Seoul's subway, one station"), so whatever opener you write for this card is replaced; the dateline carries the date and the footnote says how the station was chosen. Never state or hint at the difference between the two figures, never guess WHY they differ, and never call the station busy or quiet.
- "busmovers" lines are the day's biggest RISE and biggest FALL in bus boardings, route by route, each against that route's own typical figure for the same weekday ("Up the most: 5511, 16,571 boardings" with a value of "+40%"). Own post, never mixed with any other category. BOTH lines are compulsory, in that order (up, then down): this is a two-line card by design, like "rush". Like "rush", its opener is FIXED and written by Python ("Seoul's bus routes, against their usual Monday"), so whatever opener you write for this card is replaced; the dateline carries the date and the footnote the comparison. Never guess WHY a route rose or fell.
- "nightbus" lines are the day's busiest, second-busiest and quietest NIGHT bus routes (Seoul's N routes, which run through the small hours) plus the night total — own post, exactly the "busroutes" rules: all FOUR lines, in order, no date in the opener, the opener MUST name night buses, since the lines carry BARE ROUTE NUMBERS; like "busstops" its opener is FIXED and written by Python ("On the night buses") and whatever opener you write for this card is replaced, and it must never say "today" or "tonight", since the day shown is several days behind; never "busy" or "quiet" as adjectives.
- "busweekend" lines are the two routes whose boardings changed MOST between weekdays and the weekend over one week: the one that holds up best at the weekend and the one that falls most ("Holds up best: Route 271, 9,880 a day" with a value of "+3%" or "−12%"). Own post, BOTH lines in that order: a two-line card by design, like "rush"; the dateline carries the week and the footnote the comparison. Like "rush" and "busmovers", its opener is FIXED and written by Python ("Seoul's bus routes, weekend against weekday"), so whatever opener you write for this card is replaced. Never guess why.
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


def apply_holds(pool):
    """Drop every HELD_CATS vein from the pool (see HELD_CATS), except the
    one a hand-run --only asks for. Says so in the log each run, so a held
    vein never reads as a broken harvester."""
    held = {c for c in HELD_CATS if c != ONLY_CAT}
    n = sum(f['cat'] in held for f in pool)
    if n:
        print(f'Held back by HELD_CATS ({", ".join(sorted(held))}): {n} facts withheld.')
    return [f for f in pool if f['cat'] not in held]


def route_map_words(info, n_stops, served=None):
    """(caption, alt) for a ranked bus card's route map, `n_stops` being the
    backdrop's stop count as drawn. Both carry his sentence from the bus
    stops map ("The map is composed of gray dots that represent each of
    Seoul’s N bus stops", 11 September 2026): until 12 September the routes
    map said only "a faint backdrop of every Seoul bus stop" in its alt and
    nothing about the dots on the picture, and he remembered the fuller
    wording as being on both. The count is the backdrop actually drawn, so
    the sentence can never disagree with the picture.

    `served`, from bus_route_map_stops(with_served=True), adds the sentence
    for the coloured dots (the stops each route served, the footnote's
    count) and, only when a served stop had no place in Seoul's stop table
    (5515 on 8 September 2026: 32 served, 31 placeable, the other a
    Gyeonggi stop), says how many are not drawn, by route, so the picture
    and the footnote can never silently disagree."""
    dots = (f'The map is composed of gray dots that represent each of Seoul’s '
            f'{grouped(n_stops)} bus stops.')
    base = info['map_caption'].rstrip()
    if not base.endswith('.'):          # the callers' captions end bare
        base += '.'
    parts = [base]
    if served is not None:
        parts.append('The coloured dots are the stops each route served that day.')
        for _, _, no in info['map_routes']:
            sv = served.get(no) or {}
            missing = sv.get('count', 0) - len(sv.get('points', []))
            if missing > 0:
                parts.append(f'{missing} of Route {no}’s {sv["count"]} served stops '
                             f'{"lies" if missing == 1 else "lie"} outside Seoul’s stop '
                             f'table and {"is" if missing == 1 else "are"} not drawn.')
    parts.append(dots)
    caption = ' '.join(parts)
    nos = [no for _, _, no in info['map_routes']]
    alt = (f'Map of {len(nos)} Seoul bus routes, {info["day_en"]}: '
           + '; '.join(label for label, _, _ in info['map_routes'])
           + '. Drawn from each route’s stops in the day’s feed. '
           + ' '.join(parts[1:-1] + [dots]) + ' '
             'Not necessarily each route’s full official path.')
    return caption, alt


def daily_data_day(cat):
    """The data day `cat`'s card is built on ('YYYYMMDD'), read from
    RANKED_CARD_INFO once the harvest has run, or None for a vein that
    records none. Only the ranked bus cards carry one ('map_day'); a --daily
    on any other vein has no same-day guard, and says so in the log."""
    return (RANKED_CARD_INFO.get(cat) or {}).get('map_day')


def daily_already_posted(cat, state):
    """(True, reason) when the day `cat`'s card would show is the day its
    last post showed. The stop-level feed lags about four days and had
    advanced every day for 64 days when this was written (12 September
    2026), so a stalled feed is the exception this exists for: without it
    the 10:30 job would put 8 September's card out twice, streak line and
    all. It reads the DATA DAY, never the clock: a kickstart at 10:00 and
    the slot at 10:30 build the same day's card, and a day the feed skips
    is not the same day however long ago the last post was."""
    day = daily_data_day(cat)
    if not day:
        return False, f'no data day recorded for {cat}, so no same-day guard'
    last = (state.get('daily_last_day') or {}).get(cat)
    if last == day:
        return True, (f'{cat}: the newest day in the feed ({day}) is the day '
                      f'already posted; nothing new')
    return False, ''


def stamp_daily_day(state, primary):
    """Record the data day `primary` just posted, for daily_already_posted().
    Stamped on every live post of a vein that carries a day, hand-run or
    scheduled, so a --only post in the morning stops a --daily repeat of the
    same day too."""
    day = daily_data_day(primary)
    if day:
        state.setdefault('daily_last_day', {})[primary] = day


def apply_cooldown(pool, state, stamp_key, cat, days, label):
    """Drop `cat` from the pool if its last post is younger than `days`.

    Shared by the world, spending, bike, traffic, transport, national and
    tourism veins, each reached for far more often — relative to how much
    genuinely different content it can produce — than its share of the pool
    warrants. Two deliberate refusals to
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
    if ONLY_CAT == cat:
        # --only=<cat> asks for THIS vein now, by hand. Withholding it on its
        # own cooldown made `--only=busroutes` exit "0 fact(s) in that vein"
        # the day after it posted (10 September 2026), which reads as a
        # broken harvester rather than a guard doing its job. The scheduled
        # path never sets ONLY_CAT, so the cooldown is untouched there.
        # ⚠️ But a post made within ONLY_MIN_HOURS is refused outright (see
        # FORCE): that is a duplicate in the making, not a stale cooldown.
        if stamp and not FORCE:
            try:
                age_h = (datetime.now(timezone.utc) - datetime.fromisoformat(stamp)).total_seconds() / 3600
            except (ValueError, TypeError):
                age_h = None
            if age_h is not None and age_h < ONLY_MIN_HOURS:
                if DAILY:
                    # A kickstart and the slot itself, or two firings after a
                    # sleep: the same day's card, not a fault. Exit 0.
                    print(f'--daily={cat}: {label} posted {age_h:.1f}h ago, under '
                          f'{ONLY_MIN_HOURS}h; not reposting. No post this run.')
                    sys.exit(0)
                sys.exit(f'--only={cat}: {label} posted {age_h:.1f}h ago, under '
                         f'{ONLY_MIN_HOURS}h; refusing a duplicate. Pass --force to repost.')
        return pool
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

    Never-posted veins go first, then the longest-neglected. Promotions do not
    normally land back to back; a vein that has never led a card at all is one
    exception, so a debut queue drains at one a post instead of one every two.
    A vein stuck past SEVERE_STARVE_DAYS is the other: the floor's own
    throughput cannot give every vein its 5-day slot once the roster is this
    large (see SEVERE_STARVE_DAYS), so the bar also lifts rather than let a
    single vein queue indefinitely behind everyone else's turn.
    Returns (pool, promoted_cat), with promoted_cat None when nothing is
    promoted and the pool handed back untouched.
    """
    counts = collections.Counter(f['cat'] for f in pool)
    seen = state.get('cat_last_at') or {}
    now = datetime.now(timezone.utc)
    starved = []
    for cat, n in counts.items():
        # rush is a genuine two-line vein by design (one station, two hours)
        # and can never clear the general floor; every other vein still needs
        # STARVE_MIN_FACTS, which is what guards against a card too thin to
        # be worth a slot.
        if n < STARVE_MIN_FACTS and cat not in TWO_LINE_CATS:
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

    # Two promotions running would put the feed on rails. last_cat is the record
    # of what the previous card WAS, so comparing it with the cat we promoted
    # last time needs no second flag to fall out of step with it (a duplicate
    # flag is exactly what put two spotlights back to back on 22 Jul 2026).
    #
    # ⚠️ A vein that has NEVER led a card overrides that, and the reason is a
    # measurement rather than a preference. On 25 Aug 2026 the queue was 12 to 15
    # veins deep and draining at one promotion per two posts, so five veins live
    # since 22 July (weather 18 facts, health 12, property 8, airport 5, culture
    # 4: 47 of 258, 18% of the pool) had still never once led a card, and sat at
    # the head of every run's 'Also starved' line waiting their turn. The
    # alternation rule was holding back the debut it exists to deliver.
    #
    # It is safe to relax HERE and nowhere else because the never-posted set only
    # ever shrinks: each debut removes one, and when it empties the rule turns
    # itself off with no flag to reset. A vein shipped later re-arms it, which is
    # exactly when a debut is wanted again. Do not widen this to 'the queue is
    # deep': with 26 veins, STARVE_DAYS = 5 and four posts a day, an even
    # rotation leaves most veins nominally starved most of the time, so a
    # depth test would be permanently true and the guard would be dead code.
    back_to_back = (state.get('last_cat')
                    and state.get('last_cat') == state.get('last_promoted_cat'))
    debut_waiting = any(age is None for age, _, _ in starved)
    # ⚠️ Age, not count: severe is about the SINGLE worst-waiting vein, never
    # "how many are starved" (see SEVERE_STARVE_DAYS for why a depth test is
    # the wrong shape here). Promoting that vein below resets its own age to
    # zero, which is what keeps this self-correcting rather than a standing
    # bypass once the roster is big enough that it would trip on every call.
    severe_waiting = any(age is not None and age >= timedelta(days=SEVERE_STARVE_DAYS)
                         for age, _, _ in starved)
    if back_to_back and not (debut_waiting or severe_waiting):
        return pool, None

    # Never-posted first; then oldest-seen first.
    starved.sort(key=lambda t: (t[0] is not None,
                                -(t[0].total_seconds() if t[0] else 0)))
    age, cat, n = starved[0]
    waited = 'never posted' if age is None else f'{age.days}d since last card'
    others = ', '.join(c for _, c, _ in starved[1:]) or 'none'
    if back_to_back and debut_waiting:
        run_on = ' (back to back: a vein still waiting to debut)'
    elif back_to_back and severe_waiting:
        run_on = f' (back to back: past SEVERE_STARVE_DAYS={SEVERE_STARVE_DAYS}d)'
    else:
        run_on = ''
    print(f'Vein floor: promoting {cat} ({waited}){run_on}; this card is built '
          f'from its {n} facts alone. Also starved: {others}.')
    return [f for f in pool if f['cat'] == cat], cat


# ⚠️⚠️ Both `claude -p` calls in this file (the selector below and _ask_json,
# the label check) carry these flags, since 11 September 2026: no tools at
# all. They are text in, JSON out, and need none; unconfined, `claude -p` is
# an AGENT with Bash in this Mac's home directory, fed a prompt built from
# public-data labels four times a day. Found in everygeorgia's transcriber
# that day: on a hard image it cropped and enlarged the file through a dozen
# tool calls (60-280 s each), and on one page it ran `find ~ -iname clips.py`,
# read that project's own code, executed it on three other pages and returned
# a progress report as the answer. --restricted also ignores the user's
# settings files, so no hook fires from inside a scheduled post. The calls
# pass stdin=DEVNULL because the CLI otherwise waits three seconds for stdin
# on every hand-run call. Verified the same day: `--tools ""` is accepted by
# the Mini's CLI under the bots' own Keychain token, and a real selection
# comes back as the same JSON.
CONFINED = ['--restricted', '--tools', '']


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
            r = subprocess.run(['claude', '-p', *CONFINED, '--model', CLAUDE_MODEL, prompt],
                               capture_output=True, text=True, env=claude_env(),
                               stdin=subprocess.DEVNULL, timeout=CLAUDE_TIMEOUT)
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


def complete_busroutes(picks, pool, cat='busroutes'):
    """A busroutes (or stations) card carries all four of busiest/2nd-busiest/
    quietest/total, or it is not one — the same hole-in-the-ranking problem
    complete_boxoffice() exists to prevent, on a ranking a quarter the length.
    Own-vein cards only, for the same cross-pair reason as boxoffice."""
    by_id = {f['id']: f for f in pool}
    if not picks or any(by_id[p['id']]['cat'] != cat for p in picks):
        return picks
    have = {p['id'] for p in picks}
    return picks + [{'id': f['id'], 'emoji': ''} for f in pool
                    if f['cat'] == cat and f['id'] not in have]


def even_out_emoji(lines, cats):
    """Within any one vein, all lines carry an emoji, or none of them do.

    The prompt tells the selector to tag a line ONLY where an obvious emoji
    exists, which is right for a genuinely mixed CARD: a cross-vein pair post
    can carry a fully-tagged vein beside a fully-bare one. It is wrong within
    ONE vein's own lines, where every line is the same KIND of thing (four
    films, four museums, four bike stats) and an emoji on some but not all
    reads as an oversight rather than a judgement — exactly what the second
    box office live preview looked like: 🕷 Spider-Man, 👻 Insidious, 🕵️ Conan,
    and The Odyssey bare at the top.

    Deterministic rather than another sentence in the prompt: the selector is
    being asked for a judgement per line, and consistency across lines is not
    a judgement, it is a rule. Applied per category (not globally across the
    whole card) so a genuine cross-vein pair is left alone; universal across
    every category (not a chosen allowlist) since 31 Aug 2026.
    """
    for cat in cats:
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
# while the live line dates itself inline, the card splits into two labeled
# groups — the dated month over its lines, "Right now" over the live ones. The
# masthead dateline stays the design for single-frame dated cards. See compose().
LIVE_CATS = {'crowd', 'air', 'bike', 'traffic'}
# Veins that carry a liftable month/quarter period (they set a dateline). The
# same ones the dateline logic promotes; used early, before scope is built, to
# spot a groupable live+dated cross pair while ordering the lines. ⚠️ airport
# carries one only on its single-month frame — the twenty-year pair spans two
# months and lifts nothing — so membership here means "may set a dateline", and
# grouped is confirmed later against whether one actually did.
DATED_PERIOD_CATS = {'tourism', 'property', 'spending', 'avgbill', 'boxoffice',
                     'rush', 'airport', 'incheon', 'rail'}
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
# optimizing for wit and arrangement across a whole card, and a reader asked
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
    r = subprocess.run(['claude', '-p', *CONFINED, '--model', model, prompt],
                       capture_output=True, text=True, env=claude_env(),
                       stdin=subprocess.DEVNULL, timeout=CLAUDE_TIMEOUT)
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


# Any Hangul syllable. NOT the CJK ideographs: 90 days of every feed held not
# one Hanja, so widening this would be guessing at a case that has never
# happened, and every character added can hide a real fault. Same reasoning as
# bot_variety_check.py's Korean ranges.
HANGUL = re.compile(r'[가-힣]')


def check_korean(lines, opener_ko, opener_en, log=print):
    """Is the Korean card in Korean?

    On 24 August 2026 a crowd card went out with three of its four Korean
    labels still reading "Estimated crowd in Seoul Station right now". Nothing
    noticed: check_labels below asks a MODEL whether a label still says what its
    figure is, and an English label does say that. Language is a different
    question and a regex owns it outright, so this runs unconditionally rather
    than behind CHECK_LABELS, needs no network, and cannot itself fail.

    ⚠️ IT REPORTS AND REPAIRS NOTHING, deliberately. There is no Korean to fall
    back to: for the veins the selector translates, its answer is the only
    Korean that exists, and the pool's own label is the English this is
    complaining about. The veins that DO own their Korean (crowd, spotlight,
    rush set label_ko) never reach the selector for it and so can no longer fail
    this way at all — the 24 August card could not recur today.

    ⚠️ And it never blocks a post. The measured rate is one card in ninety-nine,
    and a card with English labels is a bad card while a card that never posts
    is a dead bot. That also means a false positive costs one log line, which is
    why there is no exemption list: a Latin-only Korean label is conceivable (a
    film title on a boxoffice card) and has never once occurred — 0 of 99 Korean
    cards in the feed on 26 August 2026, the three above being the only Latin
    labels in the whole history. An exemption for a case that has never happened
    is a guess that can hide a real one.

    The report says whether each flagged label is BYTE-IDENTICAL to its English
    sibling, because that is what separates the failure seen (an untranslated
    copy) from the one imagined (a proper noun with no Hangul in it).
    """
    bad = [{'ko': l['label_ko'], 'en': l['label_en'],
            'copied': l['label_ko'] == l['label_en']}
           for l in lines if not HANGUL.search(l['label_ko'] or '')]
    if opener_ko and not HANGUL.search(opener_ko):
        bad.append({'ko': opener_ko, 'en': opener_en,
                    'copied': opener_ko == opener_en, 'opener': True})
    if not bad:
        return []
    for b in bad:
        where = 'opener' if b.get('opener') else 'label'
        how = 'copied from the English' if b['copied'] else 'no Hangul'
        log(f'  !! Korean {where} is not Korean ({how}): {b["ko"]!r}')
    _observe_korean(bad)
    return bad


def _observe_korean(bad):
    """⚠️ Dry runs report nothing, exactly as _observe_labels refuses to: a test
    filing itself with the Sunday review is a fault invented by reporting it."""
    if not reporting():
        return
    copied = sum(1 for b in bad if b['copied'])
    text = (f'{len(bad)} Korean label(s)/opener carried no Hangul '
            f'({copied} byte-identical to the English): {bad[0]["ko"][:60]!r}')
    try:
        subprocess.run(
            ['python3', str(OBSERVE), 'add', '--source', 'seoul-index-korean',
             '--kind', 'finding', '--key', 'seoul-index-korean-untranslated',
             text], check=False, capture_output=True)
    except OSError:
        pass


# A trailing clause carrying a four-digit year: "…, July 2026", "…, 1 June–25
# August 1976". The `$` is the meaningful anchor — it is what separates a date
# that QUALIFIES a label from a year that IS one, as on the boxhist card, where
# "2026: The Odyssey" has the year exactly where it belongs.
#
# ⚠️ The comma is NOT load-bearing and the comment here used to imply it was.
# Measured 27 August 2026 against the looser `([^,]*(?:19|20)\d\d[^,]*)$` over
# all 81 cards in card_history.jsonl: one hit each, the same card, nothing
# either form found that the other missed. It is kept because it is the
# narrower reading of "a date appended to a label" and it yields a clean tail
# for the report, not because any card has ever needed it. The cost is that a
# comma-less wording ("Passengers through Gimpo July 2026") would slip past;
# widen it the day one appears, rather than guessing at one now.
_TRAILING_YEAR = re.compile(r', ([^,]*(?:19|20)\d\d[^,]*)$')


def check_masthead(lines, dateline_en, dateline_ko, grouped, log=print):
    """Is a date sitting on every row of the card and nowhere above them?

    On 27 August 2026 the Gimpo card went out as three rows each ending
    "July 2026" with no dateline at all, while the property card two days
    earlier flew "June 2026" in red under its title and left its rows bare.
    Nothing noticed, because that is not a card that looks broken: it renders
    perfectly and merely says the same four words three times.

    The vein was fixed where it lives, but the SHAPE is not vein-specific and
    the next one to bake a month into a label would repeat it in silence. This
    asks the question of the finished card instead, so it holds for veins that
    do not exist yet.

    ⚠️ The test is that EVERY row shares the clause, not that any row carries a
    date. A date on one row of three is a discriminator and belongs there — that
    is the airport vein's own twenty-year frame, July 2026 against July 2006,
    where a masthead would be a claim about a line it does not cover. Only a
    clause true of every line could have been a masthead.

    ⚠️ Judged per language, and reported if EITHER shows it. The selector writes
    some Korean labels while Python writes the English, so the two can disagree,
    and a card tidy in one language and repeating itself in the other is a real
    fault that a both-languages test would pass.

    ⚠️ It reports and repairs nothing, and never blocks a post. Lifting the
    clause automatically was considered and rejected: four veins here
    deliberately keep a date OFF the masthead (the weather season span, the
    books window, the OECD vintage, the library-ratio population month), each
    commented with its reason, and a generic lift would overturn all four. This
    is a prompt to go and look, never a verdict.

    Measured before it was written: 1 of the 81 cards in card_history.jsonl
    matches, and it is the card above.
    """
    if grouped or dateline_en:
        return []                       # the date already flies above the rows
    found = []
    for lang, masthead in (('en', dateline_en), ('ko', dateline_ko)):
        labels = [l[f'label_{lang}'] or '' for l in lines]
        if len(labels) < 2:
            continue
        tails = {m.group(1) for m in map(_TRAILING_YEAR.search, labels) if m}
        if len(tails) == 1 and len([x for x in labels if _TRAILING_YEAR.search(x)]) \
                == len(labels):
            found.append({'lang': lang, 'tail': tails.pop(),
                          'labels': list(labels)})
    for f in found:
        log(f'  !! every {f["lang"]} label ends ", {f["tail"]}" and no dateline '
            f'flew: the date belongs on the masthead, not on every row')
    if found:
        _observe_masthead(found)
    return found


def _observe_masthead(found):
    """⚠️ Dry runs report nothing, exactly as _observe_korean refuses to: a test
    filing itself with the Sunday review is a fault invented by reporting it."""
    if not reporting():
        return
    langs = '/'.join(f['lang'] for f in found)
    text = (f'{langs} card repeated {found[0]["tail"]!r} on all '
            f'{len(found[0]["labels"])} rows with no dateline')
    try:
        subprocess.run(
            ['python3', str(OBSERVE), 'add', '--source', 'seoul-index-masthead',
             '--kind', 'finding', '--key', 'seoul-index-date-on-every-row',
             text], check=False, capture_output=True)
    except OSError:
        pass


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
    if not reporting():
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


# --- category-mixing guard --------------------------------------------------
# Added 30 August 2026, after a card mixed 'price' and 'avgbill' with no
# sanctioned reason: SELECT_PROMPT tells the selector every vein is its own
# post, with exactly two named exceptions (the LIVE_CATS+SCOPED_CATS "grouped"
# layout, and a genuine CROSS_PAIRS coincidence) — but nothing before this
# checked the model actually used one rather than just mixing veins because
# nothing stopped it. `sel` carries no field naming which mechanism (if
# either) justified a mix, so this recomputes cross_vein_pairs(pool) itself
# rather than trusting the model's say-so.
def _validate_card_categories(precats, picks, pool):
    """Raise RuntimeError if any category in `precats` has no sanctioned
    reason to share the card. Returns True when the card is a genuine
    CROSS_PAIRS mix (as opposed to the live+scoped grouped layout, which
    already separates categories under their own subheads) — the case where
    sorted-by-value lines from two different veins sit directly next to each
    other with nothing to tell them apart, so _cross_pair_hints() below has
    work to do."""
    if len(precats) <= 1:
        return False
    live, scoped = precats & LIVE_CATS, precats & SCOPED_CATS
    # Matches maybe_grouped's own test exactly (bool(precats & LIVE_CATS) and
    # bool(precats & SCOPED_CATS)): at least one live category and at least
    # one scoped category, nothing outside either set. Does NOT require
    # exactly one of each — two different scoped veins can legitimately ride
    # alongside one live vein (see test_two_scoped_veins_do_not_group: crowd
    # + library + complaint is "merely cramped, not wrong", since nothing
    # groups when more than one scope competes for the masthead, but it is
    # still a sanctioned card, not an invalid one).
    live_scoped_ok = bool(live) and bool(scoped) and precats == live | scoped
    ids = {p['id'] for p in picks}
    cross_linked = set()
    for pr in cross_vein_pairs(pool):
        if pr['a'] in ids and pr['b'] in ids:
            cross_linked.add(pr['a_cat'])
            cross_linked.add(pr['b_cat'])
    allowed = (live | scoped if live_scoped_ok else set()) | cross_linked
    bad = precats - allowed
    if bad:
        raise RuntimeError(
            f'card mixes categories {sorted(precats)} with no sanctioned reason '
            f'(no live+scoped grouping or CROSS_PAIRS coincidence links '
            f'{sorted(bad)} to the rest of the picks)')
    return bool(cross_linked) and not live_scoped_ok


# A prefix ("<descriptor>, <original label>") added ONLY on a genuine
# cross-pair card, matching the "Cheapest, a traditional market (Dongjak-gu)"
# shape the price vein already uses elsewhere. A category absent from this
# dict already carries its own metric word in its bare label (verified
# against every cross-pairable category's actual label code, 30 Aug 2026:
# property says "paid"/"deposit", tourism says "Visitors", transport/rush
# and airport say "boardings"/"Passengers", national's pop_seoul fact is a
# full sentence, and crowd's label is pinned to always read "Estimated
# crowd, <place>") and needs no hint. 'price' and 'infant' are handled
# separately below, since their descriptor is run-specific (which item,
# which age band) rather than a fixed word.
_CROSS_HINT = {
    'spending': ('Quarterly spending', '분기별 지출'),
    'avgbill': ('Average bill', '평균 결제액'),
    'boxoffice': ('Admissions', '관객수'),
    'rush': ('Boardings', '승차 인원'),
    'daynight': ('Population', '생활인구'),
    'library': ('Library members', '도서관 회원'),
}


def _cross_pair_hints(lines):
    """Give each cross-pair line its own metric, since the opener structurally
    can't (SELECT_PROMPT requires a neutral opener on a cross-pair card, and
    naming one vein's metric there would falsely frame the other's line) and
    the coincidence itself must stay unremarked (SELECT_PROMPT: "let the
    coincidence sit there unremarked... never write a line, opener or note
    that points out that the two numbers match"). This only labels what each
    figure IS — it never says the two figures are close, which is the
    reader's own discovery to make."""
    for l in lines:
        cat = l['cat']
        if cat == 'price':
            # The ranked lead/cheapest/dearest lines already say "Cheapest"/
            # "Dearest" — inherently a price word — so only the plain,
            # unranked lines need the item named.
            if 'Cheapest' in l['label_en'] or 'Dearest' in l['label_en']:
                continue
            if PRICE_LABEL['en']:
                rest = l['label_en']
                l['label_en'] = f'Price of {PRICE_LABEL["en"]}, {rest[0].lower()}{rest[1:]}'
            if PRICE_LABEL['ko'] and l['label_ko']:
                l['label_ko'] = f'{PRICE_LABEL["ko"]} 가격, {l["label_ko"]}'
        elif cat == 'infant':
            if INFANT_PERIOD['en']:
                l['label_en'] = f"{INFANT_PERIOD['en']}, {l['label_en']}"
            if INFANT_PERIOD['ko']:
                l['label_ko'] = f"{INFANT_PERIOD['ko']}, {l['label_ko']}"
        elif cat in _CROSS_HINT:
            hint_en, hint_ko = _CROSS_HINT[cat]
            l['label_en'] = f"{hint_en}, {l['label_en']}"
            l['label_ko'] = f"{hint_ko}, {l['label_ko']}"


def compose(sel, pool):
    by_id = {f['id']: f for f in pool}
    picks = [p for p in sel.get('picks', []) if p.get('id') in by_id]
    picks = complete_boxoffice(picks, pool)
    for ranked_cat in RANKED_CATS:
        picks = complete_busroutes(picks, pool, cat=ranked_cat)
    # A rush card can be ONE station's two hours: the whole point of that
    # shape is a single place's own morning/evening swing, and a third line
    # from anywhere else would reintroduce the cross-source mixing the
    # one-station design exists to avoid. Every other vein still needs 3+.
    is_rush_pair = (len(picks) == 2
                    and all(by_id[p['id']]['cat'] == 'rush' for p in picks)
                    and len({by_id[p['id']]['pair'] for p in picks}) == 1)
    # The other two-line cards (TWO_LINE_CATS less rush): one vein, both of
    # its lines, his call on 11 September 2026, and complete_busroutes() has
    # already made sure both are here.
    pick_cats = {by_id[p['id']]['cat'] for p in picks}
    is_two_line_pair = (len(picks) == 2 and len(pick_cats) == 1
                        and pick_cats <= TWO_LINE_CATS - {'rush'})
    if len(picks) < 3 and not (is_rush_pair or is_two_line_pair):
        raise RuntimeError(f'selector returned too few valid picks: {len(picks)}')
    spotlight = any(by_id[p['id']]['cat'] == 'spotlight' for p in picks)
    precats = {by_id[p['id']]['cat'] for p in picks}
    is_cross_pair = _validate_card_categories(precats, picks, pool)
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

    # A then-and-now card (weather's fifty-year pairs) repeats a long criterion
    # on every row and distinguishes the rows by a period buried at the end of
    # it — "Nights never below 25°C (77°F), 1 June–25 August 1976". At card width
    # every such label wraps, and the year, the ONE token the reader is scanning
    # for, lands alone on the second line. Worse, the value sort then interleaves
    # the pairs (23, 15, 2, 0), so neither pair is adjacent to itself.
    #
    # So the metric is drawn ONCE as a group subhead and the periods bold beneath
    # it. Requirements are deliberately strict: every line must carry the split,
    # and EVERY metric must have at least two lines. A subhead over a single row
    # is a heading over nothing, and one bare row beside grouped ones reads as an
    # orphan — in either case the card falls through to the flat layout, which is
    # still correct, just longer.
    def _head(p):
        return by_id[p['id']].get('head_en')

    def _period_year(p):
        m = re.search(r'(?:19|20)\d\d', by_id[p['id']].get('period_en') or '')
        return int(m.group()) if m else 0

    heads = [_head(p) for p in picks]
    tally = {h: heads.count(h) for h in heads}
    metric_grouped = (
        not spotlight and not maybe_grouped and len(picks) > 1
        and all(heads) and all(by_id[p['id']].get('period_en') for p in picks)
        and all(n >= 2 for n in tally.values()))

    # A cross-pair card mixing 'tourism' (one MONTH of visitors) and
    # 'boxoffice' (one DAY of admissions) puts two different spans of time on
    # one card. per_pairs further down already refuses to lift either as a
    # single masthead dateline when they disagree (correctly — neither span
    # covers the whole card), but that left both spans stranded together in
    # one footnote line with nothing to say which line either one explains:
    # https://bsky.app/profile/seoul-index.bsky.social/post/3mudkt6v5d42v
    # (30 Aug 2026) read "Admissions, The Odyssey: 92,090" directly above two
    # tourism lines with no visual break, and a footnote reading "Paid-
    # admission sites, June 2026 · Seoul screens, the day's four most-
    # watched, 30 August" some way below it — accurate, but disconnected
    # from which line either clause covers.
    #
    # So this specific pair groups like the weather then-and-now cards do:
    # each span drawn once as a subhead over the lines it covers. Worded
    # around the SPAN rather than reused from the standalone cards' own
    # footnote text, which names a count ("the day's four most-watched")
    # that stops being true once only one or two of those four are on a
    # cross-pair card. Scoped tightly to this one pair rather than every
    # DATED_PERIOD_CATS combination: the other members (property, spending,
    # avgbill, rush, airport) either share no unit with tourism/boxoffice
    # (won vs people, so cross_vein_pairs() can never link them in) or carry
    # their period a different way (airport's rides per-line, not a module
    # global) — generalising this without a second real example to test
    # against would be guessing.
    period_grouped = (
        not spotlight and not maybe_grouped and not metric_grouped
        and {'tourism', 'boxoffice'} <= precats
        and bool(TOUR_M['month_en']) and bool(BOXOFFICE_D['month_en']))
    period_subheads = {}
    if period_grouped:
        # The box office span is just its date (BOXOFFICE_D['en']/['ko']
        # already read "30 August" / "8월 30일") — a bare date reads as one
        # day on its own, with nothing more needed to say so.
        period_subheads['boxoffice'] = (BOXOFFICE_D['en'], BOXOFFICE_D['ko'])
        period_subheads['tourism'] = (
            f'The entire month of {TOUR_M["month_en"]}',
            f'{TOUR_M["month_ko"]} 한 달 전체')

    # A spotlight card is one place read along a clock — now, then the usual for
    # this hour, then the hours ahead. Sorting that by size would scramble the
    # sequence into nonsense, so it keeps the harvester's order instead.
    if spotlight or precats & ORDERED_CATS:
        order = {f['id']: i for i, f in enumerate(pool)}
        picks = sorted(picks, key=lambda p: order.get(p['id'], 0))
    elif metric_grouped:
        # Metrics in the selector's own order — it chose the arrangement — and
        # newest period first inside each, so every group reads now-then-then.
        # NOT by value: which of 1976 and 2026 is larger is the thing the card
        # is asking about, and letting it decide the order answers the question
        # in the layout before the reader has read the numbers.
        head_order = {}
        for p in picks:
            head_order.setdefault(_head(p), len(head_order))
        picks = sorted(picks,
                       key=lambda p: (head_order[_head(p)], -_period_year(p)))
    elif maybe_grouped:
        # Dated group first, live group second; each largest-first, so the two
        # near-equal values that make the cross pair land either side of the
        # divide and sit next to each other across it.
        picks = sorted(picks, key=lambda p: (
            by_id[p['id']]['cat'] in LIVE_CATS, -_val(p)))
    elif period_grouped:
        # Group by category so each subhead sits over a contiguous run of
        # lines, ordered by each category's OWN largest value so the card
        # reads the same top-to-bottom as the plain value sort would have —
        # the pair that made this a cross-pair card in the first place
        # belongs at the top either way.
        cat_top = {}
        for p in picks:
            c = by_id[p['id']]['cat']
            cat_top[c] = max(cat_top.get(c, 0.0), _val(p))
        picks = sorted(picks, key=lambda p: (
            -cat_top[by_id[p['id']]['cat']], -_val(p)))
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
                      'pin': bool(f.get('pin') or f.get('label_ko')),
                      'head_en': f.get('head_en'), 'head_ko': f.get('head_ko'),
                      'period_en': f.get('period_en'),
                      'period_ko': f.get('period_ko'),
                      'place_en': f.get('place_en'),
                      'place_ko': f.get('place_ko')})
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
    if first_fact['cat'] in ('transport',) + RANKED_CATS:
        fid = first_fact['id']
        if fid.startswith('sub') or fid.startswith('st_'):
            opener_emoji = '🚇'
        elif fid.startswith('bus'):
            opener_emoji = '🚌'
        elif fid.startswith('rail'):
            opener_emoji = '🚆'
        elif fid.startswith('wx'):
            # The day's own weather, read off the row (see wx_day_emoji).
            opener_emoji = RANKED_CARD_INFO.get('wxday', {}).get('emoji') or '🌤'
        elif fid.startswith('rescue'):
            opener_emoji = '🐾'
        elif fid.startswith('kopis'):
            opener_emoji = '🎭'
        elif fid.startswith('kepco'):
            opener_emoji = '⚡️'
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
        # ⚠️ PINNED LABELS ARE NOT EXEMPT HERE, unlike the dedupe_labels trim in
        # the else branch, and the difference is what each one removes. That trim
        # strips whatever the labels happen to share, which on a pinned label can
        # be load-bearing — taking "June" off "Hottest day, June 1976" turns a
        # month's record into a claim about the whole year. This strips exactly
        # two things, "Estimated" and a trailing "right now", and a grouped card
        # states BOTH elsewhere by construction: the live group's subhead reads
        # "Right now", and an estimated line always puts the KT caveat in the
        # footnote. So it can only ever remove a second copy.
        #
        # Pinning the crowd label on 26 August 2026 made it exempt and put the
        # duplication back: "Estimated crowd, Hongdae" under a "Right now"
        # subhead, over a footnote reading "Crowds are KT-estimated". That was
        # roughly one card in nine — 9 of the 79 in card_history are crowd
        # crossed with tourism or library.
        for l in lines:
            if l['live']:
                for lang, ko in (('en', False), ('ko', True)):
                    l[f'label_{lang}'] = _strip_live_frame(l[f'label_{lang}'], ko)
    else:
        for lang, ko in (('en', False), ('ko', True)):
            opener = opener_en if lang == 'en' else opener_ko
            trimmed = dedupe_labels([l[f'label_{lang}'] for l in lines], opener, korean=ko)
            for l, t in zip(lines, trimmed):
                if not l['pin']:
                    l[f'label_{lang}'] = t

    # Is the Korean card actually in Korean? Deterministic, so it runs whether
    # or not the model checker below does. See check_korean.
    check_korean(lines, opener_ko, opener_en)

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

    # Runs LAST, after every trim and check above, so nothing downstream
    # mistakes an added hint for an unchecked assertion or trims it back off.
    if is_cross_pair:
        _cross_pair_hints(lines)

    # Source line credits every distinct source used. Seoul Open Data covers
    # everything except the KOSIS 'national' figures, which get their own credit.
    # Categories whose figures come from a publisher other than Seoul Open
    # Data; anything outside this set is credited to data.seoul.go.kr.
    # 'books' is deliberately NOT here: since 22 August 2026 the loan counts come
    # from Seoul's own portal (SeoulLibraryBookRentNumInfo), not data4library.
    non_seoul = {'national', 'world', 'nation', 'property', 'weather', 'airport',
                 'health', 'healthcost', 'culture', 'tourism', 'level', 'boxoffice',
                 'boxhist', 'incheon', 'rail', 'railstations', 'seoulstation', 'wxday',
                 'rescue', 'kopis', 'kepco', 'kepcohist', 'kepcohouse', 'railcommuter'}
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
    uses_kma = bool({'weather', 'river', 'wxday'} & cats)
    uses_kac = 'airport' in cats
    uses_iiac = 'incheon' in cats
    uses_korail = bool({'rail', 'railstations', 'seoulstation', 'railcommuter'} & cats)
    uses_hira = 'health' in cats
    # A different HIRA dataset from uses_hira above (cost, not patient
    # counts — see hira_cost_facts()), but the same providing agency, so it
    # shares that domain credit rather than pointing at data.go.kr's
    # auto-conversion plumbing, which is not where a reader would go to
    # verify a HIRA figure.
    uses_hira_cost = 'healthcost' in cats
    uses_mcst = 'culture' in cats
    uses_tour = 'tourism' in cats
    uses_books = 'books' in cats
    uses_kobis = bool({'boxoffice', 'boxhist'} & cats)
    # The rescue register is the quarantine agency's, served through data.go.kr.
    uses_apqa = 'rescue' in cats
    uses_kopis = 'kopis' in cats
    uses_kepco = bool({'kepco', 'kepcohist', 'kepcohouse'} & cats)
    srcs = (['data.seoul.go.kr'] if uses_seoul else []) + \
           (['kosis.kr'] if uses_kosis or lib_ratio else []) + \
           ([OECD_DOMAIN] if uses_oecd else []) + \
           (['rt.molit.go.kr'] if uses_molit else []) + \
           (['data.kma.go.kr'] if uses_kma else []) + \
           (['airport.co.kr'] if uses_kac else []) + \
           (['airport.kr'] if uses_iiac else []) + \
           (['korail.com'] if uses_korail else []) + \
           (['opendata.hira.or.kr'] if uses_hira or uses_hira_cost else []) + \
           (['mcst.go.kr'] if uses_mcst else []) + \
           (['know.tour.go.kr'] if uses_tour else []) + \
           (['hrfco.go.kr'] if 'level' in cats else []) + \
           (['kobis.or.kr'] if uses_kobis else []) + \
           (['animal.go.kr'] if uses_apqa else []) + \
           (['kopis.or.kr'] if uses_kopis else []) + \
           (['bigdata.kepco.co.kr'] if uses_kepco else []) + \
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
    # The single month an airport card covers, once it is known to be single.
    # Empty when the card spans two months (the twenty-year frame) or carries no
    # airport line at all.
    kac_period = ('', '')
    # Same idea for the Incheon and Korail veins, which never span two months
    # (neither has a then/now pair), so this is empty only when the card
    # carries no line of that category at all.
    iiac_period = ('', '')
    korail_period = ('', '')
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
    if uses_apqa:
        src_en += ' · Animal and Plant Quarantine Agency'
        src_ko += ' · 농림축산검역본부'
    if uses_kopis:
        src_en += ' · KOPIS, Korea Arts Management Service'
        src_ko += ' · 공연예술통합전산망, 예술경영지원센터'
    if uses_kepco:
        src_en += ' · Korea Electric Power Corporation'
        src_ko += ' · 한국전력공사'
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
            # ⚠️ THE PERIOD SLOT IS DELIBERATELY None ON BOTH OF THESE, and that
            # is the load-bearing part. A period here is PROMOTED to the masthead
            # dateline (see the per_pairs logic below), which would fly the span
            # as a red line under the title in the same weight as the metric
            # subheads — three competing reds, with a date range the first thing
            # the eye lands on. Judged by eye against the alternative on
            # 26 August 2026 and rejected. This is a key to the figures, so it
            # stays in the muted footnote under them.
            #
            # ⚠️ It reads "Seoul's reference station, observing since 1907"
            # rather than the "Official Seoul station (108)" it was until that
            # date. "(108)" is a station index number: it names the station only
            # to someone who already knew which one it was. It is also PUBLISHED
            # PROSE now rather than a parenthetical, so the year has to be right
            # — see WX_OBSERVING_SINCE for why it is 1907 and not 1904.
            #
            # A season row says "Summer 2026" and nothing else says which days
            # that counts, so the span leads. It is added ONLY when a season line
            # is actually on the card: a card of last month's readings would
            # otherwise carry a window covering none of its figures, which is
            # worse than carrying no window at all.
            if WX_SEASON['en'] and any(
                    (l.get('period_en') or '').startswith('Summer')
                    for l in lines):
                scope_en.append((WX_SEASON['en'], None))
                scope_ko.append((WX_SEASON['ko'], None))
            scope_en.append((f'Seoul’s reference station, '
                             f'observing since {WX_OBSERVING_SINCE}', None))
            scope_ko.append((f'서울 대표 관측소, '
                             f'{WX_OBSERVING_SINCE}년 관측 개시', None))
            # ⚠️ And it is NOT also appended to src_en. See the NOTE further
            # down: anything on the card footnote is left off the source reply,
            # because the reply sits one post below the image and would repeat
            # what a reader had just read. That rule is why the KT-estimate
            # caveat is not on the reply either.
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
        # Shortened from "... at this gauge" on 31 Aug 2026: the opener
        # ("The Han at Jamsu Bridge") already scopes the whole card to one
        # gauge, so the descriptor was repeating it. The period half of this
        # pair still carries the reading time up to the masthead dateline —
        # only the wording changed, not the (descriptor, period) mechanism.
        scope_en.append(('Flood-warning tiers', LEVEL_PERIOD['en']))
        scope_ko.append(('홍수특보 기준수위', LEVEL_PERIOD['ko']))
    if 'bike' in cats:
        # ⚠️ Bikes waiting can genuinely outnumber docking points, both
        # citywide and per station (measured 31 Aug 2026: 1,078 of ~2,700
        # stations, one showing 36 against 15 racks) — not a bot error. Most
        # Ttareungi bikes now use a QR/wheel self-lock rather than clicking
        # into an individual dock, so a full rack does not stop a bike being
        # parked at that station; "docking points" counts installed rack
        # slots, "bikes waiting" counts bikes actually parked there by any
        # method. Raised by a reader on the 3muefbvgoce2v post. Korean
        # round-tripped through Naver by the user before landing.
        scope_en.append(('QR-lock bikes can be parked past a full rack', None))
        scope_ko.append(('QR 잠금 자전거는 거치대가 가득 차도 인근 주차 가능', None))
    if 'rush' in cats and RUSH_M['en']:
        # ⚠️ Without this the card says "City Hall, 6 p.m.: 347,582" under a
        # month dateline, which reads as one evening and is out by about
        # thirtyfold. The dateline says WHICH month; only this says that each
        # figure is the whole of it.
        scope_en.append(('The total monthly boardings during the designated hour',
                         RUSH_M['en']))
        scope_ko.append(('해당 시간대 승차 인원, 한 달 합계', RUSH_M['ko']))
    if 'stations' in cats and STATION_DAY['en']:
        # Same shape as busroutes directly above.
        scope_en.append((None, STATION_DAY['en']))
        scope_ko.append((None, STATION_DAY['ko']))
    for ranked_cat, info in RANKED_CARD_INFO.items():
        if ranked_cat in cats and info.get('day_en'):
            # A card may fly a worded dateline (nightbus: "Boardings on
            # 7 September") while its map keeps the bare day_en.
            scope_en.append((None, info.get('dateline_en') or info['day_en']))
            scope_ko.append((None, info.get('dateline_ko') or info['day_ko']))
    if uses_kac:
        src_en += ' · Korea Airports Corporation'
        src_ko += ' · 한국공항공사'
        # ⚠️ Read off THIS CARD's lines, never from the newest published month.
        # The twenty-year frame puts two months on one card, and a masthead is a
        # claim about every line under it: "July 2026" flown over a 2006 figure
        # is simply false. So the month lifts only when every airport line
        # agrees on it, which is the domestic/international frame — and that is
        # the frame that shipped on 27 August 2026 with July 2026 spelled out on
        # all three rows and no dateline at all, which is what this fixes.
        # No descriptor: the opener already names the airport, and a scope
        # reading "Gimpo airport" would only repeat it.
        kac_months = {(l['period_en'], l['period_ko']) for l in lines
                      if l['cat'] == 'airport'}
        if len(kac_months) == 1 and all(next(iter(kac_months))):
            kac_period = kac_months.pop()
            scope_en.append((None, kac_period[0]))
            scope_ko.append((None, kac_period[1]))
    if uses_iiac:
        src_en += ' · Incheon International Airport Corporation'
        src_ko += ' · 인천국제공항공사'
        # Same reasoning as uses_kac above: iiac_facts() never spans two
        # months, but read it off THIS CARD's lines anyway rather than
        # assume, in case a future edit adds a then/now frame here too.
        iiac_months = {(l['period_en'], l['period_ko']) for l in lines
                       if l['cat'] == 'incheon'}
        if len(iiac_months) == 1 and all(next(iter(iiac_months))):
            iiac_period = iiac_months.pop()
            scope_en.append((None, iiac_period[0]))
            scope_ko.append((None, iiac_period[1]))
    if uses_korail:
        src_en += ' · Korea Railroad Corporation'
        src_ko += ' · 한국철도공사'
        korail_months = {(l['period_en'], l['period_ko']) for l in lines
                         if l['cat'] == 'rail'}
        if len(korail_months) == 1 and all(next(iter(korail_months))):
            korail_period = korail_months.pop()
            scope_en.append((None, korail_period[0]))
            scope_ko.append((None, korail_period[1]))
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
    if uses_hira_cost and not uses_hira:
        # Same two provisos as uses_hira, and the agency credit already rides
        # the shared opendata.hira.or.kr entry above — HIRA is only named
        # again here if the sibling vein isn't also on the card.
        src_en += ' · HIRA'
        src_ko += ' · 건강보험심사평가원'
    if uses_hira_cost and HEALTH_COST_Y['y']:
        scope_en.append((f'Insured treatment cost at Seoul institutions, '
                         f'{HEALTH_COST_Y["y"]}', None))
        scope_ko.append((f'서울 소재 요양기관 건강보험 진료비, '
                         f'{HEALTH_COST_Y["y"]}년', None))
    if uses_mcst:
        src_en += ' · MCST'
        src_ko += ' · 문화체육관광부'
        if CULTURE_Y['y']:
            scope_en.append((f'Culture-facility survey, {CULTURE_Y["y"]} figures', None))
            scope_ko.append((f'문화기반시설총람, {CULTURE_Y["y"]}년 기준', None))
    # Captured so period_grouped (below) can strip exactly the entries it
    # promotes to a subhead, without reconstructing the same f-strings a
    # second time and risking the two copies drifting apart.
    tour_scope_pair = boxoffice_scope_pair = None
    if uses_tour:
        # Paid-admission scope and the (months-old) data month are keys to
        # the figures; both ride the card.
        src_en += ' · KCTI'
        src_ko += ' · 한국문화관광연구원'
        if TOUR_M['en']:
            tour_scope_pair = (('Paid-admission sites', TOUR_M['en']),
                               ('유료 관광지 입장객', TOUR_M['ko']))
            scope_en.append(tour_scope_pair[0])
            scope_ko.append(tour_scope_pair[1])
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
        elif BOXOFFICE_D['en'] and cats == {'boxoffice'}:
            # The card is the day's top four, in order, always -- but that is
            # only true of an OWN-VEIN card (complete_boxoffice's own test for
            # "top up to four" is this same cats == {'boxoffice'}, and it is
            # deliberately skipped for a cross-pair: see its docstring). It
            # said "of the day's five most-watched" for an hour on 23 Aug 2026,
            # while the selector was free to choose four of five: that was
            # honest about a card with a hole in its ranking, but a hole in a
            # ranking is a worse card than a fixed one. What the vein lost is
            # variety between days, which the chart itself supplies as it moves.
            # Spelled out, as the card spells out months: "the day's 5
            # most-watched" is prose, not a figure, and the only numerals on
            # this card should be the ones being reported.
            boxoffice_scope_pair = (
                (f'Seoul screens, the day’s '
                 f'{SMALL_NUMBERS_EN.get(BOXOFFICE_N, BOXOFFICE_N)} '
                 f'most-watched', BOXOFFICE_D['en']),
                # 편 is a counter and takes a space after a spelled-out
                # numeral: 다섯 편, not 다섯편, which the first render produced.
                (f'서울 지역 상영, 그날 관객수 상위 '
                 f'{SMALL_NUMBERS_KO.get(BOXOFFICE_N, BOXOFFICE_N)} 편',
                 BOXOFFICE_D['ko']))
            scope_en.append(boxoffice_scope_pair[0])
            scope_ko.append(boxoffice_scope_pair[1])
        elif BOXOFFICE_D['en']:
            # A cross-pair: boxoffice contributed only one or two lines, not
            # the vein's own complete four (complete_boxoffice deliberately
            # does not top these up -- "completing the chart there would
            # wreck the pairing"). A count claim here repeats exactly the bug
            # the own-vein wording above was written to fix: verified live on
            # the 31 Aug 2026 tourism+boxoffice post, whose alt text read "the
            # day's four most-watched" over a card carrying a single film
            # line. State the scope and the date; the card itself already
            # shows the reader how many films are on it.
            boxoffice_scope_pair = (
                ('Seoul screens', BOXOFFICE_D['en']),
                ('서울 지역 상영', BOXOFFICE_D['ko']))
            scope_en.append(boxoffice_scope_pair[0])
            scope_ko.append(boxoffice_scope_pair[1])
    if period_grouped:
        # Both entries just appended are about to head their own group as a
        # subhead (see period_subheads / _items below) instead of sitting
        # inline in the footnote — drop them here so the card does not say
        # either span twice.
        if tour_scope_pair:
            scope_en = [e for e in scope_en if e != tour_scope_pair[0]]
            scope_ko = [e for e in scope_ko if e != tour_scope_pair[1]]
        if boxoffice_scope_pair:
            scope_en = [e for e in scope_en if e != boxoffice_scope_pair[0]]
            scope_ko = [e for e in scope_ko if e != boxoffice_scope_pair[1]]
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

    # ⚠️ The boxhist card carries no dateline of its own (each row is a
    # different year and names it), which leaves the masthead sitting empty
    # while the credit rides a full extra reply post underneath. Flying the
    # credit there instead was chosen 28 August 2026 after a reader asked for
    # it on https://bsky.app/profile/seoul-index.bsky.social/post/3mu5baibvie24.
    # Gated on the card being ENTIRELY the boxhist vein (never true today —
    # boxhist never shares a post with boxoffice or anything else — but this
    # is what stops a future cross-category boxhist card from losing its real
    # dateline, or another publisher's credit, to a masthead that only
    # explains part of the card). `credit_on_card` also drops the credit from
    # the trailing source line (see _body below) and from the threaded source
    # reply (see main()), so the two posts don't say the same thing twice.
    credit_on_card = (cats == {'boxhist'})
    if credit_on_card:
        dateline_en, dateline_ko = src_en, src_ko

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
    # ⚠️ Stripped only once the month has ACTUALLY been promoted, never on the
    # strength of being liftable: a second dated vein crossing this one leaves
    # both periods inline in the footnote (see per_pairs above), and a label
    # that had already given its month away would then sit on a card that says
    # nowhere which month it covers. removesuffix, so a label that does not end
    # in the promoted month is left exactly as it is.
    if kac_period[0] and (group_en or dateline_en) == kac_period[0]:
        for l in lines:
            if l['cat'] == 'airport':
                l['label_en'] = l['label_en'].removesuffix(f', {kac_period[0]}')
                l['label_ko'] = l['label_ko'].removesuffix(f', {kac_period[1]}')
    if iiac_period[0] and (group_en or dateline_en) == iiac_period[0]:
        for l in lines:
            if l['cat'] == 'incheon':
                l['label_en'] = l['label_en'].removesuffix(f', {iiac_period[0]}')
                l['label_ko'] = l['label_ko'].removesuffix(f', {iiac_period[1]}')
    if korail_period[0] and (group_en or dateline_en) == korail_period[0]:
        for l in lines:
            if l['cat'] == 'rail':
                l['label_en'] = l['label_en'].removesuffix(f', {korail_period[0]}')
                l['label_ko'] = l['label_ko'].removesuffix(f', {korail_period[1]}')
    # Is a date sitting on every row and nowhere above them? Asked HERE, after
    # the strip above and after grouped/dateline settled, so it sees the card as
    # it will actually be drawn rather than a draft of it — the same reason
    # check_labels runs last. Deterministic, no network, never blocks a post.
    # period_grouped counts as "already flies above the rows" too, exactly
    # like grouped: its two spans sit as subheads, not a masthead, but the
    # question check_masthead asks (is a date stuck on every row with nothing
    # above them?) is already answered either way.
    check_masthead(lines, dateline_en, dateline_ko, grouped or period_grouped)

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
    elif 'stations' in cats:
        # What the ranking counts (see STATION_DAY's block): summed across a
        # station's lines, Seoul only.
        note_en, note_ko = STATION_CAVEAT_EN, STATION_CAVEAT_KO
    elif any(rc in cats and rc in RANKED_CARD_INFO for rc in RANKED_CATS):
        info = next(RANKED_CARD_INFO[rc] for rc in RANKED_CATS
                    if rc in cats and rc in RANKED_CARD_INFO)
        note_en, note_ko = info['note_en'], info['note_ko']
    elif cats == {'air'}:
        # The scale the PM figures sit on, his call, 11 September 2026:
        # a bare µg/m³ says nothing to a reader without it. AirKorea's own
        # index bands (khaiInfo, read 11 September 2026): PM2.5 좋음 0-15,
        # 나쁨 from 36; PM10 좋음 0-30, 나쁨 from 81. Only on an all-air card;
        # an air line riding on a crowd card keeps the crowd note.
        note_en, note_ko = AIR_SCALE_NOTE_EN, AIR_SCALE_NOTE_KO
    else:
        note_en = 'Crowds are KT-estimated' if estimated else ''
        note_ko = '인구는 KT 추정' if estimated else ''
    # ⚠️ Appended to note_en directly, never to scope_en: scope_en/scope_ko are
    # read positionally by the zip() at per_pairs above, so an English-only
    # scope entry with no Korean twin would shift every later scope_ko entry
    # out of alignment with the scope_en it is meant to pair with. And it has
    # no Korean twin on purpose — jeonse has no Western equivalent, the same
    # problem "The Anyangcheon" had before "tributaries" fixed it, but a
    # Korean reader needs no gloss for a term they already know (the imperial-
    # conversion reasoning, not the KT-estimate one).
    if any(p['id'] in JEONSE_IDS for p in picks):
        note_en = ' · '.join(p for p in [note_en, JEONSE_NOTE_EN] if p)
    # Same not-to-scope_en reasoning as jeonse above, and the same English-only
    # rule: a won figure needs no dollar anchor for a reader who already
    # thinks in won. One rate note covers every won_en() value on the card,
    # however many are picked, without adding anything to any single line's
    # width (see WON_CATS / USD_RATE for why a per-value suffix was rejected).
    # Silently absent when refresh_usd_rate() could not get a rate at all —
    # an absent footnote is honest, a wrong or ancient one is not.
    if (cats & WON_CATS) and USD_RATE.get('rate'):
        per_usd = round(1 / USD_RATE['rate'])
        note_en = ' · '.join(p for p in [note_en, f'$1 ≈ ₩{per_usd:,}']
                             if p)
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
    # post. Spotlights set a single (prefix, anchor, url) tuple; normalize to a
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

    # --- bolding the variable ---------------------------------------------
    # Same rule as the then-and-now subheads one level up: bold what CHANGES
    # between the rows, and leave what they share alone. There the metric was
    # constant and the period varied, so the period bolded; on a card whose rows
    # are one metric read at four places, the place is the variable and it bolds
    # while "Estimated crowd," stays regular.
    #
    # ⚠️ It is decided FROM THE LABELS, not from a list of veins. Take each
    # label, cut the place out of it, and bold only if every remainder is
    # identical: that is literally the test "is the place the one thing that
    # differs". A vein list would have to be maintained against wordings it
    # cannot see, and would be wrong per CARD rather than per vein — a tourism
    # card of four "Visitors to X" qualifies while one mixing in "Busiest subway
    # station, Gangnam" does not, and both are the same vein.
    #
    # ⚠️ THE REMAINDER MUST BE NON-EMPTY, and that guard is the whole reason
    # bolding means anything. The river and water veins label their rows with
    # BARE NAMES, so cutting the place leaves nothing and every label is
    # trivially "identical" — the test passes and the card comes out with every
    # row in bold, which is the same as no row in bold, only heavier. Those
    # cards say what varies by having nothing else on the line.
    #
    # ⚠️ Judged per language. The English labels can agree while the Korean ones
    # do not (the selector writes those), and a card half-bolded in one language
    # is worse than a card bolded in neither.
    def _emph(lang):
        places = [l.get(f'place_{lang}') for l in lines]
        if not all(places):
            return
        rest = {l[f'label_{lang}'].replace(pl, '', 1).strip()
                for l, pl in zip(lines, places)
                if pl in l[f'label_{lang}']}
        if len(rest) != 1 or not next(iter(rest)):
            return
        if len(places) != len([1 for l, pl in zip(lines, places)
                               if pl in l[f'label_{lang}']]):
            return
        for l, pl in zip(lines, places):
            l[f'emph_{lang}'] = pl

    _emph('en')
    _emph('ko')

    # rush bolds the station name UNCONDITIONALLY, overriding whatever _emph()
    # above decided. That heuristic bolds what VARIES between rows and leaves
    # what they share alone — right for a card naming four different places at
    # one hour, backwards for rush, where the station is the one constant and
    # the hour is what changes. User's call, 1 Sept 2026.
    for l in lines:
        if l['cat'] == 'rush' and l.get('place_en'):
            l['emph_en'], l['emph_ko'] = l['place_en'], l['place_ko']

    # busroutes bolds its RANK word ("Busiest", "Quietest") the same
    # unconditional way rush bolds its station — for the identical reason.
    # _emph()'s heuristic wants a run shared by every OTHER row and varying
    # in just one; here every row's rank word AND route number both vary, so
    # the "rest of the label must match" check never passes and nothing
    # would bold at all. The total line carries no place_en and is handled
    # below by bolding the whole label instead, not a run inside it.
    for l in lines:
        if l['cat'] not in RANKED_CATS:
            continue
        if l.get('place_en'):
            l['emph_en'], l['emph_ko'] = l['place_en'], l['place_ko']
        else:
            # 'lines' entries carry no fact id to key off (see the
            # lines.append() dict above), so "no place_en" is what
            # identifies the total line among the four — it is the only
            # busroutes fact built without one.
            l['bold'] = True

    # No per-line emoji on a busroutes line: three routes and a total read as
    # a small table, and a bus glyph repeated four times in a row added
    # nothing four times over. The opener keeps one, set below. Scoped to
    # busroutes lines only — a cross-pair card can legitimately put one
    # busroutes line beside an emoji-carrying line from another vein, and
    # even_out_emoji() below already handles within-vein consistency on its
    # own; this only needs to zero out busroutes specifically.
    # ⚠️ Except a ranked card whose registry entry names an emoji per line
    # (wxday, his call, 11 September 2026: high, low and rain are
    # different things, unlike four routes), keyed on the pinned
    # English label, so the selector's choice never reaches these lines.
    for l in lines:
        if l['cat'] in RANKED_CATS:
            per_line = RANKED_CARD_INFO.get(l['cat'], {}).get('line_emoji') or {}
            l['emoji'] = _valid_emoji(per_line.get(l['label_en']))

    # The ordered elements the card draws, per language. A grouped cross pair puts
    # a date subhead over the dated lines and a "Right now" subhead over the live
    # ones; otherwise just the rows (an ungrouped dated card flies its month as a
    # masthead dateline, passed separately). The same list feeds the alt-text twin
    # below, so card and alt text never drift.
    def _items(lang):
        rows = [{'emoji': l['emoji'], 'label': l[f'label_{lang}'],
                 'value': l[f'value_{lang}'],
                 **({'emph': l[f'emph_{lang}']} if l.get(f'emph_{lang}') else {}),
                 **({'bold': True} if l.get('bold') else {})}
                for l in lines]
        if metric_grouped:
            # Metric as the subhead, period as the row, bolded because under the
            # subhead the period IS the whole distinction. The rows are already
            # ordered metric-by-metric (see the sort above), so a change of head
            # is where a new group starts.
            out, last = [], None
            for r, l in zip(rows, lines):
                head = l[f'head_{lang}']
                if head != last:
                    out.append({'subhead': head})
                    last = head
                out.append({**r, 'label': l[f'period_{lang}'], 'bold': True})
            return out
        if period_grouped:
            # One subhead per category (tourism's month, boxoffice's day),
            # over the contiguous run of that category's lines — the sort
            # above already put same-category lines together. Rows keep
            # their usual weight (unlike metric_grouped's bolded periods):
            # the distinction here is which SPAN a line belongs to, not a
            # value on the row itself, so the subhead alone carries it.
            out, last_cat = [], None
            for r, l in zip(rows, lines):
                if l['cat'] != last_cat:
                    out.append({'subhead': period_subheads[l['cat']][
                        0 if lang == 'en' else 1]})
                    last_cat = l['cat']
                out.append(r)
            return out
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
        # src is blanked (never just short) for a credit_on_card card, so the
        # trailing line is dropped rather than left as a bare '\n': every other
        # card still gets its usual '\nSource: ...' tail unchanged.
        src_tail = f'\n{src}{tail}' if (src or tail) else ''
        return curly(op + ':\n' + (f'{masthead}\n' if masthead else '')
                     + '\n'.join(parts)
                     + (f'\n{note}' if note else '') + src_tail)
    # credit_on_card: the masthead line above IS src_en/src_ko now, so passing
    # it again here would print the credit twice in the same plaintext body
    # (once as the masthead, once as its usual trailing line). source_reply()
    # still hyperlinks kobis.or.kr wherever it sits in the body, so the domain
    # stays clickable in the plaintext-fallback path even with src blanked here.
    en_body = _body(op_en, items_en, note_en, '' if credit_on_card else src_en,
                    tail_en, '' if grouped else dateline_en)
    ko_body = _body(op_ko, items_ko, note_ko, '' if credit_on_card else src_ko,
                    tail_ko, '' if grouped else dateline_ko)

    return {
        'opener': {'emoji': opener_emoji, 'en': opener_en, 'ko': opener_ko},
        'lines': lines, 'items_en': items_en, 'items_ko': items_ko,
        'grouped': grouped, 'period_grouped': period_grouped,
        'src_en': src_en, 'src_ko': src_ko,
        'credit_on_card': credit_on_card,
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
                ('airport.kr', 'https://www.airport.kr'),
                ('korail.com', 'https://www.korail.com'),
                ('opendata.hira.or.kr', 'https://opendata.hira.or.kr'),
                ('mcst.go.kr', 'https://www.mcst.go.kr'),
                ('know.tour.go.kr', 'https://know.tour.go.kr'),
                ('hrfco.go.kr', 'https://www.hrfco.go.kr'),
                ('kobis.or.kr', 'https://www.kobis.or.kr'),
                ('animal.go.kr', 'https://www.animal.go.kr'),
                ('kopis.or.kr', 'https://www.kopis.or.kr'),
                ('bigdata.kepco.co.kr', 'https://bigdata.kepco.co.kr'),
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
    dateline = ('' if (c.get('grouped') or c.get('period_grouped'))
               else c.get(f'dateline_{lang}', ''))
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
            # ⚠️ The masthead as the card ACTUALLY FLEW IT, and `grouped` beside
            # it so a reader can tell the two empty cases apart: a grouped card
            # carries its date on a group subhead and correctly has no masthead,
            # while an ungrouped one with no dateline has the date nowhere above
            # the rows. Added 27 August 2026, because until then this log
            # recorded the rows and not the line above them — so the Gimpo card
            # that repeated "July 2026" three times and flew nothing read here
            # exactly like a card that had flown it properly, and no audit of
            # this file could ever have found it.
            'dateline': '' if c.get('grouped') else c.get('dateline_en', ''),
            'grouped': bool(c.get('grouped')),
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
        # Printed where it sits on the card: under the title, above the rows.
        # Entries written before 27 August 2026 carry no 'dateline' key at all,
        # so they print nothing rather than an confident empty masthead.
        if r.get('dateline'):
            print(f'  {r["dateline"]}')
        elif r.get('grouped'):
            print('  (grouped: the date rides a subhead)')
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
    kopis_key = config.get('kopis_key')
    kepco_key = config.get('kepco_key')
    state = json.loads(STATE.read_text()) if STATE.exists() else {}

    # Inspect the cross-vein collisions the detector finds for the live pool,
    # then exit — no selector call, no post. A deterministic look at what the
    # selector will be offered (unlike a --dry-run, whose picks the model makes).
    if SHOW_CROSS:
        pool = build_pool(api_key, state, kosis_key, gov_key, hrfco_key,
                          kobis_key, kopis_key, kepco_key)
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
                          kobis_key, kopis_key, kepco_key)
        if len(pool) < 5:
            sys.exit(f'Pool too small ({len(pool)} facts) — data sources may be down.')

        # Vein cooldowns (see WORLD_COOLDOWN_DAYS, SPENDING_COOLDOWN_DAYS, the
        # bike/traffic/transport trio added 31 Aug 2026, and tourism added
        # 9 Sep 2026 for the same frozen-pair reason as spending). Applied
        # before the rotation below, so that a post held back here is dropped
        # from the running rather than merely deferred to the next post.
        pool = apply_cooldown(pool, state, 'last_world_at', 'world',
                              WORLD_COOLDOWN_DAYS, 'World')
        pool = apply_cooldown(pool, state, 'last_spending_at', 'spending',
                              SPENDING_COOLDOWN_DAYS, 'Spending')
        pool = apply_cooldown(pool, state, 'last_bike_at', 'bike',
                              BIKE_COOLDOWN_DAYS, 'Bike')
        pool = apply_cooldown(pool, state, 'last_traffic_at', 'traffic',
                              TRAFFIC_COOLDOWN_DAYS, 'Traffic')
        pool = apply_cooldown(pool, state, 'last_transport_at', 'transport',
                              TRANSPORT_COOLDOWN_DAYS, 'Transport')
        pool = apply_cooldown(pool, state, 'last_national_at', 'national',
                              NATIONAL_COOLDOWN_DAYS, 'National')
        pool = apply_cooldown(pool, state, 'last_tourism_at', 'tourism',
                              TOURISM_COOLDOWN_DAYS, 'Tourism')
        pool = apply_cooldown(pool, state, 'last_infra_at', 'infra',
                              INFRA_COOLDOWN_DAYS, 'Infrastructure')
        pool = apply_cooldown(pool, state, 'last_busroutes_at', 'busroutes',
                              BUSROUTES_COOLDOWN_DAYS, 'Bus routes')
        pool = apply_cooldown(pool, state, 'last_stations_at', 'stations',
                              STATIONS_COOLDOWN_DAYS, 'Stations')
        pool = apply_cooldown(pool, state, 'last_nightbus_at', 'nightbus',
                              NIGHTBUS_COOLDOWN_DAYS, 'Night bus')
        pool = apply_cooldown(pool, state, 'last_busweekend_at', 'busweekend',
                              BUSWEEKEND_COOLDOWN_DAYS, 'Weekend swing')
        pool = apply_cooldown(pool, state, 'last_busstops_at', 'busstops',
                              BUSSTOPS_COOLDOWN_DAYS, 'Bus stops')
        pool = apply_cooldown(pool, state, 'last_railstations_at', 'railstations',
                              RAILSTATIONS_COOLDOWN_DAYS, 'Rail stations')
        pool = apply_cooldown(pool, state, 'last_seoulstation_at', 'seoulstation',
                              SEOULSTATION_COOLDOWN_DAYS, 'Seoul Station')
        pool = apply_cooldown(pool, state, 'last_stationgap_at', 'stationgap',
                              STATIONGAP_COOLDOWN_DAYS, 'Station gap')
        pool = apply_cooldown(pool, state, 'last_wxday_at', 'wxday',
                              WXDAY_COOLDOWN_DAYS, 'Weather day')
        pool = apply_cooldown(pool, state, 'last_rescue_at', 'rescue',
                              RESCUE_COOLDOWN_DAYS, 'Rescued animals')
        pool = apply_cooldown(pool, state, 'last_kopis_at', 'kopis',
                              KOPIS_COOLDOWN_DAYS, 'Performances')
        pool = apply_cooldown(pool, state, 'last_kepco_at', 'kepco',
                              KEPCO_COOLDOWN_DAYS, 'Electricity')
        pool = apply_cooldown(pool, state, 'last_kepcohist_at', 'kepcohist',
                              KEPCO_COOLDOWN_DAYS, 'Electricity then-and-now')
        pool = apply_cooldown(pool, state, 'last_kepcohouse_at', 'kepcohouse',
                              KEPCO_HOUSE_COOLDOWN_DAYS, 'Household electricity')
        pool = apply_cooldown(pool, state, 'last_railcommuter_at', 'railcommuter',
                              RAILCOMMUTER_COOLDOWN_DAYS, 'Commuter rail')
        pool = apply_holds(pool)

        # The floor under the veins the selector never reaches for. Applied
        # after the cooldowns so a promoted vein is never one the cooldown has
        # just withheld, and instead of the rotation below rather than before
        # it: the promoted vein IS the card, so there is nothing to rotate away
        # from (and a promoted vein cannot be last_cat anyway — it has not
        # posted for STARVE_DAYS).
        if ONLY_CAT:
            only = [f for f in pool if f['cat'] == ONLY_CAT]
            need = 2 if ONLY_CAT in TWO_LINE_CATS else 3
            if len(only) < need:
                if DAILY:
                    # The vein withheld its card and printed why above (a
                    # ranking it could not make, an English form it could
                    # not find). Unattended, that is a quiet day, not a
                    # failed job.
                    print(f'--daily={ONLY_CAT}: {len(only)} fact(s) in that vein, '
                          f'need {need}; the vein withheld its card. No post this run.')
                    sys.exit(0)
                sys.exit(f'--only={ONLY_CAT}: {len(only)} fact(s) in that vein, '
                         f'need at least {need} to build a card. Pool has: '
                         f'{", ".join(sorted({f["cat"] for f in pool}))}.')
            if DAILY:
                dup, why = daily_already_posted(ONLY_CAT, state)
                if dup:
                    print(f'--daily={why}. No post this run.')
                    sys.exit(0)
                if why:
                    print(f'--daily={ONLY_CAT}: {why}.')
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

    # rush's opener is fixed rather than modeled: with exactly one pair ever
    # possible, there is nothing left for the selector to choose beyond that
    # pair itself, so the wording is Python's the same way the two clock-time
    # labels already are (see rush_facts()). User's own wording, 1 Sept 2026.
    by_cat = {f['id']: f['cat'] for f in pool}
    if sel.get('picks') and all(by_cat.get(p.get('id')) == 'rush' for p in sel['picks']):
        sel['opener_en'], sel['opener_ko'] = 'Boarding the subway', '지하철 승차'
        sel['opener_emoji'] = '🚇'
    # An all-air card likewise, his wording, 11 September 2026: the vein's
    # own four lines (see air_facts) under the selector's "Seoul, right now"
    # said nothing about air until the lines themselves did. Only when
    # every pick is air: an air line riding on a crowd card keeps the
    # selector's opener, since the card is then about more than air.
    for cat, (op_en, op_ko) in FIXED_OPENERS.items():
        if sel.get('picks') and all(by_cat.get(p.get('id')) == cat for p in sel['picks']):
            sel['opener_en'], sel['opener_ko'] = op_en, op_ko
            if cat == 'air' and AIR_NOW['emoji']:
                sel['opener_emoji'] = AIR_NOW['emoji']
    # A ranked card whose registry entry carries an opener (busmovers, since
    # 11 September 2026) is the same arrangement: Python's words, not the
    # selector's. The weekday in it changes with the day.
    for rc, info in RANKED_CARD_INFO.items():
        if (info.get('opener_en') and sel.get('picks')
                and all(by_cat.get(p.get('id')) == rc for p in sel['picks'])):
            sel['opener_en'], sel['opener_ko'] = info['opener_en'], info['opener_ko']

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
    if c.get('credit_on_card'):
        print('\n(credit rides the card masthead — no source reply will be posted)')
    else:
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

        if c.get('credit_on_card'):
            # 2-post chain: EN card → KO card. The credit already flies on the
            # card itself (see `credit_on_card` in compose()), so the source
            # reply this thread would otherwise carry is dropped rather than
            # repeating what the card just said one post above it — the same
            # reasoning the KT-estimate caveat and the crowd note already
            # follow for the footnote.
            p1 = bsky.send_image(text=tag_line(), image=en_bytes, image_alt=en_alt,
                                 langs=['en'], image_aspect_ratio=en_ar)
            posted_uri = p1.uri
            root_ref = models.create_strong_ref(p1)
            bsky.send_image(text=tag_line(), image=ko_bytes, image_alt=ko_alt,
                            reply_to=_reply(root_ref, root_ref), langs=['ko'],
                            image_aspect_ratio=ko_ar)
            print('\nPosted (2-post thread: EN card, KO card — credit rides the card).')
        else:
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
            p4 = bsky.send_post(text=ko_source, reply_to=_reply(p3_ref, root_ref), langs=['ko'])
            print('\nPosted (4-post thread: EN card, EN source, KO card, KO source).')
            if primary == 'stations' and STATION_MAP_INFO['day']:
                # The station card's fifth post. Its own broad except, on
                # purpose: the four-post thread above is already public and
                # the state write (last_cat, cooldown stamps, recent_ids)
                # happens AFTER this block, so anything escaping here — a
                # TypeError from a malformed API envelope, an OSError on the
                # temp file, an atproto error at send time — would abort the
                # run with the post out and the state unsaved, and the next
                # run could repost the same card. A lost map is a lost reply;
                # a lost state write is a duplicate thread. The generic block
                # below (busroutes and the history cards) follows the same
                # contract for the same reason.
                try:
                    labels = STATION_MAP_INFO['stations']
                    pins = [(labels[0][0], RED, (labels[0][1], labels[0][2])),
                            (labels[1][0], '#e08a1e', (labels[1][1], labels[1][2])),
                            (labels[2][0], '#000000', (labels[2][1], labels[2][2]))]
                    map_path = Path(tempfile.mkdtemp()) / 'station_map.png'
                    _, map_size = render_station_map(
                        pins, seoul_bus_stop_coords(api_key), map_path,
                        title=STATION_DAY['en'],
                        caption='Boardings summed across each station’s lines; stations inside Seoul')
                    names = [l[0].split(': ', 1)[-1] for l in labels]
                    map_alt = (
                        f'Map of three Seoul subway stations on {STATION_DAY["en"]}: '
                        f'busiest ({names[0]}), second-busiest ({names[1]}) and quietest '
                        f'({names[2]}), each marked and named over a faint backdrop of every '
                        f'Seoul bus stop. Boardings are summed across each station’s lines; '
                        f'stations inside Seoul only.')
                    map_ar = models.AppBskyEmbedDefs.AspectRatio(
                        width=map_size[0], height=map_size[1])
                    bsky.send_image(text='', image=map_path.read_bytes(),
                                    image_alt=map_alt, langs=['en'],
                                    reply_to=_reply(models.create_strong_ref(p4), root_ref),
                                    image_aspect_ratio=map_ar)
                    print('Posted a 5th reply: the station map.')
                except Exception as e:  # noqa: BLE001 — deliberately broad, see above
                    print(f'\nStation map failed ({type(e).__name__}: {e}); '
                          f'thread already posted without it.')
            if primary in RANKED_CARD_INFO and (RANKED_CARD_INFO[primary].get('map_routes')
                                                 or RANKED_CARD_INFO[primary].get('map_pins')):
                # The history cards' fifth post: the same route map as
                # busroutes, drawn for whichever routes the card named; or,
                # for the bus stops card, three pinned stops through the
                # stations map's renderer. Same broad-except contract, same
                # reason.
                try:
                    info = RANKED_CARD_INFO[primary]
                    map_path = Path(tempfile.mkdtemp()) / f'{primary}_map.png'
                    if info.get('map_pins'):
                        pins = [(label, colour, tuple(xy)) for label, colour, xy in info['map_pins']]
                        _, map_size = render_station_map(
                            pins, seoul_bus_stop_coords(api_key), map_path,
                            title=info.get('map_title') or info['day_en'],
                            caption=info['map_caption'])
                        map_alt = info['map_alt']
                    else:
                        nos = [no for _, _, no in info['map_routes']]
                        route_stops, seoul_stops, served = bus_route_map_stops(
                            api_key, info['map_day'], nos, with_served=True)
                        routes = [(label, colour, route_stops.get(no, []),
                                   (served.get(no) or {}).get('points', []))
                                  for label, colour, no in info['map_routes']]
                        map_caption, map_alt = route_map_words(info, len(seoul_stops), served)
                        _, map_size = render_bus_route_map(
                            routes, seoul_stops, map_path, title=info['day_en'],
                            caption=map_caption)
                    map_ar = models.AppBskyEmbedDefs.AspectRatio(
                        width=map_size[0], height=map_size[1])
                    bsky.send_image(text='', image=map_path.read_bytes(),
                                    image_alt=map_alt, langs=['en'],
                                    reply_to=_reply(models.create_strong_ref(p4), root_ref),
                                    image_aspect_ratio=map_ar)
                    print(f'Posted a 5th reply: the {primary} map.')
                except Exception as e:  # noqa: BLE001 — deliberately broad, see the station map
                    print(f'\n{primary} map failed ({type(e).__name__}: {e}); '
                          f'thread already posted without it.')
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
    stamp_daily_day(state, primary)
    if promoted:
        state['last_promoted_cat'] = promoted
    if primary == 'world':
        state['last_world_at'] = state['last_success_at']
    if primary == 'spending':
        state['last_spending_at'] = state['last_success_at']
    if primary == 'nation':
        state['last_nation_at'] = state['last_success_at']
    if primary == 'bike':
        state['last_bike_at'] = state['last_success_at']
    if primary == 'traffic':
        state['last_traffic_at'] = state['last_success_at']
    if primary == 'transport':
        state['last_transport_at'] = state['last_success_at']
    if primary == 'national':
        state['last_national_at'] = state['last_success_at']
    if primary == 'tourism':
        state['last_tourism_at'] = state['last_success_at']
    if primary == 'infra':
        state['last_infra_at'] = state['last_success_at']
    if primary == 'busroutes':
        state['last_busroutes_at'] = state['last_success_at']
    if primary == 'stations':
        state['last_stations_at'] = state['last_success_at']
    if primary == 'nightbus':
        state['last_nightbus_at'] = state['last_success_at']
    if primary == 'busweekend':
        state['last_busweekend_at'] = state['last_success_at']
    if primary == 'busstops':
        state['last_busstops_at'] = state['last_success_at']
    if primary == 'railstations':
        state['last_railstations_at'] = state['last_success_at']
    if primary == 'seoulstation':
        state['last_seoulstation_at'] = state['last_success_at']
    if primary == 'stationgap':
        state['last_stationgap_at'] = state['last_success_at']
    if primary == 'wxday':
        state['last_wxday_at'] = state['last_success_at']
    if primary == 'rescue':
        state['last_rescue_at'] = state['last_success_at']
    if primary == 'kopis':
        state['last_kopis_at'] = state['last_success_at']
    if primary == 'kepco':
        state['last_kepco_at'] = state['last_success_at']
    if primary == 'kepcohist':
        state['last_kepcohist_at'] = state['last_success_at']
    if primary == 'kepcohouse':
        state['last_kepcohouse_at'] = state['last_success_at']
    if primary == 'railcommuter':
        state['last_railcommuter_at'] = state['last_success_at']
    write_json_atomic(STATE, state, ensure_ascii=False, indent=2)

    log_card(c, sel, primary, posted_uri, handle, fallback=cards is None)


if __name__ == '__main__':
    # Gated on __name__, not installed at module level — this file is
    # imported by test suites, and mutating subprocess.run at import time
    # would leak into every other test sharing the process. Same shape as
    # the reporting() guard above: a --dry-run flag reads sys.argv, but a
    # test process's argv says nothing about it, so import-time is the
    # only safe gate. See api_call_log.py's own docstring.
    import api_call_log
    api_call_log.install('seoul_index_post.py')
    main()
