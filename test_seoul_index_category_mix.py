"""Tests for the category-mixing guard and cross-pair label hints, added
30 August 2026 after a real post mixed 'price' and 'avgbill' with no
sanctioned reason: SELECT_PROMPT tells the selector every vein is its own
post, with exactly two named exceptions (the LIVE_CATS+SCOPED_CATS "grouped"
layout, and a genuine CROSS_PAIRS coincidence), but nothing before this
checked the model actually used one rather than just mixing veins because
nothing stopped it. The card that slipped through:

    Conditions: Convenience stores          ₩7,398
    A supermarket in Yongsan-gu             ₩5,980
    A traditional market in Gangbuk-gu      ₩5,630
    Internet cafés                          ₩5,441

— a genuine CROSS_PAIR (the model's own internal `note` proves it knew), but
with no code check confirming that, and no way for a reader to tell two of
those four figures are prices and two are average bills.

No network, no model call, no posting: S.CHECK_LABELS is switched off, as in
every other compose()-exercising test file here.
"""
import sys
import unittest
from pathlib import Path

sys.argv = ['test']
sys.path.insert(0, str(Path(__file__).resolve().parent))
import seoul_index_post as S

S.CHECK_LABELS = False


def picks_for(facts):
    return [{'id': f['id'], 'label_en': f['label_en'],
             'label_ko': f.get('label_ko') or f['label_en'], 'emoji': ''}
            for f in facts]


def sel_for(facts, opener_en='Seoul by the numbers', opener_ko='숫자로 보는 서울'):
    return {'opener_en': opener_en, 'opener_ko': opener_ko,
            'opener_emoji': '', 'picks': picks_for(facts)}


class ValidateCardCategories(unittest.TestCase):
    def test_a_single_category_always_passes(self):
        facts = [S.fact('a', 'price', 'A', '₩1', '₩1'),
                 S.fact('b', 'price', 'B', '₩2', '₩2'),
                 S.fact('c', 'price', 'C', '₩3', '₩3')]
        self.assertFalse(
            S._validate_card_categories({'price'}, picks_for(facts), facts))

    def test_two_categories_with_no_link_at_all_is_refused(self):
        # Magnitudes far apart: never a CROSS_PAIRS candidate, and neither
        # category is in LIVE_CATS, so neither sanctioned mechanism applies.
        facts = [S.fact('p1', 'price', 'A', '₩100', '₩100', num=100, unit='won'),
                 S.fact('p2', 'price', 'B', '₩200', '₩200', num=200, unit='won'),
                 S.fact('a1', 'avgbill', 'C', '₩100000', '₩100000',
                        num=100000, unit='won')]
        with self.assertRaises(RuntimeError):
            S._validate_card_categories({'price', 'avgbill'}, picks_for(facts), facts)

    def test_a_genuine_cross_pair_is_allowed_and_reported_as_one(self):
        # Within CROSS_HEAT_MAX (0.15) of each other, real 'price'/'avgbill'
        # figures from the actual 30 Aug 2026 card.
        facts = [S.fact('p1', 'price', 'A', '₩5,630', '₩5,630',
                        num=5630, unit='won'),
                 S.fact('a1', 'avgbill', 'B', '₩5,441', '₩5,441',
                        num=5441, unit='won'),
                 S.fact('a2', 'avgbill', 'C', '₩7,398', '₩7,398',
                        num=7398, unit='won')]
        self.assertTrue(
            S._validate_card_categories({'price', 'avgbill'}, picks_for(facts), facts))

    def test_a_third_unlinked_category_is_still_refused(self):
        # A real cross-pair between price and avgbill does not license a
        # bare third vein riding along with no pair connecting it to anything.
        facts = [S.fact('p1', 'price', 'A', '₩5,630', '₩5,630',
                        num=5630, unit='won'),
                 S.fact('a1', 'avgbill', 'B', '₩5,441', '₩5,441',
                        num=5441, unit='won'),
                 S.fact('w1', 'water', 'C', '1,000m³', '1,000m³')]
        with self.assertRaises(RuntimeError):
            S._validate_card_categories(
                {'price', 'avgbill', 'water'}, picks_for(facts), facts)

    def test_live_plus_scoped_grouping_needs_no_cross_pair(self):
        # 'tourism' carries no num/unit, so it can never be CROSS_PAIRS-linked
        # — the live+scoped mechanism is the only thing that can sanction this.
        facts = [S.fact('crowd1', 'crowd', 'Estimated crowd, Hongdae',
                        '1,000', '1,000', num=1000, unit='people'),
                 S.fact('tour1', 'tourism', 'Visitors to N Seoul Tower',
                        '2,000,000', '2,000,000'),
                 S.fact('tour2', 'tourism', 'Visitors to Lotte World',
                        '1,800,000', '1,800,000')]
        self.assertFalse(
            S._validate_card_categories({'crowd', 'tourism'}, picks_for(facts), facts))

    def test_a_live_category_alone_with_an_unscoped_category_is_refused(self):
        # 'crowd' (live) beside 'weather' (in neither LIVE_CATS nor
        # SCOPED_CATS) matches neither sanctioned mechanism. ('water' would
        # NOT work here — it's deliberately one of the SCOPED_CATS.)
        facts = [S.fact('crowd1', 'crowd', 'Estimated crowd, Hongdae',
                        '1,000', '1,000', num=1000, unit='people'),
                 S.fact('w1', 'weather', "Seoul's high yesterday", '28°C', '28°C'),
                 S.fact('w2', 'weather', "Seoul's low yesterday", '22°C', '22°C')]
        with self.assertRaises(RuntimeError):
            S._validate_card_categories({'crowd', 'weather'}, picks_for(facts), facts)


class CrossPairHints(unittest.TestCase):
    def tearDown(self):
        S.PRICE_LABEL['en'] = S.PRICE_LABEL['ko'] = None
        S.INFANT_PERIOD['en'] = S.INFANT_PERIOD['ko'] = None

    def test_the_actual_failing_card_now_names_both_metrics(self):
        S.PRICE_LABEL['en'], S.PRICE_LABEL['ko'] = 'a napa cabbage', '배추 1포기'
        facts = [
            S.fact('p_gangbuk', 'price', 'A traditional market in Gangbuk-gu',
                   '₩5,630', '₩5,630', num=5630, unit='won'),
            S.fact('p_yongsan', 'price', 'A supermarket in Yongsan-gu',
                   '₩5,980', '₩5,980', num=5980, unit='won'),
            S.fact('a_pcbang', 'avgbill', 'Internet cafés', '₩5,441', '₩5,441',
                   num=5441, unit='won'),
            S.fact('a_cvs', 'avgbill', 'Convenience stores', '₩7,398', '₩7,398',
                   num=7398, unit='won'),
        ]
        c = S.compose(sel_for(facts), facts)
        by_id = {l['label_en']: l for l in c['lines']}
        self.assertIn(
            'Price of a napa cabbage, a traditional market in Gangbuk-gu', by_id)
        self.assertIn(
            'Price of a napa cabbage, a supermarket in Yongsan-gu', by_id)
        self.assertIn('Average bill, Internet cafés', by_id)
        self.assertIn('Average bill, Convenience stores', by_id)
        # The coincidence itself is still never stated anywhere on the card.
        whole_card = ' '.join(l['label_en'] for l in c['lines']) + c['opener']['en']
        self.assertNotIn('close', whole_card.lower())
        self.assertNotIn('almost', whole_card.lower())
        self.assertEqual(c['opener']['en'], 'Seoul by the numbers')

    def test_korean_labels_get_the_same_treatment(self):
        S.PRICE_LABEL['en'], S.PRICE_LABEL['ko'] = 'a napa cabbage', '배추 1포기'
        facts = [
            S.fact('p1', 'price', 'A traditional market in Gangbuk-gu',
                   '₩5,630', '₩5,630', num=5630, unit='won',
                   label_ko='강북구의 전통시장'),
            S.fact('a1', 'avgbill', 'Internet cafés', '₩5,441', '₩5,441',
                   num=5441, unit='won', label_ko='PC방'),
            S.fact('a2', 'avgbill', 'Convenience stores', '₩7,398', '₩7,398',
                   num=7398, unit='won', label_ko='편의점'),
        ]
        c = S.compose(sel_for(facts), facts)
        by_id = {l['label_ko']: l for l in c['lines']}
        self.assertIn('배추 1포기 가격, 강북구의 전통시장', by_id)
        self.assertIn('평균 결제액, PC방', by_id)

    def test_a_single_vein_avgbill_card_is_left_exactly_as_is(self):
        facts = [
            S.fact('a1', 'avgbill', 'Internet cafés', '₩5,441', '₩5,441'),
            S.fact('a2', 'avgbill', 'Convenience stores', '₩7,398', '₩7,398'),
            S.fact('a3', 'avgbill', 'Motels', '₩60,553', '₩60,553'),
        ]
        c = S.compose(sel_for(facts, 'Average bill in Seoul', '서울의 평균 결제액'),
                     facts)
        labels = [l['label_en'] for l in c['lines']]
        self.assertIn('Internet cafés', labels)
        self.assertNotIn('Average bill, Internet cafés', labels)

    def test_price_ranked_lines_are_not_double_hinted(self):
        # "Cheapest"/"Dearest" already say this is about cost; the item name
        # still needs adding, since neither word says WHICH item.
        S.PRICE_LABEL['en'], S.PRICE_LABEL['ko'] = 'a napa cabbage', '배추 1포기'
        facts = [
            S.fact('p1', 'price', 'Cheapest, a traditional market (Dongjak-gu)',
                   '₩5,000', '₩5,000', num=5000, unit='won'),
            S.fact('a1', 'avgbill', 'Internet cafés', '₩5,100', '₩5,100',
                   num=5100, unit='won'),
            S.fact('a2', 'avgbill', 'Convenience stores', '₩5,200', '₩5,200',
                   num=5200, unit='won'),
        ]
        c = S.compose(sel_for(facts), facts)
        price_line = next(l for l in c['lines'] if l['cat'] == 'price')
        self.assertEqual(price_line['label_en'],
                         'Cheapest, a traditional market (Dongjak-gu)')

    def test_infant_gets_the_run_specific_age_band_not_a_generic_word(self):
        # infant + library, NOT infant + crowd: crowd is a LIVE_CATS member,
        # so infant+crowd takes the *other* sanctioned path (the live+scoped
        # "grouped" layout, already covered by ScopedVeinHeadsItsOwnGroup in
        # test_seoul_index_selection.py) and correctly gets no hint — its
        # group subhead already does the disambiguating. library is scoped
        # but not live, so this pairing can only be sanctioned as a genuine
        # CROSS_PAIR, which is the path this test means to exercise.
        S.INFANT_PERIOD['en'], S.INFANT_PERIOD['ko'] = 'Children under 6', '영유아 인구'
        facts = [
            S.fact('infant_2016', 'infant', '2016', '75,536', '75,536',
                   num=75536, unit='people'),
            S.fact('infant_2025', 'infant', '2025', '41,600', '41,600',
                   num=41600, unit='people'),
            S.fact('library_60', 'library', '60s', '76,000', '76,000',
                   num=76000, unit='people'),
        ]
        c = S.compose(sel_for(facts), facts)
        infant_labels = {l['label_en'] for l in c['lines'] if l['cat'] == 'infant'}
        self.assertEqual(infant_labels,
                         {'Children under 6, 2016', 'Children under 6, 2025'})

    def test_categories_that_already_name_their_metric_are_untouched(self):
        facts = [
            S.fact('prop1', 'property',
                   'Most paid for an apartment (Yongsan-gu)',
                   '₩5,000,000,000', '₩5,000,000,000', num=5000000000, unit='won'),
            S.fact('a1', 'avgbill', 'Internet cafés', '₩5,441,000,000',
                   '₩5,441,000,000', num=5441000000, unit='won'),
            S.fact('a2', 'avgbill', 'Convenience stores', '₩5,600,000,000',
                   '₩5,600,000,000', num=5600000000, unit='won'),
        ]
        c = S.compose(sel_for(facts), facts)
        prop_line = next(l for l in c['lines'] if l['cat'] == 'property')
        self.assertEqual(prop_line['label_en'],
                         'Most paid for an apartment (Yongsan-gu)')


if __name__ == '__main__':
    unittest.main()
