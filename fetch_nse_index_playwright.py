from urllib.parse import quote
from playwright.sync_api import sync_playwright


DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


def _extract_fields(data: dict, index: str) -> dict:
    pi = (data or {}).get("priceInfo", {})
    return {
        "index": (data or {}).get("info", {}).get("index" ) or index,
        "last": pi.get("last" ) or pi.get("lastPrice"),
        "change": pi.get("change"),
        "pChange": pi.get("pChange"),
        "open": pi.get("open"),
        "dayHigh": pi.get("intraDayHighLow", {}).get("max") or pi.get("dayHigh"),
        "dayLow": pi.get("intraDayHighLow", {}).get("min") or pi.get("dayLow"),
        "previousClose": pi.get("previousClose"),
    }


def fetch_index(index_name: str, engine: str = "firefox", headed: bool = False) -> dict:
    """Fetch NSE index quote (e.g., 'NIFTY 50', 'NIFTY BANK')."""
    with sync_playwright() as p:
        # Direct API call first
        api = p.request.new_context(
            extra_http_headers={
                "User-Agent": DEFAULT_UA,
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "en-US,en;q=0.9",
                "Referer": "https://www.nseindia.com/",
                "Origin": "https://www.nseindia.com",
            }
        )
        url = f"https://www.nseindia.com/api/quote-index?index={quote(index_name)}"
        r = api.get(url, timeout=30000)
        if r.ok:
            return _extract_fields(r.json(), index_name)

        # Fallback: page capture
        browser_type = getattr(p, engine)
        launch_args = {}
        if engine == "chromium":
            launch_args["args"] = ["--disable-http2", "--disable-features=NetworkService,NetworkServiceInProcess"]
        browser = browser_type.launch(headless=not headed, **launch_args)
        context = browser.new_context(user_agent=DEFAULT_UA, timezone_id="Asia/Kolkata", locale="en-IN")
        page = context.new_page()
        target = None
        def is_index_api(resp):
            return resp.url.startswith("https://www.nseindia.com/api/quote-index?index=") and resp.status == 200
        try:
            with page.expect_response(is_index_api, timeout=45000) as info:
                page.goto(f"https://www.nseindia.com/market-data/live-equity-market?symbol={quote(index_name)}", wait_until="domcontentloaded")
            target = info.value.json()
        finally:
            context.close(); browser.close()
        if not target:
            raise RuntimeError(f"Failed to fetch index {index_name}")
        return _extract_fields(target, index_name)


if __name__ == "__main__":
    import sys, json
    name = "NIFTY 50"
    engine = "firefox"
    headed = False
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    flags = [a for a in sys.argv[1:] if a.startswith("--")]
    if args:
        name = " ".join(args)
    for f in flags:
        if f.startswith("--engine="):
            engine = f.split("=",1)[1]
        elif f == "--headed":
            headed = True
    try:
        data = fetch_index(name, engine=engine, headed=headed)
        print(json.dumps(data, indent=2))
    except Exception as e:
        print(f"Error: {e}")

