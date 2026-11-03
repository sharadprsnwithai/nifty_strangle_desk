from pathlib import Path
from unittest.mock import MagicMock, patch
import pyotp
import pytest
from kite.auth import generate_totp_code, save_access_token_to_env, login_with_totp
from config import KiteConfig

def test_generate_totp_code():
    # RFC 6238 Base32 test secret
    secret = "JBSWY3DPEHPK3PXP"
    code = generate_totp_code(secret)
    assert isinstance(code, str)
    assert len(code) == 6
    assert code.isdigit()

def test_save_access_token_to_env(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("KITE_API_KEY=dummy\nKITE_ACCESS_TOKEN=old_token\n")
    
    success = save_access_token_to_env("new_access_token_123", env_path=env_file)
    assert success is True
    
    updated_text = env_file.read_text()
    assert "KITE_ACCESS_TOKEN=new_access_token_123" in updated_text
    assert "old_token" not in updated_text
    assert "KITE_API_KEY=dummy" in updated_text

def test_save_access_token_to_env_creates_file_if_missing(tmp_path):
    env_file = tmp_path / "new_env.env"
    assert not env_file.exists()
    
    success = save_access_token_to_env("token_abc", env_path=env_file)
    assert success is True
    assert env_file.exists()
    assert "KITE_ACCESS_TOKEN=token_abc" in env_file.read_text()

def test_login_with_totp_mocked_success(tmp_path):
    env_file = tmp_path / ".env"
    config = KiteConfig(
        api_key="mock_api_key",
        api_secret="mock_secret",
        user_id="mock_user",
        password="mock_pass",
        totp_key="JBSWY3DPEHPK3PXP"
    )

    with patch("requests.Session") as MockSession, \
         patch("kite.auth.KiteConnect") as MockKite:
        
        session = MagicMock()
        MockSession.return_value = session

        # Mock login response
        login_resp = MagicMock()
        login_resp.json.return_value = {"status": "success", "data": {"request_id": "req_123"}}
        
        # Mock 2FA response
        twofa_resp = MagicMock()
        twofa_resp.json.return_value = {"status": "success"}

        # Mock OAuth redirect hop
        hop1_resp = MagicMock()
        hop1_resp.headers = {"Location": "https://127.0.0.1/?action=login&status=success&request_token=mock_req_token_xyz"}

        session.post.side_effect = [login_resp, twofa_resp]
        session.get.return_value = hop1_resp

        # Mock KiteConnect session generation
        kc_instance = MagicMock()
        kc_instance.generate_session.return_value = {"access_token": "generated_access_token_999"}
        MockKite.return_value = kc_instance

        token = login_with_totp(config, env_path=env_file)
        assert token == "generated_access_token_999"
        assert "KITE_ACCESS_TOKEN=generated_access_token_999" in env_file.read_text()
