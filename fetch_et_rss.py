#!/usr/bin/env python3
import argparse
import datetime as dt
import email.utils
import json
import os
import sys
import time
from typing import Dict, List, Optional, Tuple

from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
import xml.etree.ElementTree as ET


# Default feeds from user input
DEFAULT_FEEDS = [
    "https://b2b.economictimes.indiatimes.com/rss/topstories",
    "https://b2b.economictimes.indiatimes.com/rss/recentstories",
    "https://b2b.economictimes.indiatimes.com/rss/entrepreneur",
    "https://b2b.economictimes.indiatimes.com/rss/infra",
    "https://b2b.economictimes.indiatimes.com/rss/travel",
    "https://b2b.economictimes.indiatimes.com/rss/hr",
    "https://b2b.economictimes.indiatimes.com/rss/hospitality",
    "https://b2b.economictimes.indiatimes.com/rss/legal",
    "https://b2b.economictimes.indiatimes.com/rss/auto",
    "https://b2b.economictimes.indiatimes.com/rss/retail",
    "https://b2b.economictimes.indiatimes.com/rss/health",
    "https://b2b.economictimes.indiatimes.com/rss/telecom",
    "https://b2b.economictimes.indiatimes.com/rss/energy",
    "https://b2b.economictimes.indiatimes.com/rss/cio",
    "https://b2b.economictimes.indiatimes.com/rss/realty",
    "https://b2b.economictimes.indiatimes.com/rss/government",
    "https://b2b.economictimes.indiatimes.com/rss/brand-equity",
    "https://b2b.economictimes.indiatimes.com/rss/bfsi",
    "https://b2b.economictimes.indiatimes.com/rss/ciso",
    "https://b2b.economictimes.indiatimes.com/rss/cfo",
]


STATE_PATH = "et_rss_state.json"


def ensure_dir(path: str) -> None:
    d = os.path.dirname(path)
    if d and not os.path.exists(d):
        os.makedirs(d, exist_ok=True)


def load_state(path: str) -> Dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {"feeds": {}}
    except json.JSONDecodeError:
        return {"feeds": {}}


def save_state(path: str, state: Dict) -> None:
    ensure_dir(path)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, sort_keys=True)
    os.replace(tmp, path)


def http_get(url: str, etag: Optional[str] = None, last_modified: Optional[str] = None, timeout: int = 20) -> Tuple[int, Dict[str, str], bytes]:
    headers = {
        "User-Agent": "codex-stock/et-rss (https://github.com/openai)"
    }
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified
    req = Request(url, headers=headers)
    try:
        with urlopen(req, timeout=timeout) as resp:
            status = getattr(resp, 'status', 200)
            # Python <3.9 may not expose headers as mapping; normalize
            resp_headers = {k.lower(): v for k, v in resp.headers.items()}
            body = resp.read()
            return status, resp_headers, body
    except HTTPError as e:
        # HTTPError is also a file-like object; handle 304
        status = e.code
        resp_headers = {k.lower(): v for k, v in e.headers.items()} if e.headers else {}
        data = e.read() if hasattr(e, 'read') else b""
        return status, resp_headers, data
    except URLError as e:
        raise RuntimeError(f"Network error fetching {url}: {e}")


def parse_rss(xml_bytes: bytes) -> List[Dict]:
    # Handle encodings if XML declares one; ET can handle bytes directly
    root = ET.fromstring(xml_bytes)

    # Namespace handling for content:encoded
    ns = {
        'content': 'http://purl.org/rss/1.0/modules/content/'
    }

    items: List[Dict] = []
    # Typical RSS structure: <rss><channel><item>...</item></channel></rss>
    channel = None
    if root.tag.lower().endswith('rss'):
        channel = root.find('channel')
    if channel is None:
        # Try Atom style (unlikely for ET feeds) or fallback to findall
        candidates = root.findall('.//item')
    else:
        candidates = channel.findall('item')

    for it in candidates:
        def txt(tag: str) -> Optional[str]:
            el = it.find(tag)
            return el.text.strip() if el is not None and el.text is not None else None

        title = txt('title')
        link = txt('link')
        guid = txt('guid') or link
        pub_date_raw = txt('pubDate')
        description = txt('description')

        # content:encoded if present
        content_el = it.find('content:encoded', ns)
        content_html = content_el.text if content_el is not None else None

        # Normalize pubDate to ISO8601 if possible
        published_iso = None
        if pub_date_raw:
            try:
                dt_obj = email.utils.parsedate_to_datetime(pub_date_raw)
                # Ensure timezone-aware
                if dt_obj.tzinfo is None:
                    dt_obj = dt_obj.replace(tzinfo=dt.timezone.utc)
                published_iso = dt_obj.isoformat()
            except Exception:
                published_iso = None

        items.append({
            'title': title,
            'link': link,
            'guid': guid,
            'published': published_iso or pub_date_raw,
            'description': description,
            'content_html': content_html,
        })

    return items


def filter_new_items(feed_url: str, items: List[Dict], state: Dict, keep_seen: int = 1000) -> Tuple[List[Dict], Dict]:
    feed_state = state.setdefault('feeds', {}).setdefault(feed_url, {})
    seen = feed_state.setdefault('seen', [])
    seen_set = set(seen)
    new_items: List[Dict] = []
    for it in items:
        key = it.get('guid') or it.get('link') or it.get('title')
        if not key:
            continue
        if key in seen_set:
            continue
        new_items.append(it)
        seen.append(key)
    # Trim seen list to prevent unbounded growth
    if len(seen) > keep_seen:
        feed_state['seen'] = seen[-keep_seen:]
    return new_items, state


def _parse_published_dt(value: Optional[str]) -> Optional[dt.datetime]:
    if not value:
        return None
    # Try ISO 8601 first
    try:
        d = dt.datetime.fromisoformat(value)
        if d.tzinfo is None:
            d = d.replace(tzinfo=dt.timezone.utc)
        return d
    except Exception:
        pass
    # Try RFC 2822 via email.utils
    try:
        d = email.utils.parsedate_to_datetime(value)
        if d and d.tzinfo is None:
            d = d.replace(tzinfo=dt.timezone.utc)
        return d
    except Exception:
        return None


def run_once(
    feed_urls: List[str],
    state_path: str,
    only_new: bool = False,
    max_items: Optional[int] = None,
    *,
    force: bool = False,
    sort_by_published_desc: bool = True,
) -> List[Dict]:
    out: List[Dict] = []
    state = load_state(state_path)
    for url in feed_urls:
        feed_state = state.setdefault('feeds', {}).setdefault(url, {})
        etag = None if force else feed_state.get('etag')
        last_mod = None if force else feed_state.get('last_modified')

        status, headers, body = http_get(url, etag=etag, last_modified=last_mod)
        if status == 304:
            # Not modified; nothing to add
            continue
        if status != 200:
            print(f"Warning: GET {url} -> {status}", file=sys.stderr)
            continue

        # Update caching headers
        new_etag = headers.get('etag')
        new_last_mod = headers.get('last-modified')
        if new_etag:
            feed_state['etag'] = new_etag
        if new_last_mod:
            feed_state['last_modified'] = new_last_mod

        # Parse items
        try:
            items = parse_rss(body)
        except Exception as e:
            print(f"Error parsing RSS for {url}: {e}", file=sys.stderr)
            continue

        if only_new:
            items, state = filter_new_items(url, items, state)

        # Sort per-feed items if requested
        if sort_by_published_desc:
            items.sort(
                key=lambda x: (_parse_published_dt(x.get('published')) or dt.datetime.min.replace(tzinfo=dt.timezone.utc)),
                reverse=True,
            )

        # Optionally limit items per feed
        if isinstance(max_items, int):
            items = items[:max_items]

        ts = dt.datetime.now(dt.timezone.utc).isoformat()
        for it in items:
            it_out = dict(it)
            it_out['feed'] = url
            it_out['fetched_at'] = ts
            out.append(it_out)

    # Final global sort (across feeds) to ensure latest-first order
    if sort_by_published_desc:
        out.sort(
            key=lambda x: (_parse_published_dt(x.get('published')) or dt.datetime.min.replace(tzinfo=dt.timezone.utc)),
            reverse=True,
        )

    save_state(state_path, state)
    return out


def print_text(items: List[Dict], include_content: bool = True) -> None:
    for it in items:
        print(f"- {it.get('title')}")
        print(f"  URL: {it.get('link')}")
        if it.get('published'):
            print(f"  Published: {it['published']}")
        if include_content:
            # Prefer content_html, fallback to description
            content = it.get('content_html') or it.get('description')
            if content:
                # crude strip of HTML tags for readability
                text = strip_html(content)
                synopsis = text.strip().splitlines()
                synopsis = [ln for ln in synopsis if ln.strip()]
                if synopsis:
                    snippet = synopsis[0]
                    print(f"  Summary: {snippet[:400]}")
        print()


def strip_html(html: str) -> str:
    # Minimal HTML stripper using ET; wrap in a root element to avoid errors
    try:
        wrapped = f"<root>{html}</root>"
        root = ET.fromstring(wrapped)
        return ''.join(root.itertext())
    except Exception:
        # Fallback: remove tags crudely
        import re
        return re.sub(r"<[^>]+>", " ", html)


def parse_args(argv: List[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Fetch ET B2B RSS feeds with caching")
    p.add_argument("--feeds", help="Comma-separated feed URLs or 'default'", default="default")
    p.add_argument("--state", help="Path to state JSON", default=STATE_PATH)
    p.add_argument("--only-new", help="Emit only items not seen before", action="store_true")
    p.add_argument("--max-items", type=int, default=None, help="Max items per feed")
    p.add_argument("--format", choices=["text", "json", "ndjson"], default="text")
    p.add_argument("--no-content", action="store_true", help="Do not include content/description in text output")
    return p.parse_args(argv)


def main(argv: List[str]) -> int:
    args = parse_args(argv)
    if args.feeds.strip().lower() == 'default':
        feeds = DEFAULT_FEEDS
    else:
        feeds = [u.strip() for u in args.feeds.split(',') if u.strip()]
        if not feeds:
            print("No feed URLs provided", file=sys.stderr)
            return 2

    try:
        items = run_once(
            feed_urls=feeds,
            state_path=args.state,
            only_new=args.only_new,
            max_items=args.max_items,
        )
    except RuntimeError as e:
        print(str(e), file=sys.stderr)
        return 1

    if args.format == 'text':
        print_text(items, include_content=not args.no_content)
    elif args.format == 'json':
        print(json.dumps(items, indent=2, ensure_ascii=False))
    else:  # ndjson
        for it in items:
            print(json.dumps(it, ensure_ascii=False))

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
