# trading-skills

Agent skills for trading research, for **Claude Code** and other tools that load `SKILL.md` skills.

| Skill | What it does |
|---|---|
| [`trading-agents`](skills/trading-agents/) | Multi-agent stock analysis: analysts → bull/bear debate → trader → risk debate → portfolio manager. Portfolio-aware, with optional moomoo integration. |

---

## trading-agents — a multi-agent stock analysis skill

An agent skill for **Claude Code** (and other tools that load `SKILL.md` skills, such as Codex) that runs the [TradingAgents](https://github.com/TauricResearch/TradingAgents) methodology inside your coding agent:

```
analysts (fundamentals · technical · news/sentiment)
  → bull vs bear debate
    → trader plan
      → risk debate (aggressive · conservative · neutral)
        → portfolio manager: BUY / ADD / HOLD / TRIM / SELL + size, stop, target
```

It is **portfolio-aware**: give it your holdings and it judges the ticker against your real book (concentration, sector weights, correlation, margin, options exposure, your risk profile). It also keeps a **decisions ledger** that grades every past call against SPY at 30 and 180 days, and shows that track record to the agents the next time you analyze the same ticker.

> ⚠️ **Decision support, not financial advice.** LLM output can be wrong, and market data can be stale or missing. Verify everything before trading. This skill never places orders.

### What you get

- `reports/YYYY-MM-DD-TICKER.md`: the full report, with every stage's reasoning and a decision summary.
- `reports/ledger.md`: one row per call, graded automatically as each horizon matures.
- Deterministic Python for the number-crunching (indicators, FX, concentration, ledger math), so the LLM spends its tokens on judgment.

**See a full example:** [`docs/sample-report-MSFT.md`](docs/sample-report-MSFT.md). It's a complete run in which the analysts debate, the risk team argues over position size, and the portfolio manager trims an oversized position back under the 20% cap.

### Install

1. Copy the skill folder into your skills directory:

   | Tool | Location |
   |---|---|
   | Claude Code (all projects) | `~/.claude/skills/trading-agents/` |
   | Claude Code (one project) | `<project>/.claude/skills/trading-agents/` |
   | Codex | `<project>/.agents/skills/trading-agents/` |

   ```bash
   git clone https://github.com/pyuan07/trading-skills
   cp -r trading-skills/skills/trading-agents ~/.claude/skills/
   ```

2. Give the skill a way to run its Python scripts. **Easiest: install [uv](https://docs.astral.sh/uv/getting-started/installation/)** and you're done. Each script declares its own dependencies, and `uv run` fetches them automatically the first time.

   Or, without uv, create the skill's own venv once (Python 3.10+):

   ```bash
   # macOS / Linux
   cd ~/.claude/skills/trading-agents
   python3 -m venv .venv && .venv/bin/python -m pip install -r scripts/requirements.txt
   ```
   ```powershell
   # Windows
   cd $HOME\.claude\skills\trading-agents
   python -m venv .venv; .venv\Scripts\python.exe -m pip install -r scripts\requirements.txt
   ```

### Use

Work from any folder you like. That folder becomes the skill's workspace: the skill reads your holdings from it and writes `work/` and `reports/` into it.

```
analyze NVDA
should I trim MSFT?
deep dive on 7203.T          ← "deep dive" / "full pipeline" runs every stage as its own subagent
review my portfolio          ← screens all holdings, then you pick which ones get a full run
```

#### Optional: your portfolio

Put these files in your working folder. Templates are in `skills/trading-agents/examples/`.

- **`holdings.csv`**: one row per position. Only `ticker` and `qty` are required:

  ```csv
  ticker,name,qty,price,cost,market_value,currency,account,margin
  AAPL,Apple Inc.,50,230.00,180.00,11500.00,USD,brokerage,no
  7203.T,Toyota Motor,100,2800,2500,280000,JPY,brokerage,no
  ```
  - Use Yahoo Finance symbols (`7203.T`, `ES3.SI`, `0700.HK`). OCC option symbols (`SPY270115P00500000`) are detected automatically and treated as exposure context.
  - Several files (`holdings-ira.csv`, `holdings-taxable.csv`) are merged.
  - **moomoo / Futu users:** drop the position export (`持仓-<account>(<id>)-<timestamp>.csv`) into the folder as-is. It's read natively.
- **`portfolio-profile.json`**: `risk_tolerance`, `max_position_pct`, `margin_comfort`, `horizon`, and free-text `notes` that the portfolio manager must respect.

Without either file the skill still works. It just analyzes the ticker without reference to a portfolio.

#### moomoo users: live positions and extra research (optional)

If the **moomoo MCP** is connected to your agent, the skill uses it automatically, and you don't need a CSV:

| From moomoo | Used for |
|---|---|
| `account_positions` (all your securities accounts) | Live holdings, including short positions and options, instead of a CSV export |
| `quote_research_analyst_consensus` | Analyst rating mix and target-price range, passed to the fundamentals analyst |
| `quote_short_interest` | Short-interest trend and days to cover, passed to the fundamentals and news analysts |
| `quote_unusual_capital` (optional) | Unusual capital and short-selling activity, passed to the news analyst |

The skill only **reads** from moomoo. Its instructions forbid calling any order, trade, cancel or modify tool. If the MCP isn't connected, or a market isn't covered by your data permissions, the skill quietly falls back to `holdings.csv` and Yahoo.

Price history and indicators still come from Yahoo, so the skill works the same for everyone, moomoo customer or not.

#### Run depth and cost

| Depth | What runs | When |
|---|---|---|
| **lite** (default) | Scripts, plus subagents for 2 analysts and the bull/bear debate. Trader, risk and PM are written inline by the orchestrator. | Everyday use |
| **full** | Every stage is a separate subagent | Ask for a "full pipeline" or "deep dive" |

### Data sources

- **Yahoo Finance via [yfinance](https://github.com/ranaroussi/yfinance)**: prices, fundamentals, headlines, FX. It's free and needs no key. It is an unofficial API, so it's subject to Yahoo's terms and occasional breakage, and the skill falls back to web lookups when it fails.
- **Web search** for context such as why a stock moved and breaking news.
- **moomoo MCP** (optional): live positions and research data, as described above.
- **Alpha Vantage MCP** (optional): used sparingly if you have it connected.

All values are normalized to **USD**, and the benchmark is **SPY**.

## Development

```bash
cd skills/trading-agents/scripts
pip install -r requirements-dev.txt
pytest -q          # offline: yfinance is faked in tests
```

## Credits and license

This project adapts the methodology and agent roles of [TradingAgents](https://github.com/TauricResearch/TradingAgents) (Tauric Research, Apache-2.0). See [NOTICE](NOTICE). It is released under the [Apache License 2.0](LICENSE).
