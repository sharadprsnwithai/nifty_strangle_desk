import json
import pytest
from pathlib import Path
from strategy.stock_futures.rehydrator import (
    resolve_symbol,
    UniverseRehydrator,
)
from strategy.stock_futures.config import StockFuturesConfig
from strategy.stock_futures.order_manager import StockFuturesOrderManager
from strategy.stock_futures.desk import StockFuturesDesk


def test_resolve_symbol():
    # Exact company names
    assert resolve_symbol("Laurus Labs Limited") == "LAURUSLABS"
    assert resolve_symbol("Multi Commodity Exchange of India Limited") == "MCX"
    assert resolve_symbol("The Federal Bank  Limited") == "FEDERALBNK"  # double space handled
    assert resolve_symbol("Vodafone Idea Limited") == "IDEA"
    assert resolve_symbol("GE Vernova T&D India Limited") == "GVT_D"

    # Direct tickers
    assert resolve_symbol("ABB") == "ABB"
    assert resolve_symbol("BSE") == "BSE"

    # Non-equity cash / collateral filtering
    assert resolve_symbol("Triparty Repo") is None
    assert resolve_symbol("TREPS Cash") is None


def test_universe_rehydrator_parsing(tmp_path):
    universe_file = tmp_path / "test_universe.json"
    rehydrator = UniverseRehydrator(universe_file=universe_file)

    sample_text = """
    Laurus Labs Limited
    Multi Commodity Exchange of India Limited
    ABB India Limited
    Triparty Repo
    """

    res = rehydrator.rehydrate(sample_text, save=True)
    assert res["constituents_count"] == 3
    assert set(res["symbols"]) == {"LAURUSLABS", "MCX", "ABB"}
    assert universe_file.exists()


def test_universe_rehydrator_detects_changes(tmp_path):
    universe_file = tmp_path / "test_universe.json"
    rehydrator = UniverseRehydrator(universe_file=universe_file)

    # Initial list: LAURUSLABS, MCX
    res1 = rehydrator.rehydrate("LAURUSLABS\nMCX", save=True)
    assert res1["constituents_count"] == 2

    # Updated list: LAURUSLABS, ABB (MCX removed, ABB added)
    res2 = rehydrator.rehydrate("LAURUSLABS\nABB", save=True)
    assert res2["changes"]["added"] == ["ABB"]
    assert res2["changes"]["removed"] == ["MCX"]
    assert res2["changes"]["is_changed"] is True


def test_desk_dynamic_hot_reloading(tmp_path):
    universe_file = tmp_path / "dynamic_universe.json"
    state_file = tmp_path / "trades_state.json"

    # Start desk with 2 stocks
    init_data = {
        "updated_at": "2026-11-21T00:00:00",
        "symbols": ["ABB", "MCX"],
        "lot_sizes": {"ABB": 125, "MCX": 250}
    }
    with open(universe_file, "w", encoding="utf-8") as f:
        json.dump(init_data, f)

    cfg = StockFuturesConfig(universe_file=universe_file, state_file=state_file)
    assert set(cfg.symbols) == {"ABB", "MCX"}

    om = StockFuturesOrderManager(mode="PAPER", config=cfg, state_file=state_file)
    desk = StockFuturesDesk(order_manager=om, config=cfg)

    # Now update universe file (simulating rehydration with new stock LAURUSLABS)
    new_data = {
        "updated_at": "2026-11-21T01:00:00",
        "symbols": ["ABB", "MCX", "LAURUSLABS"],
        "lot_sizes": {"ABB": 125, "MCX": 250, "LAURUSLABS": 1700}
    }
    import time
    time.sleep(0.05)  # ensure mtime difference
    with open(universe_file, "w", encoding="utf-8") as f:
        json.dump(new_data, f)

    # Desk check_and_rehydrate_universe should detect and reload
    rehydrated = desk.check_and_rehydrate_universe()
    assert rehydrated is True
    assert "LAURUSLABS" in desk.config.symbols
    assert desk.config.lot_sizes["LAURUSLABS"] == 1700
    assert desk.order_manager.config.lot_sizes["LAURUSLABS"] == 1700


def test_auto_rehydrate_drop_file(tmp_path, monkeypatch):
    universe_file = tmp_path / "universe.json"
    drop_file = tmp_path / "drop_constituents.txt"
    drop_file.write_text("Laurus Labs Limited\nMulti Commodity Exchange of India Limited\nTriparty Repo")

    rehydrator = UniverseRehydrator(universe_file=universe_file)
    # Monkeypatch drop_files list in auto_rehydrate
    result = rehydrator.rehydrate(source=drop_file, save=True)
    assert result["constituents_count"] == 2
    assert set(result["symbols"]) == {"LAURUSLABS", "MCX"}


def test_desk_perform_daily_rehydration(tmp_path):
    universe_file = tmp_path / "daily_universe.json"
    state_file = tmp_path / "daily_state.json"

    cfg = StockFuturesConfig(universe_file=universe_file, state_file=state_file)
    om = StockFuturesOrderManager(mode="PAPER", config=cfg, state_file=state_file)
    desk = StockFuturesDesk(order_manager=om, config=cfg)

    # Calling perform_daily_rehydration
    success = desk.perform_daily_rehydration(ignore_holidays=True)
    assert universe_file.exists()
