"""Pure concentration/sector helpers for stage0. No network here - callers pass in data."""
from __future__ import annotations
import json
from pathlib import Path

# Non-US holdings CSVs carry a bare code (e.g. "1023", "5347", "ES3") plus a ccy field; Yahoo
# needs an exchange suffix to resolve them. US/USD codes take no suffix.
# Only currencies that map to a single main exchange are listed (CNY is ambiguous: .SS/.SZ;
# EUR spans many exchanges) — for those, put the full Yahoo symbol in the holdings file.
CCY_SUFFIX = {"SGD": ".SI", "MYR": ".KL", "HKD": ".HK", "JPY": ".T", "GBP": ".L",
              "AUD": ".AX", "CAD": ".TO", "KRW": ".KS", "TWD": ".TW", "INR": ".NS"}


def yahoo_symbol(code: str, ccy: str) -> str:
    """Map a holdings-CSV code + currency to the Yahoo Finance symbol used for downloads."""
    code = (code or "").strip().upper()
    suffix = CCY_SUFFIX.get((ccy or "USD").strip().upper())
    if suffix and "." not in code:   # already-suffixed symbols (e.g. 7203.T, ES3.SI) pass through
        return code + suffix
    return code


def recent_moves(closes, symbol):
    """1d/5d/1m(21 trading day) pct-change for `symbol`'s column in `closes`, using the last
    available close and the close N trading rows back. Returns a dict with keys '1d'/'5d'/'1m'
    -> float fraction, or None per key when the symbol/data isn't available. Never raises."""
    out = {"1d": None, "5d": None, "1m": None}
    if closes is None or getattr(closes, "empty", True):
        return out
    cols = {c.upper(): c for c in closes.columns}
    col = cols.get((symbol or "").upper())
    if col is None:
        return out
    s = closes[col].dropna()
    for key, n in (("1d", 1), ("5d", 5), ("1m", 21)):
        if len(s) > n:
            try:
                prev = s.iloc[-1 - n]
                if prev:
                    out[key] = float(s.iloc[-1] / prev - 1)
            except (IndexError, ZeroDivisionError, TypeError):
                out[key] = None
    return out


def correlations(returns, ticker):
    ticker = ticker.upper()
    cols = {c.upper(): c for c in returns.columns}
    if ticker not in cols:
        return []
    corr = returns.corr()[cols[ticker]]
    out = []
    for sym, orig in cols.items():
        if sym == ticker:
            continue
        v = corr[orig]
        if v == v:  # drop NaN
            out.append((sym, float(v)))
    return sorted(out, key=lambda x: -abs(x[1]))


def sector_weights(positions, sectors):
    total = sum(abs(p["usd"]) for p in positions) or 1.0
    agg = {}
    for p in positions:
        sec = sectors.get(p["code"].upper()) or sectors.get(p["code"]) or "Unknown"
        agg[sec] = agg.get(sec, 0.0) + abs(p["usd"])
    return sorted(((s, 100 * v / total) for s, v in agg.items()), key=lambda x: -x[1])


def sizing_guardrail(ticker, positions, total, max_pos_pct):
    held = next((p for p in positions if p["code"].upper() == ticker.upper()), None)
    cur_usd = held["usd"] if held else 0.0
    cur_pct = 100 * cur_usd / total if total else 0.0
    cap_usd = max_pos_pct / 100 * total if total else 0.0
    headroom = cap_usd - cur_usd
    if not total:
        return (f"No portfolio total available - size {ticker} by profile "
                f"(max_position_pct {max_pos_pct}%) only.")
    if headroom <= 0:
        return (f"{ticker} is already {cur_pct:.1f}% of the ${total:,.0f} book, at/over the "
                f"{max_pos_pct}% cap - ADD would breach it; only HOLD/TRIM/SELL respect the cap.")
    return (f"{ticker} is {cur_pct:.1f}% (${cur_usd:,.0f}) of the ${total:,.0f} book. "
            f"Adding more than ${headroom:,.0f} takes it past the {max_pos_pct}% cap.")


def load_sector_cache(path):
    path = Path(path)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_sector_cache(path, data):
    Path(path).write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
