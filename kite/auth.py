import os
import re
import time
import logging
from pathlib import Path
from typing import Optional, Union
import pyotp
import requests
from kiteconnect import KiteConnect
from config import KiteConfig

logger = logging.getLogger("kite.auth")

DEFAULT_ENV_PATH = Path(__file__).resolve().parent.parent / ".env"

def generate_totp_code(totp_key: str) -> str:
    """Generates a 6-digit TOTP token using RFC 6238."""
    clean_key = totp_key.replace(" ", "").strip()
    return pyotp.TOTP(clean_key).now()

def save_access_token_to_env(access_token: str, env_path: Optional[Union[str, Path]] = None) -> bool:
    """Updates or appends KITE_ACCESS_TOKEN in the target .env file and active environment."""
    os.environ["KITE_ACCESS_TOKEN"] = access_token
    target_path = Path(env_path) if env_path else DEFAULT_ENV_PATH

    if not target_path.exists():
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_text(f"KITE_ACCESS_TOKEN={access_token}\n", encoding="utf-8")
        return True

    lines = target_path.read_text(encoding="utf-8").splitlines()
    updated = False
    new_lines = []

    for line in lines:
        if line.strip().startswith("KITE_ACCESS_TOKEN="):
            new_lines.append(f"KITE_ACCESS_TOKEN={access_token}")
            updated = True
        else:
            new_lines.append(line)

    if not updated:
        new_lines.append(f"KITE_ACCESS_TOKEN={access_token}")

    target_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
    return True

def login_with_totp(
    config: KiteConfig,
    env_path: Optional[Union[str, Path]] = None,
    max_retries: int = 3,
    retry_delay: float = 2.0
) -> Optional[str]:
    """
    Performs fully automated headless login with Zerodha using TOTP with built-in retries:
    1. Submits User ID + Password to Kite Login API.
    2. Generates 6-digit TOTP code and submits to 2FA endpoint.
    3. Traverses OAuth hops to capture request_token.
    4. Exchanges request_token with KiteConnect for fresh access_token.
    5. Saves new access_token to .env.
    """
    # Check if another container recently updated .env with a valid token
    target_env = Path(env_path) if env_path else DEFAULT_ENV_PATH
    if target_env.exists():
        try:
            mtime = target_env.stat().st_mtime
            if time.time() - mtime < 30.0:
                for line in target_env.read_text(encoding="utf-8").splitlines():
                    if line.strip().startswith("KITE_ACCESS_TOKEN="):
                        cached_token = line.strip().split("=", 1)[1].strip()
                        if cached_token:
                            test_kite = KiteConnect(api_key=config.api_key)
                            test_kite.set_access_token(cached_token)
                            try:
                                test_kite.profile()
                                logger.info("Detected fresh access token from concurrent container in .env. Reusing token.")
                                os.environ["KITE_ACCESS_TOKEN"] = cached_token
                                return cached_token
                            except Exception:
                                pass
        except Exception:
            pass

    # Check if another container recently updated .env with a valid token
    target_env = Path(env_path) if env_path else DEFAULT_ENV_PATH
    if target_env.exists():
        try:
            mtime = target_env.stat().st_mtime
            if time.time() - mtime < 30.0:
                for line in target_env.read_text(encoding="utf-8").splitlines():
                    if line.strip().startswith("KITE_ACCESS_TOKEN="):
                        cached_token = line.strip().split("=", 1)[1].strip()
                        if cached_token:
                            test_kite = KiteConnect(api_key=config.api_key)
                            test_kite.set_access_token(cached_token)
                            try:
                                test_kite.profile()
                                logger.info("Detected fresh access token from concurrent container in .env. Reusing token.")
                                os.environ["KITE_ACCESS_TOKEN"] = cached_token
                                return cached_token
                            except Exception:
                                pass
        except Exception:
            pass

    # Check if another container recently updated .env with a valid token
    target_env = Path(env_path) if env_path else DEFAULT_ENV_PATH
    if target_env.exists():
        try:
            mtime = target_env.stat().st_mtime
            if time.time() - mtime < 30.0:
                for line in target_env.read_text(encoding="utf-8").splitlines():
                    if line.strip().startswith("KITE_ACCESS_TOKEN="):
                        cached_token = line.strip().split("=", 1)[1].strip()
                        if cached_token:
                            test_kite = KiteConnect(api_key=config.api_key)
                            test_kite.set_access_token(cached_token)
                            try:
                                test_kite.profile()
                                logger.info("Detected fresh access token from concurrent container in .env. Reusing token.")
                                os.environ["KITE_ACCESS_TOKEN"] = cached_token
                                return cached_token
                            except Exception:
                                pass
        except Exception:
            pass

    for attempt in range(1, max_retries + 1):
        try:
            with requests.Session() as session:
                session.headers.update({
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                    "X-Kite-Version": "3"
                })

                # Step 1: Login with User ID and Password
                logger.info("Submitting login credentials to Zerodha Kite (Attempt %d/%d)...", attempt, max_retries)
                login_resp = session.post(
                    "https://kite.zerodha.com/api/login",
                    data={"user_id": config.user_id, "password": config.password},
                    timeout=15
                )

                if not login_resp.text:
                    logger.warning("Empty response from Kite login endpoint (Attempt %d/%d). Retrying...", attempt, max_retries)
                    if attempt < max_retries:
                        time.sleep(retry_delay)
                    continue

                try:
                    login_json = login_resp.json()
                except Exception as je:
                    logger.warning("Kite login endpoint returned non-JSON response (%s). Retrying (Attempt %d/%d)...", je, attempt, max_retries)
                    if attempt < max_retries:
                        time.sleep(retry_delay)
                    continue

                if login_json.get("status") != "success":
                    logger.error("Login Step 1 failed: %s", login_json.get("message", "Unknown login error"))
                    return None

                request_id = login_json["data"]["request_id"]

                # Step 2: Generate TOTP code and submit TwoFA
                logger.info("Generating live TOTP code via pyotp...")
                totp_code = generate_totp_code(config.totp_key)

                twofa_resp = session.post(
                    "https://kite.zerodha.com/api/twofa",
                    data={
                        "user_id": config.user_id,
                        "request_id": request_id,
                        "twofa_value": totp_code,
                        "twofa_type": "totp"
                    },
                    timeout=15
                )

                if not twofa_resp.text:
                    logger.warning("Empty response from Kite 2FA endpoint (Attempt %d/%d). Retrying...", attempt, max_retries)
                    if attempt < max_retries:
                        time.sleep(retry_delay)
                    continue

                try:
                    twofa_json = twofa_resp.json()
                except Exception as je:
                    logger.warning("Kite 2FA endpoint returned non-JSON response (%s). Retrying (Attempt %d/%d)...", je, attempt, max_retries)
                    if attempt < max_retries:
                        time.sleep(retry_delay)
                    continue

                if twofa_json.get("status") != "success":
                    logger.error("TOTP 2FA failed: %s", twofa_json.get("message", "Invalid TOTP"))
                    return None

                logger.info("2FA TOTP verification successful!")

                # Step 3: OAuth redirect to extract request_token
                oauth_url = f"https://kite.zerodha.com/connect/login?api_key={config.api_key}&v=3"
                r_hop1 = session.get(oauth_url, allow_redirects=False, timeout=15)
                loc1 = r_hop1.headers.get("Location") or r_hop1.headers.get("location", "")

                request_token = None
                if "request_token=" in loc1:
                    match = re.search(r"request_token=([a-zA-Z0-9]+)", loc1)
                    if match:
                        request_token = match.group(1)
                elif loc1:
                    if loc1.startswith("/"):
                        loc1 = "https://kite.zerodha.com" + loc1
                    r_hop2 = session.get(loc1, allow_redirects=False, timeout=15)
                    loc2 = r_hop2.headers.get("Location") or r_hop2.headers.get("location", "")
                    match = re.search(r"request_token=([a-zA-Z0-9]+)", loc2)
                    if match:
                        request_token = match.group(1)

                if not request_token:
                    try:
                        r_full = session.get(oauth_url, allow_redirects=True, timeout=15)
                        match = re.search(r"request_token=([a-zA-Z0-9]+)", r_full.url)
                        if match:
                            request_token = match.group(1)
                    except requests.exceptions.ConnectionError as ce:
                        match = re.search(r"request_token=([a-zA-Z0-9]+)", str(ce))
                        if match:
                            request_token = match.group(1)

                if not request_token:
                    logger.warning("Could not extract request_token from OAuth redirect hops (Attempt %d/%d).", attempt, max_retries)
                    if attempt < max_retries:
                        time.sleep(retry_delay)
                    continue

                logger.info("Extracted OAuth request_token successfully.")

                # Step 4: Exchange request_token for access_token
                kc = KiteConnect(api_key=config.api_key)
                session_data = kc.generate_session(request_token, api_secret=config.api_secret)
                access_token = session_data.get("access_token")

                if access_token:
                    save_access_token_to_env(access_token, env_path=env_path)
                    logger.info("Fresh Kite access token obtained and saved to .env!")
                    return access_token

        except Exception as e:
            logger.warning("Automated TOTP login attempt %d/%d encountered error: %s", attempt, max_retries, e)
            if attempt < max_retries:
                time.sleep(retry_delay)

    logger.error("Automated TOTP login failed after %d attempts.", max_retries)
    return None
