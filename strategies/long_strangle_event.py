"""Long strangle for binary-event plays.

Buys an OTM call + OTM put on the same expiry. Long vega + long gamma.
Profits on a large move in either direction.

Use when:
  - A scheduled binary event is imminent (EIA report, OPEC, Fed)
  - IV is reasonable (< max_iv) so we're not paying through the nose
  - We expect realized vol to outpace implied around the event

Phase 1: this strategy fires only when the user manually flags via a pattern
or when the analysis_agent (Phase 2) decides. By default, it never fires
autonomously since we don't yet have an event calendar.
"""

from __future__ import annotations

from datetime import date, datetime

from core.contracts import FopSpec, ComboLegSpec
from core.risk import StopRules
from core.verticals import _eligible_chain, _expiry_groups
from strategies.base import Strategy, StrategySignal


class LongStrangleEvent(Strategy):

    def _build_stop_rules(self) -> StopRules:
        return StopRules(
            profit_target_pct=float(self.params.get("profit_target_pct", 1.00)),
            stop_loss_pct=float(self.params.get("stop_loss_pct", 0.50)),
            time_stop_dte=int(self.params.get("time_stop_dte", 1)),
            is_credit=False,
        )

    def evaluate(self, market_state: dict, current_positions: list[dict]) -> list[StrategySignal]:
        # In Phase 1, this strategy needs an explicit event signal in the
        # market_state. The news_collector / analysis_agent will set this.
        if not self.enabled or not self.can_open_more(current_positions):
            return []
        if not market_state.get("event_imminent", False):
            return []

        snap = market_state.get("snapshot")
        if snap is None or snap.empty:
            return []

        trading_class = self.params.get("trading_class", "LO")
        target_dte_min = int(self.params.get("target_dte_min", 1))
        target_dte_max = int(self.params.get("target_dte_max", 5))
        max_iv = float(self.params.get("max_iv", 0.80))
        target_call_delta = float(self.params.get("call_delta", 0.20))
        target_put_delta = float(self.params.get("put_delta", -0.20))

        chain = _eligible_chain(snap, trading_class)
        if chain.empty:
            return []
        groups = _expiry_groups(chain, target_dte_min, target_dte_max, date.today())
        if not groups:
            return []

        # Pick the soonest qualifying expiry
        expiry, exp_dt, dte, sub = groups[0]
        spot = float(sub["underlying_last"].iloc[0])
        sub = sub.copy()
        sub["dist"] = (sub["strike"] - spot).abs()
        atm_iv = float(sub.sort_values("dist").head(2)["iv"].mean())
        if atm_iv > max_iv:
            return []

        calls = sub[sub["right"] == "C"].copy()
        puts = sub[sub["right"] == "P"].copy()
        if calls.empty or puts.empty:
            return []
        calls["delta_diff"] = (calls["delta"] - target_call_delta).abs()
        puts["delta_diff"] = (puts["delta"] - target_put_delta).abs()
        c_row = calls.sort_values("delta_diff").iloc[0]
        p_row = puts.sort_values("delta_diff").iloc[0]
        call_k = float(c_row["strike"])
        put_k = float(p_row["strike"])
        c_mid = float(c_row["mid"])
        p_mid = float(p_row["mid"])
        if c_mid <= 0 or p_mid <= 0:
            return []

        debit = c_mid + p_mid
        max_loss = debit * 1000
        # Long strangle has unbounded upside; we cap "max profit" at 5x debit for sizing
        max_profit_estimate = 5.0 * debit * 1000

        qty = int(self.params.get("default_qty", 1))
        legs = [
            ComboLegSpec(FopSpec("CL", trading_class, expiry, call_k, "C"), qty, "BUY"),
            ComboLegSpec(FopSpec("CL", trading_class, expiry, put_k, "P"), qty, "BUY"),
        ]
        thesis = (
            f"LongStrangleEvent: event imminent, ATM IV {atm_iv*100:.1f}% < max {max_iv*100:.0f}%. "
            f"Buy {trading_class} {expiry} {call_k:.0f}C + {put_k:.0f}P "
            f"for ${debit:.2f} debit. DTE={dte}. Spot={spot:.2f}. "
            f"Profits on move > ${debit:.2f} either way."
        )
        return [StrategySignal(
            structure="long_strangle",
            legs=legs,
            qty=qty,
            target_debit=debit,
            max_loss=max_loss,
            max_profit=max_profit_estimate,
            expected_value=None,
            expiry_date=exp_dt,
            thesis=thesis,
            confidence=0.50,
            metadata={"atm_iv": atm_iv, "dte": dte, "spot": spot, "event_driven": True},
        )]
