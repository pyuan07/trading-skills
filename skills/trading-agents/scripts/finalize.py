# /// script
# requires-python = ">=3.10"
# dependencies = ["yfinance>=1.5,<2", "pandas>=2.0,<4", "lxml>=4.9"]
# ///
"""finalize.py TICKER --run-dir work/YYYY-MM-DD-TICKER [--base-dir .]

Stage 5 wrap-up:
  1. Assembles reports/YYYY-MM-DD-TICKER.md from the stage files in the run dir.
  2. Fetches the current quote (USD-converted) and SPY via yfinance.
  3. Appends the ledger row from decision.md's CALL and RATIONALE line.
Prints the decision summary to stdout for the orchestrator.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from ta_common import DISCLAIMER, die, now_utc, quote, today, usd_quote

# (filename, section title) in report order; missing optional files are skipped with a note
SECTIONS = [
    ("context.md", "Run context (Stage 0)"),
    ("fundamentals.md", "Fundamentals analyst"),
    ("technical.md", "Technical analysis (script-generated)"),
    ("news-sentiment.md", "News & sentiment analyst"),
    ("bull.md", "Bull researcher"),
    ("bear.md", "Bear researcher"),
    ("trader-plan.md", "Trader plan"),
    ("risk-aggressive.md", "Risk debate — aggressive"),
    ("risk-conservative.md", "Risk debate — conservative"),
    ("risk-neutral.md", "Risk debate — neutral"),
    ("decision.md", "Portfolio manager decision"),
]

# Strict Stage 5 output contract: a "Final call:" line naming exactly ONE of
# BUY/ADD/HOLD/TRIM/SELL, a RATIONALE: line, and the SIZE_USD/SIZE_PCT/STOP/TARGET contract
# lines. Anything else is a contract violation — fail loudly so the orchestrator re-dispatches
# the portfolio manager rather than logging a mis-parse.
# Anchored to the line start (allowing markdown decoration like '#', '**', '-', '>') so prose
# such as "Before the final call we SELL..." earlier in the file can't be mistaken for it.
FINAL_CALL_LINE_RE = re.compile(r"^[\s#>*_\-]*final\s+call\b.*$", re.IGNORECASE | re.MULTILINE)
CALL_TOKEN_RE = re.compile(r"\b(BUY|ADD|HOLD|TRIM|SELL)\b", re.IGNORECASE)
RATIONALE_RE = re.compile(r"^\s*\**RATIONALE:?\**\s*(?P<r>.+?)\s*$", re.MULTILINE | re.IGNORECASE)
CONF_RE = re.compile(r"^\s*\**Confidence:?\**\s*(?P<c>low|med(?:ium)?|high)\b", re.MULTILINE | re.IGNORECASE)
_NUM = r"(?P<v>n/a|[-+]?\d[\d,]*\.?\d*)"
SIZE_USD_RE = re.compile(r"^\s*SIZE_USD:?\s*" + _NUM, re.MULTILINE | re.IGNORECASE)
SIZE_PCT_RE = re.compile(r"^\s*SIZE_PCT:?\s*(?P<v>[-+]?\d[\d,]*\.?\d*)", re.MULTILINE | re.IGNORECASE)
STOP_RE = re.compile(r"^\s*STOP:?\s*(?P<v>[-+]?\d[\d,]*\.?\d*)", re.MULTILINE | re.IGNORECASE)
TARGET_RE = re.compile(r"^\s*TARGET:?\s*(?P<v>[-+]?\d[\d,]*\.?\d*)", re.MULTILINE | re.IGNORECASE)


def parse_decision(text: str) -> dict:
    lm = FINAL_CALL_LINE_RE.search(text)
    if not lm:
        raise RuntimeError("decision.md violates the Stage 5 contract: no 'Final call:' line")
    tokens = CALL_TOKEN_RE.findall(lm.group(0))
    calls = {c.upper() for c in tokens}
    if len(calls) != 1:
        # tie-break: prose words on the line ("do not sell hedges") are lowercase; if exactly
        # one token is written in UPPERCASE, that's the call
        upper = {c for c in tokens if c.isupper()}
        if len(upper) == 1:
            calls = upper
        else:
            found = "/".join(sorted(calls)) if calls else "none"
            raise RuntimeError(
                f"'Final call:' line must name exactly one of BUY/ADD/HOLD/TRIM/SELL, found: {found}"
            )
    call = calls.pop()

    rm = RATIONALE_RE.search(text)
    if not rm:
        raise RuntimeError("decision.md violates the Stage 5 contract: no 'RATIONALE:' line")
    # a '|' in the rationale would break the ledger's markdown table
    rationale = re.sub(r"\s*\|\s*", " / ", rm.group("r").strip().rstrip("."))
    if not rationale:
        raise RuntimeError("RATIONALE line is empty")

    def need(rx, label):
        m = rx.search(text)
        if not m:
            raise RuntimeError(f"decision.md violates the Stage 5 contract: no '{label}:' line")
        return m.group("v").replace(",", "")

    size_usd = need(SIZE_USD_RE, "SIZE_USD")
    size_usd = "n/a" if size_usd.lower() == "n/a" else size_usd
    cm = CONF_RE.search(text)
    return {"call": call, "conf": (cm.group("c").lower().replace("medium", "med") if cm else "--"),
            "rationale": rationale, "size_usd": size_usd,
            "size_pct": need(SIZE_PCT_RE, "SIZE_PCT"),
            "stop": need(STOP_RE, "STOP"), "target": need(TARGET_RE, "TARGET")}


def _f(s):
    try:
        return float(str(s).replace(",", ""))
    except (TypeError, ValueError):
        return None


def sizing_warnings(d, call, total_usd, entry_ref, max_pos_pct):
    """Non-fatal sanity checks on the PM's numbers. Returns warning strings (possibly empty)."""
    w = []
    size_usd, size_pct = _f(d.get("size_usd")), _f(d.get("size_pct"))
    stop, target = _f(d.get("stop")), _f(d.get("target"))
    # the cap limits what a position may grow to — a TRIM/SELL can't breach it by being large
    if size_pct is not None and call.upper() in ("BUY", "ADD") and abs(size_pct) > max_pos_pct:
        w.append(f"SIZE_PCT {size_pct}% exceeds max_position_pct {max_pos_pct}% (cap breach — confirm intent)")
    if size_usd is not None and size_pct is not None and total_usd:
        implied = 100 * size_usd / total_usd
        if abs(implied - size_pct) > 1.5:   # >1.5pp apart
            w.append(f"SIZE_PCT {size_pct}% disagrees with SIZE_USD/total ({implied:.1f}%)")
    up = call.upper()
    if entry_ref and stop is not None and target is not None:
        if up in ("BUY", "ADD"):
            if stop >= entry_ref:
                w.append(f"STOP {stop} is not below entry ref {entry_ref:.2f} for a {up}")
            if target <= entry_ref:
                w.append(f"TARGET {target} is not above entry ref {entry_ref:.2f} for a {up}")
        elif up in ("TRIM", "SELL"):
            if stop <= entry_ref:
                w.append(f"STOP {stop} is not above entry ref {entry_ref:.2f} for a {up}")
    return w


def _portfolio_total(run_dir):
    m = re.search(r"Total equity/ETF USD value:\s*\*\*\$([\d,]+\.?\d*)\*\*",
                  (run_dir / "context.md").read_text(encoding="utf-8"))
    return float(m.group(1).replace(",", "")) if m else None


def _max_pos_pct(base):
    import json
    p = base / "portfolio-profile.json"
    try:
        return float(json.loads(p.read_text(encoding="utf-8")).get("max_position_pct", 20))
    except Exception:
        return 20.0


def upsert_ledger_row(ledger_path: Path, row: str, date_s: str, ticker: str) -> str:
    """Append `row`, or replace today's still-UNRESOLVED row for the same ticker (same-day
    re-run: regenerated report, retried finalize) so the tally never double-counts one call.
    Returns 'appended' or 'replaced'."""
    lines = ledger_path.read_text(encoding="utf-8").splitlines()
    prefix = f"| {date_s} | {ticker} |"
    for i, ln in enumerate(lines):
        if ln.startswith(prefix) and ln.rstrip().endswith("UNRESOLVED | UNRESOLVED |"):
            lines[i] = row
            action = "replaced (same-day re-run)"
            break
    else:
        lines.append(row)
        action = "appended"
    # atomic replace (same as stage0.write_ledger): the ledger must never be left truncated
    tmp = ledger_path.with_suffix(".md.tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    tmp.replace(ledger_path)
    return action


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("ticker")
    ap.add_argument("--run-dir", required=True)
    ap.add_argument("--base-dir", default=".")
    args = ap.parse_args()

    ticker = args.ticker.upper()
    run_dir = Path(args.run_dir)
    base = Path(args.base_dir).resolve()

    decision_path = run_dir / "decision.md"
    if not decision_path.exists():
        die(f"{decision_path} not found — run Stage 5 first")
    decision_text = decision_path.read_text(encoding="utf-8")
    try:
        d = parse_decision(decision_text)
    except Exception as e:
        die(str(e))
    call, rationale, conf = d["call"], d["rationale"], d["conf"]

    # quotes (USD)
    try:
        price_usd = usd_quote(ticker)
        spy_now, _ = quote("SPY")
    except Exception as e:
        die(f"quote fetch failed: {e}")

    # sizing sanity warnings — non-fatal, never abort the run
    try:
        warns = sizing_warnings(d, call, _portfolio_total(run_dir), price_usd, _max_pos_pct(base))
    except Exception:
        warns = []

    # report
    report_dir = base / "reports"
    report_dir.mkdir(exist_ok=True)
    report_path = report_dir / f"{today()}-{ticker}.md"
    parts = [
        f"# TradingAgents Report — {ticker} — {today()}",
        "",
        DISCLAIMER,
        "",
        f"Data as of: {now_utc()}. Price at report time: {price_usd:.2f} USD; SPY {spy_now:.2f}.",
        "",
        "## Decision summary",
        "",
        f"**{call}** ({conf} confidence) — {rationale}",
        "",
        f"Size: {d['size_usd']} USD / {d['size_pct']}% · Stop: {d['stop']} · Target: {d['target']}",
        "",
    ]
    if warns:
        parts += ["## Sizing checks", ""]
        parts += [f"- WARNING: {w}" for w in warns]
        parts += [""]
    missing = []
    for fname, title in SECTIONS:
        p = run_dir / fname
        if p.exists():
            parts += [f"## {title}", "", p.read_text(encoding="utf-8").strip(), "", "---", ""]
        else:
            missing.append(fname)
    if missing:
        parts += [f"_Missing stage files skipped: {', '.join(missing)}_", ""]
    parts += ["## Sources", "",
              "Yahoo Finance via yfinance data pack (`data/` in the run dir), plus any targeted web searches "
              "cited inside the analyst sections. Alpha Vantage MCP used only where noted.", ""]
    report_path.write_text("\n".join(parts), encoding="utf-8")

    # ledger append
    ledger_path = report_dir / "ledger.md"
    if not ledger_path.exists():
        die("reports/ledger.md not found — stage0.py should have created it")
    row = (f"| {today()} | {ticker} | {call} | {conf} | {price_usd:.2f} | {spy_now:.2f} "
           f"| {rationale} | UNRESOLVED | UNRESOLVED |")
    action = upsert_ledger_row(ledger_path, row, today(), ticker)

    print(f"OK: report {report_path}")
    print(f"    ledger row {action}: {row}")
    print(f"DECISION: {call} — {rationale}")
    if missing:
        print(f"    WARNING: missing stage files: {', '.join(missing)}")
    for w in warns:
        print(f"    WARNING: {w}")


if __name__ == "__main__":
    main()
