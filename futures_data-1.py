"""Optional, read-only Binance USD-M futures context for ALT100.

Standalone research adapter. No API keys, orders, leverage, or modifications to
existing signal generation. REST is a snapshot, not a continuous live feed.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from math import isfinite
import re
import time

import httpx

BASE_URL = 'https://fapi.binance.com'
_SYMBOL = re.compile(r'^[A-Z0-9]{2,22}USDT$')


class FuturesDataError(RuntimeError):
    """Public derivatives feed was missing, invalid, or unavailable."""


@dataclass(frozen=True)
class FuturesSnapshot:
    symbol: str
    captured_at_ms: int
    mark_price: float
    index_price: float
    funding_rate: float
    next_funding_time_ms: int
    open_interest: float
    bid: float
    ask: float
    spread_pct: float


def _positive(value: object, name: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise FuturesDataError(f'Invalid {name}') from exc
    if not isfinite(result) or result <= 0:
        raise FuturesDataError(f'Invalid {name}')
    return result


def _finite(value: object, name: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise FuturesDataError(f'Invalid {name}') from exc
    if not isfinite(result):
        raise FuturesDataError(f'Invalid {name}')
    return result


async def _get(client: httpx.AsyncClient, path: str, symbol: str) -> dict:
    # Keep the scope to three fixed, public USD-M market-data endpoints.
    try:
        response = await client.get(BASE_URL + path, params={'symbol': symbol}, timeout=10)
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            raise FuturesDataError('Malformed derivatives response')
        if data.get('symbol') != symbol:
            raise FuturesDataError('Derivatives symbol mismatch')
        return data
    except (httpx.HTTPError, ValueError) as exc:
        raise FuturesDataError(f'Unavailable futures market data: {path}') from exc


async def fetch_futures_snapshot(client: httpx.AsyncClient, symbol: str,
                                 *, now_ms: int | None = None) -> FuturesSnapshot:
    """Fetch one USD-M perpetual symbol's mark, funding, OI and best quote.

    Requires a Binance USD-M USDT symbol. Missing/invalid data fails closed.
    Open interest is contracts in exchange units, not dollar notional; funding
    is a decimal (e.g. 0.0001 = 0.01%). Futures metrics must not be treated
    as spot quotes or substituted into the current strategy without research.
    """
    if not isinstance(symbol, str) or not _SYMBOL.fullmatch(symbol):
        raise ValueError('Expected uppercase USDT futures symbol')
    mark, interest, book = await asyncio.gather(
        _get(client, '/fapi/v1/premiumIndex', symbol),
        _get(client, '/fapi/v1/openInterest', symbol),
        _get(client, '/fapi/v1/ticker/bookTicker', symbol),
    )
    captured = int(time.time() * 1000) if now_ms is None else int(now_ms)
    mark_price = _positive(mark.get('markPrice'), 'mark price')
    index_price = _positive(mark.get('indexPrice'), 'index price')
    funding = _finite(mark.get('lastFundingRate'), 'funding rate')
    oi = _positive(interest.get('openInterest'), 'open interest')
    bid = _positive(book.get('bidPrice'), 'bid')
    ask = _positive(book.get('askPrice'), 'ask')
    if ask <= bid:
        raise FuturesDataError('Crossed or invalid bid/ask')
    try:
        next_funding = int(mark['nextFundingTime'])
        exchange_time = int(interest['time'])
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise FuturesDataError('Invalid futures timestamps') from exc
    if not (0 <= captured - exchange_time <= 120_000):
        raise FuturesDataError('Stale or future open interest data')
    if next_funding < 0:
        raise FuturesDataError('Invalid funding timestamp')
    return FuturesSnapshot(symbol, captured, mark_price, index_price, funding,
                           next_funding, oi, bid, ask,
                           100.0 * (ask - bid) / ((ask + bid) / 2))


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='Public USD-M futures snapshot (no trading)')
    parser.add_argument('symbol', help='e.g. ETHUSDT')
    args = parser.parse_args()

    async def run() -> None:
        async with httpx.AsyncClient() as client:
            print(await fetch_futures_snapshot(client, args.symbol))

    asyncio.run(run())
