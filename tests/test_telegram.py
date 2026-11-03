from unittest.mock import patch, MagicMock
import urllib.error
from notifications.telegram import TelegramNotifier


def test_telegram_notifier_disabled_without_credentials():
    notifier = TelegramNotifier()
    assert notifier.enabled is False
    assert notifier.send_message("Hello") is False

    notifier_partial = TelegramNotifier(bot_token="12345", chat_id="")
    assert notifier_partial.enabled is False
    assert notifier_partial.send_message("Hello") is False


def test_telegram_notifier_send_message_success():
    notifier = TelegramNotifier(bot_token="bot_token_123", chat_id="chat_id_456")
    assert notifier.enabled is True

    mock_resp = MagicMock()
    mock_resp.getcode.return_value = 200

    with patch("urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.return_value.__enter__.return_value = mock_resp
        success = notifier.send_message("<b>Trade Opened</b>")
        assert success is True
        mock_urlopen.assert_called_once()
        req = mock_urlopen.call_args[0][0]
        assert "botbot_token_123/sendMessage" in req.full_url


def test_telegram_notifier_handles_network_error_gracefully():
    notifier = TelegramNotifier(bot_token="bot_token_123", chat_id="chat_id_456")

    with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("Connection refused")):
        # Should not raise exception
        success = notifier.send_message("Test Alert")
        assert success is False
