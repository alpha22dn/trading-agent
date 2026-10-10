import { useCallback, useEffect, useRef, useState } from 'react'
import { api, fmtDate, fmtTime } from '../api.js'
import { Card, Notice, Pill, useConfirm } from '../components.jsx'

const GATE_LABELS = [
  ['nse500', 'NSE500'], ['watchlist', 'Watchlist'], ['valuation', 'Valuation'], ['liquidity', 'Liquidity'], ['results', 'No results soon'],
]

export function Gate({ ok }) {
  if (ok === null || ok === undefined) return <span className="gate gate-na" aria-label="not checked">–</span>
  return <span className={`gate ${ok ? 'gate-ok' : 'gate-no'}`} aria-label={ok ? 'pass' : 'fail'}>{ok ? '✓' : '✗'}</span>
}

export default function Watchlist({ tick }) {
  const [data, setData] = useState(null)
  const [cal, setCal] = useState(null)
  const [all, setAll] = useState(false)
  const [msg, setMsg] = useState(null)
  const [filter, setFilter] = useState('')
  const [confirm, dialog] = useConfirm()
  const fileRef = useRef(null)
  const xlsxRef = useRef(null)

  const load = useCallback(() => {
    api.get(`/api/watchlist${all ? '?all=1' : ''}`).then(setData).catch((e) => setMsg({ kind: 'error', text: e.message }))
    api.get('/api/results-calendar').then(setCal).catch(() => {})
  }, [all])
  useEffect(load, [load, tick])

  const act = async (opts, fn) => {
    if (!(await confirm(opts))) return
    try {
      const r = await fn()
      if (opts.done) setMsg({ kind: 'good', text: typeof opts.done === 'function' ? opts.done(r) : opts.done })
    } catch (e) { setMsg({ kind: 'error', text: e.message }) }
    load()
  }

  const uploadXlsx = (f) => act(
    { title: `Replace the watchlist from ${f.name}?`, body: 'Reads Company links, EPS 12M and Profit growth from the Screener table in the file. The previous list is kept in history.', confirmLabel: 'Replace watchlist',
      done: (r) => `Watchlist: ${r.accepted} stocks.` + (r.not_in_nse500.length ? ` Dropped (not in NSE500): ${r.not_in_nse500.length}.` : '') + (r.bse_only.length ? ` BSE-only skipped: ${r.bse_only.length}.` : '') + (r.bad_lines.length ? ` Unreadable rows: ${r.bad_lines.length}.` : '') + (r.rescan ? ' Re-scanning signals with the new list…' : '') },
    () => api.upload('/api/watchlist/upload', f),
  )

  const pastedDays = data && data.pasted_on ? Math.floor((Date.now() - new Date(data.pasted_on)) / 86400000) : null
  const rows = data ? data.rows.filter((r) => !filter || r.symbol.includes(filter.toUpperCase()) || (r.sector || '').toLowerCase().includes(filter.toLowerCase())) : []
  const st = cal && cal.status

  return (
    <>
      <h1>Watchlist</h1>
      <p className="lead">A stock can signal only if it passes all five gates, checked every EOD run. Gates 1–2 come from your uploads; 3–5 are recomputed from today's prices and NSE's results calendar.</p>
      {msg && <Notice kind={msg.kind} onClose={() => setMsg(null)}>{msg.text}</Notice>}

      <div className="grid-2">
        <Card title="Screener results" action={data && data.watchlist_size > 0 && <Pill kind={pastedDays > data.stale_after_days ? 'warning' : 'good'}>{data.watchlist_size} stocks · pasted {fmtDate(data.pasted_on.slice(0, 10))}</Pill>}>
          {pastedDays !== null && pastedDays > data.stale_after_days && <Notice kind="warning">The watchlist is {pastedDays} days old. Re-run the Screener query and upload fresh results.</Notice>}
          <div className="actions" style={{ marginTop: 0 }}>
            <input ref={xlsxRef} type="file" accept=".xlsx,.xlsm" hidden onChange={(e) => { const f = e.target.files[0]; e.target.value = ''; if (f) uploadXlsx(f) }} />
            <button className="btn btn-primary" onClick={() => xlsxRef.current.click()}>Upload Excel file</button>
          </div>
          <ol className="how">
            <li>Run the query on Screener (copy it from the Login screen).</li>
            <li>Select the results table and paste it into a blank Excel sheet; save it as .xlsx.</li>
            <li>Upload the file here. It replaces the whole watchlist; the previous one is kept in history.</li>
          </ol>
          <p className="fine">The results need the columns <strong>EPS 12M</strong> and <strong>Profit growth</strong> (Screener: Edit columns). NSE codes are read from the company-name links Excel keeps, so paste the table as is. Stocks outside NSE500 and BSE-only companies are dropped.</p>
        </Card>

        <Card title="Results calendar (NSE board meetings)">
          {!st ? <p className="muted">Loading…</p> : (
            <>
              <div className="session-status">
                <Pill kind={st.fresh ? 'good' : 'critical'}>{st.fresh ? `Current for ${fmtDate(st.session)} · fetched ${fmtTime(st.last_ok_at)}` : `Not fetched since the ${fmtDate(st.session)} close`}</Pill>
                <Pill kind={st.history && st.history.ok ? 'good' : 'warning'}>{st.history && st.history.ok ? `History from ${fmtDate(st.history.from)}` : 'History not loaded'}</Pill>
              </div>
              {st.daily && st.daily.error && <p className="fine">Last error: {st.daily.error}</p>}
              <p className="fine">{st.count} results meetings stored. Fetched automatically at the day's first Kite login and in the EOD run. Signals need a calendar fetched after their session's close; without one, no new entries are placed.</p>
              <div className="actions">
                <button className="btn btn-sm" onClick={() => act({ title: 'Fetch upcoming results meetings from NSE?', confirmLabel: 'Fetch', done: 'Fetching…' }, () => api.post('/api/results-calendar/fetch', { confirm: true }))}>Fetch now</button>
                <button className="btn btn-sm" onClick={() => act({ title: 'Load 3 years of results history from NSE?', body: 'About 40 requests, one per month, paced one per second. Needed for the "same quarter last year" window and for the historical scan.', confirmLabel: 'Load history', done: 'Loading results history…' }, () => api.post('/api/results-calendar/fetch', { confirm: true, history: true }))}>Load 3-year history</button>
              </div>
              {cal.upcoming.length > 0 && (
                <>
                  <h3 className="sub">Next 30 days</h3>
                  <ul className="chips">
                    {cal.upcoming.filter((u) => u.on_watchlist || all).slice(0, 40).map((u) => (
                      <li key={u.symbol + u.event_date}><strong>{u.symbol}</strong> {fmtDate(u.event_date)}</li>
                    ))}
                    {!all && !cal.upcoming.some((u) => u.on_watchlist) && <li className="muted">None for watchlist stocks</li>}
                  </ul>
                </>
              )}
            </>
          )}
        </Card>
      </div>

      <Card title={data ? `Eligibility · ${data.eligible_count} of ${data.rows.length} ${all ? 'NSE500' : 'watchlist'} stocks pass all gates` : 'Eligibility'}
        action={
          <div className="toolbar">
            <input className="search" placeholder="Filter stock or sector" value={filter} onChange={(e) => setFilter(e.target.value)} aria-label="Filter" />
            <label className="toggle"><input type="checkbox" checked={all} onChange={(e) => setAll(e.target.checked)} /> All NSE500</label>
          </div>
        }>
        {!data ? <p className="muted">Loading…</p> : rows.length === 0 ? (
          <p className="muted">{all ? 'No stocks match.' : 'The watchlist is empty. Upload your Screener results above.'}</p>
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Stock</th><th>Sector</th><th className="num">Close</th><th className="num">EPS</th><th className="num">P/E</th>
                  <th className="num">Profit gr. %</th><th className="num">₹ cr/day</th><th>Next results</th>
                  {GATE_LABELS.map(([k, l]) => <th key={k} className="ctr">{l}</th>)}
                  <th className="ctr">Eligible</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((r) => (
                  <tr key={r.symbol}>
                    <td><strong>{r.symbol}</strong></td>
                    <td className="fine-cell">{r.sector}</td>
                    <td className="num">{r.close != null ? r.close.toLocaleString('en-IN', { maximumFractionDigits: 2 }) : '—'}</td>
                    <td className="num">{r.eps ?? '—'}</td>
                    <td className="num">{r.pe ?? '—'}</td>
                    <td className="num" title={r.growth_used != null && r.growth_used < r.profit_growth ? `Capped at ${r.growth_used} for the P/E check` : ''}>{r.profit_growth ?? '—'}{r.growth_used != null && r.growth_used < r.profit_growth ? <span className="fine"> → {r.growth_used}</span> : null}</td>
                    <td className="num">{r.traded_value_cr ?? '—'}</td>
                    <td title={r.results_reason || ''}>{r.next_results ? fmtDate(r.next_results) : '—'}</td>
                    {GATE_LABELS.map(([k]) => <td key={k} className="ctr" title={k === 'results' && r.results_reason ? r.results_reason : ''}><Gate ok={r.gates[k]} /></td>)}
                    <td className="ctr">{r.eligible ? <Pill kind="good">Yes</Pill> : <span className="fine">No</span>}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        <p className="fine">
          Valuation: P/E = today's close ÷ uploaded EPS; needs P/E &gt; 0 and P/E &lt; profit growth, with growth capped at {data ? data.growth_cap : 40}% so a one-year jump off a weak base can't let an expensive stock through. Liquidity: 20-day average traded value ≥ ₹10 crore.
          No results soon: no results meeting within the next 5 trading days, and not within 7 days of last year's same-quarter results date. As of {data ? fmtDate(data.as_of) : '—'}.
        </p>
      </Card>

      <Card title="NSE500 list">
        <p className="fine">
          Updated automatically from NSE every trading day (Overview → Data setup shows the last check). Sectors come from its Industry column.
          Uploading <a href="https://niftyindices.com/IndexConstituent/ind_nifty500list.csv" target="_blank" rel="noreferrer">ind_nifty500list.csv</a> by hand is only needed if NSE can't be reached.
        </p>
        <input ref={fileRef} type="file" accept=".csv,text/csv" hidden
          onChange={(e) => { const f = e.target.files[0]; e.target.value = ''; if (f) act({ title: `Upload ${f.name}?`, body: 'Replaces the NSE500 list and sectors.', confirmLabel: 'Upload', done: (r) => `NSE500 list: ${r.matched} of ${r.rows} matched.` }, () => api.upload('/api/data/nse500', f)) }} />
        <button className="btn btn-sm" onClick={() => fileRef.current.click()}>Upload CSV instead</button>
      </Card>
      {dialog}
    </>
  )
}
