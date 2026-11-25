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


def test_ensure_kite_session_reuses_env_token_from_peer_strategy():
    from kite.client import ensure_kite_session
    old_kite = MagicMock()
    old_kite.access_token = "old_expired_token"
    old_kite.profile.side_effect = Exception("Incorrect `api_key` or `access_token`.")

    cfg_with_new_token = KiteConfig(
        api_key="mock_api_key",
        api_secret="mock_secret",
        user_id="mock_user",
        password="mock_password",
        totp_key="JBSWY3DPEHPK3PXP",
        access_token="new_valid_token_from_other_strategy"
    )

    valid_peer_client = MagicMock()
    valid_peer_client.profile.return_value = {"user_id": "mock_user"}

    with patch("kite.client.load_config", return_value=cfg_with_new_token), \
         patch("kite.client._validate_token", return_value=valid_peer_client), \
         patch("kite.client.get_kite_client") as mock_get_client:

        result_client = ensure_kite_session(kite=old_kite, force_login=False)
        assert old_kite.set_access_token.called
        old_kite.set_access_token.assert_called_with("new_valid_token_from_other_strategy")
        mock_get_client.assert_not_called()


def test_get_kite_client_market_closed_skips_api_validation(mock_config):
    with patch("kite.client.KiteConnect") as MockKite, \
         patch("strategy.holidays.is_market_open", return_value=(False, "Market closed today (Weekend)")), \
         patch("strategy.holidays.is_premarket_window", return_value=False):
        mock_instance = MagicMock()
        MockKite.return_value = mock_instance

        client = get_kite_client(config=mock_config, check_market_hours=True)
        assert client == mock_instance
        mock_instance.set_access_token.assert_called_with("valid_cached_token")
        mock_instance.profile.assert_not_called()


def test_get_kite_client_market_open_validates_profile(mock_config):
    with patch("kite.client.KiteConnect") as MockKite, \
         patch("strategy.holidays.is_market_open", return_value=(True, "Market OPEN")), \
         patch("strategy.holidays.is_premarket_window", return_value=False):
        mock_instance = MagicMock()
        mock_instance.profile.return_value = {"user_id": "mock_user"}
        MockKite.return_value = mock_instance

        client = get_kite_client(config=mock_config, check_market_hours=True)
        assert client == mock_instance
        mock_instance.profile.assert_called_once()


def test_ensure_kite_session_market_closed_skips_profile():
    from kite.client import ensure_kite_session
    mock_kite = MagicMock()
    with patch("strategy.holidays.is_market_open", return_value=(False, "Market closed")), \
         patch("strategy.holidays.is_premarket_window", return_value=False):
        res = ensure_kite_session(kite=mock_kite, force_login=False, check_market_hours=True)
        assert res == mock_kite
        mock_kite.profile.assert_not_called()


def test_ensure_kite_session_adopts_env_token_even_when_current_token_empty():
    from kite.client import ensure_kite_session
    mock_kite = MagicMock()
    mock_kite.access_token = None
    mock_kite.profile.side_effect = Exception("Incorrect `api_key` or `access_token`.")
    cfg_with_new_token = KiteConfig(
        api_key="mock_api_key",
        api_secret="mock_secret",
        user_id="mock_user",
        password="mock_password",
        totp_key="JBSWY3DPEHPK3PXP",
        access_token="peer_fresh_token"
    )
    valid_peer_client = MagicMock()
    with patch("kite.client.load_config", return_value=cfg_with_new_token), \
         patch("kite.client._validate_token", return_value=valid_peer_client), \
         patch("kite.client.get_kite_client") as mock_get_client:
        res = ensure_kite_session(kite=mock_kite, force_login=False)
        assert res is mock_kite
        mock_kite.set_access_token.assert_called_once_with("peer_fresh_token")
        mock_get_client.assert_not_called()


