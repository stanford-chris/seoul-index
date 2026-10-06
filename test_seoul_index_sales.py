"""seoul_index_sales.py: the city's own citywide estimates (VwsmMegaSelngW),
never a sum over commercial districts, and never a partial quarter written
over a good file. Run by path or by discovery."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))
import seoul_index_sales as T


def mega(q, n, mega_cd='11'):
    return [{'STDR_YYQU_CD': q, 'MEGA_CD': mega_cd, 'SVC_INDUTY_CD_NM': f'업종{i}',
             'THSMON_SELNG_AMT': 1000.0 * (i + 1), 'THSMON_SELNG_CO': 10.0}
            for i in range(n)]


class CitywideSales(unittest.TestCase):
    def run_main(self, rows):
        out = Path(tempfile.mkdtemp()) / 'sales_agg.json'

        def get(url):
            if '/1/1/' in url:
                return {T.SERVICE: {'list_total_count': len(rows)}}
            return {T.SERVICE: {'row': rows}}
        with mock.patch.object(T, 'http_get_json', get), \
             mock.patch.object(T, 'OUT', out), \
             mock.patch.object(T.net_guard, 'require_network', lambda *a: None), \
             mock.patch.object(T, 'CONFIG', mock.Mock(read_text=lambda: '{"api_key": "K"}')), \
             mock.patch('builtins.print'):
            T.main()
        return json.loads(out.read_text())

    def test_only_seoul_rows_and_the_newest_quarter(self):
        out = self.run_main(mega('20261', 63) + mega('20262', 63) + mega('20262', 63, '26'))
        self.assertEqual(out['latest_quarter'], '20262')
        self.assertEqual(out['quarters']['20262'], 63)
        self.assertEqual(out['by_quarter']['20262']['업종0']['amt'], 1000.0)

    def test_a_partial_quarter_is_refused(self):
        with self.assertRaises(SystemExit):
            self.run_main(mega('20261', 63) + mega('20262', 10))


if __name__ == '__main__':
    unittest.main()
