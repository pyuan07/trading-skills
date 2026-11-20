"""Shared helpers for the trading-agents data layer scripts.

All scripts exit non-zero with a one-line stderr reason on failure so the
skill orchestrator can retry once and then fall back to the WebFetch path.
"""
from __future__ import annotations

import re
import sys
from datetime import date, datetime, timedelta, timezone

import yfinance as yf

DISCLAIMER = "> ⚠️ Decision support, not financial advice. Verify all data before trading."

LEDGER_HEADER = "| Date | Ticker | Call | Conf | Price (USD) | SPY | Rationale | Status 30d | Status 180d |"
LEDGER_SEP = "|---|---|---|---|---|---|---|---|---|"

# OCC-style option symbol: underlying + YYMMDD expiry + C/P + strike (e.g. TSM260918P360000)
OCC_RE = re.compile(r"^(?P<underlying>[A-Z]{1,6})(?P<expiry>\d{6})(?P<cp>[CP])(?P<strike>\d{6,8})$")


def die(msg: str, code: int = 1) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(code)


def today() -> str:
    return date.today().isoformat()


def now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def parse_occ(symbol: str):
    """Return dict with underlying/expiry(date)/cp/strike for an OCC symbol, else None."""
    m = OCC_RE.match(symbol.strip().upper())
    if not m:
        return None
    exp = m.group("expiry")
    try:
        expiry = date(2000 + int(exp[0:2]), int(exp[2:4]), int(exp[4:6]))
    except ValueError:
        return None
    return {
        "underlying": m.group("underlying"),
        "expiry": expiry.isoformat(),
        "cp": "call" if m.group("cp") == "C" else "put",
        "strike": int(m.group("strike")) / 1000.0,
    }


def quote(symbol: str):
    """Return (last_price, currency) for a Yahoo symbol. Raises on failure."""
    t = yf.Ticker(symbol)
    fi = t.fast_info
    price = fi["lastPrice"]
    ccy = (fi["currency"] or "USD").upper()
    if price is None:
        raise RuntimeError(f"no price for {symbol}")
    return float(price), ccy


def fx_to_usd(ccy: str, _cache: dict = {}) -> float:
    """USD per 1 unit of ccy, via Yahoo '<CCY>USD=X'. Cached per process."""
    ccy = ccy.upper()
    if ccy == "USD":
        return 1.0
    if ccy not in _cache:
        price, _ = quote(f"{ccy}USD=X")
        _cache[ccy] = price
    return _cache[ccy]


def usd_quote(symbol: str) -> float:
    """Last price converted to USD regardless of the ticker's native currency."""
    price, ccy = quote(symbol)
    return price * fx_to_usd(ccy)


def _period_return(symbol: str, start: date, end: date):
    """Return (ret, start_date_used, end_date_used) from the first ADJUSTED daily close on/after
    `start` to the first on/after `end` (searched within a ~2-week window past `end`)."""
    h = yf.Ticker(symbol).history(
        start=start.isoformat(),
        end=(end + timedelta(days=14)).isoformat(),
        auto_adjust=True,
    )
    if h is None or h.empty:
        raise RuntimeError(f"no history for {symbol} from {start}")
    closes = h["Close"]
    dates = [ts.date() for ts in closes.index]
    end_i = next((i for i, d in enumerate(dates) if d >= end), None)
    if end_i is None:
        raise RuntimeError(f"no close for {symbol} on/after {end}")
    return float(closes.iloc[end_i] / closes.iloc[0] - 1), dates[0], dates[end_i]


def period_return_usd(symbol: str, start: date, end: date):
    """Total USD return over [start -> end], close-to-close. Returns (ret, start_date, end_date).

    Uses auto-adjusted closes so splits/dividends inside the window don't read as a price move
    (a raw-close comparison would grade a 10:1 split as -90%), and compounds the FX-pair return
    over the same window for non-USD listings.
    """
    _, ccy = quote(symbol)
    ret, d0, d1 = _period_return(symbol, start, end)
    if ccy != "USD":
        fx_ret, _, _ = _period_return(f"{ccy}USD=X", start, end)
        ret = (1 + ret) * (1 + fx_ret) - 1
    return ret, d0, d1


def fmt(x, nd: int = 2) -> str:
    """Format a number with thousands separators, or '--' when missing."""
    if x is None:
        return "--"
    try:
        return f"{x:,.{nd}f}"
    except (TypeError, ValueError):
        return str(x)


def pct(x, nd: int = 1) -> str:
    """Format a 0-1 fraction as a percentage, or '--' when missing."""
    if x is None:
        return "--"
    try:
        return f"{100 * x:.{nd}f}%"
    except (TypeError, ValueError):
        return str(x)
