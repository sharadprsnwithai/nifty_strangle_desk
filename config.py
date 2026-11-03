import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from dotenv import load_dotenv

@dataclass
class KiteConfig:
    api_key: str
    api_secret: str
    user_id: str
    password: str
    totp_key: str
    access_token: Optional[str] = None
    telegram_bot_token: Optional[str] = None
    telegram_chat_id: Optional[str] = None

def load_config(env_path: Optional[Path] = None) -> KiteConfig:
    target_env = env_path or (Path(__file__).resolve().parent / ".env")
    if target_env.exists():
        load_dotenv(dotenv_path=target_env, override=True)
    
    api_key = os.getenv("KITE_API_KEY", "").strip()
    api_secret = os.getenv("KITE_API_SECRET", "").strip()
    user_id = os.getenv("KITE_USER_ID", "").strip()
    password = os.getenv("KITE_PASSWORD", "").strip()
    totp_key = os.getenv("KITE_TOTP_KEY", "").strip().replace(" ", "")
    access_token = os.getenv("KITE_ACCESS_TOKEN", "").strip() or None
    telegram_bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip() or None
    telegram_chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip() or None

    missing = []
    if not api_key: missing.append("KITE_API_KEY")
    if not api_secret: missing.append("KITE_API_SECRET")
    if not user_id: missing.append("KITE_USER_ID")
    if not password: missing.append("KITE_PASSWORD")
    if not totp_key: missing.append("KITE_TOTP_KEY")

    if missing:
        raise ValueError(
            f"Missing required Kite credentials in {target_env.name}: {', '.join(missing)}. "
            "Please copy .env.example to .env and populate your credentials."
        )

    return KiteConfig(
        api_key=api_key,
        api_secret=api_secret,
        user_id=user_id,
        password=password,
        totp_key=totp_key,
        access_token=access_token,
        telegram_bot_token=telegram_bot_token,
        telegram_chat_id=telegram_chat_id
    )
