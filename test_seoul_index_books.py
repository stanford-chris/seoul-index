"""Tests for the books vein, rebuilt 22 Aug 2026 on Seoul's own loan counts.

Two failures are worth defending against here, and neither announces itself.

**Scope.** `SeoulLibraryBookRentNumInfo` counts loans at ONE library, 서울도서관,
where the most-borrowed book runs to a few dozen checkouts. The vein it replaced
(data4library) covered all 215 public libraries. A card that loses the scope
still reads perfectly: "The most-borrowed book: 32" is simply understood as a
claim about Seoul, and it is wrong by two orders of magnitude.

**Period.** The API publishes no date of any kind. The 60-day window comes from
서울도서관's own page, so the harvester re-reads it every run — and a card that
loses it is a set of loan counts covering nobody knows what.

No network, no model call, no posting: the HTTP layer is stubbed per test.
"""
import json
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.argv = ['test']
sys.path.insert(0, str(Path(__file__).resolve().parent))
import seoul_index_post as S
import seoul_index_books_harvest as H


def agg(days=60, age_days=0, n=10, scope_en='Seoul Library', **over):
    stamp = datetime.now(timezone.utc) - timedelta(days=age_days)
    out = {
        'generated_at': stamp.isoformat(),
        'source': 'SeoulLibraryBookRentNumInfo',
        'scope_en': scope_en, 'scope_ko': '서울도서관',
        'window_days': days,
        'period': {'label_en': '22 August', 'label_ko': '8월 22일'},
        'books': [{'ranking': i, 'bookname': f'책 {i}', 'authors': '지음',
                   'loan_count': 33 - i} for i in range(1, n + 1)],
    }
    out.update(over)
    return out


class Cache:
    """Point BOOKS_AGG at a temp file holding `data`, and clear the globals the
    vein sets, so no test can pass on another's leftovers."""

    def __init__(self, data):
        self.data = data

    def __enter__(self):
        import tempfile
        self.tmp = Path(tempfile.mkdtemp()) / 'books_agg.json'
        if self.data is not None:
            self.tmp.write_text(json.dumps(self.data, ensure_ascii=False))
        self.real = S.BOOKS_AGG
        S.BOOKS_AGG = self.tmp
        S.BOOKS_PERIOD.update({'en': None, 'ko': None})
        S.BOOKS_WINDOW.update({'days': None, 'scope_en': None, 'scope_ko': None})
        return self

    def __exit__(self, *exc):
        S.BOOKS_AGG = self.real


def card(facts):
    """Compose a books-only card from `facts`, as the poster would."""
    sel = {'opener_en': 'What Seoul Library lent most',
           'opener_ko': '서울도서관에서 가장 많이 빌려 간 책', 'opener_emoji': '📚',
           'picks': [{'id': f['id']} for f in facts]}
    return S.compose(sel, facts)


class TheCacheIsReadOrTheVeinGoesQuiet(unittest.TestCase):

    def test_a_good_cache_makes_three_facts(self):
        with Cache(agg()):
            f = S.books_facts()
        self.assertEqual([x['id'] for x in f],
                         ['book_top', 'book_low', 'book_sum'])
        self.assertEqual([x['value_en'] for x in f], ['32', '23', '275'])

    def test_no_file_is_silence_not_an_error(self):
        with Cache(None):
            self.assertEqual(S.books_facts(), [])

    def test_one_book_is_not_a_pair(self):
        with Cache(agg(n=1)):
            self.assertEqual(S.books_facts(), [])

    def test_a_stale_harvest_goes_quiet(self):
        # The window is ROLLING. A 60-day window last measured months ago is not
        # a fact about now, and the harvest is monthly, so this age means the
        # job has stopped rather than that nothing has changed.
        with Cache(agg(age_days=S.BOOKS_MAX_AGE_DAYS + 1)):
            self.assertEqual(S.books_facts(), [])
        with Cache(agg(age_days=S.BOOKS_MAX_AGE_DAYS - 1)):
            self.assertEqual(len(S.books_facts()), 3)

    def test_a_cache_with_no_window_is_never_posted(self):
        # Counts whose period is unknown are exactly what the old source's
        # rejection was about. Silence, not a card with no window on it.
        d = agg()
        del d['window_days']
        with Cache(d):
            self.assertEqual(S.books_facts(), [])

    def test_a_naive_timestamp_is_silence_not_a_crash(self):
        with Cache(agg(generated_at='2026-08-22T09:59:18')):
            self.assertEqual(S.books_facts(), [])


class TheCardCarriesTheLibraryAndTheWindow(unittest.TestCase):

    def setUp(self):
        with Cache(agg()):
            self.c = card(S.books_facts())

    def test_the_footnote_names_the_library(self):
        # The whole risk of this source: 32 read as a citywide figure.
        self.assertIn('Seoul Library', self.c['note_en'])
        self.assertIn('서울도서관', self.c['note_ko'])

    def test_the_footnote_carries_the_window(self):
        self.assertIn('last 60 days', self.c['note_en'])
        self.assertIn('최근 60일', self.c['note_ko'])

    def test_the_window_follows_the_harvest_rather_than_the_code(self):
        with Cache(agg(days=30)):
            c = card(S.books_facts())
        self.assertIn('last 30 days', c['note_en'])
        self.assertIn('최근 30일', c['note_ko'])

    def test_the_as_of_date_heads_the_card_and_is_not_the_window(self):
        # Two different things, worded separately on purpose: the date is when
        # the figures were read, not the window's end.
        self.assertEqual(self.c['dateline_en'], '22 August')
        self.assertNotIn('22 August', self.c['note_en'])

    def test_the_credit_is_seoul_not_the_old_publisher(self):
        self.assertIn('data.seoul.go.kr', self.c['src_en'])
        self.assertNotIn('data4library', self.c['src_en'])


class TheWindowIsVerifiedNotAssumed(unittest.TestCase):
    """`verify_window` is the only thing standing between these counts and a
    period nobody can vouch for, so every path out of it is checked."""

    def page(self, text):
        H.http_get = lambda url, _t=text: _t

    def tearDown(self):
        H.http_get = self.real

    def setUp(self):
        self.real = H.http_get
        self.titles = ['모순 :양귀자 장편소설', '작별하지 않는다', '바깥은 여름']

    def test_the_window_is_read_from_the_library(self):
        self.page('TOP 100 목록 (최근 60일 자료집계) ' + ' '.join(self.titles))
        self.assertEqual(H.verify_window(self.titles), 60)

    def test_a_changed_window_is_followed(self):
        self.page('(최근 30일 자료집계) ' + ' '.join(self.titles))
        self.assertEqual(H.verify_window(self.titles), 30)

    def test_a_page_with_no_window_aborts_the_harvest(self):
        self.page('대출이 많은 책 TOP 100 목록 ' + ' '.join(self.titles))
        with self.assertRaises(RuntimeError):
            H.verify_window(self.titles)

    def test_a_page_that_no_longer_shows_our_list_aborts(self):
        # The window belongs to the library's list; it is only ours to borrow
        # while the API is still serving that same list.
        self.page('(최근 60일 자료집계) 전혀 다른 책들')
        with self.assertRaises(RuntimeError):
            H.verify_window(self.titles)

    def test_a_partial_match_is_not_a_match(self):
        self.page('(최근 60일 자료집계) ' + self.titles[0])
        with self.assertRaises(RuntimeError):
            H.verify_window(self.titles)

    def test_an_implausible_window_aborts(self):
        self.page('(최근 9999일 자료집계) ' + ' '.join(self.titles))
        with self.assertRaises(RuntimeError):
            H.verify_window(self.titles)


# ⚠️ Keep this at the END of the file: above the last class it runs before those
# tests are defined and reports a confident, short "OK" (see ~/Scripts/CLAUDE.md,
# where exactly that hid 8 tests in test_update_projects_note.py).
if __name__ == '__main__':
    unittest.main()
