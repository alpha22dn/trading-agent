// Extensions to KLineChart (Apache 2.0): extra indicators and drawing tools for the Research tab.
// Registered once, on first import.
import { registerIndicator, registerOverlay } from 'klinecharts'

// ---------- helpers ----------

const tr = (d, prev) => (prev ? Math.max(d.high - d.low, Math.abs(d.high - prev.close), Math.abs(d.low - prev.close)) : d.high - d.low)

function wilderAtr(list, n) {
  const out = []
  let atr = null
  let sum = 0
  list.forEach((d, i) => {
    const t = tr(d, list[i - 1])
    if (i < n) {
      sum += t
      if (i === n - 1) atr = sum / n
    } else atr = (atr * (n - 1) + t) / n
    out.push(i >= n - 1 ? atr : null)
  })
  return out
}

function emaSeries(values, n) {
  const k = 2 / (n + 1)
  const out = []
  let cur = null
  let sum = 0
  values.forEach((v, i) => {
    if (i < n) {
      sum += v
      if (i === n - 1) cur = sum / n
    } else cur = v * k + cur * (1 - k)
    out.push(i >= n - 1 ? cur : null)
  })
  return out
}

const istDay = (ts) => Math.floor((ts + 19800000) / 86400000)

// ---------- indicators ----------

registerIndicator({
  name: 'VWAP',
  shortName: 'VWAP',
  series: 'price',
  precision: 2,
  calcParams: [],
  figures: [{ key: 'vwap', title: 'VWAP: ', type: 'line' }],
  // Session VWAP: resets every trading day (typical price × volume).
  calc: (list) => {
    let day = null
    let pv = 0
    let vol = 0
    return list.map((d) => {
      const k = istDay(d.timestamp)
      if (k !== day) { day = k; pv = 0; vol = 0 }
      pv += ((d.high + d.low + d.close) / 3) * (d.volume || 0)
      vol += d.volume || 0
      return { vwap: vol ? pv / vol : null }
    })
  },
})

registerIndicator({
  name: 'ATR',
  shortName: 'ATR',
  precision: 2,
  calcParams: [14],
  figures: [{ key: 'atr', title: 'ATR: ', type: 'line' }],
  calc: (list, ind) => wilderAtr(list, ind.calcParams[0]).map((v) => ({ atr: v })),
})

registerIndicator({
  name: 'SUPERTREND',
  shortName: 'Supertrend',
  series: 'price',
  precision: 2,
  calcParams: [10, 3],
  figures: [
    { key: 'up', title: 'Up: ', type: 'line', styles: () => ({ color: '#1baf7a' }) },
    { key: 'down', title: 'Down: ', type: 'line', styles: () => ({ color: '#e34948' }) },
  ],
  calc: (list, ind) => {
    const [n, mult] = ind.calcParams
    const atr = wilderAtr(list, n)
    let fu = null
    let fl = null
    let trend = 1
    return list.map((d, i) => {
      if (atr[i] == null) return {}
      const mid = (d.high + d.low) / 2
      const bu = mid + mult * atr[i]
      const bl = mid - mult * atr[i]
      const pc = list[i - 1]?.close
      fu = fu == null || bu < fu || pc > fu ? bu : fu
      fl = fl == null || bl > fl || pc < fl ? bl : fl
      if (trend === 1 && d.close < fl) trend = -1
      else if (trend === -1 && d.close > fu) trend = 1
      return trend === 1 ? { up: fl } : { down: fu }
    })
  },
})

registerIndicator({
  name: 'DONCHIAN',
  shortName: 'DC',
  series: 'price',
  precision: 2,
  calcParams: [20],
  figures: [
    { key: 'upper', title: 'Upper: ', type: 'line' },
    { key: 'mid', title: 'Mid: ', type: 'line' },
    { key: 'lower', title: 'Lower: ', type: 'line' },
  ],
  calc: (list, ind) => {
    const n = ind.calcParams[0]
    return list.map((d, i) => {
      if (i < n - 1) return {}
      const w = list.slice(i - n + 1, i + 1)
      const upper = Math.max(...w.map((x) => x.high))
      const lower = Math.min(...w.map((x) => x.low))
      return { upper, lower, mid: (upper + lower) / 2 }
    })
  },
})

registerIndicator({
  name: 'KELTNER',
  shortName: 'KC',
  series: 'price',
  precision: 2,
  calcParams: [20, 2, 10],
  figures: [
    { key: 'upper', title: 'Upper: ', type: 'line' },
    { key: 'mid', title: 'Mid: ', type: 'line' },
    { key: 'lower', title: 'Lower: ', type: 'line' },
  ],
  calc: (list, ind) => {
    const [n, mult, an] = ind.calcParams
    const mid = emaSeries(list.map((d) => d.close), n)
    const atr = wilderAtr(list, an)
    return list.map((d, i) => (mid[i] == null || atr[i] == null ? {} : { mid: mid[i], upper: mid[i] + mult * atr[i], lower: mid[i] - mult * atr[i] }))
  },
})

registerIndicator({
  name: 'STOCH',
  shortName: 'Stoch',
  precision: 2,
  calcParams: [14, 3, 3],
  minValue: 0,
  maxValue: 100,
  figures: [
    { key: 'k', title: '%K: ', type: 'line' },
    { key: 'd', title: '%D: ', type: 'line' },
  ],
  calc: (list, ind) => {
    const [n, ks, ds] = ind.calcParams
    const raw = list.map((d, i) => {
      if (i < n - 1) return null
      const w = list.slice(i - n + 1, i + 1)
      const hi = Math.max(...w.map((x) => x.high))
      const lo = Math.min(...w.map((x) => x.low))
      return hi === lo ? 50 : ((d.close - lo) / (hi - lo)) * 100
    })
    const sma = (arr, m) => arr.map((_, i) => {
      const w = arr.slice(Math.max(0, i - m + 1), i + 1)
      return w.length < m || w.some((x) => x == null) ? null : w.reduce((a, b) => a + b, 0) / m
    })
    const k = sma(raw, ks)
    const d = sma(k, ds)
    return list.map((_, i) => ({ k: k[i], d: d[i] }))
  },
})

registerIndicator({
  name: 'ADX',
  shortName: 'ADX',
  precision: 2,
  calcParams: [14],
  figures: [
    { key: 'adx', title: 'ADX: ', type: 'line' },
    { key: 'pdi', title: '+DI: ', type: 'line' },
    { key: 'mdi', title: '-DI: ', type: 'line' },
  ],
  // Wilder's ADX, the same definition the momentum strategy uses.
  calc: (list, ind) => {
    const n = ind.calcParams[0]
    let atr = 0, pdm = 0, mdm = 0, adx = null
    const dxs = []
    return list.map((d, i) => {
      if (i === 0) return {}
      const p = list[i - 1]
      const up = d.high - p.high
      const dn = p.low - d.low
      const plus = up > dn && up > 0 ? up : 0
      const minus = dn > up && dn > 0 ? dn : 0
      const t = tr(d, p)
      if (i <= n) {
        atr += t; pdm += plus; mdm += minus
        if (i < n) return {}
      } else {
        atr = atr - atr / n + t
        pdm = pdm - pdm / n + plus
        mdm = mdm - mdm / n + minus
      }
      const pdi = atr ? (100 * pdm) / atr : 0
      const mdi = atr ? (100 * mdm) / atr : 0
      const dx = pdi + mdi ? (100 * Math.abs(pdi - mdi)) / (pdi + mdi) : 0
      dxs.push(dx)
      if (dxs.length === n) adx = dxs.reduce((a, b) => a + b, 0) / n
      else if (dxs.length > n) adx = (adx * (n - 1) + dx) / n
      return { pdi, mdi, adx }
    })
  },
})

// ---------- drawing tools ----------

const fmt = (v) => (Math.abs(v) >= 100 ? v.toFixed(2) : v.toPrecision(4))

registerOverlay({
  name: 'rectangle',
  totalStep: 3,
  needDefaultPointFigure: true,
  needDefaultXAxisFigure: true,
  needDefaultYAxisFigure: true,
  createPointFigures: ({ coordinates }) => (coordinates.length < 2 ? [] : [{
    type: 'polygon',
    attrs: { coordinates: [coordinates[0], { x: coordinates[1].x, y: coordinates[0].y }, coordinates[1], { x: coordinates[0].x, y: coordinates[1].y }] },
    styles: { style: 'stroke_fill' },
  }]),
})

registerOverlay({
  name: 'circleShape',
  totalStep: 3,
  needDefaultPointFigure: true,
  needDefaultXAxisFigure: true,
  needDefaultYAxisFigure: true,
  createPointFigures: ({ coordinates }) => (coordinates.length < 2 ? [] : [{
    type: 'circle',
    attrs: { ...coordinates[0], r: Math.hypot(coordinates[1].x - coordinates[0].x, coordinates[1].y - coordinates[0].y) },
    styles: { style: 'stroke_fill' },
  }]),
})

registerOverlay({
  name: 'triangle',
  totalStep: 4,
  needDefaultPointFigure: true,
  needDefaultXAxisFigure: true,
  needDefaultYAxisFigure: true,
  createPointFigures: ({ coordinates }) => [{ type: coordinates.length < 3 ? 'line' : 'polygon', attrs: { coordinates }, styles: { style: 'stroke_fill' } }],
})

registerOverlay({
  name: 'arrowLine',
  totalStep: 3,
  needDefaultPointFigure: true,
  needDefaultXAxisFigure: true,
  needDefaultYAxisFigure: true,
  createPointFigures: ({ coordinates }) => {
    if (coordinates.length < 2) return []
    const [a, b] = coordinates
    const ang = Math.atan2(b.y - a.y, b.x - a.x)
    const L = 12
    const head = [-0.45, 0.45].map((d) => ({ x: b.x - L * Math.cos(ang + d), y: b.y - L * Math.sin(ang + d) }))
    return [
      { type: 'line', attrs: { coordinates: [a, b] } },
      { type: 'polygon', attrs: { coordinates: [head[0], b, head[1]] }, styles: { style: 'fill' } },
    ]
  },
})

registerOverlay({
  name: 'textLabel',
  totalStep: 2,
  needDefaultPointFigure: false,
  needDefaultXAxisFigure: true,
  needDefaultYAxisFigure: true,
  createPointFigures: ({ coordinates, overlay }) => [{
    type: 'text',
    attrs: { x: coordinates[0].x, y: coordinates[0].y, text: overlay.extendData?.text || 'Text', align: 'left', baseline: 'bottom' },
  }],
})

registerOverlay({
  name: 'fibExtension',
  totalStep: 4,
  needDefaultPointFigure: true,
  needDefaultXAxisFigure: true,
  needDefaultYAxisFigure: true,
  createPointFigures: ({ coordinates, overlay, bounding }) => {
    const pts = overlay.points
    const figs = [{ type: 'line', attrs: { coordinates: coordinates.slice(0, 3) }, styles: { style: 'dashed' } }]
    if (coordinates.length < 3 || pts.length < 3 || pts.some((p) => p.value == null)) return figs
    const move = pts[1].value - pts[0].value
    const dy = coordinates[1].y - coordinates[0].y
    const x0 = coordinates[2].x
    for (const lv of [0, 0.382, 0.618, 1, 1.272, 1.618, 2, 2.618]) {
      const y = coordinates[2].y + dy * lv
      figs.push({ type: 'line', attrs: { coordinates: [{ x: x0, y }, { x: bounding.width, y }] } })
      figs.push({ type: 'text', ignoreEvent: true, attrs: { x: x0, y, text: `${lv} (${fmt(pts[2].value + move * lv)})`, baseline: 'bottom' } })
    }
    return figs
  },
})

function positionTool(name, long) {
  registerOverlay({
    name,
    totalStep: 4,
    needDefaultPointFigure: true,
    needDefaultXAxisFigure: true,
    needDefaultYAxisFigure: true,
    // Clicks: entry, stop, target. The zones run from the entry to the target's time (at least 60 px).
    createPointFigures: ({ coordinates, overlay }) => {
      const pts = overlay.points
      if (coordinates.length < 2) return []
      const [e, s, t] = coordinates
      const right = Math.max((t || s).x, e.x + 60)
      const figs = [
        { type: 'rect', attrs: { x: e.x, y: Math.min(e.y, s.y), width: right - e.x, height: Math.abs(s.y - e.y) }, styles: { style: 'fill', color: 'rgba(227,73,72,0.18)' } },
      ]
      if (t) figs.push({ type: 'rect', attrs: { x: e.x, y: Math.min(e.y, t.y), width: right - e.x, height: Math.abs(t.y - e.y) }, styles: { style: 'fill', color: 'rgba(27,175,122,0.18)' } })
      figs.push({ type: 'line', attrs: { coordinates: [{ x: e.x, y: e.y }, { x: right, y: e.y }] } })
      if (pts.length >= 3 && pts.every((p) => p.value != null)) {
        const [pe, ps, pt] = pts.map((p) => p.value)
        const risk = long ? pe - ps : ps - pe
        const reward = long ? pt - pe : pe - pt
        const rr = risk > 0 ? reward / risk : 0
        figs.push({ type: 'text', ignoreEvent: true, attrs: { x: e.x + 4, y: t.y, text: `Target ${fmt(pt)} (${((reward / pe) * 100).toFixed(2)}%)`, baseline: long ? 'top' : 'bottom' } })
        figs.push({ type: 'text', ignoreEvent: true, attrs: { x: e.x + 4, y: s.y, text: `Stop ${fmt(ps)} (${((risk / pe) * 100).toFixed(2)}%)`, baseline: long ? 'bottom' : 'top' } })
        figs.push({ type: 'text', ignoreEvent: true, attrs: { x: e.x + 4, y: e.y, text: `${long ? 'Long' : 'Short'} ${fmt(pe)} · R:R ${rr.toFixed(2)}`, baseline: 'bottom' } })
      }
      return figs
    },
  })
}
positionTool('longPosition', true)
positionTool('shortPosition', false)

registerOverlay({
  name: 'priceRange',
  totalStep: 3,
  needDefaultPointFigure: true,
  needDefaultXAxisFigure: true,
  needDefaultYAxisFigure: true,
  createPointFigures: ({ coordinates, overlay }) => {
    if (coordinates.length < 2) return []
    const [a, b] = coordinates
    const [pa, pb] = overlay.points
    const figs = [{ type: 'rect', attrs: { x: Math.min(a.x, b.x), y: Math.min(a.y, b.y), width: Math.abs(b.x - a.x), height: Math.abs(b.y - a.y) }, styles: { style: 'fill', color: 'rgba(42,120,214,0.15)' } }]
    if (pa?.value != null && pb?.value != null) {
      const d = pb.value - pa.value
      const bars = pb.dataIndex != null && pa.dataIndex != null ? pb.dataIndex - pa.dataIndex : null
      figs.push({ type: 'text', ignoreEvent: true, attrs: { x: (a.x + b.x) / 2, y: Math.min(a.y, b.y) - 4, text: `${d >= 0 ? '+' : ''}${fmt(d)} (${((d / pa.value) * 100).toFixed(2)}%)${bars != null ? ` · ${bars} bars` : ''}`, align: 'center', baseline: 'bottom' } })
    }
    return figs
  },
})

// ---------- markers and levels drawn by the screen itself (not saved as your drawings) ----------

// One point: a small up/down triangle with a label (agent signals, buys and sells).
registerOverlay({
  name: 'agentMark',
  totalStep: 2,
  needDefaultPointFigure: false,
  needDefaultXAxisFigure: false,
  needDefaultYAxisFigure: false,
  createPointFigures: ({ coordinates, overlay }) => {
    const e = overlay.extendData || {}
    const { x, y } = coordinates[0]
    const up = e.dir !== 'down'
    const color = e.color || (up ? '#1baf7a' : '#e34948')
    const tip = up ? y + 6 : y - 6
    const base = up ? tip + 9 : tip - 9
    return [
      { type: 'polygon', ignoreEvent: true, attrs: { coordinates: [{ x, y: tip }, { x: x - 6, y: base }, { x: x + 6, y: base }] }, styles: { style: 'fill', color } },
      ...(e.text ? [{ type: 'text', ignoreEvent: true, attrs: { x, y: up ? base + 2 : base - 2, text: e.text, align: 'center', baseline: up ? 'top' : 'bottom' },
        styles: { color: '#ffffff', backgroundColor: color, size: 11, paddingLeft: 4, paddingRight: 4, paddingTop: 2, paddingBottom: 2, borderRadius: 3 } }] : []),
    ]
  },
})

// Two points: a horizontal level from the first point's time to the second's, labelled (agent stops/targets).
registerOverlay({
  name: 'agentLevel',
  totalStep: 3,
  needDefaultPointFigure: false,
  needDefaultXAxisFigure: false,
  needDefaultYAxisFigure: false,
  createPointFigures: ({ coordinates, overlay }) => {
    if (coordinates.length < 2) return []
    const e = overlay.extendData || {}
    const [a, b] = coordinates
    const color = e.color || '#7a7973'
    return [
      { type: 'line', ignoreEvent: true, attrs: { coordinates: [{ x: a.x, y: a.y }, { x: b.x, y: a.y }] }, styles: { color, style: e.dashed ? 'dashed' : 'solid', dashedValue: [4, 3] } },
      ...(e.text ? [{ type: 'text', ignoreEvent: true, attrs: { x: b.x, y: a.y, text: e.text, align: 'right', baseline: 'bottom' },
        styles: { color, backgroundColor: 'transparent', size: 11 } }] : []),
    ]
  },
})

// One point: a full-width price line with a label on the right (replay positions and orders).
registerOverlay({
  name: 'levelLine',
  totalStep: 2,
  needDefaultPointFigure: false,
  needDefaultXAxisFigure: false,
  needDefaultYAxisFigure: true,
  createPointFigures: ({ coordinates, overlay, bounding }) => {
    const e = overlay.extendData || {}
    const y = coordinates[0].y
    const color = e.color || '#2a78d6'
    return [
      { type: 'line', ignoreEvent: true, attrs: { coordinates: [{ x: 0, y }, { x: bounding.width, y }] }, styles: { color, style: e.dashed ? 'dashed' : 'solid', dashedValue: [5, 4] } },
      ...(e.text ? [{ type: 'text', ignoreEvent: true, attrs: { x: bounding.width - 6, y, text: e.text, align: 'right', baseline: 'middle' },
        styles: { color: '#ffffff', backgroundColor: color, size: 11, paddingLeft: 5, paddingRight: 5, paddingTop: 2, paddingBottom: 2, borderRadius: 3 } }] : []),
    ]
  },
})

// ---------- catalogues for the toolbars ----------

export const INDICATORS = [
  // name, label, where ('main' = on the price chart, 'sub' = own pane), default params
  ['MA', 'Moving average (MA)', 'main'], ['EMA', 'Exponential moving average (EMA)', 'main'], ['SMA', 'Smoothed moving average (SMA)', 'main'],
  ['BOLL', 'Bollinger Bands', 'main'], ['VWAP', 'VWAP (session)', 'main'], ['SUPERTREND', 'Supertrend', 'main'],
  ['DONCHIAN', 'Donchian channel', 'main'], ['KELTNER', 'Keltner channel', 'main'], ['SAR', 'Parabolic SAR', 'main'], ['BBI', 'Bull and bear index (BBI)', 'main'],
  ['VOL', 'Volume', 'sub'], ['RSI', 'RSI', 'sub'], ['MACD', 'MACD', 'sub'], ['ATR', 'ATR', 'sub'], ['ADX', 'ADX / DMI (Wilder)', 'sub'],
  ['STOCH', 'Stochastic', 'sub'], ['KDJ', 'KDJ', 'sub'], ['CCI', 'CCI', 'sub'], ['WR', 'Williams %R', 'sub'], ['OBV', 'On-balance volume', 'sub'],
  ['ROC', 'Rate of change', 'sub'], ['MTM', 'Momentum', 'sub'], ['TRIX', 'TRIX', 'sub'], ['BIAS', 'Bias', 'sub'], ['DMI', 'DMI (KLineChart)', 'sub'],
  ['AO', 'Awesome oscillator', 'sub'], ['PSY', 'Psychological line', 'sub'], ['VR', 'Volume ratio', 'sub'], ['EMV', 'Ease of movement', 'sub'],
  ['PVT', 'Price-volume trend', 'sub'], ['CR', 'CR', 'sub'], ['BRAR', 'BRAR', 'sub'], ['DMA', 'DMA', 'sub'], ['AVP', 'Average price', 'main'],
]

export const TOOLS = [
  { group: 'Lines', items: [
    ['segment', 'Trend line'], ['rayLine', 'Ray'], ['straightLine', 'Extended line'], ['arrowLine', 'Arrow'],
    ['horizontalStraightLine', 'Horizontal line'], ['horizontalRayLine', 'Horizontal ray'], ['verticalStraightLine', 'Vertical line'],
    ['priceLine', 'Price line'], ['parallelStraightLine', 'Parallel channel'], ['priceChannelLine', 'Price channel'],
  ] },
  { group: 'Fibonacci', items: [['fibonacciLine', 'Fib retracement'], ['fibExtension', 'Fib extension']] },
  { group: 'Shapes', items: [['rectangle', 'Rectangle'], ['circleShape', 'Circle'], ['triangle', 'Triangle'], ['brush', 'Brush'], ['textLabel', 'Text']] },
  { group: 'Measure', items: [['longPosition', 'Long position'], ['shortPosition', 'Short position'], ['priceRange', 'Measure']] },
]
