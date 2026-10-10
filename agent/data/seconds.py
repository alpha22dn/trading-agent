"""1-second bars recorded from Kite's live price stream (decided 2026-10-01).

Kite's historical data stops at 1-minute bars, so second-level history only exists from the day recording starts.
Every stock the stream carries (the Nifty 500 and Nifty 50) is recorded:

- Each tick's last price goes into that stock's current second (open, high, low, close); volume is the increase in
  the day's cumulative traded volume. Seconds with no tick have no bar. The second is taken from the exchange
  timestamp when it is sane (within 2 minutes of the clock), else from the clock. Ticks are snapshots about once a
  second, so a very brief price inside a second can be missed; the 1-minute bars remain the reference.
- Only 09:15:00–15:35:00 IST is kept (no pre-open).
- Storage: one file per day, runtime/seconds/YYYY-MM-DD.db, one row per stock per minute holding that minute's
  seconds delta-encoded and zlib-compressed. Rows are written a few seconds after each minute ends.
- Kept indefinitely. If the drive holding the files drops below `min_free_gb`, recording pauses with an alert and
  resumes once there is 1 GB more than that.
"""
from __future__ import annotations

import logging
import shutil
import sqlite3
import threading
import time
import zlib
from contextlib import contextmanager
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from typing import Any, Iterator

from ..clock import IST, now_ist

log = logging.getLogger("agent")

START = dtime(9, 15)
END = dtime(15, 35)
FLUSH_EVERY = 5.0
SCHEMA = """CREATE TABLE IF NOT EXISTS sec1 (
    token  INTEGER NOT NULL,
    minute INTEGER NOT NULL,      -- minute start, epoch seconds
    n      INTEGER NOT NULL,      -- seconds with a bar
    data   BLOB NOT NULL,         -- encode()
    PRIMARY KEY (token, minute)
) WITHOUT ROWID;"""


def _cfg() -> dict[str, Any]:
    from ..settings import get_app_config
    return {"record": True, "min_free_gb": 10} | (get_app_config().get("seconds") or {})


def folder() -> Path:
    from ..db import current_db_path
    return current_db_path().parent / "seconds"


def day_file(d: date) -> Path:
    return folder() / f"{d.isoformat()}.db"


@contextmanager
def _open(path: Path, create: bool = False) -> Iterator[sqlite3.Connection]:
    if create:
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30, check_same_thread=False)
    try:
        if create:
            conn.executescript(SCHEMA)
        yield conn
        conn.commit()
    finally:
        conn.close()


# ---------- encoding ----------

def _varint(out: bytearray, n: int) -> None:
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return


def _zz(n: int) -> int:
    return (n << 1) ^ (n >> 63)


def encode(rows: list[tuple[int, int, int, int, int, int]]) -> bytes:
    """rows: (second 0–59, open, high, low, close in paise, volume), ascending seconds."""
    out = bytearray()
    _varint(out, len(rows))
    prev_s, prev_c = -1, 0
    for s, o, h, lo, c, v in rows:
        _varint(out, s - prev_s - 1)
        _varint(out, _zz(o - prev_c))
        _varint(out, h - o)
        _varint(out, o - lo)
        _varint(out, c - lo)
        _varint(out, v)
        prev_s, prev_c = s, c
    return zlib.compress(bytes(out), 6)


def decode(blob: bytes) -> list[tuple[int, int, int, int, int, int]]:
    buf = zlib.decompress(blob)
    i = 0

    def rd() -> int:
        nonlocal i
        n = shift = 0
        while True:
            b = buf[i]
            i += 1
            n |= (b & 0x7F) << shift
            if not b & 0x80:
                return n
            shift += 7

    out = []
    prev_s, prev_c = -1, 0
    for _ in range(rd()):
        s = prev_s + 1 + rd()
        z = rd()
        o = prev_c + ((z >> 1) ^ -(z & 1))
        h = o + rd()
        lo = o - rd()
        c = lo + rd()
        v = rd()
        out.append((s, o, h, lo, c, v))
        prev_s, prev_c = s, c
    return out


# ---------- recording ----------

class Recorder:
    def __init__(self, clock=time.time):
        self.clock = clock
        self._lock = threading.Lock()
        self._cur: dict[int, list[int]] = {}        # token -> [epoch second, o, h, l, c, v] in paise
        self._cum: dict[int, int] = {}              # token -> last cumulative volume
        self._done: dict[tuple[int, int], list[tuple[int, int, int, int, int, int]]] = {}   # (token, minute) -> rows
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.paused_low_disk = False
        self._alerted_on: str | None = None
        self.stats: dict[str, Any] = {"seconds": 0, "minutes_written": 0, "last_write": None, "day": None}

    # stream thread
    def on_ticks(self, ticks: dict) -> None:
        if self.paused_low_disk:
            return
        now = self.clock()
        for tok, t in ticks.items():
            ets = t.exchange_ts
            sec = int(ets) if ets and abs(ets - now) < 120 else int(now)
            local = datetime.fromtimestamp(sec, IST).time()
            if not (START <= local < END) or not t.ltp:
                continue
            px = round(t.ltp * 100)
            vol = 0
            if t.volume is not None:
                prev = self._cum.get(tok)
                vol = max(0, t.volume - prev) if prev is not None else 0
                self._cum[tok] = t.volume
            with self._lock:
                cur = self._cur.get(tok)
                if cur is None or sec > cur[0]:
                    if cur is not None:
                        self._close(tok, cur)
                    self._cur[tok] = [sec, px, px, px, px, vol]
                elif sec == cur[0]:
                    cur[2] = max(cur[2], px)
                    cur[3] = min(cur[3], px)
                    cur[4] = px
                    cur[5] += vol
                else:                                   # an older second arriving late: fold its volume in
                    cur[5] += vol

    def _close(self, tok: int, bar: list[int]) -> None:
        sec = bar[0]
        minute = sec - sec % 60
        self._done.setdefault((tok, minute), []).append((sec - minute, bar[1], bar[2], bar[3], bar[4], bar[5]))
        self.stats["seconds"] += 1

    # writer thread
    def flush(self, final: bool = False) -> int:
        """Write every minute that has ended (all of them when final)."""
        now = int(self.clock())
        with self._lock:
            if final:
                for tok, bar in list(self._cur.items()):
                    self._close(tok, bar)
                self._cur.clear()
            else:
                for tok, bar in list(self._cur.items()):         # a stock that stopped ticking: close its second
                    if bar[0] < now - 2:
                        self._close(tok, bar)
                        del self._cur[tok]
            ready = {k: v for k, v in self._done.items() if final or k[1] + 60 <= now - 2}
            for k in ready:
                del self._done[k]
        if not ready:
            return 0
        by_day: dict[date, list[tuple[int, int, int, bytes]]] = {}
        for (tok, minute), rows in ready.items():
            rows.sort()
            merged: list[tuple[int, int, int, int, int, int]] = []
            for r in rows:                                         # the same second twice: merge
                if merged and merged[-1][0] == r[0]:
                    m = merged[-1]
                    merged[-1] = (m[0], m[1], max(m[2], r[2]), min(m[3], r[3]), r[4], m[5] + r[5])
                else:
                    merged.append(r)
            d = datetime.fromtimestamp(minute, IST).date()
            by_day.setdefault(d, []).append((tok, minute, len(merged), encode(merged)))
        for d, rows in by_day.items():
            with _open(day_file(d), create=True) as conn:
                conn.executemany("INSERT OR REPLACE INTO sec1 (token, minute, n, data) VALUES (?, ?, ?, ?)", rows)
            self.stats["day"] = d.isoformat()
        self.stats["minutes_written"] += len(ready)
        self.stats["last_write"] = now
        return len(ready)

    def check_disk(self) -> None:
        from .. import audit
        need = float(_cfg()["min_free_gb"]) * 1024 ** 3
        folder().mkdir(parents=True, exist_ok=True)
        free = shutil.disk_usage(folder()).free
        self.stats["free_gb"] = round(free / 1024 ** 3, 1)
        if not self.paused_low_disk and free < need:
            self.paused_low_disk = True
            log.warning("1-second recording paused: %.1f GB free", free / 1024 ** 3)
            today = now_ist().date().isoformat()
            if self._alerted_on != today:
                self._alerted_on = today
                audit.alert("seconds_recording", f"1-second recording paused: only {free / 1024 ** 3:.1f} GB free on "
                            f"the drive (minimum {_cfg()['min_free_gb']} GB). It resumes by itself once there is more "
                            "space.", level="warning")
        elif self.paused_low_disk and free > need + 1024 ** 3:
            self.paused_low_disk = False
            log.info("1-second recording resumed: %.1f GB free", free / 1024 ** 3)

    def _run(self) -> None:
        checked = 0.0
        while not self._stop.wait(FLUSH_EVERY):
            try:
                if time.monotonic() - checked > 60:
                    checked = time.monotonic()
                    self.check_disk()
                self.flush()
            except Exception:  # noqa: BLE001 - recording never stops the agent
                log.exception("1-second recorder flush failed")
        try:
            self.flush(final=True)
        except Exception:  # noqa: BLE001
            log.exception("1-second recorder final flush failed")

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        try:
            self.check_disk()
        except Exception:  # noqa: BLE001
            log.exception("disk check failed")
        self._thread = threading.Thread(target=self._run, name="seconds-recorder", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=10)

    def status(self) -> dict[str, Any]:
        f = day_file(now_ist().date())
        return {"recording": not self.paused_low_disk, "paused_low_disk": self.paused_low_disk,
                "today_mb": round(f.stat().st_size / 1024 ** 2, 1) if f.exists() else 0.0} | self.stats


# ---------- reading ----------

def days() -> list[str]:
    d = folder()
    return sorted(p.stem for p in d.glob("????-??-??.db")) if d.exists() else []


def read(token: int, from_ms: int, to_ms: int) -> dict[int, list[list[float]]]:
    """{minute start ms: [[second, open, high, low, close, volume], ...]} for minutes in [from, to)."""
    out: dict[int, list[list[float]]] = {}
    lo, hi = from_ms // 1000, to_ms // 1000
    d = datetime.fromtimestamp(lo, IST).date()
    last = datetime.fromtimestamp(max(lo, hi - 1), IST).date()
    while d <= last:
        f = day_file(d)
        if f.exists():
            with _open(f) as conn:
                for minute, blob in conn.execute("SELECT minute, data FROM sec1 WHERE token = ? AND minute >= ? AND "
                                                 "minute < ? ORDER BY minute", (token, lo, hi)):
                    out[minute * 1000] = [[s, o / 100, h / 100, l_ / 100, c / 100, v] for s, o, h, l_, c, v in decode(blob)]
        d += timedelta(days=1)
    return out


def summary() -> dict[str, Any]:
    ds = days()
    size = sum(p.stat().st_size for p in folder().glob("*.db")) if ds else 0
    return {"days": len(ds), "first": ds[0] if ds else None, "last": ds[-1] if ds else None,
            "total_mb": round(size / 1024 ** 2, 1)}
