"""Tests for the six veins added 21 Aug 2026 from the data.seoul.go.kr sweep.

Every test here asserts a GUARD FIRES. That is deliberate: none of the traps
these veins carry announces itself. The feeds do not error, they do not return
nothing, and they do not crash the harvester — they hand back a plausible number
that means something other than what its field name says. A green run of the
bot proves nothing about any of them, which is why they are tested where they
live rather than through a composed card.

No network, no model call, no posting: http_get_json is stubbed per test.
"""
import sys, unittest
from pathlib import Path

sys.argv = ['test']
sys.path.insert(0, str(Path(__file__).resolve().parent))
import seoul_index_post as S


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

    def test_a_film_without_an_english_title_is_dropped(self):
        """The English card carries no Hangul, and a romanisation is a guess."""
        titles = dict(TITLES, **{'movieCd=2': _bo_info('')})
        with Stub({'searchDailyBoxOfficeList': _bo_rows(FIVE), **titles}):
            facts = S.boxoffice_facts('KEY')
        labels = [f['label_en'] for f in facts]
        self.assertNotIn('스파이더맨', labels)
        self.assertNotIn('', labels)
        self.assertIn('The Odyssey', labels)

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

    def test_a_thin_day_produces_no_card(self):
        """Two films is not a card, and a two-line card is worse than none."""
        with Stub({'searchDailyBoxOfficeList': _bo_rows(FIVE[:2]), **TITLES}):
            self.assertEqual(S.boxoffice_facts('KEY'), [])


if __name__ == '__main__':
    unittest.main(verbosity=1)
