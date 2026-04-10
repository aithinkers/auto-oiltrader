"""Contract specs and conid resolution.

Builds IB Contract objects, caches conIds, builds combo BAGs.
The trader_daemon uses these to construct orders.
"""

from __future__ import annotations

from dataclasses import dataclass


CL_MULT = 1000  # 1 CL contract = 1000 barrels


@dataclass(frozen=True)
class FutSpec:
    symbol: str            # 'CL' | 'BZ'
    exchange: str          # 'NYMEX'
    currency: str = "USD"
    multiplier: str = "1000"


@dataclass(frozen=True)
class FopSpec:
    symbol: str            # 'CL'
    trading_class: str     # 'LO', 'LO1', 'LO2', etc.
    expiry: str            # 'YYYYMMDD'
    strike: float
    right: str             # 'C' | 'P'
    exchange: str = "NYMEX"
    currency: str = "USD"
    multiplier: str = "1000"


@dataclass(frozen=True)
class ComboLegSpec:
    """One leg of a combo, in spec form. Resolved to ConId at order time."""
    instrument: FopSpec | FutSpec
    ratio: int             # 1, 2, etc
    action: str            # 'BUY' | 'SELL'


def to_ib_fut(spec: FutSpec):
    """Build an ibapi Contract for a future. Imported lazily."""
    from ibapi.contract import Contract
    c = Contract()
    c.symbol = spec.symbol
    c.secType = "FUT"
    c.exchange = spec.exchange
    c.currency = spec.currency
    return c


def to_ib_fop(spec: FopSpec):
    """Build an ibapi Contract for a futures option."""
    from ibapi.contract import Contract
    c = Contract()
    c.symbol = spec.symbol
    c.secType = "FOP"
    c.exchange = spec.exchange
    c.currency = spec.currency
    c.lastTradeDateOrContractMonth = spec.expiry
    c.strike = float(spec.strike)
    c.right = spec.right
    c.tradingClass = spec.trading_class
    c.multiplier = spec.multiplier
    return c


def to_ib_combo_bag(legs: list[ComboLegSpec], conid_map: dict[FopSpec | FutSpec, int]):
    """Build an ibapi BAG Contract from a list of leg specs.

    `conid_map` must contain a conId for every leg's instrument. Resolve via
    contract_details first.
    """
    from ibapi.contract import Contract, ComboLeg

    if not legs:
        raise ValueError("combo must have at least one leg")
    first = legs[0].instrument
    bag = Contract()
    bag.symbol = first.symbol
    bag.secType = "BAG"
    bag.exchange = first.exchange
    bag.currency = first.currency

    bag.comboLegs = []
    for leg in legs:
        if leg.instrument not in conid_map:
            raise KeyError(f"missing conId for {leg.instrument}")
        cl = ComboLeg()
        cl.conId = conid_map[leg.instrument]
        cl.ratio = leg.ratio
        cl.action = leg.action
        cl.exchange = leg.instrument.exchange
        bag.comboLegs.append(cl)
    return bag


# ---------------------------------------------------------------------------
# Convenience builders for common structures
# ---------------------------------------------------------------------------
def iron_condor_legs(
    trading_class: str,
    expiry: str,
    short_put_k: float,
    long_put_k: float,
    short_call_k: float,
    long_call_k: float,
    qty: int = 1,
) -> list[ComboLegSpec]:
    """Standard short iron condor: collect credit, profit if underlying stays in body."""
    return [
        ComboLegSpec(FopSpec("CL", trading_class, expiry, short_put_k, "P"), qty, "SELL"),
        ComboLegSpec(FopSpec("CL", trading_class, expiry, long_put_k, "P"), qty, "BUY"),
        ComboLegSpec(FopSpec("CL", trading_class, expiry, short_call_k, "C"), qty, "SELL"),
        ComboLegSpec(FopSpec("CL", trading_class, expiry, long_call_k, "C"), qty, "BUY"),
    ]


def call_butterfly_legs(
    trading_class: str,
    expiry: str,
    lower_k: float,
    body_k: float,
    upper_k: float,
    qty: int = 1,
) -> list[ComboLegSpec]:
    """Long call butterfly: pay debit, max profit at body strike."""
    return [
        ComboLegSpec(FopSpec("CL", trading_class, expiry, lower_k, "C"), qty, "BUY"),
        ComboLegSpec(FopSpec("CL", trading_class, expiry, body_k, "C"), qty * 2, "SELL"),
        ComboLegSpec(FopSpec("CL", trading_class, expiry, upper_k, "C"), qty, "BUY"),
    ]


def put_debit_spread_legs(
    trading_class: str,
    expiry: str,
    long_k: float,
    short_k: float,
    qty: int = 1,
) -> list[ComboLegSpec]:
    """Bear put debit spread."""
    return [
        ComboLegSpec(FopSpec("CL", trading_class, expiry, long_k, "P"), qty, "BUY"),
        ComboLegSpec(FopSpec("CL", trading_class, expiry, short_k, "P"), qty, "SELL"),
    ]
