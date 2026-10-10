import { useCallback, useEffect, useMemo, useState } from 'react'
import { api, fmtDate, fmtTime } from '../api.js'
import CandleChart from '../CandleChart.jsx'
import { Card, Notice, Pill, useConfirm } from '../components.jsx'
import { Gate } from './Watchlist.jsx'

const fmt = (n, d = 2) => (n == null ? '—' : Number(n).toLocaleString('en-IN', { minimumFractionDigits: d, maximumFractionDigits: d }))
const BAND_KIND = { A: 'good', B: 'neutral', C: 'neutral' }

export default function Signals({ tick }) {
  const [tab, setTab] = useState('today')
  const [confirm, dialog] = useConfirm()
  const [msg, setMsg] = useState(null)

  const act = async (opts, fn) => {
    if (!(await confirm(opts))) return
    try { await fn(); if (opts.done) setMsg({ kind: 'good', text: opts.done }) } catch (e) { setMsg({ kind: 'error', text: e.message }) }
  }

  return (
    <>
      <div className="title-row">
        <h1>Signals</h1>
        <div className="tabs" role="tablist">
          <button role="tab" aria-selected={tab === 'today'} className={tab === 'today' ? 'on' : ''} onClick={() => setTab('today')}>Latest session</button>
          <button role="tab" aria-selected={tab === 'history'} className={tab === 'history' ? 'on' : ''} onClick={() => setTab('history')}>2-year history</button>
        </div>
      </div>
      {msg && <Notice kind={msg.kind} onClose={() => setMsg(null)}>{msg.text}</Notice>}
      {tab === 'today' ? <Today tick={tick} act={act} /> : <History tick={tick} act={act} />}
      {dialog}
    </>
  )
}

function Today({ tick, act }) {
  const [data, setData] = useState(null)
  const [sel, setSel] = useState(null)
  const [version, setVersion] = useState('')
  const load = useCallback(() => { api.get(`/api/signals${version ? `?version=${encodeURIComponent(version)}` : ''}`).then(setData).catch(() => {}) }, [version])
  useEffect(load, [load, tick])
  const rows = (data && data.signals) || []
  useEffect(() => { if (rows.length && !rows.find((r) => r.id === (sel && sel.id))) setSel(rows[0]) }, [rows])

  if (!data) return <p className="muted">Loading…</p>
  const scan = data.last_scan
  const mf = data.market_filter
  const cal = data.results_calendar
  const tradeable = rows.filter((r) => r.eligible)

  const strats = data.strategies || []
  const cur = strats.find((x) => x.version === data.version)
  return (
    <>
      {strats.length > 1 && (
        <div className="toolbar strategy-pick">
          <label className="form-inline"><span>Strategy</span>
            <select value={data.version} onChange={(e) => { setVersion(e.target.value); setSel(null) }}>
              {strats.map((x) => <option key={x.version} value={x.version}>{x.name}{x.enabled ? '' : ' (off)'}</option>)}
            </select></label>
          {cur && <span className="fine">{cur.version} · {cur.enabled ? (cur.portfolios.length ? `trades ${cur.portfolios.join(', ')}` : 'ON, no portfolio ticked') : 'OFF: not scanned each evening'}</span>}
        </div>
      )}
      <div className="tiles">
        <div className="tile"><div className="tile-label">Signal day</div><div className="tile-value">{data.date ? fmtDate(data.date) : '—'}</div>
          <div className="tile-sub">{scan ? `Scanned ${scan.scanned} stocks · ${fmtTime(scan.at)}` : 'No scan yet'}</div></div>
        <div className="tile"><div className="tile-label">Tradeable</div><div className="tile-value">{tradeable.length} <span className="tile-of">of {rows.length} triggers</span></div>
          <div className="tile-sub">{placedText(tradeable)}</div></div>
        {mf.enabled === false ? (
          <div className="tile"><div className="tile-label">Market filter</div><div className="tile-status"><Pill kind="neutral">Off</Pill></div>
            <div className="tile-sub">{mf.close ? `Nifty ${fmt(mf.close, 1)} ${mf.close >= mf.sma ? '≥' : '<'} SMA ${fmt(mf.sma, 1)} · information only` : 'Not used for entries'}</div></div>
        ) : (
          <div className="tile"><div className="tile-label">Market filter</div><div className="tile-status"><Pill kind={mf.allow_entries ? 'good' : 'critical'}>{mf.allow_entries ? 'Entries allowed' : 'Entries blocked'}</Pill></div>
            <div className="tile-sub">{mf.reason || `Nifty ${fmt(mf.close, 1)} ≥ SMA ${fmt(mf.sma, 1)}`}</div></div>
        )}
        <div className="tile"><div className="tile-label">Results calendar</div><div className="tile-status"><Pill kind={cal.fresh ? 'good' : 'critical'}>{cal.fresh ? `Current for ${fmtDate(cal.session)}` : `Not fetched since the ${fmtDate(cal.session)} close`}</Pill></div>
          <div className="tile-sub">{cal.fresh ? `Fetched ${fmtTime(cal.last_ok_at)} · ${cal.count} meetings stored` : 'Entries blocked until it loads (fetched at login and in the EOD run)'}</div></div>
      </div>

      <Card title="Triggers" action={<button className="btn btn-sm" onClick={() => act({ title: 'Re-run the scan on cached data?', body: 'Recomputes indicators and re-checks every gate for the latest session (no new data is downloaded), then places buy GTTs for tradeable signals in rank order and cancels pending buys whose signal dropped out. During market hours a signal is placed only if the live price is still below its trigger.', confirmLabel: 'Re-run scan', done: 'Scan started.' }, () => api.confirm('/api/signals/scan'))}>Re-run scan</button>}>
        {rows.length === 0 ? (
          <p className="muted">{data.date ? 'No stock met all four Momentum v1 conditions on this day.' : 'No scan yet. The EOD run scans at 21:00, or press Re-run scan.'}</p>
        ) : <SignalTable rows={rows} sel={sel} onSelect={setSel} />}
      </Card>

      {sel && (
        <Card title={`${sel.symbol} · ${fmtDate(sel.date)}`}>
          <SignalDetail s={sel} />
          <CandleChart symbol={sel.symbol} signal={sel} />
        </Card>
      )}
    </>
  )
}

function SignalTable({ rows, sel, onSelect, showDate }) {
  return (
    <div className="table-wrap">
      <table className="clickable">
        <thead>
          <tr>
            {showDate && <th>Date</th>}
            <th>Stock</th><th>Sector</th><th className="num">Change %</th><th className="num">Close</th><th className="num">ADX</th>
            <th className="num">Entry</th><th className="num">Limit</th><th className="num">Stop</th><th className="num">Target</th>
            <th className="num">RR</th><th>Band</th><th>Status</th>{!showDate && <th>Buy GTT</th>}
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.id} className={sel && sel.id === r.id ? 'selected' : ''} onClick={() => onSelect(r)} tabIndex={0}
              onKeyDown={(e) => (e.key === 'Enter' || e.key === ' ') && onSelect(r)} aria-selected={sel && sel.id === r.id}>
              {showDate && <td>{fmtDate(r.date)}</td>}
              <td><strong>{r.symbol}</strong></td>
              <td className="fine-cell">{r.sector}</td>
              <td className="num">{fmt(r.indicators.change_pct)}</td>
              <td className="num">{fmt(r.indicators.close)}</td>
              <td className="num">{fmt(r.indicators.adx14, 1)}</td>
              <td className="num">{fmt(r.entry_trigger)}</td>
              <td className="num">{fmt(r.entry_limit)}</td>
              <td className="num">{fmt(r.stop)}</td>
              <td className="num">{fmt(r.target)}</td>
              <td className="num">{fmt(r.rr)}</td>
              <td>{r.rr_band ? <Pill kind={BAND_KIND[r.rr_band]}>{r.rr_band}</Pill> : <span className="fine">—</span>}</td>
              <td>{r.eligible ? <Pill kind="good">Tradeable</Pill> : <span className="fine" title={(r.block_reasons || []).join('\n')}>{(r.block_reasons || [])[0] || 'Blocked'}{(r.block_reasons || []).length > 1 ? ` +${r.block_reasons.length - 1}` : ''}</span>}</td>
              {!showDate && <td>{r.eligible ? <AccountMarks accounts={r.accounts} /> : null}</td>}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function placedText(rows) {
  const n = rows.reduce((s, r) => s + (r.accounts || []).filter((a) => a.taken).length, 0)
  if (!rows.length) return 'Buy GTTs are placed per account, in rank order, after every refresh'
  if (!rows.some((r) => (r.accounts || []).length)) return 'Not yet ranked: buy GTTs are placed after the next refresh'
  return `${n} buy GTT${n === 1 ? '' : 's'} placed across your portfolios`
}

function AccountMarks({ accounts = [] }) {
  if (!accounts.length) return <span className="fine">—</span>
  return (
    <span className="acct-marks">
      {accounts.map((a) => (
        <span key={a.account_id} className={`acct-mark ${a.taken ? 'on' : ''}`} title={a.taken ? `${a.account_id}: ${a.qty} shares, rank ${a.rank}` : `${a.account_id}: ${a.skip_reason}`}>
          {a.taken ? '✓ ' : ''}{a.account_id}
        </span>
      ))}
    </span>
  )
}

function SignalDetail({ s }) {
  const i = s.indicators
  const g = s.gates || {}
  return (
    <div className="detail">
      <div>
        <h3 className="sub">Conditions on the signal candle</h3>
        <ul className="checks">
          <li><Gate ok={i.change_pct >= 3} /> Close {fmt(i.close)} is {fmt(i.change_pct)}% above previous close {fmt(i.prev_close)} (needs ≥ 3%)</li>
          <li><Gate ok={i.close >= i.bb_upper} /> Close ≥ upper Bollinger Band {fmt(i.bb_upper)}</li>
          <li><Gate ok={i.atr14 > i.atr14_prev} /> ATR(14) {fmt(i.atr14, 3)} &gt; yesterday's {fmt(i.atr14_prev, 3)}</li>
          <li><Gate ok={i.adx14 > 20} /> ADX(14) {fmt(i.adx14, 1)} &gt; 20</li>
        </ul>
      </div>
      <div>
        <h3 className="sub">Gates</h3>
        <ul className="checks">
          {[['nse500', 'NSE500 member'], ['watchlist', 'On watchlist'], ['valuation', 'Valuation (P/E < profit growth, capped)'], ['liquidity', `Liquidity (₹${fmt(i.traded_value_cr, 1)} cr/day)`], ['results', 'No results soon'], ['market', 'Market filter']].filter(([k]) => k in g).map(([k, l]) => (
            <li key={k}><Gate ok={g[k]} /> {l}{g[k] === null ? ' — not available for historical signals' : ''}</li>
          ))}
        </ul>
        {s.block_reasons && s.block_reasons.length > 0 && <p className="fine">Blocked: {s.block_reasons.join(' · ')}</p>}
      </div>
      {s.eligible && (s.accounts || []).length > 0 && (
        <div>
          <h3 className="sub">Per account</h3>
          <ul className="checks">
            {s.accounts.map((a) => (
              <li key={a.account_id}><Gate ok={!!a.taken} /> <strong>{a.account_id}</strong> {a.taken ? `${a.qty} shares, rank ${a.rank}${a.trade_state ? ` · ${a.trade_state.toLowerCase().replace('_', ' ')}` : ''}` : a.skip_reason}</li>
            ))}
          </ul>
        </div>
      )}
      <div>
        <h3 className="sub">Levels</h3>
        <dl className="kv">
          <dt>Entry trigger</dt><dd>{fmt(s.entry_trigger)}</dd>
          <dt>Entry limit</dt><dd>{fmt(s.entry_limit)}</dd>
          <dt>Stop</dt><dd>{fmt(s.stop)}</dd>
          <dt>Swing low (20)</dt><dd>{fmt(s.swing_low)}</dd>
          <dt>Target (2.0 ext.)</dt><dd>{fmt(s.target)}</dd>
          <dt>RR</dt><dd>{fmt(s.rr)} {s.rr_band ? `(band ${s.rr_band})` : '(below 1.5)'}</dd>
        </dl>
      </div>
    </div>
  )
}

function History({ tick, act }) {
  const [h, setH] = useState(null)
  const [month, setMonth] = useState(null)
  const [list, setList] = useState(null)
  const [sel, setSel] = useState(null)
  const load = useCallback(() => { api.get('/api/history').then(setH).catch(() => {}) }, [])
  useEffect(load, [load, tick])
  useEffect(() => {
    if (!month) return
    setList(null)
    api.get(`/api/signals?month=${month}`).then((r) => { setList(r.signals); setSel(r.signals.find((x) => x.eligible) || r.signals[0] || null) })
  }, [month, tick])

  const s = h && h.summary
  const mfOn = !s || s.market_filter_enabled !== false
  const run = () => act({ title: 'Run the 2-year historical scan?', body: 'Recomputes indicators and replays Momentum v1 over the cached candles. Takes about half a minute.', confirmLabel: 'Run scan', done: 'Historical scan started.' }, () => api.confirm('/api/history/run'))

  return (
    <>
      <Card title="Signals per month" action={<button className="btn btn-sm btn-primary" disabled={h && h.running} onClick={run}>{h && h.running ? 'Running…' : s ? 'Re-run scan' : 'Run scan'}</button>}>
        {!s ? <p className="muted">{h && h.running ? 'Scanning…' : 'Not run yet. This is the Phase 1 check: how often v1 fires over two years.'}</p> : (
          <>
            <p className="fine">
              Candles {fmtDate(s.first_date)} – {fmtDate(s.last_date)}{s.indicators_from ? `; signals possible from ${fmtDate(s.indicators_from)} after the 100-session indicator warm-up` : ''} · {s.symbols} NSE500 stocks · run {fmtTime(s.at)}.
              {' '}<strong>{s.totals.tradeable}</strong> signals passed RR, liquidity{mfOn ? ', market filter' : ''}{s.results_history_from ? ' and the results blackout' : ''}
              {' '}out of {s.totals.triggers} technical triggers (~{Math.round(s.totals.tradeable / Math.max(1, s.months.length))} a month).
            </p>
            {!s.results_history_from && <Notice kind="warning">Results history isn't loaded, so the results blackout was not applied. Load it on the Watchlist screen and re-run.</Notice>}
            <MonthBars months={s.months} onPick={setMonth} picked={month} />
            <div className="table-wrap">
              <table className="clickable">
                <thead><tr><th>Month</th><th className="num">Triggers</th><th className="num">RR ≥ 1.5</th><th className="num">+ Liquid</th>{mfOn && <th className="num">+ Market filter</th>}<th className="num">Tradeable</th><th className="num">A</th><th className="num">B</th><th className="num">C</th><th className="num">On today's watchlist</th></tr></thead>
                <tbody>
                  {[...s.months].reverse().map((m) => (
                    <tr key={m.month} className={month === m.month ? 'selected' : ''} onClick={() => setMonth(m.month)} tabIndex={0} onKeyDown={(e) => e.key === 'Enter' && setMonth(m.month)}>
                      <td>{monthLabel(m.month)}</td><td className="num">{m.triggers}</td><td className="num">{m.rr_ok}</td><td className="num">{m.liquid}</td>
                      {mfOn && <td className="num">{m.market_ok}</td>}<td className="num"><strong>{m.tradeable}</strong></td><td className="num">{m.band_A}</td><td className="num">{m.band_B}</td><td className="num">{m.band_C}</td><td className="num">{m.on_current_watchlist}</td>
                    </tr>
                  ))}
                </tbody>
                <tfoot><tr><td>Total</td><td className="num">{s.totals.triggers}</td><td className="num">{s.totals.rr_ok}</td><td className="num">{s.totals.liquid}</td>{mfOn && <td className="num">{s.totals.market_ok}</td>}<td className="num"><strong>{s.totals.tradeable}</strong></td><td className="num">{s.totals.band_A}</td><td className="num">{s.totals.band_B}</td><td className="num">{s.totals.band_C}</td><td className="num">{s.totals.on_current_watchlist}</td></tr></tfoot>
              </table>
            </div>
            <p className="fine">Historical fundamentals aren't available, so the watchlist and valuation gates can't be replayed. "On today's watchlist" uses your current paste and is only indicative. The NSE500 list is today's, so stocks that left the index are missing.</p>
          </>
        )}
      </Card>

      {month && (
        <MonthList month={month} list={list} sel={sel} onSelect={setSel} />
      )}
      {sel && month && (
        <Card title={`${sel.symbol} · ${fmtDate(sel.date)}`}>
          <SignalDetail s={sel} />
          <CandleChart symbol={sel.symbol} signal={sel} />
        </Card>
      )}
    </>
  )
}

function MonthList({ month, list, sel, onSelect }) {
  const [onlyTradeable, setOnlyTradeable] = useState(true)
  const [limit, setLimit] = useState(40)
  useEffect(() => setLimit(40), [month, onlyTradeable])
  if (!list) return <Card title={monthLabel(month)}><p className="muted">Loading…</p></Card>
  const rows = onlyTradeable ? list.filter((r) => r.eligible) : list
  return (
    <Card title={`${monthLabel(month)} · ${rows.length} ${onlyTradeable ? 'tradeable' : 'triggers'}`}
      action={<label className="toggle"><input type="checkbox" checked={onlyTradeable} onChange={(e) => setOnlyTradeable(e.target.checked)} /> Tradeable only</label>}>
      {rows.length === 0 ? <p className="muted">None this month.</p> : <SignalTable rows={rows.slice(0, limit)} sel={sel} onSelect={onSelect} showDate />}
      {rows.length > limit && <div className="actions"><button className="btn btn-sm" onClick={() => setLimit(limit + 40)}>Show {Math.min(40, rows.length - limit)} more of {rows.length - limit}</button></div>}
    </Card>
  )
}

function monthLabel(m) {
  const [y, mo] = m.split('-')
  return new Date(+y, +mo - 1, 1).toLocaleDateString('en-IN', { month: 'short', year: 'numeric' })
}

// One series (tradeable signals per month), so no legend box: the card title names it.
function MonthBars({ months, onPick, picked }) {
  const [hover, setHover] = useState(null)
  const max = useMemo(() => Math.max(1, ...months.map((m) => m.tradeable)), [months])
  const W = 760, H = 180, pad = { l: 34, r: 8, t: 12, b: 26 }
  const bw = (W - pad.l - pad.r) / months.length
  const y = (v) => pad.t + (H - pad.t - pad.b) * (1 - v / max)
  const ticks = [0, Math.round(max / 2), max]
  return (
    <div className="bars-wrap">
      <svg viewBox={`0 0 ${W} ${H}`} className="bars" role="img" aria-label="Tradeable signals per month">
        {ticks.map((t) => (
          <g key={t}><line x1={pad.l} x2={W - pad.r} y1={y(t)} y2={y(t)} className="grid" /><text x={pad.l - 6} y={y(t) + 4} className="axis" textAnchor="end">{t}</text></g>
        ))}
        {months.map((m, i) => {
          const x = pad.l + i * bw + 1
          const h = Math.max(0, H - pad.b - y(m.tradeable))
          return (
            <g key={m.month} onMouseEnter={() => setHover(i)} onMouseLeave={() => setHover(null)} onClick={() => onPick(m.month)} style={{ cursor: 'pointer' }}>
              <rect x={pad.l + i * bw} y={pad.t} width={bw} height={H - pad.t - pad.b} fill="transparent" />
              <path className={`bar ${picked === m.month ? 'bar-on' : ''}`} d={roundedTop(x, y(m.tradeable), Math.max(1, bw - 2), h, Math.min(4, h))} />
              {(i % 3 === 0 || (i === months.length - 1 && i % 3 === 2)) && <text x={x + (bw - 2) / 2} y={H - 8} className="axis" textAnchor="middle">{monthLabel(m.month).replace(' ', " '").replace(/'20/, "'")}</text>}
            </g>
          )
        })}
      </svg>
      {hover !== null && (
        <div className="bar-tip" style={{ left: `${((pad.l + hover * bw + bw / 2) / W) * 100}%` }}>
          <strong>{monthLabel(months[hover].month)}</strong><br />
          {months[hover].tradeable} tradeable · {months[hover].triggers} triggers
        </div>
      )}
    </div>
  )
}

function roundedTop(x, y, w, h, r) {
  if (h <= 0) return ''
  return `M${x},${y + h} V${y + r} Q${x},${y} ${x + r},${y} H${x + w - r} Q${x + w},${y} ${x + w},${y + r} V${y + h} Z`
}
