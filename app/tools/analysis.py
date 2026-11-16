"""Pure, deterministic financial computation.

These functions are deliberately free of I/O: they take raw numbers and return
computed values. The LLM never performs these calculations; tools call these
functions and attach provenance to the result.
"""

from __future__ import annotations

from typing import Any

import numpy as np


def _valid_positive_prices(closes: list[float]) -> np.ndarray | None:
    """Return finite, strictly positive prices or ``None`` when unavailable.

    Providers may preserve a literal zero, but price-based indicators cannot
    assign it a mathematically meaningful return, ratio, or drawdown. Treat it
    the same as other unavailable input at the computation boundary.
    """
    if not closes:
        return None
    try:
        values = np.asarray(closes, dtype=float)
    except (TypeError, ValueError):
        return None
    if values.ndim != 1 or not np.all(np.isfinite(values)) or np.any(values <= 0):
        return None
    return values


def compute_ma(closes: list[float], window: int) -> float | None:
    """Latest simple moving average."""
    if not closes or window <= 0 or len(closes) < window:
        return None
    values = _valid_positive_prices(closes[-window:])
    if values is None:
        return None
    value = float(np.mean(values))
    return value if np.isfinite(value) else None


def compute_rsi(closes: list[float], period: int = 14) -> float | None:
    """Latest Relative Strength Index (Wilder smoothing)."""
    if len(closes) < period + 1 or period <= 0:
        return None
    values = _valid_positive_prices(closes)
    if values is None:
        return None
    deltas = np.diff(values)
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
    avg_gain = float(gains[:period].mean())
    avg_loss = float(losses[:period].mean())
    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    value = float(100 - 100 / (1 + rs))
    return value if np.isfinite(value) else None


def compute_rsi_series(closes: list[float], period: int = 14) -> list[float | None]:
    """Full RSI series (Wilder smoothing); ``None`` before enough history exists."""
    n = len(closes)
    result: list[float | None] = [None] * n
    if n <= period or period <= 0:
        return result
    values = _valid_positive_prices(closes)
    if values is None:
        return result
    deltas = np.diff(values)
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
    avg_gain = float(gains[:period].mean())
    avg_loss = float(losses[:period].mean())

    def _rsi(gain: float, loss: float) -> float:
        if loss == 0:
            return 100.0
        value = float(100 - 100 / (1 + gain / loss))
        return value if np.isfinite(value) else 100.0

    result[period] = _rsi(avg_gain, avg_loss)
    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        result[i + 1] = _rsi(avg_gain, avg_loss)
    return result


def _ema(values: list[float], span: int) -> list[float]:
    alpha = 2.0 / (span + 1.0)
    result = [values[0]]
    for value in values[1:]:
        result.append(alpha * value + (1 - alpha) * result[-1])
    return result


def compute_macd(
    closes: list[float], fast: int = 12, slow: int = 26, signal: int = 9
) -> dict[str, float] | None:
    """Latest MACD line, signal line and histogram."""
    if len(closes) < slow + signal:
        return None
    values = _valid_positive_prices(closes)
    if values is None:
        return None
    prices = values.tolist()
    ema_fast = _ema(prices, fast)
    ema_slow = _ema(prices, slow)
    macd_line = [f - s for f, s in zip(ema_fast, ema_slow)]
    signal_line = _ema(macd_line, signal)
    result = (macd_line[-1], signal_line[-1], macd_line[-1] - signal_line[-1])
    if not all(np.isfinite(value) for value in result):
        return None
    return {"macd": round(result[0], 6), "signal": round(result[1], 6), "histogram": round(result[2], 6)}


def compute_macd_series(
    closes: list[float], fast: int = 12, slow: int = 26, signal: int = 9
) -> tuple[list[float], list[float], list[float]]:
    """Full MACD line, signal line and histogram series."""
    values = _valid_positive_prices(closes)
    if values is None:
        return [], [], []
    prices = values.tolist()
    ema_fast = _ema(prices, fast)
    ema_slow = _ema(prices, slow)
    macd_line = [f - s for f, s in zip(ema_fast, ema_slow)]
    signal_line = _ema(macd_line, signal)
    histogram = [m - s for m, s in zip(macd_line, signal_line)]
    return macd_line, signal_line, histogram


def compute_annualized_volatility(closes: list[float], window: int | None = None) -> float | None:
    """Annualized volatility of daily (log-free) returns."""
    values = closes[-window:] if window else closes
    if len(values) < 2:
        return None
    arr = _valid_positive_prices(values)
    if arr is None:
        return None
    returns = np.diff(arr) / arr[:-1]
    value = float(np.std(returns, ddof=1) * np.sqrt(252))
    return value if np.isfinite(value) else None


def compute_drawdown(closes: list[float]) -> dict[str, float] | None:
    """Max drawdown (peak-to-trough) and current drawdown, as positive fractions."""
    values = _valid_positive_prices(closes)
    if values is None:
        return None
    peak = float(values[0])
    max_dd = 0.0
    for price in values:
        peak = max(peak, price)
        max_dd = max(max_dd, (peak - price) / peak)
    current_dd = (peak - values[-1]) / peak
    if not np.isfinite(max_dd) or not np.isfinite(current_dd):
        return None
    return {"max_drawdown": round(max_dd, 6), "current_drawdown": round(current_dd, 6)}


def project_linear_trend(
    closes: list[float], horizon: int = 20, window: int | None = None
) -> list[float]:
    """Naive linear-regression extrapolation of recent closes.

    This is a *statistical illustration*, not a prediction. Past price behaviour
    does not predict future returns.
    """
    if len(closes) < 2 or horizon <= 0:
        return []
    data = closes[-window:] if window else closes
    if len(data) < 2:
        return []
    x = np.arange(len(data), dtype=float)
    y = np.asarray(data, dtype=float)
    slope, intercept = np.polyfit(x, y, 1)
    future_x = np.arange(len(data), len(data) + horizon, dtype=float)
    return [round(float(intercept + slope * xi), 4) for xi in future_x]


def compute_pe(price: float | None, eps: float | None) -> float | None:
    """Price / earnings. Returns None when EPS is missing or non-positive."""
    if price is None or eps is None or eps <= 0:
        return None
    return round(price / eps, 3)


def compute_ev_ebitda(
    market_cap: float | None,
    total_debt: float | None,
    cash: float | None,
    ebitda: float | None,
) -> float | None:
    """Enterprise value / EBITDA."""
    if market_cap is None or total_debt is None or cash is None or ebitda is None or ebitda <= 0:
        return None
    return round((market_cap + total_debt - cash) / ebitda, 3)


def compute_fcf_yield(free_cash_flow: float | None, market_cap: float | None) -> float | None:
    """Free cash flow yield (FCF / market cap)."""
    if free_cash_flow is None or market_cap is None or market_cap <= 0:
        return None
    return round(free_cash_flow / market_cap, 6)


def compute_dcf(
    free_cash_flow: float | None,
    shares_outstanding: float | None,
    *,
    growth_rate: float = 0.08,
    discount_rate: float = 0.10,
    terminal_growth: float = 0.03,
    years: int = 5,
) -> dict[str, Any] | None:
    """Simple two-stage DCF. Returns intrinsic value per share plus assumptions.

    This intentionally ignores net-debt adjustments and uses FCF directly as the
    cash-flow proxy — a simplification that must be disclosed to the user.
    """
    if (
        free_cash_flow is None
        or shares_outstanding is None
        or free_cash_flow <= 0
        or shares_outstanding <= 0
        or discount_rate <= terminal_growth
        or years <= 0
    ):
        return None

    pv_sum = 0.0
    cash_flow = free_cash_flow
    for year in range(1, years + 1):
        cash_flow = free_cash_flow * (1 + growth_rate) ** year
        pv_sum += cash_flow / (1 + discount_rate) ** year

    terminal_value = (
        cash_flow * (1 + terminal_growth) / (discount_rate - terminal_growth)
    ) / (1 + discount_rate) ** years
    enterprise_value = pv_sum + terminal_value
    per_share = enterprise_value / shares_outstanding

    return {
        "intrinsic_value_per_share": round(per_share, 2),
        "assumptions": {
            "free_cash_flow": round(free_cash_flow, 2),
            "growth_rate": growth_rate,
            "discount_rate": discount_rate,
            "terminal_growth": terminal_growth,
            "projection_years": years,
            "note": "Simplified DCF: uses FCF as cash-flow proxy and ignores net-debt adjustments.",
        },
    }
