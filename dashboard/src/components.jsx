import { useEffect, useRef, useState } from 'react'
import { api } from './api.js'

// Status is never colour alone: every pill carries an icon and a word.
const STATUS = {
  good: { icon: '●', cls: 'good' },
  warning: { icon: '▲', cls: 'warning' },
  serious: { icon: '◆', cls: 'serious' },
  critical: { icon: '■', cls: 'critical' },
  neutral: { icon: '○', cls: 'neutral' },
}

export function Pill({ kind = 'neutral', children, title }) {
  const s = STATUS[kind] || STATUS.neutral
  return (
    <span className={`pill pill-${s.cls}`} title={title}>
      <span aria-hidden="true" className="pill-icon">{s.icon}</span>
      {children}
    </span>
  )
}

export function Card({ title, action, children, className = '' }) {
  return (
    <section className={`card ${className}`}>
      {(title || action) && (
        <header className="card-head">
          {title && <h2>{title}</h2>}
          {action}
        </header>
      )}
      {children}
    </section>
  )
}

export function Tile({ label, value, sub, status }) {
  return (
    <div className="tile">
      <div className="tile-label">{label}</div>
      <div className="tile-value">{value}</div>
      {status && <div className="tile-status">{status}</div>}
      {sub && <div className="tile-sub">{sub}</div>}
    </div>
  )
}

// Every state-changing action goes through this dialog.
export function useConfirm() {
  const [pending, setPending] = useState(null)
  const ask = (opts) => new Promise((resolve) => setPending({ ...opts, resolve }))
  const close = (ok) => { pending.resolve(ok); setPending(null) }
  const dialog = pending && <ConfirmDialog {...pending} onClose={close} />
  return [ask, dialog]
}

function ConfirmDialog({ title, body, confirmLabel = 'Confirm', danger, onClose }) {
  const btn = useRef(null)
  useEffect(() => {
    btn.current && btn.current.focus()
    const onKey = (e) => e.key === 'Escape' && onClose(false)
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])
  return (
    <div className="modal-backdrop" onClick={() => onClose(false)}>
      <div className="modal" role="dialog" aria-modal="true" aria-labelledby="confirm-title" onClick={(e) => e.stopPropagation()}>
        <h3 id="confirm-title">{title}</h3>
        {body && <p>{body}</p>}
        <div className="modal-actions">
          <button className="btn" onClick={() => onClose(false)}>Cancel</button>
          <button ref={btn} className={`btn ${danger ? 'btn-danger' : 'btn-primary'}`} onClick={() => onClose(true)}>
            {confirmLabel}
          </button>
        </div>
      </div>
    </div>
  )
}

export function CopyButton({ text, label = 'Copy' }) {
  const [done, setDone] = useState(false)
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(text)
    } catch {
      const ta = document.createElement('textarea')
      ta.value = text
      document.body.appendChild(ta)
      ta.select()
      document.execCommand('copy')
      ta.remove()
    }
    setDone(true)
    setTimeout(() => setDone(false), 1800)
  }
  return <button className="btn btn-primary" onClick={copy}>{done ? 'Copied ✓' : label}</button>
}

export function Notice({ kind = 'info', children, onClose }) {
  return (
    <div className={`notice notice-${kind}`} role={kind === 'error' ? 'alert' : 'status'}>
      <div>{children}</div>
      {onClose && <button className="link" onClick={onClose} aria-label="Dismiss">✕</button>}
    </div>
  )
}

export function Progress({ value, max }) {
  const pct = max ? Math.round((value / max) * 100) : 0
  return (
    <div className="progress" role="progressbar" aria-valuenow={value} aria-valuemin={0} aria-valuemax={max}>
      <div className="progress-bar" style={{ width: `${pct}%` }} />
      <span className="progress-label">{value} / {max} ({pct}%)</span>
    </div>
  )
}

// A small form dialog for paper edits: fields [{name, label, type, value, min, step}].
export function useForm() {
  const [pending, setPending] = useState(null)
  const ask = (opts) => new Promise((resolve) => setPending({ ...opts, resolve }))
  const close = (values) => { pending.resolve(values); setPending(null) }
  const dialog = pending && <FormDialog {...pending} onClose={close} />
  return [ask, dialog]
}

function FormDialog({ title, body, fields, confirmLabel = 'Confirm', danger, onClose }) {
  const [values, setValues] = useState(() => Object.fromEntries(fields.map((f) => [f.name, f.value ?? ''])))
  const first = useRef(null)
  useEffect(() => {
    first.current && first.current.focus()
    const onKey = (e) => e.key === 'Escape' && onClose(null)
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])
  const submit = (e) => { e.preventDefault(); onClose(values) }
  return (
    <div className="modal-backdrop" onClick={() => onClose(null)}>
      <form className="modal" role="dialog" aria-modal="true" aria-labelledby="form-title" onClick={(e) => e.stopPropagation()} onSubmit={submit}>
        <h3 id="form-title">{title}</h3>
        {body && <p>{body}</p>}
        {fields.map((f, i) => f.type === 'checkbox' ? (
          <label key={f.name} className="form-check">
            <input ref={i === 0 ? first : null} type="checkbox" checked={!!values[f.name]} disabled={f.disabled}
              onChange={(e) => setValues({ ...values, [f.name]: e.target.checked })} />
            <span>{f.label}{f.hint && <span className="fine"> · {f.hint}</span>}</span>
          </label>
        ) : (
          <label key={f.name} className="form-field">
            <span>{f.label}</span>
            {f.type === 'symbol'
              ? <SymbolInput inputRef={i === 0 ? first : null} value={values[f.name]} onChange={(v) => setValues({ ...values, [f.name]: v })} />
              : <input ref={i === 0 ? first : null} className="search" type={f.type || 'text'} step={f.step} min={f.min} max={f.max}
                  maxLength={f.maxLength} placeholder={f.placeholder} disabled={f.disabled}
                  value={values[f.name]} required={f.required !== false} onChange={(e) => setValues({ ...values, [f.name]: e.target.value })} />}
            {f.hint && <span className="fine">{f.hint}</span>}
          </label>
        ))}
        <div className="modal-actions">
          <button type="button" className="btn" onClick={() => onClose(null)}>Cancel</button>
          <button type="submit" className={`btn ${danger ? 'btn-danger' : 'btn-primary'}`}>{confirmLabel}</button>
        </div>
      </form>
    </div>
  )
}

// NSE symbol picker: suggestions appear as you type (symbol or company name); arrows + Enter or a click to pick.
export function SymbolInput({ value, onChange, inputRef }) {
  const [items, setItems] = useState([])
  const [open, setOpen] = useState(false)
  const [active, setActive] = useState(0)
  const picked = useRef('')
  useEffect(() => {
    const q = (value || '').trim()
    if (!q || q === picked.current) { setItems([]); return }
    const t = setTimeout(() => {
      api.get(`/api/instruments/search?q=${encodeURIComponent(q)}&limit=10`)
        .then((r) => { setItems(r); setActive(0); setOpen(true) }).catch(() => setItems([]))
    }, 120)
    return () => clearTimeout(t)
  }, [value])
  const pick = (s) => { picked.current = s.symbol; onChange(s.symbol); setItems([]); setOpen(false) }
  const onKey = (e) => {
    if (!open || !items.length) return
    if (e.key === 'ArrowDown') { e.preventDefault(); setActive((a) => Math.min(items.length - 1, a + 1)) }
    else if (e.key === 'ArrowUp') { e.preventDefault(); setActive((a) => Math.max(0, a - 1)) }
    else if (e.key === 'Enter') { e.preventDefault(); pick(items[active]) }
    else if (e.key === 'Escape') { e.stopPropagation(); setOpen(false) }
  }
  const show = open && items.length > 0
  return (
    <div className="combo">
      <input ref={inputRef} className="search" role="combobox" aria-expanded={show} aria-controls="symbol-options" aria-autocomplete="list"
        aria-activedescendant={show ? `symbol-opt-${active}` : undefined} autoComplete="off" spellCheck={false} required
        placeholder="Type a symbol or company name" value={value}
        onChange={(e) => { picked.current = ''; onChange(e.target.value.toUpperCase()) }} onKeyDown={onKey}
        onFocus={() => items.length && setOpen(true)} onBlur={() => setTimeout(() => setOpen(false), 150)} />
      {show && (
        <ul id="symbol-options" role="listbox" className="combo-list">
          {items.map((s, i) => (
            <li key={s.symbol} id={`symbol-opt-${i}`} role="option" aria-selected={i === active} className={i === active ? 'on' : ''}
              onMouseEnter={() => setActive(i)} onMouseDown={(e) => { e.preventDefault(); pick(s) }}>
              <strong>{s.symbol}</strong>
              <span className="combo-name">{s.name}</span>
              {s.nse500 && <span className="combo-tag">NSE500</span>}
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}
