import { useState } from 'react'
import { ago, getJSON, postJSON } from '../lib/api'
import { useDemo } from '../lib/demo'
import { tone } from '../lib/tone'
import { Icon, NoData, OkEmpty, Pill, Section, SectionHead, SkeletonRows } from './common'

const EV_ICON = { opened: 'ph-siren', acknowledged: 'ph-eye', resolved: 'ph-check-circle', escalated: 'ph-arrow-fat-up',
  fix_attempt: 'ph-wrench', page_resolved: 'ph-bell-slash', slo_burn_started: 'ph-gauge' }

function IncidentRow({ i, onAsk, onChanged }) {
  const demo = useDemo()
  const [open, setOpen] = useState(false)
  const [detail, setDetail] = useState(null)
  const [resolving, setResolving] = useState(false)
  const [note, setNote] = useState('')
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState(null)
  const t = SEV_T[(i.severity || '').toLowerCase()] || 'warn'
  const acked = detail?.events?.some(e => e.event_type === 'acknowledged')

  const loadDetail = async () => {
    try { setDetail(await getJSON(`/api/incidents/${i.id}`)) } catch (e) { setMsg({ ok: false, text: e.message }) }
  }
  const toggle = () => { const next = !open; setOpen(next); if (next && !detail && !demo) loadDetail() }
  const ack = async () => {
    setBusy(true); setMsg(null)
    try { await postJSON(`/api/incidents/${i.id}/ack`); setMsg({ ok: true, text: 'Acknowledged — the timeline and Slack show you are on it.' }); await loadDetail(); setOpen(true) }
    catch (e) { setMsg({ ok: false, text: e.message }) }
    setBusy(false)
  }
  const resolve = async () => {
    setBusy(true); setMsg(null)
    try { await postJSON(`/api/incidents/${i.id}/resolve`, { note }); setMsg({ ok: true, text: 'Resolved. Any on-call page for it was closed too.' }); onChanged() }
    catch (e) { setMsg({ ok: false, text: e.message }) }
    setBusy(false)
  }

  return (
    <div style={{ padding: '11px 0', background: 'var(--rule) no-repeat bottom / 100% 1px', display: 'flex', flexDirection: 'column', gap: 8 }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 12, flexWrap: 'wrap' }}>
        <Pill t={t} label={i.severity || 'unknown'} icon={false} mono />
        <div style={{ flex: '1 1 260px', minWidth: 0 }}>
          <div style={{ fontSize: 13.5, fontWeight: 500 }}>{i.title}</div>
          <div className="muted" style={{ fontSize: 11.5 }}>
            <span className="mono">{String(i.id).slice(0, 8)}</span> · {[i.service, i.namespace].filter(Boolean).join(' · ')} · opened {ago(i.opened_at)}
            {acked && <span style={{ color: 'var(--color-accent)' }}> · acknowledged</span>}
          </div>
        </div>
        <span className="mono" style={{ display: 'inline-flex', alignItems: 'center', gap: 5, fontSize: 12 }}><Icon name="ph-clock" style={{ color: 'var(--muted)' }} />{since(i.opened_at)}</span>
        <div style={{ display: 'flex', gap: 4, flexWrap: 'wrap' }}>
          <button className="btn btn-ghost" style={{ fontSize: 12, padding: '3px 6px' }} onClick={toggle}><Icon name={open ? 'ph-caret-up' : 'ph-list-bullets'} />Timeline</button>
          <button className="btn btn-ghost" style={{ fontSize: 12, padding: '3px 6px' }} onClick={() => onAsk(`Summarize incident ${i.id} ("${i.title}") and what's likely causing it.`)}><Icon name="ph-sparkle" />Summarize</button>
          <button className="btn btn-secondary" style={{ fontSize: 12, padding: '3px 8px' }} onClick={ack} disabled={demo || busy || acked}><Icon name="ph-eye" />{acked ? 'Acknowledged' : 'Acknowledge'}</button>
          <button className="btn btn-primary" style={{ fontSize: 12, padding: '3px 8px' }} onClick={() => setResolving(r => !r)} disabled={demo || busy}><Icon name="ph-check" />Resolve</button>
        </div>
      </div>

      {resolving && (
        <form onSubmit={e => { e.preventDefault(); resolve() }} style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap', padding: '10px 12px', borderRadius: 'var(--radius-md)', background: 'color-mix(in srgb, var(--color-text) 4%, transparent)' }}>
          <input id={`resolve-note-${i.id}`} className="input" value={note} onChange={e => setNote(e.target.value)} placeholder="What fixed it? (optional — saved on the incident)" style={{ flex: '1 1 260px', fontSize: 12.5 }} />
          <button className="btn btn-primary" type="submit" disabled={busy}>{busy ? 'Resolving…' : 'Confirm resolve'}</button>
          <button className="btn btn-ghost" type="button" onClick={() => setResolving(false)}>Cancel</button>
          <span className="muted" style={{ fontSize: 11.5, flexBasis: '100%' }}>Also closes its PagerDuty/OpsGenie page and stops its SLO error-budget burn.</span>
        </form>
      )}
      {msg && <div style={{ fontSize: 12, color: msg.ok ? 'var(--st-ok)' : 'var(--st-crit)' }}><Icon name={msg.ok ? 'ph-check-circle' : 'ph-x-circle'} /> {msg.text}</div>}

      {open && (
        <div style={{ paddingLeft: 8, display: 'flex', flexDirection: 'column', gap: 6 }}>
          {demo && <div className="muted" style={{ fontSize: 12 }}>Timelines aren't available in demo mode.</div>}
          {!demo && !detail && <div className="muted" style={{ fontSize: 12 }}><Icon name="ph-circle-notch" className="spin" /> Loading…</div>}
          {detail?.events?.length === 0 && <div className="muted" style={{ fontSize: 12 }}>No timeline events recorded.</div>}
          {detail?.events?.map((e, k) => (
            <div key={k} style={{ display: 'flex', gap: 9, alignItems: 'flex-start', fontSize: 12.5 }}>
              <Icon name={EV_ICON[e.event_type] || 'ph-dot-outline'} style={{ color: 'var(--color-accent)', marginTop: 2 }} />
              <span className="mono muted" style={{ fontSize: 11, minWidth: 64 }}>{ago(e.timestamp)}</span>
              <span style={{ fontWeight: 500 }}>{e.event_type.replace(/_/g, ' ')}</span>
              {e.detail && <span className="muted" style={{ minWidth: 0, wordBreak: 'break-word' }}>{e.detail}</span>}
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

const SEV_T = { critical: 'crit', high: 'crit', sev1: 'crit', warning: 'warn', medium: 'warn', sev2: 'warn', low: 'neutral', info: 'neutral' }
const SLO_T = { healthy: 'ok', ok: 'ok', warning: 'warn', at_risk: 'warn', critical: 'crit', exhausted: 'crit', breached: 'crit' }

function since(ts) {
  const ms = Date.now() - Date.parse(ts)
  if (Number.isNaN(ms)) return '—'
  const m = Math.floor(ms / 60000)
  return m < 60 ? `${m}m` : m < 1440 ? `${Math.floor(m / 60)}h ${m % 60}m` : `${Math.floor(m / 1440)}d`
}

export default function Incidents({ incidents, slos, onAsk, onChanged }) {
  const open = incidents.data?.filter(i => i.status === 'open') || []
  const closed = incidents.data?.filter(i => i.status !== 'open') || []
  const sloList = Array.isArray(slos.data) ? slos.data : []

  return (
    <div data-screen-label="05 Incidents & SLOs" style={{ display: 'flex', flexDirection: 'column', gap: 22 }}>
      <Section>
        <SectionHead title="Open incidents" note={incidents.data ? `${open.length} open` : null} />
        {!incidents.data && incidents.loading && <SkeletonRows rows={2} />}
        {!incidents.data && incidents.error && <NoData note={'Not "no incidents" — the incident store couldn\'t be read.'} error={incidents.error} />}
        {incidents.data && open.length === 0 && (
          <OkEmpty title="No open incidents" sub={closed[0] ? `Last one, ${closed[0].title}, opened ${ago(closed[0].opened_at)}.` : 'None recorded yet.'} />
        )}
        {open.length > 0 && (
          <div className="surface" style={{ display: 'flex', flexDirection: 'column', padding: '2px 14px' }}>
            {open.map(i => <IncidentRow key={i.id} i={i} onAsk={onAsk} onChanged={onChanged} />)}
          </div>
        )}
      </Section>

      {closed.length > 0 && (
        <Section>
          <SectionHead title="Recently resolved" note={`last ${Math.min(closed.length, 6)}`} />
          <div className="surface" style={{ display: 'flex', flexDirection: 'column', padding: '2px 14px' }}>
            {closed.slice(0, 6).map(i => (
              <div key={i.id} style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap', padding: '9px 0', background: 'var(--rule) no-repeat bottom / 100% 1px', fontSize: 12.5 }}>
                <Icon name="ph-check-circle" style={{ color: 'var(--st-ok)' }} />
                <span style={{ flex: '1 1 260px', minWidth: 0 }}>{i.title}</span>
                <span className="muted" style={{ fontSize: 11.5 }}>resolved {ago(i.resolved_at)}</span>
              </div>
            ))}
          </div>
        </Section>
      )}

      <Section>
        <SectionHead title="SLOs · error budget remaining" />
        {!slos.data && slos.loading && <SkeletonRows rows={2} />}
        {(slos.error || slos.data?.error) && <NoData note="The SLO store couldn't be read." error={slos.error || slos.data?.error} />}
        {slos.data && !slos.data.error && sloList.length === 0 && <div className="muted" style={{ fontSize: 12.5 }}>No SLOs defined. Create one with <code>agent slo create</code>.</div>}
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(230px, 1fr))', gap: 10 }}>
          {sloList.map(s => {
            const x = tone(SLO_T[s.status] || (s.budget_pct >= 50 ? 'ok' : s.budget_pct >= 20 ? 'warn' : 'crit'))
            return (
              <div key={s.id} style={{ display: 'flex', flexDirection: 'column', gap: 8, padding: 14, borderRadius: 'var(--radius-md)', background: 'var(--color-surface)', border: `1px solid ${x.t === 'ok' ? 'var(--color-divider)' : x.line}` }}>
                <div style={{ display: 'flex', alignItems: 'flex-start', gap: 8 }}>
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <div style={{ fontSize: 13.5, fontWeight: 500 }}>{s.name}</div>
                    <div className="mono muted" style={{ fontSize: 11 }}>{s.target_pct}% · {s.window_days}d · {s.service}</div>
                  </div>
                  <Icon name={x.icon} size={16} style={{ color: x.c }} />
                </div>
                <div style={{ display: 'flex', alignItems: 'baseline', gap: 6 }}>
                  <span style={{ fontSize: 22, fontWeight: 500, letterSpacing: '-0.01em', color: x.t === 'ok' ? 'var(--color-text)' : x.c }}>{Math.round(s.budget_pct)}%</span>
                  <span className="muted" style={{ fontSize: 11.5 }}>of budget left</span>
                </div>
                <div style={{ height: 6, borderRadius: 3, background: 'color-mix(in srgb, var(--color-text) 9%, transparent)', overflow: 'hidden' }}>
                  <div style={{ height: '100%', width: `${Math.max(0, Math.min(100, s.budget_pct))}%`, background: x.c, borderRadius: 3 }} />
                </div>
                <div className="muted" style={{ fontSize: 11.5 }}>{s.used_minutes} of {s.budget_minutes} min used</div>
              </div>
            )
          })}
        </div>
      </Section>
    </div>
  )
}
