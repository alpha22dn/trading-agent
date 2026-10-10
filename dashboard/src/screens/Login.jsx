import { useEffect, useState } from 'react'
import { api, fmtDuration, fmtTime } from '../api.js'
import { Card, CopyButton, Notice, Pill, useConfirm } from '../components.jsx'

export default function Login({ ov, params, reload }) {
  const [query, setQuery] = useState('')
  const [flash, setFlash] = useState(null)
  const [confirm, dialog] = useConfirm()

  useEffect(() => {
    api.get('/api/screener-query').then((r) => setQuery(r.query)).catch(() => {})
  }, [])

  useEffect(() => {
    if (params.get('ok')) setFlash({ kind: 'good', text: 'Logged in to Kite. Instruments are syncing in the background.' })
    else if (params.get('error')) setFlash({ kind: 'error', text: params.get('error') })
  }, [params])

  const s = ov && ov.session

  const logout = async () => {
    const ok = await confirm({
      title: 'Log out of Kite?',
      body: 'No new orders will be placed until you log in again. Existing GTTs stay active at Zerodha.',
      confirmLabel: 'Log out',
      danger: true,
    })
    if (!ok) return
    await api.confirm('/api/kite/logout')
    reload()
  }

  return (
    <>
      <h1>Login</h1>
      <p className="lead">One manual login each morning powers the whole trading day. If you skip it, no new orders are placed; existing GTTs keep protecting positions.</p>

      {flash && <Notice kind={flash.kind === 'good' ? 'good' : 'error'} onClose={() => { setFlash(null); location.hash = '#/login' }}>{flash.text}</Notice>}

      <div className="grid-2">
        <Card title="Kite session">
          {!s ? <p className="muted">Loading…</p> : (
            <>
              <div className="session-status">
                <Pill kind={s.valid ? 'good' : 'critical'}>{s.valid ? 'Active' : s.reason || 'Not logged in'}</Pill>
                {s.valid && <span className="muted">expires in {fmtDuration(s.seconds_left)}</span>}
              </div>
              <dl className="kv">
                <dt>User</dt><dd>{s.user_id ? `${s.user_id}${s.user_name ? ` · ${s.user_name}` : ''}` : '—'}</dd>
                <dt>Logged in</dt><dd>{fmtTime(s.login_at)}</dd>
                <dt>Expires</dt><dd>{fmtTime(s.expires_at)}</dd>
              </dl>

              {!s.configured ? (
                <Notice kind="warning">
                  <strong>Kite keys missing.</strong> Open <code>.env</code> in the project folder, fill in
                  <code> KITE_API_KEY</code> and <code>KITE_API_SECRET</code>, then restart <code>run.bat</code>.
                  In your Kite Connect app, set the redirect URL to:
                  <div className="inline-copy"><code>{s.redirect_url}</code><CopyButton text={s.redirect_url} /></div>
                </Notice>
              ) : (
                <div className="actions">
                  <a className={`btn ${s.valid ? '' : 'btn-primary btn-lg'}`} href="/api/kite/login">
                    {s.valid ? 'Log in again' : 'Log in to Kite'}
                  </a>
                  {s.valid && <button className="btn btn-danger-quiet" onClick={logout}>Log out</button>}
                </div>
              )}
              <p className="fine">You enter your password and 2FA on Zerodha's own page. The agent never sees them; it only receives today's access token.</p>
            </>
          )}
        </Card>

        <Card title="Screener query" action={query && <CopyButton text={query} label="Copy query" />}>
          <pre className="query">{query || 'Loading…'}</pre>
          <p className="fine">
            Run this on <a href="https://www.screener.in/screen/new/" target="_blank" rel="noreferrer">screener.in</a> weekly
            during results season, monthly otherwise. Pasting results into the Watchlist arrives in Phase 1.
            The text comes from <code>config/app.yaml</code>.
          </p>
        </Card>
      </div>
      {dialog}
    </>
  )
}
