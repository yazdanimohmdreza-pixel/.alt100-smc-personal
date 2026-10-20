import unittest
import inspect
import bot
from strategy import Signal

class PersonalOnlyTests(unittest.TestCase):
    def test_no_subscriber_dependency_or_payment(self):
        src = inspect.getsource(bot)
        for forbidden in ('SUBSCRIBERS_JSON', 'active_chat_ids', 'paid_access', 'payment_gateway'):
            self.assertNotIn(forbidden, src)

    def test_delivery_id_is_setup_key_not_chat_id(self):
        sig = Signal('ETHUSDT', 'BUY', 1700000000000, 100, 99, 101, 102,
                    82, '1h + 15m + 5m', 1.1, 1.0, ('rule',), 'SWEEP',
                    'trigger', 'SWEEP:1700000000:1')
        key = bot.delivery_key(sig)
        self.assertIn('ETHUSDT', key)
        self.assertNotIn('123456789', key)
        self.assertEqual(key, bot.delivery_key(sig))

if __name__ == '__main__':
    unittest.main()
