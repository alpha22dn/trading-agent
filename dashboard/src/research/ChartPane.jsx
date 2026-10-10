// One chart of the Research layout: its own stock, timeframe, indicators and chart type; your drawings per stock;
// live prices; the agent's signals and trades; replay positions/orders as lines. The replay clock (engine.js)
// drives its data while a replay is on.
import { forwardRef, useCallback, useEffect, useImperativeHandle, useMemo, useRef, useState } from 'react'
import { dispose, init } from 'klinecharts'
import { api, onPrice } from '../api.js'
import { bucketStart, nextBucket } from './replay.js'

export const TFS = [
  ['1m', { type: 'minute', span: 1 }], ['5m', { type: 'minute', span: 5 }], ['15m', { type: 'minute', span: 15 }],
  ['1h', { type: 'hour', span: 1 }], ['4h', { type: 'hour', span: 4 }],
  ['1D', { type: 'day', span: 1 }], ['1W', { type: 'week', span: 1 }], ['1M', { type: 'month', span: 1 }],
]
export const MAIN = 'candle_pane'
const tfOf = (period) => TFS.find(([, p]) => p.type === period.type && p.span === period.span)[0]

export function css(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim()
}

export function isDark() {
  const t = document.documentElement.dataset.theme
  if (t) return t === 'dark'
  return window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches
}

export function chartStyles(type) {
  const up = css('--series-up') || '#1baf7a'
  const down = css('--series-down') || '#e34948'
  const border = css('--border') || '#e2e1dc'
  const muted = css('--muted') || '#7a7973'
  return {
    grid: { horizontal: { color: border }, vertical: { color: border } },
    candle: {
      type,
      bar: { upColor: up, downColor: down, noChangeColor: muted, upBorderColor: up, downBorderColor: down, noChangeBorderColor: muted, upWickColor: up, downWickColor: down, noChangeWickColor: muted },
      area: { lineColor: css('--series-1') || '#2a78d6', backgroundColor: [{ offset: 0, color: 'rgba(42,120,214,0.02)' }, { offset: 1, color: 'rgba(42,120,214,0.18)' }] },
      priceMark: { last: { upColor: up, downColor: down, noChangeColor: muted } },
      tooltip: { title: { show: true } },
    },
    indicator: { tooltip: { title: { show: true } } },
  }
}

const isoMs = (s) => (s ? Date.parse(s) : null)
const dayMs = (d) => Date.parse(`${d}T00:00:00+05:30`)

const ChartPane = forwardRef(function ChartPane({ id, symbol, tf, layout, onLayout, active, onActivate, clock, picking,
  onPicked, agentOn, levels, replayT, onToolDone, onTextEdit, onInfo, showLabel }, ref) {
  const host = useRef(null)
  const chart = useRef(null)
  const sub = useRef(null)
  const selected = useRef(null)
  const drawing = useRef(null)
  const symRef = useRef(symbol.symbol)
  const tfRef = useRef(tf)
  symRef.current = symbol.symbol
  tfRef.current = tf
  const quiet = useRef(false)
  const pending = useRef({})
  const agentIds = useRef([])          // [{ id, t }] agent overlays with their time (hidden after the replay time)
  const [pickX, setPickX] = useState(null)
  const [err, setErr] = useState(null)
  const downAt = useRef(null)

  // ----- your drawings, saved per stock -----
  const doSave = useCallback((sym) => {
    const c = chart.current
    if (!c) return
    const overlays = c.getOverlays({ groupId: sym }).filter((o) => o.points.length > 0 && o.currentStep === -1).map((o) => ({
      name: o.name, points: o.points.map((p) => ({ timestamp: p.timestamp, value: p.value })), extendData: o.extendData ?? null,
      lock: o.lock, visible: o.visible, mode: o.mode, styles: o.styles ?? null,
    }))
    api.put(`/api/research/layout/${encodeURIComponent(`sym:${sym}`)}`, { overlays }).catch(() => {})
  }, [])
  const saveDrawings = useCallback((sym = symRef.current) => {
    if (quiet.current || !sym || sym.startsWith('agent') || sym === 'trade') return
    clearTimeout(pending.current[sym])
    pending.current[sym] = setTimeout(() => { delete pending.current[sym]; if (!quiet.current) doSave(sym) }, 400)
  }, [doSave])
  const flushSaves = useCallback(() => {
    for (const [sym, t] of Object.entries(pending.current)) { clearTimeout(t); doSave(sym) }
    pending.current = {}
  }, [doSave])

  // the screen's callbacks change every render; the overlay events must not (they re-load the drawings)
  const cbs = useRef({})
  cbs.current = { onToolDone, onTextEdit, onInfo, onLayout }
  const events = useMemo(() => ({
    onDrawEnd: (e) => { drawing.current = null; cbs.current.onToolDone(); saveDrawings(e.overlay.groupId) },
    onPressedMoveEnd: (e) => saveDrawings(e.overlay.groupId),
    onRemoved: (e) => saveDrawings(e.overlay.groupId),
    onSelected: (e) => { selected.current = e.overlay.id },
    onDeselected: (e) => { if (selected.current === e.overlay.id) selected.current = null },
    onDoubleClick: (e) => { if (e.overlay.name === 'textLabel') cbs.current.onTextEdit(e.overlay.id, e.overlay.extendData?.text) },
  }), [saveDrawings])

  const indicatorList = () => (chart.current ? chart.current.getIndicators().map((i) => ({ id: i.id, name: i.name, paneId: i.paneId, calcParams: i.calcParams })) : [])
  const persistInds = () => cbs.current.onLayout({ indicators: indicatorList().map((i) => ({ name: i.name, pane: i.paneId === MAIN ? 'main' : 'sub', calcParams: i.calcParams })) })

  // ----- the chart -----
  useEffect(() => {
    const c = init(host.current, { timezone: 'Asia/Kolkata', locale: 'en-US', styles: isDark() ? 'dark' : 'light' })
    chart.current = c
    if (id === 0) window.__researchChart = c
    c.setStyles(chartStyles(layout.chartType || 'candle_solid'))
    for (const i of layout.indicators || []) {
      const v = { name: i.name, ...(i.calcParams ? { calcParams: i.calcParams } : {}) }
      if (i.pane === 'main') c.createIndicator({ ...v, paneId: MAIN }, true)
      else c.createIndicator(v)
    }
    c.setDataLoader({
      getBars: async ({ type, timestamp, symbol: s, period, callback }) => {
        if (type === 'backward') { callback([], { backward: false }); return }
        const t = tfOf(period)
        if (type === 'init' && clock.active) {
          try { await clock.initPane(id, s.ticker, t, period, callback); setErr(null) } catch (e) { setErr(e.message); callback([], false) }
          return
        }
        try {
          const r = await api.get(`/api/research/bars?symbol=${encodeURIComponent(s.ticker)}&tf=${t}&limit=${type === 'init' ? 1500 : 800}${type === 'forward' && timestamp ? `&before=${timestamp}` : ''}`)
          const cs = c.getSymbol()
          const cp = c.getPeriod()
          if (!cs || cs.ticker !== s.ticker || !cp || cp.type !== period.type || cp.span !== period.span) return
          if (type === 'init') cbs.current.onInfo(id, { first: r.first_stored })
          callback(r.bars, { forward: r.more, backward: false })
          setErr(null)
        } catch (e) {
          setErr(e.message)
          callback([], false)
        }
      },
      subscribeBar: ({ symbol: s, period, callback }) => { sub.current = { sym: s.ticker, tf: tfOf(period), callback } },
      unsubscribeBar: () => { sub.current = null },
    })
    clock.register(id, { symbol: () => symRef.current, tf: () => tfRef.current, sub: () => sub.current, chart: () => chart.current,
      reload: () => chart.current && chart.current.resetData() })
    const ro = new ResizeObserver(() => c.resize())
    ro.observe(host.current)
    const mq = window.matchMedia ? window.matchMedia('(prefers-color-scheme: dark)') : null
    const onTheme = () => { c.setStyles(isDark() ? 'dark' : 'light'); c.setStyles(chartStyles(c.getStyles().candle.type)) }
    mq && mq.addEventListener('change', onTheme)
    return () => {
      flushSaves(); clock.unregister(id); ro.disconnect(); mq && mq.removeEventListener('change', onTheme)
      dispose(host.current); chart.current = null
    }
  }, [])

  // stock / timeframe
  useEffect(() => {
    const c = chart.current
    if (!c) return
    if (clock.active) {
      const p = clock.panes.get(id)
      if (p && (!p.feed || p.feed.symbol !== symbol.symbol)) clock.resetPane(id)
    }
    c.setSymbol({ ticker: symbol.symbol, pricePrecision: symbol.tick && symbol.tick < 0.05 ? 4 : 2, volumePrecision: 0 })
    c.setPeriod(TFS.find(([k]) => k === tf)[1])
  }, [symbol, tf])

  // that stock's drawings
  useEffect(() => {
    const c = chart.current
    if (!c) return undefined
    let stale = false
    const sym = symbol.symbol
    flushSaves()
    quiet.current = true
    for (const o of c.getOverlays()) if (o.groupId !== 'trade' && !String(o.groupId).startsWith('agent')) c.removeOverlay({ id: o.id })
    drawing.current = null
    selected.current = null
    api.get(`/api/research/layout/${encodeURIComponent(`sym:${sym}`)}`).then((r) => {
      if (stale) return
      for (const o of (r.data && r.data.overlays) || []) {
        c.createOverlay({ ...o, groupId: sym, styles: o.styles || undefined, extendData: o.extendData ?? undefined, ...events })
      }
    }).catch(() => {}).finally(() => { if (!stale) quiet.current = false })
    return () => { stale = true }
  }, [symbol.symbol, events, flushSaves])

  // live prices move the last bar (not during a replay)
  useEffect(() => onPrice(symbol.symbol, (p) => {
    const s = sub.current
    if (!s || s.sym !== symbol.symbol || !p || clock.active) return
    const list = chart.current ? chart.current.getDataList() : []
    const last = list[list.length - 1]
    const start = bucketStart(s.tf, Date.now())
    if (!last || start == null || start < last.timestamp) return
    const ltp = p.ltp
    let bar
    if (s.tf === '1D') bar = { timestamp: start, open: p.open || ltp, high: Math.max(p.high || ltp, ltp), low: Math.min(p.low || ltp, ltp), close: ltp, volume: start === last.timestamp ? last.volume : 0 }
    else if (start === last.timestamp) bar = { ...last, high: Math.max(last.high, ltp, s.tf.match(/W|M/) ? p.high || ltp : ltp), low: Math.min(last.low, ltp, s.tf.match(/W|M/) ? p.low || ltp : ltp), close: ltp }
    else bar = { timestamp: start, open: ltp, high: ltp, low: ltp, close: ltp, volume: 0 }
    s.callback(bar)
  }), [symbol.symbol])

  // ----- the agent's signals and trades -----
  useEffect(() => {
    const c = chart.current
    if (!c) return undefined
    let stale = false
    c.removeOverlay({ groupId: 'agent' })
    agentIds.current = []
    if (!agentOn) return undefined
    api.get(`/api/research/agent?symbol=${encodeURIComponent(symbol.symbol)}`).then((r) => {
      if (stale || !chart.current) return
      const add = (o, t) => {
        const oid = c.createOverlay({ ...o, groupId: 'agent', lock: true })
        if (oid) agentIds.current.push({ id: oid, t })
      }
      for (const s of r.signals) {
        const t = s.candle_at ? Date.parse(`${s.candle_at}:00+05:30`) : dayMs(s.date)
        add({ name: 'agentMark', points: [{ timestamp: t, value: s.entry_trigger }],
          extendData: { dir: 'up', color: s.eligible ? '#2a78d6' : '#9a9993', text: `Signal ${s.entry_trigger}${s.eligible ? '' : ' (blocked)'}` } }, t)
      }
      const now = Date.now()
      for (const tr of r.trades) {
        const t0 = isoMs(tr.entry_at)
        const t1 = isoMs(tr.closed_at) || now
        add({ name: 'agentMark', points: [{ timestamp: t0, value: tr.entry_price }], extendData: { dir: 'up', text: `${tr.account_id} buy ${tr.qty} @ ${tr.entry_price}` } }, t0)
        for (const e of tr.exits) {
          const te = isoMs(e.at)
          if (te) add({ name: 'agentMark', points: [{ timestamp: te, value: e.price }], extendData: { dir: 'down', text: `${tr.account_id} sell ${e.qty} @ ${e.price} · ${e.reason}` } }, te)
        }
        if (tr.initial_stop) add({ name: 'agentLevel', points: [{ timestamp: t0, value: tr.initial_stop }, { timestamp: t1, value: tr.initial_stop }], extendData: { color: '#e34948', dashed: true, text: 'initial stop' } }, t0)
        // the current stop and target are today's values: in a replay they show only once the trade has closed
        if (tr.stop && tr.stop !== tr.initial_stop) add({ name: 'agentLevel', points: [{ timestamp: t0, value: tr.stop }, { timestamp: t1, value: tr.stop }], extendData: { color: '#e34948', text: `stop ${tr.stop}` } }, t1)
        if (tr.target) add({ name: 'agentLevel', points: [{ timestamp: t0, value: tr.target }, { timestamp: t1, value: tr.target }], extendData: { color: '#1baf7a', dashed: !!tr.closed_at, text: `target ${tr.target}` } }, t1)
      }
    }).catch(() => {})
    return () => { stale = true }
  }, [agentOn, symbol.symbol])

  // during a replay, nothing the agent did after the replay time is shown
  useEffect(() => {
    const c = chart.current
    if (!c) return
    for (const a of agentIds.current) c.overrideOverlay({ id: a.id, visible: replayT == null || a.t <= replayT })
  }, [replayT, agentOn])

  // ----- replay trading lines -----
  useEffect(() => {
    const c = chart.current
    if (!c) return
    c.removeOverlay({ groupId: 'trade' })
    const last = c.getDataList().slice(-1)[0]
    const t = last ? last.timestamp : Date.now()
    for (const l of levels || []) c.createOverlay({ name: 'levelLine', groupId: 'trade', lock: true, points: [{ timestamp: t, value: l.price }], extendData: l })
  }, [JSON.stringify(levels || [])])

  // ----- what the toolbar asks of the active chart -----
  useImperativeHandle(ref, () => ({
    chart: () => chart.current,
    startTool(name, magnet, extendData) {
      const c = chart.current
      if (!c) return
      if (drawing.current) c.removeOverlay({ id: drawing.current })
      drawing.current = c.createOverlay({ name, groupId: symRef.current, mode: magnet ? 'weak_magnet' : 'normal', lock: false, ...(extendData ? { extendData } : {}), ...events })
    },
    cancelTool() { if (drawing.current && chart.current) { chart.current.removeOverlay({ id: drawing.current }); drawing.current = null; return true } return false },
    removeSelected() { if (selected.current && chart.current) { chart.current.removeOverlay({ id: selected.current }); selected.current = null } },
    editText(oid, text) { chart.current.overrideOverlay({ id: oid, extendData: { text } }); saveDrawings() },
    forDrawings(fn) { for (const o of chart.current.getOverlays({ groupId: symRef.current })) fn(chart.current, o); saveDrawings() },
    removeAll() { chart.current.removeOverlay({ groupId: symRef.current }); saveDrawings() },
    indicators: indicatorList,
    addIndicator(name, where) {
      const c = chart.current
      if (where === 'main') { if (c.getIndicators({ name, paneId: MAIN }).length) return; c.createIndicator({ name, paneId: MAIN }, true) } else c.createIndicator(name)
      persistInds()
    },
    removeIndicator(ind) { chart.current.removeIndicator({ paneId: ind.paneId, name: ind.name }); persistInds() },
    setIndicatorParams(ind, calcParams) { chart.current.overrideIndicator({ name: ind.name, paneId: ind.paneId, calcParams }); persistInds() },
    setChartType(t) { chart.current.setStyles(chartStyles(t)); cbs.current.onLayout({ chartType: t }) },
    screenshot() {
      const url = chart.current.getConvertPictureUrl(true, 'png', css('--surface') || '#ffffff')
      const a = document.createElement('a')
      a.href = url
      a.download = `${symRef.current}-${tfRef.current}-${new Date().toISOString().slice(0, 16).replace(':', '')}.png`
      a.click()
    },
  }))

  const pickAt = (e) => {
    const c = chart.current
    const rect = host.current.getBoundingClientRect()
    const pt = c.convertFromPixel([{ x: e.clientX - rect.left, y: e.clientY - rect.top }], { paneId: MAIN })
    const p = Array.isArray(pt) ? pt[0] : pt
    const list = c.getDataList()
    let ts = p && p.timestamp
    if (ts == null && p && p.dataIndex != null && list.length) ts = list[Math.max(0, Math.min(list.length - 1, p.dataIndex))].timestamp
    if (ts != null) onPicked(nextBucket(tf, ts))
  }

  return (
    <div className={`rs-pane${active ? ' active' : ''}`} onMouseDownCapture={onActivate}>
      {showLabel && <div className="rs-pane-label">{symbol.symbol} · {tf}</div>}
      <div className="rs-chart" ref={host}
        onMouseDown={(e) => { downAt.current = [e.clientX, e.clientY] }}
        onMouseMove={picking ? (e) => setPickX(e.clientX - host.current.getBoundingClientRect().left) : undefined}
        onMouseLeave={() => setPickX(null)}
        onMouseUp={(e) => {
          const d = downAt.current
          if (picking && d && Math.hypot(e.clientX - d[0], e.clientY - d[1]) < 5) pickAt(e)
        }} />
      {picking && (
        <div className="rs-pick" aria-hidden="true">
          {pickX != null && <div className="rs-pick-line" style={{ left: pickX }} />}
          {active && <div className="rs-pick-hint">Click a bar: it becomes the last bar shown. Drag or scroll to go further back.</div>}
        </div>
      )}
      {err && <div className="rs-error" role="alert">{err}</div>}
    </div>
  )
})

export default ChartPane
