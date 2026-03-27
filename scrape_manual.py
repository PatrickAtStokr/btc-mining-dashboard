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


# ── Luxor Pool Data ──────────────────────────────────────────────────────────

# BMN subaccount IDs from the public Luxor watcher view (watcherV2.getKpi)
BMN_SUBACCOUNT_IDS = [
    1159344, 1164940, 1165207, 1166386, 1166705, 1168281, 1169205, 1170074,
    1170223, 1170298, 1171775, 1171859, 1173258, 1173990, 1174106, 1174442,
    1174461, 1174471
]

def fetch_luxor():
    """
    Fetch Luxor BMN pool stats via the public tRPC watcher API.

    Replicates the exact call the Luxor watcher dashboard makes:
    POST https://app.luxor.tech/api/trpc/watcherV2.getKpi?batch=1
    with kpiType=5 (summary KPIs) — no auth required (workspaceId is empty).

    Subaccount IDs are fixed for the BMN pool.
    """
    import time as _time

    print("  [Luxor] Calling watcher tRPC API (kpiType=5 summary)...")

    now = int(_time.time())

    payload = {
        "0": {
            "json": {
                "currencyProfile": 1,
                "workspaceId": "",
                "kpiType": 5,
                "subaccounts": {
                    "ids": BMN_SUBACCOUNT_IDS,
                    "names": []
                }
            }
        }
    }

    result = {}
    try:
        req = urllib.request.Request(
            "https://app.luxor.tech/api/trpc/watcherV2.getKpi?batch=1",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Origin": "https://app.luxor.tech",
                "Referer": "https://app.luxor.tech/",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30, context=SSL_CTX) as resp:
            raw = resp.read().decode()

        print(f"  [Luxor] Raw response (first 800 chars): {raw[:800]}")

        data = json.loads(raw)

        # tRPC batch response is a list: [{result: {data: {json: ...}}}]
        if isinstance(data, list) and data:
            inner = data[0].get("result", {}).get("data", {})
            # Handle tRPC v10 envelope
            if "json" in inner:
                inner = inner["json"]

            print(f"  [Luxor] Parsed inner keys: {list(inner.keys()) if isinstance(inner, dict) else type(inner)}")

            # Extract fields — print everything so we can see the structure
            if isinstance(inner, dict):
                for k, v in inner.items():
                    print(f"  [Luxor]   {k}: {v}")

                # Common field name patterns from Luxor tRPC responses
                def _get(*keys):
                    for k in keys:
                        if k in inner and inner[k] is not None:
                            return inner[k]
                    return None

                hr5m = _get("hashrate5m", "hashrate5Min", "fiveMinHashrate", "averageHashrate5m")
                if hr5m is not None:
                    result["bmn_hashrate_5m_eh"] = round(float(hr5m) / 1e18, 3)

                hr24 = _get("hashrate24hr", "hashrate24h", "averageHashrate24h", "dailyHashrate")
                if hr24 is not None:
                    result["bmn_hashrate_24h_eh"] = round(float(hr24) / 1e18, 3)

                workers = _get("activeWorkers", "workers", "onlineWorkers", "totalWorkers")
                if workers is not None:
                    result["bmn_active_miners"] = int(workers)

                uptime = _get("uptimePercentage", "uptime", "uptimePct")
                if uptime is not None:
                    result["bmn_uptime_pct"] = round(float(uptime), 2)

                rev = _get("revenue24hr", "revenue", "dailyRevenue", "btcRevenue")
                if rev is not None:
                    # Revenue may be in satoshis or BTC depending on currencyProfile
                    rev_f = float(rev)
                    result["bmn_revenue_btc"] = round(rev_f / 1e8, 8) if rev_f > 1000 else round(rev_f, 8)

    except Exception as e:
        print(f"  [Luxor] tRPC call failed: {type(e).__name__}: {e}")

    if result:
        print(f"  ✓ Luxor: {len(result)} fields")
        for k, v in result.items():
            print(f"    {k}: {v}")
    else:
        print("  ✗ Luxor: no data extracted")

    return result


# ── STRC / Strategy Preferred Stock ──────────────────────────────────────────

def fetch_strc_shares_outstanding():
    """
    Fetch STRC preferred shares outstanding from SEC EDGAR XBRL API.
    Strategy (formerly MicroStrategy) CIK: 0001050446

    The EDGAR companyfacts endpoint returns all XBRL-tagged financial data.
    We look for PreferredStockSharesOutstanding and filter for STRC-specific
    entries (Series A Perpetual Strife Preferred Stock).

    Returns: int (shares outstanding) or None if unavailable.
    """
    print("  [STRC] Fetching shares outstanding from SEC EDGAR...")
    try:
        # SEC EDGAR requires a User-Agent with contact info
        req = urllib.request.Request(
            "https://data.sec.gov/api/xbrl/companyfacts/CIK0001050446.json",
            headers={
                "User-Agent": "STOKR-Mining-Intel/1.0 (patrick@stokr.io)",
                "Accept": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=30, context=SSL_CTX) as resp:
            data = json.loads(resp.read().decode())

        usgaap = data.get("facts", {}).get("us-gaap", {})

        # Look for preferred shares outstanding
        for key in ["PreferredStockSharesOutstanding", "PreferredStockSharesIssued"]:
            concept = usgaap.get(key, {})
            entries = concept.get("units", {}).get("shares", [])
            if not entries:
                continue

            # Filter to most recent 10-Q/10-K filing, prefer entries that
            # mention STRC or Series A in their frame/dimension
            # Sort by end date descending to get most recent
            entries_sorted = sorted(entries, key=lambda x: x.get("end", ""), reverse=True)

            # Take the most recent value — Strategy only has one preferred class (STRC)
            for entry in entries_sorted:
                val = entry.get("val")
                end_date = entry.get("end", "")
                form = entry.get("form", "")
                if val and val > 0:
                    print(f"  [STRC] {key}: {val:,.0f} shares (as of {end_date}, {form})")
                    return int(val)

        # Fallback: try dei namespace (some filers report here)
        dei = data.get("facts", {}).get("dei", {})
        for key in ["EntityCommonStockSharesOutstanding"]:
            concept = dei.get(key, {})
            entries = concept.get("units", {}).get("shares", [])
            if entries:
                entries_sorted = sorted(entries, key=lambda x: x.get("end", ""), reverse=True)
                if entries_sorted:
                    val = entries_sorted[0].get("val")
                    if val:
                        print(f"  [STRC] dei:{key}: {val:,.0f} shares (common, not preferred)")

        print("  [STRC] Could not find preferred shares outstanding in EDGAR")
        return None

    except Exception as e:
        print(f"  [STRC] EDGAR API failed: {type(e).__name__}: {e}")
        return None


def fetch_strc():
    """
    Fetch STRC data from Strategy APIs.
    1. strcKpiData for price, dividend, volume, notional
    2. getPreferreds for btcRating (not available in strcKpiData)
    """
    print("  Fetching STRC from api.strategy.com...")

    result = {}

    # ── Step 1: KPI endpoint for price, dividend, volume, notional ───────────
    try:
        req = urllib.request.Request(
            "https://api.strategy.com/btc/strcKpiData",
            headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=30, context=SSL_CTX) as resp:
            data = json.loads(resp.read().decode())

        if isinstance(data, list) and data:
            row = data[0]
            if row.get("price"):
                result["strc_price"] = round(float(row["price"]), 2)
            if row.get("currentDividend"):
                result["strc_dividend_pct"] = round(float(row["currentDividend"]), 2)
            if row.get("averageVolume"):
                result["strc_vol_30d_m"] = round(float(row["averageVolume"]), 1)
            if row.get("notional"):
                result["strc_notional_m"] = round(float(row["notional"]) / 1e6, 1)
            print(f"  ✓ STRC (KPI API): {len(result)} fields")
            for k, v in result.items():
                print(f"    {k}: {v}")
        else:
            print("  ✗ STRC KPI API returned empty or invalid data")
    except Exception as e:
        print(f"  ✗ STRC KPI API failed: {type(e).__name__}: {e}")

    # ── Step 2: getPreferreds endpoint for btcRating ─────────────────────────
    try:
        req = urllib.request.Request(
            "https://api.strategy.com/btc/getPreferreds",
            headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=30, context=SSL_CTX) as resp:
            data = json.loads(resp.read().decode())

        strc = next((x for x in data if x.get("company") == "STRC"), None)
        if strc:
            if strc.get("btcRating"):
                result["strc_btc_rating"] = round(float(strc["btcRating"]), 1)
                print(f"    strc_btc_rating: {result['strc_btc_rating']}")
            # Use getPreferreds as fallback for fields not in KPI endpoint
            if "strc_price" not in result and strc.get("price"):
                result["strc_price"] = round(float(strc["price"]), 2)
            if "strc_dividend_pct" not in result and strc.get("currentDividend"):
                result["strc_dividend_pct"] = round(float(strc["currentDividend"]), 2)
            if "strc_vol_30d_m" not in result and strc.get("averageVolume"):
                result["strc_vol_30d_m"] = round(float(strc["averageVolume"]), 1)
            print(f"  ✓ STRC (getPreferreds): btcRating fetched")
        else:
            print("  ✗ STRC not found in getPreferreds response")
    except Exception as e:
        print(f"  ✗ STRC getPreferreds failed: {type(e).__name__}: {e}")

    if result:
        print(f"  ✓ STRC total: {len(result)} fields")
    else:
        print("  ✗ STRC: no data from any source")

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
    for key in ["strc_price", "strc_dividend_pct", "strc_btc_rating", "strc_notional_m", "strc_vol_30d_m"]:
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
                "strc_price", "strc_dividend_pct", "strc_btc_rating",
                "strc_notional_m", "strc_vol_30d_m"]
    missing = [k for k in expected if not entry.get(k)]
    if missing:
        print(f"\n  ⚠ Missing: {', '.join(missing)}")
    else:
        print(f"\n  ✓ All fields populated")


if __name__ == "__main__":
    main()
