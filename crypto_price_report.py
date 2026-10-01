"""
Hourly forex & metals reporter -> Discord + Telegram
-------------------------------------------------------
Fetches current quotes, % change, an RSI-based momentum read, an
illustrative TP/SL when RSI flags an extreme, and a price chart, for a list
of forex pairs and metals from Twelve Data's API. Posts the summary + chart
image to Discord (with @everyone / personal ping) and, if configured,
Telegram too.

Requires a free API key: twelvedata.com -> sign up -> API key on your
dashboard. The free tier has a requests-per-minute cap, which is why calls
are paced with a delay between them.

Setup:
    pip install requests matplotlib
"""

import io
import json
import os
import re
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import requests

# ================= CONFIG =================

# Twelve Data symbols (BASE/QUOTE) on the left, display label on the right.
# Add/remove freely - symbol list at twelvedata.com/symbolsearch
PAIRS = {
    "XAU/USD": "Gold (XAU)",
    "XAG/USD": "Silver (XAG)",
    "EUR/USD": "EUR/USD",
    "GBP/USD": "GBP/USD",
    "USD/JPY": "USD/JPY",
    "AUD/USD": "AUD/USD",
    "USD/CAD": "USD/CAD",
    "USD/CHF": "USD/CHF",
}

CHART_DAYS = 30
RSI_PERIOD = 14
BETWEEN_CALLS_SECONDS = 8   # Twelve Data's free tier has a per-minute cap

# Illustrative TP/SL, shown only when RSI is at an extreme. Forex/metals
# move far less per day than crypto, so these are deliberately tighter
# than the crypto version was. Fixed 2:1 reward:risk - not a forecast.
SL_PCT = 0.005   # 0.5% adverse move
TP_PCT = 0.01    # 1% favorable move

# Africa/Lagos = WAT = UTC+1 year-round, no DST to worry about.
LOCAL_TZ = ZoneInfo("Africa/Lagos")
LOCAL_TZ_LABEL = "WAT"

# Discord: Settings > Advanced > Developer Mode, then right-click your name
# in any server > Copy User ID. Leave blank to skip the personal ping.
PING_USER_ID = ""

DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "PASTE_YOUR_DISCORD_WEBHOOK_URL_HERE")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
TWELVEDATA_API_KEY = os.environ.get("TWELVEDATA_API_KEY", "PASTE_YOUR_TWELVEDATA_KEY_HERE")

# ============================================

QUOTE_URL = "https://api.twelvedata.com/quote"
TIME_SERIES_URL = "https://api.twelvedata.com/time_series"


def _get_with_retry(url, params, max_retries=5, base_delay=10):
    """GET with exponential backoff. Twelve Data sometimes signals rate
    limiting via HTTP 429, sometimes via a 200 response with an error body -
    checking for both rather than trusting just the status code."""
    delay = base_delay
    for attempt in range(max_retries):
        resp = requests.get(url, params=params, timeout=15)
        data = None
        try:
            data = resp.json()
        except ValueError:
            pass

        is_rate_limited = resp.status_code == 429 or (
            isinstance(data, dict) and data.get("code") == 429
        )
        if is_rate_limited:
            if attempt == max_retries - 1:
                break
            print(f"Rate limited, waiting {delay}s (retry {attempt + 1}/{max_retries})...")
            time.sleep(delay)
            delay *= 2
            continue

        if isinstance(data, dict) and data.get("status") == "error":
            raise RuntimeError(f"Twelve Data error: {data.get('message', data)}")

        resp.raise_for_status()
        return data

    raise RuntimeError(
        "Still rate-limited by Twelve Data after several retries. Try "
        "increasing BETWEEN_CALLS_SECONDS or reducing the number of pairs."
    )


def fetch_quote(symbol):
    return _get_with_retry(QUOTE_URL, {"symbol": symbol, "apikey": TWELVEDATA_API_KEY})


def fetch_history(symbol, days=CHART_DAYS):
    data = _get_with_retry(
        TIME_SERIES_URL,
        {"symbol": symbol, "interval": "1day", "outputsize": days, "apikey": TWELVEDATA_API_KEY},
    )
    values = data.get("values", []) if isinstance(data, dict) else []
    prices = [float(v["close"]) for v in values if "close" in v]
    prices.reverse()  # Twelve Data returns newest-first; RSI needs oldest-first
    return prices


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
    reward:risk off current price - not a forecast, not advice."""
    if rsi is None or price is None:
        return None
    if rsi <= 30:
        return {"direction": "possible long (oversold)", "entry": price,
                 "sl": price * (1 - SL_PCT), "tp": price * (1 + TP_PCT)}
    if rsi >= 70:
        return {"direction": "possible short (overbought)", "entry": price,
                 "sl": price * (1 + SL_PCT), "tp": price * (1 - TP_PCT)}
    return None


def format_price(n):
    if n is None:
        return "?"
    if n >= 10:
        return f"{n:,.2f}"
    return f"{n:.5f}"


def build_message(quotes, rsi_by_symbol):
    now_utc = datetime.now(timezone.utc)
    now_local = now_utc.astimezone(LOCAL_TZ)
    time_str = (
        f"{now_local.strftime('%Y-%m-%d %H:%M')} {LOCAL_TZ_LABEL}"
        f"  /  {now_utc.strftime('%H:%M')} UTC"
    )

    mentions = ["@everyone"]
    if PING_USER_ID:
        mentions.append(f"<@{PING_USER_ID}>")

    lines = [" ".join(mentions), "", f"**Forex & Metals Update** \u2014 {time_str}", ""]

    for symbol, label in PAIRS.items():
        q = quotes.get(symbol)
        if not q:
            lines.append(f"{label}: no data")
            lines.append("")
            continue

        price = q.get("price")
        change = q.get("percent_change")
        rsi = rsi_by_symbol.get(symbol)

        arrow = "\U0001F7E2\u25b2" if (change or 0) >= 0 else "\U0001F534\u25bc"
        change_str = f"{change:+.2f}%" if change is not None else "?"

        lines.append(
            f"**{label}**: {format_price(price)}  {arrow} {change_str}  "
            f"| RSI(14d): {rsi_label(rsi)}"
        )

        levels = compute_trade_levels(price, rsi)
        if levels:
            lines.append(
                f"   \u21b3 {levels['direction']} \u2014 entry ~{format_price(levels['entry'])}, "
                f"TP {format_price(levels['tp'])}, SL {format_price(levels['sl'])} "
                f"(illustrative 2:1, not advice)"
            )

        lines.append("")

    lines.append(
        "_RSI is a mechanical momentum indicator; TP/SL above is a fixed "
        "illustrative 2:1 ratio off current price, not a forecast or "
        "personalized advice. Not financial advice._"
    )
    return "\n".join(lines)


def split_message(message, limit=1900):
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


def build_chart(history_by_symbol):
    n = len(history_by_symbol)
    cols = 3
    rows = (n + cols - 1) // cols
    fig, axes = plt.subplots(rows, cols, figsize=(5 * cols, 3 * rows))
    axes = axes.flatten() if hasattr(axes, "flatten") else [axes]

    for ax, (symbol, prices) in zip(axes, history_by_symbol.items()):
        label = PAIRS.get(symbol, symbol)
        ax.plot(prices, linewidth=1.5)
        ax.set_title(f"{label} \u00b7 {CHART_DAYS}d")
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
    chart_buf.seek(0)
    chunks = split_message(message)
    payload = {"content": chunks[0], "allowed_mentions": {"parse": ["everyone", "users"]}}
    files = {"file": ("forex_chart.png", chart_buf, "image/png")}
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


def _discord_to_telegram_html(text):
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"_(.+?)_", r"<i>\1</i>", text)
    return text


def send_to_telegram(message, chart_buf):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram not configured - skipping.")
        return
    base_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
    html_message = _discord_to_telegram_html(message)
    chart_buf.seek(0)
    resp = requests.post(
        f"{base_url}/sendPhoto",
        data={"chat_id": TELEGRAM_CHAT_ID},
        files={"photo": ("forex_chart.png", chart_buf, "image/png")},
        timeout=30,
    )
    resp.raise_for_status()
    resp = requests.post(
        f"{base_url}/sendMessage",
        data={"chat_id": TELEGRAM_CHAT_ID, "text": html_message,
              "parse_mode": "HTML", "disable_web_page_preview": "true"},
        timeout=15,
    )
    resp.raise_for_status()


def main():
    symbols = list(PAIRS.keys())

    quotes = {}
    history_by_symbol = {}
    rsi_by_symbol = {}

    for i, symbol in enumerate(symbols):
        q = fetch_quote(symbol)
        price = float(q["close"]) if isinstance(q, dict) and "close" in q else None
        change = (
            float(q["percent_change"])
            if isinstance(q, dict) and q.get("percent_change") not in (None, "")
            else None
        )
        quotes[symbol] = {"price": price, "percent_change": change}

        time.sleep(BETWEEN_CALLS_SECONDS)

        prices = fetch_history(symbol)
        history_by_symbol[symbol] = prices
        rsi_by_symbol[symbol] = compute_rsi(prices)

        if i < len(symbols) - 1:
            time.sleep(BETWEEN_CALLS_SECONDS)

    message = build_message(quotes, rsi_by_symbol)
    chart_buf = build_chart(history_by_symbol)
    print(message)

    try:
        send_to_discord(message, chart_buf)
        print("Sent to Discord.")
    except Exception as e:
        print(f"Discord send failed: {e}")

    try:
        send_to_telegram(message, chart_buf)
    except Exception as e:
        print(f"Telegram send failed: {e}")


if __name__ == "__main__":
    main()
