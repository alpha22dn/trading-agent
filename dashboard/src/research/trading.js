// Paper trading inside a replay (decided 2026-10-01): practice only, separate from the agent's portfolios.
//
// Orders: market, limit or stop (trigger), buy or sell, each optionally with a stop-loss and a target that are
// placed when the entry fills and cancel each other (OCO). Long and short. One net position per stock: an order
// against the position reduces or closes it, any remainder opens the other way.
//
// Fills, on the replayed prices (recorded seconds, else the simulated path):
//   buy limit L: when the low reaches L, at L (or the open, if it opened below L)
//   sell limit L: when the high reaches L, at L (or the open, if it opened above)
//   buy stop S: when the high reaches S, at S (or the open, if it gapped above)
//   sell stop S: when the low reaches S, at S (or the open, if it gapped below)
//   market: at the latest replayed price, or the next price if the stock has none yet
// Within one price step stops are checked before limits, and a bracket's stop/target can only fill from the next
// step after its entry. Shorts are intraday (Zerodha does not carry an equity short overnight): an open short is
// closed at the session's last price when the replay moves to the next day.
// Every closed trade is recorded by the server, which adds Zerodha charges (intraday rates for the same day).
import { api } from '../api.js'

const IST_MS = 19800000
const istDay = (t) => Math.floor((t + IST_MS) / 86400000)

export class ReplayTrading {
  constructor(session, onChange = () => {}) {
    this.session = session
    this.onChange = onChange
    this.orders = []          // working orders
    this.pos = {}             // symbol -> { qty (signed), avg, since, estCharges }
    this.trades = []          // closed (as recorded by the server)
    this.last = {}            // symbol -> { t, price }
    this.seq = 1
    this.log = []             // what happened, newest first
  }

  note(text) {
    this.log.unshift(text)
    this.log = this.log.slice(0, 30)
  }

  // ----- orders -----
  place({ symbol, side, type, qty, price, trigger, sl, tp }) {
    qty = Math.floor(Number(qty))
    if (!symbol || !['buy', 'sell'].includes(side) || !['market', 'limit', 'stop'].includes(type)) throw new Error('Bad order')
    if (!(qty > 0)) throw new Error('Quantity must be at least 1')
    if (type === 'limit' && !(price > 0)) throw new Error('Enter a limit price')
    if (type === 'stop' && !(trigger > 0)) throw new Error('Enter a trigger price')
    const ref = type === 'limit' ? price : type === 'stop' ? trigger : this.last[symbol]?.price
    if (ref != null) {
      if (sl != null && (side === 'buy' ? sl >= ref : sl <= ref)) throw new Error(`The stop-loss must be ${side === 'buy' ? 'below' : 'above'} the entry`)
      if (tp != null && (side === 'buy' ? tp <= ref : tp >= ref)) throw new Error(`The target must be ${side === 'buy' ? 'above' : 'below'} the entry`)
    }
    const o = { id: this.seq++, symbol, side, type, qty, price: type === 'limit' ? price : null, trigger: type === 'stop' ? trigger : null,
      sl: sl ?? null, tp: tp ?? null, role: 'entry', placedAt: this.last[symbol]?.t ?? null, oco: null }
    if (type === 'market' && this.last[symbol]) {
      this.fill(o, this.last[symbol].price, this.last[symbol].t)
    } else {
      this.orders.push(o)
      this.note(`Placed ${this.describe(o)}`)
    }
    this.onChange()
    return o
  }

  describe(o) {
    const what = o.type === 'market' ? 'at market' : o.type === 'limit' ? `limit ${o.price}` : `stop ${o.trigger}`
    return `${o.side === 'buy' ? 'Buy' : 'Sell'} ${o.qty} ${o.symbol} ${what}${o.role !== 'entry' ? ` (${o.role})` : ''}`
  }

  cancel(id) {
    const o = this.orders.find((x) => x.id === id)
    if (!o) return
    this.orders = this.orders.filter((x) => x.id !== id)
    this.note(`Cancelled ${this.describe(o)}`)
    this.onChange()
  }

  closePosition(symbol, reason = 'closed') {
    const p = this.pos[symbol]
    const l = this.last[symbol]
    if (!p || !p.qty || !l) return
    this.fill({ id: this.seq++, symbol, side: p.qty > 0 ? 'sell' : 'buy', type: 'market', qty: Math.abs(p.qty), role: reason }, l.price, l.t)
    this.onChange()
  }

  // ----- fills -----
  fill(o, px, t) {
    const sym = o.symbol
    const p = this.pos[sym] || { qty: 0, avg: 0, since: null }
    const signed = o.side === 'buy' ? o.qty : -o.qty
    this.orders = this.orders.filter((x) => x.id !== o.id)
    this.note(`Filled ${this.describe(o)} at ${px}`)
    let open = signed
    if (p.qty && Math.sign(p.qty) !== Math.sign(signed)) {
      const closeQty = Math.min(Math.abs(p.qty), Math.abs(signed))
      this.record(sym, p.qty > 0 ? 'long' : 'short', closeQty, p.avg, p.since, px, t, o.role === 'entry' ? 'closed' : o.role)
      p.qty += Math.sign(signed) * closeQty
      open = signed - Math.sign(signed) * closeQty
      if (!p.qty) { p.avg = 0; p.since = null; this.cancelExits(sym) }
    }
    if (open) {
      const q = Math.abs(p.qty) + Math.abs(open)
      p.avg = Math.round(((Math.abs(p.qty) * p.avg + Math.abs(open) * px) / q) * 100) / 100
      p.qty += open
      p.since = p.since ?? t
      if (o.role === 'entry' && (o.sl != null || o.tp != null)) {
        const oco = `oco${o.id}`
        const side = open > 0 ? 'sell' : 'buy'
        const qty = Math.abs(open)
        if (o.sl != null) this.orders.push({ id: this.seq++, symbol: sym, side, type: 'stop', qty, trigger: o.sl, price: null, role: 'stop', oco, placedAt: t })
        if (o.tp != null) this.orders.push({ id: this.seq++, symbol: sym, side, type: 'limit', qty, price: o.tp, trigger: null, role: 'target', oco, placedAt: t })
      }
      this.estimate(sym)
    }
    this.pos[sym] = p
    if (o.oco) this.orders = this.orders.filter((x) => x.oco !== o.oco)          // the other leg
  }

  cancelExits(sym) {
    this.orders = this.orders.filter((x) => !(x.symbol === sym && x.role !== 'entry'))
  }

  async estimate(sym) {
    const p = this.pos[sym]
    if (!p || !p.qty) return
    try {
      const r = await api.get(`/api/research/charges?side=${p.qty > 0 ? 'long' : 'short'}&qty=${Math.abs(p.qty)}&entry=${p.avg}&exit=${p.avg}&entry_ts=${p.since}&exit_ts=${p.since}`)
      if (this.pos[sym] === p) { p.estCharges = r.charges; this.onChange() }
    } catch { /* estimate only */ }
  }

  async record(symbol, side, qty, entryPrice, entryTs, exitPrice, exitTs, reason) {
    const body = { session: this.session, symbol, side, qty, entry_ts: entryTs, entry_price: entryPrice, exit_ts: exitTs, exit_price: exitPrice, exit_reason: reason }
    const provisional = { ...body, gross: Math.round((side === 'long' ? exitPrice - entryPrice : entryPrice - exitPrice) * qty * 100) / 100, pending: true }
    this.trades.push(provisional)
    this.onChange()
    try {
      const r = await api.post('/api/research/trades', body)
      Object.assign(provisional, r, { pending: false })
    } catch (e) {
      provisional.error = e.message
    }
    this.onChange()
  }

  // ----- prices from the replay -----
  onRows(symbol, rows) {
    let changed = false
    for (const r of rows) {
      const l = this.last[symbol]
      if (l && r.t <= l.t) continue                                  // the same stock on two charts: once only
      const p = this.pos[symbol]
      if (l && p && p.qty < 0 && istDay(r.t) !== istDay(l.t)) {     // a short is not carried overnight
        this.fill({ id: this.seq++, symbol, side: 'buy', type: 'market', qty: -p.qty, role: 'end of day (short)' }, l.price, l.t)
        changed = true
      }
      this.last[symbol] = { t: r.t, price: r.c }
      if (this.step(symbol, r)) changed = true
    }
    if (changed) this.onChange()
    return changed
  }

  step(symbol, r) {
    let hit = false
    const working = this.orders.filter((o) => o.symbol === symbol && !(o.role !== 'entry' && o.placedAt != null && o.placedAt >= r.t))
    // stops first (conservative when a stop and a target are both touched in one step)
    working.sort((a, b) => (a.type === 'stop' ? 0 : 1) - (b.type === 'stop' ? 0 : 1))
    for (const o of working) {
      if (!this.orders.includes(o)) continue                          // cancelled by its OCO partner this step
      let px = null
      if (o.type === 'market') px = r.o
      else if (o.type === 'limit') {
        if (o.side === 'buy' && r.l <= o.price) px = Math.min(o.price, r.o)
        if (o.side === 'sell' && r.h >= o.price) px = Math.max(o.price, r.o)
      } else if (o.type === 'stop') {
        if (o.side === 'buy' && r.h >= o.trigger) px = Math.max(o.trigger, r.o)
        if (o.side === 'sell' && r.l <= o.trigger) px = Math.min(o.trigger, r.o)
      }
      if (px != null) { this.fill(o, Math.round(px * 100) / 100, r.t); hit = true }
    }
    return hit
  }

  // ----- views -----
  positions() {
    return Object.entries(this.pos).filter(([, p]) => p.qty).map(([symbol, p]) => {
      const last = this.last[symbol]?.price ?? p.avg
      const gross = Math.round((last - p.avg) * p.qty * 100) / 100
      return { symbol, qty: p.qty, avg: p.avg, last, gross, net: p.estCharges != null ? Math.round((gross - p.estCharges) * 100) / 100 : null, since: p.since }
    })
  }

  summary() {
    const done = this.trades.filter((t) => !t.pending && !t.error)
    const sum = (k) => Math.round(done.reduce((a, t) => a + (t[k] || 0), 0) * 100) / 100
    const open = this.positions().reduce((a, p) => a + p.gross, 0)
    return { trades: done.length, won: done.filter((t) => t.net > 0).length, gross: sum('gross'), charges: sum('charges'), net: sum('net'), open: Math.round(open * 100) / 100 }
  }
}
