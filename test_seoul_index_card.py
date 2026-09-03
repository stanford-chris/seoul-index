"""Tests for _shoot()'s retry in seoul_index_card.py.

The failure to fear here is not a crash but a single Chrome hang costing a
whole post — the 3 September 2026 weather run lost its card to one 60s
TimeoutExpired with nothing wrong in the HTML. So the tests pin: one hang is
retried and a second attempt that succeeds still renders; two hangs in a row
raise CardRenderError (never let the raw TimeoutExpired escape uncaught,
since callers only catch CardRenderError); and a real Chrome crash (no PNG,
no timeout) is NOT retried, because retrying a crash would just mask it.

Only subprocess.run and the Pillow crop step are mocked — the real Chrome
binary at CHROME is never invoked, and the real tempfile/Path plumbing runs
as-is, so these don't depend on how Chrome behaves, only on whether it exists
at CHROME (true on this Mac, where these bots actually run).
"""
import subprocess
import sys
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

    def test_two_hangs_raise_card_render_error_not_timeout_expired(self):
        def always_hangs(cmd, **k):
            raise subprocess.TimeoutExpired(cmd='chrome', timeout=60)

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


if __name__ == '__main__':
    unittest.main()
