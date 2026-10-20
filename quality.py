"""Execution-feasibility checks for research signals; uses live public quotes.

No order placement. Estimates are deliberately conservative and not a promise
of a realizable fill or of future liquidity.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN, ROUND_UP, InvalidOperation
from math import isfinite


@dataclass(frozen=True)
class MarketQuality:
    bid: float
    ask: float
    spread_pct: float
    price_drift_pct: float
    assumed_roundtrip_cost_pct: float


def evaluate_quote(signal, ticker: dict | None, now_ms: int,
                   max_spread_pct: float = 0.22,
                   rejection_reason: list[str] | None = None) -> MarketQuality | None:
    """Keep the existing fail-closed gate, optionally recording one diagnostic code.

    `rejection_reason` never affects acceptance or price calculations. Codes are
    safe for GitHub logs and contain no Telegram credentials or private data.
    """
    def reject(reason: str) -> None:
        if rejection_reason is not None:
            rejection_reason.append(reason)
        return None

    if not isinstance(ticker, dict):
        return reject('missing_quote')
    try:
        bid, ask = float(ticker['bidPrice']), float(ticker['askPrice'])
    except (ValueError, TypeError, KeyError):
        return reject('malformed_quote')
    if (not isfinite(bid) or not isfinite(ask) or bid <= 0 or
            ask <= bid or signal.entry <= 0 or signal.risk_pct <= 0):
        return reject('invalid_quote_or_signal')
    age_ms = now_ms - signal.candle_close
    if age_ms < 0 or age_ms > 9 * 60_000:
        return reject('stale_or_future_trigger')
    mid = (bid + ask) / 2
    spread_pct = 100 * (ask - bid) / mid
    drift_pct = 100 * abs(mid - signal.entry) / signal.entry
    if spread_pct > max_spread_pct:
        return reject('spread_too_wide')
    # Do not chase a price that has already moved far from its 5m confirmation.
    if drift_pct > min(.40, .35 * signal.risk_pct):
        return reject('price_moved_from_trigger')
    # Explicit conservative assumptions: 0.1% taker fee per side and 0.05%
    # slippage per side, plus the present quoted bid/ask spread (one crossing).
    cost = .30 + spread_pct
    if cost > .55 * signal.risk_pct:
        return reject('estimated_cost_exceeds_risk_budget')
    return MarketQuality(bid, ask, spread_pct, drift_pct, cost)


def tick_size(symbol_info: dict | None) -> str | None:
    if not isinstance(symbol_info, dict):
        return None
    for item in symbol_info.get('filters', []):
        if item.get('filterType') == 'PRICE_FILTER':
            try:
                d = Decimal(str(item['tickSize']))
                if d > 0 and d.is_finite():
                    return str(d)
            except (KeyError, InvalidOperation, TypeError):
                return None
    return None


def format_tick_price(value: float, step: str | None, up: bool = False) -> str:
    try:
        v = Decimal(str(value))
        if not v.is_finite() or v <= 0:
            raise ValueError('Invalid price')
        if step is None:
            return f'{value:.8g}'
        t = Decimal(step)
        if not t.is_finite() or t <= 0:
            raise ValueError('Invalid tick')
        rounded = (v / t).to_integral_value(rounding=ROUND_UP if up else ROUND_DOWN) * t
        precision = max(0, -t.normalize().as_tuple().exponent)
        return f'{rounded:.{precision}f}'
    except (InvalidOperation, ValueError, TypeError, OverflowError) as exc:
        raise ValueError(f'Cannot format quote with exchange tickSize: {exc}') from exc
