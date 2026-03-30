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

    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-lite:generateContent?key={api_key}"

    body = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.7,
            "maxOutputTokens": 8192,
            "responseMimeType": "application/json",
        }
    }).encode()

    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST"
    )

    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            resp = json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode()[:800]
        print(f"Gemini API error — HTTP {e.code}:", file=sys.stderr)
        print(body, file=sys.stderr)
        if e.code == 400 and "API_KEY_INVALID" in body:
            print("\nHint: Your GEMINI_API_KEY is invalid. Check that:", file=sys.stderr)
            print("  1. The secret name is exactly GEMINI_API_KEY (case-sensitive)", file=sys.stderr)
            print("  2. The key was pasted without extra spaces or newlines", file=sys.stderr)
            print("  3. The key starts with 'AIza'", file=sys.stderr)
        elif e.code == 403:
            print("\nHint: API access denied. Make sure the Gemini API is enabled for your project.", file=sys.stderr)
        elif e.code == 429:
            print("\nHint: Rate limited. Free tier allows 250 requests/day. Try again later.", file=sys.stderr)
        elif e.code == 404:
            print("\nHint: Model not found. The gemini-2.5-flash model may have been renamed.", file=sys.stderr)
        sys.exit(1)
    except urllib.error.URLError as e:
        print(f"Network error calling Gemini API: {e}", file=sys.stderr)
        sys.exit(1)

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

    # Build context block for the LLM — include computed insights, not just raw numbers
    context_parts = []
    if btc:
        context_parts.append(f"BTC Price: ${btc['price']:,.0f} (7d change: {btc['change_7d_pct']:+.1f}%, 7d range: ${btc['low_7d']:,.0f}–${btc['high_7d']:,.0f})")
    if net:
        context_parts.append(f"Network hashrate: {net['hashrate_eh']} EH/s, Difficulty: {net['difficulty_t']}T")
        if net.get("prev_difficulty_t"):
            diff_chg = ((net["difficulty_t"] - net["prev_difficulty_t"]) / net["prev_difficulty_t"]) * 100
            context_parts.append(f"Last difficulty adjustment: {diff_chg:+.1f}%")
            if diff_chg > 3:
                context_parts.append("→ Significant difficulty increase — compressing hashprice, squeezing less efficient miners")
            elif diff_chg < -3:
                context_parts.append("→ Difficulty decrease — hashprice recovery, relief for marginal miners")
    if latest_bmn:
        if latest_bmn.get("hashprice_usd"):
            hp = float(latest_bmn['hashprice_usd'])
            context_parts.append(f"Hashprice: ${hp}/PH/day")
            # Add profitability tier context
            if hp < 45:
                context_parts.append("→ Hashprice below S19 XP breakeven (~$45) — mid-tier miners under margin pressure")
            elif hp < 60:
                context_parts.append("→ Hashprice supports next-gen ASICs but legacy S19j Pro fleet (~$60 breakeven) at risk")
            else:
                context_parts.append("→ Hashprice supports broad profitability across most ASIC tiers")
        if latest_bmn.get("bmn_revenue_btc"):
            context_parts.append(f"BMN daily revenue: ₿{latest_bmn['bmn_revenue_btc']}")
        if latest_bmn.get("strc_price"):
            strc_p = float(latest_bmn['strc_price'])
            strc_rating = latest_bmn.get('strc_btc_rating', '?')
            premium_discount = "at par" if abs(strc_p - 100) < 0.5 else ("above par (premium)" if strc_p > 100.5 else "below par (discount)")
            context_parts.append(f"STRC: ${strc_p} ({premium_discount}), BTC Rating: {strc_rating}x")
        if latest_bmn.get("bmn_active_miners"):
            context_parts.append(f"BMN fleet: {latest_bmn['bmn_active_miners']} active miners, {latest_bmn.get('bmn_hashrate_24h_eh', '?')} EH/s (24h)")

    context = "\n".join(context_parts) if context_parts else "No live data available — generate generic mining market signals."

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    prompt = f"""You are a senior Bitcoin mining analyst at STOKR, a tokenized securities platform.
Today is {today}. You write research-grade signals — NOT data summaries.

=== DOMAIN KNOWLEDGE (use this to inform your analysis) ===

HASHPRICE ECONOMICS:
Hashprice = (BTC Price × Block Subsidy + Tx Fees) / Global Hash Rate.
BTC price up → hashprice up. Hash rate up → hashprice down. Difficulty adjusts every ~2 weeks.
Post-halving (April 2024, subsidy now 3.125 BTC), hashprice is in compression phase.
Historical range: peaks ~$400/PH/day in bulls, troughs ~$40-60 in bears.

MINER PROFITABILITY TIERS (at $0.04-0.05/kWh):
- S21 Pro (15 J/TH): profitable down to ~$30/PH/day
- S19 XP (21.5 J/TH): breakeven ~$45/PH/day
- S19j Pro (29.5 J/TH): breakeven ~$60/PH/day — shutdown risk if hashprice drops
- Legacy S19 (34.5 J/TH): marginal, first to capitulate

STRC MECHANICS (CRITICAL — get this right):
STRC is Strategy's perpetual preferred stock, par $100, paying ~10% annual dividend.
BTC Rating = number of BTC per STRC share in Strategy's treasury. It fluctuates with BTC purchases/dilution.
STRC trading near par means the market prices it close to its liquidation preference — this is NEUTRAL, not bullish.
STRC trading ABOVE par = premium (demand for yield). BELOW par = discount (credit/duration risk).
BTC Rating declining = dilution or BTC sales. BTC Rating rising = accretive BTC purchases.

BMN2 (STOKR's Bitcoin Mining Note):
Tokenized hashrate product. Revenue is a function of hashprice × allocated hash rate.
Active miners and uptime directly impact daily BTC yield per token.

=== CURRENT DATA ===
{context}

=== YOUR TASK ===
Generate a JSON object with this EXACT structure:
{{
  "signal_points": [
    "First: the single most important research insight for a mining investor today (1 sentence)",
    "Second: a key risk, opportunity, or structural shift to watch (1 sentence)",
    "Third: a forward-looking catalyst or inflection point (1 sentence)"
  ],
  "signals": {{
    "btc_price": [
      "First point: what the BTC price trend means for miner margins and hashprice (1 sentence)",
      "Second point: a driver, risk, or catalyst affecting BTC and therefore mining economics (1 sentence)"
    ],
    "hashprice": [
      "First point: hashprice trend and what it implies for miner profitability tiers (1 sentence)",
      "Second point: what is driving hashprice direction — BTC price, hash rate growth, or fees (1 sentence)"
    ],
    "difficulty": [
      "First point: difficulty/hashrate trend and which miner tiers are under pressure (1 sentence)",
      "Second point: competitive dynamics — is hash rate growing (compressing margins) or declining (1 sentence)"
    ],
    "bmn_revenue": [
      "First point: BMN revenue trend and what is driving it — hashprice, uptime, or allocation (1 sentence)",
      "Second point: outlook for BMN yield given current network and hashprice trajectory (1 sentence)"
    ],
    "strc": [
      "First point: STRC price position vs par and what the premium/discount signals (1 sentence)",
      "Second point: BTC Rating movement and what it means for the underlying BTC backing (1 sentence)"
    ],
    "bmn_hashrate": [
      "First point: fleet performance — uptime, active miners, operational efficiency (1 sentence)",
      "Second point: how BMN hashrate compares to network growth and what that means for share of mining rewards (1 sentence)"
    ]
  }}
}}

=== RULES ===
- Each point must be ONE sentence, max 140 characters
- NEVER just restate the numbers — explain what they MEAN for an investor
- Reference the domain knowledge above to give structural insight
- Be specific and opinionated — what should a mining investor pay attention to?
- signal_points should be the 3 most actionable research takeaways, not data summaries
- NEVER say "STRC at par indicates investor confidence" — that is factually wrong
- If data is unavailable, give a structural insight about that metric instead"""

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
