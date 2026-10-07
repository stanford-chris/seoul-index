"""The source checks themselves (seoul_index_post.py, "source checks", and
PROVENANCE in seoul_index_provenance.py): each passes the shape of feed it
was measured on and fails the one fault it exists to catch. Checks are ON
here; the other suites turn them off because their feeds are synthetic.
Run by path or by discovery."""
import collections
import json
import sys
import unittest
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))
import seoul_index_post as S

Fail = S.SourceCheckFailed


class ChecksOn(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(S, 'SOURCE_CHECKS', True)
        p.start()
        self.addCleanup(p.stop)


class TheSwitch(unittest.TestCase):
    def test_checks_are_on_in_the_module_as_shipped(self):
        # Read from the source, not the module: other suites in this process
        # turn the switch off for their synthetic feeds.
        src = (Path(__file__).parent / 'seoul_index_post.py').read_text()
        self.assertIn('\nSOURCE_CHECKS = True\n', src)
        self.assertNotIn('\nSOURCE_CHECKS = False', src)


class Decorator(ChecksOn):
    def test_a_malformed_row_is_a_failed_check_not_a_crash(self):
        @S.source_check
        def check(r):
            return r['missing']
        with self.assertRaises(Fail):
            check({})

    def test_off_means_the_check_does_not_run(self):
        @S.source_check
        def check():
            raise AssertionError('ran')
        with mock.patch.object(S, 'SOURCE_CHECKS', False):
            self.assertIsNone(check())


def crowd_row(**kw):
    r = {'AREA_NM': '강남역', 'REPLACE_YN': 'N', 'AREA_PPLTN_MIN': '22000',
         'AREA_PPLTN_MAX': '24000', 'MALE_PPLTN_RATE': '48.5', 'FEMALE_PPLTN_RATE': '51.5',
         'RESNT_PPLTN_RATE': '20.0', 'NON_RESNT_PPLTN_RATE': '80.0'}
    for a, v in zip((0, 10, 20, 30, 40, 50, 60, 70), (2, 5, 30, 25, 18, 10, 6, 4.1)):
        r[f'PPLTN_RATE_{a}'] = str(v)
    r.update(kw)
    return r


class Crowd(ChecksOn):
    def test_a_good_row_passes(self):
        S.check_crowd_row(crowd_row(), '강남역')

    def test_another_place_fails(self):
        with self.assertRaises(Fail):
            S.check_crowd_row(crowd_row(AREA_NM='홍대'), '강남역')

    def test_a_substituted_reading_fails(self):
        with self.assertRaises(Fail):
            S.check_crowd_row(crowd_row(REPLACE_YN='Y'), '강남역')

    def test_age_shares_not_summing_fail(self):
        with self.assertRaises(Fail):
            S.check_crowd_row(crowd_row(PPLTN_RATE_20='40'), '강남역')

    def test_shares_not_summing_fail(self):
        with self.assertRaises(Fail):
            S.check_crowd_row(crowd_row(FEMALE_PPLTN_RATE='60'), '강남역')


def air_rows(n=25, **kw):
    hour = datetime.now(S.SEOUL_TZ).strftime('%Y%m%d%H00')
    rows = [{'MSRSTN_NM': f'구{i}', 'MSRMT_YMD': hour, 'PM': '30', 'FPM': '19',
             'CAI_GRD': '보통'} for i in range(n)]
    rows[0].update(kw)
    return rows


class Air(ChecksOn):
    def test_a_good_hour_passes(self):
        S.check_air_rows('K', air_rows())

    def test_a_district_missing_fails(self):
        with self.assertRaises(Fail):
            S.check_air_rows('K', air_rows(24))

    def test_pm10_below_pm25_fails(self):
        with self.assertRaises(Fail):
            S.check_air_rows('K', air_rows(PM='10'))

    def test_an_old_hour_fails(self):
        old = (datetime.now(S.SEOUL_TZ) - timedelta(hours=5)).strftime('%Y%m%d%H00')
        rows = [dict(r, MSRMT_YMD=old) for r in air_rows()]
        with self.assertRaises(Fail):
            S.check_air_rows('K', rows)

    def test_an_unknown_grade_fails(self):
        with self.assertRaises(Fail):
            S.check_air_rows('K', air_rows(CAI_GRD='위험'))


def water_day(day, intake=(100, 100, 100, 100, 100, 50), ratio=0.99):
    centers = list(S.WATER_SITES) + ['광암']
    rows = [{'YMD': day, 'ROF_SE_NM': '취수', 'BUSNP_NM': c, 'MSRMT_VL': str(v)}
            for c, v in zip(centers, intake)]
    rows += [{'YMD': day, 'ROF_SE_NM': '송수', 'BUSNP_NM': c, 'MSRMT_VL': str(v * ratio)}
             for c, v in zip(centers, intake)]
    rows += [{'YMD': day, 'ROF_SE_NM': '공급량', 'BUSNP_NM': f'사업소{i}', 'MSRMT_VL': '50'}
             for i in range(10)]
    return rows


class Water(ChecksOn):
    def day(self, back=1):
        return (datetime.now(S.SEOUL_TZ) - timedelta(days=back)).strftime('%Y%m%d')

    def test_a_whole_new_day_passes(self):
        d = self.day()
        S.check_water_rows(water_day(d) + water_day(self.day(2)), d)

    def test_a_short_day_fails(self):
        d = self.day()
        with self.assertRaises(Fail):
            S.check_water_rows(water_day(d)[1:], d)

    def test_an_unknown_center_fails(self):
        d = self.day()
        rows = water_day(d)
        rows[0] = dict(rows[0], BUSNP_NM='새정수')
        with self.assertRaises(Fail):
            S.check_water_rows(rows, d)

    def test_swapped_measures_fail(self):
        d = self.day()
        with self.assertRaises(Fail):
            S.check_water_rows(water_day(d, ratio=1.2), d)


def daynight(night_over_day=False):
    rows = []
    for i in range(25):
        day, night = (300.0, 400.0) if night_over_day and i % 2 else (400.0, 300.0)
        rows.append({'SIGNGU_NM': f'구{i}', 'DAY_LVPOP_CO': day, 'NIGHT_LVPOP_CO': night,
                     'TOT_LVPOP_CO': 350.0, 'DAIL_MUMM_LVPOP_CO': 250.0,
                     'DAIL_MXMM_LVPOP_CO': 450.0, 'LVPOP_CO': 300.0,
                     'LNGTR_STAY_FRGNR_CO': 40.0, 'SRTPD_STAY_FRGNR_CO': 10.0})
    city = {k: sum(r[k] for r in rows) if isinstance(rows[0][k], float) else k for k in rows[0]}
    city['SIGNGU_NM'] = '서울시'
    return rows + [city]


class DayNight(ChecksOn):
    def test_a_good_day_passes_even_where_night_outnumbers_day(self):
        S.check_daynight_day(daynight(night_over_day=True), '20261003')

    def test_districts_not_summing_to_the_city_fail(self):
        rows = daynight()
        rows[0] = dict(rows[0], DAY_LVPOP_CO=420.0)
        with self.assertRaises(Fail):
            S.check_daynight_day(rows, '20261003')

    def test_a_missing_district_fails(self):
        with self.assertRaises(Fail):
            S.check_daynight_day(daynight()[1:], '20261003')


class Library(ChecksOn):
    def body(self, convention_shift=0):
        this = datetime.now(S.SEOUL_TZ).year
        rows = [{'AGE_RANGE': str(b), 'BRDT': str(this + 1 - b - k - convention_shift),
                 'MBR_CNT': '5'} for b in range(10, 90, 10) for k in range(10)]
        return {'list_total_count': len(rows), 'row': rows}

    def test_korean_counting_age_passes(self):
        S.check_library_rows(self.body())

    def test_a_switch_to_international_age_fails(self):
        with self.assertRaises(Fail):
            S.check_library_rows(self.body(convention_shift=1))

    def test_a_short_read_fails(self):
        b = self.body()
        b['list_total_count'] += 1
        with self.assertRaises(Fail):
            S.check_library_rows(b)


class Price(ChecksOn):
    def test_agreeing_fields_pass(self):
        rows = [{'SPCIES': '특란(60g)', 'QY': '10개'}]
        self.assertTrue(S.price_product_consistent('계란 10개', rows))

    def test_a_variety_listing_two_grades_is_skipped(self):
        rows = [{'SPCIES': '한우, 1등급, 1+등급 등심', 'QY': ''}]
        with mock.patch('builtins.print'):
            self.assertFalse(S.price_product_consistent('소고기(국산) 100g', rows))

    def test_a_quantity_naming_another_unit_is_skipped(self):
        rows = [{'SPCIES': '(국산)염장고등어', 'QY': '1마리'}]
        with mock.patch('builtins.print'):
            self.assertFalse(S.price_product_consistent('고등어(염장) 1손(대)', rows))


def kepco(n_gu=25, drop=None, metro='서울특별시'):
    rows = []
    for g in range(n_gu):
        for c in ('주택용', '일반용', '산업용'):
            if drop == (g, c):
                continue
            rows.append({'year': '2026', 'month': '07', 'metro': metro, 'city': f'구{g}', 'cntr': c})
    return rows


class Kepco(ChecksOn):
    def test_a_whole_month_passes(self):
        S.check_kepco_month(kepco(), 2026, 7, ('주택용', '일반용'))

    def test_a_district_short_of_a_named_tariff_fails(self):
        with self.assertRaises(Fail):
            S.check_kepco_month(kepco(drop=(3, '주택용')), 2026, 7, ('주택용', '일반용'))

    def test_another_month_fails(self):
        with self.assertRaises(Fail):
            S.check_kepco_month(kepco(), 2026, 8, None)

    def test_rows_outside_seoul_fail(self):
        with self.assertRaises(Fail):
            S.check_kepco_month(kepco(metro='경기도'), 2026, 7, None)


class Korail(ChecksOn):
    def fetched(self, n=255, drop=None):
        names = [f'역{i}' for i in range(n - len(S.KORAIL_SEOUL_STATIONS))] + list(S.KORAIL_SEOUL_STATIONS)
        day = {k: (1, 1) for k in names if k != drop}
        return {'20261004': day, '20261003': dict(day), '20261002': {'서울': (1, 1)}}

    def test_a_whole_newest_day_passes(self):
        S.check_korail_day(self.fetched(), '20261004')

    def test_the_page_cut_oldest_day_fails(self):
        with self.assertRaises(Fail):
            S.check_korail_day(self.fetched(), '20261002')

    def test_a_short_day_fails(self):
        with self.assertRaises(Fail):
            S.check_korail_day(self.fetched(n=200), '20261004')

    def test_a_missing_seoul_station_fails(self):
        with self.assertRaises(Fail):
            S.check_korail_day(self.fetched(drop='용산'), '20261004')


def kopis_xml(seoul_tickets=100, cancel=50):
    areas = ['서울', '인천', '경기', '부산', '대구', '울산', '경북', '경남', '전남광주',
             '전북', '대전', '충북', '세종', '충남', '강원도', '제주도']
    def row(a, mult=1):
        t = seoul_tickets if a == '서울' else 10
        return (f'<prfst><area>{a}</area><prfdtcnt>{mult}</prfdtcnt><amount>{mult}</amount>'
                f'<totnmrs>{t * mult}</totnmrs><nmrs>{(t + cancel) * mult}</nmrs>'
                f'<nmrcancl>{cancel * mult}</nmrcancl><prfcnt>{mult}</prfcnt>'
                f'<prfprocnt>{mult}</prfprocnt></prfst>')
    total_t = seoul_tickets + 10 * 15
    body = ''.join(row(a) for a in areas)
    body += ''.join(f'<prfst><area>{g}</area><prfdtcnt>0</prfdtcnt><amount>0</amount>'
                    f'<totnmrs>0</totnmrs><nmrs>0</nmrs><nmrcancl>0</nmrcancl>'
                    f'<prfcnt>0</prfcnt><prfprocnt>0</prfprocnt></prfst>'
                    for g in ('경기/인천', '경상도', '전라도', '충청도'))
    body += (f'<prfst><area>합계</area><prfdtcnt>16</prfdtcnt><amount>16</amount>'
             f'<totnmrs>{total_t}</totnmrs><nmrs>{total_t + 16 * cancel}</nmrs>'
             f'<nmrcancl>{16 * cancel}</nmrcancl><prfcnt>16</prfcnt><prfprocnt>16</prfprocnt></prfst>')
    return ET.fromstring(f'<prfsts>{body}</prfsts>')


class Kopis(ChecksOn):
    def test_regions_summing_to_the_total_pass(self):
        S.check_kopis_regions(kopis_xml())

    def test_a_region_missing_from_the_total_fails(self):
        root = kopis_xml()
        root.remove(root.findall('prfst')[1])
        with self.assertRaises(Fail):
            S.check_kopis_regions(root)


class World(ChecksOn):
    def rows(self, unit='M2_PS', dup=False):
        filt = {'MEASURE': 'GREEN_AREA', 'UNIT_MEASURE': 'M2_PS'}
        out = [dict(filt, REF_AREA=c, TIME_PERIOD='2021', UNIT_MEASURE=unit,
                    TERRITORIAL_LEVEL='FUA', OBS_VALUE='40') for c in ('KOR01F', 'JPN01F')]
        return (out + out[:1] if dup else out), filt

    def test_one_row_per_city_year_passes(self):
        rows, filt = self.rows()
        S.check_world_rows('green', rows, filt, ['KOR01F', 'JPN01F'])

    def test_a_duplicate_city_year_fails(self):
        rows, filt = self.rows(dup=True)
        with self.assertRaises(Fail):
            S.check_world_rows('green', rows, filt, ['KOR01F', 'JPN01F'])


class Reconciles(ChecksOn):
    """The network checks, each against a stub returning what was measured."""

    def test_bike_racks_equal_to_the_register_pass_and_unequal_fail(self):
        reg = [{'RENT_ID': f'ST-{i}', 'HOLD_NUM': '10'} for i in range(3)]

        def get(url):
            if '/1/1/' in url:
                return {'stationInfo': {'list_total_count': '3', 'row': reg[:1]}}
            return {'stationInfo': {'list_total_count': '3', 'row': reg}}
        live = {f'ST-{i}': [10] for i in range(3)}
        with mock.patch.object(S, 'http_get_json', get):
            S.check_bike_racks('K', live, 30)
            with self.assertRaises(Fail):
                S.check_bike_racks('K', live, 31)
            with self.assertRaises(Fail):
                S.check_bike_racks('K', dict(live, **{'ST-9': [10]}), 40)

    def test_infant_ages_must_equal_the_band_table(self):
        by_year = {y: {a: 100 for a in range(6)} for y in (2015, 2020, 2025)}
        band = [{'PRD_DE': f'{y}12', 'C2_NM': '0 - 4세', 'DT': '500'} for y in (2015, 2020, 2025)]
        with mock.patch.object(S, 'http_get_json', lambda url: band):
            S.check_infant_years('k', by_year, [2015, 2020, 2025])
        band[1]['DT'] = '501'
        with mock.patch.object(S, 'http_get_json', lambda url: band):
            with self.assertRaises(Fail):
                S.check_infant_years('k', by_year, [2015, 2020, 2025])

    def test_complaints_within_the_measured_tolerance_pass(self):
        sector = [{'YEAR': '2025', 'RCPT_CNT_TOTAL': 902_959.0}]
        stub = lambda url: {'SmartUncomfStatSector': {'list_total_count': 1, 'row': sector}}
        with mock.patch.object(S, 'http_get_json', stub):
            S.check_complaint_years('K', [('2025', 903_127.0)])
            with self.assertRaises(Fail):
                S.check_complaint_years('K', [('2025', 950_000.0)])

    def test_national_population_must_equal_december(self):
        dec = [{'C1': '00', 'DT': '51117378'}, {'C1': '11', 'DT': '9299548'}]
        with mock.patch.object(S, 'http_get_json', lambda url: dec):
            S.check_national_pop('k', '2025', 51117378, 9299548)
            with self.assertRaises(Fail):
                S.check_national_pop('k', '2025', 51117378, 9299549)

    def test_fertility_must_agree_with_its_age_rates(self):
        asfr = [{'C1': c, 'DT': str(v)} for c in ('00', '11')
                for v in (0.4, 3.8, 21.2, 73.1, 52.1, 8.5, 0.3)]
        with mock.patch.object(S, 'http_get_json', lambda url: asfr):
            S.check_national_fertility('k', '2025', 0.799, 0.799)
            with self.assertRaises(Fail):
                S.check_national_fertility('k', '2025', 0.799, 0.632)

    def test_the_hong_kong_constants_must_match_the_census_tables(self):
        def curl(url, **kw):
            if '130-06102' in url:
                return json.dumps({'dataSet': [{'freq': 'Y', 'sv': 'DH',
                                               'period': str(S.HK_HOUSEHOLDS_YEAR), 'figure': 2778.1}]})
            return json.dumps({'dataSet': [{'sv': 'POP', 'svDesc': "Number ('000)", 'SEX': '',
                                           'AGE': '', 'period': f'{S.HK_TOTAL_YEAR}12',
                                           'figure': pop}]})
        pop = 7508.7
        with mock.patch.object(S, '_curl', curl):
            S.check_hk_constants()
        pop = 7600.0
        with mock.patch.object(S, '_curl', curl):
            with self.assertRaises(Fail):
                S.check_hk_constants()


if __name__ == '__main__':
    unittest.main()
