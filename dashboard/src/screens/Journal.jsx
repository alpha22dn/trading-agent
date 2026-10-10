import { Fragment, useCallback, useEffect, useMemo, useState } from 'react'
import { api, fmtDate, fmtInr, fmtTime } from '../api.js'
import CandleChart from '../CandleChart.jsx'
import { Card, Notice, Pill, Tile } from '../components.jsx'
import { REASON } from './Positions.jsx'

const fmt = (n, d = 2) => (n == null ? '—' : Number(n).toLocaleString('en-IN', { minimumFractionDigits: d, maximumFractionDigits: d }))
const signed = (n) => (n == null ? '—' : `${n < 0 ? '−' : n > 0 ? '+' : ''}${fmtInr(Math.abs(n))}`)
const rText = (n) => (n == null ? '—' : `${n > 0 ? '+' : ''}${fmt(n)}R`)
const upDown = (n) => (n > 0 ? 'up' : n < 0 ? 'down' : '')
const pct = (n) => (n == null ? '—' : `${n > 0 ? '+' : ''}${fmt(n)}%`)
const EMPTY = { account: '', symbol: '', result: '', exit_reason: '', band: '', start: '', end: '' }

function exitLabel(m) {
  if (m.target_reached && m.exit_reason === 'stop') return 'Target, then trailed stop'
  return REASON[m.exit_reason] || m.exit_reason || '—'
}

function Source({ e }) {
  if (e.narrative_source === 'llm') return <span className="tag tag-agent" title={e.model}>Explained by {e.model || 'LLM'}</span>
  return <span className="tag tag-manual" title={e.last_error || 'Written from the numbers by code'}>Template text</span>
}

export default function Journal({ tick }) {
  const [view, setView] = useState('trades')
  return (
    <>
      <div className="title-row">
        <h1>Journal</h1>
        <div className="tabs" role="tablist" aria-label="Journal view">
          {[['trades', 'Trades'], ['weekly', 'Weekly reviews']].map(([k, l]) => (
            <button key={k} role="tab" aria-selected={view === k} className={view === k ? 'on' : ''} onClick={() => setView(k)}>{l}</button>
          ))}
        </div>
      </div>
      {view === 'trades' ? <Trades tick={tick} /> : <Weekly tick={tick} />}
    </>
  )
}

// ---------- trades ----------

function Trades({ tick }) {
  const [f, setF] = useState(EMPTY)
  const [data, setData] = useState(null)
  const [open, setOpen] = useState(null)
  const [msg, setMsg] = useState(null)
  const query = useMemo(() => new URLSearchParams(Object.entries(f).filter(([, v]) => v)).toString(), [f])
  const load = useCallback(() => {
    api.get(`/api/journal${query ? `?${query}` : ''}`).then(setData).catch((e) => setMsg({ kind: 'error', text: e.message }))
  }, [query])
  useEffect(load, [load, tick])
  const [all, setAll] = useState(null)   // unfiltered, for the filter choices
  useEffect(() => { api.get('/api/journal').then(setAll).catch(() => {}) }, [tick])

  const set = (k) => (e) => setF({ ...f, [k]: e.target.value })
  const rows = data ? data.entries : []
  const choices = (fn) => [...new Set((all ? all.entries : []).map(fn).filter(Boolean))].sort()
  const s = useMemo(() => {
    const rs = rows.map((e) => e.metrics.r_multiple).filter((r) => r != null)
    const won = rows.filter((e) => (e.metrics.net_pnl || 0) > 0).length
    return { n: rows.length, won, avgR: rs.length ? rs.reduce((a, b) => a + b, 0) / rs.length : null,
      net: rows.reduce((a, e) => a + (e.metrics.net_pnl || 0), 0), charges: rows.reduce((a, e) => a + (e.metrics.charges || 0), 0) }
  }, [rows])
  const sel = rows.find((e) => e.id === open)
  const sync = async () => {
    try { await api.confirm('/api/journal/sync'); setMsg({ kind: 'good', text: 'Journaling closed trades and asking for explanations in the background…' }) }
    catch (e) { setMsg({ kind: 'error', text: e.message }) }
  }
  const waiting = rows.filter((e) => e.narrative_source !== 'llm').length

  return (
    <>
      {msg && <Notice kind={msg.kind} onClose={() => setMsg(null)}>{msg.text}</Notice>}
      {data && data.not_journaled > 0 && (
        <Notice kind="info">{data.not_journaled} closed trade{data.not_journaled > 1 ? 's are' : ' is'} not in the journal yet: they are added at the next EOD run.
          {' '}<button className="link" onClick={sync}>Add now</button></Notice>
      )}
      <div className="tiles">
        <Tile label="Trades" value={s.n} sub={s.n ? `${s.won} won · ${Math.round((s.won / s.n) * 100)}%` : 'none match'} />
        <Tile label="Average R" value={<span className={upDown(s.avgR)}>{rText(s.avgR)}</span>} sub="multiples of the initial risk" />
        <Tile label="Net P&L" value={<span className={upDown(s.net)}>{signed(s.net)}</span>} sub={`after ${fmtInr(s.charges)} charges`} />
        <Tile label="Explanations" value={data ? data.llm.provider === 'template' ? 'Template' : (data.llm.model || data.llm.provider) : '—'}
          sub={data && data.llm.provider !== 'template' ? (waiting ? `${waiting} still on template text` : 'all explained') : 'no LLM set up in .env'} />
      </div>

      <Card title="Closed trades" action={
        <div className="toolbar">
          {data && data.llm.provider !== 'template' && waiting > 0 && <button className="btn btn-sm" onClick={sync}>Explain now</button>}
          <a className="btn btn-sm" href={`/api/journal/export.csv${query ? `?${query}` : ''}`} download>Export CSV</a>
        </div>}>
        <div className="toolbar journal-filters" role="search">
          <label className="form-inline"><span>Portfolio</span>
            <select value={f.account} onChange={set('account')}><option value="">All</option>
              {choices((e) => e.metrics.account_id).map((a) => <option key={a}>{a}</option>)}</select></label>
          <label className="form-inline"><span>Symbol</span>
            <input className="search" value={f.symbol} onChange={set('symbol')} placeholder="e.g. TATA" style={{ width: 120 }} /></label>
          <label className="form-inline"><span>Result</span>
            <select value={f.result} onChange={set('result')}><option value="">All</option><option value="win">Won</option><option value="loss">Lost</option></select></label>
          <label className="form-inline"><span>Exit</span>
            <select value={f.exit_reason} onChange={set('exit_reason')}><option value="">All</option>
              {choices((e) => e.metrics.exit_reason).map((r) => <option key={r} value={r}>{REASON[r] || r}</option>)}</select></label>
          <label className="form-inline"><span>Band</span>
            <select value={f.band} onChange={set('band')}><option value="">All</option>
              {choices((e) => e.metrics.rr_band).map((b) => <option key={b}>{b}</option>)}</select></label>
          <label className="form-inline"><span>Closed from</span><input className="search" type="date" value={f.start} onChange={set('start')} style={{ width: 150 }} /></label>
          <label className="form-inline"><span>to</span><input className="search" type="date" value={f.end} onChange={set('end')} style={{ width: 150 }} /></label>
          {query && <button className="link" onClick={() => setF(EMPTY)}>Clear filters</button>}
        </div>
        {!data ? <p className="muted">Loading…</p> : rows.length === 0 ? (
          <p className="muted">{query ? 'No journal entries match these filters.' : 'No closed trades yet. Each trade is journaled at the EOD run after it closes.'}</p>
        ) : (
          <div className="table-wrap">
            <table className="clickable">
              <thead><tr>
                <th>Closed</th><th>Portfolio</th><th>Symbol</th><th>Band</th><th className="num">Qty</th><th className="num">Entry</th>
                <th className="num">Avg exit</th><th className="num">Days</th><th>Exit</th><th className="num">Net P&amp;L</th><th className="num">R</th>
                <th className="num" title="Best and worst point while open, in R">MFE / MAE</th><th />
              </tr></thead>
              <tbody>{rows.map((e) => {
                const m = e.metrics
                const ex = m.excursion || {}
                return (
                  <tr key={e.id} className={open === e.id ? 'selected' : ''} tabIndex={0} onClick={() => setOpen(open === e.id ? null : e.id)}
                    onKeyDown={(k) => k.key === 'Enter' && setOpen(open === e.id ? null : e.id)}>
                    <td>{fmtDate(e.closed_on)}</td>
                    <td>{m.account_id}</td>
                    <td><strong>{m.symbol}</strong>{m.overrides.length > 0 && <> <span className="tag tag-manual" title="Changed by hand">Edited</span></>}</td>
                    <td>{m.rr_band || '—'}</td>
                    <td className="num">{m.qty}</td>
                    <td className="num">{fmt(m.entry.price)}</td>
                    <td className="num">{fmt(m.avg_exit)}</td>
                    <td className="num">{m.holding_days}</td>
                    <td>{exitLabel(m)}</td>
                    <td className={`num ${upDown(m.net_pnl)}`}><strong>{signed(m.net_pnl)}</strong></td>
                    <td className={`num ${upDown(m.r_multiple)}`}>{rText(m.r_multiple)}</td>
                    <td className="num fine-cell">{ex.mfe_r != null ? `+${fmt(ex.mfe_r)} / −${fmt(ex.mae_r)}` : '—'}</td>
                    <td>{e.narrative_source === 'llm' ? <span aria-label="Explained" title="Explained">✦</span> : null}</td>
                  </tr>
                )
              })}</tbody>
            </table>
          </div>
        )}
        <p className="fine">P&amp;L is after all Zerodha charges. MFE / MAE: how far the price went for and against the trade while it was open,
          in multiples of the initial risk (1-minute bars when downloaded, else daily candles). Click a trade for its full record.</p>
      </Card>
      {sel && <EntryDetail e={sel} onClose={() => setOpen(null)} />}
    </>
  )
}

function EntryDetail({ e, onClose }) {
  const m = e.metrics
  const ex = m.excursion || {}
  const lv = m.levels
  const levels = useMemo(() => [
    { price: m.entry.price, title: 'Entry', kind: 'entry' },
    { price: lv.initial_stop, title: 'Initial stop', kind: 'initial_stop' },
    ...(lv.final_stop !== lv.initial_stop ? [{ price: lv.final_stop, title: 'Final stop', kind: 'stop' }] : []),
    { price: lv.target, title: m.target_reached ? 'Target (hit)' : 'Target', kind: m.target_reached ? 'target_hit' : 'target' },
  ], [e.id])
  const markers = useMemo(() => [
    ...(m.signal_date ? [{ at: m.signal_date, text: 'Signal', kind: 'signal' }] : []),
    { at: m.entry.at, text: `Buy ${m.qty}`, kind: 'buy' },
    ...m.exits.filter((x) => x.at).map((x) => ({ at: x.at, text: `Sell ${x.qty} (${REASON[x.reason] || x.reason})`, kind: 'sell' })),
  ], [e.id])
  const ind = m.signal_indicators || {}
  return (
    <Card title={`${m.symbol} · ${m.account_id} · trade ${m.trade_id}`} action={<button className="link" onClick={onClose} aria-label="Close">✕</button>}>
      <div className="journal-story">
        <Source e={e} />
        <p>{e.narrative}</p>
        {e.narrative_source !== 'llm' && e.last_error && <p className="fine">The LLM explanation failed ({e.last_error}); it is retried at the next EOD run.</p>}
      </div>
      <div className="detail">
        <div>
          <h3 className="sub">Plan</h3>
          <dl className="kv">
            <dt>Signal</dt><dd>{fmtDate(m.signal_date)} · band {m.rr_band || '—'} · RR {fmt(m.rr)}</dd>
            <dt>Trigger / limit</dt><dd>{fmt(lv.entry_trigger)} / {fmt(lv.entry_limit)}</dd>
            <dt>Stop</dt><dd>{fmt(lv.initial_stop)}{lv.final_stop !== lv.initial_stop ? ` → ${fmt(lv.final_stop)}` : ''}</dd>
            <dt>Target</dt><dd>{fmt(lv.target)}</dd>
            <dt>Sizing</dt><dd>{m.qty} shares · {m.sizing_case || '—'} · risk {fmtInr(m.risk_rupees)}</dd>
            <dt>Version</dt><dd>{m.strategy_version}</dd>
          </dl>
        </div>
        <div>
          <h3 className="sub">Result</h3>
          <dl className="kv">
            <dt>Bought</dt><dd>{fmt(m.entry.price)} · {fmtTime(m.entry.at)}</dd>
            {m.exits.map((x, i) => <Fragment key={i}><dt>Sold {x.qty}</dt><dd>{fmt(x.price)} · {REASON[x.reason] || x.reason} · {fmtTime(x.at)}</dd></Fragment>)}
            <dt>Held</dt><dd>{m.holding_days} session{m.holding_days === 1 ? '' : 's'}</dd>
            <dt>Charges</dt><dd>{fmtInr(m.charges)}</dd>
            <dt>Net P&amp;L</dt><dd className={upDown(m.net_pnl)}><strong>{signed(m.net_pnl)}</strong> · {rText(m.r_multiple)}</dd>
          </dl>
        </div>
        <div>
          <h3 className="sub">While open</h3>
          {ex.source ? (
            <dl className="kv">
              <dt>Best (MFE)</dt><dd>{fmt(ex.high)} · +{fmt(ex.mfe_pct)}% · +{fmt(ex.mfe_r)}R</dd>
              <dt>Worst (MAE)</dt><dd>{fmt(ex.low)} · −{fmt(ex.mae_pct)}% · −{fmt(ex.mae_r)}R</dd>
              <dt>From</dt><dd>{ex.source} data</dd>
            </dl>
          ) : <p className="muted">No candles for the holding period.</p>}
          {Object.keys(ind).length > 0 && (
            <>
              <h3 className="sub">Signal day</h3>
              <ul className="chips">{Object.entries(ind).filter(([, v]) => typeof v !== 'object').slice(0, 12)
                .map(([k, v]) => <li key={k}>{k.replace(/_/g, ' ')}: {typeof v === 'number' ? fmt(v) : String(v)}</li>)}</ul>
            </>
          )}
        </div>
      </div>
      {(m.stop_history.length > 0 || m.overrides.length > 0) && (
        <div className="detail">
          {m.stop_history.length > 0 && <div>
            <h3 className="sub">Stop history</h3>
            <ul className="timeline">{m.stop_history.map((h, i) => <li key={i}>{fmtTime(h.at)} · stop {fmt(h.triggers[0])}{h.triggers.length > 1 ? ` · target ${fmt(h.triggers[1])}` : ''}{h.by === 'user' ? ' · by hand' : ''}</li>)}</ul>
          </div>}
          {m.overrides.length > 0 && <div>
            <h3 className="sub">Changed by hand</h3>
            <ul className="timeline">{m.overrides.map((o, i) => <li key={i}>{fmtTime(o.detected_at)} · {o.change_type.replace(/_/g, ' ')}{o.before != null ? `: ${o.before} → ${o.after}` : ''}</li>)}</ul>
          </div>}
        </div>
      )}
      <CandleChart key={e.id} symbol={m.symbol} levels={levels} markers={markers} height={300} focus={{ from: m.signal_date || m.entry.at.slice(0, 10), to: e.closed_on }} />
    </Card>
  )
}

// ---------- weekly reviews ----------

function Weekly({ tick }) {
  const [data, setData] = useState(null)
  const [open, setOpen] = useState(null)
  const [msg, setMsg] = useState(null)
  useEffect(() => { api.get('/api/journal/weekly').then(setData).catch((e) => setMsg({ kind: 'error', text: e.message })) }, [tick])
  const run = async () => {
    try { const r = await api.confirm('/api/journal/weekly/run'); setMsg({ kind: 'good', text: `Building the review of the week from ${fmtDate(r.started)}…` }) }
    catch (e) { setMsg({ kind: 'error', text: e.message }) }
  }
  if (!data) return <p className="muted">Loading…</p>
  const cur = data.reviews.find((r) => r.id === open) || data.reviews[0]
  return (
    <>
      {msg && <Notice kind={msg.kind} onClose={() => setMsg(null)}>{msg.text}</Notice>}
      <div className="title-row bt-subhead">
        <p className="lead" style={{ margin: 0 }}>Written every Saturday at 10:00 for the week just ended, per portfolio and combined.</p>
        <button className="btn btn-sm" onClick={run} style={{ marginLeft: 'auto' }}>Review this week so far</button>
      </div>
      {data.reviews.length === 0 ? <Card><p className="muted">No weekly reviews yet. The first one is written on Saturday.</p></Card> : (
        <div className="weekly-layout">
          <nav className="card weekly-list" aria-label="Weeks">
            <ul>{data.reviews.map((r) => (
              <li key={r.id}><button className={cur.id === r.id ? 'on' : ''} onClick={() => setOpen(r.id)}>
                <strong>{fmtDate(r.week_start)}</strong>
                <span className="fine">{r.metrics.combined.closed} closed · {signed(r.metrics.combined.net_pnl)}</span>
              </button></li>))}</ul>
          </nav>
          <WeekDetail r={cur} />
        </div>
      )}
    </>
  )
}

function WeekDetail({ r }) {
  const m = r.metrics
  const c = m.combined
  const accts = Object.entries(m.accounts)
  return (
    <Card title={`Week of ${fmtDate(m.week_start)} – ${fmtDate(m.week_end)}`} action={<Source e={r} />}>
      <div className="journal-story">{r.narrative.split(/\n+/).map((p, i) => <p key={i}>{p}</p>)}</div>
      <div className="table-wrap">
        <table>
          <thead><tr><th>Portfolio</th><th className="num">Week return</th><th className="num">Max DD</th><th className="num">Closed</th>
            <th className="num">Won</th><th className="num">Expectancy</th><th className="num">Net P&amp;L</th><th className="num">To date</th></tr></thead>
          <tbody>
            {accts.map(([id, a]) => (
              <tr key={id}>
                <td><strong>{id}</strong>{a.strategy_version ? <> <span className="tag tag-agent">{a.strategy_version}</span></> : null}</td>
                <td className={`num ${upDown(a.week_return_pct)}`}>{pct(a.week_return_pct)}</td>
                <td className="num">{fmt(a.max_drawdown_pct, 1)}%</td>
                <td className="num">{a.week.closed}</td>
                <td className="num">{a.week.win_pct != null ? `${fmt(a.week.win_pct, 0)}%` : '—'}</td>
                <td className={`num ${upDown(a.week.expectancy_r)}`}>{rText(a.week.expectancy_r)}</td>
                <td className={`num ${upDown(a.week.net_pnl)}`}>{signed(a.week.net_pnl)}</td>
                <td className="num fine-cell">{a.to_date.closed} · {rText(a.to_date.expectancy_r)}</td>
              </tr>
            ))}
            <tr className="total-row">
              <td><strong>Combined</strong></td><td /><td /><td className="num">{c.closed}</td>
              <td className="num">{c.win_pct != null ? `${fmt(c.win_pct, 0)}%` : '—'}</td>
              <td className={`num ${upDown(c.expectancy_r)}`}>{rText(c.expectancy_r)}</td>
              <td className={`num ${upDown(c.net_pnl)}`}><strong>{signed(c.net_pnl)}</strong></td><td />
            </tr>
          </tbody>
        </table>
      </div>
      <div className="detail">
        <div>
          <h3 className="sub">By RR band</h3>
          <Breakdown g={c.by_band} />
        </div>
        <div>
          <h3 className="sub">By sizing case</h3>
          <Breakdown g={c.by_sizing} />
        </div>
      </div>
      <h3 className="sub">Skipped or outranked signals</h3>
      {m.skipped.length === 0 ? <p className="muted">None this week.</p> : (
        <div className="table-wrap"><table>
          <thead><tr><th>Signal</th><th>Symbol</th><th>Band</th><th>Why skipped</th><th>Would have</th><th className="num">R</th></tr></thead>
          <tbody>{m.skipped.map((s, i) => (
            <tr key={i}><td>{fmtDate(s.date)}</td><td><strong>{s.symbol}</strong></td><td>{s.rr_band}</td>
              <td className="fine-cell">{Object.entries(s.reasons).map(([a, why]) => `${a}: ${why}`).join(' · ')}</td>
              <td>{OUTCOME[s.would_have.outcome] || s.would_have.outcome}</td>
              <td className={`num ${upDown(s.would_have.r)}`}>{rText(s.would_have.r)}</td></tr>))}</tbody>
        </table></div>
      )}
      <h3 className="sub">Changed by hand</h3>
      {m.overrides.length === 0 ? <p className="muted">No manual changes this week.</p> : (
        <div className="table-wrap"><table>
          <thead><tr><th>Portfolio</th><th>Symbol</th><th>Change</th><th>Actual</th><th>Original plan would have</th></tr></thead>
          <tbody>{m.overrides.map((o, i) => (
            <tr key={i}><td>{o.account_id}</td><td><strong>{o.symbol}</strong></td>
              <td>{o.change.replace(/_/g, ' ')}{o.before != null && typeof o.before !== 'object' ? `: ${o.before} → ${o.after}` : ''}</td>
              <td>{o.actual.state === 'CLOSED' ? `${signed(o.actual.net_pnl)} · ${rText(o.actual.r)}` : 'still open'}</td>
              <td>{o.system_plan ? `${OUTCOME[o.system_plan.outcome] || o.system_plan.outcome}${o.system_plan.r != null ? ` · ${rText(o.system_plan.r)}` : ''}` : '—'}</td></tr>))}</tbody>
        </table></div>
      )}
      <p className="fine">{m.notes}</p>
    </Card>
  )
}

const OUTCOME = { target: 'Reached the target', stop: 'Hit the stop', open: 'Still open', not_filled: 'Not filled', no_data: 'No candles yet' }

function Breakdown({ g }) {
  const rows = Object.entries(g || {})
  if (!rows.length) return <p className="muted">No closed trades.</p>
  return (
    <table>
      <thead><tr><th /><th className="num">Closed</th><th className="num">Won</th><th className="num">Expectancy</th><th className="num">Net</th></tr></thead>
      <tbody>{rows.map(([k, s]) => (
        <tr key={k}><td>{k === 'None' ? '—' : k}</td><td className="num">{s.closed}</td><td className="num">{s.win_pct != null ? `${fmt(s.win_pct, 0)}%` : '—'}</td>
          <td className={`num ${upDown(s.expectancy_r)}`}>{rText(s.expectancy_r)}</td><td className={`num ${upDown(s.net_pnl)}`}>{signed(s.net_pnl)}</td></tr>))}</tbody>
    </table>
  )
}
