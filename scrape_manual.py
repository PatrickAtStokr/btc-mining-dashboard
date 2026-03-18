"""
Automated scraper for STOKR Mining Intelligence Dashboard — manual_data.json.
Replaces the daily screenshot workflow entirely.

Sources:
  1. BMN2 Dashboard (https://bmn2.mining.blockstream.com/) — Playwright intercepts
     XHR/fetch API calls the page makes, capturing clean JSON directly.
     → mined_per_token_btc, btc_price, hashprice_usd, bmn_total_hashrate_eh,
       bmn_circulating, bmn_value_per_token, bmn_term_day, bmn_days_remaining

     BMN2 Value formula (from dashboard):
       value = (mined_per_token × btc_price) + (days_remaining × hashprice_per_ph_per_day)

  2. Luxor Pool API (https://api.luxor.tech/graphql) — GraphQL with API key
     → bmn_hashrate_5m_eh, bmn_hashrate_24h_eh, bmn_active_miners,
       bmn_uptime_pct, bmn_revenue_btc
     Set as GitHub Secrets: LUXOR_API_KEY, LUXOR_SUBACCOUNT

  3. STRC / Strategy preferred stock (Nasdaq: STRC) — yfinance
     → strc_price, strc_dividend_pct, strc_notional_m, strc_vol_30d_m

Usage:
    python scrape_manual.py              # run all scrapers
    python scrape_manual.py --skip-luxor # skip Luxor if no API key yet
"""

import json
import os
import re
import sys
import time
import urllib.request
import ssl
from datetime import datetime, timezone, date

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
MANUAL_FILE = os.path.join(SCRIPT_DIR, "manual_data.json")

SSL_CTX = ssl.create_default_context()
try:
    import certifi
    SSL_CTX.load_verify_locations(certifi.where())
except Exception:
    SSL_CTX = ssl._create_unverified_context()

TODAY = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
SKIP_LUXOR = "--skip-luxor" in sys.argv

# BMN2 term constants (fixed: Sept 5 2024 → Sept 4 2028, 1460 days total)
BMN2_START = date(2024, 9, 5)
BMN2_END   = date(2028, 9, 4)
BMN2_TOTAL_DAYS = 1460


# ── BMN2 Dashboard (Playwright + network interception) ───────────────────────

def scrape_bmn2():
    """
    Load bmn2.mining.blockstream.com with Playwright and intercept the API
    calls the page makes. This gives us clean JSON without fragile DOM parsing.
    Falls back to DOM text extraction if no API calls are captured.
    """
    print("  Loading BMN2 dashboard and intercepting API calls...")
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("  ✗ Playwright not installed.")
        return {}

    captured = []   # will hold all JSON responses captured from XHR/fetch

    def handle_response(response):
        """Capture all JSON responses the page receives."""
        try:
            ct = response.headers.get("content-type", "")
            if "json" in ct and response.status == 200:
                url = response.url
                # Skip tiny responses and known irrelevant endpoints
                body = response.body()
                if len(body) > 20:
                    try:
                        data = json.loads(body)
                        captured.append({"url": url, "data": data})
                        print(f"    [API] {url[:80]} ({len(body)} bytes)")
                    except Exception:
                        pass
        except Exception:
            pass

    result = {}
    page_text = ""

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.on("response", handle_response)
        page.goto("https://bmn2.mining.blockstream.com/", wait_until="networkidle", timeout=60000)
        page.wait_for_timeout(5000)   # let deferred requests finish
        page_text = page.inner_text("body")
        browser.close()

    print(f"  Captured {len(captured)} JSON API responses")

    # ── Try to extract from captured API responses first ──────────────────────
    # Flatten all JSON data into a searchable dict
    def deep_find(obj, keys, depth=0):
        """Recursively search for keys in nested dicts/lists."""
        found = {}
        if depth > 6:
            return found
        if isinstance(obj, dict):
            for k, v in obj.items():
                k_lower = k.lower().replace("_", "").replace("-", "")
                for target in keys:
                    if target in k_lower:
                        found[target] = (k, v)
                found.update(deep_find(v, keys, depth + 1))
        elif isinstance(obj, list):
            for item in obj:
                found.update(deep_find(item, keys, depth + 1))
        return found

    search_keys = ["mined", "hashprice", "hashrate", "circulating", "btcprice",
                   "currentbtc", "termday", "dayselapsed", "totaldays", "value"]

    all_api_data = {}
    for cap in captured:
        hits = deep_find(cap["data"], search_keys)
        for k, (orig_key, val) in hits.items():
            if val is not None:
                all_api_data[orig_key] = val

    if all_api_data:
        print(f"  API keys found: {list(all_api_data.keys())[:20]}")

    # ── Parse page text as fallback / complement ──────────────────────────────
    def parse_float(pattern, text, label):
        m = re.search(pattern, text, re.IGNORECASE | re.DOTALL)
        if m:
            val = float(m.group(1).replace(",", ""))
            print(f"    [DOM] {label}: {val}")
            return val
        return None

    # Mined per BMN2 token
    v = parse_float(r"Mined per BMN2 token[\s\S]{0,30}?([\d]+\.[\d]+)\s*BTC", page_text, "Mined/token")
    if v: result["bmn_mined_per_token_btc"] = v

    # BTC price
    v = parse_float(r"Current BTC price[\s\S]{0,10}\$([\d,]+\.?\d*)\s*USD", page_text, "BTC Price")
    if v: result["btc_price"] = v

    # Hashprice
    v = parse_float(r"Current Hashprice[\s\S]{0,10}\$([\d.]+)\s*per\s*Ph", page_text, "Hashprice")
    if v: result["hashprice_usd"] = v

    # Total Hashrate
    v = parse_float(r"Total Hashrate[\s\S]{0,10}([\d.]+)\s*Eh/s", page_text, "Total Hashrate")
    if v: result["bmn_total_hashrate_eh"] = v

    # Circulating BMN2
    v = parse_float(r"Total Circulating BMN2[\s\S]{0,10}([\d,]+\.?\d*)", page_text, "Circulating")
    if v: result["bmn_circulating"] = v

    # Current Value per BMN2
    v = parse_float(r"Current Value per BMN2[\s\S]{0,30}\$([\d,]+)", page_text, "Value/BMN2")
    if v: result["bmn_value_per_token_usd"] = v

    # ── Compute BMN2 term progress ────────────────────────────────────────────
    today_date = date.today()
    days_elapsed = (today_date - BMN2_START).days
    days_remaining = (BMN2_END - today_date).days
    result["bmn_term_day"] = days_elapsed
    result["bmn_days_remaining"] = days_remaining
    print(f"    [CALC] Term: Day {days_elapsed} of {BMN2_TOTAL_DAYS} ({days_remaining} remaining)")

    # ── Compute BMN2 Value using official formula ─────────────────────────────
    # BMN2 Value = (mined_per_token × btc_price) + (days_remaining × hashprice_per_ph)
    mined  = result.get("bmn_mined_per_token_btc")
    price  = result.get("btc_price")
    hp     = result.get("hashprice_usd")

    if mined and price and hp and days_remaining:
        btc_value     = mined * price
        forward_value = days_remaining * hp
        bmn2_value    = round(btc_value + forward_value, 2)
        result["bmn_value_per_token_usd"] = bmn2_value
        print(f"    [CALC] BMN2 Value: ${bmn2_value:,.2f} = (₿{mined} × ${price:,.0f}) + ({days_remaining}d × ${hp})")
    elif result.get("bmn_value_per_token_usd"):
        print(f"    [DOM]  BMN2 Value: ${result['bmn_value_per_token_usd']:,.2f} (from page)")

    if not result:
        print("  ✗ Could not extract any values")
    else:
        print(f"  ✓ BMN2: {len(result)} fields")

    return result


# ── Luxor Pool API (GraphQL) ─────────────────────────────────────────────────

def fetch_luxor():
    """Query Luxor Mining Pool GraphQL API for BMN pool stats."""
    api_key    = os.environ.get("LUXOR_API_KEY", "")
    subaccount = os.environ.get("LUXOR_SUBACCOUNT", "")

    if not api_key or not subaccount:
        print("  ⚠ LUXOR_API_KEY or LUXOR_SUBACCOUNT not set — skipping Luxor")
        return {}

    print(f"  Querying Luxor API for subaccount '{subaccount}'...")

    query = """
    query {
        getMiningSummary(mpn: BTC, userName: "%s") {
            hashrate5m
            hashrate1hr
            hashrate24hr
            activeWorkers
            revenue24hr
            uptimePercentage
        }
    }
    """ % subaccount

    result = {}
    try:
        payload = json.dumps({"query": query}).encode("utf-8")
        req = urllib.request.Request(
            "https://api.luxor.tech/graphql",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "x-lux-api-key": api_key,
                "User-Agent": "STOKR-Mining-Intel/1.0",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30, context=SSL_CTX) as resp:
            data = json.loads(resp.read().decode())

        summary = data.get("data", {}).get("getMiningSummary", {})
        if summary:
            if summary.get("hashrate5m"):
                result["bmn_hashrate_5m_eh"]  = round(summary["hashrate5m"]  / 1e18, 3)
            if summary.get("hashrate24hr"):
                result["bmn_hashrate_24h_eh"] = round(summary["hashrate24hr"] / 1e18, 3)
            if summary.get("activeWorkers") is not None:
                result["bmn_active_miners"]   = summary["activeWorkers"]
            if summary.get("uptimePercentage") is not None:
                result["bmn_uptime_pct"]      = round(summary["uptimePercentage"], 2)
            if summary.get("revenue24hr"):
                result["bmn_revenue_btc"]     = round(summary["revenue24hr"] / 1e8, 8)
            print(f"  ✓ Luxor: {len(result)} fields")
            for k, v in result.items():
                print(f"    {k}: {v}")
        else:
            print(f"  ✗ Luxor returned empty. Response: {str(data)[:300]}")

    except Exception as e:
        print(f"  ✗ Luxor error: {e}")

    return result


# ── STRC / Strategy Preferred Stock (yfinance) ───────────────────────────────

def fetch_strc():
    """Fetch STRC stock data from Yahoo Finance via yfinance."""
    print("  Fetching STRC (Nasdaq) via yfinance...")
    try:
        import yfinance as yf
    except ImportError:
        print("  ✗ yfinance not installed.")
        return {}

    result = {}
    try:
        ticker = yf.Ticker("STRC")
        info   = ticker.info

        price = info.get("currentPrice") or info.get("regularMarketPrice") or info.get("previousClose")
        if price:
            result["strc_price"] = round(float(price), 2)

        div_rate = info.get("dividendRate")
        if div_rate:
            result["strc_dividend_pct"] = round(float(div_rate), 2)
        else:
            div_yield = info.get("dividendYield")
            if div_yield:
                result["strc_dividend_pct"] = round(float(div_yield) * 100, 2)

        market_cap = info.get("marketCap")
        if market_cap:
            result["strc_notional_m"] = round(market_cap / 1e6, 1)

        avg_vol = info.get("averageVolume")
        if avg_vol and price:
            result["strc_vol_30d_m"] = round((avg_vol * float(price)) / 1e6, 1)

        for k, v in result.items():
            print(f"    {k}: {v}")
        print(f"  ✓ STRC: {len(result)} fields")

    except Exception as e:
        print(f"  ✗ STRC error: {e}")

    return result


# ── Merge & Write ─────────────────────────────────────────────────────────────

def merge_into_manual_data(bmn2_data, luxor_data, strc_data):
    """Merge scraped data into manual_data.json, updating today's entry."""

    if os.path.exists(MANUAL_FILE):
        with open(MANUAL_FILE, "r") as f:
            manual = json.load(f)
    else:
        manual = {"updated": TODAY, "signal": "", "data": [], "hashprice_history": []}

    existing_dates = {e["date"] for e in manual["data"]}
    is_update = TODAY in existing_dates

    # Start from yesterday's entry as base (carry forward stale fields)
    entry = {}
    if manual["data"]:
        entry = {k: v for k, v in manual["data"][-1].items()}
    entry["date"] = TODAY

    # Layer BMN2 data
    for key in ["btc_price", "hashprice_usd", "bmn_mined_per_token_btc",
                "bmn_total_hashrate_eh", "bmn_circulating",
                "bmn_value_per_token_usd", "bmn_term_day", "bmn_days_remaining"]:
        if key in bmn2_data:
            entry[key] = bmn2_data[key]

    # Derive hashprice_btc
    if entry.get("hashprice_usd") and entry.get("btc_price") and entry["btc_price"] > 0:
        entry["hashprice_btc"] = round(entry["hashprice_usd"] / entry["btc_price"], 5)

    # Layer Luxor data
    for key in ["bmn_hashrate_5m_eh", "bmn_hashrate_24h_eh", "bmn_active_miners",
                "bmn_uptime_pct", "bmn_revenue_btc"]:
        if key in luxor_data:
            entry[key] = luxor_data[key]

    # Layer STRC data
    for key in ["strc_price", "strc_dividend_pct", "strc_notional_m", "strc_vol_30d_m"]:
        if key in strc_data:
            entry[key] = strc_data[key]

    # Network data from data.json (difficulty, hashrate)
    data_json_path = os.path.join(SCRIPT_DIR, "data.json")
    if os.path.exists(data_json_path):
        with open(data_json_path, "r") as f:
            auto = json.load(f)
        if auto.get("network_history"):
            latest_net = auto["network_history"][-1]
            if "difficulty_t" in latest_net:
                entry["difficulty"] = latest_net["difficulty_t"]
            if "network_hashrate_eh" in latest_net:
                entry["network_hashrate_eh"] = latest_net["network_hashrate_eh"]

    if is_update:
        for i, e in enumerate(manual["data"]):
            if e["date"] == TODAY:
                manual["data"][i] = entry
                print(f"  Updated entry for {TODAY}")
                break
    else:
        manual["data"].append(entry)
        print(f"  Added new entry for {TODAY}")

    manual["updated"] = TODAY

    with open(MANUAL_FILE, "w") as f:
        json.dump(manual, f, separators=(",", ":"))

    size_kb = os.path.getsize(MANUAL_FILE) / 1024
    print(f"  Saved {size_kb:.0f} KB — {len(manual['data'])} entries total")
    return entry


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print(f"=== STOKR Mining Dashboard — Data Scraper ===")
    print(f"Date: {TODAY}\n")

    print("[1/3] BMN2 Dashboard (Playwright + network intercept):")
    bmn2_data = scrape_bmn2()
    print()

    print("[2/3] Luxor Pool API (GraphQL):")
    luxor_data = {} if SKIP_LUXOR else fetch_luxor()
    if SKIP_LUXOR:
        print("  Skipped (--skip-luxor)")
    print()

    print("[3/3] STRC / Strategy Preferred (yfinance):")
    strc_data = fetch_strc()
    print()

    print("Merging into manual_data.json:")
    entry = merge_into_manual_data(bmn2_data, luxor_data, strc_data)

    print(f"\n=== Summary for {TODAY} ===")
    for k, v in sorted(entry.items()):
        print(f"  {k}: {v}")

    expected = ["btc_price", "hashprice_btc", "hashprice_usd", "difficulty",
                "network_hashrate_eh", "bmn_hashrate_5m_eh", "bmn_hashrate_24h_eh",
                "bmn_active_miners", "bmn_uptime_pct", "bmn_revenue_btc",
                "bmn_mined_per_token_btc", "bmn_value_per_token_usd",
                "bmn_term_day", "bmn_days_remaining",
                "strc_price", "strc_dividend_pct", "strc_notional_m", "strc_vol_30d_m"]
    missing = [k for k in expected if not entry.get(k)]
    if missing:
        print(f"\n  ⚠ Missing: {', '.join(missing)}")
    else:
        print(f"\n  ✓ All fields populated")


if __name__ == "__main__":
    main()
