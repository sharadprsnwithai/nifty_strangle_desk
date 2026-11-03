# Kite API TOTP Connection & Positions Desk Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a modular, automated connection to Zerodha's Kite Connect API using headless TOTP 2FA authentication, cache the daily access token, and fetch/display current trading positions in both terminal tabular and structured JSON formats.

**Architecture:** A standalone modular desk where configuration (`config.py`) validates secrets from `.env`, `kite/auth.py` performs headless 2FA login using `pyotp` and extracts OAuth tokens, `kite/client.py` manages the `KiteConnect` session and token caching, `kite/positions.py` extracts position metrics and renders tabular/JSON formats, and `main.py` provides the user-facing CLI.

**Tech Stack:** Python 3.12, `uv`, `kiteconnect`, `pyotp`, `requests`, `python-dotenv`, `tabulate`, `pytest`.

**Spec:** `docs/superpowers/specs/2026-11-03-kite-api-totp-positions-design.md`

## Global Constraints
- Python: `>=3.10`
- Package management: `uv` with `pyproject.toml` and fallback `requirements.txt`
- Code formatting and structure: Zerodha logic strictly under `kite/` package
- Security: `.env` must be gitignored; credentials never logged or committed
- Formatting: Output supports formatted table by default and `--json` raw format

---

### Task 1: Project Scaffolding & Configuration Manager

**Files:**
- Create: `pyproject.toml`
- Create: `requirements.txt`
- Create: `.gitignore`
- Create: `.env.example`
- Create: `config.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Produces: `config.KiteConfig` class with `api_key`, `api_secret`, `user_id`, `password`, `totp_key`, `access_token` and `load_config() -> KiteConfig`.

- [ ] **Step 1: Create project configuration and dependency files**

Create `pyproject.toml`:
```toml
[project]
name = "sharads-intraday-option-selling-desk"
version = "0.1.0"
description = "Intraday option selling desk with Kite Connect API and TOTP authentication"
readme = "README.md"
requires-python = ">=3.10"
dependencies = [
    "kiteconnect>=5.0.0",
    "pyotp>=2.9.0",
    "requests>=2.31.0",
    "python-dotenv>=1.0.0",
    "tabulate>=0.9.0",
]

[project.optional-dependencies]
dev = [
    "pytest>=8.0.0",
]
```

Create `requirements.txt`:
```txt
kiteconnect>=5.0.0
pyotp>=2.9.0
requests>=2.31.0
python-dotenv>=1.0.0
tabulate>=0.9.0
pytest>=8.0.0
```

Create `.gitignore`:
```gitignore
.env
.venv/
__pycache__/
*.pyc
.pytest_cache/
*.log
docs/superpowers/
```

Create `.env.example`:
```env
# Zerodha Kite Connect API Credentials
KITE_API_KEY=your_api_key_here
KITE_API_SECRET=your_api_secret_here

# Automated Headless TOTP Login Credentials
KITE_USER_ID=your_zerodha_user_id
KITE_PASSWORD=your_zerodha_password
KITE_TOTP_KEY=your_32_character_base32_totp_key

# Cached Daily Access Token (auto-updated by login flow)
KITE_ACCESS_TOKEN=
```

- [ ] **Step 2: Initialize virtual environment and install dependencies via `uv`**

Run:
```bash
uv venv
uv pip install -r requirements.txt
```

- [ ] **Step 3: Write the failing test for configuration loading**

Create `tests/test_config.py`:
```python
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
```

- [ ] **Step 4: Run test to verify it fails**

Run: `uv run pytest tests/test_config.py -v`  
Expected: FAIL (`ModuleNotFoundError: No module named 'config'`)

- [ ] **Step 5: Write implementation in `config.py`**

Create `config.py`:
```python
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
        access_token=access_token
    )
```

- [ ] **Step 6: Run test to verify it passes**

Run: `uv run pytest tests/test_config.py -v`  
Expected: PASS (2 passed)

- [ ] **Step 7: Commit**

```bash
git add pyproject.toml requirements.txt .gitignore .env.example config.py tests/test_config.py
git commit -m "feat: scaffold project and add configuration loader"
```

---

### Task 2: Headless TOTP Authentication Engine

**Files:**
- Create: `kite/auth.py`
- Test: `tests/test_auth.py`

**Interfaces:**
- Consumes: `config.KiteConfig`
- Produces: `generate_totp_code(totp_key: str) -> str`, `save_access_token_to_env(access_token: str, env_path: Optional[Path]) -> bool`, `login_with_totp(config: KiteConfig, env_path: Optional[Path]) -> Optional[str]`

- [ ] **Step 1: Write the failing tests for auth functions**

Create `tests/test_auth.py`:
```python
from pathlib import Path
import pyotp
from kite.auth import generate_totp_code, save_access_token_to_env

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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_auth.py -v`  
Expected: FAIL (`ModuleNotFoundError: No module named 'kite'`)

- [ ] **Step 3: Implement `kite/auth.py`**

Create `kite/auth.py`:
```python
import os
import re
import logging
from pathlib import Path
from typing import Optional
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

def save_access_token_to_env(access_token: str, env_path: Optional[Path] = None) -> bool:
    """Updates or appends KITE_ACCESS_TOKEN in the target .env file and active environment."""
    os.environ["KITE_ACCESS_TOKEN"] = access_token
    target_path = env_path or DEFAULT_ENV_PATH

    if not target_path.exists():
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

def login_with_totp(config: KiteConfig, env_path: Optional[Path] = None) -> Optional[str]:
    """
    Performs fully automated headless login with Zerodha using TOTP:
    1. Submits User ID + Password to Kite Login API.
    2. Generates 6-digit TOTP code and submits to 2FA endpoint.
    3. Traverses OAuth hops to capture request_token.
    4. Exchanges request_token with KiteConnect for fresh access_token.
    5. Saves new access_token to .env.
    """
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "X-Kite-Version": "3"
    })

    try:
        # Step 1: Login with User ID and Password
        logger.info("Submitting login credentials to Zerodha Kite...")
        login_resp = session.post(
            "https://kite.zerodha.com/api/login",
            data={"user_id": config.user_id, "password": config.password},
            timeout=15
        )
        login_json = login_resp.json()
        if login_json.get("status") != "success":
            logger.error(f"Login Step 1 failed: {login_json.get('message', 'Unknown login error')}")
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
        twofa_json = twofa_resp.json()
        if twofa_json.get("status") != "success":
            logger.error(f"TOTP 2FA failed: {twofa_json.get('message', 'Invalid TOTP')}")
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
            logger.error("Could not extract request_token from OAuth redirect hops.")
            return None

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
        logger.error(f"Automated TOTP login encountered error: {e}", exc_info=True)
        return None

    return None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_auth.py -v`  
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add kite/auth.py tests/test_auth.py
git commit -m "feat: implement headless TOTP authentication engine"
```

---

### Task 3: Kite Client Session Manager

**Files:**
- Create: `kite/__init__.py`
- Create: `kite/client.py`
- Test: `tests/test_client.py`

**Interfaces:**
- Consumes: `config.KiteConfig`, `kite.auth.login_with_totp`
- Produces: `get_kite_client(config: Optional[KiteConfig], force_login: bool) -> KiteConnect`

- [ ] **Step 1: Write the failing test for client initialization and token reuse**

Create `tests/test_client.py`:
```python
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
        # First call to profile raises TokenException
        mock_instance.profile.side_effect = [TokenException("Token is invalid"), {"user_id": "mock_user"}]
        MockKite.return_value = mock_instance
        mock_login.return_value = "newly_generated_token"
        
        client = get_kite_client(config=mock_config, force_login=False)
        assert client == mock_instance
        mock_login.assert_called_once()
        mock_instance.set_access_token.assert_called_with("newly_generated_token")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_client.py -v`  
Expected: FAIL (`ModuleNotFoundError: No module named 'kite.client'`)

- [ ] **Step 3: Implement `kite/__init__.py` and `kite/client.py`**

Create `kite/__init__.py`:
```python
from .auth import login_with_totp, generate_totp_code
from .client import get_kite_client

__all__ = ["login_with_totp", "generate_totp_code", "get_kite_client"]
```

Create `kite/client.py`:
```python
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
            logger.warning(f"Validation failed ({e}). Proceeding to re-authenticate...")

    logger.info("Logging in with TOTP 2FA...")
    new_token = login_with_totp(cfg)
    if not new_token:
        raise RuntimeError("Failed to authenticate with Zerodha Kite using TOTP. Check credentials in .env.")

    kite.set_access_token(new_token)
    profile = kite.profile()
    logger.info(f"Successfully authenticated as {profile.get('user_name', cfg.user_id)} ({profile.get('user_id')})")
    return kite
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_client.py -v`  
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add kite/__init__.py kite/client.py tests/test_client.py
git commit -m "feat: add Kite client session manager with automatic token validation"
```

---

### Task 4: Positions Processing & Display Engine

**Files:**
- Create: `kite/positions.py`
- Test: `tests/test_positions.py`

**Interfaces:**
- Consumes: `kiteconnect.KiteConnect`
- Produces: `fetch_positions(kite: KiteConnect) -> dict`, `summarize_positions(raw_positions: dict) -> dict`, `format_positions_table(summary: dict, view: str, open_only: bool) -> str`

- [ ] **Step 1: Write the failing test for position parsing, aggregation, and formatting**

Create `tests/test_positions.py`:
```python
from unittest.mock import MagicMock
import pytest
from kite.positions import fetch_positions, summarize_positions, format_positions_table

@pytest.fixture
def sample_positions_payload():
    return {
        "net": [
            {
                "tradingsymbol": "NIFTY26NOV24500CE",
                "exchange": "NFO",
                "instrument_token": 123456,
                "product": "NRML",
                "quantity": -50,
                "buy_quantity": 0,
                "buy_price": 0.0,
                "sell_quantity": 50,
                "sell_price": 120.0,
                "last_price": 95.0,
                "pnl": 1250.0,
                "m2m": 1250.0,
                "realised": 0.0,
                "unrealised": 1250.0,
            },
            {
                "tradingsymbol": "BANKNIFTY26NOV52000PE",
                "exchange": "NFO",
                "instrument_token": 654321,
                "product": "MIS",
                "quantity": 0,
                "buy_quantity": 30,
                "buy_price": 200.0,
                "sell_quantity": 30,
                "sell_price": 220.0,
                "last_price": 215.0,
                "pnl": 600.0,
                "m2m": 0.0,
                "realised": 600.0,
                "unrealised": 0.0,
            }
        ],
        "day": [
            {
                "tradingsymbol": "BANKNIFTY26NOV52000PE",
                "exchange": "NFO",
                "instrument_token": 654321,
                "product": "MIS",
                "quantity": 0,
                "buy_quantity": 30,
                "buy_price": 200.0,
                "sell_quantity": 30,
                "sell_price": 220.0,
                "last_price": 215.0,
                "pnl": 600.0,
                "m2m": 0.0,
                "realised": 600.0,
                "unrealised": 0.0,
            }
        ]
    }

def test_summarize_positions(sample_positions_payload):
    summary = summarize_positions(sample_positions_payload)
    
    assert summary["totals"]["total_realised"] == 600.0
    assert summary["totals"]["total_unrealised"] == 1250.0
    assert summary["totals"]["total_pnl"] == 1850.0
    assert summary["totals"]["open_positions_count"] == 1
    assert summary["totals"]["closed_positions_count"] == 1
    assert len(summary["net_positions"]) == 2
    assert len(summary["day_positions"]) == 1

def test_format_positions_table(sample_positions_payload):
    summary = summarize_positions(sample_positions_payload)
    table_text = format_positions_table(summary, view="net", open_only=False)
    
    assert "NIFTY26NOV24500CE" in table_text
    assert "BANKNIFTY26NOV52000PE" in table_text
    assert "Total P&L" in table_text
    assert "+1850.00" in table_text

def test_format_positions_table_open_only(sample_positions_payload):
    summary = summarize_positions(sample_positions_payload)
    table_text = format_positions_table(summary, view="net", open_only=True)
    
    assert "NIFTY26NOV24500CE" in table_text
    # Closed position should be filtered out
    assert "BANKNIFTY26NOV52000PE" not in table_text

def test_empty_positions():
    empty_payload = {"net": [], "day": []}
    summary = summarize_positions(empty_payload)
    assert summary["totals"]["total_pnl"] == 0.0
    table_text = format_positions_table(summary, view="net", open_only=False)
    assert "No positions found" in table_text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_positions.py -v`  
Expected: FAIL (`ModuleNotFoundError: No module named 'kite.positions'`)

- [ ] **Step 3: Implement `kite/positions.py`**

Create `kite/positions.py`:
```python
from typing import Dict, List, Any
from tabulate import tabulate
from kiteconnect import KiteConnect

def fetch_positions(kite: KiteConnect) -> Dict[str, List[Dict[str, Any]]]:
    """Retrieves raw positions payload from Kite Connect."""
    return kite.positions()

def summarize_positions(raw_positions: Dict[str, List[Dict[str, Any]]]) -> Dict[str, Any]:
    """
    Parses raw positions and calculates portfolio metrics:
    total realised, total unrealised, total P&L, open count, closed count.
    """
    net_list = raw_positions.get("net", [])
    day_list = raw_positions.get("day", [])

    total_realised = 0.0
    total_unrealised = 0.0
    total_m2m = 0.0
    open_count = 0
    closed_count = 0

    parsed_net = []
    for pos in net_list:
        qty = pos.get("quantity", 0)
        pnl = float(pos.get("pnl", 0.0))
        m2m = float(pos.get("m2m", 0.0))
        realised = float(pos.get("realised", 0.0))
        unrealised = float(pos.get("unrealised", 0.0))

        total_realised += realised
        total_unrealised += unrealised
        total_m2m += m2m

        if qty != 0:
            open_count += 1
        else:
            closed_count += 1

        parsed_net.append({
            "symbol": pos.get("tradingsymbol", ""),
            "exchange": pos.get("exchange", ""),
            "product": pos.get("product", ""),
            "quantity": qty,
            "buy_price": float(pos.get("buy_price", 0.0)),
            "sell_price": float(pos.get("sell_price", 0.0)),
            "last_price": float(pos.get("last_price", 0.0)),
            "pnl": pnl,
            "m2m": m2m,
            "realised": realised,
            "unrealised": unrealised,
        })

    parsed_day = []
    for pos in day_list:
        parsed_day.append({
            "symbol": pos.get("tradingsymbol", ""),
            "exchange": pos.get("exchange", ""),
            "product": pos.get("product", ""),
            "quantity": pos.get("quantity", 0),
            "buy_price": float(pos.get("buy_price", 0.0)),
            "sell_price": float(pos.get("sell_price", 0.0)),
            "last_price": float(pos.get("last_price", 0.0)),
            "pnl": float(pos.get("pnl", 0.0)),
            "m2m": float(pos.get("m2m", 0.0)),
        })

    total_pnl = total_realised + total_unrealised

    return {
        "net_positions": parsed_net,
        "day_positions": parsed_day,
        "totals": {
            "total_realised": round(total_realised, 2),
            "total_unrealised": round(total_unrealised, 2),
            "total_m2m": round(total_m2m, 2),
            "total_pnl": round(total_pnl, 2),
            "open_positions_count": open_count,
            "closed_positions_count": closed_count,
        }
    }

def format_positions_table(summary: Dict[str, Any], view: str = "net", open_only: bool = False) -> str:
    """Formats position data and summary metrics as a clean terminal table."""
    positions = summary["net_positions"] if view == "net" else summary["day_positions"]

    if open_only:
        positions = [p for p in positions if p["quantity"] != 0]

    view_title = "NET POSITIONS (Cumulative)" if view == "net" else "DAY POSITIONS (Intraday)"
    lines = [f"\n=== {view_title} ==="]

    if not positions:
        lines.append("No positions found.")
    else:
        table_rows = []
        for p in positions:
            qty_str = f"{p['quantity']:+d}" if p['quantity'] != 0 else "0 (CLOSED)"
            pnl_val = p["pnl"]
            pnl_str = f"{pnl_val:+.2f}"
            
            table_rows.append([
                p["symbol"],
                p["exchange"],
                p["product"],
                qty_str,
                f"{p['buy_price']:.2f}",
                f"{p['sell_price']:.2f}",
                f"{p['last_price']:.2f}",
                pnl_str
            ])

        headers = ["Symbol", "Exch", "Product", "Net Qty", "Buy Avg", "Sell Avg", "LTP", "P&L (₹)"]
        lines.append(tabulate(table_rows, headers=headers, tablefmt="grid"))

    # Summary section
    totals = summary["totals"]
    summary_rows = [
        ["Total Realised P&L", f"{totals['total_realised']:+.2f}"],
        ["Total Unrealised P&L", f"{totals['total_unrealised']:+.2f}"],
        ["Total MTM (M2M)", f"{totals['total_m2m']:+.2f}"],
        ["Total Net P&L", f"{totals['total_pnl']:+.2f}"],
        ["Open / Closed Count", f"{totals['open_positions_count']} open / {totals['closed_positions_count']} closed"]
    ]
    lines.append("\n=== PORTFOLIO SUMMARY ===")
    lines.append(tabulate(summary_rows, tablefmt="simple"))

    return "\n".join(lines)
```

- [ ] **Step 4: Update `kite/__init__.py` to export position functions**

Update `kite/__init__.py`:
```python
from .auth import login_with_totp, generate_totp_code
from .client import get_kite_client
from .positions import fetch_positions, summarize_positions, format_positions_table

__all__ = [
    "login_with_totp",
    "generate_totp_code",
    "get_kite_client",
    "fetch_positions",
    "summarize_positions",
    "format_positions_table"
]
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/test_positions.py -v`  
Expected: PASS (4 passed)

- [ ] **Step 6: Commit**

```bash
git add kite/positions.py kite/__init__.py tests/test_positions.py
git commit -m "feat: add positions processing and terminal formatting engine"
```

---

### Task 5: User-Facing CLI & Integration

**Files:**
- Create: `main.py`
- Create: `README.md`
- Test: `tests/test_cli.py`

**Interfaces:**
- Produces: CLI script accepting `--json`, `--force-login`, `--open-only`, `--day`, `--net`.

- [ ] **Step 1: Write CLI test to verify argument parsing and execution paths**

Create `tests/test_cli.py`:
```python
import json
from unittest.mock import patch, MagicMock
import pytest
from main import run_cli

@pytest.fixture
def mock_kite_summary():
    return {
        "net_positions": [{"symbol": "NIFTY26NOV24500CE", "quantity": 50, "pnl": 500.0, "exchange": "NFO", "product": "NRML", "buy_price": 100.0, "sell_price": 0.0, "last_price": 110.0}],
        "day_positions": [],
        "totals": {"total_pnl": 500.0, "total_realised": 0.0, "total_unrealised": 500.0, "total_m2m": 500.0, "open_positions_count": 1, "closed_positions_count": 0}
    }

def test_cli_json_output(capsys, mock_kite_summary):
    with patch("main.get_kite_client") as mock_client, \
         patch("main.fetch_positions") as mock_fetch, \
         patch("main.summarize_positions", return_value=mock_kite_summary):
        
        mock_fetch.return_value = {"net": [], "day": []}
        
        exit_code = run_cli(["--json"])
        assert exit_code == 0
        
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["totals"]["total_pnl"] == 500.0

def test_cli_table_output(capsys, mock_kite_summary):
    with patch("main.get_kite_client") as mock_client, \
         patch("main.fetch_positions") as mock_fetch, \
         patch("main.summarize_positions", return_value=mock_kite_summary):
        
        mock_fetch.return_value = {"net": [], "day": []}
        
        exit_code = run_cli([])
        assert exit_code == 0
        
        captured = capsys.readouterr()
        assert "NIFTY26NOV24500CE" in captured.out
        assert "PORTFOLIO SUMMARY" in captured.out
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_cli.py -v`  
Expected: FAIL (`ModuleNotFoundError: No module named 'main'`)

- [ ] **Step 3: Implement `main.py`**

Create `main.py`:
```python
import sys
import json
import argparse
import logging
from typing import List, Optional
from config import load_config
from kite import get_kite_client, fetch_positions, summarize_positions, format_positions_table

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S"
)
logger = logging.getLogger("desk.main")

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Sharad's Intraday Option Selling Desk — Kite Connect TOTP & Positions Viewer"
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output raw structured JSON to stdout instead of formatted table"
    )
    parser.add_argument(
        "--force-login",
        action="store_true",
        help="Force fresh headless TOTP login, discarding any cached access token"
    )
    parser.add_argument(
        "--open-only",
        action="store_true",
        help="Display only currently open/active positions (filters out qty == 0)"
    )
    view_group = parser.add_mutually_exclusive_group()
    view_group.add_argument(
        "--day",
        action="store_true",
        help="Display only today's intraday positions"
    )
    view_group.add_argument(
        "--net",
        action="store_true",
        help="Display cumulative net positions (default)"
    )
    return parser

def run_cli(args: Optional[List[str]] = None) -> int:
    parser = build_parser()
    parsed_args = parser.parse_args(args)

    if parsed_args.json:
        # Silence standard info logs in JSON mode so stdout remains clean JSON
        logging.getLogger().setLevel(logging.ERROR)

    try:
        config = load_config()
    except Exception as e:
        logger.error(f"Configuration error: {e}")
        return 1

    try:
        kite = get_kite_client(config=config, force_login=parsed_args.force_login)
        raw_positions = fetch_positions(kite)
        summary = summarize_positions(raw_positions)

        if parsed_args.json:
            print(json.dumps(summary, indent=2))
        else:
            view = "day" if parsed_args.day else "net"
            table_str = format_positions_table(summary, view=view, open_only=parsed_args.open_only)
            print(table_str)

        return 0
    except Exception as e:
        logger.error(f"Error fetching Kite positions: {e}", exc_info=True)
        return 1

if __name__ == "__main__":
    sys.exit(run_cli())
```

- [ ] **Step 4: Create `README.md` documentation**

Create `README.md`:
```markdown
# Sharad's Intraday Option Selling Desk

A modular algorithmic desk layer connected to Zerodha Kite Connect API using automated headless TOTP 2FA.

## Features
- **Headless TOTP 2FA**: Automated daily login via `pyotp` RFC 6238 without manual browser interaction.
- **Smart Token Caching**: Caches and validates daily `access_token` in `.env` to avoid unnecessary logins.
- **Comprehensive Positions Viewer**: Displays both Net and Day positions with quantity, entry averages, LTP, MTM, and P&L metrics.
- **Dual Output**: Formatted terminal tables or raw structured `--json` output.

## Setup & Installation

1. Make sure Python `>= 3.10` and `uv` are available.
2. Clone or open this repository:
   ```bash
   cd d:\code\sharads_intraday_option_selling_desk
   ```
3. Create virtual environment and install dependencies:
   ```bash
   uv venv
   uv pip install -r requirements.txt
   ```
4. Copy `.env.example` to `.env` and fill in your Zerodha credentials:
   ```bash
   cp .env.example .env
   ```

## Usage

### View Current Positions (Tabular)
```bash
uv run python main.py
```

### View Only Open Positions
```bash
uv run python main.py --open-only
```

### Output Clean JSON (for automations / pipelines)
```bash
uv run python main.py --json
```

### Force Fresh Login (refresh access token)
```bash
uv run python main.py --force-login
```

## Running Tests
```bash
uv run pytest -v
```
```

- [ ] **Step 5: Run all test suites to verify full pass**

Run: `uv run pytest -v`  
Expected: PASS (all tests pass across `test_config`, `test_auth`, `test_client`, `test_positions`, `test_cli`)

- [ ] **Step 6: Commit**

```bash
git add main.py README.md tests/test_cli.py
git commit -m "feat: add CLI entrypoint and complete project documentation"
```
