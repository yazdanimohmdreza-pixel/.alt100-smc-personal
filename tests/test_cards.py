import unittest
from bot import format_signal, delivery_key
from quality import MarketQuality
from strategy import Signal

class CardTests(unittest.TestCase):
    def signal(self, direction='BUY'):
        return Signal('ETHUSDT', direction, 1700000000000, 100, 99.1, 101.9, 103.5,
                      87, '1h + 15m + 5m', 1.5, .9, ('جمع‌آوری نقدینگی', 'OB <b>fake</b>'),
                      'SWEEP', '5m trigger', 'SWEEP:1111:1', 99.8, 100.0, 99.9)

    def test_compact_buy_card_escaping_and_labels(self):
        msg = format_signal(self.signal(), MarketQuality(100,100.02,.02,.01,.32),'0.01')
        self.assertIn('سفارش شرطی روی پولبک', msg)
        self.assertIn('انقضا', msg)
        self.assertIn('اسپرد', msg)
        self.assertIn('99.90', msg)
        self.assertNotIn('<b>fake</b>', msg)
        self.assertIn('&lt;b&gt;fake&lt;/b&gt;', msg)
        self.assertNotIn('درصد برد', msg)

    def test_sell_warning_does_not_advise_short(self):
        msg = format_signal(self.signal('SELL_WARNING'))
        self.assertIn('هشدار ضعف اسپات', msg)
        self.assertIn('نه', msg)
        self.assertNotIn('سفارش شرطی روی پولبک', msg)

    def test_same_zone_multiple_candles_have_same_duplicate_key(self):
        a = self.signal()
        from dataclasses import replace
        b = replace(a, candle_close=a.candle_close + 300000)
        self.assertEqual(delivery_key(a), delivery_key(b))

if __name__ == '__main__':
    unittest.main()
