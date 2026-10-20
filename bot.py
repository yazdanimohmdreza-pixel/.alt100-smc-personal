"""Telegram research alerts for 100 liquid USDT altcoins; no exchange orders."""
from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

from strategy import closed_candles, make_signal, Signal
from quality import MarketQuality, evaluate_quote, tick_size, format_tick_price

DATA_URL = os.getenv('BINANCE_DATA_URL', 'https://data-api.binance.vision').rstrip('/')
TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', '').strip()
CHAT_ID = os.getenv('TELEGRAM_CHAT_ID', '').strip()
MIN_SCORE = int(os.getenv('SIGNAL_MIN_SCORE', '75'))
MAX_COINS = min(max(int(os.getenv('MAX_COINS', '100')), 1), 100)
MAX_ALERTS = min(max(int(os.getenv('MAX_ALERTS_PER_SCAN', '8')), 1), 20)
STATE_FILE = Path(os.getenv('STATE_FILE', 'state/signals.json'))
LOG = logging.getLogger('alt100')
EXCLUDE = {
    'BTC', 'USDC', 'FDUSD', 'TUSD', 'USDP', 'USDD', 'USDE', 'DAI', 'PAX', 'BUSD',
    'EUR', 'EURI', 'TRY', 'BRL', 'ARS', 'GBP', 'JPY', 'AUD', 'UAH', 'RUB',
    'WBTC', 'WBETH', 'WETH',
}


async def api_get(client: httpx.AsyncClient, path: str, params: dict | None = None):
    for retry in range(4):
        try:
            r = await client.get(DATA_URL + path, params=params, timeout=18)
            if r.status_code in {418, 429, 500, 502, 503, 504}:
                delay = min(float(r.headers.get('Retry-After', '0')) or 2 ** retry, 45)
                await asyncio.sleep(delay)
                continue
            r.raise_for_status()
            return r.json()
        except (httpx.TransportError, httpx.HTTPStatusError, ValueError) as e:
            if retry == 3:
                raise RuntimeError(f'API {path}: {e}') from e
            await asyncio.sleep(1.5 * 2 ** retry)
    raise RuntimeError(f'API {path}: repeated rate limits')


def eligible(base: str) -> bool:
    if base in EXCLUDE or base.startswith(('USD', 'EUR')):
        return False
    if len(base) > 3 and base.endswith(('UP', 'DOWN', 'BULL', 'BEAR')):
        return False
    return True


def choose_universe(info: dict, tickers: list, n: int = 100) -> list[tuple[str, float]]:
    allowed = {
        x['symbol'] for x in info.get('symbols', [])
        if x.get('status') == 'TRADING' and x.get('quoteAsset') == 'USDT'
        and x.get('isSpotTradingAllowed', True)
        and eligible(x.get('baseAsset', ''))
    }
    ranked = []
    for item in tickers:
        symbol = item.get('symbol', '')
        if symbol not in allowed:
            continue
        try:
            vol = float(item.get('quoteVolume', 0))
        except (TypeError, ValueError):
            continue
        if vol >= 1_000_000:
            ranked.append((symbol, vol))
    ranked.sort(key=lambda x: x[1], reverse=True)
    return ranked[:n]


def pretty_price(num: float) -> str:
    if num >= 100:
        return f'{num:,.2f}'
    if num >= 1:
        return f'{num:,.4f}'
    if num >= 0.01:
        return f'{num:,.6f}'
    return f'{num:,.9f}'


def format_signal(s: Signal, quote: MarketQuality | None = None,
                  step: str | None = None) -> str:
    """Concise, actionable research card without pretending to know a fill price.

    BUY is a conditional spot limit plan. SELL_WARNING is never a short call.
    """
    date = datetime.fromtimestamp(s.candle_close / 1000, timezone.utc).strftime('%d %b %Y • %H:%M UTC')
    reasons = '\n'.join('  • ' + html.escape(reason) for reason in s.reasons[:4])
    price = lambda x: format_tick_price(x, step)
    zone = (f'{price(s.entry_zone_low)} – {price(s.entry_zone_high)}'
            if s.entry_zone_low and s.entry_zone_high else 'تعریف نشده')
    if s.direction == 'BUY':
        headline = '🟢 <b>ALT100 | فرصت خرید اسپات</b>'
        levels = (f'🎯 <b>سفارش شرطی روی پولبک:</b> <code>{price(s.limit_entry or s.entry)}</code> USDT\n'
                  f'⛔ <b>ابطال / حد ضرر:</b> <code>{price(s.stop)}</code>\n'
                  f'✅ <b>هدف اول:</b> <code>{price(s.tp1)}</code> (1.5R نسبت به ورود شرطی)\n'
                  f'✅ <b>هدف دوم:</b> <code>{price(s.tp2)}</code> (2.5R نسبت به ورود شرطی)\n')
        caveat = 'ورود فقط در صورت پولبک و برقرارماندن ستاپ؛ احتمال پرنشدن سفارش وجود دارد.'
    else:
        headline = '🔴 <b>ALT100 | هشدار ضعف اسپات</b>'
        levels = (f'🔔 <b>قیمت تأیید هشدار:</b> <code>{price(s.entry)}</code> USDT\n'
                  f'⛔ <b>ابطال دیدگاه نزولی:</b> <code>{price(s.stop)}</code>\n'
                  f'📉 <b>سطح نزولی اول:</b> <code>{price(s.tp1)}</code>\n'
                  f'📉 <b>سطح نزولی دوم:</b> <code>{price(s.tp2)}</code>\n')
        caveat = 'این هشدار، دستور شورت/فیوچرز یا توصیه فروش قطعی نیست.'
    cost = (f' | اسپرد: {quote.spread_pct:.2f}% | هزینه فرضی رفت‌وبرگشت: '
            f'{quote.assumed_roundtrip_cost_pct:.2f}%' if quote else '')
    return (
        f'{headline}\n'
        f'🪙 <b>{html.escape(s.symbol)}</b> | 1H ➜ 15M ➜ 5M\n'
        f'🕒 <b>بسته‌شدن تریگر:</b> {date}\n'
        f'🧩 <b>ستاپ:</b> {html.escape(s.setup)}\n\n'
        f'📍 <b>ناحیه ۱۵دقیقه:</b> <code>{zone}</code> USDT\n'
        f'⚡ <b>قیمت تأیید ۵دقیقه:</b> <code>{price(s.entry)}</code>\n'
        f'{levels}'
        f'📏 <b>فاصله ابطال:</b> {s.risk_pct:.2f}%'
        f'{cost}\n'
        f'📊 <b>امتیاز انطباق شروط:</b> {s.score}/100 (نه احتمال برد)\n\n'
        f'<b>چرا این هشدار صادر شد؟</b>\n{reasons}\n\n'
        f'⏳ <b>انقضا:</b> حداکثر ۱۰ دقیقه بعد از بسته‌شدن کندل تریگر.\n'
        f'⚠️ {caveat} کارمزد/اسلیپیج واقعی متغیر است؛ ربات معامله نمی‌کند.'
    )


async def telegram_send(client: httpx.AsyncClient, chat_id: str, message: str) -> bool:
    # Never log HTTP exception URLs: Telegram Bot tokens are embedded in URLs.
    try:
        response = await client.post(
            f'https://api.telegram.org/bot{TOKEN}/sendMessage',
            json={'chat_id': chat_id, 'text': message, 'parse_mode': 'HTML',
                  'disable_web_page_preview': True}, timeout=25,
        )
        response.raise_for_status()
        body = response.json()
    except httpx.HTTPStatusError as exc:
        raise RuntimeError(f'Telegram send failed: HTTP {exc.response.status_code}') from None
    except (httpx.TransportError, ValueError):
        raise RuntimeError('Telegram send failed: network/response error') from None
    if not body.get('ok', False):
        raise RuntimeError('Telegram returned non-OK response')
    return True


def delivery_key(sig: Signal) -> str:
    """Owner-only: stable setup ID, never saves Telegram identifiers."""
    return f'{sig.symbol}|{sig.direction}|{sig.setup_id or sig.candle_close}'


def load_state() -> dict:
    if not STATE_FILE.exists():
        return {'started': False, 'sent': {}}
    try:
        data = json.loads(STATE_FILE.read_text(encoding='utf-8'))
        if not isinstance(data, dict) or not isinstance(data.get('sent', {}), dict):
            raise ValueError('state format invalid')
        return data
    except (ValueError, OSError) as exc:
        raise RuntimeError(f'State unreadable; refusing to send duplicates: {exc}') from exc


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    temp = STATE_FILE.with_suffix('.tmp')
    temp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temp.replace(STATE_FILE)


async def analyse_one(client: httpx.AsyncClient, sem: asyncio.Semaphore,
                      symbol: str, vol: float, now_ms: int):
    async with sem:
        try:
            raw_fifteen, raw_hour, raw_five = await asyncio.gather(
                api_get(client, '/api/v3/klines', {'symbol': symbol, 'interval': '15m', 'limit': 140}),
                api_get(client, '/api/v3/klines', {'symbol': symbol, 'interval': '1h', 'limit': 140}),
                api_get(client, '/api/v3/klines', {'symbol': symbol, 'interval': '5m', 'limit': 140}),
            )
            fifteen = closed_candles(raw_fifteen, now_ms)
            hourly = closed_candles(raw_hour, now_ms)
            five = closed_candles(raw_five, now_ms)
            if not five or not fifteen or not hourly:
                return symbol, None, 'no closed candles'
            # Refuse missing time intervals / trading halts rather than letting
            # price gaps masquerade as BOS, FVG or displacement.
            for series, interval in ((five, 300_000), (fifteen, 900_000),
                                     (hourly, 3_600_000)):
                if len(series) < 65 or any(b.open_time - a.open_time != interval
                                          for a, b in zip(series[-65:-1], series[-64:])):
                    return symbol, None, 'gapped candle history'
            # A delayed job must never disguise stale higher-TF context as live.
            if not (0 <= now_ms - five[-1].close_time <= 11*60_000 and
                    0 <= now_ms - fifteen[-1].close_time <= 20*60_000 and
                    0 <= now_ms - hourly[-1].close_time <= 70*60_000):
                return symbol, None, 'stale candles on one or more timeframes'
            return symbol, make_signal(symbol, fifteen, hourly, five, vol, MIN_SCORE), None
        except Exception as exc:
            LOG.warning('%s: %s', symbol, exc)
            return symbol, None, str(exc)


async def main():
    if not TOKEN or not CHAT_ID:
        raise SystemExit('Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in GitHub Secrets.')
    if MIN_SCORE < 0 or MIN_SCORE > 100:
        raise SystemExit('SIGNAL_MIN_SCORE must be between 0 and 100')
    started_at = time.time()
    now_ms = int(started_at * 1000)
    state = load_state()
    keep_after = now_ms - 7 * 86_400_000
    state['sent'] = {k: v for k, v in state.get('sent', {}).items()
                     if isinstance(v, int) and v >= keep_after}
    limits = httpx.Limits(max_connections=12, max_keepalive_connections=12)
    async with httpx.AsyncClient(limits=limits, headers={'User-Agent': 'ALT100-SMC-Personal/1.0'}) as client:
        info, tickers = await asyncio.gather(
            api_get(client, '/api/v3/exchangeInfo'),
            api_get(client, '/api/v3/ticker/24hr'),
        )
        symbol_info = {x.get('symbol'): x for x in info.get('symbols', [])}
        pairs = choose_universe(info, tickers, MAX_COINS)
        if len(pairs) < 50:
            raise RuntimeError(f'Only {len(pairs)} liquid altcoin pairs; no sends to avoid misleading coverage.')
        LOG.info('Universe of %s altcoins selected', len(pairs))
        sem = asyncio.Semaphore(4)
        observations = await asyncio.gather(*[
            analyse_one(client, sem, symbol, vol, now_ms) for symbol, vol in pairs
        ])
        errors = [(sym, err) for sym, _, err in observations if err]
        if len(errors) > len(pairs) // 3:
            raise RuntimeError(f'Price feed unreliable: {len(errors)}/{len(pairs)} failed; no signals sent.')
        raw_candidates = [s for _, s, _ in observations if s is not None]
        # Fetch bid/ask AFTER the scan, not before 300 candle requests.
        # This matters for five-minute scalp validity.
        books = await api_get(client, '/api/v3/ticker/bookTicker')
        if not isinstance(books, list):
            raise RuntimeError('Bid/ask quotes unavailable; refusing to send unverified scalp alerts')
        book_by_symbol = {x.get('symbol'): x for x in books if isinstance(x, dict)}
        # Real-time marketability gate: quote spread, drift and assumed cost-to-risk.
        # Failing to verify a quote suppresses that alert, never fabricates a spread.
        checked = []
        quote_check_at = int(time.time() * 1000)
        for signal in raw_candidates:
            quality = evaluate_quote(signal, book_by_symbol.get(signal.symbol), quote_check_at)
            step = tick_size(symbol_info.get(signal.symbol))
            if quality is None or step is None:
                continue
            checked.append((signal, quality, step))
        checked.sort(key=lambda item: (item[0].score, item[0].volume_ratio), reverse=True)
        signals = [item[0] for item in checked]
        quality_by_signal = {s: (q, step) for s, q, step in checked}
        to_send = [s for s in signals if delivery_key(s) not in state['sent']]
        LOG.info('%s analyzed; %s candidates; %s new; %s errors',
                 len(pairs)-len(errors), len(signals), len(to_send), len(errors))
        if not state.get('started'):
            await telegram_send(client, CHAT_ID,
                f'✅ <b>ALT100 SMC Personal فعال شد</b>\n'
                f'اسکن {len(pairs)} آلت‌کوین: ساختار ۱ساعته، ناحیه ۱۵دقیقه و تأیید ۵دقیقه.\n'
                'فقط هنگام سیگنال، پیام ارسال می‌شود. هیچ معامله‌ای انجام نمی‌شود.')
            state['started'] = True
            save_state(state)
        count = 0
        for sig in to_send[:MAX_ALERTS]:
            quality, step = quality_by_signal[sig]
            # Re-check signal age after scanning the entire 100-coin market.
            if int(time.time() * 1000) - sig.candle_close > 10 * 60_000:
                LOG.info('%s alert expired before delivery', sig.symbol)
                continue
            key = delivery_key(sig)
            message = format_signal(sig, quality, step)
            await telegram_send(client, CHAT_ID, message)
            state['sent'][key] = sig.candle_close
            save_state(state)  # only mark as sent after Telegram acknowledged
            count += 1
            await asyncio.sleep(0.15)
        LOG.info('SENT=%s, seconds=%0.1f', count, time.time()-started_at)
        summary = os.environ.get('GITHUB_STEP_SUMMARY')
        if summary:
            with open(summary, 'a', encoding='utf-8') as f:
                f.write(f'## ALT100 SMC scan\n- Universe: {len(pairs)}\n'
                        f'- Successfully analysed: {len(pairs)-len(errors)}\n'
                        f'- Setups: {len(raw_candidates)}\n- Quote-approved: {len(signals)}\n- Messages sent: {count}\n'
                        f'- Errors: {len(errors)}\n')


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    asyncio.run(main())
