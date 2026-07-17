#!/usr/bin/env python3
"""MCP server wrapping papertrade — gives Claude Code tool access to the paper broker."""

import contextlib
import io
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
        "SELECT id,ts,side,qty,symbol,limit_price,status,filled_price,source,request_id,"
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
        price_limit,
        status,
        fp,
        source,
        key,
        reason,
    ) in rows:
        px = f"@{fp:.2f}" if fp else (f"lim {price_limit:.2f}" if price_limit else "")
        meta = f" source={source or 'unknown'}" + (f" key={key}" if key else "")
        rejected = f" reason={reason}" if reason else ""
        out.append(
            f"#{oid} {ts} {side} {qty:g} {symbol} {px} [{status}]{meta}{rejected}"
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
    names = [n for (n,) in conn.execute("SELECT name FROM accounts")]
    out = []
    for name in names:
        cash, dep, real = conn.execute(
            "SELECT cash, deposits, realized FROM accounts WHERE name=?", (name,)
        ).fetchone()
        eq, unreal, npos = cash, 0.0, 0
        for sym, qty, avg, mult, ac, margin in conn.execute(
            "SELECT symbol,qty,avg_cost,mult,asset_class,margin FROM positions WHERE account=?",
            (name,),
        ):
            npos += 1
            try:
                px = pt.live_price(sym)
            except SystemExit:
                continue
            u = qty * mult * (px - avg)
            unreal += u
            eq += (u + margin) if ac == "future" else qty * mult * px
        total = real + unreal
        ret = total / dep * 100 if dep else 0.0
        out.append(
            f"{name}: equity {eq:,.2f}  cash {cash:,.2f}  unreal {unreal:+,.2f}  "
            f"total {total:+,.2f} ({ret:+.2f}%)  {npos} positions"
        )
    conn.close()
    return "\n".join(out) or "no accounts"


@mcp.tool()
def performance(account: str) -> str:
    """Portfolio performance since inception: return, CAGR, Sharpe, Sortino, volatility, max drawdown.
    Reconstructs a daily equity curve from the trade/cashflow ledger and real historical prices."""
    conn = pt.db()
    try:
        curve = pt.equity_curve(conn, account, live=True)
    finally:
        conn.close()
    m = pt.performance_metrics(curve)
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
                "SELECT COUNT(*) FROM orders WHERE account=? AND status='pending'",
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


if __name__ == "__main__":
    mcp.run()
