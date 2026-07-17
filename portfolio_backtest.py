"""Portfolio-aware historical backtests powered by backtesting.py.

The simulation is intentionally a current-holdings retrospective, not a claim
that the portfolio actually held today's positions throughout history.  It
reads the selected account and its open positions from SQLite, builds a
synthetic daily portfolio series, and lets backtesting.py apply a buy-and-hold
execution model and transaction costs to that series.
"""

from __future__ import annotations

import contextlib
import io
import logging
import math
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from typing import Callable

logging.getLogger("numexpr").setLevel(logging.WARNING)
import pandas as pd  # noqa: E402  (silence import-time logging before pandas loads)


HistoryFn = Callable[[str, str, str], object]


def _iso_date(value, name):
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        raise SystemExit(f"{name} must be YYYY-MM-DD") from None


def _default_history(symbol, start, end):
    """Adjusted daily closes with an inclusive end date."""
    import logging

    import yfinance as yf

    logging.getLogger("yfinance").setLevel(logging.CRITICAL)
    exclusive_end = (_iso_date(end, "end") + timedelta(days=1)).isoformat()
    history = yf.Ticker(symbol).history(
        start=start,
        end=exclusive_end,
        interval="1d",
        auto_adjust=True,
        actions=False,
    )
    return history["Close"] if "Close" in history else pd.Series(dtype=float)


def _clean_history(raw, symbol, start, end):
    if isinstance(raw, pd.DataFrame):
        if "Close" not in raw:
            return pd.Series(dtype=float, name=symbol)
        raw = raw["Close"]
    elif isinstance(raw, dict):
        raw = pd.Series(raw)
    elif not isinstance(raw, pd.Series):
        try:
            raw = pd.Series(raw)
        except Exception:
            return pd.Series(dtype=float, name=symbol)

    series = pd.to_numeric(raw, errors="coerce")
    index = pd.to_datetime(series.index, errors="coerce", utc=True)
    valid = ~index.isna()
    series = series.loc[valid].copy()
    index = index[valid].tz_convert(None).normalize()
    series.index = index
    series = series.groupby(level=0).last().sort_index()
    series = series.loc[pd.Timestamp(start) : pd.Timestamp(end)]
    series = series[series.map(lambda value: math.isfinite(value) and value > 0)]
    series.name = symbol
    return series.astype(float)


class YahooHistoryCache:
    """Request-scoped adjusted-close cache shared by graphs and backtests."""

    def __init__(self, loader=None):
        self._loader = loader or _default_history
        self._cache = {}
        self._lock = threading.Lock()

    def history(self, symbol, start, end):
        start_date = _iso_date(start, "start")
        end_date = _iso_date(end, "end")
        with self._lock:
            cached = self._cache.get(symbol)
            if cached and cached[0] <= start_date and cached[1] >= end_date:
                return cached[2].loc[pd.Timestamp(start) : pd.Timestamp(end)].copy()
            fetch_start = min(start_date, cached[0]) if cached else start_date
            fetch_end = max(end_date, cached[1]) if cached else end_date

        raw = self._loader(symbol, fetch_start.isoformat(), fetch_end.isoformat())
        series = _clean_history(
            raw, symbol, fetch_start.isoformat(), fetch_end.isoformat()
        )
        with self._lock:
            existing = self._cache.get(symbol)
            if existing:
                series = pd.concat([existing[2], series])
                series = series.loc[~series.index.duplicated(keep="last")].sort_index()
                fetch_start = min(fetch_start, existing[0])
                fetch_end = max(fetch_end, existing[1])
            self._cache[symbol] = (fetch_start, fetch_end, series)
        return series.loc[pd.Timestamp(start) : pd.Timestamp(end)].copy()

    def daily_closes(self, symbols, start, end, exclude=None):
        """Return papertrade's date-to-close shape, fetching symbols concurrently."""
        ordered = list(dict.fromkeys(symbols))

        def fetch(symbol):
            if exclude and exclude(symbol):
                return symbol, {}
            try:
                series = self.history(symbol, start, end)
                return symbol, {
                    stamp.date().isoformat(): float(value)
                    for stamp, value in series.items()
                }
            except Exception:
                return symbol, {}

        if len(ordered) < 2:
            return dict(fetch(symbol) for symbol in ordered)
        with ThreadPoolExecutor(max_workers=min(8, len(ordered))) as executor:
            return dict(executor.map(fetch, ordered))


def _snapshot(conn, account):
    rows = conn.execute(
        "SELECT a.cash,p.symbol,p.qty,p.avg_cost,p.mult,p.asset_class,p.margin "
        "FROM accounts a LEFT JOIN positions p ON p.account=a.name "
        "WHERE a.name=? ORDER BY p.symbol",
        (account,),
    ).fetchall()
    if not rows:
        raise SystemExit(f"no account '{account}'")
    cash = float(rows[0][0])
    positions = [
        {
            "symbol": symbol,
            "qty": float(qty),
            "avg_cost": float(avg_cost),
            "multiplier": float(multiplier),
            "asset_class": asset_class,
            "margin": float(margin),
        }
        for _, symbol, qty, avg_cost, multiplier, asset_class, margin in rows
        if symbol is not None and abs(float(qty)) > 1e-12
    ]
    return cash, positions


def _base_result(account, start, end, lookback_days, commission):
    return {
        "account": account,
        "engine": "backtesting.py",
        "engine_version": None,
        "status": "no_positions",
        "hypothesis": (
            "Today's open quantities and current cash were held unchanged over "
            "the historical window."
        ),
        "universe_source": (
            "Open positions in the existing SQLite positions table for the "
            f"selected account '{account}'."
        ),
        "start": start,
        "end": end,
        "lookback_days": lookback_days,
        "commission_bps": commission * 10_000,
        "bars": 0,
        "symbols": [],
        "skipped": [],
        "curve": [],
        "metrics": {},
        "warnings": [
            "Current holdings are known today, so this retrospective has "
            "look-ahead and survivorship bias and is not an out-of-sample strategy test.",
            "Cash earns no interest; taxes, borrow fees, funding, slippage, and "
            "market impact are not modeled.",
        ],
    }


def _finite_number(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _run_engine(portfolio_values, commission):
    try:
        import backtesting
        from backtesting import Strategy
        from backtesting.lib import FractionalBacktest
    except ImportError:
        raise SystemExit(
            "backtesting.py is not installed — run: pip install -r requirements.txt"
        ) from None

    class HoldCurrentPortfolio(Strategy):
        def init(self):
            self._entered = False

        def next(self):
            if not self._entered:
                self.buy(size=0.9999, tag="current-portfolio")
                self._entered = True

    first_date, last_date = portfolio_values.index[0], portfolio_values.index[-1]
    prices = portfolio_values.rename("Close").to_frame()
    prices["Open"] = prices["Close"]
    prices["High"] = prices["Close"]
    prices["Low"] = prices["Close"]
    prices["Volume"] = 0.0
    prices = prices[["Open", "High", "Low", "Close", "Volume"]]

    # backtesting.py begins calling Strategy.next() on bar 2 and reserves the
    # last bar to finalize open trades. Flat padding lets the trade span the
    # complete real data window without fabricating a return.
    before = prices.iloc[[0]].copy()
    before.index = [first_date - pd.Timedelta(days=1)]
    after = prices.iloc[[-1]].copy()
    after.index = [last_date + pd.Timedelta(days=1)]
    padded = pd.concat([before, prices, after])

    engine = FractionalBacktest(
        padded,
        HoldCurrentPortfolio,
        cash=float(portfolio_values.iloc[0]),
        commission=commission,
        trade_on_close=True,
        exclusive_orders=True,
        finalize_trades=True,
    )
    with (
        contextlib.redirect_stdout(io.StringIO()),
        contextlib.redirect_stderr(io.StringIO()),
    ):
        stats = engine.run()

    raw_curve = stats["_equity_curve"]["Equity"]
    actual_curve = raw_curve.reindex(portfolio_values.index).astype(float)
    # Attribute the exit commission from the flat padding bar to the final real
    # date so the displayed curve reconciles to backtesting.py's final equity.
    actual_curve.iloc[-1] = float(stats["Equity Final [$]"])
    return backtesting.__version__, stats, actual_curve


def run_portfolio_backtest(
    conn,
    account,
    *,
    start=None,
    end=None,
    lookback_days=730,
    commission=0.001,
    history_fn: HistoryFn | None = None,
):
    """Backtest the selected database portfolio and return JSON-safe results.

    ``history_fn`` is injectable for deterministic/offline tests and receives
    ``(symbol, start_iso, end_iso)``.  It may return a Close Series, a DataFrame
    containing Close, or a date-to-close mapping.
    """
    try:
        lookback_days = int(lookback_days)
    except (TypeError, ValueError):
        raise SystemExit("lookback_days must be an integer") from None
    if lookback_days < 2 or lookback_days > 36_500:
        raise SystemExit("lookback_days must be between 2 and 36500")
    try:
        commission = float(commission)
    except (TypeError, ValueError):
        raise SystemExit("commission must be a number") from None
    if not math.isfinite(commission) or commission < 0 or commission > 0.1:
        raise SystemExit("commission must be between 0 and 0.1")

    end_date = _iso_date(end, "end") if end else date.today()
    start_date = (
        _iso_date(start, "start") if start else end_date - timedelta(days=lookback_days)
    )
    if start_date >= end_date:
        raise SystemExit("start must be before end")
    start_iso, end_iso = start_date.isoformat(), end_date.isoformat()
    result = _base_result(account, start_iso, end_iso, lookback_days, commission)

    cash, positions = _snapshot(conn, account)
    if not positions:
        result["message"] = "No open positions in this portfolio to backtest."
        return result

    eligible = []
    for position in positions:
        if position["asset_class"] == "option":
            result["skipped"].append(
                {
                    "symbol": position["symbol"],
                    "reason": "options do not have a reliable continuous historical chain",
                }
            )
        else:
            eligible.append(position)
    if result["skipped"]:
        result["warnings"].append(
            "Options were omitted because reliable point-in-time option-chain history is unavailable."
        )

    loader = history_fn or _default_history

    def load(position):
        symbol = position["symbol"]
        try:
            raw = loader(symbol, start_iso, end_iso)
            series = _clean_history(raw, symbol, start_iso, end_iso)
            if len(series) < 3:
                return position, None, "fewer than 3 valid daily closes"
            return position, series, None
        except Exception as exc:
            return position, None, f"history unavailable ({type(exc).__name__})"

    if history_fn is None and len(eligible) > 1:
        with ThreadPoolExecutor(max_workers=min(8, len(eligible))) as executor:
            loaded = list(executor.map(load, eligible))
    else:
        loaded = [load(position) for position in eligible]

    usable = []
    for position, series, reason in loaded:
        if reason:
            result["skipped"].append({"symbol": position["symbol"], "reason": reason})
        else:
            usable.append((position, series))
    if not usable:
        result["status"] = "no_data"
        result["message"] = "No eligible positions had enough overlapping history."
        return result

    prices = pd.concat([series for _, series in usable], axis=1, join="inner").dropna()
    prices = prices.loc[~prices.index.duplicated(keep="last")].sort_index()
    if len(prices) < 3:
        result["status"] = "no_data"
        result["message"] = (
            "Portfolio positions have fewer than 3 overlapping daily closes."
        )
        return result

    portfolio_values = pd.Series(float(cash), index=prices.index, dtype=float)
    components = []
    for position, _ in usable:
        symbol = position["symbol"]
        series = prices[symbol]
        qty = position["qty"]
        multiplier = position["multiplier"]
        if position["asset_class"] == "future":
            contribution = position["margin"] + qty * multiplier * (
                series - position["avg_cost"]
            )
        else:
            contribution = qty * multiplier * series
        portfolio_values = portfolio_values.add(contribution, fill_value=0.0)
        components.append(
            {
                **position,
                "history_start": prices.index[0].date().isoformat(),
                "history_end": prices.index[-1].date().isoformat(),
                "start_price": float(series.iloc[0]),
                "end_price": float(series.iloc[-1]),
                "start_value": float(contribution.iloc[0]),
                "end_value": float(contribution.iloc[-1]),
            }
        )

    finite = portfolio_values.map(math.isfinite)
    if not finite.all() or (portfolio_values <= 0).any():
        result["status"] = "invalid_equity"
        result["message"] = (
            "The historical current-holdings proxy reached zero or negative equity; "
            "backtesting.py requires a positive synthetic price series."
        )
        result["symbols"] = components
        return result

    try:
        version, stats, engine_curve = _run_engine(portfolio_values, commission)
    except SystemExit:
        raise
    except Exception as exc:
        result["status"] = "engine_error"
        result["message"] = f"backtesting.py failed: {type(exc).__name__}: {exc}"
        result["symbols"] = components
        return result

    daily_returns = engine_curve.pct_change().dropna()
    positive_days = (
        float((daily_returns > 0).sum() / len(daily_returns) * 100)
        if len(daily_returns)
        else 0.0
    )
    metric_keys = {
        "return_pct": "Return [%]",
        "annual_return_pct": "Return (Ann.) [%]",
        "cagr_pct": "CAGR [%]",
        "annual_volatility_pct": "Volatility (Ann.) [%]",
        "sharpe": "Sharpe Ratio",
        "sortino": "Sortino Ratio",
        "calmar": "Calmar Ratio",
        "max_drawdown_pct": "Max. Drawdown [%]",
        "exposure_time_pct": "Exposure Time [%]",
        "commissions": "Commissions [$]",
        "trades": "# Trades",
    }
    metrics = {
        name: _finite_number(stats.get(stat_name))
        for name, stat_name in metric_keys.items()
    }
    metrics.update(
        {
            "initial_equity": float(portfolio_values.iloc[0]),
            "final_equity": _finite_number(stats.get("Equity Final [$]")),
            "peak_equity": _finite_number(stats.get("Equity Peak [$]")),
            "positive_days_pct": positive_days,
            "gross_hold_return_pct": float(
                (portfolio_values.iloc[-1] / portfolio_values.iloc[0] - 1) * 100
            ),
        }
    )

    result.update(
        {
            "engine_version": version,
            "status": "ok",
            "start": prices.index[0].date().isoformat(),
            "end": prices.index[-1].date().isoformat(),
            "bars": len(prices),
            "symbols": components,
            "curve": [
                {"date": timestamp.date().isoformat(), "equity": float(value)}
                for timestamp, value in engine_curve.items()
            ],
            "metrics": metrics,
            "message": (
                f"Backtested {len(components)} current position(s) from account "
                f"'{account}' over {len(prices)} common daily bars."
            ),
        }
    )
    if any(position["asset_class"] == "future" for position, _ in usable):
        result["warnings"].append(
            "Futures use Yahoo's continuous series and current contract margin; "
            "roll costs and changing margin requirements are not modeled."
        )
    result["warnings"].append(
        "The configured commission is charged on entry and exit of the synthetic "
        "portfolio; constituent-level spreads and fees are not modeled."
    )
    return result
