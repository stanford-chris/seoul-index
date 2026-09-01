"""Tests for the rush vein (CardSubwayTime), added 25 August 2026.

⚠️ THE POINT OF THIS FILE IS THE DE-DUPLICATION TEST, and it is worth saying why
before anything else. CardSubwayTime serves every row TWICE, byte-identical.
Summed naively, every figure the vein publishes is exactly double — and a doubled
card is not a broken card. It renders, its ratios hold, its ranking holds, its
Korean is fine, and the only thing wrong with it is the numbers. Nothing in a
green bot run, a dry run or an eyeballed card would ever show it. So the guard is
pinned here, where it can fail loudly, and the fixture below deliberately doubles
its rows the way the live feed does.

The other tests follow the same rule as test_seoul_index_veins.py: each asserts a
guard FIRES, because none of these failures announces itself. A short read looks
like a quieter city; an unnameable station looks like Korean leaking onto the
English card; a stale cache looks like a working vein.

No network, no model call, no posting: http_get_json is stubbed per test.
"""
import sys, unittest
from datetime import datetime
from pathlib import Path

sys.argv = ['test']
sys.path.insert(0, str(Path(__file__).resolve().parent))
import seoul_index_post as S


class Stub:
    """Swap http_get_json for a canned CardSubwayTime payload."""

    def __init__(self, rows, total=None):
        self.rows = rows
        self.total = len(rows) if total is None else total
        self.calls = 0

    def __enter__(self):
        self.real = S.http_get_json
        S.http_get_json = self._get
        return self

    def __exit__(self, *exc):
        S.http_get_json = self.real

    def _get(self, url):
        self.calls += 1
        if 'CardSubwayTime' not in url:
            raise RuntimeError(f'no stub for {url}')
        return {'CardSubwayTime': {'RESULT': {'CODE': 'INFO-000'},
                                   'row': self.rows,
                                   'list_total_count': self.total}}


def row(station, am, pm, line='2호선', filler=20000):
    """One feed row. `filler` pads the other 22 hours so the station clears
    RUSH_FLOOR without the two tested hours having to carry it alone."""
    r = {'USE_MM': '202607', 'SBWY_ROUT_LN_NM': line, 'STTN': station}
    for h in range(24):
        r[f'HR_{h}_GET_ON_NOPE'] = float(filler)
        r[f'HR_{h}_GET_OFF_NOPE'] = float(filler)
    r[f'HR_{S.RUSH_AM}_GET_ON_NOPE'] = float(am)
    r[f'HR_{S.RUSH_PM}_GET_ON_NOPE'] = float(pm)
    return r


# Four stations that all resolve in seoul_index_names_en.json, spread across the
# morning/evening axis so the vein has both ends to choose from.
def fixture(double=False):
    rows = [row('종각', 10_000, 220_000),      # the evening end: a workplace
            row('시청', 19_000, 340_000),
            row('삼성', 20_000, 300_000),
            row('신정', 78_000, 15_000)]       # the morning end: a dormitory
    return [r for r in rows for _ in (0, 1)] if double else rows


def values(facts):
    return {f['label_en']: f['value_en'] for f in facts}


class TheDuplicatedRowIsDroppedNotSummed(unittest.TestCase):
    """The feed's twin rows must not double the published figures."""

    def test_a_doubled_feed_gives_the_same_figures_as_a_single_one(self):
        with Stub(fixture(double=False)):
            single = values(S.rush_facts('k', {}))
        with Stub(fixture(double=True)) as st:
            doubled = values(S.rush_facts('k', {}))
        self.assertEqual(single, doubled)
        # and the figure is the row's own, not twice it
        self.assertEqual(doubled['Jonggak, 6 p.m.'], '220,000')

    def test_a_row_sharing_a_key_but_differing_is_still_summed(self):
        """⚠️ De-duplication is on WHOLE-ROW identity, never on (line, station).

        A second row for the same station on the same line that carries
        DIFFERENT figures is real data — a feed correction, a split, a second
        direction — and dropping it would throw away boardings that happened.
        """
        # Kept in proportion so 종각 remains the evening extreme and the test is
        # about the summing rather than about which station wins the end.
        rows = fixture() + [row('종각', 1_000, 30_000)]
        with Stub(rows):
            got = values(S.rush_facts('k', {}))
        self.assertEqual(got['Jonggak, 8 a.m.'], '11,000')      # 10,000 + 1,000
        self.assertEqual(got['Jonggak, 6 p.m.'], '250,000')     # 220,000 + 30,000

    def test_the_same_station_on_two_lines_is_summed(self):
        rows = fixture() + [row('종각', 1_000, 9_000, line='1호선')]
        with Stub(rows):
            got = values(S.rush_facts('k', {}))
        self.assertEqual(got['Jonggak, 6 p.m.'], '229,000')


class AShortReadIsSilenceNotAQuieterCity(unittest.TestCase):
    """A page that did not all arrive must produce no card at all."""

    def test_fewer_rows_than_the_feed_promised_yields_nothing(self):
        with Stub(fixture(), total=len(fixture()) + 500):
            self.assertEqual(S.rush_facts('k', {}), [])

    def test_a_failed_request_yields_nothing(self):
        def boom(url):
            raise RuntimeError('down')
        real, S.http_get_json = S.http_get_json, boom
        try:
            self.assertEqual(S.rush_facts('k', {}), [])
        finally:
            S.http_get_json = real

    def test_a_non_numeric_figure_yields_nothing(self):
        rows = fixture()
        rows[0]['HR_18_GET_ON_NOPE'] = '-'
        with Stub(rows):
            self.assertEqual(S.rush_facts('k', {}), [])


class AStationMustBeNameableInEnglish(unittest.TestCase):
    """⚠️ Skipped, never romanised and never left in Korean on the English card:
    the rule the box office vein applies to a film KOFIC has no title for."""

    def test_an_unmapped_station_is_not_offered(self):
        # 없는역's ratio (≈0.996) beats every named station's, including
        # Jonggak's (≈0.913) — it would win outright if nameable. Excluded
        # before ranking, so Jonggak wins among what is actually offerable.
        rows = fixture() + [row('없는역', 500_000, 1_000)]
        with Stub(rows):
            facts = S.rush_facts('k', {})
        self.assertNotIn('없는역', ' '.join(f['label_en'] for f in facts))
        self.assertIn('Jonggak, 8 a.m.', values(facts))

    def test_every_english_label_is_free_of_hangul(self):
        with Stub(fixture()):
            facts = S.rush_facts('k', {})
        for f in facts:
            self.assertFalse(any('가' <= c <= '힣' for c in f['label_en']),
                             f'Hangul on the English card: {f["label_en"]!r}')

    def test_a_bracketed_landmark_still_resolves(self):
        """신정(은행정) and 광화문(세종문화회관) are how the feed names them; the
        name table carries the bare form. 58 of 72 misses resolve this way."""
        self.assertEqual(S.en_lookup('신정(은행정)', 'stations'), 'Sinjeong')
        self.assertEqual(S.en_lookup('광화문(세종문화회관)', 'stations'), 'Gwanghwamun')
        self.assertIsNone(S.en_lookup('없는역', 'stations'))

    def test_an_exact_key_still_wins_over_the_stripped_one(self):
        self.assertEqual(S.en_lookup('종각', 'stations'), 'Jonggak')


class TheLabelsAreOwnedByPython(unittest.TestCase):
    """Clock times are numbers, so the selector may not reword or translate them."""

    def test_every_label_is_pinned_and_carries_its_own_korean(self):
        with Stub(fixture()):
            facts = S.rush_facts('k', {})
        self.assertTrue(facts)
        for f in facts:
            self.assertTrue(f['pin'], f'unpinned: {f["label_en"]}')
            self.assertTrue(f['label_ko'], f'no Korean: {f["label_en"]}')

    def test_place_is_set_and_is_a_real_substring_of_both_labels(self):
        # compose()'s rush override bolds place_en/place_ko unconditionally
        # (user's call, 1 Sept 2026) — it never checks they occur in the
        # label, so that must be guaranteed here, not there.
        with Stub(fixture()):
            facts = S.rush_facts('k', {})
        for f in facts:
            self.assertIn(f['place_en'], f['label_en'])
            self.assertIn(f['place_ko'], f['label_ko'])

    def test_the_two_hours_of_one_station_form_a_pair(self):
        with Stub(fixture()):
            facts = S.rush_facts('k', {})
        pairs = {}
        for f in facts:
            pairs.setdefault(f['pair'], []).append(f)
        self.assertTrue(pairs)
        for name, group in pairs.items():
            self.assertEqual(len(group), 2, f'{name} is not a two-hour pair')

    def test_only_one_end_is_offered(self):
        # A fresh state has no rush_last_side, which defaults to the pm side
        # (see test_the_side_alternates_across_months below for why it is
        # not simply "whichever is more dramatic") — Jonggak, not Sinjeong.
        with Stub(fixture()):
            got = values(S.rush_facts('k', {}))
        self.assertIn('Jonggak, 6 p.m.', got)
        self.assertIn('Jonggak, 8 a.m.', got)
        self.assertNotIn('Sinjeong, 8 a.m.', got)
        self.assertEqual(len(got), 2)

    def test_the_side_alternates_across_months(self):
        # ⚠️ NOT "whichever swings harder" — measured 1 Sept 2026, the top 12
        # real stations by |ratio| were all evening-heavy, so that rule would
        # have shown the same kind of place every time. Side is state-driven
        # instead: pm, then am, then pm again, regardless of which is bigger.
        state = {}
        with Stub(fixture()):
            first = values(S.rush_facts('k', state))
        self.assertEqual(state['rush_last_side'], 'pm')
        self.assertIn('Jonggak, 6 p.m.', first)

        state.pop('rush_cache', None)   # the month rolling over
        with Stub(fixture()):
            second = values(S.rush_facts('k', state))
        self.assertEqual(state['rush_last_side'], 'am')
        self.assertIn('Sinjeong, 8 a.m.', second)

        state.pop('rush_cache', None)
        with Stub(fixture()):
            third = values(S.rush_facts('k', state))
        self.assertEqual(state['rush_last_side'], 'pm')
        self.assertIn('Jonggak, 6 p.m.', third)


class TheMonthRidesOnTheCard(unittest.TestCase):
    """⚠️ Each figure is a WHOLE MONTH of that hour. compose() states that in the
    footnote and reads RUSH_M for the month, so an unset RUSH_M would leave the
    card claiming one evening's boardings."""

    def test_the_month_is_published_for_the_footnote(self):
        S.RUSH_M['en'] = S.RUSH_M['ko'] = None
        with Stub(fixture()):
            S.rush_facts('k', {})
        self.assertTrue(S.RUSH_M['en'])
        self.assertTrue(S.RUSH_M['ko'])
        self.assertRegex(S.RUSH_M['ko'], r'^\d{4}년 \d{1,2}월$')

    def test_the_vein_is_dated_so_the_month_becomes_a_dateline(self):
        self.assertIn('rush', S.DATED_PERIOD_CATS)


class TheMonthIsFetchedOnceNotEveryPost(unittest.TestCase):
    """The source publishes monthly; three posts a day must not re-fetch it."""

    def test_the_second_call_makes_no_request(self):
        state = {}
        with Stub(fixture()) as first:
            S.rush_facts('k', state)
        self.assertGreater(first.calls, 0)
        self.assertIn('rush_cache', state)
        with Stub(fixture()) as second:
            again = S.rush_facts('k', state)
        self.assertEqual(second.calls, 0)
        self.assertTrue(again)

    def test_the_cached_month_is_what_the_footnote_reports(self):
        state = {}
        with Stub(fixture()):
            S.rush_facts('k', state)
        month = state['rush_cache']['month']
        S.RUSH_M['en'] = None
        with Stub(fixture()):
            S.rush_facts('k', state)
        dt = datetime.strptime(month, '%Y%m')
        self.assertEqual(S.RUSH_M['ko'], f'{dt.year}년 {dt.month}월')


class ATinyStationIsNotAFinding(unittest.TestCase):
    """⚠️ The extremes of a ratio are otherwise decided by a few dozen people."""

    def test_a_station_below_the_floor_is_not_offered(self):
        rows = fixture() + [row('마들', 900, 3, filler=1)]   # a perfect ratio, 900 people
        with Stub(rows):
            got = values(S.rush_facts('k', {}))
        self.assertNotIn('Madeul, 8 a.m.', got)

    def test_too_few_qualifying_stations_is_silence(self):
        with Stub([row('종각', 10_000, 220_000), row('신정', 78_000, 15_000)]):
            self.assertEqual(S.rush_facts('k', {}), [])


# ⚠️ Keep this at the END of the file. Above the last class it runs before those
# tests are defined and reports a confident, short "OK": that had happened in
# test_seoul_index_books.py by 25 August 2026, hiding 9 tests from a direct run
# while discovery still saw all 39.
if __name__ == '__main__':
    unittest.main(verbosity=1)
