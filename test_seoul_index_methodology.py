"""Tests that the pinned methodology thread still describes the live bot.

The thread is posted BY HAND — seoul_index_methodology.py has no launchd job —
so nothing re-runs when a vein is added, and until now nothing compared the two
files. That is exactly how it drifted: commit 9eb796d cut the World Bank vein
and removed data.worldbank.org from the credits, the vein came back on
21 Aug 2026 and the credits did not. The pinned thread then described the
account with a live publisher missing, and no run of anything would have said
so: the daily posts credit their own sources correctly, so the feed looked
right while the account's own description of itself was wrong.

Hence a CROSS-FILE assertion. Every publisher the daily posts credit must also
be credited in the pinned thread, and clickably. The failure this catches is
silent by construction, which is the same reason the vein tests exist.

Note what it cannot catch: whether the prose still describes what the cards
actually do. The same 21 Aug drift left the comparisons card explaining only
the OECD half, so a reader would take a World Bank nation card for a metro one.
No test reads for that. Re-read the cards when a vein changes what a reader is
being shown, not just who published it.

No network and no posting: the facets are built locally and nothing is sent.
"""
import sys, unittest
from pathlib import Path

# Both modules refuse unrecognised argv at import time (a bare run posts live),
# so unittest's own arguments have to be cleared before importing either.
sys.argv = ['test']
sys.path.insert(0, str(Path(__file__).resolve().parent))
import seoul_index_post as S
import seoul_index_methodology as M

BSKY_POST_LIMIT = 300


class PinnedThreadCreditsEveryPublisher(unittest.TestCase):

    def test_credits_match_the_daily_posts(self):
        """The two lists are the same publishers, pointing at the same URLs.

        Compared as dicts rather than as sets so a domain credited in both
        files but linked to different places also fails: a credit that goes
        to the wrong publisher is worse than one that is missing.
        """
        posts = dict(S.LINK_DOMAINS)
        pinned = dict(M.SOURCE_DOMAINS)
        missing = {d: u for d, u in posts.items() if d not in pinned}
        extra = {d: u for d, u in pinned.items() if d not in posts}
        self.assertFalse(missing, f'the posts credit publishers the pinned '
                                  f'thread does not: {sorted(missing)}')
        self.assertFalse(extra, f'the pinned thread credits publishers no post '
                                f'can use: {sorted(extra)}')
        for dom, url in posts.items():
            self.assertEqual(pinned[dom], url,
                             f'{dom} links somewhere different in each file')

    def test_every_credited_domain_is_clickable(self):
        """Each domain in SOURCE_DOMAINS gets its own facet on the real line.

        A domain absent from SOURCE_LINE is not an error in _source_tb: it is
        simply skipped, so it would ride the thread as unlinked plain text.
        """
        tb = M._source_tb()
        text = tb.build_text()
        raw = text.encode()
        linked = {}
        for f in tb.build_facets():
            seg = raw[f.index.byte_start:f.index.byte_end].decode()
            linked[seg] = f.features[0].uri
        for dom, url in M.SOURCE_DOMAINS:
            self.assertIn(dom, text, f'{dom} is credited but missing from '
                                     f'SOURCE_LINE, so it would not be linked')
            self.assertEqual(linked.get(dom), url,
                             f'{dom} did not become a clickable link')
        self.assertEqual(len(linked), len(M.SOURCE_DOMAINS))

    def test_no_domain_hides_inside_another(self):
        """No credited domain may be a substring of another.

        _source_tb walks the line by first occurrence and skips any match that
        starts before the position it has reached, so a domain nested inside a
        longer one (a bare tour.go.kr beside know.tour.go.kr) silently loses
        its link instead of erroring. Guarded here because the day that is
        introduced is the day it goes unnoticed.
        """
        doms = [d for d, _ in M.SOURCE_DOMAINS]
        for d in doms:
            inside = [o for o in doms if o != d and d in o]
            self.assertFalse(inside, f'{d} is contained in {inside}: one of '
                                     f'the two would not be hyperlinked')

    def test_source_reply_fits_one_post(self):
        """The credits are the LAST post of the thread.

        Over the limit, the six cards are already up and the run dies posting
        the reply, leaving a pinned thread whose sources are nowhere on it.
        """
        n = len(M._source_tb().build_text())
        self.assertLessEqual(n, BSKY_POST_LIMIT,
                             f'source reply is {n} characters')


def _card(alt='About this account'):
    """A methodology card as the repo returns it: no text, one image."""
    return {'uri': 'at://did/app.bsky.feed.post/x', 'value': {
        'text': '', 'createdAt': '2026-08-23T00:00:00Z',
        'embed': {'images': [{'alt': alt}]}}}


def _credits(text=None):
    return {'uri': 'at://did/app.bsky.feed.post/y', 'value': {
        'text': M.SOURCE_LINE if text is None else text,
        'createdAt': '2026-08-23T00:00:01Z'}}


def _thread(cards=None, last=None):
    return [_card() for _ in range(len(M.CARDS) if cards is None else cards)] + [
        _credits() if last is None else last]


class ReplaceRecognisesItsOwnThread(unittest.TestCase):
    """Guards --replace, which deletes whatever was pinned when it started.

    Nothing guarantees the pinned post is a methodology thread. Pin a daily
    card by hand, forget about it, and a later --replace would delete that card
    and its source reply instead. is_methodology_thread is the only thing
    standing between that mistake and a permanent deletion, so its false cases
    matter more than its true one.
    """

    def test_a_real_thread_is_recognised(self):
        self.assertTrue(M.is_methodology_thread(_thread()))

    def test_a_thread_of_a_DIFFERENT_length_is_still_recognised(self):
        """The thread being replaced is by definition the PREVIOUS shape.

        This test exists because the check used to compare the old thread with
        len(CARDS), so the first time the thread grew (six cards to eight, when
        the counts paragraph became its own card) --replace posted and pinned
        the new thread and then refused to delete the old one: the comparison
        failed exactly when it was needed. Both a shorter and a longer thread
        must be recognised.
        """
        for n in (len(M.CARDS) - 2, len(M.CARDS) - 1, len(M.CARDS) + 1):
            self.assertTrue(M.is_methodology_thread(_thread(cards=n)),
                            f'{n} cards + credits should still be recognised')

    def test_a_thread_too_short_or_too_long_is_refused(self):
        """A bound is still wanted: nothing of ours is two records or thirty."""
        for n in (0, 1, M.MAX_THREAD_RECORDS):
            self.assertFalse(M.is_methodology_thread(_thread(cards=n)),
                             f'{n} cards + credits should not be recognised')

    def test_a_card_with_text_is_refused(self):
        """A daily index card is an image WITH a caption of hashtags.

        That is the shape most likely to be pinned by mistake, so it is the one
        the check has to reject.
        """
        thread = _thread()
        thread[0]['value']['text'] = '#Seoul #서울'
        self.assertFalse(M.is_methodology_thread(thread))

    def test_a_card_without_an_image_is_refused(self):
        thread = _thread()
        thread[1]['value'].pop('embed')
        self.assertFalse(M.is_methodology_thread(thread))

    def test_a_last_post_that_is_not_the_credits_is_refused(self):
        self.assertFalse(M.is_methodology_thread(
            _thread(last=_credits('Sources: data.seoul.go.kr'))))

    def test_the_credits_still_open_with_the_prefix(self):
        """The recogniser reads SOURCE_PREFIX, the thread posts SOURCE_LINE.

        Reword the line without the prefix and every future --replace would
        refuse to clean up after itself, silently, one run too late.
        """
        self.assertTrue(M.SOURCE_LINE.startswith(M.SOURCE_PREFIX))

    def test_replace_is_a_recognised_argument(self):
        self.assertIn('--replace', M._KNOWN_ARGS)


if __name__ == '__main__':
    unittest.main()
