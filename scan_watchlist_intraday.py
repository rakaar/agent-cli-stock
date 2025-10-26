#!/usr/bin/env python3
"""
Top-down intraday scanner for a fixed watchlist.

- Reads symbols from watchlist.json
- Fetches NIFTY 50 % change for RS
- Scrapes each symbol via nse_scraper.run() (parallel)
- Computes 0–7 intraday score per AGENTS.md
- Applies quick risk gates and session-awareness
- Emits JSON and Markdown summaries
"""
from __future__ import annotations

import argparse
import concurrent.futures as futures
import json
from dataclasses import dataclass
from datetime import datetime, time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from zoneinfo import ZoneInfo

# Local modules
from fetch_nse_index_playwright import fetch_index  # type: ignore
from nse_scraper import run as scrape_symbol  # type: ignore

# Heuristics/constants mirrored from AGENTS.md
VWAP_POS_THR = 0.5  # %
RS_STRONG_THR = 1.0  # % stronger than index
MOMENTUM_THR = 2.0  # % day change considered momentum
OIR_BUY_THR = 1.5   # buy pressure
SPREAD_GOOD_THR = 0.25  # %
CIRCUIT_NEAR_THR = 1.0  # % (near/at upper circuit)
W52_NEAR_THR = 0.5  # % within 52W high considered breakout vicinity
MARGIN_ELEVATED_THR = 75.0  # heuristic; applicable_margin_rate > this = elevated


@dataclass
class ScanResult:
    symbol: str
    raw: Dict[str, Any]
    index_pChange: float
    score: int
    rs: Optional[float]
    components: Dict[str, Any]
    view: str
    rationale: str
    risk_flags: List[str]
    session_live: bool


def ist_now() -> datetime:
    return datetime.now(tz=ZoneInfo("Asia/Kolkata"))


def market_session_live(now_ist: Optional[datetime] = None) -> bool:
    now_ist = now_ist or ist_now()
    start = time(9, 15)
    end = time(15, 30)
    t = now_ist.timetz()
    return start <= t.replace(tzinfo=None) <= end


def compute_intraday_score(d: Dict[str, Any], index_pChange: Optional[float]) -> Tuple[int, Dict[str, Any], List[str]]:
    q = d.get("quote", {})
    ob = d.get("orderbook", {})
    der = d.get("derived", {})
    ranges = d.get("ranges", {})
    var = d.get("var_margins", {})

    # Extract fields
    ltp = float(q.get("ltp") or 0.0)
    chg_pct = float(q.get("chg_pct") or der.get("ltp_vs_prev_close_pct") or 0.0)
    vwap_dev = float(der.get("vwap_deviation_pct") or 0.0)
    near_high = bool(((der.get("near_day_extremes") or {}).get("near_high")) or False)
    wk52_high = float(ranges.get("wk52_high") or 0.0)

    # Relative strength vs index
    rs = None
    if index_pChange is not None:
        rs = chg_pct - float(index_pChange)

    # Order imbalance ratio (buy/sell). Prefer computed ratio over 'derived' (which may be a diff ratio)
    tbq = float(ob.get("total_buy_qty") or 0.0)
    tsq = float(ob.get("total_sell_qty") or 0.0)
    oir = None
    if tbq > 0 and tsq > 0:
        oir = tbq / max(tsq, 1.0)

    spread_pct = float(ob.get("spread_pct") or 0.0)
    volume_shares = int((d.get("activity") or {}).get("volume_shares") or 0)

    prox = (der.get("circuit_proximity_pct") or {})
    prox_up = prox.get("upper")
    applicable_margin = float((d.get("var_margins") or {}).get("applicable_margin_rate") or 0.0)

    # 52W breakout vicinity
    near_52w = False
    if wk52_high and ltp:
        near_52w = (ltp >= wk52_high * (1 - W52_NEAR_THR / 100.0))

    # Liquidity check per AGENTS.md: requires spread <= 0.25% AND 20d median volume; we skip the latter if unavailable
    liquidity_point_awarded = False
    liquidity_note = "liquidity check skipped (no 20d median)"
    if spread_pct <= SPREAD_GOOD_THR:
        # can't verify 20d median; do not award point but mark spread ok
        pass

    risk_ok = (prox_up is None or prox_up > CIRCUIT_NEAR_THR) and (applicable_margin <= MARGIN_ELEVATED_THR or applicable_margin == 0.0)

    score = 0
    components: Dict[str, Any] = {
        "vwap_bias": vwap_dev,
        "rs": rs,
        "momentum_pct": chg_pct,
        "oir": oir,
        "near_high": bool(near_high),
        "near_52w": bool(near_52w),
        "spread_pct": spread_pct,
        "liquidity_point": liquidity_point_awarded,
        "circuit_proximity_upper_pct": prox_up,
        "risk_ok": risk_ok,
        "applicable_margin_rate": applicable_margin,
        "volume_shares": volume_shares,
    }

    # Scoring per AGENTS.md (0–7)
    if vwap_dev >= VWAP_POS_THR:
        score += 1
    if rs is not None and rs >= RS_STRONG_THR:
        score += 1
    if chg_pct >= MOMENTUM_THR:
        score += 1
    if (oir is not None) and (oir >= OIR_BUY_THR):
        score += 1
    if near_high or near_52w:
        score += 1
    if liquidity_point_awarded:
        score += 1
    if risk_ok:
        score += 1

    risk_flags: List[str] = []
    if prox_up is not None and prox_up <= CIRCUIT_NEAR_THR:
        risk_flags.append("near_upper_circuit")
    if applicable_margin > MARGIN_ELEVATED_THR:
        risk_flags.append("elevated_margin")

    components["liquidity_note"] = liquidity_note

    return score, components, risk_flags


def evaluate_view(score: int, session_live: bool, risk_flags: List[str]) -> str:
    # Quick gates
    if not session_live:
        return "WATCH"
    if "near_upper_circuit" in risk_flags:
        return "WATCH"
    # Score-driven
    if score >= 6:
        return "BUY"
    if score >= 4:
        return "WATCH"
    if score <= 1:
        return "AVOID"
    return "WATCH"


def build_rationale(symbol: str, score: int, comps: Dict[str, Any], rs: Optional[float]) -> str:
    parts = []
    parts.append(f"Score {score}/7")
    # Two signals: VWAP bias and RS by default
    parts.append(f"ΔVWAP={comps['vwap_bias']:+.2f}%")
    if rs is not None:
        parts.append(f"RS={rs:+.2f}%")
    # Add one more if strong OIR or breakout
    if comps.get("oir") is not None and comps["oir"] >= OIR_BUY_THR:
        parts.append(f"OIR={comps['oir']:.2f}")
    elif comps.get("near_high") or comps.get("near_52w"):
        parts.append("near_high/52W")
    return ", ".join(parts)


def scan_symbol(symbol: str, index_pChange: float, timeout: int, engine: str, headless: bool, session_live: bool) -> ScanResult:
    data = scrape_symbol(symbol, headless=headless, timeout=timeout, engine=engine)
    score, comps, risk_flags = compute_intraday_score(data, index_pChange)
    view = evaluate_view(score, session_live=session_live, risk_flags=risk_flags)
    rationale = build_rationale(symbol, score, comps, comps.get("rs"))
    return ScanResult(
        symbol=symbol,
        raw=data,
        index_pChange=index_pChange,
        score=score,
        rs=comps.get("rs"),
        components=comps,
        view=view,
        rationale=rationale,
        risk_flags=risk_flags,
        session_live=session_live,
    )


def to_md(results: List[ScanResult]) -> str:
    lines: List[str] = []
    now = ist_now().strftime("%Y-%m-%d %H:%M IST")
    lines.append(f"# Top-down Intraday Scan — {now}")
    lines.append("")
    lines.append("Sorted by score (desc). Views follow AGENTS.md risk gates and session awareness.")
    lines.append("")
    # Group by view for readability
    order = {"BUY": 0, "WATCH": 1, "AVOID": 2}
    results_sorted = sorted(results, key=lambda r: (order.get(r.view, 9), -r.score, r.symbol))
    for r in results_sorted:
        q = r.raw.get("quote", {})
        der = r.raw.get("derived", {})
        vwap = q.get("avg_price") or 0
        ltp = q.get("ltp") or 0
        chg_pct = q.get("chg_pct") or 0
        oir = r.components.get("oir")
        near_ext = der.get("near_day_extremes") or {}
        cp = (der.get("circuit_proximity_pct") or {}).get("upper")
        lines.append(f"- **{r.view} — {r.symbol}** | Score {r.score}/7 | LTP=₹{ltp:.2f}, VWAP=₹{vwap:.2f}, ΔVWAP={r.components['vwap_bias']:+.2f}%, chg%={chg_pct:+.2f}, RS={(r.rs or 0):+.2f}, OIR={(oir if oir is not None else 'NA')}, near_high/low={near_ext.get('near_high')}/{near_ext.get('near_low')}, circuit_prox={(cp if cp is not None else 'NA')}%\n  {r.rationale}")
    lines.append("")
    return "\n".join(lines)


def to_message(results: List[ScanResult], index_name: str, index_pChange: float, topn: int = 5, only_views: Optional[List[str]] = None) -> str:
    """Build a short, human-ready message the CLI agent can forward.

    - Groups by view (BUY, WATCH, AVOID), sorted by score desc.
    - Limits to topn per group.
    - Single-line bullets per symbol referencing the intraday score and signals.
    """
    only_views = [v.strip().upper() for v in (only_views or ["BUY", "WATCH", "AVOID"])]
    order = {"BUY": 0, "WATCH": 1, "AVOID": 2}
    results_sorted = sorted(results, key=lambda r: (order.get(r.view, 9), -r.score, r.symbol))

    groups: Dict[str, List[ScanResult]] = {v: [] for v in ["BUY", "WATCH", "AVOID"]}
    for r in results_sorted:
        if r.view in groups:
            groups[r.view].append(r)

    now = ist_now().strftime("%Y-%m-%d %H:%M IST")
    hdr = f"Top-down scan — {now} | {index_name} {index_pChange:+.2f}%\n"
    hdr += "Heuristic intraday views per AGENTS.md (research-screening, not trading advice).\n"

    lines: List[str] = [hdr]
    for view in ["BUY", "WATCH", "AVOID"]:
        if view not in only_views:
            continue
        items = groups.get(view, [])[: max(0, topn)]
        if not items:
            continue
        lines.append(f"{view} candidates (top {len(items)}):")
        for r in items:
            q = r.raw.get("quote", {})
            der = r.raw.get("derived", {})
            vwap = q.get("avg_price") or 0
            ltp = q.get("ltp") or 0
            chg_pct = q.get("chg_pct") or 0
            oir = r.components.get("oir")
            near_ext = der.get("near_day_extremes") or {}
            cp = (der.get("circuit_proximity_pct") or {}).get("upper")
            sigs = [
                f"Score {r.score}/7",
                f"ΔVWAP={r.components['vwap_bias']:+.2f}%",
                f"RS={(r.rs or 0):+.2f}%",
            ]
            if oir is not None:
                sigs.append(f"OIR={oir:.2f}")
            if near_ext.get("near_high"):
                sigs.append("near_high")
            if cp is not None:
                sigs.append(f"circuit_prox={cp:.2f}%")
            lines.append(
                f"- {r.symbol}: LTP=₹{ltp:.2f}, chg%={chg_pct:+.2f} | " + ", ".join(sigs)
            )
        lines.append("")
    return "\n".join(lines).strip()


def main() -> int:
    ap = argparse.ArgumentParser(description="Scan a watchlist and compute intraday BUY/WATCH/AVOID views.")
    ap.add_argument("--watchlist", default="watchlist.json", help="Path to watchlist.json")
    ap.add_argument("--engine", default="firefox", choices=["firefox", "chromium", "webkit"], help="Playwright engine")
    ap.add_argument("--headless", action="store_true", help="Run browsers headless")
    ap.add_argument("--timeout", type=int, default=8000, help="Per-symbol extra wait in ms")
    ap.add_argument("--concurrency", type=int, default=3, help="Max concurrent scrapes")
    ap.add_argument("--out", default="topdown_scan.json", help="Write JSON output here")
    ap.add_argument("--md-out", default="topdown_scan.md", help="Write Markdown summary here")
    ap.add_argument("--stdout", action="store_true", help="Print a concise human-facing message to STDOUT between markers")
    ap.add_argument("--no-files", action="store_true", help="Do not write JSON/MD files if set")
    ap.add_argument("--topn", type=int, default=5, help="Top-N per group to include in the STDOUT message")
    ap.add_argument("--only-views", default="BUY,WATCH,AVOID", help="Comma list of views to include in the STDOUT message")
    args = ap.parse_args()

    wl = json.load(open(args.watchlist))
    symbols: List[str] = list(dict.fromkeys(wl.get("symbols", [])))  # de-dup preserve order

    # Index for RS
    index_name = wl.get("index", "NIFTY 50")
    idx = fetch_index(index_name, engine=args.engine, headed=not args.headless)
    index_pChange = float(idx.get("pChange") or 0.0)

    sess_live = market_session_live()

    results: List[ScanResult] = []

    def _task(sym: str) -> ScanResult:
        return scan_symbol(sym, index_pChange=index_pChange, timeout=args.timeout, engine=args.engine, headless=args.headless, session_live=sess_live)

    # Concurrency with threads (playwright contexts per-symbol)
    with futures.ThreadPoolExecutor(max_workers=max(1, min(args.concurrency, len(symbols)))) as ex:
        futs = [ex.submit(_task, s) for s in symbols]
        for f in futures.as_completed(futs):
            try:
                results.append(f.result())
            except Exception as e:
                # Represent failures as AVOID with reason
                sym = symbols[len(results)] if len(results) < len(symbols) else "?"
                fake = {
                    "quote": {"ltp": 0, "avg_price": 0, "chg_pct": 0},
                    "derived": {"vwap_deviation_pct": 0, "near_day_extremes": {"near_high": False, "near_low": False}, "circuit_proximity_pct": {"upper": None}},
                }
                results.append(ScanResult(
                    symbol=sym,
                    raw=fake,
                    index_pChange=index_pChange,
                    score=0,
                    rs=0.0,
                    components={"vwap_bias": 0.0, "oir": None, "near_high": False, "near_52w": False, "spread_pct": None, "liquidity_point": False, "circuit_proximity_upper_pct": None, "risk_ok": False, "applicable_margin_rate": None, "volume_shares": None},
                    view="WATCH" if not sess_live else "AVOID",
                    rationale=f"Error fetching {sym}: {e}",
                    risk_flags=["fetch_error"],
                    session_live=sess_live,
                ))

    if not args.no_files:
        # Persist JSON (slim)
        out_json = [
            {
                "symbol": r.symbol,
                "view": r.view,
                "score": r.score,
                "index_pChange": r.index_pChange,
                "rs": r.rs,
                "components": r.components,
                "risk_flags": r.risk_flags,
                "session_live": r.session_live,
                "quote": {
                    "ltp": r.raw.get("quote", {}).get("ltp"),
                    "avg_price": r.raw.get("quote", {}).get("avg_price"),
                    "chg_pct": r.raw.get("quote", {}).get("chg_pct"),
                },
            }
            for r in results
        ]
        Path(args.out).write_text(json.dumps(out_json, indent=2, ensure_ascii=False))

        # Markdown summary
        Path(args.md_out).write_text(to_md(results))

        print(f"Wrote {len(results)} items to {args.out} and {args.md_out}")

    if args.stdout:
        only_views = [v.strip() for v in (args.only_views.split(",") if args.only_views else []) if v.strip()]
        msg = to_message(results, index_name=index_name, index_pChange=index_pChange, topn=args.topn, only_views=only_views)
        print("\n--- BEGIN AGENT MESSAGE ---")
        print(msg)
        print("--- END AGENT MESSAGE ---\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
