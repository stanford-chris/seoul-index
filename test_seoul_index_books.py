"""Tests for the books vein, rebuilt 22 Aug 2026 on Seoul's own loan counts.

Two failures are worth defending against here, and neither announces itself.

**Scope.** `SeoulLibraryBookRentNumInfo` counts loans at ONE library, 서울도서관.
The vein it replaced (data4library) covered all 215 public libraries. A card that
loses the scope still reads perfectly: "Literature: 3,625" is simply understood
as a claim about Seoul, and it is wrong by two orders of magnitude.

**Period.** The API publishes no date of any kind. The 60-day window comes from
서울도서관's own page, so the harvester re-reads it every run — and a card that
loses it is a set of loan counts covering nobody knows what. The card carries no
dateline: the harvest date is when the figures were READ, not the period they
cover, and shown in the slot that means "period" on every other vein it read as
contradicting the footnote.

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

# ⚠️ compose() ends by checking its labels against the pool's own with a model
# call (see check_labels). These tests promise no network and no model call, so
# the CALL is switched off here: what they exercise is selection and composition, and a
# live checker would make them slow, non-deterministic and quota-hungry. The
# checker's own behavior is tested in test_seoul_index_labels.py.
S.CHECK_LABELS = False
import seoul_index_books_harvest as H


# Ten subjects, the real classes, with counts that are deliberately NOT a dead
# heat and NOT near each other — a fixture that happens to tie would make the
# detector tests pass without testing anything.
SUBJECTS = [
    ('8', 'Literature', '문학', 3600), ('3', 'Social sciences', '사회과학', 1700),
    ('4', 'Natural sciences', '자연과학', 1200), ('9', 'History and geography', '역사·지리', 1000),
    ('1', 'Philosophy', '철학', 900), ('5', 'Applied sciences', '기술과학', 800),
    ('6', 'Arts', '예술', 700), ('0', 'General works', '총류', 600),
    ('2', 'Religion', '종교', 500), ('7', 'Language', '어학', 400),
]


def agg(days=60, age_days=0, n=10, scope_en='Seoul Library', subjects=None, **over):
    stamp = datetime.now(timezone.utc) - timedelta(days=age_days)
    subs = subjects if subjects is not None else [
        {'code': c, 'name_en': en, 'name_ko': ko, 'loans': v, 'titles': 10}
        for c, en, ko, v in SUBJECTS[:n]]
    out = {
        'generated_at': stamp.isoformat(),
        'source': 'SeoulLibraryBookRentNumInfo',
        'scope_en': scope_en, 'scope_ko': '서울도서관',
        'window_days': days,
        'records': 3000, 'unclassified': 0,
        'subjects': subs,
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
        S.BOOKS_WINDOW.update({'days': None, 'scope_en': None,
                               'scope_ko': None, 'records': None,
                               'loans': None})
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

    def test_a_good_cache_makes_one_fact_per_subject(self):
        with Cache(agg()):
            f = S.books_facts()
        subs = [x for x in f if x['id'].startswith('book_')]
        self.assertEqual([x['id'] for x in subs[:3]],
                         ['book_8', 'book_3', 'book_4'])
        # The counts carry their share of the counted total: 11,400 loans
        # across the ten subjects, so 3,600 is 1 in 3 (see TheShareOfTheCheckouts).
        self.assertEqual([x['value_en'] for x in subs[:3]],
                         ['3,600 (1 in 3)', '1,700 (1 in 7)', '1,200 (1 in 10)'])

    def test_the_subject_names_are_the_harvest_s_words_and_are_pinned(self):
        # The selector rewording 'Applied sciences' to 'Tech' would mislabel a
        # class whose most-borrowed titles are medicine.
        with Cache(agg()):
            f = S.books_facts()
        five = next(x for x in f if x['id'] == 'book_5')
        self.assertEqual(five['label_en'], 'Applied sciences')
        self.assertEqual(five['label_ko'], '기술과학')
        self.assertTrue(all(x['pin'] for x in f))

    def test_a_subject_with_no_name_is_dropped_not_guessed(self):
        subs = [{'code': c, 'name_en': en, 'name_ko': ko, 'loans': v, 'titles': 10}
                for c, en, ko, v in SUBJECTS]
        subs[0]['name_en'] = ''
        with Cache(agg(subjects=subs)):
            ids = [x['id'] for x in S.books_facts()]
        self.assertNotIn('book_8', ids)
        self.assertIn('book_3', ids)

    def test_a_dead_heat_is_detected_only_when_it_exists(self):
        with Cache(agg()):
            self.assertEqual([x for x in S.books_facts()
                              if x['pair'] == 'book_heat'], [])
        subs = [{'code': c, 'name_en': en, 'name_ko': ko, 'loans': v, 'titles': 10}
                for c, en, ko, v in SUBJECTS]
        subs[3]['loans'] = 1190          # 1,200 vs 1,190 — inside 2%
        with Cache(agg(subjects=subs)):
            heat = [x['id'] for x in S.books_facts() if x['pair'] == 'book_heat']
        self.assertEqual(sorted(heat), ['bookheat_4', 'bookheat_9'])

    def test_the_gap_pair_is_the_least_and_most_borrowed(self):
        with Cache(agg()):
            gap = [x['id'] for x in S.books_facts() if x['pair'] == 'book_gap']
        self.assertEqual(gap, ['bookgap_7', 'bookgap_8'])

    def test_no_file_is_silence_not_an_error(self):
        with Cache(None):
            self.assertEqual(S.books_facts(), [])

    def test_too_few_subjects_is_not_a_spread(self):
        with Cache(agg(n=3)):
            self.assertEqual(S.books_facts(), [])
        with Cache(agg(n=4)):
            self.assertNotEqual(S.books_facts(), [])

    def test_a_stale_harvest_goes_quiet(self):
        # The window is ROLLING. A 60-day window last measured months ago is not
        # a fact about now, and the harvest is monthly, so this age means the
        # job has stopped rather than that nothing has changed.
        with Cache(agg(age_days=S.BOOKS_MAX_AGE_DAYS + 1)):
            self.assertEqual(S.books_facts(), [])
        with Cache(agg(age_days=S.BOOKS_MAX_AGE_DAYS - 1)):
            self.assertNotEqual(S.books_facts(), [])

    def test_the_window_is_what_makes_the_vein_speak_at_all(self):
        # With no dateline, the footnote is the ONLY thing on the card saying
        # what period these counts cover.
        d = agg()
        d['window_days'] = 0
        with Cache(d):
            self.assertEqual(S.books_facts(), [])

    def test_a_cache_with_no_window_is_never_posted(self):
        # Counts whose period is unknown are exactly what the old source\'s
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
            f = S.books_facts()
        self.c = card([x for x in f if x['id'] in
                       ('book_8', 'book_3', 'book_1', 'book_7')])

    def test_the_footnote_names_the_library(self):
        # The whole risk of this source: 32 read as a citywide figure.
        self.assertIn('Seoul Library', self.c['note_en'])
        self.assertIn('서울도서관', self.c['note_ko'])

    def test_the_footnote_carries_the_window(self):
        self.assertIn('last 60 days', self.c['note_en'])
        self.assertIn('최근 60일', self.c['note_ko'])

    def test_the_footnote_says_the_set_is_a_cut(self):
        # ⚠️ The feed is the 3,000 most-borrowed items, truncated part-way
        # through the books borrowed twice — everything borrowed once is
        # missing. A footnote reading "Loans at Seoul Library" claimed every
        # loan the library made, which it posted once before this was caught.
        self.assertIn('3,000 most-borrowed items', self.c['note_en'])
        self.assertIn('상위 자료 3,000건', self.c['note_ko'])
        self.assertNotIn('Loans at Seoul Library,', self.c['note_en'])

    def test_a_cache_with_no_record_count_is_never_posted(self):
        # Without it the footnote cannot say the set is a cut, and a card that
        # cannot say so must not be built.
        d = agg()
        d['records'] = 0
        with Cache(d):
            self.assertEqual(S.books_facts(), [])
        d = agg()
        del d['records']
        with Cache(d):
            self.assertEqual(S.books_facts(), [])

    def test_the_window_follows_the_harvest_rather_than_the_code(self):
        with Cache(agg(days=30)):
            c = card(S.books_facts())
        self.assertIn('last 30 days', c['note_en'])
        self.assertIn('최근 30일', c['note_ko'])

    def test_the_card_carries_no_dateline(self):
        # The harvest date is when the figures were read, not the period they
        # cover. In the slot that means "period" on every other vein, it read as
        # contradicting the "last 60 days" in the footnote.
        self.assertEqual(self.c['dateline_en'], '')
        self.assertEqual(self.c['dateline_ko'], '')
        self.assertNotIn('August', self.c['en_body'])

    def test_the_subjects_reach_the_card(self):
        self.assertIn('Literature: 3,600', self.c['en_body'])
        self.assertIn('문학: 3,600', self.c['ko_body'])

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


class EveryRecordMustLandInASubject(unittest.TestCase):
    """`tally()`'s two refusals. Both failure modes are silent by nature: loans
    that fall out of every class shrink each line by an invisible amount without
    changing their order, so the card reads as a quieter library rather than as
    a bug — `portfolio_brief.py`'s trap in another costume."""

    def rows(self, n, cls='8'):
        return [(f'책 {i}', 10, cls) for i in range(n)]

    def test_a_normal_spread_tallies(self):
        rows = sum((self.rows(5, c) for c in '8341'), [])
        subs, unclassified = H.tally(rows)
        self.assertEqual(unclassified, 0)
        self.assertEqual({s['code'] for s in subs}, set('8341'))
        self.assertEqual(subs[0]['loans'], 50)

    def test_a_few_unclassifiable_records_are_counted_not_hidden(self):
        rows = sum((self.rows(50, c) for c in '8341'), []) + self.rows(2, '')
        subs, unclassified = H.tally(rows)
        self.assertEqual(unclassified, 2)

    def test_too_many_unclassifiable_records_abort(self):
        rows = sum((self.rows(50, c) for c in '8341'), []) + self.rows(50, 'X')
        with self.assertRaises(ValueError):
            H.tally(rows)

    def test_the_names_come_from_the_library_s_own_categories(self):
        # KDC, not DDC: under DDC a 4 is language and a 7 is the arts, which
        # would file 이기적 유전자 under language and 여행영어 under the arts —
        # and the card would read perfectly. Verified 22 Aug 2026 against
        # lib.seoul.go.kr/statistics/favorLoan?category=N00.
        self.assertEqual(H.KDC['4'], ('Natural sciences', '자연과학'))
        self.assertEqual(H.KDC['7'], ('Language', '어학'))
        self.assertEqual(H.KDC['8'], ('Literature', '문학'))

    def test_too_few_subjects_aborts(self):
        with self.assertRaises(ValueError):
            H.tally(sum((self.rows(5, c) for c in '83'), []))


# ---------------------------------------------------------------------------
# The share of the checkouts counted: "Literature: 3,700 (1 in 3)"
# ---------------------------------------------------------------------------
# A count of 3,700 says nothing about whether that is many, and a card shows
# FOUR of the ten subjects, so the four alone cannot say what the other six
# weigh. The ratio does. Both halves of it come from the SAME truncated feed,
# which is what makes it honest — but that also means it is a share of the
# checkouts COUNTED and never of all checkouts, and the footnote must say so.

class TheShareOfTheCheckouts(unittest.TestCase):
    TOTAL = 11400        # the ten fixture subjects, summed

    def facts(self, **kw):
        with Cache(agg(**kw)):
            return {f['id']: f for f in S.books_facts()}

    def test_the_ratio_is_the_subjects_share_of_the_counted_total(self):
        f = self.facts()
        self.assertEqual(f['book_8']['value_en'], '3,600 (1 in 3)')   # 11,400/3,600
        # 11,400/400 is 28.5 exactly, and round() is banker's rounding, so it
        # goes to the EVEN 28 rather than up to 29. Pinned rather than
        # "corrected": at these magnitudes a half unit means nothing, and the
        # membership vein rounds the same way.
        self.assertEqual(f['book_7']['value_en'], '400 (1 in 28)')
        self.assertEqual(S.BOOKS_WINDOW['loans'], '11,400')

    def test_korean_counts_checkouts_in_korean(self):
        """Regression: both languages were built from one string, which put the
        English "(1 in 3)" on the KOREAN card. The counter is 건, and Korean
        counts one of every three head-final."""
        f = self.facts()
        self.assertEqual(f['book_8']['value_ko'], '3,600 (3건 중 1건)')
        self.assertNotIn('1 in', f['book_7']['value_ko'])

    def test_the_pair_facts_carry_the_same_value_as_their_plain_siblings(self):
        """book_7 and bookgap_7 are the same subject. Two values for one subject
        would put a card at odds with itself the moment a pair was picked."""
        f = self.facts()
        for pid, plain in (('bookgap_7', 'book_7'), ('bookgap_8', 'book_8')):
            self.assertEqual(f[pid]['value_en'], f[plain]['value_en'])
            self.assertEqual(f[pid]['value_ko'], f[plain]['value_ko'])

    def test_a_dead_heat_pair_carries_it_too(self):
        subs = [{'code': c, 'name_en': en, 'name_ko': ko, 'loans': v, 'titles': 10}
                for c, en, ko, v in SUBJECTS]
        subs[2]['loans'] = subs[3]['loans'] = 1200      # 자연과학 / 역사·지리 level
        f = self.facts(subjects=subs)
        self.assertEqual(f['bookheat_4']['value_en'], f['book_4']['value_en'])
        self.assertEqual(f['bookheat_9']['value_en'], f['book_9']['value_en'])

    def test_the_card_still_sorts_by_checkouts(self):
        """_sortkey strips one trailing parenthetical. A "3,600 · 1 in 3" form
        would return None and silently drop the size sort off this card."""
        f = self.facts()
        self.assertEqual(S._sortkey(f['book_8']['value_en']), ('num', 3600.0))
        labels = [it['label'] for it in
                  card([f['book_7'], f['book_8'], f['book_3']])['items_en']]
        self.assertEqual(labels, ['Literature', 'Social sciences', 'Language'])

    def test_the_footnote_states_the_denominator_and_says_counted(self):
        """A ratio whose total the reader cannot see is a number they cannot
        check — and the feed is a cut, so it is the checkouts COUNTED, never
        all of them."""
        f = self.facts()
        c = card([f['book_8'], f['book_3'], f['book_1'], f['book_7']])
        self.assertIn('11,400', c['note_en'])
        self.assertIn('checkouts counted', c['note_en'])
        self.assertNotIn('all checkouts', c['note_en'])
        self.assertIn('집계된 대출 11,400건', c['note_ko'])

    def test_the_ratio_never_reaches_the_masthead(self):
        """This vein carries no dateline at all; the note's period slot is None
        so nothing can lift out of it."""
        f = self.facts()
        c = card([f['book_8'], f['book_3'], f['book_1'], f['book_7']])
        self.assertEqual(S._card_payload(c, 'en')[3], '')

    def test_one_subject_swamping_the_rest_drops_the_ratio_everywhere(self):
        """All-or-nothing. One total divides every line, so a per-line guard
        could leave the largest subject bare while the rest carried a ratio:
        one card, two forms, for no reason a reader could see."""
        subs = [{'code': c, 'name_en': en, 'name_ko': ko, 'loans': v, 'titles': 10}
                for c, en, ko, v in SUBJECTS]
        subs[0]['loans'] = 90000        # 1 in 1 is not a ratio
        f = self.facts(subjects=subs)
        self.assertEqual(f['book_8']['value_en'], '90,000')
        self.assertEqual(f['book_7']['value_en'], '400')     # and not just the big one
        self.assertIsNone(S.BOOKS_WINDOW['loans'])
        c = card([f['book_8'], f['book_3'], f['book_1'], f['book_7']])
        self.assertNotIn('checkouts counted', c['note_en'])
        self.assertIn('most-borrowed', c['note_en'])         # the scope is unchanged

    def test_the_denominator_is_recomputed_rather_than_kept(self):
        """Seeded with a lie, a good run must replace it. The footnote prints
        this number, so a value carried over from an earlier harvest would
        state a total the card's own figures do not add up to."""
        with Cache(agg()):
            S.BOOKS_WINDOW['loans'] = '999,999'
            S.books_facts()
        self.assertEqual(S.BOOKS_WINDOW['loans'], '11,400')


# ⚠️ Keep this at the END of the file: above the last class it runs before those
# tests are defined and reports a confident, short "OK" (see ~/Scripts/CLAUDE.md,
# where exactly that hid 8 tests in test_update_projects_note.py). It had drifted
# back above the last class by 25 August 2026, hiding 9 tests from a direct run
# while discovery still saw all 39 — the two disagreed and the direct run was the
# one that lied.
if __name__ == '__main__':
    unittest.main()
