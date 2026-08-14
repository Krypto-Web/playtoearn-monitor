"""
PlayToEarn reward-restock monitor
----------------------------------
Polls the reward list (the same data that loads when you click "Redeem")
and pings a Discord webhook the moment a sold-out item becomes available.

Setup:
    pip install requests beautifulsoup4

Fill in the three values in the CONFIG section below, then run:
    python playtoearn_monitor.py --once --debug   # one check, prints raw HTML per item
    python playtoearn_monitor.py --once           # one check, clean summary only
    python playtoearn_monitor.py                  # loops forever, alerts on Discord
"""

import argparse
import json
import os
import time
from datetime import datetime

import requests
from bs4 import BeautifulSoup

# ================= CONFIG — fill these in =================

# The route the "Redeem" button opens. Confirmed from your screenshots as
# /earn/cashout. If this comes back 404, or returns a login page instead of
# the reward list, open DevTools > Network, click the request that actually
# loads the list, right-click it > Copy > Copy as cURL, and send that over
# so the URL/headers can be corrected.
CASHOUT_URL = "https://playtoearn.com/earn/cashout"

# DevTools > Network > click the request that loads the reward list >
# Headers tab > Request Headers > "cookie:" > copy the ENTIRE value.
# Read from an environment variable (set as a GitHub Actions secret) so the
# real value never has to be typed into this file. For local testing you can
# temporarily replace the os.environ.get(...) call with the string directly.
COOKIE_STRING = os.environ.get("PLAYTOEARN_COOKIE", "PASTE_YOUR_COOKIE_HEADER_HERE")

# Discord: Server Settings > Integrations > Webhooks > New Webhook > Copy URL
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "PASTE_YOUR_DISCORD_WEBHOOK_URL_HERE")

CHECK_INTERVAL_SECONDS = 300  # 5 minutes. Don't go much lower than ~60s.
STATE_FILE = "playtoearn_state.json"

# =============================================================

HEADERS = {
    "Cookie": COOKIE_STRING,
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "X-Requested-With": "XMLHttpRequest",
}


def fetch_rewards(debug=False):
    resp = requests.get(CASHOUT_URL, headers=HEADERS, timeout=15)

    if resp.status_code in (401, 403):
        snippet = resp.text[:300].replace("\n", " ").replace("\r", " ").strip()
        looks_like_bot_block = (
            "cf-mitigated" in resp.headers
            or "Just a moment" in resp.text
            or "Attention Required" in resp.text
            or "cf-chl" in resp.text
        )
        if looks_like_bot_block:
            hint = (
                "This looks like a bot-protection challenge page, not a login "
                "problem — the site may be blocking requests from GitHub's "
                "servers specifically, regardless of the cookie."
            )
        else:
            hint = (
                "This looks like a login/cookie problem rather than a bot "
                "block — the cookie is likely stale, incomplete, or wasn't "
                "saved correctly."
            )
        raise RuntimeError(
            f"Got HTTP {resp.status_code}. {hint}\nResponse snippet: {snippet!r}"
        )
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")
    nodes = soup.select(".__CashoutItem")

    if not nodes:
        raise RuntimeError(
            "No .__CashoutItem elements found in the response. Either the "
            "URL is wrong, the cookie is invalid/expired, or this route needs "
            "a different header to return the reward list instead of a full "
            "page/login redirect. Run with --debug to inspect what came back."
        )

    items = []
    for i, el in enumerate(nodes):
        classes = el.get("class", [])
        avail_el = el.select_one(".__Available")
        title_el = el.select_one(".__ItemBody")

        item = {
            "id": f"item_{i}",
            "title": title_el.get_text(strip=True) if title_el else f"Unnamed item #{i}",
            "available_raw": avail_el.get_text(strip=True) if avail_el else "?",
            "sold_out": "IsSoldOut" in classes,
            "locked": "IsLocked" in classes,
        }
        items.append(item)

        if debug:
            print(f"--- item {i} ---")
            print(el.prettify()[:800])
            print()

    return items


def load_previous_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    return {}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def send_discord_alert(message):
    try:
        r = requests.post(DISCORD_WEBHOOK_URL, json={"content": message}, timeout=10)
        r.raise_for_status()
    except requests.RequestException as e:
        print(f"[{datetime.now():%H:%M:%S}] Failed to send Discord alert: {e}")


def check_once(debug=False, alert=True):
    items = fetch_rewards(debug=debug)
    prev_state = load_previous_state()
    new_state = {}

    for item in items:
        key = item["title"]
        new_state[key] = item

        # Anything not seen before counts as "was sold out," so a
        # currently-available item will also alert on the very first run.
        # Remove the ", True" default below if you'd rather the first run
        # just silently record a baseline instead.
        was_sold_out = prev_state.get(key, {}).get("sold_out", True)

        status = "SOLD OUT" if item["sold_out"] else "AVAILABLE"
        print(f"[{datetime.now():%H:%M:%S}] {item['title']:<20} {item['available_raw']:<8} {status}")

        if alert and was_sold_out and not item["sold_out"]:
            send_discord_alert(
                f":bell: **{item['title']}** just became available! ({item['available_raw']})"
            )

    save_state(new_state)


def main():
    parser = argparse.ArgumentParser(description="Monitor PlayToEarn reward restocks")
    parser.add_argument("--once", action="store_true", help="Check once and exit")
    parser.add_argument("--debug", action="store_true", help="Print raw HTML for each item found")
    args = parser.parse_args()

    if args.once:
        check_once(debug=args.debug)
        return

    print(f"Watching for restocks every {CHECK_INTERVAL_SECONDS}s. Ctrl+C to stop.")
    while True:
        try:
            check_once(debug=args.debug)
        except Exception as e:
            print(f"[{datetime.now():%H:%M:%S}] Error: {e}")
        time.sleep(CHECK_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
