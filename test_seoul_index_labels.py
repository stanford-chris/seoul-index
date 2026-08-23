"""Tests for the label-fidelity check: what happens to a label the checker
rejects. No network, no model call, no posting — `_ask_json` is stubbed, so
what is tested is the part that decides rather than the model's opinion.

The cases are the ones the 23 August 2026 audit of 71 published cards actually
found, not invented edge cases:

  - a cross-vein card whose neutral opener leaves a tourism line reading
    "Seodaemun Prison History Hall: 25,343", with nothing saying those are a
    month's visitors. Five of the seven findings were this shape, and every one
    was on a cross-vein card.
  - a Korean line saying 체결된 (concluded) where the English says "filed",
    which is a different fact about the same transaction.
"""
import sys, unittest
from pathlib import Path

sys.argv = ['test']
sys.path.insert(0, str(Path(__file__).resolve().parent))
import seoul_index_post as S


def row(cat='tourism', pool='A month’s visitors to the Seodaemun Prison History Hall',
        en='Seodaemun Prison History Hall', ko='서대문형무소역사관', pin=False):
    return {'cat': cat, 'pool_en': pool, 'label_en': en, 'label_ko': ko,
            'value_en': '25,343', 'pin': pin}


def line(r):
    return {'label_en': r['label_en'], 'label_ko': r['label_ko'],
            'value_en': r['value_en'], 'pin': r['pin']}


class Repairs(unittest.TestCase):

    def setUp(self):
        self.logged = []
        self._real = (S._ask_json, S._log_labels)
        S._log_labels = lambda *a, **k: self.logged.append((a, k))

    def tearDown(self):
        S._ask_json, S._log_labels = self._real

    def check(self, rows, problems=None, error=None):
        if error:
            def boom(*a, **k):
                raise RuntimeError(error)
            S._ask_json = boom
        else:
            S._ask_json = lambda *a, **k: {'problems': problems or []}
        lines = [line(r) for r in rows]
        S.check_labels(lines, rows, 'Seoul by the numbers', '숫자로 보는 서울',
                       log=lambda *_: None)
        return lines

    def test_a_clean_card_is_left_alone(self):
        out = self.check([row()])
        self.assertEqual(out[0]['label_en'], 'Seodaemun Prison History Hall')

    def test_a_bare_label_falls_back_to_the_source_wording(self):
        # The audit's dominant finding: the number means nothing until the
        # label says it is a month's visitors.
        out = self.check([row()], [{'i': 0, 'lang': 'en', 'problem': 'no metric'}])
        self.assertEqual(out[0]['label_en'],
                         'A month’s visitors to the Seodaemun Prison History Hall')

    def test_korean_drift_falls_back_to_the_english_source_label(self):
        # Odd on a Korean card, and still better than a Korean line claiming
        # the leases were concluded when the source says they were filed.
        r = row(cat='property', pool='Jeonse leases filed',
                en='Jeonse leases filed', ko='체결된 전세 계약')
        out = self.check([r], [{'i': 0, 'lang': 'ko', 'problem': '체결 ≠ 신고'}])
        self.assertEqual(out[0]['label_ko'], 'Jeonse leases filed')
        self.assertEqual(out[0]['label_en'], 'Jeonse leases filed')

    def test_a_pinned_label_is_never_repaired(self):
        # A pin declares the wording load-bearing and it was never the model's
        # to reword, so a flag on one is the checker misreading the card.
        out = self.check([row(pin=True)],
                         [{'i': 0, 'lang': 'en', 'problem': 'no metric'}])
        self.assertEqual(out[0]['label_en'], 'Seodaemun Prison History Hall')

    def test_only_the_flagged_line_moves(self):
        rows = [row(), row(en='Crowd in the Seongsu cafe strip', cat='crowd',
                           pool='Estimated crowd in the Seongsu cafe strip right now')]
        out = self.check(rows, [{'i': 0, 'lang': 'en', 'problem': 'no metric'}])
        self.assertTrue(out[0]['label_en'].startswith('A month’s visitors'))
        self.assertEqual(out[1]['label_en'], 'Crowd in the Seongsu cafe strip')

    def test_a_check_that_could_not_run_leaves_the_card_alone(self):
        # The card goes out unchecked, exactly as every card did before this
        # existed. The error is logged so a checker broken for a week shows up.
        out = self.check([row()], error='claude -p timed out')
        self.assertEqual(out[0]['label_en'], 'Seodaemun Prison History Hall')
        self.assertEqual(self.logged[-1][1].get('error'), 'claude -p timed out')

    def test_a_nonsense_index_is_ignored_rather_than_crashing_the_card(self):
        for bad in ({'i': 9, 'lang': 'en', 'problem': 'x'},
                    {'i': -1, 'lang': 'en', 'problem': 'x'},
                    {'i': 'first', 'lang': 'en', 'problem': 'x'},
                    {'lang': 'en'}):
            out = self.check([row()], [bad])
            self.assertEqual(out[0]['label_en'], 'Seodaemun Prison History Hall')

    def test_an_empty_card_does_not_call_the_model(self):
        def boom(*a, **k):
            raise AssertionError('should not have been called')
        S._ask_json = boom
        S.check_labels([], [], 'x', 'y', log=lambda *_: None)


if __name__ == '__main__':
    unittest.main()
