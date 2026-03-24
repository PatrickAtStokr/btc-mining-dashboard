#!/usr/bin/env python3
"""
generate_chart_signals.py
Fetches recent market data and uses the Google Gemini API (free tier) to generate
one short contextual signal per dashboard chart + 2-3 top-level signal points.

Output: chart_signals.json
Schedule: Every 2 days via GitHub Actions

Required env vars:
  GEMINI_API_KEY  — Google AI Studio API key (free, no credit card needed)
                    Get yours at: https://aistudio.google.com/app/apikey
"""

import json, os, sys, urllib.request, urllib.error
from datetime import datetime, timedelta, timezone

# ── Fetch recent BTC data from CoinGecko (last 7 days) ──────────────────────
def fetch_btc_recent():
    url = "https://api.coingecko.com/api/v3/coins/bitcoin/market_chart?vs_currency=usd&days=7"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "STOKR-Dashboard/1.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read())
        prices = data.get("prices", [])
        if not prices:
            return None
        latest = prices[-1][1]
        oldest = prices[0][1]
        high = max(p[1] for p in prices)
        low = min(p[1] for p in prices)
        pct_7d = ((latest - oldest) / oldest) * 100 if oldest else 0
        return {"price": round(latest, 2), "high_7d": round(high, 2), "low_7d": round(low, 2), "change_7d_pct": round(pct_7d, 2)}
    except Exception as e:
        print(f"Warning: CoinGecko fetch failed: {e}", file=sys.stderr)
        return None

# ── Fetch network data from blockchain.info ──────────────────────────────────
def fetch_network_recent():
    try:
        # Hashrate (EH/s)
        url_hr = "https://api.blockchain.info/charts/hash-rate?timespan=7days&format=json"
        req = urllib.request.Request(url_hr, headers={"User-Agent": "STOKR-Dashboard/1.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            hr_data = json.loads(r.read())
        hr_values = hr_data.get("values", [])
        latest_hr = hr_values[-1]["y"] / 1e6 if hr_values else None  # TH/s → EH/s

        # Difficulty
        url_diff = "https://api.blockchain.info/charts/difficulty?timespan=30days&format=json"
        req2 = urllib.request.Request(url_diff, headers={"User-Agent": "STOKR-Dashboard/1.0"})
        with urllib.request.urlopen(req2, timeout=15) as r:
            diff_data = json.loads(r.read())
        diff_values = diff_data.get("values", [])
        latest_diff = diff_values[-1]["y"] / 1e12 if diff_values else None  # → T
        prev_diff = diff_values[-2]["y"] / 1e12 if len(diff_values) >= 2 else None

        return {
            "hashrate_eh": round(latest_hr, 1) if latest_hr else None,
            "difficulty_t": round(latest_diff, 2) if latest_diff else None,
            "prev_difficulty_t": round(prev_diff, 2) if prev_diff else None,
        }
    except Exception as e:
        print(f"Warning: blockchain.info fetch failed: {e}", file=sys.stderr)
        return None

# ── Load existing manual_data.json for BMN / STRC context ────────────────────
def load_manual_data():
    for path in ["manual_data.json", "../manual_data.json"]:
        if os.path.exists(path):
            with open(path) as f:
                return json.load(f)
    return {}

# ── Call Google Gemini API (free tier) ─────────────────────────────────────────
def call_gemini(prompt):
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("Error: GEMINI_API_KEY not set", file=sys.stderr)
        print("Get a free key at: https://aistudio.google.com/app/apikey", file=sys.stderr)
        sys.exit(1)

    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={api_key}"

    body = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.7,
            "maxOutputTokens": 1024,
            "responseMimeType": "application/json",
        }
    }).encode()

    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST"
    )

    with urllib.request.urlopen(req, timeout=60) as r:
        resp = json.loads(r.read())

    # Extract text from Gemini response structure
    try:
        return resp["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError) as e:
        print(f"Unexpected Gemini response structure: {e}", file=sys.stderr)
        print(f"Full response: {json.dumps(resp, indent=2)}", file=sys.stderr)
        sys.exit(1)


# ── Main ─────────────────────────────────────────────────────────────────────
def main():
    print("Fetching recent market data...")
    btc = fetch_btc_recent()
    net = fetch_network_recent()
    manual = load_manual_data()

    # Extract latest BMN/STRC data if available
    latest_bmn = {}
    if manual.get("data"):
        latest_bmn = manual["data"][-1]

    # Build context block for the LLM
    context_parts = []
    if btc:
        context_parts.append(f"BTC: ${btc['price']:,.0f} (7d: {btc['change_7d_pct']:+.1f}%, range ${btc['low_7d']:,.0f}–${btc['high_7d']:,.0f})")
    if net:
        context_parts.append(f"Network hashrate: {net['hashrate_eh']} EH/s, Difficulty: {net['difficulty_t']}T")
        if net.get("prev_difficulty_t"):
            diff_chg = ((net["difficulty_t"] - net["prev_difficulty_t"]) / net["prev_difficulty_t"]) * 100
            context_parts.append(f"Difficulty change: {diff_chg:+.1f}%")
    if latest_bmn:
        if latest_bmn.get("hashprice_usd"):
            context_parts.append(f"Hashprice: ${latest_bmn['hashprice_usd']}/PH/day")
        if latest_bmn.get("bmn_revenue_btc"):
            context_parts.append(f"BMN daily revenue: ₿{latest_bmn['bmn_revenue_btc']}")
        if latest_bmn.get("strc_price"):
            context_parts.append(f"STRC price: ${latest_bmn['strc_price']} (par $100, BTC rating {latest_bmn.get('strc_btc_rating', '?')}x)")
        if latest_bmn.get("bmn_active_miners"):
            context_parts.append(f"BMN active miners: {latest_bmn['bmn_active_miners']}, hashrate: {latest_bmn.get('bmn_hashrate_24h_eh', '?')} EH/s")

    context = "\n".join(context_parts) if context_parts else "No live data available — generate generic mining market signals."

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    prompt = f"""You are a Bitcoin mining market analyst writing brief signals for a STOKR dashboard.
Today is {today}. Here is the latest data:

{context}

Generate a JSON object with this EXACT structure (no markdown, no code fences, just raw JSON):
{{
  "signal_points": [
    "First top-level signal (1 sentence, most important market insight)",
    "Second top-level signal (1 sentence, key risk or opportunity)",
    "Third top-level signal (1 sentence, forward-looking note)"
  ],
  "signals": {{
    "btc_price": "One sentence about BTC price action and what it means for miners",
    "hashprice": "One sentence about hashprice trend and miner revenue implications",
    "difficulty": "One sentence about difficulty/hashrate and competitive dynamics",
    "bmn_revenue": "One sentence about BMN revenue trend or outlook",
    "strc": "One sentence about STRC price relative to par and BTC rating",
    "bmn_hashrate": "One sentence about BMN fleet performance and uptime"
  }}
}}

Rules:
- Each signal must be exactly ONE sentence, max 120 characters
- Be specific — reference actual numbers from the data
- Be analytical, not just descriptive — give insight into what the data means
- Use professional but accessible language, no hype
- signal_points should be the 2-3 most important takeaways across all charts
- If data for a chart is unavailable, write a generic but useful contextual note"""

    print("Calling Gemini API for chart signals...")
    raw = call_gemini(prompt)

    # Parse the JSON from the response (handle potential markdown wrapping)
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[1] if "\n" in cleaned else cleaned[3:]
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3]
        cleaned = cleaned.strip()

    try:
        result = json.loads(cleaned)
    except json.JSONDecodeError as e:
        print(f"Failed to parse LLM response as JSON: {e}", file=sys.stderr)
        print(f"Raw response:\n{raw}", file=sys.stderr)
        sys.exit(1)

    # Add metadata
    result["generated"] = datetime.now(timezone.utc).isoformat()
    result["data_snapshot"] = {
        "btc": btc,
        "network": net,
    }

    # Write output
    out_path = "chart_signals.json"
    with open(out_path, "w") as f:
        json.dump(result, f, indent=2)

    print(f"Wrote {out_path} ({len(json.dumps(result))} bytes)")
    print(f"Signal points: {len(result.get('signal_points', []))}")
    print(f"Chart signals: {list(result.get('signals', {}).keys())}")


if __name__ == "__main__":
    main()
