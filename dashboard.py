#!/usr/bin/env python3
"""tradingcli live dashboard — MUSE-style TUI portfolio tracker.

Usage: tradingcli dash [-a ACCOUNT] [-n SECONDS]
Keys: q quit · t tick now · r refresh now. Auto-fills limit orders each refresh.
"""

import argparse
import os
import select
import sys
import termios
import time
import tty
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import papertrade as pt
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

RED = "#ff2b4a"
GREY = "grey35"
LOGO = "▀█▀ █▀█ ▄▀█ █▀▄ █ █▄ █ █▀▀ █▀▀ █   █\n █  █▀▄ █▀█ █▄▀ █ █ ▀█ █▄█ █▄▄ █▄▄ █"


def snapshot(account_filter=None):
    conn = pt.db()
    where, params = ("WHERE name=?", (account_filter,)) if account_filter else ("", ())
    accounts = conn.execute(
        f"SELECT name, cash FROM accounts {where}", params
    ).fetchall()
    default = (
        conn.execute("SELECT value FROM config WHERE key='default_account'").fetchone()
        or [None]
    )[0]
    data, symbols = [], set()
    for name, cash in accounts:
        dep, real = conn.execute(
            "SELECT deposits, realized FROM accounts WHERE name=?", (name,)
        ).fetchone()
        pos = conn.execute(
            "SELECT symbol, qty, avg_cost, mult, asset_class, margin"
            " FROM positions WHERE account=?",
            (name,),
        ).fetchall()
        pend = conn.execute(
            "SELECT id, side, qty, symbol, limit_price FROM orders"
            " WHERE account=? AND status='pending'",
            (name,),
        ).fetchall()
        symbols |= {s for s, *_ in pos} | {s for _, _, _, s, _ in pend}
        data.append((name, cash, dep, real, pos, pend))
    conn.close()
    return data, symbols, default


def fetch_quotes(symbols):
    """{symbol: (last, prev_close) | None}. Options priced via chain (no prev_close)."""
    import yfinance as yf

    def get(s):
        try:
            if pt.OCC_RE.match(s):
                return s, (pt.option_price(s), None)  # options: no clean prev close
            fi = yf.Ticker(s).fast_info
            pc = fi.get("previousClose")
            return s, (float(fi["lastPrice"]), float(pc) if pc else None)
        except Exception:
            return s, None  # render as '?', retry next cycle

    with ThreadPoolExecutor(max_workers=8) as ex:
        return dict(ex.map(get, symbols))


def braille_chart(values, width=74, height=16):
    """Line chart in braille dots. Returns list of strings (rows)."""
    if len(values) < 2:
        return ["(not enough history yet)"]
    lo, hi = min(values), max(values)
    if hi == lo:
        hi = lo + 1
    W, H = width * 2, height * 4
    grid = [[0] * width for _ in range(height)]
    dot = [[0x01, 0x02, 0x04, 0x40], [0x08, 0x10, 0x20, 0x80]]
    n = len(values)
    xs = [round(i / (n - 1) * (W - 1)) for i in range(n)]
    ys = [round((1 - (v - lo) / (hi - lo)) * (H - 1)) for v in values]
    for i in range(n - 1):
        x0, y0, x1, y1 = xs[i], ys[i], xs[i + 1], ys[i + 1]
        steps = max(abs(x1 - x0), abs(y1 - y0), 1)
        for s in range(steps + 1):
            px, py = x0 + (x1 - x0) * s // steps, y0 + (y1 - y0) * s // steps
            if 0 <= px < W and 0 <= py < H:
                grid[py // 4][px // 2] |= dot[px % 2][py % 4]
    return ["".join(chr(0x2800 + c) for c in row) for row in grid]


def _money(v, pct=None):
    c = "green" if v >= 0 else RED
    s = f"[{c}]{v:+,.2f}[/{c}]"
    if pct is not None:
        s += f" [{c}]({pct:+.2f}%)[/{c}]"
    return s


def _pct(frac):
    c = "green" if frac >= 0 else RED
    return f"[{c}]{frac * 100:+.2f}%[/{c}]"


def render(data, quotes, default, prev=None):
    prev = prev or {}
    total_equity = total_upnl = total_day = 0.0
    panels = []
    for name, cash, deposits, realized, pos, pend in data:
        t = Table(
            expand=True, pad_edge=False, border_style=GREY, header_style=f"bold {RED}"
        )
        for col, justify in [
            ("SYMBOL", "left"),
            ("SIDE", "left"),
            ("QTY", "right"),
            ("AVG COST", "right"),
            ("PRICE", "right"),
            ("MKT VALUE", "right"),
            ("UNREAL P&L", "right"),
            ("P&L %", "right"),
        ]:
            t.add_column(col, justify=justify)
        equity, upnl_sum, day_sum = cash, 0.0, 0.0
        for sym, qty, avg, mult, ac, margin in sorted(pos):
            side = "[green]LONG[/green]" if qty > 0 else f"[{RED}]SHORT[/{RED}]"
            q = quotes.get(sym)
            if q is None:
                t.add_row(sym, side, f"{abs(qty):g}", f"{avg:.2f}", "?", "?", "?", "?")
                continue
            px, prev_close = q
            upnl = qty * mult * (px - avg)
            mv = (
                (upnl + margin) if ac == "future" else qty * mult * px
            )  # liquidation value
            pct = (px / avg - 1) * 100 * (1 if qty > 0 else -1) if avg else 0.0
            c = "green" if upnl >= 0 else RED
            equity += mv
            upnl_sum += upnl
            if prev_close:
                day_sum += qty * mult * (px - prev_close)
            last_px = prev.get(sym)
            if last_px is None or px == last_px:
                tickmark = "[dim]·[/dim]"
            elif px > last_px:
                tickmark = "[green]▲[/green]"
            else:
                tickmark = f"[{RED}]▼[/{RED}]"
            label = sym if ac != "future" else f"{sym} [dim]x{mult:g}[/dim]"
            t.add_row(
                f"[bold]{label}[/bold]",
                side,
                f"{abs(qty):g}",
                f"{avg:.2f}",
                f"{px:.2f} {tickmark}",
                f"{mv:,.2f}",
                f"[{c}]{upnl:+,.2f}[/{c}]",
                f"[{c}]{pct:+.2f}%[/{c}]",
            )
        if not pos:
            t.add_row("[dim]no positions[/dim]", "", "", "", "", "", "", "")
        total_equity += equity
        total_upnl += upnl_sum
        total_day += day_sum
        total_pnl = realized + upnl_sum
        ret_pct = (total_pnl / deposits * 100) if deposits else 0.0
        stats = Table.grid(expand=True)
        for _ in range(6):
            stats.add_column(ratio=1)
        stats.add_row(
            f"[dim]CASH[/dim]\n[bold]{cash:,.2f}[/bold]",
            f"[dim]EQUITY[/dim]\n[bold]{equity:,.2f}[/bold]",
            f"[dim]DAY[/dim]\n{_money(day_sum)}",
            f"[dim]UNREAL[/dim]\n{_money(upnl_sum)}",
            f"[dim]REALIZED[/dim]\n{_money(realized)}",
            f"[dim]TOTAL[/dim]\n{_money(total_pnl, ret_pct)}",
        )
        parts = [t, "", stats]
        if pend:
            lines = ", ".join(
                f"#{i} {side} {q:g} {s} lim {lp:.2f}" for i, side, q, s, lp in pend
            )
            parts.append(f"[yellow]◌ pending:[/yellow] {lines}")
        star = " ★" if name == default else ""
        panels.append(
            Panel(
                Group(*parts),
                title=f"[bold]{name.upper()}{star}[/bold]",
                title_align="left",
                border_style=RED if name == default else GREY,
            )
        )
    if not panels:
        panels.append(
            Panel(
                "no accounts — create one with: tradingcli new NAME", border_style=RED
            )
        )

    clock = pt.market_clock()
    transition = clock.get("next_transition") or {}
    eastern = transition.get("eastern", "")
    when = ""
    if eastern:
        when = f" · next {clock['transition']} {eastern[11:16]} ET"
    if clock["is_open"]:
        status = f"[green]●[/green]  Market open — NYSE{when}"
    else:
        status = f"[{GREY}]■[/{GREY}]  Market closed{when}"
    stamp = datetime.now().strftime("%H:%M:%S")
    banner = Panel(f"{status}   [dim]as of {stamp}[/dim]", border_style=RED)

    keys = Text.from_markup(
        f"[dim][bold]b[/bold] Buy  [{RED}]●[/{RED}]  [bold]s[/bold] Sell  [{RED}]●[/{RED}]  "
        f"[bold]o[/bold] Option  [{RED}]●[/{RED}]  [bold]g[/bold] Graph  [{RED}]●[/{RED}]  "
        f"[bold]c[/bold] Cancel  [{RED}]●[/{RED}]  [bold]n[/bold] New  [{RED}]●[/{RED}]  "
        f"[bold]e[/bold] Rename  [{RED}]●[/{RED}]  [bold]u[/bold] Switch  "
        f"[{RED}]●[/{RED}]  [bold]t[/bold] Tick  [{RED}]●[/{RED}]  "
        f"[bold]r[/bold] Refresh  [{RED}]●[/{RED}]  [bold]q[/bold] Quit[/dim]"
    )
    return Group(
        Text.from_markup(f"[bold {RED}]{LOGO}[/bold {RED}]"),
        "",
        banner,
        *panels,
        "",
        keys,
    )


def read_key(timeout):
    if not sys.stdin.isatty():
        time.sleep(timeout)
        return None
    r, _, _ = select.select([sys.stdin], [], [], timeout)
    return sys.stdin.read(1) if r else None


def run_tick(prices):
    conn = pt.db()
    try:
        with conn:
            pt.tick(conn, price_fn=lambda s: prices.get(s) or pt.live_price(s))
    finally:
        conn.close()


def prompt_new(console):
    console.print(f"\n[bold {RED}]▚ New paper portfolio[/bold {RED}]")
    name = console.input("  name: ").strip()
    if not name:
        return
    raw = (
        console.input("  starting cash [dim](default 100,000)[/dim]: ")
        .strip()
        .replace(",", "")
    )
    try:
        cash = float(raw) if raw else 100_000
    except ValueError:
        console.print(f"  [{RED}]not a number, aborted[/{RED}]")
        return
    conn = pt.db()
    try:
        with conn:
            pt.create_account(conn, name, cash, source="dashboard")
        console.print(f"  [green]created '{name}' with {cash:,.2f}[/green]")
    except SystemExit as e:
        console.print(f"  [{RED}]{e}[/{RED}]")
    finally:
        conn.close()


def first_run_setup(console):
    """One-time welcome + first portfolio. Runs only until setup_done is set in config."""
    console.clear()
    console.print(f"[bold {RED}]{LOGO}[/bold {RED}]\n")
    console.print(
        "[bold]Welcome to tradingcli[/bold] — local multi-account paper trading.\n"
    )
    console.print(
        "[dim]Data: Yahoo Finance · Storage: local SQLite · no login, no keys.\n"
        "Trade stocks, ETFs, crypto (BTC-USD), FX (EURUSD=X), futures (ES=F), and options.\n"
        "AI agents can drive it too via the bundled MCP server.[/dim]\n"
    )
    console.print("Let's create your first portfolio.\n")
    while True:
        name = console.input("  portfolio name [dim][main][/dim]: ").strip() or "main"
        raw = (
            console.input("  starting cash [dim][100,000][/dim]: ")
            .strip()
            .replace(",", "")
        )
        try:
            cash = float(raw) if raw else 100_000
        except ValueError:
            console.print(
                f"  [{RED}]starting cash must be a number — try again[/{RED}]\n"
            )
            continue
        profile = (
            console.input(
                "  risk profile [dim][standard][/dim] "
                "(conservative / standard / unrestricted): "
            )
            .strip()
            .lower()
            or "standard"
        )
        if profile not in ("conservative", "standard", "unrestricted"):
            console.print(f"  [{RED}]unknown risk profile — try again[/{RED}]\n")
            continue
        conn = pt.db()
        try:
            # Account + funding event + setup flag land together or not at all.
            with pt.writing(conn):
                pt.create_account(conn, name, cash, source="dashboard")
                if profile == "conservative":
                    pt.set_risk_limits(
                        conn,
                        name,
                        allow_short=False,
                        allow_naked_options=False,
                        max_gross_leverage=1,
                        max_order_notional=cash * 0.25 if cash else None,
                        source="dashboard",
                    )
                elif profile == "unrestricted":
                    pt.set_risk_limits(
                        conn,
                        name,
                        allow_short=True,
                        allow_naked_options=True,
                        max_gross_leverage=10,
                        clear_max_order=True,
                        source="dashboard",
                    )
                conn.execute("INSERT OR REPLACE INTO config VALUES('setup_done','1')")
        except SystemExit as exc:
            console.print(f"  [{RED}]{exc} — try again[/{RED}]\n")
            continue
        finally:
            conn.close()
        break
    console.print(f"\n  [green]✓ '{name}' created with {cash:,.2f}[/green]")
    console.print(
        "[dim]  Opening the dashboard… (press keys shown at the bottom to trade)[/dim]"
    )
    time.sleep(2.0)


def prompt_order(console, side):
    conn = pt.db()
    default = (
        conn.execute("SELECT value FROM config WHERE key='default_account'").fetchone()
        or [None]
    )[0]
    conn.close()
    console.print(
        f"\n[bold {RED}]▚ {side.capitalize()} order[/bold {RED}] "
        f"[dim](stock/ETF/crypto e.g. AAPL, BTC-USD · future e.g. ES=F)[/dim]"
    )
    account = console.input(f"  account [dim]\\[{default}][/dim]: ").strip() or default
    if not account:
        return
    symbol = console.input("  symbol: ").strip().upper()
    if not symbol:
        return
    try:
        qty = float(console.input("  qty: ").strip())
        raw = console.input("  limit price [dim](blank = market)[/dim]: ").strip()
        limit = float(raw) if raw else None
    except ValueError:
        console.print(f"  [{RED}]not a number, aborted[/{RED}]")
        return
    conn = pt.db()
    try:
        with conn:
            pt.place(conn, account, symbol, side, qty, limit, source="dashboard")
    except SystemExit as e:
        console.print(f"  [{RED}]{e}[/{RED}]")
    finally:
        conn.close()
    time.sleep(1.2)  # let the fill/pending line be read before the screen takes over


def prompt_option(console):
    conn = pt.db()
    default = (
        conn.execute("SELECT value FROM config WHERE key='default_account'").fetchone()
        or [None]
    )[0]
    conn.close()
    console.print(
        f"\n[bold {RED}]▚ Option order[/bold {RED}] "
        f"[dim](run `tradingcli chain SYMBOL` to find expiries/strikes)[/dim]"
    )
    account = console.input(f"  account [dim]\\[{default}][/dim]: ").strip() or default
    if not account:
        return
    side = console.input("  buy or sell [dim][buy][/dim]: ").strip().lower() or "buy"
    if side not in ("buy", "sell"):
        console.print(f"  [{RED}]side must be buy or sell[/{RED}]")
        return
    underlying = console.input("  underlying (e.g. AAPL): ").strip().upper()
    expiry = console.input("  expiry YYYY-MM-DD: ").strip()
    kind = console.input("  call or put [C/P]: ").strip().upper()
    if not (underlying and expiry and kind in ("C", "P")):
        console.print(f"  [{RED}]missing/invalid fields, aborted[/{RED}]")
        return
    try:
        strike = float(console.input("  strike: ").strip())
        qty = float(console.input("  contracts: ").strip())
        raw = console.input("  limit premium [dim](blank = market)[/dim]: ").strip()
        limit = float(raw) if raw else None
    except ValueError:
        console.print(f"  [{RED}]not a number, aborted[/{RED}]")
        return
    occ = pt.build_occ(underlying, expiry, strike, kind)
    conn = pt.db()
    try:
        with conn:
            pt.place(conn, account, occ, side, qty, limit, source="dashboard")
    except SystemExit as e:
        console.print(f"  [{RED}]{e}[/{RED}]")
    finally:
        conn.close()
    time.sleep(1.5)


def prompt_rename(console):
    conn = pt.db()
    names = [n for (n,) in conn.execute("SELECT name FROM accounts")]
    conn.close()
    console.print(
        f"\n[bold {RED}]▚ Rename portfolio[/bold {RED}]  [dim]{', '.join(names)}[/dim]"
    )
    old = console.input("  which: ").strip()
    if not old:
        return
    new = console.input("  new name: ").strip()
    if not new:
        return
    conn = pt.db()
    try:
        with conn:
            pt.rename_account(conn, old, new, source="dashboard")
    except SystemExit as e:
        console.print(f"  [{RED}]{e}[/{RED}]")
    finally:
        conn.close()
    time.sleep(1.0)


def prompt_graph(console, account_filter):
    conn = pt.db()
    names = [n for (n,) in conn.execute("SELECT name FROM accounts")]
    default = (
        conn.execute("SELECT value FROM config WHERE key='default_account'").fetchone()
        or [None]
    )[0]
    conn.close()
    if not names:
        return
    console.print(
        f"\n[bold {RED}]▚ Performance chart[/bold {RED}]  [dim]{', '.join(names)}[/dim]"
    )
    account = (
        console.input(f"  which [dim]\\[{account_filter or default}][/dim]: ").strip()
        or account_filter
        or default
    )
    if account not in names:
        console.print(f"  [{RED}]no portfolio '{account}'[/{RED}]")
        time.sleep(1.0)
        return
    console.print("  [dim]reconstructing equity curve from history…[/dim]")
    conn = pt.db()
    curve = pt.equity_curve(conn, account, live=True)
    conn.close()
    m = pt.performance_metrics(curve)
    if not m:
        console.print(f"  [{RED}]{account} has no activity yet[/{RED}]")
        time.sleep(1.5)
        return
    console.clear()
    eq = [e for _, e in curve]
    chart = braille_chart(eq)
    up = eq[-1] >= eq[0]
    col = "green" if up else RED
    console.print(
        f"[bold {RED}]{account.upper()}[/bold {RED}]  "
        f"[dim]{m['start']} → {m['end']}  ({m['days']}d)[/dim]\n"
    )
    console.print(f"[dim]{max(eq):,.0f}[/dim]")
    for row in chart:
        console.print(f"[{col}]{row}[/{col}]")
    console.print(f"[dim]{min(eq):,.0f}[/dim]\n")

    stats = Table.grid(expand=True, padding=(0, 2))
    for _ in range(4):
        stats.add_column()
    stats.add_row(
        f"[dim]EQUITY[/dim]\n[bold]{m['start_eq']:,.0f} → {m['end_eq']:,.0f}[/bold]",
        f"[dim]RETURN[/dim]\n{_pct(m['total'])}",
        f"[dim]CAGR[/dim]\n{_pct(m['cagr'])}",
        f"[dim]MAX DD[/dim]\n[{RED}]{m['mdd'] * 100:.2f}%[/{RED}]",
    )
    stats.add_row(
        f"[dim]SHARPE[/dim]\n[bold]{m['sharpe']:.2f}[/bold]",
        f"[dim]SORTINO[/dim]\n[bold]{m['sortino']:.2f}[/bold]",
        f"[dim]VOL (ann)[/dim]\n[bold]{m['vol'] * 100:.1f}%[/bold]",
        f"[dim]BEST / WORST[/dim]\n[green]{m['best'] * 100:+.2f}%[/green] / [{RED}]{m['worst'] * 100:+.2f}%[/{RED}]",
    )
    console.print(
        Panel(stats, border_style=RED, title="[bold]METRICS[/bold]", title_align="left")
    )
    console.input("\n[dim]press Enter to return[/dim]")


def prompt_cancel(console):
    console.print(f"\n[bold {RED}]▚ Cancel order[/bold {RED}]")
    raw = (
        console.input("  order id (# shown on the pending line): ").strip().lstrip("#")
    )
    if not raw:
        return
    conn = pt.db()
    try:
        with conn:
            pt.cancel(conn, int(raw), source="dashboard")
    except (SystemExit, ValueError) as e:
        console.print(f"  [{RED}]{e}[/{RED}]")
    finally:
        conn.close()
    time.sleep(1.0)


def prompt_use(console):
    conn = pt.db()
    names = [n for (n,) in conn.execute("SELECT name FROM accounts")]
    conn.close()
    console.print(
        f"\n[bold {RED}]▚ Switch default[/bold {RED}]  [dim]{', '.join(names)}[/dim]"
    )
    name = console.input("  account: ").strip()
    if not name:
        return
    if name not in names:
        console.print(f"  [{RED}]no account '{name}'[/{RED}]")
        return
    conn = pt.db()
    try:
        pt.set_default(conn, name, source="dashboard")
    except SystemExit as exc:
        console.print(f"  [{RED}]{exc}[/{RED}]")
    finally:
        conn.close()


def run_dashboard(console, args):
    """Full-screen loop. Returns 'quit', 'new', or 'use'."""
    tty_attrs = None
    if sys.stdin.isatty():
        tty_attrs = termios.tcgetattr(sys.stdin)
        tty.setcbreak(sys.stdin.fileno())
    try:
        with Live(console=console, screen=True, auto_refresh=False) as live:
            prev = {}
            while True:
                data, symbols, default = snapshot(args.account)
                quotes = fetch_quotes(symbols)
                prices = {s: q[0] for s, q in quotes.items() if q}
                today = datetime.now().strftime("%Y-%m-%d")
                expired = any(
                    pt.OCC_RE.match(sym) and pt.parse_occ(sym)[1] < today
                    for *_, pos, _pend in data
                    for sym, *_ in pos
                )
                if expired or any(pend for *_, pend in data):
                    run_tick(prices)
                    data, _, default = snapshot(args.account)
                live.update(render(data, quotes, default, prev), refresh=True)
                prev = prices or prev
                deadline = time.monotonic() + args.interval
                while (left := deadline - time.monotonic()) > 0:
                    key = read_key(min(left, 0.25))
                    if key == "q":
                        return "quit"
                    if key in ("n", "u", "b", "s", "c", "o", "g", "e"):
                        return {
                            "n": "new",
                            "u": "use",
                            "b": "buy",
                            "s": "sell",
                            "c": "cancel",
                            "o": "option",
                            "g": "graph",
                            "e": "rename",
                        }[key]
                    if key == "t":
                        run_tick(prices)
                        break
                    if key == "r":
                        break
    finally:
        if tty_attrs:
            termios.tcsetattr(sys.stdin, termios.TCSADRAIN, tty_attrs)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-a", "--account")
    ap.add_argument("-n", "--interval", type=float, default=2.0)
    args = ap.parse_args()

    console = Console()
    conn = pt.db()
    done = conn.execute("SELECT 1 FROM config WHERE key='setup_done'").fetchone()
    empty = conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 0
    conn.close()
    if not done and empty and sys.stdin.isatty():
        first_run_setup(console)  # one-time onboarding

    while True:
        action = run_dashboard(console, args)
        if action == "quit":
            return
        elif action == "new":
            prompt_new(console)
        elif action == "use":
            prompt_use(console)
        elif action in ("buy", "sell"):
            prompt_order(console, action)
        elif action == "option":
            prompt_option(console)
        elif action == "graph":
            prompt_graph(console, args.account)
        elif action == "rename":
            prompt_rename(console)
        elif action == "cancel":
            prompt_cancel(console)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
