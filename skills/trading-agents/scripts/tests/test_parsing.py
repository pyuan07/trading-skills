"""parse_occ (OCC option symbols) and num (CSV numeric cells)."""
from datetime import date

import pytest

from ta_common import parse_occ
from stage0 import num


class TestParseOCC:
    def test_valid_put(self):
        got = parse_occ("TSM260918P360000")
        assert got == {
            "underlying": "TSM",
            "expiry": date(2026, 9, 18).isoformat(),
            "cp": "put",
            "strike": 360.0,
        }

    def test_valid_call(self):
        got = parse_occ("NVDA260117C00500000")
        assert got["underlying"] == "NVDA"
        assert got["cp"] == "call"
        assert got["strike"] == 500.0

    def test_plain_equity_is_not_an_option(self):
        assert parse_occ("NVDA") is None
        assert parse_occ("BRK.B") is None

    def test_invalid_expiry_month_rejected(self):
        # month 13 -> not a real date -> None, not a crash
        assert parse_occ("AAA261318C010000") is None

    def test_whitespace_and_case(self):
        assert parse_occ("  tsm260918p360000  ")["underlying"] == "TSM"


class TestNum:
    @pytest.mark.parametrize("raw,expected", [
        ("1,234.56", 1234.56),
        ("+8.93%", 8.93),
        ("-12.5", -12.5),
        ("--", None),
        ("", None),
        (None, None),
        ("  ", None),
        ("garbage", None),
    ])
    def test_num(self, raw, expected):
        assert num(raw) == expected
