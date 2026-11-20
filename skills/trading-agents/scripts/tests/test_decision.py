"""parse_decision — the strict Stage 5 output contract for decision.md."""
import pytest

from finalize import parse_decision, sizing_warnings

VALID = (
    "Final call: **ADD**\n"
    "Confidence: high\n"
    "SIZE_USD: 1500\n"
    "SIZE_PCT: 18\n"
    "STOP: 380\n"
    "TARGET: 520\n"
    "RATIONALE: cheap vs peers, catalyst soon\n"
)


def test_valid_decision():
    d = parse_decision(VALID)
    assert d["call"] == "ADD" and d["conf"] == "high"
    assert d["size_usd"] == "1500" and d["size_pct"] == "18"
    assert d["stop"] == "380" and d["target"] == "520"
    assert d["rationale"] == "cheap vs peers, catalyst soon"


def test_trim_token_accepted():
    d = parse_decision(VALID.replace("**ADD**", "**TRIM**"))
    assert d["call"] == "TRIM"


def test_size_usd_na_allowed():
    d = parse_decision(VALID.replace("SIZE_USD: 1500", "SIZE_USD: n/a"))
    assert d["size_usd"] == "n/a"


def test_missing_size_usd_rejected():
    with pytest.raises(RuntimeError, match="SIZE_USD"):
        parse_decision(VALID.replace("SIZE_USD: 1500\n", ""))


def test_missing_stop_rejected():
    with pytest.raises(RuntimeError, match="STOP"):
        parse_decision(VALID.replace("STOP: 380\n", ""))


def test_missing_confidence_defaults_dash():
    d = parse_decision(VALID.replace("Confidence: high\n", ""))
    assert d["conf"] == "--"


def test_template_copied_verbatim_is_rejected():
    with pytest.raises(RuntimeError, match="exactly one"):
        parse_decision(VALID.replace("**ADD**", "**BUY / HOLD / SELL**"))


def test_missing_final_call_line_rejected():
    with pytest.raises(RuntimeError, match="no 'Final call:' line"):
        parse_decision(VALID.replace("Final call: **ADD**", "We lean positive"))


def test_missing_rationale_rejected():
    with pytest.raises(RuntimeError, match="no 'RATIONALE:' line"):
        parse_decision(VALID.replace("RATIONALE: cheap vs peers, catalyst soon\n", ""))


def test_pipe_in_rationale_sanitized():
    d = parse_decision(VALID.replace("cheap vs peers, catalyst soon", "cheap | catalyst"))
    assert "|" not in d["rationale"] and d["rationale"] == "cheap / catalyst"


def test_lowercase_prose_tiebreaks_to_uppercase_call():
    d = parse_decision(VALID.replace("**ADD**", "**BUY** — do not sell hedges"))
    assert d["call"] == "BUY"


class TestSizingWarnings:
    def base(self, **kw):
        d = {"size_usd": "1500", "size_pct": "18", "stop": "380", "target": "520"}
        d.update(kw); return d

    def test_no_warnings_when_consistent(self):
        w = sizing_warnings(self.base(), "ADD", total_usd=8000.0, entry_ref=430.0, max_pos_pct=20)
        assert w == []

    def test_pct_usd_mismatch_warns(self):
        # 1500/8000 = 18.75%, but SIZE_PCT says 5 -> mismatch
        w = sizing_warnings(self.base(size_pct="5"), "ADD", 8000.0, 430.0, 20)
        assert any("SIZE_PCT" in x for x in w)

    def test_cap_breach_warns(self):
        w = sizing_warnings(self.base(size_pct="35"), "ADD", 8000.0, 430.0, 20)
        assert any("cap" in x.lower() for x in w)

    def test_buy_stop_above_entry_warns(self):
        w = sizing_warnings(self.base(stop="500"), "BUY", 8000.0, 430.0, 20)  # stop above entry 430
        assert any("stop" in x.lower() for x in w)

    def test_na_size_skips_pct_check(self):
        w = sizing_warnings(self.base(size_usd="n/a"), "HOLD", None, 430.0, 20)
        assert all("SIZE_PCT" not in x for x in w)


def test_final_call_prose_earlier_in_file_is_ignored():
    text = "Before the final call we weighed whether to SELL or BUY.\n" + VALID
    assert parse_decision(text)["call"] == "ADD"


def test_large_trim_is_not_flagged_as_cap_breach():
    d = {"size_usd": "-5000", "size_pct": "-25", "stop": "110", "target": "80"}
    assert not any("exceeds max_position_pct" in w for w in sizing_warnings(d, "TRIM", 20000, 100.0, 20))
    assert any("exceeds max_position_pct" in w
               for w in sizing_warnings({**d, "size_usd": "5000", "size_pct": "25", "stop": "90", "target": "120"},
                                        "ADD", 20000, 100.0, 20))
