import os, json, time, hashlib, logging
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
SESSION.headers.update({"User-Agent": "cross-exchange-arbitrage-monitor/0.1"})

def dec(x):
    try:
        return D(str(x))
    except (InvalidOperation, TypeError, ValueError):
        return D("0")

def get_json(url, timeout=15):
    r = SESSION.get(url, timeout=timeout)
    r.raise_for_status()
    data = r.json()
    if not isinstance(data, dict):
        raise ValueError(f"Unexpected JSON from {url}")
    return data

def parse_wallex_levels(book, side):
    out = []
    for row in book.get(side, []) or []:
        p, q = dec(row.get("price")), dec(row.get("quantity"))
        if p > 0 and q > 0:
            out.append((p, q))
    return sorted(out, key=lambda x: x[0], reverse=(side == "bid"))

def parse_nobitex_levels(book, side):
    out = []
    key = "asks" if side == "ask" else "bids"
    for row in book.get(key, []) or []:
        if isinstance(row, (list, tuple)) and len(row) >= 2:
            p, q = dec(row[0]), dec(row[1])
            if p > 0 and q > 0:
                out.append((p, q))
    return sorted(out, key=lambda x: x[0], reverse=(side == "bid"))

def quote_info(code):
    c = code.upper()
    # Common rial/toman aliases. Values convert native quote price to rial.
    if c in {"IRT", "IRR", "RLS"}:
        return ("IRR", D("1"))
    if c in {"TMN", "TOMAN"}:
        return ("IRR", D("10"))
    return (c, D("1"))

def split_symbol(symbol, known_quotes):
    s = symbol.upper()
    for q in sorted(known_quotes, key=len, reverse=True):
        if s.endswith(q) and len(s) > len(q):
            return s[:-len(q)], q
    return None

def wallex_snapshot():
    markets = get_json(WALLEX_MARKETS)
    depth = get_json(WALLEX_DEPTH)
    symbols = markets.get("result", {}).get("symbols", {})
    books = depth.get("result", depth.get("data", {}))
    if not symbols or not books:
        raise ValueError("Wallex markets/depth response is empty or has changed schema")
    result = {}
    quotes = {str(v.get("quoteAsset", "")).upper() for v in symbols.values()}
    for symbol, meta in symbols.items():
        base = str(meta.get("baseAsset", "")).upper()
        quote = str(meta.get("quoteAsset", "")).upper()
        book = books.get(symbol)
        if not base or not quote or not book:
            continue
        qnorm, multiplier = quote_info(quote)
        result[(base, qnorm)] = {
            "venue": "Wallex", "symbol": symbol, "base": base, "quote": qnorm,
            "multiplier": multiplier, "asks": parse_wallex_levels(book, "ask"),
            "bids": parse_wallex_levels(book, "bid"),
            "received_at": time.time(), "meta": meta
        }
    return result

def nobitex_snapshot():
    books = get_json(NOBITEX_DEPTH)
    stats = get_json(NOBITEX_STATS)
    if books.get("status") != "ok":
        raise ValueError("Nobitex orderbook response status is not ok")
    stats_map = stats.get("stats", {})
    result = {}
    # Discover the symbol suffixes present in the current all-orderbook response.
    symbols = [k for k, v in books.items() if isinstance(v, dict) and k not in {"status", "global"}]
    quote_codes = {"IRT", "IRR", "RLS", "USDT", "USDC", "BTC", "ETH"}
    for symbol in symbols:
        pair = split_symbol(symbol, quote_codes)
        if not pair:
            continue
        base, quote_raw = pair
        qnorm, multiplier = quote_info(quote_raw)
        stat_key = f"{base.lower()}-{'rls' if quote_raw in {'IRT','IRR','RLS'} else quote_raw.lower()}"
        stat = stats_map.get(stat_key, {})
        book = books[symbol]
        # Nobitex lastUpdate is milliseconds since epoch. Reject stale snapshots later.
        last_update = dec(book.get("lastUpdate"))
        result[(base, qnorm)] = {
            "venue": "Nobitex", "symbol": symbol, "base": base, "quote": qnorm,
            "multiplier": multiplier, "asks": parse_nobitex_levels(book, "ask"),
            "bids": parse_nobitex_levels(book, "bid"),
            "received_at": time.time(),
            "exchange_ts": float(last_update / D("1000")) if last_update > 0 else None,
            "is_closed": stat.get("isClosed") is not False,
        }
    if not result:
        raise ValueError("No Nobitex markets could be parsed from /v3/orderbook/all")
    return result

def walk_depth(buy_asks, sell_bids, buy_mult, sell_mult, buy_fee, sell_fee, threshold):
    # Prices are normalized to a common quote unit (IRR or the same stablecoin).
    asks = [[p * buy_mult, q] for p, q in buy_asks]
    bids = [[p * sell_mult, q] for p, q in sell_bids]
    i = j = 0
    total_qty = total_cost = total_revenue = D("0")
    while i < len(asks) and j < len(bids):
        ap, aq = asks[i]
        bp, bq = bids[j]
        gross_pct = (bp / ap - D("1")) * D("100")
        net_per_unit = bp * (D("1") - sell_fee) - ap * (D("1") + buy_fee)
        # Only consume contiguous best levels that individually satisfy strict criteria.
        if gross_pct <= threshold or net_per_unit <= 0:
            break
        qty = min(aq, bq)
        total_qty += qty
        total_cost += ap * qty
        total_revenue += bp * qty
        asks[i][1] -= qty
        bids[j][1] -= qty
        if asks[i][1] <= 0: i += 1
        if bids[j][1] <= 0: j += 1
    if total_qty <= 0 or total_cost <= 0:
        return None
    gross_pct = (total_revenue / total_cost - D("1")) * D("100")
    net_profit = total_revenue * (D("1") - sell_fee) - total_cost * (D("1") + buy_fee)
    net_pct = net_profit / total_cost * D("100")
    return {
        "qty": total_qty, "buy_avg": total_cost / total_qty,
        "sell_avg": total_revenue / total_qty, "cost": total_cost,
        "revenue": total_revenue, "gross_pct": gross_pct,
        "net_profit": net_profit, "net_pct": net_pct,
    }

def load_state():
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}

def save_state(state):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

def send_telegram(text):
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        raise RuntimeError("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID secret")
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    r = SESSION.post(url, json={"chat_id": chat_id, "text": text, "disable_web_page_preview": True}, timeout=15)
    r.raise_for_status()
    payload = r.json()
    if not payload.get("ok"):
        raise RuntimeError(f"Telegram API error: {payload}")

def fee_env(venue):
    raw = os.getenv(f"{venue.upper()}_TAKER_FEE", "").strip()
    if not raw:
        return None
    val = dec(raw)
    # Enter fee as percent, e.g. 0.25 means 0.25%, not 25%.
    if val < 0 or val >= 10:
        raise ValueError(f"{venue}_TAKER_FEE must be a percentage between 0 and 10")
    return val / D("100")

def money(x):
    return f"{x:,.0f}"

def main():
    threshold = dec(os.getenv("MIN_GROSS_SPREAD_PCT", "2"))
    max_age = int(os.getenv("MAX_BOOK_AGE_SECONDS", "20"))
    max_capture_gap = int(os.getenv("MAX_CAPTURE_GAP_SECONDS", "8"))
    # Two public snapshots are requested concurrently to reduce time skew.
    with ThreadPoolExecutor(max_workers=2) as pool:
        fw = pool.submit(wallex_snapshot)
        fn = pool.submit(nobitex_snapshot)
        wallex, nobitex = fw.result(), fn.result()

    fee_w, fee_n = fee_env("WALLEX"), fee_env("NOBITEX")
    state = load_state()
    now = time.time()
    new_state = dict(state)
    alerts = []

    shared = sorted(set(wallex) & set(nobitex))
    for key in shared:
        w, n = wallex[key], nobitex[key]
        if not w["asks"] or not w["bids"] or not n["asks"] or not n["bids"]:
            continue
        if n.get("is_closed", False):
            continue
        if n.get("exchange_ts") and now - n["exchange_ts"] > max_age:
            continue
        if abs(w["received_at"] - n["received_at"]) > max_capture_gap:
            continue
        # If one market is rial-quoted and the other is USDT-quoted they do not match.
        # The key already enforces quote equality after alias normalization.
        for buy, sell in ((w, n), (n, w)):
            buy_fee = fee_w if buy["venue"] == "Wallex" else fee_n
            sell_fee = fee_w if sell["venue"] == "Wallex" else fee_n
            result = walk_depth(
                buy["asks"], sell["bids"], buy["multiplier"], sell["multiplier"],
                buy_fee or D("0"), sell_fee or D("0"), threshold
            )
            if not result or result["gross_pct"] <= threshold:
                continue
            fee_known = buy_fee is not None and sell_fee is not None
            net_positive = fee_known and result["net_profit"] > 0
            # Transfer/deposit availability is account-specific and not exposed by these public books.
            confirmed = bool(net_positive and os.getenv("TRANSFER_ROUTE_CONFIRMED", "").lower() == "true")
            route = f'{key[0]}|{key[1]}|{buy["venue"]}|{sell["venue"]}'
            payload = {
                "base": key[0], "quote": key[1], "buy": buy, "sell": sell,
                "result": result, "fee_known": fee_known, "net_positive": net_positive,
                "confirmed": confirmed, "checked_at": datetime.now(timezone.utc).isoformat()
            }
            old = state.get(route, {})
            old_seen = float(old.get("last_seen", 0))
            materially_changed = (
                not old or now - old_seen > 900 or
                abs(float(result["gross_pct"]) - float(old.get("gross_pct", 0))) >= 0.25 or
                abs(float(result["qty"]) - float(old.get("qty", 0))) / max(float(old.get("qty", 0)), 1e-12) >= 0.10
            )
            new_state[route] = {
                "last_seen": now, "gross_pct": float(result["gross_pct"]),
                "qty": float(result["qty"]), "net_profit": float(result["net_profit"])
            }
            if materially_changed:
                status = "تأییدنشده / غیرقابل اجرا" if not confirmed else "تأییدشده"
                unit = "ریال" if key[1] == "IRR" else key[1]
                fee_line = (
                    f'کارمزد خرید: {buy_fee*100:.4f}% | کارمزد فروش: {sell_fee*100:.4f}%'
                    if fee_known else "کارمزدها: تنظیم نشده؛ سود خالص قابل تأیید نیست"
                )
                net_line = (
                    f'سود خالص تخمینی: {money(result["net_profit"])} {unit} ({result["net_pct"]:.3f}%)'
                    if fee_known else "سود خالص تخمینی: نامشخص"
                )
                msg = (
                    f'فرصت آربیتراژ: {status}\n'
                    f'{key[0]} / {key[1]}\n'
                    f'خرید: {buy["venue"]} ({buy["symbol"]})\n'
                    f'فروش: {sell["venue"]} ({sell["symbol"]})\n'
                    f'داده بازار خرید: {("https://api.wallex.ir/v1/depth?symbol=" + buy["symbol"]) if buy["venue"] == "Wallex" else ("https://apiv2.nobitex.ir/v3/orderbook/" + buy["symbol"])}\n'
                    f'داده بازار فروش: {("https://api.wallex.ir/v1/depth?symbol=" + sell["symbol"]) if sell["venue"] == "Wallex" else ("https://apiv2.nobitex.ir/v3/orderbook/" + sell["symbol"])}\n'
                    f'میانگین خرید: {money(result["buy_avg"])} {unit}\n'
                    f'میانگین فروش: {money(result["sell_avg"])} {unit}\n'
                    f'اختلاف ناخالص: {result["gross_pct"]:.3f}% (شرط: بیشتر از {threshold}%)\n'
                    f'{net_line}\n{fee_line}\n'
                    f'حجم قابل اجرا در عمق فعلی: {result["qty"]} {key[0]}\n'
                    f'زمان ثبت بررسی: {payload["checked_at"]}\n'
                    'توجه: داده‌ها ممکن است پیش از اجرا تغییر کنند؛ این پیام تضمین سود یا دستور معامله نیست.'
                )
                alerts.append(msg)

    save_state(new_state)
    if alerts:
        for msg in alerts:
            send_telegram(msg)
    logging.info("Shared markets=%s | alerts=%s", len(shared), len(alerts))
    print(f"Completed. shared_markets={len(shared)} alerts={len(alerts)}")

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    main()
