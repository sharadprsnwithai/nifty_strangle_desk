import os
import time
import logging
from pathlib import Path
from contextlib import contextmanager
from typing import Optional
from kiteconnect import KiteConnect
from kiteconnect.exceptions import TokenException
from config import load_config, KiteConfig
from kite.auth import login_with_totp

logger = logging.getLogger("kite.client")

LOCK_FILE = Path(__file__).resolve().parent.parent / "data" / ".kite_auth.lock"


@contextmanager
def interprocess_auth_lock(lock_path: Path = LOCK_FILE, timeout_seconds: float = 30.0, poll_seconds: float = 0.5):
    """
    Acquires an atomic inter-process file lock across Windows and Linux.
    Ensures that concurrent strategy processes (e.g. Strangle Desk, ORB Desk, Silver ORB)
    do not fight over TOTP login or overwrite tokens.
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    start_time = time.time()
    fd = None

    while True:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_RDWR)
            os.write(fd, f"pid={os.getpid()},time={time.time()}\n".encode("utf-8"))
            break
        except (FileExistsError, OSError):
            # Check for stale lock (older than 60 seconds)
            try:
                mtime = lock_path.stat().st_mtime
                if time.time() - mtime > 60.0:
                    try:
                        lock_path.unlink(missing_ok=True)
                        logger.warning("Removed stale authentication lock file (%s).", lock_path)
                    except OSError:
                        pass
            except OSError:
                pass

            if time.time() - start_time >= timeout_seconds:
                logger.warning("Auth lock wait timed out after %.1fs; proceeding anyway.", timeout_seconds)
                break
            time.sleep(poll_seconds)

    try:
        yield
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
            try:
                lock_path.unlink(missing_ok=True)
            except OSError:
                pass


def _validate_token(api_key: str, token: str) -> Optional[KiteConnect]:
    """Tests if a token is active and valid with Zerodha Kite without mutating caller state."""
    if not token:
        return None
    try:
        test_kite = KiteConnect(api_key=api_key)
        test_kite.set_access_token(token)
        profile = test_kite.profile()
        logger.info("Validated Kite access token for user %s (%s).", profile.get("user_name"), profile.get("user_id"))
        return test_kite
    except Exception:
        return None


def get_kite_client(
    config: Optional[KiteConfig] = None,
    force_login: bool = False,
    validate: Optional[bool] = None,
    check_market_hours: bool = False,
    ignore_holidays: bool = False,
) -> KiteConnect:
    """
    Initializes and authenticates a KiteConnect client session.
    1. If check_market_hours=True and market is closed, loads cached access_token
       locally without making network API calls to kite.profile().
    2. If market is active (or check_market_hours=False), validates token using kite.profile().
    3. If force_login=True or token is missing/expired, executes automated TOTP login.
       Uses an inter-process lock and double-checked .env validation to prevent concurrent
       strategy processes from conflicting or invalidating each other's tokens.
    """
    cfg = config or load_config()
    kite = KiteConnect(api_key=cfg.api_key)

    token = cfg.access_token

    # Check if market is currently open or in pre-market window if check_market_hours is enabled
    market_reason = "Market hours check disabled"
    if check_market_hours:
        from strategy.holidays import is_market_open, is_premarket_window
        market_open, market_reason = is_market_open(ignore_holidays=ignore_holidays)
        is_pre = is_premarket_window(ignore_holidays=ignore_holidays)
        should_validate = validate if validate is not None else (market_open or is_pre or ignore_holidays)
    else:
        should_validate = validate if validate is not None else True

    if not force_login and token:
        kite.set_access_token(token)
        if not should_validate:
            logger.info("[KITE] Market is closed (%s). Loaded cached access token locally without API call.", market_reason)
            return kite

        try:
            logger.info("Validating cached Kite access token...")
            profile = kite.profile()
            logger.info(f"Connected to Kite as {profile.get('user_name', cfg.user_id)} ({profile.get('user_id')})")
            return kite
        except TokenException:
            logger.warning("Cached access token has expired or is invalid. Initiating fresh TOTP login...")
        except Exception as e:
            logger.warning("Validation failed (%s). Proceeding to re-authenticate...", e)

    # If market is closed and no valid token, do not hit Kite auth when market is closed
    if not should_validate and not force_login:
        logger.info("[KITE] Market is closed (%s). Deferring TOTP login until market open / pre-market session.", market_reason)
        return kite

    # Protect TOTP login with inter-process lock so multiple desks don't login concurrently
    with interprocess_auth_lock():
        # Double-checked locking pattern for multi-process environments:
        # If another strategy process refreshed .env while we waited for the lock, reuse that token!
        if config is None:
            reloaded_cfg = load_config()
            reloaded_token = reloaded_cfg.access_token
            if reloaded_token and reloaded_token != token:
                logger.info("Detected fresh access token written to .env by a peer strategy process. Validating...")
                test_client = _validate_token(reloaded_cfg.api_key, reloaded_token)
                if test_client:
                    logger.info("Shared access token from .env validated successfully. Reusing session without redundant login.")
                    return test_client
            cfg = reloaded_cfg

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


def is_token_error(e: Exception) -> bool:
    """
    Detects whether an exception specifically indicates an expired, revoked,
    or invalid Zerodha Kite Connect session access token (as opposed to an
    instrument_token error, rate limit, or transient network issue).
    """
    if isinstance(e, TokenException) or "tokenexception" in type(e).__name__.lower():
        return True

    err_str = str(e).lower()
    # Explicitly exclude instrument_token / contract lookup errors
    if "instrument_token" in err_str or "instrument token" in err_str:
        return False
    if "too many requests" in err_str or "429" in err_str:
        return False

    return (
        "incorrect `api_key`" in err_str
        or "incorrect api_key" in err_str
        or ("access_token" in err_str and ("invalid" in err_str or "expired" in err_str or "missing" in err_str or "incorrect" in err_str))
        or "token is invalid" in err_str
        or "token expired" in err_str
        or "session expired" in err_str
        or "invalid token" in err_str
        or "user not logged in" in err_str
        or ("403" in err_str and "forbidden" in err_str)
    )


def ensure_kite_session(
    kite: Optional[KiteConnect] = None,
    force_login: bool = False,
    check_market_hours: bool = False,
    ignore_holidays: bool = False,
) -> KiteConnect:
    """
    Validates active KiteConnect session by verifying connectivity with kite.profile().
    If expired, invalid, or force_login=True:
    1. First checks if .env already has a fresh, valid token generated by another strategy.
       If so, adopts that token without performing an unnecessary login.
    2. If .env token is also expired/missing, acquires an inter-process lock and performs
       automated TOTP login, safely updating .env and returning the shared authenticated session.
    """
    if check_market_hours and not force_login:
        from strategy.holidays import is_market_open, is_premarket_window
        market_open, market_reason = is_market_open(ignore_holidays=ignore_holidays)
        is_pre = is_premarket_window(ignore_holidays=ignore_holidays)
        if not (market_open or is_pre or ignore_holidays):
            logger.info("[KITE] Market is closed (%s). Skipping session profile check.", market_reason)
            return kite

    if kite is not None and not force_login:
        try:
            kite.profile()
            return kite
        except Exception as e:
            err_str = str(e).lower()
            if "too many requests" in err_str or "429" in err_str:
                logger.warning("Kite API rate limit encountered during profile check (%s). Preserving existing token.", e)
                return kite
            if "read timed out" in err_str or "connection" in err_str or "timeout" in err_str or "502" in err_str or "503" in err_str or "504" in err_str:
                logger.warning("Transient network/server error during Kite profile check (%s). Preserving existing session.", e)
                return kite
            if not is_token_error(e):
                logger.warning("Kite profile check encountered unexpected non-auth error (%s). Preserving existing session.", e)
                return kite
            logger.warning("Kite session token is expired or invalidated (%s). Refreshing...", e)

    # In multi-strategy execution, check if .env has already been updated with a valid token
    current_token = getattr(kite, "access_token", None) if kite else None
    if kite is None or current_token is None or isinstance(current_token, str):
        cfg = load_config()
        env_token = cfg.access_token
        if env_token and isinstance(env_token, str) and env_token != current_token:
            logger.info("Found updated access token in .env from a peer strategy. Testing validity before logging in...")
            test_client = _validate_token(cfg.api_key, env_token)
            if test_client:
                logger.info("Token from .env is valid (shared across strategies). Reusing session without new login.")
                if kite is not None and hasattr(kite, "set_access_token"):
                    try:
                        kite.set_access_token(env_token)
                    except Exception:
                        pass
                    return kite
                return test_client

    logger.info("Acquiring fresh Kite session via automated TOTP login...")
    new_client = get_kite_client(force_login=True)
    if kite is not None and hasattr(new_client, "access_token") and hasattr(kite, "set_access_token"):
        try:
            kite.set_access_token(new_client.access_token)
        except Exception:
            pass
    return new_client
