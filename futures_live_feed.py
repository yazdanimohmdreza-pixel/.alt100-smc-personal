"""Read-only USD-M futures feed for the independent ALT100 trend engine.

Run on demand/on GitHub Actions (REST snapshots, NOT a persistent websocket).
SMC spot engine and its trade criteria are intentionally untouched.
No keys, account requests, trade orders, or leverage.
"""
from __future__ import annotations

import asyncio
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
import html
from math import isfinite
import time

import httpx

from futures_data import BASE_URL, FuturesSnapshot
from quality import MarketQuality, evaluate_quote, tick_size, format_tick_price
from strategy import Signal, closed_candles
from strategy_trend import make_trend_signal

_ALLOWED = frozenset({
    '/fapi/v1/exchangeInfo', '/fapi/v1/ticker/24hr', '/fapi/v1/klines',
    '/fapi/v1/premiumIndex', '/fapi/v1/openInterest', '/fapi/v1/ticker/bookTicker',
})
_DURATIONS = {'1h': 3_600_000, '15m': 900_000, '5m': 300_000}


class FuturesFeedError(RuntimeError):
    pass


@dataclass(frozen=True)
class FuturesApprovedSignal:
    signal: Signal
    snapshot: FuturesSnapshot
    quality: MarketQuality
    price_step: str


@dataclass(frozen=True)
class FuturesScan:
    universe: int
    analyzed: int
    raw_setups: int
    approved: tuple[FuturesApprovedSignal, ...]
    errors: int
    rejected: dict[str, int]


async def _get(client: httpx.AsyncClient, path: str, params: dict | None = None):
    if path not in _ALLOWED:
        raise ValueError('Unapproved futures market data endpoint')
    for attempt in range(3):
        try:
            res = await client.get(BASE_URL + path, params=params, timeout=12)
            if res.status_code in (418, 429, 500, 502, 503, 504):
                if attempt == 2:
                    raise FuturesFeedError(f'Futures API unavailable: {path}: HTTP {res.status_code}')
                # Rate limiting: leave time for other GitHub runs; bounded retries.
                await asyncio.sleep(1.5 * (2 ** attempt))
                continue
            res.raise_for_status()
            return res.json()
        except (httpx.HTTPError, ValueError) as exc:
            if attempt == 2:
                raise FuturesFeedError(f'Futures market data error: {path}') from exc
            await asyncio.sleep(1.5 * (2 ** attempt))
    raise FuturesFeedError('Futures market-data retries exhausted')


def select_futures_universe(info: dict, daily: list, spot_pairs: list[tuple[str, float]],
                            maximum: int = 100) -> list[tuple[str, float, dict]]:
    """Only active USDT perpetual contracts that belong to the existing spot universe.

    Filter on futures *quote* turnover, not spot turnover. BTC can remain excluded
    by the spot selector. No quarterly/COIN-M/delivery contracts enter the scan.
    """
    if not isinstance(info, dict) or not isinstance(daily, list):
        raise FuturesFeedError('Invalid USD-M futures exchange metadata')
    allowed = {s for s, _ in spot_pairs}
    contracts = {
        v.get('symbol'): v for v in info.get('symbols', []) if isinstance(v, dict)
        and v.get('symbol') in allowed
        and v.get('status') == 'TRADING'
        and v.get('contractType') == 'PERPETUAL'
        and v.get('quoteAsset') == 'USDT'
        and v.get('marginAsset') == 'USDT'
    }
    output = []
    for row in daily:
        if not isinstance(row, dict) or row.get('symbol') not in contracts:
            continue
        try:
            volume = float(row['quoteVolume'])
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
        if isfinite(volume) and volume >= 1_000_000:
            output.append((row['symbol'], volume, contracts[row['symbol']]))
    output.sort(key=lambda item: item[1], reverse=True)
    return output[:max(0, min(100, maximum))]


def _number(value: object, key: str, *, positive: bool = True) -> float:
    try:
        n = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise FuturesFeedError(f'Invalid futures {key}') from exc
    if not isfinite(n) or (positive and n <= 0):
        raise FuturesFeedError(f'Invalid futures {key}')
    return n


async def fetch_verified_snapshot(client: httpx.AsyncClient, symbol: str,
                                  *, now_ms: int | None = None) -> FuturesSnapshot:
    """Require fresh independent timestamps for mark, OI and best bid/ask.

    All three are *USD-M futures* public GET responses. Reject exchange symbol
    mismatches, invalid prices and delayed feeds; never use spot as fallback.
    """
    mark, interest, book = await asyncio.gather(
        _get(client, '/fapi/v1/premiumIndex', {'symbol': symbol}),
        _get(client, '/fapi/v1/openInterest', {'symbol': symbol}),
        _get(client, '/fapi/v1/ticker/bookTicker', {'symbol': symbol}),
    )
    if not all(isinstance(x, dict) and x.get('symbol') == symbol
               for x in (mark, interest, book)):
        raise FuturesFeedError('Futures symbol mismatch in real-time quote')
    checked_at = int(time.time() * 1000) if now_ms is None else now_ms
    for name, item, max_age in (
            ('mark', mark, 30_000), ('open_interest', interest, 120_000),
            ('best_quote', book, 30_000)):
        try:
            exchange_at = int(item['time'])
        except (KeyError, ValueError, TypeError, OverflowError) as exc:
            raise FuturesFeedError(f'Missing futures {name} timestamp') from exc
        if not 0 <= checked_at - exchange_at <= max_age:
            raise FuturesFeedError(f'Stale or future futures {name}')
    bid = _number(book.get('bidPrice'), 'bid')
    ask = _number(book.get('askPrice'), 'ask')
    if ask <= bid:
        raise FuturesFeedError('Futures order book is crossed')
    try:
        funding_at = int(mark['nextFundingTime'])
    except (KeyError, ValueError, TypeError, OverflowError) as exc:
        raise FuturesFeedError('Invalid next funding time') from exc
    if funding_at < 0:
        raise FuturesFeedError('Invalid next funding time')
    return FuturesSnapshot(
        symbol, checked_at, _number(mark.get('markPrice'), 'mark_price'),
        _number(mark.get('indexPrice'), 'index_price'),
        _number(mark.get('lastFundingRate'), 'funding_rate', positive=False),
        funding_at, _number(interest.get('openInterest'), 'open_interest'),
        bid, ask, 100 * (ask - bid) / ((ask + bid)/2),
    )


async def _one(client: httpx.AsyncClient, sem: asyncio.Semaphore, symbol: str,
               volume: float, now_ms: int, min_score: int):
    async with sem:
        try:
            raw15, raw1h, raw5 = await asyncio.gather(*[
                _get(client, '/fapi/v1/klines',
                     {'symbol': symbol, 'interval': interval, 'limit': 140})
                for interval in ('15m', '1h', '5m')
            ])
            if not all(isinstance(raw, list) for raw in (raw15, raw1h, raw5)):
                raise FuturesFeedError('Invalid USD-M futures kline response')
            series = [closed_candles(raw, now_ms) for raw in (raw15, raw1h, raw5)]
            if not all(series):
                return symbol, None, 'empty_closed_candles'
            for bars, interval in zip(series, ('15m', '1h', '5m')):
                if (len(bars) < 70 or
                    any(b.open_time-a.open_time != _DURATIONS[interval]
                        for a, b in zip(bars[-70:-1], bars[-69:]))):
                    return symbol, None, 'gapped_futures_history'
            f15, h1, f5 = series
            if not (0 <= now_ms-f5[-1].close_time <= 9*60_000 and
                    0 <= now_ms-f15[-1].close_time <= 20*60_000 and
                    0 <= now_ms-h1[-1].close_time <= 70*60_000):
                return symbol, None, 'stale_futures_candles'
            return symbol, make_trend_signal(symbol, f15, h1, f5, volume, min_score), None
        except (httpx.HTTPError, ValueError, FuturesFeedError, TypeError) as exc:
            # Don't log credentials or full URLs. The feed has none.
            return symbol, None, type(exc).__name__


async def scan_futures(client: httpx.AsyncClient, spot_pairs: list[tuple[str, float]],
                       now_ms: int, min_score: int = 75,
                       maximum: int = 100) -> FuturesScan:
    """Scan second trend engine using native USD-M candles and quotes only.

    Futures outages MUST NOT affect the original spot SMC scan. The caller
    handles this function's errors independently; no silent spot fallback.
    """
    info, daily = await asyncio.gather(
        _get(client, '/fapi/v1/exchangeInfo'),
        _get(client, '/fapi/v1/ticker/24hr'),
    )
    universe = select_futures_universe(info, daily, spot_pairs, maximum)
    if not universe:
        raise FuturesFeedError('No verified active USDT perpetual contracts')
    sem = asyncio.Semaphore(4)
    checked = await asyncio.gather(*[
        _one(client, sem, symbol, vol, now_ms, min_score)
        for symbol, vol, _ in universe
    ])
    errors = [(symbol, reason) for symbol, _, reason in checked if reason]
    if len(errors) > len(checked)//3:
        raise FuturesFeedError(f'Futures price-feed unhealthy: {len(errors)}/{len(checked)} failed')
    raw = [s for _, s, error in checked if s is not None and error is None]
    symbol_info = {sym: meta for sym, _, meta in universe}
    approved = []
    rejected = Counter()
    for s in raw:
        try:
            # Live mark/funding/open-interest and executable order-book quotes.
            # Fetch these AFTER the entire candle scan to reduce stale quotes.
            snap = await fetch_verified_snapshot(client, s.symbol)
        except (FuturesFeedError, httpx.HTTPError, ValueError, TypeError):
            # Fail closed on unexpected invalid/missing futures data.
            rejected['unavailable_or_stale_futures_quote'] += 1
            continue
        quality_reasons = []
        # Futures quotes are deliberately NOT substituted with spot prices.
        q = evaluate_quote(s, {'bidPrice': snap.bid, 'askPrice': snap.ask},
                           int(time.time() * 1000), rejection_reason=quality_reasons)
        step = tick_size(symbol_info[s.symbol])
        if q is None:
            rejected[quality_reasons[0] if quality_reasons else 'quality_failed'] += 1
        elif step is None:
            rejected['missing_futures_tick_size'] += 1
        else:
            approved.append(FuturesApprovedSignal(s, snap, q, step))
    approved.sort(key=lambda result: (result.signal.score, result.signal.volume_ratio), reverse=True)
    return FuturesScan(len(universe), len(universe) - len(errors), len(raw),
                       tuple(approved), len(errors), dict(rejected))


def render_futures_signal(item: FuturesApprovedSignal, *, issued_ms: int) -> str:
    """Telegram HTML research card, with actual 3R/4R targets and UTC issue time."""
    s, snap = item.signal, item.snapshot
    is_long = s.direction == 'BUY'
    side = '🟢 LONG' if is_long else '🔴 SHORT'
    asset = html.escape(s.symbol.removesuffix('USDT'))
    icon = {'BTC': '₿', 'ETH': 'Ξ', 'SOL': '◎'}.get(asset, '🪙')
    price = lambda v: format_tick_price(v, item.price_step)
    issued = datetime.fromtimestamp(issued_ms / 1000, timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
    confirmed = datetime.fromtimestamp(s.candle_close / 1000, timezone.utc).strftime('%H:%M:%S UTC')
    reason1 = html.escape(s.reasons[0])
    reason2 = html.escape('؛ '.join(s.reasons[1:4]))
    invalidation = ('افت و تثبیت قیمت زیر حد ضرر' if is_long else 'رشد و تثبیت قیمت بالای حد ضرر')
    return (
        '<b>✨ CoinWhisper | ALT100</b>\n'
        f'{icon} <b>{asset}/USDT</b> | 1H + 15M + 5M\n'
        f'🕒 صدور: <code>{issued}</code>\n'
        f'📉 آخرین کندل تأیید: {confirmed}\n'
        '🧠 Trend Pullback | USD-M Futures\n'
        f'<b>{side} — هشدار تحلیلی فیوچرز</b>\n\n'
        '🎯 TP3: <i>تعریف نشده</i>\n'
        f'🎯 TP2: <code>{price(s.tp2)}</code> (4R)\n'
        f'🎯 TP1: <code>{price(s.tp1)}</code> (3R)\n'
        f'📍 ENTRY: <code>{price(s.entry)}</code>\n'
        f'🛑 STOP: <code>{price(s.stop)}</code>\n\n'
        '🔎 <b>دلایل ورود</b>\n'
        f'۱. {reason1}\n'
        f'۲. {reason2}\n\n'
        '🚫 <b>شرایط ابطال</b>\n'
        f'۱. {invalidation}: {price(s.stop)}\n'
        '۲. انقضای تریگر یا از دست‌رفتن شروط روند/پولبک قبل از ورود.\n\n'
        f'📊 امتیاز: {s.score}/100 | اسپرد: {item.quality.spread_pct:.3f}%\n'
        f'📈 Mark: {price(snap.mark_price)} | Index: {price(snap.index_price)}\n'
        f'📌 Funding: {snap.funding_rate*100:+.4f}% | OI: {snap.open_interest:,.2f}\n'
        '⚠️ فقط هشدار؛ ربات معامله باز نمی‌کند. هزینه معامله، فاندینگ و اسلیپیج واقعی متغیرند.'
    )
