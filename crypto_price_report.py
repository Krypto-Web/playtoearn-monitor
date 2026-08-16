"""
Hourly crypto price + trend reporter -> Discord
-------------------------------------------------
Fetches current prices, 24h change, a simple RSI-based momentum read, an
illustrative TP/SL when RSI flags an extreme, and a price chart, for a list
of coins from CoinGecko's free public API. Posts a formatted summary +
chart image to a Discord webhook, pinging @everyone and (optionally) one
specific person.

No login, no cookies, no account needed - this is a public API meant for
exactly this kind of use.

Setup:
    pip install requests matplotlib
"""

import io
import json
import os
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import matplotlib
matplotlib.use("Agg")  # no display available on a server/CI runner
import matplotlib.pyplot as plt
import requests

# ================= CONFIG =================

# CoinGecko coin IDs (not ticker symbols) on the left, display symbol on the
# right. Add/remove freely. Full ID list: api.coingecko.com/api/v3/coins/list
COINS = {
    "bitcoin": "BTC",
    "ethereum": "ETH",
    "solana": "SOL",
    "binancecoin": "BNB",
    "ripple": "XRP",
    "dogecoin": "DOGE",
    "cardano": "ADA",
    "polkadot": "DOT",
    "litecoin": "LTC",
    "chainlink": "LINK",
    "avalanche-2": "AVAX",
    "tron": "TRX",
    "shiba-inu": "SHIB",
}

CHART_DAYS = 30              # days of history to chart and to compute RSI from
RSI_PERIOD = 14               # standard RSI lookback window
BETWEEN_CALLS_SECONDS = 6     # pause between each coin's history request

# Illustrative TP/SL, shown only when RSI is at an extreme. Fixed 2:1
# reward:risk by default - not a prediction, just a mechanical reference.
SL_PCT = 0.02   # 2% adverse move
TP_PCT = 0.04   # 4% favorable move

# Africa/Lagos = WAT = UTC+1 year-round, no DST to worry about.
LOCAL_TZ = ZoneInfo("Africa/Lagos")
LOCAL_TZ_LABEL = "WAT"

# Discord: Settings > Advanced > enable Developer Mode, then right-click
# your name in any server > Copy User ID. Leave blank to skip the personal
# ping and only send @everyone.
PING_USER_ID = ""

DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "PASTE_YOUR_DISCORD_WEBHOOK_URL_HERE")

# ============================================

SIMPLE_PRICE_URL = "https://api.coingecko.com/api/v3/simple/price"
MARKET_CHART_URL = "https://api.coingecko.com/api/v3/coins/{id}/market_chart"


def _get_with_retry(url, params, max_retries=5, base_delay=8):
    """GET with exponential backoff specifically for CoinGecko's 429s."""
    delay = base_delay
    for attempt in range(max_retries):
        resp = requests.get(url, params=params, timeout=15)
        if resp.status_code == 429:
            if attempt == max_retries - 1:
                break
            print(f"Rate limited, waiting {delay}s (retry {attempt + 1}/{max_retries})...")
            time.sleep(delay)
            delay *= 2
            continue
        if resp.status_code == 401:
            raise RuntimeError(
                "Got HTTP 401 from CoinGecko - they may now require a free "
                "API key for this endpoint. Sign up at coingecko.com/api "
                "and we can add it as a header."
            )
        resp.raise_for_status()
        return resp

    raise RuntimeError(
        "Still rate-limited by CoinGecko after several retries. Try "
        "increasing BETWEEN_CALLS_SECONDS, reducing the number of coins, "
        "or getting a free API key at coingecko.com/api."
    )


def fetch_current(ids):
    params = {
        "ids": ",".join(ids),
        "vs_currencies": "usd",
        "include_24hr_change": "true",
        "include_market_cap": "true",
    }
    resp = _get_with_retry(SIMPLE_PRICE_URL, params)
    return resp.json()


def fetch_history(coin_id, days=CHART_DAYS):
    resp = _get_with_retry(
        MARKET_CHART_URL.format(id=coin_id),
        {"vs_currency": "usd", "days": days},
    )
    prices = resp.json().get("prices", [])
    return [p[1] for p in prices]


def compute_rsi(prices, period=RSI_PERIOD):
    if len(prices) < period + 1:
        return None

    deltas = [prices[i] - prices[i - 1] for i in range(1, len(prices))]
    gains = [d if d > 0 else 0 for d in deltas]
    losses = [-d if d < 0 else 0 for d in deltas]

    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period

    if avg_loss == 0:
        return 100.0

    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def rsi_label(rsi):
    if rsi is None:
        return "n/a"
    if rsi >= 70:
        return f"{rsi:.0f} \u00b7 overbought zone"
    if rsi <= 30:
        return f"{rsi:.0f} \u00b7 oversold zone"
    return f"{rsi:.0f} \u00b7 neutral"


def compute_trade_levels(price, rsi):
    """Illustrative TP/SL, only when RSI is at an extreme. Fixed 2:1
    reward:risk math off the current price - not a forecast, not advice."""
    if rsi is None or price is None:
        return None
    if rsi <= 30:
        return {"direction": "possible long (oversold)", "entry": price,
                 "sl": price * (1 - SL_PCT), "tp": price * (1 + TP_PCT)}
    if rsi >= 70:
        return {"direction": "possible short (overbought)", "entry": price,
                 "sl": price * (1 + SL_PCT), "tp": price * (1 - TP_PCT)}
    return None


def format_number(n):
    if n is None:
        return "?"
    if n >= 1_000_000_000:
        return f"{n / 1_000_000_000:.2f}B"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if n >= 1:
        return f"{n:,.2f}"
    return f"{n:.6f}"


def build_message(current_data, rsi_by_id):
    now_utc = datetime.now(timezone.utc)
    now_local = now_utc.astimezone(LOCAL_TZ)
    time_str = (
        f"{now_local.strftime('%Y-%m-%d %H:%M')} {LOCAL_TZ_LABEL}"
        f"  /  {now_utc.strftime('%H:%M')} UTC"
    )

    mentions = ["@everyone"]
    if PING_USER_ID:
        mentions.append(f"<@{PING_USER_ID}>")

    lines = [" ".join(mentions), "", f"**Crypto Price Update** \u2014 {time_str}", ""]

    for coin_id, symbol in COINS.items():
        info = current_data.get(coin_id)
        if not info:
            lines.append(f"{symbol}: no data")
            lines.append("")
            continue

        price = info.get("usd")
        change = info.get("usd_24h_change")
        cap = info.get("usd_market_cap")
        rsi = rsi_by_id.get(coin_id)

        arrow = "\U0001F7E2\u25b2" if (change or 0) >= 0 else "\U0001F534\u25bc"
        change_str = f"{change:+.2f}%" if change is not None else "?"

        lines.append(
            f"**{symbol}**: ${format_number(price)}  {arrow} {change_str}  "
            f"| cap ${format_number(cap)}  | RSI(14d): {rsi_label(rsi)}"
        )

        levels = compute_trade_levels(price, rsi)
        if levels:
            lines.append(
                f"   \u21b3 {levels['direction']} \u2014 entry ~${format_number(levels['entry'])}, "
                f"TP ${format_number(levels['tp'])}, SL ${format_number(levels['sl'])} "
                f"(illustrative 2:1, not advice)"
            )

        lines.append("")  # breathing room between coins

    lines.append(
        "_RSI is a mechanical momentum indicator; TP/SL above is a fixed "
        "illustrative 2:1 ratio off current price, not a forecast or "
        "personalized advice. Not financial advice._"
    )
    return "\n".join(lines)


def split_message(message, limit=1900):
    """Discord caps content at 2000 chars - split on the blank-line
    boundaries between coin blocks if we're ever over that."""
    if len(message) <= limit:
        return [message]
    parts = message.split("\n\n")
    chunks, current = [], ""
    for part in parts:
        candidate = (current + "\n\n" + part) if current else part
        if len(candidate) > limit and current:
            chunks.append(current)
            current = part
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


def build_chart(history_by_id):
    n = len(history_by_id)
    cols = 3
    rows = (n + cols - 1) // cols
    fig, axes = plt.subplots(rows, cols, figsize=(5 * cols, 3 * rows))
    axes = axes.flatten() if hasattr(axes, "flatten") else [axes]

    for ax, (coin_id, prices) in zip(axes, history_by_id.items()):
        symbol = COINS.get(coin_id, coin_id)
        ax.plot(prices, linewidth=1.5)
        ax.set_title(f"{symbol} \u00b7 {CHART_DAYS}d")
        ax.tick_params(labelbottom=False)
        ax.grid(alpha=0.3)

    for ax in axes[n:]:
        ax.axis("off")

    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=120)
    plt.close(fig)
    buf.seek(0)
    return buf


def send_to_discord(message, chart_buf):
    chunks = split_message(message)

    payload = {"content": chunks[0], "allowed_mentions": {"parse": ["everyone", "users"]}}
    files = {"file": ("crypto_chart.png", chart_buf, "image/png")}
    data = {"payload_json": json.dumps(payload)}
    resp = requests.post(DISCORD_WEBHOOK_URL, data=data, files=files, timeout=30)
    resp.raise_for_status()

    for chunk in chunks[1:]:
        resp = requests.post(
            DISCORD_WEBHOOK_URL,
            json={"content": chunk, "allowed_mentions": {"parse": []}},
            timeout=15,
        )
        resp.raise_for_status()


def main():
    ids = list(COINS.keys())

    current_data = fetch_current(ids)

    history_by_id = {}
    rsi_by_id = {}
    for i, coin_id in enumerate(ids):
        prices = fetch_history(coin_id)
        history_by_id[coin_id] = prices
        rsi_by_id[coin_id] = compute_rsi(prices)
        if i < len(ids) - 1:
            time.sleep(BETWEEN_CALLS_SECONDS)

    message = build_message(current_data, rsi_by_id)
    chart_buf = build_chart(history_by_id)

    print(message)
    send_to_discord(message, chart_buf)


if __name__ == "__main__":
    main()
