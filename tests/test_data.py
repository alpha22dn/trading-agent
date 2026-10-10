"""Phase 0: Kite session, instruments, NSE500 upload, candle backfill, market filter."""
from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from agent import audit
from agent.broker import kite_session
from agent.broker.kite_session import SessionUnavailable, compute_expiry
from agent.clock import IST
from agent.data import candles, instruments, nse500
from agent.db import connect, get_state
from agent.strategy import market_filter

from .conftest import NIFTY_TOKEN, nse500_csv


# ---------- session ----------

@pytest.mark.parametrize("login, expected", [
    (datetime(2026, 9, 22, 8, 30, tzinfo=IST), datetime(2026, 9, 23, 6, 0, tzinfo=IST)),
    (datetime(2026, 9, 22, 5, 30, tzinfo=IST), datetime(2026, 9, 22, 6, 0, tzinfo=IST)),
    (datetime(2026, 9, 22, 23, 59, tzinfo=IST), datetime(2026, 9, 23, 6, 0, tzinfo=IST)),
])
def test_token_expiry(login, expected):
    assert compute_expiry(login, 6) == expected


def test_login_flow_and_expiry(fake_kite, fixed_clock):
    assert kite_session.status()["valid"] is False
    with pytest.raises(SessionUnavailable):
        kite_session.get_kite()
    st = kite_session.complete_login("req-1")
    assert st["valid"] and st["user_id"] == "AB1234"
    assert "access_token" not in st                     # never exposed
    assert kite_session.get_kite().access_token == "at-req-1"
    fixed_clock.set(datetime(2026, 9, 23, 6, 0))        # next morning
    assert kite_session.status()["valid"] is False
    with pytest.raises(SessionUnavailable, match="expired"):
        kite_session.get_kite()


def test_missing_keys_fail_closed(monkeypatch, fixed_clock):
    from agent import settings
    monkeypatch.setenv("KITE_API_KEY", "")
    settings.get_secrets.cache_clear()
    with pytest.raises(SessionUnavailable, match="not set"):
        kite_session.login_url()


def test_invalidate_raises_alert(logged_in):
    kite_session.invalidate("token rejected")
    assert kite_session.status()["valid"] is False
    assert any(a["name"] == "kite_session" for a in audit.active_alerts())


# ---------- instruments & NSE500 ----------

def test_instruments_sync_keeps_nse_equity_and_nifty(logged_in):
    r = instruments.sync_instruments()
    assert r["stored"] == 6                              # 5 equities + NIFTY 50, no futures
    with connect() as conn:
        syms = {x[0] for x in conn.execute("SELECT symbol FROM instruments")}
    assert "NIFTY 50" in syms and "NIFTY24OCTFUT" not in syms


def test_nse500_upload_maps_sector_and_series(logged_in):
    instruments.sync_instruments()
    csv_text = nse500_csv([("RELIANCE", "EQ", "Oil Gas & Consumable Fuels"), ("TCS", "EQ", "Information Technology"),
                           ("BEL", "BE", "Capital Goods"), ("NOTLISTED", "EQ", "Services")])
    res = nse500.apply_constituents(nse500.parse_csv(csv_text.encode("utf-8-sig")))
    assert res.matched == 3
    assert "NOTLISTED" in res.unmatched
    with connect() as conn:
        rows = dict(conn.execute("SELECT symbol, sector FROM instruments WHERE is_nse500 = 1").fetchall())
    assert rows == {"RELIANCE": "Oil Gas & Consumable Fuels", "TCS": "Information Technology",
                    "BEL-BE": "Capital Goods"}


def test_nse500_reupload_reports_removed(logged_in):
    instruments.sync_instruments()
    nse500.apply_constituents(nse500.parse_csv(nse500_csv([("RELIANCE", "EQ", "Energy"), ("TCS", "EQ", "IT")])))
    res = nse500.apply_constituents(nse500.parse_csv(nse500_csv([("RELIANCE", "EQ", "Energy")])))
    assert res.removed == ["TCS"]
    assert any(a["name"] == "nse500_change" for a in audit.active_alerts())


def test_nse500_rejects_wrong_file():
    with pytest.raises(nse500.Nse500FormatError, match="Missing column"):
        nse500.parse_csv("Name,Price\nA,1\n")
    with pytest.raises(nse500.Nse500FormatError, match="expected about 500"):
        nse500.parse_csv(nse500_csv([("RELIANCE", "EQ", "Energy")], pad_to=10))


def test_nse500_needs_instruments_first():
    with pytest.raises(nse500.Nse500FormatError, match="Log in"):
        nse500.apply_constituents(nse500.parse_csv(nse500_csv([("RELIANCE", "EQ", "Energy")])))


# ---------- candles ----------

def _setup_universe(kite):
    instruments.sync_instruments()
    nse500.apply_constituents(nse500.parse_csv(nse500_csv([("RELIANCE", "EQ", "Energy"), ("TCS", "EQ", "IT"),
                                                           ("INFY", "EQ", "IT")])))


def test_backfill_two_years_without_partial_today(logged_in, fixed_clock):
    _setup_universe(logged_in)
    res = candles.run_backfill()
    assert res["total"] == 4 and res["updated"] == 4 and res["error_count"] == 0
    with connect() as conn:
        first, last = conn.execute("SELECT MIN(date), MAX(date) FROM candles_daily WHERE symbol='TCS'").fetchone()
    assert last == "2026-09-21"                          # 10:00 on 22nd: today's candle is not final
    assert date.fromisoformat(first) <= date(2024, 9, 23)
    cov = candles.coverage()
    assert cov["complete"] and cov["current"] == 3 and not cov["missing"]


def test_incremental_refresh_and_skip(logged_in, fixed_clock):
    _setup_universe(logged_in)
    candles.run_backfill()
    again = candles.run_backfill()
    assert again["skipped"] == 4 and again["rows_added"] == 0
    fixed_clock.set(datetime(2026, 9, 22, 21, 0))        # after eod_after
    kite_session.complete_login("req-2")                 # new token for the evening
    res = candles.run_backfill()
    assert res["updated"] == 4 and res["rows_added"] == 4
    hist = [c for c in logged_in.calls if c[0] == "historical"][-1]
    assert hist[2] >= date(2026, 9, 14)                  # overlap window only, not 2 years again


def test_split_adjustment_detected_and_redownloaded(logged_in, fixed_clock):
    _setup_universe(logged_in)
    candles.run_backfill()
    tcs = 1001
    logged_in.price_shift[tcs] = 0.5                     # Kite back-adjusts after a 2:1 split
    fixed_clock.set(datetime(2026, 9, 22, 21, 0))
    kite_session.complete_login("req-2")
    res = candles.run_backfill()
    assert res["adjusted"] == ["TCS"]
    with connect() as conn:
        closes = [r[0] for r in conn.execute("SELECT close FROM candles_daily WHERE symbol='TCS' ORDER BY date")]
    assert max(closes) < 200                             # whole history replaced with adjusted prices
    assert any(a["name"] == "price_adjustment" for a in audit.active_alerts())


def test_one_bad_symbol_does_not_stop_the_run(logged_in, fixed_clock):
    _setup_universe(logged_in)
    logged_in.fail_tokens.add(1002)                      # INFY
    res = candles.run_backfill()
    assert res["error_count"] == 1 and res["updated"] == 3
    assert candles.coverage()["missing"] == ["INFY"]


def test_token_rejection_aborts_and_logs_out(logged_in, fixed_clock):
    _setup_universe(logged_in)
    logged_in.token_error = True
    with pytest.raises(SessionUnavailable):
        candles.run_backfill()
    assert kite_session.status()["valid"] is False
    assert get_state(candles.STATE_KEY)["aborted"] == "Kite session rejected"


def test_backfill_needs_session(fixed_clock):
    with pytest.raises(SessionUnavailable):
        candles.run_backfill()


def test_only_one_backfill_at_a_time(logged_in):
    candles._run_lock.acquire()
    try:
        with pytest.raises(candles.AlreadyRunning):
            candles.run_backfill()
    finally:
        candles._run_lock.release()


# ---------- market filter ----------

def _nifty(closes: list[float], end: date):
    d, rows = end, []
    for c in reversed(closes):
        while d.weekday() >= 5:
            d -= timedelta(days=1)
        rows.append((d.isoformat(), c))
        d -= timedelta(days=1)
    with connect() as conn:
        for day, c in rows:
            conn.execute("INSERT INTO candles_daily (symbol, date, open, high, low, close, volume, fetched_at) "
                         "VALUES ('NIFTY 50', ?, ?, ?, ?, ?, 0, 'x')", (day, c, c, c, c))


def test_market_filter_blocks_without_data(fixed_clock, market_filter_on):
    r = market_filter.evaluate()
    assert r["allow_entries"] is False and r["state"] == "no_data"


def test_market_filter_above_and_below(fixed_clock, market_filter_on):
    _nifty([100.0] * 49 + [110.0], date(2026, 9, 21))
    assert market_filter.evaluate()["state"] == "above"
    with connect() as conn:
        conn.execute("DELETE FROM candles_daily")
    _nifty([100.0] * 49 + [90.0], date(2026, 9, 21))
    r = market_filter.evaluate()
    assert r["state"] == "below" and r["allow_entries"] is False


def test_market_filter_off_never_blocks(fixed_clock):
    _nifty([100.0] * 49 + [90.0], date(2026, 9, 21))
    r = market_filter.evaluate()
    assert r["enabled"] is False and r["allow_entries"] is True and r["state"] == "below"   # shown, not enforced


def test_market_filter_stale_data_blocks(fixed_clock, market_filter_on):
    _nifty([100.0] * 50, date(2026, 9, 17))
    r = market_filter.evaluate()
    assert r["state"] == "stale" and r["allow_entries"] is False


def test_history_extends_back_when_window_grows(logged_in, fixed_clock):
    from agent.db import set_state
    _setup_universe(logged_in)
    candles.run_backfill()
    with connect() as conn:
        full_first = conn.execute("SELECT MIN(date) FROM candles_daily WHERE symbol='TCS'").fetchone()[0]
        conn.execute("DELETE FROM candles_daily WHERE symbol='TCS' AND date < '2025-06-01'")
    set_state("history_floor", "2025-06-01")        # as if the cache had been built with a shorter window
    fixed_clock.set(datetime(2026, 9, 22, 21, 0))
    kite_session.complete_login("req-3")
    candles.run_backfill()
    with connect() as conn:
        first = conn.execute("SELECT MIN(date) FROM candles_daily WHERE symbol='TCS'").fetchone()[0]
    assert full_first <= first <= "2024-03-26"          # floor moved a day with the clock
    assert get_state("history_floor") < "2025-06-01"
    # next run does not refetch old history
    n_calls = len(logged_in.calls)
    fixed_clock.set(datetime(2026, 9, 23, 21, 0))
    kite_session.complete_login("req-4")
    candles.run_backfill()
    old = [c for c in logged_in.calls[n_calls:] if c[0] == "historical" and c[2] < date(2026, 1, 1)]
    assert old == []


def test_provisional_close_is_corrected_without_a_split_alert(logged_in, fixed_clock):
    """The EOD run saved Kite's provisional close; the next run sees the official NSE close (a small change on
    the newest stored day only). That is a correction: updated in place, no alert, no history re-download."""
    _setup_universe(logged_in)
    candles.run_backfill()                               # 10:00 on 22 Sep: up to 21 Sep
    with connect() as conn:
        official = conn.execute("SELECT close FROM candles_daily WHERE symbol='TCS' AND date='2026-09-21'").fetchone()[0]
        conn.execute("UPDATE candles_daily SET close = close * 1.024 WHERE symbol='TCS' AND date='2026-09-21'")
        conn.execute("UPDATE candles_daily SET close = close * 1.003 WHERE symbol='INFY' AND date='2026-09-21'")
    fixed_clock.set(datetime(2026, 9, 22, 21, 0))
    kite_session.complete_login("req-2")
    n_calls = len(logged_in.calls)
    res = candles.run_backfill()
    assert res["adjusted"] == [] and not any(a["name"] == "price_adjustment" for a in audit.active_alerts())
    with connect() as conn:
        assert conn.execute("SELECT close FROM candles_daily WHERE symbol='TCS' AND date='2026-09-21'"
                            ).fetchone()[0] == pytest.approx(official)
    full = [c for c in logged_in.calls[n_calls:] if c[0] == "historical" and c[2] < date(2026, 9, 1)]
    assert full == []                                    # no full re-download
    assert "2 previous closes corrected" in audit.recent(5, kind="job")[0]["message"]


def test_large_or_older_changes_still_count_as_a_split(logged_in, fixed_clock):
    _setup_universe(logged_in)
    candles.run_backfill()
    with connect() as conn:
        conn.execute("UPDATE candles_daily SET close = close * 1.08 WHERE symbol='TCS' AND date='2026-09-21'")   # > 5%
        conn.execute("UPDATE candles_daily SET close = close * 1.01 WHERE symbol='INFY' AND date='2026-09-17'")  # older day
    fixed_clock.set(datetime(2026, 9, 22, 21, 0))
    kite_session.complete_login("req-2")
    res = candles.run_backfill()
    assert sorted(res["adjusted"]) == ["INFY", "TCS"]
