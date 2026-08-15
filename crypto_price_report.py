"""
Hourly crypto price + trend reporter -> Discord
-------------------------------------------------
Fetches current prices, 24h change, a simple RSI-based momentum read, and a
price chart for a list of coins from CoinGecko's free public API, then
posts a summary + chart image to a Discord webhook.

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

CHART_DAYS = 30    # days of history to chart and to compute RSI from
RSI_PERIOD = 14    # standard RSI lookback window

DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "PASTE_YOUR_DISCORD_WEBHOOK_URL_HERE")

# ============================================

SIMPLE_PRICE_URL = "https://api.coingecko.com/api/v3/simple/price"
MARKET_CHART_URL = "https://api.coingecko.com/api/v3/coins/{id}/market_chart"


def _check_response(resp):
    if resp.status_code in (401, 429):
        raise RuntimeError(
            f"Got HTTP {resp.status_code} from CoinGecko. If this keeps "
            "happening, they may now require a free API key for this "
            "endpoint - sign up at coingecko.com/api and we can add it "
            "as a header."
        )
    resp.raise_for_status()


def fetch_current(ids):
    params = {
        "ids": ",".join(ids),
        "vs_currencies": "usd",
        "include_24hr_change": "true",
        "include_market_cap": "true",
    }
    resp = requests.get(SIMPLE_PRICE_URL, params=params, timeout=15)
    _check_response(resp)
    return resp.json()


def fetch_history(coin_id, days=CHART_DAYS):
    resp = requests.get(
        MARKET_CHART_URL.format(id=coin_id),
        params={"vs_currency": "usd", "days": days},
        timeout=15,
    )
    _check_response(resp)
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
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [f"**Crypto Price Update** \u2014 {now}", ""]

    for coin_id, symbol in COINS.items():
        info = current_data.get(coin_id)
        if not info:
            lines.append(f"{symbol}: no data")
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

    lines.append("")
    lines.append(
        "_RSI is a mechanical momentum indicator, not a buy/sell call \u2014 "
        "\"overbought\"/\"oversold\" just describe recent price momentum, "
        "not a prediction. Not financial advice._"
    )
    return "\n".join(lines)


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
    payload = {"content": message}
    files = {"file": ("crypto_chart.png", chart_buf, "image/png")}
    data = {"payload_json": json.dumps(payload)}
    resp = requests.post(DISCORD_WEBHOOK_URL, data=data, files=files, timeout=30)
    resp.raise_for_status()


def main():
    ids = list(COINS.keys())

    current_data = fetch_current(ids)

    history_by_id = {}
    rsi_by_id = {}
    for coin_id in ids:
        prices = fetch_history(coin_id)
        history_by_id[coin_id] = prices
        rsi_by_id[coin_id] = compute_rsi(prices)
        time.sleep(1.2)  # be polite to the free API between calls

    message = build_message(current_data, rsi_by_id)
    chart_buf = build_chart(history_by_id)

    print(message)
    send_to_discord(message, chart_buf)


if __name__ == "__main__":
    main()
