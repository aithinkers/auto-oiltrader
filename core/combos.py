"""Higher-level combo construction and pricing helpers.

Wraps `core.contracts` for the common case of "give me a combo for strategy X
with these strikes" and computes theoretical max profit/loss.
"""

from __future__ import annotations

from dataclasses import dataclass

from core.contracts import (
    ComboLegSpec,
    iron_condor_legs,
    call_butterfly_legs,
    put_debit_spread_legs,
)


@dataclass
class ComboMath:
    """Theoretical max profit, max loss, and breakeven for a debit/credit combo."""
    is_debit: bool          # True if you pay net, False if you collect net
    net_amount: float       # positive = debit, negative = credit (relative to perspective)
    width: float            # widest leg-strike distance
    max_profit: float
    max_loss: float
    upper_be: float
    lower_be: float


def iron_condor_math(
    short_put_k: float,
    long_put_k: float,
    short_call_k: float,
    long_call_k: float,
    net_credit: float,
    qty: int,
    multiplier: int = 1000,
) -> ComboMath:
    """Compute IC math from leg strikes and net credit. `net_credit` is positive."""
    put_width = short_put_k - long_put_k
    call_width = long_call_k - short_call_k
    width = max(put_width, call_width)
    max_profit = net_credit * qty * multiplier
    max_loss = (width - net_credit) * qty * multiplier
    return ComboMath(
        is_debit=False,
        net_amount=-net_credit,
        width=width,
        max_profit=max_profit,
        max_loss=max_loss,
        upper_be=short_call_k + net_credit,
        lower_be=short_put_k - net_credit,
    )


def call_butterfly_math(
    lower_k: float,
    body_k: float,
    upper_k: float,
    net_debit: float,
    qty: int,
    multiplier: int = 1000,
) -> ComboMath:
    width = max(body_k - lower_k, upper_k - body_k)
    max_profit = (width - net_debit) * qty * multiplier
    max_loss = net_debit * qty * multiplier
    return ComboMath(
        is_debit=True,
        net_amount=net_debit,
        width=width,
        max_profit=max_profit,
        max_loss=max_loss,
        upper_be=body_k + (width - net_debit),
        lower_be=body_k - (width - net_debit),
    )


def put_debit_spread_math(
    long_k: float,
    short_k: float,
    net_debit: float,
    qty: int,
    multiplier: int = 1000,
) -> ComboMath:
    width = long_k - short_k
    max_profit = (width - net_debit) * qty * multiplier
    max_loss = net_debit * qty * multiplier
    return ComboMath(
        is_debit=True,
        net_amount=net_debit,
        width=width,
        max_profit=max_profit,
        max_loss=max_loss,
        upper_be=long_k - net_debit,
        lower_be=0.0,   # max profit anywhere below short strike
    )
