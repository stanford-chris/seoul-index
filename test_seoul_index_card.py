"""Tests for _shoot()'s retry in seoul_index_card.py.

The failure to fear here is not a crash but a Chrome hang costing a whole
post — the 3 September 2026 weather run lost its card to one 60s
TimeoutExpired with nothing wrong in the HTML, and a single retry (added that
day) still wasn't enough on 4 September, when both attempts hung back to
back. So RENDER_TIMEOUTS now escalates across three attempts rather than
repeating one budget, and the tests pin: a hang is retried at the NEXT
attempt's (longer) budget; a hang that finally succeeds still renders; all
three attempts hanging raises CardRenderError (never let the raw
TimeoutExpired escape uncaught, since callers only catch CardRenderError);
and a real Chrome crash (no PNG, no timeout) is NOT retried at all, because
retrying a crash would just mask it.

Only subprocess.run and the Pillow crop step are mocked — the real Chrome
binary at CHROME is never invoked, and the real tempfile/Path plumbing runs
as-is, so these don't depend on how Chrome behaves, only on whether it exists
at CHROME (true on this Mac, where these bots actually run).
"""
import subprocess
import sys
import re
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import seoul_index_card as C


def _screenshot_path(cmd):
    for arg in cmd:
        if arg.startswith('--screenshot='):
            return Path(arg.split('=', 1)[1])
    raise AssertionError('no --screenshot= arg in Chrome command')


class ShootRetry(unittest.TestCase):
    def test_one_hang_then_success_still_renders(self):
        calls = {'n': 0}

        def fake_run(cmd, **k):
            calls['n'] += 1
            if calls['n'] == 1:
                raise subprocess.TimeoutExpired(cmd='chrome', timeout=60)
            _screenshot_path(cmd).write_bytes(b'not a real png, just needs to exist')
            return Mock(returncode=0, stdout='', stderr='')

        with patch.object(C.subprocess, 'run', side_effect=fake_run), \
             patch.object(C, '_crop_to_content', return_value=('out.png', (100, 50))):
            path, size = C._shoot('<html></html>', 'out.png')

        self.assertEqual(calls['n'], 2)
        self.assertEqual(size, (100, 50))

    def test_two_hangs_then_success_still_renders(self):
        """The 4 September case: the old code gave up after exactly two
        attempts. This must not still give up there."""
        calls = {'n': 0}

        def fake_run(cmd, **k):
            calls['n'] += 1
            if calls['n'] <= 2:
                raise subprocess.TimeoutExpired(cmd='chrome', timeout=k.get('timeout'))
            _screenshot_path(cmd).write_bytes(b'not a real png, just needs to exist')
            return Mock(returncode=0, stdout='', stderr='')

        with patch.object(C.subprocess, 'run', side_effect=fake_run), \
             patch.object(C, '_crop_to_content', return_value=('out.png', (100, 50))):
            path, size = C._shoot('<html></html>', 'out.png')

        self.assertEqual(calls['n'], 3)
        self.assertEqual(size, (100, 50))

    def test_each_attempt_uses_the_next_escalating_timeout(self):
        seen_timeouts = []

        def always_hangs(cmd, **k):
            seen_timeouts.append(k.get('timeout'))
            raise subprocess.TimeoutExpired(cmd='chrome', timeout=k.get('timeout'))

        with patch.object(C.subprocess, 'run', side_effect=always_hangs):
            with self.assertRaises(C.CardRenderError):
                C._shoot('<html></html>', 'out.png')

        self.assertEqual(seen_timeouts, list(C.RENDER_TIMEOUTS))

    def test_all_attempts_hanging_raises_card_render_error_not_timeout_expired(self):
        def always_hangs(cmd, **k):
            raise subprocess.TimeoutExpired(cmd='chrome', timeout=k.get('timeout'))

        with patch.object(C.subprocess, 'run', side_effect=always_hangs):
            with self.assertRaises(C.CardRenderError):
                C._shoot('<html></html>', 'out.png')

    def test_a_crash_with_no_timeout_is_not_retried(self):
        calls = {'n': 0}

        def crashes_no_png(cmd, **k):
            calls['n'] += 1
            return Mock(returncode=1, stdout='', stderr='boom')

        with patch.object(C.subprocess, 'run', side_effect=crashes_no_png):
            with self.assertRaises(C.CardRenderError):
                C._shoot('<html></html>', 'out.png')

        self.assertEqual(calls['n'], 1)


class RowEmojiWrapGuard(unittest.TestCase):
    """9 September 2026: a weather card's Conditions row ("Partly cloudy with
    a 30% chance of rain") pushed .lab (min-width:0, overflow-wrap:anywhere)
    so tight against the nowrap .val that the only break opportunity left was
    the space between the row's emoji and its label — stranding the emoji
    alone on its own line above "Conditions". https://bsky.app/profile/
    seoul-index.bsky.social/post/3muzu66kuic2c

    Real Chrome layout is what actually decides whether a row wraps (see the
    script in _build_html, verified by hand against that exact row: the
    emoji dropped and Conditions rendered on one line, while short rows like
    High/Low kept theirs) — these tests don't invoke Chrome (this file's own
    convention, see the module docstring) and instead pin the two things a
    future edit could silently break: the emoji sits in a REMOVABLE `.ico`
    span rather than as plain text glued into `.lab` (or the wrap-detection
    script has nothing to select), and that script is wired into every
    rendered card exactly once (twice would double-remove harmlessly but
    signals the template grew a duplicate <body> section)."""

    def test_row_emoji_is_a_removable_ico_span_not_bare_text(self):
        html = C._row_html({'emoji': '☁️', 'label': 'Conditions',
                            'value': 'Partly cloudy with a 30% chance of rain'})
        self.assertIn('<span class="ico">', html)
        self.assertNotIn('>☁️ Conditions', html)  # would be bare-text glue

    def test_row_with_no_emoji_has_no_ico_span(self):
        html = C._row_html({'label': 'Coffee shops', 'value': '₩651.4bn'})
        self.assertNotIn('class="ico"', html)

    def test_build_html_wires_the_wrap_detection_script_exactly_once(self):
        html = C._build_html(
            {'emoji': '☁️', 'text': "Seoul's forecast for today"},
            [{'emoji': '☁️', 'label': 'Conditions',
              'value': 'Partly cloudy with a 30% chance of rain'}])
        self.assertEqual(html.count("querySelectorAll('.r')"), 1)
        self.assertIn('ico.remove()', html)


class BusRouteMap(unittest.TestCase):
    """render_bus_route_map() added 10 Sep 2026 when the route-map thread
    reply — designed and approved over several rounds, then never actually
    wired into main() — was finally posted for real. _shoot() itself is
    mocked here exactly as ShootRetry mocks it above; these tests are about
    what render_bus_route_map() builds before handing off to it, not about
    Chrome.
    """

    def test_no_data_raises_rather_than_rendering_a_blank_image(self):
        with self.assertRaises(C.CardRenderError):
            C.render_bus_route_map([], [(127.0, 37.5)], 'out.png')
        with self.assertRaises(C.CardRenderError):
            C.render_bus_route_map([('Busiest: Route 143', '#d70000', [(127.0, 37.5)])],
                                   [], 'out.png')

    @patch.object(C, '_shoot')
    def test_shoots_a_square_window_not_the_cards_own_size(self, mock_shoot):
        mock_shoot.return_value = ('out.png', (1200, 1200))
        C.render_bus_route_map(
            [('Busiest: Route 143', '#d70000', [(127.0, 37.5), (127.01, 37.51)])],
            [(127.0, 37.5), (127.02, 37.52)], 'out.png', title='6 September')
        (doc, out_path), kwargs = mock_shoot.call_args
        self.assertEqual(kwargs['size'], (C.MAP_SIZE, C.MAP_SIZE))
        self.assertIn('<svg', doc)
        self.assertIn('6 September', doc)

    @patch.object(C, '_shoot')
    def test_each_route_gets_its_own_stroke_colour_and_legend_line(self, mock_shoot):
        mock_shoot.return_value = ('out.png', (1200, 1200))
        C.render_bus_route_map(
            [('Busiest: Route 143', '#d70000', [(127.0, 37.5), (127.01, 37.51)]),
             ('Quietest: Route 8641', '#000000', [(127.0, 37.5), (127.02, 37.52)])],
            [(127.0, 37.5), (127.02, 37.52)], 'out.png')
        doc = mock_shoot.call_args[0][0]
        self.assertIn('stroke="#d70000"', doc)
        self.assertIn('stroke="#000000"', doc)
        self.assertIn('Busiest: Route 143', doc)
        self.assertIn('Quietest: Route 8641', doc)

    @patch.object(C, '_shoot')
    def test_a_long_caption_wraps_and_lifts_the_legend(self, mock_shoot):
        # With his bus-stops sentence on it (12 September 2026) the caption
        # runs to ~150 characters, and drawn as one line it was cut at the
        # right edge. Each extra line lifts the legend by a line height.
        mock_shoot.return_value = ('out.png', (1200, 1200))
        routes = [('Busiest: Route 2211', '#d70000', [(127.0, 37.5), (127.01, 37.51)])]
        stops = [(127.0, 37.5), (127.02, 37.52)]
        long = ('Stops on each route, September 8: not necessarily its full path. The map '
                'is composed of gray dots that represent each of Seoul’s 11,236 bus stops.')
        C.render_bus_route_map(routes, stops, 'out.png', caption='short')
        one = mock_shoot.call_args[0][0]
        C.render_bus_route_map(routes, stops, 'out.png', caption=long)
        two = mock_shoot.call_args[0][0]
        self.assertEqual(one.count('font-size="9"'), 1)
        self.assertEqual(two.count('font-size="9"'), 2)
        self.assertNotIn(long, two)                 # never drawn as one line
        self.assertIn('11,236 bus stops.', two)     # nothing dropped
        legend_y = lambda doc: int(re.search(r'<rect x="30" y="(\d+)"', doc).group(1))
        self.assertEqual(legend_y(one) - legend_y(two), 12)

    @patch.object(C, '_shoot')
    def test_served_stops_are_drawn_as_dots_and_a_three_tuple_still_works(self, mock_shoot):
        # 12 September 2026: the served stops (the footnote's count) are dots
        # on the line. Older callers passing (label, colour, path) draw none.
        mock_shoot.return_value = ('out.png', (1200, 1200))
        stops = [(127.0, 37.5), (127.02, 37.52)]
        C.render_bus_route_map(
            [('Busiest: Route 2211', '#d70000', [(127.0, 37.5), (127.01, 37.51)],
              [(127.0, 37.5), (127.005, 37.505), (127.01, 37.51)])], stops, 'out.png')
        doc = mock_shoot.call_args[0][0]
        self.assertEqual(doc.count('class="served"'), 3)
        C.render_bus_route_map(
            [('Busiest: Route 2211', '#d70000', [(127.0, 37.5), (127.01, 37.51)])], stops, 'out.png')
        self.assertEqual(mock_shoot.call_args[0][0].count('class="served"'), 0)

    @patch.object(C, '_shoot')
    def test_a_route_with_no_stops_draws_no_path_but_does_not_crash(self, mock_shoot):
        # A cross-language edge case that should never actually reach here
        # (main() only calls this once bus_route_map_stops() has answered
        # for all three routes) but a route resolving to an empty stop list
        # must not raise partway through drawing the other two.
        mock_shoot.return_value = ('out.png', (1200, 1200))
        C.render_bus_route_map(
            [('Busiest: Route 143', '#d70000', [(127.0, 37.5), (127.01, 37.51)]),
             ('Quietest: Route 8641', '#000000', [])],
            [(127.0, 37.5), (127.02, 37.52)], 'out.png')
        self.assertTrue(mock_shoot.called)


if __name__ == '__main__':
    unittest.main()
