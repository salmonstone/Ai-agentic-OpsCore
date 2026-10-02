import { useState } from 'react'
import { Icon } from './common'

/** Shown only when DASHBOARD_TOKEN is set on the server and this browser
 *  isn't logged in. The token lands in an HttpOnly cookie, never in JS. */
export default function Login({ onDone }) {
  const [token, setToken] = useState('')
  const [err, setErr] = useState(null)
  const [busy, setBusy] = useState(false)
  const submit = async e => {
    e.preventDefault()
    setBusy(true); setErr(null)
    try {
      const r = await fetch('/api/login', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ token }) })
      const body = await r.json().catch(() => ({}))
      if (r.ok && body.ok) onDone()
      else setErr(body.detail || 'Wrong token.')
    } catch (e2) {
      setErr(e2.message)
    }
    setBusy(false)
  }
  return (
    <div style={{ minHeight: '100vh', display: 'grid', placeItems: 'center', padding: 16 }}>
      <form onSubmit={submit} className="surface" style={{ width: 'min(380px, 100%)', display: 'flex', flexDirection: 'column', gap: 14, padding: 24 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
          <div style={{ width: 32, height: 32, borderRadius: 8, border: '1px solid var(--color-accent)', display: 'grid', placeItems: 'center', color: 'var(--color-accent)' }}><Icon name="ph-globe-simple" size={18} /></div>
          <div><div style={{ fontSize: 16, fontWeight: 500 }}>AtlasOS</div><div className="muted" style={{ fontSize: 12 }}>This dashboard is protected</div></div>
        </div>
        <label htmlFor="login-token" style={{ display: 'flex', flexDirection: 'column', gap: 6, fontSize: 12 }}>
          <span className="muted">Dashboard token</span>
          <input id="login-token" className="input mono" type="password" autoComplete="current-password" autoFocus value={token} onChange={e => setToken(e.target.value)} />
        </label>
        {err && <div style={{ fontSize: 12, color: 'var(--st-crit)' }}><Icon name="ph-x-circle" /> {err}</div>}
        <button className="btn btn-primary" type="submit" disabled={busy || !token}>{busy ? 'Checking…' : 'Log in'}</button>
        <div className="muted" style={{ fontSize: 11.5 }}>It's the <code>DASHBOARD_TOKEN</code> secret. Forgot it? Set a new one in a terminal with <code>agent secrets set DASHBOARD_TOKEN --generate</code> and restart the dashboard.</div>
      </form>
    </div>
  )
}
