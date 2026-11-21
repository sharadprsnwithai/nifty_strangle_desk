import os
import sys
from datetime import datetime
from pathlib import Path

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import load_config
from notifications.telegram import TelegramNotifier

def test_telegram_connection():
    print("=" * 65)
    print("       TELEGRAM MESSAGING & CONNECTIVITY DIAGNOSTICS")
    print("=" * 65)

    config = load_config()
    token = config.telegram_bot_token or os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = config.telegram_chat_id or os.getenv("TELEGRAM_CHAT_ID", "").strip()

    masked_token = f"{token[:6]}...{token[-4:]}" if len(token) > 10 else ("Configured" if token else "MISSING")
    masked_chat = f"{chat_id[:3]}...{chat_id[-3:]}" if len(chat_id) > 6 else ("Configured" if chat_id else "MISSING")

    print(f"Bot Token : {masked_token}")
    print(f"Chat ID   : {masked_chat}")
    print("-" * 65)

    if not token or not chat_id:
        print("❌ ERROR: TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID is missing in .env!")
        print("Please configure both in .env to enable trade alerts & error notifications.")
        return False

    notifier = TelegramNotifier(bot_token=token, chat_id=chat_id)
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S IST")

    test_msg = (
        "🔔 <b>TRADING DESK CONNECTIVITY TEST</b>\n"
        f"• Environment: <code>Windows / Docker</code>\n"
        f"• Strangle Desk: <b>10 Lots NIFTY Options</b>\n"
        f"• Stock Futures: <b>2 Lots Momentum 30</b>\n"
        f"• Status: <b>✅ ALL SYSTEMS OPERATIONAL</b>\n"
        f"• Timestamp: <code>{now_str}</code>\n\n"
        "<i>If you received this message, Telegram trade alerts and error logging are working properly!</i>"
    )

    print("Dispatching test alert to Telegram...")
    success = notifier.send_message(test_msg)

    if success:
        print("✅ SUCCESS: Telegram notification delivered successfully!")
        print("Check your Telegram chat/channel to verify message appearance.")
        return True
    else:
        print("❌ FAILED: Could not deliver Telegram alert. Check bot token, chat ID, and network.")
        return False

if __name__ == "__main__":
    ok = test_telegram_connection()
    sys.exit(0 if ok else 1)
