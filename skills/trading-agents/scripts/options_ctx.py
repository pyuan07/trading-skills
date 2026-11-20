"""Pure option-context helpers (notional / DTE / moneyness). No network."""
from __future__ import annotations
from datetime import date


def enrich(opt, spot, today_iso):
    occ = opt["occ"]
    qty = opt.get("qty") or 0
    strike = occ["strike"]
    notional_val = strike * 100 * abs(qty) * (1 if qty > 0 else -1)
    notional = f"{notional_val:,.0f}"
    try:
        dte = (date.fromisoformat(occ["expiry"]) - date.fromisoformat(today_iso)).days
        dte_s = str(dte)
    except ValueError:
        dte_s = "--"
    if spot is None:
        money = "--"
    else:
        pct = (spot - strike) / strike
        if occ["cp"] == "put":
            state = "ITM" if spot < strike else "OTM"
        else:
            state = "ITM" if spot > strike else "OTM"
        money = f"{state} (spot {spot:,.2f} vs strike {strike:,.0f}, {pct:+.1%})"
    return {"notional": notional, "dte": dte_s, "moneyness": money}
