# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""moomoo_data.py — turn saved moomoo MCP responses into files the pipeline already reads.

The moomoo MCP tools are called by the agent (not by Python); the agent saves each raw JSON
response to a file and this script converts them deterministically. No network, no trading.

  positions  ACC1.json [ACC2.json ...] [--out holdings-moomoo.csv] [--acc-type margin ...]
      account_positions responses -> broker-neutral holdings CSV (Yahoo symbols, signed shorts).
  insights   TICKER --run-dir work/<run> [--consensus F] [--short-interest F] [--quote F] [--unusual F]
      research/short-interest/quote responses -> <run-dir>/data/moomoo.md for the analysts.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

# moomoo market prefix -> Yahoo Finance suffix
MARKET_SUFFIX = {"US": "", "HK": ".HK", "SG": ".SI", "MY": ".KL", "JP": ".T", "AU": ".AX",
                 "CA": ".TO", "SH": ".SS", "SZ": ".SZ", "KR": ".KS"}
YAHOO_TO_MARKET = {v: k for k, v in MARKET_SUFFIX.items() if v}
HOLDINGS_FIELDS = ["ticker", "name", "qty", "price", "cost", "market_value", "pl_pct",
                   "currency", "account", "margin"]


def die(msg: str) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def to_yahoo(code: str) -> str:
    """'US.VOO' -> 'VOO', 'SG.ES3' -> 'ES3.SI', 'HK.00700' -> '0700.HK', 'SH.600519' -> '600519.SS'.
    US option codes ('US.TSM260918P360000') come back bare, which the OCC parser recognizes."""
    market, _, sym = (code or "").strip().upper().partition(".")
    if not sym:
        return market
    suffix = MARKET_SUFFIX.get(market)
    if suffix is None:
        return sym                     # unknown market: best effort, bare symbol
    if market == "HK" and sym.isdigit():
        sym = sym.lstrip("0").zfill(4)  # Yahoo uses 4-digit HK codes
    return sym + suffix


def to_moomoo(ticker: str) -> str:
    """Inverse of to_yahoo for analyst lookups: 'NVDA' -> 'US.NVDA', '0700.HK' -> 'HK.00700'."""
    t = ticker.strip().upper()
    base, dot, suffix = t.rpartition(".")
    if dot and ("." + suffix) in YAHOO_TO_MARKET:
        market = YAHOO_TO_MARKET["." + suffix]
        if market == "HK" and base.isdigit():
            base = base.zfill(5)
        return f"{market}.{base}"
    return f"US.{t}"


def _load(path: Path):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        die(f"{path}: not readable JSON ({e})")
    # MCP envelopes seen: {"s":"ok","d":[...]} and {"ret_code":0,"data":{...}}
    if isinstance(data, dict):
        if "d" in data:
            return data["d"]
        if "data" in data:
            return data["data"]
    return data


def _num(s):
    try:
        return float(str(s).replace(",", ""))
    except (TypeError, ValueError):
        return None


def position_rows(items, account: str, margin: bool):
    for it in items or []:
        qty = _num(it.get("qty"))
        mv = _num(it.get("market_val"))
        if str(it.get("position_side", "LONG")).upper() == "SHORT":
            qty = -abs(qty) if qty is not None else None
            mv = -abs(mv) if mv is not None else None
        yield {
            "ticker": to_yahoo(it.get("code", "")),
            "name": it.get("stock_name", ""),
            "qty": "" if qty is None else f"{qty:g}",
            "price": it.get("nominal_price", ""),
            "cost": it.get("cost_price", "") if it.get("cost_price_valid", True) else "",
            "market_value": "" if mv is None else f"{mv:.2f}",
            "pl_pct": it.get("pl_ratio", "") if it.get("pl_ratio_valid", True) else "",
            "currency": (it.get("currency") or "USD").upper(),
            "account": account,
            "margin": "yes" if margin else "no",
        }


def cmd_positions(args) -> None:
    rows = []
    types = args.acc_type or []
    for i, f in enumerate(args.files):
        acct = f"moomoo-{Path(f).stem}"
        margin = (types[i] if i < len(types) else "cash").lower() == "margin"
        rows.extend(position_rows(_load(Path(f)), acct, margin))
    out = Path(args.out)
    with out.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=HOLDINGS_FIELDS)
        w.writeheader()
        w.writerows(rows)
    print(f"OK: {out} ({len(rows)} positions from {len(args.files)} account file(s))")


def _pct(x, scale=1.0):
    v = _num(x)
    return "--" if v is None else f"{v * scale:.1f}%"


def cmd_insights(args) -> None:
    sym = to_moomoo(args.ticker)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [f"# moomoo data — {args.ticker.upper()} ({sym})", "",
             f"Data as of: {now} (source: moomoo MCP). Sections are omitted when a call was "
             "unavailable (no market-data permission, unsupported market, or not requested).", ""]

    if args.quote:
        q = _load(Path(args.quote))
        q = (q.get("list") or q.get("items") or [q])[0] if isinstance(q, dict) else (q or [{}])[0]
        lines += ["## Live quote", "",
                  f"Last {q.get('last_price', q.get('cur_price', '--'))} · prev close "
                  f"{q.get('prev_close_price', '--')} · volume {q.get('volume', '--')} · "
                  f"time {q.get('data_date', '')} {q.get('data_time', '')}".rstrip(), ""]

    if args.consensus:
        c = _load(Path(args.consensus)) or {}
        lines += ["## Analyst consensus", "",
                  "| Analysts | Strong buy | Hold | Sell | Target avg | Target low | Target high | Updated |",
                  "|---|---|---|---|---|---|---|---|",
                  f"| {c.get('total', '--')} | {_pct(c.get('strong_buy'))} | {_pct(c.get('hold'))} | "
                  f"{_pct(c.get('sell'))} | {c.get('average', '--')} | {c.get('lowest', '--')} | "
                  f"{c.get('highest', '--')} | {c.get('update_time_str', '--')} |", ""]

    if args.short_interest:
        s = _load(Path(args.short_interest)) or {}
        items = s.get("items", []) if isinstance(s, dict) else s
        if items:
            lines += ["## Short interest (newest first)", "",
                      "| Date | Shares short | % of float | Days to cover |", "|---|---|---|---|"]
            for it in items[:6]:
                shares = _num(it.get("shares_short"))
                shares_s = "--" if shares is None else f"{shares:,.0f}"
                lines.append(f"| {it.get('timestamp_str', '--')} | {shares_s} | "
                             f"{_pct(it.get('short_percent'))} | {it.get('days_to_cover', '--')} |")
            lines.append("")

    if args.unusual:
        u = _load(Path(args.unusual)) or {}
        content = (u.get("content") if isinstance(u, dict) else "") or ""
        if content.strip():
            # "[timestamp: 1795064400]" -> "**2026-11-19:**" (epoch seconds, shown as a UTC date)
            content = re.sub(
                r"\[timestamp:\s*(\d+)\]\s*",
                lambda m: "**" + datetime.fromtimestamp(int(m.group(1)), timezone.utc).strftime("%Y-%m-%d") + ":** ",
                content.strip())
            lines += ["## Unusual capital activity (last 7 days)", "", content.replace("\n**", "\n\n**"), ""]

    run_dir = Path(args.run_dir)
    (run_dir / "data").mkdir(parents=True, exist_ok=True)
    out = run_dir / "data" / "moomoo.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"OK: {out}")


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("positions")
    p.add_argument("files", nargs="+", help="saved account_positions JSON, one file per account")
    p.add_argument("--acc-type", action="append",
                   help="'margin' or 'cash' per file, in order (from account_authorized_trd_accs)")
    p.add_argument("--out", default="holdings-moomoo.csv")
    p.set_defaults(func=cmd_positions)

    i = sub.add_parser("insights")
    i.add_argument("ticker")
    i.add_argument("--run-dir", required=True)
    for name in ("consensus", "short-interest", "quote", "unusual"):
        i.add_argument(f"--{name}")
    i.set_defaults(func=cmd_insights)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
