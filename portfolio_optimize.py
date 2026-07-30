"""Rebalance suggestions for an account's current holdings, powered by skfolio.

Reuses portfolio_backtest's Yahoo history cache and position snapshot so this
stays consistent with the existing backtest data path. Suggests target weights
for the account's current universe only — it does not propose adding symbols
the account doesn't already hold.
"""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd

from portfolio_backtest import YahooHistoryCache, _snapshot

METHODS = ("min_variance", "risk_parity", "equal_weight")
DEFAULT_LOOKBACK_DAYS = 2 * 365


def _load_returns(conn, account, start_iso, end_iso, cache):
    cash, positions = _snapshot(conn, account)
    eligible = []
    skipped = []
    for position in positions:
        if position["asset_class"] != "spot":
            skipped.append(
                {
                    "symbol": position["symbol"],
                    "reason": (
                        f"{position['asset_class']} positions are not supported; "
                        "only unlevered spot holdings are eligible"
                    ),
                }
            )
        elif position["qty"] <= 0:
            skipped.append(
                {
                    "symbol": position["symbol"],
                    "reason": "short and zero-quantity positions are not supported",
                }
            )
        else:
            eligible.append(position)
    if not eligible:
        return cash, eligible, skipped, None

    closes = cache.daily_closes(
        [p["symbol"] for p in eligible], start_iso, end_iso
    )
    series_by_symbol = {}
    for p in eligible:
        raw = closes.get(p["symbol"]) or {}
        if len(raw) < 20:
            skipped.append(
                {"symbol": p["symbol"], "reason": "fewer than 20 daily closes"}
            )
            continue
        series_by_symbol[p["symbol"]] = pd.Series(raw)
        series_by_symbol[p["symbol"]].index = pd.to_datetime(
            series_by_symbol[p["symbol"]].index
        )

    usable = [p for p in eligible if p["symbol"] in series_by_symbol]
    if len(usable) < 2:
        return cash, usable, skipped, None

    prices = pd.concat(series_by_symbol, axis=1, join="inner").sort_index().dropna()
    if len(prices) < 20:
        return cash, usable, skipped, None

    returns = prices.pct_change().dropna()
    return cash, usable, skipped, returns


def suggest_rebalance(
    conn,
    account,
    *,
    method="min_variance",
    lookback_days=DEFAULT_LOOKBACK_DAYS,
    history_fn=None,
):
    """Suggest target weights for the account's current symbol universe.

    Compares current market-value weights against a skfolio-optimized target
    (min-variance, risk-parity, or equal-weight) over trailing daily returns.
    Long-only, no leverage, no new symbols — a reallocation of the currently
    invested sleeve, not a stock-picking or entry/exit recommendation. Existing
    cash is preserved as cash rather than silently allocated.
    """
    if method not in METHODS:
        raise SystemExit(f"method must be one of {', '.join(METHODS)}")
    try:
        lookback_days = int(lookback_days)
    except (TypeError, ValueError):
        raise SystemExit("lookback_days must be an integer") from None
    if lookback_days < 20 or lookback_days > 36_500:
        raise SystemExit("lookback_days must be between 20 and 36500")

    end_date = date.today()
    start_date = end_date - timedelta(days=lookback_days)
    start_iso, end_iso = start_date.isoformat(), end_date.isoformat()

    result = {
        "account": account,
        "method": method,
        "start": start_iso,
        "end": end_iso,
        "status": "no_positions",
        "skipped": [],
        "current_weights": {},
        "target_weights": {},
        "cash_policy": "preserve",
        "weight_scope": "eligible_long_spot_positions_plus_cash",
        "message": None,
    }

    cache = YahooHistoryCache(loader=history_fn)
    cash, usable, skipped, returns = _load_returns(
        conn, account, start_iso, end_iso, cache
    )
    result["skipped"] = skipped
    if not usable:
        result["message"] = "No open equity/ETF positions to optimize."
        return result
    if returns is None:
        result["status"] = "no_data"
        result["message"] = (
            "Fewer than 2 positions had enough overlapping history "
            "(need 2+ symbols, 20+ common daily closes)."
        )
        return result

    symbols = list(returns.columns)
    # market value uses the latest close in the returns window, not live quotes,
    # so target weights are comparable to what the optimizer actually saw.
    closes = {sym: cache.history(sym, start_iso, end_iso) for sym in symbols}
    mv = {}
    for p in usable:
        sym = p["symbol"]
        if sym not in closes or closes[sym].empty:
            continue
        px = float(closes[sym].iloc[-1])
        mv[sym] = p["qty"] * p["multiplier"] * px
    invested = sum(mv.values())
    total = cash + invested
    if total <= 0 or cash < 0:
        result["status"] = "unsupported_leverage"
        result["message"] = (
            "Rebalancing requires non-negative cash and positive account equity."
        )
        return result
    cash_weight = cash / total
    result["current_weights"] = {
        **{sym: round(v / total, 4) for sym, v in mv.items()},
        "CASH": round(cash_weight, 4),
    }

    try:
        target = _optimize(returns[list(mv.keys())], method)
    except Exception as exc:
        result["status"] = "engine_error"
        result["message"] = f"skfolio failed: {type(exc).__name__}: {exc}"
        return result

    investable_weight = 1.0 - cash_weight
    result["target_weights"] = {
        **{
            sym: round(float(weight) * investable_weight, 4)
            for sym, weight in target.items()
        },
        "CASH": round(cash_weight, 4),
    }
    result["status"] = "ok_partial" if skipped else "ok"
    result["message"] = (
        f"Suggested {method.replace('_', ' ')} reallocation across "
        f"{len(target)} eligible holding(s) in '{account}'."
    )
    if skipped:
        result["message"] += (
            " Skipped positions are excluded from both current and target weights."
        )
    result["drift"] = {
        sym: round(
            result["target_weights"].get(sym, 0.0)
            - result["current_weights"].get(sym, 0.0),
            4,
        )
        for sym in set(result["target_weights"]) | set(result["current_weights"])
    }
    return result


def _optimize(returns, method):
    if method == "equal_weight":
        from skfolio.optimization import EqualWeighted

        model = EqualWeighted()
    elif method == "risk_parity":
        if returns.shape[1] < 3:
            # HRP's cluster-count search needs 3+ assets; inverse-volatility
            # weighting is the standard risk-parity fallback for pairs.
            from skfolio.optimization import InverseVolatility

            model = InverseVolatility()
        else:
            from skfolio.optimization import HierarchicalRiskParity
            from skfolio.distance import PearsonDistance

            model = HierarchicalRiskParity(distance_estimator=PearsonDistance())
    else:
        from skfolio.optimization import MeanRisk, ObjectiveFunction

        model = MeanRisk(objective_function=ObjectiveFunction.MINIMIZE_RISK)

    model.fit(returns)
    weights = model.weights_
    return dict(zip(returns.columns, weights))
