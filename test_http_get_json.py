"""http_get_json must survive a curl cut off mid-way through a multibyte
character (3 October 2026: UnicodeDecodeError killed the 08:30 run).

Uses a fake `curl` on PATH: first call prints a truncated UTF-8 body and exits
28 (curl's timeout code), second prints valid JSON. No network."""
import os, stat, tempfile, unittest, pathlib, sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import seoul_index_post as S
# These fixtures are synthetic feeds, which fail the source checks by design;
# the checks themselves are tested in test_seoul_index_source_checks.py.
S.SOURCE_CHECKS = False


class TruncatedMultibyte(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.count = pathlib.Path(self.dir, 'n')
        curl = pathlib.Path(self.dir, 'curl')
        curl.write_text(f"""#!/bin/sh
n=$(cat {self.count} 2>/dev/null || echo 0); n=$((n+1)); echo $n > {self.count}
if [ $n -eq 1 ]; then printf '{{"name": "\\352\\260'; exit 28; fi
printf '{{"name": "\\352\\260\\200"}}'
""")
        curl.chmod(curl.stat().st_mode | stat.S_IEXEC)
        self.old_path = os.environ['PATH']
        os.environ['PATH'] = self.dir + os.pathsep + self.old_path

    def tearDown(self):
        os.environ['PATH'] = self.old_path

    def test_truncated_body_is_retried_not_raised(self):
        self.assertEqual(S.http_get_json('http://example.invalid/'), {'name': '가'})
        self.assertEqual(self.count.read_text().strip(), '2')


if __name__ == '__main__':
    unittest.main()
