"""Independent ALT100 trend-pullback research strategy (shadow mode).

Second decision engine beside strategy.py, not a replacement. Uses completed
1h/15m/5m candles and pure standard-library indicators. No I/O or orders.
The caller must still enforce feed freshness, quote quality and deduplication.
"""
from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from statistics import mean

from strategy import Candle, Signal, atr

ENGINE_ID = 'TREND_PULLBACK_V1'


def ema(values: list[float], period: int) -> float | None:
    if period < 2 or len(values) < period or not all(isfinite(v) for v in values):
        return None
    value = mean(values[:period])
    weight = 2 / (period + 1)
    for price in values[period:]:
        value += weight * (price - value)
    return value


def rsi(values: list[float], period: int = 14) -> float | None:
    if period < 2 or len(values) < period + 1 or not all(isfinite(v) for v in values):
        return None
    changes = [b - a for a, b in zip(values[:-1], values[1:])]
    avg_up = mean(max(change, 0) for change in changes[:period])
    avg_down = mean(max(-change, 0) for change in changes[:period])
    for change in changes[period:]:
        avg_up = (avg_up * (period - 1) + max(change, 0)) / period
        avg_down = (avg_down * (period - 1) + max(-change, 0)) / period
    if avg_up == avg_down == 0:
        return 50.0
    if avg_down == 0:
        return 100.0
    return 100 - 100 / (1 + avg_up / avg_down)


def _valid_candles(candles: list[Candle], step_ms: int, minimum: int) -> bool:
    if len(candles) < minimum:
        return False
    for bar in candles:
        if (bar.open_time < 0 or bar.close_time != bar.open_time + step_ms - 1 or
            not all(isfinite(v) for v in (bar.open, bar.high, bar.low, bar.close, bar.volume)) or
            bar.low <= 0 or bar.low > min(bar.open, bar.close) or
            bar.high < max(bar.open, bar.close) or bar.volume < 0):
            return False
    return all(b.open_time - a.open_time == step_ms for a, b in zip(candles[:-1], candles[1:]))


def make_trend_signal(symbol: str, fifteen: list[Candle], hourly: list[Candle],
                      five: list[Candle], quote_volume: float,
                      min_score: int = 75) -> Signal | None:
    """Trade idea after a 15m EMA20 pullback and a fresh 5m breakout.

    1h EMA20/EMA50 identifies regime; 15m uses EMA20 and RSI confirmation;
    5m requires a newly closed directionally strong breakout. Reversal warnings
    are *not* short orders. Trend-only targets: TP1 = 3R, TP2 = 4R. The SMC engine is unchanged.
    """
    if (not isinstance(symbol, str) or not symbol.endswith('USDT') or
        not isfinite(quote_volume) or quote_volume < 1_000_000 or
        not 0 <= min_score <= 100 or
        not _valid_candles(hourly, 3_600_000, 70) or
        not _valid_candles(fifteen, 900_000, 70) or
        not _valid_candles(five, 300_000, 65)):
        return None
    # All last candles must refer to the same closed five-minute instant or an
    # earlier higher-timeframe close; no future higher-timeframe information.
    end = five[-1].close_time
    if not (0 <= end - hourly[-1].close_time < 3_600_000 and
            0 <= end - fifteen[-1].close_time < 900_000):
        return None
    h = [c.close for c in hourly]
    m = [c.close for c in fifteen]
    fast_h, slow_h = ema(h, 20), ema(h, 50)
    fast_m = ema(m, 20)
    momentum = rsi(m, 14)
    a15, a5 = atr(fifteen), atr(five)
    if any(x is None for x in (fast_h, slow_h, fast_m, momentum, a15, a5)):
        return None
    direction = 1 if h[-1] > fast_h > slow_h else (-1 if h[-1] < fast_h < slow_h else 0)
    if direction == 0 or abs(fast_h - slow_h) / slow_h < .002:
        return None
    if direction == 1 and not 51 <= momentum <= 72:
        return None
    if direction == -1 and not 28 <= momentum <= 49:
        return None
    # Completed 15m pullback: the previous candle tags EMA20, then the newest
    # closes in trend direction. The tag is on a single candle, never synthetic.
    prev15, cur15 = fifteen[-2:]
    tolerance = .18 * a15
    touched = (prev15.low - tolerance <= fast_m <= prev15.high + tolerance)
    confirmed = (cur15.close > fast_m and cur15.close > cur15.open if direction == 1
                 else cur15.close < fast_m and cur15.close < cur15.open)
    if not (touched and confirmed):
        return None
    cur5 = five[-1]
    prior5 = five[-4:-1]
    if direction == 1:
        breakout = cur5.close > max(b.high for b in prior5) and cur5.close > cur5.open
    else:
        breakout = cur5.close < min(b.low for b in prior5) and cur5.close < cur5.open
    if not breakout:
        return None
    avg_vol = mean(c.volume for c in five[-21:-1])
    if avg_vol <= 0:
        return None
    vol_ratio = cur5.volume / avg_vol
    if vol_ratio < 1.15:
        return None
    entry = cur5.close
    # Structural invalidation at the pullback extremes with a volatility buffer.
    stop = (min(prev15.low, cur15.low) - .2 * a15 if direction == 1
            else max(prev15.high, cur15.high) + .2 * a15)
    distance = direction * (entry - stop)
    if distance <= 0:
        return None
    risk_pct = 100 * distance / entry
    if not .35 <= risk_pct <= 3.5:
        return None
    # Confluence score is a deterministic filter, NEVER win probability.
    score = 50
    score += 10 if abs(fast_h - slow_h) / slow_h >= .006 else 7
    score += 10  # validated EMA pullback and resumption
    score += 10  # latest 5m breakout
    score += 8 if vol_ratio >= 1.5 else 5
    score += 6 if quote_volume >= 10_000_000 else 3
    score += 5 if risk_pct <= 1.5 else 0
    score = min(score, 100)
    if score < min_score:
        return None
    reasons = (
        'روند یک‌ساعته EMA20/EMA50',
        'پولبک به میانگین ۲۰ کندلی و ادامه حرکت ۱۵دقیقه',
        f'RSI پانزده‌دقیقه: {momentum:.1f}',
        f'شکست محدوده سه کندل اخیر ۵دقیقه؛ حجم نسبی {vol_ratio:.2f}',
    )
    setup_id = f'{ENGINE_ID}:{cur15.close_time}:{direction}'
    return Signal(
        symbol=symbol,
        direction='BUY' if direction == 1 else 'SELL_WARNING',
        candle_close=cur5.close_time,
        entry=entry,
        stop=stop,
        tp1=entry + direction * 3.0 * distance,
        tp2=entry + direction * 4.0 * distance,
        score=score,
        timeframe='1h + 15m + 5m',
        volume_ratio=vol_ratio,
        risk_pct=risk_pct,
        reasons=reasons,
        setup=ENGINE_ID,
        trigger='5m range breakout',
        setup_id=setup_id,
        entry_zone_low=min(prev15.low, cur15.low),
        entry_zone_high=max(prev15.high, cur15.high),
        limit_entry=entry,
    )
