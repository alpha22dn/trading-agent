"""Live quotes for the PaperBroker: LTP, day high/low, depth and circuit limits from Kite.

Decided 2026-10-01: prices come from Kite's WebSocket stream (broker/ticker.py) when it is healthy; the REST quote
is the fallback (stream not running, stalled, or a stock with no tick yet on this connection) and the source of
circuit limits, which the stream does not carry (cached per stock, refreshed every 30 minutes).

Subscriptions (decided 2026-10-01): every NSE500 stock plus Nifty 50 on every connection (`universe_tokens`), and
any other stock a caller asks a price for. When the NSE500 list changes during the day, `subscribe_universe()`
adds the new members at once.
"""
from __future__ import annotations

import logging
import threading
import time as _time
from typing import Any, Callable

from .base import Quote
from .kite_session import get_kite, invalidate, is_token_error

MAX_PER_CALL = 500


def _levels(rows) -> list[tuple[float, int]] | None:
    if not rows:
        return None
    lv = [(float(r["price"]), int(r["quantity"])) for r in rows if r.get("quantity")]
    return lv or None


def kite_quotes(symbols: list[str]) -> dict[str, Quote]:
    """Raises SessionUnavailable / API errors: callers treat any failure as 'no prices this cycle'."""
    if not symbols:
        return {}
    kite = get_kite()
    out: dict[str, Quote] = {}
    for i in range(0, len(symbols), MAX_PER_CALL):
        chunk = [f"NSE:{s}" for s in symbols[i:i + MAX_PER_CALL]]
        try:
            data = kite.quote(chunk)
        except Exception as exc:
            if is_token_error(exc):
                invalidate("token rejected while fetching quotes")
            raise
        for key, q in data.items():
            sym = key.split(":", 1)[1]
            ohlc = q.get("ohlc") or {}
            ltp = float(q["last_price"])
            depth = q.get("depth") or {}
            out[sym] = Quote(ltp=ltp, high=float(ohlc.get("high") or ltp), low=float(ohlc.get("low") or ltp),
                             upper_circuit=q.get("upper_circuit_limit"), lower_circuit=q.get("lower_circuit_limit"),
                             open=float(ohlc["open"]) if ohlc.get("open") else None,
                             bids=_levels(depth.get("buy")), asks=_levels(depth.get("sell")))
    return out


log = logging.getLogger("agent")
CIRCUIT_TTL = 30 * 60
_circuits: dict[str, tuple[float, Any, Any]] = {}     # symbol -> (fetched monotonic, upper, lower)
_tokens: dict[str, int] = {}
_stream = None
_recorder = None                     # 1-second bar recorder (data/seconds.py), runs with the stream
_stream_lock = threading.Lock()
stats: dict[str, int] = {"streamed": 0, "rest": 0}


def _token_map(symbols: list[str]) -> dict[str, int]:
    missing = [s for s in symbols if s not in _tokens]
    if missing:
        from ..db import connect
        with connect() as conn:
            for r in conn.execute(f"SELECT symbol, instrument_token FROM instruments WHERE exchange = 'NSE' AND symbol "
                                  f"IN ({','.join('?' * len(missing))})", missing):
                _tokens[r[0]] = int(r[1])
    return {s: _tokens[s] for s in symbols if s in _tokens}


def stream():
    """The running stream, or None (not started: tests, replays, backtests, or switched off in app.yaml)."""
    return _stream


def start_stream() -> None:
    """Called once by the server at startup. The stream connects itself in the market window with a valid login."""
    global _stream, _recorder
    from datetime import datetime as _dt
    from ..clock import IST, now_ist
    from ..market_calendar import MarketCalendar
    from ..settings import get_app_config, get_secrets
    from . import kite_session, ticker
    if not get_app_config()["kite"].get("websocket", True):
        return

    def creds():
        s = kite_session._raw_session()
        if not s or now_ist() >= _dt.fromisoformat(s["expires_at"]).astimezone(IST):
            return None
        return get_secrets().kite_api_key, s["access_token"]

    def window():
        now = now_ist()
        return MarketCalendar(get_app_config()).is_trading_day(now.date()) and ticker.WINDOW[0] <= now.time() < ticker.WINDOW[1]

    with _stream_lock:
        if _stream is None:
            _stream = ticker.Stream(creds, window, base=universe_tokens)
            from ..orders import fastloop
            _stream.on_ticks = fastloop.on_ticks
            from ..data import seconds
            if seconds._cfg().get("record", True):
                _recorder = seconds.Recorder()
                _stream.listeners.append(_recorder.on_ticks)
                _recorder.start()
            _stream.start()
            _push_stop.clear()
            threading.Thread(target=_push_loop, name="price-push", daemon=True).start()
            log.info("Kite price stream started: connects 09:00–15:35 on trading days with a valid login")


def universe_tokens() -> set[int]:
    """Every NSE500 stock and Nifty 50 (also fills the symbol map used for dashboard pushes)."""
    from ..db import connect
    from ..settings import get_app_config
    kc = get_app_config()["kite"]
    with connect() as conn:
        rows = conn.execute("SELECT symbol, instrument_token FROM instruments WHERE exchange = 'NSE' "
                            "AND is_nse500 = 1").fetchall()
    for sym, tok in rows:
        _tokens[sym] = int(tok)
    if kc.get("nifty50_token"):
        _tokens[kc.get("nifty50_symbol", "NIFTY 50")] = int(kc["nifty50_token"])
        return {int(r[1]) for r in rows} | {int(kc["nifty50_token"])}
    return {int(r[1]) for r in rows}


def subscribe_universe() -> None:
    """After an NSE500 list change: subscribe the new members now (the next connection picks them up anyway)."""
    st = _stream
    if st is not None:
        st.want(universe_tokens())


PUSH_EVERY = 1.0
HELD_TTL = 10.0
_push_stop = threading.Event()
_held: tuple[float, set[str]] = (-1e9, set())


def held_symbols() -> set[str]:
    """Stocks the portfolios hold or are buying (open trades incl. pending buys, outside holdings); cached 10 s."""
    global _held
    if _time.monotonic() - _held[0] < HELD_TTL:
        return _held[1]
    from ..db import connect
    with connect() as conn:
        syms = {r[0] for r in conn.execute("SELECT DISTINCT symbol FROM trades WHERE state IN "
                                           "('BUY_PENDING', 'FILLED', 'PROTECTED', 'TRAILING')")}
        syms |= {r[0] for r in conn.execute("SELECT DISTINCT symbol FROM unmanaged_holdings WHERE qty > 0")}
    _held = (_time.monotonic(), syms)
    return syms


def _push_loop() -> None:
    """Every second, the prices that changed to the dashboard. Topic 'prices':
    {"p": {symbol: [ltp, open, high, low]}, "held": true when one of them is held or being bought}.
    Screens showing portfolios refetch only on "held"; charts follow their own stock."""
    from ..events import publish
    last = _time.monotonic()
    while not _push_stop.wait(PUSH_EVERY):
        st = _stream
        if st is None:
            continue
        now = _time.monotonic()
        changed = st.changed_since(last)
        last = now
        if not changed:
            continue
        by_tok = {v: k for k, v in _tokens.items()}
        out = {by_tok[t]: [k.ltp, k.open, k.high, k.low] for t, k in changed.items() if t in by_tok}
        if out:
            try:
                held = bool(held_symbols() & out.keys())
            except Exception:  # noqa: BLE001 - say "held" so screens refresh rather than go stale
                held = True
            publish("prices", {"p": out, "held": held})


def stop_stream() -> None:
    global _stream, _recorder
    _push_stop.set()
    with _stream_lock:
        if _stream is not None:
            _stream.stop()
            _stream = None
        if _recorder is not None:
            _recorder.stop()                 # writes the seconds still in memory
            _recorder = None


def live_quotes(symbols: list[str]) -> dict[str, Quote]:
    """Stream first; REST for the rest. Raises like kite_quotes when REST is needed and fails."""
    if not symbols:
        return {}
    st = _stream
    if st is None or not st.healthy():
        out = kite_quotes(symbols)
        _remember_circuits(out)
        stats["rest"] += len(out)
        if st is not None:
            st.want(_token_map(symbols).values())
        return out
    tokens = _token_map(symbols)
    st.want(tokens.values())
    out: dict[str, Quote] = {}
    missing = []
    for s in symbols:
        t = st.tick(tokens[s]) if s in tokens else None
        if t is None:
            missing.append(s)
            continue
        out[s] = Quote(ltp=t.ltp, high=t.high or t.ltp, low=t.low or t.ltp, open=t.open or None,
                       bids=t.bids, asks=t.asks)
    stale = [s for s in out if _time.monotonic() - _circuits.get(s, (-1e9, None, None))[0] > CIRCUIT_TTL]
    rest_syms = sorted(set(missing) | set(stale))
    if rest_syms:
        try:
            rest = kite_quotes(rest_syms)
        except Exception:
            if missing:
                raise                         # a stock with no price at all: fail closed as before
            rest = {}                         # only circuits were due: keep the cached ones for now
            log.warning("circuit-limit refresh failed; using cached limits")
        _remember_circuits(rest)
        for s in missing:
            if s in rest:
                out[s] = rest[s]
        stats["rest"] += len([s for s in missing if s in rest])
    stats["streamed"] += len(out) - len([s for s in missing if s in out])
    for s, q in out.items():
        c = _circuits.get(s)
        if c and q.upper_circuit is None:
            q.upper_circuit, q.lower_circuit = c[1], c[2]
    return out


def _remember_circuits(quotes: dict[str, Quote]) -> None:
    now = _time.monotonic()
    for s, q in quotes.items():
        if q.upper_circuit is not None or q.lower_circuit is not None:
            _circuits[s] = (now, q.upper_circuit, q.lower_circuit)


def status() -> dict[str, Any]:
    st = _stream
    if st is None:
        return {"mode": "rest", "detail": "Live prices by REST quote (stream off)"} | stats
    s = st.status()
    mode = "stream" if s["healthy"] else "rest"
    rec = _recorder.status() if _recorder is not None else None
    return {"mode": mode, **s, **stats, "seconds": rec}


# Tests and replay swap this.
quote_source: Callable[[list[str]], dict[str, Quote]] = live_quotes
