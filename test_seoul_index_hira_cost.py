"""Tests for hira_cost_facts() — the 3단상병별 시도별 disease-cost vein, added
2 September 2026 and given a second per-patient frame the same day.

⚠️ This is a DIFFERENT HIRA dataset from the one hira_facts() reads (see the
block comment above hira_cost_facts() in seoul_index_post.py for why): an
odcloud auto-converted file dump, one opaque uddi per year, filtered
server-side by cond[시도구분::EQ]/cond[주상병코드::EQ]. No network, no model
call: subprocess.run is stubbed per test.
"""
import json, re, sys, types, unittest
from pathlib import Path

sys.argv = ['test']
sys.path.insert(0, str(Path(__file__).resolve().parent))
import seoul_index_post as S

CODE_RE = re.compile(r'cond%5B%EC%A3%BC%EC%83%81%EB%B3%91%EC%BD%94%EB%93%9C'
                     r'%3A%3AEQ%5D=([A-Z0-9]+)')


def stub_run(data, missing=frozenset()):
    """A fake subprocess.run returning one row per curated code, keyed by the
    cond[주상병코드::EQ] value in the request URL. `data`: {code: (cost,
    patients)}. `missing` codes get an empty `data` list, as odcloud returns
    for a code with zero matching rows."""
    def run(cmd, **kw):
        url = cmd[-1]
        m = CODE_RE.search(url)
        code = m.group(1) if m else None
        if code in missing or code not in data:
            body = {'data': [], 'matchCount': 0}
        else:
            cost, patients = data[code]
            body = {'data': [{'주상병코드': code, '시도구분': '서울',
                              '요양급여비용총액(선별포함)': cost,
                              '환자수': patients}],
                    'matchCount': 1}
        return types.SimpleNamespace(stdout=json.dumps(body), returncode=0)
    return run


# {code: (cost, patients)}, chosen so BOTH frames independently have no
# accidental dead heat but a clear (>=3x) gap — verified by computation, not
# eyeballed. Tests that want a heat pair override individual entries below.
BASE_DATA = {
    'C50': (700_000_000_000, 100_000),
    'K05': (600_000_000_000, 5_000_000),
    'N18': (500_000_000_000, 120_000),
    'C34': (400_000_000_000, 70_000),
    'M17': (300_000_000_000, 630_000),
    'M54': (200_000_000_000, 1_200_000),
    'C22': (150_000_000_000, 40_000),
    'I63': (100_000_000_000, 120_000),
    'C16': (80_000_000_000, 67_000),
    'I10': (60_000_000_000, 1_400_000),
}


def run_with(data, missing=frozenset()):
    real = S.subprocess.run
    S.subprocess.run = stub_run(data, missing)
    try:
        return S.hira_cost_facts('KEY')
    finally:
        S.subprocess.run = real


class TheHarvestReadsBothFrames(unittest.TestCase):
    def test_a_clean_run_returns_a_total_and_a_per_patient_line_per_code(self):
        facts = run_with(BASE_DATA)
        total_ids = {f['id'] for f in facts if f['id'].startswith('sickcost_')}
        pp_ids = {f['id'] for f in facts if f['id'].startswith('sickcostpp_')}
        self.assertEqual(total_ids, {f'sickcost_{c}' for c in BASE_DATA})
        self.assertEqual(pp_ids, {f'sickcostpp_{c}' for c in BASE_DATA})

    def test_total_values_are_won_formatted(self):
        facts = run_with(BASE_DATA)
        c50 = next(f for f in facts if f['id'] == 'sickcost_C50')
        self.assertEqual(c50['value_en'], '₩700.0bn')
        self.assertEqual(c50['value_ko'], '7,000억 원')

    def test_per_patient_values_are_cost_divided_by_patients(self):
        facts = run_with(BASE_DATA)
        c50 = next(f for f in facts if f['id'] == 'sickcostpp_C50')
        # 700,000,000,000 / 100,000 = 7,000,000
        self.assertEqual(c50['value_en'], '₩7m')

    def test_every_label_carries_its_own_korean(self):
        for f in run_with(BASE_DATA):
            self.assertTrue(f['label_ko'], f'no Korean: {f["label_en"]}')

    def test_the_newest_uddi_year_is_used_and_published(self):
        S.HEALTH_COST_Y['y'] = None
        run_with(BASE_DATA)
        self.assertEqual(S.HEALTH_COST_Y['y'], max(S.HIRA_COST_UDDI))

    def test_a_code_with_zero_patients_is_skipped_entirely(self):
        # A patients=0 row would divide by zero in the per-patient frame;
        # must not reach either frame, not just the second one.
        data = dict(BASE_DATA)
        data['C50'] = (700_000_000_000, 0)
        facts = run_with(data)
        ids = {f['id'] for f in facts}
        self.assertNotIn('sickcost_C50', ids)
        self.assertNotIn('sickcostpp_C50', ids)


class AMissingCodeIsSkippedNotAFailure(unittest.TestCase):
    """odcloud returns an empty `data` list for a code with no matching row —
    not an error. One or two missing codes should not sink the whole vein."""

    def test_two_missing_codes_still_yield_the_rest(self):
        facts = run_with(BASE_DATA, missing={'C16', 'I10'})
        ids = {f['id'] for f in facts}
        self.assertNotIn('sickcost_C16', ids)
        self.assertNotIn('sickcostpp_I10', ids)
        self.assertIn('sickcost_C50', ids)
        self.assertIn('sickcostpp_C50', ids)

    def test_fewer_than_three_codes_is_silence(self):
        facts = run_with(BASE_DATA, missing=set(BASE_DATA) - {'C50', 'K05'})
        self.assertEqual(facts, [])

    def test_an_unparseable_response_is_treated_as_missing(self):
        def run(cmd, **kw):
            return types.SimpleNamespace(stdout='not json', returncode=0)
        real = S.subprocess.run
        S.subprocess.run = run
        try:
            facts = S.hira_cost_facts('KEY')
        finally:
            S.subprocess.run = real
        self.assertEqual(facts, [])


class TheTotalsFrameDetectorsMirrorHiraFacts(unittest.TestCase):
    def test_a_near_equal_total_pair_is_flagged_a_dead_heat(self):
        data = dict(BASE_DATA)
        data['M17'] = (419_500_000_000, 630_000)
        data['M54'] = (417_600_000_000, 1_200_000)   # totals 0.45% apart
        facts = run_with(data)
        heat_ids = {f['id'] for f in facts if f['id'].startswith('sickcostheat_')}
        self.assertEqual(heat_ids, {'sickcostheat_M17', 'sickcostheat_M54'})
        for f in facts:
            if f['id'] in heat_ids:
                self.assertEqual(f['pair'], 'sickcost_heat')

    def test_no_near_equal_totals_means_no_heat_fact(self):
        facts = run_with(BASE_DATA)
        self.assertFalse(any(f['id'].startswith('sickcostheat_') for f in facts))

    def test_the_widest_total_spread_is_flagged_a_gap_pair(self):
        facts = run_with(BASE_DATA)   # C50 700bn vs I10 60bn, 11.7x
        gap_ids = {f['id'] for f in facts if f['id'].startswith('sickcostgap_')}
        self.assertEqual(gap_ids, {'sickcostgap_C50', 'sickcostgap_I10'})
        for f in facts:
            if f['id'] in gap_ids:
                self.assertEqual(f['pair'], 'sickcost_gap')


class ThePerPatientFrameHasItsOwnIndependentDetectors(unittest.TestCase):
    """The whole point of running frame() twice: a heat or gap pair in one
    frame must never leak into the other's pair id or fact set."""

    def test_a_near_equal_per_patient_pair_is_flagged_independently(self):
        data = dict(BASE_DATA)
        # totals stay far apart (no total heat), but per-patient values land
        # within 2%: 400bn/70,000=5,714,286 vs 300bn/52,507=5,714,290.
        data['C34'] = (400_000_000_000, 70_000)
        data['M17'] = (300_000_000_000, 52_507)
        facts = run_with(data)
        total_heat = {f['id'] for f in facts if f['id'].startswith('sickcostheat_')}
        pp_heat = {f['id'] for f in facts if f['id'].startswith('sickcostppheat_')}
        self.assertEqual(total_heat, set())
        self.assertEqual(pp_heat, {'sickcostppheat_C34', 'sickcostppheat_M17'})
        for f in facts:
            if f['id'] in pp_heat:
                self.assertEqual(f['pair'], 'sickcostpp_heat')

    def test_the_widest_per_patient_spread_is_flagged_a_gap_pair(self):
        facts = run_with(BASE_DATA)   # C50 ₩7.0m/patient vs I10 ₩42,857, 163x
        gap_ids = {f['id'] for f in facts if f['id'].startswith('sickcostppgap_')}
        self.assertEqual(gap_ids, {'sickcostppgap_C50', 'sickcostppgap_I10'})
        for f in facts:
            if f['id'] in gap_ids:
                self.assertEqual(f['pair'], 'sickcostpp_gap')

    def test_total_and_per_patient_fact_ids_never_collide(self):
        facts = run_with(BASE_DATA)
        ids = [f['id'] for f in facts]
        self.assertEqual(len(ids), len(set(ids)), 'duplicate fact id')


class TheRequestFiltersOnBothProvinceAndCode(unittest.TestCase):
    def test_every_request_names_seoul_and_the_curated_code(self):
        seen_urls = []
        def run(cmd, **kw):
            seen_urls.append(cmd[-1])
            code = CODE_RE.search(cmd[-1]).group(1)
            cost, patients = BASE_DATA[code]
            body = {'data': [{'요양급여비용총액(선별포함)': cost, '환자수': patients}]}
            return types.SimpleNamespace(stdout=json.dumps(body), returncode=0)
        real = S.subprocess.run
        S.subprocess.run = run
        try:
            S.hira_cost_facts('KEY')
        finally:
            S.subprocess.run = real
        # One request per curated code — the per-patient frame reuses the
        # SAME fetched rows, it does not re-query.
        self.assertEqual(len(seen_urls), len(S.HEALTH_COST_CONDS))
        for url in seen_urls:
            self.assertIn('%EC%84%9C%EC%9A%B8', url)   # 서울, urlencoded
            self.assertIn(S.HIRA_COST_UDDI[max(S.HIRA_COST_UDDI)], url)


if __name__ == '__main__':
    unittest.main(verbosity=1)
