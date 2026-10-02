import { useState } from 'react'
import { postJSON } from '../lib/api'
import { useData, useDemo } from '../lib/demo'
import { tone } from '../lib/tone'
import { Card, ConfirmButton, CopyButton, Field, Icon, Msg, NoData, Pill, Section, SectionHead, SkeletonRows } from './common'

const CHECKS = [
  ['lb', 'Load balancer'], ['dns', 'DNS'], ['tls_section', 'Ingress TLS'], ['tls_cert', 'Certificate'],
  ['tls_secret', 'TLS secret'], ['http', 'HTTP'], ['https', 'HTTPS'], ['backend', 'Backend pods'],
]
const OVERALL_T = { live: 'ok', degraded: 'warn', down: 'crit' }

function CheckChip({ label, c }) {
  const t = c == null || c.ok == null ? 'unk' : c.ok ? 'ok' : 'crit'
  const x = tone(t)
  const why = c?.error || c?.note || c?.warning || (c?.status_code ? `HTTP ${c.status_code}` : '') || c?.address || ''
  return (
    <span title={why} style={{ display: 'inline-flex', alignItems: 'center', gap: 4, padding: '2px 8px', borderRadius: 6, fontSize: 11.5, color: x.c, background: x.tint, border: `1px ${x.bs} ${x.line}` }}>
      <Icon name={x.icon} />{label}
    </span>
  )
}

function DomainCard({ r, onAsk }) {
  if (r.found === false) return <Card warn><div style={{ fontSize: 13 }}><Icon name="ph-warning" style={{ color: 'var(--st-warn)' }} /> {r.error}</div></Card>
  const dg = r.diagnosis || {}
  const t = OVERALL_T[dg.overall_status] || 'unk'
  return (
    <Card warn={t !== 'ok'}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
        <Icon name="ph-globe-hemisphere-west" size={19} style={{ color: 'var(--color-accent)' }} />
        <a href={`https://${r.domain}`} target="_blank" rel="noreferrer" className="mono" style={{ fontSize: 14, fontWeight: 500 }}>{r.domain} ↗</a>
        <span className="muted mono" style={{ fontSize: 11.5 }}>{r.ingress?.namespace}/{r.ingress?.name}</span>
        <span style={{ marginLeft: 'auto' }}><Pill t={t} label={dg.overall_status || 'unknown'} /></span>
      </div>
      <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
        {CHECKS.map(([k, label]) => <CheckChip key={k} label={label} c={r.checks?.[k]} />)}
      </div>
      {dg.root_cause && t !== 'ok' && <div style={{ fontSize: 12.5 }}><b>Why:</b> {dg.root_cause}</div>}
      {dg.suggested_fix && t !== 'ok' && <div className="muted" style={{ fontSize: 12.5 }}>{dg.suggested_fix}</div>}
      {dg.fix_command && t !== 'ok' && (
        <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
          <code className="term" style={{ padding: '5px 8px', fontSize: 11.5, wordBreak: 'break-all', flex: '1 1 300px' }}>{dg.fix_command}</code>
          <CopyButton text={dg.fix_command} />
        </div>
      )}
      {t !== 'ok' && (
        <div><button className="btn btn-primary" style={{ fontSize: 12 }} onClick={() => onAsk(`My domain ${r.domain} is ${dg.overall_status}. ${dg.root_cause || ''} Diagnose it and fix it (ask me to confirm any change).`)}><Icon name="ph-sparkle" />Fix with the assistant</button></div>
      )}
    </Card>
  )
}

const ENC_EMPTY = { domain: '', email: '', namespace: 'default', ingress: '', staging: false }

function EnableHttps({ onDone }) {
  const demo = useDemo()
  const [f, setF] = useState(ENC_EMPTY)
  const [busy, setBusy] = useState(false)
  const [res, setRes] = useState(null)
  const [err, setErr] = useState(null)
  const set = (k, v) => setF(x => ({ ...x, [k]: v }))
  const go = async () => {
    setBusy(true); setErr(null); setRes(null)
    try { setRes(await postJSON('/api/domains/encrypt', f)); onDone() } catch (e) { setErr(e.message) }
    setBusy(false)
  }
  const valid = f.domain && f.email && f.namespace
  return (
    <Card>
      <div style={{ fontSize: 13.5, fontWeight: 500 }}>Turn on HTTPS for a domain <span className="muted" style={{ fontWeight: 400, fontSize: 12 }}>— free Let's Encrypt certificate, renewed automatically by cert-manager</span></div>
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(190px, 1fr))', gap: 10 }}>
        <Field id="e-dom" label="Domain"><input id="e-dom" className="input mono" placeholder="app.example.com" value={f.domain} onChange={e => set('domain', e.target.value.trim().toLowerCase())} /></Field>
        <Field id="e-mail" label="Email for expiry notices"><input id="e-mail" className="input" type="email" placeholder="you@example.com" value={f.email} onChange={e => set('email', e.target.value.trim())} /></Field>
        <Field id="e-ns" label="Namespace of its Ingress"><input id="e-ns" className="input mono" value={f.namespace} onChange={e => set('namespace', e.target.value.trim())} /></Field>
        <Field id="e-ing" label="Ingress name (optional)" hint="found automatically if blank"><input id="e-ing" className="input mono" value={f.ingress} onChange={e => set('ingress', e.target.value.trim())} /></Field>
      </div>
      <label style={{ display: 'flex', gap: 8, alignItems: 'center', fontSize: 12.5 }}>
        <input type="checkbox" id="e-stg" checked={f.staging} onChange={e => set('staging', e.target.checked)} />
        Use Let's Encrypt staging first <span className="muted">(untrusted test certificate, no rate limits — good for a first try)</span>
      </label>
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
        <ConfirmButton icon="ph-lock-key" onConfirm={go} busy={busy} disabled={demo || !valid} confirmLabel="Change the cluster?">Enable HTTPS</ConfirmButton>
        <span className="muted" style={{ fontSize: 11.5 }}>Creates a ClusterIssuer and Certificate and adds TLS to the Ingress. The domain's DNS must already point at the cluster.</span>
      </div>
      {err && <Msg msg={{ ok: false, text: err }} />}
      {res && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
          {(res.steps || []).map((s, i) => (
            <div key={i} style={{ fontSize: 12.5 }}><Icon name={s.ok ? 'ph-check' : 'ph-x'} style={{ color: s.ok ? 'var(--st-ok)' : 'var(--st-crit)' }} /> {s.label} {s.detail && <span className="muted">— {s.detail}</span>}</div>
          ))}
          <Msg msg={res.ok ? { ok: true, text: 'Requested. The certificate is usually issued within 1–3 minutes — Re-check to see it go green.' } : { ok: false, text: res.error || 'Some steps failed — see above.' }} />
        </div>
      )}
    </Card>
  )
}

export default function Domains({ onAsk }) {
  const domains = useData('domains', '/api/domains', 0)
  const d = domains.data
  return (
    <div data-screen-label="Domains" style={{ display: 'flex', flexDirection: 'column', gap: 22 }}>
      <Section>
        <SectionHead title="Your domains" note="every Ingress, checked from DNS to the pods behind it">
          <button className="btn btn-secondary" onClick={() => domains.reload()} disabled={domains.loading}><Icon name="ph-arrow-clockwise" className={domains.loading ? 'spin' : undefined} />Re-check</button>
        </SectionHead>
        {!d && domains.loading && <SkeletonRows rows={3} />}
        {!d && domains.error && <NoData note="Couldn't check your domains." error={domains.error} onRetry={() => domains.reload()} />}
        {d && !d.reachable && <NoData label="Cluster" note={'The cluster isn\'t reachable, so domains couldn\'t be checked — this is not "all fine".'} onRetry={() => domains.reload()} />}
        {d?.error && <NoData note="The check didn't finish." error={d.error} />}
        {d?.reachable && !d.error && d.results.length === 0 && <div className="muted" style={{ fontSize: 12.5 }}>No Ingress with a host name in this cluster yet.</div>}
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(420px, 1fr))', gap: 10, alignItems: 'start' }}>
          {d?.results?.map((r, i) => <DomainCard key={`${r.domain}-${i}`} r={r} onAsk={onAsk} />)}
        </div>
      </Section>
      <Section>
        <SectionHead title="HTTPS" />
        <EnableHttps onDone={() => domains.reload()} />
      </Section>
    </div>
  )
}
