#!/usr/bin/env python3
import json
import os
import sys
from typing import List, Dict

# Allow running this script directly from repo root
if __name__ == "__main__" and __package__ is None:
    # Ensure repo root is on sys.path
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if repo_root not in sys.path:
        sys.path.insert(0, repo_root)

from fetch_et_rss import run_once  # type: ignore


# Stocks/Markets/News category RSS discovered from the page <link rel="alternate"> tag
FEED_URLS = [
    "https://economictimes.indiatimes.com/rssfeeds/2146843.cms",
]

OUT_PATH = "et_stocks_latest.json"
STATE_PATH = "et_stocks_state.json"


def main() -> int:
    # Force fetch to always produce fresh top-20 output even if ETag says 304.
    items: List[Dict] = run_once(
        feed_urls=FEED_URLS,
        state_path=STATE_PATH,
        only_new=False,
        max_items=50,  # fetch enough per feed, we will slice to 20 globally
        force=True,
        sort_by_published_desc=True,
    )

    # Keep newest 20 globally
    items = items[:20]

    tmp = OUT_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(items, f, indent=2, ensure_ascii=False)
    os.replace(tmp, OUT_PATH)

    print(f"Saved {len(items)} items to {OUT_PATH}")
    if items:
        print(f"Latest: {items[0].get('title')} | {items[0].get('published')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

