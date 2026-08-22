#!/usr/bin/env python3
"""
Post the Seoul Index methodology / "about" thread as prose cards, then pin it.

This is STATIC content, not part of the daily automation (no launchd). Run it by
hand — in particular as the first thing after a fresh-start wipe, so the pinned
thread carries the new card look. Posting it stands up a 7-post thread:

  1. EN "About this account" card   (image, no caption)
  2. EN "About the crowd figures" card
  3. EN "About the comparisons" card
  4. KO "이 계정에 대하여" card
  5. KO "인구 수치에 대하여" card
  6. KO "비교 수치에 대하여" card
  7. a short reply with clickable source links

Each card's full text is its alt text. The link stays clickable because it lives
in the trailing text reply, not the image (Bluesky renders post text above the
image, so a caption can't sit under the card — same reason the index posts use a
trailing source reply).

Usage:
  python3 seoul_index_methodology.py --dry-run   # render cards to cwd, print plan
  python3 seoul_index_methodology.py             # post the thread
  python3 seoul_index_methodology.py --pin        # post, then pin the root
"""

import sys
import tempfile
from pathlib import Path

from atproto import Client, client_utils, models

from seoul_index_card import render_prose_card, curly
from seoul_index_post import CONFIG, KEYCHAIN_SERVICE, keychain_password
import json

# Same lesson as seoul_index_post: membership tests mean an unrecognised flag
# would silently run LIVE, so refuse anything unknown before doing anything.
# (Until 23 Jul 2026 seoul_index_post's import-time guard covered this file by
# accident — and rejected the legitimate --pin while it was at it.)
_KNOWN_ARGS = {'--dry-run', '--pin'}
_unknown = [a for a in sys.argv[1:] if a not in _KNOWN_ARGS]
if _unknown:
    sys.exit(f'Unknown argument(s): {" ".join(_unknown)}. '
             f'Recognised: {" ".join(sorted(_KNOWN_ARGS))}. '
             f'Refusing to run (a bare run posts live).')

DRY_RUN = '--dry-run' in sys.argv
PIN = '--pin' in sys.argv
HERE = Path(__file__).parent

# --- content (exact approved prose; thread-marker emoji dropped for the card) --

EN_INTRO = ('This account provides a portrait of Seoul, based mainly on its open data '
            '(data.seoul.go.kr). Fixed counts appear exactly as published: subway '
            'taps, libraries, Wi-Fi, events, quarterly sales. The figures are the '
            'city’s; an A.I. chooses which to set side by side and largely '
            'writes the posts.')
EN_CAVEAT = ('Crowd figures are different: How many people are in a place, and '
             'their age, gender and visitor split, are not head counts. KT models '
             'them from mobile-signal data and scales to the whole city, so read '
             'them as directional, most reliable for ages 20–50.')
KO_INTRO = ('‘숫자로 보는 서울’은 '
            '주로 서울시 공공데이터(data.seoul.go.kr)로 '
            '그리는 서울의 초상입니다. '
            '지하철 승하차, 도서관·와이파이 '
            '수, 행사, 분기별 매출 등 고정 '
            '수치는 공개된 값 그대로입니다. '
            '숫자는 서울시의 데이터 그대로이고, '
            '조합과 글쓰기는 대부분 A.I.가 합니다.')
# The original plaintext thread signed off "🤖 자동 계정"; the card era
# dropped the emoji but stranded "자동 계정" as a cut-off-looking fragment.
# Dropped entirely (23 Jul 2026): the EN card has no equivalent line and
# the account is already visibly a bot.
KO_CAVEAT = ('‘인구’ 수치(특정 장소의 '
             '실시간 인구, 연령·성별, 방문객 '
             '비율)는 실측이 아니라 KT가 통신 '
             '신호로 추정해 전체 인구로 보정한 '
             '값입니다. 20~50대 구간이 가장 '
             '정확합니다.')
# Comparisons are a third kind of figure: a different publisher, and sometimes
# a different Seoul. The OECD reports functional urban areas, so its Seoul is the
# capital region, not the city the rest of the account counts. The World Bank
# lines are the other way round: whole countries, with Seoul the city itself.
# Both scopes ride the relevant post's card footnote, but the reasoning only
# fits in the pinned thread, and a card explaining only the OECD half would have
# a reader take a nation card for a metro one (the nation vein returned on
# 21 Aug 2026, after this card was written).
EN_CITIES = ('Some posts set Seoul beside other places. City comparisons come from '
             'the OECD, which measures every city the same way: they cover whole '
             'metropolitan areas, so Seoul there is the capital region of about 24 '
             'million, not the 9.6 million the other posts count. Comparisons with '
             'whole countries come from the World Bank, and there Seoul is the city '
             'itself.')
KO_CITIES = ('일부 게시물은 서울을 다른 '
             '도시나 국가와 나란히 놓습니다. '
             '도시 비교는 모든 도시를 같은 '
             '기준으로 측정하는 '
             '경제협력개발기구(OECD) 자료로, '
             '광역도시권 기준이므로 여기서 '
             '서울은 인구 약 960만 명의 '
             '서울시가 아니라 약 2,400만 명의 '
             '수도권을 뜻합니다. 국가와 '
             '비교하는 수치는 '
             '세계은행(World Bank) 자료이며, '
             '이때 서울은 서울시 자체를 '
             '뜻합니다.')

CARDS = [
    {'lang': 'en', 'heading': 'About this account', 'emoji': '\U0001f3d9️', 'body': [EN_INTRO]},
    {'lang': 'en', 'heading': 'About the crowd figures', 'emoji': '\U0001f465', 'body': [EN_CAVEAT]},
    {'lang': 'en', 'heading': 'About the comparisons', 'emoji': '\U0001f30f', 'body': [EN_CITIES]},
    {'lang': 'ko', 'heading': '이 계정에 대하여', 'emoji': '\U0001f3d9️', 'body': [KO_INTRO]},
    {'lang': 'ko', 'heading': '인구 수치에 대하여', 'emoji': '\U0001f465', 'body': [KO_CAVEAT]},
    {'lang': 'ko', 'heading': '비교 수치에 대하여', 'emoji': '\U0001f30f', 'body': [KO_CITIES]},
]

# Every publisher the bot draws on, each hyperlinked in the trailing reply.
SOURCE_LINE = ('Sources · 출처: data.seoul.go.kr, kosis.kr, data-explorer.oecd.org, '
               'rt.molit.go.kr, data.kma.go.kr, airport.co.kr, '
               'opendata.hira.or.kr, mcst.go.kr, know.tour.go.kr, '
               'hrfco.go.kr, data.worldbank.org')
SOURCE_DOMAINS = [('data.seoul.go.kr', 'https://data.seoul.go.kr'),
                  ('kosis.kr', 'https://kosis.kr'),
                  ('data-explorer.oecd.org', 'https://data-explorer.oecd.org'),
                  ('rt.molit.go.kr', 'https://rt.molit.go.kr'),
                  ('data.kma.go.kr', 'https://data.kma.go.kr'),
                  ('airport.co.kr', 'https://www.airport.co.kr'),
                  ('opendata.hira.or.kr', 'https://opendata.hira.or.kr'),
                  ('mcst.go.kr', 'https://www.mcst.go.kr'),
                  ('know.tour.go.kr', 'https://know.tour.go.kr'),
                  ('hrfco.go.kr', 'https://www.hrfco.go.kr'),
                  ('data.worldbank.org', 'https://data.worldbank.org')]


def _alt(card):
    # curly() so the alt text matches the card, which the renderer curls.
    return curly(card['heading'] + '\n\n' + '\n\n'.join(card['body']))


def _source_tb():
    """Clickable source line, no hashtags — keeps the pinned thread clean.
    Walks the domains in the order they appear so the facets stay in step with
    the text however SOURCE_LINE is reordered."""
    tb = client_utils.TextBuilder()
    hits = sorted((SOURCE_LINE.find(dom), dom, url) for dom, url in SOURCE_DOMAINS
                  if SOURCE_LINE.find(dom) != -1)
    pos = 0
    for i, dom, url in hits:
        if i < pos:  # a later domain nested inside an earlier match — skip
            continue
        tb.text(SOURCE_LINE[pos:i]).link(dom, url)
        pos = i + len(dom)
    tb.text(SOURCE_LINE[pos:])
    return tb


def render_all(out_dir):
    out = []
    for i, card in enumerate(CARDS):
        path = Path(out_dir) / f'meth_{i}_{card["lang"]}.png'
        _, size = render_prose_card(card['heading'], card['body'], path,
                                    korean=(card['lang'] == 'ko'), emoji=card['emoji'])
        out.append((str(path), size))
    return out


def main():
    rendered = render_all(Path.cwd() if DRY_RUN else tempfile.mkdtemp())

    print('Methodology thread plan:')
    for card, (path, size) in zip(CARDS, rendered):
        print(f'  [{card["lang"]}] {card["emoji"]} {card["heading"]} — {size}  {path}')
    clickable = ', '.join(dom for dom, _ in SOURCE_DOMAINS)
    print(f'  [reply] {_source_tb().build_text()!r} (clickable: {clickable})')

    if DRY_RUN:
        print('\n(dry run — rendered cards, not posting)')
        return

    config = json.loads(CONFIG.read_text())
    handle = config['handle']
    password = keychain_password(handle, KEYCHAIN_SERVICE)
    bsky = Client()
    bsky.login(handle, password)

    def _reply(parent_ref, root_ref):
        return models.AppBskyFeedPost.ReplyRef(parent=parent_ref, root=root_ref)

    root_ref = None
    prev_ref = None
    for card, (path, size) in zip(CARDS, rendered):
        ar = models.AppBskyEmbedDefs.AspectRatio(width=size[0], height=size[1])
        img = Path(path).read_bytes()
        kwargs = dict(text='', image=img, image_alt=_alt(card),
                      langs=[card['lang']], image_aspect_ratio=ar)
        if prev_ref is not None:
            kwargs['reply_to'] = _reply(prev_ref, root_ref)
        post = bsky.send_image(**kwargs)
        prev_ref = models.create_strong_ref(post)
        if root_ref is None:
            root_ref = prev_ref
    # Trailing clickable source reply.
    bsky.send_post(text=_source_tb(), reply_to=_reply(prev_ref, root_ref))
    print(f'\nPosted methodology thread ({len(CARDS)} cards + source reply).')

    if PIN:
        pin_post(bsky, root_ref)
        print('Pinned the thread root.')


def pin_post(bsky, root_ref):
    """Pin root_ref by updating ONLY the pinned_post field of the existing
    profile record, so the avatar/description are preserved."""
    got = bsky.com.atproto.repo.get_record(
        models.ComAtprotoRepoGetRecord.Params(
            repo=bsky.me.did, collection='app.bsky.actor.profile', rkey='self'))
    record = got.value
    record.pinned_post = models.ComAtprotoRepoStrongRef.Main(
        cid=root_ref.cid, uri=root_ref.uri)
    bsky.com.atproto.repo.put_record(
        models.ComAtprotoRepoPutRecord.Data(
            repo=bsky.me.did, collection='app.bsky.actor.profile', rkey='self',
            record=record, swap_record=got.cid))


if __name__ == '__main__':
    main()
