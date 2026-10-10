import { useRef, useState } from 'react'
import { api, fmtDate, fmtInr, fmtTime } from '../api.js'
import { Card, Notice, Pill, Progress, Tile, useConfirm } from '../components.jsx'

const LEVEL = { info: 'neutral', warning: 'warning', error: 'serious', critical: 'critical' }
const RUN = { ok: 'good', skipped: 'warning', error: 'critical' }

export default function Overview({ ov, reload }) {
  const [confirm, dialog] = useConfirm()
  const [msg, setMsg] = useState(null)

  if (!ov) return <><h1>Overview</h1><p className="muted">Loading…</p></>

  // Wrap every state-changing call: confirm → call → show outcome → refetch.
  const act = async (opts, fn) => {
    if (opts && !(await confirm(opts))) return
    try {
      const r = await fn()
      if (opts && opts.done) setMsg({ kind: 'good', text: typeof opts.done === 'function' ? opts.done(r) : opts.done })
    } catch (e) {
      setMsg({ kind: 'error', text: e.message })
    }
    reload()
  }

  const { session, market, market_filter: mf, kill_switch: ks, data } = ov

  return (
    <>
      <div className="title-row">
        <h1>Overview</h1>
        <span className="muted">as of {fmtTime(ov.now)}</span>
      </div>

      {msg && <Notice kind={msg.kind} onClose={() => setMsg(null)}>{msg.text}</Notice>}
      {market.calendar_expiring && (
        <Notice kind="warning">The holiday calendar in <code>config/app.yaml</code> ends on {fmtDate(market.calendar_valid_through)}. Add next year's NSE holidays.</Notice>
      )}

      <div className="tiles">
        <Tile label="Kite session"
          value={session.valid ? session.user_id : 'Logged out'}
          status={<Pill kind={session.valid ? 'good' : 'critical'}>{session.valid ? 'Active' : 'No new orders'}</Pill>}
          sub={session.valid ? `Expires ${fmtTime(session.expires_at)}` : <a href="#/login">Go to Login</a>} />
        <Tile label="Market today"
          value={market.holiday ? 'Holiday' : market.is_trading_day ? (market.is_open ? 'Open' : 'Closed') : 'Weekend'}
          status={<Pill kind={market.is_trading_day ? 'good' : 'neutral'}>{market.holiday || (market.is_trading_day ? 'Trading day' : 'No trading')}</Pill>}
          sub={`Next session ${fmtDate(market.next_trading_day)}`} />
        <Tile label={mf.enabled === false ? 'Nifty 50 vs 50-DMA · information' : 'Market filter · Nifty 50 vs 50-DMA'}
          value={mf.close ? `${mf.close.toLocaleString('en-IN')} / ${mf.sma.toLocaleString('en-IN')}` : '—'}
          status={mf.enabled === false
            ? <Pill kind="neutral">{mf.state === 'above' ? 'Above SMA' : mf.state === 'below' ? 'Below SMA' : 'No data'} · filter off</Pill>
            : <Pill kind={mf.allow_entries ? 'good' : 'critical'}>{mf.allow_entries ? 'Entries allowed' : 'Entries blocked'}</Pill>}
          sub={mf.enabled === false ? 'Not used to block entries' : mf.reason || `Close ≥ SMA on ${fmtDate(mf.date)}`} />
        <PriceFeedTile f={ov.price_feed} open={market.is_open} />
        <Tile label="Kill switch"
          value={ks.active ? 'ON' : 'Armed'}
          status={<Pill kind={ks.active ? 'critical' : 'good'}>{ks.active ? 'New entries blocked' : 'Normal'}</Pill>}
          sub={ks.active
            ? <button className="btn btn-sm" onClick={() => act({ title: 'Re-arm the kill switch?', body: 'New entries will be allowed again on the next EOD run.', confirmLabel: 'Re-arm', done: 'Kill switch re-armed.' }, () => api.confirm('/api/kill-switch/rearm'))}>Re-arm</button>
            : <button className="btn btn-sm btn-danger" onClick={() => act({ title: 'Activate the kill switch?', body: 'Cancels every pending buy GTT in all accounts and blocks new entries until you re-arm. Open positions keep their stops.', confirmLabel: 'Activate kill switch', danger: true, done: 'Kill switch is ON.' }, () => api.confirm('/api/kill-switch'))}>Activate kill switch</button>} />
      </div>

      {!data.coverage.complete && <DataSetup data={data} session={session} act={act} />}

      <Card title={`Alerts${ov.alerts.length ? ` (${ov.alerts.length})` : ''}`}
        action={Object.entries(ov.alerts.reduce((m, a) => ({ ...m, [a.name]: (m[a.name] || 0) + 1 }), {})).filter(([, n]) => n > 1).map(([name, n]) => (
          <button key={name} className="btn btn-sm" onClick={() => act({ title: `Dismiss all ${n} "${name}" alerts?`, confirmLabel: `Dismiss ${n}`,
            body: 'They stay in the audit log; only the Overview list is cleared.', done: `Dismissed ${n} "${name}" alerts.` },
          () => api.confirm(`/api/alerts/ack-all/${encodeURIComponent(name)}`))}>Dismiss all {n} · {name}</button>
        ))}>
        {ov.alerts.length === 0 ? <p className="muted">No active alerts.</p> : (
          <ul className="alerts">
            {ov.alerts.map((a) => (
              <li key={a.id}>
                <Pill kind={LEVEL[a.level]}>{a.level}</Pill>
                <div className="alert-body">
                  <div>{a.message}</div>
                  <div className="fine">{fmtTime(a.ts)} · {a.name}</div>
                </div>
                <button className="btn btn-sm" onClick={() => act(null, () => api.post(`/api/alerts/${a.id}/ack`))}>Dismiss</button>
              </li>
            ))}
          </ul>
        )}
      </Card>

      <div className="grid-2">
        <Card title="Accounts">
          <table>
            <thead><tr><th>Account</th><th className="num">Equity</th><th className="num">Return</th><th className="num">Positions</th><th className="num">Drawdown</th><th>State</th><th /></tr></thead>
            <tbody>
              {ov.accounts.map((a) => (
                <tr key={a.id}>
                  <td><a href="#/accounts"><strong>{a.id}</strong></a> <span className="fine">{a.manual_edits ? 'manual edits' : 'baseline'}</span></td>
                  <td className="num">{fmtInr(a.equity)}</td>
                  <td className="num">{a.return_pct > 0 ? '+' : ''}{a.return_pct.toFixed(2)}%</td>
                  <td className="num">{a.open_positions}{a.pending_buys ? <span className="fine"> +{a.pending_buys} pending</span> : null}</td>
                  <td className="num">{a.drawdown_pct.toFixed(1)}%</td>
                  <td>{a.halted ? <Pill kind="critical" title={a.halt_reason}>Halted</Pill> : <Pill kind="good">Active</Pill>}</td>
                  <td className="num">
                    {a.halted ? <button className="btn btn-sm" onClick={() => act({ title: `Re-arm ${a.id}?`, body: `Halted because: ${a.halt_reason}. New entries resume on the next EOD run.`, confirmLabel: 'Re-arm', done: `${a.id} re-armed.` }, () => api.confirm(`/api/accounts/${a.id}/rearm`))}>Re-arm</button> : null}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="fine">Paper accounts, marked to the latest live price or close. Details on the Accounts screen.</p>
        </Card>

        <Card title="Scheduled jobs">
          <table>
            <thead><tr><th>Job</th><th>When</th><th>Last run</th><th /></tr></thead>
            <tbody>
              {ov.jobs.map((j) => (
                <tr key={j.name} className={j.built ? '' : 'dim'}>
                  <td>{j.label}</td>
                  <td className="fine">{j.when}</td>
                  <td>
                    {!j.built ? <span className="phase-tag">Phase {j.phase}</span>
                      : j.held ? <span title="Held until the login check confirms your Kite login"><Pill kind="warning">On hold · waiting for login</Pill></span>
                      : j.last_run ? (
                        <span title={j.last_run.message}>
                          <Pill kind={RUN[j.last_run.status]}>{j.last_run.status}</Pill> <span className="fine">{fmtTime(j.last_run.at)}</span>
                        </span>
                      ) : <span className="fine">never</span>}
                  </td>
                  <td className="num">
                    {j.built && <button className="btn btn-sm" onClick={() => act({ title: `Run ${j.label} now?`, confirmLabel: 'Run now', done: `${j.label} started.` }, () => api.confirm(`/api/jobs/${j.name}/run`))}>Run</button>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {ov.jobs.some((j) => j.held) && <p className="fine">Every job waits until the login check confirms today's Kite login (it checks every minute). Missed or held jobs then run in order.</p>}
          {ov.last_scan && <p className="fine">Last scan {ov.last_scan.date}: {ov.last_scan.triggers} triggers, {ov.last_scan.eligible} tradeable · <a href="#/signals">Signals</a></p>}
          <p className="fine">
            Results calendar: {ov.results_calendar.fresh ? `current (fetched ${fmtTime(ov.results_calendar.last_ok_at)})` : `not fetched since the last session's close — new entries blocked`}
            {ov.results_calendar.history && ov.results_calendar.history.ok ? '' : ' · history not loaded'} · <a href="#/watchlist">Watchlist</a>
          </p>
        </Card>
      </div>

      {data.coverage.complete && <DataSetup data={data} session={session} act={act} />}
      {dialog}
    </>
  )
}

function DataSetup({ data, session, act }) {
  const fileRef = useRef(null)
  const cov = data.coverage
  const bf = data.backfill
  const running = bf && bf.running
  const needLogin = !session.valid
  const auto = data.nse500_auto || {}
  const nse500Done = (r) => ({
    updated: `NSE500 list updated: ${r.matched} stocks${r.added?.length ? `, ${r.added.length} joined` : ''}${r.removed?.length ? `, ${r.removed.length} left` : ''}.`,
    unchanged: 'NSE500 list is current: nothing changed.',
    failed: `Could not download from NSE: ${r.detail}`,
    blocked: `NSE's list would change ${(r.added?.length || 0) + (r.removed?.length || 0)} stocks at once; not applied.`,
    skipped: r.detail, off: 'Automatic update is off.',
  }[r.status] || r.status)

  const upload = (file) => act(
    { title: `Upload ${file.name}?`, body: 'Replaces the current NSE500 list and sectors.', confirmLabel: 'Upload',
      done: (r) => `NSE500 list: ${r.matched} of ${r.rows} matched.${r.unmatched.length ? ` Not found on Kite: ${r.unmatched.slice(0, 8).join(', ')}${r.unmatched.length > 8 ? '…' : ''}` : ''}` },
    () => api.upload('/api/data/nse500', file),
  )

  return (
    <Card title="Data setup" action={cov.complete ? <Pill kind="good">Phase 0 data complete</Pill> : <Pill kind="warning">Incomplete</Pill>}>
      <ol className="steps">
        <li>
          <div className="step-head">
            <strong>Instruments</strong>
            {data.instruments_synced_at ? <Pill kind="good">Synced {fmtTime(data.instruments_synced_at)}</Pill> : <Pill kind="warning">Not synced</Pill>}
          </div>
          <p className="fine">Refreshed automatically after each login and at 09:00.</p>
          <button className="btn btn-sm" disabled={needLogin} title={needLogin ? 'Log in first' : ''}
            onClick={() => act({ title: 'Sync instruments from Kite now?', confirmLabel: 'Sync', done: (r) => `${r.stored} NSE instruments stored.` }, () => api.confirm('/api/data/instruments/sync'))}>
            Sync now
          </button>
        </li>
        <li>
          <div className="step-head">
            <strong>NSE500 list</strong>
            {data.nse500 ? <Pill kind={auto.last_error && !auto.last_ok ? 'warning' : 'good'}>{data.nse500.matched} stocks · {fmtDate(data.nse500.at.slice(0, 10))}</Pill> : <Pill kind="warning">Not loaded yet</Pill>}
          </div>
          <p className="fine">
            {auto.enabled === false
              ? 'Automatic update is off (app.yaml nse500.auto_update). Upload NSE\'s ind_nifty500list.csv by hand.'
              : <>Downloaded from NSE automatically every trading day after the instruments sync; every stock in it streams live prices.
                {auto.last_ok ? <> Last checked {fmtTime(auto.last_ok)}{auto.last_change ? `, last changed ${fmtTime(auto.last_change)}` : ''}.</> : ''}</>}
            {auto.last_error && (!auto.last_ok || auto.last_attempt > auto.last_ok) ? <> <span className="text-bad">Last download failed; the current list stays.</span></> : ''}
            {auto.blocked ? <> <span className="text-bad">NSE's list would change {auto.blocked.added + auto.blocked.removed} stocks at once; not applied.</span></> : ''}
          </p>
          <input ref={fileRef} type="file" accept=".csv,text/csv" hidden
            onChange={(e) => { const f = e.target.files[0]; e.target.value = ''; if (f) upload(f) }} />
          <div className="row-gap">
            <button className="btn btn-sm" disabled={!data.instruments_synced_at} title={!data.instruments_synced_at ? 'Instruments first' : ''}
              onClick={() => act({ title: 'Check the NSE500 list with NSE now?', body: 'Downloads NSE\'s current Nifty 500 list and applies it if it changed. Pending buys in stocks that left are cancelled; open positions are kept.', confirmLabel: 'Check now', done: nse500Done }, () => api.confirm('/api/data/nse500/check'))}>
              Check now
            </button>
            {auto.blocked && (
              <button className="btn btn-sm" onClick={() => act({ title: 'Apply NSE\'s list anyway?', body: `${auto.blocked.added} stocks join and ${auto.blocked.removed} leave. Pending buys in stocks that leave are cancelled.`, confirmLabel: 'Apply', done: nse500Done }, () => api.confirm('/api/data/nse500/check?force=true'))}>
                Apply anyway
              </button>
            )}
            <button className="btn btn-sm" disabled={!data.instruments_synced_at} onClick={() => fileRef.current.click()}>Upload CSV instead</button>
          </div>
        </li>
        <li>
          <div className="step-head">
            <strong>Daily candles</strong>
            {cov.nse500_symbols
              ? <Pill kind={cov.complete ? 'good' : 'warning'}>{cov.current} / {cov.nse500_symbols} current to {fmtDate(cov.upto)}</Pill>
              : <Pill kind="warning">No stocks yet</Pill>}
          </div>
          <dl className="kv kv-inline">
            <dt>Missing</dt><dd>{cov.missing.length}</dd>
            <dt>Behind</dt><dd>{cov.stale.length}</dd>
            <dt>Nifty 50</dt><dd>{cov.nifty.last ? `to ${fmtDate(cov.nifty.last)}` : 'none'}</dd>
            <dt>From</dt><dd>{fmtDate(cov.earliest)}</dd>
            <dt>Candles</dt><dd>{cov.total_rows.toLocaleString('en-IN')}</dd>
          </dl>
          {running && (
            <div className="bf-progress">
              <Progress value={bf.done} max={bf.total} />
              <span className="fine">Now: {bf.current} · ~{Math.ceil(((bf.total - bf.done) / 2.5) / 60)} min left</span>
            </div>
          )}
          {bf && !running && bf.finished_at && (
            <p className="fine">
              Last run {fmtTime(bf.finished_at)}: {bf.updated} updated, {bf.skipped} already current, {bf.error_count} failed
              {bf.adjusted && bf.adjusted.length ? `, ${bf.adjusted.length} split/bonus re-downloads` : ''}.
              {bf.aborted ? ` Stopped: ${bf.aborted}.` : ''}
            </p>
          )}
          {!!cov.missing.length && cov.missing.length <= 20 && <p className="fine">Missing: {cov.missing.join(', ')}</p>}
          <button className="btn btn-sm btn-primary" disabled={needLogin || running || !cov.nse500_symbols}
            title={needLogin ? 'Log in first' : !cov.nse500_symbols ? 'Upload the NSE500 list first' : ''}
            onClick={() => act({ title: 'Download daily candles?', body: `Fetches missing history for ${cov.nse500_symbols} stocks plus Nifty 50 within Kite's rate limit. A first run takes about 4 minutes; later runs only fetch new days.`, confirmLabel: 'Start download', done: 'Candle download started.' }, () => api.confirm('/api/data/backfill'))}>
            {cov.total_rows ? 'Refresh candles' : 'Download candles'}
          </button>
          <p className="fine">The EOD run refreshes candles automatically every trading day at 21:00, once Kite has the official NSE closing prices.</p>
        </li>
        <MinuteStep m={data.minute || {}} needLogin={needLogin} ready={!!cov.nse500_symbols} act={act} />
      </ol>
    </Card>
  )
}

function MinuteStep({ m, needLogin, ready, act }) {
  const running = m.running
  return (
    <li>
      <div className="step-head">
        <strong>1-minute candles</strong>
        {m.latest ? <Pill kind="good">{m.symbols} stocks · to {fmtTime(m.latest)}</Pill> : <Pill kind="neutral">Not downloaded</Pill>}
      </div>
      <dl className="kv kv-inline">
        <dt>From</dt><dd>{m.earliest ? fmtDate(m.earliest.slice(0, 10)) : '—'}</dd>
        <dt>File</dt><dd>{m.file_mb != null ? `${m.file_mb.toLocaleString('en-IN')} MB` : '—'}</dd>
      </dl>
      {running && m.total ? (
        <div className="bf-progress">
          <Progress value={m.done} max={m.total} />
          <span className="fine">Now: {m.current} · {Number(m.rows_added || 0).toLocaleString('en-IN')} bars so far</span>
        </div>
      ) : null}
      {m.older_total ? (
        <p className="fine">
          Older history (back to 2015, as far as Kite has): {m.older_done || 0} of {m.older_total} stocks complete
          {m.older_state === 'complete' ? '.' : m.older_state === 'waiting' ? ': continues after today\'s close (it pauses 08:45–16:00 on trading days).' : running ? ': downloading now.' : ': continues with the next run after the close.'}
        </p>
      ) : null}
      {!running && m.finished_at && (
        <p className="fine">Last run {fmtTime(m.finished_at)}: {Number(m.rows_added || 0).toLocaleString('en-IN')} bars added, {m.error_count || 0} failed{m.aborted ? ` · stopped: ${m.aborted}` : ''}.</p>
      )}
      {running && (
        <button className="btn btn-sm" disabled={m.stopping}
          onClick={() => act({ title: 'Pause the 1-minute download?', confirmLabel: 'Pause after this stock',
            body: `Finishes ${m.current || 'the stock being downloaded'}, then stops. The rest is picked up after the next EOD run (or when you press Download).`,
            done: 'Pausing after the current stock.' }, () => api.confirm('/api/data/minute/stop'))}>
          {m.stopping ? `Pausing after ${m.current || 'this stock'}…` : 'Pause after this stock'}
        </button>
      )}{' '}
      <button className="btn btn-sm btn-primary" disabled={needLogin || running || !ready}
        title={needLogin ? 'Log in first' : !ready ? 'Upload the NSE500 list first' : ''}
        onClick={() => act({ title: m.latest ? 'Fetch new 1-minute bars?' : 'Download a year of 1-minute candles?',
          body: m.latest ? 'Adds any sessions not stored yet and finishes a paused download. It also runs by itself after every EOD run.'
            : 'About 3,500 requests to Kite for 500 stocks plus Nifty: roughly an hour, about 2 GB on disk (runtime\\agent_1m.db). Trading is unaffected, and a stopped download resumes where it left off.',
          confirmLabel: 'Start download', done: '1-minute download started.' }, () => api.confirm('/api/data/minute'))}>
        {m.latest ? (m.aborted ? 'Resume download' : 'Fetch new bars') : 'Download 1 year'}
      </button>
      <p className="fine">Used for the 1m–4h charts. Stored in its own file, so the daily backup stays small.</p>
    </li>
  )
}

function PriceFeedTile({ f, open }) {
  if (!f) return null
  const streaming = f.mode === 'stream'
  const on = f.mode !== 'rest' || f.connected !== undefined
  return (
    <Tile label="Live prices"
      value={streaming ? 'Streaming' : on ? (open ? 'REST quotes' : 'Stream idle') : 'REST quotes'}
      status={<Pill kind={streaming ? 'good' : open && on ? 'warning' : 'neutral'}>
        {streaming ? `WebSocket · ${f.streaming} of ${f.subscribed} stocks ticking` : on ? (open ? 'Stream not connected: polling each minute' : 'Connects 09:00–15:35') : 'WebSocket switched off'}</Pill>}
      sub={<>
        {streaming ? `${f.fast_cycles || 0} tick-driven monitor cycles since start` : (f.last_error ? `Last stream error: ${f.last_error}` : 'Falls back to the REST quote each minute')}
        {f.seconds && <div>{f.seconds.paused_low_disk
          ? <span className="text-bad">1-second recording paused: low disk space ({f.seconds.free_gb} GB free)</span>
          : `1-second bars: ${(f.seconds.seconds || 0).toLocaleString('en-IN')} recorded since start · today ${f.seconds.today_mb} MB · ${f.seconds.free_gb ?? '—'} GB free`}</div>}
      </>} />
  )
}
