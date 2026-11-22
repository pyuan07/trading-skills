"""Unit tests for finalize.py JSON export, data.js manifest updates, and dashboard template copy."""
from __future__ import annotations

import json
import re
from pathlib import Path

from finalize import build_run_json, ensure_dashboard_html, parse_decision, update_data_js


SAMPLE_DECISION = """# Portfolio manager decision — NVDA — 2026-11-22

_Lite depth: synthesized inline from the run-dir files only._

Final call: **BUY**

The **neutral risk analyst's** recommendation is the most convincing for this trade.

Execution guidelines:
- Accumulate in the $120.00 - $126.00 zone with 100% cash funding.
- Set a hard stop at 105.00.
- Target 180.00.

SIZE_USD: n/a
SIZE_PCT: 10.0
STOP: 105.00
TARGET: 180.00
Confidence: med

RATIONALE: Single-digit forward PE and cash generation outweigh governance discount
"""

SAMPLE_CONTEXT = """# Run Context — NVDA — 2026-11-22

## Portfolio (USD)

Total equity/ETF USD value: **$25,000.00**.

## Sizing guardrails

No portfolio total available - size NVDA by profile (max_position_pct 20.0%) only.

## Profile

```json
{
  "risk_tolerance": "moderate",
  "max_position_pct": 20,
  "margin_comfort": "low",
  "horizon": "6-18 months",
  "base_currency": "USD"
}
```
"""

SAMPLE_DATA_FUNDAMENTALS = """# Fundamentals — NVDA

**NVIDIA Corporation** — Technology / Semiconductors, currency USD

| Metric | Value |
|---|---|
| Market cap | 3,031.17B |
| P/E (trailing / forward) | 13.48 / 7.56 |
| PEG | 0.15 |
| P/S (ttm) | 8.38 |
| P/B | 11.35 |
| Profit margin | 62.5% |
| Operating margin | 66.1% |
| ROE | 117.4% |
| Debt/Equity | 14.31 |
| Free cash flow (ttm, from cash-flow statement: OCF − capex) | 127.01B (OCF 134.36B, capex -7.35B) |
| Revenue growth (yoy) | 103.2% |
| Earnings growth (yoy) | 107.7% |
| Dividend yield | 0.80% |
| Beta | 2.25 |
| Analyst target (mean) | 244.18 |
| Analyst recommendation | 1.81 (56 analysts) |
"""

SAMPLE_ANALYST_FUNDAMENTALS = """# Fundamental Analysis Report: NVIDIA Corporation (NVDA)

NVIDIA Corporation (NASDAQ: NVDA) achieved a Gross Margin of 74.8%.

## KEY POINTS:

- **Multiple Compression:** Trades at 7.56x forward P/E.
- **Cash Fortress:** $127.01B in TTM Free Cash Flow.
"""

SAMPLE_DATA_PRICES = """# Price & technicals — NVDA

**Last close:** 125.53  (prev 145.98, 1d -14.0%)
**52-week range:** 125.53 – 239.24  (last is -47.5% vs high)

## Returns

| | 1d | 5d | 1m (21td) | 3m (63td) | YTD |
|---|---|---|---|---|---|
| NVDA | -14.0% | -22.9% | -40.8% | -39.7% | -33.4% |
| SPY | -3.6% | -8.6% | -9.7% | -9.4% | -6.7% |

## Market regime

| Signal | Reading |
|---|---|
| SPY vs 200d SMA | 689.81 vs 730.29 (below) |
| VIX (^VIX) | 35.89 |
| 10Y Treasury yield (^TNX) | 4.91 |

## Indicators (computed from the daily series above)

| Indicator | Value | Context |
|---|---|---|
| SMA(50) | 207.00 | price -39.4% vs SMA50 |
| SMA(200) | 203.09 | price -38.2% vs SMA200 |
| RSI(14) | 15.0 | oversold <30 |
| MACD(12,26,9) | line -19.83, signal -15.20, hist -4.63 | hist expanding vs 5d ago (-4.12) |
| Bollinger(20,±2σ) | mid 181.46, upper 235.99, lower 126.93 | last close at -1% of band width |
| ATR(14) | 10.71 | 8.5% of price |
| Volume avg(20d) | 287,291,305 | last session 626,373,200 |

## Last 10 sessions (OHLCV)

| Date | Open | High | Low | Close | Volume |
|---|---|---|---|---|---|
| 2026-11-19 | 152.92 | 158.13 | 145.71 | 145.98 | 322,940,100 |
| 2026-11-20 | 143.52 | 143.73 | 124.50 | 125.53 | 626,373,200 |
"""

SAMPLE_TECHNICAL = """# Technical analysis — NVDA

**Overall bias: Neutral/mixed — signals conflict; wait for alignment.**

| Dimension | Reading |
|---|---|
| Support (nearest) | none below (at lows) |
| Resistance (nearest) | Bollinger lower 126.93; SMA200 203.09 |
"""

SAMPLE_NEWS_SENTIMENT = """# News & Sentiment Analysis — NVDA

### Key News Items & Trading Relevance

| Date | Headline / Event | Source | Trading Relevance & Impact |
|---|---|---|---|
| 2026-11-22 | Jensen Huang Resigns; SEC Launches Inquiry | Press | **High (Negative):** Governance shock and regulatory overhang. |
| 2026-11-22 | Older GPU Prices Rise | Yahoo | **Medium (Positive):** Strong physical datacenter demand. |

### 2. Sentiment Metrics

* **Overall band:** Mildly Bearish
* **Overall score:** 3.0 / 10 *(0 = Max Bearish)*
* **Confidence:** High *(Verifiable volume)*
"""

SAMPLE_BULL = """# Bullish Investment Case: NVDA

## 7. Conclusion

NVIDIA at $125.53 is a rare market anomaly trading at single-digit forward earnings.

STRONGEST POINT: Trades at 7.56x forward P/E with $127B FCF.
"""

SAMPLE_BEAR = """# Bearish Investment Case: NVDA

## 5. Conclusion: Do Not Catch This Knife

NVIDIA is undergoing a painful structural re-rating with peak margins rolling over.

STRONGEST POINT: Classic value trap with active SEC probe and margin compression.
"""

SAMPLE_RISK_AGG = "VERDICT: approve — full 20% risk budget.\n"
SAMPLE_RISK_CON = "VERDICT: shrink — cut size to 5%.\n"
SAMPLE_RISK_NEU = "VERDICT: approve — 10% position size balances risk.\n"


def _populate_run_dir(run_dir: Path) -> None:
    (run_dir / "data").mkdir(parents=True, exist_ok=True)
    (run_dir / "decision.md").write_text(SAMPLE_DECISION, encoding="utf-8")
    (run_dir / "context.md").write_text(SAMPLE_CONTEXT, encoding="utf-8")
    (run_dir / "data" / "fundamentals.md").write_text(SAMPLE_DATA_FUNDAMENTALS, encoding="utf-8")
    (run_dir / "fundamentals.md").write_text(SAMPLE_ANALYST_FUNDAMENTALS, encoding="utf-8")
    (run_dir / "data" / "prices.md").write_text(SAMPLE_DATA_PRICES, encoding="utf-8")
    (run_dir / "technical.md").write_text(SAMPLE_TECHNICAL, encoding="utf-8")
    (run_dir / "news-sentiment.md").write_text(SAMPLE_NEWS_SENTIMENT, encoding="utf-8")
    (run_dir / "bull.md").write_text(SAMPLE_BULL, encoding="utf-8")
    (run_dir / "bear.md").write_text(SAMPLE_BEAR, encoding="utf-8")
    (run_dir / "risk-aggressive.md").write_text(SAMPLE_RISK_AGG, encoding="utf-8")
    (run_dir / "risk-conservative.md").write_text(SAMPLE_RISK_CON, encoding="utf-8")
    (run_dir / "risk-neutral.md").write_text(SAMPLE_RISK_NEU, encoding="utf-8")


class TestBuildRunJson:
    def test_mandatory_keys_and_numeric_conversions(self, tmp_path):
        run_dir = tmp_path / "work" / "2026-11-22-NVDA"
        _populate_run_dir(run_dir)
        d = parse_decision(SAMPLE_DECISION)

        data = build_run_json(
            "nvda", run_dir, tmp_path, 125.53, 689.81, d, SAMPLE_DECISION, date_s="2026-11-22"
        )

        # Serializable and contains all required top-level keys
        serialized = json.dumps(data)
        parsed = json.loads(serialized)
        required = {
            "ticker", "company_name", "exchange", "date", "run_depth", "price", "spy_price",
            "decision", "context", "fundamentals", "technicals", "market_regime",
            "news_and_sentiment", "debate", "risk_debate",
        }
        assert required.issubset(parsed.keys())
        assert parsed["ticker"] == "NVDA"
        assert parsed["company_name"] == "NVIDIA Corporation"
        assert parsed["exchange"] == "NASDAQ"
        assert parsed["run_depth"] == "lite"
        assert parsed["price"] == 125.53
        assert parsed["spy_price"] == 689.81

        # Decision numeric fields
        dec = parsed["decision"]
        assert dec["call"] == "BUY"
        assert dec["size_pct"] == 10.0
        assert dec["size_usd"] is None
        assert dec["stop"] == 105.0
        assert dec["target"] == 180.0
        assert len(dec["execution_mandates"]) == 3

        # Fundamentals numeric fields
        f = parsed["fundamentals"]
        assert f["market_cap"] == "3,031.17B"
        assert f["pe_trailing"] == 13.48
        assert f["pe_forward"] == 7.56
        assert f["peg"] == 0.15
        assert f["gross_margin"] == 74.8
        assert f["operating_margin"] == 66.1
        assert f["net_margin"] == 62.5
        assert f["roe"] == 117.4
        assert f["debt_to_equity"] == 14.31
        assert f["fcf_ttm"] == "127.01B"
        assert f["ocf_ttm"] == "134.36B"
        assert f["capex_ttm"] == "-7.35B"
        assert f["revenue_growth_yoy"] == 103.2
        assert f["earnings_growth_yoy"] == 107.7
        assert f["analyst_target_mean"] == 244.18
        assert f["analyst_recommendation"] == 1.81
        assert len(f["key_points"]) == 2

        # Technicals & OHLCV
        t = parsed["technicals"]
        assert "Neutral/mixed" in t["bias"]
        assert t["price_change_1d"] == -14.0
        assert t["price_change_ytd"] == -33.4
        assert t["range_52w"] == {"low": 125.53, "high": 239.24, "pct_from_high": -47.5}
        assert t["rsi_14"] == 15.0
        assert t["macd"] == {"line": -19.83, "signal": -15.20, "hist": -4.63, "hist_5d_ago": -4.12}
        assert t["bollinger"] == {"lower": 126.93, "mid": 181.46, "upper": 235.99, "position_pct": -1.0}
        assert t["moving_averages"] == {"sma_50": 207.0, "sma_200": 203.09}
        assert t["atr_14"] == 10.71
        assert t["volume_last"] == 626373200
        assert t["volume_20d_avg"] == 287291305
        assert len(t["ohlcv"]) == 2
        assert t["ohlcv"][-1]["close"] == 125.53

        # Market regime
        mr = parsed["market_regime"]
        assert mr["spy_price"] == 689.81
        assert mr["spy_sma_200"] == 730.29
        assert mr["spy_regime"] == "Below 200d SMA"
        assert mr["vix"] == 35.89
        assert mr["us_10y_yield"] == 4.91

        # News & sentiment
        ns = parsed["news_and_sentiment"]
        assert ns["overall_band"] == "Mildly Bearish"
        assert ns["overall_score"] == 3.0
        assert ns["confidence"] == "High"
        assert len(ns["catalysts"]) == 2
        assert ns["catalysts"][0]["impact"] == "High (Negative)"

        # Debate & Risk debate
        assert parsed["debate"]["bull"]["strongest_point"].startswith("Trades at 7.56x")
        assert parsed["debate"]["bear"]["strongest_point"].startswith("Classic value trap")
        assert parsed["risk_debate"]["aggressive"]["verdict"] == "APPROVE"
        assert parsed["risk_debate"]["conservative"]["verdict"] == "SHRINK"
        assert parsed["risk_debate"]["neutral"]["chosen"] is True

    def test_handles_minimal_run_dir_gracefully(self, tmp_path):
        run_dir = tmp_path / "work" / "2026-11-22-AAPL"
        run_dir.mkdir(parents=True)
        (run_dir / "decision.md").write_text(SAMPLE_DECISION, encoding="utf-8")
        d = parse_decision(SAMPLE_DECISION)

        data = build_run_json(
            "AAPL", run_dir, tmp_path, 230.0, 690.0, d, SAMPLE_DECISION, date_s="2026-11-22"
        )
        assert data["ticker"] == "AAPL"
        assert data["company_name"] == "AAPL"
        assert data["fundamentals"]["pe_forward"] is None
        assert data["technicals"]["ohlcv"] == []
        assert data["news_and_sentiment"]["catalysts"] == []


class TestDataJsAndDashboard:
    def _extract_runs(self, data_js: Path) -> list[dict]:
        raw = data_js.read_text(encoding="utf-8")
        m = re.search(r"window\.TRADING_AGENTS_RUNS\s*=\s*(\[.*\]);\s*$", raw, re.DOTALL)
        assert m, "data.js missing window.TRADING_AGENTS_RUNS assignment"
        return json.loads(m.group(1))

    def test_update_data_js_is_idempotent_on_same_ticker_and_date(self, tmp_path):
        data_js = tmp_path / "data.js"
        run1 = {"ticker": "NVDA", "date": "2026-11-22", "price": 125.53, "decision": {"call": "BUY"}}
        run2 = {"ticker": "MSFT", "date": "2026-11-20", "price": 498.30, "decision": {"call": "TRIM"}}
        run1_updated = {"ticker": "NVDA", "date": "2026-11-22", "price": 126.00, "decision": {"call": "HOLD"}}

        assert update_data_js(data_js, run1) == "added"
        assert update_data_js(data_js, run2) == "added"
        runs = self._extract_runs(data_js)
        assert [r["ticker"] for r in runs] == ["MSFT", "NVDA"]

        # Same-day re-run replaces instead of duplicating and moves latest to front
        assert update_data_js(data_js, run1_updated) == "replaced"
        runs = self._extract_runs(data_js)
        assert len(runs) == 2
        assert runs[0]["ticker"] == "NVDA"
        assert runs[0]["price"] == 126.00
        assert runs[0]["decision"]["call"] == "HOLD"

    def test_ensure_dashboard_html_copies_once_and_preserves_existing(self, tmp_path):
        report_dir = tmp_path / "reports"
        report_dir.mkdir()

        # First call copies from the skill's templates/dashboard.html
        assert ensure_dashboard_html(report_dir) is True
        dash = report_dir / "dashboard.html"
        assert dash.exists()
        assert "TRADING_AGENTS_RUNS" in dash.read_text(encoding="utf-8")

        # Subsequent call does not overwrite user edits
        dash.write_text("<!-- custom -->", encoding="utf-8")
        assert ensure_dashboard_html(report_dir) is False
        assert dash.read_text(encoding="utf-8") == "<!-- custom -->"
