"""Kite WebSocket prices: the binary format, the stream against a local server speaking Kite's protocol, the feed
(stream first, REST for circuits and as the fallback) and the tick-driven order monitor."""
from __future__ import annotations

import asyncio
import json
import struct
import threading
import time

import pytest

from agent.broker import feed, ticker
from agent.broker.base import Quote


def full_packet(token, ltp, o, h, lo, c, bid=None, ask=None):
    """Kite's 184-byte full-mode packet: header 0-44, times/OI 44-64, then 10 depth entries of 12 bytes."""
    px = lambda x: round(x * 100)  # noqa: E731
    p = struct.pack(">11I", token, px(ltp), 10, px(ltp), 5000, 100, 100, px(o), px(h), px(lo), px(c))
    p += struct.pack(">5I", 1727760000, 0, 0, 0, 1727760000)
    for k in range(10):
        price, qty = (bid if k == 0 else ask if k == 5 else None) or (0, 0)
        p += struct.pack(">IIHH", qty, px(price), 1 if qty else 0, 0)
    assert len(p) == 184
    return p


def frame(*packets):
    out = struct.pack(">H", len(packets))
    for p in packets:
        out += struct.pack(">H", len(p)) + p
    return out


def test_parse():
    ticks = ticker.parse(frame(full_packet(7001, 101.5, 100, 102, 99.5, 100.2, bid=(101.45, 30), ask=(101.5, 20)),
                               struct.pack(">II", 256265, 2000050)))
    t = ticks[7001]
    assert (t.ltp, t.open, t.high, t.low, t.close) == (101.5, 100, 102, 99.5, 100.2)
    assert t.bids == [(101.45, 30)] and t.asks == [(101.5, 20)]
    assert ticks[256265].ltp == 20000.5
    assert ticker.parse(b"\x00") == {}                                     # heartbeat


class KiteServer:
    """A local stand-in for wss://ws.kite.trade: checks the token, records subscriptions, sends heartbeats and the
    ticks it is told to."""

    def __init__(self):
        self.messages, self.paths, self.queue = [], [], []
        self.heartbeat = True
        self.port = None
        self._loop = None
        self._ready = threading.Event()
        threading.Thread(target=self._run, daemon=True).start()
        assert self._ready.wait(5)

    def _run(self):
        import websockets
        self._loop = asyncio.new_event_loop()

        async def handler(ws):
            self.paths.append(ws.request.path)
            if "access_token=good" not in ws.request.path and "access_token=new" not in ws.request.path:
                await ws.close(1008, "bad token")
                return

            async def reader():
                async for m in ws:
                    self.messages.append(json.loads(m))
            r = asyncio.ensure_future(reader())
            try:
                while True:
                    while self.queue:
                        await ws.send(self.queue.pop(0))
                    if self.heartbeat:
                        await ws.send(b"\x00")
                    await asyncio.sleep(0.1)
            except Exception:  # noqa: BLE001 - client went away
                pass
            finally:
                r.cancel()

        async def main():
            async with websockets.serve(handler, "127.0.0.1", 0) as srv:
                self.port = srv.sockets[0].getsockname()[1]
                self._ready.set()
                await asyncio.Future()
        self._loop.run_until_complete(main())


def _wait(cond, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


@pytest.fixture
def server():
    return KiteServer()


@pytest.fixture
def stream(server, monkeypatch):
    creds = {"v": ("key", "good")}
    window = {"open": True}
    st = ticker.Stream(lambda: creds["v"], lambda: window["open"], root=f"ws://127.0.0.1:{server.port}")
    st.start()
    monkeypatch.setattr(feed, "_stream", st)
    yield st, creds, window
    st.stop()


def test_stream_subscribes_and_serves_ticks(server, stream):
    st, creds, window = stream
    assert _wait(st.healthy)
    st.want([7001, 7002])
    assert _wait(lambda: {"a": "mode", "v": ["full", [7001, 7002]]} in server.messages)
    assert {"a": "subscribe", "v": [7001, 7002]} in server.messages
    assert st.tick(7001) is None                                            # nothing ticked yet
    server.queue.append(frame(full_packet(7001, 101.5, 100, 102, 99.5, 100.2)))
    assert _wait(lambda: st.tick(7001) is not None)
    assert st.tick(7001).high == 102 and st.status()["streaming"] == 1
    # a new login: reconnects with the new token and subscribes again
    creds["v"] = ("key", "new")
    assert _wait(lambda: st.connects == 2 and st.healthy())
    assert "access_token=new" in server.paths[-1]
    assert _wait(lambda: server.messages.count({"a": "subscribe", "v": [7001, 7002]}) == 2)
    assert st.tick(7001) is None                                            # ticks are per connection
    # outside the market window: disconnected
    window["open"] = False
    assert _wait(lambda: not st.connected)


def test_stalled_stream_is_not_used(server, stream):
    st, _, _ = stream
    assert _wait(st.healthy)
    server.heartbeat = False
    assert _wait(lambda: not st.healthy(), timeout=8)


def test_bad_token_backs_off_and_hides_it(server, stream):
    st, creds, _ = stream
    assert _wait(st.healthy)
    creds["v"] = ("key", "secret-bad")
    assert _wait(lambda: st.last_error is not None and not st.connected, timeout=8)
    assert "secret-bad" not in st.last_error


def test_feed_streams_with_rest_for_circuits_and_fallback(server, stream, monkeypatch, fixed_clock):
    from agent.db import connect
    st, _, _ = stream
    with connect() as conn:
        conn.execute("INSERT INTO instruments (instrument_token, exchange, symbol, tick_size, is_nse500, updated_at) "
                     "VALUES (7001, 'NSE', 'ABC', 0.05, 1, 'x'), (7002, 'NSE', 'XYZ', 0.05, 1, 'x')")
    rest_calls = []

    def rest(symbols):
        rest_calls.append(list(symbols))
        return {s: Quote(ltp=50.0, high=51, low=49, upper_circuit=60.0, lower_circuit=40.0) for s in symbols}
    monkeypatch.setattr(feed, "kite_quotes", rest)
    monkeypatch.setattr(feed, "_circuits", {})
    monkeypatch.setattr(feed, "_tokens", {})
    assert _wait(st.healthy)
    q = feed.live_quotes(["ABC", "XYZ"])                                    # subscribed now, no ticks yet: REST
    assert rest_calls == [["ABC", "XYZ"]] and q["ABC"].ltp == 50.0
    assert _wait(lambda: {"a": "subscribe", "v": [7001, 7002]} in server.messages)
    server.queue.append(frame(full_packet(7001, 101.5, 100, 102, 99.5, 100.2)))
    assert _wait(lambda: st.tick(7001) is not None)
    q = feed.live_quotes(["ABC", "XYZ"])
    assert q["ABC"].ltp == 101.5 and q["ABC"].upper_circuit == 60.0         # streamed price + cached circuit
    assert rest_calls[-1] == ["XYZ"]                                        # only the stock with no tick
    n = len(rest_calls)
    server.queue.append(frame(full_packet(7002, 70.0, 69, 71, 68, 69)))
    assert _wait(lambda: st.tick(7002) is not None)
    q = feed.live_quotes(["ABC", "XYZ"])
    assert len(rest_calls) == n and q["XYZ"].ltp == 70.0                    # both streaming: no REST at all
    assert feed.status()["mode"] == "stream"


def test_feed_without_a_stream_is_rest(monkeypatch):
    monkeypatch.setattr(feed, "_stream", None)
    monkeypatch.setattr(feed, "kite_quotes", lambda s: {x: Quote(ltp=1, high=1, low=1) for x in s})
    assert feed.live_quotes(["ABC"])["ABC"].ltp == 1 and feed.status()["mode"] == "rest"


def test_a_tick_crossing_a_trigger_runs_the_monitor_at_once(server, stream, monkeypatch, fixed_clock):
    from agent.db import connect
    from agent.orders import fastloop, manager
    st, _, _ = stream
    with connect() as conn:
        conn.execute("INSERT INTO instruments (instrument_token, exchange, symbol, tick_size, is_nse500, updated_at) "
                     "VALUES (7001, 'NSE', 'ABC', 0.05, 1, 'x')")
        cols = {r[1]: r for r in conn.execute("PRAGMA table_info(paper_gtts)")}
        row = {"account_id": "P100", "symbol": "ABC", "type": "single", "trigger_values": "[102.5]",
               "last_price": 100, "orders": "[]", "status": "active"}
        for name, c in cols.items():                     # any other required column: a placeholder
            if c[3] and c[4] is None and name not in row and not c[5]:
                row[name] = "x"
        conn.execute(f"INSERT INTO paper_gtts ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})", list(row.values()))
    runs = []
    monkeypatch.setattr(manager, "on_cycle", lambda now=None: runs.append(time.monotonic()) or "ok")
    monkeypatch.setattr(fastloop, "_allowed", lambda: True)
    st.on_ticks = fastloop.on_ticks
    fastloop.refresh_levels()
    fastloop.start()
    try:
        assert _wait(st.healthy)
        st.want([7001])
        server.queue.append(frame(full_packet(7001, 101.0, 100, 101.2, 99.5, 100)))
        assert _wait(lambda: st.tick(7001) is not None)
        time.sleep(0.3)
        assert runs == []                                                   # no level crossed
        server.queue.append(frame(full_packet(7001, 102.6, 100, 102.6, 99.5, 100)))
        assert _wait(lambda: len(runs) == 1, timeout=3)                     # crossed 102.5: monitor runs now
    finally:
        fastloop.stop()
        fastloop._thread.join(timeout=3)


def test_changed_prices_are_pushed_to_the_dashboard_each_second(server, stream, monkeypatch):
    from agent import events
    st, _, _ = stream
    sent = []
    monkeypatch.setattr(events, "publish", lambda topic, data=None: sent.append((topic, data)))
    monkeypatch.setattr(feed, "_tokens", {"ABC": 7001})
    feed._push_stop.clear()
    t = threading.Thread(target=feed._push_loop, daemon=True)
    t.start()
    try:
        assert _wait(st.healthy)
        st.want([7001])
        server.queue.append(frame(full_packet(7001, 101.5, 100, 102, 99.5, 100.2)))
        assert _wait(lambda: any(x[0] == "prices" for x in sent), timeout=4)
        topic, data = next(x for x in sent if x[0] == "prices")
        assert data == {"p": {"ABC": [101.5, 100, 102, 99.5]}, "held": False}   # nothing held: screens don't refetch
        n = len(sent)
        time.sleep(2.2)
        assert len(sent) == n                                              # nothing changed: nothing pushed
    finally:
        feed._push_stop.set()
        t.join(timeout=3)


def test_every_nse500_stock_is_subscribed_on_each_connection(server):
    base = {"v": [7001, 7002]}
    creds = {"v": ("key", "good")}
    st = ticker.Stream(lambda: creds["v"], lambda: True, root=f"ws://127.0.0.1:{server.port}", base=lambda: base["v"])
    st.start()
    try:
        assert _wait(lambda: {"a": "subscribe", "v": [7001, 7002]} in server.messages)
        assert {"a": "mode", "v": ["full", [7001, 7002]]} in server.messages
        base["v"] = [7001, 7002, 7003]                                       # a new member: next connection has it
        creds["v"] = ("key", "new")
        assert _wait(lambda: {"a": "subscribe", "v": [7001, 7002, 7003]} in server.messages)
    finally:
        st.stop()


def test_universe_tokens_and_held_flag(monkeypatch, fixed_clock):
    from agent.db import connect
    with connect() as conn:
        conn.execute("INSERT INTO instruments (instrument_token, exchange, symbol, tick_size, is_nse500, updated_at) "
                     "VALUES (7001, 'NSE', 'ABC', 0.05, 1, 'x'), (7002, 'NSE', 'XYZ', 0.05, 0, 'x')")
        conn.execute("INSERT INTO unmanaged_holdings (account_id, symbol, qty, avg_price, detected_at) "
                     "VALUES ('P60', 'ABC', 3, 100, 'x')")
    monkeypatch.setattr(feed, "_held", (-1e9, set()))
    assert feed.universe_tokens() == {7001, 256265}                          # NSE500 + Nifty 50, not XYZ
    assert feed._tokens["ABC"] == 7001 and feed._tokens["NIFTY 50"] == 256265
    assert feed.held_symbols() == {"ABC"}


def test_screens_price_open_trades_from_the_stream(server, stream, monkeypatch, fixed_clock):
    """Positions/Accounts read the streamed price, not the order monitor's once-a-minute snapshot."""
    from agent.db import connect, set_state
    from agent.orders import views
    st, _, _ = stream
    with connect() as conn:
        conn.execute("INSERT INTO instruments (instrument_token, exchange, symbol, tick_size, is_nse500, updated_at) "
                     "VALUES (7001, 'NSE', 'ABC', 0.05, 1, 'x')")
    monkeypatch.setattr(feed, "_tokens", {})
    set_state("last_quotes", {"at": fixed_clock.now().isoformat(timespec="seconds"), "ltp": {"ABC": 99.0}})
    assert views._prices({"ABC"})["ABC"]["source"] == "live"                # no tick yet: the monitor's quote
    assert _wait(st.healthy)
    st.want([7001])
    server.queue.append(frame(full_packet(7001, 101.5, 100, 102, 99.5, 100.2)))
    assert _wait(lambda: st.tick(7001) is not None)
    p = views._prices({"ABC"})["ABC"]
    assert (p["price"], p["source"]) == (101.5, "stream")
    server.queue.append(frame(full_packet(7001, 102.25, 100, 102.5, 99.5, 100.2)))
    assert _wait(lambda: views._prices({"ABC"})["ABC"]["price"] == 102.25)  # every tick
