import json
import logging
import urllib.request
import urllib.error
from typing import Optional

logger = logging.getLogger("notifications.telegram")


class TelegramNotifier:
    """
    Lightweight Telegram bot alert notifier.
    Dispatches formatted trade alerts, risk limit triggers, and daily summaries.
    Safe and fail-soft: network timeouts or missing credentials will never interrupt trading.
    """

    def __init__(self, bot_token: Optional[str] = None, chat_id: Optional[str] = None):
        self.bot_token = (bot_token or "").strip()
        self.chat_id = (chat_id or "").strip()
        self.enabled = bool(self.bot_token and self.chat_id)

    def send_message(self, message: str) -> bool:
        """
        Sends an HTML-formatted message to the configured Telegram chat.
        Returns True on successful delivery, False otherwise.
        """
        if not self.enabled:
            logger.debug("Telegram notifications disabled (missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID).")
            return False

        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        payload = {
            "chat_id": self.chat_id,
            "text": message,
            "parse_mode": "HTML",
            "disable_web_page_preview": True
        }

        try:
            req_data = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(
                url,
                data=req_data,
                headers={"Content-Type": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=3.5) as response:
                status = response.getcode()
                if 200 <= status < 300:
                    logger.debug("Telegram alert delivered successfully.")
                    return True
                else:
                    logger.warning("Telegram API returned non-200 status code: %d", status)
                    return False
        except urllib.error.HTTPError as e:
            # If HTML parsing fails (HTTP 400 Bad Request, e.g. unclosed tags or unescaped '<'),
            # retry sending without HTML parse_mode so critical trade/risk alerts are never dropped.
            if e.code == 400 and payload.get("parse_mode") == "HTML":
                try:
                    plain_payload = {
                        "chat_id": self.chat_id,
                        "text": message,
                        "disable_web_page_preview": True
                    }
                    fallback_data = json.dumps(plain_payload).encode("utf-8")
                    fallback_req = urllib.request.Request(
                        url,
                        data=fallback_data,
                        headers={"Content-Type": "application/json"}
                    )
                    with urllib.request.urlopen(fallback_req, timeout=3.5) as fb_response:
                        if 200 <= fb_response.getcode() < 300:
                            logger.info("Telegram alert delivered via plain text fallback after HTML parse error.")
                            return True
                except Exception as fb_err:
                    logger.warning("Failed to send Telegram alert on plain text fallback: %s", fb_err)
            logger.warning("Failed to send Telegram alert: %s", e)
            return False
        except (urllib.error.URLError, TimeoutError) as e:
            logger.warning("Failed to send Telegram alert: %s", e)
            return False
        except Exception as e:
            logger.warning("Unexpected error sending Telegram alert: %s", e)
            return False
