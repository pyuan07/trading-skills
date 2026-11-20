"""moomoo_data: symbol mapping, positions -> holdings CSV, insights rendering. Synthetic data only."""
import csv
import json

import pytest

from moomoo_data import to_moomoo, to_yahoo


@pytest.mark.parametrize("code,yahoo", [
    ("US.VOO", "VOO"), ("SG.ES3", "ES3.SI"), ("HK.00700", "0700.HK"), ("HK.09988", "9988.HK"),
    ("SH.600519", "600519.SS"), ("SZ.000001", "000001.SZ"), ("JP.7203", "7203.T"),
    ("MY.1023", "1023.KL"), ("US.TSM260918P360000", "TSM260918P360000"),
])
def test_to_yahoo(code, yahoo):
    assert to_yahoo(code) == yahoo


@pytest.mark.parametrize("ticker,code", [("nvda", "US.NVDA"), ("0700.HK", "HK.00700"),
                                         ("ES3.SI", "SG.ES3"), ("BRK.B", "US.BRK.B")])
def test_to_moomoo(ticker, code):
    assert to_moomoo(ticker) == code


POSITIONS = {"s": "ok", "d": [
    {"position_side": "LONG", "code": "US.AAPL", "stock_name": "Apple", "qty": "10", "currency": "USD",
     "nominal_price": "200.0", "cost_price": "150.0", "cost_price_valid": True, "market_val": "2000.00",
     "pl_ratio": "33.3", "pl_ratio_valid": True},
    {"position_side": "SHORT", "code": "US.XYZ", "stock_name": "Shorted", "qty": "5", "currency": "USD",
     "nominal_price": "40", "cost_price": "50", "cost_price_valid": True, "market_val": "200.00",
     "pl_ratio": "20", "pl_ratio_valid": True},
    {"position_side": "LONG", "code": "SG.ES3", "stock_name": "STI ETF", "qty": "30.00000", "currency": "SGD",
     "nominal_price": "5", "cost_price": "0", "cost_price_valid": False, "market_val": "150",
     "pl_ratio": "0", "pl_ratio_valid": False},
]}


def test_positions_to_holdings_csv_roundtrips_through_stage0(tmp_path, monkeypatch, capsys):
    import moomoo_data
    from stage0 import parse_generic_holdings
    src = tmp_path / "acc1.json"
    src.write_text(json.dumps(POSITIONS), encoding="utf-8")
    out = tmp_path / "holdings-moomoo.csv"
    monkeypatch.setattr("sys.argv", ["moomoo_data.py", "positions", str(src), "--acc-type", "margin",
                                     "--out", str(out)])
    moomoo_data.main()
    rows = {r["code"]: r for r in parse_generic_holdings(out)}
    assert rows["AAPL"]["qty"] == 10 and rows["AAPL"]["acct_type"] == "Margin"
    assert rows["XYZ"]["qty"] == -5 and rows["XYZ"]["mktval"] == -200.0      # short stays negative
    assert rows["ES3.SI"]["ccy"] == "SGD" and rows["ES3.SI"]["cost"] is None  # invalid cost dropped


def test_insights_renders_sections(tmp_path, monkeypatch):
    import moomoo_data
    cons = tmp_path / "c.json"
    cons.write_text(json.dumps({"ret_code": 0, "data": {"total": 30, "strong_buy": 80, "hold": 20, "sell": 0,
                                                         "average": 100, "lowest": 80, "highest": 120,
                                                         "update_time_str": "2026-01-01"}}))
    si = tmp_path / "s.json"
    si.write_text(json.dumps({"data": {"items": [{"timestamp_str": "2026-01-01", "shares_short": 1000000,
                                                  "short_percent": 1.5, "days_to_cover": 2.1}]}}))
    monkeypatch.setattr("sys.argv", ["moomoo_data.py", "insights", "AAPL", "--run-dir", str(tmp_path / "run"),
                                     "--consensus", str(cons), "--short-interest", str(si)])
    moomoo_data.main()
    md = (tmp_path / "run" / "data" / "moomoo.md").read_text(encoding="utf-8")
    assert "US.AAPL" in md and "| 30 | 80.0% |" in md and "1,000,000" in md and "1.5%" in md


def test_live_moomoo_csv_supersedes_futu_export(holdings_dir):
    from stage0 import find_csvs, find_generic_csvs
    (holdings_dir / "holdings-moomoo.csv").write_text("ticker,qty\nAAPL,1\n", encoding="utf-8")
    assert find_csvs(holdings_dir) and any(p.name == "holdings-moomoo.csv" for p, _ in find_generic_csvs(holdings_dir))
