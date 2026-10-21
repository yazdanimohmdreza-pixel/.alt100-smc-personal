"""Offline smoke and safety tests for the USD-M futures trend feed."""
import asyncio
from dataclasses import replace
import time
import unittest
from unittest.mock import patch

import httpx

from futures_live_feed import (
    FuturesFeedError, FuturesApprovedSignal, _get, _one,
    render_futures_signal, scan_futures, select_futures_universe,
)
from futures_data import FuturesSnapshot
from quality import MarketQuality
from strategy import Signal

NOW = 80 * 3_600_000 + 60_000


def make_row(symbol='ETHUSDT', perpetual=True, margin='USDT'):
    return {'symbol': symbol, 'status': 'TRADING', 'contractType': 'PERPETUAL' if perpetual else 'CURRENT_QUARTER',
            'quoteAsset': 'USDT', 'marginAsset': margin,
            'filters': [{'filterType': 'PRICE_FILTER', 'tickSize': '0.01'}]}


def kline(start, interval, price=100, volume=1000):
    return [start, str(price-.1), str(price+.2), str(price-.2), str(price), str(volume),
            start + interval - 1, '100000', 100, '600', '60000', 0]


def raw_series(interval):
    return [kline(NOW - 60_000 - (140-i)*interval, interval) for i in range(140)]


class FuturesFeedTests(unittest.TestCase):
    def test_select_only_usdt_perpetual_overlap_and_futures_turnover(self):
        info = {'symbols': [make_row('ETHUSDT'), make_row('XRPUSDT', False),
                            make_row('SOLUSDT', True, 'BUSD'), make_row('BTCUSDT')]}
        tickers = [{'symbol': 'ETHUSDT', 'quoteVolume': '20000000'},
                   {'symbol': 'XRPUSDT', 'quoteVolume': '30000000'},
                   {'symbol': 'SOLUSDT', 'quoteVolume': '40000000'},
                   {'symbol': 'BTCUSDT', 'quoteVolume': '90000000'}]
        got = select_futures_universe(info, tickers, [('ETHUSDT', 2e7),('SOLUSDT', 2e7)], 100)
        self.assertEqual([s for s, _, _ in got], ['ETHUSDT'])

    def test_not_use_spot_volume_to_approve_futures(self):
        got = select_futures_universe({'symbols':[make_row()]},
                                     [{'symbol':'ETHUSDT','quoteVolume':'100000'}],
                                     [('ETHUSDT',1e10)])
        self.assertEqual(got, [])

    def test_unapproved_endpoint_raises(self):
        async def run():
            async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200))) as c:
                await _get(c, '/fapi/v1/order')
        with self.assertRaises(ValueError):
            asyncio.run(run())

    def test_all_requests_public_get_no_keys(self):
        paths=[]
        def handler(req):
            paths.append((req.method,req.url.path,dict(req.headers)))
            if req.url.path.endswith('exchangeInfo'):
                return httpx.Response(200,json={'symbols':[make_row()]})
            if req.url.path.endswith('24hr'):
                return httpx.Response(200,json=[{'symbol':'ETHUSDT','quoteVolume':'20000000'}])
            if req.url.path.endswith('klines'):
                d={'1h':3600000,'15m':900000,'5m':300000}[req.url.params['interval']]
                return httpx.Response(200,json=raw_series(d))
            raise AssertionError(req.url.path)
        async def run():
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
                return await scan_futures(c,[('ETHUSDT',2e7)],NOW)
        s=asyncio.run(run())
        self.assertEqual(s.universe,1)
        self.assertEqual(s.analyzed,1)
        self.assertEqual(s.raw_setups,0)
        self.assertEqual(s.errors,0)
        self.assertEqual(len(paths),5)
        self.assertTrue(all(method=='GET' and path.startswith('/fapi/v1/') for method,path,_ in paths))
        self.assertTrue(all('x-mbx-apikey' not in headers for _,_,headers in paths))

    def test_no_futures_contract_fails_closed(self):
        def handler(req):
            return httpx.Response(200,json={'symbols': []} if req.url.path.endswith('exchangeInfo') else [])
        async def run():
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
                return await scan_futures(c,[('ETHUSDT',2e7)],NOW)
        with self.assertRaises(FuturesFeedError):
            asyncio.run(run())

    def test_old_candles_rejected_no_signal(self):
        def handler(req):
            interval={'1h':3600000,'15m':900000,'5m':300000}[req.url.params['interval']]
            return httpx.Response(200,json=[kline(i*interval,interval) for i in range(140)])
        async def run():
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
                return await _one(c,asyncio.Semaphore(4),'ETHUSDT',20000000,NOW+90*24*3600000,75)
        sym, result, reason=asyncio.run(run())
        self.assertIsNone(result)
        self.assertEqual(reason,'stale_futures_candles')

    def test_broken_candle_data_is_not_a_market_signal(self):
        def handler(req):
            return httpx.Response(200,json=[[1,2]]*140)
        async def run():
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
                return await _one(c,asyncio.Semaphore(4),'ETHUSDT',20000000,NOW,75)
        self.assertEqual(asyncio.run(run())[2],'empty_closed_candles')

    def test_render_futures_targets_direction_and_times(self):
        s=Signal('ETHUSDT','BUY',1_700_000_000_000,100,99,103,104,88,
                 '1h + 15m + 5m',1.5,1,('روند EMA20/EMA50','پولبک ۱۵دقیقه','RSI: 60','شکست ۵دقیقه'),
                 'TREND_PULLBACK_V1','breakout','TREND_PULLBACK_V1:1:1')
        snap=FuturesSnapshot('ETHUSDT',1700000060000,100,99.9,.0001,1700003600000,
                             15000,99.99,100.01,.02)
        app=FuturesApprovedSignal(s,snap,MarketQuality(99.99,100.01,.02,.0,.32),'0.01')
        msg=render_futures_signal(app,issued_ms=1_700_000_060_000)
        for needle in ('CoinWhisper','LONG','TP1','TP2','TP3','103.00','104.00','ENTRY',
                       'STOP','Funding','OI','UTC','دلایل ورود','شرایط ابطال'):
            self.assertIn(needle,msg)
        bearish=replace(s,direction='SELL_WARNING',stop=101,tp1=97,tp2=96)
        self.assertIn('SHORT',render_futures_signal(replace(app,signal=bearish),issued_ms=1_700_000_060_000))

    def test_html_escaped_reasons(self):
        s=Signal('ETHUSDT','BUY',1700000000000,100,99,103,104,87,'1h + 15m + 5m',1.5,1,
                 ('<script>x</script>','EMA','RSI','vol'),'TREND_PULLBACK_V1','breakout')
        snap=FuturesSnapshot('ETHUSDT',1700000000000,100,100,.0001,1700003600000,50000,99.99,100.01,.02)
        x=render_futures_signal(FuturesApprovedSignal(s,snap,MarketQuality(99,100,1,0,1),'0.01'),
                                issued_ms=1700000000000)
        self.assertIn('&lt;script&gt;',x)
        self.assertNotIn('<script>',x)

if __name__ == '__main__':
    unittest.main()


class VerifiedQuoteTests(unittest.TestCase):
    def test_fresh_futures_snapshot_matches_all_three_symbols(self):
        from futures_live_feed import fetch_verified_snapshot
        payloads = {
            '/fapi/v1/premiumIndex': {'symbol':'ETHUSDT','time':NOW-2_000,
                'markPrice':'2500.5','indexPrice':'2499.5','lastFundingRate':'-0.0001',
                'nextFundingTime':NOW+4000000},
            '/fapi/v1/openInterest': {'symbol':'ETHUSDT','time':NOW-25_000,
                                     'openInterest':'1234567'},
            '/fapi/v1/ticker/bookTicker': {'symbol':'ETHUSDT','time':NOW-1_000,
                                          'bidPrice':'2500.0','askPrice':'2500.1'},
        }
        def handler(req):
            return httpx.Response(200,json=payloads[req.url.path])
        async def run():
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
                return await fetch_verified_snapshot(c,'ETHUSDT',now_ms=NOW)
        snap=asyncio.run(run())
        self.assertEqual(snap.mark_price,2500.5)
        self.assertEqual(snap.open_interest,1234567.0)
        self.assertEqual(snap.funding_rate,-.0001)
        self.assertGreater(snap.spread_pct,0)

    def test_stale_mark_or_book_rejected(self):
        from futures_live_feed import fetch_verified_snapshot
        for bad_path in ['/fapi/v1/premiumIndex','/fapi/v1/ticker/bookTicker']:
            payloads={
                '/fapi/v1/premiumIndex': {'symbol':'ETHUSDT','time':NOW-1000,'markPrice':'2500',
                    'indexPrice':'2500','lastFundingRate':'0','nextFundingTime':NOW+1},
                '/fapi/v1/openInterest': {'symbol':'ETHUSDT','time':NOW-1000,'openInterest':'1234'},
                '/fapi/v1/ticker/bookTicker': {'symbol':'ETHUSDT','time':NOW-1000,'bidPrice':'2500',
                                              'askPrice':'2500.01'}}
            payloads[bad_path]['time']=NOW-50000
            def handler(req):
                return httpx.Response(200,json=payloads[req.url.path])
            async def run():
                async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
                    return await fetch_verified_snapshot(c,'ETHUSDT',now_ms=NOW)
            with self.assertRaises(FuturesFeedError):
                asyncio.run(run())

    def test_mismatching_quote_symbol_rejected(self):
        from futures_live_feed import fetch_verified_snapshot
        payloads={
            '/fapi/v1/premiumIndex': {'symbol':'ETHUSDT','time':NOW-1000,'markPrice':'2500',
                    'indexPrice':'2500','lastFundingRate':'0','nextFundingTime':NOW+1},
            '/fapi/v1/openInterest': {'symbol':'BTCUSDT','time':NOW-1000,'openInterest':'1234'},
            '/fapi/v1/ticker/bookTicker': {'symbol':'ETHUSDT','time':NOW-1000,'bidPrice':'2500',
                                          'askPrice':'2500.01'}}
        def handler(req):
            return httpx.Response(200,json=payloads[req.url.path])
        async def run():
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
                return await fetch_verified_snapshot(c,'ETHUSDT',now_ms=NOW)
        with self.assertRaises(FuturesFeedError):
            asyncio.run(run())
