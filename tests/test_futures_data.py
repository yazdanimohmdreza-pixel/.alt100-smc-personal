"""Offline regression tests for read-only Binance USD-M futures snapshots.

No Binance credentials, internet connection, trading orders, or live prices required.
"""
import asyncio
import unittest

import httpx

from futures_data import FuturesDataError, fetch_futures_snapshot


NOW = 1_800_000_000_000


def example_payloads():
    return {
        '/fapi/v1/premiumIndex': {
            'symbol': 'ETHUSDT', 'markPrice': '2500.5', 'indexPrice': '2500.0',
            'lastFundingRate': '-0.000125', 'nextFundingTime': NOW + 3_600_000,
        },
        '/fapi/v1/openInterest': {
            'symbol': 'ETHUSDT', 'openInterest': '1500000.25', 'time': NOW - 10_000,
        },
        '/fapi/v1/ticker/bookTicker': {
            'symbol': 'ETHUSDT', 'bidPrice': '2500.0', 'askPrice': '2500.5',
        },
    }


class FuturesSnapshotTests(unittest.TestCase):
    def run_case(self, *, mutate=None, status=200):
        payloads = example_payloads()
        if mutate:
            mutate(payloads)
        requests = []

        def handler(request):
            requests.append(request)
            if status != 200:
                return httpx.Response(status, json={'error': 'unavailable'})
            return httpx.Response(200, json=payloads[request.url.path])

        async def run():
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                return await fetch_futures_snapshot(client, 'ETHUSDT', now_ms=NOW)

        try:
            result = asyncio.run(run())
            return result, requests
        except Exception:
            self.assertEqual(len(requests), 3)  # only three public GET endpoints
            self.assertTrue(all(r.method == 'GET' for r in requests))
            raise

    def test_valid_snapshot_and_spread(self):
        snap, requests = self.run_case()
        self.assertEqual(snap.symbol, 'ETHUSDT')
        self.assertEqual(snap.mark_price, 2500.5)
        self.assertEqual(snap.index_price, 2500.0)
        self.assertAlmostEqual(snap.funding_rate, -0.000125)
        self.assertEqual(snap.open_interest, 1500000.25)
        self.assertAlmostEqual(snap.spread_pct, 100 * .5 / 2500.25)
        self.assertEqual({r.url.path for r in requests}, {
            '/fapi/v1/premiumIndex', '/fapi/v1/openInterest',
            '/fapi/v1/ticker/bookTicker'})
        self.assertTrue(all(r.url.params['symbol'] == 'ETHUSDT' for r in requests))

    def test_bad_symbol_rejected_before_network(self):
        async def run():
            async with httpx.AsyncClient(transport=httpx.MockTransport(
                    lambda request: (_ for _ in ()).throw(AssertionError('unexpected network')))) as client:
                return await fetch_futures_snapshot(client, 'ethusdt', now_ms=NOW)
        with self.assertRaises(ValueError):
            asyncio.run(run())

    def test_symbol_mismatch_rejected(self):
        with self.assertRaises(FuturesDataError):
            self.run_case(mutate=lambda p: p['/fapi/v1/openInterest'].update(symbol='BTCUSDT'))

    def test_http_error_rejected(self):
        with self.assertRaises(FuturesDataError):
            self.run_case(status=503)

    def test_missing_quote_rejected(self):
        with self.assertRaises(FuturesDataError):
            self.run_case(mutate=lambda p: p['/fapi/v1/ticker/bookTicker'].pop('askPrice'))

    def test_crossed_book_rejected(self):
        with self.assertRaises(FuturesDataError):
            self.run_case(mutate=lambda p: p['/fapi/v1/ticker/bookTicker'].update(askPrice='2499'))

    def test_nonfinite_mark_rejected(self):
        with self.assertRaises(FuturesDataError):
            self.run_case(mutate=lambda p: p['/fapi/v1/premiumIndex'].update(markPrice='NaN'))

    def test_negative_open_interest_rejected(self):
        with self.assertRaises(FuturesDataError):
            self.run_case(mutate=lambda p: p['/fapi/v1/openInterest'].update(openInterest='-3'))

    def test_stale_open_interest_rejected(self):
        with self.assertRaises(FuturesDataError):
            self.run_case(mutate=lambda p: p['/fapi/v1/openInterest'].update(time=NOW - 120_001))

    def test_future_open_interest_rejected(self):
        with self.assertRaises(FuturesDataError):
            self.run_case(mutate=lambda p: p['/fapi/v1/openInterest'].update(time=NOW + 1))

    def test_zero_and_negative_funding_are_allowed(self):
        for rate in ('0', '-0.00025'):
            with self.subTest(rate=rate):
                snap, _ = self.run_case(
                    mutate=lambda p: p['/fapi/v1/premiumIndex'].update(lastFundingRate=rate))
                self.assertEqual(snap.funding_rate, float(rate))

    def test_bad_funding_timestamp_rejected(self):
        with self.assertRaises(FuturesDataError):
            self.run_case(mutate=lambda p: p['/fapi/v1/premiumIndex'].update(nextFundingTime='bad'))

    def test_no_credentials_or_write_requests(self):
        _, requests = self.run_case()
        self.assertTrue(all('apiKey' not in dict(r.headers) for r in requests))
        self.assertTrue(all(r.method == 'GET' and '/fapi/v1/' in r.url.path for r in requests))


if __name__ == '__main__':
    unittest.main()
