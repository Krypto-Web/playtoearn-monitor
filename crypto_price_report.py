"""
Hourly crypto price reporter -> Discord
-----------------------------------------
Fetches current prices + 24h change for a list of coins from CoinGecko's
free public API and posts a formatted summary to a Discord webhook.

No login, no cookies, no account needed - this is a public API meant for
exactly this kind of use.

Setup:
    pip install requests
"""

import os
from datetime import datetime, timezone

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
}

DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "PASTE_YOUR_DISCORD_WEBHOOK_URL_HERE")

# ============================================

API_URL = "https://api.coingecko.com/api/v3/simple/price"


def fetch_prices():
    params = {
        "ids": ",".join(COINS.keys()),
        "vs_currencies": "usd",
        "include_24hr_change": "true",
        "include_market_cap": "true",
    }
    resp = requests.get(API_URL, params=params, timeout=15)

    if resp.status_code in (401, 429):
        raise RuntimeError(
            f"Got HTTP {resp.status_code} from CoinGecko. If this keeps happening, "
            "they may now require a free API key for this endpoint - sign up at "
            "coingecko.com/api and we can add it as a header."
        )
    resp.raise_for_status()
    return resp.json()


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


def build_message(data):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [f"**Crypto Price Update** — {now}", ""]

    for coin_id, symbol in COINS.items():
        info = data.get(coin_id)
        if not info:
            lines.append(f"{symbol}: no data")
            continue

        price = info.get("usd")
        change = info.get("usd_24h_change")
        cap = info.get("usd_market_cap")

        arrow = "🟢▲" if (change or 0) >= 0 else "🔴▼"
        change_str = f"{change:+.2f}%" if change is not None else "?"

        lines.append(
            f"**{symbol}**: ${format_number(price)}  {arrow} {change_str}  "
            f"(cap: ${format_number(cap)})"
        )

    return "\n".join(lines)


def send_to_discord(message):
    resp = requests.post(DISCORD_WEBHOOK_URL, json={"content": message}, timeout=10)
    resp.raise_for_status()


def main():
    data = fetch_prices()
    message = build_message(data)
    print(message)
    send_to_discord(message)


if __name__ == "__main__":
    main()
