"""Conservative historical replay of a single Binance USDT spot symbol.

Usage (offline): python replay.py --hour 1h.json --quarter 15m.json --five 5m.json
Input JSON files are Binance raw /api/v3/klines arrays. No live download, no
forward-looking candles. Quantitative evidence requires long, independent
out-of-sample histories from different regimes, not the bundled unit tests.
"""
from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path

from strategy import Candle, closed_candles, make_signal


@dataclass(frozen=True)
class Outcome:
    symbol: str
    signal_time: int
    filled: bool
    result_r: float | None
    reason: str


def parse_kline_file(path: str) -> list[Candle]:
    raw = json.loads(Path(path).read_text(encoding='utf8'))
    if not isinstance(raw, list):
        raise ValueError('Klines must be a JSON list')
    # Exclude malformed/zero and include only actual past candles in file.
    candles = closed_candles(raw, 9_999_999_999_999)
    if len(candles) != len(raw) or any(b.close_time <= a.close_time
                                        for a, b in zip(candles, candles[1:])):
        raise ValueError('Klines malformed or not ordered')
    return candles


def summarize(outcomes: list[Outcome]) -> dict:
    total = len(outcomes)
    filled = [o.result_r for o in outcomes if o.filled and o.result_r is not None]
    wins = [x for x in filled if x > 0]
    losses = [x for x in filled if x < 0]
    gross_profit = sum(wins)
    gross_loss = -sum(losses)
    equity, peak, drawdown = 0.0, 0.0, 0.0
    for x in filled:
        equity += x
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    return {
        'alerts': total,
        'fills': len(filled),
        'fill_rate': round(len(filled) / total, 4) if total else None,
        'win_rate_on_fills': round(len(wins) / len(filled), 4) if filled else None,
        'expectancy_r': round(sum(filled) / len(filled), 4) if filled else None,
        'profit_factor': round(gross_profit / gross_loss, 3) if gross_loss else None,
        'max_drawdown_r': round(drawdown, 3),
        'notes': 'Synthetic bid/ask unavailable. Fixed 0.30% estimated roundtrip costs; stop-first within ambiguous 5m bars.'
    }


def replay(symbol: str, hourly: list[Candle], quarter: list[Candle],
           five: list[Candle], quote_volume: float = 20_000_000,
           score_threshold: int = 75) -> list[Outcome]:
    """As-of aligned signal detection; next-three-candle conditional fill.

    No buy is considered filled on the signal candle itself. Single open
    position at a time; TP1 is the only target modeled and SL wins ties.
    """
    signals = []
    last_setup = set()
    allowed_until = -1
    h, q = 0, 0
    for i in range(60, len(five)-37):
        current = five[i]
        while h < len(hourly) and hourly[h].close_time <= current.close_time:
            h += 1
        while q < len(quarter) and quarter[q].close_time <= current.close_time:
            q += 1
        if i <= allowed_until or h < 65 or q < 65:
            continue
        sig = make_signal(symbol, quarter[max(0,q-140):q],
                          hourly[max(0,h-140):h], five[max(0,i-139):i+1],
                          quote_volume, score_threshold)
        if sig is None or sig.direction != 'BUY' or sig.setup_id in last_setup:
            continue
        last_setup.add(sig.setup_id)
        entry = sig.limit_entry
        filled_at = None
        for j in range(i + 1, i + 4):
            c = five[j]
            if c.low <= entry <= c.high:
                filled_at = j
                break
        if filled_at is None:
            signals.append(Outcome(symbol, sig.candle_close, False, None, 'unfilled_limit'))
            continue
        stop = sig.stop
        target = sig.tp1
        risk = entry - stop
        if risk <= 0:
            continue
        # Conservative cost assumption: 0.1% fee each side + 0.05% slip each side.
        cost_in_r = (.003 * entry) / risk
        result = None
        exit_at = filled_at + 35
        for j in range(filled_at, min(len(five), filled_at + 36)):
            c = five[j]
            if c.low <= stop:  # Stop wins when both extremes occur in same candle.
                result, exit_at = -1 - cost_in_r, j
                break
            if j > filled_at and c.high >= target:
                # Within the entry bar, target may have occurred before fill.
                result, exit_at = 1.5 - cost_in_r, j
                break
        if result is None:
            last = five[min(exit_at, len(five)-1)].close
            result = (last - entry)/risk - cost_in_r
        signals.append(Outcome(symbol, sig.candle_close, True, result, 'simulated_exit'))
        allowed_until = min(exit_at, len(five) - 1)
    return signals


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--hour', required=True, help='Binance 1h raw klines JSON')
    parser.add_argument('--quarter', required=True, help='Binance 15m raw klines JSON')
    parser.add_argument('--five', required=True, help='Binance 5m raw klines JSON')
    parser.add_argument('--symbol', default='ETHUSDT')
    parser.add_argument('--quote-volume', type=float, default=20_000_000)
    args = parser.parse_args()
    result = replay(args.symbol, parse_kline_file(args.hour),
                    parse_kline_file(args.quarter), parse_kline_file(args.five),
                    args.quote_volume)
    print(json.dumps(summarize(result), indent=2, ensure_ascii=False))


if __name__ == '__main__':
    main()
