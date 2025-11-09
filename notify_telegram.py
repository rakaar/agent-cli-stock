#!/usr/bin/env python3
import os
import sys
import json
import time
from typing import Any, Dict, List, Optional

# Optional: load .env if present (won't fail if dotenv missing)
try:
    import dotenv
    if os.path.exists(".env"):
        dotenv.load_dotenv(".env")
except Exception:
    pass

try:
    import requests
except Exception:
    requests = None  # type: ignore


MDV2_SPECIAL = ['_', '*', '[', ']', '(', ')', '~', '`', '>', '#', '+', '-', '=', '|', '{', '}', '.', '!']


def mdv2_escape(text: str) -> str:
    out = text or ""
    for ch in MDV2_SPECIAL:
        out = out.replace(ch, f"\\{ch}")
    return out


def emoji_for_view(view: str) -> str:
    return {"BUY": "🟢", "WATCH": "🟡", "AVOID": "🔴"}.get(view.upper(), "⚪")


def _format_metrics(item: Dict[str, Any]) -> Optional[str]:
    # Try a few known shapes to surface metrics if available
    comps = item.get("components") or item.get("metrics") or {}
    quote = item.get("quote") or {}

    parts: List[str] = []

    # Prefer precomputed metrics if present
    if isinstance(comps, dict):
        if comps.get("vwap_bias") is not None:
            parts.append(f"ΔVWAP={float(comps['vwap_bias']):+.2f}%")
        if comps.get("rs") is not None:
            parts.append(f"RS={float(comps['rs']):+.2f}%")
        if comps.get("oir") is not None:
            parts.append(f"OIR={float(comps['oir']):.2f}")
        if comps.get("near_high") or comps.get("near_52w"):
            parts.append("near_high/52W")
        if comps.get("circuit_proximity_upper_pct") is not None:
            parts.append(f"circuit_prox={float(comps['circuit_proximity_upper_pct']):.2f}%")
        if comps.get("spread_pct") is not None:
            parts.append(f"spread={float(comps['spread_pct']):.2f}%")

    # Fallbacks from quote if present
    if quote:
        if quote.get("ltp") is not None:
            parts.insert(0, f"LTP=₹{float(quote['ltp']):.2f}")
        if quote.get("avg_price") is not None and quote.get("ltp") is not None:
            try:
                vdev = (float(quote['ltp']) - float(quote['avg_price'])) / float(quote['avg_price']) * 100.0
                if not any(p.startswith("ΔVWAP=") for p in parts):
                    parts.append(f"ΔVWAP={vdev:+.2f}%")
            except Exception:
                pass
        if quote.get("chg_pct") is not None:
            parts.append(f"chg%={float(quote['chg_pct']):+.2f}")

    if not parts:
        return None
    return ", ".join(parts)


def format_market_summary_detailed(items: List[Dict[str, Any]]) -> str:
    if not items:
        return "📊 *Market Screening*\n\nNo items to report."

    lines: List[str] = []
    lines.append("📊 *Market Screening — Detailed*")
    lines.append("")

    for it in items:
        symbol = mdv2_escape(str(it.get("symbol", "N/A")))
        view = str(it.get("view", "WATCH")).upper()
        score = it.get("intraday_score")
        if score is None:
            score = it.get("score")
        try:
            score_int = int(score) if score is not None else 0
        except Exception:
            score_int = 0
        title = str(it.get("title", ""))
        link = str(it.get("link", ""))
        rationale = str(it.get("rationale", "")).strip()

        # Header line: emoji, symbol, view, score
        lines.append(f"{emoji_for_view(view)} *{symbol}* — {view} — Score {score_int}/7")

        # Title + link line
        if title and link:
            lines.append(f"[{mdv2_escape(title)}]({link})")
        elif title:
            lines.append(mdv2_escape(title))

        # Metrics line if available
        metrics = _format_metrics(it)
        if metrics:
            lines.append(mdv2_escape(metrics))

        # Rationale line
        if rationale:
            lines.append("📝 " + mdv2_escape(rationale))

        lines.append("")

    return "\n".join(lines).strip()


def send_markdown_message(token: str, chat_id: str, text: str, timeout: float = 10.0) -> bool:
    if requests is None:
        print("notify_telegram: requests not available; cannot send")
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "MarkdownV2",
        "disable_web_page_preview": True,
    }
    try:
        resp = requests.post(url, json=payload, timeout=timeout)
        if resp.status_code != 200:
            print(f"notify_telegram: send failed {resp.status_code} {resp.text[:200]}")
            return False
        return True
    except Exception as e:
        print(f"notify_telegram: exception sending message: {e}")
        return False


def send_with_chunking(token: str, chat_id: str, message: str, max_len: int = 4096) -> bool:
    # Split by lines to avoid breaking markdown structures
    lines = message.splitlines()
    chunks: List[str] = []
    cur: List[str] = []
    cur_len = 0
    for ln in lines:
        add_len = len(ln) + 1  # + newline
        if cur_len + add_len > max_len - 50:  # margin for safety
            chunks.append("\n".join(cur))
            cur = [ln]
            cur_len = len(ln) + 1
        else:
            cur.append(ln)
            cur_len += add_len
    if cur:
        chunks.append("\n".join(cur))

    ok_all = True
    for i, ch in enumerate(chunks):
        tries = 0
        sent = False
        while tries < 3 and not sent:
            sent = send_markdown_message(token, chat_id, ch)
            if not sent:
                tries += 1
                time.sleep(1.5 * tries)
        ok_all = ok_all and sent
    return ok_all


def main() -> int:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")

    if not token or not chat:
        print("notify_telegram: TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID not set; skipping send")
        return 0

    if len(sys.argv) == 2 and sys.argv[1].endswith(".json"):
        try:
            items = json.load(open(sys.argv[1]))
        except Exception as e:
            print(f"notify_telegram: failed to load {sys.argv[1]}: {e}")
            items = []
        msg = format_market_summary_detailed(items)
    else:
        msg = " ".join(sys.argv[1:]) or "📊 Market Screening: no content"

    ok = send_with_chunking(token, chat, msg)
    if not ok:
        print("notify_telegram: send failed; continuing")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
