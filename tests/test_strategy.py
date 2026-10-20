import math
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from strategy import Candle, closed_candles, hourly_bias, make_signal, pivots, trigger_5m, zones_15m


def candle(i, mid, width=.25, body=.03):
    o, close = mid - body/2, mid + body/2
    return Candle(i*300000, (i+1)*300000-1, o, mid + width, mid - width, close, 1200)


class StrategyTests(unittest.TestCase):
    def test_closed_candles_exclude_unfinished_and_bad_prices(self):
        def raw(st, close, high=100):
            return [st, '99', str(high), '98', '99.5', '123', close]
        got = closed_candles([raw(0, 59999), raw(60000, 119999), raw(0, 49999, 97)], 70000)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0].close_time, 59999)

    def test_confirmed_pivots_never_use_latest_candles(self):
        mids = [4, 5, 7, 8, 7, 6, 8, 9, 8, 7, 10]
        cs = [candle(i, x, .05) for i, x in enumerate(mids)]
        points = pivots(cs, 'high', wing=2)
        self.assertTrue(all(index < len(cs) - 2 for index, _ in points))

    def test_hourly_uptrend_and_no_forecast_when_flat(self):
        h = [candle(i, 97.5 + .065*i + .75*math.sin(i*.8), .15) for i in range(80)]
        self.assertEqual(hourly_bias(h)[0], 1)
        flat = [candle(i, 100.0, .1) for i in range(80)]
        self.assertIsNone(hourly_bias(flat))

    def test_5m_trigger_requires_direction_and_closed_candle(self):
        c = [candle(i, 100, .2) for i in range(55)]
        self.assertIsNone(trigger_5m(c, 1, .1))
        c[-1] = Candle(c[-1].open_time, c[-1].close_time, 100, 100.6, 99.95, 100.55, 1000)
        self.assertIsNotNone(trigger_5m(c, 1, .2))
        self.assertIsNone(trigger_5m(c, -1, .2))

    def test_no_zone_is_no_signal(self):
        h = [candle(i, 97.5 + .065*i + .75*math.sin(i*.8), .15) for i in range(80)]
        f = [candle(i, 100 + .03 * math.sin(i*.3), .08) for i in range(80)]
        m = [candle(i, 100 + .03 * math.sin(i*.3), .08) for i in range(65)]
        self.assertIsNone(make_signal('TESTUSDT', f, h, m, 2_000_000, 75))

    def test_end_to_end_liquidity_sweep_scalp(self):
        h = [candle(i, 97.5+.065*i+.75*math.sin(i*.8), .15) for i in range(80)]
        f = [candle(i, 100+.05*math.sin(i*.5), .20) for i in range(80)]
        floor = min(x.low for x in f[68:78])
        f[78] = Candle(f[78].open_time, f[78].close_time, 99.72, 100.08, floor-.85, 99.94, 1500)
        f[79] = Candle(f[79].open_time, f[79].close_time, 99.90, 100.10, 99.82, 100.04, 1400)
        m = [candle(i, 100+.05*math.sin(i*.8), .15) for i in range(65)]
        m[-4] = Candle(m[-4].open_time, m[-4].close_time, 99.95, 100.04, 99.6, 99.91, 1000)
        m[-3] = Candle(m[-3].open_time, m[-3].close_time, 99.81, 99.98, 99.62, 99.80, 1300)
        m[-2] = Candle(m[-2].open_time, m[-2].close_time, 99.79, 100.02, 99.7, 99.78, 1400)
        m[-1] = Candle(m[-1].open_time, m[-1].close_time, 99.80, 100.20, 99.79, 100.13, 2400)
        # Align the synthetic candles to real exchange timelines: 1h, 15m, 5m.
        # Latest closed bars are synchronized at t=80h.
        h = [Candle(i*3600000, (i+1)*3600000-1, c.open, c.high, c.low, c.close, c.volume)
             for i, c in enumerate(h)]
        f = [Candle(60*3600000+i*900000, 60*3600000+(i+1)*900000-1,
                    c.open, c.high, c.low, c.close, c.volume) for i, c in enumerate(f)]
        m = [Candle(80*3600000-(65-i)*300000, 80*3600000-(64-i)*300000-1,
                    c.open, c.high, c.low, c.close, c.volume) for i, c in enumerate(m)]
        result = make_signal('TESTUSDT', f, h, m, 5_000_000, 75)
        self.assertIsNotNone(result)
        self.assertEqual(result.direction, 'BUY')
        self.assertEqual(result.setup, 'SWEEP')
        self.assertLess(result.stop, result.entry)
        self.assertGreater(result.tp1, result.limit_entry)
        self.assertGreater(result.tp2, result.tp1)
        self.assertEqual(result.timeframe, '1h + 15m + 5m')
        # Raise the threshold above the earned score: the filter must reject it.
        self.assertIsNone(make_signal('TESTUSDT', f, h, m, 5_000_000, 95))

    def test_liquidity_sweep_definition_and_invalidation(self):
        f = [candle(i, 100 + .05 * math.sin(i*.5), .20) for i in range(78)]
        level = min(x.low for x in f[-10:])
        f.append(Candle(78, 79, 99.75, 100.05, level-.25, 99.95, 1200))
        self.assertTrue(any(z.kind=='SWEEP' for z in zones_15m(f, 1, .22)))


if __name__ == '__main__':
    unittest.main()

class AdditionalZoneTests(unittest.TestCase):
    def test_swept_zone_must_not_survive_confirmed_close_through_extreme(self):
        from strategy import Zone, _valid_zone
        a = [Candle(i*900000, (i+1)*900000-1, 100, 101, 99, 100, 1000)
             for i in range(9)]
        z = Zone('SWEEP', 98.8, 99.2, 7, 'test')
        self.assertTrue(_valid_zone(z, a, 1, len(a)))
        a[8] = Candle(a[8].open_time, a[8].close_time, 99.0, 99.3, 98.5, 98.7, 1000)
        self.assertFalse(_valid_zone(z, a, 1, len(a)))

    def test_fully_mitigated_bullish_fvg_is_not_tradable(self):
        from strategy import Zone, _valid_zone
        a = [Candle(i*900000, (i+1)*900000-1, 101, 102, 100.5, 101, 1000)
             for i in range(8)]
        z = Zone('FVG', 100, 101, 5, 'test')
        self.assertTrue(_valid_zone(z, a, 1, 6))
        a[7] = Candle(a[7].open_time, a[7].close_time, 101, 102, 99.9, 101, 1000)
        self.assertFalse(_valid_zone(z, a, 1, len(a)))
