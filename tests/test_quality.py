import unittest
from dataclasses import replace
from quality import evaluate_quote, format_tick_price, tick_size
from strategy import Signal


class QuoteTests(unittest.TestCase):
    def setUp(self):
        self.sig = Signal('ETHUSDT', 'BUY', 1700000000000, 100., 99., 102., 103., 84,
                          '1h + 15m + 5m', 1.3, 1., ('test',), 'SWEEP', 'micro',
                          'SWEEP:42:1', 99.85, 100.1, 99.95)
        self.now = self.sig.candle_close + 10000

    def test_book_quote_gate_and_fees(self):
        result = evaluate_quote(self.sig, {'bidPrice': '100.01', 'askPrice': '100.03'}, self.now)
        self.assertIsNotNone(result)
        self.assertAlmostEqual(result.spread_pct, .019996, delta=.001)
        self.assertGreater(result.assumed_roundtrip_cost_pct, .30)

    def test_stale_high_spread_low_risk_or_gapped_quotes_rejected(self):
        self.assertIsNone(evaluate_quote(self.sig, {'bidPrice': '99', 'askPrice': '101'}, self.now))
        self.assertIsNone(evaluate_quote(self.sig, {'bidPrice': '100', 'askPrice': '100.02'}, self.now + 600000))
        self.assertIsNone(evaluate_quote(replace(self.sig, risk_pct=.4), {'bidPrice':'100', 'askPrice':'100.02'}, self.now))
        self.assertIsNone(evaluate_quote(self.sig, {'bidPrice': '0', 'askPrice': '100'}, self.now))
        self.assertIsNone(evaluate_quote(self.sig, {'bidPrice': '103', 'askPrice': '103.01'}, self.now))

    def test_precise_exchange_tick_rounding(self):
        self.assertEqual(tick_size({'filters':[{'filterType':'PRICE_FILTER','tickSize':'0.005'}]}), '0.005')
        self.assertEqual(format_tick_price(1.2349, '0.005'), '1.230')
        self.assertEqual(format_tick_price(1.2349, '0.005', up=True), '1.235')
        self.assertEqual(format_tick_price(.000000982, '0.00000001'), '0.00000098')
        with self.assertRaises(ValueError):
            format_tick_price(float('nan'), '.01')


if __name__ == '__main__':
    unittest.main()
