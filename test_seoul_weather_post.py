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
import sys
import unittest
from datetime import datetime
from pathlib import Path
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


class BuildCardLines(unittest.TestCase):
    def _summary(self, **over):
        base = {'hi': 30.0, 'lo': 22.0, 'pop': 40, 'sky': '3', 'pty': None,
                'humidity': None}
        base.update(over)
        return base

    def test_a_dry_forecast_has_no_precipitation_row(self):
        _, lines_en, _, lines_ko = W.build_card_lines(self._summary())
        labels_en = [l['label'] for l in lines_en]
        labels_ko = [l['label'] for l in lines_ko]
        self.assertNotIn('Precipitation', labels_en)
        self.assertNotIn('강수형태', labels_ko)

    def test_a_wet_forecast_has_one_row_not_two(self):
        # "Chance of rain" and "Precipitation" used to both appear and say
        # the same thing twice — now there is exactly one rain-related row.
        _, lines_en, _, lines_ko = W.build_card_lines(self._summary(pty='4'))
        rain_labels_en = [l['label'] for l in lines_en
                          if l['label'] in ('Chance of rain', 'Precipitation')]
        self.assertEqual(rain_labels_en, ['Precipitation'])
        row = next(l for l in lines_ko if l['label'] == '강수형태')
        self.assertEqual(row['value'], '소나기 (40%)')

    def test_a_wet_forecast_with_no_pop_shows_the_type_alone(self):
        _, lines_en, _, _ = W.build_card_lines(self._summary(pty='4', pop=None))
        row = next(l for l in lines_en if l['label'] == 'Precipitation')
        self.assertEqual(row['value'], 'Showers')

    def test_no_pop_omits_the_rain_chance_row_rather_than_showing_none(self):
        _, lines_en, _, _ = W.build_card_lines(self._summary(pop=None))
        self.assertNotIn('Chance of rain', [l['label'] for l in lines_en])

    def test_an_unrecognised_sky_code_omits_the_sky_row_rather_than_a_blank(self):
        _, lines_en, _, lines_ko = W.build_card_lines(self._summary(sky='9'))
        self.assertNotIn('Sky', [l['label'] for l in lines_en])
        self.assertNotIn('하늘상태', [l['label'] for l in lines_ko])

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


class Footnotes(unittest.TestCase):
    def test_states_the_issue_time_and_that_it_is_a_forecast(self):
        note_en, note_ko = W.footnotes('0500')
        self.assertIn('05:00', note_en)
        self.assertIn('forecast', note_en.lower())
        self.assertIn('5시', note_ko)

    def test_midnight_base_time_formats_without_a_stray_leading_zero_bug(self):
        # '2300' -> hour 23, not 3 — a naive int(base_time[:1]) trap.
        note_en, note_ko = W.footnotes('2300')
        self.assertIn('23:00', note_en)
        self.assertIn('23시', note_ko)


if __name__ == '__main__':
    unittest.main()
