"""Tests for the six veins added 21 Aug 2026 from the data.seoul.go.kr sweep.

Every test here asserts a GUARD FIRES. That is deliberate: none of the traps
these veins carry announces itself. The feeds do not error, they do not return
nothing, and they do not crash the harvester — they hand back a plausible number
that means something other than what its field name says. A green run of the
bot proves nothing about any of them, which is why they are tested where they
live rather than through a composed card.

No network, no model call, no posting: http_get_json is stubbed per test.
"""
import json, re, sys, types, unittest
from datetime import timedelta
from pathlib import Path

sys.argv = ['test']
sys.path.insert(0, str(Path(__file__).resolve().parent))
import seoul_index_post as S
import tempfile as _tempfile
from pathlib import Path as _Path
# transport_facts() writes the per-route history file; never the real one from a test.
S.BUS_HISTORY = _Path(_tempfile.mkdtemp()) / 'bus_route_history.json'

# ⚠️ compose() ends by checking its labels against the pool's own with a model
# call (see check_labels). These tests promise no network and no model call, so
# the CALL is switched off here: what they exercise is selection and composition, and a
# live checker would make them slow, non-deterministic and quota-hungry. The
# checker's own behavior is tested in test_seoul_index_labels.py.
S.CHECK_LABELS = False


class Stub:
    """Swap http_get_json for a canned payload, restoring it afterwards."""

    def __init__(self, payloads):
        self.payloads = payloads      # {substring of URL: payload}
        self.calls = []

    def __enter__(self):
        self.real = S.http_get_json
        S.http_get_json = self._get
        return self

    def __exit__(self, *exc):
        S.http_get_json = self.real

    def _get(self, url):
        self.calls.append(url)
        for key, payload in self.payloads.items():
            if key in url:
                if isinstance(payload, Exception):
                    raise payload
                return payload
        raise RuntimeError(f'no stub for {url}')


def ok(service, rows):
    return {service: {'RESULT': {'CODE': 'INFO-000'}, 'row': rows,
                      'list_total_count': len(rows)}}


# ---------------------------------------------------------------------------
# statInfantNumInfo: the field LABELS are wrong in the feed itself
# ---------------------------------------------------------------------------
# Three year columns, because a card needs three lines: a two-year fixture
# makes every one of these tests pass for the wrong reason.
INFANT_ROWS = [
    # GBCODE 00 is a HEADER: its YEARnn hold the year labels, not counts.
    {'GBCODE': '00', 'GBCODENM': '연도별',
     'YEAR01': '2016', 'YEAR02': '2021', 'YEAR03': '2025'},
    {'GBCODE': '01', 'GBCODENM': '0세',
     'YEAR01': '75,536', 'YEAR02': '45,531', 'YEAR03': '41,600'},
    {'GBCODE': '02', 'GBCODENM': '출산율(%)',
     'YEAR01': '0.94', 'YEAR02': '0.64', 'YEAR03': '0.580'},
    # ⚠️ '수' means count and holds a PERCENTAGE; '비율' means ratio and holds a
    # COUNT. They are swapped in the feed. Neither may ever reach a card.
    {'GBCODE': '07', 'GBCODENM': '어린이집,계,수',
     'YEAR01': '44.1%', 'YEAR02': '45.0%', 'YEAR03': '46.1%'},
    {'GBCODE': '08', 'GBCODENM': '어린이집,계,비율',
     'YEAR01': '131,081', 'YEAR02': '100,000', 'YEAR03': '89,559'},
]


class InfantFeedLabelsLie(unittest.TestCase):
    def facts(self, rows=None, state=None):
        with Stub({'statInfantNumInfo': ok('statInfantNumInfo', rows or INFANT_ROWS)}):
            return S.infant_facts('KEY', state if state is not None else {})

    def test_header_row_is_never_read_as_data(self):
        # Reading GBCODE 00 would publish the YEAR as a population: '2016' people.
        vals = {f['value_en'] for f in self.facts()}
        self.assertNotIn('2,016', vals)
        self.assertNotIn('2016', vals)

    def test_year_labels_come_from_the_header(self):
        labels = {f['label_en'] for f in self.facts()}
        self.assertTrue(labels <= {'2016', '2021', '2025'}, labels)

    def test_percentage_row_can_never_reach_a_card(self):
        # GBCODE 07 says 'count' and holds "44.1%". Every rotation must skip it.
        state = {}
        seen = set()
        for _ in range(8):
            for f in self.facts(state=state):
                seen.add(f['value_en'])
        self.assertNotIn('44.1%', seen)
        self.assertNotIn('44', seen)

    def test_only_allowlisted_gbcodes_are_read(self):
        # 08 is a real count, but it is not on the list and stays off the card.
        state = {}
        seen = set()
        for _ in range(8):
            for f in self.facts(state=state):
                seen.add(f['value_en'])
        self.assertNotIn('131,081', seen)

    def test_a_renamed_series_is_dropped_not_guessed(self):
        rows = [dict(r) for r in INFANT_ROWS]
        rows[1]['GBCODENM'] = '만0세'          # label changes, GBCODE does not
        self.assertTrue(self.facts(rows))       # keyed on GBCODE, so still read

    def test_a_renumbered_series_is_dropped(self):
        rows = [dict(r) for r in INFANT_ROWS]
        rows[1]['GBCODE'] = '99'               # no longer allow-listed
        self.assertEqual(self.facts(rows), [])


# ---------------------------------------------------------------------------
# SmartUncomfStatMonth: the running year hides a year-to-date total in a month
# ---------------------------------------------------------------------------
def year_row(year, months, total=None):
    r = {'YEAR': year}
    for i, m in enumerate(months, 1):
        r[f'MON_{i:02d}'] = float(m)
    r['MON_TOTAL'] = float(total if total is not None else sum(months))
    return r


COMPLETE = [year_row('2025', [50000] * 12), year_row('2024', [60000] * 12),
            year_row('2023', [70000] * 12)]
# ⚠️ The real 2026 row: MON_07 held 435,518, which was MON_TOTAL and also the
# sum of Jan-Jun. Publishing it as July would have been six times too large.
RUNNING = year_row('2026', [66038, 63038, 73495, 76110, 77418, 79419,
                            435518, 0, 0, 0, 0, 0], total=435518)


# ⚠️ A running year in DECEMBER has no empty months at all, so the zero-month
# check cannot see it and ONLY the arithmetic check does. Without this fixture
# the suite passed with the arithmetic check deleted — found by mutation, not by
# a green run.
DECEMBER_RUNNING = year_row('2027', [10000] * 11 + [110000], total=110000)


class ComplaintRunningYear(unittest.TestCase):
    def facts(self, rows):
        with Stub({'SmartUncomfStatMonth': ok('SmartUncomfStatMonth', rows)}):
            return S.complaint_facts('KEY')

    def test_the_running_year_is_rejected(self):
        labels = {f['label_en'] for f in self.facts([RUNNING] + COMPLETE)}
        self.assertNotIn('2026', labels)

    def test_the_running_years_inflated_month_never_appears(self):
        vals = {f['value_en'] for f in self.facts([RUNNING] + COMPLETE)}
        self.assertNotIn('435,518', vals)

    def test_complete_years_are_kept(self):
        labels = {f['label_en'] for f in self.facts([RUNNING] + COMPLETE)}
        self.assertEqual(labels, {'2025', '2024', '2023'})

    def test_a_year_with_an_empty_month_is_not_complete(self):
        partial = year_row('2022', [1000] * 11 + [0])
        labels = {f['label_en'] for f in self.facts(COMPLETE + [partial])}
        self.assertNotIn('2022', labels)

    def test_a_december_running_year_is_caught_by_arithmetic_alone(self):
        # Every month non-zero, so the empty-month check is blind here: the
        # months sum to 220,000 against a stated total of 110,000.
        labels = {f['label_en'] for f in self.facts([DECEMBER_RUNNING] + COMPLETE)}
        self.assertNotIn('2027', labels)
        self.assertNotIn('110,000', {f['value_en']
                                     for f in self.facts([DECEMBER_RUNNING] + COMPLETE)})

    def test_too_few_complete_years_yields_nothing(self):
        self.assertEqual(self.facts([RUNNING] + COMPLETE[:2]), [])


# ---------------------------------------------------------------------------
# SPOP_DAILYSUM_JACHI_250: a citywide row sits among the districts
# ---------------------------------------------------------------------------
def dn(name, day, night, stamp='20260817'):
    return {'STDR_DE_ID': stamp, 'SIGNGU_NM': name,
            'DAY_LVPOP_CO': str(day), 'NIGHT_LVPOP_CO': str(night)}


DN_ROWS = [dn('서울시', 9_500_000, 9_400_000),      # ⚠️ the whole city
           dn('종로구', 308_329, 207_168), dn('중구', 274_182, 192_273),
           dn('강남구', 744_354, 648_850), dn('송파구', 766_166, 726_820)]


class DaynightCitywideRow(unittest.TestCase):
    def facts(self, state=None):
        with Stub({'SPOP_DAILYSUM': ok('SPOP_DAILYSUM_JACHI_250', DN_ROWS)}):
            return S.daynight_facts('KEY', state if state is not None else {})

    def test_the_citywide_row_never_becomes_a_line(self):
        # Left in, 9.5m would tower over every district and read as one of them.
        self.assertNotIn('9,500,000', {f['value_en'] for f in self.facts()})
        self.assertNotIn('서울시', {f['label_ko'] for f in self.facts()})

    def test_the_citywide_row_is_dropped_even_if_it_gains_an_english_name(self):
        # ⚠️ Without this, the test above passes for the WRONG REASON: '서울시'
        # has no entry in the district table, so en_name's unmapped fallback
        # drops it and the explicit skip is never exercised. Give it a name and
        # only the explicit skip stands between it and the card.
        real = S.en_name
        S.en_name = lambda ko, kind: 'Seoul' if ko == '서울시' else real(ko, kind)
        try:
            facts = self.facts()
        finally:
            S.en_name = real
        self.assertNotIn('Seoul', {f['label_en'] for f in facts})
        self.assertNotIn('9,500,000', {f['value_en'] for f in facts})

    def test_districts_survive(self):
        # The extremes now carry their rank and the district in parentheses,
        # so match on containment rather than equality.
        labels = ' '.join(f['label_ko'] for f in self.facts())
        self.assertIn('종로구', labels)
        self.assertIn('중구', labels)

    def test_the_extremes_lead_with_what_they_mean(self):
        # A reader who cannot place Songpa-gu can still read the line.
        facts = self.facts()
        self.assertTrue(facts[0]['label_en'].startswith('Fullest'))
        self.assertTrue(facts[-1]['label_en'].startswith('Emptiest'))
        self.assertIn('(Songpa-gu)', facts[0]['label_en'])

    def test_day_and_night_are_never_mixed_in_one_card(self):
        state = {}
        first = {f['value_en'] for f in self.facts(state)}
        second = {f['value_en'] for f in self.facts(state)}
        self.assertNotEqual(first, second)       # alternates
        self.assertIn('308,329', first)          # daytime Jongno
        self.assertIn('207,168', second)         # night-time Jongno
        self.assertEqual(first & second, set())  # never both on one card

    def test_lines_are_flagged_estimated(self):
        self.assertTrue(all(f['estimated'] for f in self.facts()))


# ---------------------------------------------------------------------------
# WoWcbsDayStatic: three different measures share one feed
# ---------------------------------------------------------------------------
def wrow(site, measure, val, ymd='20260820'):
    return {'YMD': ymd, 'BUSNP_NM': site, 'ROF_SE_NM': measure,
            'MSRMT_VL': float(val)}


class WaterOneMeasureOnly(unittest.TestCase):
    def facts(self, rows):
        with Stub({'WoWcbsDayStatic': ok('WoWcbsDayStatic', rows)}):
            return S.water_facts('KEY')

    def test_only_intake_is_read(self):
        rows = [wrow('암사', '취수', 1_074_500), wrow('강북', '취수', 840_389),
                wrow('뚝도', '취수', 435_692),
                wrow('암사', '송수', 1_004_800),      # transmission
                wrow('동부', '공급량', 617_983)]      # supplied
        vals = {f['value_en'] for f in self.facts(rows)}
        self.assertIn('1,074,500 m³', vals)
        self.assertNotIn('1,004,800 m³', vals)   # would compare unlike things
        self.assertNotIn('617,983 m³', vals)

    def test_an_uncurated_site_is_skipped_not_romanised(self):
        rows = [wrow('암사', '취수', 1), wrow('강북', '취수', 2),
                wrow('뚝도', '취수', 3), wrow('새이름', '취수', 4)]
        self.assertNotIn('새이름', {f['label_ko'] for f in self.facts(rows)})

    def test_too_few_sites_is_no_card(self):
        self.assertEqual(self.facts([wrow('암사', '취수', 1)]), [])

    def test_only_the_newest_day_is_used(self):
        rows = [wrow('암사', '취수', 111, '20260820'), wrow('강북', '취수', 222, '20260820'),
                wrow('뚝도', '취수', 333, '20260820'), wrow('구의', '취수', 999, '20260819')]
        self.assertNotIn('999 m³', {f['value_en'] for f in self.facts(rows)})


# ---------------------------------------------------------------------------
# ListNecessariesPricesService: a flat spread is not an index
# ---------------------------------------------------------------------------
def prow(name, unit, price, gu, kind='전통시장', date='2026-08-14'):
    return {'PRDLST_NM': name, 'UNIT': unit, 'A_PRICE': str(price),
            'M_GU_NAME': gu, 'M_TYPE_NAME': kind, 'P_DATE': date,
            'M_NAME': f'{gu} 시장'}


class PriceSpreadGuard(unittest.TestCase):
    def facts(self, rows, state=None):
        with Stub({'ListNecessariesPrices':
                   ok('ListNecessariesPricesService', rows)}):
            return S.price_facts('KEY', state if state is not None else {})

    def test_a_flat_item_is_skipped(self):
        rows = [prow('배추', '1포기', 5000, '종로구'),
                prow('배추', '1포기', 5200, '중구'),
                prow('배추', '1포기', 5400, '강남구')]   # 1.08x, far under 1.5
        self.assertEqual(self.facts(rows), [])

    def test_a_real_spread_makes_a_card(self):
        rows = [prow('배추', '1포기', 2992, '노원구', '대형마트'),
                prow('배추', '1포기', 3500, '광진구'),
                prow('배추', '1포기', 6900, '동작구')]
        vals = {f['value_en'] for f in self.facts(rows)}
        self.assertIn('₩2,992', vals)
        self.assertIn('₩6,900', vals)       # both ends must survive

    def test_one_line_per_district_and_kind(self):
        rows = [prow('배추', '1포기', 2992, '노원구', '대형마트'),
                prow('배추', '1포기', 3100, '노원구', '대형마트'),   # same label
                prow('배추', '1포기', 3500, '광진구'),
                prow('배추', '1포기', 6900, '동작구')]
        labels = [f['label_en'] for f in self.facts(rows)]
        self.assertEqual(len(labels), len(set(labels)))

    def test_an_unmapped_district_is_dropped(self):
        rows = [prow('배추', '1포기', 2992, '노원구', '대형마트'),
                prow('배추', '1포기', 3500, '광진구'),
                prow('배추', '1포기', 6900, '동작구'),
                prow('배추', '1포기', 9900, '없는구')]
        self.assertNotIn('₩9,900', {f['value_en'] for f in self.facts(rows)})

    def test_a_zero_price_is_not_a_price(self):
        rows = [prow('배추', '1포기', 0, '노원구', '대형마트'),
                prow('배추', '1포기', 3500, '광진구'),
                prow('배추', '1포기', 6900, '동작구'),
                prow('배추', '1포기', 2992, '중구', '대형마트')]
        self.assertNotIn('₩0', {f['value_en'] for f in self.facts(rows)})


# ---------------------------------------------------------------------------
# WPOSInformationTime + KMA: one hour, and the Han must be in it
# ---------------------------------------------------------------------------
def wp(station, watt, hr='13:00', ymd='20260821'):
    return {'MSRSTN_NM': station, 'WATT': str(watt), 'YMD': ymd, 'HR': hr}


def kma(t1h):
    return {'response': {'body': {'items': {'item': [
        {'category': 'T1H', 'obsrValue': str(t1h)}]}}}}


class RiverNeedsTheHanAndASpread(unittest.TestCase):
    def facts(self, rows, air=15.0):
        with Stub({'WPOSInformationTime': ok('WPOSInformationTime', rows),
                   'getUltraSrtNcst': kma(air)}):
            return S.river_facts('KEY', 'GOVKEY')

    def test_an_hour_without_the_han_is_not_used(self):
        # 18:00 has three tributaries; 13:00 has all four. The Han is required.
        rows = [wp('탄천', 28.2, '18:00'), wp('중랑천', 27.1, '18:00'),
                wp('안양천', 28.8, '18:00'),
                wp('선유', 20.0, '13:00'), wp('탄천', 21.0, '13:00'),
                wp('중랑천', 22.0, '13:00')]
        labels = {f['label_en'] for f in self.facts(rows)}
        self.assertIn('The Han at Seonyu', labels)
        # Assert on the KOREAN value: it stays bare metric, while the English
        # one carries an imperial conversion.
        self.assertIn('20.0°C', {f['value_ko'] for f in self.facts(rows)})

    def test_a_flat_summer_reading_makes_no_card(self):
        rows = [wp('선유', 28.1), wp('탄천', 27.4), wp('중랑천', 26.5)]
        self.assertEqual(self.facts(rows, air=26.5), [])   # 1.6°C spread

    def test_an_open_autumn_gap_makes_a_card(self):
        rows = [wp('선유', 20.4), wp('탄천', 20.1), wp('중랑천', 19.6)]
        self.assertTrue(self.facts(rows, air=14.8))        # 5.6°C spread

    def test_the_air_line_is_always_present(self):
        rows = [wp('선유', 20.4), wp('탄천', 20.1), wp('중랑천', 19.6)]
        self.assertIn('The air', {f['label_en'] for f in self.facts(rows, 14.8)})

    def test_no_air_reading_means_no_card(self):
        # Four near-identical river temperatures are not an index on their own.
        rows = [wp('선유', 20.4), wp('탄천', 20.1), wp('중랑천', 19.6)]
        with Stub({'WPOSInformationTime': ok('WPOSInformationTime', rows),
                   'getUltraSrtNcst': RuntimeError('down')}):
            self.assertEqual(S.river_facts('KEY', 'GOVKEY'), [])

    def test_a_station_under_maintenance_is_skipped(self):
        rows = [wp('선유', 20.4), wp('탄천', '점검중'), wp('중랑천', 19.6),
                wp('안양천', 19.9)]
        vals = {f['value_en'] for f in self.facts(rows, 14.8)}
        self.assertNotIn('점검중', vals)


class RiverOpenerAndDateline(unittest.TestCase):
    """Two rules that the noon card of 23 August 2026 broke, both requested
    that day: the opener must name whichever of air and water the sort has put
    on the TOP line, and the dateline must always say which day the hour is.

    These go end to end — river_facts through the real compose() — because both
    rules live on the far side of compose's same-unit sort, and a unit test of
    either half alone would pass while the card still read wrong."""

    HOT = [('선유', 27.2), ('탄천', 26.9), ('중랑천', 25.8), ('안양천', 28.5)]
    COLD = [('선유', 20.4), ('탄천', 20.1), ('중랑천', 19.6), ('안양천', 19.9)]

    def card(self, waters, air, hr, ymd='20260823'):
        rows = [wp(n, v, hr, ymd) for n, v in waters]
        with Stub({'WPOSInformationTime': ok('WPOSInformationTime', rows),
                   'getUltraSrtNcst': kma(air)}):
            facts = S.river_facts('KEY', 'GOVKEY')
        self.assertTrue(facts, 'the vein went inert; check the spread guard')
        # A deliberately wrong opener: the selector's river wording is discarded,
        # so if the override ever stops firing this decoy lands on the card.
        sel = {'opener_en': 'DECOY', 'opener_ko': 'DECOY', 'opener_emoji': '🌡️',
               'picks': [{'id': f['id'], 'label_en': f['label_en'],
                          'label_ko': f['label_ko'], 'emoji': ''}
                         for f in facts]}
        return S.compose(sel, facts)

    def test_air_on_top_puts_air_first_in_the_opener(self):
        c = self.card(self.HOT, 31.3, '12:00')
        self.assertEqual(c['lines'][0]['label_en'], 'The air')
        self.assertEqual(c['opener']['en'], 'Air and water in Seoul')
        self.assertEqual(c['opener']['ko'], '서울의 공기와 물')

    def test_water_on_top_puts_water_first_in_the_opener(self):
        c = self.card(self.COLD, 14.8, '12:00')
        self.assertEqual(c['lines'][0]['label_en'], 'The Han at Seonyu')
        self.assertEqual(c['opener']['en'], 'Water and air in Seoul')
        self.assertEqual(c['opener']['ko'], '서울의 물과 공기')

    def test_noon_and_midnight_are_capitalised(self):
        self.assertTrue(
            self.card(self.HOT, 31.3, '12:00')['dateline_en'].startswith('Noon,'))
        self.assertTrue(
            self.card(self.HOT, 31.3, '00:00')['dateline_en'].startswith('Midnight,'))

    def test_the_numeral_hours_are_left_alone(self):
        # .capitalize() must not touch these: "3 P.m." would be worse than the
        # bare lowercase it replaced.
        self.assertEqual(self.card(self.HOT, 31.3, '15:00')['dateline_en'],
                         '3 p.m., 23 August')
        self.assertEqual(self.card(self.HOT, 31.3, '08:00')['dateline_en'],
                         '8 a.m., 23 August')

    def test_the_date_rides_even_when_the_reading_is_from_today(self):
        # The old rule dated the hour only when it was NOT today, which left the
        # ordinary card headed by a bare "noon". 선유 lags the other stations by
        # about five hours, so "which day" is never safe to leave implied.
        for hr in ('12:00', '00:00', '15:00'):
            c = self.card(self.HOT, 31.3, hr)
            self.assertIn('23 August', c['dateline_en'])
            self.assertIn('8월 23일', c['dateline_ko'])

    def test_a_reading_from_another_day_is_dated_to_that_day(self):
        c = self.card(self.HOT, 31.3, '15:00', ymd='20260821')
        self.assertEqual(c['dateline_en'], '3 p.m., 21 August')
        self.assertEqual(c['dateline_ko'], '오후 3시, 8월 21일')

    def test_the_footnote_says_what_a_cheon_is(self):
        # Bare names leave an English reader five temperatures and no idea that
        # three of them are waterways feeding the river they have heard of.
        # The card cannot say it on the dateline the way the water card does —
        # the reading hour is already there — so it goes in the footnote.
        c = self.card(self.HOT, 31.3, '12:00')
        self.assertIn(
            'The Anyangcheon, Tancheon and Jungnangcheon are tributaries of the Han',
            c['note_en'])
        self.assertIn('안양천·탄천·중랑천은 한강 지류', c['note_ko'])
        # The air caveat still follows it, in that order.
        self.assertLess(c['note_en'].index('tributaries'),
                        c['note_en'].index('forecast-zone'))

    def test_the_footnote_never_names_a_river_the_card_does_not_show(self):
        # 안양천 under maintenance: it drops off the card, so it must drop out of
        # the footnote too. Naming a river the reader cannot see is worse than
        # saying nothing.
        c = self.card([('선유', 20.4), ('탄천', 20.1), ('중랑천', 19.6)], 14.8, '12:00')
        self.assertIn('The Tancheon and Jungnangcheon are tributaries of the Han',
                      c['note_en'])
        self.assertNotIn('Anyangcheon', c['note_en'])
        self.assertNotIn('안양천', c['note_ko'])

    def test_the_han_is_not_called_its_own_tributary(self):
        c = self.card(self.HOT, 31.3, '12:00')
        self.assertNotIn('Seonyu', c['note_en'])
        self.assertEqual(c['note_en'].count('Han'), 1)   # once, as the parent


class ImperialConversions(unittest.TestCase):
    """Conversions ride the ENGLISH card only, and a difference is not a
    temperature."""

    def test_celsius_carries_fahrenheit_on_the_english_card_only(self):
        rows = [wp('선유', 20.4), wp('탄천', 20.1), wp('중랑천', 19.6)]
        with Stub({'WPOSInformationTime': ok('WPOSInformationTime', rows),
                   'getUltraSrtNcst': kma(14.8)}):
            facts = S.river_facts('KEY', 'GOVKEY')
        self.assertTrue(all('°F' in f['value_en'] for f in facts))
        self.assertFalse(any('°F' in f['value_ko'] for f in facts))

    def test_a_temperature_difference_uses_the_delta_formula(self):
        # ⚠️ The urban heat island runs about 2°C. As a TEMPERATURE that would
        # convert to 35.6°F; as the DIFFERENCE it is, it is 3.6°F. The wrong
        # formula gives a number ten times too large and entirely plausible.
        self.assertEqual(S.to_f_delta(2.0), '2.0°C (3.6°F)')
        self.assertEqual(S.to_f(2.0), '2.0°C (36°F)')

    def test_speed_carries_mph(self):
        self.assertEqual(S.to_mph(26), '26 km/h (16 mph)')

    def test_a_converted_value_still_sorts(self):
        # ⚠️ Without stripping the parenthetical, _sortkey returns None and the
        # card silently stops ordering its lines.
        self.assertEqual(S._sortkey('26.5°C (80°F)'), ('u:°C', 26.5))
        self.assertEqual(S._sortkey('26 km/h (16 mph)'), ('u:km/h', 26.0))

    def test_converted_lines_of_one_unit_still_share_a_sort_class(self):
        keys = [S._sortkey(v) for v in ('20.4°C (69°F)', '14.8°C (59°F)')]
        self.assertEqual(len({k[0] for k in keys}), 1)


# ---------------------------------------------------------------------------
# HRFCO: conditional, and the range comes back newest-first
# ---------------------------------------------------------------------------
def hr_level(pairs):
    return {'content': [{'ymdhm': t, 'wl': str(v)} for t, v in pairs]}


HR_TIERS = {'content': [{'wlobscd': '1018680', 'attwl': '3.9', 'wrnwl': '5.5',
                         'almwl': '6.2', 'srswl': '6.5'}]}


class LevelIsConditional(unittest.TestCase):
    def facts(self, pairs):
        with Stub({'waterlevel/list': hr_level(pairs),
                   'waterlevel/info': HR_TIERS}):
            return S.level_facts('KEY')

    def test_an_ordinary_river_is_silence(self):
        self.assertEqual(self.facts([('202608211900', 2.68)]), [])

    def test_a_high_river_speaks(self):
        facts = self.facts([('202608211900', 4.62)])
        self.assertIn('4.62 m', {f['value_en'] for f in facts})

    def test_newest_first_ordering_does_not_yield_a_stale_reading(self):
        # ⚠️ HRFCO returns the range NEWEST-FIRST. Reading the LAST row takes
        # the OLDEST — here a 2.10 m reading hours old, which would silence a
        # river that is actually at 4.62 m.
        pairs = [('202608211900', 4.62), ('202608211800', 3.10),
                 ('202608211700', 2.10)]
        vals = {f['value_en'] for f in self.facts(pairs)}
        self.assertIn('4.62 m', vals)
        self.assertNotIn('2.10 m', vals)

    def test_blank_current_slots_are_skipped_not_zeroed(self):
        with Stub({'waterlevel/list': {'content': [
                       {'ymdhm': '202608211910', 'wl': ''},
                       {'ymdhm': '202608211900', 'wl': '4.62'}]},
                   'waterlevel/info': HR_TIERS}):
            vals = {f['value_en'] for f in S.level_facts('KEY')}
        self.assertIn('4.62 m', vals)

    def test_tiers_are_read_live_not_hardcoded(self):
        tiers = {'content': [{'wlobscd': '1018680', 'attwl': '3.0',
                              'wrnwl': '4.0', 'almwl': '5.0', 'srswl': '6.0'}]}
        with Stub({'waterlevel/list': hr_level([('202608211900', 4.62)]),
                   'waterlevel/info': tiers}):
            vals = {f['value_en'] for f in S.level_facts('KEY')}
        self.assertIn('4.00 m', vals)       # the revised tier, not 5.50
        self.assertNotIn('5.50 m', vals)

    def test_a_partial_tier_set_is_refused(self):
        tiers = {'content': [{'wlobscd': '1018680', 'attwl': '3.9',
                              'wrnwl': '', 'almwl': '6.2', 'srswl': '6.5'}]}
        with Stub({'waterlevel/list': hr_level([('202608211900', 4.62)]),
                   'waterlevel/info': tiers}):
            self.assertEqual(S.level_facts('KEY'), [])

    def test_no_key_is_silence_not_a_crash(self):
        self.assertEqual(S.level_facts(None), [])

    def test_the_hour_is_dated_and_capitalised(self):
        # This period is the card's only datable one, so it is always lifted to
        # the masthead dateline. It read a bare lowercase "7 p.m." until
        # 23 August 2026 — no day at all, on the one card a reader may come back
        # to weeks later asking exactly that. See RiverOpenerAndDateline.
        self.facts([('202608211900', 4.62)])
        self.assertEqual(S.LEVEL_PERIOD['en'], '7 p.m., 21 August')
        self.assertEqual(S.LEVEL_PERIOD['ko'], '오후 7시, 8월 21일')

    def test_the_word_hours_lift_but_the_numerals_do_not(self):
        self.facts([('202608210000', 4.62)])
        self.assertEqual(S.LEVEL_PERIOD['en'], 'Midnight, 21 August')
        self.facts([('202608211200', 4.62)])
        self.assertEqual(S.LEVEL_PERIOD['en'], 'Noon, 21 August')
        self.facts([('202608210800', 4.62)])
        self.assertEqual(S.LEVEL_PERIOD['en'], '8 a.m., 21 August')


# ---------------------------------------------------------------------------
# SeoulLibraryMemberInfo
# ---------------------------------------------------------------------------
class LibraryBands(unittest.TestCase):
    def facts(self, rows):
        with Stub({'SeoulLibraryMemberInfo':
                   ok('SeoulLibraryMemberInfo', rows)}):
            return S.library_facts('KEY')

    def test_bands_outside_the_map_are_dropped(self):
        rows = [{'AGE_RANGE': '0', 'MBR_CNT': '268'},
                {'AGE_RANGE': '90', 'MBR_CNT': '50'},
                {'AGE_RANGE': '30', 'MBR_CNT': '70348'},
                {'AGE_RANGE': '40', 'MBR_CNT': '60143'},
                {'AGE_RANGE': '20', 'MBR_CNT': '49854'}]
        facts = self.facts(rows)
        # Assert on the IDS, not the formatted values: a guard that merges the
        # tiny bands into a real one also makes '268' vanish from the values,
        # which is how a broken guard passed this test at first.
        ids = {f['id'] for f in facts}
        self.assertNotIn('library_0', ids)
        self.assertNotIn('library_90', ids)
        self.assertEqual(ids, {'library_30', 'library_40', 'library_20'})
        self.assertEqual({f['value_en'] for f in facts},
                         {'70,348', '60,143', '49,854'})   # nothing absorbed

    def test_birth_years_within_a_band_are_summed(self):
        rows = [{'AGE_RANGE': '30', 'MBR_CNT': '40000'},
                {'AGE_RANGE': '30', 'MBR_CNT': '30348'},
                {'AGE_RANGE': '40', 'MBR_CNT': '60143'},
                {'AGE_RANGE': '20', 'MBR_CNT': '49854'}]
        vals = {f['value_en'] for f in self.facts(rows)}
        self.assertIn('70,348', vals)

    def test_teens_head_the_bands_not_ten_s(self):
        self.assertEqual(S.LIBRARY_BANDS['10'][0], 'Teens')
        self.assertEqual(S.LIBRARY_BANDS['20'][0], '20s')


# ---------------------------------------------------------------------------
# The library "1 in N": one publisher's numerator over another's denominator
# ---------------------------------------------------------------------------
# Every failure mode here is silent. A KOSIS outage returns a well-formed JSON
# OBJECT rather than a list; an off-by-one in the five-year band codes divides
# the teens by the twenty-somethings and prints a plausible number; and a stale
# LIBRARY_POP puts a footnote on a card whose values carry no ratio at all.
# None of those looks wrong in the output, so each is asserted here.

MEMBER_ROWS = [{'AGE_RANGE': '10', 'MBR_CNT': '10921'},
               {'AGE_RANGE': '20', 'MBR_CNT': '49876'},
               {'AGE_RANGE': '30', 'MBR_CNT': '70339'}]

# Real July 2026 figures. The teens are 342,321 + 372,808 = 715,129, and
# 715,129 / 10,921 rounds to 65 — a fixture of one band per decade would let a
# half-built denominator pass.
POP_BANDS = {'15': 342321, '20': 372808,        # 10-14 + 15-19 = the teens
             '25': 600000, '30': 627845,        # 20s: 1,227,845
             '35': 700000, '40': 769932}        # 30s: 1,469,932


class LibraryRatio(unittest.TestCase):
    def pop_rows(self, bands=None, prd='202607'):
        return [{'C1': '11', 'C2': c, 'DT': str(v), 'PRD_DE': prd}
                for c, v in (POP_BANDS if bands is None else bands).items()]

    def facts(self, kosis_payload, key='KOSIS-KEY'):
        # Seed the module dict with a lie: every test then proves the run
        # either replaced it or cleared it, never that it merely survived.
        S.LIBRARY_POP['en'], S.LIBRARY_POP['ko'] = 'STALE', 'STALE'
        payloads = {'SeoulLibraryMemberInfo': ok('SeoulLibraryMemberInfo', MEMBER_ROWS)}
        if kosis_payload is not None:
            payloads['kosis.kr'] = kosis_payload
        with Stub(payloads):
            return {f['id']: f for f in S.library_facts('KEY', key)}

    def test_a_decade_is_the_sum_of_two_published_five_year_bands(self):
        f = self.facts(self.pop_rows())
        # 715,129 / 10,921 = 65.5. Dividing by either half alone gives 31 or 34,
        # which is what an off-by-one in LIBRARY_POP_BANDS would print.
        self.assertEqual(f['library_10']['value_en'], '10,921 (1 in 65)')
        self.assertEqual(f['library_30']['value_en'], '70,339 (1 in 21)')
        self.assertEqual(S.LIBRARY_POP['en'], 'July 2026')

    def test_the_five_year_band_codes_are_the_ones_kosis_publishes(self):
        # KOSIS names a band by where the NEXT one starts: '15' is 10-14세.
        self.assertEqual(S.LIBRARY_POP_BANDS['10'], ('15', '20'))
        self.assertEqual(S.LIBRARY_POP_BANDS['80'], ('85', '90'))

    def test_the_ratio_is_a_trailing_parenthetical_so_the_card_still_sorts(self):
        f = self.facts(self.pop_rows())
        # _sortkey strips one trailing parenthetical. A "10,921 · 1 in 65" form
        # would return None here and silently drop the size sort on the card.
        self.assertEqual(S._sortkey(f['library_10']['value_en']), ('num', 10921.0))
        # No num/unit, deliberately, since 30 August 2026: library membership
        # is a static, undated running total, and cross_vein_pairs() used to
        # collide it against unrelated live crowd counts purely because both
        # landed in the same range. See seoul_index_post.py's library_facts().
        self.assertIsNone(f['library_10']['num'])
        self.assertIsNone(f['library_10']['unit'])

    def test_korean_counts_people_rather_than_translating_the_english(self):
        f = self.facts(self.pop_rows())
        self.assertEqual(f['library_10']['value_ko'], '10,921 (65명 중 1명)')

    def test_no_kosis_key_leaves_bare_counts_and_claims_nothing(self):
        f = self.facts(None, key=None)
        self.assertEqual(f['library_10']['value_en'], '10,921')
        self.assertEqual(S.LIBRARY_POP['en'], '')       # the seeded lie is gone

    def test_a_kosis_error_object_is_not_a_population(self):
        # KOSIS answers an outage, a dead key or a moved table with a DICT, not
        # a list, and with HTTP 200. Iterating it yields its keys as strings.
        f = self.facts({'err': '30', 'errMsg': '데이터가 존재하지 않습니다.'})
        self.assertEqual(f['library_10']['value_en'], '10,921')
        self.assertEqual(S.LIBRARY_POP['en'], '')

    def test_a_ratio_whose_month_cannot_be_stated_is_not_published(self):
        f = self.facts(self.pop_rows(prd='2026'))
        self.assertEqual(f['library_10']['value_en'], '10,921')
        self.assertEqual(S.LIBRARY_POP['en'], '')

    def test_a_decade_missing_half_its_population_gets_no_ratio(self):
        # Drop 15-19세 and nothing else: the teens must lose their ratio rather
        # than quietly divide by the 10-14 half, and every other decade keeps
        # its own. Halving the denominator would print "1 in 31" and look fine.
        half = {c: v for c, v in POP_BANDS.items() if c != '20'}
        f = self.facts(self.pop_rows(half))
        self.assertEqual(f['library_10']['value_en'], '10,921')
        self.assertEqual(f['library_30']['value_en'], '70,339 (1 in 21)')
        self.assertEqual(S.LIBRARY_POP['en'], 'July 2026')   # ratios did go out


# ---------------------------------------------------------------------------
# Card ordering
# ---------------------------------------------------------------------------
class SequenceVeinsKeepTheirOrder(unittest.TestCase):
    def test_year_and_level_veins_are_sequences_not_rankings(self):
        self.assertTrue({'level', 'complaint', 'infant'} <= S.ORDERED_CATS)

    def test_ranking_veins_are_not_in_the_set(self):
        # These are genuinely rankings and must keep the value sort.
        self.assertFalse({'price', 'water', 'daynight', 'library'} & S.ORDERED_CATS)

# ---------------------------------------------------------------------------
# boxoffice: the Seoul cut is the whole vein, and nothing announces its absence
# ---------------------------------------------------------------------------
# Drop wideAreaCd and KOBIS answers with national rows in the identical shape:
# same fields, same ranks, plausible numbers, no error. A card built on those
# would say Seoul and print the country, and every check short of comparing the
# two calls would pass. Hence a test that reads the URL rather than the output.
def _bo_rows(rows):
    return {'boxOfficeResult': {'boxofficeType': '일별 박스오피스',
                                'showRange': '20260822~20260822',
                                'dailyBoxOfficeList': rows}}


def _bo_row(cd, ko, audi, rank):
    return {'rank': str(rank), 'movieCd': cd, 'movieNm': ko,
            'audiCnt': str(audi), 'audiAcc': str(audi * 9),
            'salesAmt': str(audi * 11000), 'scrnCnt': '100', 'showCnt': '300'}


def _bo_info(en):
    return {'movieInfoResult': {'movieInfo': {'movieNmEn': en}}}


FIVE = [_bo_row('1', '오디세이', 132555, 1), _bo_row('2', '스파이더맨', 40901, 2),
        _bo_row('3', '인시디어스', 6438, 3), _bo_row('4', '코난', 6391, 4),
        _bo_row('5', '하츄핑', 4796, 5)]
TITLES = {'movieCd=1': _bo_info('The Odyssey'), 'movieCd=2': _bo_info('Spider-Man'),
          'movieCd=3': _bo_info('Insidious'), 'movieCd=4': _bo_info('Conan'),
          'movieCd=5': _bo_info('Hachupin')}


class BoxOfficeIsSeoulOnly(unittest.TestCase):

    def test_the_seoul_region_is_actually_requested(self):
        """Without this the vein silently posts national figures as Seoul's."""
        with Stub({'searchDailyBoxOfficeList': _bo_rows(FIVE), **TITLES}) as stub:
            S.boxoffice_facts('KEY')
        box = [u for u in stub.calls if 'searchDailyBoxOfficeList' in u]
        self.assertTrue(box, 'the box office was never called')
        for url in box:
            self.assertIn(f'wideAreaCd={S.KOBIS_SEOUL}', url)

    def test_it_walks_back_to_the_newest_day_with_rows(self):
        """A run before KOFIC posts must not date a card on an empty day."""
        state = {'n': 0}

        def rows(url):
            if 'searchMovieInfo' in url:
                return TITLES[[k for k in TITLES if k in url][0]]
            state['n'] += 1
            return _bo_rows([] if state['n'] == 1 else FIVE)

        real = S.http_get_json
        S.http_get_json = rows
        try:
            facts = S.boxoffice_facts('KEY')
        finally:
            S.http_get_json = real
        self.assertTrue(facts)
        self.assertGreaterEqual(state['n'], 2, 'it gave up on the first empty day')

    def test_a_film_without_an_english_title_silences_the_day(self):
        """The English card carries no Hangul and a romanization is a guess, so
        a film KOFIC has no English title for cannot go on the card. Since the
        card is the complete top four, that means no card at all: promoting the
        fifth film would fill the gap and hide it, which is the fault this vein
        was rebuilt to remove. The run must say why, or a vein that quietly
        stops posting looks like one that is simply never chosen."""
        titles = dict(TITLES, **{'movieCd=2': _bo_info('')})
        import io, contextlib
        out = io.StringIO()
        with Stub({'searchDailyBoxOfficeList': _bo_rows(FIVE), **titles}):
            with contextlib.redirect_stdout(out):
                facts = S.boxoffice_facts('KEY')
        self.assertEqual(facts, [])
        self.assertIn('no English title', out.getvalue())
        self.assertIn('스파이더맨', out.getvalue())

    def test_both_titles_are_pinned_as_published(self):
        """A film's Korean title is not a translation of its English one, so
        the selector must never be handed the job of producing it."""
        with Stub({'searchDailyBoxOfficeList': _bo_rows(FIVE), **TITLES}):
            facts = S.boxoffice_facts('KEY')
        f = next(f for f in facts if f['label_en'] == '"The Odyssey"')
        self.assertEqual(f['label_ko'], '"오디세이"')
        self.assertTrue(f['pin'])

    def test_the_card_can_be_dated(self):
        """boxoffice is in DATED_PERIOD_CATS, so compose() expects a period."""
        S.BOXOFFICE_D['en'] = S.BOXOFFICE_D['ko'] = None
        with Stub({'searchDailyBoxOfficeList': _bo_rows(FIVE), **TITLES}):
            S.boxoffice_facts('KEY')
        self.assertTrue(S.BOXOFFICE_D['en'] and S.BOXOFFICE_D['ko'])
        self.assertIn('boxoffice', S.DATED_PERIOD_CATS)

    def test_no_key_means_silence_not_an_error(self):
        self.assertEqual(S.boxoffice_facts(None), [])

    def test_a_short_day_produces_no_card(self):
        """Three films is not this card. The card IS the day's top four, so a
        short set is not a smaller card but a misleading one: a reader takes
        four lines with a hole in them for the ranking itself."""
        with Stub({'searchDailyBoxOfficeList': _bo_rows(FIVE[:3]), **TITLES}):
            self.assertEqual(S.boxoffice_facts('KEY'), [])

    def test_exactly_the_top_four_and_no_pair_facts(self):
        """Sales and tourism offer ten-odd candidates and a pair is a reason to
        choose two. This vein offers only what goes on the card, so a pair
        would be the same films again under another id."""
        with Stub({'searchDailyBoxOfficeList': _bo_rows(FIVE), **TITLES}):
            facts = [f for f in S.boxoffice_facts('KEY') if f['cat'] == 'boxoffice']
        self.assertEqual(len(facts), S.BOXOFFICE_N)
        self.assertTrue(all(f['pair'] is None for f in facts))
        self.assertEqual([f['label_en'] for f in facts],
                         ['"The Odyssey"', '"Spider-Man"', '"Insidious"',
                          '"Conan"'])


class ShoutedKobisTitlesAreFixed(unittest.TestCase):
    """KOFIC's own movieNmEn field is inconsistently cased: "The Odyssey" but
    also "THE END OF OAK STREET" (confirmed live against movieCd 20264557,
    27 Aug 2026), which reached a real card shouting. isupper() catches only
    the genuinely shouted titles and re-cases them, minor words lowercase
    except first and last."""

    def test_a_shouted_title_is_recased(self):
        self.assertEqual(S._fix_shouted_title('THE END OF OAK STREET'),
                         'The End of Oak Street')

    def test_a_properly_cased_title_is_left_alone(self):
        for t in ('The Odyssey', 'Spider-Man: Brand New Day',
                  'The Journey to Gyeongju'):
            self.assertEqual(S._fix_shouted_title(t), t)

    def test_an_internal_hyphen_is_recased_on_both_sides(self):
        self.assertEqual(S._fix_shouted_title('SPIDER-MAN RETURNS'),
                         'Spider-Man Returns')

    def test_first_and_last_word_stay_capitalised_even_if_minor(self):
        self.assertEqual(S._fix_shouted_title('OF MICE AND MEN'),
                         'Of Mice and Men')

    def test_empty_and_non_shouted_strings_pass_through(self):
        self.assertEqual(S._fix_shouted_title(''), '')
        self.assertEqual(S._fix_shouted_title('1987'), '1987')

    def test_the_fix_applies_through_kobis_title_en(self):
        """End to end, not just the helper: this is what actually reaches the
        card, via boxoffice_facts's own call to _kobis_title_en."""
        with Stub({'searchMovieInfo': _bo_info('THE END OF OAK STREET')}):
            self.assertEqual(S._kobis_title_en('KEY', '1'), 'The End of Oak Street')


class BoxOfficeCardIsAlwaysComplete(unittest.TestCase):
    """A selector that returns three films must not produce a three-film card.

    The hole is invisible on the card: four titles in descending order read as
    the ranking whether or not one is missing, which is why this is enforced
    rather than asked for.
    """

    def _pool(self):
        """Only the admissions frame: boxoffice_facts also returns the screens
        lines, and they belong to a different card."""
        with Stub({'searchDailyBoxOfficeList': _bo_rows(FIVE), **TITLES}):
            return [f for f in S.boxoffice_facts('KEY') if f['cat'] == 'boxoffice']

    def test_a_missing_film_is_added_back(self):
        pool = self._pool()
        picks = [{'id': f['id'], 'emoji': '🎬'} for f in pool[:3]]
        out = S.complete_boxoffice(picks, pool)
        self.assertEqual({p['id'] for p in out}, {f['id'] for f in pool})

    def test_a_film_added_back_carries_no_emoji(self):
        """So even_out_emoji strips the rest: a complete card with no emoji
        beats a partial one that looks styled."""
        pool = self._pool()
        picks = [{'id': f['id'], 'emoji': '🎬'} for f in pool[:3]]
        added = [p for p in S.complete_boxoffice(picks, pool)
                 if p['id'] == pool[3]['id']]
        self.assertEqual(added[0]['emoji'], '')

    def test_a_complete_card_is_untouched(self):
        pool = self._pool()
        picks = [{'id': f['id'], 'emoji': ''} for f in pool]
        self.assertEqual(S.complete_boxoffice(picks, pool), picks)

    def test_a_cross_pair_card_is_left_alone(self):
        """One film beside another vein's line is a legitimate cross pair, and
        completing the chart there would wreck the pairing."""
        pool = self._pool() + [S.fact('crowd_x', 'crowd', 'Hongdae', '1', '1')]
        picks = [{'id': pool[0]['id'], 'emoji': ''}, {'id': 'crowd_x', 'emoji': ''}]
        self.assertEqual(S.complete_boxoffice(picks, pool), picks)

class BoxOfficeEmojiAreAllOrNone(unittest.TestCase):
    """Every film carries an emoji or none does.

    The selector is told to tag a line only where an obvious emoji exists,
    which is right on a mixed card and wrong on a card of four films: the
    second live preview came back 🕷 Spider-Man, 👻 Insidious, 🕵️ Conan and a
    bare The Odyssey. Consistency across lines is a rule, not a judgement, so
    it is enforced here rather than asked for in the prompt.
    """

    def _lines(self, emoji, cat='boxoffice'):
        return [{'emoji': e, 'cat': cat} for e in emoji]

    def test_a_partial_set_is_cleared(self):
        lines = self._lines(['', '🕷', '👻', '🕵️'])
        S.even_out_emoji(lines, {'boxoffice'})
        self.assertEqual([l['emoji'] for l in lines], ['', '', '', ''])

    def test_a_complete_set_is_kept(self):
        lines = self._lines(['🎬', '🕷', '👻', '🕵️'])
        S.even_out_emoji(lines, {'boxoffice'})
        self.assertEqual([l['emoji'] for l in lines], ['🎬', '🕷', '👻', '🕵️'])

    def test_other_veins_are_evened_out_too(self):
        """Generalised to every category on 31 Aug 2026: a partial set on a
        spending card is cleared exactly like a partial set of films."""
        lines = self._lines(['☕', '', '📚'], cat='spending')
        S.even_out_emoji(lines, {'spending'})
        self.assertEqual([l['emoji'] for l in lines], ['', '', ''])

    def test_a_cross_pair_card_only_evens_out_its_films(self):
        """A boxoffice line can share a card with another vein on a cross pair,
        and the other vein's emoji are none of this rule's business."""
        lines = [{'emoji': '', 'cat': 'boxoffice'}, {'emoji': '🎬', 'cat': 'boxoffice'},
                 {'emoji': '👥', 'cat': 'crowd'}]
        S.even_out_emoji(lines, {'boxoffice', 'crowd'})
        self.assertEqual([l['emoji'] for l in lines], ['', '', '👥'])

def _scr_rows(scrn, cd, ko):
    r = _bo_row(cd, ko, 50000, 1)
    r['scrnCnt'] = str(scrn)
    return _bo_rows([r])


class ScreensFrameComparesLikeWithLike(unittest.TestCase):
    """The screens card sets today's top film against the same date five and
    ten years back. Two things can go wrong silently: the wrong dates, and the
    Seoul filter falling off one of the historical calls, either of which
    yields a plausible card that compares something else.
    """

    def _run(self, stub_extra=None):
        payloads = {'searchDailyBoxOfficeList': _bo_rows(FIVE), **TITLES}
        payloads.update(stub_extra or {})
        with Stub(payloads) as stub:
            facts = S.boxoffice_facts('KEY')
        return facts, stub

    def test_it_asks_for_the_same_date_five_and_ten_years_back(self):
        facts, stub = self._run()
        years = [f['id'].split('_')[-1] for f in facts if f['cat'] == 'boxhist']
        self.assertEqual(len(years), 3)
        newest = int(years[0])
        self.assertEqual([int(y) for y in years], [newest, newest - 5, newest - 10])

    def test_every_historical_call_keeps_the_seoul_filter(self):
        """Without it the older years quietly become national numbers, which
        are three to four times larger and would read as a collapse."""
        _, stub = self._run()
        box = [u for u in stub.calls if 'searchDailyBoxOfficeList' in u]
        self.assertGreaterEqual(len(box), 3)
        for url in box:
            self.assertIn(f'wideAreaCd={S.KOBIS_SEOUL}', url)

    def test_the_lines_are_a_sequence_not_a_ranking(self):
        """Value-sorting would scramble the years the moment a middle one came
        out highest, which is exactly what happened to the complaints card."""
        self.assertIn('boxhist', S.ORDERED_CATS)

    def test_the_label_carries_the_title_and_the_year(self):
        facts, _ = self._run()
        hist = [f for f in facts if f['cat'] == 'boxhist']
        for f in hist:
            # Year first, colon, then the title: the renderer bolds a leading
            # "YYYY:" and that only fires if the label is built this way.
            self.assertRegex(f['label_en'], r'^(19|20)\d\d: .')
            self.assertRegex(f['label_ko'], r'^(19|20)\d\d: .')
            self.assertTrue(f['pin'])

    def test_the_renderer_bolds_a_leading_year(self):
        import seoul_index_card as C
        row = C._row_html({'emoji': '', 'label': '2026: The Odyssey', 'value': '382'})
        self.assertIn('<b>2026:</b>', row)
        plain = C._row_html({'emoji': '', 'label': 'The Odyssey', 'value': '382'})
        self.assertNotIn('<b>', plain)

    def test_two_years_is_not_a_card(self):
        """A year that returns nothing drops out, and two lines is a
        comparison rather than a card."""
        state = {'n': 0}

        def get(url):
            if 'searchMovieInfo' in url:
                return TITLES[[k for k in TITLES if k in url][0]]
            state['n'] += 1
            return _bo_rows(FIVE) if state['n'] <= 2 else _bo_rows([])

        real = S.http_get_json
        S.http_get_json = get
        try:
            facts = S.boxoffice_facts('KEY')
        finally:
            S.http_get_json = real
        self.assertEqual([f for f in facts if f['cat'] == 'boxhist'], [])
        self.assertTrue([f for f in facts if f['cat'] == 'boxoffice'],
                        'the admissions frame should survive on its own')

    def test_a_leap_day_falls_back_to_the_28th(self):
        """29 Feb 2028 has no counterpart in 2023, and replace() would raise."""
        import datetime
        leap = datetime.date(2028, 2, 29)
        self.assertEqual(S._same_date(leap, 5), datetime.date(2023, 2, 28))
        self.assertEqual(S._same_date(datetime.date(2026, 8, 22), 10),
                         datetime.date(2016, 8, 22))

    def test_the_years_compared_are_inside_the_reliable_era(self):
        """⚠️ The ticketing network covered about half of screens in 2005 and
        86% in 2006, so a twenty-year comparison would measure how many cinemas
        reported rather than how many screens ran the film. Both offsets must
        stay small enough to keep every year at ≥98% coverage, i.e. 2008 on.
        """
        self.assertEqual(S.SCREENS_YEARS, (5, 10))
        self.assertLessEqual(max(S.SCREENS_YEARS), 17)

class AirportMonthRidesTheMasthead(unittest.TestCase):
    """The month a Gimpo card covers belongs on the masthead when the card is one
    month, and on every row when it is two.

    The card of 27 August 2026 got this backwards: three rows each ending
    "July 2026" and no dateline at all, where the property card the same week
    flew "June 2026" in red under its title. But the fix cannot simply move the
    month, because the twenty-year frame puts July 2026 beside July 2006 and
    there the month IS the discriminator — a masthead over that card would be a
    claim about a line it does not cover.

    End to end through kac_facts and the real compose(), because the rule spans
    both: the harvester writes the labels and compose decides what lifts, and a
    unit test of either half alone passes while the card still reads wrong.
    """

    NOW_PAX, DOM, INTL, FLIGHTS, THEN_PAX = 1856656, 1412573, 444083, 10434, 733168

    def facts(self):
        import subprocess as real_subprocess
        from datetime import datetime

        today = datetime.now(S.SEOUL_TZ).date()
        prev = (today.replace(day=1) - timedelta(days=1))
        self.y, self.m = prev.year, prev.month

        def run(cmd, **kw):
            url = cmd[-1]
            ym = re.search(r'startDePd=(\d{6})', url).group(1)
            route = re.search(r'routeBe=(\d)', url)
            year = int(ym[:4])
            if year == self.y:
                pax = (self.DOM if route and route.group(1) == '0'
                       else self.INTL if route else self.NOW_PAX)
            else:
                pax = self.THEN_PAX
            xml = ('<response><body><items><item>'
                   '<Airport>김포</Airport>'
                   f'<subpassenger>{pax}</subpassenger>'
                   f'<Subflgt>{self.FLIGHTS}</Subflgt>'
                   '</item></items></body></response>')
            return types.SimpleNamespace(stdout=xml, returncode=0)

        S.subprocess.run = run
        try:
            facts = S.kac_facts('KEY')
        finally:
            S.subprocess.run = real_subprocess.run
        self.assertTrue(facts, 'the airport vein returned nothing')
        return {f['id']: f for f in facts}

    def card(self, ids):
        by_id = self.facts()
        pool = [by_id[i] for i in ids]
        sel = {'opener_en': 'Through Gimpo airport', 'opener_ko': '김포공항에서',
               'opener_emoji': '✈️', 'picks': [{'id': i} for i in ids]}
        return S.compose(sel, pool)

    def test_one_month_lifts_to_the_dateline_and_leaves_the_rows_bare(self):
        c = self.card(['kac_dom', 'kac_intl', 'kac_flights_now'])
        self.assertEqual(c['dateline_en'], f'{S.MONTHS_EN[self.m - 1]} {self.y}')
        self.assertEqual(c['dateline_ko'], f'{self.y}년 {self.m}월')
        self.assertEqual([l['label_en'] for l in c['lines']],
                         ['Domestic passengers', 'International passengers',
                          'Flights in and out'])
        # Both languages, or a card is stripped in one and repeating itself in
        # the other — and only the English reader would ever see the difference.
        for l in c['lines']:
            self.assertNotIn(str(self.y), l['label_en'])
            self.assertNotIn(str(self.y), l['label_ko'])
        # The masthead is the ONLY place the month appears: not the footnote too.
        self.assertNotIn(str(self.y), c['note_en'])
        self.assertNotIn(str(self.y), c['note_ko'])
        # The alt text carries it as its own line, so a screen-reader user is
        # told the month exactly once, as a sighted reader is.
        self.assertIn(f'\n{c["dateline_en"]}\n', c['en_body'])

    def test_two_months_lift_nothing_and_every_row_keeps_its_own(self):
        c = self.card(['kac_pax_now', 'kac_pax_then', 'kac_flights_now'])
        self.assertEqual(c['dateline_en'], '')
        self.assertEqual(c['dateline_ko'], '')
        then = self.y - S.KAC_YEARS_BACK
        self.assertEqual(
            [l['label_en'] for l in c['lines']],
            [f'Passengers through Gimpo, {S.MONTHS_EN[self.m - 1]} {self.y}',
             f'Passengers through Gimpo, {S.MONTHS_EN[self.m - 1]} {then}',
             f'Flights in and out, {S.MONTHS_EN[self.m - 1]} {self.y}'])
        self.assertEqual(
            [l['label_ko'] for l in c['lines']],
            [f'김포공항 이용객, {self.y}년 {self.m}월',
             f'김포공항 이용객, {then}년 {self.m}월',
             f'운항 편수, {self.y}년 {self.m}월'])


class MastheadCheckIsNotVeinSpecific(unittest.TestCase):
    """check_masthead asks the finished card whether a date is on every row and
    nowhere above them.

    The airport vein was fixed where it lives, but the SHAPE is not
    vein-specific: the next vein to bake a month into a label would repeat it in
    silence, because that is not a card that looks broken. It renders perfectly
    and merely says the same four words three times.

    ⚠️ The false-negative tests matter as much as the positive one. Four veins
    here deliberately keep a date OFF the masthead — the weather season span,
    the books window, the OECD vintage, the library-ratio population month —
    each commented with its reason, so a check that argued for lifting every
    shared clause would fight four decisions made on purpose. This one reports
    and repairs nothing.
    """

    def lines(self, *labels_en, ko=None):
        ko = ko or [''] * len(labels_en)
        return [{'label_en': e, 'label_ko': k} for e, k in zip(labels_en, ko)]

    def check(self, lines, dl_en='', dl_ko='', grouped=False):
        return S.check_masthead(lines, dl_en, dl_ko, grouped, log=lambda m: None)

    def test_the_card_that_shipped_is_found(self):
        found = self.check(self.lines('Domestic passengers, July 2026',
                                      'International passengers, July 2026',
                                      'Flights in and out, July 2026'))
        self.assertEqual([f['lang'] for f in found], ['en'])
        self.assertEqual(found[0]['tail'], 'July 2026')

    def test_a_lifted_dateline_is_clean(self):
        # Not merely because the labels were stripped: a card that HAS a
        # masthead is short-circuited before the labels are looked at, since
        # whatever they say the date is already above them.
        self.assertEqual(
            self.check(self.lines('Domestic passengers', 'International passengers',
                                  'Flights in and out'), dl_en='July 2026'), [])

    def test_a_grouped_card_is_clean(self):
        # A live+dated cross pair carries its date on a group subhead and
        # correctly flies no masthead. Judging it would raise a finding on the
        # layout working.
        self.assertEqual(
            self.check(self.lines('Domestic passengers, July 2026',
                                  'International passengers, July 2026'),
                       grouped=True), [])

    def test_a_date_per_row_is_a_discriminator(self):
        # The twenty-year frame: a masthead over it would be a claim about a
        # line it does not cover.
        self.assertEqual(
            self.check(self.lines('Passengers through Gimpo, July 2026',
                                  'Passengers through Gimpo, July 2006',
                                  'Flights in and out, July 2026')), [])

    def test_one_undated_row_means_no_masthead_was_possible(self):
        self.assertEqual(
            self.check(self.lines('Domestic passengers, July 2026',
                                  'International passengers, July 2026',
                                  'Flights in and out')), [])

    def test_a_year_that_leads_a_label_is_left_alone(self):
        # boxhist writes "2026: The Odyssey". The year is the row's whole point.
        self.assertEqual(
            self.check(self.lines('2026: The Odyssey', '2021: Escape from Mogadishu',
                                  '2016: Train to Busan')), [])

    def test_the_pattern_itself_refuses_a_leading_year(self):
        # ⚠️ Pinned at the REGEX, because no card can pin it: the boxhist rows
        # above are three different years, so they fail the shared-tail test for
        # a reason that has nothing to do with the anchor, and a mutation
        # removing it passes the whole class. This is the only place the anchor
        # is decidable, and an untested guard is a guard that quietly leaves.
        self.assertIsNone(S._TRAILING_YEAR.search('2026: The Odyssey'))
        self.assertIsNone(S._TRAILING_YEAR.search('Summer 2026'))
        self.assertEqual(
            S._TRAILING_YEAR.search('Domestic passengers, July 2026').group(1),
            'July 2026')
        # The date must TRAIL. A year mid-label is the label's own business.
        self.assertIsNone(
            S._TRAILING_YEAR.search('Passengers, July 2026, both terminals'))

    def test_each_language_is_judged_alone(self):
        # ⚠️ Python writes the English and the selector writes some of the
        # Korean, so a card can be tidy in one and repeating itself in the
        # other. A both-languages test would pass this card.
        found = self.check(self.lines(
            'Domestic passengers', 'International passengers', 'Flights in and out',
            ko=['국내선 이용객, 2026년 7월', '국제선 이용객, 2026년 7월',
                '운항 편수, 2026년 7월']))
        self.assertEqual([f['lang'] for f in found], ['ko'])

    def test_a_bare_period_row_is_not_a_repeated_date(self):
        # The weather then-and-now layout draws the metric as a subhead and the
        # PERIODS as the rows. They are bare and they differ, so nothing here
        # could match — but if the regex ever lost its comma anchor it would
        # flag every then-and-now card the account has ever posted.
        self.assertEqual(self.check(self.lines('Summer 2026', 'Summer 1976')), [])

    def test_the_published_history_is_clean_but_for_the_one_card(self):
        # ⚠️ Measured, not asserted from taste: every card the account has
        # posted, replayed through the check under the WORST assumption (that
        # none of them flew a dateline, which is the loudest this can be). One
        # hit, and it is the Gimpo card. A second hit here means either a new
        # fault or a check that has started crying wolf, and both want reading.
        log = Path(__file__).resolve().parent / 'card_history.jsonl'
        if not log.exists():                 # a fresh checkout has no history
            self.skipTest('no card_history.jsonl in this checkout')
        hits = []
        for ln in log.read_text(encoding='utf-8').splitlines():
            if not ln.strip():
                continue
            rec = json.loads(ln)
            rows = [{'label_en': x.get('label', ''), 'label_ko': ''}
                    for x in rec.get('lines', [])]
            if self.check(rows):
                hits.append(rec.get('url') or rec.get('at'))
        self.assertEqual(
            len(hits), 1,
            f'expected only the Gimpo card of 27 August 2026; got {hits}')


class IncheonCardLabels(unittest.TestCase):
    """iiac_facts() sums the API's own per-row passenger/flight counts by
    country and returns the busiest one — counting, not modelling — plus the
    masthead/strip behaviour is the same shape as kac_facts()'s single-month
    frame (AirportMonthRidesTheMasthead above), so it is checked the same way,
    end to end through compose()."""

    def facts(self, rows, ym='202607'):
        import subprocess as real_subprocess

        def run(cmd, **kw):
            items = [dict(r, paxCode='여객', yearMonth=ym) for r in rows]
            body = json.dumps({'response': {'body': {'items': items}}})
            return types.SimpleNamespace(stdout=body, returncode=0)

        S.subprocess.run = run
        try:
            facts = S.iiac_facts('KEY')
        finally:
            S.subprocess.run = real_subprocess.run
        return {f['id']: f for f in facts}

    def test_passengers_and_flights_sum_across_every_row(self):
        by_id = self.facts([
            {'nationName': '일본', 'totalEff': '100', 'flightCount': '5'},
            {'nationName': '일본', 'totalEff': '50', 'flightCount': '3'},
            {'nationName': '중국', 'totalEff': '80', 'flightCount': '4'},
        ])
        self.assertEqual(by_id['iiac_pax_total']['value_en'], '230')
        self.assertEqual(by_id['iiac_flights_total']['value_en'], '12')

    def test_the_busiest_country_wins_and_carries_the_month(self):
        by_id = self.facts([
            {'nationName': '일본', 'totalEff': '100', 'flightCount': '5'},
            {'nationName': '중국', 'totalEff': '80', 'flightCount': '4'},
        ])
        top = by_id['iiac_top_country']
        self.assertEqual(top['label_en'], 'Passengers to Japan, July 2026')
        self.assertEqual(top['value_en'], '100')
        self.assertEqual(top['num'], 100)
        self.assertEqual(top['unit'], 'people')

    def test_a_cargo_only_row_is_excluded_from_the_passenger_total(self):
        # paxCode is forced to '여객' by the fixture above, so this instead
        # pins the filter itself: _iiac_rows() must drop a 화물 row rather
        # than silently summing cargo-flight passenger fields into the total.
        import subprocess as real_subprocess

        def run(cmd, **kw):
            items = [
                {'nationName': '일본', 'paxCode': '여객', 'totalEff': '100',
                 'flightCount': '5', 'yearMonth': '202607'},
                {'nationName': '일본', 'paxCode': '화물', 'totalEff': '999',
                 'flightCount': '99', 'yearMonth': '202607'},
            ]
            body = json.dumps({'response': {'body': {'items': items}}})
            return types.SimpleNamespace(stdout=body, returncode=0)

        S.subprocess.run = run
        try:
            facts = S.iiac_facts('KEY')
        finally:
            S.subprocess.run = real_subprocess.run
        by_id = {f['id']: f for f in facts}
        self.assertEqual(by_id['iiac_pax_total']['value_en'], '100')

    def test_an_unmapped_country_falls_back_to_korean_and_warns(self):
        import io, contextlib
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            by_id = self.facts([
                {'nationName': '없는나라', 'totalEff': '10', 'flightCount': '1'},
            ])
        self.assertIn('없는나라', by_id['iiac_top_country']['label_en'])
        self.assertIn('없는나라', out.getvalue())

    def test_no_key_returns_nothing(self):
        self.assertEqual(S.iiac_facts(None), [])
        self.assertEqual(S.iiac_facts(''), [])

    def test_unparseable_response_returns_nothing_not_a_crash(self):
        import subprocess as real_subprocess
        S.subprocess.run = lambda *a, **kw: types.SimpleNamespace(
            stdout='not json', returncode=1)
        try:
            self.assertEqual(S.iiac_facts('KEY'), [])
        finally:
            S.subprocess.run = real_subprocess.run

    def card(self, ids, rows):
        by_id = self.facts(rows)
        pool = [by_id[i] for i in ids]
        sel = {'opener_en': 'Incheon, one month of routes',
               'opener_ko': '인천공항에서', 'opener_emoji': '✈️',
               'picks': [{'id': i} for i in ids]}
        return S.compose(sel, pool)

    def test_the_month_lifts_to_the_dateline_and_leaves_the_rows_bare(self):
        rows = [{'nationName': '일본', 'totalEff': '100', 'flightCount': '5'}]
        c = self.card(['iiac_pax_total', 'iiac_flights_total',
                       'iiac_top_country'], rows)
        self.assertEqual(c['dateline_en'], 'July 2026')
        self.assertEqual(c['dateline_ko'], '2026년 7월')
        # compose() orders rows by its own logic (unrelated to what this test
        # checks), so this compares the SET of labels, not their sequence.
        self.assertCountEqual(
            [l['label_en'] for l in c['lines']],
            ['Passengers through Incheon', 'Flights in and out',
             'Passengers to Japan'])
        for l in c['lines']:
            self.assertNotIn('2026', l['label_en'])


class KorailCardLabels(unittest.TestCase):
    """rail_facts() reads two operations off one Korail base — intercity
    (mainLineRoutePer) and commuter (wideRailloadRoutePer) — and each must
    sum only its OWN newest run_ym.

    ⚠️ The trap this exists to catch: both operations return roughly a year
    of history with no date filter available, so a fixture carrying two
    months is the only way to prove the older one is excluded rather than
    quietly folded into the total. Measured live 4 September 2026: doing this
    the naive way overstated a month's ridership by roughly 12x."""

    def facts(self, inter_rows=(), comm_rows=()):
        import subprocess as real_subprocess

        def run(cmd, **kw):
            url = cmd[-1]
            if 'mainLineRoutePer' in url:
                items = list(inter_rows)
            elif 'wideRailloadRoutePer' in url:
                items = list(comm_rows)
            else:
                items = []
            body = json.dumps({'response': {'body': {'items': {'item': items}}}})
            return types.SimpleNamespace(stdout=body, returncode=0)

        S.subprocess.run = run
        try:
            facts = S.rail_facts('KEY')
        finally:
            S.subprocess.run = real_subprocess.run
        return {f['id']: f for f in facts}

    def test_only_the_newest_month_is_summed_not_all_of_history(self):
        by_id = self.facts(inter_rows=[
            {'run_ym': '202607', 'rte_nm': '경부선', 'utztn_nope': '100'},
            {'run_ym': '202607', 'rte_nm': '경부선', 'utztn_nope': '50'},
            # An older month for the same route: must be excluded entirely,
            # not summed into "one month's" total.
            {'run_ym': '202606', 'rte_nm': '경부선', 'utztn_nope': '9000'},
        ])
        top = by_id['korail_inter_top']
        self.assertEqual(top['value_en'], '150')
        self.assertEqual(top['label_en'], 'Riders on the Gyeongbu Line, July 2026')

    def test_rows_for_one_route_sum_across_train_models(self):
        # mainLineRoutePer carries one row per (route, car model). Two models
        # on the same route in the same month must add, not overwrite.
        by_id = self.facts(inter_rows=[
            {'run_ym': '202607', 'rte_nm': '경부선', 'utztn_nope': '100'},
            {'run_ym': '202607', 'rte_nm': '경부선', 'utztn_nope': '50'},
        ])
        self.assertEqual(by_id['korail_inter_top']['value_en'], '150')

    def test_an_older_months_bigger_number_does_not_win(self):
        # Honam's older-month figure (999999) dwarfs Gyeongbu's current one
        # (100) and must not win just because it's a bigger number sitting in
        # the same response — the newest-month filter has to run BEFORE the
        # max is taken, not just before the sum.
        by_id = self.facts(inter_rows=[
            {'run_ym': '202607', 'rte_nm': '경부선', 'utztn_nope': '100'},
            {'run_ym': '202606', 'rte_nm': '호남선', 'utztn_nope': '999999'},
        ])
        self.assertEqual(by_id['korail_inter_top']['value_en'], '100')
        self.assertIn('Gyeongbu', by_id['korail_inter_top']['label_en'])

    def test_commuter_and_intercity_are_independent(self):
        by_id = self.facts(
            inter_rows=[{'run_ym': '202607', 'rte_nm': '경부선',
                        'utztn_nope': '100'}],
            comm_rows=[{'run_ym': '202607', 'sbwy_ln_nm': '분당선',
                       'ride_nope': '200'}])
        self.assertIn('korail_inter_top', by_id)
        self.assertIn('korail_comm_top', by_id)
        self.assertEqual(by_id['korail_comm_top']['label_en'],
                         'Boardings on the Bundang Line, July 2026')
        self.assertEqual(by_id['korail_comm_top']['value_en'], '200')

    def test_commuter_total_sums_every_line_not_just_the_top_one(self):
        by_id = self.facts(comm_rows=[
            {'run_ym': '202607', 'sbwy_ln_nm': '분당선', 'ride_nope': '200'},
            {'run_ym': '202607', 'sbwy_ln_nm': '경인선', 'ride_nope': '150'},
            # An older month must be excluded from the total too.
            {'run_ym': '202606', 'sbwy_ln_nm': '분당선', 'ride_nope': '99999'},
        ])
        self.assertEqual(by_id['korail_comm_total']['value_en'], '350')

    def test_three_facts_is_the_solo_card_floor(self):
        # build_pool()'s own minimum for a vein to carry a card alone
        # (--only=<cat>) is 3 lines. Pin the count here so a future edit
        # that quietly drops one of the three doesn't only surface as
        # rail never getting promoted on its own, weeks later.
        by_id = self.facts(
            inter_rows=[{'run_ym': '202607', 'rte_nm': '경부선',
                        'utztn_nope': '100'}],
            comm_rows=[{'run_ym': '202607', 'sbwy_ln_nm': '분당선',
                       'ride_nope': '200'}])
        self.assertEqual(len(by_id), 3)

    def test_an_unmapped_line_falls_back_to_korean_and_warns(self):
        import io, contextlib
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            by_id = self.facts(inter_rows=[
                {'run_ym': '202607', 'rte_nm': '없는선', 'utztn_nope': '10'}])
        self.assertIn('없는선', by_id['korail_inter_top']['label_en'])
        self.assertIn('없는선', out.getvalue())

    def test_no_key_returns_nothing(self):
        self.assertEqual(S.rail_facts(None), [])

    def test_a_dead_operation_does_not_block_the_other(self):
        # Only the intercity call answers; commuter returns unparseable
        # output. korail_inter_top must still be produced.
        import subprocess as real_subprocess

        def run(cmd, **kw):
            url = cmd[-1]
            if 'mainLineRoutePer' in url:
                items = [{'run_ym': '202607', 'rte_nm': '경부선',
                         'utztn_nope': '100'}]
                return types.SimpleNamespace(
                    stdout=json.dumps(
                        {'response': {'body': {'items': {'item': items}}}}),
                    returncode=0)
            return types.SimpleNamespace(stdout='not json', returncode=1)

        S.subprocess.run = run
        try:
            facts = S.rail_facts('KEY')
        finally:
            S.subprocess.run = real_subprocess.run
        by_id = {f['id']: f for f in facts}
        self.assertIn('korail_inter_top', by_id)
        self.assertNotIn('korail_comm_top', by_id)

    def card(self, ids, **rows):
        by_id = self.facts(**rows)
        pool = [by_id[i] for i in ids]
        sel = {'opener_en': "Korea's rail lines, one month",
               'opener_ko': '한국의 철도', 'opener_emoji': '🚄',
               'picks': [{'id': i} for i in ids]}
        return S.compose(sel, pool)

    def test_the_month_lifts_to_the_dateline_and_leaves_the_rows_bare(self):
        c = self.card(['korail_inter_top', 'korail_comm_top',
                       'korail_comm_total'],
                      inter_rows=[{'run_ym': '202607', 'rte_nm': '경부선',
                                  'utztn_nope': '100'}],
                      comm_rows=[{'run_ym': '202607', 'sbwy_ln_nm': '분당선',
                                 'ride_nope': '200'}])
        self.assertEqual(c['dateline_en'], 'July 2026')
        # compose() orders rows by its own logic (unrelated to what this test
        # checks), so this compares the SET of labels, not their sequence.
        self.assertCountEqual(
            [l['label_en'] for l in c['lines']],
            ['Riders on the Gyeongbu Line', 'Boardings on the Bundang Line',
             'Commuter rail boardings'])
        for l in c['lines']:
            self.assertNotIn('2026', l['label_en'])


class CultureCardLabels(unittest.TestCase):
    """A museum card's own opener already says "A year at Seoul's museums"
    (서울 박물관의 1년), so a per-line "A year's visitors to..." (연간 관람객)
    repeated the year a third time — caught on the 28 Aug 2026 card
    (3mu3yywrhdj2x). Same fix in both languages, and 'culture' also became
    subject to even_out_emoji the same day: the card mixes visitor totals
    and facility counts, so an obvious emoji exists for some lines and not
    others, which is the "three of four" look that rule exists to prevent.
    Generalised from a 3-category allowlist to every category on 31 Aug
    2026, so this is now no different from any other vein — see
    BoxOfficeEmojiAreAllOrNone.test_other_veins_are_evened_out_too.
    """

    def facts(self):
        import subprocess as real_subprocess

        def run(cmd, **kw):
            url = cmd[-1]
            if 'clifMsmv1' in url:
                rows = [{'sggCd': '11110', 'crtrYr': '2023',
                         'msmNm': '국립중앙박물관', 'fyerVwngNope': '4180285'}]
            elif 'clifArglv1' in url:
                rows = [{'sggCd': '11110', 'arglNm': '서울시립미술관',
                         'fyerVwngNope': '2096952'}]
            else:
                rows = []
            body = json.dumps({'response': {'body': {'data': rows}}})
            return types.SimpleNamespace(stdout=body, returncode=0)

        S.subprocess.run = run
        try:
            facts = S.culture_facts('KEY')
        finally:
            S.subprocess.run = real_subprocess.run
        self.assertTrue(facts, 'the culture vein returned nothing')
        return {f['id']: f for f in facts}

    def test_no_redundant_a_years_prefix(self):
        top = self.facts()['culture_top_msm']
        self.assertEqual(top['label_en'], 'Visitors to the National Museum of Korea')
        self.assertNotIn('year', top['label_en'].lower())
        self.assertEqual(top['label_ko'], '국립중앙박물관 관람객')
        self.assertNotIn('연간', top['label_ko'])

    def test_culture_is_emoji_all_or_none(self):
        lines = [{'emoji': '🏛', 'cat': 'culture'}, {'emoji': '', 'cat': 'culture'}]
        S.even_out_emoji(lines, {'culture'})
        self.assertEqual([l['emoji'] for l in lines], ['', ''])


class NothingFilesFromATestRun(unittest.TestCase):
    """⚠️ THE TEST SUITE MUST NOT WRITE TO THE ESTATE'S SHARED LOG, and for two
    months it did.

    Every `_observe_*` here refused a `--dry-run` and still filed from a test,
    because DRY_RUN reads `sys.argv` and a test process's argv says nothing
    about --dry-run. Measured 27 August 2026: 148 `seoul-index-korean` findings
    in memory/observations.jsonl, six per suite run, every one of them a
    synthetic label from a fixture — and the Sunday estate-review reads that key
    as one fault recurring for weeks. The check whose job is to keep that log
    honest was the thing filling it with noise.

    Pinned here rather than left to each author's memory, because it is exactly
    the kind of rule the next test file forgets.
    """

    def state(self, name, dry, observe):
        old = (S.__name__, S.DRY_RUN, S.OBSERVE)
        S.__dict__['__name__'], S.DRY_RUN, S.OBSERVE = name, dry, observe
        try:
            return S.reporting()
        finally:
            S.__dict__['__name__'], S.DRY_RUN, S.OBSERVE = old

    def test_an_imported_module_never_reports(self):
        # This is the case that was broken, and it is how the suite sees it.
        self.assertFalse(self.state('seoul_index_post', False, S.OBSERVE))

    def test_a_dry_run_never_reports(self):
        self.assertFalse(self.state('__main__', True, S.OBSERVE))

    def test_a_missing_observe_never_reports(self):
        self.assertFalse(self.state('__main__', False, Path('/nonexistent')))

    def test_a_real_run_does_report(self):
        # ⚠️ The positive case matters as much: a guard that refuses everything
        # silences the checks it is protecting, and a log nobody writes to reads
        # exactly like an estate with nothing wrong in it.
        if not S.OBSERVE.exists():
            self.skipTest('observe.py not installed in this checkout')
        self.assertTrue(self.state('__main__', False, S.OBSERVE))

    def test_this_module_is_actually_imported_right_now(self):
        # Belt: if the suite ever ran the bot as __main__, every test above
        # would still pass while the real log filled up again.
        self.assertNotEqual(S.__name__, '__main__')


class OpenersAreNotCutMidPhrase(unittest.TestCase):
    """A hard slice at the cap shipped "…film, the same :" to a live render.

    This is not a box office bug: every vein's opener went through it, and a
    truncated opener reads as a broken bot rather than as a terse one.
    """

    def test_a_long_opener_keeps_whole_words(self):
        long = 'Screens for Seoul’s most-watched film, the same date every year'
        out = S.clean_opener(long, 'fallback')
        self.assertLessEqual(len(out), S.OPENER_MAX)
        self.assertTrue(long.startswith(out), 'the trim must be a prefix')
        self.assertFalse(out.endswith(' '))
        self.assertTrue(out.split()[-1] in long.split(),
                        f'{out!r} ends mid-word')

    def test_no_dangling_punctuation(self):
        out = S.clean_opener('Screens for Seoul’s most-watched film, the same date', 'x')
        self.assertFalse(out.rstrip().endswith((',', ':', ';', '·', '-')))

    def test_a_short_opener_is_untouched(self):
        for s in ('Cinema admissions in Seoul', 'Seoul by the numbers'):
            self.assertEqual(S.clean_opener(s, 'x'), s)

    def test_an_empty_opener_falls_back(self):
        self.assertEqual(S.clean_opener('   ', 'Seoul by the numbers'),
                         'Seoul by the numbers')


class BusRoutesVein(unittest.TestCase):
    """Added 9 Sep 2026; rebuilt 10 Sep 2026 on BOARDINGS PER STOP SERVED,
    from the history file, at the user's direction, after 63 days showed
    the raw ranking fixed at both ends (143, the longest route, busiest
    every day; 1226 quietest every day). These tests cover what would ship
    the ranking wrong rather than not at all: a route split across two
    RTE_IDs double-counted or halved, a Hangul route number or a tailored/
    night/express route reaching the ranking, a short shuttle leading on
    two stops, and the fourth line drifting off the card's one unit.
    """

    SUB_ROWS = [{'SBWY_STNS_NM': '홍대입구', 'GTON_TNOPE': '71953'},
                {'SBWY_STNS_NM': '임진강', 'GTON_TNOPE': '57'}]

    @staticmethod
    def _rows(no, rte_id, per_stop, stops, first=0):
        # Stop ids are distinct per (route, index) — `first` offsets them so a
        # second direction gets its own stops rather than re-serving the same.
        return [{'RTE_ID': rte_id, 'RTE_NO': no, 'RTE_NM': f'{no}번(A~B)',
                 'STOPS_ID': f'{no}-{first + i}', 'GTON_TNOPE': per_stop} for i in range(stops)]

    # Four rankable routes: 100 at 30 a stop (300 over 10), 1129 at 25 (split
    # across two RTE_IDs, 5 stops each — the real verified split is N13, one
    # RTE_ID per direction; night routes left the ranking, so the split is
    # carried on a branch number, the grouping being by RTE_NO regardless),
    # 272 at 9 (108 over 12) and 7719 at 1 (10 over 10).
    FIVE_ROUTES = (_rows.__func__('100', '11110001', 30, 10) + _rows.__func__('1129', '11110363', 25, 5)
                   + _rows.__func__('1129', '11110364', 25, 5, first=5) + _rows.__func__('272', '2', 9, 12)
                   + _rows.__func__('7719', '3', 1, 10))

    def setUp(self):
        # transport_facts() records the stubbed day into the history and the
        # card is built from it, so every test needs a fresh, empty history.
        S.BUS_HISTORY = _Path(_tempfile.mkdtemp()) / 'bus_route_history.json'

    def _facts(self, bus_rows, state=None):
        with Stub({'CardSubwayStatsNew': ok('CardSubwayStatsNew', self.SUB_ROWS),
                   'CardBusStatisticsServiceNew': ok('CardBusStatisticsServiceNew', bus_rows)}):
            return S.transport_facts('unused-key', state if state is not None else {})

    def by_id(self, facts, fid):
        return next((f for f in facts if f['id'] == fid), None)

    def test_a_route_split_across_two_directions_sums_not_doubles(self):
        facts = self._facts(self.FIVE_ROUTES)
        second = self.by_id(facts, 'bus_second_route')
        self.assertIsNotNone(second, 'busroutes withheld')
        # 250 boardings over 10 stops across both RTE_IDs: 25 a stop.
        self.assertEqual(second['label_en'], '2nd-busiest: 1129, 10 stops')
        self.assertEqual(second['value_en'], '25')
        self.assertEqual(second['cat'], 'busroutes'); self.assertTrue(second['pin'])

    def test_ranked_by_boardings_per_stop_with_the_average_as_fourth_line(self):
        facts = self._facts(self.FIVE_ROUTES)
        top = self.by_id(facts, 'bus_busiest_route')
        self.assertEqual(top['label_en'], 'Busiest: 100, 10 stops')
        self.assertEqual(top['value_en'], '30')
        self.assertEqual(top['label_ko'], '가장 붐빔: 100번, 정류장 10곳')
        quiet = self.by_id(facts, 'bus_quietest_route')
        self.assertEqual(quiet['label_en'], 'Quietest: 7719, 10 stops')
        self.assertEqual(quiet['value_en'], '1')
        avg = self.by_id(facts, 'bus_route_total')
        self.assertEqual(avg['label_en'], 'Average per stop, all routes')
        self.assertEqual(avg['value_en'], '16')      # 668 boardings over 42 stops
        info = S.RANKED_CARD_INFO['busroutes']
        self.assertTrue(info['note_en'].startswith('Boardings per stop served'))
        self.assertEqual([no for _, _, no in info['map_routes']], ['100', '1129', '7719'])

    def test_hangul_tailored_night_and_express_routes_never_rank(self):
        # Each would lead (or trail) if counted: village bus and rush variant
        # at 1,000 a stop, 8641 and N13 at 500, 9401 at 400, all on 12 stops.
        rows = self.FIVE_ROUTES
        for no, per in (('마포01', 1000), ('8442퇴근', 1000), ('8641', 500), ('N13', 500), ('9401', 400)):
            rows = rows + self._rows(no, no, per, 12)
        facts = self._facts(rows)
        self.assertEqual(self.by_id(facts, 'bus_busiest_route')['label_en'], 'Busiest: 100, 10 stops')
        for f in facts:
            self.assertNotIn('마포', f['label_en']); self.assertNotIn('퇴근', f['label_en'])
        # The average is over ranked routes only, so it is unchanged too.
        self.assertEqual(self.by_id(facts, 'bus_route_total')['value_en'], '16')

    def test_a_route_with_too_few_stops_served_never_ranks(self):
        # 150 at 1 a stop on 5 stops would be the quietest if counted.
        rows = self.FIVE_ROUTES + self._rows('150', '9', 1, 5)
        facts = self._facts(rows)
        self.assertEqual(self.by_id(facts, 'bus_quietest_route')['label_en'], 'Quietest: 7719, 10 stops')

    def test_no_bus_data_at_all_withholds_the_whole_ranking(self):
        facts = self._facts([])
        for fid in ('bus_busiest_route', 'bus_second_route',
                    'bus_quietest_route', 'bus_route_total'):
            self.assertIsNone(self.by_id(facts, fid))
        self.assertNotIn('busroutes', S.RANKED_CARD_INFO)

    def test_fewer_than_three_rankable_routes_withholds_rather_than_crashes(self):
        rows = self._rows('100', '1', 30, 10) + self._rows('272', '2', 9, 12)
        facts = self._facts(rows)
        self.assertIsNone(self.by_id(facts, 'bus_busiest_route'))
        # The rest of the vein still posts.
        self.assertIsNotNone(self.by_id(facts, 'bus_total'))

    def test_a_stop_served_twice_counts_once(self):
        # 62 of 326 routes carry more rows than distinct stops on a day
        # (2211: 51 rows, 40 stops, 7 Sep 2026); counting rows halved its
        # boardings per stop. Route 100 here re-serves each of its 10 stops.
        rows = self.FIVE_ROUTES + self._rows('100', '11110001', 30, 10)   # same STOPS_IDs again
        facts = self._facts(rows)
        top = self.by_id(facts, 'bus_busiest_route')
        self.assertEqual(top['label_en'], 'Busiest: 100, 10 stops')
        self.assertEqual(top['value_en'], '60')      # 600 over 10, not 20 rows

    def test_a_history_stamped_with_an_older_stop_rule_drops_its_stop_counts(self):
        S.BUS_HISTORY.write_text('{"days": {"20260907": {"100": 1}}, "stops": {"20260907": {"100": 9}}}')
        h = S.load_bus_history()
        self.assertEqual(h['stops'], {}); self.assertEqual(h['stops_rule'], S.STOPS_RULE)
        self.assertEqual(h['days']['20260907'], {'100': 1})

    def test_the_stubbed_day_lands_in_the_history_with_stop_counts(self):
        self._facts(self.FIVE_ROUTES)
        h = S.load_bus_history()
        (day, totals), = h['days'].items()
        self.assertEqual(totals['100'], 300); self.assertEqual(totals['1129'], 250)
        self.assertEqual(h['stops'][day]['1129'], 10); self.assertEqual(h['stops'][day]['272'], 12)


class StationsVein(unittest.TestCase):
    """The subway analogue of busroutes, added 10 Sep 2026 at the user's
    direction. Two apples-to-apples rules, both measured before they were
    written and both pinned here because their failure is a plausible wrong
    name in the right-looking place: rows are summed PER STATION across
    lines (the feed is one row per station per line, so Seoul Station is
    split five ways and loses to Gangnam on any single row), and only
    stations inside Seoul rank (the feed reaches Paju and Gapyeong, whose
    Korail halts are the raw quietest). The total counts every row.
    """

    # Two Seoul stops (ids beginning '1') near the Seoul stations, and one
    # Gyeonggi stop ('2') near the far station, which must NOT confer
    # membership.
    STOP_ROWS = [
        {'STOPS_NO': '100000001', 'STOPS_NM': 'a', 'XCRD': '126.972', 'YCRD': '37.556'},
        {'STOPS_NO': '100000002', 'STOPS_NM': 'b', 'XCRD': '127.100', 'YCRD': '37.513'},
        {'STOPS_NO': '100000003', 'STOPS_NM': 'c', 'XCRD': '127.017', 'YCRD': '37.540'},
        {'STOPS_NO': '100000004', 'STOPS_NM': 'e', 'XCRD': '127.028', 'YCRD': '37.498'},
        {'STOPS_NO': '200000001', 'STOPS_NM': 'd', 'XCRD': '126.746', 'YCRD': '37.888'},
    ]
    MASTER = [
        {'BLDN_NM': '서울역', 'ROUTE': '1호선', 'LAT': '37.5562', 'LOT': '126.9721'},
        {'BLDN_NM': '서울역', 'ROUTE': '4호선', 'LAT': '37.5528', 'LOT': '126.9725'},
        {'BLDN_NM': '잠실', 'ROUTE': '2호선', 'LAT': '37.5133', 'LOT': '127.1001'},
        {'BLDN_NM': '옥수', 'ROUTE': '3호선', 'LAT': '37.5403', 'LOT': '127.0175'},
        {'BLDN_NM': '강남', 'ROUTE': '2호선', 'LAT': '37.4980', 'LOT': '127.0279'},
        {'BLDN_NM': '임진강', 'ROUTE': '경의중앙선', 'LAT': '37.8884', 'LOT': '126.7468'},
    ]
    SUB_ROWS = [
        {'SBWY_ROUT_LN_NM': '2호선', 'SBWY_STNS_NM': '강남', 'GTON_TNOPE': '50000'},
        {'SBWY_ROUT_LN_NM': '1호선', 'SBWY_STNS_NM': '서울역', 'GTON_TNOPE': '30000'},
        {'SBWY_ROUT_LN_NM': '4호선', 'SBWY_STNS_NM': '서울역', 'GTON_TNOPE': '30000'},
        {'SBWY_ROUT_LN_NM': '경부선', 'SBWY_STNS_NM': '서울역', 'GTON_TNOPE': '25000'},
        {'SBWY_ROUT_LN_NM': '2호선', 'SBWY_STNS_NM': '잠실(송파구청)', 'GTON_TNOPE': '40000'},
        {'SBWY_ROUT_LN_NM': '8호선', 'SBWY_STNS_NM': '잠실', 'GTON_TNOPE': '20000'},
        {'SBWY_ROUT_LN_NM': '3호선', 'SBWY_STNS_NM': '옥수', 'GTON_TNOPE': '5000'},
        {'SBWY_ROUT_LN_NM': '경원선', 'SBWY_STNS_NM': '옥수', 'GTON_TNOPE': '50'},
        {'SBWY_ROUT_LN_NM': '경의선', 'SBWY_STNS_NM': '임진강', 'GTON_TNOPE': '9'},
    ]
    BUS_ROWS = BusRoutesVein.FIVE_ROUTES

    def _facts(self, sub_rows=None, master=None, stops=None, state=None, stub_extra=None):
        payloads = {'CardSubwayStatsNew': ok('CardSubwayStatsNew', sub_rows or self.SUB_ROWS),
                    'CardBusStatisticsServiceNew': ok('CardBusStatisticsServiceNew', self.BUS_ROWS),
                    'subwayStationMaster': ok('subwayStationMaster', master or self.MASTER),
                    'busStopLocationXyInfo': ok('busStopLocationXyInfo', stops or self.STOP_ROWS)}
        payloads.update(stub_extra or {})
        with Stub(payloads):
            return S.transport_facts('unused-key', state if state is not None else {})

    def by_id(self, facts, fid):
        return next((f for f in facts if f['id'] == fid), None)

    def test_rows_are_summed_per_station_across_lines(self):
        facts = self._facts()
        top = self.by_id(facts, 'st_busiest')
        self.assertIsNotNone(top, 'stations card withheld')
        # 30,000 + 30,000 + 25,000 across three lines beats Gangnam's single
        # 50,000 row — the per-row reading the transport vein still gives.
        self.assertEqual(top['label_en'], 'Busiest: Seoul Station')
        self.assertEqual(top['value_en'], '85,000')
        second = self.by_id(facts, 'st_second')
        self.assertEqual(second['label_en'], '2nd-busiest: Jamsil')
        self.assertEqual(second['value_en'], '60,000')   # 잠실(송파구청) + 잠실
        self.assertEqual(second['label_ko'], '두 번째로 붐빔: 잠실')
        quiet = self.by_id(facts, 'st_quietest')
        self.assertEqual(quiet['label_en'], 'Quietest: Oksu')
        self.assertEqual(quiet['value_en'], '5,050')     # the 50-boarding row folded in
        for f in (top, second, quiet):
            self.assertEqual(f['cat'], 'stations'); self.assertTrue(f['pin'])
            self.assertEqual(f['unit'], 'people')

    def test_the_transport_veins_station_lines_agree_with_the_stations_card(self):
        # Until 10 Sep 2026 the transport vein read busiest/quietest per ROW,
        # so it said Gangnam while the summed ranking said Seoul Station and
        # named a Paju halt as quietest. Both cards now read one ranking.
        facts = self._facts()
        self.assertEqual(self.by_id(facts, 'sub_busiest')['label_en'],
                         'Busiest subway station, Seoul Station, ' + S.STATION_DAY['en'])
        self.assertEqual(self.by_id(facts, 'sub_busiest')['value_en'], '85,000')
        self.assertIn('Oksu', self.by_id(facts, 'sub_quietest')['label_en'])
        self.assertEqual(self.by_id(facts, 'sub_quietest')['value_en'], '5,050')

    def test_a_station_outside_seoul_never_ranks_but_is_counted_in_the_total(self):
        facts = self._facts()
        quiet = self.by_id(facts, 'st_quietest')
        self.assertNotIn('임진강', quiet['label_ko'])
        total = self.by_id(facts, 'st_total')
        self.assertEqual(total['label_en'], 'Total subway boardings')
        self.assertEqual(total['value_en'], '200,059')   # every row, 임진강's 9 included
        self.assertEqual(self.by_id(facts, 'sub_total')['value_en'], '200,059')

    def test_a_gyeonggi_bus_stop_does_not_confer_membership(self):
        # 임진강 has a '2'-prefixed stop 20 m away; only '1' stops count.
        rows = [r for r in self.SUB_ROWS if r['SBWY_STNS_NM'] != '옥수'] + [
            {'SBWY_ROUT_LN_NM': '경의선', 'SBWY_STNS_NM': '임진강', 'GTON_TNOPE': '3000'}]
        facts = self._facts(sub_rows=rows)
        quiet = self.by_id(facts, 'st_quietest')
        self.assertEqual(quiet['label_en'], 'Quietest: Gangnam')

    def test_a_station_under_the_floor_is_not_the_quietest(self):
        rows = self.SUB_ROWS + [
            {'SBWY_ROUT_LN_NM': '3호선', 'SBWY_STNS_NM': '강남', 'GTON_TNOPE': '0'}]
        rows = [r for r in rows if r['SBWY_STNS_NM'] != '옥수'] + [
            {'SBWY_ROUT_LN_NM': '3호선', 'SBWY_STNS_NM': '옥수', 'GTON_TNOPE': '4'}]
        facts = self._facts(sub_rows=rows)
        self.assertEqual(self.by_id(facts, 'st_quietest')['label_en'], 'Quietest: Gangnam')

    def test_a_ranked_station_with_no_english_name_withholds_the_whole_card(self):
        master = self.MASTER + [
            {'BLDN_NM': '가상역', 'ROUTE': '3호선', 'LAT': '37.5403', 'LOT': '127.0175'}]
        rows = self.SUB_ROWS + [
            {'SBWY_ROUT_LN_NM': '3호선', 'SBWY_STNS_NM': '가상역', 'GTON_TNOPE': '100'}]
        facts = self._facts(sub_rows=rows, master=master)
        for fid in ('st_busiest', 'st_second', 'st_quietest', 'st_total'):
            self.assertIsNone(self.by_id(facts, fid), fid)
        self.assertIsNone(S.STATION_DAY['en'])
        # transport and busroutes are untouched by it.
        self.assertIsNotNone(self.by_id(facts, 'sub_total'))
        self.assertIsNotNone(self.by_id(facts, 'bus_busiest_route'))

    def test_coordinate_feeds_failing_withholds_stations_and_nothing_else(self):
        facts = self._facts(stub_extra={'subwayStationMaster': RuntimeError('down')})
        self.assertIsNone(self.by_id(facts, 'st_busiest'))
        self.assertIsNotNone(self.by_id(facts, 'sub_busiest'))
        # Without membership the transport lines fall back to the whole
        # network, still summed per station: Seoul Station, not Gangnam.
        self.assertIn('Seoul Station', self.by_id(facts, 'sub_busiest')['label_en'])
        self.assertIsNotNone(self.by_id(facts, 'bus_busiest_route'))

    def test_the_map_info_carries_the_three_stations_coordinates_in_rank_order(self):
        self._facts()
        labels = [x[0] for x in S.STATION_MAP_INFO['stations']]
        self.assertEqual(labels, ['Busiest: Seoul Station', '2nd-busiest: Jamsil', 'Quietest: Oksu'])
        lon, lat = S.STATION_MAP_INFO['stations'][0][1:]
        self.assertAlmostEqual(lon, 126.9721); self.assertAlmostEqual(lat, 37.5562)

    def test_a_same_day_cache_without_the_station_rule_is_refetched(self):
        with Stub({'CardSubwayStatsNew': ok('CardSubwayStatsNew', self.SUB_ROWS)}):
            day = S._latest_daily('unused-key', 'CardSubwayStatsNew', True)[0]
        stale = {'transport_cache': {'date': day, 'sub_total': 1, 'bus_total': 1,
                                     'busiest_st': 'x', 'busiest_v': 1, 'quietest_st': 'y',
                                     'quietest_v': 1, 'bus_ranked': [('143', 9), ('160', 8)],
                                     'bus_bottom': ('1226', 400), 'bus_streak_days': 1,
                                     'bus_rank_rule': S.BUS_RANK_RULE}}
        facts = self._facts(state=stale)
        self.assertIsNotNone(self.by_id(facts, 'st_busiest'), 'stale cache was served')
        self.assertEqual(stale['transport_cache']['st_rule'], S.STATION_RANK_RULE)

    def test_stations_in_seoul_uses_the_measured_cut(self):
        coords = {'near': (127.000, 37.500), 'edge': (127.0034, 37.500), 'far': (127.010, 37.500)}
        stops = [(127.000, 37.500)]
        inside = S.stations_in_seoul(coords, stops, within_km=0.3)
        self.assertIn('near', inside)
        self.assertIn('edge', inside)       # ~300 m east at this latitude
        self.assertNotIn('far', inside)     # ~885 m


class StationsCard(unittest.TestCase):
    """compose()-level checks for the stations card: identical shape to the
    busroutes card (no per-line emoji, rank word bold, total bold, harvester
    order kept, day on the dateline) plus its own footnote naming what the
    ranking counts. Same _pool()/_card() pattern as BusRoutesCard.
    """

    def _pool(self):
        return [
            S.fact('st_busiest', 'stations', 'Busiest: Seoul Station', '129,032', '129,032',
                   pin=True, label_ko='가장 붐빔: 서울역', place_en='Busiest', place_ko='가장 붐빔',
                   num=129032, unit='people'),
            S.fact('st_second', 'stations', '2nd-busiest: Jamsil', '94,554', '94,554',
                   pin=True, label_ko='두 번째로 붐빔: 잠실', place_en='2nd-busiest',
                   place_ko='두 번째로 붐빔', num=94554, unit='people'),
            S.fact('st_quietest', 'stations', 'Quietest: Oksu', '5,050', '5,050',
                   pin=True, label_ko='가장 한산함: 옥수', place_en='Quietest', place_ko='가장 한산함',
                   num=5050, unit='people'),
            S.fact('st_total', 'stations', 'Total subway boardings', '8,037,403', '8,037,403',
                   pin=True, label_ko='전체 지하철 승차 인원', num=8037403, unit='people'),
        ]

    def _card(self, ids=None):
        pool = self._pool()
        ids = ids or [f['id'] for f in pool]
        sel = {'opener_en': "Seoul's subway, station by station", 'opener_ko': '역으로 보는 서울 지하철',
               'picks': [{'id': i} for i in ids]}
        return S.compose(sel, pool)

    def setUp(self):
        S.STATION_DAY['en'], S.STATION_DAY['ko'] = '7 September', '9월 7일'

    def test_shape_matches_the_busroutes_card(self):
        c = self._card()
        self.assertTrue(all(l['emoji'] == '' for l in c['lines']))
        self.assertEqual([l['emph_en'] for l in c['lines'][:3]], ['Busiest', '2nd-busiest', 'Quietest'])
        self.assertTrue(c['lines'][3].get('bold'))
        self.assertEqual([l['label_en'] for l in c['lines']],
                         ['Busiest: Seoul Station', '2nd-busiest: Jamsil', 'Quietest: Oksu',
                          'Total subway boardings'])
        self.assertEqual(c['dateline_en'], '7 September')
        self.assertEqual(c['opener']['emoji'], '🚇')

    def test_the_footnote_says_what_the_ranking_counts(self):
        c = self._card()
        self.assertEqual(c['note_en'], 'Stations inside Seoul, all lines combined')
        self.assertEqual(c['note_ko'], '서울 시내 역, 전 노선 합산')

    def test_picking_two_of_four_completes_the_ranking(self):
        c = self._card(ids=['st_busiest', 'st_quietest'])
        self.assertEqual(len(c['items_en']), 4)

    def test_seoul_station_has_an_official_english_name(self):
        # The busiest station in the city; without this line the card
        # would withhold every day.
        self.assertEqual(S.en_lookup('서울역', 'stations'), 'Seoul Station')


class BusHistoryCards(unittest.TestCase):
    """The three cards read from bus_route_history.json (10 Sep 2026): the
    day's movers against each route's own same-weekday median, the weekend
    swing over a complete week, and the night-bus ranking. Pure functions,
    synthetic history. The withholds are the point: a holiday compared with
    ordinary weekdays, a baseline too short to mean anything, a route under
    the floor swinging 40 percent on a school trip — each is a plausible
    wrong card, not a crash.
    """

    def _hist(self, days):
        # Four Mondays of 25 Aug..15 Sep 2026 plus the surrounding week, with
        # route 100 steady at 10,000, 200 doubling on the last Monday, 300
        # halving, 400 tiny (under the floor), N13 a night route, 8641 tailored.
        from datetime import date, timedelta
        h = {'days': {}, 'holidays': {'2026': ['20260907']}}
        d0 = date(2026, 8, 10)
        for i in range(days):
            d = d0 + timedelta(days=i); key = d.strftime('%Y%m%d')
            last = (i == days - 1)
            we = d.weekday() >= 5
            h['days'][key] = {
                '100': 10000, '200': 20000 if last else 10000, '300': 5000 if last else 10000,
                '150': 11000 if last else 10000, '250': 9000 if last else 10000,
                '400': 900 if not last else 300,
                '500': 4000 if we else 10000,     # weekend loser
                '600': 8000 if we else 4000,      # weekend gainer
                '700': 2000 if we else 5000, '750': 3000 if we else 2000,   # ('800' is not a trunk number)
                'N13': 1500, 'N26': 900, 'N30': 50, '8641': 200, '마포01': 100}
        return h

    def test_movers_compare_against_the_same_weekday_only(self):
        h = self._hist(29)   # 10 Aug .. 7 Sep, last day Monday 7 Sep
        h['holidays'] = {'2026': []}
        mv, why = S.bus_movers(h, '20260907', set())
        self.assertIsNone(why)
        self.assertEqual([r[0] for r in mv['ups']], ['200', '150'])
        self.assertEqual([r[0] for r in mv['downs']], ['300', '250'])
        self.assertEqual(mv['n_prior'], 4)     # 10, 17, 24, 31 Aug
        self.assertEqual(mv['wd'], 0)
        self.assertNotIn('400', [r[0] for r in mv['ups'] + mv['downs']])   # under the floor

    def test_a_holiday_is_neither_compared_nor_in_a_baseline(self):
        h = self._hist(29)
        mv, why = S.bus_movers(h, '20260907', {'20260907'})
        self.assertIsNone(mv); self.assertIn('holiday', why)
        h2 = self._hist(36)   # to Monday 14 Sep; 7 Sep is a holiday
        mv, why = S.bus_movers(h2, '20260914', {'20260907'})
        self.assertIsNone(why)
        self.assertEqual(mv['n_prior'], 4)     # 10, 17, 24, 31 Aug — not 7 Sep

    def test_no_holiday_table_withholds_rather_than_compares(self):
        h = self._hist(29)
        self.assertEqual(S.bus_movers(h, '20260907', None)[0], None)
        self.assertEqual(S.weekend_swing(h, '20260907', None)[0], None)

    def test_too_short_a_baseline_withholds(self):
        h = self._hist(15)   # 10..24 Aug: only two prior Mondays for 24 Aug
        mv, why = S.bus_movers(h, '20260824', set())
        self.assertIsNone(mv); self.assertIn('need 3', why)

    def test_night_bus_ranks_n_routes_only(self):
        h = self._hist(3)
        nb, why = S.night_bus_rank(h, '20260812')
        self.assertIsNone(why)
        self.assertEqual([r[0] for r in nb['ranked']], ['N13', 'N26', 'N30'])
        self.assertEqual(nb['total'], 2450)

    def test_weekend_swing_uses_the_latest_complete_holiday_free_week(self):
        h = self._hist(29)
        ws, why = S.weekend_swing(h, '20260907', {'20260907'})
        self.assertIsNone(why)
        self.assertEqual(ws['week'][0], '20260831'); self.assertEqual(ws['week'][6], '20260906')
        self.assertEqual(ws['ups'][0][0], '600'); self.assertEqual(ws['ups'][1][0], '750')
        self.assertEqual({r[0] for r in ws['downs']}, {'500', '700'})
        # A holiday inside the newest week pushes it back a week.
        ws2, _ = S.weekend_swing(h, '20260907', {'20260903'})
        self.assertEqual(ws2['week'][6], '20260830')

    def test_pct_formats_with_a_real_minus_sign(self):
        self.assertEqual(S._pct(1.4), '+40%'); self.assertEqual(S._pct(0.93), '−7%')
        self.assertEqual(S._pct(1.0), '+0%')

    def test_history_add_is_idempotent_and_ascii_only(self):
        h = {'days': {}, 'holidays': {}}
        self.assertTrue(S.bus_history_add(h, '20260907', {'143': 1, '마포01': 2, 'N13': 3}))
        self.assertEqual(h['days']['20260907'], {'143': 1, 'N13': 3})
        self.assertFalse(S.bus_history_add(h, '20260907', {'143': 999}))
        self.assertEqual(h['days']['20260907']['143'], 1)

    def test_history_facts_fill_the_registry_and_keep_harvester_order(self):
        h = self._hist(29); h['holidays'] = {'2026': [], '2025': []}
        facts = S.history_bus_facts(h, '20260907', '7 September', '9월 7일')
        ids = [f['id'] for f in facts]
        self.assertEqual(ids[:4], ['busmv_up1', 'busmv_up2', 'busmv_down1', 'busmv_down2'])
        self.assertEqual(facts[0]['label_en'], 'Up: 200, 20,000 boardings')
        self.assertEqual(facts[0]['value_en'], '+100%')
        self.assertEqual(facts[2]['label_ko'], '감소: 300번, 5,000명 승차')
        self.assertIn('busnight_total', ids); self.assertIn('buswk_top1', ids)
        info = S.RANKED_CARD_INFO
        self.assertEqual(info['busmovers']['day_en'], '7 September')
        self.assertIn('previous 4 Mondays', info['busmovers']['note_en'])
        self.assertEqual([no for _, _, no in info['busmovers']['map_routes']][:1], ['200'])
        self.assertEqual(info['busweekend']['day_en'], '31 August to 6 September')
        self.assertEqual(info['busweekend']['map_day'], '20260905')
        self.assertEqual(info['nightbus']['note_en'], 'Night (N) routes only')

    def test_the_cards_compose_like_busroutes(self):
        h = self._hist(29); h['holidays'] = {'2026': [], '2025': []}
        pool = S.history_bus_facts(h, '20260907', '7 September', '9월 7일')
        for cat, opener, first_place in (('busmovers', 'Where the buses moved', 'Up'),
                                          ('nightbus', 'The night buses', 'Busiest'),
                                          ('busweekend', 'Weekends on the buses', 'Holds up best')):
            sub = [f for f in pool if f['cat'] == cat]
            sel = {'opener_en': opener, 'opener_ko': '버스', 'picks': [{'id': sub[0]['id']}]}
            c = S.compose(sel, pool)
            self.assertEqual(len(c['items_en']), 4, cat)          # completed from one pick
            self.assertTrue(all(l['emoji'] == '' for l in c['lines']), cat)
            self.assertEqual(c['lines'][0]['emph_en'], first_place, cat)
            self.assertEqual(c['dateline_en'], S.RANKED_CARD_INFO[cat]['day_en'], cat)
            self.assertEqual(c['note_en'], S.RANKED_CARD_INFO[cat]['note_en'], cat)
            self.assertEqual(c['opener']['emoji'], '🚌', cat)


class OnlyFlagGuard(unittest.TestCase):
    """--only skips its own vein's cooldown (so a hand-run can show a vein
    the day after it posted) but refuses a vein that posted within
    ONLY_MIN_HOURS unless --force: the duplicate stations thread of
    10 Sep 2026, 30 minutes after the scheduled run had chosen it.
    """

    def setUp(self):
        self._only, self._force = S.ONLY_CAT, S.FORCE
        S.ONLY_CAT, S.FORCE = 'stations', False

    def tearDown(self):
        S.ONLY_CAT, S.FORCE = self._only, self._force

    def _pool(self):
        return [{'cat': 'stations', 'id': f's{i}'} for i in range(4)] + \
               [{'cat': 'other', 'id': f'o{i}'} for i in range(6)]

    def _stamp(self, hours_ago):
        from datetime import datetime, timedelta, timezone
        return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()

    def test_a_post_minutes_ago_is_refused(self):
        state = {'last_stations_at': self._stamp(0.5)}
        with self.assertRaises(SystemExit) as cm:
            S.apply_cooldown(self._pool(), state, 'last_stations_at', 'stations', 3, 'Stations')
        self.assertIn('refusing a duplicate', str(cm.exception))

    def test_a_post_a_day_ago_is_allowed_through_its_cooldown(self):
        state = {'last_stations_at': self._stamp(26)}
        pool = S.apply_cooldown(self._pool(), state, 'last_stations_at', 'stations', 3, 'Stations')
        self.assertEqual(sum(f['cat'] == 'stations' for f in pool), 4)

    def test_force_overrides(self):
        S.FORCE = True
        state = {'last_stations_at': self._stamp(0.5)}
        pool = S.apply_cooldown(self._pool(), state, 'last_stations_at', 'stations', 3, 'Stations')
        self.assertEqual(sum(f['cat'] == 'stations' for f in pool), 4)

    def test_the_scheduled_path_is_untouched(self):
        S.ONLY_CAT = None
        state = {'last_stations_at': self._stamp(0.5)}
        pool = S.apply_cooldown(self._pool(), state, 'last_stations_at', 'stations', 3, 'Stations')
        self.assertEqual(sum(f['cat'] == 'stations' for f in pool), 0)   # ordinary cooldown


class HeldVeins(unittest.TestCase):
    """HELD_CATS keeps a vein out of the pool until he decides (busroutes,
    10 Sep 2026, after the per-stop rebuild). It must hold against selection
    AND the vein floor, and still let a hand-run --only show the vein."""

    def setUp(self):
        self._held, self._only = set(S.HELD_CATS), S.ONLY_CAT
        S.HELD_CATS, S.ONLY_CAT = {'busroutes'}, None

    def tearDown(self):
        S.HELD_CATS, S.ONLY_CAT = self._held, self._only

    def _pool(self):
        return [{'cat': 'busroutes', 'id': f'b{i}'} for i in range(4)] + \
               [{'cat': 'other', 'id': f'o{i}'} for i in range(6)]

    def test_a_held_vein_leaves_the_pool(self):
        pool = S.apply_holds(self._pool())
        self.assertEqual({f['cat'] for f in pool}, {'other'})

    def test_only_still_shows_a_held_vein(self):
        S.ONLY_CAT = 'busroutes'
        pool = S.apply_holds(self._pool())
        self.assertEqual(sum(f['cat'] == 'busroutes' for f in pool), 4)

    def test_the_live_hold_is_the_one_he_asked_for(self):
        # Pins the current instruction; change this test when he decides.
        self.assertEqual(self._held, {'busroutes'})

class BusRouteStreak(unittest.TestCase):
    """bus_rank_streaks() reads both streaks straight from the history —
    replacing, on 10 Sep 2026, a state counter that had started at 2 the day
    the vein was built while the real run stood at 63. The footnote is only
    as honest as this walk, and its failure is a plausible wrong number.
    """

    def _hist(self, n, top_seq, bottom_seq):
        # n consecutive days ending 7 Sep 2026; per-stop leader/trailer set
        # per day by top_seq/bottom_seq (lists of route numbers, oldest first).
        from datetime import date, timedelta
        h = {'days': {}, 'stops': {}, 'holidays': {}}
        for i in range(n):
            d = (date(2026, 9, 7) - timedelta(days=n - 1 - i)).strftime('%Y%m%d')
            top, bottom = top_seq[i], bottom_seq[i]
            h['days'][d] = {top: 5000, bottom: 100, '500': 2000, '600': 1500}
            h['stops'][d] = {top: 10, bottom: 10, '500': 10, '600': 10}
        return h

    def test_a_full_record_reads_as_every_day_recorded(self):
        h = self._hist(5, ['143'] * 5, ['1226'] * 5)
        top, td, bottom, bd, rec, first = S.bus_rank_streaks(h, '20260907')
        self.assertEqual((top, td, bottom, bd, rec, first), ('143', 5, '1226', 5, 5, '20260903'))
        en, ko = S._streak_note(h, '20260907')
        self.assertEqual(en, '143 busiest and 1226 quietest on every day recorded, 5 since 3 September')
        self.assertIn('기록된 5일(9월 3일부터) 내내', ko)

    def test_a_broken_streak_counts_only_the_run(self):
        h = self._hist(6, ['160', '143', '143', '143', '143', '143'], ['1226'] * 6)
        top, td, bottom, bd, rec, first = S.bus_rank_streaks(h, '20260907')
        self.assertEqual((td, bd, rec), (5, 6, 6))
        en, _ = S._streak_note(h, '20260907')
        self.assertEqual(en, '143 has led for the past 5 days · 1226 quietest on every day recorded, 6 since 2 September')

    def test_a_short_streak_says_nothing(self):
        h = self._hist(3, ['160', '160', '143'], ['1226', '1226', '7719'])
        self.assertEqual(S._streak_note(h, '20260907'), ('', ''))

    def test_a_missing_day_ends_the_walk_rather_than_bridging_it(self):
        h = self._hist(6, ['143'] * 6, ['1226'] * 6)
        del h['days']['20260904']
        top, td, bottom, bd, rec, first = S.bus_rank_streaks(h, '20260907')
        self.assertEqual((td, bd, rec, first), (3, 3, 3, '20260905'))

    def test_no_stop_counts_means_no_streak_and_no_crash(self):
        h = self._hist(3, ['143'] * 3, ['1226'] * 3); h['stops'] = {}
        self.assertIsNone(S.bus_rank_streaks(h, '20260907'))


class BusRoutesCard(unittest.TestCase):
    """compose()-level checks for the busroutes card shape: no per-line
    emoji, the rank word bold rather than the route number, the total's
    whole label bold, harvester order preserved rather than value-sorted
    (the total would otherwise jump to the top, being the largest number on
    the card), and the day lifted onto the dateline with the streak — when
    long enough to say anything — riding the footnote instead.
    """

    def _pool(self):
        return [
            S.fact('bus_busiest_route', 'busroutes', 'Busiest: 2211, 40 stops',
                   '455', '455', pin=True, label_ko='가장 붐빔: 2211번, 정류장 40곳',
                   place_en='Busiest', place_ko='가장 붐빔'),
            S.fact('bus_second_route', 'busroutes', '2nd-busiest: 5515, 33 stops',
                   '392', '392', pin=True, label_ko='두 번째로 붐빔: 5515번, 정류장 33곳',
                   place_en='2nd-busiest', place_ko='두 번째로 붐빔'),
            S.fact('bus_quietest_route', 'busroutes', 'Quietest: 1226, 20 stops',
                   '21', '21', pin=True, label_ko='가장 한산함: 1226번, 정류장 20곳',
                   place_en='Quietest', place_ko='가장 한산함'),
            S.fact('bus_route_total', 'busroutes', 'Average per stop, all routes',
                   '158', '158', pin=True, label_ko='정류장당 평균, 전체 노선'),
        ]

    def _card(self, ids=None):
        pool = self._pool()
        ids = ids or [f['id'] for f in pool]
        sel = {'opener_en': "Seoul's buses", 'opener_ko': '버스로 보는 서울',
               'picks': [{'id': i} for i in ids]}
        return S.compose(sel, pool)

    NOTE = 'Boardings per stop served · trunk and branch routes with 10 or more stops'

    def setUp(self):
        S.RANKED_CARD_INFO.pop('busroutes', None)

    def _info(self, note_extra=''):
        S.RANKED_CARD_INFO['busroutes'] = {
            'day_en': '6 September', 'day_ko': '9월 6일',
            'note_en': ' · '.join(x for x in (self.NOTE, note_extra) if x),
            'note_ko': '정류장 1곳당 승차 인원 · 정류장 10곳 이상 간선·지선'}

    def test_no_line_carries_an_emoji(self):
        c = self._card()
        self.assertTrue(all(l['emoji'] == '' for l in c['lines']))

    def test_the_rank_word_bolds_not_the_route_number(self):
        c = self._card()
        busiest = next(l for l in c['items_en'] if 'Busiest:' in l['label'])
        self.assertEqual(busiest.get('emph'), 'Busiest')

    def test_the_total_line_bolds_whole(self):
        c = self._card()
        total = next(l for l in c['items_en'] if 'Average' in l['label'])
        self.assertTrue(total.get('bold'))
        # Not also given an emph run — it has no place_en to bold a run from.
        self.assertNotIn('emph', total)

    def test_order_is_the_ranking_not_sorted_by_value(self):
        # The total (in the millions) is the largest number on the card; a
        # plain value sort would put it first and scramble the ranking.
        c = self._card()
        labels = [l['label'] for l in c['items_en']]
        self.assertEqual([l.split(':')[0] for l in labels],
                         ['Busiest', '2nd-busiest', 'Quietest', 'Average per stop, all routes'])

    def test_the_opener_emoji_is_the_bus_regardless_of_the_selector(self):
        pool = self._pool()
        sel = {'opener_en': "Seoul's buses", 'opener_ko': '버스로 보는 서울',
               'opener_emoji': '🚇',  # a wrong selector pick, must be overridden
               'picks': [{'id': f['id']} for f in pool]}
        c = S.compose(sel, pool)
        self.assertEqual(c['opener']['emoji'], '🚌')

    def test_picking_only_two_of_four_completes_the_ranking(self):
        # SELECT_PROMPT says all four are compulsory; complete_busroutes()
        # enforces it the same way complete_boxoffice() enforces all four
        # films, so a selector that under-picks still ships a whole ranking.
        c = self._card(ids=['bus_busiest_route', 'bus_quietest_route'])
        self.assertEqual(len(c['items_en']), 4)

    def test_the_day_lifts_to_the_dateline_and_the_footnote_carries_the_measure(self):
        self._info()
        c = self._card()
        self.assertEqual(c['dateline_en'], '6 September')
        self.assertEqual(c['note_en'], self.NOTE)
        self.assertEqual(c['note_ko'], '정류장 1곳당 승차 인원 · 정류장 10곳 이상 간선·지선')

    def test_a_streak_note_rides_the_footnote_after_the_measure(self):
        self._info('2211 has led for the past 10 days')
        c = self._card()
        self.assertEqual(c['note_en'], self.NOTE + ' · 2211 has led for the past 10 days')
        self.assertEqual(c['dateline_en'], '6 September')

    def test_without_registry_info_the_card_still_composes_with_no_dateline(self):
        # transport_facts() always fills the registry when it builds the
        # card; this pins that a bare pool does not crash compose().
        c = self._card()
        self.assertEqual(len(c['items_en']), 4)

class BusRouteMapStops(unittest.TestCase):
    """bus_route_map_stops() added 10 Sep 2026 for the route-map thread reply
    — a second full-day pass over the same feed transport_facts() already
    reads, paid only on the rare run that actually posts a busroutes card.
    Reuses the same stop-order trick already verified for route_paths() in
    seoul-transit-art/harvest.py: each row's own stop-name field ends in a
    bracketed sequence number.
    """

    STOP_ROWS = [
        {'STOPS_NO': '100000003', 'STOPS_NM': 'A', 'XCRD': '127.00', 'YCRD': '37.50'},
        {'STOPS_NO': '101000057', 'STOPS_NM': 'B', 'XCRD': '127.01', 'YCRD': '37.51'},
        {'STOPS_NO': '101000060', 'STOPS_NM': 'C', 'XCRD': '127.02', 'YCRD': '37.52'},
        # A Gyeonggi-run continuation of a Seoul route: id begins '2', not
        # in any Seoul coordinate table by convention (see SEOUL_STOP in
        # seoul-transit-art/harvest.py) — must be excluded from both the
        # route path and the background silhouette.
        {'STOPS_NO': '200000001', 'STOPS_NM': 'D', 'XCRD': '126.90', 'YCRD': '37.40'},
    ]
    # Deliberately out of published order, to prove the sort is real and not
    # an accident of row order in the feed.
    BUS_ROWS = [
        {'RTE_ID': '1', 'RTE_NO': '143', 'STOPS_ID': '101000060', 'SBWY_STNS_NM': 'C(00003)'},
        {'RTE_ID': '1', 'RTE_NO': '143', 'STOPS_ID': '100000003', 'SBWY_STNS_NM': 'A(00001)'},
        {'RTE_ID': '1', 'RTE_NO': '143', 'STOPS_ID': '101000057', 'SBWY_STNS_NM': 'B(00002)'},
        {'RTE_ID': '2', 'RTE_NO': '272', 'STOPS_ID': '200000001', 'SBWY_STNS_NM': 'D(00001)'},
        # A night route: two RTE_IDs, one per direction, each numbered from 1.
        {'RTE_ID': '363', 'RTE_NO': 'N13', 'STOPS_ID': '100000003', 'SBWY_STNS_NM': 'A(00001)'},
        {'RTE_ID': '363', 'RTE_NO': 'N13', 'STOPS_ID': '101000057', 'SBWY_STNS_NM': 'B(00002)'},
        {'RTE_ID': '363', 'RTE_NO': 'N13', 'STOPS_ID': '101000060', 'SBWY_STNS_NM': 'C(00003)'},
        {'RTE_ID': '364', 'RTE_NO': 'N13', 'STOPS_ID': '101000060', 'SBWY_STNS_NM': 'C(00001)'},
        {'RTE_ID': '364', 'RTE_NO': 'N13', 'STOPS_ID': '100000003', 'SBWY_STNS_NM': 'A(00002)'},
    ]

    def _stub(self):
        return Stub({'busStopLocationXyInfo': ok('busStopLocationXyInfo', self.STOP_ROWS),
                     'CardBusStatisticsServiceNew': ok('CardBusStatisticsServiceNew', self.BUS_ROWS)})

    def test_a_routes_stops_come_back_in_published_order_not_feed_order(self):
        with self._stub():
            routes, _ = S.bus_route_map_stops('key', '20260906', ['143'])
        self.assertEqual(routes['143'],
                         [(127.00, 37.50), (127.01, 37.51), (127.02, 37.52)])

    def test_a_route_split_by_direction_draws_one_direction_not_a_zigzag(self):
        # Sorting both directions' sequence numbers together would give
        # A, C, B, A, C (1, 1, 2, 2, 3): the scribble the first night-bus
        # map drew on 10 Sep 2026. The longer direction alone is A, B, C.
        with self._stub():
            routes, _ = S.bus_route_map_stops('key', '20260906', ['N13'])
        self.assertEqual(routes['N13'],
                         [(127.00, 37.50), (127.01, 37.51), (127.02, 37.52)])

    def test_a_route_with_only_out_of_seoul_stops_returns_empty_not_a_crash(self):
        with self._stub():
            routes, _ = S.bus_route_map_stops('key', '20260906', ['272'])
        self.assertEqual(routes['272'], [])

    def test_the_background_excludes_stops_outside_seoul(self):
        with self._stub():
            _, seoul_stops = S.bus_route_map_stops('key', '20260906', ['143'])
        self.assertEqual(len(seoul_stops), 3)
        self.assertNotIn((126.90, 37.40), seoul_stops)

    def test_a_route_not_present_that_day_returns_an_empty_list_not_a_key_error(self):
        with self._stub():
            routes, _ = S.bus_route_map_stops('key', '20260906', ['9999'])
        self.assertEqual(routes['9999'], [])


if __name__ == '__main__':
    unittest.main(verbosity=1)
