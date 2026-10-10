// Bar replay for the Research chart (phase 2, 2026-10-01).
//
// The replay walks forward through the stored 1-minute bars (daily bars before the 1-minute store begins).
// `cursor` is the time up to which everything is revealed: every base bar starting before it is on the chart,
// nothing after it is. The chart's own timeframe is built from those base bars, so a 1h or 1D candle forms
// minute by minute and indicators only ever see revealed bars.
import { api } from '../api.js'

export const IST_MS = 19800000
export const DAY_MS = 86400000
export const TF_MIN = { '1m': 1, '5m': 5, '15m': 15, '1h': 60, '4h': 240 }
export const SPEEDS = [0.1, 0.25, 0.5, 1, 2, 3, 5, 10, 20, 50]

const istDay = (ts) => Math.floor((ts + IST_MS) / DAY_MS)

// Start (epoch ms) of the chart bar that contains `ts`; null before the session opens on intraday timeframes.
export function bucketStart(tf, ts) {
  if (TF_MIN[tf]) {
    const m = TF_MIN[tf]
    const day = istDay(ts) * DAY_MS - IST_MS
    const open = day + (9 * 60 + 15) * 60000
    const mins = Math.floor((ts - open) / 60000)
    return mins < 0 ? null : open + Math.floor(mins / m) * m * 60000
  }
  const d = new Date(ts + IST_MS)
  const midnight = Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate()) - IST_MS
  if (tf === '1D') return midnight
  if (tf === '1W') return midnight - ((d.getUTCDay() + 6) % 7) * DAY_MS
  return Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), 1) - IST_MS
}

// Start of the bar after the one that starts at `ts` (used when you pick a bar: it becomes the last one shown).
export function nextBucket(tf, ts) {
  if (TF_MIN[tf]) return ts + TF_MIN[tf] * 60000
  const d = new Date(ts + IST_MS)
  if (tf === '1D') return ts + DAY_MS
  if (tf === '1W') return ts + 7 * DAY_MS
  return Date.UTC(d.getUTCFullYear(), d.getUTCMonth() + 1, 1) - IST_MS
}

// Fold one revealed bar into the chart's last bar (or start a new one).
export function merge(last, b, ts) {
  if (last && last.timestamp === ts) {
    return { ...last, high: Math.max(last.high, b.high), low: Math.min(last.low, b.low), close: b.close, volume: (last.volume || 0) + (b.volume || 0) }
  }
  return { timestamp: ts, open: b.open, high: b.high, low: b.low, close: b.close, volume: b.volume || 0 }
}

// The prices inside a minute are not stored: an animated minute walks open → low → high → close on an up minute
// and open → high → low → close on a down one. Only for display; labelled "simulated" in the controls.
export function subTicks(b) {
  const up = b.close >= b.open
  const path = [b.open, up ? b.low : b.high, up ? b.high : b.low, b.close]
  return path.map((p, i) => ({ open: p, high: p, low: p, close: p, volume: i === path.length - 1 ? b.volume || 0 : 0 }))
}

export class ReplayFeed {
  constructor(symbol) {
    this.symbol = symbol
    this.buffer = []
    this.base = null           // '1m' or '1D' for the bars in the buffer
    this.end = false
    this.next = null           // time to ask the server from next
    this.loading = null
    this.minutesFrom = null
  }

  async fetch(after, limit = 5000) {
    const r = await api.get(`/api/research/replay?symbol=${encodeURIComponent(this.symbol)}&after=${after}&limit=${limit}`)
    this.minutesFrom = r.minutes_from ? Date.parse(r.minutes_from) : null
    return r
  }

  // The base bars in [from, to): to build the partly formed bar when the replay starts or the timeframe changes.
  async range(from, to) {
    const out = []
    let from_ = from
    for (let i = 0; i < 10 && from_ < to; i++) {
      const r = await this.fetch(from_, 20000)
      for (const b of r.bars) if (after({ ...b, base: r.base }) <= to) out.push({ ...b, base: r.base })   // only bars fully before the cursor
      if (r.end || r.bars.length === 0 || r.bars[r.bars.length - 1].timestamp >= to) break
      from_ = r.bars[r.bars.length - 1].timestamp + (r.base === '1m' ? 60000 : DAY_MS)
    }
    return out
  }

  reset(cursor) {
    this.buffer = []
    this.end = false
    this.next = cursor
    this.loading = null
  }

  async fill(min = 300) {
    if (this.buffer.length >= min || this.end) return
    if (!this.loading) {
      this.loading = (async () => {
        const r = await this.fetch(this.next)
        for (const b of r.bars) this.buffer.push({ ...b, base: r.base })
        if (r.bars.length) this.next = r.bars[r.bars.length - 1].timestamp + (r.base === '1m' ? 60000 : DAY_MS)
        // daily bars stop where the minutes begin; an empty daily answer is not the end unless the server says so
        this.end = r.end && r.bars.length === 0 ? true : r.end
        if (!r.bars.length && !r.end && this.minutesFrom) this.next = Math.max(this.next, bucketStart('1D', this.minutesFrom))
      })().finally(() => { this.loading = null })
    }
    await this.loading
  }

  async take() {
    if (!this.buffer.length) await this.fill(1)
    if (!this.buffer.length && !this.end) await this.fill(1)
    const b = this.buffer.shift() || null
    if (this.buffer.length < 300 && !this.end) this.fill().catch(() => {})
    return b
  }

  async peek() {
    if (!this.buffer.length) await this.fill(1)
    if (!this.buffer.length && !this.end) await this.fill(1)      // daily bars hand over to minutes
    return this.buffer[0] || null
  }
}

// The time just after a base bar.
export const after = (b) => b.timestamp + (b.base === '1D' ? DAY_MS : 60000)
export const sameDay = (a, b) => istDay(a) === istDay(b)

// Recorded 1-second bars (from the day recording started), fetched an hour at a time and cached.
// peek(minute) is synchronous for the animation frame: rows, null (none recorded), or undefined (not loaded yet).
const PENDING = Symbol('pending')
export class SecondsSource {
  constructor(symbol) {
    this.symbol = symbol
    this.days = null
    this.cache = new Map()
    this.loadingDays = null
  }

  static day(ts) {
    return new Date(ts + IST_MS).toISOString().slice(0, 10)
  }

  async loadDays() {
    if (this.days) return
    if (!this.loadingDays) {
      this.loadingDays = api.get('/api/research/seconds/days').then((r) => { this.days = new Set(r.days) }).catch(() => { this.days = new Set() })
    }
    await this.loadingDays
  }

  peek(minute) {
    if (!this.days) { this.loadDays(); return undefined }
    if (!this.days.has(SecondsSource.day(minute))) return null
    const v = this.cache.get(minute)
    return v === PENDING ? undefined : v
  }

  // Fetch the hour from `minute` (same day) unless it is cached or on its way.
  async want(minute) {
    await this.loadDays()
    if (!this.days.has(SecondsSource.day(minute)) || this.cache.has(minute)) return
    const end = Math.min(minute + 60 * 60000, bucketStart('1D', minute) + DAY_MS)
    for (let m = minute; m < end; m += 60000) if (!this.cache.has(m)) this.cache.set(m, PENDING)
    try {
      const r = await api.get(`/api/research/seconds?symbol=${encodeURIComponent(this.symbol)}&start=${minute}&end=${end}`)
      for (let m = minute; m < end; m += 60000) if (this.cache.get(m) === PENDING) this.cache.set(m, r.minutes[m] || null)
    } catch {
      for (let m = minute; m < end; m += 60000) if (this.cache.get(m) === PENDING) this.cache.delete(m)
    }
  }
}

// Data seconds in one base bar: a minute, or a whole session for a daily bar (before the 1-minute store).
export const baseSeconds = (b) => (b.base === '1D' ? 22500 : 60)
// Data seconds in one chart bar, for "1× = one bar a second".
export const barSeconds = (tf) => (TF_MIN[tf] ? TF_MIN[tf] * 60 : { '1D': 22500, '1W': 112500, '1M': 472500 }[tf])
