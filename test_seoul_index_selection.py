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
    """The station moved off the card footnote onto the source reply, and
    "(108)" became words (26 August 2026)."""

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

    def test_station_is_a_credit_not_a_card_footnote(self):
        c = self._compose(self._yday_pool())
        self.assertIn('reference station', c['src_en'])
        self.assertNotIn('reference station', c['note_en'])
        self.assertNotIn('108', c['note_en'])
        self.assertNotIn('108', c['src_en'])

    def test_the_observing_year_is_1907_and_reaches_the_reader(self):
        """⚠️ Not 1904 — that is when Korea's network began, not this station.
        Verified against the bot's own source: station 108's first daily row in
        the ASOS API is 1907-10-01 and every span before it returns NO_DATA. It
        is published prose now, so a wrong year is a wrong claim in the feed."""
        self.assertEqual(S.WX_OBSERVING_SINCE, 1907)
        c = self._compose(self._yday_pool())
        self.assertIn('observing since 1907', c['src_en'])
        self.assertIn('1907년 관측 개시', c['src_ko'])

    def test_the_summer_span_rides_only_when_a_summer_line_does(self):
        """A row saying "Summer 2026" needs the window spelled out somewhere,
        and the window is still growing. But a card of last month's readings
        carries no summer row, and a span covering none of its figures would be
        worse than no span at all."""
        S.WX_SEASON['en'], S.WX_SEASON['ko'] = '1 June–25 August', '6월 1일–8월 25일'
        self.assertNotIn('Summer figures run',
                         self._compose(self._yday_pool())['src_en'])
        summer = [S.fact('wx_s_swelter_now', 'weather',
                         'Days of 33°C (91°F) or more, 1 June–25 August 2026',
                         '15', '15', pin=True,
                         label_ko='최고기온 33°C 이상인 날, 2026년 6월 1일–8월 25일',
                         head_en='Days of 33°C (91°F) or more',
                         head_ko='최고기온 33°C 이상인 날',
                         period_en='Summer 2026', period_ko='2026년 여름')]
        c = self._compose(self._yday_pool() + summer)
        self.assertIn('Summer figures run 1 June–25 August', c['src_en'])
        self.assertIn('여름 수치는 6월 1일–8월 25일 기준', c['src_ko'])



if __name__ == '__main__':
    unittest.main(verbosity=2)
