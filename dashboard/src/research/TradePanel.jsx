// Replay trading panel: order ticket, positions, working orders, closed trades with Zerodha charges.
import { useEffect, useState } from 'react'

const inr = (v) => (v == null ? '—' : `${v < 0 ? '−' : ''}₹${Math.abs(v).toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`)
const tone = (v) => (v > 0 ? 'pos' : v < 0 ? 'neg' : '')
const when = (t) => (t ? new Date(t + 19800000).toISOString().slice(5, 19).replace('T', ' ') : '')

export default function TradePanel({ engine, symbol, version, onClose }) {
  const [side, setSide] = useState('buy')
  const [type, setType] = useState('market')
  const [qty, setQty] = useState(10)
  const [price, setPrice] = useState('')
  const [useSl, setUseSl] = useState(false)
  const [sl, setSl] = useState('')
  const [useTp, setUseTp] = useState(false)
  const [tp, setTp] = useState('')
  const [tab, setTab] = useState('positions')
  const [msg, setMsg] = useState(null)
  const last = engine ? engine.last[symbol] : null

  useEffect(() => {
    // prefill prices around the current price when the stock changes or the ticket opens
    if (!last) return
    const p = last.price
    setPrice((v) => (v === '' ? String(p) : v))
    setSl((v) => (v === '' ? String(Math.round(p * (side === 'buy' ? 0.99 : 1.01) * 100) / 100) : v))
    setTp((v) => (v === '' ? String(Math.round(p * (side === 'buy' ? 1.02 : 0.98) * 100) / 100) : v))
  }, [symbol, !!last])

  if (!engine) return null
  const num = (v) => (v === '' || v == null ? null : Number(v))
  const submit = (e) => {
    e.preventDefault()
    try {
      engine.place({ symbol, side, type, qty: num(qty), price: type === 'limit' ? num(price) : null, trigger: type === 'stop' ? num(price) : null,
        sl: useSl ? num(sl) : null, tp: useTp ? num(tp) : null })
      setMsg(null)
    } catch (err) {
      setMsg(err.message)
    }
  }
  const pos = engine.positions()
  const sum = engine.summary()
  const trades = [...engine.trades].reverse()

  return (
    <div className="rs-trade" data-version={version}>
      <form className="rs-ticket" onSubmit={submit}>
        <div className="rs-ticket-head">
          <strong>{symbol}</strong>
          <span className="fine">{last ? `${last.price} · ${when(last.t)}` : 'no replay price yet'}</span>
        </div>
        <div className="rs-seg" role="group" aria-label="Side">
          <button type="button" className={side === 'buy' ? 'on buy' : ''} onClick={() => setSide('buy')}>Buy</button>
          <button type="button" className={side === 'sell' ? 'on sell' : ''} onClick={() => setSide('sell')}>Sell</button>
        </div>
        <div className="rs-ticket-row">
          <label>Type<select value={type} onChange={(e) => setType(e.target.value)}>
            <option value="market">Market</option><option value="limit">Limit</option><option value="stop">Stop (trigger)</option>
          </select></label>
          <label>Qty<input type="number" min="1" step="1" value={qty} onChange={(e) => setQty(e.target.value)} /></label>
          {type !== 'market' && <label>{type === 'limit' ? 'Limit' : 'Trigger'}<input type="number" step="0.05" value={price} onChange={(e) => setPrice(e.target.value)} /></label>}
        </div>
        <div className="rs-ticket-row">
          <label className="rs-inline"><input type="checkbox" checked={useSl} onChange={(e) => setUseSl(e.target.checked)} /> Stop-loss</label>
          <input type="number" step="0.05" value={sl} disabled={!useSl} onChange={(e) => setSl(e.target.value)} aria-label="Stop-loss price" />
          <label className="rs-inline"><input type="checkbox" checked={useTp} onChange={(e) => setUseTp(e.target.checked)} /> Target</label>
          <input type="number" step="0.05" value={tp} disabled={!useTp} onChange={(e) => setTp(e.target.value)} aria-label="Target price" />
        </div>
        <button className={`btn btn-sm ${side === 'buy' ? 'rs-buy' : 'rs-sell'}`}>
          {side === 'buy' ? 'Buy' : 'Sell'} {qty} {type === 'market' ? 'at market' : `${type} ${price}`}
        </button>
        {msg && <div className="text-bad fine">{msg}</div>}
        <p className="fine">Practice only, never sent to Kite. Shorts are closed at the session's last price (no overnight equity shorts). Charges as Zerodha: intraday when bought and sold the same day.</p>
      </form>
      <div className="rs-blotter">
        <div className="rs-blotter-head">
          <div className="rs-tabs" role="tablist">
            {[['positions', `Positions (${pos.length})`], ['orders', `Orders (${engine.orders.length})`], ['trades', `Trades (${engine.trades.length})`], ['log', 'Log']].map(([k, l]) => (
              <button key={k} role="tab" aria-selected={tab === k} className={tab === k ? 'on' : ''} onClick={() => setTab(k)}>{l}</button>
            ))}
          </div>
          <span className="rs-sum">
            Closed net <strong className={tone(sum.net)}>{inr(sum.net)}</strong> · gross {inr(sum.gross)} · charges {inr(sum.charges)}
            {' · '}{sum.won}/{sum.trades} won · open <strong className={tone(sum.open)}>{inr(sum.open)}</strong>
          </span>
          <button type="button" className="rs-icon-btn" onClick={onClose} aria-label="Hide the trading panel" title="Hide">✕</button>
        </div>
        <div className="rs-blotter-body">
          {tab === 'positions' && (pos.length ? (
            <table className="rs-table"><thead><tr><th>Stock</th><th>Side</th><th className="num">Qty</th><th className="num">Avg</th><th className="num">Last</th><th className="num">P&amp;L</th><th className="num">≈ Net</th><th /></tr></thead>
              <tbody>{pos.map((p) => (
                <tr key={p.symbol}><td>{p.symbol}</td><td>{p.qty > 0 ? 'Long' : 'Short'}</td><td className="num">{Math.abs(p.qty)}</td><td className="num">{p.avg}</td><td className="num">{p.last}</td>
                  <td className={`num ${tone(p.gross)}`}>{inr(p.gross)}</td><td className={`num ${tone(p.net)}`}>{inr(p.net)}</td>
                  <td><button type="button" className="btn btn-sm" onClick={() => engine.closePosition(p.symbol)}>Close</button></td></tr>
              ))}</tbody></table>
          ) : <p className="fine">No open positions.</p>)}
          {tab === 'orders' && (engine.orders.length ? (
            <table className="rs-table"><thead><tr><th>Stock</th><th>Order</th><th className="num">Qty</th><th className="num">Price</th><th>For</th><th /></tr></thead>
              <tbody>{engine.orders.map((o) => (
                <tr key={o.id}><td>{o.symbol}</td><td>{o.side === 'buy' ? 'Buy' : 'Sell'} {o.type}</td><td className="num">{o.qty}</td><td className="num">{o.price ?? o.trigger ?? 'market'}</td><td>{o.role}{o.sl || o.tp ? ` (SL ${o.sl ?? '—'} / T ${o.tp ?? '—'})` : ''}</td>
                  <td><button type="button" className="btn btn-sm" onClick={() => engine.cancel(o.id)}>Cancel</button></td></tr>
              ))}</tbody></table>
          ) : <p className="fine">No working orders.</p>)}
          {tab === 'trades' && (trades.length ? (
            <table className="rs-table"><thead><tr><th>Stock</th><th>Side</th><th className="num">Qty</th><th>In</th><th className="num">Entry</th><th>Out</th><th className="num">Exit</th><th>Why</th><th className="num">Gross</th><th className="num">Charges</th><th className="num">Net</th></tr></thead>
              <tbody>{trades.map((t, i) => (
                <tr key={t.id || `p${i}`}><td>{t.symbol}</td><td>{t.side === 'long' ? 'Long' : 'Short'}</td><td className="num">{t.qty}</td><td>{when(t.entry_ts)}</td><td className="num">{t.entry_price}</td>
                  <td>{when(t.exit_ts)}</td><td className="num">{t.exit_price}</td><td>{t.exit_reason}</td><td className={`num ${tone(t.gross)}`}>{inr(t.gross)}</td>
                  <td className="num">{t.pending ? '…' : inr(t.charges)}{t.intraday ? <span className="fine"> intraday</span> : ''}</td><td className={`num ${tone(t.net)}`}>{t.pending ? '…' : inr(t.net)}</td></tr>
              ))}</tbody></table>
          ) : <p className="fine">No closed trades in this replay yet.</p>)}
          {tab === 'log' && <ul className="rs-log">{engine.log.map((l, i) => <li key={i}>{l}</li>)}</ul>}
        </div>
      </div>
    </div>
  )
}
