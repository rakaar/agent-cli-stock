#!/usr/bin/env python3
import os, json, sys
from stock_track.notifiers.telegram_notify import send_message, format_market_summary

def main():
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat  = os.environ["TELEGRAM_CHAT_ID"]

    if len(sys.argv) == 2 and sys.argv[1].endswith(".json"):
        items = json.load(open(sys.argv[1]))
        msg = format_market_summary(items)
    else:
        msg = " ".join(sys.argv[1:]) or "Stock Tracker: no content"

    send_message(token, chat, msg)

if __name__ == "__main__":
    main()

