import { useCallback, useEffect, useMemo, useState } from 'react'
import { api, useOnPrices, fmtDate, fmtInr, fmtTime } from '../api.js'
import CandleChart from '../CandleChart.jsx'
import { Card, Notice, Pill, useConfirm, useForm } from '../components.jsx'

const fmt = (n, d = 2) => (n == null ? '—' : Number(n).toLocaleString('en-IN', { minimumFractionDigits: d, maximumFractionDigits: d }))
const signed = (n) => (n == null ? '—' : `${n < 0 ? '−' : n > 0 ? '+' : ''}${fmtInr(Math.abs(n))}`)

export const STATE = {
  BUY_PENDING: { kind: 'neutral', label: 'Buy pending' },
  FILLED: { kind: 'critical', label: 'Filled · no stop yet' },
  PROTECTED: { kind: 'good', label: 'Protected' },
  TRAILING: { kind: 'good', label: 'Trailing' },
  CLOSED: { kind: 'neutral', label: 'Closed' },
  CANCELLED: { kind: 'neutral', label: 'Cancelled' },
}
export const REASON = { stop: 'Stop', target: 'Target', expired: 'Not filled in 2 sessions', buy_cancelled: 'Buy unfilled at close', buy_rejected: 'Buy rejected', kill_switch: 'Kill switch', gap_exit: 'Gap below stop, sold at open', manual_cancel: 'Buy cancelled by you', manual_sell: 'Sold by hand', signal_dropped: 'Signal dropped on refresh', strategy_off: 'Strategy turned off', intraday_cutoff: 'Not triggered by 15:15', reset: 'Portfolio reset', left_nse500: 'Left the NSE500' }

function StatePill({ s, t }) {
  if (t && t.awaiting_user) return <Pill kind="critical">No stop · waiting for you</Pill>
  if (t && t.paused) return <Pill kind="warning">Paused</Pill>
  const st = STATE[s] || { kind: 'neutral', label: s }
  return <Pill kind={st.kind}>{st.label}</Pill>
}

export default function Positions({ tick }) {
  const [acct, setAcct] = useState('all')
  const [data, setData] = useState(null)
  const [err, setErr] = useState(null)
  const [selId, setSelId] = useState(null)
  const load = useCallback(() => {
    api.get(`/api/positions${acct === 'all' ? '' : `?account=${acct}`}`).then((d) => { setData(d); setErr(null) }).catch((e) => setErr(e.message))
  }, [acct])
  useEffect(load, [load, tick])
  useOnPrices(() => api.get(`/api/positions${acct === 'all' ? '' : `?account=${acct}`}`).then((d) => { setData(d); setErr(null) }))

  const open = (data && data.open) || []
  const closed = (data && data.closed) || []
  const outside = (data && data.unmanaged) || []
  const manualKey = (u) => `m:${u.account_id}:${u.symbol}`
  useEffect(() => {
    if (!data) return
    const keys = [...open.map((t) => t.id), ...outside.map(manualKey), ...closed.map((t) => t.id)]
    if (!keys.includes(selId)) setSelId(open[0] ? open[0].id : outside[0] ? manualKey(outside[0]) : null)
  }, [data])
  const selManual = typeof selId === 'string' ? outside.find((u) => manualKey(u) === selId) : null

  const holding = open.filter((t) => ['FILLED', 'PROTECTED', 'TRAILING'].includes(t.state))
  const [ask, dialog] = useForm()
  const [msg, setMsg] = useState(null)
  const manualAccts = ((data && data.manual_accounts) || []).filter((a) => acct === 'all' || a === acct)
  const buyOutside = async (account) => {
    const v = await ask({ title: `Buy outside the system · ${account}`, confirmLabel: 'Buy at market (paper)',
      body: 'Simulates a buy you place yourself in Kite, at the live offer price during market hours. The agent tags it Manual and leaves it unmanaged (no stop), but counts it toward cash and the sector cap.',
      fields: [{ name: 'symbol', label: 'NSE symbol', type: 'symbol' }, { name: 'qty', label: 'Quantity', type: 'number', min: 1, step: 1 }] })
    if (!v) return
    try {
      const r = await api.post(`/api/accounts/${account}/buy-outside`, { symbol: v.symbol, qty: Number(v.qty), confirm: true })
      setMsg({ kind: 'good', text: `Bought ${r.qty} ${v.symbol.toUpperCase()} @ ${fmt(r.price)} outside the system.` })
      setSelId(`m:${account}:${v.symbol.toUpperCase()}`); load()
    } catch (e) { setMsg({ kind: 'error', text: e.message }) }
  }
  const sum = (xs, k) => xs.reduce((s, x) => s + (x[k] || 0), 0)
  const unreal = sum(holding, 'unrealized') + sum(outside, 'unrealized')
  const netTrades = sum(holding, 'net_unrealized')
  const netOutside = sum(outside, 'net_unrealized')
  const net = netTrades + netOutside
  const chargesOpen = sum(holding, 'buy_charges_open') + sum(outside, 'buy_charges_open') + sum(holding, 'sell_charges_est') + sum(outside, 'sell_charges_est')
  const streamRow = open.find((t) => t.price_source === 'stream') || outside.find((u) => u.price_source === 'stream')
  const liveToday = open.some((t) => t.price_source === 'live' || t.price_source === 'stream') || outside.some((u) => u.price_source === 'live' || u.price_source === 'stream')

  return (
    <>
      <div className="title-row">
        <h1>Positions &amp; GTTs</h1>
        <div className="tabs" role="tablist" aria-label="Account">
          {['all', ...((data && data.accounts) || ['P30', 'P60', 'P100'])].map((a) => (
            <button key={a} role="tab" aria-selected={acct === a} className={acct === a ? 'on' : ''} onClick={() => setAcct(a)}>{a === 'all' ? 'All accounts' : a}</button>
          ))}
        </div>
      </div>
      {err && <Notice kind="error">{err}</Notice>}
      {!data ? <p className="muted">Loading…</p> : (
        <>
          <div className="tiles">
            <div className="tile"><div className="tile-label">Open positions</div><div className="tile-value">{holding.length}{outside.length ? <span className="tile-of"> + {outside.length} outside</span> : null}</div>
              <div className="tile-sub">{open.length - holding.length} buy GTT{open.length - holding.length === 1 ? '' : 's'} pending</div></div>
            <div className="tile"><div className="tile-label">Net unrealised P&amp;L</div><div className={`tile-value ${net > 0 ? 'up' : net < 0 ? 'down' : ''}`}>{signed(net)}</div>
              <div className="tile-sub">Gross {signed(unreal)} − ₹{fmt(chargesOpen)} charges (buy paid + sell now){outside.length ? ` · ${signed(netTrades)} agent, ${signed(netOutside)} manual` : ''}</div></div>
            <div className="tile"><div className="tile-label">Prices</div><div className="tile-value" style={{ fontSize: 16 }}>{streamRow ? 'Streaming' : liveToday ? fmtTime(data.last_quotes_at) : 'Last close'}</div>
              <div className="tile-sub">{streamRow ? `Kite live prices · updated ${fmtTime(streamRow.price_as_of)}` : liveToday ? 'Live quotes from the order monitor' : 'Live quotes arrive during market hours'}</div></div>
          </div>

          <Card title="Open trades" action={manualAccts.map((a) => <button key={a} className="btn btn-sm" onClick={() => buyOutside(a)}>Buy outside the system · {a}</button>)}>
            {msg && <Notice kind={msg.kind} onClose={() => setMsg(null)}>{msg.text}</Notice>}
            {open.length + outside.length === 0 ? <p className="muted">No open trades. Buy GTTs are placed by the EOD run for tradeable signals.</p> : (
              <div className="table-wrap">
                <table className="clickable">
                  <thead><tr>
                    {acct === 'all' && <th>Account</th>}<th>Stock</th><th>Tag</th><th>State</th><th className="num">Qty</th><th className="num">Entry</th>
                    <th className="num">Price</th><th className="num">P&amp;L</th><th className="num" title="After Zerodha charges: buy charges paid and the charges to sell these shares now">Net P&amp;L</th><th className="num">R</th><th className="num">Stop</th><th className="num">Target</th><th>Exit order</th>
                  </tr></thead>
                  <tbody>
                    {open.map((t) => (
                      <tr key={t.id} className={selId === t.id ? 'selected' : ''} onClick={() => setSelId(t.id)} tabIndex={0}
                        onKeyDown={(e) => (e.key === 'Enter' || e.key === ' ') && setSelId(t.id)} aria-selected={selId === t.id}>
                        {acct === 'all' && <td>{t.account_id}</td>}
                        <td><strong>{t.symbol}</strong> <span className="fine">{t.sizing_case === 'trail_only' ? 'trail-only' : t.sizing_case === 'full' ? 'intraday' : ''}</span></td>
                        <td><span className="tag tag-agent">Agent</span></td>
                        <td><StatePill s={t.state} t={t} /></td>
                        <td className="num">{t.state === 'BUY_PENDING' ? t.qty : t.open_qty !== t.qty ? `${t.open_qty} / ${t.qty}` : t.qty}
                          {t.bought_qty != null && t.bought_qty < t.qty && t.state !== 'BUY_PENDING' ? <div className="fine">{t.bought_qty} of {t.qty} bought</div> : null}</td>
                        <td className="num">{t.entry_price != null ? fmt(t.entry_price) : <span className="fine">≥ {fmt(t.entry_trigger)}</span>}</td>
                        <td className="num">{fmt(t.price)}</td>
                        <td className={`num ${t.unrealized > 0 ? 'up' : t.unrealized < 0 ? 'down' : ''}`}>{t.unrealized != null ? signed(t.unrealized) : '—'}</td>
                        <NetCell x={t} />
                        <td className="num">{t.r_now != null ? fmt(t.r_now) : '—'}</td>
                        <td className="num">{fmt(t.stop)}</td>
                        <td className="num">{t.state === 'TRAILING' ? <span className="fine">hit</span> : fmt(t.target)}</td>
                        <td className="fine-cell">{gttSummary(t)}</td>
                      </tr>
                    ))}
                    {outside.map((u) => {
                      const k = manualKey(u)
                      return (
                        <tr key={k} className={selId === k ? 'selected' : ''} onClick={() => setSelId(k)} tabIndex={0}
                          onKeyDown={(e) => (e.key === 'Enter' || e.key === ' ') && setSelId(k)} aria-selected={selId === k}>
                          {acct === 'all' && <td>{u.account_id}</td>}
                          <td><strong>{u.symbol}</strong></td>
                          <td><span className="tag tag-manual">Manual</span></td>
                          <td><Pill kind="neutral">Held · unmanaged</Pill></td>
                          <td className="num">{u.qty}</td>
                          <td className="num">{fmt(u.avg_price)}</td>
                          <td className="num">{fmt(u.price)}{u.price_source === 'close' ? <div className="fine">close {fmtDate(u.price_as_of)}</div> : null}</td>
                          <td className={`num ${u.unrealized > 0 ? 'up' : u.unrealized < 0 ? 'down' : ''}`}>{signed(u.unrealized)}</td>
                          <NetCell x={u} />
                          <td className="num">—</td><td className="num">—</td><td className="num">—</td>
                          <td className="fine-cell">No stop (not managed by the agent)</td>
                        </tr>
                      )
                    })}
                  </tbody>
                </table>
              </div>
            )}
            <p className="fine"><strong>Agent</strong>: placed and managed by the agent. <strong>Manual</strong>: bought by you outside the system; the agent does not place stops on it but counts it toward cash and the sector cap. Select a row for its details and live chart. On {(data.manual_accounts || []).join(', ') || 'the manual-edit account'} you can also change it by hand, as you would in Kite; the agent detects and adopts the change.</p>
          </Card>

          {selManual ? <ManualDetail u={selManual} canTrade={(data.manual_accounts || []).includes(selManual.account_id)} onChanged={load} />
            : selId != null && typeof selId === 'number' ? <TradeDetail id={selId} tick={tick} onChanged={load} /> : null}
          {dialog}

          <Card title="Closed and cancelled">
            {closed.length === 0 ? <p className="muted">None yet.</p> : (
              <div className="table-wrap">
                <table className="clickable">
                  <thead><tr>
                    {acct === 'all' && <th>Account</th>}<th>Stock</th><th>Result</th><th className="num">Qty</th><th className="num">Entry</th>
                    <th className="num">Exit</th><th className="num">Net P&amp;L</th><th className="num">R</th><th className="num">Charges</th><th>Closed</th>
                  </tr></thead>
                  <tbody>
                    {closed.map((t) => {
                      const sold = t.exits.reduce((s, e) => s + e.qty, 0)
                      const avgExit = sold ? t.exits.reduce((s, e) => s + e.qty * e.price, 0) / sold : null
                      return (
                        <tr key={t.id} className={`${selId === t.id ? 'selected' : ''} ${t.state === 'CANCELLED' ? 'dim' : ''}`} onClick={() => setSelId(t.id)} tabIndex={0}
                          onKeyDown={(e) => e.key === 'Enter' && setSelId(t.id)}>
                          {acct === 'all' && <td>{t.account_id}</td>}
                          <td><strong>{t.symbol}</strong>{t.before_reset ? <div className="fine">before reset</div> : null}</td>
                          <td>{t.state === 'CLOSED' ? <Pill kind={t.net_pnl >= 0 ? 'good' : 'critical'}>{t.net_pnl >= 0 ? 'Win' : 'Loss'} · {t.exits.some((e) => e.reason === 'target') && t.exit_reason === 'stop' ? 'Target, then trailed stop' : REASON[t.exit_reason] || t.exit_reason}</Pill> : <span className="fine">{REASON[t.exit_reason] || t.exit_reason}</span>}</td>
                          <td className="num">{t.qty}</td>
                          <td className="num">{fmt(t.entry_price)}</td>
                          <td className="num">{fmt(avgExit)}</td>
                          <td className={`num ${t.net_pnl > 0 ? 'up' : t.net_pnl < 0 ? 'down' : ''}`}>{t.net_pnl != null ? signed(t.net_pnl) : '—'}</td>
                          <td className="num">{t.r_multiple != null ? fmt(t.r_multiple) : '—'}</td>
                          <td className="num">{t.state === 'CLOSED' ? fmtInr(t.charges) : '—'}</td>
                          <td>{t.closed_at ? fmtDate(t.closed_at.slice(0, 10)) : '—'}</td>
                        </tr>
                      )
                    })}
                  </tbody>
                </table>
              </div>
            )}
          </Card>
        </>
      )}
    </>
  )
}

function NetCell({ x }) {
  if (x.net_unrealized == null) return <td className="num">—</td>
  const n = x.net_unrealized
  return <td className={`num ${n > 0 ? 'up' : n < 0 ? 'down' : ''}`} title={`Buy charges ₹${fmt(x.buy_charges_open)} + sell now ₹${fmt(x.sell_charges_est)}`}>{signed(n)}</td>
}

function gttSummary(t) {
  if (t.awaiting_user) return 'No exit: waiting for your confirmation'
  if (t.paused && t.state === 'FILLED') return 'Paused: no exit placed'
  const g = (t.gtts || []).find((x) => x.purpose === 'exit')
  const b = (t.gtts || []).find((x) => x.purpose === 'buy')
  const buying = b && b.order && b.order.status === 'open' && b.order.filled_qty > 0 ? `Buying: ${b.order.filled_qty} of ${b.order.qty} filled · ` : ''
  if (t.state !== 'BUY_PENDING' && buying) return buying + (g ? exitText(t, g) : 'exit attaching')
  return g || t.state === 'BUY_PENDING' ? exitText(t, g) : (t.state === 'FILLED' ? 'Attaching on the next cycle' : '—')
}

function exitText(t, g) {
  if (t.state === 'BUY_PENDING') {
    const b = (t.gtts || []).find((x) => x.purpose === 'buy')
    if (b && b.order && b.order.status === 'open') return `Triggered · limit ${fmt(b.order.price)} resting`
    return `Buy GTT ${fmt(t.entry_trigger)} → limit ${fmt(t.entry_limit)}`
  }
  if (!g) return t.state === 'FILLED' ? 'Attaching on the next cycle' : '—'
  if (g.order && g.order.status === 'open') return g.order.filled_qty > 0
    ? `Selling: ${g.order.filled_qty} of ${g.order.qty} filled, rest working @ ${fmt(g.order.price)}`
    : `Triggered · ${g.order.side} ${g.order.qty} @ ${fmt(g.order.price)} unfilled`
  if (t.exit_at_open) return `Below stop: selling all ${t.open_qty} at the next open`
  const legs = g.legs.orders
  return g.type === 'oco' ? `OCO: stop ${legs[0].qty} · target ${legs[1].qty}` : `Stop on ${legs[0].qty}, limit ${fmt(legs[0].price)}`
}

// A holding you bought yourself: no stop from the agent. Sell by hand, and a live chart with your average price.
function ManualDetail({ u, canTrade, onChanged }) {
  const [ask, dialog] = useForm()
  const [msg, setMsg] = useState(null)
  const levels = useMemo(() => [{ price: u.avg_price, title: 'Entry (avg)', kind: 'entry' }], [u.avg_price])
  const markers = useMemo(() => (u.detected_at ? [{ at: u.detected_at, text: `Buy ${u.qty}`, kind: 'buy' }] : []), [u.detected_at, u.qty])
  const sell = async () => {
    const v = await ask({ title: `Sell ${u.symbol} · ${u.account_id}`, confirmLabel: 'Sell at market (paper)', danger: true,
      body: 'Simulates a market sell you place yourself in Kite, at the live bid during market hours (else the last close).',
      fields: [{ name: 'qty', label: `Quantity (max ${u.qty})`, type: 'number', min: 1, max: u.qty, step: 1, value: u.qty }] })
    if (!v) return
    try {
      const r = await api.post(`/api/accounts/${u.account_id}/sell-outside`, { symbol: u.symbol, qty: Number(v.qty), confirm: true })
      setMsg({ kind: 'good', text: `Sold ${r.qty} ${u.symbol} @ ${fmt(r.price)}.` }); onChanged()
    } catch (e) { setMsg({ kind: 'error', text: e.message }) }
  }
  return (
    <Card title={`${u.account_id} · ${u.symbol} · Manual`} action={canTrade && <button className="btn btn-sm btn-danger-quiet" onClick={sell}>Sell</button>}>
      {msg && <Notice kind={msg.kind} onClose={() => setMsg(null)}>{msg.text}</Notice>}
      <div className="detail">
        <div>
          <h3 className="sub">Holding</h3>
          <dl className="kv">
            <dt>Bought</dt><dd>{u.qty} shares @ {fmt(u.avg_price)} · {fmtTime(u.detected_at)}</dd>
            <dt>Price</dt><dd>{fmt(u.price)} <span className="fine">{u.price_source === 'live' || u.price_source === 'stream' ? 'live' : u.price_as_of ? `close ${fmtDate(u.price_as_of)}` : ''}</span></dd>
            <dt>Unrealised</dt><dd>{signed(u.unrealized)} gross · <strong>{signed(u.net_unrealized)} net</strong> <span className="fine">after ₹{fmt(u.buy_charges_open)} buy + ₹{fmt(u.sell_charges_est)} sell charges</span></dd>
            <dt>Stop / target</dt><dd>None: the agent does not manage this holding</dd>
          </dl>
        </div>
      </div>
      <CandleChart symbol={u.symbol} levels={levels} markers={markers} live height={320} />
      {dialog}
    </Card>
  )
}

function EditBar({ t, onDone }) {
  const [confirm, cdialog] = useConfirm()
  const [ask, fdialog] = useForm()
  const [msg, setMsg] = useState(null)
  const open = ['BUY_PENDING', 'FILLED', 'PROTECTED', 'TRAILING'].includes(t.state)
  const exitG = (t.gtt_history || []).find((g) => g.purpose === 'exit' && g.status === 'active')
  const buyG = (t.gtt_history || []).find((g) => g.purpose === 'buy' && g.status === 'active')
  const run = async (fn, done) => { try { await fn(); setMsg({ kind: 'good', text: done }); onDone() } catch (e) { setMsg({ kind: 'error', text: e.message }) } }
  const edit = (body, done) => run(() => api.post(`/api/trades/${t.id}/edit`, { ...body, confirm: true }), done)
  const price = async (action, label, current) => {
    const v = await ask({ title: `${label} · ${t.symbol}`, body: 'Changes the paper GTT the way you would in Kite. The agent adopts it; trailing only tightens from there.',
      confirmLabel: label, fields: [{ name: 'value', label: 'New trigger price', type: 'number', step: '0.05', value: current }] })
    if (v) edit({ action, value: Number(v.value) }, `${label}: ${v.value}`)
  }
  const sell = async () => {
    const v = await ask({ title: `Sell by hand · ${t.symbol}`, body: 'Simulates a market sell you place yourself. The agent records it, shrinks or closes the trade and cancels leftover GTTs.',
      confirmLabel: 'Sell at market (paper)', danger: true, fields: [{ name: 'qty', label: `Quantity (max ${t.open_qty})`, type: 'number', min: 1, max: t.open_qty, step: 1, value: t.open_qty }] })
    if (v) edit({ action: 'sell', qty: Number(v.qty) }, `Sold ${v.qty} by hand`)
  }
  const cancel = async (action, what) => {
    if (await confirm({ title: `Cancel the ${what} GTT?`, danger: true, confirmLabel: `Cancel ${what} GTT`,
      body: action === 'cancel_exit' ? 'The position will have no stop. The agent alerts and waits for your confirmation before placing a new one.' : 'The trade is marked cancelled by you; the agent will not re-enter on this signal.' }))
      edit({ action }, `${what[0].toUpperCase() + what.slice(1)} GTT cancelled`)
  }
  const confirmExit = async () => {
    if (await confirm({ title: 'Place a new exit GTT?', confirmLabel: 'Yes, place it', body: 'The agent places the standard stop (or OCO) on the next monitor cycle.' }))
      run(() => api.confirm(`/api/trades/${t.id}/confirm-exit`), 'Confirmed: a new exit goes on at the next cycle.')
  }
  const resume = async () => {
    if (await confirm({ title: 'Resume management?', confirmLabel: 'Resume', body: 'Check the stop and target first (adjust them if the split/bonus changed the price scale). Trailing and re-placing start again.' }))
      run(() => api.confirm(`/api/trades/${t.id}/resume`), 'Management resumed.')
  }
  return (
    <>
      {msg && <Notice kind={msg.kind} onClose={() => setMsg(null)}>{msg.text}</Notice>}
      {t.awaiting_user && <Notice kind="error">You cancelled the exit GTT: {t.open_qty} shares have no stop. <button className="btn btn-sm btn-primary" onClick={confirmExit}>Place a new exit</button></Notice>}
      {t.paused && <Notice kind="warning">Management paused: {t.paused}. <button className="btn btn-sm" onClick={resume}>Resume</button></Notice>}
      {t.manual_edits && open && (
        <div className="edit-bar">
          <span className="fine">Paper edits ({t.account_id})</span>
          {exitG && <button className="btn btn-sm" onClick={() => price('move_stop', 'Move stop', t.stop)}>Move stop</button>}
          {exitG && exitG.type === 'oco' && <button className="btn btn-sm" onClick={() => price('move_target', 'Move target', t.target)}>Move target</button>}
          {exitG && <button className="btn btn-sm btn-danger-quiet" onClick={() => cancel('cancel_exit', 'exit')}>Cancel exit GTT</button>}
          {buyG && <button className="btn btn-sm btn-danger-quiet" onClick={() => cancel('cancel_buy', 'buy')}>Cancel buy GTT</button>}
          {t.open_qty > 0 && <button className="btn btn-sm btn-danger-quiet" onClick={sell}>Sell by hand</button>}
        </div>
      )}
      {cdialog}{fdialog}
    </>
  )
}

function overrideText(o) {
  const a = o.after || {}, b = o.before || {}
  switch (o.change_type) {
    case 'stop_moved': return `Stop moved ${fmt(o.before)} → ${fmt(o.after)}`
    case 'target_moved': return `Target moved ${fmt(o.before)} → ${fmt(o.after)}`
    case 'exit_cancelled': return `Exit GTT cancelled (stop was ${fmt(b.stop)})`
    case 'buy_cancelled': return 'Buy GTT cancelled: no re-entry on this signal'
    case 'buy_modified': return `Buy GTT changed: trigger ${fmt(a.trigger)}, limit ${fmt(a.limit)}, ${a.qty} shares`
    case 'manual_sell': return `Sold ${a.sold} by hand @ ${fmt(a.price)}`
    case 'exit_qty_changed': return `Exit quantities changed to ${(o.after || []).join(' / ')}`
    case 'exit_adopted': return `Exit GTT found at the broker adopted: ${Object.entries(a).map(([k, v]) => `${k} ${fmt(v)}`).join(', ')}`
    default: return o.change_type.replace(/_/g, ' ')
  }
}

// Price lines for a trade: before the buy, the GTT trigger and limit; once bought, the fill price, the
// current stop (and the initial stop if it has moved) and the target (dotted once reached).
function tradeLevels(t) {
  const out = []
  if (t.state === 'BUY_PENDING' || (t.entry_price == null && t.state === 'CANCELLED')) {
    out.push({ price: t.entry_trigger, title: 'Entry trigger', kind: 'entry' }, { price: t.entry_limit, title: 'Limit', kind: 'limit' })
  } else if (t.entry_price != null) out.push({ price: t.entry_price, title: 'Entry', kind: 'entry' })
  out.push({ price: t.stop, title: t.stop !== t.initial_stop && t.entry_price != null ? 'Stop (trailed)' : 'Stop', kind: 'stop' })
  if (t.initial_stop != null && t.initial_stop !== t.stop) out.push({ price: t.initial_stop, title: 'Initial stop', kind: 'initial_stop' })
  const hit = t.state === 'TRAILING' || t.target_hit_on
  out.push({ price: t.target, title: hit ? 'Target (hit)' : 'Target', kind: hit ? 'target_hit' : 'target' })
  return out
}

function tradeMarkers(t) {
  const out = []
  if (t.signal && t.signal.date) out.push({ at: t.signal.date, text: 'Signal', kind: 'signal' })
  if (t.entry_at && t.entry_price != null) out.push({ at: t.entry_at, text: `Buy ${t.bought_qty ?? t.qty}`, kind: 'buy' })
  for (const e of t.exits || []) if (e.at) out.push({ at: e.at, text: `Sell ${e.qty} (${REASON[e.reason] || e.reason})`, kind: 'sell' })
  return out
}

function TradeDetail({ id, tick, onChanged }) {
  const [t, setT] = useState(null)
  const [n, setN] = useState(0)
  useEffect(() => { api.get(`/api/trades/${id}`).then(setT).catch(() => setT(null)) }, [id, tick, n])
  useOnPrices(() => api.get(`/api/trades/${id}`).then(setT), { symbol: t ? t.symbol : null })
  const levels = useMemo(() => (t ? tradeLevels(t) : []), [t && JSON.stringify(tradeLevels(t))])
  const markers = useMemo(() => (t ? tradeMarkers(t) : []), [t && JSON.stringify(tradeMarkers(t))])
  if (!t) return null
  const live = ['BUY_PENDING', 'FILLED', 'PROTECTED', 'TRAILING'].includes(t.state)
  const exitG = t.gtt_history.filter((g) => g.purpose === 'exit')
  const stops = exitG.flatMap((g) => g.trigger_history.map((h) => ({ at: h.at, stop: h.triggers[0], type: g.type })))
  return (
    <Card title={`${t.account_id} · ${t.symbol} · ${STATE[t.state]?.label || t.state}`}>
      <EditBar t={t} onDone={() => { setN((x) => x + 1); onChanged && onChanged() }} />
      <div className="detail">
        <div>
          <h3 className="sub">Trade</h3>
          <dl className="kv">
            <dt>Signal day</dt><dd>{t.signal ? fmtDate(t.signal.date) : '—'} · band {t.rr_band} (RR {fmt(t.rr)})</dd>
            <dt>Sizing</dt><dd>{t.qty} shares · {t.sizing_case === 'split' ? 'split (OCO, half at target)' : t.sizing_case === 'full' ? 'whole position at stop or target (intraday pullback)' : 'trail-only (1–3 shares)'}</dd>
            <dt>Entry</dt><dd>{t.entry_price != null ? `${fmt(t.entry_price)} · ${fmtTime(t.entry_at)}` : `trigger ${fmt(t.entry_trigger)}, limit ${fmt(t.entry_limit)}`}</dd>
            <dt>Initial stop</dt><dd>{fmt(t.initial_stop)} <span className="fine">risk {fmtInr(t.entry_price ? (t.entry_price - t.initial_stop) * t.qty : null)}</span></dd>
            <dt>Current stop</dt><dd>{fmt(t.stop)}{t.locked_in != null && t.locked_in > 0 ? <span className="fine"> · locks in {fmtInr(t.locked_in)}</span> : null}</dd>
            <dt>Target</dt><dd>{fmt(t.target)}{t.target_hit_on ? <span className="fine"> · reached {fmtDate(t.target_hit_on)}</span> : null}</dd>
            <dt>Charges so far</dt><dd>{fmtInr(t.charges)}</dd>
            {t.net_unrealized != null && <><dt>Unrealised</dt><dd>{signed(t.unrealized)} gross · <strong>{signed(t.net_unrealized)} net</strong> <span className="fine">if sold now (₹{fmt(t.sell_charges_est)} sell charges)</span></dd></>}
            {t.net_pnl != null && <><dt>Net P&amp;L</dt><dd><strong>{signed(t.net_pnl)}</strong> · {fmt(t.r_multiple)}R</dd></>}
          </dl>
        </div>
        <div>
          <h3 className="sub">Stop history</h3>
          {stops.length === 0 ? <p className="fine">No exit GTT yet.</p> : (
            <ol className="timeline">
              {stops.map((s, i) => <li key={i}><span className="fine">{fmtTime(s.at)}</span><span>{fmt(s.stop)} {i === 0 ? <span className="fine">({s.type === 'oco' ? 'OCO placed' : 'stop placed'})</span> : null}</span></li>)}
            </ol>
          )}
        </div>
        {t.overrides && t.overrides.length > 0 && (
          <div>
            <h3 className="sub">Your changes (adopted)</h3>
            <ol className="timeline">
              {t.overrides.map((o) => <li key={o.id}><span className="fine">{fmtTime(o.detected_at)}</span> {overrideText(o)}</li>)}
            </ol>
          </div>
        )}
        <div>
          <h3 className="sub">Events</h3>
          <ol className="timeline">
            {t.events.map((e) => <li key={e.id}><span className="fine">{fmtTime(e.ts)}</span> {e.message}</li>)}
          </ol>
        </div>
      </div>
      <CandleChart symbol={t.symbol} levels={levels} markers={markers} live={live} height={320} />
    </Card>
  )
}
