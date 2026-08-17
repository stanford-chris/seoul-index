"""Tests for the 17 Aug 2026 selection fixes: the vein floor, the repeat guard
and the spotlight flat-card check. No network, no model call, no posting."""
import sys, unittest
from pathlib import Path
from datetime import datetime, timedelta, timezone

sys.argv = ['test']
sys.path.insert(0, str(Path(__file__).resolve().parent))
import seoul_index_post as S


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
        state = {'cat_last_at': {'crowd': ago(1)},
                 'last_cat': 'world', 'last_promoted_cat': 'world'}
        _, cat = S.promote_starved(POOL, state)
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


if __name__ == '__main__':
    unittest.main(verbosity=2)
