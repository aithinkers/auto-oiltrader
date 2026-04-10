"""Iron Condor for range-bound markets.

Phase 1 default strategy. Sells a delta-targeted iron condor when:
  - ATM IV > vol_filter_min_iv
  - DTE in [target_dte_min, target_dte_max]
  - Available credit >= min_credit_pct_of_width
  - Not at max_concurrent positions
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from core.contracts import iron_condor_legs
from core.risk import StopRules
from strategies.base import Strategy, StrategySignal


class IronCondorRange(Strategy):

    def _build_stop_rules(self) -> StopRules:
        return StopRules(
            profit_target_pct=float(self.params.get("profit_target_pct", 0.50)),
            stop_loss_pct=float(self.params.get("stop_loss_pct", 1.00)),
            time_stop_dte=int(self.params.get("time_stop_dte", 3)),
            is_credit=True,
        )

    def evaluate(self, market_state: dict, current_positions: list[dict]) -> list[StrategySignal]:
        if not self.enabled or not self.can_open_more(current_positions):
            return []

        snap = market_state.get("snapshot")
        if snap is None or snap.empty:
            return []

        trading_class = self.params.get("trading_class", "LO")
        target_dte_min = int(self.params.get("target_dte_min", 5))
        target_dte_max = int(self.params.get("target_dte_max", 14))
        min_iv = float(self.params.get("vol_filter_min_iv", 0.40))
        short_put_delta = float(self.params.get("short_put_delta", -0.20))
        short_call_delta = float(self.params.get("short_call_delta", 0.20))
        long_put_offset = float(self.params.get("long_put_offset", 5))
        long_call_offset = float(self.params.get("long_call_offset", 5))
        min_credit_pct = float(self.params.get("min_credit_pct_of_width", 0.20))

        today = date.today()
        signals: list[StrategySignal] = []

        # Find candidate (class, expiry) pairs that meet DTE filter
        candidates = (
            snap[(snap["sec_type"] == "FOP") & (snap["trading_class"] == trading_class)]
            .groupby("expiry")
            .first()
            .reset_index()
        )
        for _, row in candidates.iterrows():
            try:
                exp_dt = datetime.strptime(row["expiry"], "%Y%m%d").date()
            except (ValueError, TypeError):
                continue
            dte = (exp_dt - today).days
            if not (target_dte_min <= dte <= target_dte_max):
                continue

            chain = snap[
                (snap["sec_type"] == "FOP")
                & (snap["trading_class"] == trading_class)
                & (snap["expiry"] == row["expiry"])
                & (snap["iv"] > 0)
                & (snap["delta"].notna())
            ]
            if len(chain) < 8:
                continue

            spot = float(chain["underlying_last"].iloc[0])
            atm = chain.iloc[(chain["strike"] - spot).abs().argsort()[:2]]
            atm_iv = float(atm["iv"].mean())
            if atm_iv < min_iv:
                continue

            # Find short put closest to target delta
            puts = chain[chain["right"] == "P"]
            calls = chain[chain["right"] == "C"]
            if puts.empty or calls.empty:
                continue
            short_p_row = puts.iloc[(puts["delta"] - short_put_delta).abs().argsort()[:1]].iloc[0]
            short_c_row = calls.iloc[(calls["delta"] - short_call_delta).abs().argsort()[:1]].iloc[0]
            short_p_k = float(short_p_row["strike"])
            short_c_k = float(short_c_row["strike"])
            long_p_k = short_p_k - long_put_offset
            long_c_k = short_c_k + long_call_offset

            # Estimate credit (use mids)
            def mid_or_nan(rows, k, right):
                m = rows[(rows["strike"] == k) & (rows["right"] == right)]
                if m.empty:
                    return float("nan")
                return float(m["mid"].iloc[0]) if m["mid"].iloc[0] == m["mid"].iloc[0] else float("nan")

            sp = mid_or_nan(chain, short_p_k, "P")
            lp = mid_or_nan(chain, long_p_k, "P")
            sc = mid_or_nan(chain, short_c_k, "C")
            lc = mid_or_nan(chain, long_c_k, "C")
            if any(x != x for x in (sp, lp, sc, lc)):
                continue

            credit = (sp - lp) + (sc - lc)
            width = max(short_p_k - long_p_k, long_c_k - short_c_k)
            if credit < min_credit_pct * width:
                continue

            max_profit = credit * 1000
            max_loss = (width - credit) * 1000

            qty = int(self.params.get("default_qty", 1))
            legs = iron_condor_legs(
                trading_class=trading_class,
                expiry=row["expiry"],
                short_put_k=short_p_k,
                long_put_k=long_p_k,
                short_call_k=short_c_k,
                long_call_k=long_c_k,
                qty=qty,
            )
            thesis = (
                f"IronCondorRange: ATM IV {atm_iv*100:.1f}% > {min_iv*100:.0f}%, "
                f"DTE {dte}, credit ${credit:.2f} = {credit/width*100:.0f}% of ${width:.0f} width. "
                f"Sell {short_p_k:.0f}P/{short_c_k:.0f}C, buy {long_p_k:.0f}P/{long_c_k:.0f}C wings."
            )
            signals.append(
                StrategySignal(
                    structure="iron_condor",
                    legs=legs,
                    qty=qty,
                    target_debit=-credit,    # negative = credit
                    max_loss=max_loss,
                    max_profit=max_profit,
                    expected_value=None,     # to be computed by trade agent
                    expiry_date=exp_dt,
                    thesis=thesis,
                    confidence=0.55,
                    metadata={"atm_iv": atm_iv, "dte": dte, "spot": spot},
                )
            )
            # Only one signal per evaluation cycle (don't spam)
            break

        return signals
