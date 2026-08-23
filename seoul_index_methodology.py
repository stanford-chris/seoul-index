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
  python3 seoul_index_methodology.py --pin       # post, then pin the root
  python3 seoul_index_methodology.py --replace   # post, pin, delete the old thread

--replace exists because the thread is not edited, it is re-posted: a card is an
image, so changing a word means seven new records. Without it the superseded
thread stays in the feed, unpinned and wrong, and the only tool for removing it
was wipe_posts.py, which takes the whole account. --replace implies --pin, and
deletes ONLY the records of the thread that was pinned when the run started, and
only after the new thread is up and pinned. The order matters: an account left
without a pinned methodology thread for a few seconds is recoverable, one whose
credits were deleted before the replacement posted is not.

The superseded wording is printed before it goes rather than archived to a file
(wipe_posts.py archives, because the wording of a daily card exists nowhere
else). This thread's prose IS the code, so git already holds every version.
"""

import sys
import tempfile
from pathlib import Path

from atproto import Client, client_utils, models

from seoul_index_card import render_prose_card, curly
from seoul_index_post import CONFIG, KEYCHAIN_SERVICE, keychain_password
# all_records only reads the repo. It lives in wipe_posts because that is where
# it was first needed, and --replace needs the same listing: the records of one
# thread, straight from the PDS rather than from a feed view.
from wipe_posts import all_records
import json

# Same lesson as seoul_index_post: membership tests mean an unrecognised flag
# would silently run LIVE, so refuse anything unknown before doing anything.
# (Until 23 Jul 2026 seoul_index_post's import-time guard covered this file by
# accident — and rejected the legitimate --pin while it was at it.)
_KNOWN_ARGS = {'--dry-run', '--pin', '--replace'}
_unknown = [a for a in sys.argv[1:] if a not in _KNOWN_ARGS]
if _unknown:
    sys.exit(f'Unknown argument(s): {" ".join(_unknown)}. '
             f'Recognised: {" ".join(sorted(_KNOWN_ARGS))}. '
             f'Refusing to run (a bare run posts live).')

DRY_RUN = '--dry-run' in sys.argv
REPLACE = '--replace' in sys.argv
# Replacing without pinning would leave the account with no pinned thread at all,
# which is the one outcome worse than a stale one.
PIN = '--pin' in sys.argv or REPLACE
HERE = Path(__file__).parent

# --- content (exact approved prose; thread-marker emoji dropped for the card) --

# Rewritten 23 Aug 2026. The old wording named five Seoul Open Data examples
# and said the figures were “the city’s”, which was true when the account had one
# publisher and misleading once it had eleven: a reader met a KMA temperature or
# a World Bank line with nothing on the card to say the account went there. The
# example list is now drawn from across the veins, and the closing sentence
# points at the credits rather than repeating a publisher name that would go
# stale the next time a vein lands.
EN_INTRO = ('This account provides a portrait of Seoul, based mainly on the city’s own '
            'open data (data.seoul.go.kr), with other publishers’ figures alongside it: '
            'weather, '
            'rivers, property, health, tourism, and Seoul set against other cities and '
            'countries. Counts appear exactly as published: subway taps, libraries, '
            'quarterly sales, apartment filings, what the central library lends. An '
            'A.I. chooses which to set side by side and largely writes the posts. '
            'Every publisher is credited at the end of this thread.')
EN_CAVEAT = ('Crowd figures are different: How many people are in a place, and '
             'their age, gender and visitor split, are not head counts. KT models '
             'them from mobile-signal data and scales to the whole city, so read '
             'them as directional, most reliable for ages 20–50.')
KO_INTRO = ('‘숫자로 보는 서울’은 주로 서울시 '
            '공공데이터(data.seoul.go.kr)에 기상·하천·부동산·보건·관광 '
            '등 다른 기관의 자료를 더해 그리는 서울의 '
            '초상입니다. 다른 도시·국가와 비교한 수치도 '
            '있습니다. 지하철 승하차, 도서관 수, 분기별 매출, '
            '아파트 실거래, 서울도서관 대출 등 고정 수치는 '
            '공개된 값 그대로입니다. 조합과 글쓰기는 대부분 '
            'A.I.가 하며, 모든 출처는 이 스레드 마지막에 있습니다.')
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
# The trailing reply always opens with this, and no card post carries text at
# all, which is how --replace tells a methodology thread from any other thread
# it might find pinned. Kept as its own constant so the recogniser and the line
# itself cannot drift apart.
SOURCE_PREFIX = 'Sources · 출처: '
SOURCE_LINE = (SOURCE_PREFIX + 'data.seoul.go.kr, kosis.kr, data-explorer.oecd.org, '
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

    # Read this before posting: once the new root is pinned the old uri is gone
    # from the profile record, and with it the only pointer to what to delete.
    old_root_uri = current_pinned_uri(bsky) if REPLACE else None
    if REPLACE and old_root_uri is None:
        print('--replace: nothing is pinned, so there is no thread to replace.')

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

    if REPLACE and old_root_uri:
        replace_old_thread(bsky, old_root_uri)


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


def current_pinned_uri(bsky):
    """The uri of the currently pinned post, or None if nothing is pinned."""
    got = bsky.com.atproto.repo.get_record(
        models.ComAtprotoRepoGetRecord.Params(
            repo=bsky.me.did, collection='app.bsky.actor.profile', rkey='self'))
    pinned = getattr(got.value, 'pinned_post', None)
    return pinned.uri if pinned else None


def old_thread_records(did, root_uri):
    """Every record of the thread rooted at root_uri, oldest first.

    A reply names its root, so one pass over the repo collects the whole thread
    without walking parent links. Listed from the PDS rather than from a thread
    view for the same reason wipe_posts lists it that way: a view can drop
    replies, and the source reply is the one post here with no image on it.
    """
    _, recs = all_records(did)
    mine = [r for r in recs
            if r['uri'] == root_uri
            or (((r['value'].get('reply') or {}).get('root') or {}).get('uri')
                == root_uri)]
    return sorted(mine, key=lambda r: r['value']['createdAt'])


def is_methodology_thread(recs):
    """Does this look like a thread THIS script posted?

    --replace deletes whatever was pinned, and what is pinned is not guaranteed
    to be a methodology thread: pin a daily card by hand, forget, and a later
    --replace would take that card and every reply under it. So the shape is
    checked first, and it is a shape no daily post has: exactly len(CARDS)
    captioned-nothing image posts, then one text reply opening with the credits.
    A card post carries empty text by construction (Bluesky renders text above
    the image, so a caption cannot sit under the card).
    """
    if len(recs) != len(CARDS) + 1:
        return False
    *cards, last = recs
    if any((r['value'].get('text') or '') for r in cards):
        return False
    if not all((r['value'].get('embed') or {}).get('images') for r in cards):
        return False
    return (last['value'].get('text') or '').startswith(SOURCE_PREFIX)


def replace_old_thread(bsky, old_root_uri):
    """Delete the superseded thread, after printing the wording it takes with
    it. Refuses, loudly and without deleting anything, if what was pinned is not
    a methodology thread: by this point the replacement is already up, so the
    safe failure is to leave both threads standing and say so."""
    recs = old_thread_records(bsky.me.did, old_root_uri)
    print(f'\nSuperseded thread, {len(recs)} record(s) '
          f'(rooted at {old_root_uri.split("/")[-1]}):')
    for r in recs:
        v = r['value']
        body = v.get('text') or ' / '.join(
            i.get('alt', '') for i in (v.get('embed') or {}).get('images', []))
        print(f'  {r["uri"].split("/")[-1]}  {body[:100].replace(chr(10), " ")}')

    if not is_methodology_thread(recs):
        sys.exit('\nThat is not the shape of a methodology thread, so nothing '
                 'was deleted. The new thread is posted and pinned; the old one '
                 'is still up. Sort it out by hand.')

    failed = [r['uri'] for r in recs if not bsky.delete_post(r['uri'])]
    _, left = all_records(bsky.me.did)
    survivors = [r['uri'] for r in recs if r['uri'] in {x['uri'] for x in left}]
    print(f'Deleted {len(recs) - len(survivors)} of {len(recs)}.')
    if failed or survivors:
        sys.exit(f'Deletion incomplete: {len(failed)} call(s) failed, '
                 f'{len(survivors)} record(s) still in the repo.')


if __name__ == '__main__':
    main()
