// Research tab: full-page charts over the stored history (KLineChart core + our TradingView-style toolbars).
// Phase 1: search, timeframes, chart types, indicators, drawings per stock, live prices, screenshot, full screen.
// Phase 2: replay. Phase 3: 1, 2 or 4 charts on one replay clock (engine.js), paper trading inside a replay
// (trading.js), the agent's signals and trades on the chart, 1-second data.
import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '../api.js'
import { useConfirm } from '../components.jsx'
import { INDICATORS, TOOLS } from './kc.js'
import { ToolIcon } from './icons.jsx'
import { IST_MS, SPEEDS, TF_MIN } from './replay.js'
import { ReplayClock } from './engine.js'
import { ReplayTrading } from './trading.js'
import ChartPane, { TFS } from './ChartPane.jsx'
import TradePanel from './TradePanel.jsx'

const TF_TITLE = { '1m': '1 minute', '5m': '5 minutes', '15m': '15 minutes', '1h': '1 hour', '4h': '4 hours', '1D': '1 day', '1W': '1 week', '1M': '1 month' }
const CHART_TYPES = [['candle_solid', 'Candles'], ['candle_up_stroke', 'Hollow candles'], ['ohlc', 'Bars'], ['area', 'Line']]
const DEFAULT_SYMBOL = { symbol: 'NIFTY 50', name: 'NIFTY 50', sector: 'Index', tick: 0.05 }
const DEFAULT_PANE = { symbol: DEFAULT_SYMBOL, tf: '1D', chartType: 'candle_solid', indicators: [{ name: 'VOL', pane: 'sub' }] }
const LAYOUTS = [[1, 'One chart'], [2, 'Two charts side by side'], [4, 'Four charts']]

const store = {
  get: (k, d) => { try { const v = localStorage.getItem(k); return v == null ? d : JSON.parse(v) } catch { return d } },
  set: (k, v) => { try { localStorage.setItem(k, JSON.stringify(v)) } catch { /* private window */ } },
}

// ---------- symbol search ----------

function SymbolSearch({ symbol, onPick }) {
  const [q, setQ] = useState('')
  const [open, setOpen] = useState(false)
  const [items, setItems] = useState([])
  const [pos, setPos] = useState(null)
  const [hi, setHi] = useState(0)
  const box = useRef(null)
  const input = useRef(null)

  useEffect(() => {
    if (!open) return undefined
    const t = setTimeout(() => {
      api.get(`/api/research/symbols?q=${encodeURIComponent(q)}&limit=40`).then((r) => { setItems(r); setHi(0) }).catch(() => setItems([]))
    }, 120)
    return () => clearTimeout(t)
  }, [q, open])

  useEffect(() => {
    const close = (e) => { if (box.current && !box.current.contains(e.target)) setOpen(false) }
    document.addEventListener('mousedown', close)
    return () => document.removeEventListener('mousedown', close)
  }, [])

  const pick = (s) => { onPick(s); setOpen(false); setQ(''); input.current && input.current.blur() }
  const key = (e) => {
    if (e.key === 'ArrowDown') { e.preventDefault(); setHi((h) => Math.min(items.length - 1, h + 1)) }
    else if (e.key === 'ArrowUp') { e.preventDefault(); setHi((h) => Math.max(0, h - 1)) }
    else if (e.key === 'Enter' && items[hi]) pick(items[hi])
    else if (e.key === 'Escape') { setOpen(false); input.current.blur() }
  }

  return (
    <div className="rs-search" ref={box}>
      <svg viewBox="0 0 24 24" className="rs-search-icon" aria-hidden="true"><circle cx="11" cy="11" r="6.5" /><path d="M16 16l4.5 4.5" /></svg>
      <input ref={input} value={open ? q : symbol} placeholder="Search Nifty 500" aria-label="Search stocks"
        role="combobox" aria-expanded={open} aria-controls="rs-search-list"
        onFocus={(e) => { const r = e.target.getBoundingClientRect(); setPos({ top: r.bottom + 4, left: r.left }); setOpen(true); setQ('') }} onChange={(e) => setQ(e.target.value)} onKeyDown={key} />
      {open && (
        <ul className="rs-search-list" id="rs-search-list" role="listbox" style={pos ? { top: pos.top, left: Math.max(8, Math.min(pos.left, window.innerWidth - 470)) } : undefined}>
          {items.length === 0 && <li className="rs-empty">No Nifty 500 stock matches</li>}
          {items.map((s, i) => (
            <li key={s.symbol} role="option" aria-selected={i === hi} className={i === hi ? 'on' : ''}
              onMouseEnter={() => setHi(i)} onMouseDown={(e) => { e.preventDefault(); pick(s) }}>
              <strong>{s.symbol}</strong><span className="rs-name">{s.name}</span><span className="rs-sector">{s.sector}</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

// ---------- indicators dialog ----------

function IndicatorDialog({ active, onAdd, onRemove, onParams, onClose }) {
  const [q, setQ] = useState('')
  const list = INDICATORS.filter(([n, l]) => !q || `${n} ${l}`.toLowerCase().includes(q.toLowerCase()))
  return (
    <div className="modal-backdrop" onMouseDown={onClose}>
      <div className="modal rs-modal" role="dialog" aria-label="Indicators" onMouseDown={(e) => e.stopPropagation()}>
        <div className="rs-modal-head"><strong>Indicators</strong><button className="rs-icon-btn" onClick={onClose} aria-label="Close">✕</button></div>
        {active.length > 0 && (
          <div className="rs-active">
            <div className="rs-sub">On the chart</div>
            {active.map((ind) => (
              <div key={ind.id} className="rs-active-row">
                <span className="rs-active-name">{ind.name}</span>
                <ParamsInput value={ind.calcParams} onChange={(p) => onParams(ind, p)} />
                <button className="btn btn-sm btn-danger-quiet" onClick={() => onRemove(ind)}>Remove</button>
              </div>
            ))}
          </div>
        )}
        <input className="rs-filter" autoFocus placeholder="Search indicators" value={q} onChange={(e) => setQ(e.target.value)} />
        <ul className="rs-ind-list">
          {list.map(([n, l, where]) => (
            <li key={n}><button onClick={() => onAdd(n, where)}><span>{l}</span><span className="rs-sector">{where === 'main' ? 'on price' : 'own pane'}</span></button></li>
          ))}
        </ul>
      </div>
    </div>
  )
}

function ParamsInput({ value, onChange }) {
  const [text, setText] = useState((value || []).join(', '))
  useEffect(() => setText((value || []).join(', ')), [JSON.stringify(value)])
  if (!value || value.length === 0) return <span className="rs-sector">no settings</span>
  const commit = () => {
    const nums = text.split(/[,\s]+/).filter(Boolean).map(Number)
    if (nums.length && nums.every((n) => Number.isFinite(n) && n > 0)) onChange(nums)
    else setText(value.join(', '))
  }
  return <input className="rs-params" value={text} aria-label="Settings" onChange={(e) => setText(e.target.value)} onBlur={commit} onKeyDown={(e) => e.key === 'Enter' && commit()} />
}

function TextDialog({ initial, onDone, onClose }) {
  const [t, setT] = useState(initial || '')
  return (
    <div className="modal-backdrop" onMouseDown={onClose}>
      <form className="modal rs-modal rs-modal-sm" onMouseDown={(e) => e.stopPropagation()} onSubmit={(e) => { e.preventDefault(); if (t.trim()) onDone(t.trim()) }}>
        <div className="rs-modal-head"><strong>Text</strong></div>
        <input className="rs-filter" autoFocus value={t} onChange={(e) => setT(e.target.value)} placeholder="Label text" />
        <div className="rs-modal-foot"><button type="button" className="btn btn-sm" onClick={onClose}>Cancel</button><button className="btn btn-sm btn-primary">{initial ? 'Save' : 'Place on chart'}</button></div>
      </form>
    </div>
  )
}

// ---------- replay controls ----------

const fmtCursor = (ms) => {
  if (ms == null) return ''
  const d = new Date(ms + IST_MS)
  const day = d.toLocaleDateString('en-IN', { weekday: 'short', day: '2-digit', month: 'short', year: 'numeric', timeZone: 'UTC' })
  const hms = d.toISOString().slice(11, 19)
  return hms === '00:00:00' ? day : `${day} ${hms}`
}

function ReplayBar({ rv, tf, active, onPickMode, onJump, onPlay, onPause, onStep, onSession, onSpeed, onUnit, onExit }) {
  const [date, setDate] = useState('')
  const on = rv.phase === 'on'
  return (
    <div className="rs-replay" role="toolbar" aria-label="Replay">
      <span className="rs-replay-tag">Replay</span>
      <button className={`rs-text-btn${rv.phase === 'pick' ? ' on' : ''}`} onClick={onPickMode} title="Pick the bar to start from on the chart">
        <svg viewBox="0 0 24 24"><path d="M12 3v18M7 8l5-5 5 5" /></svg> Select bar
      </button>
      <form className="rs-jump" onSubmit={(e) => { e.preventDefault(); onJump(date) }}>
        <input type="datetime-local" value={date} onChange={(e) => setDate(e.target.value)} aria-label="Start the replay at a date and time (IST)" />
        <button className="rs-text-btn" disabled={!date}>Go</button>
      </form>
      {on && (
        <>
          <span className="rs-sep" />
          {rv.playing
            ? <button className="rs-icon-btn rs-play" onClick={onPause} title="Pause (Space)" aria-label="Pause"><svg viewBox="0 0 24 24"><path d="M8 5v14M16 5v14" /></svg></button>
            : <button className="rs-icon-btn rs-play" onClick={onPlay} disabled={rv.ended} title="Play (Space)" aria-label="Play"><svg viewBox="0 0 24 24"><path d="M7 5l12 7-12 7z" /></svg></button>}
          <button className="rs-icon-btn" onClick={onStep} disabled={rv.ended} title={`Step forward one ${rv.unit === 'bar' ? `${tf} bar` : 'minute'} (Shift+→)`} aria-label="Step forward">
            <svg viewBox="0 0 24 24"><path d="M6 5l9 7-9 7zM18 5v14" /></svg>
          </button>
          <button className="rs-icon-btn" onClick={onSession} disabled={rv.ended} title="Jump to the end of this session" aria-label="Jump to the end of this session">
            <svg viewBox="0 0 24 24"><path d="M4 5l7 7-7 7zM12 5l7 7-7 7zM21 5v14" /></svg>
          </button>
          <select className="rs-select" value={rv.speed} onChange={(e) => onSpeed(Number(e.target.value))} aria-label="Speed" title="Steps per second">
            {SPEEDS.map((v) => <option key={v} value={v}>{v}×</option>)}
          </select>
          <select className="rs-select" value={rv.unit} onChange={(e) => onUnit(e.target.value)} aria-label="Pace and step" title="What 1× means, and what the step button moves">
            <option value="minute">1× = 1 minute a second</option>
            <option value="bar">1× = 1 {tf} bar a second</option>
          </select>
          {rv.secMode && <span className={`rs-sec-tag ${rv.secMode}`} title="Playback always moves second by second. Minutes with recorded 1-second bars play them; minutes from before recording started follow a simulated open → low/high → close path.">{rv.secMode === 'real' ? 'real seconds' : 'simulated seconds'}</span>}
          <span className="rs-replay-time">{fmtCursor(rv.T)}</span>
          {rv.ended && <span className="rs-replay-note">End of stored data</span>}
          {rv.note && <span className="rs-replay-note">{rv.note}</span>}
        </>
      )}
      {!on && <span className="rs-replay-note">{active ? 'Pick a new start: click a bar or enter a date' : 'Click a bar on the chart, or enter a date and time'}</span>}
      <span className="rs-grow" />
      <button className="rs-text-btn" onClick={onExit} title="Leave replay and return to the live chart">
        <svg viewBox="0 0 24 24"><path d="M6 6l12 12M18 6L6 18" /></svg> Exit replay
      </button>
    </div>
  )
}

// ---------- the screen ----------

// The saved layout: how many charts, which is active, each chart's stock/timeframe/indicators/type, agent overlay.
// (Phase 1 saved only one chart's indicators and type; that is read as chart 1.)
function readLayout(data) {
  const panes = data && Array.isArray(data.panes) ? data.panes : [{
    ...DEFAULT_PANE,
    symbol: store.get('research.symbol', DEFAULT_SYMBOL),
    tf: store.get('research.tf', '1D'),
    ...(data && data.indicators ? { indicators: data.indicators } : {}),
    ...(data && data.chartType ? { chartType: data.chartType } : {}),
  }]
  while (panes.length < 4) panes.push({ ...panes[0] })
  return { count: [1, 2, 4].includes(data && data.count) ? data.count : 1, active: Math.min((data && data.active) || 0, 3), panes, agent: !!(data && data.agent) }
}

export default function Research({ toggleNav }) {
  const wrap = useRef(null)
  const paneRefs = useRef([])
  const [lay, setLay] = useState(null)
  const [tool, setTool] = useState(null)
  const [openGroup, setOpenGroup] = useState(null)
  const [magnet, setMagnet] = useState(false)
  const [locked, setLocked] = useState(false)
  const [hidden, setHidden] = useState(false)
  const [dlg, setDlg] = useState(null)
  const [info, setInfo] = useState({})
  const [ask, confirmDialog] = useConfirm()
  const [rv, setRv] = useState(null)               // null | { phase: 'pick' } | { phase: 'on', ... }
  const [tradeOpen, setTradeOpen] = useState(true)
  const [tradeVer, setTradeVer] = useState(0)
  const trading = useRef(null)
  const lastUi = useRef(0)
  const activeRef = useRef(0)

  // ----- the replay clock -----
  const clockRef = useRef(null)
  if (!clockRef.current) {
    clockRef.current = new ReplayClock({
      onChange: (force) => {
        const now = performance.now()
        if (!force && now - lastUi.current < 120) return
        lastUi.current = now
        const c = clockRef.current
        const ap = c.panes.get(activeRef.current)                     // the tag follows the active chart
        setRv((cur) => (c.active ? { phase: 'on', T: c.T, playing: c.playing, speed: c.speed, unit: c.unit, secMode: (ap && ap.secMode) || c.secMode, ended: c.ended, note: c.note }
          : cur && cur.phase === 'pick' ? cur : null))
      },
      onRows: (symbol, rows) => { if (trading.current && trading.current.onRows(symbol, rows)) setTradeVer((v) => v + 1) },
    })
  }
  const clock = clockRef.current

  useEffect(() => {
    api.get('/api/research/layout/global').then((r) => setLay(readLayout(r.data))).catch(() => setLay(readLayout(null)))
  }, [])

  const saveLay = useCallback((fn) => {
    setLay((cur) => {
      const next = fn(cur)
      api.put('/api/research/layout/global', next).catch(() => {})
      return next
    })
  }, [])

  const setPane = useCallback((i, patch) => saveLay((cur) => ({ ...cur, panes: cur.panes.map((p, k) => (k === i ? { ...p, ...patch } : p)) })), [saveLay])

  if (!lay) return <div className="rs" ref={wrap} />
  const act = lay.active < lay.count ? lay.active : 0
  activeRef.current = act
  const pane = lay.panes[act]
  const handle = () => paneRefs.current[act]
  const symbol = pane.symbol
  const tf = pane.tf

  // ----- replay controls -----
  const endSession = () => {
    const tr = trading.current
    if (tr) for (const p of tr.positions()) tr.closePosition(p.symbol, 'replay ended')
  }
  const startAt = (T) => {
    endSession()
    trading.current = new ReplayTrading(`R${Date.now()}`, () => setTradeVer((v) => v + 1))
    setTradeVer((v) => v + 1)
    clock.start(T)
  }
  const exitReplay = () => { endSession(); clock.exit(); setRv(null) }
  const jumpTo = (value) => {
    const m = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/.exec(value || '')
    if (m) startAt(Date.UTC(+m[1], +m[2] - 1, +m[3], +m[4], +m[5]) - IST_MS)
  }
  const activeTf = () => lay.panes[act].tf
  const play = () => clock.play(activeTf)
  const pause = () => clock.pause()

  // ----- drawing tools on the active chart -----
  const startTool = (name) => {
    setOpenGroup(null)
    if (name === 'textLabel') { setDlg({ kind: 'text' }); return }
    setTool(name)
    handle() && handle().startTool(name, magnet)
  }
  const placeText = (text) => {
    if (dlg.id) handle().editText(dlg.id, text)
    else { setTool('textLabel'); handle().startTool('textLabel', false, { text }) }
    setDlg(null)
  }

  // ----- trading lines for each chart -----
  const tr = trading.current
  const levelsFor = (sym) => {
    if (!tr || !rv || rv.phase !== 'on') return []
    const out = []
    const p = tr.pos[sym]
    if (p && p.qty) {
      const last = tr.last[sym]?.price ?? p.avg
      const g = Math.round((last - p.avg) * p.qty)
      out.push({ price: p.avg, color: '#2a78d6', text: `${p.qty > 0 ? 'Long' : 'Short'} ${Math.abs(p.qty)} @ ${p.avg} · ${g >= 0 ? '+' : '−'}₹${Math.abs(g).toLocaleString('en-IN')}` })
    }
    for (const o of tr.orders.filter((x) => x.symbol === sym)) {
      const px = o.price ?? o.trigger
      if (px == null) continue
      const color = o.role === 'stop' ? '#e34948' : o.role === 'target' ? '#1baf7a' : '#7a7973'
      out.push({ price: px, color, dashed: true, text: `${o.role === 'entry' ? `${o.side === 'buy' ? 'Buy' : 'Sell'} ${o.type}` : o.role === 'stop' ? 'Stop-loss' : 'Target'} ${o.qty} @ ${px}` })
    }
    return out
  }

  const replayOn = rv && rv.phase === 'on'
  return (
    <div className="rs" ref={wrap}>
      <div className="rs-top" role="toolbar" aria-label="Chart">
        <button className="rs-icon-btn" onClick={toggleNav} title="Show the menu" aria-label="Show the menu">
          <svg viewBox="0 0 24 24"><path d="M4 7h16M4 12h16M4 17h16" /></svg>
        </button>
        <SymbolSearch symbol={symbol.symbol} onPick={(s) => setPane(act, { symbol: s })} />
        <span className="rs-sep" />
        <div className="rs-tfs" role="group" aria-label="Timeframe">
          {TFS.map(([k]) => <button key={k} className={k === tf ? 'on' : ''} title={TF_TITLE[k]} aria-label={TF_TITLE[k]} aria-pressed={k === tf} onClick={() => setPane(act, { tf: k })}>{k}</button>)}
        </div>
        <span className="rs-sep" />
        <select className="rs-select" value={pane.chartType || 'candle_solid'} onChange={(e) => handle().setChartType(e.target.value)} aria-label="Chart type">
          {CHART_TYPES.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
        </select>
        <button className="rs-text-btn" onClick={() => setDlg({ kind: 'ind' })}>
          <svg viewBox="0 0 24 24"><path d="M3 17l5-6 4 3 6-8 3 3" /></svg> Indicators
        </button>
        <div className="rs-layouts" role="group" aria-label="Layout">
          {LAYOUTS.map(([n, l]) => (
            <button key={n} className={lay.count === n ? 'on' : ''} title={l} aria-label={l} aria-pressed={lay.count === n}
              onClick={() => saveLay((cur) => ({ ...cur, count: n, active: cur.active < n ? cur.active : 0 }))}>
              <svg viewBox="0 0 24 24">{n === 1 ? <rect x="4" y="5" width="16" height="14" rx="1" /> : n === 2 ? <><rect x="3" y="5" width="8" height="14" rx="1" /><rect x="13" y="5" width="8" height="14" rx="1" /></>
                : <><rect x="3" y="4" width="8" height="7" rx="1" /><rect x="13" y="4" width="8" height="7" rx="1" /><rect x="3" y="13" width="8" height="7" rx="1" /><rect x="13" y="13" width="8" height="7" rx="1" /></>}</svg>
            </button>
          ))}
        </div>
        <button className={`rs-text-btn${lay.agent ? ' on' : ''}`} aria-pressed={lay.agent} title="Show the agent's signals, buys, sells, stops and targets on the charts"
          onClick={() => saveLay((cur) => ({ ...cur, agent: !cur.agent }))}>
          <svg viewBox="0 0 24 24"><path d="M12 3l2.5 5 5.5.8-4 3.9.9 5.5L12 15.6 7.1 18.2 8 12.7 4 8.8l5.5-.8z" /></svg> Agent
        </button>
        <button className={`rs-text-btn${rv ? ' on' : ''}`} aria-pressed={!!rv} title="Bar replay: pick a point in the past and play the charts forward"
          onClick={() => (rv ? exitReplay() : setRv({ phase: 'pick' }))}>
          <svg viewBox="0 0 24 24"><path d="M5 5v14M9 6l10 6-10 6z" /></svg> Replay
        </button>
        {replayOn && (
          <button className={`rs-text-btn${tradeOpen ? ' on' : ''}`} aria-pressed={tradeOpen} onClick={() => setTradeOpen(!tradeOpen)} title="Paper trade during the replay">
            <svg viewBox="0 0 24 24"><path d="M4 18l5-6 4 3 7-9M15 6h5v5" /></svg> Trade
          </button>
        )}
        <span className="rs-grow" />
        <span className="rs-meta">
          {[symbol.name && symbol.name !== symbol.symbol ? symbol.name : '', symbol.sector,
            info[act] && info[act].first && TF_MIN[tf] ? `1-minute data from ${String(info[act].first).slice(0, 10)}` : ''].filter(Boolean).join(' · ')}
        </span>
        <button className="rs-icon-btn" onClick={() => handle().screenshot()} title="Save a picture of the active chart" aria-label="Screenshot">
          <svg viewBox="0 0 24 24"><path d="M4 8h3l2-2.5h6L17 8h3v11H4z" /><circle cx="12" cy="13" r="3.5" /></svg>
        </button>
        <button className="rs-icon-btn" onClick={() => (document.fullscreenElement ? document.exitFullscreen() : wrap.current.requestFullscreen && wrap.current.requestFullscreen())} title="Full screen" aria-label="Full screen">
          <svg viewBox="0 0 24 24"><path d="M4 9V4h5M15 4h5v5M20 15v5h-5M9 20H4v-5" /></svg>
        </button>
      </div>
      {rv && <ReplayBar rv={rv} tf={tf} active={clock.active} onPickMode={() => { pause(); setRv({ phase: 'pick' }) }} onJump={jumpTo}
        onPlay={play} onPause={pause} onStep={() => clock.step(clock.unit === 'bar' ? 'bar' : 'minute', tf)} onSession={() => clock.step('session', tf)}
        onSpeed={(v) => { clock.speed = v; clock.onChange(true) }} onUnit={(v) => { clock.unit = v; clock.onChange(true) }} onExit={exitReplay} />}
      <div className="rs-body">
        <div className="rs-tools" role="toolbar" aria-label="Drawing tools" aria-orientation="vertical">
          <button className={!tool ? 'on' : ''} title="Cursor (Esc)" aria-label="Cursor" onClick={() => { handle() && handle().cancelTool(); setTool(null) }}>
            <ToolIcon name="cursor" />
          </button>
          {TOOLS.map((g) => {
            const activeIn = g.items.find(([n]) => n === tool)
            const shown = activeIn || g.items[0]
            return (
              <div key={g.group} className="rs-tool-group">
                <button className={activeIn ? 'on' : ''} title={shown[1]} aria-label={shown[1]} onClick={() => startTool(shown[0])}><ToolIcon name={shown[0]} /></button>
                <button className="rs-more" aria-label={`More ${g.group.toLowerCase()} tools`} aria-expanded={openGroup?.group === g.group}
                  onClick={(e) => { const r = e.currentTarget.parentElement.getBoundingClientRect(); setOpenGroup(openGroup?.group === g.group ? null : { group: g.group, top: r.top, left: r.right + 6 }) }}>›</button>
                {openGroup?.group === g.group && (
                  <div className="rs-flyout" role="menu" style={{ top: Math.min(openGroup.top, window.innerHeight - 60 - g.items.length * 34), left: openGroup.left }}>
                    <div className="rs-sub">{g.group}</div>
                    {g.items.map(([n, l]) => (
                      <button key={n} role="menuitem" className={n === tool ? 'on' : ''} onClick={() => startTool(n)}><ToolIcon name={n} /> {l}</button>
                    ))}
                  </div>
                )}
              </div>
            )
          })}
          <span className="rs-tools-sep" />
          <button className={magnet ? 'on' : ''} title="Magnet: snap to open, high, low, close" aria-pressed={magnet} onClick={() => setMagnet(!magnet)}><ToolIcon name="magnet" /></button>
          <button className={locked ? 'on' : ''} title="Lock all drawings" aria-pressed={locked} onClick={() => { const v = !locked; setLocked(v); handle().forDrawings((c, o) => c.overrideOverlay({ id: o.id, lock: v })) }}><ToolIcon name="lock" /></button>
          <button className={hidden ? 'on' : ''} title="Hide all drawings" aria-pressed={hidden} onClick={() => { const v = !hidden; setHidden(v); handle().forDrawings((c, o) => c.overrideOverlay({ id: o.id, visible: !v })) }}><ToolIcon name="eye" /></button>
          <button title="Remove all drawings on this stock" onClick={async () => { if (await ask({ title: `Remove every drawing on ${symbol.symbol}?`, confirmLabel: 'Remove all', danger: true })) handle().removeAll() }}><ToolIcon name="trash" /></button>
        </div>
        <div className="rs-main" onMouseDown={() => setOpenGroup(null)}>
          <div className={`rs-grid n${lay.count}`}>
            {lay.panes.slice(0, lay.count).map((p, i) => (
              <ChartPane key={i} id={i} ref={(el) => { paneRefs.current[i] = el }} symbol={p.symbol} tf={p.tf} layout={p}
                onLayout={(patch) => setPane(i, patch)} active={i === act && lay.count > 1} showLabel={lay.count > 1}
                onActivate={() => { if (i !== lay.active) saveLay((cur) => ({ ...cur, active: i })) }}
                clock={clock} picking={rv && rv.phase === 'pick'} onPicked={startAt} agentOn={lay.agent}
                levels={levelsFor(p.symbol.symbol)} replayT={replayOn ? rv.T : null}
                onToolDone={() => setTool(null)} onTextEdit={(id, text) => setDlg({ kind: 'text', id, text })}
                onInfo={(k, v) => setInfo((cur) => ({ ...cur, [k]: v }))} />
            ))}
          </div>
          {replayOn && tradeOpen && <TradePanel engine={tr} symbol={symbol.symbol} version={tradeVer} onClose={() => setTradeOpen(false)} />}
        </div>
      </div>
      <Keys handle={handle} clock={clock} rv={rv} setRv={setRv} setTool={setTool} startTool={startTool} play={play} pause={pause} tf={tf} />
      {dlg && dlg.kind === 'ind' && <IndicatorDialog active={handle() ? handle().indicators() : []} onAdd={(n, w) => { handle().addIndicator(n, w); setDlg({ kind: 'ind' }) }}
        onRemove={(ind) => { handle().removeIndicator(ind); setDlg({ kind: 'ind' }) }} onParams={(ind, p) => { handle().setIndicatorParams(ind, p); setDlg({ kind: 'ind' }) }} onClose={() => setDlg(null)} />}
      {confirmDialog}
      {dlg && dlg.kind === 'text' && <TextDialog initial={dlg.text} onDone={placeText} onClose={() => setDlg(null)} />}
    </div>
  )
}

// Keyboard: Space play/pause, Shift+→ step, Esc cancel, Delete removes the selected drawing, Alt+T/H/V/F/R tools.
function Keys({ handle, clock, rv, setRv, setTool, startTool, play, pause, tf }) {
  useEffect(() => {
    const key = (e) => {
      if (e.target.closest && e.target.closest('input, textarea, select')) return
      const h = handle()
      if (!h) return
      if (clock.active && e.key === ' ') { e.preventDefault(); clock.playing ? pause() : play(); return }
      if (clock.active && e.shiftKey && e.key === 'ArrowRight') { e.preventDefault(); clock.step('minute', tf); return }
      if (e.key === 'Escape' && rv && rv.phase === 'pick') { setRv(clock.active ? { phase: 'on', T: clock.T } : null); return }
      if (e.key === 'Escape') { if (h.cancelTool()) setTool(null) }
      else if (e.key === 'Delete' || e.key === 'Backspace') h.removeSelected()
      else if (e.altKey && !e.ctrlKey) {
        const k = { t: 'segment', h: 'horizontalStraightLine', v: 'verticalStraightLine', f: 'fibonacciLine', r: 'rectangle' }[e.key.toLowerCase()]
        if (k) { e.preventDefault(); startTool(k) }
      }
    }
    window.addEventListener('keydown', key)
    return () => window.removeEventListener('keydown', key)
  })
  return null
}
