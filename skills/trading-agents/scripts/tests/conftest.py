"""Shared fixtures. No test here touches the live market: network-boundary functions
(quote / hist_close_usd / the fetch_data writers) go through a single `yf` module
attribute, which we monkeypatch with a fake that returns canned pandas objects.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

# Scripts use flat imports (`import ta_common`); put the scripts dir on the path.
SCRIPTS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS_DIR))


# ---------- fake yfinance ----------

class _FakeTicker:
    def __init__(self, symbol: str, registry: dict):
        self.symbol = symbol
        self._r = registry.get(symbol, {})

    @property
    def fast_info(self):
        # a real ticker always has a last price; default it to the canned period-end close,
        # falling back to start_close for registries that only supply close_at (per-maturity).
        return {"lastPrice": self._r.get("last", self._r.get("end_close", self._r.get("start_close"))),
                "currency": self._r.get("currency", "USD")}

    def history(self, start=None, end=None, period=None, auto_adjust=True):
        s = pd.Timestamp(start or "2020-01-01")
        c0 = self._r.get("start_close", self._r.get("end_close"))
        c1 = self._r.get("end_close", c0)
        if end is None:
            return pd.DataFrame({"Close": [c0]}, index=pd.DatetimeIndex([s]))
        e = pd.Timestamp(end) - pd.Timedelta(days=14)   # lands on the true maturity date
        c1 = (self._r.get("close_at") or {}).get(e.date().isoformat(), c1)
        if e <= s:
            return pd.DataFrame({"Close": [c0]}, index=pd.DatetimeIndex([s]))
        return pd.DataFrame({"Close": [c0, c1]}, index=pd.DatetimeIndex([s, e]))


class FakeYF:
    """Stand-in for the yfinance module. `registry`: symbol -> {last, currency, hist_close}."""
    def __init__(self, registry: dict):
        self.registry = registry

    def Ticker(self, symbol: str):
        return _FakeTicker(symbol, self.registry)


@pytest.fixture
def fake_yf(monkeypatch):
    """Patch ta_common.yf with a FakeYF built from a per-test registry, and clear the FX cache."""
    import ta_common

    def _install(registry: dict):
        monkeypatch.setattr(ta_common, "yf", FakeYF(registry))
        # fx_to_usd memoizes on a shared default-arg dict; reset it between tests.
        ta_common.fx_to_usd.__defaults__[0].clear()
        return registry

    return _install


# ---------- golden holdings CSVs (both account header variants, UTF-8-BOM) ----------

MARGIN_CSV = (
    "代码,名称,持有数量,现价,摊薄成本价,市值,盈亏比例,币种\n"
    "NVDA,英伟达,100,207.67,150.000,20767.00,+38.45%,USD\n"
    "TSM260918P360000,台积电PUT,-2,3.50,4.000,-700.00,-12.50%,USD\n"
    "ES3.SI,新加坡ETF,500,3.50,3.000,1750.00,+16.67%,SGD\n"
)

CASH_CSV = (
    "代码,名称,持有数量,现价,平均成本价,市值,未实现盈亏比例,币种\n"
    "META,Meta,50,669.21,600.000,33460.50,+11.54%,USD\n"
    "SHORTCO,做空标的,-10,20.00,25.000,-200.00,+20.00%,USD\n"
)


@pytest.fixture
def holdings_dir(tmp_path):
    """A base dir with one margin + one cash holdings CSV, named to match CSV_NAME_RE."""
    (tmp_path / "持仓-保证金账户(0001)-20260709-024150.csv").write_text(
        MARGIN_CSV, encoding="utf-8-sig")
    (tmp_path / "持仓-综合账户(0002)-20260709-024219.csv").write_text(
        CASH_CSV, encoding="utf-8-sig")
    return tmp_path


@pytest.fixture
def golden_ledger(tmp_path):
    """9-col ledger: a young UNRESOLVED row, a matured UNRESOLVED row, and a resolved row."""
    from datetime import date, timedelta
    young = (date.today() - timedelta(days=3)).isoformat()
    matured = (date.today() - timedelta(days=45)).isoformat()
    path = tmp_path / "ledger.md"
    path.write_text(
        "# Decisions Ledger\n\nSome intro prose about the 30-day and 180-day horizons.\n\n"
        "| Date | Ticker | Call | Conf | Price (USD) | SPY | Rationale | Status 30d | Status 180d |\n"
        "|---|---|---|---|---|---|---|---|---|\n"
        f"| {young} | NVDA | HOLD | med | 200.00 | 700.00 | young row | UNRESOLVED | UNRESOLVED |\n"
        f"| {matured} | AAA | BUY | high | 100.00 | 700.00 | matured row | UNRESOLVED | UNRESOLVED |\n"
        f"| 2026-01-01 | BBB | SELL | low | 50.00 | 600.00 | already done | WIN (30d, 2026-01-31) | UNRESOLVED |\n",
        encoding="utf-8")
    return path


@pytest.fixture
def legacy_ledger(tmp_path):
    """Old 7-col ledger, for the one-time migration test."""
    from datetime import date, timedelta
    matured = (date.today() - timedelta(days=45)).isoformat()
    path = tmp_path / "ledger.md"
    path.write_text(
        "# Decisions Ledger\n\nOld intro.\n\n"
        "| Date | Ticker | Call | Price (USD) | SPY | Rationale | Status |\n"
        "|---|---|---|---|---|---|---|\n"
        f"| {matured} | AAA | BUY | 100.00 | 700.00 | legacy row | UNRESOLVED |\n"
        "| 2026-01-01 | BBB | SELL | 50.00 | 600.00 | done | WIN (30d, 2026-01-31) |\n",
        encoding="utf-8")
    return path
