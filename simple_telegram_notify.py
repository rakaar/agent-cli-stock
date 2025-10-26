#!/usr/bin/env python3
import os
import json
import sys
import requests
import textwrap

def send_telegram_message(token, chat_id, message):
    """Send a message to Telegram using the Bot API"""
    url = f"https://api.telegram.org/bot{token}/sendMessage"

    payload = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "MarkdownV2",
        "disable_web_page_preview": True
    }

    try:
        response = requests.post(url, json=payload, timeout=10)
        response.raise_for_status()
        return True
    except Exception as e:
        print(f"Error sending Telegram message: {e}")
        return False

def escape_markdown(text):
    """Escape special characters for MarkdownV2"""
    special_chars = ['_', '*', '[', ']', '(', ')', '~', '`', '>', '#', '+', '-', '=', '|', '{', '}', '.', '!']
    for char in special_chars:
        text = text.replace(char, f'\\{char}')
    return text

def format_market_items(items):
    """Format market items for Telegram message"""
    if not items:
        return "📊 *Market Analysis*\n\nNo market items available."

    lines = ["📊 *Market Analysis Summary*"]
    lines.append("")

    for item in items:
        title = escape_markdown(item.get('title', 'Unknown')[:60])
        symbol = escape_markdown(item.get('symbol', 'N/A'))
        view = item.get('view', 'WATCH')
        rationale = escape_markdown(item.get('rationale', 'No rationale provided'))

        emoji = {"BUY": "🟢", "WATCH": "🟡", "AVOID": "🔴"}.get(view, "⚪")

        lines.append(f"{emoji} *{symbol}* — {view}")
        lines.append(f"📝 {rationale}")
        lines.append("")

    lines.append("_🤖 Generated with Claude Code_")
    return "\n".join(lines)

def main():
    # Check environment variables
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")

    if not token or not chat_id:
        print("Error: TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID environment variables must be set")
        sys.exit(1)

    # Load market items from JSON file or use default message
    if len(sys.argv) == 2 and sys.argv[1].endswith(".json"):
        try:
            with open(sys.argv[1], 'r') as f:
                items = json.load(f)
            message = format_market_items(items)
        except Exception as e:
            print(f"Error loading JSON file: {e}")
            message = "📊 *Market Analysis*\n\nError loading market data."
    else:
        message = " ".join(sys.argv[1:]) or "📊 Market Analysis: No content provided"

    # Split message if too long (Telegram limit is 4096 characters)
    if len(message) > 4000:
        parts = textwrap.wrap(message, width=4000, break_long_words=False, replace_whitespace=False)
        success = True
        for i, part in enumerate(parts):
            if i > 0:
                # Add continuation indicator for subsequent parts
                part = f"_...continued_ {part}" if not part.startswith('_...continued_') else part
            if not send_telegram_message(token, chat_id, part):
                success = False
                break
        return success
    else:
        return send_telegram_message(token, chat_id, message)

if __name__ == "__main__":
    success = main()
    if not success:
        print("Failed to send Telegram notification")
        sys.exit(1)
    print("Telegram notification sent successfully")