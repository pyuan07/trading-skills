---
name: trading-agents
description: Multi-agent stock analysis using the TradingAgents methodology (analysts → bull/bear debate → trader → risk debate → portfolio manager), portfolio-aware (holdings CSV, or live positions via the moomoo MCP when connected). Use when the user asks to analyze a ticker, get a buy/hold/sell view, or review their portfolio. Triggers: "analyze NVDA", "should I buy/sell X", "review my portfolio", "trading agents".
---

# TradingAgents Pipeline

> ⚠️ Decision support, not financial advice. Verify all data before trading.

Two modes:
- **Single ticker** (user names a symbol): run Stages 0–5 below.
- **Portfolio review** (user asks about their whole portfolio): see "Portfolio review mode" — screen first, then run Stages 0–5 only on names the USER selects.

## Run depth: lite (default) vs full

Orthogonal to the modes above; applies per ticker. State which depth ran in the final summary to the user.

- **lite (DEFAULT):** Stages 0–2 run exactly as written (scripts + real subagents for the 2 analysts and bull/bear). Stages 3–5 are synthesized INLINE by the orchestrator: for each of trader, the three risk stances, and the portfolio manager, read that agent's prompt file and write its output file yourself — condensed (a few focused paragraphs each) but with the same filenames and the same output contracts. `decision.md` must still satisfy the Stage 5 contract (single-call `Final call:` line, the `SIZE_USD:`/`SIZE_PCT:`/`STOP:`/`TARGET:` lines, `Confidence:` line, and `RATIONALE:` line) — `finalize.py` parses it strictly and fails otherwise. The portfolio-manager firewall still applies: base Stages 3–5 only on the run-dir files, no new web content.
- **full:** every stage dispatches real subagents exactly as written below. Use ONLY when the user explicitly asks for a full/deep run (e.g. "full pipeline", "deep dive").

All agent prompt files live in `agents/` next to this file. Dispatch each as a **general-purpose subagent** whose prompt is: the agent file's full contents, then the run-specific inputs listed for that stage. **Model routing:** Stage 1 analysts are extraction/summarization work — dispatch them with `model: "sonnet"`. Stage 2+ subagents (bull, bear, and in full depth trader/risk/PM) are the reasoning core — dispatch them WITHOUT a model override so they inherit the default (large) model. (If your agent harness has no per-subagent model override, ignore the routing and use the default.) Subagents write their output file themselves; verify each expected file exists before starting the next stage. On failure, re-dispatch only the missing one — but only once: if that retry also fails to produce the expected file, stop and report the failure to the user rather than retrying indefinitely.

Work dir for a run: `work/YYYY-MM-DD-TICKER/` (create at start). Report: `reports/YYYY-MM-DD-TICKER.md`. Ledger: `reports/ledger.md`.

## Data-layer scripts (run with uv or the skill's venv, NEVER bare global python)

Deterministic work (CSV/FX/ledger math, data fetching, report assembly) is done by Python scripts in `scripts/` next to this file.

- `SKILL_DIR` = the absolute path of the directory containing this SKILL.md.
- Run every script from the user's **current working directory** (where `holdings.csv`, `portfolio-profile.json`, `work/` and `reports/` live).
- **Runner `<RUN>`**, first match wins:
  1. `SKILL_DIR/.venv/Scripts/python.exe` (Windows) or `SKILL_DIR/.venv/bin/python` (macOS/Linux) if it exists;
  2. otherwise `uv run` if `uv` is on PATH — each script declares its own dependencies inline, so uv needs no setup;
  3. otherwise STOP and give the user a setup option below.

```
<RUN> <SKILL_DIR>/scripts/<script>.py ...
```

Setup options (one-time) — easiest is installing uv (https://docs.astral.sh/uv/); or create the venv — do not substitute global Python:

```
# macOS / Linux
python3 -m venv <SKILL_DIR>/.venv && <SKILL_DIR>/.venv/bin/python -m pip install -r <SKILL_DIR>/scripts/requirements.txt
# Windows
python -m venv <SKILL_DIR>/.venv && <SKILL_DIR>/.venv/Scripts/python.exe -m pip install -r <SKILL_DIR>/scripts/requirements.txt
```

Script failure rule: a script exits non-zero with a one-line `ERROR:` reason. Retry it ONCE; if it fails again, fall back to the manual/WebFetch path described in that stage (and each analyst prompt's fallback section), tell the user which path was used, and continue.

## Optional: moomoo MCP (use only if the moomoo MCP tools are available in this session)

If moomoo MCP tools (e.g. `account_positions`, `quote_research_analyst_consensus`) are connected, use them for live positions and extra research data. They are **read-only here: NEVER call any order, trade, cancel or modify tool from this skill.** If they are not connected, or a call fails / lacks permission, skip this section silently and use the CSV + Yahoo path.

**Positions (before Stage 0, once per session):**
1. Call `account_authorized_trd_accs`. Keep accounts whose `enable_market` includes a securities market (1 HK, 2 US, 4 CN, 6 SG, 8 AU, 11 MY, 12 CA, 15 JP, 18 KR); skip futures-only (5) and FX-only (9) accounts.
2. For each kept account call `account_positions` with its `account_id`, and save each raw JSON response unchanged to `work/<run>/moomoo/positions-<n>.json`.
3. Run `<RUN> <SKILL_DIR>/scripts/moomoo_data.py positions work/<run>/moomoo/positions-*.json --acc-type <acc_type per file, in order> --out holdings-moomoo.csv`. Stage 0 then reads it like any holdings file (and ignores any `持仓-*.csv` export so nothing is counted twice).

**Research data (Stage 1 pre-step, per ticker, after `fetch_data.py`):** the moomoo symbol is `US.NVDA`, `HK.00700`, `SG.ES3`, etc. Call `quote_research_analyst_consensus` and `quote_short_interest` (`count: 6`; HK/US only), optionally `quote_unusual_capital` (`language_id: 2`), save each raw response to `work/<run>/moomoo/`, then run `<RUN> <SKILL_DIR>/scripts/moomoo_data.py insights <TICKER> --run-dir work/<run> --consensus <file> --short-interest <file> [--unusual <file>]` → writes `work/<run>/data/moomoo.md`, which the fundamentals and news analysts read. Tell both analysts whether it exists.

## Stage 0 — Context load (orchestrator, one script call)

Run `stage0.py --ticker <TICKER> --base-dir . --run-dir work/<run>` (or `--portfolio-review` instead of `--ticker` in portfolio-review mode). It does all of the following deterministically: loads holdings from `holdings.csv` / `holdings-*.csv` (broker-neutral format, see `examples/holdings.example.csv` next to this file) and/or the newest moomoo/Futu `持仓-*.csv` export per account (warns if >7 days old; with no holdings file at all it runs ticker-only), option-row detection (OCC pattern — options are risk/exposure context, never run through the equity pipeline), shorts kept as negative exposure, `portfolio-profile.json` (defaults + warning if missing), FX + USD normalization via Yahoo, ledger reflection (grades UNRESOLVED cells for this ticker — or ALL rows in portfolio-review — independently at each of the 30-day and 180-day horizons once each matures, scoring the call's return vs SPY over exactly [call date → +Nd], adjusted close-to-close so splits/dividends don't count as moves, as WIN/LOSS/FLAT per horizon, and rewrites `reports/ledger.md`; cells short of their horizon stay UNRESOLVED), and writes `work/<run>/context.md`. `context.md` now also carries a `## Concentration` block (sector weights + this ticker's correlation to existing holdings), a `## Sizing guardrails` block, and enriched options columns (notional, DTE, moneyness) in `## Options positions`.

Verify `context.md` exists and skim its `## Warnings` section; relay any warnings to the user. **Manual fallback** (script failed twice): follow the step-by-step Stage 0 procedure in `reference-stage0-manual.md` next to this file.

Ledger table header (for reference):
`| Date | Ticker | Call | Conf | Price (USD) | SPY | Rationale | Status 30d | Status 180d |`

## Stage 1 — Data + analysts (1 script + 2 subagents in parallel)

**Pre-step (orchestrator):** run `fetch_data.py <TICKER> --run-dir work/<run>` — writes the data pack `work/<run>/data/prices.md` (quote, returns, computed indicators, last 10 OHLCV), `data/fundamentals.md`, `data/news.md`, **and `work/<run>/technical.md` itself** (a deterministic rule-based technical read — no technical-analyst subagent runs in the normal path). The data pack now also includes SPY-relative returns and a market-regime block, so agents cite those instead of estimating relative performance.

Dispatch `agents/fundamentals-analyst.md` and `agents/news-sentiment-analyst.md` in parallel, each with `model: "sonnet"`. Run-specific input for each: ticker, today's date, path to `work/<run>/`, whether the data pack at `work/<run>/data/` exists (or "missing/failed — use your fallback path"), and the instruction to write its output to this exact file — the mapping is fixed and must not be improvised:

- `agents/fundamentals-analyst.md` → `work/<run>/fundamentals.md`
- `agents/news-sentiment-analyst.md` → `work/<run>/news-sentiment.md`
- `work/<run>/technical.md` ← written by `fetch_data.py` (script, not an agent)

These three filenames are the interface contract that Stage 2 ("the 3 analyst files") and Stage 5 depend on.

**If `fetch_data.py` fails twice:** no data pack and no technical.md exist — tell both dispatched analysts "the data pack is missing/failed — use your fallback path" AND additionally dispatch `agents/technical-analyst.md` (fallback-only prompt) → `work/<run>/technical.md`, also with `model: "sonnet"`.

## Stage 2 — Research debate (2 subagents, SEQUENTIAL, 1 round)

1. `agents/bull-researcher.md` — inputs: the 3 analyst files + `context.md`. Writes `bull.md`.
2. `agents/bear-researcher.md` — inputs: the 3 analyst files + `context.md` + `bull.md` (it rebuts the bull). Writes `bear.md`.

## Stage 3 — Trader (1 subagent in full depth; inline in lite)

`agents/trader.md` — inputs: all Stage 1–2 files + `context.md`. Writes `trader-plan.md`.

## Stage 4 — Risk debate (3 subagents, parallel, 1 round — full depth; inline in lite)

Dispatch `agents/risk-aggressive.md`, `agents/risk-conservative.md`, `agents/risk-neutral.md`. Inputs for each: `trader-plan.md` + `context.md` (the real USD portfolio: concentration vs max_position_pct, margin vs margin_comfort). Each writes `risk-<stance>.md`.

## Stage 5 — Portfolio manager (1 subagent in full depth; inline in lite) + wrap-up

`agents/portfolio-manager.md` — inputs: ONLY the files in `work/<run>/` (never raw web content). Writes `decision.md`, which MUST contain: a `Final call:` line naming exactly ONE of **BUY / ADD / HOLD / TRIM / SELL** (never listing alternatives), the sizing/exit contract lines `SIZE_USD:`, `SIZE_PCT:`, `STOP:`, `TARGET:` (respecting `max_position_pct` from the profile), a `Confidence: low|med|high` line, and a ≤10-word rationale line formatted `RATIONALE: <words>` with no `|` characters. `finalize.py` validates this contract and exits with `ERROR:` if violated — on that error, re-dispatch (or in lite, rewrite) `decision.md` once, then re-run `finalize.py`.

Then the orchestrator runs `finalize.py <TICKER> --run-dir work/<run> --base-dir .` — it assembles `reports/YYYY-MM-DD-TICKER.md` (disclaimer header, data-as-of, decision summary, each stage file as a section, sources note), fetches the current USD price + SPY via Yahoo, and appends the ledger row from `decision.md`'s call and RATIONALE line. It prints `DECISION: ...` — show the user that decision summary and the report path. **Manual fallback** (script failed twice): assemble the report by concatenating the stage files yourself, fetch the quote + SPY from the Yahoo Finance quote page (convert to USD if the native currency isn't USD), and append the ledger row `| YYYY-MM-DD | TICKER | CALL | conf | price | spy | rationale | UNRESOLVED | UNRESOLVED |` by hand.

## Portfolio review mode

1. Run `stage0.py --portfolio-review --base-dir . --run-dir work/<run>` once — it resolves ALL unresolved ledger rows (every ticker) and writes the shared `context.md` without a `## Past calls` section.
2. Dispatch `agents/screener.md` with `context.md`; it writes `work/<run>/screening.md` ranking holdings by needs-attention with one-line reasons.
3. Present the ranking with AskUserQuestion (multiSelect) — the USER picks which names get full runs. Never auto-run all.
4. For each chosen ticker, run Stages 1–5 (including the `fetch_data.py` pre-step) in its own `work/YYYY-MM-DD-TICKER/` dir. First run `stage0.py --ticker <T> --skip-resolve --run-dir work/YYYY-MM-DD-T` — it rebuilds the full per-ticker `context.md` (existing-holding banner, bolded row, margin note, `## Past calls` section) WITHOUT re-grading the ledger (`--skip-resolve`; the shared pass in step 1 already graded every ticker). Do not hand-copy or hand-patch the shared context.md.

## Data rules (apply to every stage)

- **Primary: the pre-fetched data pack** (`work/<run>/data/`, written by `fetch_data.py`) — exact numbers for prices, indicators, fundamentals, and recent headlines. Agents read it first and never re-fetch or re-derive what it already contains.
- **Targeted WebSearch** (a few calls per analyst, per each prompt's cap) for judgment-relevant context the pack can't give: why a move happened, breaking news, retail sentiment, macro prints.
- **Fallback: Yahoo Finance via WebFetch/WebSearch** — only when the data pack failed twice. Quotes from `https://finance.yahoo.com/quote/<symbol>`; fundamentals from its financials/statistics tabs. Symbols: US plain (`AAPL`); non-US tickers (SG `.SI` e.g. `ES3.SI`; MY `.KL` e.g. `1023.KL`; HK `.HK`; CN, etc.) take the exchange suffix.
- **Secondary (optional — only if an Alpha Vantage MCP server is connected; skip otherwise): Alpha Vantage MCP tools.** Free-tier keys allow ~25 calls/day, so assume that cap shared across the ENTIRE run (all stages, all tickers) — use SPARINGLY, reserved for structured data where Yahoo is weak. Treat an Alpha Vantage `rate_limit` response as normal, not an error: fall back to Yahoo and continue without flagging it.
- Make targeted calls only — never sweep endpoints.
