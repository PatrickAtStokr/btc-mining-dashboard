"""
Automated data fetcher for STOKR Mining Intelligence Dashboard.
Runs via GitHub Actions on a schedule — no server required.

Fetches from free public APIs (no keys needed):
  - BTC daily price:  CoinGecko (fallback: CryptoCompare)
  - Network hashrate:  blockchain.info
  - Network difficulty: blockchain.info

Output: data.json in the same directory as this script.
The dashboard HTML reads this file on every page load.

Usage:
    python fetch_data.py              # full history (~5000 days)
    python fetch_data.py --days 365   # last year only
"""

import json
import urllib.request
import ssl
import os
import sys
import time
from datetime import datetime, timezone, timedelta

# SSL context — GitHub Actions uses Linux so this is mainly for local dev on macOS
SSL_CTX = ssl.create_default_context()
try:
    import certifi
    SSL_CTX.load_verify_locations(certifi.where())
except Exception:
    SSL_CTX = ssl._create_unverified_context()

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_FILE = os.path.join(SCRIPT_DIR, "data.json")

DAYS = 5181  # back to ~2012-01-01
if "--days" in sys.argv:
    idx = sys.argv.index("--days")
    if idx + 1 < len(sys.argv):
        DAYS = int(sys.argv[idx + 1])


def _fetch_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "STOKR-Mining-Intel/1.0"})
    with urllib.request.urlopen(req, timeout=60, context=SSL_CTX) as resp:
        return json.loads(resp.read().decode())


# ── BTC Price ────────────────────────────────────────────────────────────────

def fetch_btc_coingecko(total_days):
    """CoinGecko free API — paginated in 365-day windows."""
    CHUNK = 365
    all_results = {}
    now = datetime.now(tz=timezone.utc)
    remaining = total_days
    end_ts = now
    chunks_needed = (total_days + CHUNK - 1) // CHUNK
    chunk_num = 0

    while remaining > 0:
        chunk_num += 1
        fetch_days = min(remaining, CHUNK)
        start_ts = end_ts - timedelta(days=fetch_days)
        url = (
            f"https://api.coingecko.com/api/v3/coins/bitcoin/market_chart/range"
            f"?vs_currency=usd"
            f"&from={int(start_ts.timestamp())}"
            f"&to={int(end_ts.timestamp())}"
        )
        print(f"    Chunk {chunk_num}/{chunks_needed}: {start_ts.strftime('%Y-%m-%d')} → {end_ts.strftime('%Y-%m-%d')}...", end=" ", flush=True)
        data = _fetch_json(url)
        count = 0
        for ts, price in data["prices"]:
            dt = datetime.fromtimestamp(ts / 1000, tz=timezone.utc)
            date_str = dt.strftime("%Y-%m-%d")
            if date_str not in all_results:
                all_results[date_str] = round(price, 2)
                count += 1
        print(f"{count} days")
        end_ts = start_ts
        remaining -= fetch_days
        if remaining > 0:
            time.sleep(2.5)

    return {d: p for d, p in sorted(all_results.items())}


def fetch_btc_cryptocompare(total_days):
    """CryptoCompare free API — paginated in 2000-day windows."""
    CHUNK = 2000
    all_results = {}
    now = datetime.now(tz=timezone.utc)
    to_ts = int(now.timestamp())
    remaining = total_days
    chunks_needed = (total_days + CHUNK - 1) // CHUNK
    chunk_num = 0

    while remaining > 0:
        chunk_num += 1
        fetch_limit = min(remaining, CHUNK)
        url = (
            f"https://min-api.cryptocompare.com/data/v2/histoday"
            f"?fsym=BTC&tsym=USD&limit={fetch_limit}&toTs={to_ts}"
        )
        print(f"    Chunk {chunk_num}/{chunks_needed}: fetching {fetch_limit} days...", end=" ", flush=True)
        data = _fetch_json(url)
        entries = data["Data"]["Data"]
        count = 0
        earliest_ts = None
        for entry in entries:
            dt = datetime.fromtimestamp(entry["time"], tz=timezone.utc)
            date_str = dt.strftime("%Y-%m-%d")
            close = entry["close"]
            if close > 0 and date_str not in all_results:
                all_results[date_str] = round(close, 2)
                count += 1
            if earliest_ts is None or entry["time"] < earliest_ts:
                earliest_ts = entry["time"]
        print(f"{count} days")
        to_ts = earliest_ts - 86400
        remaining -= fetch_limit
        if remaining > 0:
            time.sleep(1)

    return {d: p for d, p in sorted(all_results.items())}


def fetch_btc_prices(total_days):
    for name, fetcher in [("CoinGecko", fetch_btc_coingecko), ("CryptoCompare", fetch_btc_cryptocompare)]:
        try:
            print(f"  Trying {name}:")
            result = fetcher(total_days)
            print(f"  ✓ {name}: {len(result)} days\n")
            return result
        except Exception as e:
            print(f"  ✗ {name} failed: {e}\n")
    return {}


# ── Network Data ─────────────────────────────────────────────────────────────

def fetch_blockchain_chart(chart, label):
    url = f"https://api.blockchain.info/charts/{chart}?timespan=all&format=json&sampled=true"
    print(f"  Fetching {label}...", end=" ", flush=True)
    req = urllib.request.Request(url, headers={"User-Agent": "STOKR-Mining-Intel/1.0"})
    with urllib.request.urlopen(req, timeout=60, context=SSL_CTX) as resp:
        data = json.loads(resp.read().decode())
    points = data["values"]
    print(f"{len(points)} points")
    return points


def fetch_network_data():
    print("  Fetching network data from blockchain.info:")
    hashrate_raw = fetch_blockchain_chart("hash-rate", "Network Hashrate")
    time.sleep(1)
    difficulty_raw = fetch_blockchain_chart("difficulty", "Network Difficulty")

    def to_daily(points, scale=1.0):
        by_date = {}
        for p in points:
            dt = datetime.fromtimestamp(p["x"], tz=timezone.utc)
            date = dt.strftime("%Y-%m-%d")
            by_date[date] = round(p["y"] * scale, 4)
        return by_date

    hr = to_daily(hashrate_raw, scale=1/1_000_000)      # GH/s → EH/s
    diff = to_daily(difficulty_raw, scale=1/1_000_000_000_000)  # raw → T

    print(f"  ✓ Network data: {len(hr)} hashrate days, {len(diff)} difficulty days\n")
    return hr, diff


# ── Derived Hashprice ────────────────────────────────────────────────────────

def compute_hashprice_history(btc_prices, net_hashrate):
    """
    Derive daily hashprice (USD/PH/day) from BTC price + network hashrate.
    Formula: hashprice = (144 * 3.125 * btc_price) / (network_hashrate_eh * 1000)
    This is subsidy-only (excludes tx fees) — typically within 3-5% of Luxor values.
    Network hashrate is interpolated between available data points.
    """
    if not net_hashrate:
        return []

    # Build sorted list of known (date, hashrate) pairs for interpolation
    known = sorted([(d, hr) for d, hr in net_hashrate.items()])
    known_dates = [datetime.fromisoformat(d) for d, _ in known]
    known_hrs   = [hr for _, hr in known]

    result = []
    for date_str, btc_price in sorted(btc_prices.items()):
        dt = datetime.fromisoformat(date_str)
        # Find surrounding known points for interpolation
        hr = None
        for i in range(len(known_dates) - 1):
            if known_dates[i] <= dt <= known_dates[i + 1]:
                span = (known_dates[i + 1] - known_dates[i]).days
                if span == 0:
                    hr = known_hrs[i]
                else:
                    frac = (dt - known_dates[i]).days / span
                    hr = known_hrs[i] + frac * (known_hrs[i + 1] - known_hrs[i])
                break
        if hr is None:
            # Outside known range — use nearest endpoint
            if dt < known_dates[0]:
                hr = known_hrs[0]
            elif dt > known_dates[-1]:
                hr = known_hrs[-1]
        if hr and hr > 0:
            hp = round((450 * btc_price) / (hr * 1000), 4)
            result.append({"date": date_str, "hashprice_usd": hp})

    print(f"  ✓ Hashprice computed: {len(result)} days (derived from BTC × network hashrate)\n")
    return result


# ── Merge & Output ───────────────────────────────────────────────────────────

def main():
    print(f"=== STOKR Mining Dashboard — Data Fetch ===\n")
    print(f"Fetching {DAYS}-day history...\n")

    # Fetch all sources
    btc_prices = fetch_btc_prices(DAYS)
    try:
        net_hashrate, net_difficulty = fetch_network_data()
    except Exception as e:
        print(f"  ✗ Network data failed: {e}\n")
        net_hashrate, net_difficulty = {}, {}

    # Compute derived hashprice history
    print("Computing hashprice history (derived from BTC × network hashrate):")
    hashprice_history = compute_hashprice_history(btc_prices, net_hashrate)

    # Build unified output
    output = {
        "updated": datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "btc_history": [
            {"date": d, "btc_price": p}
            for d, p in sorted(btc_prices.items())
        ],
        "network_history": [],
        "hashprice_history": hashprice_history,
    }

    # Merge network data
    all_net_dates = sorted(set(net_hashrate) | set(net_difficulty))
    for date in all_net_dates:
        entry = {"date": date}
        if date in net_hashrate:
            entry["network_hashrate_eh"] = net_hashrate[date]
        if date in net_difficulty:
            entry["difficulty_t"] = net_difficulty[date]
        output["network_history"].append(entry)

    # Write output
    with open(OUTPUT_FILE, "w") as f:
        json.dump(output, f, separators=(",", ":"))  # compact — saves ~30% file size

    size_kb = os.path.getsize(OUTPUT_FILE) / 1024
    print(f"=== Done ===")
    print(f"Output: {OUTPUT_FILE} ({size_kb:.0f} KB)")
    print(f"BTC history:     {len(output['btc_history'])} days")
    print(f"Network history: {len(output['network_history'])} days")
    print(f"Updated:         {output['updated']}")

    if output["btc_history"]:
        last = output["btc_history"][-1]
        print(f"Latest BTC:      ${last['btc_price']:,.2f} ({last['date']})")
    if output["network_history"]:
        last = output["network_history"][-1]
        print(f"Latest hashrate: {last.get('network_hashrate_eh', 'N/A')} EH/s")
        print(f"Latest diff:     {last.get('difficulty_t', 'N/A')} T")
    if output["hashprice_history"]:
        last = output["hashprice_history"][-1]
        print(f"Latest hashprice:{last['hashprice_usd']} USD/PH/day ({last['date']})")


if __name__ == "__main__":
    main()
