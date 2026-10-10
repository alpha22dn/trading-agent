import { useEffect, useMemo, useRef, useState } from 'react'
import { CandlestickSeries, ColorType, CrosshairMode, LineSeries, LineStyle, createChart, createSeriesMarkers } from 'lightweight-charts'
import { api, onPrice } from './api.js'

// Colours come from CSS variables so the chart follows light/dark mode.
function palette() {
  const css = getComputedStyle(document.documentElement)
  const v = (name) => css.getPropertyValue(name).trim()
  return {
    bg: v('--surface'), text: v('--text-2'), grid: v('--border'),
    up: v('--series-up'), down: v('--series-down'), bb: v('--muted'),
    entry: v('--accent'), stop: v('--critical'), target: v('--good'), muted: v('--muted'),
  }
}

const TIMEFRAMES = [['1m', '1m'], ['5m', '5m'], ['15m', '15m'], ['1h', '1h'], ['4h', '4h'], ['1D', '1D']]
const IST_OFFSET = 19800
const LIVE_MS = 60000          // full refetch (volume, corrections); pushed prices move the candle in between
const TF_MIN = { '1m': 1, '5m': 5, '15m': 15, '1h': 60, '4h': 240 }
let lastTf = '1D' // remembered while the dashboard is open

// Levels drawn as labelled price lines. kind decides the colour and dash.
function lineStyle(kind, p) {
  switch (kind) {
    case 'entry': return [p.entry, LineStyle.Dashed]
    case 'limit': return [p.entry, LineStyle.Dotted]
    case 'stop': return [p.stop, LineStyle.Dashed]
    case 'initial_stop': return [p.stop, LineStyle.Dotted]
    case 'target': return [p.target, LineStyle.Dashed]
    case 'target_hit': return [p.target, LineStyle.Dotted]
    default: return [p.muted, LineStyle.Dashed]
  }
}

// Seconds on the chart's axis (IST wall clock) for an ISO time or a YYYY-MM-DD date.
function chartSeconds(iso) {
  if (iso.length === 10) return Date.parse(iso + 'T00:00:00Z') / 1000
  return Math.floor(Date.parse(iso) / 1000) + IST_OFFSET
}

// Candles with entry / limit / stop / target as labelled price lines, and markers (signal, buy, sells).
// Up candles are hollow and down candles filled, so direction never relies on colour alone.
// `signal` (Signals screen) is turned into levels; `levels` and `markers` can be passed directly.
// `live`: the current candle moves with each pushed price; a full refetch every 60 s keeps volume and stops current.
// focus: {from, to} ISO dates to open the chart on (a closed trade in the Journal), with some bars either side.
export default function CandleChart({ symbol, signal, levels, markers, live = false, height = 360, focus }) {
  const box = useRef(null)
  const chartRef = useRef(null)
  const levelPrices = useRef([]) // kept in view by the price scale, so a far-off target is not cut off
  const [tf, setTfState] = useState(lastTf)
  const setTf = (v) => { lastTf = v; setTfState(v) }
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const [scheme, setScheme] = useState(0)
  const daily = tf === '1D'

  const lv = useMemo(() => levels || (signal ? [
    { price: signal.entry_trigger, title: 'Entry', kind: 'entry' },
    { price: signal.entry_limit, title: 'Limit', kind: 'limit' },
    { price: signal.stop, title: 'Stop', kind: 'stop' },
    { price: signal.target, title: 'Target', kind: 'target' },
  ] : []), [JSON.stringify(levels), signal && signal.id, signal && signal.stop, signal && signal.entry_trigger])
  const mk = useMemo(() => markers || (signal && signal.date ? [{ at: signal.date, text: 'Signal', kind: 'signal' }] : []),
    [JSON.stringify(markers), signal && signal.date])

  // ---- data ----
  useEffect(() => {
    if (!symbol) return undefined
    setData(null); setError(null)
    let cancelled = false
    const url = () => {
      if (!daily) return `/api/candles/${encodeURIComponent(symbol)}/intraday?tf=${tf}`
      let until = ''
      if (signal && signal.date && !live) { // a historical signal: show ~2 months after it
        const d = new Date(signal.date + 'T00:00:00'); d.setDate(d.getDate() + 60)
        until = `&until=${d.toISOString().slice(0, 10)}`
      }
      return `/api/candles/${encodeURIComponent(symbol)}?days=170${until}${live ? '&live=1' : ''}`
    }
    const load = (first) => api.get(url())
      .then((r) => { if (!cancelled) { setData({ ...r, tf }); setError(null) } })
      .catch((e) => { if (!cancelled && first) setError(e.message) })
    load(true)
    const timer = live ? setInterval(() => load(false), LIVE_MS) : null
    return () => { cancelled = true; if (timer) clearInterval(timer) }
  }, [symbol, signal && signal.date, tf, live])

  useEffect(() => {
    const mq = window.matchMedia('(prefers-color-scheme: dark)')
    const on = () => setScheme((s) => s + 1)
    mq.addEventListener('change', on)
    return () => mq.removeEventListener('change', on)
  }, [])

  // ---- chart instance (rebuilt only for a new stock, timeframe or colour scheme) ----
  useEffect(() => {
    if (!box.current) return undefined
    const p = palette()
    const chart = createChart(box.current, {
      height,
      layout: { background: { type: ColorType.Solid, color: p.bg }, textColor: p.text, fontFamily: 'inherit', attributionLogo: false },
      grid: { vertLines: { visible: false }, horzLines: { color: p.grid } },
      rightPriceScale: { borderVisible: false },
      timeScale: { borderVisible: false, timeVisible: !daily, secondsVisible: false },
      crosshair: { mode: CrosshairMode.Normal },
      localization: { locale: 'en-IN' },
    })
    const candles = chart.addSeries(CandlestickSeries, {
      upColor: 'rgba(0,0,0,0)', borderUpColor: p.up, wickUpColor: p.up,
      downColor: p.down, borderDownColor: p.down, wickDownColor: p.down,
      priceLineVisible: false,
      autoscaleInfoProvider: (original) => {
        const r = original()
        if (!r || !r.priceRange) return r
        // Daily: every level stays in view. Intraday: only levels near the bars, so a far target
        // doesn't flatten the candles (its line is still drawn when you scroll or zoom out).
        const { minValue: lo, maxValue: hi } = r.priceRange
        const ps = levelPrices.current.filter((x) => daily || (x > lo * 0.88 && x < hi * 1.12))
        if (!ps.length) return r
        return { ...r, priceRange: { minValue: Math.min(r.priceRange.minValue, ...ps), maxValue: Math.max(r.priceRange.maxValue, ...ps) } }
      },
    })
    const bb = daily ? chart.addSeries(LineSeries, { color: p.bb, lineWidth: 1, lineStyle: LineStyle.Dotted, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false, title: 'BB upper' }) : null
    const ro = new ResizeObserver(() => chart.applyOptions({ width: box.current.clientWidth }))
    ro.observe(box.current)
    chartRef.current = { chart, candles, bb, p, lines: [], markers: null, fitted: false, tf }
    return () => { ro.disconnect(); chart.remove(); chartRef.current = null }
  }, [symbol, tf, scheme, height])

  // ---- bars ----
  const times = useMemo(() => (data && data.tf === tf ? data.candles.map((c) => (daily ? c.date : c.time)) : []), [data, tf])
  const lastBar = useRef(null)
  useEffect(() => {
    const c = chartRef.current
    if (!c || !data || data.tf !== c.tf) return
    const bars = data.candles.map((b, i) => ({ time: times[i], open: b.open, high: b.high, low: b.low, close: b.close }))
    c.candles.setData(bars)
    lastBar.current = bars.length ? { ...bars[bars.length - 1] } : null
    if (c.bb) c.bb.setData(data.candles.filter((b) => b.bb_upper != null).map((b) => ({ time: b.date, value: b.bb_upper })))
    if (!c.fitted) {
      c.chart.applyOptions({ width: box.current.clientWidth }) // the box was hidden while loading
      const n = data.candles.length
      const day = (i) => (daily ? data.candles[i].date : new Date(times[i] * 1000).toISOString().slice(0, 10))
      let a = -1
      let b = -1
      if (focus && n) {
        for (let i = 0; i < n; i++) { const d = day(i); if (a < 0 && d >= focus.from) a = i; if (d <= focus.to) b = i }
      }
      if (a >= 0 && b >= a) {
        const pad = daily ? 25 : Math.max(20, Math.round((b - a) * 0.3))
        c.chart.timeScale().setVisibleLogicalRange({ from: Math.max(0, a - pad), to: Math.min(n + 2, b + pad) })
      } else if (daily || n < 150) c.chart.timeScale().fitContent()
      else c.chart.timeScale().setVisibleLogicalRange({ from: n - 150, to: n + 2 })
      c.fitted = true
    }
  }, [data, times])

  // ---- live: each pushed price moves the current candle (about once a second while Kite streams) ----
  useEffect(() => {
    if (!symbol || !live) return undefined
    return onPrice(symbol, (q) => {
      const c = chartRef.current
      if (!c || !data || data.tf !== c.tf || !q || !q.ltp) return
      const last = lastBar.current
      let bar
      if (daily) {
        const today = new Date(Date.now() + IST_OFFSET * 1000).toISOString().slice(0, 10)
        bar = { time: today, open: q.open || (last && last.time === today ? last.open : q.ltp),
                high: Math.max(q.high || q.ltp, q.ltp), low: Math.min(q.low || q.ltp, q.ltp), close: q.ltp }
        if (last && last.time > today) return
      } else {
        const mins = TF_MIN[tf] || 1
        const ist = Math.floor(Date.now() / 1000) + IST_OFFSET
        const dayStart = Math.floor(ist / 86400) * 86400 + 9 * 3600 + 15 * 60
        if (ist < dayStart) return
        const bucket = dayStart + Math.floor((ist - dayStart) / (mins * 60)) * mins * 60
        if (last && last.time === bucket) bar = { ...last, high: Math.max(last.high, q.ltp), low: Math.min(last.low, q.ltp), close: q.ltp }
        else if (!last || bucket > last.time) bar = { time: bucket, open: q.ltp, high: q.ltp, low: q.ltp, close: q.ltp }
        else return
      }
      try { c.candles.update(bar); lastBar.current = bar } catch { /* out-of-order: the next refetch fixes it */ }
    })
  }, [symbol, tf, live, data, daily])

  // ---- levels and markers ----
  useEffect(() => {
    const c = chartRef.current
    if (!c || !data || data.tf !== c.tf) return
    c.lines.forEach((l) => c.candles.removePriceLine(l))
    levelPrices.current = lv.filter((l) => l.price != null).map((l) => l.price)
    c.lines = lv.filter((l) => l.price != null).map((l) => {
      const [color, style] = lineStyle(l.kind, c.p)
      return c.candles.createPriceLine({ price: l.price, color, title: l.title, lineWidth: 2, lineStyle: style, axisLabelVisible: true })
    })
    // Each marker goes on the bar it falls in (the last bar starting at or before it).
    const snapped = []
    for (const m of mk) {
      // A date-only marker (the signal day) sits on that session's last bar on intraday charts.
      const s = chartSeconds(!daily && m.at.length === 10 ? `${m.at}T15:29:00+05:30` : m.at)
      let t = null
      for (const x of times) { if (x === s || (typeof x === 'number' ? x <= s : chartSeconds(x) <= s)) t = x; else break }
      if (t == null || (!daily && s - (typeof t === 'number' ? t : 0) > 86400)) continue // outside the loaded bars
      const buy = m.kind === 'buy' || m.kind === 'signal'
      snapped.push({ time: t, position: buy ? 'belowBar' : 'aboveBar', color: m.kind === 'sell' ? c.p.stop : c.p.entry,
        shape: buy ? 'arrowUp' : 'arrowDown', text: m.text })
    }
    snapped.sort((a, b) => (a.time < b.time ? -1 : a.time > b.time ? 1 : 0))
    if (c.markers) c.markers.setMarkers(snapped)
    else c.markers = createSeriesMarkers(c.candles, snapped)
  }, [lv, mk, data, times])

  if (!symbol) return null
  const kinds = new Set(lv.map((l) => l.kind))
  return (
    <div>
      <div className="chart-bar">
        <div className="tabs tabs-sm" role="tablist" aria-label="Timeframe">
          {TIMEFRAMES.map(([id, label]) => (
            <button key={id} role="tab" aria-selected={tf === id} className={tf === id ? 'on' : ''} onClick={() => setTf(id)}>{label}</button>
          ))}
        </div>
        <span className="fine">{live ? 'Live · moves with each streamed price' : daily ? '' : 'From Kite'}{!daily ? ' · exchange time (IST)' : ''}</span>
      </div>
      {error && <p className="muted">Chart unavailable: {error}</p>}
      {!data && !error && <div className="chart-skeleton" style={{ height }} />}
      <div ref={box} style={{ width: '100%', display: data ? 'block' : 'none' }} aria-label={`${symbol} ${tf} candles with entry, stop and target lines`} role="img" />
      <div className="chart-legend fine">
        <span><i className="lg lg-up" /> Up {daily ? 'day' : 'bar'} (hollow)</span>
        <span><i className="lg lg-down" /> Down {daily ? 'day' : 'bar'}</span>
        {daily && <span><i className="lg lg-bb" /> Bollinger upper (20, 2)</span>}
        {(kinds.has('entry') || kinds.has('limit')) && <span><i className="lg lg-entry" /> Entry{kinds.has('limit') ? ' / limit' : ''}</span>}
        {kinds.has('stop') && <span><i className="lg lg-stop" /> Stop{kinds.has('initial_stop') ? ' (dotted: initial)' : ''}</span>}
        {(kinds.has('target') || kinds.has('target_hit')) && <span><i className="lg lg-target" /> Target</span>}
      </div>
    </div>
  )
}
