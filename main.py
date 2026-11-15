import os
import json
import time
import logging
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import requests

WALLEX_MARKETS = "https://api.wallex.ir/v1/markets"
WALLEX_DEPTH = "https://api.wallex.ir/v2/depth/all"
NOBITEX_DEPTH = "https://apiv2.nobitex.ir/v3/orderbook/all"
NOBITEX_STATS = "https://apiv2.nobitex.ir/market/stats"

STATE_FILE = Path("state.json")
D = Decimal
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "wallex-nobitex-arbitrage-monitor/0.2"})


def dec(value):
    try:
        return D(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return D("0")


def get_json(url, timeout=20):
    response = SESSION.get(url, timeout=timeout)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError(f"Unexpected JSON object from {url}")
    return payload


def quote_info(code):
    """
    Normalize only quote currencies whose units are known.
    Internal fiat unit is TOMAN:
      IRT/TMN/TOMAN = toman
      IRR/RLS       = rial, converted to toman by /10
    Stablecoins and crypto quotes are kept separate.
    """
    code = str(code or "").upper()
    if code in {"IRT", "TMN", "TOMAN"}:
        return "TOMAN", D("1")
    if code in {"IRR", "RLS"}:
        return "TOMAN", D("0.1")
    if code:
        return code, D("1")
    return None, D("1")


def split_symbol(symbol, known_quotes):
    symbol = str(symbol).upper()
    for quote in sorted(known_quotes, key=len, reverse=True):
        if symbol.endswith(quote) and len(symbol) > len(quote):
            return symbol[:-len(quote)], quote
    return None


def parse_levels(book, side, venue):
    keys = ("asks", "ask") if side == "ask" else ("bids", "bid")
    rows = None
    for key in keys:
        if isinstance(book, dict) and key in book:
            rows = book.get(key)
            break
    if rows is None:
        raise ValueError(f"{venue} orderbook has no {side} field")

    levels = []
    for row in rows or []:
        if isinstance(row, dict):
            price = row.get("price", row.get("p"))
            quantity = row.get("quantity", row.get("qty", row.get("amount", row.get("q"))))
        elif isinstance(row, (list, tuple)) and len(row) >= 2:
            price, quantity = row[0], row[1]
        else:
            continue
        p, q = dec(price), dec(quantity)
        if p > 0 and q > 0:
            levels.append((p, q))

    return sorted(levels, key=lambda item: item[0], reverse=(side == "bid"))


def get_market_container(payload):
    # Wallex has used result.symbols; fail closed if response shape differs.
    result = payload.get("result")
    if isinstance(result, dict) and isinstance(result.get("symbols"), dict):
        return result["symbols"]
    data = payload.get("data")
    if isinstance(data, dict) and isinstance(data.get("symbols"), dict):
        return data["symbols"]
    raise ValueError("Wallex markets response schema not recognized")


def wallex_snapshot():
    markets_payload = get_json(WALLEX_MARKETS)
    depth_payload = get_json(WALLEX_DEPTH)
    symbols = get_market_container(markets_payload)

    books = depth_payload.get("result")
    if not isinstance(books, dict):
        books = depth_payload.get("data")
    if not isinstance(books, dict):
        raise ValueError("Wallex depth response schema not recognized")

    result = {}
    received = time.time()

    for symbol, meta in symbols.items():
        if not isinstance(meta, dict):
            continue
        base = str(meta.get("baseAsset") or "").upper()
        quote_raw = str(meta.get("quoteAsset") or "").upper()
        book = books.get(symbol)
        if not base or not quote_raw or not isinstance(book, dict):
            continue

        quote, multiplier = quote_info(quote_raw)
        if not quote:
            continue

        asks = parse_levels(book, "ask", "Wallex")
        bids = parse_levels(book, "bid", "Wallex")
        if not asks or not bids:
            continue

        # Avoid silently overwriting duplicate normalized markets.
        key = (base, quote)
        if key in result:
            logging.warning("Duplicate Wallex normalized market ignored: %s", key)
            continue

        result[key] = {
            "venue": "Wallex",
            "symbol": str(symbol),
            "base": base,
            "quote": quote,
            "multiplier": multiplier,
            "asks": asks,
            "bids": bids,
            "received_at": received,
            "exchange_ts": None,
        }

    if not result:
        raise ValueError("No Wallex orderbooks parsed; check current API schema")
    return result



def nobitex_snapshot():
    books_payload = get_json(NOBITEX_DEPTH)
    stats_payload = get_json(NOBITEX_STATS)

    if books_payload.get("status") != "ok":
        raise ValueError("Nobitex orderbook response status is not ok")

    stats_map = stats_payload.get("stats", {})
    if not isinstance(stats_map, dict):
        stats_map = {}

    result = {}
    received = time.time()

    known_quotes = {
        "IRT", "IRR", "RLS", "TMN", "TOMAN",
        "USDT", "USDC", "BTC", "ETH"
    }

    for symbol, book in books_payload.items():
        if symbol in {"status", "global"} or not isinstance(book, dict):
            continue

        pair = split_symbol(symbol, known_quotes)
        if not pair:
            continue

        base, quote_raw = pair
        quote, multiplier = quote_info(quote_raw)

        if not quote:
            continue

        # Keep the existing unit conversion pending API verification.
        if quote_raw == "IRT":
            multiplier = D("0.1")

        asks = parse_levels(book, "ask", "Nobitex")
        bids = parse_levels(book, "bid", "Nobitex")

        if not asks or not bids:
            continue

        last_update = dec(book.get("lastUpdate"))
        exchange_ts = (
            float(last_update / D("1000"))
            if last_update > 0 else None
        )

        stat_key = (
            f"{base.lower()}-"
            f"{'rls' if quote_raw in {'IRT', 'IRR', 'RLS', 'TMN', 'TOMAN'} else quote_raw.lower()}"
        )

        stat = stats_map.get(stat_key, {})
        is_closed = (
            stat.get("isClosed") is True
            if isinstance(stat, dict) else False
        )

        result[(base, quote)] = {
            "venue": "Nobitex",
            "symbol": str(symbol),
            "source_symbol": symbol,
            "base": base,
            "quote": quote,
            "multiplier": multiplier,
            "asks": asks,
            "bids": bids,
            "received_at": received,
            "exchange_ts": exchange_ts,
            "is_closed": is_closed,
        }

    if not result:
        raise ValueError("No Nobitex orderbooks parsed; check current API schema")

    return result



def walk_depth(buy_asks, sell_bids, buy_multiplier, sell_multiplier,
               buy_fee, sell_fee, min_spread_pct):

    asks = [
        [price * buy_multiplier, qty]
        for price, qty in buy_asks
        if price > 0 and qty > 0
    ]

    bids = [
        [price * sell_multiplier, qty]
        for price, qty in sell_bids
        if price > 0 and qty > 0
    ]

    i = j = 0
    total_qty = D("0")
    total_cost = D("0")
    total_revenue = D("0")

    while i < len(asks) and j < len(bids):
        ask_price, ask_qty = asks[i]
        bid_price, bid_qty = bids[j]

        if ask_price <= 0 or bid_price <= 0:
            break

        gross_pct_at_level = (
            bid_price / ask_price - D("1")
        ) * D("100")

        net_per_unit = (
            bid_price * (D("1") - sell_fee)
            - ask_price * (D("1") + buy_fee)
        )

        if gross_pct_at_level <= min_spread_pct:
            break

        if net_per_unit <= 0:
            break

        qty = min(ask_qty, bid_qty)

        if qty <= 0:
            break

        total_qty += qty
        total_cost += ask_price * qty
        total_revenue += bid_price * qty

        asks[i][1] -= qty
        bids[j][1] -= qty

        if asks[i][1] <= 0:
            i += 1

        if bids[j][1] <= 0:
            j += 1

    if total_qty <= 0 or total_cost <= 0:
        return None

    gross_pct = (
        total_revenue / total_cost - D("1")
    ) * D("100")

    net_profit = (
        total_revenue * (D("1") - sell_fee)
        - total_cost * (D("1") + buy_fee)
    )

    net_pct = net_profit / total_cost * D("100")

    return {
        "qty": total_qty,
        "buy_avg": total_cost / total_qty,
        "sell_avg": total_revenue / total_qty,
        "cost": total_cost,
        "revenue": total_revenue,
        "gross_pct": gross_pct,
        "net_profit": net_profit,
        "net_pct": net_pct,
    }

def fee_env(venue):
    raw = os.getenv(f"{venue.upper()}_TAKER_FEE", "").strip()
    if not raw:
        return None
    value = dec(raw)
    # User enters percentage: 0.25 means 0.25%.
    if value < 0 or value >= 10:
        raise ValueError(f"{venue}_TAKER_FEE must be between 0 and 10 percent")
    return value / D("100")


def confirmed_routes():
    """
    Explicit manually-reviewed routes, comma-separated:
    BTC|TOMAN|Wallex|Nobitex
    This is still a manual assertion, not live proof of deposit/withdrawal.
    """
    raw = os.getenv("TRANSFER_CONFIRMED_ROUTES", "")
    return {item.strip() for item in raw.split(",") if item.strip()}


def load_state():
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as exc:
        logging.warning("Could not read state file; starting fresh: %s", exc)
        return {}


def save_state(state):
    temporary = STATE_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(STATE_FILE)


def send_telegram(message):
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()

    if not token or not chat_id:
        raise RuntimeError(
            "Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID"
        )

    url = f"https://api.telegram.org/bot{token}/sendMessage"

    for attempt in range(5):
        # فاصله برای جلوگیری از محدودیت تعداد پیام
        time.sleep(1.2)

        response = SESSION.post(
            url,
            json={
                "chat_id": chat_id,
                "text": message,
                "disable_web_page_preview": True
            },
            timeout=30,
        )

        try:
            payload = response.json()
        except ValueError:
            payload = {}

        if response.status_code == 429:
            retry_after = (
                payload.get("parameters", {}).get("retry_after", 10)
            )

            logging.warning(
                "Telegram rate limit. Waiting %s seconds.",
                retry_after
            )

            if attempt < 4:
                time.sleep(int(retry_after) + 1)
                continue

            raise RuntimeError(
                "Telegram rate limit persisted after 5 attempts"
            )

        response.raise_for_status()

        if not payload.get("ok"):
            raise RuntimeError(
                f"Telegram API error: {payload}"
            )

        return

    raise RuntimeError("Telegram message could not be delivered")



def money(value):
    value = D(str(value))

    if value == 0:
        return "0.0000"

    if abs(value) < 100:
        return f"{value:,.4f}"

    return f"{value:,.2f}"


def market_url(market):
    # These are API data links, not guaranteed web trading-page URLs.
    if market["venue"] == "Wallex":
        return f'https://api.wallex.ir/v1/depth?symbol={market["symbol"]}'
    return f'https://apiv2.nobitex.ir/v3/orderbook/{market["symbol"]}'


def main():
    threshold = dec(os.getenv("MIN_GROSS_SPREAD_PCT", "2"))
    max_age = int(os.getenv("MAX_BOOK_AGE_SECONDS", "30"))
    max_capture_gap = int(os.getenv("MAX_CAPTURE_GAP_SECONDS", "10"))

    if threshold < 0:
        raise ValueError("MIN_GROSS_SPREAD_PCT cannot be negative")

    with ThreadPoolExecutor(max_workers=2) as executor:
        wallex_future = executor.submit(wallex_snapshot)
        nobitex_future = executor.submit(nobitex_snapshot)
        wallex = wallex_future.result()
        nobitex = nobitex_future.result()
    for market in wallex.values():
        market["venue"] = "Wallex"

    for market in nobitex.values():
        market["venue"] = "Nobitex"
    wallex_fee = fee_env("WALLEX")
    nobitex_fee = fee_env("NOBITEX")
    routes = confirmed_routes()

    state = load_state()
    new_state = dict(state)
    now = time.time()
    alerts = []

    shared = sorted(set(wallex) & set(nobitex))
    for key in shared:
        wallex_market = wallex[key]
        nobitex_market = nobitex[key]

        if nobitex_market.get("is_closed"):
            continue
        if abs(wallex_market["received_at"] - nobitex_market["received_at"]) > max_capture_gap:
            continue
        if nobitex_market.get("exchange_ts") is not None:
            if now - nobitex_market["exchange_ts"] > max_age:
                continue
        else:
            logging.warning("Nobitex timestamp missing for %s; opportunity cannot be confirmed", key)

        for buy, sell in ((wallex_market, nobitex_market), (nobitex_market, wallex_market)):
            buy_fee = wallex_fee if buy["venue"] == "Wallex" else nobitex_fee
            sell_fee = wallex_fee if sell["venue"] == "Wallex" else nobitex_fee

            # Unknown fees are never assumed to be zero for a positive-net claim.
            calculation_buy_fee = buy_fee if buy_fee is not None else D("0")
            calculation_sell_fee = sell_fee if sell_fee is not None else D("0")

            result = walk_depth(
                buy["asks"], sell["bids"], buy["multiplier"], sell["multiplier"],
                calculation_buy_fee, calculation_sell_fee, threshold
            )
            if not result or result["gross_pct"] <= threshold:
                continue

            fees_known = buy_fee is not None and sell_fee is not None
            net_positive = fees_known and result["net_profit"] > 0
            route = f'{key[0]}|{key[1]}|{buy["venue"]}|{sell["venue"]}'
            manually_reviewed = route in routes
            # A route is never labelled confirmed based only on public order books.
            confirmed = bool(net_positive and manually_reviewed)

            old = state.get(route, {})
            old_seen = float(old.get("last_seen", 0))
            materially_changed = (
                not old
                or now - old_seen > 900
                or abs(float(result["gross_pct"]) - float(old.get("gross_pct", 0))) >= 0.25
                or abs(float(result["qty"]) - float(old.get("qty", 0))) /
                   max(float(old.get("qty", 0)), 1e-12) >= 0.10
            )

            new_state[route] = {
                "last_seen": now,
                "gross_pct": float(result["gross_pct"]),
                "qty": float(result["qty"]),
                "net_profit": float(result["net_profit"]),
            }

            if not materially_changed:
                continue

            status = "تأییدنشده / نیازمند بررسی" if not confirmed else "تأییدشده طبق تأیید دستی مسیر"
            unit = "تومان" if key[1] == "TOMAN" else key[1]
            if fees_known:
                fee_text = (
                    f'کارمزد خرید: {buy_fee * 100:.4f}% | '
                    f'کارمزد فروش: {sell_fee * 100:.4f}%'
                )
                net_text = (
                    f'سود خالص تخمینی پس از کارمزد: '
                    f'{money(result["net_profit"])} {unit} ({result["net_pct"]:.3f}%)'
                )
            else:
                fee_text = "کارمزدها: نامشخص؛ سود خالص قابل تأیید نیست"
                net_text = "سود خالص: نامشخص (کارمزد هر دو صرافی تنظیم نشده است)"

            timestamp_quality = (
                "زمان دفتر سفارش نوبیتکس موجود است"
                if nobitex_market.get("exchange_ts") is not None
                else "زمان به‌روزرسانی دفتر سفارش نوبیتکس در پاسخ موجود نبود"
            )

            message = (
                f'فرصت اختلاف قیمت: {status}\n'
                f'{key[0]} / {key[1]}\n'
                f'خرید: {buy["venue"]} ({buy["symbol"]})\n'
                f'فروش: {sell["venue"]} ({sell["symbol"]})\n'
                f'لینک داده خرید: {market_url(buy)}\n'
                f'لینک داده فروش: {market_url(sell)}\n'
                f'میانگین قیمت خرید: {money(result["buy_avg"])} {unit}\n'
                f'میانگین قیمت فروش: {money(result["sell_avg"])} {unit}\n'
                f'اختلاف ناخالص قابل اجرا: {result["gross_pct"]:.3f}% '
                f'(شرط: بیشتر از {threshold}%)\n'
                f'{net_text}\n{fee_text}\n'
                f'حجم قابل اجرا در عمق فعلی: {result["qty"]} {key[0]}\n'
                f'وضعیت زمان داده: {timestamp_quality}\n'
                f'زمان بررسی: {datetime.now(timezone.utc).isoformat()}\n'
                'هشدار: این محاسبه تخمینی است؛ لغزش قیمت، محدودیت حساب، شبکه انتقال، '
                'کارمزد برداشت و تغییر دفتر سفارش می‌تواند نتیجه را عوض کند. '
                'این پیام دستور معامله یا تضمین سود نیست.'
            )
            alerts.append(message)

    # Send first, persist state afterwards. If Telegram fails, next run can retry.
    for alert in alerts:
        send_telegram(alert)

    save_state(new_state)
    logging.info("Wallex=%s Nobitex=%s shared=%s alerts=%s",
                 len(wallex), len(nobitex), len(shared), len(alerts))
    print(f"Completed. wallex={len(wallex)} nobitex={len(nobitex)} "
          f"shared_markets={len(shared)} alerts={len(alerts)}")


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s"
    )
    main()
