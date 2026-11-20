from options_ctx import enrich


def _opt(cp="put", strike=370.0, qty=-1):
    return {"occ": {"underlying": "TSM", "expiry": "2026-09-18", "cp": cp, "strike": strike},
            "qty": qty, "usd": -1310.0}


def test_notional_signed_by_side():
    e = enrich(_opt(qty=-1), spot=410.0, today_iso="2026-07-17")
    assert e["notional"].startswith("-")          # short -> negative
    assert "37,000" in e["notional"]              # 370 * 100 * 1


def test_dte_positive():
    e = enrich(_opt(), spot=410.0, today_iso="2026-07-17")
    assert e["dte"] == "63"                        # 2026-07-17 -> 2026-09-18


def test_moneyness_put_itm_otm():
    itm = enrich(_opt(cp="put", strike=430.0), spot=410.0, today_iso="2026-07-17")
    otm = enrich(_opt(cp="put", strike=370.0), spot=410.0, today_iso="2026-07-17")
    assert "ITM" in itm["moneyness"] and "OTM" in otm["moneyness"]


def test_missing_spot_degrades():
    e = enrich(_opt(), spot=None, today_iso="2026-07-17")
    assert e["moneyness"] == "--"
