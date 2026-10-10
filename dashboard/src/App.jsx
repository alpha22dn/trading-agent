import { useEffect, useState } from 'react'
import { api, useLive, useOnPrices } from './api.js'
import { Pill } from './components.jsx'
import Accounts from './screens/Accounts.jsx'
import Journal from './screens/Journal.jsx'
import Learning from './screens/Learning.jsx'
import Login from './screens/Login.jsx'
import Overview from './screens/Overview.jsx'
import Placeholder from './screens/Placeholder.jsx'
import Research from './research/Research.jsx'
import Positions from './screens/Positions.jsx'
import Signals from './screens/Signals.jsx'
import Strategies from './screens/Strategies.jsx'
import Watchlist from './screens/Watchlist.jsx'

// The eight screens of spec section 11. Later phases replace the placeholders.
const SCREENS = [
  { id: 'login', label: 'Login', phase: 0 },
  { id: 'overview', label: 'Overview', phase: 0 },
  { id: 'accounts', label: 'Accounts', phase: 2, built: true },
  { id: 'strategies', label: 'Strategies', phase: 3, built: true },
  { id: 'positions', label: 'Positions & GTTs', phase: 2, built: true },
  { id: 'signals', label: 'Signals', phase: 1, built: true },
  { id: 'watchlist', label: 'Watchlist', phase: 1, built: true },
  { id: 'research', label: 'Research', phase: 3, built: true },
  { id: 'journal', label: 'Journal', phase: 3, built: true },
  { id: 'learning', label: 'Learning', phase: 3, built: true },
]

function parseHash() {
  const [path, query = ''] = location.hash.replace(/^#\/?/, '').split('?')
  return { screen: path || null, params: new URLSearchParams(query) }
}

// On a server the dashboard asks for its password first (DASHBOARD_AUTH=required); on the laptop it never does.
function SignIn({ onDone }) {
  const [pw, setPw] = useState('')
  const [err, setErr] = useState(null)
  const [busy, setBusy] = useState(false)
  const submit = async (e) => {
    e.preventDefault()
    setBusy(true)
    try {
      await api.post('/api/auth/login', { password: pw })
      onDone()
    } catch (ex) {
      setErr(ex.message)
      setPw('')
    } finally {
      setBusy(false)
    }
  }
  return (
    <div className="signin">
      <form className="signin-card" onSubmit={submit}>
        <div className="brand"><span className="brand-mark" aria-hidden="true" /><div className="brand-name">Trading Agent</div></div>
        <label htmlFor="pw">Dashboard password</label>
        <input id="pw" type="password" autoFocus autoComplete="current-password" value={pw} onChange={(e) => setPw(e.target.value)} />
        {err && <div className="notice notice-error" role="alert">{err}</div>}
        <button className="btn btn-primary" disabled={busy || !pw}>{busy ? 'Signing in…' : 'Sign in'}</button>
        <p className="fine">Kite is logged in separately each day, from the Login screen.</p>
      </form>
    </div>
  )
}

export default function App() {
  const [auth, setAuth] = useState(null)
  const checkAuth = () => api.get('/api/auth/status').then(setAuth).catch(() => setAuth({ required: false, signed_in: true }))
  useEffect(() => {
    checkAuth()
    const on = () => setAuth((a) => (a ? { ...a, signed_in: false } : a))
    window.addEventListener('auth-required', on)
    return () => window.removeEventListener('auth-required', on)
  }, [])
  if (!auth) return null
  if (auth.required && !auth.signed_in) return <SignIn onDone={() => { checkAuth(); location.reload() }} />
  return <Main auth={auth} />
}

function Main({ auth }) {
  const [route, setRoute] = useState(parseHash())
  const live = useLive('/api/overview')
  const ov = live.data
  const onOverview = !route.screen || route.screen === 'overview'
  useOnPrices(() => (onOverview ? live.reload() : null))

  useEffect(() => {
    const onHash = () => setRoute(parseHash())
    window.addEventListener('hashchange', onHash)
    return () => window.removeEventListener('hashchange', onHash)
  }, [])

  // First open of the day lands on Login until the Kite session is valid.
  const screen = route.screen === 'backtest' ? 'research' : route.screen || (ov && !ov.session.valid ? 'login' : 'overview')
  const current = SCREENS.find((s) => s.id === screen) || SCREENS[1]
  // Research is a full-page chart: the menu slides in over it on demand.
  const full = current.id === 'research'
  const [navOpen, setNavOpen] = useState(false)
  useEffect(() => setNavOpen(false), [current.id])

  return (
    <div className={`shell${full ? ' shell-full' : ''}${full && navOpen ? ' nav-open' : ''}`}>
      {full && navOpen && <div className="nav-scrim" onClick={() => setNavOpen(false)} />}
      <nav className="sidebar" aria-label="Screens">
        <div className="brand">
          <span className="brand-mark" aria-hidden="true" />
          <div>
            <div className="brand-name">Trading Agent</div>
            <div className="brand-sub">{ov ? `${ov.strategies_on ?? 1} strateg${(ov.strategies_on ?? 1) === 1 ? 'y' : 'ies'} ON · ${ov.broker_mode}` : 'connecting…'}</div>
          </div>
        </div>
        <ul>
          {SCREENS.map((s) => (
            <li key={s.id}>
              <a href={`#/${s.id}`} className={s.id === current.id ? 'active' : ''} aria-current={s.id === current.id ? 'page' : undefined}>
                <span>{s.label}</span>
                {s.phase > 0 && !s.built && <span className="phase-tag">P{s.phase}</span>}
              </a>
            </li>
          ))}
        </ul>
        <div className="sidebar-foot">
          {ov && (
            <Pill kind={ov.session.valid ? 'good' : 'critical'}>
              {ov.session.valid ? 'Kite session active' : 'Not logged in'}
            </Pill>
          )}
          {auth.required && (
            <button className="btn btn-sm" onClick={() => api.post('/api/auth/logout').then(() => location.reload())}>Sign out</button>
          )}
          <div className={`conn ${live.connected ? 'on' : 'off'}`}>
            <span aria-hidden="true">{live.connected ? '●' : '○'}</span> {live.connected ? 'Live' : 'Reconnecting'}
          </div>
        </div>
      </nav>
      <main className="main">
        {live.error && !ov && (
          <div className="notice notice-error" role="alert">
            Can't reach the backend ({live.error}). Is run.bat still open?
          </div>
        )}
        {current.id === 'login' && <Login ov={ov} params={route.params} reload={live.reload} />}
        {current.id === 'overview' && <Overview ov={ov} reload={live.reload} />}
        {current.id === 'accounts' && <Accounts tick={live.tick} />}
        {current.id === 'strategies' && <Strategies tick={live.tick} />}
        {current.id === 'positions' && <Positions tick={live.tick} />}
        {current.id === 'signals' && <Signals tick={live.tick} />}
        {current.id === 'watchlist' && <Watchlist tick={live.tick} />}
        {current.id === 'research' && <Research toggleNav={() => setNavOpen((v) => !v)} />}
        {current.id === 'journal' && <Journal tick={live.tick} />}
        {current.id === 'learning' && <Learning tick={live.tick} />}
        {current.phase > 0 && !current.built && <Placeholder screen={current} />}
      </main>
    </div>
  )
}
