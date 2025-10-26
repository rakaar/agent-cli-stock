import argparse
import json
import math
import re
import sys
from datetime import datetime
from typing import Any, Dict, List, Optional

from zoneinfo import ZoneInfo
from urllib.parse import quote
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError



def _to_float(x: Any) -> Optional[float]:
    if x is None:
        return None
    if isinstance(x, (int, float)):
        return float(x)
    s = str(x).strip()
    if s in {"-", "—", "", "NA", "N/A", "null", "None"}:
        return None
    s = s.replace(",", "").replace("%", "").replace("₹", "").replace("Rs.", "").strip()
    # Handle units like Cr, Lac, Lakh
    mul = 1.0
    if s.endswith("Cr") or s.endswith("Cr.") or s.endswith("cr"):
        mul = 1e7
        s = re.sub(r"\s*[cC]r\.?$", "", s).strip()
    elif re.search(r"(Lac|Lakh)s?\.?$", s, re.I):
        mul = 1e5
        s = re.sub(r"\s*(Lac|Lakh)s?\.?$", "", s, flags=re.I).strip()
    try:
        return float(s) * mul
    except Exception:
        return None


def _to_int(x: Any) -> Optional[int]:
    f = _to_float(x)
    if f is None or math.isnan(f):
        return None
    try:
        return int(round(f))
    except Exception:
        return None


def _get(d: Dict[str, Any], path: str, default=None):
    cur = d
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def _parse_ts(s: Optional[str]) -> Optional[str]:
    if not s:
        return None
    # Common NSE format: '30-Sep-2025 15:30:00'
    for fmt in ["%d-%b-%Y %H:%M:%S", "%d-%b-%y %H:%M:%S"]:
        try:
            dt = datetime.strptime(s, fmt)
            # NSE times are IST
            dt = dt.replace(tzinfo=ZoneInfo("Asia/Kolkata"))
            return dt.isoformat()
        except Exception:
            continue
    # Fallback: return as-is
    return None


def normalize(symbol: str, merged: Dict[str, Any]) -> Dict[str, Any]:
    price_info = merged.get("priceInfo", {})
    security_info = merged.get("securityInfo", {})
    trade_info = merged.get("tradeInfo", {})
    info = merged.get("info", {})
    orderbook = merged.get("marketDeptOrderBook", {})
    corp = merged.get("corporate", {})

    ltp = _to_float(price_info.get("lastPrice")) or 0.0
    bid0 = (
        (orderbook.get("bid") or [{}])[0] if isinstance(orderbook.get("bid"), list) else {}
    )
    ask0 = (
        (orderbook.get("ask") or [{}])[0] if isinstance(orderbook.get("ask"), list) else {}
    )
    best_bid = _to_float(bid0.get("price")) or 0.0
    best_ask = _to_float(ask0.get("price")) or 0.0
    spread_abs = best_ask - best_bid if best_ask and best_bid else 0.0
    spread_pct = (spread_abs / ltp * 100.0) if ltp else 0.0

    week_hl = price_info.get("weekHighLow") or {}
    intra_hl = price_info.get("intraDayHighLow") or {}
    upper_band = _to_float(price_info.get("upperCP")) or _to_float(_get(price_info, "priceBand.upper")) or 0.0
    lower_band = _to_float(price_info.get("lowerCP")) or _to_float(_get(price_info, "priceBand.lower")) or 0.0
    prev_close = _to_float(price_info.get("previousClose")) or 0.0
    band_pct = ((upper_band - prev_close) / prev_close * 100.0) if prev_close else 0.0

    # Volatilities
    daily_vol = _to_float(trade_info.get("dailyVolatility")) or 0.0
    annual_vol = _to_float(trade_info.get("annualisedVolatility")) or 0.0

    # VaR and margins
    var = {
        "security_var": _to_float(trade_info.get("securityVar")) or 0.0,
        "index_var": _to_float(trade_info.get("indexVar")) or 0.0,
        "var_margin": _to_float(trade_info.get("varMargin")) or 0.0,
        "extreme_loss_rate": _to_float(trade_info.get("extremeLossRate")) or 0.0,
        "applicable_margin_rate": _to_float(trade_info.get("applicableMarginRate")) or 0.0,
    }

    # Orderbook arrays
    def _map_side(side: str) -> List[Dict[str, Any]]:
        arr = orderbook.get(side) or []
        out: List[Dict[str, Any]] = []
        for i in range(min(5, len(arr))):
            row = arr[i] or {}
            out.append({"p": _to_float(row.get("price")) or 0.0, "q": _to_int(row.get("quantity")) or 0})
        # pad to 5
        while len(out) < 5:
            out.append({"p": 0.0, "q": 0})
        return out

    ts_iso = _parse_ts(price_info.get("lastUpdateTime"))
    if not ts_iso:
        # fallback to now in IST
        ts_iso = datetime.now(tz=ZoneInfo("Asia/Kolkata")).isoformat()

    # Activity
    total_traded_volume = _to_int(price_info.get("totalTradedVolume")) or _to_int(trade_info.get("tradedVolume")) or 0
    total_traded_value = _to_float(price_info.get("totalTradedValue")) or _to_float(trade_info.get("tradedValue")) or 0.0
    value_cr = (total_traded_value / 1e7) if total_traded_value else 0.0

    # Deliverables
    pct_deliverable = _to_float(trade_info.get("deliveryToTradedQuantity")) or _to_float(trade_info.get("deliveryPositionPercent")) or 0.0

    # Ticker meta
    mcap_cr = _to_float(price_info.get("totalMarketCap"))
    ff_mcap_cr = _to_float(price_info.get("ffmc")) or _to_float(trade_info.get("freeFloatMarketCap"))
    industry = info.get("industry") or info.get("industryInfo") or "—"
    indices = info.get("indices") or []
    if isinstance(indices, str):
        indices = [indices]
    surv = security_info.get("surveillance") or security_info.get("surveillanceIndicator") or "—"

    # Build JSON
    # Corporate announcements (if present in payload)
    ann = corp.get("announcements") or []
    ann_slim: List[Dict[str, Any]] = []
    for a in ann[:10]:  # keep last 10 to match UI scale
        if not isinstance(a, dict):
            continue
        ann_slim.append({
            "time": str(a.get("dt") or a.get("date") or a.get("time") or a.get("announcementTime") or ""),
            "headline": a.get("headline") or a.get("subject") or a.get("title") or "",
            "desc": a.get("desc") or a.get("details") or "",
            "type": a.get("type") or a.get("category") or "",
            "pdf": a.get("pdfLink") or a.get("attachment") or "",
        })

    out = {
        "symbol": symbol.upper(),
        "ts": ts_iso,
        "quote": {
            "ltp": ltp,
            "chg": _to_float(price_info.get("change")) or 0.0,
            "chg_pct": _to_float(price_info.get("pChange")) or 0.0,
            "open": _to_float(price_info.get("open")) or 0.0,
            "day_high": _to_float(price_info.get("intraDayHighLow.max")) or _to_float(intra_hl.get("max")) or _to_float(price_info.get("dayHigh")) or 0.0,
            "day_low": _to_float(price_info.get("intraDayHighLow.min")) or _to_float(intra_hl.get("min")) or _to_float(price_info.get("dayLow")) or 0.0,
            "prev_close": prev_close,
            "avg_price": _to_float(price_info.get("vwap")) or 0.0,
        },
        "orderbook": {
            "spread_abs": spread_abs,
            "spread_pct": spread_pct,
            "bids": _map_side("bid"),
            "asks": _map_side("ask"),
            "total_buy_qty": _to_int(orderbook.get("totalBuyQuantity")) or 0,
            "total_sell_qty": _to_int(orderbook.get("totalSellQuantity")) or 0,
            "impact_cost": _to_float(trade_info.get("impactCost")) or 0.0,
        },
        "activity": {
            "volume_shares": total_traded_volume,
            "value_cr": value_cr,
        },
        "bands_vol": {
            "upper_band": upper_band or 0.0,
            "lower_band": lower_band or 0.0,
            "band_pct": band_pct,
            "daily_vol": daily_vol,
            "annual_vol": annual_vol,
            "tick_size": _to_float(security_info.get("tickSize")) or 0.05,
        },
        "var_margins": var,
        "ranges": {
            "wk52_high": _to_float(week_hl.get("max")) or _to_float(_get(price_info, "weekHighLow.max")) or 0.0,
            "wk52_low": _to_float(week_hl.get("min")) or _to_float(_get(price_info, "weekHighLow.min")) or 0.0,
        },
        "deliverables": {
            "pct_deliverable": pct_deliverable or 0.0,
        },
        "meta": {
            "face_value": _to_float(security_info.get("faceValue")) or 0.0,
            "market_lot": _to_int(security_info.get("marketLot")) or 1,
            "mcap_cr": (mcap_cr / 1e7) if mcap_cr else 0.0,
            "free_float_mcap_cr": (ff_mcap_cr / 1e7) if ff_mcap_cr else 0.0,
            "industry": industry or "—",
            "indices": indices or [],
            "surveillance_indicator": surv or "—",
            # Extra identity fields displayed on the quote page
            "series": info.get("series") or security_info.get("series") or "—",
            "isin": security_info.get("isin") or info.get("isin") or "—",
            "instrument": security_info.get("instrument") or info.get("instrument") or "—",
            "status": info.get("status") or security_info.get("tradingStatus") or "—",
            "board_name": security_info.get("boardName") or "—",
            "is_fno": bool(info.get("isFNOSec") or security_info.get("isFnO")),
        },
        "news_flags": {
            "has_fresh_announcement": bool((corp.get("announcements") or [])),
            "latest_announcement_time": None,
        },
        "corporate": {
            "announcements": ann_slim,
        },
    }

    # Populate latest announcement time if present
    latest_dt: Optional[str] = None
    for a in (corp.get("announcements") or []):
        # try typical keys
        for k in ["dt", "date", "time", "announcementTime"]:
            if k in a and a[k]:
                latest_dt = str(a[k])
                break
        if latest_dt:
            break
    if latest_dt:
        out["news_flags"]["latest_announcement_time"] = latest_dt

    # Derived intraday helpers
    try:
        total_buy = out["orderbook"]["total_buy_qty"]
        total_sell = out["orderbook"]["total_sell_qty"]
        denom = (total_buy + total_sell) or 1
        ob_imb = (total_buy - total_sell) / denom
    except Exception:
        ob_imb = 0.0

    ltp = out["quote"].get("ltp") or 0.0
    vwap = out["quote"].get("avg_price") or 0.0
    prev_close = out["quote"].get("prev_close") or 0.0
    vwap_dev_pct = ((ltp - vwap) / vwap * 100.0) if vwap else 0.0

    up_cp = out["bands_vol"].get("upper_band") or 0.0
    lo_cp = out["bands_vol"].get("lower_band") or 0.0
    prox_up = ((up_cp - ltp) / ltp * 100.0) if ltp and up_cp else None
    prox_lo = ((ltp - lo_cp) / ltp * 100.0) if ltp and lo_cp else None

    day_high = out["quote"].get("day_high") or 0.0
    day_low = out["quote"].get("day_low") or 0.0
    near_dh = (ltp and day_high and ((day_high - ltp) / ltp * 100.0) <= 0.2) or False
    near_dl = (ltp and day_low and ((ltp - day_low) / ltp * 100.0) <= 0.2) or False

    out["derived"] = {
        "order_imbalance_ratio": round(ob_imb, 4),
        "vwap_deviation_pct": round(vwap_dev_pct, 3),
        "circuit_proximity_pct": {
            "upper": round(prox_up, 3) if prox_up is not None else None,
            "lower": round(prox_lo, 3) if prox_lo is not None else None,
        },
        "near_day_extremes": {"near_high": bool(near_dh), "near_low": bool(near_dl)},
        "ltp_vs_prev_close_pct": round(((ltp - prev_close) / prev_close * 100.0), 3) if prev_close else 0.0,
    }

    return out


def run(symbol: str, headless: bool, timeout: int, engine: str = "firefox") -> Dict[str, Any]:
    from playwright.sync_api import Playwright, sync_playwright

    # URL-encode symbol to handle names like 'M&MFIN'
    sym_enc = quote(symbol.upper(), safe="")
    start_url = f"https://www.nseindia.com/get-quotes/equity?symbol={sym_enc}"

    merged: Dict[str, Any] = {}

    def merge_in(obj: Dict[str, Any]):
        nonlocal merged
        # Shallow merge for known sections
        for k, v in obj.items():
            if isinstance(v, dict):
                merged.setdefault(k, {}).update(v)
            else:
                merged[k] = v

    with sync_playwright() as p:
        browser_type = getattr(p, engine)
        # Try a couple of flags when chromium is used
        launch_args = {}
        if engine == "chromium":
            launch_args["args"] = ["--disable-http2", "--disable-features=NetworkService,NetworkServiceInProcess"]
        browser = browser_type.launch(headless=headless, **launch_args)
        # Engine-appropriate UA improves headless success rate
        if engine == "firefox":
            ua = (
                "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:130.0) "
                "Gecko/20100101 Firefox/130.0"
            )
        elif engine == "webkit":
            ua = (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/15.6 Safari/605.1.15"
            )
        else:
            ua = (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/118.0.0.0 Safari/537.36"
            )
        context = browser.new_context(
            user_agent=ua,
            viewport={"width": 1366, "height": 900},
            timezone_id="Asia/Kolkata",
            locale="en-IN",
            ignore_https_errors=True,
        )

        page = context.new_page()

        # Capture quote-equity API responses as the page loads/tabs switch
        def on_response(resp):
            try:
                url = resp.url
                ctype = resp.headers.get("content-type", "")
                if resp.ok and "application/json" in ctype:
                    data = resp.json()
                    if isinstance(data, dict):
                        # Primary: quote-equity sections
                        if "/api/quote-equity" in url:
                            merge_in(data)
                        else:
                            # Opportunistically merge common keys if present
                            keys_of_interest = {
                                "priceInfo",
                                "tradeInfo",
                                "securityInfo",
                                "marketDeptOrderBook",
                                "info",
                                "corporate",
                            }
                            if any(k in data for k in keys_of_interest):
                                merge_in(data)
            except Exception:
                pass

        page.on("response", on_response)

        # Try navigating directly to the symbol page first to reduce handshake issues
        try:
            page.goto(start_url, wait_until="domcontentloaded")
        except Exception:
            # Fallback: try commit and then allow scripts to run
            try:
                page.goto(start_url, wait_until="commit")
            except Exception:
                # We will rely on API fallback below
                pass

        # Wait a bit for network calls
        page.wait_for_timeout(1500)

        # Click through key tabs to trigger API calls
        tab_names = [
            r"Price Information|Price Info",
            r"Trade Information|Trade Info",
            r"Securities Information|Security Information",
            r"Order Book|Order book",
            r"Corporate|Corporate Actions|Announcements",
        ]
        for pat in tab_names:
            try:
                page.get_by_role("tab", name=re.compile(pat, re.I)).click(timeout=1500)
                page.wait_for_timeout(600)
            except Exception:
                continue

        # Allow more time for pending XHRs, bounded by timeout
        waited = 0
        step = 400
        while waited < max(1000, timeout):
            # Stop early when we have the essentials
            if merged.get("priceInfo") and merged.get("marketDeptOrderBook"):
                break
            page.wait_for_timeout(step)
            waited += step

        # As a fallback, try to extract a few from DOM if still missing
        try:
            if not merged.get("priceInfo"):
                # Attempt to read LTP and change from common selectors/text
                ltp_txt = page.locator("text=/LTP|Last Traded Price|LTP:/i").locator("xpath=../following-sibling::*").first.text_content()
                if ltp_txt:
                    merged.setdefault("priceInfo", {})["lastPrice"] = _to_float(ltp_txt)
        except Exception:
            pass

        # Final fallback: directly call NSE JSON endpoints using Playwright's request client
        if not merged.get("priceInfo"):
            try:
                # Try to pre-fetch cookies using stdlib (HTTP/1.1) to bypass HTTP/2 issues
                cookie_pairs: List[str] = []
                try:
                    req = Request("https://www.nseindia.com/", headers={
                        "User-Agent": ua,
                        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                        "Accept-Language": "en-IN,en;q=0.9",
                        "Connection": "keep-alive",
                    })
                    with urlopen(req, timeout=(timeout/1000.0 + 5)) as resp:
                        hdrs = resp.getheaders()
                        for k, v in hdrs:
                            if k.lower() == "set-cookie" and v:
                                # keep only name=value
                                parts = v.split(";")
                                if parts:
                                    nameval = parts[0].strip()
                                    if nameval:
                                        cookie_pairs.append(nameval)
                except Exception:
                    pass

                api = p.request.new_context(
                    base_url="https://www.nseindia.com",
                    extra_http_headers={
                        "User-Agent": ua,
                        "Accept": "application/json, text/plain, */*",
                        "Accept-Language": "en-IN,en;q=0.9",
                        "Referer": start_url,
                        "Origin": "https://www.nseindia.com",
                        "Connection": "keep-alive",
                        **({"Cookie": "; ".join(cookie_pairs)} if cookie_pairs else {}),
                    },
                )
                # Hit base and sectioned endpoints
                endpoints = [
                    f"/api/quote-equity?symbol={sym_enc}",
                    f"/api/quote-equity?symbol={sym_enc}&section=trade_info",
                    f"/api/quote-equity?symbol={sym_enc}&section=price_info",
                    f"/api/quote-equity?symbol={sym_enc}&section=security_info",
                ]
                for ep in endpoints:
                    try:
                        r = api.get(ep, timeout=timeout + 5000)
                        if r.ok:
                            data = r.json()
                            if isinstance(data, dict):
                                merge_in(data)
                    except Exception:
                        continue
            except Exception:
                pass

        browser.close()

    return normalize(symbol, merged)


def main():
    parser = argparse.ArgumentParser(description="Scrape NSE equity quote via Playwright and normalize JSON.")
    parser.add_argument("--symbol", required=True, help="Equity symbol, e.g., RELIANCE")
    parser.add_argument("--headless", action="store_true", help="Run browser in headless mode")
    parser.add_argument("--timeout", type=int, default=5000, help="Extra wait time in ms for network")
    parser.add_argument("--engine", default="firefox", choices=["firefox", "chromium", "webkit"], help="Playwright engine to use")
    parser.add_argument("--out", help="Write JSON to file instead of stdout")
    args = parser.parse_args()

    try:
        result = run(args.symbol, headless=args.headless, timeout=args.timeout, engine=args.engine)
    except Exception as e:
        print(json.dumps({"error": str(e)}), file=sys.stderr)
        sys.exit(2)

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, separators=(",", ":"))
    else:
        print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))


if __name__ == "__main__":
    main()
