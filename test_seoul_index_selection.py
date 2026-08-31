"""Tests for the 17 Aug 2026 selection fixes: the vein floor, the repeat guard
and the spotlight flat-card check. No network, no model call, no posting."""
import sys, unittest
from pathlib import Path
from datetime import datetime, timedelta, timezone

sys.argv = ['test']
sys.path.insert(0, str(Path(__file__).resolve().parent))
import seoul_index_post as S
import seoul_index_card as C

# ⚠️ compose() ends by checking its labels against the pool's own with a model
# call (see check_labels). These tests promise no network and no model call, so
# the CALL is switched off here: what they exercise is selection and composition, and a
# live checker would make them slow, non-deterministic and quota-hungry. The
# checker's own behaviour is tested in test_seoul_index_labels.py.
S.CHECK_LABELS = False


def f(fid, cat):
    return {'id': fid, 'cat': cat, 'label_en': fid, 'value_en': '1',
            'estimated': False, 'pair': None}


def ago(days):
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


POOL = ([f(f'crowd{i}', 'crowd') for i in range(8)]
        + [f(f'world{i}', 'world') for i in range(6)]
        + [f(f'culture{i}', 'culture') for i in range(4)]
        + [f(f'air{i}', 'air') for i in range(2)])          # too small to promote


class VeinFloor(unittest.TestCase):
    def test_never_posted_vein_is_promoted_and_pool_narrowed(self):
        pool, cat = S.promote_starved(POOL, {'cat_last_at': {'crowd': ago(0)}})
        self.assertIn(cat, ('world', 'culture'))
        self.assertTrue(all(x['cat'] == cat for x in pool))

    def test_never_posted_beats_merely_old(self):
        state = {'cat_last_at': {'crowd': ago(0), 'world': ago(90)}}
        _, cat = S.promote_starved(POOL, state)
        self.assertEqual(cat, 'culture')          # never posted at all

    def test_oldest_wins_when_all_have_posted(self):
        state = {'cat_last_at': {'crowd': ago(1), 'world': ago(20),
                                 'culture': ago(9), 'air': ago(99)}}
        _, cat = S.promote_starved(POOL, state)
        self.assertEqual(cat, 'world')

    def test_small_vein_never_promoted(self):
        # 'air' has 2 facts and the longest wait, but cannot fill a card.
        state = {'cat_last_at': {'crowd': ago(1), 'world': ago(1),
                                 'culture': ago(1), 'air': ago(999)}}
        pool, cat = S.promote_starved(POOL, state)
        self.assertIsNone(cat)
        self.assertEqual(len(pool), len(POOL))

    def test_nothing_starved_leaves_pool_untouched(self):
        fresh = {c: ago(1) for c in ('crowd', 'world', 'culture', 'air')}
        pool, cat = S.promote_starved(POOL, {'cat_last_at': fresh})
        self.assertIsNone(cat)
        self.assertIs(pool, POOL)

    def test_no_two_promotions_running(self):
        # Every promotable vein has led before, so nothing is waiting to debut
        # and the alternation rule holds. ⚠️ Leave a never-posted vein in this
        # fixture and the test passes for the wrong reason: the debut override
        # below would fire and the guard would never be exercised at all.
        state = {'cat_last_at': {'crowd': ago(1), 'world': ago(20),
                                 'culture': ago(9)},
                 'last_cat': 'world', 'last_promoted_cat': 'world'}
        _, cat = S.promote_starved(POOL, state)
        self.assertIsNone(cat)

    def test_never_posted_vein_overrides_the_alternation_rule(self):
        # 'culture' has never led a card, so a promotion may follow a promotion.
        state = {'cat_last_at': {'crowd': ago(1), 'world': ago(20)},
                 'last_cat': 'world', 'last_promoted_cat': 'world'}
        _, cat = S.promote_starved(POOL, state)
        self.assertEqual(cat, 'culture')

    def test_debut_override_does_not_reach_a_vein_too_small_to_promote(self):
        # The only never-posted vein here is 'air', and 2 facts cannot fill a
        # card. It must not count as a debut waiting: if it did, 'world' would
        # be promoted back to back on a queue that can never actually drain.
        small = ([f(f'world{i}', 'world') for i in range(6)]
                 + [f(f'air{i}', 'air') for i in range(2)])
        state = {'cat_last_at': {'world': ago(20)},
                 'last_cat': 'world', 'last_promoted_cat': 'world'}
        _, cat = S.promote_starved(small, state)
        self.assertIsNone(cat)

    def test_promotion_resumes_after_an_ordinary_post(self):
        state = {'cat_last_at': {'crowd': ago(1)},
                 'last_cat': 'crowd', 'last_promoted_cat': 'world'}
        _, cat = S.promote_starved(POOL, state)
        self.assertIsNotNone(cat)

    def test_unparseable_stamp_counts_as_starved_not_as_a_crash(self):
        state = {'cat_last_at': {'crowd': 'not-a-date', 'world': ago(0),
                                 'culture': ago(0)}}
        _, cat = S.promote_starved(POOL, state)
        self.assertEqual(cat, 'crowd')


class RepeatGuard(unittest.TestCase):
    def setUp(self):
        self.calls = []

    def fake_select(self, answers):
        """Stand in for the claude -p call, returning canned picks in turn and
        recording the pool it was handed each time."""
        it = iter(answers)

        def _sel(pool, state):
            self.calls.append({x['id'] for x in pool})
            return {'picks': [{'id': i} for i in next(it)]}
        return _sel

    def test_verbatim_repeat_is_rejected_then_replaced(self):
        state = {'recent_cards': [['a', 'b', 'c', 'd']]}
        pool = [f(x, 'spending') for x in 'abcdefgh']
        S.select = self.fake_select([['a', 'b', 'c', 'd'], ['e', 'f', 'g']])
        sel = S.select_fresh(pool, state)
        self.assertEqual(S.card_signature(sel['picks']), ['e', 'f', 'g'])

    def test_rejected_ids_are_withheld_from_the_retry(self):
        state = {'recent_cards': [['a', 'b', 'c', 'd']]}
        pool = [f(x, 'spending') for x in 'abcdefgh']
        S.select = self.fake_select([['a', 'b', 'c', 'd'], ['e', 'f', 'g']])
        S.select_fresh(pool, state)
        self.assertEqual(self.calls[1] & {'a', 'b', 'c', 'd'}, set())

    def test_three_shared_lines_is_a_repeat_even_when_the_fourth_moves(self):
        """The six spending cards of Aug 2026: same top three, fourth line
        moving. Verbatim matching missed this; the overlap rule must not."""
        state = {'recent_cards': [['karaoke', 'books', 'chicken', 'motels']]}
        pool = [f(x, 'spending') for x in
                ('karaoke', 'books', 'chicken', 'motels', 'billiards', 'pcbang',
                 'pets', 'cafes')]
        S.select = self.fake_select([
            ['karaoke', 'books', 'chicken', 'billiards'],   # 3 shared: reject
            ['pcbang', 'pets', 'cafes'],
        ])
        sel = S.select_fresh(pool, state)
        self.assertEqual(S.card_signature(sel['picks']), ['cafes', 'pcbang', 'pets'])

    def test_two_shared_lines_is_allowed(self):
        state = {'recent_cards': [['a', 'b', 'c', 'd']]}
        pool = [f(x, 'crowd') for x in 'abcdefgh']
        S.select = self.fake_select([['a', 'b', 'e']])
        sel = S.select_fresh(pool, state)
        self.assertEqual(S.card_signature(sel['picks']), ['a', 'b', 'e'])
        self.assertEqual(len(self.calls), 1)          # no wasted model call

    def test_gives_up_and_posts_rather_than_skipping_the_slot(self):
        state = {'recent_cards': [['a', 'b', 'c']]}
        pool = [f(x, 'crowd') for x in 'abc']
        S.select = self.fake_select([['a', 'b', 'c']] * S.SELECT_RETRIES)
        sel = S.select_fresh(pool, state)
        self.assertEqual(S.card_signature(sel['picks']), ['a', 'b', 'c'])
        self.assertEqual(len(self.calls), S.SELECT_RETRIES)

    def test_promoted_vein_only_rejects_verbatim(self):
        """A promoted vein's pool is deliberately tiny, so the overlap rule
        would reject every card it can build. strict=False must let a 2-of-3
        overlap through, but still catch an exact repeat."""
        state = {'recent_cards': [['c1', 'c2', 'c3']]}
        pool = [f(x, 'culture') for x in ('c1', 'c2', 'c3', 'c4')]
        S.select = self.fake_select([['c1', 'c2', 'c3'], ['c1', 'c2', 'c4']])
        sel = S.select_fresh(pool, state, strict=False)
        self.assertEqual(S.card_signature(sel['picks']), ['c1', 'c2', 'c4'])

    def test_empty_state_is_no_obstacle(self):
        pool = [f(x, 'crowd') for x in 'abc']
        S.select = self.fake_select([['a', 'b', 'c']])
        sel = S.select_fresh(pool, {})
        self.assertEqual(S.card_signature(sel['picks']), ['a', 'b', 'c'])


class SpotlightFlatness(unittest.TestCase):
    """The check lives inside spotlight_facts, which needs a live API call, so
    replay the rule itself over the values the two dead cards carried."""

    @staticmethod
    def verdict(nums):
        hi = max(nums)
        spread = (hi - min(nums)) / hi if hi else 0.0
        return (len(set(nums)) >= S.SPOTLIGHT_MIN_DISTINCT
                and spread >= S.SPOTLIGHT_MIN_SPREAD)

    def test_rejects_the_two_cards_that_prompted_this(self):
        self.assertFalse(self.verdict([11000, 11000, 11000, 6250]))   # Yeonnam-dong
        self.assertFalse(self.verdict([3250, 3250, 3750, 3250]))      # Haebangchon

    def test_keeps_the_tightest_real_keeper(self):
        self.assertTrue(self.verdict([8750, 8250, 4750]))             # Bukchon
        self.assertTrue(self.verdict([13000, 13000, 25000, 11000]))   # Sadang

    def test_rejects_three_distinct_but_near_equal_values(self):
        self.assertFalse(self.verdict([11000, 11050, 11100]))

    def test_keeps_the_smallest_place_on_the_roster(self):
        self.assertTrue(self.verdict([550, 475, 450, 150]))           # Nodeul


class NationCardNamesTheMetric(unittest.TestCase):
    """A nation card's row labels are bare place names, so the card must state
    what is measured. When the opener does not (the generic "Seoul and the
    nation"), the metric rides the masthead subtitle — above the rows — and is
    NOT left only in the source-reply credit line, where a reader of the card
    never sees it. When the opener already names the metric, nothing is added.
    Regression guard for the 21 Aug 2026 birth-rate card that shipped four bare
    numbers with no label."""

    @staticmethod
    def _nation_sel_pool(opener_en, opener_ko, pair, rows):
        # rows: [(iso, place_en, place_ko, value)], value language-neutral.
        pool = [S.fact(f'nation_{pair}_{iso}', 'nation', en, v, v,
                       pair=f'nation_{pair}', year='2024')
                for iso, en, ko, v in rows]
        sel = {'opener_en': opener_en, 'opener_ko': opener_ko,
               'opener_emoji': '👶',
               'picks': [{'id': f'nation_{pair}_{iso}', 'label_en': en,
                          'label_ko': ko, 'emoji': ''}
                         for iso, en, ko, v in rows]}
        return sel, pool

    FERTILITY = [('USA', 'United States', '미국', '1.63'),
                 ('JPN', 'Japan', '일본', '1.15'),
                 ('KOR', 'South Korea', '대한민국', '0.75'),
                 ('SEOUL', 'Seoul', '서울', '0.58')]

    def test_generic_opener_lifts_metric_onto_the_card(self):
        sel, pool = self._nation_sel_pool(
            'Seoul and the nation', '서울과 전국', 'fertility', self.FERTILITY)
        c = S.compose(sel, pool)
        # On the card face, above the rows:
        self.assertEqual(c['dateline_en'], 'Births per woman')
        self.assertEqual(c['dateline_ko'], '여성 1명당 출생아 수')
        # Not duplicated in the credit line...
        self.assertNotIn('Births per woman', c['src_en'])
        # ...and present exactly once in the alt text (so screen readers get it).
        self.assertEqual(c['en_body'].count('Births per woman'), 1)
        # The scope/vintage stays in the footnote, unchanged.
        self.assertEqual(c['note_en'], 'Seoul against whole countries, 2024')

    def test_opener_that_names_the_metric_adds_nothing(self):
        sel, pool = self._nation_sel_pool(
            'Births per woman', '여성 1명당 출생아 수', 'fertility', self.FERTILITY)
        c = S.compose(sel, pool)
        self.assertEqual(c['dateline_en'], '')      # no subtitle — opener said it
        self.assertEqual(c['dateline_ko'], '')
        self.assertNotIn('Births per woman', c['src_en'])  # never in the credit


class ScopedVeinHeadsItsOwnGroup(unittest.TestCase):
    """A scope must never fly over lines it does not describe.

    Regression guard for the card posted 22 Aug 2026, which crossed Seoul
    Library membership with the live crowd and came out as:

        Seoul by the numbers:
        60s: 14,003
        Estimated crowd in Gwanghwamun right now: 11,000
        Teens: 10,917
        Seoul Station: 7,750
        Crowds are KT-estimated · Members of Seoul Library

    "60s" and "Teens" count library members, and the only thing on the card
    saying so sat in the footnote next to a KT caveat, where it reads as
    attribution. The vein carries "own post, never mixed" for exactly this
    reason, but a cross-vein collision overrides that rule and takes the opener
    generic, so the opener could not carry the meaning either.

    The fix is NOT to fly the descriptor as a masthead: it would then sit above
    the Gwanghwamun crowd line and claim it as library members too. It heads its
    own group, over its own lines.
    """

    LIBRARY = [('library_60', '60s', '60대', '14,003', 14003),
               ('library_10', 'Teens', '10대', '10,917', 10917)]
    CROWD = [('crowd_Gwanghwamun', 'Estimated crowd in Gwanghwamun right now',
              '지금 광화문의 추정 인파', '11,000', 11000),
             ('crowd_Seoul Station', 'Estimated crowd at Seoul Station right now',
              '지금 서울역의 추정 인파', '7,750', 7750)]

    def _sel_pool(self, rows):
        pool, picks = [], []
        for fid, en, ko, v, n in rows:
            cat = fid.split('_')[0]
            pool.append(S.fact(fid, cat, en, v, v, pair=f'{cat}_pair',
                               estimated=(cat == 'crowd'), pin=(cat == 'library'),
                               num=n, unit='people', label_ko=None))
            picks.append({'id': fid, 'label_en': en, 'label_ko': ko, 'emoji': ''})
        return ({'opener_en': 'Seoul by the numbers', 'opener_ko': '숫자로 보는 서울',
                 'opener_emoji': '🏙️', 'picks': picks}, pool)

    def _subheads(self, items):
        return [it['subhead'] for it in items if 'subhead' in it]

    def _rows_under(self, items, subhead):
        out, on = [], False
        for it in items:
            if 'subhead' in it:
                on = it['subhead'] == subhead
                continue
            if on:
                out.append(it['label'])
        return out

    def test_descriptor_heads_its_own_lines_not_the_card(self):
        c = S.compose(*self._sel_pool(self.LIBRARY + self.CROWD))
        self.assertTrue(c['grouped'])
        self.assertEqual(self._subheads(c['items_en']),
                         ['Members of Seoul Library', 'Right now'])
        # Each subhead covers ITS OWN lines and no others. This is the assertion
        # that a masthead lift would fail.
        self.assertEqual(self._rows_under(c['items_en'], 'Members of Seoul Library'),
                         ['60s', 'Teens'])
        self.assertEqual(len(self._rows_under(c['items_en'], 'Right now')), 2)
        # Off the masthead: _card_payload suppresses it on a grouped card, so a
        # non-empty dateline here would print the scope over all four lines.
        self.assertEqual(S._card_payload(c, 'en')[3], '')

    def test_descriptor_leaves_the_footnote_when_it_heads_a_group(self):
        c = S.compose(*self._sel_pool(self.LIBRARY + self.CROWD))
        self.assertNotIn('Members of Seoul Library', c['note_en'])
        self.assertNotIn('서울도서관 등록 회원', c['note_ko'])
        # The KT caveat is a warning about the numbers, not a key to them, and
        # stays where it was.
        self.assertIn('KT', c['note_en'])
        # Once, on the card face, in the alt text a screen reader gets.
        self.assertEqual(c['en_body'].count('Members of Seoul Library'), 1)

    def test_korean_card_groups_too(self):
        c = S.compose(*self._sel_pool(self.LIBRARY + self.CROWD))
        self.assertEqual(self._subheads(c['items_ko']),
                         ['서울도서관 등록 회원', '지금'])
        self.assertEqual(c['ko_body'].count('서울도서관 등록 회원'), 1)

    def test_own_post_library_card_is_unchanged(self):
        """No live lines, no grouping: the opener carries the meaning there."""
        c = S.compose(*self._sel_pool(self.LIBRARY + [
            ('library_20', '20s', '20대', '9,800', 9800)]))
        self.assertFalse(c['grouped'])
        self.assertEqual(self._subheads(c['items_en']), [])
        self.assertIn('Members of Seoul Library', c['note_en'])

    def test_a_period_vein_heads_a_group_instead_of_the_masthead(self):
        """Same fault, other route in: infant/daynight/water/price carry their
        scope as a PERIOD, which lifted to the masthead over the live lines.
        The age band is the case that bites — it is the only thing saying which
        of the four child series the bare years belong to."""
        S.INFANT_PERIOD['en'], S.INFANT_PERIOD['ko'] = 'Under-ones', '0세'
        try:
            rows = [('infant_2016', '2016', '2016년', '75,536', 75536),
                    ('infant_2025', '2025', '2025년', '41,600', 41600)]
            c = S.compose(*self._sel_pool(rows + self.CROWD))
            self.assertTrue(c['grouped'])
            self.assertEqual(self._subheads(c['items_en']),
                             ['Under-ones', 'Right now'])
            self.assertEqual(self._rows_under(c['items_en'], 'Under-ones'),
                             ['2016', '2025'])
            self.assertEqual(S._card_payload(c, 'en')[3], '')
        finally:
            S.INFANT_PERIOD['en'] = S.INFANT_PERIOD['ko'] = None

    def test_two_scoped_veins_do_not_group(self):
        """No single head is true, so nothing is promoted and every scope stays
        in the footnote: the old behaviour, which is merely cramped, not wrong."""
        rows = (self.LIBRARY[:1] + self.CROWD[:1] +
                [('complaint_2019', '2019', '2019년', '412,000', 412000),
                 ('complaint_2024', '2024', '2024년', '498,000', 498000)])
        c = S.compose(*self._sel_pool(rows))
        self.assertFalse(c['grouped'])
        self.assertIn('Members of Seoul Library', c['note_en'])
        self.assertIn('Reports to Seoul', c['note_en'])

    def test_the_two_scope_tables_cannot_drift(self):
        """DESCRIPTOR_SCOPES is the single source of the words; compose() reads
        it for the footnote and the subhead alike. If a vein is ever listed in
        one place only, the card says one thing and the footnote another."""
        for cat, (en, ko) in S.DESCRIPTOR_SCOPES.items():
            self.assertTrue(en and ko, f'{cat} is missing a language')
            self.assertIn(cat, S.SCOPED_CATS,
                          f'{cat} has a descriptor but would never group')


class TourismBoxofficeCrossPairGroupsBySpan(unittest.TestCase):
    """A tourism+boxoffice CROSS_PAIR puts a whole month's visitors beside one
    day's admissions on the same card. Flagged live, 30 Aug 2026
    (https://bsky.app/profile/seoul-index.bsky.social/post/3mudkt6v5d42v):
    neither span could be lifted to a single masthead (they disagree), so both
    sat inline in one footnote line with nothing tying either one to the lines
    it actually covers. period_grouped fixes this the way metric_grouped and
    the live+scoped "grouped" layout already fix the same shape of problem
    elsewhere: draw each span once, as a subhead over its own lines.
    """

    def _pool_and_sel(self):
        S.TOUR_M['en'], S.TOUR_M['ko'] = 'June 2026', '2026년 6월'
        S.TOUR_M['month_en'], S.TOUR_M['month_ko'] = 'June', '6월'
        S.BOXOFFICE_D['en'], S.BOXOFFICE_D['ko'] = '30 August', '8월 30일'
        S.BOXOFFICE_D['month_en'], S.BOXOFFICE_D['month_ko'] = 'August', '8월'
        pool = [
            S.fact('bo_1', 'boxoffice', '"The Odyssey"', '92,090', '92,090',
                  label_ko='"오디세이"', pin=True, num=92090, unit='people'),
            S.fact('tour_aq', 'tourism', 'Visitors to the Lotte World Aquarium',
                  '87,648', '87,648', label_ko='롯데월드 아쿠아리움 방문객',
                  pin=True, num=87648, unit='people'),
            S.fact('tour_sky', 'tourism', 'Visitors to Seoul Sky', '86,492',
                  '86,492', label_ko='서울스카이 방문객', pin=True,
                  num=86492, unit='people'),
        ]
        picks = [{'id': f['id'], 'emoji': ''} for f in pool]
        sel = {'opener_en': 'Seoul by the numbers',
              'opener_ko': '숫자로 보는 서울', 'opener_emoji': '', 'picks': picks}
        return sel, pool

    def tearDown(self):
        for d in (S.TOUR_M, S.BOXOFFICE_D):
            for k in list(d):
                d[k] = None

    def _subheads(self, items):
        return [it['subhead'] for it in items if 'subhead' in it]

    def _rows_under(self, items, subhead):
        out, on = [], False
        for it in items:
            if 'subhead' in it:
                on = it['subhead'] == subhead
                continue
            if on:
                out.append(it['label'])
        return out

    def test_the_card_is_recognised_as_a_cross_pair_and_groups_by_span(self):
        c = S.compose(*self._pool_and_sel())
        self.assertTrue(c['period_grouped'])
        self.assertFalse(c['grouped'], 'not the live+scoped mechanism')

    def test_each_span_heads_only_its_own_lines(self):
        c = S.compose(*self._pool_and_sel())
        self.assertEqual(self._subheads(c['items_en']),
                         ['30 August', 'The entire month of June'])
        self.assertEqual(
            self._rows_under(c['items_en'], '30 August'),
            ['Admissions, "The Odyssey"'])
        self.assertEqual(
            self._rows_under(c['items_en'], 'The entire month of June'),
            ['Visitors to the Lotte World Aquarium',
             'Visitors to Seoul Sky'])

    def test_korean_card_groups_too(self):
        c = S.compose(*self._pool_and_sel())
        self.assertEqual(self._subheads(c['items_ko']),
                         ['8월 30일', '6월 한 달 전체'])

    def test_the_span_is_not_also_left_in_the_footnote(self):
        """It now heads a group instead — saying it twice is the fault this
        exists to fix, in a new shape. The exact day DOES legitimately appear
        now (in the subhead itself, via en_body/ko_body), just not a second
        time in the footnote proper (note_en/note_ko)."""
        c = S.compose(*self._pool_and_sel())
        self.assertNotIn('Paid-admission sites', c['note_en'])
        self.assertNotIn('most-watched', c['note_en'])
        self.assertNotIn('30 August', c['note_en'])
        self.assertNotIn('June 2026', c['note_en'])
        self.assertEqual(c['en_body'].count('30 August'), 1)
        self.assertEqual(c['en_body'].count('The entire month of June'), 1)

    def test_no_masthead_flies_over_the_whole_card(self):
        """Neither span is true of every line, so lifting either as a single
        dateline would misstate the other vein's lines — the same reasoning
        _card_payload already applies to a live+scoped grouped card."""
        c = S.compose(*self._pool_and_sel())
        self.assertEqual(S._card_payload(c, 'en')[3], '')
        self.assertEqual(S._card_payload(c, 'ko')[3], '')

    def test_check_masthead_is_not_fooled_by_the_grouping(self):
        """check_masthead's own guard (grouped or period_grouped) must see
        this card as already explained, not flag it for having no masthead."""
        c = S.compose(*self._pool_and_sel())
        found = S.check_masthead(c['lines'], c['dateline_en'], c['dateline_ko'],
                                 c['grouped'] or c['period_grouped'],
                                 log=lambda m: None)
        self.assertEqual(found, [])


class LibraryRatioOnTheCard(unittest.TestCase):
    """The "1 in N" beside a library count is a ratio between two publishers.

    The numerator is Seoul Library's members and the denominator is KOSIS's
    registered population of that age — and the two do NOT cover the same
    people: 서울도서관's 준회원 class is open to any Korean national with no Seoul
    connection at all, and 정회원 covers people who work or study in Seoul while
    living elsewhere. So the card may state the ratio and must never state a
    share, which is what "Members need not live in Seoul" is doing in the
    footnote. A card that ships the ratio without it is misreporting, and
    nothing in the rendered output would look wrong.
    """

    RATIO = [('library_30', '30s', '30대', '70,339 (1 in 21)', 70339),
             ('library_10', 'Teens', '10대', '10,921 (1 in 65)', 10921),
             ('library_80', '80s', '80대', '684 (1 in 551)', 684)]
    BARE = [('library_30', '30s', '30대', '70,339', 70339),
            ('library_10', 'Teens', '10대', '10,921', 10921),
            ('library_80', '80s', '80대', '684', 684)]

    def setUp(self):
        S.LIBRARY_POP['en'], S.LIBRARY_POP['ko'] = 'July 2026', '2026년 7월'

    def tearDown(self):
        S.LIBRARY_POP['en'] = S.LIBRARY_POP['ko'] = ''

    def _compose(self, rows):
        pool, picks = [], []
        for fid, en, ko, v, n in rows:
            cat = fid.split('_')[0]
            pool.append(S.fact(fid, cat, en, v, v, pair=f'{cat}_pair',
                               estimated=(cat == 'crowd'), pin=(cat == 'library'),
                               num=n, unit='people'))
            picks.append({'id': fid, 'label_en': en, 'label_ko': ko, 'emoji': ''})
        return S.compose({'opener_en': 'Who holds a card at Seoul Library',
                          'opener_ko': '서울도서관 회원증을 가진 사람',
                          'opener_emoji': '📚', 'picks': picks}, pool)

    def test_the_ratio_credits_kosis_as_a_national_card_does(self):
        c = self._compose(self.RATIO)
        self.assertIn('kosis.kr', c['src_en'])
        self.assertIn('Statistics Korea', c['src_en'])
        self.assertIn('통계청', c['src_ko'])

    def test_the_footnote_says_what_is_divided_by_and_who_is_counted(self):
        c = self._compose(self.RATIO)
        self.assertIn('registered population that age', c['note_en'])
        self.assertIn('July 2026', c['note_en'])
        self.assertIn('Members need not live in Seoul', c['note_en'])
        self.assertIn('주민등록인구 대비', c['note_ko'])
        self.assertIn('서울 거주자에 한정되지 않음', c['note_ko'])

    def test_the_population_month_never_becomes_the_cards_dateline(self):
        """The vintage rides in the DESCRIPTOR, not the period slot.

        A period there would be the card's only one, lift to the masthead, and
        date the MEMBERSHIP figures as July 2026 — and the membership service
        publishes no date at all, so that masthead would be an invention.
        """
        c = self._compose(self.RATIO)
        self.assertEqual(c.get('dateline_en', ''), '')
        self.assertEqual(S._card_payload(c, 'en')[3], '')
        self.assertEqual(S._card_payload(c, 'ko')[3], '')

    def test_the_card_still_sorts_by_member_count(self):
        """_sortkey strips the trailing parenthetical, so the size order that
        every library card has always had survives the ratio."""
        labels = [it['label'] for it in self._compose(self.RATIO)['items_en']
                  if 'subhead' not in it]
        self.assertEqual(labels, ['30s', 'Teens', '80s'])

    def test_no_ratio_means_no_kosis_credit_and_no_claim_about_one(self):
        """A KOSIS outage leaves the same library lines with bare counts. The
        credit and both notes must go with the ratio, not linger from it."""
        S.LIBRARY_POP['en'] = S.LIBRARY_POP['ko'] = ''
        c = self._compose(self.BARE)
        self.assertNotIn('kosis.kr', c['src_en'])
        self.assertNotIn('Statistics Korea', c['src_en'])
        self.assertNotIn('registered population', c['note_en'])
        self.assertNotIn('Members need not live in Seoul', c['note_en'])
        self.assertIn('Members of Seoul Library', c['note_en'])   # unchanged


class ThenAndNowGroupsByMetric(unittest.TestCase):
    """The fifty-year weather pairs draw the metric once as a group subhead with
    the periods bolded beneath it (26 August 2026).

    ⚠️ THE REFUSAL TESTS ARE THE POINT, not the one that groups. A subhead over a
    single row is a heading over nothing, and one un-split row sitting beside
    grouped ones reads as an orphan — both are silently ugly rather than loudly
    broken, so nothing would report them. The flat layout is always correct, just
    longer, which is why it is what the card falls back to.

    No network and no model call: the facts are built by hand in the shape
    kma_facts() emits, so this pins the CONTRACT between the harvester and
    compose() rather than a live sky.
    """

    @staticmethod
    def _wx(fid, head, period, value, head_ko=None, period_ko=None):
        return S.fact(fid, 'weather', f'{head}, {period}', value, value,
                      pin=True, label_ko=f'{head_ko or head}, {period_ko or period}',
                      head_en=head, head_ko=head_ko or head,
                      period_en=period, period_ko=period_ko or period)

    TROPICAL = 'Nights never below 25°C (77°F)'
    SWELTER = 'Days of 33°C (91°F) or more'

    def _compose(self, pool):
        sel = {'opener_en': '50 years apart', 'opener_ko': '50년의 차이',
               'opener_emoji': '',
               'picks': [{'id': f['id'], 'label_en': '', 'label_ko': '',
                          'emoji': ''} for f in pool]}
        return S.compose(sel, pool)

    def _pairs(self):
        # Deliberately in an order the VALUE sort would scramble: 23, 15, 2, 0
        # interleaves the two pairs, which is what shipped on 26 August 2026.
        return [self._wx('wx_s_tropical_now', self.TROPICAL, 'Summer 2026', '23'),
                self._wx('wx_s_swelter_now', self.SWELTER, 'Summer 2026', '15'),
                self._wx('wx_s_swelter_then', self.SWELTER, 'Summer 1976', '2'),
                self._wx('wx_s_tropical_then', self.TROPICAL, 'Summer 1976', '0')]

    def test_metric_heads_its_group_and_periods_bold_beneath_it(self):
        items = self._compose(self._pairs())['items_en']
        self.assertEqual(
            [it.get('subhead') or it['label'] for it in items],
            [self.TROPICAL, 'Summer 2026', 'Summer 1976',
             self.SWELTER, 'Summer 2026', 'Summer 1976'])
        rows = [it for it in items if 'subhead' not in it]
        self.assertTrue(all(r['bold'] for r in rows))
        self.assertEqual([r['value'] for r in rows], ['23', '0', '15', '2'])

    def test_pairs_are_adjacent_and_ordered_newest_first_not_by_value(self):
        """⚠️ The regression this exists for. Value-sorted, the card read
        23, 15, 2, 0 — neither pair beside itself. And the order must NOT come
        from the values even now: which of 1976 and 2026 is larger is the thing
        the card is asking, so letting it choose the order answers the question
        in the layout before the reader has read a number."""
        pool = self._pairs()
        # 1976 hotter than 2026 on the swelter pair: a value sort would put the
        # older year first in that group alone, and the two groups would then
        # disagree about which way time runs down the card.
        pool[1]['value_en'] = pool[1]['value_ko'] = '2'
        pool[2]['value_en'] = pool[2]['value_ko'] = '15'
        rows = [it for it in self._compose(pool)['items_en']
                if 'subhead' not in it]
        self.assertEqual([r['label'] for r in rows],
                         ['Summer 2026', 'Summer 1976',
                          'Summer 2026', 'Summer 1976'])

    def test_korean_card_groups_on_its_own_words(self):
        ko_trop, ko_swel = '최저기온 25°C 이상인 날', '최고기온 33°C 이상인 날'
        pool = [self._wx('a_now', self.TROPICAL, 'Summer 2026', '23',
                         head_ko=ko_trop, period_ko='2026년 여름'),
                self._wx('a_then', self.TROPICAL, 'Summer 1976', '0',
                         head_ko=ko_trop, period_ko='1976년 여름'),
                self._wx('b_now', self.SWELTER, 'Summer 2026', '15',
                         head_ko=ko_swel, period_ko='2026년 여름'),
                self._wx('b_then', self.SWELTER, 'Summer 1976', '2',
                         head_ko=ko_swel, period_ko='1976년 여름')]
        items = self._compose(pool)['items_ko']
        self.assertEqual([it.get('subhead') or it['label'] for it in items],
                         [ko_trop, '2026년 여름', '1976년 여름',
                          ko_swel, '2026년 여름', '1976년 여름'])

    def test_a_metric_with_one_line_refuses_to_group(self):
        """A subhead over a single row is a heading over nothing."""
        # tropical now, swelter now, hot now, wet now: four metrics, one line
        # each, so every subhead would head a single row.
        pool = self._pairs()[:2] + [
            self._wx('wx_s_hot_now', 'Hottest day', 'Summer 2026', '38.0°C'),
            self._wx('wx_s_wet_now', 'Wettest day', 'Summer 2026', '119.7mm')]
        items = self._compose(pool)['items_en']
        self.assertFalse(any('subhead' in it for it in items))
        self.assertIn('Summer 2026', items[0]['label'])   # full label, unsplit

    def test_one_unsplit_line_stops_the_whole_card_grouping(self):
        """wx_yday_* carry no metric/period split — there is no sibling year to
        set them against. One of them on the card and the layout goes flat, so a
        bare row never sits orphaned beside two grouped ones."""
        pool = self._pairs() + [S.fact('wx_yday_hi', 'weather',
                                       "Seoul's high yesterday", '32.7°C',
                                       '32.7°C', pin=True)]
        items = self._compose(pool)['items_en']
        self.assertFalse(any('subhead' in it for it in items))

    def test_two_unsplit_lines_still_stop_the_grouping(self):
        """⚠️ The case the every-metric-has-two-rows guard CANNOT see. Two
        yesterday lines share the same absent head, so they count as a group of
        two and sail past that guard — and the card then draws a subhead with no
        text in it, over rows whose label is None. Nothing else would report
        that, because an empty subhead renders as an empty line."""
        pool = self._pairs()[:2] + [
            S.fact('wx_yday_hi', 'weather', "Seoul's high yesterday",
                   '32.7°C', '32.7°C', pin=True),
            S.fact('wx_yday_lo', 'weather', "Seoul's low yesterday",
                   '25.2°C', '25.2°C', pin=True)]
        items = self._compose(pool)['items_en']
        self.assertFalse(any('subhead' in it for it in items))
        self.assertTrue(all(it.get('label') for it in items))

    def test_the_whole_label_survives_for_the_checker_and_the_fallback(self):
        """The card draws the split; `lines` keeps the self-describing string.
        It is what check_labels judges and what a failed render posts as text,
        and a bare "Summer 1976: 0" in either place measures nothing."""
        c = self._compose(self._pairs())
        self.assertIn(f'{self.TROPICAL}, Summer 1976',
                      [l['label_en'] for l in c['lines']])


class BoldPeriodRow(unittest.TestCase):
    """The renderer half of the metric grouping. Pure string work — no Chrome,
    no screenshot — so the one line that turns compose()'s `bold` flag into
    markup is covered rather than trusted."""

    def test_a_flagged_row_bolds_its_whole_label(self):
        html = C._row_html({'emoji': '', 'label': 'Summer 1976', 'value': '0',
                            'bold': True})
        self.assertIn('<b>Summer 1976</b>', html)

    def test_an_unflagged_row_is_not_bolded(self):
        html = C._row_html({'emoji': '', 'label': 'Summer 1976', 'value': '0'})
        self.assertNotIn('<b>', html)

    def test_a_leading_year_still_bolds_just_the_year(self):
        """The older _YEAR_LEAD rule owns "2026: The Odyssey" and must keep it:
        there the year is the scannable part and the title is not."""
        html = C._row_html({'emoji': '', 'label': '2026: The Odyssey',
                            'value': '1', 'bold': True})
        self.assertIn('<b>2026:</b>', html)
        self.assertNotIn('<b>2026: The Odyssey</b>', html)


class WeatherCreditNamesTheStation(unittest.TestCase):
    """"(108)" became words, and the span joined it, on the card footnote
    (26 August 2026)."""

    def _compose(self, pool):
        sel = {'opener_en': 'Yesterday', 'opener_ko': '어제', 'opener_emoji': '',
               'picks': [{'id': f['id'], 'label_en': '', 'label_ko': '',
                          'emoji': ''} for f in pool]}
        return S.compose(sel, pool)

    # Three, because compose() refuses a card of fewer.
    YDAY = [('wx_yday_hi', "Seoul's high yesterday", '32.7°C'),
            ('wx_yday_lo', "Seoul's low yesterday", '25.2°C'),
            ('wx_yday_rain', 'Rain on Seoul yesterday', '20.1mm')]

    def _yday_pool(self):
        return [S.fact(i, 'weather', en, v, v, pin=True) for i, en, v in self.YDAY]

    def test_the_station_is_named_in_words_on_the_card(self):
        c = self._compose(self._yday_pool())
        self.assertIn('reference station', c['note_en'])
        self.assertIn('대표 관측소', c['note_ko'])
        self.assertNotIn('108', c['note_en'])   # a station index number names
        self.assertNotIn('108', c['note_ko'])   # it only to someone who knew

    def test_it_is_not_repeated_on_the_source_reply(self):
        """The reply sits one post under the image. ⚠️ This is the repo’s
        standing rule, the same one that keeps the KT-estimate caveat off the
        reply: whatever the card footnote says, the reply must not say again."""
        c = self._compose(self._yday_pool())
        self.assertNotIn('reference station', c['src_en'])
        self.assertNotIn('관측소', c['src_ko'])
        self.assertEqual(c['src_en'], 'Source: data.kma.go.kr · KMA')

    def test_the_span_never_becomes_the_red_masthead_line(self):
        """⚠️ The trap this vein sits one character away from. A scope entry
        carrying a PERIOD is promoted to the dateline, which draws it in red
        under the title in the same weight as the metric subheads: three reds
        competing, with a date range leading the card. Judged by eye and
        rejected on 26 August 2026. The period slot must stay None, and nothing
        about that is visible at the call site."""
        S.WX_SEASON['en'], S.WX_SEASON['ko'] = '1 June–25 August', '6월 1일–8월 25일'
        c = self._compose(self._yday_pool() + self._summer())
        self.assertEqual(c['dateline_en'], '')
        self.assertEqual(c['dateline_ko'], '')
        self.assertIn('1 June–25 August', c['note_en'])

    def test_the_observing_year_is_1907_and_reaches_the_reader(self):
        """⚠️ Not 1904 — that is when Korea's network began, not this station.
        Settled against the bot's own source: station 108's first daily row in
        the ASOS API is 1907-10-01 and every span before it returns NO_DATA. It
        is published prose now, so a wrong year is a wrong claim in the feed."""
        self.assertEqual(S.WX_OBSERVING_SINCE, 1907)
        c = self._compose(self._yday_pool())
        self.assertIn('observing since 1907', c['note_en'])
        self.assertIn('1907년 관측 개시', c['note_ko'])

    @staticmethod
    def _summer():
        return [S.fact('wx_s_swelter_now', 'weather',
                       'Days of 33°C (91°F) or more, 1 June–25 August 2026',
                       '15', '15', pin=True,
                       label_ko='최고기온 33°C 이상인 날, 2026년 6월 1일–8월 25일',
                       head_en='Days of 33°C (91°F) or more',
                       head_ko='최고기온 33°C 이상인 날',
                       period_en='Summer 2026', period_ko='2026년 여름')]

    def test_the_summer_span_rides_only_when_a_summer_line_does(self):
        """A row saying "Summer 2026" needs the window spelled out somewhere,
        and the window is still growing. But a card of last month's readings
        carries no summer row, and a span covering none of its figures would be
        worse than no span at all."""
        S.WX_SEASON['en'], S.WX_SEASON['ko'] = '1 June–25 August', '6월 1일–8월 25일'
        self.assertNotIn('1 June–25 August',
                         self._compose(self._yday_pool())['note_en'])
        c = self._compose(self._yday_pool() + self._summer())
        self.assertIn('1 June–25 August', c['note_en'])
        self.assertIn('6월 1일–8월 25일', c['note_ko'])

    def test_the_span_leads_the_footnote_and_the_station_follows(self):
        """Reading order on the card: which days, then whose instrument."""
        S.WX_SEASON['en'], S.WX_SEASON['ko'] = '1 June–25 August', '6월 1일–8월 25일'
        c = self._compose(self._yday_pool() + self._summer())
        self.assertEqual(
            c['note_en'],
            '1 June–25 August · Seoul’s reference station, observing since 1907')



class BoldTheVariablePlace(unittest.TestCase):
    """A card whose rows are one metric read at four places bolds the PLACE and
    leaves the shared wording alone (26 August 2026). Same rule as the
    then-and-now subheads: bold what changes.

    ⚠️ THE REFUSALS ARE THE POINT. Bold means "this is what differs", so a card
    where it lands on every row, or on rows that differ in more than the place,
    is worse than a card with no bold at all — and both render perfectly, so
    nothing would report them.
    """

    @staticmethod
    def _p(fid, cat, label, value, en, ko, label_ko=None):
        return S.fact(fid, cat, label, value, value, pin=True,
                      label_ko=label_ko or label, place_en=en, place_ko=ko)

    def _compose(self, pool, opener='Seoul, right now'):
        sel = {'opener_en': opener, 'opener_ko': '지금 서울은', 'opener_emoji': '',
               'picks': [{'id': f['id'], 'label_en': '', 'label_ko': '',
                          'emoji': ''} for f in pool]}
        return S.compose(sel, pool)

    CROWD = [('Gangnam Station', '강남역', '81,000'),
             ('Seoul Station', '서울역', '23,000'),
             ('Gyeongbokgung', '경복궁', '1,750')]

    def _crowd(self):
        return [self._p(f'crowd_{en}', 'crowd', f'Estimated crowd, {en}', v,
                        en, ko, label_ko=f'{ko} 추정 인파')
                for en, ko, v in self.CROWD]

    def test_the_place_bolds_and_the_shared_wording_does_not(self):
        items = self._compose(self._crowd())['items_en']
        self.assertEqual([it['emph'] for it in items],
                         ['Gangnam Station', 'Seoul Station', 'Gyeongbokgung'])

    def test_bare_name_rows_are_never_bolded(self):
        """⚠️ The guard that makes bold mean anything. River and water label
        their rows with BARE NAMES, so cutting the place out leaves nothing and
        every remainder is trivially equal: the test passes and the card comes
        out entirely in bold, which is the same as no bold, only heavier."""
        pool = [self._p('river_air', 'river', 'The air', '31.3°C',
                        'The air', '기온'),
                self._p('river_a', 'river', 'The Anyangcheon', '28.5°C',
                        'The Anyangcheon', '안양천'),
                self._p('river_h', 'river', 'The Han at Seonyu', '27.2°C',
                        'The Han at Seonyu', '선유 한강')]
        items = self._compose(pool, 'Water and air in Seoul')['items_en']
        self.assertFalse(any('emph' in it for it in items))

    def test_rows_differing_in_more_than_the_place_are_not_bolded(self):
        """Same vein, different card. Four "Visitors to X" qualify; the moment a
        row measures something else the place is no longer the variable, and
        bolding it would point at the wrong difference."""
        pool = [self._p('tour_a', 'tourism', 'Visitors to Gyeongbokgung',
                        '87,648', 'Gyeongbokgung', '경복궁'),
                self._p('tour_b', 'tourism', 'Visitors to Seoul Sky',
                        '86,492', 'Seoul Sky', '서울스카이'),
                self._p('tour_c', 'tourism', 'Foreign visitors to Bukchon',
                        '12,004', 'Bukchon', '북촌')]
        self.assertFalse(any('emph' in it for it in
                             self._compose(pool)['items_en']))

    def test_a_line_with_no_place_at_all_takes_the_card_back_to_plain(self):
        """⚠️ Cards mix veins. A crowd line can sit beside a citywide total that
        is about no place whatever, and there the place is not the variable —
        one row would have nothing to bold while three did. Without the
        all-places precondition this does not merely bold wrongly, it reaches a
        None substring test and raises, which on a scheduled run is a card that
        never posts."""
        pool = self._crowd() + [
            S.fact('prop_filed', 'property', 'Apartment sales filed citywide',
                   '4,001', '4,001', pin=True)]
        items = self._compose(pool)['items_en']
        self.assertFalse(any('emph' in it for it in items))

    def test_all_rows_or_none(self):
        """A label that does not contain its own place (the selector reworded
        it) takes the whole card back to plain. Three bolded rows and one not
        reads as a claim about the fourth."""
        pool = self._crowd()
        pool[1]['label_en'] = 'Estimated crowd at the main railway terminus'
        self.assertFalse(any('emph' in it for it in
                             self._compose(pool)['items_en']))

    def test_each_language_is_judged_on_its_own_labels(self):
        """⚠️ The Korean labels are the selector's, so they can disagree with
        the English. A card bolded in one language and not the other is fine;
        a card half-bolded in one is not."""
        pool = self._crowd()
        pool[2]['label_ko'] = '고궁 인파'          # place absent from the label
        c = self._compose(pool)
        self.assertTrue(all('emph' in it for it in c['items_en']))
        self.assertFalse(any('emph' in it for it in c['items_ko']))

    def test_korean_can_fail_the_test_while_holding_every_place(self):
        """⚠️ The sharper half of the same rule, and the one the check above
        cannot see: every Korean label CONTAINS its place, so the all-rows check
        passes, and only comparing the Korean remainders to each other catches
        that they are not the same sentence. Judge Korean off the English labels
        and this card bolds on a difference the reader is not looking at."""
        pool = self._crowd()
        pool[1]['label_ko'] = '서울역 인파'        # others read "…역 추정 인파"
        c = self._compose(pool)
        self.assertTrue(all('emph' in it for it in c['items_en']))
        self.assertFalse(any('emph' in it for it in c['items_ko']))


class CrowdLabelIsOneShape(unittest.TestCase):
    """⚠️ The rewording this replaced is in the feed: the 24 August card read
    "Estimated crowd, Gangnam Station", "Estimated crowd in Seoul Station right
    now" and "Estimated crowd at Nodeul Island the same minute" — three
    sentences for one metric, the last of them wrapping. The place has to sit in
    the same position on every row or it cannot be bolded, and the selector
    cannot be asked for that."""

    def test_the_pool_label_puts_the_place_last_and_pins_it(self):
        got = [{'en': 'Gangnam Station', 'ko': '강남역', 'mid': 81000,
                'visitor': '31.1', 'female': '48.0', 'twenties': '22.3'}]
        facts = []
        for g in got:                       # mirrors crowd_facts' own loop
            facts.append(S.fact(f'crowd_{g["en"]}', 'crowd',
                                f'Estimated crowd, {g["en"]}', '1', '1',
                                pin=True, place_en=g['en'], place_ko=g['ko']))
        self.assertEqual(facts[0]['label_en'], 'Estimated crowd, Gangnam Station')
        self.assertTrue(facts[0]['pin'])

    def test_the_live_harvester_agrees_with_that_shape(self):
        """Read off the module rather than restated here, so the two cannot
        drift: a reworded pool label must break this test, not the card."""
        import inspect
        src = inspect.getsource(S.crowd_facts)
        head = src.split('facts.append')[1]      # the main crowd fact alone
        self.assertIn("f'Estimated crowd, {g[\"en\"]}'", head)
        self.assertIn('place_en=g[\'en\']', head)
        # ⚠️ Unpinned, the selector rewords this line per row and the four
        # places stop lining up — which is the state the 24 August card shipped
        # in, and the reason the place cannot be bolded without it.
        self.assertIn('pin=True', head)
        # ⚠️ And `pin` covers ENGLISH ONLY. With label_ko left None the selector
        # translates each row on its own, so the Korean card keeps the four
        # shapes and never bolds: the EN twin fixed and the KO twin not.
        self.assertIn('label_ko=f\'{g["ko"]} 추정 인파\'', head)


class EmphRendersAsOneBoldRun(unittest.TestCase):
    def test_the_run_bolds_and_the_rest_does_not(self):
        html = C._row_html({'emoji': '', 'label': 'Estimated crowd, Seoul Station',
                            'value': '23,000', 'emph': 'Seoul Station'})
        self.assertIn('Estimated crowd, <b>Seoul Station</b>', html)

    def test_a_run_that_is_not_in_the_label_changes_nothing(self):
        """compose() takes the run from the harvester and the label can be the
        selector's rewrite, so a miss must be silent rather than an error."""
        html = C._row_html({'emoji': '', 'label': 'Estimated crowd downtown',
                            'value': '1', 'emph': 'Seoul Station'})
        self.assertNotIn('<b>', html)

    def test_a_place_that_also_appears_in_the_shared_wording_bolds_once(self):
        html = C._row_html({'emoji': '', 'label': 'Seoul Station, Seoul Station',
                            'value': '1', 'emph': 'Seoul Station'})
        self.assertEqual(html.count('<b>'), 1)



class GroupedCardStripsFramingEvenFromAPinnedLabel(unittest.TestCase):
    """⚠️ A regression introduced on 26 August 2026 by pinning the crowd label,
    and caught the same day. Pinning made the line exempt from
    _strip_live_frame, so a live+dated cross pair drew "Estimated crowd,
    Hongdae" under a "Right now" subhead over a footnote reading "Crowds are
    KT-estimated": the duplication that function exists to remove. 9 of the 79
    cards in card_history are crowd crossed with tourism or library, so this was
    roughly one card in nine."""

    def _pool(self):
        S.TOUR_M['en'], S.TOUR_M['ko'] = 'July 2026', '2026년 7월'
        crowd = [S.fact(f'crowd_{en}', 'crowd', f'Estimated crowd, {en}', v, v,
                        estimated=True, pin=True, label_ko=f'{ko} 추정 인파',
                        place_en=en, place_ko=ko,
                        num=int(v.replace(',', '')), unit='people')
                 for en, ko, v in (('Hongdae', '홍대', '77,000'),
                                   ('Jamsil', '잠실', '63,000'))]
        tour = [S.fact(f'tour_{ko}', 'tourism', f'Visitors to {en}', v, v,
                       num=int(v.replace(',', '')), unit='people',
                       place_en=en, place_ko=ko)
                for en, ko, v in (('Gyeongbokgung', '경복궁', '87,648'),
                                  ('Seoul Sky', '서울스카이', '86,492'))]
        return crowd + tour

    def _compose(self, pool):
        sel = {'opener_en': 'Seoul by the numbers', 'opener_ko': '숫자로 보는 서울',
               'opener_emoji': '',
               'picks': [{'id': f['id'], 'label_en': '', 'label_ko': '',
                          'emoji': ''} for f in pool]}
        return S.compose(sel, pool)

    def test_estimated_is_stripped_in_both_languages(self):
        c = self._compose(self._pool())
        self.assertTrue(c['grouped'])
        labels = [it.get('subhead') or it['label']
                  for it in c['items_en']] + [it.get('subhead') or it['label']
                                              for it in c['items_ko']]
        self.assertIn('Crowd, Hongdae', labels)
        self.assertIn('홍대 인파', labels)
        self.assertNotIn('Estimated crowd, Hongdae', labels)
        self.assertNotIn('홍대 추정 인파', labels)

    def test_the_card_still_says_it_once(self):
        """⚠️ The claim that makes stripping a PINNED label safe: this removes
        only framing the card states elsewhere. If the footnote ever stopped
        carrying the caveat, the strip would be deleting the only copy."""
        c = self._compose(self._pool())
        self.assertIn('KT-estimated', c['note_en'])
        self.assertIn('KT 추정', c['note_ko'])
        self.assertIn('Right now', [it.get('subhead') for it in c['items_en']])

    def test_a_dated_line_keeps_its_own_framing(self):
        """⚠️ Why the strip is confined to the LIVE lines. The subhead that
        makes "right now" redundant sits over the live group only, and the KT
        caveat belongs to estimated lines only — so on a dated line the same two
        words are the card's only copy, and stripping them deletes information
        rather than a duplicate. The selector writes these labels, so a dated
        line beginning "Estimated" is a wording it can produce at any time."""
        pool = self._pool()
        for f in pool:
            if f['cat'] == 'tourism':
                f['label_en'] = 'Estimated ' + f['label_en'].lower()
                f['pin'] = True
        labels = [it.get('subhead') or it['label']
                  for it in self._compose(pool)['items_en']]
        self.assertIn('Estimated visitors to gyeongbokgung', labels)

    def test_an_ungrouped_crowd_card_is_left_alone(self):
        """The plain four-place card keeps its full label: there is no subhead
        saying "Right now", so nothing else on it carries the framing."""
        pool = [f for f in self._pool() if f['cat'] == 'crowd']
        pool += [S.fact('crowd_Gwanghwamun', 'crowd',
                        'Estimated crowd, Gwanghwamun', '23,000', '23,000',
                        estimated=True, pin=True, label_ko='광화문 추정 인파',
                        place_en='Gwanghwamun', place_ko='광화문')]
        c = self._compose(pool)
        self.assertFalse(c['grouped'])
        self.assertEqual(c['lines'][0]['label_en'], 'Estimated crowd, Hongdae')
        self.assertTrue(all('emph' in it for it in c['items_en']))



class TheKoreanCardMustBeInKorean(unittest.TestCase):
    """⚠️ Replays the card that shipped on 24 August 2026: three of four Korean
    labels still in English, on a live account, unnoticed. check_labels asks a
    MODEL whether a label still says what its figure is, and an English label
    does say that — so the fault sailed past the only check that looked."""

    AUG24 = [('강남역 추정 인파', 'Estimated crowd, Gangnam Station'),
             ('Estimated crowd in Seoul Station right now',
              'Estimated crowd in Seoul Station right now'),
             ('Estimated crowd in Gyeongbokgung right now',
              'Estimated crowd in Gyeongbokgung right now'),
             ('Estimated crowd at Nodeul Island the same minute',
              'Estimated crowd at Nodeul Island the same minute')]

    @staticmethod
    def _lines(pairs):
        return [{'label_ko': ko, 'label_en': en} for ko, en in pairs]

    def setUp(self):
        S.DRY_RUN = True          # keeps the estate observation log out of tests

    def test_it_catches_the_card_that_actually_shipped(self):
        bad = S.check_korean(self._lines(self.AUG24), '지금 서울은',
                             'Seoul, right now', log=lambda *_: None)
        self.assertEqual(len(bad), 3)
        self.assertTrue(all(b['copied'] for b in bad))

    def test_a_healthy_card_says_nothing(self):
        good = [('홍대 추정 인파', 'Estimated crowd, Hongdae'),
                ('잠실 추정 인파', 'Estimated crowd, Jamsil')]
        self.assertEqual(S.check_korean(self._lines(good), '지금 서울은',
                                        'Seoul, right now'), [])

    def test_the_opener_is_judged_too(self):
        good = [('홍대 추정 인파', 'Estimated crowd, Hongdae')]
        bad = S.check_korean(self._lines(good), 'Seoul, right now',
                             'Seoul, right now', log=lambda *_: None)
        self.assertEqual(len(bad), 1)
        self.assertTrue(bad[0]['opener'])

    def test_one_hangul_syllable_is_enough(self):
        """⚠️ The false positive to fear. Korean labels legitimately carry Latin
        — a brand, a station romanisation, a film title — and flagging those
        would put a finding in the log on healthy cards, which is how a log
        stops being read. Measured 26 August 2026: across 99 Korean cards in the
        feed the ONLY Latin-only labels were the three above."""
        mixed = [('Seoul Sky 서울스카이', 'Seoul Sky (Lotte World Tower)'),
                 ('F1 더 무비', 'F1 The Movie')]
        self.assertEqual(S.check_korean(self._lines(mixed), '지금 서울은',
                                        'Seoul, right now'), [])

    def test_hanja_is_not_korean_for_this_bot(self):
        """⚠️ Why the range is Hangul syllables and NOT the CJK ideographs.
        Widening it would let a label of pure Hanja pass as Korean, and this
        account writes its Korean in Hangul — 90 days of all six bot feeds held
        not one Hanja. Without this the range is only a comment claiming to be
        load-bearing: a mutation widening it passed the whole suite."""
        hanja = [('漢江 水溫', 'Water temperature in the Han')]
        bad = S.check_korean(self._lines(hanja), '지금 서울은',
                             'Seoul, right now', log=lambda *_: None)
        self.assertEqual(len(bad), 1)
        self.assertFalse(bad[0]['copied'])     # not a copy, simply not Hangul

    def test_it_runs_with_the_model_checker_switched_off(self):
        """⚠️ The whole point: it is deterministic, so it must not sit behind
        CHECK_LABELS. A card built when the model checker is off, unreachable or
        out of quota is exactly when this is the only thing looking."""
        import inspect
        src = inspect.getsource(S.compose)
        i_check = src.index('check_korean(lines')
        i_gate = src.index('if CHECK_LABELS:')
        self.assertLess(i_check, i_gate)

    def test_it_never_blocks_the_card(self):
        """A card with English labels is a bad card; a card that never posts is
        a dead bot. One in ninety-nine does not buy the right to refuse."""
        pool = [S.fact(f'crowd_{i}', 'crowd', f'Estimated crowd, Place {i}',
                       f'{i},000', f'{i},000', pin=True,
                       label_ko=f'Estimated crowd, Place {i}')
                for i in (1, 2, 3)]
        sel = {'opener_en': 'Seoul, right now', 'opener_ko': 'Seoul, right now',
               'opener_emoji': '',
               'picks': [{'id': f['id'], 'label_en': '', 'label_ko': '',
                          'emoji': ''} for f in pool]}
        c = S.compose(sel, pool)
        self.assertEqual(len(c['lines']), 3)
        self.assertTrue(c['ko_body'])

    def test_a_dry_run_writes_nothing_to_the_estate_log(self):
        """A test filing itself with the Sunday review is a fault invented by
        the reporting of it. Same rule as _observe_labels."""
        calls = []
        real = S.subprocess.run
        S.subprocess.run = lambda *a, **k: calls.append(a) or real(
            ['true'], capture_output=True)
        try:
            S.DRY_RUN = True
            S.check_korean(self._lines(self.AUG24), '지금 서울은',
                           'Seoul, right now', log=lambda *_: None)
        finally:
            S.subprocess.run = real
        self.assertEqual(calls, [])



if __name__ == '__main__':
    unittest.main(verbosity=2)
