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

# ⚠️ compose() ends by checking its labels against the pool's own with a model
# call (see check_labels). These tests promise no network and no model call, so
# the CALL is switched off here: what they exercise is selection and composition, and a
# live checker would make them slow, non-deterministic and quota-hungry. The
# checker's own behaviour is tested in test_seoul_index_labels.py.
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
        self.assertEqual(f['library_10']['num'], 10921)   # collisions unaffected

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
        """The English card carries no Hangul and a romanisation is a guess, so
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
        f = next(f for f in facts if f['label_en'] == 'The Odyssey')
        self.assertEqual(f['label_ko'], '오디세이')
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
                         ['The Odyssey', 'Spider-Man', 'Insidious', 'Conan'])


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

    def test_other_veins_keep_their_mixed_emoji(self):
        """A spending card carrying ☕ beside an abstract share is correct."""
        lines = self._lines(['☕', '', '📚'], cat='spending')
        S.even_out_emoji(lines, {'spending'})
        self.assertEqual([l['emoji'] for l in lines], ['☕', '', '📚'])

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


if __name__ == '__main__':
    unittest.main(verbosity=1)
