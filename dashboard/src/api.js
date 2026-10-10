// Thin wrappers around the backend REST API and the live-update WebSocket.
import { useCallback, useEffect, useRef, useState } from 'react'

async function request(method, path, body) {
  const opts = { method, headers: {} }
  if (body instanceof FormData) opts.body = body
  else if (body !== undefined) {
    opts.headers['Content-Type'] = 'application/json'
    opts.body = JSON.stringify(body)
  }
  const res = await fetch(path, opts)
  if (res.status === 401 && !path.startsWith('/api/auth/')) window.dispatchEvent(new Event('auth-required'))
  const text = await res.text()
  const data = text ? JSON.parse(text) : null
  if (!res.ok) throw new Error((data && data.detail) || `${res.status} ${res.statusText}`)
  return data
}

export const api = {
  get: (path) => request('GET', path),
  post: (path, body) => request('POST', path, body),
  put: (path, body) => request('PUT', path, body),
  del: (path) => request('DELETE', path),
  // State-changing actions carry an explicit confirmation flag; the backend rejects them otherwise.
  confirm: (path) => request('POST', path, { confirm: true }),
  upload: (path, file) => {
    const fd = new FormData()
    fd.append('file', file)
    return request('POST', path, fd)
  },
}

// ---------- live prices pushed by the agent (topic 'prices', about once a second while Kite streams) ----------

const priceStore = { p: {}, at: 0, listeners: new Set() }

function putPrices(msg) {
  const now = Date.now()
  const data = (msg && msg.p) || {}
  for (const [sym, v] of Object.entries(data)) priceStore.p[sym] = { ltp: v[0], open: v[1], high: v[2], low: v[3], at: now }
  priceStore.at = now
  for (const fn of priceStore.listeners) fn(data, !!(msg && msg.held))
}

// Subscribe to one stock's pushed prices; returns the unsubscribe function.
export function onPrice(symbol, fn) {
  const l = (d) => { if (d && d[symbol]) fn(priceStore.p[symbol]) }
  priceStore.listeners.add(l)
  return () => priceStore.listeners.delete(l)
}

export function latestPrice(symbol) {
  return priceStore.p[symbol] || null
}

// The latest pushed price of one stock; re-renders when it changes.
export function usePrice(symbol) {
  const [, bump] = useState(0)
  useEffect(() => {
    if (!symbol) return undefined
    const fn = (d) => { if (d && d[symbol]) bump((x) => x + 1) }
    priceStore.listeners.add(fn)
    return () => priceStore.listeners.delete(fn)
  }, [symbol])
  return symbol ? priceStore.p[symbol] || null : null
}

// Calls `fn` (a reload) when a held stock's price moves (or, with `symbol`, that stock's), at most once per `ms`
// and never while the previous call is running. Every NSE500 stock streams, so screens wait for their own stocks.
export function useOnPrices(fn, { ms = 1000, symbol = null } = {}) {
  const busy = useRef(false)
  const last = useRef(0)
  const ref = useRef(fn)
  ref.current = fn
  useEffect(() => {
    const on = async (data, held) => {
      if (symbol ? !(data && data[symbol]) : !held) return
      const now = Date.now()
      if (busy.current || now - last.current < ms) return
      busy.current = true
      last.current = now
      try { await ref.current() } catch { /* the next push retries */ } finally { busy.current = false }
    }
    priceStore.listeners.add(on)
    return () => priceStore.listeners.delete(on)
  }, [ms, symbol])
}

// Fetches `path`, refetches whenever the backend pushes an update over the WebSocket,
// and falls back to polling if the socket is down. Price pushes go to the price store, not to refetches.
export function useLive(path, pollMs = 20000) {
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const [connected, setConnected] = useState(false)
  const [tick, setTick] = useState(0) // bumps on every pushed update (not prices), so other screens can refetch
  const timer = useRef(null)

  const load = useCallback(async () => {
    try {
      setData(await api.get(path))
      setError(null)
    } catch (e) {
      setError(e.message)
    }
  }, [path])

  useEffect(() => {
    let ws
    let closed = false
    let retry = 1000
    const refetchSoon = () => {
      clearTimeout(timer.current)
      timer.current = setTimeout(() => { load(); setTick((t) => t + 1) }, 250)
    }
    const connect = () => {
      const proto = location.protocol === 'https:' ? 'wss' : 'ws'
      ws = new WebSocket(`${proto}://${location.host}/ws`)
      ws.onopen = () => { setConnected(true); retry = 1000; load() }
      ws.onmessage = (ev) => {
        let m = null
        try { m = JSON.parse(ev.data) } catch { m = null }
        if (m && m.topic === 'prices') { putPrices(m.data); return }
        refetchSoon()
      }
      ws.onclose = () => {
        setConnected(false)
        if (!closed) setTimeout(connect, (retry = Math.min(retry * 2, 30000)))
      }
    }
    load()
    connect()
    const poll = setInterval(load, pollMs)
    return () => { closed = true; clearInterval(poll); clearTimeout(timer.current); ws && ws.close() }
  }, [load, pollMs])

  return { data, error, connected, tick, reload: load }
}

export function fmtTime(iso) {
  if (!iso) return '—'
  const d = new Date(iso)
  return d.toLocaleString('en-IN', { day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit', hour12: false })
}

export function fmtDate(iso) {
  if (!iso) return '—'
  return new Date(iso + (iso.length === 10 ? 'T00:00:00' : '')).toLocaleDateString('en-IN', { day: '2-digit', month: 'short', year: 'numeric' })
}

export function fmtInr(n) {
  if (n === null || n === undefined) return '—'
  return '₹' + Number(n).toLocaleString('en-IN', { maximumFractionDigits: 0 })
}

export function fmtDuration(seconds) {
  if (!seconds) return '0m'
  const h = Math.floor(seconds / 3600)
  const m = Math.floor((seconds % 3600) / 60)
  return h ? `${h}h ${m}m` : `${m}m`
}
