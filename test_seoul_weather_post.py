"""Tests for seoul_weather_post.py, the daily forecast card.

The failure to fear here is not a crash but a plausible wrong card: a high
read off the wrong hour, a rain row that fires when the day is dry, a sky
condition read from an hour with no data. So the tests pin the reduction
from raw getVilageFcst rows to a card's figures (today_summary), the base-
time fallback around midnight (latest_base), and which rows a card does and
does not carry (build_card_lines) — not the rendering or posting, which are
covered by seoul_index_card.py's own smoke test and by hand-verification
against a live dry run.

No network, no Chrome, no posting: nothing here touches http_get_json,
render_card or atproto.
"""
import json
import sys
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

sys.argv = ['test']
sys.path.insert(0, str(Path(__file__).resolve().parent))
import seoul_weather_post as W

KST = ZoneInfo('Asia/Seoul')


def _row(fcst_date, fcst_time, category, value):
    return {'fcstDate': fcst_date, 'fcstTime': fcst_time, 'category': category,
            'fcstValue': value}


class FmtC(unittest.TestCase):
    def test_a_whole_degree_drops_the_decimal(self):
        self.assertEqual(W.fmt_c(31.0), '31°C')

    def test_a_genuine_fraction_keeps_one_decimal(self):
        self.assertEqual(W.fmt_c(26.5), '26.5°C')

    def test_english_pairs_a_rounded_fahrenheit_regardless(self):
        self.assertEqual(W.fmt_c_en(31.0), '31°C (88°F)')
        self.assertEqual(W.fmt_c_en(26.5), '26.5°C (80°F)')


class FormatAmpm(unittest.TestCase):
    def test_on_the_hour_omits_minutes(self):
        self.assertEqual(W.format_ampm(5, 0), '5 a.m.')
        self.assertEqual(W.format_ampm(17, 0), '5 p.m.')

    def test_minutes_are_kept_and_zero_padded(self):
        self.assertEqual(W.format_ampm(5, 3), '5:03 a.m.')
        self.assertEqual(W.format_ampm(18, 56), '6:56 p.m.')

    def test_midnight_and_noon_are_still_twelve(self):
        self.assertEqual(W.format_ampm(0, 0), '12 a.m.')
        self.assertEqual(W.format_ampm(12, 0), '12 p.m.')


class FmtHhmmAmpm(unittest.TestCase):
    def test_english(self):
        self.assertEqual(W.fmt_hhmm_ampm('0533'), '5:33 a.m.')
        self.assertEqual(W.fmt_hhmm_ampm('1856'), '6:56 p.m.')

    def test_korean_uses_the_ampm_ko_convention(self):
        # Same 오전/오후 vocabulary as _ampm_ko() in seoul_index_post.py.
        self.assertEqual(W.fmt_hhmm_ampm_ko('0533'), '오전 5시 33분')
        self.assertEqual(W.fmt_hhmm_ampm_ko('1856'), '오후 6시 56분')

    def test_korean_on_the_hour_omits_minutes(self):
        self.assertEqual(W.fmt_hhmm_ampm_ko('0500'), '오전 5시')


class ToIn(unittest.TestCase):
    def test_converts_and_rounds_to_two_decimal_places(self):
        self.assertEqual(W.to_in(34.8), '34.8mm (1.37 in)')
        self.assertEqual(W.to_in(4.0), '4.0mm (0.16 in)')


class FmtForecastRain(unittest.TestCase):
    def test_exact_figure(self):
        self.assertEqual(W.fmt_forecast_rain_en(4.0, True), '4.0mm (0.16 in)')
        self.assertEqual(W.fmt_forecast_rain_ko(4.0, True), '4.0mm')

    def test_inexact_figure_reads_as_a_floor(self):
        self.assertEqual(W.fmt_forecast_rain_en(30.0, False), '30.0mm (1.18 in) or more')
        self.assertEqual(W.fmt_forecast_rain_ko(30.0, False), '30.0mm 이상')

    def test_zero_but_inexact_is_a_trace_not_a_dry_day(self):
        # Distinguishes "every hour was 강수없음" (a genuine zero, handled
        # by omitting the row entirely) from "the only rain forecast was a
        # '1mm 미만' hour with no number to add" — both sum to 0.0, but only
        # the second is a real forecast worth stating.
        self.assertEqual(W.fmt_forecast_rain_en(0.0, False), 'less than 1mm (0.04 in)')
        self.assertEqual(W.fmt_forecast_rain_ko(0.0, False), '1mm 미만')


class LatestBase(unittest.TestCase):
    def test_picks_the_most_recent_ready_run(self):
        # 08:25 KST: 0800 published (ready at 08:20), 1100 not yet.
        now = datetime(2026, 8, 29, 8, 25, tzinfo=KST)
        self.assertEqual(W.latest_base(now), ('20260829', '0800'))

    def test_just_before_a_run_is_ready_falls_back_to_the_prior_one(self):
        # 08:19 KST: 0800 (ready 08:20) has not landed yet — 0500 still wins.
        now = datetime(2026, 8, 29, 8, 19, tzinfo=KST)
        self.assertEqual(W.latest_base(now), ('20260829', '0500'))

    def test_just_after_midnight_falls_back_to_yesterdays_last_run(self):
        # Before today's own 0200 has published, only yesterday's 2300 run
        # exists — and that run already forecasts today in full.
        now = datetime(2026, 8, 29, 1, 0, tzinfo=KST)
        self.assertEqual(W.latest_base(now), ('20260828', '2300'))

    def test_exactly_on_the_publish_boundary_counts_as_ready(self):
        now = datetime(2026, 8, 29, 8, 20, tzinfo=KST)
        self.assertEqual(W.latest_base(now), ('20260829', '0800'))


class TodaySummary(unittest.TestCase):
    def test_high_low_come_from_the_hourly_series_not_tmn_tmx(self):
        # TMN/TMX deliberately absent (as they are for "today" once the day
        # is under way) — the summary must still find a high and a low.
        items = [
            _row('20260829', '0900', 'TMP', '24'),
            _row('20260829', '1200', 'TMP', '30'),
            _row('20260829', '1500', 'TMP', '31'),
            _row('20260829', '1800', 'TMP', '27'),
        ]
        s = W.today_summary(items, '20260829')
        self.assertEqual(s['hi'], 31.0)
        self.assertEqual(s['lo'], 24.0)

    def test_other_dates_in_the_same_response_are_ignored(self):
        items = [
            _row('20260829', '1200', 'TMP', '30'),
            _row('20260830', '1200', 'TMP', '99'),  # tomorrow — must not leak in
        ]
        s = W.today_summary(items, '20260829')
        self.assertEqual(s['hi'], 30.0)
        self.assertEqual(s['lo'], 30.0)

    def test_no_temperature_data_for_the_target_date_is_none(self):
        items = [_row('20260830', '1200', 'TMP', '30')]
        self.assertIsNone(W.today_summary(items, '20260829'))

    def test_a_row_with_no_tmp_at_all_does_not_crash(self):
        items = [_row('20260829', '0900', 'SKY', '1')]  # TMP missing that hour
        self.assertIsNone(W.today_summary(items, '20260829'))

    def test_pop_is_the_days_own_maximum(self):
        items = [
            _row('20260829', '0900', 'TMP', '25'),
            _row('20260829', '0900', 'POP', '20'),
            _row('20260829', '1500', 'TMP', '30'),
            _row('20260829', '1500', 'POP', '60'),
            _row('20260829', '1800', 'TMP', '27'),
            _row('20260829', '1800', 'POP', '30'),
        ]
        s = W.today_summary(items, '20260829')
        self.assertEqual(s['pop'], 60)

    def test_sky_is_read_at_the_hour_of_the_days_high(self):
        items = [
            _row('20260829', '0900', 'TMP', '25'),
            _row('20260829', '0900', 'SKY', '4'),   # cloudy at 9am
            _row('20260829', '1500', 'TMP', '31'),  # the day's high
            _row('20260829', '1500', 'SKY', '1'),   # clear at 3pm — this wins
        ]
        s = W.today_summary(items, '20260829')
        self.assertEqual(s['sky'], '1')

    def test_pty_is_the_first_nonzero_hour_even_if_the_high_hour_is_dry(self):
        # A hot, dry afternoon can still open with an early shower. Reading
        # PTY only at the high-temperature hour would miss it entirely.
        items = [
            _row('20260829', '0600', 'TMP', '24'),
            _row('20260829', '0600', 'PTY', '1'),   # rain at 6am
            _row('20260829', '1500', 'TMP', '31'),  # the day's high — dry
            _row('20260829', '1500', 'PTY', '0'),
        ]
        s = W.today_summary(items, '20260829')
        self.assertEqual(s['pty'], '1')

    def test_a_dry_day_reports_no_precipitation_type(self):
        items = [
            _row('20260829', '0900', 'TMP', '28'),
            _row('20260829', '0900', 'PTY', '0'),
            _row('20260829', '1500', 'TMP', '31'),
            _row('20260829', '1500', 'PTY', '0'),
        ]
        s = W.today_summary(items, '20260829')
        self.assertIsNone(s['pty'])

    def test_no_pop_anywhere_is_none_not_zero(self):
        # None (unmeasured) and 0 (a genuine 0% forecast) must not collapse
        # into the same value — the row is omitted only for the former.
        items = [_row('20260829', '1200', 'TMP', '25')]
        s = W.today_summary(items, '20260829')
        self.assertIsNone(s['pop'])

    def test_humidity_is_read_at_the_same_hour_as_sky(self):
        items = [
            _row('20260829', '0900', 'TMP', '25'),
            _row('20260829', '0900', 'REH', '80'),
            _row('20260829', '1500', 'TMP', '31'),   # the day's high — this hour wins
            _row('20260829', '1500', 'REH', '55'),
        ]
        s = W.today_summary(items, '20260829')
        self.assertEqual(s['humidity'], 55)

    def test_missing_humidity_is_none_not_a_crash(self):
        items = [_row('20260829', '1500', 'TMP', '31')]
        s = W.today_summary(items, '20260829')
        self.assertIsNone(s['humidity'])


class ForecastRainToday(unittest.TestCase):
    """PCP reduction — the same shape of test as TodaySummary above: the
    failure to fear is a plausible wrong total, not a crash."""

    def test_no_pcp_rows_at_all_is_none_not_zero(self):
        items = [_row('20260829', '1500', 'TMP', '31')]
        mm, exact = W.forecast_rain_today(items, '20260829')
        self.assertIsNone(mm)
        self.assertIsNone(exact)

    def test_a_dry_day_sums_to_zero_and_stays_exact(self):
        items = [_row('20260829', h, 'PCP', '강수없음') for h in ('0600', '0900', '1200')]
        self.assertEqual(W.forecast_rain_today(items, '20260829'), (0.0, True))

    def test_plain_figures_sum_exactly(self):
        items = [
            _row('20260829', '0600', 'PCP', '4.0mm'),
            _row('20260829', '0700', 'PCP', '9.0mm'),
            _row('20260829', '0800', 'PCP', '강수없음'),
        ]
        self.assertEqual(W.forecast_rain_today(items, '20260829'), (13.0, True))

    def test_a_trace_hour_marks_the_total_inexact(self):
        # '1mm 미만' has no number to add — confirmed live 1 September 2026 —
        # so it can only push the true total up, never down.
        items = [
            _row('20260829', '0600', 'PCP', '4.0mm'),
            _row('20260829', '0900', 'PCP', '1mm 미만'),
        ]
        self.assertEqual(W.forecast_rain_today(items, '20260829'), (4.0, False))

    def test_a_bucketed_heavy_rain_range_also_marks_inexact(self):
        # Not observed live (no heavy-rain day yet), but KMA's published
        # format buckets the top end the same way it buckets the trace end.
        items = [_row('20260829', '1500', 'PCP', '30.0~50.0mm')]
        self.assertEqual(W.forecast_rain_today(items, '20260829'), (0.0, False))

    def test_other_dates_in_the_same_response_are_ignored(self):
        items = [
            _row('20260829', '1500', 'PCP', '4.0mm'),
            _row('20260830', '1500', 'PCP', '99.0mm'),  # tomorrow — must not leak in
        ]
        self.assertEqual(W.forecast_rain_today(items, '20260829'), (4.0, True))

    def test_other_categories_at_the_same_hour_are_ignored(self):
        items = [
            _row('20260829', '1500', 'TMP', '31'),
            _row('20260829', '1500', 'PCP', '2.0mm'),
        ]
        self.assertEqual(W.forecast_rain_today(items, '20260829'), (2.0, True))


class BuildCardLines(unittest.TestCase):
    def _summary(self, **over):
        base = {'hi': 30.0, 'lo': 22.0, 'pop': 40, 'sky': '3', 'pty': None,
                'humidity': None}
        base.update(over)
        return base

    def test_precipitation_is_never_its_own_row_any_more(self):
        # Conditions now carries everything rain-related, on every kind of
        # day — there is no longer a separate "Precipitation" row at all.
        for over in ({}, {'pty': '4'}, {'pty': '4', 'pop': None}, {'pop': None}):
            _, lines_en, _, lines_ko = W.build_card_lines(self._summary(**over))
            self.assertNotIn('Precipitation', [l['label'] for l in lines_en])
            self.assertNotIn('강수형태', [l['label'] for l in lines_ko])

    def test_a_dry_forecast_folds_the_rain_chance_into_conditions(self):
        # sky='3', pop=40, pty=None — the exact shape the old "Sky" label
        # used to split into two rows ("Sky: Partly cloudy" and a separate
        # "Chance of rain: 40%"); now it is one "Conditions" row.
        _, lines_en, _, lines_ko = W.build_card_lines(self._summary())
        self.assertNotIn('Sky', [l['label'] for l in lines_en])
        self.assertNotIn('Chance of rain', [l['label'] for l in lines_en])
        row_en = next(l for l in lines_en if l['label'] == 'Conditions')
        self.assertEqual(row_en['value'], 'Partly cloudy with a 40% chance of rain')
        row_ko = next(l for l in lines_ko if l['label'] == '날씨')
        self.assertEqual(row_ko['value'], '구름많음, 강수확률 40%')

    def test_a_wet_forecast_names_the_actual_type_not_generic_rain(self):
        # An active precipitation type (snow, showers, ...) is folded into
        # Conditions as the noun, rather than always saying "rain" or
        # splitting into a second row.
        _, lines_en, _, lines_ko = W.build_card_lines(self._summary(pty='4'))
        row_en = next(l for l in lines_en if l['label'] == 'Conditions')
        self.assertEqual(row_en['value'], 'Partly cloudy with a 40% chance of showers')
        # Korean stays with the generic 강수확률 figure rather than a type
        # word, matching the dry-day phrasing — deliberately not attempting
        # a natural Korean sentence naming the specific type.
        row_ko = next(l for l in lines_ko if l['label'] == '날씨')
        self.assertEqual(row_ko['value'], '구름많음, 강수확률 40%')

    def test_a_wet_forecast_with_no_pop_shows_sky_and_type_with_no_percent(self):
        _, lines_en, _, lines_ko = W.build_card_lines(self._summary(pty='4', pop=None))
        row_en = next(l for l in lines_en if l['label'] == 'Conditions')
        self.assertEqual(row_en['value'], 'Partly cloudy with showers')
        row_ko = next(l for l in lines_ko if l['label'] == '날씨')
        self.assertEqual(row_ko['value'], '구름많음, 소나기')

    def test_no_pop_and_no_precipitation_type_shows_sky_alone(self):
        _, lines_en, _, _ = W.build_card_lines(self._summary(pop=None))
        row = next(l for l in lines_en if l['label'] == 'Conditions')
        self.assertEqual(row['value'], 'Partly cloudy')

    def test_an_unrecognised_sky_code_still_states_the_rain_chance(self):
        # No sky text to prepend, but the figure itself must not be dropped.
        _, lines_en, _, lines_ko = W.build_card_lines(self._summary(sky='9'))
        row_en = next(l for l in lines_en if l['label'] == 'Conditions')
        self.assertEqual(row_en['value'], 'A 40% chance of rain')
        row_ko = next(l for l in lines_ko if l['label'] == '날씨')
        self.assertEqual(row_ko['value'], '강수확률 40%')

    def test_an_unrecognised_sky_code_with_an_active_type_and_no_pop(self):
        _, lines_en, _, lines_ko = W.build_card_lines(self._summary(sky='9', pty='4', pop=None))
        row_en = next(l for l in lines_en if l['label'] == 'Conditions')
        self.assertEqual(row_en['value'], 'Showers')
        row_ko = next(l for l in lines_ko if l['label'] == '날씨')
        self.assertEqual(row_ko['value'], '소나기')

    def test_a_clear_forecast_with_no_pop_shows_conditions_alone(self):
        _, lines_en, _, _ = W.build_card_lines(self._summary(sky='1', pop=None))
        row = next(l for l in lines_en if l['label'] == 'Conditions')
        self.assertEqual(row['value'], 'Clear')

    def test_conditions_emoji_prefers_precipitation_over_plain_sky(self):
        _, lines_en, _, _ = W.build_card_lines(self._summary(sky='1', pty='1'))
        row = next(l for l in lines_en if l['label'] == 'Conditions')
        self.assertEqual(row['emoji'], W.PTY_EMOJI['1'])

    def test_high_and_low_always_carry_both_units_in_english_only(self):
        opener_en, lines_en, opener_ko, lines_ko = W.build_card_lines(self._summary())
        hi_en = next(l for l in lines_en if l['label'] == 'High')
        hi_ko = next(l for l in lines_ko if l['label'] == '최고기온')
        self.assertIn('°F', hi_en['value'])
        self.assertNotIn('°F', hi_ko['value'])  # Korean card stays metric-only

    def test_opener_emoji_prefers_precipitation_over_plain_sky(self):
        opener_en, _, _, _ = W.build_card_lines(self._summary(sky='1', pty='1'))
        self.assertEqual(opener_en['emoji'], W.PTY_EMOJI['1'])

    def test_no_humidity_omits_the_row(self):
        _, lines_en, _, _ = W.build_card_lines(self._summary())
        self.assertNotIn('Humidity', [l['label'] for l in lines_en])

    def test_humidity_row_when_present(self):
        _, lines_en, _, lines_ko = W.build_card_lines(self._summary(humidity=62))
        self.assertEqual(next(l for l in lines_en if l['label'] == 'Humidity')['value'], '62%')
        self.assertEqual(next(l for l in lines_ko if l['label'] == '습도')['value'], '62%')

    def test_no_sunrise_or_sunset_omits_the_row(self):
        # today_summary() never sets these — they land in the summary dict
        # separately, from a different call, so absence must not crash.
        _, lines_en, _, _ = W.build_card_lines(self._summary())
        self.assertNotIn('Sunrise · Sunset', [l['label'] for l in lines_en])

    def test_only_one_of_the_pair_present_also_omits_the_row(self):
        # fetch_sun_times() only ever returns both fields or neither, but
        # the row logic itself should not assume that — a summary carrying
        # just one must not silently print the other as an empty half.
        _, lines_en, _, _ = W.build_card_lines(self._summary(sunrise='0533'))
        self.assertNotIn('Sunrise · Sunset', [l['label'] for l in lines_en])
        _, lines_en, _, _ = W.build_card_lines(self._summary(sunset='1856'))
        self.assertNotIn('Sunrise · Sunset', [l['label'] for l in lines_en])

    def test_sunrise_and_sunset_share_one_row(self):
        # Sunrise's time bolds via 'emph' inside the (regular) label;
        # Sunset's time bolds because it's the row's actual value, with
        # the word "Sunset" itself living in 'value_lead' at regular
        # weight — so both clock times bold and neither word does.
        _, lines_en, _, lines_ko = W.build_card_lines(
            self._summary(sunrise='0533', sunset='1856'))
        row_en = next(l for l in lines_en if l['label'].startswith('Sunrise'))
        row_ko = next(l for l in lines_ko if l['label'].startswith('일출'))
        self.assertEqual(row_en['label'], 'Sunrise 5:33 a.m.')
        self.assertEqual(row_en['emph'], '5:33 a.m.')
        self.assertEqual(row_en['value_lead'], '🌙 Sunset ')
        self.assertEqual(row_en['value'], '6:56 p.m.')
        self.assertEqual(row_en['alt'], 'Sunrise 5:33 a.m., Sunset 6:56 p.m.')
        self.assertEqual(row_ko['label'], '일출 오전 5시 33분')
        self.assertEqual(row_ko['value_lead'], '🌙 일몰 ')
        self.assertEqual(row_ko['value'], '오후 6시 56분')

    def test_no_rain_24h_omits_the_row(self):
        _, lines_en, _, _ = W.build_card_lines(self._summary())
        self.assertNotIn("Yesterday's rainfall", [l['label'] for l in lines_en])

    def test_a_genuinely_dry_yesterday_also_omits_the_row(self):
        # A measured 0.0mm and no reading at all must read the same way —
        # matching kma_facts()'s own `if rn:` rule in seoul_index_post.py.
        _, lines_en, _, _ = W.build_card_lines(self._summary(rain_24h=0.0))
        self.assertNotIn("Yesterday's rainfall", [l['label'] for l in lines_en])

    def test_rain_24h_row_when_present(self):
        _, lines_en, _, lines_ko = W.build_card_lines(self._summary(rain_24h=12.5))
        self.assertEqual(next(l for l in lines_en if l['label'] == "Yesterday's rainfall")['value'],
                         '12.5mm (0.49 in)')
        self.assertEqual(next(l for l in lines_ko if l['label'] == '어제 강수량')['value'],
                         '12.5mm')

    def test_no_forecast_rain_data_omits_the_row(self):
        # forecast_rain_today() returned (None, None) — no PCP rows at all.
        _, lines_en, _, _ = W.build_card_lines(self._summary())
        self.assertNotIn('Rain expected', [l['label'] for l in lines_en])

    def test_a_genuinely_dry_forecast_also_omits_the_row(self):
        # Every hour read 강수없음 — (0.0, True) — same convention as
        # rain_24h's own dry-day omission.
        _, lines_en, _, _ = W.build_card_lines(
            self._summary(rain_forecast_mm=0.0, rain_forecast_exact=True))
        self.assertNotIn('Rain expected', [l['label'] for l in lines_en])

    def test_a_trace_only_forecast_still_shows_the_row(self):
        # (0.0, False): the only rain forecast was a '1mm 미만' hour, which
        # is real information and must not read the same as a dry day.
        _, lines_en, _, lines_ko = W.build_card_lines(
            self._summary(rain_forecast_mm=0.0, rain_forecast_exact=False))
        self.assertEqual(next(l for l in lines_en if l['label'] == 'Rain expected')['value'],
                         'less than 1mm (0.04 in)')
        self.assertEqual(next(l for l in lines_ko if l['label'] == '예상 강수량')['value'],
                         '1mm 미만')

    def test_forecast_rain_row_when_exact(self):
        _, lines_en, _, lines_ko = W.build_card_lines(
            self._summary(rain_forecast_mm=13.0, rain_forecast_exact=True))
        self.assertEqual(next(l for l in lines_en if l['label'] == 'Rain expected')['value'],
                         '13.0mm (0.51 in)')
        self.assertEqual(next(l for l in lines_ko if l['label'] == '예상 강수량')['value'],
                         '13.0mm')

    def test_forecast_rain_row_when_inexact_reads_as_a_floor(self):
        _, lines_en, _, lines_ko = W.build_card_lines(
            self._summary(rain_forecast_mm=30.0, rain_forecast_exact=False))
        self.assertEqual(next(l for l in lines_en if l['label'] == 'Rain expected')['value'],
                         '30.0mm (1.18 in) or more')
        self.assertEqual(next(l for l in lines_ko if l['label'] == '예상 강수량')['value'],
                         '30.0mm 이상')

    def test_rain_expected_and_yesterday_rainfall_are_adjacent_separate_rows(self):
        # Both are simple standalone rows (unlike sunrise/sunset, which
        # merges) — a merged version of this pair was tried and rejected:
        # the combined text is too long for one row and wraps badly. They
        # sit next to each other in the card, "Rain expected" first.
        _, lines_en, _, lines_ko = W.build_card_lines(self._summary(
            rain_forecast_mm=35.0, rain_forecast_exact=False, rain_24h=34.8))
        labels_en = [l['label'] for l in lines_en]
        self.assertIn('Rain expected', labels_en)
        self.assertIn("Yesterday's rainfall", labels_en)
        self.assertLess(labels_en.index('Rain expected'),
                        labels_en.index("Yesterday's rainfall"))
        rain_row = next(l for l in lines_en if l['label'] == 'Rain expected')
        y_row = next(l for l in lines_en if l['label'] == "Yesterday's rainfall")
        self.assertEqual(rain_row['value'], '35.0mm (1.38 in) or more')
        self.assertEqual(y_row['value'], '34.8mm (1.37 in)')
        self.assertNotIn('value_lead', rain_row)
        self.assertNotIn('value_lead', y_row)


class FetchSunTimes(unittest.TestCase):
    """XML parsing only, via a mocked curl — no real network call. This is
    the one fetcher in the file that isn't JSON, so it's the one place a
    schema surprise would be silent otherwise."""

    def _run(self, stdout, returncode=0):
        with patch('seoul_weather_post.subprocess.run') as mock_run:
            mock_run.return_value.returncode = returncode
            mock_run.return_value.stdout = stdout
            return W.fetch_sun_times('fake-key', '20260830')

    def test_a_normal_response_yields_both_times(self):
        xml = ('<response><body><items><item>'
               '<sunrise>0533</sunrise><sunset>1856</sunset>'
               '</item></items></body></response>')
        self.assertEqual(self._run(xml), ('0533', '1856'))

    def test_an_unapproved_key_error_response_is_none_not_a_crash(self):
        # data.go.kr's real shape for "no 활용신청 on this API yet" — a
        # different root element entirely, with no sunrise/sunset anywhere.
        xml = ('<OpenAPI_ServiceResponse><cmmMsgHeader>'
               '<returnAuthMsg>SERVICE_KEY_IS_NOT_REGISTERED_ERROR</returnAuthMsg>'
               '</cmmMsgHeader></OpenAPI_ServiceResponse>')
        self.assertIsNone(self._run(xml))

    def test_malformed_xml_is_none_not_a_crash(self):
        self.assertIsNone(self._run('<response><body>'))

    def test_empty_response_is_none(self):
        self.assertIsNone(self._run(''))

    def test_curl_failure_is_none(self):
        self.assertIsNone(self._run('<response/>', returncode=1))

    def test_no_key_short_circuits_without_calling_curl(self):
        with patch('seoul_weather_post.subprocess.run') as mock_run:
            self.assertIsNone(W.fetch_sun_times(None, '20260830'))
            mock_run.assert_not_called()


class YesterdayRain(unittest.TestCase):
    def test_a_wet_day_returns_the_millimetres(self):
        with patch('seoul_weather_post._wx_rows', return_value=[{'sumRn': '12.5'}]):
            self.assertEqual(W.yesterday_rain('fake-key', date(2026, 8, 30)), 12.5)

    def test_a_dry_day_is_none_not_zero(self):
        # sumRn == 0.0 and a missing row must read the same way — matching
        # kma_facts()'s own `if rn:` rule in seoul_index_post.py.
        with patch('seoul_weather_post._wx_rows', return_value=[{'sumRn': '0.0'}]):
            self.assertIsNone(W.yesterday_rain('fake-key', date(2026, 8, 30)))

    def test_a_missing_row_is_none(self):
        with patch('seoul_weather_post._wx_rows', return_value=[]):
            self.assertIsNone(W.yesterday_rain('fake-key', date(2026, 8, 30)))

    def test_no_key_short_circuits_without_calling_wx_rows(self):
        with patch('seoul_weather_post._wx_rows') as mock_rows:
            self.assertIsNone(W.yesterday_rain(None, date(2026, 8, 30)))
            mock_rows.assert_not_called()


class Footnotes(unittest.TestCase):
    def test_states_the_issue_time_and_that_it_is_a_forecast(self):
        # House style: "a.m."/"p.m.", lowercase, with periods — not a bare
        # 24-hour clock reading.
        note_en, note_ko = W.footnotes('0500')
        self.assertIn('issued at 5 a.m. KST', note_en)
        self.assertIn('forecast', note_en.lower())
        self.assertIn('5시', note_ko)

    def test_midnight_base_time_formats_without_a_stray_leading_zero_bug(self):
        # '2300' -> hour 23, not 3 — a naive int(base_time[:1]) trap.
        note_en, note_ko = W.footnotes('2300')
        self.assertIn('issued at 11 p.m. KST', note_en)
        self.assertIn('23시', note_ko)

    def test_every_kma_base_hour_renders_a_clean_twelve_hour_reading(self):
        expected = {'0200': '2 a.m.', '0500': '5 a.m.', '0800': '8 a.m.',
                    '1100': '11 a.m.', '1400': '2 p.m.', '1700': '5 p.m.',
                    '2000': '8 p.m.', '2300': '11 p.m.'}
        for base_time, want in expected.items():
            note_en, _ = W.footnotes(base_time)
            self.assertIn(want, note_en, base_time)

    def test_default_still_claims_nothing_is_observed(self):
        # Yesterday's rainfall is the one row on the card that IS an ASOS
        # observation, not a KMA forecast — a card without that row must
        # keep the blanket claim, which the bare "not an observed reading"
        # default form asserts.
        note_en, note_ko = W.footnotes('0500')
        self.assertIn('not an observed reading', note_en)
        self.assertNotIn('except', note_en)
        self.assertNotIn('다만', note_ko)

    def test_yesterday_rain_present_carves_out_the_one_exception(self):
        # Otherwise the footnote flatly misdescribes the one reading on the
        # card that actually is observed.
        note_en, note_ko = W.footnotes('0500', has_yesterday_rain=True)
        self.assertIn('not an observed reading', note_en)
        self.assertIn("except yesterday's rainfall, which is", note_en)
        self.assertIn('실제 관측값이 아닙니다', note_ko)
        self.assertIn('어제 강수량은 실제 관측값입니다', note_ko)


class AlreadyPosted(unittest.TestCase):
    """already_posted() is what makes the 06:30 safety-net launchd fire (see
    com.chrisstanford.seoulweather.plist) safe: it must not repost when
    05:25 already succeeded, and it must not stay silent forever if 05:25
    genuinely failed."""

    def _with_log(self, lines):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        log_path = Path(td.name) / 'weather_history.jsonl'
        if lines is not None:
            log_path.write_text('\n'.join(lines) + ('\n' if lines else ''))
        return patch.object(W, 'WEATHER_LOG', log_path)

    def test_no_log_file_at_all_is_not_posted(self):
        with self._with_log(None):
            self.assertFalse(W.already_posted('20260904'))

    def test_empty_log_is_not_posted(self):
        with self._with_log([]):
            self.assertFalse(W.already_posted('20260904'))

    def test_a_different_date_in_the_log_is_not_posted(self):
        with self._with_log([json.dumps({'target_date': '20260903'})]):
            self.assertFalse(W.already_posted('20260904'))

    def test_todays_date_in_the_log_is_posted(self):
        with self._with_log([json.dumps({'target_date': '20260903'}),
                              json.dumps({'target_date': '20260904'})]):
            self.assertTrue(W.already_posted('20260904'))

    def test_a_corrupt_line_is_skipped_not_fatal(self):
        # A torn write (process killed mid-append) must not make every
        # later re-run of this check blow up rather than answer the question.
        with self._with_log(['{not valid json', json.dumps({'target_date': '20260904'})]):
            self.assertTrue(W.already_posted('20260904'))


if __name__ == '__main__':
    unittest.main()
