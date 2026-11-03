import os
from pathlib import Path
import pytest
from config import load_config, KiteConfig

def test_load_config_missing_env(monkeypatch):
    monkeypatch.delenv("KITE_API_KEY", raising=False)
    monkeypatch.delenv("KITE_API_SECRET", raising=False)
    monkeypatch.delenv("KITE_USER_ID", raising=False)
    monkeypatch.delenv("KITE_PASSWORD", raising=False)
    monkeypatch.delenv("KITE_TOTP_KEY", raising=False)
    
    with pytest.raises(ValueError) as exc:
        load_config(env_path=Path("non_existent.env"))
    assert "Missing required Kite credentials" in str(exc.value)

def test_load_config_success(monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "KITE_API_KEY=test_key\n"
        "KITE_API_SECRET=test_secret\n"
        "KITE_USER_ID=test_user\n"
        "KITE_PASSWORD=test_pass\n"
        "KITE_TOTP_KEY=JBSWY3DPEHPK3PXP\n"
        "KITE_ACCESS_TOKEN=existing_token\n"
    )
    cfg = load_config(env_path=env_file)
    assert cfg.api_key == "test_key"
    assert cfg.api_secret == "test_secret"
    assert cfg.user_id == "test_user"
    assert cfg.password == "test_pass"
    assert cfg.totp_key == "JBSWY3DPEHPK3PXP"
    assert cfg.access_token == "existing_token"

def test_load_config_optional_access_token(monkeypatch, tmp_path):
    monkeypatch.delenv("KITE_ACCESS_TOKEN", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "KITE_API_KEY=test_key\n"
        "KITE_API_SECRET=test_secret\n"
        "KITE_USER_ID=test_user\n"
        "KITE_PASSWORD=test_pass\n"
        "KITE_TOTP_KEY=JBSWY3DPEHPK3PXP\n"
    )
    cfg = load_config(env_path=env_file)
    assert cfg.access_token is None
    assert cfg.telegram_bot_token is None
    assert cfg.telegram_chat_id is None


def test_load_config_with_telegram_credentials(monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "KITE_API_KEY=test_key\n"
        "KITE_API_SECRET=test_secret\n"
        "KITE_USER_ID=test_user\n"
        "KITE_PASSWORD=test_pass\n"
        "KITE_TOTP_KEY=JBSWY3DPEHPK3PXP\n"
        "TELEGRAM_BOT_TOKEN=my_bot_token_123\n"
        "TELEGRAM_CHAT_ID=my_chat_id_456\n"
    )
    cfg = load_config(env_path=env_file)
    assert cfg.telegram_bot_token == "my_bot_token_123"
    assert cfg.telegram_chat_id == "my_chat_id_456"
