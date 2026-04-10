"""Black-76 pricing, IV solver, greeks, RND extraction.

All futures-options math. No I/O. No state.
"""

from __future__ import annotations

from math import erf, log, sqrt

import numpy as np


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + erf(x / sqrt(2.0)))


def _norm_pdf(x: float) -> float:
    return (1.0 / sqrt(2.0 * 3.14159265358979323846)) * np.exp(-0.5 * x * x)


def black76_call(F: float, K: float, T: float, sigma: float, r: float = 0.0) -> float:
    """Black-76 call price for European futures option."""
    if T <= 0 or sigma <= 0 or F <= 0 or K <= 0:
        return max(F - K, 0.0)
    d1 = (log(F / K) + 0.5 * sigma * sigma * T) / (sigma * sqrt(T))
    d2 = d1 - sigma * sqrt(T)
    return float(np.exp(-r * T) * (F * _norm_cdf(d1) - K * _norm_cdf(d2)))


def black76_put(F: float, K: float, T: float, sigma: float, r: float = 0.0) -> float:
    if T <= 0 or sigma <= 0 or F <= 0 or K <= 0:
        return max(K - F, 0.0)
    d1 = (log(F / K) + 0.5 * sigma * sigma * T) / (sigma * sqrt(T))
    d2 = d1 - sigma * sqrt(T)
    return float(np.exp(-r * T) * (K * _norm_cdf(-d2) - F * _norm_cdf(-d1)))


def implied_vol(
    target_price: float,
    F: float,
    K: float,
    T: float,
    right: str,
    r: float = 0.0,
    tol: float = 1e-5,
    max_iter: int = 100,
) -> float:
    """Bisection IV solver. Returns NaN if no solution found."""
    pricer = black76_call if right == "C" else black76_put
    lo, hi = 1e-4, 5.0
    for _ in range(max_iter):
        mid = 0.5 * (lo + hi)
        px = pricer(F, K, T, mid, r)
        if abs(px - target_price) < tol:
            return mid
        if px > target_price:
            hi = mid
        else:
            lo = mid
    return float("nan")


def greeks(
    F: float,
    K: float,
    T: float,
    sigma: float,
    right: str,
    r: float = 0.0,
) -> dict[str, float]:
    """Compute Black-76 greeks. Returns dict with delta, gamma, vega, theta."""
    if T <= 0 or sigma <= 0 or F <= 0 or K <= 0:
        return {"delta": 0.0, "gamma": 0.0, "vega": 0.0, "theta": 0.0}
    d1 = (log(F / K) + 0.5 * sigma * sigma * T) / (sigma * sqrt(T))
    d2 = d1 - sigma * sqrt(T)
    disc = float(np.exp(-r * T))
    pdf = _norm_pdf(d1)

    if right == "C":
        delta = disc * _norm_cdf(d1)
    else:
        delta = -disc * _norm_cdf(-d1)

    gamma = disc * pdf / (F * sigma * sqrt(T))
    vega = F * disc * pdf * sqrt(T) / 100.0   # per 1 vol pt
    theta_per_year = -(F * disc * pdf * sigma) / (2.0 * sqrt(T))
    theta = theta_per_year / 365.0

    return {"delta": delta, "gamma": gamma, "vega": vega, "theta": theta}


def atm_straddle_brenner_subrahmanyam(F: float, sigma: float, T: float) -> float:
    """Approximate ATM straddle price (no need for B-76 evaluation)."""
    if F <= 0 or sigma <= 0 or T <= 0:
        return float("nan")
    return 0.8 * F * sigma * sqrt(T)


def extract_rnd_from_smile(
    strikes: np.ndarray,
    ivs: np.ndarray,
    F: float,
    T: float,
    n_grid: int = 400,
) -> tuple[np.ndarray, np.ndarray]:
    """Breeden-Litzenberger RND extraction via cubic-spline-smoothed call prices.

    Returns (K_grid, density). Density is normalized to integrate to 1.
    """
    from scipy.interpolate import CubicSpline

    if len(strikes) < 5 or T <= 0:
        return np.array([]), np.array([])

    order = np.argsort(strikes)
    K = strikes[order]
    sig = ivs[order]
    iv_spline = CubicSpline(K, sig)
    K_grid = np.linspace(K[0], K[-1], n_grid)
    iv_grid = np.clip(iv_spline(K_grid), 0.01, 5.0)
    call_grid = np.array([black76_call(F, k, T, s) for k, s in zip(K_grid, iv_grid)])
    dK = K_grid[1] - K_grid[0]
    d2C = np.zeros_like(call_grid)
    d2C[1:-1] = (call_grid[2:] - 2 * call_grid[1:-1] + call_grid[:-2]) / (dK * dK)
    d2C = np.clip(d2C, 0, None)
    area = float(np.trapezoid(d2C, K_grid))
    if area > 0:
        d2C /= area
    return K_grid, d2C
