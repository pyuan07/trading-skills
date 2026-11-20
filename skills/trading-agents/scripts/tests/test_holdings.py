"""find_csvs (newest-per-account, label->type) and parse_holdings (both header variants, BOM,
option detection, negative shorts). This is the highest-risk parsing surface in the skill."""
from stage0 import find_csvs, parse_holdings


class TestFindCSVs:
    def test_finds_both_accounts_with_types(self, holdings_dir):
        found = {acct: acct_type for _, acct, acct_type, _ in find_csvs(holdings_dir)}
        assert found == {"0001": "Margin", "0002": "Cash"}

    def test_newest_timestamp_per_account_wins(self, holdings_dir):
        # add an older margin file for the same account; the newer one must win
        (holdings_dir / "持仓-保证金账户(0001)-20260701-010101.csv").write_text(
            "代码,名称,持有数量,现价,摊薄成本价,市值,盈亏比例,币种\n", encoding="utf-8-sig")
        rows = [r for r in find_csvs(holdings_dir) if r[1] == "0001"]
        assert len(rows) == 1
        assert rows[0][3].strftime("%Y%m%d") == "20260709"  # the newer file


class TestParseHoldings:
    def _parse(self, holdings_dir, fname, acct, acct_type):
        return list(parse_holdings(holdings_dir / fname, acct, acct_type))

    def test_margin_variant_uses_摊薄成本价(self, holdings_dir):
        rows = self._parse(holdings_dir, "持仓-保证金账户(0001)-20260709-024150.csv", "0001", "Margin")
        nvda = next(r for r in rows if r["code"] == "NVDA")
        assert nvda["cost"] == 150.0          # from 摊薄成本价
        assert nvda["qty"] == 100.0
        assert nvda["pl_pct"] == 38.45
        assert nvda["ccy"] == "USD"

    def test_cash_variant_uses_平均成本价(self, holdings_dir):
        rows = self._parse(holdings_dir, "持仓-综合账户(0002)-20260709-024219.csv", "0002", "Cash")
        meta = next(r for r in rows if r["code"] == "META")
        assert meta["cost"] == 600.0          # from 平均成本价
        assert meta["pl_pct"] == 11.54        # from 未实现盈亏比例

    def test_option_row_detected(self, holdings_dir):
        rows = self._parse(holdings_dir, "持仓-保证金账户(0001)-20260709-024150.csv", "0001", "Margin")
        opt = next(r for r in rows if r["code"].startswith("TSM26"))
        assert opt["occ"] is not None
        assert opt["occ"]["underlying"] == "TSM"
        assert opt["qty"] == -2.0             # short option, negative preserved

    def test_short_equity_negative_preserved(self, holdings_dir):
        rows = self._parse(holdings_dir, "持仓-综合账户(0002)-20260709-024219.csv", "0002", "Cash")
        short = next(r for r in rows if r["code"] == "SHORTCO")
        assert short["qty"] == -10.0
        assert short["mktval"] == -200.0
        assert short["occ"] is None           # not an option

    def test_non_usd_currency_read(self, holdings_dir):
        rows = self._parse(holdings_dir, "持仓-保证金账户(0001)-20260709-024150.csv", "0001", "Margin")
        sg = next(r for r in rows if r["code"] == "ES3.SI")
        assert sg["ccy"] == "SGD"


class TestGenericHoldings:
    """Broker-neutral holdings.csv (English headers, case-insensitive, optional columns)."""

    def _write(self, tmp_path, text, name="holdings.csv"):
        p = tmp_path / name
        p.write_text(text, encoding="utf-8")
        return p

    def test_found_alongside_named_variants(self, tmp_path):
        from stage0 import find_generic_csvs
        self._write(tmp_path, "ticker,qty\nAAPL,1\n")
        self._write(tmp_path, "ticker,qty\nMSFT,1\n", name="holdings-ira.csv")
        self._write(tmp_path, "ticker,qty\nX,1\n", name="other.csv")
        names = sorted(p.name for p, _ in find_generic_csvs(tmp_path))
        assert names == ["holdings-ira.csv", "holdings.csv"]

    def test_full_row_parsed(self, tmp_path):
        from stage0 import parse_generic_holdings
        p = self._write(tmp_path,
                        "Ticker,Name,Qty,Price,Cost,Market_Value,PL_Pct,Currency,Account,Margin\n"
                        "nvda,NVIDIA,100,207.67,150,\"20,767.00\",+38.45%,usd,IBKR,yes\n")
        (r,) = list(parse_generic_holdings(p))
        assert r["code"] == "NVDA" and r["qty"] == 100.0 and r["mktval"] == 20767.0
        assert r["ccy"] == "USD" and r["acct"] == "IBKR" and r["acct_type"] == "Margin"
        assert r["pl_pct"] == 38.45 and r["occ"] is None

    def test_minimal_columns_default_sensibly(self, tmp_path):
        from stage0 import parse_generic_holdings
        p = self._write(tmp_path, "symbol,shares,price\nES3.SI,500,3.5\n,,\n")
        (r,) = list(parse_generic_holdings(p))
        assert r["mktval"] == 1750.0          # qty * price when market_value absent
        assert r["ccy"] == "USD" and r["acct"] == "holdings" and r["acct_type"] == "Cash"

    def test_option_and_short_rows(self, tmp_path):
        from stage0 import parse_generic_holdings
        p = self._write(tmp_path, "ticker,qty,market_value\nTSM260918P360000,-2,-700\nSHORTCO,-10,-200\n")
        opt, short = list(parse_generic_holdings(p))
        assert opt["occ"]["underlying"] == "TSM" and opt["qty"] == -2.0
        assert short["occ"] is None and short["mktval"] == -200.0

    def test_missing_ticker_column_raises(self, tmp_path):
        import pytest
        from stage0 import parse_generic_holdings
        p = self._write(tmp_path, "qty,price\n1,2\n")
        with pytest.raises(ValueError, match="ticker"):
            list(parse_generic_holdings(p))
