// One replay clock for every chart on screen (Research tab, phase 3).
//
// `T` (epoch ms) is the replay time: everything before it is on the charts, nothing after it. Each chart pane has
// its own feed of base bars (its stock's 1-minute bars, or daily bars before the 1-minute store begins) and shows
// the bar in progress second by second: recorded 1-second bars where they exist, otherwise the simulated
// open → low/high → close path. A minute always ends exactly as the stored minute. When no chart has data at `T`
// (overnight, weekends, holidays) the clock jumps to the next bar at no cost.
//
// Every revealed price is also handed to `onRows(symbol, rows)` so replay paper trading can fill orders on it.
import { api } from '../api.js'
import { DAY_MS, ReplayFeed, SecondsSource, TF_MIN, barSeconds, bucketStart, merge, nextBucket, subTicks } from './replay.js'

const SESSION_OPEN_MS = (9 * 60 + 15) * 60000
const DAY_SPAN_S = 22500

// Time span of a base bar: a minute, or a daily bar's session 09:15–15:30.
export const spanStart = (b) => (b.base === '1D' ? b.timestamp + SESSION_OPEN_MS : b.timestamp)
export const spanSec = (b) => (b.base === '1D' ? DAY_SPAN_S : 60)
export const spanEnd = (b) => spanStart(b) + spanSec(b) * 1000
const fullyBefore = (b, t) => spanEnd(b) <= t

export class ReplayClock {
  constructor({ onChange = () => {}, onRows = () => {} } = {}) {
    this.onChange = onChange
    this.onRows = onRows
    this.panes = new Map()       // id -> { api, feed, cur, secs }
    this.active = false
    this.playing = false
    this.T = null
    this.speed = 1
    this.unit = 'minute'
    this.gen = 0
    this.ended = false
    this.secMode = null
    this.note = null
  }

  // ----- panes -----
  register(id, paneApi) {
    this.panes.set(id, { api: paneApi, feed: null, cur: null, secs: null })
  }

  unregister(id) {
    this.panes.delete(id)
  }

  resetPane(id) {
    const p = this.panes.get(id)
    if (!p || this.T == null) return
    const sym = p.api.symbol()
    p.cur = null
    p.feed = new ReplayFeed(sym)
    p.feed.reset(this.T - (this.T % 60000))          // from the minute in progress
    p.secs = new SecondsSource(sym)
    p.done = false
  }

  // The pane's chart at the replay time: complete bars before the forming bar, and the forming bar from the base
  // bars fully before T. The bar in progress (if any) is played again from the next frame.
  async initPane(id, ticker, tf, period, callback) {
    const p = this.panes.get(id)
    if (!p) { callback([], false); return }
    if (p.cur) { p.feed.buffer.unshift(p.cur.bar); p.cur = null }
    if (!p.feed || p.feed.symbol !== ticker) this.resetPane(id)
    const T = this.T
    const start = bucketStart(tf, T) ?? T
    const [hist, part] = await Promise.all([
      api.get(`/api/research/bars?symbol=${encodeURIComponent(ticker)}&tf=${tf}&limit=1500&before=${start}`),
      start < T ? p.feed.range(start, T) : Promise.resolve([]),
    ])
    const c = p.api.chart()
    const cs = c && c.getSymbol()
    const cp = c && c.getPeriod()
    if (!this.active || !cs || cs.ticker !== ticker || !cp || cp.type !== period.type || cp.span !== period.span) return
    let bars = hist.bars
    let forming = null
    for (const b of part) {
      if (!fullyBefore(b, T)) continue
      const ts = b.base === '1D' && TF_MIN[tf] ? null : bucketStart(tf, b.timestamp)
      if (ts != null) forming = merge(forming, b, ts)
    }
    if (forming) bars = bars.concat([forming])
    this.note = TF_MIN[tf] && p.feed.minutesFrom && T < p.feed.minutesFrom
      ? `${ticker}: 1-minute data starts ${new Date(p.feed.minutesFrom + 19800000).toISOString().slice(0, 10)}; use 1D, 1W or 1M before that` : null
    callback(bars, { forward: hist.more, backward: false })
    this.onChange()
  }

  // ----- starting and stopping -----
  start(T) {
    this.gen += 1
    this.active = true
    this.playing = false
    this.ended = false
    this.T = T
    for (const id of this.panes.keys()) this.resetPane(id)
    for (const p of this.panes.values()) p.api.reload()
    this.onChange()
  }

  exit() {
    this.gen += 1
    this.active = false
    this.playing = false
    for (const p of this.panes.values()) { p.cur = null; p.feed = null }
    for (const p of this.panes.values()) p.api.reload()
    this.onChange()
  }

  // ----- revealing -----
  rowsFor(p, bar) {
    // [[seconds into the bar, o, h, l, c, v]]: recorded seconds, else the simulated path across the bar
    if (bar.base === '1m' && p.secs) {
      const rows = p.secs.peek(bar.timestamp)
      if (rows === undefined) p.secs.want(bar.timestamp)
      if (rows && rows.length) return { rows, real: true }
    }
    const dur = spanSec(bar)
    return { rows: subTicks(bar).map((t, i) => [Math.round((i * dur) / 4), t.open, t.high, t.low, t.close, t.volume]), real: false }
  }

  begin(p, bar) {
    const sb = p.api.sub()
    const tf = p.api.tf()
    const bts = !sb || (bar.base === '1D' && TF_MIN[tf]) ? null : bucketStart(tf, bar.timestamp)
    const c = p.api.chart()
    const list = c ? c.getDataList() : []
    const last = list[list.length - 1]
    if (bar.base === '1m' && p.secs) {
      p.secs.want(bar.timestamp)
      p.secs.want(bar.timestamp + 30 * 60000)
    }
    return { bar, sb, bts, before: last && bts != null && last.timestamp === bts ? { ...last } : null, rows: null, real: false, sent: 0 }
  }

  _rows(p, cur) {
    if (!cur.rows) {
      const r = this.rowsFor(p, cur.bar)
      cur.rows = r.rows
      cur.real = r.real
    }
    return cur.rows
  }

  _send(p, cur, upto) {
    // hand the rows from `sent` to `upto` to the trading engine
    const rows = this._rows(p, cur)
    if (upto <= cur.sent) return
    const start = spanStart(cur.bar)
    const out = rows.slice(cur.sent, upto).map(([s, o, h, l, c]) => ({ t: start + s * 1000, o, h, l, c }))
    cur.sent = upto
    this.onRows(p.feed.symbol, out)
  }

  partial(p, off) {
    const cur = p.cur
    const rows = this._rows(p, cur)
    p.secMode = cur.real ? 'real' : 'simulated'
    this.secMode = p.secMode
    let n = 0
    let part = null
    for (const [s, o, h, l, c, v] of rows) {
      if (s >= off) break
      n += 1
      part = part ? { ...part, high: Math.max(part.high, h), low: Math.min(part.low, l), close: c, volume: part.volume + v }
        : { open: o, high: h, low: l, close: c, volume: v }
    }
    this._send(p, cur, n)
    if (part && cur.bts != null && p.api.sub() === cur.sb) cur.sb.callback(merge(cur.before, part, cur.bts))
  }

  finish(p) {
    const cur = p.cur
    this._send(p, cur, this._rows(p, cur).length)
    if (cur.bts != null && p.api.sub() === cur.sb) cur.sb.callback(merge(cur.before, cur.bar, cur.bts))
    p.cur = null
  }

  // Everything of one pane up to time X.
  async processPane(p, X, gen) {
    for (;;) {
      if (!p.feed) return
      if (!p.cur) {
        const b = await p.feed.peek()
        if (gen !== this.gen) return
        if (!b) { p.done = true; return }
        if (spanStart(b) >= X) return
        await p.feed.take()
        p.cur = this.begin(p, b)
      }
      if (spanEnd(p.cur.bar) <= X) this.finish(p)
      else { this.partial(p, (X - spanStart(p.cur.bar)) / 1000); return }
    }
  }

  // The earliest moment any pane has data at or after T (null when every feed has ended).
  async nextData() {
    let best = null
    for (const p of this.panes.values()) {
      if (!p.feed) continue
      const s = p.cur ? Math.max(this.T, spanStart(p.cur.bar)) : (await p.feed.peek())
      const t = p.cur ? s : s ? spanStart(s) : null
      if (t != null && (best == null || t < best)) best = t
    }
    return best
  }

  async _skipGap(gen) {
    const next = await this.nextData()
    if (gen !== this.gen) return false
    if (next == null) { this.ended = true; return false }
    if (next > this.T) this.T = next
    return true
  }

  async moveTo(X, gen) {
    for (const p of this.panes.values()) {
      await this.processPane(p, X, gen)
      if (gen !== this.gen) return false
    }
    this.T = X
    return true
  }

  // Move `budget` seconds of market time forward (gaps between sessions cost nothing).
  async advance(budget, gen) {
    if (!(await this._skipGap(gen))) return false
    return this.moveTo(this.T + budget * 1000, gen)
  }

  rate(activeTf) {
    return (this.unit === 'bar' ? barSeconds(activeTf) : 60) * this.speed
  }

  // ----- controls -----
  play(activeTf) {
    if (!this.active || this.playing) return
    this.playing = true
    this.ended = false
    const gen = this.gen
    this.onChange()
    let last = performance.now()
    let busy = false
    const frame = async (now) => {
      if (!this.playing || gen !== this.gen) return
      const dt = Math.min(0.25, Math.max(0, (now - last) / 1000))
      last = now
      if (!busy) {
        busy = true
        const ok = await this.advance(this.rate(activeTf()) * dt, gen)
        busy = false
        if (gen !== this.gen) return
        if (!ok && this.ended) { this.playing = false; this.onChange(true); return }
        this.onChange()
      }
      requestAnimationFrame(frame)
    }
    // the first hour of recorded seconds before the clock starts
    Promise.all([...this.panes.values()].map((p) => p.secs ? p.secs.want(this.T - (this.T % 60000)) : null)).finally(() => requestAnimationFrame(frame))
  }

  pause() {
    this.playing = false
    this.gen += 1
    this.onChange(true)
  }

  // Jump at once: to the end of the current minute, of the active chart's bar, or of the session.
  async step(how, tf) {
    this.pause()
    const gen = this.gen
    if (!(await this._skipGap(gen))) { this.onChange(true); return }
    let X
    if (how === 'bar') X = nextBucket(tf, bucketStart(tf, this.T) ?? this.T)
    else if (how === 'session') X = bucketStart('1D', this.T) + DAY_MS
    else X = this.T - (this.T % 60000) + 60000
    if (X <= this.T) X = this.T + 60000
    await this.moveTo(X, gen)
    this.onChange(true)
  }
}
