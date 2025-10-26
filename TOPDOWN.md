# Top-Down Intraday Scanner

This adds a parallel, watchlist-driven workflow to complement the bottom-up (news-first) flow in `AGENTS.md`.

- Input: `watchlist.json` with an index and NSE symbols to track.
- Engine: Playwright-based NSE scrapes using `nse_scraper.py` per symbol, plus `fetch_nse_index_playwright.py` for index % change.
- Output: `topdown_scan.json` and `topdown_scan.md` with views BUY / WATCH / AVOID per `AGENTS.md` rules.

> Research screening only. Not trading advice.

---

## 1) One-time setup

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
.venv/bin/python -m playwright install firefox
```

---

## 2) Configure your watchlist

Edit `watchlist.json` (seeded with your screenshots):

```json
{
  "index": "NIFTY 50",
  "symbols": ["HINDCOPPER","M&MFIN","CANBK","AXISBANK","BANKBARODA","CENTRALBK","LEMONTREE","ORIENTHOT","SBIN","TATACOMM","TCS","TEJASNET","UNIONBANK","TATASTEEL","RPOWER","POWERINDIA","STEELXIND","HBLENGINE","RIIL"]
}
```

- Symbols must be exact NSE tickers. Use `company_master.json` to verify.
- You can add/remove symbols freely; duplicates are auto de-duped.

## 3) Run the scanner

Market hours (IST 09:15–15:30) recommended for live signals.

```bash
python3 scan_watchlist_intraday.py --headless --engine firefox --timeout 8000 --concurrency 4 \
  --watchlist watchlist.json --out topdown_scan.json --md-out topdown_scan.md
```

---

## 3.1) CLI Agent Mode (send human-ready message)

If you are invoking this from a CLI agent, use the flags below to print a concise message the agent can forward directly to the human. The message is emitted between markers so it’s easy to extract.

```bash
python3 scan_watchlist_intraday.py --headless --engine firefox --timeout 8000 --concurrency 4 \
  --watchlist watchlist.json --stdout --no-files --topn 5 --only-views BUY,WATCH
```

What the flags do:

- `--stdout`: print a short human-facing summary between markers.
- `--no-files`: skip writing `topdown_scan.json` and `topdown_scan.md`.
- `--topn`: limit items per view in the message (default 5).
- `--only-views`: restrict to certain views (comma list). Examples: `BUY` or `BUY,WATCH`.

Output structure:

```
--- BEGIN AGENT MESSAGE ---
Top-down scan — 2025-10-09 14:57 IST | NIFTY 50 +0.45%
Heuristic intraday views per AGENTS.md (research-screening, not trading advice).

BUY candidates (top 3):
- SBIN: LTP=₹858.05, chg%=+0.77 | Score 6/7, ΔVWAP=+0.94%, RS=+0.50%, OIR=1.70, near_high
- TCS: LTP=₹3027.00, chg%=+1.80 | Score 5/7, ΔVWAP=+0.60%, RS=+1.10%

WATCH candidates (top 3):
- TATASTEEL: LTP=₹171.94, chg%=+0.30 | Score 3/7, ΔVWAP=+0.10%, RS=-0.20%
--- END AGENT MESSAGE ---
```

Agent actions:

- Parse the text strictly between `BEGIN/END AGENT MESSAGE` and post it to the human as the recommendation note.
- If outside 09:15–15:30 IST, the scanner intentionally biases to WATCH; consider re-running during session for actionable signals.

---

## 4) Integrate with the news-first flow (`AGENTS.md`)

Use the bottom-up news to refine the top-down scan. This improves signal quality and avoids hallucinations.

- **Step A — Fetch latest news (limit 5)**
  - Run: `python3 fetch_stocks_news_top20.py` (writes `et_stocks_latest.json`).
  - Manually read the top 5 items (headline + body/summary) as per `AGENTS.md`.
  - Assign sectors using `sectors.json` and map direct company mentions via `company_master.json`.

- **Step B — Classify impact**
  - Mark each mention as: `first-order` (directly named company) or `second-order` (clear peer ripple; state why).
  - It is OK to state: "no relevant news found" for a symbol or the entire watchlist.

- **Step C — Run the top-down scan**
  - Example: `python3 scan_watchlist_intraday.py --headless --engine firefox --timeout 12000 --concurrency 4 --watchlist watchlist.json --stdout --no-files --only-views BUY,WATCH`.
  - This prints the BUY/WATCH list with intraday scores and signals.

- **Step D — Merge into a single note for the human**
  - For each symbol in the scan:
    - If it appears in the news (first-order), add a 1–2 line narrative that quotes or paraphrases the article and attach the link.
    - If it’s a plausible peer (second-order), say so explicitly and state the mechanism (e.g., "hotel RevPAR theme → ORIENTHOT").
    - If no article is relevant, write: "No direct/second-order news relevance today; view based on live signals only." This is acceptable.
  - Use `AGENTS.md` overrides after scoring (e.g., upgrade on concrete positive catalysts, downshift on adverse items).

Suggested overlay format to append to the agent message:

```
News overlay:
- <SYMBOL> — <first-order|second-order|none>: "<short quote/paraphrase>" (<link>)
  View: <BUY|WATCH|AVOID> (Score X/7). Signals: ΔVWAP +a.aa%, RS +b.bb%, OIR c.cc. Rationale: <1 sentence tying news to signals>.
```

Critical-thinking checklist (the agent should follow and explicitly state uncertainties):

- **Evidence**: quote/paraphrase a specific phrase; include the link.
- **Causality**: explain why the news should affect the symbol (first/second-order). If unsure, say "ambiguous".
- **Recency vs session**: if outside trading hours in IST, bias to WATCH.
- **Conflict**: if news is positive but live signals are weak (or vice-versa), call this out and reflect it in the view per `AGENTS.md` risk gates.
- **No-news path**: explicitly state "no relevant news found" when applicable; do not fabricate narratives.

---

## 5) What the script does (scoring and gates)

For each symbol it:

- Fetches a normalized NSE snapshot via `nse_scraper.run()`.
  - +1 if VWAP bias ≥ +0.5% (`vwap_deviation_pct`).
  - +1 if Relative Strength vs index ≥ +1.0% (`chg% - index pChange`).
  - +1 if Momentum ≥ +2.0% (`chg%`).
  - +1 if Order Imbalance Ratio ≥ 1.5 (`total_buy_qty / total_sell_qty`).
  - +1 if near day high OR within 0.5% of 52W high.
  - +1 Liquidity check requires spread ≤ 0.25% and 20D median volume; if median not available live, this point is skipped (not counted).
  - +1 Risk OK (not near upper circuit ≤1%, and margins not elevated).
- Applies quick risk gates and session awareness:
  - Outside trading hours → WATCH.
  - Near/at upper circuit → WATCH.
  - Abnormally high margins → down-weight to WATCH.

Outputs Markdown lines like:

```
- BUY — SBIN | Score 6/7 | LTP=₹858.05, VWAP=₹850.10, ΔVWAP=+0.94%, chg%=+0.77, RS=+0.50, OIR=1.7, near_high/low=True/False, circuit_prox=2.5%
  Score 6/7, ΔVWAP +0.94%, RS +0.50%, OIR 1.7.
```

---

## 6) Notes and tips

- Symbols with `&` (e.g., `M&MFIN`) are supported (URL-encoded in `nse_scraper.py`).
- If any fields are missing, increase `--timeout` (e.g., 12000).
- If Playwright fails intermittently, re-run; the script handles partial data and marks such items as WATCH/AVOID with a reason.
- For RS baseline you can switch indexes by changing `"index"` in `watchlist.json` (e.g., `"NIFTY BANK"`).

---

## 7) Programmatic usage

The scanner writes two files:

- `topdown_scan.json`: structured summary per symbol (score, view, core fields).
- `topdown_scan.md`: human-readable BUY/WATCH/AVOID list for quick review.

You can import `scan_watchlist_intraday.py` and call `main()` or adapt `scan_symbol()` for custom pipelines.

---

## 8) Safety / Guardrails

- Treat outputs as research-screening. Do not place trades solely based on this.
- Obey `AGENTS.md` prohibitions: no BUY when at/near circuit lock, or under restrictive surveillance / F&O ban (if available), and avoid over-reliance on missing data.
