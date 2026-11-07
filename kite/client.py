import logging
from typing import Optional
from kiteconnect import KiteConnect
from kiteconnect.exceptions import TokenException
from config import load_config, KiteConfig
from kite.auth import login_with_totp

logger = logging.getLogger("kite.client")


def get_kite_client(config: Optional[KiteConfig] = None, force_login: bool = False) -> KiteConnect:
    """
    Initializes and authenticates a KiteConnect client session.
    1. Checks if a cached access_token exists and validates it using kite.profile().
    2. If force_login=True or token is missing/expired, executes automated TOTP login.
    """
    cfg = config or load_config()
    kite = KiteConnect(api_key=cfg.api_key)

    token = cfg.access_token

    if not force_login and token:
        kite.set_access_token(token)
        try:
            logger.info("Validating cached Kite access token...")
            profile = kite.profile()
            logger.info(f"Connected to Kite as {profile.get('user_name', cfg.user_id)} ({profile.get('user_id')})")
            return kite
        except TokenException:
            logger.warning("Cached access token has expired or is invalid. Initiating fresh TOTP login...")
        except Exception as e:
            logger.warning("Validation failed (%s). Proceeding to re-authenticate...", e)

    logger.info("Logging in with TOTP 2FA...")
    new_token = login_with_totp(cfg)
    if not new_token:
        raise RuntimeError("Failed to authenticate with Zerodha Kite using TOTP. Check credentials in .env.")

    kite.set_access_token(new_token)
    try:
        profile = kite.profile()
        logger.info(f"Successfully authenticated as {profile.get('user_name', cfg.user_id)} ({profile.get('user_id')})")
    except Exception as e:
        logger.warning("Could not fetch profile after fresh TOTP authentication (%s), but access token is set.", e)
    return kite


def ensure_kite_session(kite: Optional[KiteConnect] = None, force_login: bool = False) -> KiteConnect:
    """
    Validates active KiteConnect session by verifying connectivity with kite.profile().
    If expired, invalid, or force_login=True, initiates headless TOTP login
    and returns an authenticated KiteConnect instance.
    """
    if kite is not None and not force_login:
        try:
            kite.profile()
            return kite
        except Exception as e:
            err_str = str(e).lower()
            if "token" in err_str or "api_key" in err_str or "access_token" in err_str or "tokenexception" in type(e).__name__.lower():
                logger.warning("Kite session token is expired or invalidated (%s). Refreshing...", e)
            else:
                logger.warning("Kite profile check encountered unexpected error (%s). Refreshing session...", e)

    logger.info("Acquiring fresh Kite session via automated TOTP login...")
    return get_kite_client(force_login=True)
