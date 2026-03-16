"""
Automated scraper for STOKR Mining Intelligence Dashboard — manual_data.json.
Replaces the daily screenshot workflow.

Sources:
  1. BMN2 Dashboard (https://bmn2.mining.blockstream.com/) — Playwright headless browser
     → mined_per_token_btc, btc_price, hashprice_usd
  2. Luxor Pool API (https://api.luxor.tech/graphql) — GraphQL with API key
     → bmn_hashrate_5m_eh, bmn_hashrate_24h_eh, bmn_active_miners,
       bmn_uptime_pct, bmn_revenue_btc
  3. STRC / Strategy preferred stock (Nasdaq: STRC) — yfinance
     → strc_price, strc_dividend_pct, strc_notional_m, strc_vol_30d_m

Environment variables (set as GitHub Secrets):
  LUXOR_API_KEY       — Luxor pool API key (required for Luxor data)
  LUXOR_SUBACCOUNT    — Luxor subaccount name for BMN operations

Usage:
    python scrape_manual.py              # run all scrapers
    python scrape_manual.py --skip-luxor # skip Luxor if no API key yet
"""

import json
import os
import sys
import time
import urllib.request
import ssl
from datetime import datetime, timezone

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


# ── BMN2 Dashboard (Playwright) ─────────────────────────────────────────────

def scrape_bmn2():
    """Scrape bmn2.mining.blockstream.com with Playwright headless Chromium."""
    print("  Scraping BMN2 dashboard...")
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("  ✗ Playwright not installed. Run: pip install playwright && playwright install chromium")
        return {}

    result = {}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto("https://bmn2.mining.blockstream.com/", wait_until="networkidle", timeout=60000)

        # Wait for data to render (the page is a JS app)
        page.wait_for_timeout(5000)

        # Get full page text for parsing
        text = page.inner_text("body")
        browser.close()

    print(f"  Page text length: {len(text)} chars")

    # Parse values from page text
    import re

    # Mined per BMN2 token: "0.29005547 BTC"
    m = re.search(r"Mined per BMN2 token\s*[\n\r]+\s*([\d.]+)\s*BTC", text)
    if m:
        result["bmn_mined_per_token_btc"] = float(m.group(1))
        print(f"    Mined per BMN2: {result['bmn_mined_per_token_btc']} BTC")

    # Current BTC price: "$72,203.00 USD"
    m = re.search(r"Current BTC price\s*\$?([\d,]+\.?\d*)\s*USD", text)
    if m:
        result["btc_price"] = float(m.group(1).replace(",", ""))
        print(f"    BTC Price: ${result['btc_price']:,.2f}")

    # Current Hashprice: "$31.29 per Ph"
    m = re.search(r"Current Hashprice\s*\$?([\d.]+)\s*per\s*Ph", text)
    if m:
        result["hashprice_usd"] = float(m.group(1))
        print(f"    Hashprice: ${result['hashprice_usd']}/PH")

    # Total Hashrate: "24.9218 Eh/s"
    m = re.search(r"Total Hashrate\s*([\d.]+)\s*Eh/s", text)
    if m:
        result["bmn_total_hashrate_eh"] = float(m.group(1))
        print(f"    Total Hashrate: {result['bmn_total_hashrate_eh']} EH/s")

    # Total Circulating BMN2: "24,921.7815"
    m = re.search(r"Total Circulating BMN2\s*([\d,]+\.?\d*)", text)
    if m:
        result["bmn_circulating"] = float(m.group(1).replace(",", ""))
        print(f"    Circulating BMN2: {result['bmn_circulating']}")

    if not result:
        print("  ✗ Could not parse any values from BMN2 dashboard")
    else:
        print(f"  ✓ BMN2: {len(result)} fields scraped")

    return result


# ── Luxor Pool API (GraphQL) ────────────────────────────────────────────────

def fetch_luxor():
    """Query Luxor Mining Pool GraphQL API for BMN pool stats."""
    api_key = os.environ.get("LUXOR_API_KEY", "")
    subaccount = os.environ.get("LUXOR_SUBACCOUNT", "")

    if not api_key or not subaccount:
        print("  ⚠ LUXOR_API_KEY or LUXOR_SUBACCOUNT not set — skipping Luxor")
        return {}

    print(f"  Querying Luxor API for subaccount '{subaccount}'...")

    # Query 1: Mining summary (hashrate, revenue, active workers)
    query_summary = """
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
        payload = json.dumps({"query": query_summary}).encode("utf-8")
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
            # Hashrates come in H/s from the API — convert to EH/s
            if summary.get("hashrate5m"):
                result["bmn_hashrate_5m_eh"] = round(summary["hashrate5m"] / 1e18, 3)
            if summary.get("hashrate24hr"):
                result["bmn_hashrate_24h_eh"] = round(summary["hashrate24hr"] / 1e18, 3)
            if summary.get("activeWorkers"):
                result["bmn_active_miners"] = summary["activeWorkers"]
            if summary.get("uptimePercentage") is not None:
                result["bmn_uptime_pct"] = round(summary["uptimePercentage"], 2)
            if summary.get("revenue24hr"):
                # Revenue comes in satoshis — convert to BTC
                result["bmn_revenue_btc"] = round(summary["revenue24hr"] / 1e8, 8)

            print(f"  ✓ Luxor: {len(result)} fields fetched")
            for k, v in result.items():
                print(f"    {k}: {v}")
        else:
            print(f"  ✗ Luxor API returned empty summary. Response: {json.dumps(data)[:500]}")

    except Exception as e:
        print(f"  ✗ Luxor API error: {e}")

    return result


# ── STRC / Strategy Preferred Stock (yfinance) ──────────────────────────────

def fetch_strc():
    """Fetch STRC stock data from Yahoo Finance via yfinance."""
    print("  Fetching STRC data from Yahoo Finance...")
    try:
        import yfinance as yf
    except ImportError:
        print("  ✗ yfinance not installed. Run: pip install yfinance")
        return {}

    result = {}

    try:
        strc = yf.Ticker("STRC")
        info = strc.info

        # Current price
        price = info.get("currentPrice") or info.get("regularMarketPrice") or info.get("previousClose")
        if price:
            result["strc_price"] = round(price, 2)
            print(f"    Price: ${result['strc_price']}")

        # Dividend rate (annual %) — STRC is variable rate
        div_rate = info.get("dividendRate")
        if div_rate:
            # dividendRate is annual $ amount per share; convert to % of $100 par
            result["strc_dividend_pct"] = round(div_rate, 2)
            print(f"    Dividend Rate: {result['strc_dividend_pct']}%")
        else:
            # Try dividend yield
            div_yield = info.get("dividendYield")
            if div_yield:
                result["strc_dividend_pct"] = round(div_yield * 100, 2)
                print(f"    Dividend Yield: {result['strc_dividend_pct']}%")

        # Market cap → notional in $M
        market_cap = info.get("marketCap")
        if market_cap:
            result["strc_notional_m"] = round(market_cap / 1e6, 1)
            print(f"    Notional: ${result['strc_notional_m']}M")

        # 30-day average volume in $M
        avg_vol = info.get("averageVolume")
        if avg_vol and price:
            vol_30d_m = round((avg_vol * price) / 1e6, 1)
            result["strc_vol_30d_m"] = vol_30d_m
            print(f"    Avg Vol 30D: ${vol_30d_m}M")

        # BTC Rating: compute from Strategy's BTC holdings
        # Strategy (MSTR) holds BTC; BTC Rating ≈ (MSTR BTC per STRC share × BTC price) / STRC par
        # We'll try to get this from MSTR info
        try:
            mstr = yf.Ticker("MSTR")
            mstr_info = mstr.info
            # Strategy's total BTC holdings are not directly in yfinance
            # BTC Rating will be computed if we have the data, otherwise skipped
            print("    BTC Rating: requires manual input or MSTR BTC holdings data (skipped)")
        except Exception:
            pass

        if result:
            print(f"  ✓ STRC: {len(result)} fields fetched")
        else:
            print("  ✗ Could not fetch STRC data from Yahoo Finance")

    except Exception as e:
        print(f"  ✗ STRC fetch error: {e}")

    return result


# ── Merge & Write ────────────────────────────────────────────────────────────

def merge_into_manual_data(bmn2_data, luxor_data, strc_data):
    """Merge scraped data into manual_data.json as a new daily entry."""

    # Load existing manual_data.json
    if os.path.exists(MANUAL_FILE):
        with open(MANUAL_FILE, "r") as f:
            manual = json.load(f)
    else:
        manual = {"updated": TODAY, "signal": "", "data": [], "hashprice_history": []}

    # Check if today's entry already exists
    existing_dates = {e["date"] for e in manual["data"]}
    is_update = TODAY in existing_dates

    # Build today's entry from all sources
    entry = {}

    # Start with previous day's data as defaults (carry forward)
    if manual["data"]:
        prev = manual["data"][-1]
        entry = {k: v for k, v in prev.items()}
    entry["date"] = TODAY

    # Layer in BMN2 data
    if bmn2_data.get("btc_price"):
        entry["btc_price"] = bmn2_data["btc_price"]
    if bmn2_data.get("hashprice_usd"):
        entry["hashprice_usd"] = bmn2_data["hashprice_usd"]
        # Derive hashprice_btc
        if entry.get("btc_price") and entry["btc_price"] > 0:
            entry["hashprice_btc"] = round(bmn2_data["hashprice_usd"] / entry["btc_price"], 5)
    if bmn2_data.get("bmn_mined_per_token_btc"):
        entry["bmn_mined_per_token_btc"] = bmn2_data["bmn_mined_per_token_btc"]

    # Layer in Luxor data
    for key in ["bmn_hashrate_5m_eh", "bmn_hashrate_24h_eh", "bmn_active_miners",
                "bmn_uptime_pct", "bmn_revenue_btc"]:
        if key in luxor_data:
            entry[key] = luxor_data[key]

    # Layer in STRC data
    for key in ["strc_price", "strc_dividend_pct", "strc_notional_m", "strc_vol_30d_m"]:
        if key in strc_data:
            entry[key] = strc_data[key]

    # Network data (difficulty, hashrate) — carried from fetch_data.py / data.json
    # Read latest from data.json if available
    data_json_path = os.path.join(SCRIPT_DIR, "data.json")
    if os.path.exists(data_json_path):
        with open(data_json_path, "r") as f:
            auto_data = json.load(f)
        if auto_data.get("network_history"):
            latest_net = auto_data["network_history"][-1]
            if "difficulty_t" in latest_net:
                entry["difficulty"] = latest_net["difficulty_t"]
            if "network_hashrate_eh" in latest_net:
                entry["network_hashrate_eh"] = latest_net["network_hashrate_eh"]

    # Update or append
    if is_update:
        for i, e in enumerate(manual["data"]):
            if e["date"] == TODAY:
                manual["data"][i] = entry
                print(f"\n  Updated existing entry for {TODAY}")
                break
    else:
        manual["data"].append(entry)
        print(f"\n  Added new entry for {TODAY}")

    manual["updated"] = TODAY

    # Write back
    with open(MANUAL_FILE, "w") as f:
        json.dump(manual, f, separators=(",", ":"))

    size_kb = os.path.getsize(MANUAL_FILE) / 1024
    print(f"  Saved manual_data.json ({size_kb:.0f} KB, {len(manual['data'])} entries)")

    return entry


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    print(f"=== STOKR Mining Dashboard — Manual Data Scraper ===")
    print(f"Date: {TODAY}\n")

    # 1. Scrape BMN2 Dashboard
    print("[1/3] BMN2 Dashboard (Playwright):")
    bmn2_data = scrape_bmn2()
    print()

    # 2. Fetch Luxor Pool Data
    print("[2/3] Luxor Pool API:")
    if SKIP_LUXOR:
        print("  Skipped (--skip-luxor flag)")
        luxor_data = {}
    else:
        luxor_data = fetch_luxor()
    print()

    # 3. Fetch STRC Data
    print("[3/3] STRC / Strategy Preferred (Yahoo Finance):")
    strc_data = fetch_strc()
    print()

    # Merge into manual_data.json
    print("Merging into manual_data.json:")
    entry = merge_into_manual_data(bmn2_data, luxor_data, strc_data)

    # Summary
    print(f"\n=== Done ===")
    print(f"Entry for {TODAY}:")
    for k, v in sorted(entry.items()):
        print(f"  {k}: {v}")

    # Report what's still missing
    expected = ["btc_price", "hashprice_btc", "hashprice_usd", "difficulty",
                "network_hashrate_eh", "bmn_hashrate_5m_eh", "bmn_hashrate_24h_eh",
                "bmn_active_miners", "bmn_uptime_pct", "bmn_revenue_btc",
                "bmn_mined_per_token_btc", "strc_price", "strc_dividend_pct",
                "strc_btc_rating", "strc_notional_m", "strc_vol_30d_m"]
    missing = [k for k in expected if k not in entry or entry.get(k) is None]
    if missing:
        print(f"\n  ⚠ Still missing/manual: {', '.join(missing)}")


if __name__ == "__main__":
    main()
