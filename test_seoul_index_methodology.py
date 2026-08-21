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


if __name__ == '__main__':
    unittest.main()
