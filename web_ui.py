#!/usr/bin/env python3
"""FastAPI web backend for TradingCLI paper-trading.

Serves the REST API at /api/* and the single-page frontend at /.
Run with:  python web_ui.py          (port 8080)
           uvicorn web_ui:app --port 8080
"""

from __future__ import annotations

import asyncio
import atexit
import contextlib
import os
import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi import FastAPI, Query, Request, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

# ---------------------------------------------------------------------------
# Imports from the same directory
# ---------------------------------------------------------------------------
_DIR = Path(__file__).resolve().parent
if str(_DIR) not in sys.path:
    sys.path.insert(0, str(_DIR))

import papertrade as pt   # noqa: E402
import portfolio_backtest as pbt  # noqa: E402

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
app = FastAPI(title="TradingCLI", version="1.0.0")
# CORS: same-origin frontend needs no CORS; allow localhost for dev only.
# Do NOT use allow_origins=["*"] with mutating routes and no auth.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:8080",
        "http://127.0.0.1:8080",
        "http://localhost:3000",
        "http://127.0.0.1:3000",
    ],
    allow_methods=["*"],
    allow_headers=["*"],
)

_pool = ThreadPoolExecutor(max_workers=8)
atexit.register(lambda: _pool.shutdown(wait=False))

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ok(data=None):
    return {"ok": True, "data": data}

def _err(error: str):
    return JSONResponse({"ok": False, "error": str(error)}, status_code=400)

@contextlib.contextmanager
def _get_conn():
    """Yield a SQLite connection (check_same_thread=False for web) and always close it."""
    conn = sqlite3.connect(pt._db_path() if hasattr(pt, '_db_path') else pt.DB, timeout=10, isolation_level=None, check_same_thread=False)
    try:
        conn.execute("PRAGMA busy_timeout=10000")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        yield conn
    finally:
        try:
            conn.close()
        except Exception:
            pass

def _resolve(conn, account: str | None) -> str:
    """Resolve account name or fall back to default."""
    return pt.resolve_account(conn, account)

async def _run(fn, *a, **kw):
    """Run a blocking function in the thread pool, catching SystemExit."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = asyncio.get_event_loop()
    def _work():
        try:
            return fn(*a, **kw)
        except SystemExit as exc:
            raise RuntimeError(str(exc))
    return await loop.run_in_executor(_pool, _work)

async def _safe_run(fn, *a, **kw):
    """Run fn in thread pool; return _ok(data) or _err(error)."""
    try:
        data = await _run(fn, *a, **kw)
        return _ok(data)
    except RuntimeError as exc:
        return _err(str(exc))

def _safe(fn, *a, **kw):
    """Call fn; convert SystemExit → _err."""
    try:
        return _ok(fn(*a, **kw))
    except SystemExit as exc:
        return _err(str(exc))
    except Exception as exc:
        return _err(f"{type(exc).__name__}: {exc}")

# ---------------------------------------------------------------------------
# Static / Frontend
# ---------------------------------------------------------------------------

_STATIC_DIR = _DIR / "static"

# Serve files under /static/*
if _STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

@app.get("/", response_class=HTMLResponse)
async def index():
    """Serve the SPA frontend."""
    idx = _STATIC_DIR / "index.html"
    if idx.exists():
        return HTMLResponse(idx.read_text())
    return HTMLResponse(
        "<html><body><h1>TradingCLI</h1>"
        "<p>Frontend not built yet. Place <code>static/index.html</code>.</p>"
        "</body></html>",
        status_code=200,
    )

# ---------------------------------------------------------------------------
# ACCOUNTS
# ---------------------------------------------------------------------------

@app.get("/api/accounts")
async def api_accounts():
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT name,cash,deposits,realized,created FROM accounts ORDER BY name"
        ).fetchall()
        if not rows:
            return _ok([])
        # Gather all symbols in one batch so marks load concurrently, not N sequential calls.
        all_positions: dict[str, list] = {}
        all_symbols: set[str] = set()
        for name, cash, deposits, realized, created in rows:
            positions = conn.execute(
                "SELECT symbol,qty,avg_cost,mult,asset_class,margin "
                "FROM positions WHERE account=?", (name,)
            ).fetchall()
            all_positions[name] = positions
            for sym, *_ in positions:
                all_symbols.add(sym)
        marks: dict[str, float | None] = {}
        if all_symbols:
            try:
                raw = await _run(pt.batch_prices, list(all_symbols), ignore_errors=True)
                marks = {s: v for s, v in raw.items() if v is not None}
            except RuntimeError:
                marks = {}
        accounts = []
        for name, cash, deposits, realized, created in rows:
            positions = all_positions[name]
            equity = cash
            unrealized = 0.0
            for sym, qty, avg, mult, ac, margin in positions:
                px = marks.get(sym)
                if px is None:
                    px = avg
                u = qty * mult * (px - avg)
                unrealized += u
                equity += (u + margin) if ac == "future" else qty * mult * px
            total_pnl = realized + unrealized
            return_pct = total_pnl / deposits * 100 if deposits else 0.0
            accounts.append({
                "name": name,
                "cash": cash,
                "deposits": deposits,
                "realized": realized,
                "created": created,
                "equity": equity,
                "unrealized_pnl": unrealized,
                "day_pnl": 0.0,
                "total_pnl": total_pnl,
                "return_pct": return_pct,
            })
        return _ok(accounts)


@app.post("/api/accounts")
async def api_create_account(request: Request):
    body = await request.json()
    name = body.get("name", "").strip()
    cash = float(body.get("cash", 0))
    if not name:
        return _err("name required")
    with _get_conn() as conn:
        return _safe(pt.create_account, conn, name, cash, source="web")


@app.post("/api/accounts/{name}/default")
async def api_set_default(name: str):
    with _get_conn() as conn:
        return _safe(pt.set_default, conn, name, source="web")


@app.post("/api/accounts/{name}/deposit")
async def api_deposit(name: str, request: Request):
    body = await request.json()
    amount = float(body.get("amount", 0))
    with _get_conn() as conn:
        return _safe(pt.adjust_cash, conn, name, amount, source="web")


@app.delete("/api/accounts/{name}")
async def api_wipe_account(name: str):
    with _get_conn() as conn:
        return _safe(pt.wipe_account, conn, name, source="web")


# ---------------------------------------------------------------------------
# POSITIONS
# ---------------------------------------------------------------------------

@app.get("/api/positions")
async def api_positions(account: str | None = Query(None)):
    with _get_conn() as conn:
        try:
            name = _resolve(conn, account)
        except SystemExit as exc:
            return _err(str(exc))
        try:
            positions = await _run(pt.list_positions, conn, name)
        except RuntimeError as exc:
            return _err(str(exc))
        result = []
        for p in positions:
            result.append({
                "symbol": p["symbol"],
                "side": p["side"],
                "qty": p["qty"],
                "avg_cost": p["avg_entry_price"],
                "current_price": p["current_price"],
                "market_value": p["market_value"],
                "unrealized_pnl": p["unrealized_pl"],
                "pnl_pct": (p["unrealized_pl"] / (p["qty"] * p["avg_entry_price"]) * 100
                            if p["qty"] and p["avg_entry_price"] else 0),
                "asset_class": p["asset_class"],
            })
        return _ok(result)


@app.post("/api/positions/close")
async def api_close_position(request: Request):
    body = await request.json()
    account = body.get("account")
    symbol = body.get("symbol", "").upper()
    qty = body.get("qty")
    percent = body.get("percent")
    if not symbol:
        return _err("symbol required")
    with _get_conn() as conn:
        try:
            name = _resolve(conn, account)
        except SystemExit as exc:
            return _err(str(exc))
        return _safe(
            pt.close_position, conn, name, symbol,
            qty=qty, percent=percent, source="web",
        )


@app.post("/api/positions/close-all")
async def api_close_all(request: Request):
    body = await request.json()
    account = body.get("account")
    with _get_conn() as conn:
        try:
            name = _resolve(conn, account)
        except SystemExit as exc:
            return _err(str(exc))
        return _safe(pt.close_all_positions, conn, name, source="web")


# ---------------------------------------------------------------------------
# ORDERS
# ---------------------------------------------------------------------------

@app.get("/api/orders")
async def api_orders(
    account: str | None = Query(None),
    status: str | None = Query(None),
):
    with _get_conn() as conn:
        try:
            name = _resolve(conn, account)
        except SystemExit as exc:
            return _err(str(exc))
        query = "SELECT id,account,symbol,side,qty,limit_price,status,filled_price,ts," \
                "order_type,stop_price,trail_percent,time_in_force " \
                "FROM orders WHERE account=?"
        params: list = [name]
        if status:
            query += " AND status=?"
            params.append(status)
        query += " ORDER BY id DESC LIMIT 200"
        rows = conn.execute(query, params).fetchall()
        orders = []
        for (oid, acct, sym, side, qty, limit_px, st, filled_px, ts,
             otype, stop_px, trail_pct, tif) in rows:
            orders.append({
                "id": oid,
                "account": acct,
                "symbol": sym,
                "side": side,
                "qty": qty,
                "order_type": otype,
                "limit_price": limit_px,
                "stop_price": stop_px,
                "trail_percent": trail_pct,
                "time_in_force": tif,
                "status": st,
                "ts": ts,
            })
        return _ok(orders)


@app.post("/api/orders")
async def api_place_order(request: Request):
    body = await request.json()
    account = body.get("account")
    symbol = body.get("symbol", "").upper()
    side = body.get("side", "buy").lower()
    qty = float(body.get("qty", 0))
    order_type = body.get("order_type", "market")
    limit_price = body.get("limit_price")
    stop_price = body.get("stop_price")
    trail_percent = body.get("trail_percent")
    time_in_force = body.get("time_in_force", "gtc")

    if not symbol:
        return _err("symbol required")
    if qty <= 0:
        return _err("qty must be > 0")

    with _get_conn() as conn:
        try:
            name = _resolve(conn, account)
        except SystemExit as exc:
            return _err(str(exc))

        if order_type == "market" and not limit_price and not stop_price:
            return _safe(
                pt.place, conn, name, symbol, side, qty, source="web",
            )
        return _safe(
            pt.submit_order, conn, name, symbol, side, qty,
            order_type=order_type,
            limit_price=limit_price,
            stop_price=stop_price,
            trail_percent=trail_percent,
            time_in_force=time_in_force,
            source="web",
        )


@app.post("/api/orders/{order_id}/cancel")
async def api_cancel_order(order_id: int):
    with _get_conn() as conn:
        return _safe(pt.cancel, conn, order_id, source="web")


@app.post("/api/orders/cancel-all")
async def api_cancel_all_orders(request: Request):
    body = await request.json()
    account = body.get("account")
    with _get_conn() as conn:
        try:
            name = _resolve(conn, account)
        except SystemExit as exc:
            return _err(str(exc))
        return _safe(pt.cancel_all_orders, conn, name, source="web")


# ---------------------------------------------------------------------------
# WATCHLISTS
# ---------------------------------------------------------------------------

@app.get("/api/watchlists")
async def api_watchlists(account: str | None = Query(None)):
    with _get_conn() as conn:
        try:
            name = _resolve(conn, account)
        except SystemExit as exc:
            return _err(str(exc))
        try:
            watchlists = await _run(pt.list_watchlists, conn, name)
        except RuntimeError as exc:
            return _err(str(exc))
        # Fetch all watchlist quotes concurrently instead of sequentially.
        async def _enrich(wl):
            try:
                enriched = await _run(pt.watchlist_quotes, conn, name, wl["id"])
                symbols = []
                for q in enriched.get("quotes", []):
                    symbols.append({
                        "symbol": q["symbol"],
                        "price": q.get("price"),
                        "change": None,
                        "change_pct": None,
                    })
                return {
                    "id": wl["id"],
                    "name": wl["name"],
                    "symbols": symbols,
                }
            except RuntimeError:
                return {
                    "id": wl["id"],
                    "name": wl["name"],
                    "symbols": [],
                }
        if not watchlists:
            return _ok([])
        results = await asyncio.gather(*[_enrich(wl) for wl in watchlists])
        return _ok(list(results))


@app.post("/api/watchlists")
async def api_create_watchlist(request: Request):
    body = await request.json()
    account = body.get("account")
    wl_name = body.get("name", "").strip()
    symbols = body.get("symbols", [])
    if not wl_name:
        return _err("name required")
    with _get_conn() as conn:
        try:
            name = _resolve(conn, account)
        except SystemExit as exc:
            return _err(str(exc))
        return _safe(pt.create_watchlist, conn, name, wl_name, symbols=symbols, source="web")


@app.delete("/api/watchlists/{wl_id}")
async def api_delete_watchlist(wl_id: int, account: str | None = Query(None)):
    with _get_conn() as conn:
        try:
            name = _resolve(conn, account)
        except SystemExit as exc:
            return _err(str(exc))
        return _safe(pt.delete_watchlist, conn, name, wl_id, source="web")


# ---------------------------------------------------------------------------
# MARKET
# ---------------------------------------------------------------------------

@app.get("/api/market/status")
async def api_market_status():
    return _safe(pt.market_clock)


@app.get("/api/market/quote/{symbol}")
async def api_quote(symbol: str):
    try:
        quote = await _run(pt.latest_quote, symbol)
    except RuntimeError as exc:
        return _err(str(exc))
    # Fetch history concurrently (quote already resolved); keep only one bars call.
    prev_close = None
    volume = None
    try:
        history = await _run(pt.market_history, symbol, "bars", timeframe="1Day", limit=2)
        bars = history.get("data", [])
        if len(bars) >= 2:
            prev_close = bars[-2].get("close")
            volume = bars[-1].get("volume")
        elif bars:
            volume = bars[-1].get("volume")
    except RuntimeError:
        pass
    price = quote.get("last", 0)
    change = price - prev_close if prev_close else None
    change_pct = (change / prev_close * 100) if prev_close and prev_close != 0 else None
    return _ok({
        "symbol": quote["symbol"],
        "price": price,
        "change": change,
        "change_pct": change_pct,
        "previous_close": prev_close,
        "volume": volume,
    })


@app.get("/api/market/history/{symbol}")
async def api_market_history(
    symbol: str,
    timeframe: str = Query("1Day"),
    limit: int = Query(30),
):
    try:
        result = await _run(pt.market_history, symbol, "bars", timeframe=timeframe, limit=limit)
    except RuntimeError as exc:
        return _err(str(exc))
    data = result.get("data", [])
    # Normalize to {date, open, high, low, close, volume}
    bars = []
    for bar in data:
        bars.append({
            "date": bar.get("timestamp", ""),
            "open": bar.get("open"),
            "high": bar.get("high"),
            "low": bar.get("low"),
            "close": bar.get("close"),
            "volume": bar.get("volume"),
        })
    return _ok(bars)


@app.get("/api/market/news/{symbol}")
async def api_market_news(symbol: str, limit: int = Query(5)):
    try:
        items = await _run(pt.market_news, symbol, limit=limit)
    except RuntimeError as exc:
        return _err(str(exc))
    result = []
    for item in items:
        result.append({
            "title": item.get("title"),
            "url": item.get("url"),
            "publisher": item.get("publisher"),
            "published_at": item.get("published"),
        })
    return _ok(result)


# ---------------------------------------------------------------------------
# BACKTEST / PERFORMANCE
# ---------------------------------------------------------------------------

@app.get("/api/backtest")
async def api_backtest(
    account: str | None = Query(None),
    days: int = Query(3650),
):
    with _get_conn() as conn:
        try:
            name = _resolve(conn, account)
        except SystemExit as exc:
            return _err(str(exc))
        try:
            result = await _run(pbt.run_portfolio_backtest, conn, name, lookback_days=days)
        except RuntimeError as exc:
            return _err(str(exc))
        # Normalize curve to [{date, equity}]
        curve = [{"date": c[0], "equity": c[1]} for c in result.get("curve", [])]
        return _ok({
            "status": result.get("status"),
            "metrics": result.get("metrics", {}),
            "curve": curve,
            "symbols": result.get("symbols", []),
            "skipped": result.get("skipped", []),
        })


@app.get("/api/equity-curve")
async def api_equity_curve(account: str | None = Query(None)):
    with _get_conn() as conn:
        try:
            name = _resolve(conn, account)
        except SystemExit as exc:
            return _err(str(exc))
        try:
            curve = await _run(pt.equity_curve, conn, name, live=True)
        except RuntimeError as exc:
            return _err(str(exc))
        return _ok([{"date": c[0], "equity": c[1]} for c in curve])


@app.get("/api/performance")
async def api_performance(account: str | None = Query(None), benchmark: str | None = Query(None)):
    with _get_conn() as conn:
        try:
            name = _resolve(conn, account)
        except SystemExit as exc:
            return _err(str(exc))
        try:
            curve, metrics = await _run(pt.account_performance, conn, name, live=True)
        except RuntimeError as exc:
            return _err(str(exc))
        if benchmark:
            try:
                b = await _run(pt.benchmark_history, benchmark, metrics.get("start","")[:10], metrics.get("end","")[:10])
                if b and len(b) > 1:
                    b_sorted = sorted(b.items())
                    b_ret = (b_sorted[-1][1] / b_sorted[0][1] - 1) * 100 if b_sorted[0][1] else None
                    metrics["benchmark"] = benchmark
                    metrics["benchmark_return"] = b_ret
                    if b_ret is not None and isinstance(metrics.get("total"), float):
                        metrics["alpha"] = metrics["total"]*100 - b_ret
            except RuntimeError:
                pass
        return _ok(metrics)


# ---------------------------------------------------------------------------
# RISK
# ---------------------------------------------------------------------------

@app.get("/api/risk/{account}")
async def api_get_risk(account: str):
    with _get_conn() as conn:
        return _safe(pt.risk_limits, conn, account)


@app.put("/api/risk/{account}")
async def api_set_risk(account: str, request: Request):
    body = await request.json()
    with _get_conn() as conn:
        return _safe(
            pt.set_risk_limits, conn, account,
            allow_short=body.get("allow_short"),
            allow_naked_options=body.get("allow_naked_options"),
            max_gross_leverage=body.get("max_gross_leverage"),
            max_order_notional=body.get("max_order_notional"),
            borrow_bps=body.get("borrow_bps"),
            commission_bps=body.get("commission_bps"),
            slippage_bps=body.get("slippage_bps"),
            allow_fractional=body.get("allow_fractional"),
            source="web",
        )


# ---------------------------------------------------------------------------
# UTILS
# ---------------------------------------------------------------------------

@app.get("/api/events")
async def api_events(account: str | None = Query(None), since_id: int = Query(0), limit: int = Query(100)):
    with _get_conn() as conn:
        try:
            evs = pt.list_events(conn, account, since_id=since_id, limit=limit)
            return _ok(evs)
        except SystemExit as exc:
            return _err(str(exc))

@app.get("/api/events/stream")
async def api_events_stream(account: str | None = Query(None), since_id: int = Query(0)):
    from fastapi.responses import StreamingResponse
    import json as _json
    async def gen():
        last = since_id
        for _ in range(30):
            with _get_conn() as conn:
                try:
                    evs = pt.list_events(conn, account, since_id=last, limit=100)
                except SystemExit:
                    evs = []
            for ev in evs:
                last = max(last, ev.get("id", last))
                yield f"data: {_json.dumps(ev)}\n\n"
            import asyncio as _aio
            await _aio.sleep(1)
        yield "data: {\"done\": true}\n\n"
    return StreamingResponse(gen(), media_type="text/event-stream")

@app.get("/api/health")
async def api_health():
    with _get_conn() as conn:
        result = pt.healthcheck(conn)
    # market_clock is independent of DB — fetch after conn is closed so we don't hold a WAL reader during a slow Yahoo/calendar call
    try:
        mc = await _run(pt.market_clock)
    except RuntimeError:
        mc = {}
    result["market_clock"] = mc
    return _ok(result)


@app.get("/api/config")
async def api_config():
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT value FROM config WHERE key='default_account'"
        ).fetchone()
        return _ok({"default_account": row[0] if row else None})

@app.get("/api/config/list")
async def api_config_list():
    with _get_conn() as conn:
        return _ok(pt.config_list(conn))

@app.get("/api/config/{key}")
async def api_config_get(key: str):
    with _get_conn() as conn:
        return _safe(pt.config_get, conn, key)

@app.put("/api/config/{key}")
async def api_config_set(key: str, request: Request):
    body = await request.json()
    value = str(body.get("value", ""))
    with _get_conn() as conn:
        return _safe(pt.config_set, conn, key, value, source="web")

@app.delete("/api/config/{key}")
async def api_config_delete(key: str):
    with _get_conn() as conn:
        return _safe(pt.config_delete, conn, key, source="web")


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

@app.get("/api/export")
async def api_export(account: str | None = Query(None), limit: int = Query(5000), format: str = Query("csv")):
    with _get_conn() as conn:
        try:
            name = _resolve(conn, account)
        except SystemExit as exc:
            return _err(str(exc))
        if format == "parquet":
            try:
                data = pt.export_history(conn, name, limit, fmt="parquet")
                from fastapi.responses import Response
                return Response(content=data, media_type="application/octet-stream", headers={"Content-Disposition": f"attachment; filename={name}-orders.parquet"})
            except SystemExit as exc:
                return _err(str(exc))
        try:
            csv_data = pt.trade_history_csv(conn, name, limit)
            return _ok({"csv": csv_data})
        except SystemExit as exc:
            return _err(str(exc))

@app.post("/api/import")
async def api_import(request: Request):
    body = await request.json()
    account = body.get("account")
    data = body.get("data", "")
    fmt = body.get("format", "csv")
    if not account or not data:
        return _err("account and data required")
    with _get_conn() as conn:
        try:
            name = _resolve(conn, account)
        except SystemExit as exc:
            return _err(str(exc))
        try:
            import base64
            raw = base64.b64decode(data) if fmt == "parquet" and isinstance(data, str) and len(data) > 100 and not data.strip().startswith("id,") else data
            count = pt.import_history(conn, name, raw, fmt=fmt, source="web")
            return _ok({"imported": count})
        except SystemExit as exc:
            return _err(str(exc))
        except Exception as exc:
            return _err(str(exc))

@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket):
    await websocket.accept()
    try:
        while True:
            with _get_conn() as conn:
                health = pt.healthcheck(conn)
            await websocket.send_json({"type": "health", "data": health})
            import asyncio as _aio2
            await _aio2.sleep(5)
    except Exception:
        try:
            await websocket.close()
        except Exception:
            pass

def main():
    import uvicorn
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", 8080))
    print(f"TradingCLI web server starting on http://{host}:{port}")
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
