import pandas as pd
from concentration import correlations, sector_weights, sizing_guardrail, \
    load_sector_cache, save_sector_cache, yahoo_symbol, recent_moves

def test_correlations_rank_and_exclude_self():
    idx = pd.date_range("2026-01-01", periods=5)
    df = pd.DataFrame({
        "AAA": [10, 11, 12, 13, 14],
        "BBB": [10, 11, 12, 13, 14],       # perfectly correlated
        "CCC": [14, 12.6, 11.4545, 10.5, 9.6923],  # returns are the exact negation of AAA's
    }, index=idx).pct_change().dropna()
    out = correlations(df, "AAA")
    assert out[0][0] == "BBB" and round(out[0][1], 3) == 1.0
    assert any(t == "CCC" and round(c, 3) == -1.0 for t, c in out)
    assert all(t != "AAA" for t, _ in out)

def test_sector_weights_sum_by_sector():
    positions = [{"code": "AAA", "usd": 6000.0}, {"code": "BBB", "usd": 2000.0},
                 {"code": "CCC", "usd": 2000.0}]
    sectors = {"AAA": "Tech", "BBB": "Tech", "CCC": "Energy"}
    w = dict(sector_weights(positions, sectors))
    assert round(w["Tech"], 1) == 80.0 and round(w["Energy"], 1) == 20.0

def test_sizing_guardrail_headroom():
    positions = [{"code": "TSM", "usd": 1745.0}]
    txt = sizing_guardrail("TSM", positions, total=8000.0, max_pos_pct=20)
    assert "TSM" in txt and "%" in txt and "cap" in txt.lower()

def test_sector_cache_roundtrip(tmp_path):
    p = tmp_path / "holdings-meta.json"
    save_sector_cache(p, {"AAA": {"sector": "Tech", "fetched": "2026-07-01"}})
    assert load_sector_cache(p)["AAA"]["sector"] == "Tech"
    assert load_sector_cache(tmp_path / "missing.json") == {}

def test_yahoo_symbol_suffix_by_currency():
    assert yahoo_symbol("NVDA", "USD") == "NVDA"
    assert yahoo_symbol("ES3", "SGD") == "ES3.SI"
    assert yahoo_symbol("1023", "MYR") == "1023.KL"
    assert yahoo_symbol("5347", "MYR") == "5347.KL"
    assert yahoo_symbol("0700", "HKD") == "0700.HK"
    # already-suffixed codes aren't double-suffixed; ccy is case/space-insensitive
    assert yahoo_symbol("ES3.SI", " sgd ") == "ES3.SI"

def test_recent_moves_from_closes():
    idx = pd.date_range("2026-01-01", periods=25)
    prices = [100 + i for i in range(25)]  # steady +1/day
    closes = pd.DataFrame({"AAA": prices}, index=idx)
    moves = recent_moves(closes, "aaa")  # case-insensitive column lookup
    assert round(moves["1d"], 4) == round(124 / 123 - 1, 4)  # last=124, prev=123
    assert moves["5d"] is not None and moves["1m"] is not None

def test_recent_moves_missing_symbol_or_empty_closes():
    closes = pd.DataFrame({"AAA": [1, 2, 3]})
    assert recent_moves(closes, "ZZZ") == {"1d": None, "5d": None, "1m": None}
    assert recent_moves(pd.DataFrame(), "AAA") == {"1d": None, "5d": None, "1m": None}
    assert recent_moves(None, "AAA") == {"1d": None, "5d": None, "1m": None}
