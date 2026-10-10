import { useCallback, useEffect, useRef, useState } from 'react'
import { api, fmtDate, fmtInr } from '../api.js'
import { Card, Notice, Pill, useConfirm } from '../components.jsx'

const signed = (n) => (n == null ? '—' : `${n < 0 ? '−' : n > 0 ? '+' : ''}${fmtInr(Math.abs(n))}`)

export default function Strategies({ tick }) {
  const [data, setData] = useState(null)
  const [msg, setMsg] = useState(null)
  const [dialog, setDialog] = useState(null)       // {mode: 'add' | 'edit', strategy?}
  const [confirm, confirmDialog] = useConfirm()
  const load = useCallback(() => { api.get('/api/strategies').then(setData).catch((e) => setMsg({ kind: 'error', text: e.message })) }, [])
  useEffect(load, [load, tick])

  const act = async (fn, done) => {
    try { const r = await fn(); setMsg({ kind: 'good', text: typeof done === 'function' ? done(r) : done }); load(); return r } catch (e) { setMsg({ kind: 'error', text: e.message }); return null }
  }

  const toggle = async (s) => {
    const on = !s.enabled
    if (!on && !(await confirm({ title: `Turn ${s.name} off?`, confirmLabel: 'Turn off',
      body: `It stops placing new buys${s.pending_buys ? ` and its ${s.pending_buys} pending buy GTT${s.pending_buys > 1 ? 's are' : ' is'} cancelled` : ''}. ${s.open_trades ? `Its ${s.open_trades} open trade${s.open_trades > 1 ? 's keep' : ' keeps'} their stops and trail to exit under their own rules.` : 'It has no open trades.'}` }))) return
    act(() => api.post(`/api/strategies/${s.id}/toggle`, { confirm: true, enabled: on }),
      (r) => on ? `${s.name} is ON${s.portfolios.length ? ': scanning and placing its buys now.' : '. Tick a portfolio for it to trade.'}`
        : `${s.name} is OFF${r.cancelled_buys ? `; ${r.cancelled_buys} pending buy(s) cancelled` : ''}.`)
  }

  const tick_ = async (s, id, checked) => {
    const next = checked ? [...s.portfolios, id] : s.portfolios.filter((x) => x !== id)
    if (!checked && !(await confirm({ title: `Stop ${s.name} on ${id}?`, confirmLabel: 'Untick',
      body: `${s.name} stops placing buys in ${id}; its pending buys there are cancelled. Open trades in ${id} keep their stops and trail to exit.` }))) return
    act(() => api.post(`/api/strategies/${s.id}/portfolios`, { confirm: true, portfolios: next }),
      (r) => checked ? `${s.name} now trades ${id}${s.enabled ? ': scanning and placing its buys now.' : ' (turn it ON to start).'}`
        : `${s.name} no longer trades ${id}${r.cancelled_buys ? `; ${r.cancelled_buys} pending buy(s) cancelled` : ''}.`)
  }

  const remove = async (s) => {
    if (!(await confirm({ title: `Delete ${s.name}?`, danger: true, confirmLabel: 'Delete strategy',
      body: `It stops trading everywhere${s.pending_buys ? ` and its ${s.pending_buys} pending buy(s) are cancelled` : ''}. ${s.open_trades ? `Its ${s.open_trades} open trade(s) keep their stops and trail to exit.` : ''} Its trades stay in the journal and history.` }))) return
    act(() => api.post(`/api/strategies/${s.id}/delete`, { confirm: true }), `${s.name} deleted.`)
  }

  if (!data) return <><h1>Strategies</h1><p className="muted">Loading…</p></>
  const ports = data.portfolios.filter((p) => !p.challenger)
  const on = data.strategies.filter((s) => s.enabled)

  return (
    <>
      <div className="title-row">
        <h1>Strategies</h1>
        <span className="fine">{on.length} of {data.strategies.length} ON</span>
        <button className="btn btn-sm btn-primary" style={{ marginLeft: 'auto' }} onClick={() => setDialog({ mode: 'add' })}>Add strategy</button>
      </div>
      <p className="lead">Each ON strategy is scanned on its own timeframe (daily ones at the EOD run, intraday ones every 5 minutes) and trades the portfolios ticked for it. When several share a portfolio,
        their signals are ranked together (RR band, then price, then liquidity) and a stock is bought once. Turning a strategy off
        cancels its pending buys; its open trades run to exit under their own rules.</p>
      {msg && <Notice kind={msg.kind} onClose={() => setMsg(null)}>{msg.text}</Notice>}

      <div className="strategy-grid">
        {data.strategies.map((s) => (
          <section key={s.id} className={`card strategy ${s.enabled ? '' : 'strategy-off'}`}>
            <header className="card-head">
              <div>
                <h2>{s.name}</h2>
                <div className="fine">{s.kind_label} · {s.version}{s.versions.length > 1 ? ` · ${s.versions.length} versions` : ''}</div>
              </div>
              <Switch on={!!s.enabled} label={`${s.name} ${s.enabled ? 'on' : 'off'}`} onClick={() => toggle(s)} />
            </header>
            <fieldset className="strategy-ports">
              <legend className="field-label">Runs on</legend>
              {ports.map((p) => (
                <label key={p.id} className="port-check">
                  <input type="checkbox" checked={s.portfolios.includes(p.id)} onChange={(e) => tick_(s, p.id, e.target.checked)} />
                  <span><strong>{p.id}</strong> <span className="fine">{fmtInr(p.capital)}</span></span>
                </label>
              ))}
            </fieldset>
            {s.enabled && !s.portfolios.length ? <p className="fine warn-line">ON but no portfolio ticked: it is not scanned or traded.</p> : null}
            <dl className="kv kv-strategy">
              <dt>Open trades</dt><dd>{s.open_trades}{s.pending_buys ? ` · ${s.pending_buys} buy pending` : ''}</dd>
              <dt>Closed trades</dt><dd>{s.closed_trades}{s.closed_trades ? ` · ${signed(s.net_pnl)} net` : ''}</dd>
              <dt>Since</dt><dd>{fmtDate(s.created_at.slice(0, 10))}</dd>
            </dl>
            <ul className="chips strategy-settings" aria-label="Settings">
              {((data.settings_by_kind && data.settings_by_kind[s.kind]) || data.settings).filter((f) => s.settings[f.key] !== undefined).map((f) => (
                <li key={f.key} title={f.label}>{chip(f.key, s.settings[f.key])}</li>
              ))}
            </ul>
            <div className="acct-actions">
              <button className="btn btn-sm" onClick={() => setDialog({ mode: 'edit', strategy: s })}>Edit…</button>
              <button className="btn btn-sm btn-danger-quiet" onClick={() => remove(s)}
                disabled={data.learning_active === s.id} title={data.learning_active === s.id ? 'A learning proposal is testing it' : undefined}>Delete…</button>
            </div>
          </section>
        ))}
      </div>
      {data.strategies.length === 0 && <Card><p className="muted">No strategies. Add one to start trading.</p></Card>}

      <Card title="Rule types">
        <table>
          <thead><tr><th>Rule type</th><th>Timeframe</th><th>What it does</th><th>Status</th></tr></thead>
          <tbody>{data.kinds.map((k) => (
            <tr key={k.kind}><td><strong>{k.label}</strong></td><td>{k.timeframe}</td><td className="rule-desc">{k.description}</td>
              <td>{k.built ? <Pill kind="good">Built</Pill> : <Pill kind="neutral">Not built</Pill>}</td></tr>
          ))}</tbody>
        </table>
        <p className="fine">A strategy is a rule type plus its settings. To add a different rule type (for example a mean-reversion entry), describe its
          entry, stop, target and exit rules in the chat; it is built and tested before it appears here.</p>
      </Card>

      {dialog && <StrategyDialog data={data} mode={dialog.mode} strategy={dialog.strategy} onClose={() => setDialog(null)}
        onSaved={(text) => { setDialog(null); setMsg({ kind: 'good', text }); load() }} />}
      {confirmDialog}
    </>
  )
}

const CHIP = {
  min_change_pct: (v) => `Up ≥ ${v}%`, adx_min: (v) => `ADX > ${v}`, swing_lookback: (v) => `Swing ${v} sessions`,
  entry_trigger_buffer_pct: (v) => `Trigger +${v}%`, entry_limit_buffer_pct: (v) => `Limit +${v}%`,
  stop_buffer_pct: (v) => `Stop −${v}%`, target_extension: (v) => `Target ${v}× range`, min_rr: (v) => `RR ≥ ${v}`,
  cancel_after_sessions: (v) => `Cancel after ${v}`, trail_buffer_pct: (v) => `Trail −${v}%`,
  up_min_pct: (v) => `Up ≥ ${v}% on day`, touch_pct: (v) => `Dip within ${v}%`, ema_period: (v) => `EMA ${v}`,
  top_liquid: (v) => `+ top ${v} liquid`,
}
const chip = (k, v) => (CHIP[k] ? CHIP[k](v) : `${k} ${v}`)

function Switch({ on, label, onClick }) {
  return (
    <button type="button" role="switch" aria-checked={on} aria-label={label} className={`switch ${on ? 'on' : ''}`} onClick={onClick}>
      <span className="switch-track" aria-hidden="true"><span className="switch-thumb" /></span>
      <span className="switch-text">{on ? 'ON' : 'OFF'}</span>
    </button>
  )
}

function StrategyDialog({ data, mode, strategy, onClose, onSaved }) {
  const adding = mode === 'add'
  const fieldsOf = (k) => (data.settings_by_kind && data.settings_by_kind[k]) || data.settings
  const sameKind = (k) => data.strategies.filter((s) => s.kind === k)
  const firstOf = (k) => { const l = sameKind(k); return l.find((s) => s.enabled) || l[0] || null }
  const defaultsOf = (k) => (data.defaults_by_kind && data.defaults_by_kind[k]) || {}
  const [kind, setKind] = useState(adding ? 'momentum_breakout' : strategy.kind)
  const [copyFrom, setCopyFrom] = useState(() => { const f = firstOf('momentum_breakout'); return f ? f.id : null })
  const src = adding ? data.strategies.find((s) => s.id === copyFrom && s.kind === kind) : strategy
  const [name, setName] = useState(adding ? '' : strategy.name)
  const [vals, setVals] = useState(() => ({ ...(src ? src.settings : {}) }))
  const [ports, setPorts] = useState([])
  const [enabled, setEnabled] = useState(true)
  const [err, setErr] = useState(null)
  const [busy, setBusy] = useState(false)
  const nameRef = useRef(null)
  useEffect(() => {
    nameRef.current && nameRef.current.focus()
    const onKey = (e) => e.key === 'Escape' && onClose()
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])
  const pickKind = (k) => {
    setKind(k)
    const f = firstOf(k)
    setCopyFrom(f ? f.id : null)
    setVals({ ...(f ? f.settings : defaultsOf(k)) })
  }
  const pickCopy = (id) => {
    setCopyFrom(id || null)
    const s = data.strategies.find((x) => x.id === id)
    setVals({ ...(s ? s.settings : defaultsOf(kind)) })
  }
  const fields = fieldsOf(kind)
  const base = src ? src.settings : defaultsOf(kind)
  const changed = fields.filter((f) => vals[f.key] !== undefined && vals[f.key] !== '' && Number(vals[f.key]) !== base[f.key])
  const kindInfo = data.kinds.find((k) => k.kind === kind)

  const save = async (e) => {
    e.preventDefault()
    setBusy(true); setErr(null)
    const settings = Object.fromEntries(fields.filter((f) => vals[f.key] !== '' && vals[f.key] !== undefined).map((f) => [f.key, Number(vals[f.key])]))
    try {
      if (adding) {
        const r = await api.post('/api/strategies', { confirm: true, name, kind, settings, portfolios: ports, enabled, copy_from: copyFrom })
        onSaved(`${r.name} added as ${r.version}${enabled && ports.length ? ': scanning and placing its buys now.' : enabled ? '. Tick a portfolio for it to trade.' : ' (OFF).'}`)
      } else {
        const r = await api.post(`/api/strategies/${strategy.id}/edit`, { confirm: true, name, settings })
        onSaved(r.changes.length ? `${r.name}: ${r.changes.join('; ')}.` : 'No changes.')
      }
    } catch (x) { setErr(x.message); setBusy(false) }
  }

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <form className="modal modal-wide" role="dialog" aria-modal="true" aria-labelledby="sd-title" onClick={(e) => e.stopPropagation()} onSubmit={save}>
        <h3 id="sd-title">{adding ? 'Add a strategy' : `Edit ${strategy.name}`}</h3>
        {err && <Notice kind="error">{err}</Notice>}
        <div className="sd-grid">
          <label className="form-field"><span>Name</span>
            <input ref={nameRef} className="search" required maxLength={60} value={name} placeholder="e.g. Tight stop" onChange={(e) => setName(e.target.value)} /></label>
          {adding ? (
            <label className="form-field"><span>Rule type</span>
              <select value={kind} onChange={(e) => pickKind(e.target.value)}>
                {data.kinds.map((k) => <option key={k.kind} value={k.kind} disabled={!k.built}>{k.label}{k.built ? '' : ' (not built)'}</option>)}
              </select></label>
          ) : <div className="form-field"><span>Rule type</span><div>{strategy.kind_label} · now {strategy.version}</div></div>}
          {adding && (
            <label className="form-field"><span>Start from the settings of</span>
              <select value={copyFrom ?? ''} onChange={(e) => pickCopy(Number(e.target.value))}>
                {sameKind(kind).map((s) => <option key={s.id} value={s.id}>{s.name} ({s.version})</option>)}
                {kind !== 'momentum_breakout' && <option value="">The rule type's defaults</option>}
              </select></label>
          )}
        </div>
        {kindInfo && <p className="fine">{kindInfo.description}</p>}
        <h4 className="sub">Settings {changed.length > 0 && <span className="fine">· {changed.length} changed from {src ? src.name : 'the defaults'}</span>}</h4>
        <div className="whatif-grid">
          {fields.map((f) => {
            const v = vals[f.key] ?? ''
            const diff = v !== '' && Number(v) !== base[f.key]
            return (
              <label key={f.key} className={`form-field ${diff ? 'whatif-changed' : ''}`}>
                <span>{f.label}</span>
                <input className="search" type="number" min={f.min} max={f.max} step={f.step} value={v}
                  onChange={(e) => setVals({ ...vals, [f.key]: e.target.value })} />
                {diff && <span className="fine">was {base[f.key]}</span>}
              </label>
            )
          })}
        </div>
        {adding ? (
          <>
            <fieldset className="strategy-ports">
              <legend className="field-label">Runs on</legend>
              {data.portfolios.filter((p) => !p.challenger).map((p) => (
                <label key={p.id} className="port-check">
                  <input type="checkbox" checked={ports.includes(p.id)} onChange={(e) => setPorts(e.target.checked ? [...ports, p.id] : ports.filter((x) => x !== p.id))} />
                  <span><strong>{p.id}</strong> <span className="fine">{fmtInr(p.capital)}</span></span>
                </label>
              ))}
            </fieldset>
            <label className="form-check"><input type="checkbox" checked={enabled} onChange={(e) => setEnabled(e.target.checked)} />
              <span>Turn it ON now <span className="fine">· it is scanned right away and places buys in the ticked portfolios</span></span></label>
          </>
        ) : (
          <p className="fine">Changing a setting creates a new version of this strategy (the old one is kept). New signals use the new
            version; open trades and pending buys keep the version they were placed with.</p>
        )}
        <div className="modal-actions">
          <button type="button" className="btn" onClick={onClose}>Cancel</button>
          <button type="submit" className="btn btn-primary" disabled={busy}>{adding ? 'Add strategy' : 'Save'}</button>
        </div>
      </form>
    </div>
  )
}
