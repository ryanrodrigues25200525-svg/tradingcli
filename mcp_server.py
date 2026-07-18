#!/usr/bin/env python3
"""MCP server wrapping papertrade — gives Claude Code tool access to the paper broker."""

import contextlib
import functools
import io
import inspect
import json
import os
import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import papertrade as pt
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("papertrade")


def _capture(fn, *args, **kw):
    buf = io.StringIO()
    conn = pt.db()
    try:
        with conn, contextlib.redirect_stdout(buf):
            fn(conn, *args, **kw)
    except SystemExit as e:  # papertrade signals user errors via SystemExit
        return f"error: {e}"
    finally:
        conn.close()
    return buf.getvalue().strip() or "ok"


@mcp.tool()
def account_create(
    name: str,
    cash: float = 100_000,
    idempotency_key: str | None = None,
    agent: str = "mcp",
) -> str:
    """Create an account. Reuse idempotency_key safely when retrying the same mutation."""
    result = _capture(
        pt.create_account,
        name,
        cash,
        source=agent,
        request_id=idempotency_key,
    )
    return f"created '{name}' with {cash:,.2f}" if result == "ok" else result


@mcp.tool()
def account_list() -> str:
    """List all paper accounts and their cash balances."""
    conn = pt.db()
    rows = conn.execute("SELECT name, cash FROM accounts").fetchall()
    conn.close()
    return "\n".join(f"{n}: {c:,.2f}" for n, c in rows) or "no accounts"


@mcp.tool()
def buy(
    account: str,
    symbol: str,
    qty: float,
    limit: float | None = None,
    idempotency_key: str | None = None,
    agent: str = "mcp",
) -> str:
    """Buy shares. Market order fills immediately at the live price; pass limit for a resting limit order."""
    return _capture(
        pt.place,
        account,
        symbol,
        "buy",
        qty,
        limit,
        source=agent,
        request_id=idempotency_key,
    )


@mcp.tool()
def sell(
    account: str,
    symbol: str,
    qty: float,
    limit: float | None = None,
    idempotency_key: str | None = None,
    agent: str = "mcp",
) -> str:
    """Sell shares (selling more than held opens a short). Market order unless limit is given."""
    return _capture(
        pt.place,
        account,
        symbol,
        "sell",
        qty,
        limit,
        source=agent,
        request_id=idempotency_key,
    )


@mcp.tool()
def tick() -> str:
    """Check all pending limit orders against current prices and fill any that crossed."""
    return _capture(pt.tick)


@mcp.tool()
def positions(account: str) -> str:
    """List open positions (symbol, side, qty, avg cost, asset class) for an account."""
    conn = pt.db()
    rows = conn.execute(
        "SELECT symbol, qty, avg_cost, asset_class FROM positions WHERE account=?",
        (account,),
    ).fetchall()
    conn.close()
    return (
        "\n".join(
            f"{s}: {'long' if q > 0 else 'short'} {abs(q):g} @ {a:.2f} [{ac}]"
            for s, q, a, ac in rows
        )
        or "no positions"
    )


@mcp.tool()
def orders(account: str, limit: int = 100, offset: int = 0) -> str:
    """List paginated order history, including source agent and idempotency key."""
    limit, offset = max(1, min(limit, 500)), max(0, offset)
    conn = pt.db()
    rows = conn.execute(
        "SELECT id,ts,side,qty,symbol,order_type,limit_price,stop_price,time_in_force,"
        "status,filled_price,source,request_id,client_order_id,parent_id,order_class,"
        "reject_reason FROM orders WHERE account=? ORDER BY id DESC LIMIT ? OFFSET ?",
        (account, limit, offset),
    ).fetchall()
    conn.close()
    out = []
    for (
        oid,
        ts,
        side,
        qty,
        symbol,
        order_type,
        price_limit,
        stop_price,
        time_in_force,
        status,
        fp,
        source,
        key,
        client_id,
        parent_id,
        order_class,
        reason,
    ) in rows:
        px = (
            f"@{fp:.2f}"
            if fp is not None
            else f"lim {price_limit:.2f}"
            if price_limit is not None
            else f"stop {stop_price:.2f}"
            if stop_price is not None
            else ""
        )
        meta = f" source={source or 'unknown'}" + (f" key={key}" if key else "")
        meta += f" client={client_id}" if client_id else ""
        meta += f" parent={parent_id}" if parent_id else ""
        rejected = f" reason={reason}" if reason else ""
        out.append(
            f"#{oid} {ts} {side} {qty:g} {symbol} {order_type}/{time_in_force} "
            f"{order_class} {px} [{status}]{meta}{rejected}"
        )
    return "\n".join(out) or "no orders"


@mcp.tool()
def pnl(account: str) -> str:
    """Mark-to-market P&L report for an account: positions, unrealized P&L, cash, total equity."""
    return _capture(pt.pnl, account)


@mcp.tool()
def rename_account(
    old: str,
    new: str,
    idempotency_key: str | None = None,
    agent: str = "mcp",
) -> str:
    """Rename a paper portfolio. Updates positions, orders, history, and the default pointer."""
    return _capture(
        pt.rename_account,
        old,
        new,
        source=agent,
        request_id=idempotency_key,
    )


@mcp.tool()
def summary() -> str:
    """One-line-per-portfolio snapshot for all accounts: cash, equity, unrealized P&L, position count.
    Fast overview for deciding which account to act on."""
    conn = pt.db()
    accounts = conn.execute(
        "SELECT name,cash,deposits,realized FROM accounts ORDER BY name"
    ).fetchall()
    positions = conn.execute(
        "SELECT account,symbol,qty,avg_cost,mult,asset_class,margin "
        "FROM positions ORDER BY account,symbol"
    ).fetchall()
    conn.close()
    positions_by_account: dict[str, list[tuple]] = {name: [] for name, *_ in accounts}
    for account, *position in positions:
        positions_by_account.setdefault(account, []).append(position)
    marks = pt.batch_prices([position[1] for position in positions], ignore_errors=True)
    out = []
    for name, cash, dep, real in accounts:
        eq, unreal, npos, approximate = cash, 0.0, 0, False
        for sym, qty, avg, mult, ac, margin in positions_by_account.get(name, []):
            npos += 1
            px = marks.get(sym)
            if px is None:
                # Preserve a useful approximate equity value during a quote outage.
                eq += margin if ac == "future" else qty * mult * avg
                approximate = True
                continue
            u = qty * mult * (px - avg)
            unreal += u
            eq += (u + margin) if ac == "future" else qty * mult * px
        total = real + unreal
        ret = total / dep * 100 if dep else 0.0
        equity_prefix = "~" if approximate else ""
        out.append(
            f"{name}: equity {equity_prefix}{eq:,.2f}  cash {cash:,.2f}  "
            f"unreal {unreal:+,.2f}  "
            f"total {total:+,.2f} ({ret:+.2f}%)  {npos} positions"
        )
    return "\n".join(out) or "no accounts"


@mcp.tool()
def performance(account: str) -> str:
    """Portfolio performance since inception: return, CAGR, Sharpe, Sortino, volatility, max drawdown.
    Reconstructs a daily equity curve from the trade/cashflow ledger and real historical prices."""
    conn = pt.db()
    try:
        curve, m = pt.account_performance(conn, account, live=True)
    finally:
        conn.close()
    if not m:
        return f"{account}: no activity yet"
    spark = pt.sparkline([e for _, e in curve])
    return (
        f"{account}  {m['start']} -> {m['end']} ({m['days']}d)\n{spark}\n"
        f"equity {m['start_eq']:,.0f} -> {m['end_eq']:,.0f}\n"
        f"return {m['total'] * 100:+.2f}%  CAGR {m['cagr'] * 100:+.2f}%\n"
        f"sharpe {m['sharpe']:.2f}  sortino {m['sortino']:.2f}  vol {m['vol'] * 100:.1f}%\n"
        f"maxDD {m['mdd'] * 100:.2f}%  best {m['best'] * 100:+.2f}%  worst {m['worst'] * 100:+.2f}%"
    )


@mcp.tool()
def portfolio_backtest(
    account: str,
    start: str | None = None,
    end: str | None = None,
    lookback_days: int = 1825,
    commission_bps: float = 10.0,
) -> str:
    """Backtest the account's current open positions and cash with backtesting.py.

    The universe is read from this account's SQLite positions, options without
    reliable continuous history are reported as skipped, and results include
    the equity curve, return, CAGR, volatility, Sharpe, Sortino, and drawdown.
    The default window is five years; request up to 36500 calendar days. This
    is a current-holdings retrospective, not an out-of-sample strategy test.
    """
    import portfolio_backtest as pbt

    conn = pt.db()
    try:
        result = pbt.run_portfolio_backtest(
            conn,
            account,
            start=start,
            end=end,
            lookback_days=lookback_days,
            commission=commission_bps / 10_000,
        )
        return json.dumps(result, indent=2)
    except SystemExit as exc:
        return f"error: {exc}"
    finally:
        conn.close()


@mcp.tool()
def buy_option(
    account: str,
    underlying: str,
    expiry: str,
    strike: float,
    kind: str,
    contracts: float,
    limit: float | None = None,
    idempotency_key: str | None = None,
    agent: str = "mcp",
) -> str:
    """Buy an option. expiry is YYYY-MM-DD, kind is 'C' or 'P', contracts x100 shares.
    Market order unless limit (premium) is given. Use option_chain to find valid expiries/strikes."""
    occ = pt.build_occ(underlying, expiry, strike, kind)
    return _capture(
        pt.place,
        account,
        occ,
        "buy",
        contracts,
        limit,
        source=agent,
        request_id=idempotency_key,
    )


@mcp.tool()
def sell_option(
    account: str,
    underlying: str,
    expiry: str,
    strike: float,
    kind: str,
    contracts: float,
    limit: float | None = None,
    idempotency_key: str | None = None,
    agent: str = "mcp",
) -> str:
    """Sell/write an option (opens a short if not covering). Same params as buy_option."""
    occ = pt.build_occ(underlying, expiry, strike, kind)
    return _capture(
        pt.place,
        account,
        occ,
        "sell",
        contracts,
        limit,
        source=agent,
        request_id=idempotency_key,
    )


@mcp.tool()
def option_chain(underlying: str, expiry: str | None = None) -> str:
    """List option expiries for an underlying, or near-the-money strikes for one expiry (YYYY-MM-DD)."""
    import io
    import contextlib as cl

    buf = io.StringIO()
    try:
        with cl.redirect_stdout(buf):
            pt.show_chain(underlying, expiry)
    except SystemExit as e:
        return f"error: {e}"
    return buf.getvalue().strip()


@mcp.tool()
def futures_symbols() -> str:
    """List supported futures symbols with their contract multiplier and initial margin."""
    return "\n".join(
        f"{s}: x{m:g}, margin {mg:,.0f}" for s, (m, mg) in pt.FUTURES.items()
    )


@mcp.tool()
def close_position(
    account: str,
    symbol: str,
    idempotency_key: str | None = None,
    agent: str = "mcp",
) -> str:
    """Flatten an open position at market (sells a long, buys back a short) in one call."""
    return _capture(
        pt.close_position,
        account,
        symbol,
        source=agent,
        request_id=idempotency_key,
    )


@mcp.tool()
def set_default_account(
    name: str,
    idempotency_key: str | None = None,
    agent: str = "mcp",
) -> str:
    """Set the default portfolio used by the CLI and dashboard when no account is specified."""
    return _capture(
        pt.set_default,
        name,
        source=agent,
        request_id=idempotency_key,
    )


@mcp.tool()
def market_status() -> str:
    """Holiday/early-close-aware NYSE status and next open/close time."""
    return json.dumps(pt.market_clock(), indent=2)


@mcp.tool()
def watchlist(symbols: str) -> str:
    """Live quotes for a comma-separated list of symbols you don't necessarily hold (research)."""
    out = []
    for s in [x.strip().upper() for x in symbols.split(",") if x.strip()]:
        try:
            out.append(f"{s}: {pt.live_price(s):.2f}")
        except SystemExit as e:
            out.append(f"{s}: {e}")
    return "\n".join(out) or "no symbols"


@mcp.tool()
def cancel_order(
    order_id: int,
    idempotency_key: str | None = None,
    agent: str = "mcp",
) -> str:
    """Cancel a pending limit order by its id (see the orders tool)."""
    return _capture(
        pt.cancel,
        order_id,
        source=agent,
        request_id=idempotency_key,
    )


@mcp.tool()
def order_cancel(
    order_id: int,
    idempotency_key: str | None = None,
    agent: str = "mcp",
) -> str:
    """Cancel one pending order by id; canonical counterpart to order_cancel_all."""
    return cancel_order(order_id, idempotency_key=idempotency_key, agent=agent)


@mcp.tool()
def delete_account(
    name: str,
    idempotency_key: str | None = None,
    agent: str = "mcp",
) -> str:
    """Permanently delete a paper account and all its positions and order history."""
    return _capture(
        pt.wipe_account,
        name,
        source=agent,
        request_id=idempotency_key,
    )


@mcp.tool()
def reset_account(
    name: str,
    cash: float = 100_000,
    idempotency_key: str | None = None,
    agent: str = "mcp",
) -> str:
    """Wipe an account's positions and order history and restore its cash balance."""
    return _capture(
        pt.wipe_account,
        name,
        reset_cash=cash,
        source=agent,
        request_id=idempotency_key,
    )


@mcp.tool()
def deposit(
    account: str,
    amount: float,
    idempotency_key: str | None = None,
    agent: str = "mcp",
) -> str:
    """Add cash to a paper account."""
    if amount <= 0:
        return "error: deposit amount must be positive"
    return _capture(
        pt.adjust_cash,
        account,
        amount,
        source=agent,
        request_id=idempotency_key,
    )


@mcp.tool()
def withdraw(
    account: str,
    amount: float,
    idempotency_key: str | None = None,
    agent: str = "mcp",
) -> str:
    """Remove cash from a paper account (fails if it would go negative)."""
    if amount <= 0:
        return "error: withdrawal amount must be positive"
    return _capture(
        pt.adjust_cash,
        account,
        -amount,
        source=agent,
        request_id=idempotency_key,
    )


@mcp.tool()
def quote(symbol: str) -> str:
    """Live last-trade price for a symbol from Yahoo Finance."""
    symbol = symbol.strip().upper()
    if not symbol:
        return "error: symbol required"
    try:
        return f"{symbol}: {pt.live_price(symbol):.2f}"
    except SystemExit as exc:
        return f"error: {exc}"


@mcp.tool()
def get_default_account() -> str:
    """Return the account used when CLI/TUI calls omit an explicit account."""
    conn = pt.db()
    try:
        row = conn.execute(
            "SELECT value FROM config WHERE key='default_account'"
        ).fetchone()
        return row[0] if row else "no default account"
    finally:
        conn.close()


@mcp.tool()
def account_details(account: str) -> str:
    """Account balances, configured risk limits, and open-position count."""
    conn = pt.db()
    try:
        row = conn.execute(
            "SELECT cash,deposits,realized,created FROM accounts WHERE name=?",
            (account,),
        ).fetchone()
        if not row:
            return f"error: no account '{account}'"
        cash, deposits, realized, created = row
        details = {
            "account": account,
            "cash": cash,
            "deposits": deposits,
            "realized": realized,
            "created": created,
            "positions": conn.execute(
                "SELECT COUNT(*) FROM positions WHERE account=?", (account,)
            ).fetchone()[0],
            "pending_orders": conn.execute(
                "SELECT COUNT(*) FROM orders WHERE account=?"
                " AND status IN ('pending','held')",
                (account,),
            ).fetchone()[0],
            "risk": pt.risk_limits(conn, account),
        }
        return json.dumps(details, indent=2)
    finally:
        conn.close()


@mcp.tool()
def preview_order(
    account: str,
    symbol: str,
    side: str,
    qty: float,
    price: float | None = None,
) -> str:
    """Dry-run an order against cash, margin, short, naked-option, and leverage limits."""
    conn = pt.db()
    try:
        return json.dumps(
            pt.preview_order(conn, account, symbol, side.lower(), qty, price), indent=2
        )
    except SystemExit as exc:
        return f"error: {exc}"
    finally:
        conn.close()


@mcp.tool()
def risk_get(account: str) -> str:
    """Show the account's shorting, naked-option, leverage, and order-size limits."""
    conn = pt.db()
    try:
        return json.dumps(pt.risk_limits(conn, account), indent=2)
    except SystemExit as exc:
        return f"error: {exc}"
    finally:
        conn.close()


@mcp.tool()
def risk_set(
    account: str,
    allow_short: bool | None = None,
    allow_naked_options: bool | None = None,
    max_gross_leverage: float | None = None,
    max_order_notional: float | None = None,
    clear_max_order: bool = False,
    idempotency_key: str | None = None,
    agent: str = "mcp",
) -> str:
    """Change selected account risk limits; omitted fields retain their current values."""
    return _capture(
        pt.set_risk_limits,
        account,
        allow_short,
        allow_naked_options,
        max_gross_leverage,
        max_order_notional,
        clear_max_order,
        source=agent,
        request_id=idempotency_key,
    )


@mcp.tool()
def asset_search(query: str, limit: int = 8) -> str:
    """Search Yahoo Finance for matching symbols, asset types, and exchanges."""
    try:
        return json.dumps(pt.search_assets(query, limit), indent=2)
    except SystemExit as exc:
        return f"error: {exc}"


@mcp.tool()
def validate_symbol(symbol: str) -> str:
    """Validate a symbol and return its live price, class, multiplier, and margin."""
    try:
        return json.dumps(pt.validate_asset(symbol), indent=2)
    except SystemExit as exc:
        return f"error: {exc}"


@mcp.tool()
def bulk_quotes(symbols: str) -> str:
    """Fetch up to 50 comma-separated live symbols concurrently."""
    requested = [s.strip().upper() for s in symbols.split(",") if s.strip()][:50]
    if not requested:
        return "error: no symbols"

    def fetch(symbol):
        try:
            return symbol, pt.live_price(symbol), None
        except SystemExit as exc:
            return symbol, None, str(exc)

    with ThreadPoolExecutor(max_workers=min(8, len(requested))) as pool:
        values = list(pool.map(fetch, requested))
    return json.dumps(
        [
            {"symbol": symbol, "price": price, "error": error}
            for symbol, price, error in values
        ],
        indent=2,
    )


@mcp.tool()
def healthcheck() -> str:
    """Check SQLite integrity, WAL mode, schema version, and core record counts."""
    conn = pt.db()
    try:
        return json.dumps(pt.healthcheck(conn), indent=2)
    finally:
        conn.close()


@mcp.tool()
def database_backup() -> str:
    """Create a consistent online backup under ~/.papertrade_backups."""
    conn = pt.db()
    try:
        return pt.backup_database(conn)
    except (OSError, sqlite3.Error) as exc:
        return f"error: backup failed: {exc}"
    finally:
        conn.close()


@mcp.tool()
def trade_history(account: str, limit: int = 100, offset: int = 0) -> str:
    """Paginated attributed order history; newest records are returned first."""
    return orders(account, limit, offset)


@mcp.tool()
def export_history(account: str, limit: int = 5000) -> str:
    """Export up to 10,000 account orders as CSV text."""
    conn = pt.db()
    try:
        return pt.trade_history_csv(conn, account, limit)
    except SystemExit as exc:
        return f"error: {exc}"
    finally:
        conn.close()


@mcp.tool()
def audit_log(account: str | None = None, limit: int = 100, offset: int = 0) -> str:
    """Paginated mutation audit trail with agent and idempotency attribution."""
    conn = pt.db()
    try:
        rows = pt.audit_events(conn, account, limit, offset)
        return (
            "\n".join(
                f"#{oid} {ts} [{source}] {action} {acct or '-'}"
                f"{' key=' + key if key else ''} {details}"
                for oid, ts, source, key, action, acct, details in rows
            )
            or "no audit events"
        )
    finally:
        conn.close()


@mcp.tool()
def sync_corporate_actions(account: str | None = None, agent: str = "mcp") -> str:
    """Apply unseen Yahoo Finance stock/ETF dividends and splits exactly once."""
    return _capture(pt.sync_corporate_actions, account, source=agent)


@mcp.tool()
def order_submit(
    account: str,
    symbol: str,
    side: str,
    qty: float | None = None,
    notional: float | None = None,
    order_type: str = "market",
    limit_price: float | None = None,
    stop_price: float | None = None,
    trail_price: float | None = None,
    trail_percent: float | None = None,
    time_in_force: str = "gtc",
    extended_hours: bool = False,
    client_order_id: str | None = None,
    order_class: str = "simple",
    take_profit: float | None = None,
    stop_loss: float | None = None,
    stop_loss_limit: float | None = None,
    dry_run: bool = False,
    idempotency_key: str | None = None,
    agent: str = "mcp",
) -> str:
    """Submit market, limit, stop, stop-limit, trailing, bracket, OCO, or OTO orders."""
    tp = {"limit_price": take_profit} if take_profit is not None else None
    sl = (
        {"stop_price": stop_loss, "limit_price": stop_loss_limit}
        if stop_loss is not None
        else None
    )
    return _capture(
        pt.submit_order,
        account,
        symbol,
        side,
        qty,
        notional,
        order_type,
        limit_price,
        stop_price,
        trail_price,
        trail_percent,
        time_in_force,
        extended_hours,
        client_order_id,
        order_class,
        tp,
        sl,
        dry_run,
        source=agent,
        request_id=idempotency_key or client_order_id,
    )


@mcp.tool()
def order_get(
    order_id: int | None = None,
    client_order_id: str | None = None,
    account: str | None = None,
) -> str:
    """Get one order by numeric id or by account plus client order id."""
    conn = pt.db()
    try:
        return json.dumps(
            pt.get_order(conn, order_id, client_order_id, account), indent=2
        )
    except SystemExit as exc:
        return f"error: {exc}"
    finally:
        conn.close()


@mcp.tool()
def order_replace(
    order_id: int,
    qty: float | None = None,
    limit_price: float | None = None,
    stop_price: float | None = None,
    trail: float | None = None,
    time_in_force: str | None = None,
    client_order_id: str | None = None,
    idempotency_key: str | None = None,
    agent: str = "mcp",
) -> str:
    """Replace selected fields on a pending order and return the new order id."""
    return _capture(
        pt.replace_order,
        order_id,
        qty,
        limit_price,
        stop_price,
        trail,
        time_in_force,
        client_order_id,
        source=agent,
        request_id=idempotency_key,
    )


@mcp.tool()
def order_cancel_all(
    account: str | None = None,
    idempotency_key: str | None = None,
    agent: str = "mcp",
) -> str:
    """Cancel every pending/held order, optionally restricted to one account."""
    return _capture(
        pt.cancel_all_orders,
        account,
        source=agent,
        request_id=idempotency_key,
    )


@mcp.tool()
def position_get(account: str, symbol: str) -> str:
    """Get one live-marked position with market value and unrealized P&L."""
    conn = pt.db()
    try:
        return json.dumps(pt.get_position(conn, account, symbol), indent=2)
    except SystemExit as exc:
        return f"error: {exc}"
    finally:
        conn.close()


@mcp.tool()
def position_close(
    account: str,
    symbol: str,
    qty: float | None = None,
    percent: float | None = None,
    idempotency_key: str | None = None,
    agent: str = "mcp",
) -> str:
    """Close all or part of one position by quantity or percentage."""
    return _capture(
        pt.close_position,
        account,
        symbol,
        qty,
        percent,
        source=agent,
        request_id=idempotency_key,
    )


@mcp.tool()
def position_close_all(
    account: str,
    idempotency_key: str | None = None,
    agent: str = "mcp",
) -> str:
    """Liquidate every open position in an account at current market prices."""
    return _capture(
        pt.close_all_positions,
        account,
        source=agent,
        request_id=idempotency_key,
    )


@mcp.tool()
def option_contract(symbol: str) -> str:
    """Parse and quote one OCC option contract."""
    try:
        return json.dumps(pt.option_contract_details(symbol), indent=2)
    except SystemExit as exc:
        return f"error: {exc}"


@mcp.tool()
def option_exercise(
    account: str,
    symbol: str,
    qty: float | None = None,
    idempotency_key: str | None = None,
    agent: str = "mcp",
) -> str:
    """Exercise a held long option into its underlying shares."""
    return _capture(
        pt.exercise_option,
        account,
        symbol,
        qty,
        source=agent,
        request_id=idempotency_key,
    )


@mcp.tool()
def option_do_not_exercise(
    account: str,
    symbol: str,
    idempotency_key: str | None = None,
    agent: str = "mcp",
) -> str:
    """Persist a do-not-exercise instruction for a held long option."""
    return _capture(
        pt.do_not_exercise_option,
        account,
        symbol,
        source=agent,
        request_id=idempotency_key,
    )


@mcp.tool()
def option_multi_leg(
    account: str,
    legs_json: str,
    limit_price: float | None = None,
    client_order_id: str | None = None,
    idempotency_key: str | None = None,
    agent: str = "mcp",
) -> str:
    """Atomically trade two-to-four option legs supplied as a JSON list."""
    try:
        legs = json.loads(legs_json)
    except json.JSONDecodeError as exc:
        return f"error: invalid legs JSON: {exc}"
    return _capture(
        pt.submit_option_multileg,
        account,
        legs,
        limit_price,
        source=agent,
        request_id=idempotency_key or client_order_id,
        client_order_id=client_order_id,
    )


@mcp.tool()
def watchlist_create(
    account: str,
    name: str,
    symbols: str = "",
    idempotency_key: str | None = None,
    agent: str = "mcp",
) -> str:
    """Create a persistent named watchlist with comma-separated symbols."""
    return _capture(
        pt.create_watchlist,
        account,
        name,
        symbols,
        source=agent,
        request_id=idempotency_key,
    )


@mcp.tool()
def watchlist_list(account: str) -> str:
    """List an account's persistent watchlists."""
    conn = pt.db()
    try:
        return json.dumps(pt.list_watchlists(conn, account), indent=2)
    except SystemExit as exc:
        return f"error: {exc}"
    finally:
        conn.close()


@mcp.tool()
def watchlist_get(account: str, watchlist: str) -> str:
    """Get one named watchlist or numeric watchlist id."""
    conn = pt.db()
    try:
        return json.dumps(pt.get_watchlist(conn, account, watchlist), indent=2)
    except SystemExit as exc:
        return f"error: {exc}"
    finally:
        conn.close()


@mcp.tool()
def watchlist_add(
    account: str,
    watchlist: str,
    symbol: str,
    idempotency_key: str | None = None,
    agent: str = "mcp",
) -> str:
    """Add a symbol to a persistent watchlist."""
    return _capture(
        pt.add_watchlist_symbol,
        account,
        watchlist,
        symbol,
        source=agent,
        request_id=idempotency_key,
    )


@mcp.tool()
def watchlist_remove(
    account: str,
    watchlist: str,
    symbol: str,
    idempotency_key: str | None = None,
    agent: str = "mcp",
) -> str:
    """Remove a symbol from a persistent watchlist."""
    return _capture(
        pt.remove_watchlist_symbol,
        account,
        watchlist,
        symbol,
        source=agent,
        request_id=idempotency_key,
    )


@mcp.tool()
def watchlist_delete(
    account: str,
    watchlist: str,
    idempotency_key: str | None = None,
    agent: str = "mcp",
) -> str:
    """Delete a persistent watchlist."""
    return _capture(
        pt.delete_watchlist,
        account,
        watchlist,
        source=agent,
        request_id=idempotency_key,
    )


@mcp.tool()
def watchlist_quotes(account: str, watchlist: str) -> str:
    """Return live quotes for every symbol saved in a watchlist."""
    conn = pt.db()
    try:
        return json.dumps(pt.watchlist_quotes(conn, account, watchlist), indent=2)
    except SystemExit as exc:
        return f"error: {exc}"
    finally:
        conn.close()


@mcp.tool()
def trading_calendar(start: str | None = None, end: str | None = None) -> str:
    """List NYSE sessions, including holidays and early closes."""
    try:
        return json.dumps(pt.market_calendar(start, end), indent=2)
    except SystemExit as exc:
        return f"error: {exc}"


@mcp.tool()
def account_activity(
    account: str,
    activity_type: str | None = None,
    start: str | None = None,
    end: str | None = None,
    limit: int = 100,
) -> str:
    """Unified account fills, orders, transfers, dividends, splits, and option events."""
    conn = pt.db()
    try:
        return json.dumps(
            pt.account_activities(conn, account, activity_type, start, end, limit),
            indent=2,
        )
    except SystemExit as exc:
        return f"error: {exc}"
    finally:
        conn.close()


@mcp.tool()
def market_bars(
    symbol: str,
    start: str | None = None,
    end: str | None = None,
    timeframe: str = "1Day",
    limit: int = 100,
) -> str:
    """Historical OHLCV bars from Yahoo Finance."""
    try:
        return json.dumps(
            pt.market_history(symbol, "bars", start, end, timeframe, limit), indent=2
        )
    except SystemExit as exc:
        return f"error: {exc}"


@mcp.tool()
def market_quotes(
    symbol: str,
    start: str | None = None,
    end: str | None = None,
    timeframe: str = "1Day",
    limit: int = 100,
) -> str:
    """Indicative historical quote series derived from Yahoo aggregates."""
    try:
        return json.dumps(
            pt.market_history(symbol, "quotes", start, end, timeframe, limit),
            indent=2,
        )
    except SystemExit as exc:
        return f"error: {exc}"


@mcp.tool()
def market_trades(
    symbol: str,
    start: str | None = None,
    end: str | None = None,
    timeframe: str = "1Day",
    limit: int = 100,
) -> str:
    """Historical aggregate trade series from Yahoo Finance."""
    try:
        return json.dumps(
            pt.market_history(symbol, "trades", start, end, timeframe, limit),
            indent=2,
        )
    except SystemExit as exc:
        return f"error: {exc}"


@mcp.tool()
def market_latest_quote(symbol: str) -> str:
    """Latest bid, ask, and last indication."""
    try:
        return json.dumps(pt.latest_quote(symbol), indent=2)
    except SystemExit as exc:
        return f"error: {exc}"


@mcp.tool()
def market_latest_trade(symbol: str) -> str:
    """Latest trade indication."""
    try:
        return json.dumps(pt.latest_trade(symbol), indent=2)
    except SystemExit as exc:
        return f"error: {exc}"


@mcp.tool()
def market_snapshot(symbol: str) -> str:
    """Combined latest quote, daily bar, previous close, and change."""
    try:
        return json.dumps(pt.market_snapshot(symbol), indent=2)
    except SystemExit as exc:
        return f"error: {exc}"


@mcp.tool()
def market_news(symbol: str, limit: int = 10) -> str:
    """Recent symbol news headlines and links."""
    try:
        return json.dumps(pt.market_news(symbol, limit), indent=2)
    except SystemExit as exc:
        return f"error: {exc}"


@mcp.tool()
def market_most_actives(limit: int = 20) -> str:
    """Current most-active equity screener."""
    try:
        return json.dumps(pt.market_screener("most_actives", limit), indent=2)
    except SystemExit as exc:
        return f"error: {exc}"


@mcp.tool()
def market_movers(limit: int = 10) -> str:
    """Current day gainers and losers."""
    try:
        return json.dumps(
            {
                "gainers": pt.market_screener("day_gainers", limit),
                "losers": pt.market_screener("day_losers", limit),
            },
            indent=2,
        )
    except SystemExit as exc:
        return f"error: {exc}"


@mcp.tool()
def market_crypto_orderbook(symbol: str) -> str:
    """Indicative crypto top-of-book; Yahoo does not expose full exchange depth."""
    try:
        return json.dumps(pt.crypto_orderbook(symbol), indent=2)
    except SystemExit as exc:
        return f"error: {exc}"


@mcp.tool()
def forex_rate(pair: str) -> str:
    """Latest FX rate for a pair such as USD/EUR."""
    normalized = pair.upper().replace("/", "")
    if len(normalized) != 6:
        return "error: forex pair must look like USD/EUR"
    try:
        return json.dumps(pt.latest_quote(f"{normalized}=X"), indent=2)
    except SystemExit as exc:
        return f"error: {exc}"


LEGACY_MCP_TOOLS = frozenset(
    {
        "buy",
        "sell",
        "close_position",
        "watchlist",
        "cancel_order",
        "quote",
        "trade_history",
    }
)

CORE_MCP_TOOLS = frozenset(
    {
        "mcp_catalog",
        "account_create",
        "account_list",
        "account_details",
        "get_default_account",
        "set_default_account",
        "rename_account",
        "summary",
        "deposit",
        "withdraw",
        "risk_get",
        "risk_set",
        "preview_order",
        "orders",
        "order_submit",
        "order_get",
        "order_replace",
        "order_cancel",
        "order_cancel_all",
        "positions",
        "position_get",
        "position_close",
        "position_close_all",
        "pnl",
        "performance",
        "portfolio_backtest",
        "tick",
        "asset_search",
        "validate_symbol",
        "bulk_quotes",
        "market_status",
        "trading_calendar",
        "market_latest_quote",
        "market_snapshot",
        "market_bars",
        "market_news",
        "market_most_actives",
        "market_movers",
        "buy_option",
        "sell_option",
        "option_chain",
        "option_contract",
        "futures_symbols",
        "watchlist_create",
        "watchlist_list",
        "watchlist_get",
        "watchlist_add",
        "watchlist_remove",
        "watchlist_delete",
        "watchlist_quotes",
        "account_activity",
        "audit_log",
        "healthcheck",
        "database_backup",
    }
)

ACTIVE_MCP_PROFILE = ""
ACTIVE_MCP_RESPONSE_FORMAT = ""
ACTIVE_MCP_TOOLS: frozenset[str] = frozenset()


@mcp.tool()
def mcp_catalog() -> str:
    """Describe the active catalog, response contract, profiles, and legacy aliases."""
    return json.dumps(
        {
            "profile": ACTIVE_MCP_PROFILE,
            "response_format": ACTIVE_MCP_RESPONSE_FORMAT,
            "tool_count": len(ACTIVE_MCP_TOOLS),
            "tools": sorted(ACTIVE_MCP_TOOLS),
            "profiles": {
                "core": "Focused default without destructive account tools or duplicate aliases.",
                "advanced": "Every canonical capability, including destructive and specialist tools.",
                "full": "Advanced plus all legacy compatibility aliases.",
                "compat": "Alias for full, with legacy responses by default.",
            },
            "legacy_aliases": {
                "buy": "order_submit",
                "sell": "order_submit",
                "close_position": "position_close",
                "watchlist": "bulk_quotes",
                "cancel_order": "order_cancel",
                "quote": "market_latest_quote",
                "trade_history": "orders",
            },
        },
        indent=2,
    )


def _json_payload(result):
    if not isinstance(result, str):
        return result
    stripped = result.strip()
    if not stripped:
        return ""
    try:
        return json.loads(stripped)
    except (TypeError, json.JSONDecodeError):
        return result


def _json_success(result):
    return json.dumps(
        {"ok": True, "data": _json_payload(result)},
        separators=(",", ":"),
        default=str,
    )


def _json_error(message, code="tool_error"):
    return json.dumps(
        {"ok": False, "error": {"code": code, "message": str(message)}},
        separators=(",", ":"),
    )


def _wrap_json_tool(tool):
    original = tool.fn
    if inspect.iscoroutinefunction(original):

        @functools.wraps(original)
        async def async_wrapped(*args, **kwargs):
            try:
                result = await original(*args, **kwargs)
            except SystemExit as exc:
                return _json_error(exc)
            except Exception as exc:
                return _json_error(exc, "internal_error")
            if isinstance(result, str) and result.strip().lower().startswith("error:"):
                return _json_error(result.strip()[6:].strip())
            return _json_success(result)

        tool.fn = async_wrapped
        return

    @functools.wraps(original)
    def wrapped(*args, **kwargs):
        try:
            result = original(*args, **kwargs)
        except SystemExit as exc:
            return _json_error(exc)
        except Exception as exc:
            return _json_error(exc, "internal_error")
        if isinstance(result, str) and result.strip().lower().startswith("error:"):
            return _json_error(result.strip()[6:].strip())
        return _json_success(result)

    tool.fn = wrapped


def _configure_mcp_catalog():
    global ACTIVE_MCP_PROFILE, ACTIVE_MCP_RESPONSE_FORMAT, ACTIVE_MCP_TOOLS

    requested = os.environ.get("PAPERTRADE_MCP_PROFILE", "core").strip().lower()
    profile = "full" if requested == "compat" else requested
    if profile not in {"core", "advanced", "full"}:
        raise RuntimeError(
            "PAPERTRADE_MCP_PROFILE must be core, advanced, full, or compat"
        )
    default_format = "legacy" if profile == "full" else "json"
    response_format = (
        os.environ.get("PAPERTRADE_MCP_RESPONSE_FORMAT", default_format).strip().lower()
    )
    if response_format not in {"json", "legacy"}:
        raise RuntimeError("PAPERTRADE_MCP_RESPONSE_FORMAT must be json or legacy")

    available = set(mcp._tool_manager._tools)
    missing = CORE_MCP_TOOLS - available
    if missing:
        raise RuntimeError(f"core MCP tools were not registered: {sorted(missing)}")
    if profile == "core":
        allowed = set(CORE_MCP_TOOLS)
    elif profile == "advanced":
        allowed = available - LEGACY_MCP_TOOLS
    else:
        allowed = available
    for name in available - allowed:
        mcp.remove_tool(name)

    ACTIVE_MCP_PROFILE = profile
    ACTIVE_MCP_RESPONSE_FORMAT = response_format
    ACTIVE_MCP_TOOLS = frozenset(allowed)
    if response_format == "json":
        for tool in mcp._tool_manager._tools.values():
            _wrap_json_tool(tool)


_configure_mcp_catalog()


if __name__ == "__main__":
    mcp.run()
