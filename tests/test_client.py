from unittest.mock import MagicMock, patch
import pytest
from kiteconnect.exceptions import TokenException
from config import KiteConfig
from kite.client import get_kite_client

@pytest.fixture
def mock_config():
    return KiteConfig(
        api_key="mock_api_key",
        api_secret="mock_secret",
        user_id="mock_user",
        password="mock_password",
        totp_key="JBSWY3DPEHPK3PXP",
        access_token="valid_cached_token"
    )

def test_get_kite_client_cached_token_valid(mock_config):
    with patch("kite.client.KiteConnect") as MockKite:
        mock_instance = MagicMock()
        mock_instance.profile.return_value = {"user_id": "mock_user"}
        MockKite.return_value = mock_instance
        
        client = get_kite_client(config=mock_config, force_login=False)
        assert client == mock_instance
        mock_instance.set_access_token.assert_called_with("valid_cached_token")
        mock_instance.profile.assert_called_once()

def test_get_kite_client_cached_token_expired_triggers_login(mock_config):
    with patch("kite.client.KiteConnect") as MockKite, \
         patch("kite.client.login_with_totp") as mock_login:
        
        mock_instance = MagicMock()
        # First call to profile raises TokenException, second call after login succeeds
        mock_instance.profile.side_effect = [TokenException("Token is invalid"), {"user_id": "mock_user"}]
        MockKite.return_value = mock_instance
        mock_login.return_value = "newly_generated_token"
        
        client = get_kite_client(config=mock_config, force_login=False)
        assert client == mock_instance
        mock_login.assert_called_once()
        mock_instance.set_access_token.assert_called_with("newly_generated_token")

def test_get_kite_client_force_login(mock_config):
    with patch("kite.client.KiteConnect") as MockKite, \
         patch("kite.client.login_with_totp") as mock_login:
        
        mock_instance = MagicMock()
        mock_instance.profile.return_value = {"user_id": "mock_user"}
        MockKite.return_value = mock_instance
        mock_login.return_value = "forced_token_123"
        
        client = get_kite_client(config=mock_config, force_login=True)
        assert client == mock_instance
        mock_login.assert_called_once()
        mock_instance.set_access_token.assert_called_with("forced_token_123")

def test_get_kite_client_no_cached_token_triggers_login():
    config_no_token = KiteConfig(
        api_key="mock_api_key",
        api_secret="mock_secret",
        user_id="mock_user",
        password="mock_password",
        totp_key="JBSWY3DPEHPK3PXP",
        access_token=None
    )
    with patch("kite.client.KiteConnect") as MockKite, \
         patch("kite.client.login_with_totp") as mock_login:
        mock_instance = MagicMock()
        mock_instance.profile.return_value = {"user_id": "mock_user"}
        MockKite.return_value = mock_instance
        mock_login.return_value = "fresh_token_456"
        
        client = get_kite_client(config=config_no_token)
        assert client == mock_instance
        mock_login.assert_called_once_with(config_no_token)
        mock_instance.set_access_token.assert_called_once_with("fresh_token_456")

def test_get_kite_client_login_failure_raises_runtime_error(mock_config):
    with patch("kite.client.KiteConnect") as MockKite, \
         patch("kite.client.login_with_totp") as mock_login:
        mock_instance = MagicMock()
        mock_instance.profile.side_effect = TokenException("Token expired")
        MockKite.return_value = mock_instance
        mock_login.return_value = None
        
        with pytest.raises(RuntimeError, match="Failed to authenticate with Zerodha Kite using TOTP"):
            get_kite_client(config=mock_config, force_login=False)

def test_get_kite_client_default_config_loads_from_load_config(mock_config):
    with patch("kite.client.load_config", return_value=mock_config) as mock_load_config, \
         patch("kite.client.KiteConnect") as MockKite:
        mock_instance = MagicMock()
        mock_instance.profile.return_value = {"user_id": "mock_user"}
        MockKite.return_value = mock_instance
        
        client = get_kite_client()
        mock_load_config.assert_called_once()
        assert client == mock_instance
