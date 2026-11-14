import pytest
from unittest.mock import patch

@pytest.fixture(autouse=True)
def mock_default_weekday():
    """
    Ensure unit tests run deterministically regardless of whether the test suite
    is executed on a weekday or weekend. Individual holiday tests can still override this.
    """
    with patch("strategy.strangle_desk.is_trading_holiday", return_value=(False, "")):
        yield
