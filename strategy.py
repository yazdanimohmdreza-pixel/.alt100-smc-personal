"""ALT100 multi-timeframe SMC/price-action research alerts (closed candles only).

1h: confirmed swing structure and break of structure (BOS).
15m: liquidity sweeps, fair value gaps (FVG), displacement/order blocks,
     and break/retest zones.
5m: confirmed micro-structure break, engulfing, or wick-rejection trigger.
No order placement, leverage, or guarantee of winning trades.
"""
from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from statistics import mean
from typing import Optional


@dataclass(frozen=True)
class Candle:
    open_time: int
    close_time: int
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True)
class Signal:
    symbol: str
    direction: str  # BUY (spot) or SELL_WARNING (do not open shorts)
    candle_close: int
    entry: float
    stop: float
    tp1: float
    tp2: float
    score: int
    timeframe: str
    volume_ratio: float
    risk_pct: float
    reasons: tuple[str, ...]
    setup: str
    trigger: str
    # Stable setup identifier prevents repeated paid alerts on every new 5m candle.
    setup_id: str = ''
    entry_zone_low: float = 0.0
    entry_zone_high: float = 0.0
    limit_entry: float = 0.0


def closed_candles(data: list, now_ms: int) -> list[Candle]:
    """Only fully closed Binance candles; no forming candle look-ahead."""
    out: list[Candle] = []
    for k in data:
        if len(k) < 7:
            continue
        try:
            c = Candle(int(k[0]), int(k[6]), *(float(k[i]) for i in (1, 2, 3, 4, 5)))
        except (ValueError, TypeError, OverflowError):
            continue
        if (not all(isfinite(v) for v in (c.open, c.high, c.low, c.close, c.volume)) or
                c.close_time >= now_ms or c.open_time >= c.close_time or
                c.low <= 0 or c.high < c.low or
                min(c.open, c.close) < c.low or max(c.open, c.close) > c.high or
                c.volume < 0):
            continue
        out.append(c)
    return out


def atr(candles: list[Candle], period: int = 14) -> Optional[float]:
    if len(candles) < period + 1:
        return None
    tr = [max(b.high - b.low, abs(b.high - a.close), abs(b.low - a.close))
          for a, b in zip(candles[:-1], candles[1:])]
    av = mean(tr[:period])
    for x in tr[period:]:
        av = (av * (period - 1) + x) / period
    return av if av > 0 else None


def pivots(candles: list[Candle], side: str, wing: int = 2) -> list[tuple[int, float]]:
    """Fully confirmed pivots only (needs `wing` bars to the right)."""
    vals = [c.high if side == 'high' else c.low for c in candles]
    points = []
    for i in range(wing, len(vals) - wing):
        nearby = vals[i - wing:i] + vals[i + 1:i + wing + 1]
        if (vals[i] > max(nearby) if side == 'high' else vals[i] < min(nearby)):
            points.append((i, vals[i]))
    return points


def hourly_bias(hourly: list[Candle]) -> tuple[int, str] | None:
    """Confirmed HH/HL or LL/LH; recent structural BOS takes precedence."""
    if len(hourly) < 55:
        return None
    highs = pivots(hourly, 'high')
    lows = pivots(hourly, 'low')
    if len(highs) < 2 or len(lows) < 2:
        return None
    last = hourly[-1].close
    hi, lo = highs[-1][1], lows[-1][1]
    hh = highs[-1][1] > highs[-2][1]
    hl = lows[-1][1] > lows[-2][1]
    lh = highs[-1][1] < highs[-2][1]
    ll = lows[-1][1] < lows[-2][1]
    # Last closed hour must show plausible bias, with no opposite structure break.
    if last > hi and lows[-1][0] >= len(hourly) - 42:
        return 1, 'شکست ساختار صعودی (BOS) در یک‌ساعته'
    if last < lo and highs[-1][0] >= len(hourly) - 42:
        return -1, 'شکست ساختار نزولی (BOS) در یک‌ساعته'
    if hh and hl and last > lo:
        return 1, 'ساختار HH/HL در یک‌ساعته'
    if ll and lh and last < hi:
        return -1, 'ساختار LL/LH در یک‌ساعته'
    return None


@dataclass(frozen=True)
class Zone:
    kind: str
    low: float
    high: float
    created: int
    note: str


def _valid_zone(zone: Zone, candles: list[Candle], direction: int, end: int) -> bool:
    if zone.high <= zone.low:
        return False
    # Invalidated closes AND fully mitigated gaps must be rejected.
    future = candles[zone.created + 1:end]
    if direction == 1:
        if any(c.close < zone.low for c in future):
            return False
        if zone.kind == 'FVG' and any(c.low <= zone.low for c in future):
            return False
    else:
        if any(c.close > zone.high for c in future):
            return False
        if zone.kind == 'FVG' and any(c.high >= zone.high for c in future):
            return False
    return True


def zones_15m(candles: list[Candle], direction: int, vol: float) -> list[Zone]:
    """Locate a few objectively testable SMC zones (without repainting)."""
    n = len(candles)
    output: list[Zone] = []
    # FVG: 3-bar imbalance with a displacement middle candle.
    for i in range(max(2, n - 23), n):
        prev, mid, cur = candles[i - 2:i + 1]
        if abs(mid.close - mid.open) < 0.45 * vol:
            continue
        if direction == 1 and mid.close > mid.open and cur.low > prev.high:
            z = Zone('FVG', prev.high, cur.low, i, 'ناحیه عدم تعادل FVG در ۱۵دقیقه')
        elif direction == -1 and mid.close < mid.open and cur.high < prev.low:
            z = Zone('FVG', cur.high, prev.low, i, 'ناحیه عدم تعادل FVG در ۱۵دقیقه')
        else:
            continue
        if 0.10 * vol <= z.high - z.low <= 2.0 * vol and _valid_zone(z, candles, direction, n):
            output.append(z)

    # Order block: opposite candle immediately preceding a structural displacement.
    for i in range(max(6, n - 19), n):
        impulse, candidate = candles[i], candles[i - 1]
        body = abs(impulse.close - impulse.open)
        before = candles[i - 6:i]
        if body < 0.70 * vol:
            continue
        if direction == 1 and (candidate.close < candidate.open and impulse.close > impulse.open
                               and impulse.close > max(c.high for c in before)):
            z = Zone('OB', candidate.low, candidate.high, i, 'اوردر بلاک صعودی پس از جابه‌جایی ساختاری ۱۵دقیقه')
        elif direction == -1 and (candidate.close > candidate.open and impulse.close < impulse.open
                                 and impulse.close < min(c.low for c in before)):
            z = Zone('OB', candidate.low, candidate.high, i, 'اوردر بلاک نزولی پس از جابه‌جایی ساختاری ۱۵دقیقه')
        else:
            continue
        if z.high - z.low <= 2.6 * vol and _valid_zone(z, candles, direction, n):
            output.append(z)

    # Liquidity sweep: recent 15m wick crosses prior 10-bar extreme and closes back inside.
    for i in range(max(10, n - 5), n):
        c = candles[i]
        prior = candles[i - 10:i]
        if direction == 1:
            level = min(x.low for x in prior)
            sweep = c.low < level and c.close > level and c.close > c.low + .52 * (c.high - c.low)
            z = Zone('SWEEP', c.low, level, i, 'جمع‌آوری نقدینگی زیر کف ۱۵دقیقه و بازگشت قیمت')
        else:
            level = max(x.high for x in prior)
            sweep = c.high > level and c.close < level and c.close < c.low + .48 * (c.high - c.low)
            z = Zone('SWEEP', level, c.high, i, 'جمع‌آوری نقدینگی بالای سقف ۱۵دقیقه و بازگشت قیمت')
        if sweep and 0 < z.high - z.low <= 2.5 * vol and _valid_zone(z, candles, direction, n):
            output.append(z)

    # A real retest needs a *previous* confirmed breakout, not just an old
    # pivot with a close above it. The breakout candle is the zone's stable ID.
    highs, lows = pivots(candles, 'high'), pivots(candles, 'low')
    levels = highs if direction == 1 else lows
    for idx, level in levels[-5:]:
        breakout = None
        for j in range(idx + 3, n - 1):
            before, current = candles[j - 1], candles[j]
            if direction == 1 and before.close <= level < current.close:
                breakout = j
            elif direction == -1 and before.close >= level > current.close:
                breakout = j
        if breakout is None or n - breakout > 12:
            continue
        zone = Zone('BOS_RETEST', level - .25 * vol, level + .25 * vol, breakout,
                    'شکست ساختار ۱۵دقیقه و بازآزمایی سطح شکسته‌شده')
        # Revisited after breakout; a breakout without any retest is NOT a setup.
        subsequent = candles[breakout + 1:]
        touched = any(c.low <= zone.high and c.high >= zone.low for c in subsequent)
        if touched and _valid_zone(zone, candles, direction, n):
            output.append(zone)
    return output


def trigger_5m(candles: list[Candle], direction: int, vol: float) -> str | None:
    """Closed 5m confirmation; never anticipates a candle that has not closed."""
    cur, prev = candles[-1], candles[-2]
    spread = cur.high - cur.low
    if spread <= 0 or abs(cur.close - cur.open) < 0.18 * vol:
        return None
    body = abs(cur.close - cur.open)
    if direction == 1 and cur.close > cur.open:
        if cur.close > max(c.high for c in candles[-4:-1]):
            return 'تغییر ساختار خرد / شکست سقف سه کندل ۵دقیقه'
        if prev.close < prev.open and cur.close > prev.open and cur.open <= prev.close:
            return 'انگلفینگ صعودی ۵دقیقه'
        if (cur.open - cur.low) >= max(.6 * body, .18 * vol) and cur.close > cur.low + .7 * spread:
            return 'رد قیمت و فتیله برگشتی صعودی ۵دقیقه'
    if direction == -1 and cur.close < cur.open:
        if cur.close < min(c.low for c in candles[-4:-1]):
            return 'تغییر ساختار خرد / شکست کف سه کندل ۵دقیقه'
        if prev.close > prev.open and cur.close < prev.open and cur.open >= prev.close:
            return 'انگلفینگ نزولی ۵دقیقه'
        if (cur.high - cur.open) >= max(.6 * body, .18 * vol) and cur.close < cur.low + .3 * spread:
            return 'رد قیمت و فتیله برگشتی نزولی ۵دقیقه'
    return None



def has_confirmed_zone_touch(five: list[Candle], fifteen: list[Candle], zone: Zone) -> bool:
    """Require an actual 5m OHLC overlap strictly AFTER the 15m zone was confirmed.

    A bar ending alongside the 15m formation bar cannot validate that zone.
    Compare single-bar ranges, never extrema aggregated across unrelated bars.
    Uses only the last six completed 5m bars and no future data.
    """
    if not 0 <= zone.created < len(fifteen) or zone.high <= zone.low:
        return False
    confirmed_at = fifteen[zone.created].close_time
    return any(c.open_time > confirmed_at and
               c.low <= zone.high and c.high >= zone.low
               for c in five[-6:])


def make_signal(symbol: str, fifteen: list[Candle], hourly: list[Candle], five: list[Candle],
                quote_volume: float, min_score: int = 75) -> Optional[Signal]:
    if (len(fifteen) < 65 or len(hourly) < 65 or len(five) < 55 or
            quote_volume < 1_000_000):
        return None
    a15, a5 = atr(fifteen), atr(five)
    if a15 is None or a5 is None:
        return None
    bias = hourly_bias(hourly)
    if bias is None:
        return None
    direction, h_reason = bias
    trigger = trigger_5m(five, direction, a5)
    if trigger is None:
        return None
    cur = five[-1]
    baseline = mean(c.volume for c in five[-21:-1])
    if baseline <= 0:
        return None
    vol_ratio = cur.volume / baseline
    if vol_ratio < .75:  # filter near-empty candles without forcing 1.35x spikes
        return None

    valid = []
    for z in zones_15m(fifteen, direction, a15):
        # Require one real 5m overlap occurring after 15m zone confirmation.
        touched = has_confirmed_zone_touch(five, fifteen, z)
        close_enough = (z.low - .25 * a5 <= cur.close <= z.high + 1.20 * a5
                        if direction == 1 else
                        z.low - 1.20 * a5 <= cur.close <= z.high + .25 * a5)
        if touched and close_enough:
            valid.append(z)
    if not valid:
        return None
    priority = {'SWEEP': 3, 'OB': 3, 'FVG': 2, 'BOS_RETEST': 1}
    zone = max(valid, key=lambda z: (priority[z.kind], z.created))

    entry = cur.close
    recent = five[-9:]
    buffer = .20 * a5
    if direction == 1:
        stop = min(min(c.low for c in recent), zone.low) - buffer
        risk = entry - stop
        tp1, tp2 = entry + 1.5 * risk, entry + 2.5 * risk
    else:
        stop = max(max(c.high for c in recent), zone.high) + buffer
        risk = stop - entry
        tp1, tp2 = entry - 1.5 * risk, entry - 2.5 * risk
    if risk <= 0 or tp2 <= 0:
        return None
    # The limit at midpoint of the last valid 15m zone is a *conditional* plan.
    # A 5m confirmation is NOT proof that this future pullback will occur.
    mid = (zone.low + zone.high) / 2
    limit_entry = min(entry, mid) if direction == 1 else max(entry, mid)
    if direction == 1 and limit_entry <= stop:
        return None
    if direction == -1 and limit_entry >= stop:
        return None
    # All targets and risk figures must use the SAME conditional limit entry.
    risk = abs(limit_entry - stop)
    tp1 = limit_entry + direction * 1.5 * risk
    tp2 = limit_entry + direction * 2.5 * risk
    if tp2 <= 0:
        return None
    risk_pct = 100 * risk / limit_entry
    # Very narrow stops are consumed by round-trip costs for scalp trades.
    if not .35 <= risk_pct <= 3.5:
        return None
    # Explicit confluence score. Not an estimated win probability.
    score = 42
    score += 10 if 'BOS' in h_reason else 7
    score += {'SWEEP': 10, 'OB': 9, 'FVG': 8, 'BOS_RETEST': 7}[zone.kind]
    score += 6 if len({z.kind for z in valid}) >= 2 else 0
    score += 7 if vol_ratio >= 1.50 else (4 if vol_ratio >= 1.0 else 1)
    score += 8 if 'ساختار خرد' in trigger else 4
    score += 5 if quote_volume >= 10_000_000 else 2
    score += 5 if risk_pct <= 1.5 else 0
    score = min(100, score)
    if score < min_score:
        return None
    reasons = (h_reason, zone.note, trigger,
               f'حجم نسبی کندل ۵دقیقه: {vol_ratio:.2f} برابر میانگین ۲۰ کندل')
    # Stable ID for duplicate suppression; zone time identifies a market event.
    setup_id = f'{zone.kind}:{fifteen[zone.created].open_time}:{direction}'
    return Signal(symbol, 'BUY' if direction == 1 else 'SELL_WARNING', cur.close_time,
                  entry, stop, tp1, tp2, score, '1h + 15m + 5m',
                  vol_ratio, risk_pct, reasons, zone.kind, trigger,
                  setup_id, zone.low, zone.high, limit_entry)
