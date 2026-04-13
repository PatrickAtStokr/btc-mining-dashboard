#!/usr/bin/env python3
"""
generate_chart_signals.py
Fetches recent market data from multiple sources, then uses 6 focused per-chart
Gemini API calls to produce research-quality signals (not data summaries).

Output: chart_signals.json
Schedule: Every 2 days via GitHub Actions

Required env vars:
  GEMINI_API_KEY  — Google AI Studio API key (free, no credit card needed)
                    Get yours at: https://aistudio.google.com/app/apikey
"""

import json, os, sys, urllib.request, urllib.error, time
from datetime import datetime, timedelta, timezone

UA = {"User-Agent": "STOKR-Dashboard/1.0"}

# ═══════════════════════════════════════════════════════════════════════════════
# DATA FETCHING — gather context from multiple sources before any LLM calls
# ═══════════════════════════════════════════════════════════════════════════════

def _get_json(url, timeout=15):
    """Helper: fetch JSON from a URL, return None on failure."""
    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except Exception as e:
        print(f"  Warning: fetch failed for {url[:80]}… — {e}", file=sys.stderr)
        return None


def fetch_btc_recent():
    """CoinGecko: BTC price + 7-day range."""
    data = _get_json("https://api.coingecko.com/api/v3/coins/bitcoin/market_chart?vs_currency=usd&days=7")
    if not data:
        return None
    prices = data.get("prices", [])
    if not prices:
        return None
    latest, oldest = prices[-1][1], prices[0][1]
    return {
        "price": round(latest, 2),
        "high_7d": round(max(p[1] for p in prices), 2),
        "low_7d": round(min(p[1] for p in prices), 2),
        "change_7d_pct": round(((latest - oldest) / oldest) * 100, 2) if oldest else 0,
    }


def fetch_network_data():
    """blockchain.info: hashrate + difficulty (current & previous epoch)."""
    hr_data = _get_json("https://api.blockchain.info/charts/hash-rate?timespan=7days&format=json")
    diff_data = _get_json("https://api.blockchain.info/charts/difficulty?timespan=60days&format=json")
    result = {}
    if hr_data and hr_data.get("values"):
        vals = hr_data["values"]
        result["hashrate_eh"] = round(vals[-1]["y"] / 1e6, 1)
        if len(vals) >= 2:
            hr_7d_ago = vals[0]["y"] / 1e6
            result["hashrate_7d_ago_eh"] = round(hr_7d_ago, 1)
            result["hashrate_7d_change_pct"] = round(((result["hashrate_eh"] - hr_7d_ago) / hr_7d_ago) * 100, 1)
    if diff_data and diff_data.get("values"):
        vals = diff_data["values"]
        result["difficulty_t"] = round(vals[-1]["y"] / 1e12, 2)
        if len(vals) >= 2:
            result["prev_difficulty_t"] = round(vals[-2]["y"] / 1e12, 2)
            result["diff_change_pct"] = round(
                ((result["difficulty_t"] - result["prev_difficulty_t"]) / result["prev_difficulty_t"]) * 100, 1
            )
    return result or None


def fetch_mempool_difficulty():
    """mempool.space: next difficulty adjustment projection."""
    data = _get_json("https://mempool.space/api/v1/difficulty-adjustment")
    if not data:
        return None
    return {
        "progress_pct": round(data.get("progressPercent", 0), 1),
        "estimated_change_pct": round(data.get("difficultyChange", 0), 1),
        "remaining_blocks": data.get("remainingBlocks"),
        "remaining_time_sec": data.get("remainingTime"),
        "estimated_date": data.get("estimatedRetargetDate"),
    }


def fetch_mempool_fees():
    """mempool.space: current fee environment."""
    data = _get_json("https://mempool.space/api/v1/fees/recommended")
    if not data:
        return None
    return {
        "fastest_sat_vb": data.get("fastestFee"),
        "half_hour_sat_vb": data.get("halfHourFee"),
        "economy_sat_vb": data.get("economyFee"),
    }


def fetch_mempool_hashrate():
    """mempool.space: hashrate and difficulty for last 3 months (more granular)."""
    data = _get_json("https://mempool.space/api/v1/mining/hashrate/3m")
    if not data:
        return None
    result = {}
    if data.get("currentHashrate"):
        result["current_hashrate_eh"] = round(data["currentHashrate"] / 1e18, 1)
    if data.get("currentDifficulty"):
        result["current_difficulty_t"] = round(data["currentDifficulty"] / 1e12, 2)
    return result or None


def load_manual_data():
    """Load manual_data.json for BMN/STRC context."""
    for path in ["manual_data.json", "../manual_data.json"]:
        if os.path.exists(path):
            with open(path) as f:
                return json.load(f)
    return {}


def load_previous_signals():
    """Load previous chart_signals.json for delta framing."""
    for path in ["chart_signals.json", "../chart_signals.json"]:
        if os.path.exists(path):
            try:
                with open(path) as f:
                    return json.load(f)
            except Exception:
                pass
    return None


# ═══════════════════════════════════════════════════════════════════════════════
# GEMINI API
# ═══════════════════════════════════════════════════════════════════════════════

def call_gemini(prompt, retries=2):
    """Call Gemini API with retry on transient errors. Returns parsed text."""
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
            "maxOutputTokens": 4096,
            "responseMimeType": "application/json",
        }
    }).encode()

    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=60) as r:
                resp = json.loads(r.read())
            return resp["candidates"][0]["content"]["parts"][0]["text"]
        except urllib.error.HTTPError as e:
            err_body = e.read().decode()[:800]
            print(f"  Gemini API error — HTTP {e.code} (attempt {attempt+1}):", file=sys.stderr)
            print(f"  {err_body[:200]}", file=sys.stderr)
            if e.code == 429 and attempt < retries:
                wait = 10 * (attempt + 1)
                print(f"  Rate limited — waiting {wait}s before retry...", file=sys.stderr)
                time.sleep(wait)
                continue
            if e.code == 400 and "API_KEY_INVALID" in err_body:
                print("\n  Hint: GEMINI_API_KEY is invalid. Check key starts with 'AIza'.", file=sys.stderr)
            sys.exit(1)
        except urllib.error.URLError as e:
            print(f"  Network error: {e}", file=sys.stderr)
            if attempt < retries:
                time.sleep(5)
                continue
            sys.exit(1)
        except (KeyError, IndexError) as e:
            print(f"  Unexpected response structure: {e}", file=sys.stderr)
            sys.exit(1)


def parse_gemini_json(raw):
    """Parse JSON from Gemini response, handling markdown wrapping."""
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[1] if "\n" in cleaned else cleaned[3:]
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3]
        cleaned = cleaned.strip()
    return json.loads(cleaned)


# ═══════════════════════════════════════════════════════════════════════════════
# PER-CHART RESEARCH PROMPTS
# ═══════════════════════════════════════════════════════════════════════════════

DOMAIN_PREAMBLE = """You are a senior Bitcoin mining analyst at STOKR. Write research-grade signals — NOT data summaries.
CRITICAL RULES:
- Each point must be ONE sentence, max 140 characters
- NEVER just restate numbers — explain what they MEAN for a mining investor
- Be specific and opinionated
- NEVER say "STRC at par indicates investor confidence" — factually wrong
- Output ONLY the JSON requested, no markdown"""


def build_chart_prompt(chart_key, data_context, prev_signals, today):
    """Build a focused prompt for one chart's research signal."""

    # Previous signal for delta framing
    prev = ""
    if prev_signals and prev_signals.get("signals", {}).get(chart_key):
        old = prev_signals["signals"][chart_key]
        if isinstance(old, list):
            prev = f"\nPREVIOUS SIGNAL (for delta framing — what changed since?):\n" + "\n".join(f"- {s}" for s in old)
        elif isinstance(old, str):
            prev = f"\nPREVIOUS SIGNAL: {old}"

    prompts = {
        "btc_price": f"""{DOMAIN_PREAMBLE}
Today: {today}. You are analyzing BTC PRICE for the STOKR Mining Intelligence dashboard.

CONTEXT:
{data_context.get('btc_context', 'No BTC data available.')}

RESEARCH ANGLES (pick the most relevant):
- How does the current BTC trend translate into hashprice? (Every 1% BTC move = ~1% hashprice move)
- Are we in a consolidation, breakout, or breakdown pattern? What does that mean for miner treasury strategies?
- What macro or ETF flow drivers are moving BTC right now?
- How does current price compare to public miner cost-of-production ($40-60K range for most)?
- Is BTC above/below the level where marginal miners start capitulating?
{prev}

Return JSON: {{"points": ["first research insight (1 sentence)", "second research insight (1 sentence)"]}}""",

        "hashprice": f"""{DOMAIN_PREAMBLE}
Today: {today}. You are analyzing HASHPRICE for the STOKR Mining Intelligence dashboard.

HASHPRICE = (BTC Price × 3.125 block subsidy + tx fees) / Global Hash Rate
Post-halving compression phase since April 2024. Historical peaks ~$400/PH/day, troughs ~$40-60.

Profitability breakevens (at $0.04-0.05/kWh):
- S21 Pro (15 J/TH): ~$30/PH/day
- S19 XP (21.5 J/TH): ~$45/PH/day
- S19j Pro (29.5 J/TH): ~$60/PH/day
- Legacy S19 (34.5 J/TH): ~$75/PH/day

CONTEXT:
{data_context.get('hashprice_context', 'No hashprice data available.')}

RESEARCH ANGLES:
- Which ASIC tiers are currently profitable vs at risk of shutdown?
- Is hashprice trending toward or away from critical breakeven levels?
- What's driving the hashprice direction — BTC price, hash rate growth, or fee environment?
- How do current fee levels affect the subsidy-only vs all-in hashprice gap?
{prev}

Return JSON: {{"points": ["first research insight (1 sentence)", "second research insight (1 sentence)"]}}""",

        "difficulty": f"""{DOMAIN_PREAMBLE}
Today: {today}. You are analyzing NETWORK DIFFICULTY & HASHRATE for the STOKR Mining Intelligence dashboard.

Difficulty adjusts every 2,016 blocks (~2 weeks) to target 10-minute block times.
Rising hashrate → difficulty increase → hashprice compression.
Falling hashrate → difficulty decrease → hashprice recovery.

CONTEXT:
{data_context.get('difficulty_context', 'No difficulty data available.')}

RESEARCH ANGLES:
- What does the next difficulty adjustment mean for hashprice? (Calculate: if difficulty rises X%, hashprice drops ~X%)
- Is hash rate growth accelerating or decelerating? What does the trend suggest about new capacity coming online?
- Are we seeing signs of miner capitulation (hash rate drops) or expansion (hash rate surges)?
- How many days/blocks until the next adjustment and what's the projected magnitude?
{prev}

Return JSON: {{"points": ["first research insight (1 sentence)", "second research insight (1 sentence)"]}}""",

        "bmn_revenue": f"""{DOMAIN_PREAMBLE}
Today: {today}. You are analyzing BMN DAILY REVENUE for the STOKR Mining Intelligence dashboard.

BMN2 is STOKR's tokenized hashrate product — a 4-year Bitcoin Mining Note.
Revenue = hashprice × allocated hashrate × uptime.
Revenue is denominated in BTC, so USD value also depends on BTC price.

CONTEXT:
{data_context.get('bmn_context', 'No BMN data available.')}

RESEARCH ANGLES:
- Is BMN revenue trending up or down, and what's the primary driver (hashprice vs uptime vs allocation)?
- How does current daily yield compare to the projected yield needed for target returns?
- What network conditions (difficulty, fees) would need to change for a meaningful revenue shift?
- How does the BMN pool's share of global mining output trend relative to network growth?
{prev}

Return JSON: {{"points": ["first research insight (1 sentence)", "second research insight (1 sentence)"]}}""",

        "strc": f"""{DOMAIN_PREAMBLE}
Today: {today}. You are analyzing STRC (Strategy Preferred Stock) for the STOKR Mining Intelligence dashboard.

STRC MECHANICS (get this right):
- Perpetual preferred stock, par $100, ~10% annual dividend
- Above par = yield premium (market wants the yield). Below par = credit/duration discount.
- Trading near par is NEUTRAL — it means the market prices it at liquidation preference. NOT a bullish signal.
- BTC Rating = BTC per STRC share in Strategy's treasury
- BTC Rating declining = dilution or BTC sales. Rising = accretive BTC purchases.
- STRC is NOT an equity — it has preferred liquidation priority but limited upside.

CONTEXT:
{data_context.get('strc_context', 'No STRC data available.')}

RESEARCH ANGLES:
- Is STRC's premium/discount widening or narrowing? What's driving that?
- What does the BTC Rating trend tell us about Strategy's treasury management (accretive vs dilutive)?
- How does the ~10% dividend compare to current risk-free rates? Is the yield spread compressing?
- What would cause STRC to trade materially above or below par?
{prev}

Return JSON: {{"points": ["first research insight (1 sentence)", "second research insight (1 sentence)"]}}""",

        "bmn_hashrate": f"""{DOMAIN_PREAMBLE}
Today: {today}. You are analyzing BMN FLEET PERFORMANCE for the STOKR Mining Intelligence dashboard.

BMN2 fleet = pool of ASIC miners producing hashrate for BMN2 token holders.
Key metrics: active miners, 24h hashrate (EH/s), uptime %, shares efficiency.
Fleet health directly impacts token holder yield.

CONTEXT:
{data_context.get('fleet_context', 'No fleet data available.')}

RESEARCH ANGLES:
- Is the fleet hashrate growing, stable, or declining relative to network growth?
- What does the active miner count trend suggest about fleet maintenance or expansion?
- BMN's share of global hashrate: is it holding, gaining, or losing ground?
- Are there any operational signals (uptime drops, efficiency changes) that investors should watch?
{prev}

Return JSON: {{"points": ["first research insight (1 sentence)", "second research insight (1 sentence)"]}}""",
    }

    return prompts.get(chart_key, "")


def build_signal_points_prompt(all_signals, data_context, today):
    """Build prompt for the 3 top-level STOKR Signal bullets, synthesizing all chart insights."""
    signals_summary = ""
    for key, val in all_signals.items():
        if isinstance(val, list):
            signals_summary += f"\n{key}: {' | '.join(val)}"

    return f"""{DOMAIN_PREAMBLE}
Today: {today}. You have just completed 6 individual chart analyses for the STOKR Mining Intelligence dashboard.
Your task: synthesize the most important takeaways into 3 top-level STOKR Signal bullets.

CHART ANALYSIS RESULTS:
{signals_summary}

KEY DATA POINTS:
{data_context.get('summary_context', 'No summary data available.')}

The 3 signal points should be:
1. The single most important research insight across all charts (what matters most RIGHT NOW)
2. A key risk or opportunity that cuts across multiple metrics
3. A forward-looking catalyst or inflection point to watch

Return JSON: {{"signal_points": ["first (1 sentence)", "second (1 sentence)", "third (1 sentence)"]}}"""


# ═══════════════════════════════════════════════════════════════════════════════
# CONTEXT BUILDERS — assemble per-chart research context from all data sources
# ═══════════════════════════════════════════════════════════════════════════════

def build_data_contexts(btc, net, mempool_diff, mempool_fees, mempool_hr, latest_bmn, prev_bmn):
    """Build per-chart context strings from all fetched data."""
    ctx = {}

    # ── BTC Price context ──
    parts = []
    if btc:
        parts.append(f"BTC Price: ${btc['price']:,.0f} (7d: {btc['change_7d_pct']:+.1f}%, range ${btc['low_7d']:,.0f}–${btc['high_7d']:,.0f})")
    if mempool_fees:
        parts.append(f"Fee environment: fastest {mempool_fees['fastest_sat_vb']} sat/vB, economy {mempool_fees['economy_sat_vb']} sat/vB")
    ctx["btc_context"] = "\n".join(parts) if parts else "No BTC data."

    # ── Hashprice context ──
    parts = []
    if latest_bmn.get("hashprice_usd"):
        hp = float(latest_bmn["hashprice_usd"])
        parts.append(f"Current hashprice: ${hp}/PH/day")
        if prev_bmn and prev_bmn.get("hashprice_usd"):
            prev_hp = float(prev_bmn["hashprice_usd"])
            hp_chg = ((hp - prev_hp) / prev_hp) * 100
            parts.append(f"Previous data point hashprice: ${prev_hp}/PH/day ({hp_chg:+.1f}% change)")
        if hp < 45:
            parts.append("⚠ Below S19 XP breakeven (~$45) — mid-tier miners under pressure")
        elif hp < 60:
            parts.append("S21 Pro profitable, but S19j Pro fleet (~$60 breakeven) at risk")
    if btc:
        parts.append(f"BTC at ${btc['price']:,.0f} (7d: {btc['change_7d_pct']:+.1f}%)")
    if net and net.get("hashrate_eh"):
        parts.append(f"Network hashrate: {net['hashrate_eh']} EH/s")
    if mempool_fees:
        parts.append(f"Tx fees: fastest {mempool_fees['fastest_sat_vb']} sat/vB — {'elevated, boosting all-in hashprice' if mempool_fees['fastest_sat_vb'] > 20 else 'low, subsidy-dominant revenue'}")
    ctx["hashprice_context"] = "\n".join(parts) if parts else "No hashprice data."

    # ── Difficulty context ──
    parts = []
    if net:
        if net.get("difficulty_t"):
            parts.append(f"Current difficulty: {net['difficulty_t']}T")
        if net.get("diff_change_pct"):
            parts.append(f"Last adjustment: {net['diff_change_pct']:+.1f}%")
        if net.get("hashrate_eh"):
            parts.append(f"Network hashrate: {net['hashrate_eh']} EH/s")
        if net.get("hashrate_7d_change_pct"):
            parts.append(f"Hashrate 7d trend: {net['hashrate_7d_change_pct']:+.1f}%")
    if mempool_diff:
        parts.append(f"Next adjustment: {mempool_diff['estimated_change_pct']:+.1f}% projected, {mempool_diff['remaining_blocks']} blocks remaining ({mempool_diff['progress_pct']:.0f}% through epoch)")
        if mempool_diff.get("estimated_date"):
            try:
                est = datetime.fromtimestamp(mempool_diff["estimated_date"] / 1000, tz=timezone.utc)
                parts.append(f"Estimated retarget date: {est.strftime('%Y-%m-%d')}")
            except Exception:
                pass
    if mempool_hr and mempool_hr.get("current_hashrate_eh"):
        parts.append(f"Mempool.space hashrate: {mempool_hr['current_hashrate_eh']} EH/s (cross-reference)")
    ctx["difficulty_context"] = "\n".join(parts) if parts else "No difficulty data."

    # ── BMN Revenue context ──
    parts = []
    if latest_bmn.get("bmn_revenue_btc"):
        parts.append(f"BMN daily revenue: ₿{latest_bmn['bmn_revenue_btc']}")
        if btc:
            usd_rev = float(latest_bmn["bmn_revenue_btc"]) * btc["price"]
            parts.append(f"USD equivalent: ~${usd_rev:,.0f}/day")
    if prev_bmn and prev_bmn.get("bmn_revenue_btc"):
        prev_rev = float(prev_bmn["bmn_revenue_btc"])
        curr_rev = float(latest_bmn.get("bmn_revenue_btc", 0))
        if prev_rev > 0 and curr_rev > 0:
            rev_chg = ((curr_rev - prev_rev) / prev_rev) * 100
            parts.append(f"Revenue change from previous data point: {rev_chg:+.1f}%")
    if latest_bmn.get("hashprice_usd"):
        parts.append(f"Current hashprice: ${latest_bmn['hashprice_usd']}/PH/day (primary revenue driver)")
    if latest_bmn.get("bmn_uptime_pct"):
        parts.append(f"Fleet uptime: {latest_bmn['bmn_uptime_pct']}%")
    ctx["bmn_context"] = "\n".join(parts) if parts else "No BMN revenue data."

    # ── STRC context ──
    parts = []
    if latest_bmn.get("strc_price"):
        sp = float(latest_bmn["strc_price"])
        premium_discount = "at par (neutral)" if abs(sp - 100) < 0.5 else ("above par — yield premium" if sp > 100.5 else "below par — credit/duration discount")
        parts.append(f"STRC: ${sp} ({premium_discount})")
    if latest_bmn.get("strc_btc_rating"):
        parts.append(f"BTC Rating: {latest_bmn['strc_btc_rating']}x")
        if prev_bmn and prev_bmn.get("strc_btc_rating"):
            prev_r = float(prev_bmn["strc_btc_rating"])
            curr_r = float(latest_bmn["strc_btc_rating"])
            direction = "rising (accretive)" if curr_r > prev_r else ("declining (dilutive)" if curr_r < prev_r else "flat")
            parts.append(f"BTC Rating trend: {direction} (prev: {prev_r}x)")
    if latest_bmn.get("strc_dividend_pct"):
        parts.append(f"Dividend yield: {latest_bmn['strc_dividend_pct']}%")
    if latest_bmn.get("strc_notional_m"):
        parts.append(f"Notional outstanding: ${latest_bmn['strc_notional_m']}M")
    ctx["strc_context"] = "\n".join(parts) if parts else "No STRC data."

    # ── BMN Fleet context ──
    parts = []
    if latest_bmn.get("bmn_active_miners"):
        parts.append(f"Active miners: {latest_bmn['bmn_active_miners']}")
    if latest_bmn.get("bmn_hashrate_24h_eh"):
        parts.append(f"Fleet hashrate (24h): {latest_bmn['bmn_hashrate_24h_eh']} EH/s")
        if net and net.get("hashrate_eh"):
            share = (float(latest_bmn["bmn_hashrate_24h_eh"]) / net["hashrate_eh"]) * 100
            parts.append(f"BMN share of global hashrate: {share:.2f}%")
    if latest_bmn.get("bmn_uptime_pct"):
        parts.append(f"Uptime: {latest_bmn['bmn_uptime_pct']}%")
    if latest_bmn.get("bmn_shares_efficiency"):
        parts.append(f"Shares efficiency: {latest_bmn['bmn_shares_efficiency']}%")
    if prev_bmn and prev_bmn.get("bmn_active_miners") and latest_bmn.get("bmn_active_miners"):
        miner_chg = int(latest_bmn["bmn_active_miners"]) - int(prev_bmn["bmn_active_miners"])
        if miner_chg != 0:
            parts.append(f"Miner count change: {miner_chg:+d} since previous data point")
    ctx["fleet_context"] = "\n".join(parts) if parts else "No fleet data."

    # ── Summary context (for top-level signal points) ──
    summary_parts = []
    if btc:
        summary_parts.append(f"BTC ${btc['price']:,.0f} ({btc['change_7d_pct']:+.1f}% 7d)")
    if latest_bmn.get("hashprice_usd"):
        summary_parts.append(f"Hashprice ${latest_bmn['hashprice_usd']}/PH/day")
    if mempool_diff:
        summary_parts.append(f"Next difficulty: {mempool_diff['estimated_change_pct']:+.1f}%")
    if latest_bmn.get("strc_price"):
        summary_parts.append(f"STRC ${latest_bmn['strc_price']}")
    if latest_bmn.get("bmn_revenue_btc"):
        summary_parts.append(f"BMN rev ₿{latest_bmn['bmn_revenue_btc']}/day")
    ctx["summary_context"] = " | ".join(summary_parts) if summary_parts else "No data."

    return ctx


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    chart_keys = ["btc_price", "hashprice", "difficulty", "bmn_revenue", "strc", "bmn_hashrate"]

    # ── Step 1: Fetch all data sources ──
    print("Fetching market data from multiple sources...")
    btc = fetch_btc_recent()
    net = fetch_network_data()
    mempool_diff = fetch_mempool_difficulty()
    mempool_fees = fetch_mempool_fees()
    mempool_hr = fetch_mempool_hashrate()
    manual = load_manual_data()
    prev_signals = load_previous_signals()

    sources_ok = sum(1 for x in [btc, net, mempool_diff, mempool_fees, mempool_hr] if x)
    print(f"  Data sources fetched: {sources_ok}/5 succeeded")

    # Extract latest + previous BMN data for delta framing
    latest_bmn = {}
    prev_bmn = {}
    if manual.get("data") and len(manual["data"]) >= 1:
        latest_bmn = manual["data"][-1]
    if manual.get("data") and len(manual["data"]) >= 2:
        prev_bmn = manual["data"][-2]

    # ── Step 2: Build per-chart context ──
    print("Building per-chart research context...")
    data_context = build_data_contexts(btc, net, mempool_diff, mempool_fees, mempool_hr, latest_bmn, prev_bmn)

    # ── Step 3: Run 6 focused Gemini calls (one per chart) ──
    all_signals = {}
    for i, key in enumerate(chart_keys):
        print(f"  [{i+1}/6] Generating signal for: {key}...")
        prompt = build_chart_prompt(key, data_context, prev_signals, today)
        if not prompt:
            print(f"    Skipped — no prompt for {key}", file=sys.stderr)
            all_signals[key] = ["Signal unavailable.", ""]
            continue

        try:
            raw = call_gemini(prompt)
            parsed = parse_gemini_json(raw)
            points = parsed.get("points", [])
            if isinstance(points, list) and len(points) >= 2:
                all_signals[key] = [points[0], points[1]]
            elif isinstance(points, list) and len(points) == 1:
                all_signals[key] = [points[0], ""]
            else:
                print(f"    Warning: unexpected format for {key}", file=sys.stderr)
                all_signals[key] = ["Signal generation error.", ""]
        except json.JSONDecodeError as e:
            print(f"    JSON parse error for {key}: {e}", file=sys.stderr)
            all_signals[key] = ["Signal generation error.", ""]

        # Brief pause between calls to be kind to free tier rate limits
        if i < len(chart_keys) - 1:
            time.sleep(2)

    # ── Step 4: Generate top-level STOKR Signal points ──
    print("  [7/7] Generating top-level STOKR Signal...")
    try:
        sp_prompt = build_signal_points_prompt(all_signals, data_context, today)
        raw = call_gemini(sp_prompt)
        parsed = parse_gemini_json(raw)
        signal_points = parsed.get("signal_points", [])
        if not isinstance(signal_points, list) or len(signal_points) < 3:
            signal_points = (signal_points or []) + [""] * 3
            signal_points = signal_points[:3]
    except (json.JSONDecodeError, Exception) as e:
        print(f"    Signal points generation error: {e}", file=sys.stderr)
        signal_points = ["Signal generation error.", "", ""]

    # ── Step 5: Assemble and write output ──
    result = {
        "signal_points": signal_points[:3],
        "signals": all_signals,
        "generated": datetime.now(timezone.utc).isoformat(),
        "data_snapshot": {
            "btc": btc,
            "network": net,
            "mempool_difficulty": mempool_diff,
            "mempool_fees": mempool_fees,
        },
    }

    out_path = "chart_signals.json"
    with open(out_path, "w") as f:
        json.dump(result, f, indent=2)

    print(f"\nWrote {out_path} ({len(json.dumps(result))} bytes)")
    print(f"Signal points: {len(result.get('signal_points', []))}")
    print(f"Chart signals: {list(result.get('signals', {}).keys())}")
    print("Done.")


if __name__ == "__main__":
    main()
