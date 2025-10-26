
# 🔧 Runtime & Outputs

## Runtime environment

* The working directory is the repo root.
* **Environment variables are loaded from `.env` *before* running any scripts.**

  * Expected keys:

    * `TELEGRAM_BOT_TOKEN` — Bot token from @BotFather (keep secret).
    * `TELEGRAM_CHAT_ID` — Supergroup ID (e.g., `-1002799372540`).
  * Loader contract (bash):

    ```bash
    # Load .env into the environment (if present) before any commands
    if [ -f .env ]; then set -a; . ./.env; set +a; fi
    ```
  * Loader contract (Python entrypoint, optional):

    ```python
    # at top-level of the main script
    try:
        import dotenv, os
        if os.path.exists(".env"): dotenv.load_dotenv(".env")
    except Exception:
        pass
    ```

## Artifacts (files the agent MUST produce)

* `artifacts/market_items.json` — final, structured summary used for Telegram.

  * Schema (array of objects):

    ```json
    [
      {
        "title": "string (news headline or item title)",
        "link": "https://… (optional)",
        "symbol": "RELIANCE",
        "view": "BUY | WATCH | AVOID",
        "intraday_score": 0,
        "rationale": "1–2 lines concise reason"
      }
    ]
    ```
* Any intermediate scratch files go in `artifacts/tmp/` (OK to delete later).

## Telegram group notification (new capability)

After the market screening is complete, the agent must **post a concise summary** to the Telegram group:

* Use MarkdownV2 formatting and safe chunking at 4096 chars.
* Prefer the Python notifier (handles escaping, chunking, retries).
* The notification step **MUST NOT** block the main flow; log error and continue on failure.

---

## Purpose

* Whenever you are prompted to check the market, you need to do the following.
* Read the latest market news and suggest Indian stocks to **BUY / WATCH / AVOID** based on **human reading** of the news **plus** a disciplined check of **real-time NSE signals**.
* Decisions must **start from the news narrative** and be **refined** (not replaced) by real-time data. No naive keyword/NLP sector picks.

## High-Level Workflow

1. Fetch newest headlines (JSON), then **read** each item's headline and summary/body.
2. Assign affected sector(s) using `sectors.json` (human judgment only).
3. Map companies/brands to tickers via `company_master.json`.
4. For each **directly mentioned** company (and any high-conviction peers), fetch **real-time NSE** quote.
5. Produce a **BUY / WATCH / AVOID** view that ties the **news** to **live signals** (VWAP, order imbalance, circuit proximity, etc.), including one-line rationale and link.
6. **Load `.env` and send summary to Telegram group** (final notification step).

> Treat these as **research-screening** notes, not trading advice.

---

## Step 1 — Get Latest News (limit 5)

* Preferred feed: Economic Times Stocks/Markets (top 20 newest; **process latest 5**).
* From repo root:

  * `python3 fetch_stocks_news_top20.py`
* Output: `et_stocks_latest.json` (newest first; use first 5).

Sanity helpers:

```bash
python3 - <<'PY'
import json; d=json.load(open('et_stocks_latest.json'))
for i,x in enumerate(d[:5],1): print(i, x['title'])
PY
```

---

## Step 2 — Decide Sectors (Manual Reading Required)

* Read headline + summary/body for each of the 5 items.
* Choose sector(s) **only** from `sectors.json`.
* Prefer **narrow** sectors unless impact is clearly broad. Note ambiguity if unsure.

Quick peek:

```bash
python3 - <<'PY'
import json; print('\n'.join(json.load(open('sectors.json'))))
PY
```

---

## Step 3 — Map to Companies and Tickers

* Extract **direct** company mentions. Map brands → parents when obvious (“Jio” → Reliance Industries).
* Use `company_master.json` for exact symbol & sector.
* Consider peers only if article **clearly** implies sector ripple effects.

Examples:

```bash
# fuzzy by name
python3 - <<'PY'
import json; q='reliance'
cm=json.load(open('company_master.json'))
print(*[{k:x[k] for k in ('symbol','name','sector')} for x in cm if q.lower() in x['name'].lower()][:10], sep='\n')
PY
```

---

## Step 4 — Get Real-Time Market Snapshot (NSE)

Fetch quotes (Playwright Firefox, headless, from `.venv`):

```bash
.venv/bin/python -m playwright install firefox   # one-time
.venv/bin/python nse_scraper.py --symbol <SYMBOL> --engine firefox --headless --timeout 8000
```

Optional index context:

```bash
.venv/bin/python fetch_nse_index_playwright.py "NIFTY 50" --engine=firefox
```

If any field is missing, re-run with `--timeout 12000`.

**Session awareness** (IST):

* Ignore “live” signals outside **09:15–15:30 IST**. If outside session, treat as **stale** and switch to **WATCH** unless the news is exceptionally actionable.

---

## Step 4.1 — Real-Time Signals: What They Mean & How to Use Them

Below are the key objects/fields returned by `nse_scraper.py` and **exactly how to interpret them** intraday. Treat thresholds as **heuristics**, not absolutes.

### Quote (price & momentum)

* `ltp` — last traded price.

* `chg_pct` — % vs previous close.
  • **Context**: compare to index `% change` (from NIFTY 50).
  • **Relative Strength (RS)**: `RS = chg_pct - index_pChange`.
  • **Heuristics**:

  * RS ≥ **+1.0%** → **stronger** than market.
  * RS ≤ **−1.0%** → **weaker** than market.

* `avg_price` — **VWAP**.
  • `vwap_deviation_pct = 100 * (ltp - avg_price) / avg_price`.
  • Above VWAP by **≥+0.5%** = positive intraday bias; below by **≤−0.5%** = negative bias.

* `day_high/low` — intraday extremes.
  • `near_day_extremes` flags proximity; useful for **breakout/breakdown** context.

### Order Book (participation & pressure)

* `total_buy_qty` vs `total_sell_qty`; `spread_pct`; top-5 bids/asks.
  • **Order Imbalance Ratio (OIR)** ≈ `total_buy_qty / max(total_sell_qty,1)`.
  • OIR ≥ **1.5** → **buying pressure**; OIR ≤ **0.67** → **selling pressure**.
  • `spread_pct` > **0.25%** → thin liquidity; be cautious with **BUY** stance.

### Bands & Volatility

* `upper_band/lower_band` — circuit limits; `circuit_proximity_pct`.
  • If within **≤1.0%** of **upper** circuit: upside constrained; prefer **WATCH** (or **AVOID** if news doesn’t justify freeze risk).
  • If price is **locked** at circuit, note **no liquidity** for entry/exit.

* `daily_vol / annual_vol` — background risk.
  • High `daily_vol` (> peer median) = larger swings; **tighten** conviction.

### Risk & Margins

* `security_var`, `extreme_loss_rate`, `applicable_margin_rate`.
  • Elevated margins imply higher risk category; **down-weight** aggressive views.

### Ranges (context)

* `wk52_high / wk52_low`.
  • **Breakout** if `ltp ≥ wk52_high * 0.995` (within 0.5%).
  • **Breakdown** if `ltp ≤ wk52_low * 1.005`.
  • Breakouts paired with **VWAP > 0** and **RS > 0** strengthen **BUY**/**BUY on dips**.

### Deliverables

* `pct_deliverable` (usually prior day, not live).
  • Use **only as context**: rising delivery over recent days supports **sustained** moves.

### Derived (pre-computed intraday hints)

* `order_imbalance_ratio` — same idea as OIR above. Use thresholds **1.5 / 0.67**.
* `vwap_deviation_pct` — already computed. Use **±0.5%** and **±1.0%** tiers.
* `circuit_proximity_pct` — use **≤1.0%** as “near circuit”.
* `near_day_extremes` — booleans like `near_high: true` or `near_low: true`.
* `ltp_vs_prev_close_pct` — same as `chg_pct`; prefer the field that’s present.

---

## Step 4.2 — Quick Risk Gates (run before making a call)

1. **Trading status**: `meta.status` must be trading; else set **WATCH**.
2. **Surveillance**: If `meta.surveillance_indicator` is restrictive, **down-weight** to **WATCH** unless news is major.
3. **F&O ban**: If `meta.is_fno` and stock is in F&O **ban list** (if you fetch it elsewhere), avoid **BUY** calls.
4. **Circuit lock**: If price is **at** circuit, set **WATCH** (or **AVOID** if news is weak).

---

## Step 5 — Decision Framework (Score, then Override with Narrative)

Build a simple score, then allow the **news narrative** to override when justified.

**CRITICAL**: Always calculate the intraday score using **Python code** to avoid arithmetic errors. Do NOT calculate manually.

### 5.1 Intraday Score (0–7)

| Signal              | Condition                                                         | Pts |
| ------------------- | ----------------------------------------------------------------- | --- |
| VWAP bias           | `vwap_deviation_pct ≥ +0.5%`                                      | +1  |
| Stronger than index | `RS ≥ +1.0%`                                                      | +1  |
| Breakout context    | `near_high` or within 0.5% of 52W high                            | +1  |
| Liquidity decent    | `spread_pct <= 0.25%` **and** `volume_shares` above 20-day median* | +1  |
| Risk ok             | `circuit_proximity_pct > 1.0%` **and** margins not elevated       | +1  |

* If 20-day median not available live, skip this point and note "liquidity check skipped".

**Calculation Method**

**ALWAYS use Python code** to calculate the intraday score. Example:

```python
score = 0
if vwap_deviation_pct >= 0.5: score += 1
if rs >= 1.0: score += 1
if chg_pct >= 2.0: score += 1
if order_imbalance_ratio >= 1.5: score += 1
if near_high or (ltp >= wk52_high * 0.995): score += 1
if spread_pct <= 0.25 and volume_shares is not None and volume_shares > 20: score += 1
if circuit_proximity_upper > 1.0: score += 1
print(f"Intraday Score: {score}/7")
```

**Interpretation**

* **6–7** → BUY (or BUY on dips if extended).
* **4–5** → BUY **or** WATCH (use news strength to decide).
* **0–1** → AVOID.

### 5.2 Narrative Overrides (apply after scoring)

* **Positive, concrete catalysts** (orders won, regulatory approvals, guidance upgrades, verified M&A terms): **upgrade** one notch (WATCH→BUY, AVOID→WATCH).
* **Negative, fundamental hits** (fraud, downgrades with specifics, adverse regulation, guidance cuts): **downgrade** one notch.
* **Upper-circuit near/locked** with fresh news: prefer **WATCH** (entry/exit impractical).
* **Thin order book / wide spread**: cap at **WATCH**.

---

## Output Template (per article)

* **Title**: <headline>
* **Link**: <url>
* **Sectors**: <from sectors.json>
* **Direct companies**: `<SYMBOL (Name)> …`
* **Peers/second-order**: <optional>
* **Realtime**: `LTP=₹…, VWAP=₹…, ΔVWAP=+x.xx%, chg%=+y.yy, RS=+z.zz, OIR=…, near_high/low=…, circuit_prox=…%`
* **Intraday Score**: `X/7` (calculated via Python)
* **View**: **BUY / WATCH / AVOID —** one-line rationale that **explicitly** ties the **news** to **signals** and **references the intraday score**.

Keep it to **1–3 sentences**.

---

## Worked Examples (from your notes)

### Example A — ASTERDM

**Context provided**: `ltp=₹695.7`, `VWAP=₹691.8`, `chg%=+5.15%`, `near_day_extremes=false`.

**Calculate score using Python**:
```python
vwap_deviation_pct = (695.7 - 691.8) / 691.8 * 100  # +0.56%
rs = 5.15 - 0.7  # +4.45% (assume index +0.7%)
score = 0
if vwap_deviation_pct >= 0.5: score += 1  # +1
if rs >= 1.0: score += 1  # +1
if 5.15 >= 2.0: score += 1  # +1 (momentum)
# OIR unknown: +0
# Not near high/low: +0
# Spread unknown: +0
# Circuit proximity assumed ok: +1
score += 1
print(f"Score: {score}/7")  # 4/7
```

**Score = 4/7** → **BUY or WATCH**.
**Call (example)**: If the **news** is a concrete positive catalyst (e.g., deal closure), say **BUY**; otherwise **WATCH** with "buy on dips" if it stays **above VWAP** and RS remains >0.

**Rationale**: "Score 4/7: Positive live momentum above VWAP (+0.56%) and strong RS vs NIFTY (+4.45%). If news is structural, initiate small BUY; else WATCH for consolidation above VWAP."

---

### Example B — SAATVIKGL

**Context provided**: `ltp=₹506`, `VWAP=₹486.28`, `chg%=+10.0%`, `near_high=true`, `upper_band=₹506`, `wk52_high=₹506` (hit upper circuit & 52-week high).

**Calculate score using Python**:
```python
vwap_deviation_pct = (506 - 486.28) / 486.28 * 100  # +4.05%
rs = 10.0 - 0.0  # +10.0% (assume index flat)
score = 0
if vwap_deviation_pct >= 0.5: score += 1  # +1
if rs >= 1.0: score += 1  # +1
if 10.0 >= 2.0: score += 1  # +1 (momentum)
# OIR unknown: +0
if True: score += 1  # +1 (near_high)
# Spread unknown: +0
circuit_prox = (506 - 506) / 506 * 100  # 0%
if circuit_prox > 1.0: score += 0  # +0 (at circuit)
print(f"Score: {score}/7")  # 4/7
```

**Score = 4/7**, **BUT** near/at upper circuit (circuit_proximity ≤1%) → **risk gate** → **WATCH**, not BUY (despite score).

**Call**: **WATCH**.
**Rationale**: "Score 4/7 but at upper circuit and 52-week breakout; liquidity constrained. Strong RS and above VWAP, but entry/exit impractical—WATCH for next liquid session."

---

## Allowed Tools

* `grep`, `sed`, `awk` and small Python one-liners for lookups.
* Re-run ET fetcher to refresh headlines.
* NSE Playwright scripts:

  * `.venv/bin/python nse_scraper.py --symbol <SYMBOL> --engine firefox --headless --timeout 8000`
  * `.venv/bin/python fetch_nse_index_playwright.py "NIFTY 50" --engine=firefox`

---

## Prohibited / Guardrails

* **No** NLP/keyword sector classifiers—**read** the article.
* **No** symbol substring guesses without reading context.
* **No** trading advice; this is **research screening**.
* **No** “BUY” when at/near **circuit lock**, in **F&O ban**, or with **abnormal surveillance** flags.

---

## Quality Bar

* Quote or paraphrase **specific** phrases from the article when justifying sector/company selection.
* **Always calculate and display the intraday score (X/7) using Python code**.
* Anchor the **view** in **two live signals** (e.g., VWAP bias + RS, or OIR + near_high) **and reference the intraday score**.
* Be concise and specific; **1–3 sentences** per item.
* Always articulate **why** the live signals **support or contradict** the news narrative.

---

## Notes on Missing/Noisy Data

* If a field is missing, **state it** and proceed with reduced confidence.
* If quotes are stale (outside market hours), mark **WATCH** and rely on the **news** + **context** only.
* Retries with higher `--timeout` if Playwright times out.

---

## Key Files

* `ET_news_rss.md` — how to fetch ET feeds.
* `fetch_stocks_news_top20.py` → `et_stocks_latest.json`.
* `company_master.json` — name→symbol map.
* `sectors.json` — canonical sector list.
* `nse_scraper.py` — live NSE snapshot with derived signals.
* `fetch_nse_index_playwright.py` — NIFTY 50 % change for RS.

---

---

## Step 6 — Send Telegram Notification

After completing the market screening and building the final JSON file:

1. **Load environment variables** (required first step):

   ```bash
   if [ -f .env ]; then set -a; . ./.env; set +a; fi
   ```

2. **Send to Telegram using existing notifier**:

   ```bash
   python3 notify_telegram.py artifacts/market_items.json
   ```

**Important Notes:**
* The notification step **must not** block the main flow; log errors and continue if sending fails
* Never print secrets in logs; tokens only come from environment variables
* Outside trading hours, still post with **WATCH** by session rule
* Keep Telegram posts compact: 1 bullet per item with "symbol — VIEW — score — 1-liner"

---

### TL;DR for the Agent

1. **Start with the news** → sector + companies.
2. **Fetch live** → look at **VWAP bias**, **RS vs NIFTY**, **OIR**, **near_high/52W**, **circuit proximity**, **spread/liquidity**.
3. **Calculate intraday score (0–7) using Python code** (never manually), then **override** with **news strength** and **risk gates**.
4. Output **BUY / WATCH / AVOID** with **intraday score (X/7)** + **two signals + one narrative line that references the score**.
5. **Always show the intraday score to the user** in your final output.
6. **Build `artifacts/market_items.json`** and **send Telegram notification** (load `.env` first, use `notify_telegram.py`).


