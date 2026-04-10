"""Tests for core/pricing.py — Black-76, IV solver, greeks."""

import math

import pytest

from core.pricing import black76_call, black76_put, implied_vol, greeks


def test_black76_call_atm():
    # ATM call with 50% vol, 30 DTE
    px = black76_call(F=100, K=100, T=30/365, sigma=0.50)
    assert 4.5 < px < 6.5


def test_black76_put_call_parity():
    F, K, T, sigma = 100, 95, 30/365, 0.40
    c = black76_call(F, K, T, sigma)
    p = black76_put(F, K, T, sigma)
    # C - P = F - K (for futures options at r=0)
    assert abs((c - p) - (F - K)) < 0.05


def test_implied_vol_roundtrip():
    F, K, T, sigma = 100, 100, 30/365, 0.40
    px = black76_call(F, K, T, sigma)
    iv = implied_vol(px, F, K, T, "C")
    assert abs(iv - sigma) < 0.005


def test_greeks_atm_call():
    g = greeks(F=100, K=100, T=30/365, sigma=0.50, right="C")
    assert 0.4 < g["delta"] < 0.6   # ATM call delta ≈ 0.5
    assert g["gamma"] > 0
    assert g["vega"] > 0
    assert g["theta"] < 0


def test_greeks_atm_put():
    g = greeks(F=100, K=100, T=30/365, sigma=0.50, right="P")
    assert -0.6 < g["delta"] < -0.4
