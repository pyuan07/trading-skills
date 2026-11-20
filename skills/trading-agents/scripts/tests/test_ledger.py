"""read_ledger / write_ledger round-trip, the fixed-horizon resolve_rows logic, and the
finalize.py same-day upsert. Grading uses the fake yfinance so it is deterministic and offline.
Grading is adjusted close-to-close over [call date -> +30d]; the recorded Price/SPY cells are
display-only (see test_grading_ignores_recorded_price_cells)."""
from datetime import date, timedelta

from finalize import upsert_ledger_row
from stage0 import read_ledger, write_ledger, resolve_rows, migrate_row, BAND_30, BAND_180, HORIZONS
from ta_common import today


class TestLedgerIO:
    def test_roundtrip_preserves_prose_and_9col_rows(self, golden_ledger):
        pre, rows = read_ledger(golden_ledger)
        assert "intro prose" in pre
        assert len(rows) == 3
        assert rows[0]["ticker"] == "NVDA" and rows[0]["conf"] == "med"
        assert rows[0]["status_30"] == "UNRESOLVED" and rows[0]["status_180"] == "UNRESOLVED"
        assert rows[2]["status_30"].startswith("WIN")
        write_ledger(golden_ledger, pre, rows)
        pre2, rows2 = read_ledger(golden_ledger)
        assert rows2 == rows and "intro prose" in pre2

    def test_legacy_7col_is_migrated_to_9col(self, legacy_ledger):
        pre, rows = read_ledger(legacy_ledger)
        assert len(rows) == 2
        aaa = rows[0]
        assert aaa["conf"] == "--"
        assert aaa["status_30"] == "UNRESOLVED" and aaa["status_180"] == "UNRESOLVED"
        bbb = rows[1]
        assert bbb["status_30"].startswith("WIN")   # old Status → Status 30d
        assert bbb["status_180"] == "UNRESOLVED"
        # written back out as 9-col
        write_ledger(legacy_ledger, pre, rows)
        text = legacy_ledger.read_text(encoding="utf-8")
        assert "Status 30d | Status 180d" in text

    def test_malformed_row_preserved_verbatim_never_graded(self, golden_ledger):
        bad = "| 2026-01-05 | CCC | BUY | 10.00 | cheap |"  # 5 cells, neither 7 nor 9
        golden_ledger.write_text(golden_ledger.read_text(encoding="utf-8") + bad + "\n",
                                 encoding="utf-8")
        pre, rows = read_ledger(golden_ledger)
        assert {"_raw": bad} in rows
        notes, changed = resolve_rows(rows, "CCC")
        assert changed is False
        write_ledger(golden_ledger, pre, rows)
        assert bad in golden_ledger.read_text(encoding="utf-8")


def _row(days_old, call, ticker="AAA", conf="med"):
    d = (date.today() - timedelta(days=days_old)).isoformat()
    return {"date": d, "ticker": ticker, "call": call, "conf": conf, "price": "100.00",
            "spy": "700.00", "rationale": "x", "status_30": "UNRESOLVED", "status_180": "UNRESOLVED"}


class TestResolveHorizon:
    def test_young_row_both_unresolved_without_network(self, golden_ledger):
        _, rows = read_ledger(golden_ledger)
        young = [r for r in rows if r["ticker"] == "NVDA"]
        notes, changed = resolve_rows(young, "NVDA")
        assert young[0]["status_30"] == "UNRESOLVED" and young[0]["status_180"] == "UNRESOLVED"
        assert changed is False

    def test_45d_row_grades_30d_only(self, fake_yf):
        rows = [_row(45, "BUY")]
        call_date = date.fromisoformat(rows[0]["date"])
        m30 = (call_date + timedelta(days=30)).isoformat()
        fake_yf({"AAA": {"currency": "USD", "start_close": 100.0, "end_close": 130.0,
                         "close_at": {m30: 130.0}},
                 "SPY": {"currency": "USD", "start_close": 700.0, "end_close": 735.0,
                         "close_at": {m30: 735.0}}})
        notes, changed = resolve_rows(rows, "AAA")
        assert changed is True
        assert rows[0]["status_30"].startswith("WIN")     # +30% vs +5% -> rel +25%
        assert rows[0]["status_180"] == "UNRESOLVED"      # not matured

    def test_200d_row_grades_both_horizons_independently(self, fake_yf):
        rows = [_row(200, "BUY")]
        cd = date.fromisoformat(rows[0]["date"])
        m30 = (cd + timedelta(days=30)).isoformat()
        m180 = (cd + timedelta(days=180)).isoformat()
        # 30d: stock lags (LOSS); 180d: stock beats (WIN)
        fake_yf({"AAA": {"currency": "USD", "start_close": 100.0,
                         "close_at": {m30: 101.0, m180: 150.0}},
                 "SPY": {"currency": "USD", "start_close": 700.0,
                         "close_at": {m30: 770.0, m180: 735.0}}})
        resolve_rows(rows, "AAA")
        assert rows[0]["status_30"].startswith("LOSS")
        assert rows[0]["status_180"].startswith("WIN")

    def test_hold_tracks_spy_is_win(self, fake_yf):
        rows = [_row(45, "HOLD")]
        cd = date.fromisoformat(rows[0]["date"]); m30 = (cd + timedelta(days=30)).isoformat()
        # stock +5%, SPY +5% -> rel 0 -> HOLD WIN (tracked)
        fake_yf({"AAA": {"currency": "USD", "start_close": 100.0, "close_at": {m30: 105.0}},
                 "SPY": {"currency": "USD", "start_close": 700.0, "close_at": {m30: 735.0}}})
        resolve_rows(rows, "AAA")
        assert rows[0]["status_30"].startswith("WIN")

    def test_hold_materially_lags_is_loss(self, fake_yf):
        rows = [_row(45, "HOLD")]
        cd = date.fromisoformat(rows[0]["date"]); m30 = (cd + timedelta(days=30)).isoformat()
        # stock flat, SPY +10% -> rel -10% < -3% -> HOLD LOSS (should have trimmed)
        fake_yf({"AAA": {"currency": "USD", "start_close": 100.0, "close_at": {m30: 100.0}},
                 "SPY": {"currency": "USD", "start_close": 700.0, "close_at": {m30: 770.0}}})
        resolve_rows(rows, "AAA")
        assert rows[0]["status_30"].startswith("LOSS")

    def test_trim_wins_when_name_lags(self, fake_yf):
        rows = [_row(45, "TRIM")]
        cd = date.fromisoformat(rows[0]["date"]); m30 = (cd + timedelta(days=30)).isoformat()
        # stock -10%, SPY +10% -> rel -20% -> TRIM/SELL WIN
        fake_yf({"AAA": {"currency": "USD", "start_close": 100.0, "close_at": {m30: 90.0}},
                 "SPY": {"currency": "USD", "start_close": 700.0, "close_at": {m30: 770.0}}})
        resolve_rows(rows, "AAA")
        assert rows[0]["status_30"].startswith("WIN")

    def test_add_flat_within_band(self, fake_yf):
        rows = [_row(45, "ADD")]
        cd = date.fromisoformat(rows[0]["date"]); m30 = (cd + timedelta(days=30)).isoformat()
        # rel 0 -> ADD FLAT
        fake_yf({"AAA": {"currency": "USD", "start_close": 100.0, "close_at": {m30: 110.0}},
                 "SPY": {"currency": "USD", "start_close": 700.0, "close_at": {m30: 770.0}}})
        resolve_rows(rows, "AAA")
        assert rows[0]["status_30"].startswith("FLAT")

    def test_unparseable_date_left_alone(self, fake_yf):
        rows = [{"date": "not-a-date", "ticker": "AAA", "call": "BUY", "conf": "med",
                 "price": "100", "spy": "700", "rationale": "x",
                 "status_30": "UNRESOLVED", "status_180": "UNRESOLVED"}]
        notes, changed = resolve_rows(rows, "AAA")
        assert rows[0]["status_30"] == "UNRESOLVED" and changed is False
        assert "unparseable" in notes[0]


class TestUpsertLedgerRow:
    def _row(self, rationale="first take"):
        return (f"| {today()} | NVDA | BUY | med | 200.00 | 700.00 | {rationale} "
                f"| UNRESOLVED | UNRESOLVED |")

    def test_appends_when_no_same_day_row(self, golden_ledger):
        n_before = len(golden_ledger.read_text(encoding="utf-8").splitlines())
        action = upsert_ledger_row(golden_ledger, self._row(), today(), "NVDA")
        assert action == "appended"
        assert len(golden_ledger.read_text(encoding="utf-8").splitlines()) == n_before + 1

    def test_same_day_rerun_replaces_instead_of_duplicating(self, golden_ledger):
        upsert_ledger_row(golden_ledger, self._row("first take"), today(), "NVDA")
        action = upsert_ledger_row(golden_ledger, self._row("second take"), today(), "NVDA")
        assert action.startswith("replaced")
        text = golden_ledger.read_text(encoding="utf-8")
        assert text.count(f"| {today()} | NVDA |") == 1
        assert "second take" in text and "first take" not in text

    def test_resolved_same_day_row_is_not_touched(self, golden_ledger):
        resolved = f"| {today()} | NVDA | BUY | med | 200.00 | 700.00 | done | WIN (30d, x) | UNRESOLVED |"
        golden_ledger.write_text(golden_ledger.read_text(encoding="utf-8") + resolved + "\n",
                                 encoding="utf-8")
        action = upsert_ledger_row(golden_ledger, self._row(), today(), "NVDA")
        assert action == "appended"
        text = golden_ledger.read_text(encoding="utf-8")
        assert resolved in text and "first take" in text
