"""
Direct Luxor Mining Pool data fetcher — no API key needed for public dashboard.
Fetches live pool stats from https://mining.luxor.tech/mining/bitcoin

This script inspects network requests the dashboard makes and extracts:
  - Hashrate (5m, 24h) in EH/s
  - Active miners count
  - Uptime %
  - Revenue 24h (BTC)
  - Hashprice (USD/PH/day)
  - Shares efficiency %

Usage (local testing):
    python fetch_luxor.py [--subaccount-name "BMN"]

Returns JSON to stdout.
"""

import json
import sys
import urllib.request
import ssl
from datetime import datetime, timezone

SSL_CTX = ssl.create_default_context()
try:
    import certifi
    SSL_CTX.load_verify_locations(certifi.where())
except Exception:
    SSL_CTX = ssl._create_unverified_context()


def fetch_luxor_public(subaccount_name="BMN"):
    """
    Fetch Luxor public dashboard data.

    The Luxor dashboard makes API calls to fetch pool stats.
    We'll extract the key metrics by calling the same endpoints or
    scraping the rendered data.

    For now, we'll use the GraphQL endpoint if available, or fall back
    to inspecting the HTML for rendered values.
    """

    print(f"[Luxor] Fetching data for subaccount: {subaccount_name}", file=sys.stderr)

    result = {}

    # ── Attempt 1: Try GraphQL API (no auth needed for public data) ────────────
    try:
        print(f"[Luxor] Trying GraphQL endpoint...", file=sys.stderr)

        query = """
        query {
            getPublicMiningSummary(mpn: BTC, userName: "%s") {
                hashrate5m
                hashrate24hr
                activeWorkers
                revenue24hr
                uptimePercentage
                sharesEfficiency
            }
        }
        """ % subaccount_name

        payload = json.dumps({"query": query}).encode("utf-8")
        req = urllib.request.Request(
            "https://api.luxor.tech/graphql",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "STOKR-Mining-Intel/1.0",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30, context=SSL_CTX) as resp:
            data = json.loads(resp.read().decode())

        summary = data.get("data", {}).get("getPublicMiningSummary", {})
        if summary:
            print(f"[Luxor] ✓ Got data from GraphQL", file=sys.stderr)
            if summary.get("hashrate5m"):
                result["bmn_hashrate_5m_eh"] = round(summary["hashrate5m"] / 1e18, 3)
            if summary.get("hashrate24hr"):
                result["bmn_hashrate_24h_eh"] = round(summary["hashrate24hr"] / 1e18, 3)
            if summary.get("activeWorkers"):
                result["bmn_active_miners"] = summary["activeWorkers"]
            if summary.get("uptimePercentage") is not None:
                result["bmn_uptime_pct"] = round(summary["uptimePercentage"], 2)
            if summary.get("revenue24hr"):
                result["bmn_revenue_btc"] = round(summary["revenue24hr"] / 1e8, 8)
            if summary.get("sharesEfficiency") is not None:
                result["bmn_shares_efficiency_pct"] = round(summary["sharesEfficiency"], 2)
            return result
    except Exception as e:
        print(f"[Luxor] GraphQL failed ({type(e).__name__}), trying HTML fallback...", file=sys.stderr)

    # ── Attempt 2: Scrape rendered HTML ───────────────────────────────────────
    try:
        print(f"[Luxor] Fetching dashboard HTML...", file=sys.stderr)

        url = f"https://mining.luxor.tech/mining/bitcoin?user={subaccount_name}"
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "STOKR-Mining-Intel/1.0"},
        )
        with urllib.request.urlopen(req, timeout=30, context=SSL_CTX) as resp:
            html = resp.read().decode('utf-8', errors='ignore')

        import re

        # Extract metrics from HTML
        # These patterns match the visible text on the page

        # Hashrate 5m: "28.027 EH/s"
        m = re.search(r'Hashrate\s*\(?5\s*min\)?\s*[\s\S]{0,50}?([\d.]+)\s*EH/s', html)
        if m:
            result["bmn_hashrate_5m_eh"] = float(m.group(1))
            print(f"[Luxor] 5m hashrate: {result['bmn_hashrate_5m_eh']} EH/s", file=sys.stderr)

        # Hashrate 24h: "27.512 EH/s"
        m = re.search(r'Hashrate\s*\(?24\s*hour|24.*hour\)?\s*[\s\S]{0,50}?([\d.]+)\s*EH/s', html)
        if m:
            result["bmn_hashrate_24h_eh"] = float(m.group(1))
            print(f"[Luxor] 24h hashrate: {result['bmn_hashrate_24h_eh']} EH/s", file=sys.stderr)

        # Active miners: "103899"
        m = re.search(r'Active\s*miners?\s*[\s\S]{0,30}?([\d]+)(?:\s|<)', html)
        if m:
            result["bmn_active_miners"] = int(m.group(1))
            print(f"[Luxor] Active miners: {result['bmn_active_miners']}", file=sys.stderr)

        # Uptime: "95.76 %"
        m = re.search(r'Uptime\s*\(?24\s*hour\)?\s*[\s\S]{0,30}?([\d.]+)\s*%', html)
        if m:
            result["bmn_uptime_pct"] = float(m.group(1))
            print(f"[Luxor] Uptime: {result['bmn_uptime_pct']}%", file=sys.stderr)

        # Revenue 24h: "11.93728924 BTC"
        m = re.search(r'Revenue\s*\(?24\s*hour\)?\s*[\s\S]{0,50}?([\d.]+)\s*BTC', html, re.IGNORECASE)
        if m:
            result["bmn_revenue_btc"] = float(m.group(1))
            print(f"[Luxor] Revenue 24h: {result['bmn_revenue_btc']} BTC", file=sys.stderr)

        # Shares Efficiency: "99.98 %"
        m = re.search(r'(?:Shares|Share)\s*Efficiency\s*[\s\S]{0,30}?([\d.]+)\s*%', html)
        if m:
            result["bmn_shares_efficiency_pct"] = float(m.group(1))
            print(f"[Luxor] Shares Efficiency: {result['bmn_shares_efficiency_pct']}%", file=sys.stderr)

        # Hashprice: "0.00044 BTC/PH/s/Day" or similar
        m = re.search(r'Hashprice\s*[\s\S]{0,50}?(0\.[\d]+)\s*BTC/PH', html)
        if m:
            hashprice_btc = float(m.group(1))
            result["hashprice_btc"] = hashprice_btc
            print(f"[Luxor] Hashprice: {hashprice_btc} BTC/PH", file=sys.stderr)

        if result:
            print(f"[Luxor] ✓ Scraped {len(result)} fields from HTML", file=sys.stderr)
            return result
        else:
            print(f"[Luxor] ✗ Could not extract any metrics from HTML", file=sys.stderr)
            return {}

    except Exception as e:
        print(f"[Luxor] ✗ HTML scraping failed: {type(e).__name__}: {e}", file=sys.stderr)
        return {}


def main():
    subaccount = "BMN"

    # Allow override via command line
    if "--subaccount-name" in sys.argv:
        idx = sys.argv.index("--subaccount-name")
        if idx + 1 < len(sys.argv):
            subaccount = sys.argv[idx + 1]

    result = fetch_luxor_public(subaccount)

    if result:
        result["timestamp"] = datetime.now(tz=timezone.utc).isoformat()
        print(json.dumps(result, indent=2))
        sys.exit(0)
    else:
        print(json.dumps({"error": "Could not fetch Luxor data", "timestamp": datetime.now(tz=timezone.utc).isoformat()}, indent=2), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
