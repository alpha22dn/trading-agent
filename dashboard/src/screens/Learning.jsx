import { useCallback, useEffect, useState } from 'react'
import { api, fmtDate, fmtInr, fmtTime } from '../api.js'
import { Card, Notice, Pill, Progress, useConfirm, useForm } from '../components.jsx'
import EquityChart from '../EquityChart.jsx'

const fmt = (n, d = 2) => (n == null ? '—' : Number(n).toLocaleString('en-IN', { minimumFractionDigits: d, maximumFractionDigits: d }))
const signed = (n) => (n == null ? '—' : `${n < 0 ? '−' : n > 0 ? '+' : ''}${fmtInr(Math.abs(n))}`)
const rText = (n) => (n == null ? '—' : `${n > 0 ? '+' : ''}${fmt(n)}R`)
const pct = (n) => (n == null ? '—' : `${n > 0 ? '+' : ''}${fmt(n)}%`)
const upDown = (n) => (n > 0 ? 'up' : n < 0 ? 'down' : '')

const STATUS = {
  analysing: ['neutral', 'Gathering evidence'], validating: ['neutral', 'Validating'], challenger: ['good', 'Challenger running'],
  decision: ['warning', 'Waiting for your decision'], approved: ['good', 'Approved'], rejected: ['neutral', 'Rejected'],
  failed: ['neutral', 'Failed validation'], no_proposal: ['neutral', 'Nothing to propose'], error: ['critical', 'Error'],
}
const PARAM = {
  min_rr: 'Minimum reward:risk', entry_trigger_buffer_pct: 'Entry trigger above close (%)', entry_limit_buffer_pct: 'Limit above trigger (%)',
  stop_buffer_pct: 'Stop below signal low (%)', target_extension: 'Target extension (× range)', trail_buffer_pct: 'Trail below prior low (%)',
  cancel_after_sessions: 'Cancel unfilled buy after (sessions)',
}
const RULE = { stop_too_tight: 'Stop too tight', target_too_far: 'Target too far', weak_band: 'Weak RR band', unfilled_buys: 'Buys not filling', trail_gives_back: 'Trail gives back' }

function StatusPill({ s }) {
  const [k, l] = STATUS[s] || ['neutral', s]
  return <Pill kind={k}>{l}</Pill>
}

export default function Learning({ tick }) {
  const [data, setData] = useState(null)
  const [open, setOpen] = useState(null)
  const [msg, setMsg] = useState(null)
  const [confirm, dialog] = useConfirm()
  const load = useCallback(() => { api.get('/api/learning').then(setData).catch((e) => setMsg({ kind: 'error', text: e.message })) }, [])
  useEffect(load, [load, tick])
  useEffect(() => {   // the analysis runs in its own process: poll while it works
    if (!data || !data.running) return undefined
    const t = setInterval(load, 4000)
    return () => clearInterval(t)
  }, [data && data.running, load])

  const act = async (fn, done) => {
    try { const r = await fn(); setMsg({ kind: 'good', text: typeof done === 'function' ? done(r) : done }); load() } catch (e) { setMsg({ kind: 'error', text: e.message }) }
  }
  const [pick, setPick] = useState('')
  const propose = async () => {
    const sid = pick ? Number(pick) : data.next_pick
    const st = data.strategies.find((x) => x.id === sid)
    const ok = await confirm({ title: `Look for a proposal for ${st ? st.name : 'the next strategy'} now?`, confirmLabel: 'Start',
      body: `The agent reads ${st ? `${st.name}'s` : 'the strategy\'s'} closed trades in the journal for one parameter change with evidence behind it, then tests it walk-forward over the last ${data.rules.validation_months} months of 1-minute data. It runs in the background and can take a while; nothing changes in your portfolios unless it passes, and then only a new ₹1,00,000 challenger portfolio trades it.` })
    if (ok) act(() => api.post('/api/learning/propose', { confirm: true, strategy_id: sid }), (r) => `Proposal ${r.id} started for ${st ? st.name : 'the strategy'}.`)
  }
  if (!data) return <><h1>Learning</h1><p className="muted">Loading…</p></>
  const active = data.proposals.find((p) => p.id === data.active)
  const shown = data.proposals.find((p) => p.id === open) || active

  return (
    <>
      <div className="title-row">
        <h1>Learning</h1>
        <span className="fine">{data.strategies.filter((x) => x.enabled).length} strategies ON</span>
        <div className="toolbar" style={{ marginLeft: 'auto' }}>
          <label className="form-inline"><span>For</span>
            <select value={pick} onChange={(e) => setPick(e.target.value)} disabled={!!active}>
              <option value="">Next in turn{data.next_pick ? ` (${(data.strategies.find((x) => x.id === data.next_pick) || {}).name})` : ''}</option>
              {data.strategies.map((x) => <option key={x.id} value={x.id}>{x.name}{x.enabled ? '' : ' (off)'}</option>)}
            </select></label>
          <button className="btn btn-sm btn-primary" onClick={propose} disabled={!!active || !data.strategies.length}
            title={active ? 'One proposal at a time' : undefined}>Look for a proposal now</button>
        </div>
      </div>
      <p className="lead">Every Saturday (or when you ask) the agent takes the next ON strategy in turn (the one with the most newly closed trades)
        and looks for <strong>one</strong> parameter change the journal supports, tests it
        walk-forward in {data.rules.folds} periods, and if it holds up runs it in a ₹1,00,000 challenger portfolio for {data.rules.challenger_sessions} sessions.
        You approve or reject it; nothing changes a strategy without you, and any earlier version can be restored.</p>
      {msg && <Notice kind={msg.kind} onClose={() => setMsg(null)}>{msg.text}</Notice>}

      {shown ? <Proposal p={shown} rules={data.rules} running={data.running} onDone={load} setMsg={setMsg} isActive={shown.id === data.active}
        onClose={open && open !== data.active ? () => setOpen(null) : null} />
        : <Card><p className="muted">No proposal is running. The next one starts with the weekly review on Saturday at 10:00.</p></Card>}

      <Card title="All proposals">
        {data.proposals.length === 0 ? <p className="muted">None yet.</p> : (
          <div className="table-wrap"><table className="clickable">
            <thead><tr><th>#</th><th>Started</th><th>Strategy</th><th>How</th><th>Change</th><th>Evidence</th><th>Status</th><th>Outcome</th></tr></thead>
            <tbody>{data.proposals.map((p) => (
              <tr key={p.id} className={shown && shown.id === p.id ? 'selected' : ''} tabIndex={0} onClick={() => setOpen(p.id)} onKeyDown={(e) => e.key === 'Enter' && setOpen(p.id)}>
                <td>{p.id}</td><td>{fmtDate(p.created_at.slice(0, 10))}</td><td>{p.strategy_name || p.base_version}</td><td>{p.trigger}</td>
                <td>{p.param ? <>{PARAM[p.param] || p.param}: <strong>{fmt(p.from_value)} → {fmt(p.to_value)}</strong></> : '—'}</td>
                <td>{p.evidence ? `${RULE[p.evidence.rule] || p.evidence.rule} (${p.evidence.evidence})` : '—'}</td>
                <td><StatusPill s={p.status} /></td>
                <td className="fine-cell">{p.message}</td>
              </tr>))}</tbody>
          </table></div>
        )}
      </Card>

      <Versions data={data} onDone={load} setMsg={setMsg} />
      <Integrations />
      {dialog}
    </>
  )
}

// ---------- one proposal ----------

function Proposal({ p, rules, running, onDone, setMsg, isActive, onClose }) {
  const [confirm, dialog] = useConfirm()
  const [ask, formDialog] = useForm()
  const e = p.evidence
  const v = p.validation
  const c = p.comparison
  const act = async (fn, done) => {
    try { await fn(); setMsg({ kind: 'good', text: done }); onDone() } catch (err) { setMsg({ kind: 'error', text: err.message }) }
  }
  const approve = async () => {
    const early = c && c.sessions < c.needed
    const ok = await confirm({ title: `Make ${p.version} ${p.strategy_name || 'the strategy'}'s current version?`, confirmLabel: 'Approve', danger: early,
      body: `${early ? `The challenger has run only ${c.sessions} of ${c.needed} sessions. ` : ''}From the next scan every portfolio running ${p.strategy_name || 'this strategy'} trades ${p.version} (${PARAM[p.param]} ${fmt(p.from_value)} → ${fmt(p.to_value)}); open trades keep the rules they were opened with. The challenger portfolio ${p.challenger_account} is closed at market and archived. ${p.base_version} can be restored with one click under Strategy versions.` })
    if (ok) act(() => api.confirm(`/api/learning/${p.id}/approve`), `${p.version} is now ${p.strategy_name || 'the strategy'}'s current version.`)
  }
  const reject = async () => {
    const r = await ask({ title: `Reject proposal ${p.id}?`, confirmLabel: 'Reject', danger: true,
      body: p.challenger_account ? `The challenger portfolio ${p.challenger_account} is closed at market and archived; its trades stay in the journal. The strategy is unchanged.` : 'The strategy is unchanged.',
      fields: [{ name: 'note', label: 'Note (optional)', required: false, maxLength: 200, placeholder: 'why, for the record' }] })
    if (r) act(() => api.post(`/api/learning/${p.id}/reject`, { confirm: true, note: r.note }), `Proposal ${p.id} rejected.`)
  }
  const working = ['analysing', 'validating'].includes(p.status)
  const decidable = ['challenger', 'decision'].includes(p.status)

  return (
    <Card title={<>Proposal {p.id} <span className="fine">· {p.strategy_name || p.base_version} · {p.trigger} · {fmtTime(p.created_at)}</span></>}
      action={<div className="toolbar"><StatusPill s={p.status} />{onClose && <button className="link" onClick={onClose} aria-label="Close">✕</button>}</div>}>
      <p className={working ? 'working' : ''}>{working && running ? <span className="spinner" aria-hidden="true" /> : null}{p.message}</p>
      {working && !running && isActive && <Notice kind="warning">The analysis process isn't running. It is marked as stopped at the next restart; you can then start a new one.</Notice>}
      {p.param && (
        <div className="proposal-change">
          <div className="fine">{PARAM[p.param] || p.param}</div>
          <div className="proposal-values"><span>{fmt(p.from_value)}</span><span aria-hidden="true">→</span><strong>{fmt(p.to_value)}</strong>
            {p.version && <span className="tag tag-agent">{p.version}</span>}</div>
          <div className="fine">Measured against {p.base_version}</div>
        </div>
      )}
      {e && (
        <div className="detail">
          <div>
            <h3 className="sub">Why: {RULE[e.rule] || e.rule} <span className={`tag ${e.evidence === 'strong' ? 'tag-agent' : 'tag-manual'}`}>{e.evidence} evidence</span></h3>
            <p>{p.explanation || e.text}</p>
            <p className="fine">From {e.source.kind === 'live' ? `${e.source.trades} closed live trades` :
              `${e.source.live_trades} closed live trades plus a baseline backtest of ${fmtDate(e.source.backtest_period[0])} – ${fmtDate(e.source.backtest_period[1])} (${e.source.backtest_trades} trades), because fewer than ${rules.min_live_trades} live trades have closed`}.</p>
          </div>
          <div>
            <h3 className="sub">Every rule checked</h3>
            <table>
              <thead><tr><th>Rule</th><th>Change</th><th className="num">Strength</th><th>Threshold</th></tr></thead>
              <tbody>{(e.all_rules || []).map((r) => (
                <tr key={r.rule} title={r.text}><td>{RULE[r.rule] || r.rule}</td><td className="fine-cell">{fmt(r.from)} → {fmt(r.to)}</td>
                  <td className="num">{fmt(r.strength)}</td><td>{r.met ? 'met' : '—'}</td></tr>))}</tbody>
            </table>
          </div>
        </div>
      )}
      {v && <Validation v={v} rules={rules} />}
      {c && <Comparison p={p} c={c} />}
      {decidable && (
        <div className="actions proposal-actions">
          <span className="fine">{p.status === 'decision' ? 'The challenger has run long enough: your call.' : `Decision after ${c ? c.needed : rules.challenger_sessions} sessions; you can also decide now.`}</span>
          <button className="btn btn-sm btn-danger-quiet" onClick={reject}>Reject…</button>
          <button className="btn btn-sm btn-primary" onClick={approve}>Approve {p.version}</button>
        </div>
      )}
      {dialog}{formDialog}
    </Card>
  )
}

function Validation({ v, rules }) {
  const side = (s) => <>
    <td className={`num ${upDown(s.return_pct)}`}>{pct(s.return_pct)}</td><td className="num">{s.closed}</td>
    <td className={`num ${upDown(s.avg_r)}`}>{rText(s.avg_r)}</td><td className={`num ${upDown(s.net_pnl)}`}>{signed(s.net_pnl)}</td>
  </>
  return (
    <>
      <h3 className="sub">Walk-forward validation {v.passed ? <Pill kind="good">Passed</Pill> : <Pill kind="warning">Did not pass</Pill>}</h3>
      <div className="table-wrap"><table>
        <thead>
          <tr><th rowSpan={2}>Period</th><th colSpan={4} className="ctr">Current version</th><th colSpan={4} className="ctr">Challenger</th><th rowSpan={2}>Better?</th></tr>
          <tr><th className="num">Return</th><th className="num">Trades</th><th className="num">Avg R</th><th className="num">Net</th>
            <th className="num">Return</th><th className="num">Trades</th><th className="num">Avg R</th><th className="num">Net</th></tr>
        </thead>
        <tbody>
          {v.folds.map((f) => (
            <tr key={f.start}><td>{fmtDate(f.start)} – {fmtDate(f.end)}</td>{side(f.champion)}{side(f.challenger)}
              <td>{f.challenger.net_pnl > f.champion.net_pnl ? '✓ yes' : '— no'}</td></tr>))}
          {v.champion && (
            <tr className="total-row"><td><strong>All periods</strong></td>
              <td /><td className="num">{v.champion.closed}</td><td className="num">{rText(v.champion.avg_r)}</td><td className="num">{signed(v.champion.net_pnl)}</td>
              <td /><td className="num">{v.challenger.closed}</td><td className="num">{rText(v.challenger.avg_r)}</td><td className="num"><strong>{signed(v.challenger.net_pnl)}</strong></td>
              <td>{v.periods_better} of {v.folds.length}</td></tr>)}
        </tbody>
      </table></div>
      <p className="fine">Each period is a separate ₹1,00,000 backtest on 1-minute data with the live risk rules, after charges. To pass, the
        challenger needs at least {rules.min_trades} trades, higher net P&amp;L and average R overall, and a better result in at least half the periods.
        {v.reasons && v.reasons.length ? ` Not passed: ${v.reasons.join('; ')}.` : ''}</p>
    </>
  )
}

function Comparison({ p, c }) {
  const ids = Object.keys(c.portfolios)
  // champion portfolios in their account colours; the challenger in its own colour and dashed (never colour alone)
  const series = ids.filter((id) => c.portfolios[id].curve.length && c.portfolios[id].base).map((id) => ({
    id, capital: c.portfolios[id].base, equity_curve: c.portfolios[id].curve,
    color: c.portfolios[id].challenger ? '--challenger' : `--acct-${id}`, dashed: c.portfolios[id].challenger,
    label: c.portfolios[id].challenger ? `${id} (challenger)` : id,
  }))
  return (
    <>
      <h3 className="sub">Challenger vs the strategy's portfolios since {fmtDate(c.since)}</h3>
      <Progress value={Math.min(c.sessions, c.needed)} max={c.needed} />
      <div className="table-wrap"><table>
        <thead><tr><th>Portfolio</th><th>Version</th><th className="num">Return</th><th className="num">Closed</th><th className="num">Avg R</th><th className="num">Net P&amp;L</th></tr></thead>
        <tbody>{ids.map((id) => {
          const x = c.portfolios[id]
          return (
            <tr key={id} className={x.challenger ? 'selected' : ''}><td><strong>{id}</strong></td><td>{x.challenger ? p.version : p.base_version}</td>
              <td className={`num ${upDown(x.return_pct)}`}>{pct(x.return_pct)}</td><td className="num">{x.closed}</td>
              <td className={`num ${upDown(x.avg_r)}`}>{rText(x.avg_r)}</td><td className={`num ${upDown(x.net_pnl)}`}>{signed(x.net_pnl)}</td></tr>
          )
        })}</tbody>
      </table></div>
      <EquityChart accounts={series} height={240} emptyText="The curves start with the challenger's first EOD run." />
      <p className="fine">Returns are on each portfolio's value when the challenger started, so different capitals compare directly. The
        challenger ({p.challenger_account}) trades the same signals under {p.version}; the others trade {p.base_version}.</p>
    </>
  )
}

// ---------- versions ----------

function flat(o, pre = '') {
  return Object.entries(o || {}).reduce((acc, [k, v]) => (v && typeof v === 'object' && !Array.isArray(v)
    ? { ...acc, ...flat(v, `${pre}${k}.`) } : { ...acc, [`${pre}${k}`]: JSON.stringify(v) }), {})
}

function Versions({ data, onDone, setMsg }) {
  const [confirm, dialog] = useConfirm()
  const current = Object.fromEntries(data.versions.filter((x) => x.is_champion).map((x) => [x.strategy_id, x]))
  const restore = async (x) => {
    const cur = current[x.strategy_id]
    const ok = await confirm({ title: `Make ${x.version} ${x.strategy_name}'s current version again?`, confirmLabel: 'Restore',
      body: `From the next scan the portfolios running ${x.strategy_name} trade ${x.version}${cur ? ` instead of ${cur.version}` : ''}. Open trades keep the rules they were opened with. You can switch back the same way.` })
    if (!ok) return
    try { await api.post('/api/learning/rollback', { confirm: true, version: x.version }); setMsg({ kind: 'good', text: `${x.version} is ${x.strategy_name}'s current version again.` }); onDone() }
    catch (e) { setMsg({ kind: 'error', text: e.message }) }
  }
  return (
    <Card title="Strategy versions">
      <div className="table-wrap"><table>
        <thead><tr><th>Strategy</th><th>Version</th><th>Status</th><th>Since</th><th>Differs from its current version</th><th /></tr></thead>
        <tbody>{data.versions.map((x) => {
          const cur = current[x.strategy_id]
          const base = flat(cur ? cur.parameters : {})
          const mine = flat(x.parameters)
          const diff = Object.keys({ ...base, ...mine }).filter((k) => k !== 'version' && base[k] !== mine[k])
          return (
            <tr key={x.version}>
              <td>{x.strategy_name || '—'}</td>
              <td><strong>{x.version}</strong></td>
              <td>{x.is_champion ? <Pill kind="good">Current</Pill> : x.status === 'challenger' ? <Pill kind="neutral">Challenger</Pill> : <span className="muted">{x.status}</span>}</td>
              <td>{fmtDate((x.approved_on || x.created_at || '').slice(0, 10))}</td>
              <td className="fine-cell">{x.is_champion ? '—' : diff.length ? diff.map((k) => `${k.split('.').pop()}: ${base[k] ?? '—'} → ${mine[k] ?? '—'}`).join(' · ') : 'same settings'}</td>
              <td className="num">{!x.is_champion && x.status === 'retired' && x.strategy_id && <button className="btn btn-sm" onClick={() => restore(x)}>Make current</button>}</td>
            </tr>
          )
        })}</tbody>
      </table></div>
      <p className="fine">Every version is immutable: a change (on the Strategies screen or an approved proposal) becomes a new version with its
        own file in config\strategies. A retired version can be made its strategy's current version again with one click.</p>
      {dialog}
    </Card>
  )
}

// ---------- integrations ----------

function Integrations() {
  const [s, setS] = useState(null)
  const [msg, setMsg] = useState(null)
  useEffect(() => { api.get('/api/integrations').then(setS).catch(() => {}) }, [])
  const test = async (path, done) => {
    setMsg({ kind: 'info', text: 'Sending…' })
    try { const r = await api.confirm(path); setMsg({ kind: 'good', text: done(r) }) } catch (e) { setMsg({ kind: 'error', text: e.message }) }
  }
  if (!s) return null
  return (
    <Card title="Explanations and notifications">
      {msg && <Notice kind={msg.kind} onClose={() => setMsg(null)}>{msg.text}</Notice>}
      <div className="detail">
        <div>
          <h3 className="sub">Journal explanations</h3>
          <dl className="kv">
            <dt>Provider</dt><dd>{s.llm.provider === 'template' ? 'None (template text)' : s.llm.provider === 'anthropic' ? 'Claude API' : 'Ollama (local)'}</dd>
            {s.llm.model && <><dt>Model</dt><dd>{s.llm.model}</dd></>}
            <dt>Set up</dt><dd>{s.llm.configured ? <Pill kind="good">Ready</Pill> : <Pill kind="warning">{s.llm.provider === 'ollama' ? 'OLLAMA_MODEL missing in .env' : 'ANTHROPIC_API_KEY missing in .env'}</Pill>}</dd>
          </dl>
          {s.llm.provider !== 'template' && <button className="btn btn-sm" style={{ marginTop: 10 }} onClick={() => test('/api/llm/test', (r) => `${r.model} answered.`)}>Test the LLM</button>}
        </div>
        <div>
          <h3 className="sub">Telegram</h3>
          <dl className="kv">
            <dt>Status</dt><dd>{s.telegram.configured ? <Pill kind="good">Set up</Pill> : <Pill kind="neutral">Not set up</Pill>}</dd>
            <dt>Sends</dt><dd>{s.telegram.sends.join(', ')}</dd>
          </dl>
          {s.telegram.configured && <button className="btn btn-sm" style={{ marginTop: 10 }} onClick={() => test('/api/notify/test', () => 'Test message sent: check Telegram.')}>Send a test message</button>}
        </div>
      </div>
      <p className="fine">Set in .env (LLM_PROVIDER, ANTHROPIC_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID), then restart run.bat. Keys are never shown or logged.</p>
    </Card>
  )
}
