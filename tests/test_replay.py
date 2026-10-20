import unittest
from replay import Outcome, summarize, replay
from strategy import Candle

class ReplayTests(unittest.TestCase):
    def test_empty_results_do_not_claim_performance(self):
        summary = summarize([])
        self.assertEqual(summary['alerts'], 0)
        self.assertIsNone(summary['win_rate_on_fills'])
        self.assertIsNone(summary['profit_factor'])

    def test_metrics_use_filled_trades_only(self):
        outcomes = [Outcome('X', 1, False, None, 'unfilled'),
                    Outcome('X', 2, True, 1.2, 'tp'),
                    Outcome('X', 3, True, -.8, 'stop')]
        summary = summarize(outcomes)
        self.assertEqual(summary['alerts'], 3)
        self.assertEqual(summary['fills'], 2)
        self.assertEqual(summary['win_rate_on_fills'], .5)
        self.assertEqual(summary['expectancy_r'], .2)
        self.assertEqual(summary['max_drawdown_r'], .8)

    def test_insufficient_history_yields_no_trades(self):
        data = [Candle(i*300000, (i+1)*300000-1, 99,100,98,99,100)
                for i in range(50)]
        self.assertEqual(replay('ETHUSDT', data, data, data), [])

if __name__ == '__main__':
    unittest.main()
