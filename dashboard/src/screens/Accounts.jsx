import { useCallback, useEffect, useState } from 'react'
import { api, fmtDate, fmtInr, fmtTime, useOnPrices } from '../api.js'
import { Card, Notice, Pill, useConfirm, useForm } from '../components.jsx'
import EquityChart from '../EquityChart.jsx'

const pct = (n, d = 2) => (n == null ? '—' : `${n > 0 ? '+' : ''}${Number(n).toFixed(d)}%`)
const signed = (n) => (n == null ? '—' : `${n < 0 ? '−' : n > 0 ? '+' : ''}${fmtInr(Math.abs(n))}`)
const PALETTE_IDS = ['P30', 'P60', 'P100']   // validated account colours; other portfolios get their own chart

export default function Accounts({ tick }) {
  const [data, setData] = useState(null)
  const [msg, setMsg] = useState(null)
  const [confirm, dialog] = useConfirm()
  const [ask, formDialog] = useForm()
  const [strats, setStrats] = useState([])
  const load = useCallback(() => {
    api.get('/api/accounts').then(setData).catch((e) => setMsg({ kind: 'error', text: e.message }))
    api.get('/api/strategies').then((r) => setStrats(r.strategies)).catch(() => {})
  }, [])
  useEffect(load, [load, tick])
  useOnPrices(() => api.get('/api/accounts').then(setData))

  const rearm = async (a) => {
    const ok = await confirm({ title: `Re-arm ${a.id}?`, body: `Halted because: ${a.halt_reason}. New entries resume on the next EOD run.`, confirmLabel: 'Re-arm' })
    if (!ok) return
    try { await api.confirm(`/api/accounts/${a.id}/rearm`); setMsg({ kind: 'good', text: `${a.id} re-armed.` }); load() } catch (e) { setMsg({ kind: 'error', text: e.message }) }
  }

  const act = async (fn, done) => {
    try { const r = await fn(); setMsg({ kind: 'good', text: typeof done === 'function' ? done(r) : done }); load() } catch (e) { setMsg({ kind: 'error', text: e.message }) }
  }
  const add = async () => {
    const v = await ask({ title: 'Add a paper portfolio', confirmLabel: 'Add portfolio',
      body: 'A new paper portfolio trades the ticked strategies with the same risk rules, sized on its own capital. Change its strategies any time on the Strategies screen.',
      fields: [{ name: 'id', label: 'Short id (shown everywhere)', placeholder: 'e.g. P200', maxLength: 12 },
        { name: 'name', label: 'Name', placeholder: 'e.g. Two lakh', required: false, maxLength: 60 },
        { name: 'capital', label: 'Starting capital (₹)', type: 'number', min: 1000, step: 1000, value: 100000 },
        { name: 'manual_edits', label: 'Allow paper hand edits', type: 'checkbox', value: false, hint: 'move stops, sell by hand, buy outside the system (as on P60)' },
        ...strats.map((st) => ({ name: `s_${st.id}`, label: `Trades ${st.name}`, type: 'checkbox', value: !!st.enabled, hint: st.enabled ? st.version : `${st.version} · OFF` }))] })
    if (v) act(() => api.post('/api/accounts', { confirm: true, id: v.id, name: v.name, capital: Number(v.capital), manual_edits: v.manual_edits,
      strategy_ids: strats.filter((st) => v[`s_${st.id}`]).map((st) => st.id) }),
      (r) => `${r.id} added with ${fmtInr(r.capital)}.`)
  }
  const edit = async (a) => {
    const v = await ask({ title: `Edit ${a.id}`, confirmLabel: 'Save',
      body: a.flat ? 'Changing the capital starts the portfolio again at the new amount: cash, capital and peak are set to it, and the return, drawdown and equity curve restart. Past trades stay in the history.'
        : `${a.id} holds positions or pending buys, so its capital can't change now. Use Reset to close everything and start again at a new capital.`,
      fields: [{ name: 'name', label: 'Name', value: a.name, maxLength: 60 },
        { name: 'capital', label: 'Capital (₹)', type: 'number', min: 1000, step: 1000, value: a.capital, disabled: !a.flat,
          hint: a.flat ? `Now ${fmtInr(a.capital)}; equity ${fmtInr(a.equity)}` : 'Only when the portfolio holds nothing' },
        { name: 'manual_edits', label: 'Allow paper hand edits', type: 'checkbox', value: !!a.manual_edits }] })
    if (!v) return
    const body = { confirm: true, name: v.name, manual_edits: v.manual_edits }
    if (a.flat && Number(v.capital) !== a.capital) {
      const ok = await confirm({ title: `Restart ${a.id} at ${fmtInr(Number(v.capital))}?`, confirmLabel: 'Change capital',
        body: `Cash becomes ${fmtInr(Number(v.capital))} (it is ${fmtInr(a.cash)} now). The return, drawdown and equity curve start again; past trades are kept.` })
      if (!ok) return
      body.capital = Number(v.capital)
    }
    act(() => api.post(`/api/accounts/${a.id}/edit`, body), (r) => (r.changes.length ? `${a.id}: ${r.changes.join('; ')}.` : 'No changes.'))
  }
  const reset = async (a) => {
    let h
    try { h = await api.get(`/api/accounts/${a.id}/holdings`) } catch (e) { setMsg({ kind: 'error', text: e.message }); return }
    const parts = [h.positions && `sells ${h.positions} open position${h.positions > 1 ? 's' : ''}`,
      h.pending_buys && `cancels ${h.pending_buys} pending buy${h.pending_buys > 1 ? 's' : ''}`,
      h.outside && `sells ${h.outside} stock${h.outside > 1 ? 's' : ''} held outside the system`].filter(Boolean)
    const v = await ask({ title: `Reset ${a.id}`, confirmLabel: 'Reset portfolio', danger: true,
      body: `${parts.length ? `This ${parts.join(', ')}, at ${h.market_open ? 'the live price' : "the latest price (the market is closed)"}. Then` : 'It holds nothing. Then'} the portfolio starts again at the capital below: cash, peak, return, drawdown and equity curve restart, and a drawdown halt is cleared. Past trades stay in the history, marked as before the reset.`,
      fields: [{ name: 'capital', label: 'Start again with (₹)', type: 'number', min: 1000, step: 1000, value: a.capital }] })
    if (v) act(() => api.post(`/api/accounts/${a.id}/reset`, { confirm: true, capital: Number(v.capital) }),
      (r) => {
        const n = (k, one, many) => `${k} ${k === 1 ? one : many}`
        return `${a.id} reset to ${fmtInr(r.account.capital)}: ${n(r.closed.closed, 'trade', 'trades')} closed, ${n(r.closed.cancelled_buys, 'buy', 'buys')} cancelled, ${n(r.closed.outside_sold, 'outside holding', 'outside holdings')} sold (cash before the restart ${fmtInr(r.cash_before_restart)}).`
      })
  }

  if (!data) return <><h1>Accounts</h1><p className="muted">Loading…</p></>
  const combined = data.accounts.filter((a) => PALETTE_IDS.includes(a.id))
  const others = data.accounts.filter((a) => !PALETTE_IDS.includes(a.id))
  const ks = data.kill_switch || {}
  const mon = data.monitor

  return (
    <>
      <div className="title-row">
        <h1>Accounts</h1>
        {ks.active ? <Pill kind="critical">Kill switch on</Pill> : null}
        <span className="fine" style={{ marginLeft: 'auto' }}>{mon ? `Order monitor ${fmtTime(mon.at)} · ${mon.symbols} symbols` : 'Order monitor has not run yet'}</span>
        <button className="btn btn-sm btn-primary" onClick={add}>Add portfolio</button>
      </div>
      {msg && <Notice kind={msg.kind} onClose={() => setMsg(null)}>{msg.text}</Notice>}

      <div className="acct-grid">
        {data.accounts.map((a) => <AccountCard key={a.id} a={a} strats={a.strategy_version ? [] : strats.filter((st) => st.portfolios.includes(a.id))}
          onRearm={() => rearm(a)} onEdit={() => edit(a)} onReset={() => reset(a)} />)}
      </div>

      <Card title="Equity curve · return on starting capital">
        {combined.length > 0 && <EquityChart accounts={combined} />}
        {others.length > 0 && (
          <div className="acct-extra-curves">
            {others.map((a) => (
              <div key={a.id}><p className="fine"><strong>{a.id}</strong> {a.name !== a.id ? `· ${a.name}` : ''}</p>
                <EquityChart accounts={[{ ...a, color: '--run-A' }]} height={200} emptyText="Starts with the next EOD run." /></div>
            ))}
          </div>
        )}
        <p className="fine">One point per EOD run: cash plus holdings at the day's close, net of all charges. After a reset or a
          capital change the curve starts again from 0%.{others.length > 0 ? ' Portfolios beyond P30, P60 and P100 get their own chart so every line stays easy to tell apart.' : ''}</p>
      </Card>
      {dialog}{formDialog}
    </>
  )
}

function AccountCard({ a, strats, onRearm, onEdit, onReset }) {
  const ddLimit = 6
  const winRate = a.closed_trades ? Math.round((a.wins / a.closed_trades) * 100) : null
  return (
    <section className="card acct">
      <header className="card-head">
        <div>
          <h2>{PALETTE_IDS.includes(a.id) && <span className={`swatch sw-${a.id}`} aria-hidden="true" />}{a.id}
            {a.manual_edits ? <span className="tag tag-manual" title="Paper hand edits allowed">Hand edits</span> : null}
            {a.strategy_version ? <a href="#/learning" className="tag tag-agent" title="Trades a challenger strategy version (Learning screen)">Challenger · {a.strategy_version}</a> : null}</h2>
          <div className="fine">{a.name}</div>
        </div>
        {a.halted ? <Pill kind="critical" title={a.halt_reason}>Halted</Pill> : <Pill kind="good">Active</Pill>}
      </header>
      <div className="acct-equity">
        <span className="tile-value">{fmtInr(a.equity)}</span>
        <span className={`delta ${a.return_pct > 0 ? 'up' : a.return_pct < 0 ? 'down' : ''}`}>{pct(a.return_pct)}</span>
      </div>
      <div className="fine">Capital {fmtInr(a.capital)}{a.reset_at ? ` since ${fmtDate(a.reset_at.slice(0, 10))}` : ''} · peak {fmtInr(a.peak)}
        {a.restarts ? ` · restarted ${a.restarts}×` : ''}</div>
      <dl className="kv">
        <dt>Cash</dt><dd>{fmtInr(a.cash)}</dd>
        <dt>Reserved for buy GTTs</dt><dd>{fmtInr(a.reserved)}</dd>
        <dt>Free cash</dt><dd><strong>{fmtInr(a.free_cash)}</strong></dd>
        <dt>Holdings</dt><dd>{fmtInr(a.holdings)}</dd>
        <dt>Positions</dt><dd>{a.open_positions} open{a.pending_buys ? `, ${a.pending_buys} buy pending` : ''} <span className="fine">of 5 slots</span></dd>
        <dt>Closed trades</dt><dd>{a.closed_trades}{winRate != null ? ` · ${winRate}% won · avg ${a.avg_r?.toFixed(2)}R` : ''}</dd>
        <dt>Realised P&amp;L (net)</dt><dd>{signed(a.realized_pnl)}</dd>
        <dt>Charges paid</dt><dd>{fmtInr(a.charges_paid)}</dd>
      </dl>
      {!a.strategy_version && (
        <div className="fine acct-strategies">Strategies: {strats.length
          ? strats.map((st, i) => <span key={st.id}>{i ? ', ' : ''}<a href="#/strategies">{st.name}</a>{st.enabled ? '' : ' (off)'}</span>)
          : <a href="#/strategies">none ticked</a>}</div>
      )}
      <div className="dd">
        <div className="dd-head"><span>Drawdown from peak</span><span><strong>{a.drawdown_pct.toFixed(1)}%</strong> <span className="fine">halts at {ddLimit}%</span></span></div>
        <div className="dd-track" role="meter" aria-valuemin={0} aria-valuemax={ddLimit} aria-valuenow={a.drawdown_pct} aria-label={`${a.id} drawdown`}>
          <div className={`dd-bar ${a.drawdown_pct >= 4 ? 'warn' : ''}`} style={{ width: `${Math.min(100, (a.drawdown_pct / ddLimit) * 100)}%` }} />
        </div>
      </div>
      <div className="acct-actions">
        <button className="btn btn-sm" onClick={onEdit}>Edit</button>
        <button className="btn btn-sm btn-danger-quiet" onClick={onReset}>Reset…</button>
      </div>
      {a.halted ? (
        <div className="actions">
          <span className="fine">{a.halt_reason}{a.halted_at ? ` · ${fmtDate(a.halted_at.slice(0, 10))}` : ''}</span>
          <button className="btn btn-sm" onClick={onRearm}>Re-arm</button>
        </div>
      ) : null}
    </section>
  )
}
