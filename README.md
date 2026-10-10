# Trading Agent — Momentum v1 (paper MVP)

A rules-based swing-trading agent for NSE equities. It places **GTT orders only** through Zerodha Kite Connect and is
controlled from a local dashboard. The MVP is **paper trading only**: three virtual accounts (₹30k, ₹60k, ₹1 lakh).

The build spec is the source of truth:
[Trading Agent — Build Spec](https://claude.ai/code/artifact/5d6168b3-19e4-42db-8c51-88dcc6060eb0).
The build goes phase by phase, and a phase starts only after the previous phase's checks pass.

| Phase | Status | Done when |
|---|---|---|
| 0. Foundation | **Built** | Login works daily; candles cached for all NSE500 stocks |
| 1. Signals | **Built** | Historical scan reports v1 signals per month over 2 years |
| 2. Execution | **Built** | Every scenario test passes in replay mode |
| 3. Insight | — | A replayed month produces a full journal and one learning proposal |
| 4. Forward paper run | — | All MVP acceptance criteria met |

## One-time setup (Windows, Command Prompt)

1. **Install Python 3.12 or 3.13** from python.org and tick *Add python.exe to PATH*.
   Node.js is optional; a prebuilt dashboard is included.
2. **Create a Kite Connect app** at <https://developers.kite.trade/apps> (the paid Connect plan includes
   historical data). Set its **Redirect URL** to:
   ```
   http://127.0.0.1:8000/api/kite/callback
   ```
3. In this folder, double-click **`setup.bat`** or run it from Command Prompt:
   ```
   cd /d "D:\AI Projects\trading-agent"
   setup.bat
   ```
4. Open **`.env`** in Notepad and fill in `KITE_API_KEY` and `KITE_API_SECRET`. Never share or commit this file.

## Every trading day

1. Double-click **`run.bat`**. The dashboard opens at <http://127.0.0.1:8000>. Keep the window open.
2. On the **Login** screen, click **Log in to Kite**. You sign in on Zerodha's own page; the agent only gets
   today's access token. The token expires at 06:00 the next morning.
3. That's it. Instruments sync right after login. The EOD run at 21:00 refreshes candles (after Kite has the official NSE close). The database is
   backed up at 21:30.

If you don't log in, no new orders are placed. The 09:10 login check raises an alert.

## First-time data load (the Phase 0 check)

On **Overview → Data setup**, after logging in:

1. **Instruments** sync on their own after login.
2. **NSE500 list**: download
   [`ind_nifty500list.csv`](https://niftyindices.com/IndexConstituent/ind_nifty500list.csv) and upload it.
   Refresh it monthly.
3. **Daily candles**: click *Download candles*. The first run fetches 2 years for about 500 stocks plus
   Nifty 50, within Kite's rate limit (about 4 minutes). Later runs fetch only new days.

Phase 0 is done when Data setup shows **Phase 0 data complete** and login has worked on real trading days.

## Phase 1: signals

The EOD run (21:00) now does: candles → indicators → NSE results calendar → signal scan.

- **Watchlist screen:** upload the .xlsx you pasted the Screener results into (NSE code from the company links,
  EPS, profit growth), see pass/fail on each of the five gates per stock, load the NSE results calendar, upload the NSE500 list.
- **Signals screen:** the latest session's triggers with the four conditions, levels, RR band and why each one
  is or isn't tradeable, plus a candlestick chart with entry, limit, stop and target lines. The *2-year history*
  tab runs the Phase 1 check: signals per month.
- **Results calendar:** fetched from NSE's board-meeting announcements (nseindia.com). If today's fetch fails,
  every new entry is blocked. Load the 3-year history once (Watchlist screen) so the "same quarter last year"
  window and the historical scan can use it.

Indicators follow the standard (TradingView / TA-Lib) definitions and are checked against TA-Lib in the tests.
The first 100 sessions of each stock are warm-up, so a new listing can't signal for about 5 months, and the
candle cache now keeps 2.5 years so the scan covers a full 2 years after warm-up. Prices are rounded to the
stock's tick size in the conservative direction (entry up, stop and target down) and RR is computed after rounding.

## Phase 2a: paper execution

The EOD run now does: candles → indicators → results calendar → **trade maintenance** → signal scan →
**sizing, ranking and buy GTTs**. An **order monitor** runs every minute from 09:15 to 15:30.

- **PaperBroker** simulates Kite GTTs, orders, holdings and cash per account, fed by live Kite quotes each minute
  (LTP, day high/low, circuit limits). It applies Kite's rules: triggers ≥ 0.25% from the price (9 paise at
  ₹50 or below), OCO triggers either side of the price, triggered limit orders valid for the day only, buys
  rejected without funds, no buy fill at upper circuit and no sell fill at lower circuit. Every fill pays full
  Zerodha delivery charges (config `charges`).
- **Sizing and ranking per account:** qty = 1% risk ÷ (limit − stop), capped at 25% of capital; band A → B → C,
  then lower price, then higher traded value. Pending buy GTTs count toward the 5 slots, the sector cap of 2 and
  reserved cash. Every candidate not taken is stored with its reason (Signals screen, per account).
- **Trade states:** BUY_PENDING → FILLED → PROTECTED → TRAILING → CLOSED, or CANCELLED. The exit GTT is attached on
  the monitor cycle after the fill (or at EOD). Qty ≥ 4: OCO with the stop on all shares and the target on half;
  when the target leg fills a new stop goes on the rest. Qty 1–3: one stop, trailed from the day the close
  reaches the target. From the day after the target, EOD trailing sets stop = max(stop, entry, day low − 0.5%);
  updates Kite would reject are skipped and logged. Unfilled buy GTTs are cancelled after 2 sessions.
- **Controls:** the kill switch cancels every pending buy GTT; an account that falls 6% below its peak equity
  is halted until re-armed. Invariants are checked every cycle (one exit GTT per position, no orphan GTTs).
- **Accounts screen:** equity, free and reserved cash, positions, drawdown and the equity curve.
  **Positions & GTTs screen:** open trades with live P&L and R, exit orders, stop history and event log;
  closed trades with net P&L after charges.

## Phase 2b: manual changes, replay, scenario suite

- **Reconciliation** (every monitor cycle; the broker wins). What the agent does when you change something in Kite:

  | You… | The agent |
  |---|---|
  | move a stop or target | adopts it; trailing continues from it, only tightening |
  | cancel a buy GTT | marks the trade cancelled by you; no re-entry on that signal |
  | cancel an exit GTT | critical alert; no new exit until you press *Place a new exit* |
  | sell by hand | records the sale, shrinks or closes the trade, cancels leftover GTTs |
  | buy a stock yourself | leaves it unmanaged; it counts toward cash and the sector cap, and is never bought again |

  Every adopted change is logged as an override (Positions → trade detail → *Your changes*).
- **Paper edits on P60:** the Positions screen has Move stop, Move target, Cancel exit/buy GTT, Sell by hand and
  Buy outside the system, only on the manual-edit account. P30 and P100 stay untouched baselines.
- **Duplicate GTTs:** an exit GTT found at the broker but not recorded (a crash between placing and recording) is
  adopted, not duplicated; a leftover buy GTT is cancelled before a new one is placed; a stop on a stock no longer
  held is cancelled. Every cycle also checks broker holdings = open trades + outside holdings.
- **Split or bonus** on a held stock (seen in the candle refresh): management pauses (no trailing or re-placing)
  and a critical alert asks you to check the levels and press *Resume*.
- **Drawdown re-arm:** re-arming a halted account restarts its peak at the latest equity, so the 6% limit counts
  from there.
- **Replay mode:** `.venv\Scripts\python -m agent.cli replay --from 2025-01-01 --to 2026-09-18` runs the live
  engine over past sessions on a separate database (`runtime\replays\`; the live one is only read) and prints
  per-account return, drawdown, win rate, average R and charges; the full report is saved as JSON. Each session
  is simulated at the open, both extremes and the close (open → low → high → close on up days, open → high →
  low → close on down days), then the EOD run. Watchlist and valuation gates are skipped (no past fundamentals).
  A drawdown halt is re-armed the same evening in a replay (nobody is there to do it); the report counts halts.
- **Scenario suite** (`tests/test_scenarios.py`): every scenario in spec section 13 runs through the replay engine
  on synthetic candles.

## Charts

Every candle chart (Signals, Positions) has timeframe buttons: 1m, 5m, 15m, 1h, 4h and 1D. Daily bars come from
the local candle cache and show the Bollinger band and the signal marker. Intraday bars are built from the stored
1-minute candles (5m, 15m, 1h and 4h anchored at 09:15; 4h = 09:15–13:15 and 13:15–15:30), with today's session
added live from Kite during market hours; a stock with nothing stored yet is fetched from Kite directly. Shown in
exchange time. Charts are for viewing only; the strategy uses daily candles.

**1-minute candles:** Overview → Data setup → *Download 1 year* fetches a year of 1-minute bars for the NSE500
and the Nifty (about 3,500 Kite requests, roughly an hour, about 2 GB). After that the EOD run (fallback job 22:30) adds each
session. They live in their own file, `runtime\agent_1m.db` (keyed by instrument token, prices in paise), so the
main database and its daily backup stay small. A stopped download resumes where it left off.

## Commands

```
run.bat                              start backend + scheduler + dashboard
test.bat                             run the automated tests
.venv\Scripts\python -m agent.cli status    session, data coverage and job status
.venv\Scripts\python -m agent.cli backup    back up the database now
.venv\Scripts\python -m agent.cli results   fetch the NSE results calendar (upcoming + 3-year history)
.venv\Scripts\python -m agent.cli history   2-year signal scan, printed per month
.venv\Scripts\python -m agent.cli replay --from YYYY-MM-DD --to YYYY-MM-DD   replay the engine over past sessions
```

To work on the dashboard with live reload: `run.bat` in one window, then `cd dashboard && npm run dev`
in another, and open <http://127.0.0.1:5173>.

## Layout

```
agent/
  settings.py        .env secrets + config/*.yaml (nothing trading-related is hard-coded)
  db.py              SQLite schema (spec section 12), seeding, backups
  market_calendar.py NSE trading days, holidays, sessions
  scheduler.py       daily jobs (spec section 4)
  audit.py           audit_log + alerts
  api/app.py         REST + WebSocket for the dashboard
  broker/            Kite login/session, broker interface, PaperBroker, live quote feed
  data/              instruments, NSE500 list, candle cache, watchlist, NSE results calendar
  strategy/          market filter, indicators, Momentum v1 rules, gates and scanner
  risk/              kill switch, account halts, sizing and ranking
  orders/            trade state machine, reconciliation, paper edits, charges, trailing, equity, screen views
  replay.py          replay mode (same engine, past sessions, separate database)
  journal/ learning/ notify/   later phases
config/
  app.yaml           accounts, risk limits, schedule, holidays, Screener query
  strategies/momentum_v1.yaml   every Momentum v1 number
dashboard/           React + Vite (Journal and Learning are placeholders until Phase 3)
tests/               pytest suite (fake Kite client, no network)
runtime/             database, backups, logs (not committed)
```

## Rules that hold from Phase 0

- **Fail closed.** No session, an expired token, or missing or stale data means no new orders. A rejected token
  logs the session out and raises an alert.
- **No automated 2FA.** Login is always manual on Zerodha's page.
- **Versioned strategy config.** `momentum_v1` is registered in the database with a fingerprint. If the YAML is
  edited in place, the app refuses to start. Changes become new versions through the Learning screen (Phase 3).
- **Live trading is locked.** `broker.mode: live` is rejected until KiteBroker exists and paper testing passes.
- **Local only.** The server binds to 127.0.0.1 and refuses anything else. State-changing API calls need an
  explicit confirmation.
- **Kill switch.** Cancels every pending buy GTT and blocks new entries until re-armed.

## Design notes (spec deviations to review)

- Additions to the spec's data model: an `app_state` table (Kite session, kill switch, job runs, download
  progress), `level` / `acknowledged` columns on `audit_log`, and `eligible` / `gates` / `block_reasons` /
  `source` columns on `signals` (every technical trigger is stored with the reason it was or wasn't tradeable).
- Indicators are computed in pandas rather than pandas-ta (pandas-ta doesn't support Python 3.14 reliably);
  the tests pin them to TA-Lib reference values.
- **Split/bonus handling in the candle cache.** Kite back-adjusts history after corporate actions. Each refresh
  re-reads the last 5 sessions. If an old close moved by more than 0.5%, the stock's history is re-downloaded
  and an alert is raised. The spec's "pause management and alert" rule for held stocks comes in Phase 2.
- **Market filter switched off** (decided 2026-09-23, `universe.market_filter.enabled: false`). A rough 2-year check
  found trades it allowed did worse than those it blocked. Nifty vs its 50-day SMA still shows on the Overview for
  information; set `enabled: true` to make it block entries again (it fails closed on missing or stale data).
- **Phase 2a decisions** (review): pending buys count toward slots, sector cap and cash; sizing uses the account's
  starting capital; after the target leg fills, the new stop sits at the old stop until that EOD moves it to entry;
  paper fills: a GTT armed before the open is checked against the day's high/low, if both OCO legs are touched in
  the same window the stop is assumed first, a just-triggered order fills at the trigger price (or at the open
  when the stock gapped through it) and a resting one at its limit.
- **Gap below the stop** (decided 2026-09-23): if the stop's limit order doesn't fill, it stays live for the day. If
  it expires and the close is still below the stop, the agent sells the whole position at the next open with a
  GTT-only OCO around the close (0.25% + one tick either side, both legs full quantity). Spec section 6 updated.
- **Partial fills** (decided 2026-09-23): an order is done only when filled quantity = order quantity. The paper
  broker fills at most what the market depth offers at the limit each minute; the rest keeps working. Shares
  bought so far get a stop on the next cycle and the stop grows as more fill. A partly filled exit keeps selling;
  anything still held when it ends gets a new exit. Every cycle checks broker holdings = trades' open quantity.
  If a buy is only partly filled when the session ends, the agent keeps the shares bought (open question).
- The holiday list covers 2026 only. The Overview warns 30 days before it runs out.
-sample change