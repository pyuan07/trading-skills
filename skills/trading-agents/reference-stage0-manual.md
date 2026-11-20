# Stage 0 — manual fallback procedure

Use ONLY when `stage0.py` has failed twice. This is the orchestrator-executed procedure the
script replaced; it must produce the same `context.md` and ledger format as the script.

1. **Find holdings files** in the working directory:
   - Broker-neutral: `holdings.csv` and `holdings-*.csv` (English headers, see `examples/holdings.example.csv`; required `ticker`, `qty`; optional `name, price, cost, market_value, pl_pct, currency, account, margin`; `market_value` defaults to `qty × price`, `currency` defaults to USD).
   - moomoo/Futu export: glob `持仓-*.csv`, parse the `YYYYMMDD-HHMMSS` timestamp in each filename, keep the newest per account number (the digits in parentheses). UTF-8 with BOM; headers differ per account — margin uses `摊薄成本价`, cash uses `平均成本价`; read the header row, do not guess columns.

   If the newest file is >7 days old, warn the user but continue. If none exist, warn and run ticker-only (skip position-aware parts of Stages 4–5).
2. **Detect option rows:** a symbol matching an OCC-style pattern (underlying + 6-digit expiry `YYMMDD` + `C`/`P` + strike, e.g. `TSM260918P360000`) is a listed option, not an equity — do not run the equity pipeline against it. Treat option positions as risk/exposure context for their underlying.
3. Read `portfolio-profile.json`. If missing, tell the user to copy `examples/portfolio-profile.example.json` (next to this file) into the working directory and fill it in; continue with `risk_tolerance: moderate, max_position_pct: 20, margin_comfort: low` and note the assumption in the report.
4. **Normalize to USD:** for every non-USD position, fetch the FX rate from Yahoo Finance (`https://finance.yahoo.com/quote/<FROM>USD=X`, e.g. `SGDUSD=X`, `JPYUSD=X`). Compute each position's USD market value, total portfolio USD value, and each position's % of total. **SHORT positions:** negative quantity/market value stays negative — signed exposure feeds the concentration math in Stage 4's risk debate.
5. **Ledger reflection (30-day AND 180-day horizons — same rule as `stage0.py`):** read `reports/ledger.md` (create it with the header row below if absent). For every row matching this run's ticker (ALL tickers in portfolio-review mode), handle each of the `Status 30d` / `Status 180d` cells independently:
   - If the cell is not `UNRESOLVED`, leave it.
   - If call date + N days is still in the future, leave it `UNRESOLVED`.
   - Otherwise grade over exactly [call date → call date + N days]: from the Yahoo Finance history page take the first daily close on/after each endpoint for the ticker AND for SPY (adjusted closes, so splits/dividends don't count as moves; convert non-USD closes to USD with FX from those same dates — the recorded Price/SPY cells are display-only, do NOT grade against them). `rel` = ticker return − SPY return.
   - Band = ±3% at 30d, ±6% at 180d. BUY/ADD: WIN if `rel > band`, LOSS if `rel < −band`, else FLAT. TRIM/SELL: mirrored (WIN if the name lagged SPY by more than the band). HOLD: LOSS only if `rel < −band`, otherwise WIN.
   - Write the cell as `WIN (30d, <end close date>)` / `LOSS (180d, ...)` / `FLAT (...)`.
6. Write `work/<run>/context.md` with sections: `## Portfolio (USD)` (table: ticker, name, qty, currency, native mkt value, USD value, % of equity, cost, P/L %, account, margin flag), `## Sizing guardrails` (current % of book vs `max_position_pct` and the USD headroom), `## Options positions` (OCC symbol, underlying, signed qty, USD value, account, one-line tag; omit the section if no options), `## Profile` (the JSON, pretty-printed), `## Past calls for TICKER` (matching ledger rows incl. just-resolved outcomes, or "none"; omit entirely in portfolio-review mode), and `## Warnings`.

Ledger table header (create exactly):

```
| Date | Ticker | Call | Conf | Price (USD) | SPY | Rationale | Status 30d | Status 180d |
|---|---|---|---|---|---|---|---|---|
```
