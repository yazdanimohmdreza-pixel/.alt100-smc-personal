"""CoinWhisper signal *observation* engine. No orders, balances or PnL claims.

Uses closed, contiguous, exchange-specific 5m candles strictly after a Telegram
alert was issued. Track conditional level *touches*, not executed positions.
STOP/target touched on the same bar, or on the entry bar => ambiguous.
The SMC strategy and existing Telegram formatting are not changed by this module.
"""
from __future__ import annotations

from math import isfinite

INTERVAL_MS = 300_000
ENTRY_LIFETIME_MS = 10 * 60_000
OBSERVATION_WINDOW_MS = 24 * 3_600_000
TERMINAL = frozenset({
    'expired_without_entry', 'tp2_observed', 'stop_observed',
    'tp1_then_stop_observed', 'ambiguous_entry_candle',
    'ambiguous_stop_and_target', 'window_complete', 'incomplete_market_data',
})


def _safe_price(value: object, label: str) -> float:
    try:
        n = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f'invalid {label}') from exc
    if not isfinite(n) or n <= 0:
        raise ValueError(f'invalid {label}')
    return n


def new_record(signal, issued_at_ms: int, *, market: str = 'BINANCE_SPOT',
               engine: str = 'SMC_SPOT') -> dict:
    """Capture immutable alert levels when Telegram acknowledges the message.

    For SELL_WARNING this is a hypothetical bearish reference, NOT a short fill.
    For BUY, level entry is a proposed conditional spot limit, NOT a spot fill.
    """
    if signal.direction not in ('BUY', 'SELL_WARNING'):
        raise ValueError('unsupported research alert direction')
    if not isinstance(issued_at_ms, int) or issued_at_ms <= signal.candle_close:
        raise ValueError('issue time must follow closed trigger')
    side = 1 if signal.direction == 'BUY' else -1
    entry = _safe_price(signal.limit_entry or signal.entry, 'reference entry')
    stop, tp1, tp2 = (_safe_price(getattr(signal, k), k) for k in ('stop', 'tp1', 'tp2'))
    if not (stop < entry < tp1 < tp2 if side == 1 else stop > entry > tp1 > tp2):
        raise ValueError('inconsistent reference price / stop / targets')
    trigger_close = int(signal.candle_close)
    expires = trigger_close + ENTRY_LIFETIME_MS
    if issued_at_ms > expires:
        raise ValueError('signal was already expired before Telegram acknowledgment')
    start = (issued_at_ms // INTERVAL_MS + 1) * INTERVAL_MS
    key = f'{signal.symbol}|{signal.direction}|{signal.setup_id or trigger_close}'
    return {
        'schema': 1, 'key': key, 'symbol': str(signal.symbol), 'direction': signal.direction,
        'engine': engine, 'market': market,
        'record_kind': 'signal_level_observation_not_a_trade',
        'entry_kind': 'conditional_spot_limit' if side == 1 else 'hypothetical_bearish_limit',
        'trigger_close_ms': trigger_close, 'issued_at_ms': issued_at_ms,
        'entry_expires_at_ms': expires,
        'observe_until_ms': issued_at_ms + OBSERVATION_WINDOW_MS,
        'next_open_ms': start, 'entry': entry, 'stop': stop,
        'tp1': tp1, 'tp2': tp2, 'risk': abs(entry - stop),
        'entry_touch_ms': None, 'tp1_touch_ms': None, 'tp2_touch_ms': None,
        'stop_touch_ms': None, 'status': 'waiting_for_entry',
        # Intrabar path is unknowable; skip entry-bar excursion entirely.
        'mfe_r_after_entry_bar': 0.0, 'mae_r_after_entry_bar': 0.0,
        'evaluated_closed_bars': 0,
    }


def _bar_ok(c, now_ms: int) -> bool:
    fields = (c.open, c.high, c.low, c.close, c.volume)
    return (isinstance(c.open_time, int) and isinstance(c.close_time, int)
            and c.close_time == c.open_time + INTERVAL_MS - 1
            and 0 <= c.open_time and c.close_time < now_ms
            and all(isfinite(x) for x in fields)
            and 0 < c.low <= min(c.open, c.close)
            and c.high >= max(c.open, c.close) and c.volume >= 0)


def advance_record(record: dict, candles: list, *, now_ms: int) -> dict:
    """Advance from previously recorded cursor using *complete* 5m candles.

    The caller must fetch historical bars from the same exchange/market as the
    alert. Missing candles fail closed; don't infer a TP/SL sequence from a gap.
    Already processed candles are ignored, allowing safe repeated GitHub runs.
    """
    r = dict(record)
    if r['status'] in TERMINAL:
        return r
    side = 1 if r['direction'] == 'BUY' else -1
    entry, stop, tp1, tp2 = (r[x] for x in ('entry', 'stop', 'tp1', 'tp2'))
    risk = r['risk']
    cursor = r['next_open_ms']
    for c in sorted(candles, key=lambda bar: bar.open_time):
        if c.open_time < cursor:
            continue
        if c.open_time > cursor:
            r['status'] = 'incomplete_market_data'
            r['gap_start_ms'] = cursor
            break
        if c.close_time >= now_ms:
            break
        if not _bar_ok(c, now_ms):
            r['status'] = 'incomplete_market_data'
            r['invalid_open_ms'] = c.open_time
            break
        if c.close_time >= r['observe_until_ms']:
            r['status'] = 'window_complete'
            break
        if r['entry_touch_ms'] is None and c.close_time > r['entry_expires_at_ms']:
            r['status'] = 'expired_without_entry'
            break
        r['evaluated_closed_bars'] += 1
        cursor += INTERVAL_MS
        r['next_open_ms'] = cursor
        touched_entry = c.low <= entry if side == 1 else c.high >= entry
        hit_stop = (c.low <= stop) if side == 1 else (c.high >= stop)
        hit_tp1 = (c.high >= tp1) if side == 1 else (c.low <= tp1)
        hit_tp2 = (c.high >= tp2) if side == 1 else (c.low <= tp2)
        if r['entry_touch_ms'] is None:
            if not touched_entry:
                continue
            r['entry_touch_ms'] = c.close_time
            r['status'] = 'observing_after_entry_touch'
            # Even touching a target *on the entry candle* may have occurred
            # BEFORE entry. OHLC cannot settle this, including stop order.
            if hit_stop or hit_tp1 or hit_tp2:
                r['status'] = 'ambiguous_entry_candle'
                break
            continue
        favorable = ((c.high - entry) if side == 1 else (entry - c.low)) / risk
        adverse = ((entry - c.low) if side == 1 else (c.high - entry)) / risk
        r['mfe_r_after_entry_bar'] = round(max(r['mfe_r_after_entry_bar'], favorable, 0.0), 6)
        r['mae_r_after_entry_bar'] = round(max(r['mae_r_after_entry_bar'], adverse, 0.0), 6)
        if hit_stop and (hit_tp1 or hit_tp2):
            r['status'] = 'ambiguous_stop_and_target'
            break
        if hit_tp1 and r['tp1_touch_ms'] is None:
            r['tp1_touch_ms'] = c.close_time
            r['status'] = 'tp1_observed_still_tracking'
        if hit_tp2:
            if r['tp1_touch_ms'] is None:
                r['tp1_touch_ms'] = c.close_time
            r['tp2_touch_ms'] = c.close_time
            r['status'] = 'tp2_observed'
            break
        if hit_stop:
            r['stop_touch_ms'] = c.close_time
            r['status'] = ('tp1_then_stop_observed' if r['tp1_touch_ms'] is not None
                           else 'stop_observed')
            break
    if r['status'] not in TERMINAL and r['next_open_ms'] >= r['observe_until_ms']:
        r['status'] = 'window_complete'
    return r


def report_counts(records: dict[str, dict]) -> dict[str, int]:
    """Status counts only: no fabricated win rate or realized financial PnL."""
    output: dict[str, int] = {}
    for r in records.values():
        if r.get('record_kind') != 'signal_level_observation_not_a_trade':
            continue
        status = r.get('status', 'unknown')
        output[status] = output.get(status, 0) + 1
    return output
