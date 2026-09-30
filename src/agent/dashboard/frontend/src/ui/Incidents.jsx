import { ago } from '../lib/api'
import { tone } from '../lib/tone'
import { Icon, NoData, OkEmpty, Pill, Section, SectionHead, SkeletonRows } from './common'

const SEV_T = { critical: 'crit', high: 'crit', sev1: 'crit', warning: 'warn', medium: 'warn', sev2: 'warn', low: 'neutral', info: 'neutral' }
const SLO_T = { healthy: 'ok', ok: 'ok', warning: 'warn', at_risk: 'warn', critical: 'crit', exhausted: 'crit', breached: 'crit' }

function since(ts) {
  const ms = Date.now() - Date.parse(ts)
  if (Number.isNaN(ms)) return '—'
  const m = Math.floor(ms / 60000)
  return m < 60 ? `${m}m` : m < 1440 ? `${Math.floor(m / 60)}h ${m % 60}m` : `${Math.floor(m / 1440)}d`
}

export default function Incidents({ incidents, slos, onAsk }) {
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
            {open.map(i => {
              const t = SEV_T[(i.severity || '').toLowerCase()] || 'warn'
              return (
                <div key={i.id} style={{ display: 'flex', alignItems: 'center', gap: 12, flexWrap: 'wrap', padding: '11px 0', background: 'var(--rule) no-repeat bottom / 100% 1px' }}>
                  <Pill t={t} label={i.severity || 'unknown'} icon={false} mono />
                  <span className="mono muted" style={{ fontSize: 12 }}>{String(i.id).slice(0, 10)}</span>
                  <div style={{ flex: '1 1 260px', minWidth: 0 }}>
                    <div style={{ fontSize: 13.5, fontWeight: 500 }}>{i.title}</div>
                    <div className="muted" style={{ fontSize: 11.5 }}>{[i.service, i.namespace].filter(Boolean).join(' · ')} · opened {ago(i.opened_at)}</div>
                  </div>
                  <span className="mono" style={{ display: 'inline-flex', alignItems: 'center', gap: 5, fontSize: 12 }}><Icon name="ph-clock" style={{ color: 'var(--muted)' }} />{since(i.opened_at)}</span>
                  <button className="btn btn-ghost" style={{ fontSize: 12, padding: '3px 6px' }} onClick={() => onAsk(`Summarize incident ${i.id} ("${i.title}") and what's likely causing it.`)}>
                    <Icon name="ph-sparkle" />Summarize
                  </button>
                </div>
              )
            })}
          </div>
        )}
      </Section>

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
