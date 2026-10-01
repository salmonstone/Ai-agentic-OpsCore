import { useCallback, useEffect, useState } from 'react'
import { postJSON, usePoll } from '../lib/api'
import { useDemo } from '../lib/demo'
import { tone } from '../lib/tone'
import { Icon, NoData, Section, SectionHead, SkeletonRows } from './common'
import { logSources } from './LogViewer'

const STATUS = {
  ok: ['ok', 'Connected', 'ph-check-circle'],
  warn: ['warn', 'Partly set up', 'ph-warning'],
  off: ['neutral', 'Not set up', 'ph-plug'],
  error: ['crit', "Can't connect", 'ph-x-circle'],
}

function StatusPill({ s }) {
  if (!s) return <span className="muted" style={{ fontSize: 11.5, display: 'inline-flex', gap: 5, alignItems: 'center' }}><Icon name="ph-circle-notch" className="spin" />checking…</span>
  const [t, label, icon] = STATUS[s.status] || ['unk', s.status, 'ph-question']
  const x = tone(t)
  return (
    <span style={{ display: 'inline-flex', alignItems: 'center', gap: 4, padding: '1px 8px', borderRadius: 6, fontSize: 11.5, fontWeight: 500, whiteSpace: 'nowrap', color: x.c, background: x.tint, border: `1px solid ${x.line}` }}>
      <Icon name={icon} />{label}
    </span>
  )
}

function Protection({ protectedNow, onProtected }) {
  const [token, setToken] = useState(null)
  const [err, setErr] = useState(null)
  const [busy, setBusy] = useState(false)
  const [copied, setCopied] = useState(false)
  const protect = async () => {
    setBusy(true); setErr(null)
    try { const r = await postJSON('/api/settings/protect'); setToken(r.token); onProtected() } catch (e) { setErr(e.message) }
    setBusy(false)
  }
  const logout = async () => { await postJSON('/api/logout'); location.reload() }
  const copy = async () => { try { await navigator.clipboard.writeText(token); setCopied(true) } catch { /* blocked */ } }

  return (
    <div className="surface" style={{ display: 'flex', gap: 14, alignItems: 'flex-start', flexWrap: 'wrap', padding: 16, border: protectedNow ? undefined : '1px solid color-mix(in srgb, var(--st-warn) 45%, transparent)' }}>
      <div style={{ width: 36, height: 36, borderRadius: 10, display: 'grid', placeItems: 'center', flex: 'none', color: protectedNow ? 'var(--st-ok)' : 'var(--st-warn)', background: `color-mix(in srgb, ${protectedNow ? 'var(--st-ok)' : 'var(--st-warn)'} 12%, transparent)` }}>
        <Icon name={protectedNow ? 'ph-lock-key' : 'ph-lock-key-open'} size={19} />
      </div>
      <div style={{ flex: '1 1 320px', minWidth: 0, display: 'flex', flexDirection: 'column', gap: 6 }}>
        <div style={{ fontSize: 14, fontWeight: 500 }}>{protectedNow ? 'This dashboard is protected' : 'This dashboard has no login'}</div>
        <div className="muted" style={{ fontSize: 12.5 }}>
          {protectedNow
            ? 'Every page and action needs the dashboard token. You can save credentials below.'
            : "Anyone who can open this page can control your systems. Protect it before saving credentials or sharing it — you'll get a token to log in with."}
        </div>
        {token && (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 6, marginTop: 4 }}>
            <div style={{ fontSize: 12.5, fontWeight: 500, color: 'var(--st-warn)' }}>Save this token now — it won't be shown again.</div>
            <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
              <code className="term" style={{ padding: '7px 10px', fontSize: 12.5, userSelect: 'all' }}>{token}</code>
              <button className="btn btn-secondary" onClick={copy}><Icon name={copied ? 'ph-check' : 'ph-copy'} />{copied ? 'Copied' : 'Copy'}</button>
            </div>
            <div className="muted" style={{ fontSize: 11.5 }}>It's stored in your OS keychain as DASHBOARD_TOKEN. This browser is already logged in.</div>
          </div>
        )}
        {err && <div style={{ fontSize: 12, color: 'var(--st-crit)' }}><Icon name="ph-x-circle" /> {err}</div>}
      </div>
      {!protectedNow && !token && <button className="btn btn-primary" onClick={protect} disabled={busy}><Icon name="ph-lock-key" />{busy ? 'Protecting…' : 'Protect this dashboard'}</button>}
      {protectedNow && !token && <button className="btn btn-secondary" onClick={logout}><Icon name="ph-sign-out" />Log out</button>}
    </div>
  )
}

function Field({ f, value, onChange, onClear, disabled }) {
  const id = `set-${f.name}`
  const label = (
    <label htmlFor={id} className="muted" style={{ fontSize: 11.5, display: 'flex', alignItems: 'center', gap: 6 }}>
      {f.label}<span className="mono" style={{ opacity: .7, fontSize: 10.5 }}>{f.name}</span>
      {f.set && !disabled && <button type="button" className="btn btn-ghost" onClick={onClear} style={{ marginLeft: 'auto', padding: '0 4px', fontSize: 11, color: 'var(--muted)' }}>Remove</button>}
    </label>
  )
  let input
  if (f.kind === 'select') {
    input = (
      <select id={id} className="input mono" value={value ?? f.value ?? ''} onChange={e => onChange(e.target.value)} disabled={disabled} style={{ fontSize: 12.5 }}>
        {f.options.map(o => <option key={o} value={o}>{o}</option>)}
      </select>
    )
  } else if (f.secret) {
    input = (
      <input id={id} className="input mono" type="password" autoComplete="off" value={value ?? ''} disabled={disabled}
        placeholder={f.set ? `•••••••• saved in ${f.where} — type to replace` : 'not set'}
        onChange={e => onChange(e.target.value)} style={{ fontSize: 12.5 }} />
    )
  } else {
    input = (
      <input id={id} className="input mono" type={f.kind === 'url' ? 'url' : 'text'} value={value ?? f.value ?? ''} disabled={disabled}
        placeholder="not set" onChange={e => onChange(e.target.value)} style={{ fontSize: 12.5 }} />
    )
  }
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 5, minWidth: 0 }}>
      {label}{input}
      {f.warning && <span style={{ fontSize: 11, color: 'var(--st-warn)' }}><Icon name="ph-warning" /> {f.warning}</span>}
    </div>
  )
}

function IntegrationCard({ integ, status, canSave, blockedReason, onSaved, onCheck }) {
  const [draft, setDraft] = useState({})
  const [clear, setClear] = useState([])
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState(null)
  const dirty = Object.values(draft).some(v => v !== undefined && v !== '') || clear.length > 0

  const save = async () => {
    setBusy(true); setMsg(null)
    try {
      const r = await postJSON('/api/settings', { values: Object.fromEntries(Object.entries(draft).filter(([, v]) => v !== '')), clear })
      setDraft({}); setClear([])
      setMsg({ ok: true, text: ['Saved.', ...r.notes, r.restart_note].join(' ') })
      onSaved()
    } catch (e) {
      setMsg({ ok: false, text: e.message })
    }
    setBusy(false)
  }
  const slackTest = async () => {
    setMsg(null)
    try { const r = await postJSON('/api/settings/slack-test'); setMsg({ ok: r.sent, text: r.sent ? 'Test message sent — check your Slack channel.' : r.detail }) }
    catch (e) { setMsg({ ok: false, text: e.message }) }
  }

  return (
    <div className="surface" style={{ display: 'flex', flexDirection: 'column', gap: 12, padding: 16 }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
        <Icon name={integ.icon} size={20} style={{ color: 'var(--color-accent)' }} />
        <span style={{ fontSize: 14.5, fontWeight: 500, flex: 1 }}>{integ.name}</span>
        <StatusPill s={status} />
      </div>
      {status?.detail && <div style={{ fontSize: 12, color: status.status === 'error' ? 'var(--st-crit)' : 'var(--muted)', wordBreak: 'break-word' }}>{status.detail}</div>}
      <div className="muted" style={{ fontSize: 12 }}>{integ.help}</div>
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(220px, 1fr))', gap: 10 }}>
        {integ.fields.map(f => (
          <Field key={f.name} f={clear.includes(f.name) ? { ...f, set: false, value: '' } : f} value={draft[f.name]} disabled={!canSave}
            onChange={v => setDraft(d => ({ ...d, [f.name]: v }))}
            onClear={() => setClear(c => (c.includes(f.name) ? c : [...c, f.name]))} />
        ))}
      </div>
      {clear.length > 0 && <div style={{ fontSize: 12, color: 'var(--st-warn)' }}><Icon name="ph-warning" /> Will remove: {clear.join(', ')}</div>}
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
        <button className="btn btn-primary" onClick={save} disabled={!canSave || !dirty || busy}><Icon name="ph-floppy-disk" />{busy ? 'Saving…' : 'Save'}</button>
        {(dirty) && <button className="btn btn-ghost" onClick={() => { setDraft({}); setClear([]) }}>Discard</button>}
        <button className="btn btn-secondary" onClick={onCheck}><Icon name="ph-plugs-connected" />Test connection</button>
        {integ.id === 'slack' && <button className="btn btn-secondary" onClick={slackTest} disabled={status?.status === 'off'}><Icon name="ph-paper-plane-tilt" />Send test message</button>}
        {!canSave && <span className="muted" style={{ fontSize: 11.5 }}>{blockedReason}</span>}
      </div>
      {msg && <div style={{ fontSize: 12, color: msg.ok ? 'var(--st-ok)' : 'var(--st-crit)' }}><Icon name={msg.ok ? 'ph-check-circle' : 'ph-x-circle'} /> {msg.text}</div>}
    </div>
  )
}

export default function Settings({ live, context, onNav, onLogs }) {
  const demo = useDemo()
  const settings = usePoll('/api/settings')
  const sysLogs = usePoll('/api/logs/system')
  const [status, setStatus] = useState({})

  const check = useCallback(async id => {
    setStatus(s => ({ ...s, [id]: null }))
    try { const r = await postJSON(`/api/settings/check/${id}`); setStatus(s => ({ ...s, [id]: r })) }
    catch (e) { setStatus(s => ({ ...s, [id]: { status: 'error', detail: e.message } })) }
  }, [])

  const ids = settings.data?.integrations.map(i => i.id).join(',')
  useEffect(() => { if (ids) ids.split(',').forEach(check) }, [ids, check])

  const d = settings.data
  const canSave = !!d?.protected && !demo
  const blockedReason = demo ? 'Saving is disabled in demo mode.' : 'Protect the dashboard (above) to save credentials.'

  return (
    <div data-screen-label="08 Settings" style={{ display: 'flex', flexDirection: 'column', gap: 22 }}>
      {!d && settings.loading && <SkeletonRows rows={4} />}
      {!d && settings.error && <NoData label="Settings" note="Couldn't read AtlasOS settings." error={settings.error} onRetry={() => settings.reload()} />}
      {d && (
        <>
          <Protection protectedNow={d.protected} onProtected={() => settings.reload()} />
          {!d.keychain && (
            <div className="nodata"><div style={{ color: 'var(--st-unk)' }}><Icon name="ph-question" /> No OS keychain found on this machine — secrets can't be saved from here.</div></div>
          )}

          <Section>
            <SectionHead title="Integrations" note={d.keychain ? `secrets are stored in ${d.keychain}, never shown again` : null}>
              <button className="btn btn-ghost" onClick={() => d.integrations.forEach(i => check(i.id))} style={{ fontSize: 12 }}><Icon name="ph-arrow-clockwise" />Re-test all</button>
            </SectionHead>
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(420px, 1fr))', gap: 12 }}>
              {/* Kubernetes is connected through kubeconfig, not a setting — status + a link to the connect form */}
              <div className="surface" style={{ display: 'flex', flexDirection: 'column', gap: 12, padding: 16 }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
                  <Icon name="ph-cube" size={20} style={{ color: 'var(--color-accent)' }} />
                  <span style={{ fontSize: 14.5, fontWeight: 500, flex: 1 }}>Kubernetes</span>
                  <StatusPill s={!live ? null : live.offline ? { status: context ? 'error' : 'off' } : { status: 'ok' }} />
                </div>
                <div className="muted" style={{ fontSize: 12 }}>
                  Uses your kubeconfig. Current context: <span className="mono">{context || 'none'}</span>
                  {live?.offline ? ' — the API server is not reachable.' : ''}
                </div>
                <div><button className="btn btn-secondary" onClick={() => onNav('cluster')}><Icon name="ph-plugs-connected" />Connect or switch cluster</button></div>
              </div>
              {d.integrations.map(i => (
                <IntegrationCard key={i.id} integ={i} status={status[i.id]} canSave={canSave} blockedReason={blockedReason}
                  onCheck={() => check(i.id)} onSaved={() => { settings.reload(); check(i.id) }} />
              ))}
            </div>
          </Section>

          <Section>
            <SectionHead title="AtlasOS logs" note="credentials are masked" />
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(210px, 1fr))', gap: 8 }}>
              {(sysLogs.data?.logs || []).map(l => (
                <button key={l.name} className="hoverable" disabled={!l.exists} onClick={() => onLogs(logSources.system(l.name, l.label))}
                  style={{ display: 'flex', alignItems: 'center', gap: 9, padding: '10px 12px', borderRadius: 'var(--radius-md)', border: '1px solid var(--color-divider)', background: 'var(--color-surface)', textAlign: 'left', opacity: l.exists ? 1 : 0.5 }}>
                  <Icon name="ph-scroll" size={17} style={{ color: 'var(--color-accent)' }} />
                  <span style={{ flex: 1, fontSize: 13 }}>{l.label}</span>
                  {!l.exists && <span className="muted" style={{ fontSize: 11 }}>no file yet</span>}
                </button>
              ))}
            </div>
          </Section>
        </>
      )}
    </div>
  )
}
