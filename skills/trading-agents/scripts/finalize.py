# /// script
# requires-python = ">=3.10"
# dependencies = ["yfinance>=1.5,<2", "pandas>=2.0,<4", "lxml>=4.9"]
# ///
"""finalize.py TICKER --run-dir work/YYYY-MM-DD-TICKER [--base-dir .]

Stage 5 wrap-up:
  1. Assembles reports/YYYY-MM-DD-TICKER.md from the stage files in the run dir.
  2. Fetches the current quote (USD-converted) and SPY via yfinance.
  3. Appends the ledger row from decision.md's CALL and RATIONALE line.
  4. Emits reports/YYYY-MM-DD-TICKER.json, updates reports/data.js, and ensures
     reports/dashboard.html exists.
Prints the decision summary to stdout for the orchestrator.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from pathlib import Path

from ta_common import DISCLAIMER, die, now_utc, quote, today, usd_quote

VALID_LANGUAGES = ("en", "zh", "both")
DISCLAIMER_ZH = "> ⚠️ 决策辅助工具，非投资建议。交易前请核实所有数据。"

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

SECTIONS_ZH = [
    ("context.md", "投资组合上下文 (阶段 0)"),
    ("fundamentals.md", "基本面分析"),
    ("technical.md", "技术面分析 (脚本生成)"),
    ("news-sentiment.md", "资讯与市场情绪"),
    ("bull.md", "多头论点"),
    ("bear.md", "空头论点"),
    ("trader-plan.md", "交易计划"),
    ("risk-aggressive.md", "风险辩论 — 激进派"),
    ("risk-conservative.md", "风险辩论 — 保守派"),
    ("risk-neutral.md", "风险辩论 — 中立派"),
    ("decision.md", "投资组合经理最终决策"),
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

DATA_JS_HEADER = (
    "// Auto-generated data bundle for TradingAgents Dashboard\n"
    "// Allows opening dashboard.html directly via file:// protocol without local CORS restrictions\n"
    "window.TRADING_AGENTS_RUNS = "
)


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
        return float(str(s).replace(",", "").replace("%", "").strip())
    except (TypeError, ValueError):
        return None


def _i(s):
    v = _f(s)
    return int(round(v)) if v is not None else None


def _read_opt(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.exists() else ""


def _strip_md(text: str) -> str:
    return re.sub(r"\*\*(.+?)\*\*", r"\1", text).strip()


def _parse_kv_table(text: str) -> dict[str, str]:
    """Parse 2-column markdown tables (`| key | value |`) into a lowercase-key dict."""
    out: dict[str, str] = {}
    for ln in text.splitlines():
        ln = ln.strip()
        if not ln.startswith("|") or not ln.endswith("|"):
            continue
        cells = [c.strip() for c in ln.strip("|").split("|")]
        if len(cells) < 2 or all(set(c) <= {"-", ":"} for c in cells):
            continue
        key = _strip_md(cells[0]).lower()
        if key and key not in ("metric", "signal", "dimension", "indicator", "item", "date"):
            out[key] = cells[1]
    return out


def _extract_key_points(text: str) -> list[str]:
    """Extract bullet lines after a `KEY POINTS:` heading/label."""
    m = re.search(r"(?:^|\n)[\s#*]*KEY\s+POINTS:?\s*\n(?P<body>.+?)(?:\n---|\n##\s|\Z)", text, re.DOTALL | re.IGNORECASE)
    if not m:
        return []
    pts = []
    for ln in m.group("body").splitlines():
        bm = re.match(r"^\s*[-*]\s+(.+)$", ln)
        if bm:
            pts.append(_strip_md(bm.group(1)))
    return pts


def _extract_execution_mandates(decision_text: str) -> list[str]:
    """Extract bulleted execution guidelines/mandates from decision.md."""
    m = re.search(r"execution[^\n]*:\s*\n(?P<body>(?:\s*[-*]\s+.+\n?)+)", decision_text, re.IGNORECASE)
    body = m.group("body") if m else decision_text
    mandates = []
    for ln in body.splitlines():
        bm = re.match(r"^\s*[-*]\s+(.+)$", ln)
        if bm:
            mandates.append(_strip_md(bm.group(1)))
    return mandates


def _parse_context(run_dir: Path, base: Path, ticker: str) -> dict:
    text = _read_opt(run_dir / "context.md")
    total_usd = _portfolio_total(run_dir) or 0.0

    # Profile JSON from context.md code block or portfolio-profile.json
    profile: dict = {}
    pm = re.search(r"##\s+Profile.*?```json\s*(\{.*?\})\s*```", text, re.DOTALL | re.IGNORECASE)
    if pm:
        try:
            profile = json.loads(pm.group(1))
        except Exception:
            profile = {}
    if not profile:
        p = base / "portfolio-profile.json"
        if p.exists():
            try:
                profile = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                profile = {}

    gm = re.search(r"##\s+Sizing guardrails\s*\n+([^\n#]+)", text, re.IGNORECASE)
    guardrails = gm.group(1).strip() if gm else ""

    # Detect existing holding: bolded ticker row in portfolio table or guardrail wording
    is_holding = bool(
        re.search(rf"\|\s*\*\*{re.escape(ticker)}\*\*\s*\|", text)
        or re.search(rf"\b{re.escape(ticker)}\s+is\s+already\b", guardrails, re.IGNORECASE)
    )
    return {
        "portfolio_total_usd": total_usd,
        "is_existing_holding": is_holding,
        "profile": profile,
        "sizing_guardrails": guardrails,
    }


def _parse_fundamentals(run_dir: Path) -> tuple[str, dict]:
    """Return (company_name, fundamentals_dict) parsed from data/fundamentals.md & fundamentals.md."""
    data_text = _read_opt(run_dir / "data" / "fundamentals.md")
    analyst_text = _read_opt(run_dir / "fundamentals.md")

    company_name = ""
    cm = re.search(r"^\*\*(.+?)\*\*\s+[—\-]", data_text, re.MULTILINE)
    if cm:
        company_name = cm.group(1).strip()
    elif analyst_text:
        hm = re.search(r"^#\s+[^:]+:\s*(.+?)(?:\s*\([A-Z0-9.\-]+\))?\s*$", analyst_text, re.MULTILINE)
        if hm:
            company_name = hm.group(1).strip()

    kv = _parse_kv_table(data_text)

    pe_trailing = pe_forward = None
    pe_raw = kv.get("p/e (trailing / forward)", "")
    if "/" in pe_raw:
        parts = pe_raw.split("/", 1)
        pe_trailing, pe_forward = _f(parts[0]), _f(parts[1])

    fcf_ttm = ocf_ttm = capex_ttm = None
    for k, v in kv.items():
        if k.startswith("free cash flow"):
            fm = re.match(r"([^\s(]+)(?:\s*\(OCF\s+([^,]+),\s*capex\s+([^)]+)\))?", v, re.IGNORECASE)
            if fm:
                fcf_ttm = fm.group(1).strip()
                ocf_ttm = fm.group(2).strip() if fm.group(2) else None
                capex_ttm = fm.group(3).strip() if fm.group(3) else None
            break

    gross_margin = _f(kv.get("gross margin"))
    if gross_margin is None and analyst_text:
        gm = re.search(r"Gross\s+Margin[^.\n]*?\b(\d+(?:\.\d+)?)%", analyst_text, re.IGNORECASE)
        if gm:
            gross_margin = _f(gm.group(1))

    rec_raw = kv.get("analyst recommendation", "")
    rec_m = re.match(r"([-+]?\d+(?:\.\d+)?)", rec_raw)
    analyst_rec = _f(rec_m.group(1)) if rec_m else None

    fundamentals = {
        "market_cap": kv.get("market cap") if kv.get("market cap") not in (None, "--") else None,
        "pe_trailing": pe_trailing,
        "pe_forward": pe_forward,
        "peg": _f(kv.get("peg")),
        "ps_ttm": _f(kv.get("p/s (ttm)")),
        "pb": _f(kv.get("p/b")),
        "gross_margin": gross_margin,
        "operating_margin": _f(kv.get("operating margin")),
        "net_margin": _f(kv.get("profit margin")),
        "roe": _f(kv.get("roe")),
        "debt_to_equity": _f(kv.get("debt/equity")),
        "fcf_ttm": fcf_ttm,
        "ocf_ttm": ocf_ttm,
        "capex_ttm": capex_ttm,
        "revenue_growth_yoy": _f(kv.get("revenue growth (yoy)")),
        "earnings_growth_yoy": _f(kv.get("earnings growth (yoy)")),
        "dividend_yield": _f(kv.get("dividend yield")),
        "beta": _f(kv.get("beta")),
        "analyst_target_mean": _f(kv.get("analyst target (mean)")),
        "analyst_recommendation": analyst_rec,
        "key_points": _extract_key_points(analyst_text),
    }
    return company_name, fundamentals


def _parse_technicals_and_regime(run_dir: Path, ticker: str, spy_now: float) -> tuple[dict, dict]:
    prices_text = _read_opt(run_dir / "data" / "prices.md")
    tech_text = _read_opt(run_dir / "technical.md")

    # Overall bias
    bm = re.search(r"\*\*Overall bias:\s*(.+?)\*\*", tech_text, re.IGNORECASE)
    bias = bm.group(1).strip().rstrip(".") if bm else ""

    # Returns row for ticker
    ret_1d = ret_5d = ret_1m = ret_ytd = None
    rm = re.search(
        rf"^\|\s*{re.escape(ticker)}\s*\|\s*([^|]+)\|\s*([^|]+)\|\s*([^|]+)\|\s*([^|]+)\|\s*([^|]+)\|",
        prices_text,
        re.MULTILINE | re.IGNORECASE,
    )
    if rm:
        ret_1d, ret_5d, ret_1m, ret_ytd = _f(rm.group(1)), _f(rm.group(2)), _f(rm.group(3)), _f(rm.group(5))

    # 52-week range
    range_52w = None
    r52 = re.search(
        r"52-week range:\*\*\s*([\d,.]+)\s*[–\-]\s*([\d,.]+)\s*\(last is\s*([-+]?[\d.]+)%",
        prices_text,
        re.IGNORECASE,
    )
    if r52:
        range_52w = {"low": _f(r52.group(1)), "high": _f(r52.group(2)), "pct_from_high": _f(r52.group(3))}

    # Indicators table (3 cols: Indicator | Value | Context)
    ind: dict[str, tuple[str, str]] = {}
    for ln in prices_text.splitlines():
        ln = ln.strip()
        if not ln.startswith("|") or not ln.endswith("|"):
            continue
        cells = [c.strip() for c in ln.strip("|").split("|")]
        if len(cells) == 3 and not all(set(c) <= {"-", ":"} for c in cells):
            ind[cells[0].lower()] = (cells[1], cells[2])

    sma_50 = _f(ind.get("sma(50)", (None, ""))[0])
    sma_200 = _f(ind.get("sma(200)", (None, ""))[0])
    rsi_14 = _f(ind.get("rsi(14)", (None, ""))[0])
    atr_14 = _f(ind.get("atr(14)", (None, ""))[0])

    macd = None
    if "macd(12,26,9)" in ind:
        mv, mc = ind["macd(12,26,9)"]
        mm = re.search(r"line\s*([-+]?[\d.]+),\s*signal\s*([-+]?[\d.]+),\s*hist\s*([-+]?[\d.]+)", mv)
        if mm:
            h5 = re.search(r"5d ago\s*\(([-+]?[\d.]+)\)", mc)
            macd = {
                "line": _f(mm.group(1)),
                "signal": _f(mm.group(2)),
                "hist": _f(mm.group(3)),
                "hist_5d_ago": _f(h5.group(1)) if h5 else None,
            }

    bollinger = None
    for k, (bv, bc) in ind.items():
        if k.startswith("bollinger"):
            bbm = re.search(r"mid\s*([\d,.]+),\s*upper\s*([\d,.]+),\s*lower\s*([\d,.]+)", bv)
            if bbm:
                pos = re.search(r"at\s*([-+]?[\d.]+)%", bc)
                bollinger = {
                    "lower": _f(bbm.group(3)),
                    "mid": _f(bbm.group(1)),
                    "upper": _f(bbm.group(2)),
                    "position_pct": _f(pos.group(1)) if pos else None,
                }
            break

    vol_avg = vol_last = None
    if "volume avg(20d)" in ind:
        vv, vc = ind["volume avg(20d)"]
        vol_avg = _i(vv)
        lm = re.search(r"last session\s*([\d,]+)", vc)
        vol_last = _i(lm.group(1)) if lm else None

    # Last 10 sessions OHLCV
    ohlcv = []
    for ln in prices_text.splitlines():
        om = re.match(
            r"^\|\s*(\d{4}-\d{2}-\d{2})\s*\|\s*([\d,.]+)\s*\|\s*([\d,.]+)\s*\|\s*([\d,.]+)\s*\|\s*([\d,.]+)\s*\|\s*([\d,]+)\s*\|$",
            ln.strip(),
        )
        if om:
            ohlcv.append({
                "date": om.group(1),
                "open": _f(om.group(2)),
                "high": _f(om.group(3)),
                "low": _f(om.group(4)),
                "close": _f(om.group(5)),
                "volume": _i(om.group(6)),
            })

    # Support / Resistance from technical.md
    tech_kv = _parse_kv_table(tech_text)
    support = tech_kv.get("support (nearest)", "")
    resistance = tech_kv.get("resistance (nearest)", "")

    # Market regime from data/prices.md
    kv_prices = _parse_kv_table(prices_text)
    spy_regime_raw = kv_prices.get("spy vs 200d sma", "")
    spy_sma_200 = None
    spy_regime = ""
    spy_price_regime = round(float(spy_now), 2)
    sm = re.search(r"([\d,.]+)\s+vs\s+([\d,.]+)\s*\(([^)]+)\)", spy_regime_raw)
    if sm:
        spy_price_regime = _f(sm.group(1)) or spy_price_regime
        spy_sma_200 = _f(sm.group(2))
        pos = sm.group(3).strip().lower()
        spy_regime = "Above 200d SMA" if "above" in pos else ("Below 200d SMA" if "below" in pos else sm.group(3).strip())

    technicals = {
        "bias": bias,
        "price_change_1d": ret_1d,
        "price_change_5d": ret_5d,
        "price_change_1m": ret_1m,
        "price_change_ytd": ret_ytd,
        "range_52w": range_52w,
        "rsi_14": rsi_14,
        "macd": macd,
        "bollinger": bollinger,
        "moving_averages": {"sma_50": sma_50, "sma_200": sma_200} if (sma_50 is not None or sma_200 is not None) else None,
        "atr_14": atr_14,
        "volume_last": vol_last,
        "volume_20d_avg": vol_avg,
        "support": support,
        "resistance": resistance,
        "ohlcv": ohlcv,
    }
    market_regime = {
        "spy_price": spy_price_regime,
        "spy_sma_200": spy_sma_200,
        "spy_regime": spy_regime,
        "vix": _f(kv_prices.get("vix (^vix)")),
        "us_10y_yield": _f(kv_prices.get("10y treasury yield (^tnx)")),
    }
    return technicals, market_regime


def _parse_news_and_sentiment(run_dir: Path) -> dict:
    ns_text = _read_opt(run_dir / "news-sentiment.md")
    data_news = _read_opt(run_dir / "data" / "news.md")

    bm = re.search(r"Overall\s+band:?\**\s*([^\n*(]+)", ns_text, re.IGNORECASE)
    overall_band = _strip_md(bm.group(1)) if bm else ""

    sm = re.search(r"Overall\s+score:?\**\s*([-+]?\d+(?:\.\d+)?)", ns_text, re.IGNORECASE)
    overall_score = _f(sm.group(1)) if sm else None

    cm = re.search(r"\bConfidence:?\**\s*([A-Za-z]+)", ns_text, re.IGNORECASE)
    confidence = cm.group(1).strip() if cm else ""

    catalysts = []
    # Parse 4-col table: | Date | Headline | Source | Impact/Summary |
    for ln in ns_text.splitlines():
        ln = ln.strip()
        if not ln.startswith("|") or not ln.endswith("|"):
            continue
        cells = [c.strip() for c in ln.strip("|").split("|")]
        if len(cells) >= 4 and re.match(r"\d{4}-\d{2}-\d{2}", cells[0]):
            impact_cell = _strip_md(cells[3])
            im = re.match(r"^([^:]+):\s*(.+)$", impact_cell)
            if im:
                impact, summary = im.group(1).strip(), im.group(2).strip()
            else:
                impact, summary = "Neutral", impact_cell
            catalysts.append({
                "date": cells[0],
                "headline": _strip_md(cells[1]),
                "impact": impact,
                "summary": summary,
            })

    # Fallback to data/news.md top headlines if analyst table wasn't present
    if not catalysts and data_news:
        lines = data_news.splitlines()
        for i, ln in enumerate(lines):
            hm = re.match(r"^-\s+\*\*(.+?)\*\*\s+[—\-]\s+([^,]+),\s*(\d{4}-\d{2}-\d{2})", ln.strip())
            if hm:
                summary = ""
                if i + 1 < len(lines) and lines[i + 1].strip().startswith("- "):
                    nxt = lines[i + 1].strip()[2:].strip()
                    if not nxt.startswith("http"):
                        summary = nxt
                catalysts.append({
                    "date": hm.group(3),
                    "headline": hm.group(1).strip(),
                    "impact": "Neutral",
                    "summary": summary,
                })
                if len(catalysts) >= 6:
                    break

    return {
        "overall_band": overall_band,
        "overall_score": overall_score,
        "confidence": confidence,
        "catalysts": catalysts,
    }


def _parse_debate_side(path: Path) -> dict:
    text = _read_opt(path)
    if not text:
        return {"summary": "", "strongest_point": ""}

    sp_m = re.search(r"^\s*\**STRONGEST\s+POINT:?\**\s*(.+)$", text, re.MULTILINE | re.IGNORECASE)
    strongest = _strip_md(sp_m.group(1)) if sp_m else ""

    # Prefer first paragraph of Conclusion or Executive Summary / Core Thesis
    summary = ""
    for heading in (r"##\s+[\d.]*\s*Conclusion", r"##\s+[\d.]*\s*(?:Executive\s+Summary|The\s+Core\s+Thesis)"):
        hm = re.search(heading + r"[^\n]*\n+([^\n#*-][^\n]+)", text, re.IGNORECASE)
        if hm:
            summary = _strip_md(hm.group(1))
            break
    if not summary:
        summary = strongest
    return {"summary": summary, "strongest_point": strongest}


def _parse_risk_debate(run_dir: Path, decision_text: str) -> dict:
    # Determine which stance the PM picked (first match in the top half of decision.md)
    chosen_stance = None
    top = "\n".join(decision_text.splitlines()[:12]).lower()
    for stance in ("neutral", "conservative", "aggressive"):
        if re.search(rf"\b{stance}\b[^.\n]*(?:most\s+convincing|chosen|selected|adopted|recommendation)", top):
            chosen_stance = stance
            break

    out = {}
    for stance in ("aggressive", "conservative", "neutral"):
        text = _read_opt(run_dir / f"risk-{stance}.md")
        vm = re.search(r"^\s*\**VERDICT:?\**\s*(.+)$", text, re.MULTILINE | re.IGNORECASE)
        verdict = notes = ""
        if vm:
            raw = _strip_md(vm.group(1))
            parts = re.split(r"\s+[—\-–]\s+", raw, maxsplit=1)
            verdict = parts[0].strip().upper()
            notes = parts[1].strip() if len(parts) > 1 else ""
        entry = {"verdict": verdict, "notes": notes}
        if chosen_stance == stance:
            entry["chosen"] = True
        out[stance] = entry
    return out


def _load_translations(run_dir: Path) -> dict | None:
    """Load optional bilingual overlay from work/<run>/translations.json."""
    p = run_dir / "translations.json"
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (json.JSONDecodeError, OSError):
        return None


def build_run_json(
    ticker: str,
    run_dir: Path,
    base: Path,
    price_usd: float,
    spy_now: float,
    d: dict,
    decision_text: str,
    date_s: str | None = None,
    lang: str = "en",
) -> dict:
    """Assemble the structured JSON payload for reports/YYYY-MM-DD-TICKER.json."""
    ticker = ticker.upper()
    date_s = date_s or today()
    company_name, fundamentals = _parse_fundamentals(run_dir)
    technicals, market_regime = _parse_technicals_and_regime(run_dir, ticker, spy_now)

    # Best-effort exchange extraction (e.g. "NASDAQ: NVDA")
    exchange = ""
    for fname in ("fundamentals.md", "bull.md"):
        em = re.search(rf"\b(NASDAQ|NYSE|AMEX|HKEX|SGX|TSE|LSE)\s*:\s*{re.escape(ticker)}\b", _read_opt(run_dir / fname))
        if em:
            exchange = em.group(1)
            break

    run_depth = "lite" if re.search(r"lite\s+depth", decision_text, re.IGNORECASE) else "full"

    payload = {
        "ticker": ticker,
        "company_name": company_name or ticker,
        "exchange": exchange,
        "date": date_s,
        "language": lang,
        "run_depth": run_depth,
        "price": round(float(price_usd), 2),
        "spy_price": round(float(spy_now), 2),
        "decision": {
            "call": d["call"],
            "confidence": d["conf"],
            "rationale": d["rationale"],
            "size_pct": _f(d["size_pct"]),
            "size_usd": None if str(d["size_usd"]).lower() == "n/a" else _f(d["size_usd"]),
            "stop": _f(d["stop"]),
            "target": _f(d["target"]),
            "execution_mandates": _extract_execution_mandates(decision_text),
        },
        "context": _parse_context(run_dir, base, ticker),
        "fundamentals": fundamentals,
        "technicals": technicals,
        "market_regime": market_regime,
        "news_and_sentiment": _parse_news_and_sentiment(run_dir),
        "debate": {
            "bull": _parse_debate_side(run_dir / "bull.md"),
            "bear": _parse_debate_side(run_dir / "bear.md"),
        },
        "risk_debate": _parse_risk_debate(run_dir, decision_text),
    }
    translations = _load_translations(run_dir)
    if translations:
        payload["translations"] = translations
    return payload


def update_data_js(data_js_path: Path, run_data: dict) -> str:
    """Prepend or replace (same ticker+date) run_data in reports/data.js atomically."""
    runs: list[dict] = []
    if data_js_path.exists():
        raw = data_js_path.read_text(encoding="utf-8")
        m = re.search(r"window\.TRADING_AGENTS_RUNS\s*=\s*(\[.*\])\s*;?\s*$", raw, re.DOTALL)
        if m:
            try:
                parsed = json.loads(m.group(1))
                if isinstance(parsed, list):
                    runs = parsed
            except json.JSONDecodeError:
                runs = []

    key = (run_data.get("ticker"), run_data.get("date"))
    existed = any((r.get("ticker"), r.get("date")) == key for r in runs)
    runs = [r for r in runs if (r.get("ticker"), r.get("date")) != key]
    runs.insert(0, run_data)

    payload = DATA_JS_HEADER + json.dumps(runs, indent=2, ensure_ascii=False) + ";\n"
    tmp = data_js_path.with_suffix(".js.tmp")
    tmp.write_text(payload, encoding="utf-8")
    tmp.replace(data_js_path)
    return "replaced" if existed else "added"


def ensure_dashboard_html(report_dir: Path, template_path: Path | None = None, force: bool = False) -> bool:
    """Copy the skill's dashboard.html template into report_dir if not already present (or force=True)."""
    dest = report_dir / "dashboard.html"
    if dest.exists() and not force:
        return False
    src = template_path or (Path(__file__).resolve().parent.parent / "templates" / "dashboard.html")
    if not src.exists():
        return False
    shutil.copyfile(src, dest)
    return True


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
    p = run_dir / "context.md"
    if not p.exists():
        return None
    m = re.search(r"Total equity/ETF USD value:\s*\*\*\$([\d,]+\.?\d*)\*\*",
                  p.read_text(encoding="utf-8"))
    return float(m.group(1).replace(",", "")) if m else None


def _resolve_lang(base: Path, cli_lang: str | None) -> str:
    if cli_lang and cli_lang.lower() in VALID_LANGUAGES:
        return cli_lang.lower()
    p = base / "portfolio-profile.json"
    try:
        val = str(json.loads(p.read_text(encoding="utf-8")).get("language", "en")).strip().lower()
        return val if val in VALID_LANGUAGES else "en"
    except Exception:
        return "en"


def _render_markdown_report(
    ticker: str,
    date_s: str,
    price_usd: float,
    spy_now: float,
    d: dict,
    warns: list[str],
    run_dir: Path,
    lang: str = "en",
) -> tuple[str, list[str]]:
    is_zh = lang == "zh"
    call, rationale, conf = d["call"], d["rationale"], d["conf"]
    if is_zh:
        parts = [
            f"# TradingAgents 投研报告 — {ticker} — {date_s}",
            "",
            DISCLAIMER_ZH,
            "",
            f"数据截止: {now_utc()}。报告时股价: {price_usd:.2f} USD；SPY {spy_now:.2f}。",
            "",
            "## 决策摘要",
            "",
            f"**{call}** (置信度: {conf}) — {rationale}",
            "",
            f"仓位规模: {d['size_usd']} USD / {d['size_pct']}% · 止损: {d['stop']} · 目标价: {d['target']}",
            "",
        ]
        if warns:
            parts += ["## 仓位校验提示", ""]
            parts += [f"- WARNING: {w}" for w in warns]
            parts += [""]
        sections = SECTIONS_ZH
        missing_tpl = "_缺失阶段文件已跳过: {files}_"
        sources_title = "## 数据来源"
        sources_body = (
            "数据主要来自 Yahoo Finance (yfinance 数据包 `data/` 目录)，结合各分析师章节引用的定向网页搜索。"
            "仅在明确标注时使用 Alpha Vantage MCP。"
        )
    else:
        parts = [
            f"# TradingAgents Report — {ticker} — {date_s}",
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
        sections = SECTIONS
        missing_tpl = "_Missing stage files skipped: {files}_"
        sources_title = "## Sources"
        sources_body = (
            "Yahoo Finance via yfinance data pack (`data/` in the run dir), plus any targeted web searches "
            "cited inside the analyst sections. Alpha Vantage MCP used only where noted."
        )

    missing = []
    for fname, title in sections:
        p = run_dir / fname
        if p.exists():
            parts += [f"## {title}", "", p.read_text(encoding="utf-8").strip(), "", "---", ""]
        else:
            missing.append(fname)
    if missing:
        parts += [missing_tpl.format(files=", ".join(missing)), ""]
    parts += [sources_title, "", sources_body, ""]
    return "\n".join(parts), missing


def _max_pos_pct(base):
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
    ap.add_argument("--lang", choices=VALID_LANGUAGES, help="report language: en, zh, or both (defaults to portfolio-profile.json)")
    ap.add_argument("--refresh-dashboard", action="store_true", help="overwrite existing reports/dashboard.html with latest template")
    args = ap.parse_args()

    ticker = args.ticker.upper()
    run_dir = Path(args.run_dir)
    base = Path(args.base_dir).resolve()
    lang = _resolve_lang(base, args.lang)

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

    date_s = today()

    # report
    report_dir = base / "reports"
    report_dir.mkdir(exist_ok=True)
    report_path = report_dir / f"{date_s}-{ticker}.md"
    primary_md_lang = "zh" if lang == "zh" else "en"
    report_text, missing = _render_markdown_report(
        ticker, date_s, price_usd, spy_now, d, warns, run_dir, lang=primary_md_lang
    )
    report_path.write_text(report_text, encoding="utf-8")

    # optional bilingual Chinese markdown report when lang == "both"
    zh_report_path = None
    if lang == "both":
        tr = _load_translations(run_dir) or {}
        if isinstance(tr.get("report_zh"), str) and tr["report_zh"].strip():
            zh_report_path = report_dir / f"{date_s}-{ticker}.zh.md"
            zh_report_path.write_text(tr["report_zh"].strip() + "\n", encoding="utf-8")

    # ledger append
    ledger_path = report_dir / "ledger.md"
    if not ledger_path.exists():
        die("reports/ledger.md not found — stage0.py should have created it")
    row = (f"| {date_s} | {ticker} | {call} | {conf} | {price_usd:.2f} | {spy_now:.2f} "
           f"| {rationale} | UNRESOLVED | UNRESOLVED |")
    action = upsert_ledger_row(ledger_path, row, date_s, ticker)

    # structured JSON report + offline dashboard bundle
    run_json = build_run_json(ticker, run_dir, base, price_usd, spy_now, d, decision_text, date_s=date_s, lang=lang)
    json_path = report_dir / f"{date_s}-{ticker}.json"
    tmp_json = json_path.with_suffix(".json.tmp")
    tmp_json.write_text(json.dumps(run_json, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp_json.replace(json_path)

    data_js_action = update_data_js(report_dir / "data.js", run_json)
    copied_dashboard = ensure_dashboard_html(report_dir, force=args.refresh_dashboard)

    print(f"OK: report {report_path}")
    if zh_report_path:
        print(f"    report_zh {zh_report_path}")
    print(f"    json {json_path} (data.js {data_js_action}, lang={lang})")
    if copied_dashboard:
        print(f"    dashboard {report_dir / 'dashboard.html'} (installed from template)")
    print(f"    ledger row {action}: {row}")
    print(f"DECISION: {call} — {rationale}")
    if missing:
        print(f"    WARNING: missing stage files: {', '.join(missing)}")
    for w in warns:
        print(f"    WARNING: {w}")


if __name__ == "__main__":
    main()
