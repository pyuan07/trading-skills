# /// script
# requires-python = ">=3.10"
# dependencies = ["yfinance>=1.5,<2", "pandas>=2.0,<4", "lxml>=4.9"]
# ///
"""stage0.py (--ticker T | --portfolio-review) [--run-dir work/...] [--base-dir .]

Replaces Stage 0 of the trading-agents skill:
  1. Finds holdings files (warns if >7 days old): broker-neutral holdings.csv /
     holdings-*.csv, and/or the newest moomoo/Futu 持仓-*.csv export per account.
  2. Parses holdings (UTF-8-BOM, header names differ per account), detects
     OCC-style option rows, keeps short positions negative.
  3. Normalizes everything to USD via Yahoo FX rates.
  4. Grades matured UNRESOLVED ledger rows (this ticker, or ALL with --portfolio-review)
     at 30-day and 180-day horizons vs SPY — adjusted close-to-close, so splits/dividends
     don't read as moves — rewriting reports/ledger.md atomically. Rows younger than the
     horizon stay UNRESOLVED. --skip-resolve skips this step (portfolio-review per-ticker
     reruns where the shared pass already graded every ticker).
  5. Writes work/<run>/context.md with the sections downstream agents expect.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import yfinance as yf

import concentration
import options_ctx
from ta_common import LEDGER_HEADER, LEDGER_SEP, die, fmt, fx_to_usd, parse_occ, pct, period_return_usd, today, usd_quote

CSV_NAME_RE = re.compile(r"持仓-(?P<label>.+?)\((?P<acct>\d+)\)-(?P<ts>\d{8}-\d{6})\.csv$")

PROFILE_DEFAULTS = {
    "risk_tolerance": "moderate",
    "max_position_pct": 20,
    "margin_comfort": "low",
    "horizon": "6-18 months",
    "base_currency": "USD",
}

BAND_30 = 0.03    # ±3% vs SPY counts FLAT at the 30-day horizon
BAND_180 = 0.06   # ±6% at the 180-day horizon (more noise over 6 months)
HORIZONS = ((30, "status_30", BAND_30), (180, "status_180", BAND_180))

LEDGER_KEYS = ["date", "ticker", "call", "conf", "price", "spy", "rationale", "status_30", "status_180"]


def migrate_row(cells):
    """Legacy 7-cell ledger row -> 9-key dict. Insert Conf='--', old Status -> Status 30d,
    Status 180d='UNRESOLVED'. Returns None if `cells` is not exactly 7 long."""
    if len(cells) != 7:
        return None
    date_, ticker, call, price, spy, rationale, status = cells
    return dict(zip(LEDGER_KEYS,
                    [date_, ticker, call, "--", price, spy, rationale, status, "UNRESOLVED"]))


def num(s):
    """Parse a CSV numeric cell ('1,234.56', '+8.93%', '--') to float or None. Sign preserved."""
    s = (s or "").strip().replace(",", "").replace("%", "").replace("+", "")
    if s in ("", "--"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def find_csvs(base: Path):
    """Newest holdings CSV per account number. Returns list of (path, acct, acct_type, dt)."""
    newest = {}
    for p in base.glob("持仓-*.csv"):
        m = CSV_NAME_RE.search(p.name)
        if not m:
            continue
        dt = datetime.strptime(m.group("ts"), "%Y%m%d-%H%M%S")
        acct = m.group("acct")
        if acct not in newest or dt > newest[acct][3]:
            acct_type = "Margin" if "保证金" in m.group("label") else ("Cash" if "综合" in m.group("label") else m.group("label"))
            newest[acct] = (p, acct, acct_type, dt)
    return list(newest.values())


def parse_holdings(path: Path, acct: str, acct_type: str):
    """Yield dicts for each position row. Handles both account header variants."""
    rows = list(csv.DictReader(io.open(path, encoding="utf-8-sig")))
    for r in rows:
        code = (r.get("代码") or "").strip()
        if not code:
            continue
        cost = num(r.get("摊薄成本价") or r.get("平均成本价"))
        pl_pct = num(r.get("盈亏比例") or r.get("未实现盈亏比例"))
        yield {
            "code": code,
            "name": (r.get("名称") or "").strip(),
            "qty": num(r.get("持有数量")),
            "price": num(r.get("现价")),
            "cost": cost,
            "mktval": num(r.get("市值")),
            "pl_pct": pl_pct,
            "ccy": (r.get("币种") or "USD").strip().upper(),
            "acct": acct,
            "acct_type": acct_type,
            "occ": parse_occ(code),
        }


# ---------- generic (broker-neutral) holdings CSV ----------
#
# holdings.csv / holdings-*.csv in the base dir, English headers (case-insensitive):
#   ticker (required), qty (required), currency, price, cost, market_value, pl_pct, name,
#   account, margin (yes/no). market_value defaults to qty * price when omitted.

GENERIC_ALIASES = {
    "code": ("ticker", "symbol"),
    "name": ("name", "description"),
    "qty": ("qty", "quantity", "shares"),
    "price": ("price", "last", "last_price"),
    "cost": ("cost", "avg_cost", "cost_basis", "average_cost"),
    "mktval": ("market_value", "mkt_value", "value"),
    "pl_pct": ("pl_pct", "pnl_pct", "unrealized_pl_pct"),
    "ccy": ("currency", "ccy"),
    "acct": ("account", "acct"),
    "margin": ("margin",),
}


def find_generic_csvs(base: Path):
    """Broker-neutral holdings files: holdings.csv and holdings-*.csv. Returns (path, mtime)."""
    paths = sorted(set(base.glob("holdings.csv")) | set(base.glob("holdings-*.csv")))
    return [(p, datetime.fromtimestamp(p.stat().st_mtime)) for p in paths]


def parse_generic_holdings(path: Path):
    """Yield position dicts (same shape as parse_holdings) from a broker-neutral CSV."""
    with io.open(path, encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        headers = {(h or "").strip().lower(): h for h in (reader.fieldnames or [])}
        rows = list(reader)

    def col(r, key):
        for alias in GENERIC_ALIASES[key]:
            if alias in headers:
                return r.get(headers[alias])
        return None

    if not any(a in headers for a in GENERIC_ALIASES["code"]):
        raise ValueError(f"{path.name}: needs a 'ticker' (or 'symbol') column")
    for r in rows:
        code = (col(r, "code") or "").strip().upper()
        if not code:
            continue
        qty, price = num(col(r, "qty")), num(col(r, "price"))
        mktval = num(col(r, "mktval"))
        if mktval is None and qty is not None and price is not None:
            mktval = qty * price
        margin = (col(r, "margin") or "").strip().lower() in ("yes", "y", "true", "1")
        cost, pl_pct = num(col(r, "cost")), num(col(r, "pl_pct"))
        if pl_pct is None and cost and price is not None:
            pl_pct = round(100 * (price / cost - 1), 2) * (1 if (qty or 0) >= 0 else -1)
        yield {
            "code": code,
            "name": (col(r, "name") or "").strip(),
            "qty": qty,
            "price": price,
            "cost": cost,
            "mktval": mktval,
            "pl_pct": pl_pct,
            "ccy": (col(r, "ccy") or "").strip().upper() or "USD",
            "acct": (col(r, "acct") or "").strip() or path.stem,
            "acct_type": "Margin" if margin else "Cash",
            "occ": parse_occ(code),
        }


# ---------- ledger ----------

def read_ledger(path: Path):
    """Return (pre_lines, rows) where rows are dicts of the ledger table cells."""
    if not path.exists():
        return None, []
    lines = path.read_text(encoding="utf-8").splitlines()
    pre, rows, in_table = [], [], False
    just_saw_header = False
    for ln in lines:
        cells = [c.strip() for c in ln.strip().strip("|").split("|")] if ln.strip().startswith("|") else None
        if cells and cells[0] == "Date":
            in_table = True
            # migrating a legacy (7-col) ledger must also migrate its header/sep text, not just
            # the row cells, so a rewritten legacy ledger doesn't end up with a stale header.
            pre.append(LEDGER_HEADER)
            just_saw_header = True
            continue
        if just_saw_header:
            just_saw_header = False
            if cells and set(ln) <= set("|- "):
                pre.append(LEDGER_SEP)
                continue
        if in_table and cells and not set(ln) <= set("|- "):
            if len(cells) == 9:
                rows.append(dict(zip(LEDGER_KEYS, cells)))
            elif len(cells) == 7:
                rows.append(migrate_row(cells))
            else:
                # wrong cell count (hand-edited row, stray '|' in a rationale): preserve verbatim,
                # never grade or rebind its columns
                rows.append({"_raw": ln})
        else:
            pre.append(ln)
    return "\n".join(pre), rows


def grade(call, rel, band):
    """WIN/LOSS/FLAT for a call given relative return `rel` (stock-SPY) and a FLAT `band`.
    HOLD is asymmetric: WIN when it merely tracks SPY, LOSS only when it materially lags."""
    call = call.upper()
    if call == "HOLD":
        return "LOSS" if rel < -band else "WIN"
    direction = 1 if call in ("BUY", "ADD") else -1   # TRIM/SELL are 'reduce' bets
    if abs(rel) <= band:
        return "FLAT"
    return "WIN" if rel * direction > 0 else "LOSS"


def resolve_rows(rows, target_ticker):
    """Grade matured horizon cells in place. Each of status_30 / status_180 is graded once its
    own horizon passes, over exactly [call_date -> call_date+N], adjusted close-to-close vs SPY.
    Returns (notes, changed); changed is True only if some cell's status was rewritten."""
    notes, changed = [], False
    today_d = date.today()
    todo = [r for r in rows if "_raw" not in r
            and (target_ticker is None or r["ticker"].upper() == target_ticker)]
    for r in todo:
        try:
            call_date = date.fromisoformat(r["date"])
        except ValueError:
            notes.append(f"{r['ticker']}: unparseable call date {r['date']!r}; left UNRESOLVED")
            continue
        for days, key, band in HORIZONS:
            if not r[key].startswith("UNRESOLVED"):
                continue
            mature = call_date + timedelta(days=days)
            if today_d < mature:
                notes.append(f"{r['ticker']} {r['call']} @{r['date']}: "
                             f"{(mature - today_d).days}d until {days}d horizon; {key} UNRESOLVED")
                continue
            try:
                stock_ret, _, used_d = period_return_usd(r["ticker"], call_date, mature)
                spy_ret, _, _ = period_return_usd("SPY", call_date, mature)
                rel = stock_ret - spy_ret
                status = grade(r["call"], rel, band)
                r[key] = f"{status} ({days}d, {used_d})"
                changed = True
                notes.append(f"{r['ticker']} {r['call']} ({r['date']}) {days}d: to {used_d} "
                             f"{stock_ret:+.1%} vs SPY {spy_ret:+.1%} (rel {rel:+.1%}) -> {status}")
            except Exception as e:
                notes.append(f"{r['ticker']}: {days}d resolution at {mature} failed ({e}); UNRESOLVED")
    return notes, changed


def write_ledger(path: Path, pre: str, rows):
    out = pre.rstrip("\n") + "\n"
    for r in rows:
        if "_raw" in r:
            out += r["_raw"] + "\n"
        else:
            out += ("| " + " | ".join(r[k] for k in LEDGER_KEYS) + " |\n")
    # atomic replace: the ledger is the pipeline's long-term memory — never truncate it in place
    tmp = path.with_suffix(".md.tmp")
    tmp.write_text(out, encoding="utf-8")
    tmp.replace(path)


# ---------- concentration / sector (network-boundary, best-effort) ----------

def batch_closes(symbols):
    """Best-effort ~6mo daily closes for correlation and recent-move math. Returns an empty
    DataFrame on ANY failure (network down, bad symbol, empty list, ...) so callers can degrade
    gracefully. `symbols` should already be Yahoo-resolved (see concentration.yahoo_symbol)."""
    try:
        if not symbols:
            return pd.DataFrame()
        data = yf.download(symbols, period="6mo", auto_adjust=True, progress=False)["Close"]
        if isinstance(data, pd.Series):
            data = data.to_frame(name=symbols[0])
        return data
    except Exception:
        return pd.DataFrame()


def sector_lookup(symbols, cache_path):
    """Best-effort sector-per-symbol lookup with a 30-day-stale on-disk cache. Fetches
    `.info["sector"]` only for symbols missing from the cache or whose cached entry is stale.
    Never raises; unknown/failed lookups fall back to 'Unknown'."""
    cache = concentration.load_sector_cache(cache_path)
    now = datetime.now()
    stale_cutoff = now - timedelta(days=30)
    result, dirty = {}, False
    for sym in symbols:
        sym = sym.upper()
        entry = cache.get(sym)
        needs_fetch = True
        if entry:
            try:
                needs_fetch = datetime.fromisoformat(entry.get("fetched", "")) < stale_cutoff
            except ValueError:
                needs_fetch = True
        if needs_fetch:
            sector = (entry or {}).get("sector", "Unknown")
            try:
                info = yf.Ticker(sym).info
                sector = info.get("sector") or "Unknown"
            except Exception:
                pass
            cache[sym] = {"sector": sector, "fetched": now.isoformat()}
            dirty = True
        result[sym] = cache[sym]["sector"]
    if dirty:
        try:
            concentration.save_sector_cache(cache_path, cache)
        except Exception:
            pass
    return result


# ---------- context.md ----------

def build_context(ticker, positions, options, fx_used, sources, profile, profile_note, past_rows,
                   max_pos_pct, returns=None, sectors=None, spot_by_underlying=None, closes=None):
    run_date = today()
    title = f"# Run Context — {ticker or 'Portfolio review'} — {run_date}"
    fx_str = ", ".join(f"{c}→USD {r:.4f}" for c, r in sorted(fx_used.items())) or "all positions already USD"
    src_str = "; ".join(sources)

    total = sum(p["usd"] for p in positions)
    lines = [title, "", f"Data as of: {run_date}. FX: {fx_str}. Holdings source: {src_str}.", ""]

    held = next((p for p in positions if ticker and p["code"].upper() == ticker), None)
    if held:
        lines += [f"**{ticker} is an existing holding** - this run evaluates ADD / HOLD / TRIM / SELL, not a fresh-buy decision.", ""]

    lines += ["## Portfolio (USD)", "",
              f"Equity/ETF positions, market value converted to USD. Percentages are of total equity/ETF value (${fmt(total)}). Short positions carry negative values.",
              "",
              "| Ticker | Name | Qty | Currency | Mkt val (native) | USD value | % of equity | Cost | P/L % | 1d | 5d | 1m | Account | Margin |",
              "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for p in sorted(positions, key=lambda x: -abs(x["usd"])):
        pct_of = 100 * p["usd"] / total if total else 0
        # abs(): a -25% short is just as concentrated as a +25% long
        warn = " ⚠️" if abs(pct_of) > max_pos_pct else ""
        bold = "**" if ticker and p["code"].upper() == ticker else ""
        sym = concentration.yahoo_symbol(p["code"], p["ccy"])
        moves = concentration.recent_moves(closes, sym)
        lines.append(
            f"| {bold}{p['code']}{bold} | {p['name']} | {fmt(p['qty'], 0)} | {p['ccy']} | {fmt(p['mktval'])} | "
            f"{fmt(p['usd'])} | {pct_of:.1f}%{warn} | {fmt(p['cost'], 3)} | {'--' if p['pl_pct'] is None else fmt(p['pl_pct'], 2) + '%'} | "
            f"{pct(moves['1d'])} | {pct(moves['5d'])} | {pct(moves['1m'])} | "
            f"{p['acct_type']}({p['acct']}) | {'yes' if p['acct_type'] == 'Margin' else 'no'} |")
    lines += ["", f"Total equity/ETF USD value: **${fmt(total)}**.", ""]

    if ticker is not None:
        lines += ["## Sizing guardrails", "",
                  concentration.sizing_guardrail(ticker, positions, total, max_pos_pct), ""]

    lines += ["## Concentration", ""]
    if sectors is None:
        lines += ["Sector data unavailable.", ""]
    else:
        weights = concentration.sector_weights(positions, sectors) if positions else []
        if weights:
            lines += ["| Sector | % of book |", "|---|---|"]
            lines += [f"| {sec} | {pct:.1f}% |" for sec, pct in weights]
            lines.append("")
        else:
            lines += ["No equity/ETF positions to compute sector weights.", ""]
    if ticker is not None:
        if returns is None or returns.empty:
            lines += ["Correlation data unavailable.", ""]
        else:
            corrs = concentration.correlations(returns, ticker)
            if corrs:
                top = ", ".join(f"{sym} {c:.2f}" for sym, c in corrs[:5])
                lines += [f"{ticker} is most correlated with: {top} (6-mo daily returns).", ""]
            else:
                lines += [f"No correlation data available for {ticker}.", ""]

    if options:
        spots = spot_by_underlying or {}
        net_opt = sum(o["usd"] for o in options)
        net_notional = 0.0
        lines += ["## Options positions", "",
                  "| OCC symbol | Underlying | Qty | Notional (USD) | DTE | Moneyness | USD mkt val | Account | Tag |",
                  "|---|---|---|---|---|---|---|---|---|"]
        for o in options:
            side = "long" if (o["qty"] or 0) > 0 else "short"
            occ = o["occ"]
            tag = f"{side} {occ['cp']} on {occ['underlying']}, exp {occ['expiry']}, strike {fmt(occ['strike'], 0)}"
            enriched = options_ctx.enrich(o, spots.get(occ["underlying"]), run_date)
            try:
                net_notional += float(enriched["notional"].replace(",", ""))
            except ValueError:
                pass
            lines.append(f"| {o['code']} | {occ['underlying']} | {'+' if (o['qty'] or 0) > 0 else ''}{fmt(o['qty'], 0)} ({side}) | "
                         f"{enriched['notional']} | {enriched['dte']} | {enriched['moneyness']} | "
                         f"{fmt(o['usd'])} | {o['acct_type']}({o['acct']}) | {tag} |")
        lines += ["", f"Net option market value: **${fmt(net_opt)}**. Net option notional: **${fmt(net_notional)}** vs "
                      f"equity/ETF book total **${fmt(total)}**. Options are risk/exposure context for their "
                      "underlying — not run through the equity pipeline.", ""]

    lines += ["## Profile", ""]
    if profile_note:
        lines += [profile_note, ""]
    lines += ["```json", json.dumps(profile, indent=2, ensure_ascii=False), "```", ""]
    if held and held["acct_type"] == "Margin" and str(profile.get("margin_comfort", "")).lower() == "low":
        lines += [f"{ticker} is held on **margin** (account {held['acct']}) while `margin_comfort` is **low** — relevant to any ADD decision.", ""]

    if ticker is not None:
        lines += [f"## Past calls for {ticker}", ""]
        if past_rows:
            lines += ["| Date | Call | Conf | Price (USD) | SPY | Rationale | Status 30d | Status 180d |",
                      "|---|---|---|---|---|---|---|---|"]
            for r in past_rows:
                lines.append(f"| {r['date']} | {r['call']} | {r['conf']} | {r['price']} | {r['spy']} "
                             f"| {r['rationale']} | {r['status_30']} | {r['status_180']} |")
        else:
            lines.append("None — no prior ledger entries for this ticker.")
        lines.append("")
    return "\n".join(lines)


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description=__doc__)
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--ticker", help="single-ticker mode")
    mode.add_argument("--portfolio-review", action="store_true", help="resolve ALL ledger rows, no Past-calls section")
    ap.add_argument("--run-dir", help="default: work/YYYY-MM-DD-<TICKER|portfolio>")
    ap.add_argument("--base-dir", default=".", help="working dir containing holdings.csv (or 持仓-*.csv), reports/, portfolio-profile.json")
    ap.add_argument("--skip-resolve", action="store_true",
                    help="portfolio-review per-ticker reruns: build the full per-ticker context.md "
                         "(holding banner, Past-calls section) without re-grading the ledger")
    args = ap.parse_args()
    if args.skip_resolve and args.portfolio_review:
        die("--skip-resolve only applies to --ticker runs")

    base = Path(args.base_dir).resolve()
    ticker = args.ticker.upper() if args.ticker else None
    run_dir = Path(args.run_dir) if args.run_dir else base / "work" / f"{today()}-{ticker or 'portfolio'}"
    run_dir.mkdir(parents=True, exist_ok=True)
    warnings = []

    # 1-2. holdings
    csvs = find_csvs(base)
    positions, options, sources = [], [], []
    generic = find_generic_csvs(base)
    if csvs and any(p.name == "holdings-moomoo.csv" for p, _ in generic):
        # live moomoo positions supersede a manual 持仓-*.csv export — never count both
        warnings.append("holdings-moomoo.csv (live moomoo positions) present — ignoring "
                        + ", ".join(p.name for p, *_ in csvs))
        csvs = []
    if not csvs and not generic:
        warnings.append("no holdings file found (holdings.csv, or a moomoo/Futu 持仓-*.csv export) — "
                        "running ticker-only (position-aware stages degrade)")

    def _add_source(path, dt):
        age = (datetime.now() - dt).days
        sources.append(f"{path.name} ({dt.date()}, {age}d old)")
        if age > 7:
            warnings.append(f"{path.name} is {age} days old (>7)")

    for path, acct, acct_type, dt in csvs:
        _add_source(path, dt)
        for p in parse_holdings(path, acct, acct_type):
            (options if p["occ"] else positions).append(p)
    for path, dt in generic:
        _add_source(path, dt)
        try:
            for p in parse_generic_holdings(path):
                (options if p["occ"] else positions).append(p)
        except (ValueError, OSError, csv.Error) as e:
            warnings.append(f"{path.name} skipped: {e}")

    # 3. USD normalization (signed: shorts stay negative)
    fx_used = {}
    for p in positions + options:
        if p["mktval"] is None:
            warnings.append(f"{p['code']}: market value missing — counted as $0 in USD totals and concentration math")
        mv = p["mktval"] if p["mktval"] is not None else 0.0
        try:
            rate = fx_to_usd(p["ccy"])
        except Exception as e:
            warnings.append(f"{p['code']}: FX {p['ccy']}->USD unavailable ({e}) — counted as $0")
            rate = 0.0
        if p["ccy"] != "USD":
            fx_used[p["ccy"]] = rate
        p["usd"] = mv * rate

    # profile
    profile_path = base / "portfolio-profile.json"
    profile_note = None
    if profile_path.exists():
        try:
            profile = json.loads(profile_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            die(f"portfolio-profile.json is not valid JSON: {e}")
        if not isinstance(profile, dict):
            die("portfolio-profile.json must be a JSON object")
    else:
        profile = dict(PROFILE_DEFAULTS)
        profile_note = ("Using defaults — `portfolio-profile.json` not found. "
                        "Tell the user to copy the skill's `examples/portfolio-profile.example.json` into the "
                        "working directory; assumption noted in the report.")
        warnings.append("portfolio-profile.json missing; defaults used")
    try:
        max_pos_pct = float(profile.get("max_position_pct", 20))
    except (TypeError, ValueError):
        die(f"portfolio-profile.json max_position_pct is not a number: {profile.get('max_position_pct')!r}")

    # 4. ledger reflection
    ledger_path = base / "reports" / "ledger.md"
    ledger_path.parent.mkdir(exist_ok=True)
    if not ledger_path.exists():
        ledger_path.write_text(
            "# Decisions Ledger\n\nAppend-only. Each run appends a new row and grades prior rows "
            "for the analyzed ticker at TWO fixed horizons (30 and 180 days): the call's return vs "
            "SPY over exactly [call date → call date + N days], adjusted close-to-close (splits/"
            "dividends don't count; Price/SPY cells are display-only. WIN = correct in the called "
            "direction, LOSS = wrong, FLAT = within band (±3% at 30d, ±6% at 180d). HOLD scores WIN "
            "when it merely tracks SPY, LOSS only when it materially lags. TRIM/SELL are 'reduce' "
            "bets (WIN if the name later lags SPY). Cells stay UNRESOLVED until their horizon matures.\n\n"
            f"{LEDGER_HEADER}\n{LEDGER_SEP}\n",
            encoding="utf-8")
    pre, rows = read_ledger(ledger_path)
    malformed = sum(1 for r in rows if "_raw" in r)
    if malformed:
        warnings.append(f"{malformed} ledger row(s) don't have 9 cells — preserved verbatim, never graded; fix by hand")
    if args.skip_resolve:
        notes, changed = [], False
    else:
        notes, changed = resolve_rows(rows, ticker)  # ticker=None in portfolio-review -> all rows
    if changed:
        write_ledger(ledger_path, pre, rows)
    past_rows = [r for r in rows if ticker and r.get("ticker", "").upper() == ticker]

    # concentration: correlations + sector weights (best-effort, network; never abort stage0)
    equity_syms = [p["code"] for p in positions]
    corr_syms = sorted({concentration.yahoo_symbol(p["code"], p["ccy"]) for p in positions}
                        | ({ticker} if ticker else set()))
    try:
        closes = batch_closes(corr_syms)
        # NaNs and all: a few failed/suffix-less symbols must not blank out the whole matrix.
        # concentration.correlations() uses pandas .corr(), which is pairwise-complete by default.
        returns = closes.pct_change(fill_method=None)
    except Exception as e:
        warnings.append(f"correlation data unavailable ({e})")
        closes, returns = pd.DataFrame(), pd.DataFrame()
    try:
        sectors = sector_lookup(equity_syms, base / "reports" / "holdings-meta.json")
    except Exception as e:
        warnings.append(f"sector data unavailable ({e})")
        sectors = None

    # options: spot per unique underlying (best-effort, network; never abort stage0)
    spot_by_underlying = {}
    for u in sorted({o["occ"]["underlying"] for o in options}):
        try:
            spot_by_underlying[u] = usd_quote(u)
        except Exception as e:
            spot_by_underlying[u] = None
            warnings.append(f"spot price unavailable for {u} ({e}) — options moneyness shown as '--'")

    # 5. context.md
    ctx = build_context(ticker, positions, options, fx_used, sources or ["(none)"],
                        profile, profile_note, past_rows, max_pos_pct,
                        returns=returns, sectors=sectors, spot_by_underlying=spot_by_underlying,
                        closes=closes)
    if warnings:
        ctx += "\n## Warnings\n\n" + "\n".join(f"- {w}" for w in warnings) + "\n"
    (run_dir / "context.md").write_text(ctx, encoding="utf-8")

    total = sum(p["usd"] for p in positions)
    print(f"OK: {run_dir / 'context.md'}")
    print(f"    {len(positions)} equity positions (${total:,.0f} USD), {len(options)} option positions")
    for n in notes:
        print(f"    ledger: {n}")
    for w in warnings:
        print(f"    WARNING: {w}")


if __name__ == "__main__":
    main()
