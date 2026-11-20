"""build_context — the Stage 0 context.md markdown, including the concentration warning."""
import pandas as pd
from stage0 import build_context


def _pos(code, usd, ccy="USD", acct_type="Cash", occ=None, name="x"):
    return {"code": code, "name": name, "qty": 100, "price": 1.0, "cost": 1.0,
            "mktval": usd, "pl_pct": 0.0, "ccy": ccy, "acct": "1", "acct_type": acct_type,
            "usd": usd, "occ": occ}


def test_concentration_warning_above_max():
    # 30% position with max_position_pct 20 -> flagged with warning marker.
    positions = [_pos("NVDA", 3000), _pos("MSFT", 7000)]
    ctx = build_context("NVDA", positions, [], {}, ["src"], {"max_position_pct": 20},
                        None, [], max_pos_pct=20)
    nvda_line = next(ln for ln in ctx.splitlines() if "| NVDA |" in ln or "**NVDA**" in ln)
    assert "⚠️" in nvda_line

    # 10% position under the cap -> no warning marker on its row
    positions2 = [_pos("AAA", 1000), _pos("BBB", 9000)]
    ctx2 = build_context("AAA", positions2, [], {}, ["src"], {"max_position_pct": 20},
                         None, [], max_pos_pct=20)
    aaa_line = next(ln for ln in ctx2.splitlines() if "**AAA**" in ln)
    assert "⚠️" not in aaa_line


def test_analyzed_ticker_is_bolded_and_flagged_as_holding():
    positions = [_pos("NVDA", 5000), _pos("MSFT", 5000)]
    ctx = build_context("NVDA", positions, [], {}, ["src"], {"max_position_pct": 50},
                        None, [], max_pos_pct=50)
    assert "**NVDA**" in ctx
    assert "existing holding" in ctx


def test_options_section_rendered():
    occ = {"underlying": "TSM", "expiry": "2026-09-18", "cp": "put", "strike": 360.0}
    options = [{**_pos("TSM260918P360000", -700, occ=occ), "qty": -2}]
    ctx = build_context("TSM", [_pos("TSM", 5000)], options, {}, ["src"],
                        {"max_position_pct": 20}, None, [], max_pos_pct=20)
    assert "## Options positions" in ctx
    assert "short put on TSM" in ctx


def test_past_calls_section_present_for_ticker():
    past = [{"date": "2026-01-01", "call": "BUY", "conf": "med", "price": "100", "spy": "700",
             "rationale": "r", "status_30": "WIN (30d, 2026-01-31)", "status_180": "UNRESOLVED"}]
    ctx = build_context("AAA", [_pos("AAA", 100)], [], {}, ["src"],
                        {"max_position_pct": 20}, None, past, max_pos_pct=20)
    assert "## Past calls for AAA" in ctx
    assert "WIN" in ctx


def test_short_position_concentration_warning_uses_magnitude():
    # A -75%-of-equity short is just as concentrated as a long; signed pct must not hide it.
    positions = [_pos("SHRT", -3000), _pos("MSFT", 7000)]  # total 4000, SHRT = -75%
    ctx = build_context("MSFT", positions, [], {}, ["src"], {"max_position_pct": 20},
                        None, [], max_pos_pct=20)
    shrt_line = next(ln for ln in ctx.splitlines() if "| SHRT |" in ln)
    assert "⚠️" in shrt_line


def test_existing_holding_banner_uses_five_way_action_space():
    positions = [_pos("NVDA", 5000)]
    ctx = build_context("NVDA", positions, [], {}, ["src"], {"max_position_pct": 50},
                        None, [], max_pos_pct=50)
    assert "ADD / HOLD / TRIM / SELL" in ctx


def test_portfolio_table_has_recent_move_columns():
    positions = [_pos("NVDA", 5000), _pos("ES3", 2000, ccy="SGD")]
    header = next(ln for ln in build_context(
        "NVDA", positions, [], {}, ["src"], {"max_position_pct": 50}, None, [], max_pos_pct=50
    ).splitlines() if ln.startswith("| Ticker |"))
    assert "| 1d | 5d | 1m |" in header

    # No closes DataFrame supplied -> every recent-move cell degrades to '--', never crashes.
    ctx_no_closes = build_context("NVDA", positions, [], {}, ["src"], {"max_position_pct": 50},
                                   None, [], max_pos_pct=50)
    nvda_line = next(ln for ln in ctx_no_closes.splitlines() if "| NVDA |" in ln or "**NVDA**" in ln)
    assert nvda_line.count("--") >= 3

    # With a closes DataFrame covering NVDA (mapped 1:1, USD) the move is a real number, not '--'.
    idx = pd.date_range("2026-01-01", periods=25)
    closes = pd.DataFrame({"NVDA": [100 + i for i in range(25)]}, index=idx)
    ctx_with_closes = build_context("NVDA", positions, [], {}, ["src"], {"max_position_pct": 50},
                                     None, [], max_pos_pct=50, closes=closes)
    nvda_line2 = next(ln for ln in ctx_with_closes.splitlines() if "| NVDA |" in ln or "**NVDA**" in ln)
    assert "%" in nvda_line2
