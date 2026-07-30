#!/usr/bin/env python3
"""tradingcli live dashboard — MUSE-style TUI portfolio tracker.

Usage: tradingcli dash [-a ACCOUNT] [-n SECONDS]
Keys: q quit · t tick now · r refresh now · v toggle compact view · up/down scroll.
Auto-fills limit orders each refresh.
"""

import argparse
import os
import select
import sys
import termios
import threading
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
PAGE_SIZE = 3  # portfolio panels shown per screen in detail view; up/down scroll one at a time


def snapshot(account_filter=None):
    with pt.connection() as conn:
        if account_filter:
            accounts = conn.execute(
                "SELECT name,cash,deposits,realized FROM accounts WHERE name=?",
                (account_filter,),
            ).fetchall()
        else:
            accounts = conn.execute(
                "SELECT name,cash,deposits,realized FROM accounts"
            ).fetchall()
        default = (
            conn.execute(
                "SELECT value FROM config WHERE key='default_account'"
            ).fetchone()
            or [None]
        )[0]
        data, symbols = [], set()
        for name, cash, dep, real in accounts:
            pos = conn.execute(
                "SELECT symbol, qty, avg_cost, mult, asset_class, margin"
                " FROM positions WHERE account=?",
                (name,),
            ).fetchall()
            pend = conn.execute(
                "SELECT id,side,qty,symbol,order_type,limit_price,stop_price,"
                "trail_price,trail_percent,time_in_force FROM orders"
                " WHERE account=? AND status='pending'",
                (name,),
            ).fetchall()
            symbols |= {s for s, *_ in pos} | {order[3] for order in pend}
            data.append((name, cash, dep, real, pos, pend))
    return data, symbols, default


def fetch_quotes(symbols, executor=None):
    """{symbol: (last, prev_close) | None}. Options priced via chain (no prev_close).

    Option legs are grouped by (underlying, expiry) and each chain is downloaded
    once — a portfolio with several strikes on the same underlying/expiry (common
    with spreads, condors, butterflies) previously re-downloaded that whole chain
    once per leg, which dominated refresh time as position count grew.

    Pass a shared `executor` (e.g. from run_dashboard's live loop) to reuse one
    thread pool across refresh cycles instead of spawning and tearing down a
    fresh batch of OS threads every cycle -- that churn is real CPU/RAM
    overhead for a long-running background process, not just wasted work.
    """
    import yfinance as yf

    stocks, option_groups = [], {}
    for s in symbols:
        if pt.OCC_RE.match(s):
            root, expiry, strike, cp = pt.parse_occ(s)
            option_groups.setdefault((root, expiry), []).append((s, strike, cp))
        else:
            stocks.append(s)

    def get_stock(s):
        if pt._yf_backoff_active():
            return {s: None}
        try:
            fi = yf.Ticker(s).fast_info
            pc = fi.get("previousClose")
            return {s: (float(fi["lastPrice"]), float(pc) if pc else None)}
        except Exception as e:
            pt._yf_note_error(e)
            return {s: None}  # render as '?', retry next cycle

    def get_option_group(root, expiry, legs):
        if pt._yf_backoff_active():
            return {sym: None for sym, _strike, _cp in legs}
        try:
            chain = yf.Ticker(root).option_chain(expiry)
        except Exception as e:
            pt._yf_note_error(e)
            return {sym: None for sym, _strike, _cp in legs}
        out = {}
        for sym, strike, cp in legs:
            try:
                df = chain.calls if cp == "C" else chain.puts
                r = df[df.strike == strike].iloc[0]
                bid, ask, last = float(r.bid), float(r.ask), float(r.lastPrice)
                mid = (bid + ask) / 2 if bid > 0 and ask > 0 else last
                out[sym] = (mid, None) if mid > 0 else None
            except Exception:
                out[sym] = None
        return out

    def run(ex):
        quotes = {}
        futures = [ex.submit(get_stock, s) for s in stocks]
        futures += [
            ex.submit(get_option_group, root, expiry, legs)
            for (root, expiry), legs in option_groups.items()
        ]
        for fut in futures:
            quotes.update(fut.result())
        return quotes

    if executor is not None:
        return run(executor)
    # No shared pool given (e.g. a one-shot call) -- size one to the task
    # count same as before, capped well short of unbounded concurrency.
    task_count = len(stocks) + len(option_groups)
    with ThreadPoolExecutor(max_workers=min(16, max(1, task_count))) as ex:
        return run(ex)


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


def _pending_order_label(order):
    oid, side, qty, symbol, kind, limit, stop, trail, trail_pct, tif = order
    if kind == "limit":
        trigger = f"lim {limit:.2f}"
    elif kind == "stop":
        trigger = f"stop {stop:.2f}"
    elif kind == "stop_limit":
        trigger = f"stop {stop:.2f} / lim {limit:.2f}"
    elif kind == "trailing_stop":
        configured = f"{trail:.2f}" if trail is not None else f"{trail_pct:.2f}%"
        trigger = f"trail {configured}"
        if stop is not None:
            trigger += f" (stop {stop:.2f})"
    else:
        trigger = kind
    return f"#{oid} {side} {qty:g} {symbol} {trigger} {tif}"


_market_status = {"line": None, "computed_at": 0.0, "computing": False}
_market_status_lock = threading.Lock()


def _refresh_market_status():
    """Kick off a background computation of market open/closed, if not already running."""
    with _market_status_lock:
        if _market_status["computing"]:
            return
        _market_status["computing"] = True

    def worker():
        try:
            clock = pt.market_clock()
            transition = clock.get("next_transition") or {}
            eastern = transition.get("eastern", "")
            when = f" · next {clock['transition']} {eastern[11:16]} ET" if eastern else ""
            if clock["is_open"]:
                line = f"[green]●[/green]  Market open — NYSE{when}"
            else:
                line = f"[{GREY}]■[/{GREY}]  Market closed{when}"
        except Exception:
            line = f"[{GREY}]■[/{GREY}]  Market status unavailable"
        with _market_status_lock:
            _market_status["line"] = line
            _market_status["computed_at"] = time.monotonic()
            _market_status["computing"] = False

    threading.Thread(target=worker, daemon=True).start()


def _format_countdown(seconds):
    seconds = max(0, round(seconds))
    m, s = divmod(seconds, 60)
    return f"{m}:{s:02d}" if m else f"{s}s"


def _market_banner(keys, refreshing=False, next_refresh_at=None):
    # pt.market_clock() first-call cost (~0.5-0.7s: lazy pandas + exchange_calendars
    # import and NYSE calendar construction) used to block the very first frame.
    # Market open/closed barely changes, so compute it off-thread and show a
    # placeholder until it lands; a 60s TTL keeps it accurate without ever
    # blocking a redraw again.
    with _market_status_lock:
        line = _market_status["line"]
        stale = time.monotonic() - _market_status["computed_at"] > 60
    if line is None or stale:
        _refresh_market_status()
    status = line or "[dim]●  checking market status…[/dim]"
    stamp = datetime.now().strftime("%H:%M:%S")
    extra = ""
    if pt._yf_backoff_active():
        wait = max(0, round(pt._yf_rate_limited_until - time.monotonic()))
        extra = f"   [{RED}]⚠ Yahoo Finance rate limited — retrying in ~{wait}s[/{RED}]"
    elif refreshing:
        # Visible confirmation that r/t (or the periodic timer) actually
        # kicked off a fetch -- without this, a keypress that lands while a
        # fetch is already in flight is silently ignored (only one fetch
        # runs at a time) and gives no feedback either way.
        extra = "   [dim]⟳ refreshing quotes…[/dim]"
    elif next_refresh_at is not None:
        extra = f"   [dim]next refresh in {_format_countdown(next_refresh_at - time.monotonic())}[/dim]"
    status_line = Text.from_markup(f"{status}   [dim]as of {stamp}[/dim]{extra}")
    return Panel(Group(status_line, keys), border_style=RED)


def _account_totals(cash, deposits, realized, pos, quotes):
    equity, upnl_sum, day_sum = cash, 0.0, 0.0
    for sym, qty, avg, mult, ac, margin in pos:
        q = quotes.get(sym)
        if q is None:
            equity += margin if ac == "future" else qty * mult * avg
            continue
        px, prev_close = q
        upnl = qty * mult * (px - avg)
        mv = (upnl + margin) if ac == "future" else qty * mult * px
        equity += mv
        upnl_sum += upnl
        if prev_close:
            day_sum += qty * mult * (px - prev_close)
    total_pnl = realized + upnl_sum
    ret_pct = (total_pnl / deposits * 100) if deposits else 0.0
    return equity, day_sum, upnl_sum, total_pnl, ret_pct


def render_compact(data, quotes, default, refreshing=False, next_refresh_at=None):
    t = Table(expand=True, pad_edge=False, border_style=GREY, header_style=f"bold {RED}")
    for col, justify in [
        ("PORTFOLIO", "left"),
        ("CASH", "right"),
        ("EQUITY", "right"),
        ("DAY", "right"),
        ("UNREAL", "right"),
        ("REALIZED", "right"),
        ("TOTAL", "right"),
        ("RETURN", "right"),
        ("POS", "right"),
    ]:
        t.add_column(col, justify=justify)
    for name, cash, deposits, realized, pos, pend in data:
        equity, day_sum, upnl_sum, total_pnl, ret_pct = _account_totals(
            cash, deposits, realized, pos, quotes
        )
        star = " ★" if name == default else ""
        label = f"[bold]{name.upper()}{star}[/bold]"
        if pend:
            label += " [yellow]◌[/yellow]"
        t.add_row(
            label,
            f"{cash:,.2f}",
            f"{equity:,.2f}",
            _money(day_sum),
            _money(upnl_sum),
            _money(realized),
            _money(total_pnl),
            f"{ret_pct:+.2f}%",
            f"{len(pos)}",
        )
    if not data:
        t.add_row("[dim]no accounts[/dim]", "", "", "", "", "", "", "", "")

    keys = Text.from_markup(
        f"[dim][bold]v[/bold] Detail view  [{RED}]●[/{RED}]  [bold]b[/bold] Buy  [{RED}]●[/{RED}]  "
        f"[bold]s[/bold] Sell  [{RED}]●[/{RED}]  [bold]o[/bold] Option  [{RED}]●[/{RED}]  "
        f"[bold]n[/bold] New  [{RED}]●[/{RED}]  [bold]e[/bold] Rename  [{RED}]●[/{RED}]  "
        f"[bold]u[/bold] Switch  [{RED}]●[/{RED}]  [bold]t[/bold] Tick  [{RED}]●[/{RED}]  "
        f"[bold]r[/bold] Refresh  [{RED}]●[/{RED}]  [bold]q[/bold] Quit[/dim]"
    )
    return Group(
        Text.from_markup(f"[bold {RED}]{LOGO}[/bold {RED}]"),
        "",
        _market_banner(keys, refreshing=refreshing, next_refresh_at=next_refresh_at),
        Panel(t, title="[bold]ALL PORTFOLIOS[/bold]", title_align="left", border_style=RED),
    )


def render(
    data,
    quotes,
    default,
    prev=None,
    scroll=0,
    total=None,
    refreshing=False,
    next_refresh_at=None,
):
    prev = prev or {}
    total = len(data) if total is None else total
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
                fallback = margin if ac == "future" else qty * mult * avg
                equity += fallback
                t.add_row(
                    sym,
                    side,
                    f"{abs(qty):g}",
                    f"{avg:.2f}",
                    "?",
                    f"~{fallback:,.2f}",
                    "?",
                    "?",
                )
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
            lines = ", ".join(_pending_order_label(order) for order in pend)
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

    keys = Text.from_markup(
        f"[dim][bold]v[/bold] Compact view  [{RED}]●[/{RED}]  [bold]b[/bold] Buy  [{RED}]●[/{RED}]  "
        f"[bold]s[/bold] Sell  [{RED}]●[/{RED}]  "
        f"[bold]o[/bold] Option  [{RED}]●[/{RED}]  [bold]g[/bold] Backtesting & Graphs  [{RED}]●[/{RED}]  "
        f"[bold]c[/bold] Cancel  [{RED}]●[/{RED}]  [bold]n[/bold] New  [{RED}]●[/{RED}]  "
        f"[bold]e[/bold] Rename  [{RED}]●[/{RED}]  [bold]u[/bold] Switch  "
        f"[{RED}]●[/{RED}]  [bold]↑/↓[/bold] Scroll  [{RED}]●[/{RED}]  [bold]t[/bold] Tick  [{RED}]●[/{RED}]  "
        f"[bold]r[/bold] Refresh  [{RED}]●[/{RED}]  [bold]q[/bold] Quit[/dim]"
    )
    banner = _market_banner(keys, refreshing=refreshing, next_refresh_at=next_refresh_at)

    parts = [Text.from_markup(f"[bold {RED}]{LOGO}[/bold {RED}]"), "", banner, *panels, ""]
    if total > len(panels):
        shown_from = scroll + 1
        shown_to = scroll + len(panels)
        parts.append(
            Text.from_markup(
                f"[dim]portfolios {shown_from}-{shown_to} of {total} — ↑/↓ to scroll[/dim]"
            )
        )
    return Group(*parts)


_ARROWS = {b"A": "UP", b"B": "DOWN", b"C": "RIGHT", b"D": "LEFT"}
_pending_keys = b""


def _pop_key(buf):
    """Split one key (or arrow escape sequence) off the front of buf."""
    if buf[:1] == b"\x1b" and len(buf) >= 3 and buf[1:2] == b"[":
        code = _ARROWS.get(buf[2:3])
        if code:
            return code, buf[3:]
    try:
        return buf[:1].decode(), buf[1:]
    except UnicodeDecodeError:
        return None, buf[1:]


def read_key(timeout):
    """Return the next keypress, or None. Arrow keys decode to UP/DOWN/LEFT/RIGHT.

    Reads whatever bytes the terminal has buffered in one shot instead of
    polling byte-by-byte for the rest of an escape sequence — polling with
    short per-byte timeouts can miss the trailing bytes under any latency
    (ssh, tmux) and silently drop the keypress.
    """
    global _pending_keys
    if not sys.stdin.isatty():
        time.sleep(timeout)
        return None
    if not _pending_keys:
        r, _, _ = select.select([sys.stdin], [], [], timeout)
        if not r:
            return None
        _pending_keys = os.read(sys.stdin.fileno(), 64)
        if not _pending_keys:
            return None
    key, _pending_keys = _pop_key(_pending_keys)
    return key


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
    with pt.connection() as conn:
        default = (
            conn.execute(
                "SELECT value FROM config WHERE key='default_account'"
            ).fetchone()
            or [None]
        )[0]
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
    with pt.connection() as conn:
        default = (
            conn.execute(
                "SELECT value FROM config WHERE key='default_account'"
            ).fetchone()
            or [None]
        )[0]
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
    try:
        occ = pt.build_occ(underlying, expiry, strike, kind)
    except SystemExit as e:
        console.print(f"  [{RED}]{e}[/{RED}]")
        return
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
    with pt.connection() as conn:
        names = [n for (n,) in conn.execute("SELECT name FROM accounts")]
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


def _display_number(value, pattern=".2f", suffix=""):
    if value is None:
        return "—"
    return f"{value:{pattern}}{suffix}"


def _chart_group(values, color, width=36, height=9):
    if not values:
        return Group("[dim]No curve available.[/dim]")
    rows = braille_chart(values, width=width, height=height)
    return Group(
        f"[dim]{max(values):,.0f}[/dim]",
        *[f"[{color}]{row}[/{color}]" for row in rows],
        f"[dim]{min(values):,.0f}[/dim]",
    )


def backtesting_graphs_view(account, current_curve, backtest, current_metrics=None):
    """Build the side-by-side current performance and backtest display."""
    current = current_metrics or pt.performance_metrics(current_curve)
    current_values = [equity for _, equity in current_curve]
    current_color = (
        "green"
        if not current_values or current_values[-1] >= current_values[0]
        else RED
    )
    current_stats = Table.grid(expand=True, padding=(0, 1))
    current_stats.add_column(ratio=1)
    current_stats.add_column(ratio=1)
    if current:
        current_stats.add_row(
            f"[dim]EQUITY[/dim]\n[bold]{current['start_eq']:,.0f} → {current['end_eq']:,.0f}[/bold]",
            f"[dim]RETURN[/dim]\n{_pct(current['total'])}",
        )
        current_stats.add_row(
            f"[dim]CAGR[/dim]\n{_pct(current['cagr'])}",
            f"[dim]MAX DD[/dim]\n[{RED}]{current['mdd'] * 100:.2f}%[/{RED}]",
        )
        current_stats.add_row(
            f"[dim]SHARPE / SORTINO[/dim]\n[bold]{current['sharpe']:.2f} / {current['sortino']:.2f}[/bold]",
            f"[dim]VOL (ANN.)[/dim]\n[bold]{current['vol'] * 100:.1f}%[/bold]",
        )
        current_range = (
            f"[dim]{current['start']} → {current['end']} ({current['days']}d)[/dim]"
        )
    else:
        current_stats.add_row("[dim]No recorded activity yet.[/dim]", "")
        current_range = "[dim]Waiting for the first account event.[/dim]"
    current_panel = Panel(
        Group(
            current_range,
            "",
            _chart_group(current_values, current_color),
            "",
            current_stats,
        ),
        title="[bold]CURRENT PERFORMANCE[/bold]",
        title_align="left",
        border_style=GREY,
    )

    status = backtest.get("status")
    if status == "ok":
        metrics = backtest["metrics"]
        backtest_values = [point["equity"] for point in backtest["curve"]]
        backtest_color = (
            "green"
            if not backtest_values or backtest_values[-1] >= backtest_values[0]
            else RED
        )
        backtest_stats = Table.grid(expand=True, padding=(0, 1))
        backtest_stats.add_column(ratio=1)
        backtest_stats.add_column(ratio=1)
        backtest_stats.add_row(
            "[dim]EQUITY[/dim]\n[bold]"
            f"{metrics['initial_equity']:,.0f} → {metrics['final_equity']:,.0f}[/bold]",
            "[dim]RETURN[/dim]\n[bold]"
            f"{_display_number(metrics['return_pct'], '+.2f', '%')}[/bold]",
        )
        backtest_stats.add_row(
            "[dim]CAGR[/dim]\n[bold]"
            f"{_display_number(metrics['cagr_pct'], '+.2f', '%')}[/bold]",
            f"[dim]MAX DD[/dim]\n[{RED}]"
            f"{_display_number(metrics['max_drawdown_pct'], '.2f', '%')}[/{RED}]",
        )
        backtest_stats.add_row(
            "[dim]SHARPE / SORTINO[/dim]\n[bold]"
            f"{_display_number(metrics['sharpe'])} / "
            f"{_display_number(metrics['sortino'])}[/bold]",
            "[dim]VOL / COSTS[/dim]\n[bold]"
            f"{_display_number(metrics['annual_volatility_pct'], '.1f', '%')} / "
            f"{_display_number(metrics['commissions'], ',.2f')}[/bold]",
        )
        backtest_body = Group(
            f"[dim]{backtest['start']} → {backtest['end']} ({backtest['bars']} bars)[/dim]",
            "",
            _chart_group(backtest_values, backtest_color),
            "",
            backtest_stats,
        )
    else:
        backtest_body = Group(
            f"[dim]{backtest.get('start', '')} → {backtest.get('end', '')}[/dim]",
            "",
            f"[{RED}]{backtest.get('message', 'Backtest unavailable.')}[/{RED}]",
            "",
            "[dim]Open positions in this portfolio will automatically become the backtest universe.[/dim]",
        )
    backtest_panel = Panel(
        backtest_body,
        title="[bold]CURRENT PORTFOLIO BACKTEST[/bold]",
        title_align="left",
        border_style=RED,
    )

    comparison = Table.grid(expand=True, padding=(0, 1))
    comparison.add_column(ratio=1)
    comparison.add_column(ratio=1)
    comparison.add_row(current_panel, backtest_panel)

    symbols = ", ".join(item["symbol"] for item in backtest.get("symbols", []))
    skipped = ", ".join(
        f"{item['symbol']} ({item['reason']})" for item in backtest.get("skipped", [])
    )
    notes = Table.grid(expand=True)
    notes.add_column(style="dim", width=12)
    notes.add_column(ratio=1)
    notes.add_row("PORTFOLIO", account)
    notes.add_row("UNIVERSE", symbols or "no eligible open positions")
    if skipped:
        notes.add_row("SKIPPED", skipped)
    notes.add_row("MODEL", backtest.get("hypothesis", ""))
    notes.add_row(
        "COSTS",
        f"{backtest.get('commission_bps', 0):g} bps at synthetic entry and exit",
    )
    if backtest.get("warnings"):
        notes.add_row("CAUTION", " ".join(backtest["warnings"]))
    return Group(
        f"[bold {RED}]BACKTESTING & GRAPHS — {account.upper()}[/bold {RED}]",
        "",
        comparison,
        Panel(notes, title="[bold]BACKTEST DEFINITION[/bold]", border_style=GREY),
    )


def prompt_backtesting_graphs(console, account_filter):
    with pt.connection() as conn:
        names = [n for (n,) in conn.execute("SELECT name FROM accounts")]
        default = (
            conn.execute(
                "SELECT value FROM config WHERE key='default_account'"
            ).fetchone()
            or [None]
        )[0]
    if not names:
        return
    console.print(
        f"\n[bold {RED}]▚ Backtesting & Graphs[/bold {RED}]  [dim]{', '.join(names)}[/dim]"
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
    import portfolio_backtest as pbt

    history = console.input(
        "  history [dim](6m/1y/2y/5y/10y/max or days)[/dim] [5y]: "
    ).strip()
    try:
        lookback_days = pbt.parse_lookback_days(history)
    except SystemExit as exc:
        console.print(f"  [{RED}]{exc}[/{RED}]")
        time.sleep(1.5)
        return
    console.print(
        "  [dim]reconstructing current performance and backtesting this "
        f"portfolio's open positions over up to {lookback_days:,} days…[/dim]"
    )

    history_cache = pbt.YahooHistoryCache()

    def cached_closes(symbols, start, end):
        return history_cache.daily_closes(symbols, start, end, exclude=pt.OCC_RE.match)

    conn = pt.db()
    try:
        curve, current_metrics = pt.account_performance(
            conn, account, closes_fn=cached_closes, live=True
        )
        backtest = pbt.run_portfolio_backtest(
            conn,
            account,
            lookback_days=lookback_days,
            history_fn=history_cache.history,
        )
    except SystemExit as exc:
        console.print(f"  [{RED}]{exc}[/{RED}]")
        time.sleep(1.5)
        return
    finally:
        conn.close()
    console.clear()
    console.print(
        backtesting_graphs_view(
            account, curve, backtest, current_metrics=current_metrics
        )
    )
    console.input("\n[dim]press Enter to return[/dim]")


# Compatibility for callers that imported the previous dashboard helper.
prompt_graph = prompt_backtesting_graphs


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
    with pt.connection() as conn:
        names = [n for (n,) in conn.execute("SELECT name FROM accounts")]
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
        # One pool reused for the whole session instead of spawning/tearing
        # down a fresh batch of OS threads every refresh cycle -- real CPU
        # and memory churn for a background process that's meant to sit
        # around all day. 16 concurrent requests is a deliberate balance:
        # plenty of speedup over sequential fetching without holding open an
        # unreasonable number of connections/threads at once.
        with (
            Live(console=console, screen=True, auto_refresh=False) as live,
            ThreadPoolExecutor(max_workers=16) as pool,
        ):
            prev = {}
            scroll = 0
            compact = False
            prices = {}
            quotes = {}  # seeded from SQLite below for an instant offline first paint
            pending = None  # set below; declared here so the first redraw() can read it
            next_refresh_at = None  # ditto -- set once the first fetch lands
            data, symbols, default = snapshot(args.account)
            cached = pt.features.cached_prices(pt.DB, symbols)
            quotes = {
                symbol: (mark["price"], None)
                for symbol, mark in cached.items()
            }
            prices = {symbol: quote[0] for symbol, quote in quotes.items()}

            def redraw():
                max_scroll = max(0, len(data) - PAGE_SIZE)
                s = min(scroll, max_scroll)
                refreshing = pending is not None
                if compact:
                    live.update(
                        render_compact(
                            data,
                            quotes,
                            default,
                            refreshing=refreshing,
                            next_refresh_at=next_refresh_at,
                        ),
                        refresh=True,
                    )
                else:
                    visible = data[s : s + PAGE_SIZE]
                    live.update(
                        render(
                            visible,
                            quotes,
                            default,
                            prev,
                            scroll=s,
                            total=len(data),
                            refreshing=refreshing,
                            next_refresh_at=next_refresh_at,
                        ),
                        refresh=True,
                    )

            def start_fetch(syms):
                """Run fetch_quotes on a plain background thread (not a pool
                worker, so it can freely use `pool` for its own sub-tasks
                without a self-submission wait) and return a mutable holder
                the main loop polls without ever blocking on it."""
                holder = {"done": False, "quotes": None, "started_at": time.monotonic()}

                def worker():
                    try:
                        holder["quotes"] = fetch_quotes(syms, executor=pool)
                    except Exception:
                        holder["quotes"] = {}
                    holder["done"] = True

                threading.Thread(target=worker, daemon=True).start()
                return holder

            # Paint immediately with last-known prices (blank '?' on the very
            # first run) instead of leaving the screen empty while quotes
            # fetch over the network.
            redraw()
            pending = start_fetch(symbols)
            last_tick_redraw = time.monotonic()

            while True:
                # A short, constant poll — never the multi-second wait a
                # blocking fetch used to impose — so keys are always read
                # promptly, including while a refresh is in flight in the
                # background. This is the actual fix for "sometimes doesn't
                # respond": the old loop simply wasn't reading input for the
                # 1-2s a fetch was running, every single refresh cycle. That
                # dead zone was a control-flow bug, not a raw-speed one — the
                # same blocking structure would feel identical in any
                # language, so this is fixed here rather than by a rewrite.
                key = read_key(0.15)

                # Keep one in-flight generation at a time. Older versions
                # discarded the holder after 45 seconds while its daemon
                # thread and pool jobs kept running, allowing repeated stalls
                # to accumulate workers. The UI remains responsive while this
                # holder is pending, and a late result is accepted instead of
                # creating an unbounded sequence of abandoned refreshes.
                if pending is not None and not pending["done"]:
                    if time.monotonic() - pending["started_at"] > 45.0:
                        pending["timed_out"] = True

                if pending is not None and pending["done"]:
                    quotes = pending["quotes"]
                    prices = {s: q[0] for s, q in quotes.items() if q}
                    today = datetime.now().strftime("%Y-%m-%d")
                    expired = any(
                        pt.OCC_RE.match(sym) and pt.parse_occ(sym)[1] < today
                        for *_, pos, _pend in data
                        for sym, *_ in pos
                    )
                    if expired or any(pend for *_, pend in data):
                        run_tick(prices)
                        data, symbols, default = snapshot(args.account)
                    prev = prices or prev
                    pending = None
                    next_refresh_at = time.monotonic() + args.interval
                    redraw()

                if pending is None and time.monotonic() >= next_refresh_at:
                    pending = start_fetch(symbols)

                # Keep the "next refresh in Ns" countdown actually ticking
                # even when nothing else changed -- every other redraw() call
                # above only fires on a real state change (fetch landed, key
                # pressed), which would otherwise leave the displayed number
                # stale between those events.
                if next_refresh_at is not None and time.monotonic() - last_tick_redraw >= 1.0:
                    last_tick_redraw = time.monotonic()
                    redraw()

                if key is None:
                    continue
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
                        "g": "backtesting_graphs",
                        "e": "rename",
                    }[key]
                if key == "v":
                    compact = not compact
                    redraw()
                elif key == "DOWN" and not compact:
                    max_scroll = max(0, len(data) - PAGE_SIZE)
                    scroll = min(scroll + 1, max_scroll)
                    redraw()
                elif key == "UP" and not compact:
                    scroll = max(scroll - 1, 0)
                    redraw()
                elif key in ("t", "r") and pending is None:
                    if key == "t":
                        run_tick(prices)
                    pending = start_fetch(symbols)
    finally:
        if tty_attrs:
            termios.tcsetattr(sys.stdin, termios.TCSADRAIN, tty_attrs)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-a", "--account")
    ap.add_argument(
        "-n",
        "--interval",
        type=float,
        default=900.0,
        help="seconds between automatic price refreshes (default 900 = 15min; "
        "press t/r for an on-demand refresh any time)",
    )
    args = ap.parse_args()

    console = Console()
    with pt.connection() as conn:
        done = conn.execute("SELECT 1 FROM config WHERE key='setup_done'").fetchone()
        empty = conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 0
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
        elif action == "backtesting_graphs":
            prompt_backtesting_graphs(console, args.account)
        elif action == "rename":
            prompt_rename(console)
        elif action == "cancel":
            prompt_cancel(console)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
