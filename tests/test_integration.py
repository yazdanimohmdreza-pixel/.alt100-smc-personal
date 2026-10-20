import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, AsyncMock

import bot


class MockScanTests(unittest.TestCase):
    def test_100_coin_offline_scan_does_not_invent_signals(self):
        now_ms = 1700000000000
        symbols = [f'COIN{i:03d}USDT' for i in range(100)]
        calls = {'klines': 0}
        info = {'symbols': [
            {'symbol': sym, 'quoteAsset': 'USDT', 'baseAsset': sym[:-4],
             'status': 'TRADING', 'isSpotTradingAllowed': True,
             'filters': [{'filterType': 'PRICE_FILTER', 'tickSize': '0.01'}]}
            for sym in symbols]}
        tickers = [{'symbol': sym, 'quoteVolume': '12000000'} for sym in symbols]
        books = [{'symbol': sym, 'bidPrice': '99.99', 'askPrice': '100.01'} for sym in symbols]

        async def fake_get(client, path, params=None):
            if path.endswith('exchangeInfo'):
                return info
            if path.endswith('24hr'):
                return tickers
            if path.endswith('bookTicker'):
                return books
            calls['klines'] += 1
            interval = params['interval']
            duration = {'5m': 300000, '15m': 900000, '1h': 3600000}[interval]
            return [[now_ms-(140-i)*duration, '100', '100.50', '99.50', '100',
                     '2000', now_ms-(139-i)*duration-1] for i in range(140)]

        with tempfile.TemporaryDirectory() as d:
            send = AsyncMock(return_value=True)
            with patch.object(bot, 'api_get', fake_get), \
                 patch.object(bot, 'telegram_send', send), \
                 patch.object(bot, 'TOKEN', 'not-real-token'), \
                 patch.object(bot, 'CHAT_ID', '123456'), \
                 patch.object(bot, 'STATE_FILE', Path(d) / 'state.json'), \
                 patch.object(bot.time, 'time', return_value=now_ms/1000), \
                 patch.dict(bot.os.environ, {'GITHUB_STEP_SUMMARY': ''}):
                asyncio.run(bot.main())
            self.assertEqual(calls['klines'], 300)
            self.assertEqual(send.await_count, 1)  # activation, no invented trade
            self.assertTrue((Path(d) / 'state.json').exists())

if __name__ == '__main__':
    unittest.main()
