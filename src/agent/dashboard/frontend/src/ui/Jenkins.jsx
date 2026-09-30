import { ago, dur } from '../lib/api'
import { BUILD_TONE, TC } from '../lib/tone'
import { Icon, NoData, OkEmpty, Pill, Section, SectionHead, SkeletonRows, TableCard } from './common'

const TREND_C = { SUCCESS: TC.ok, FAILURE: TC.crit, UNSTABLE: TC.warn, ABORTED: 'var(--muted)', RUNNING: TC.accent }

export default function Jenkins({ builds, onAsk }) {
  const d = builds.data
  if (!d && builds.loading) return <SkeletonRows rows={6} />
  if (!d) return <NoData label="Failed builds · Jobs" note="Not &quot;no failed builds&quot; — the dashboard server didn't answer." error={builds.error} onRetry={() => builds.reload()} />
  if (!d.configured) return <NoData label="Jenkins" note="Jenkins isn't configured (JENKINS_URL is not set)." />
  if (!d.connected) return <NoData label="Failed builds · Jobs" note={'Not "no failed builds" — AtlasOS couldn\'t ask Jenkins.'} error={d.error} onRetry={() => builds.reload()} />

  const dayAgo = Date.now() - 86400000
  return (
    <div data-screen-label="03 Jenkins" style={{ display: 'flex', flexDirection: 'column', gap: 22 }}>
      <Section>
        <SectionHead title="Failed builds" note={d.failures.length ? `${d.failures.length} in the last 7 days` : null} />
        {d.failures.length === 0 && <OkEmpty title="No failed builds in the last 7 days" sub={`${d.jobs.length} jobs checked ${ago(builds.at)}`} />}
        {d.failures.map((b, i) => {
          const showEarlier = b.timestamp < dayAgo && (i === 0 || d.failures[i - 1].timestamp >= dayAgo)
          return (
            <div key={`${b.job}#${b.number}`} style={{ display: 'contents' }}>
              {showEarlier && <div className="kicker" style={{ marginTop: 8 }}>Earlier this week</div>}
              <article className="surface" style={{ display: 'grid', gridTemplateColumns: 'minmax(0,1fr) auto', gap: '10px 18px', padding: '14px 16px' }}>
                <div style={{ display: 'flex', flexDirection: 'column', gap: 6, minWidth: 0 }}>
                  <div className="muted" style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap', fontSize: 12 }}>
                    <Icon name="ph-x-circle" size={15} style={{ color: 'var(--st-crit)' }} />
                    <span className="mono" style={{ fontSize: 12.5, color: 'var(--color-text)' }}>{b.job} #{b.number}</span>
                    <span>{ago(b.timestamp)} · ran {dur(b.duration_ms)}{b.node ? ` on ${b.node}` : ''}</span>
                  </div>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 5, fontSize: 10.5, letterSpacing: '.08em', textTransform: 'uppercase', color: 'var(--color-accent-300)', marginTop: 2 }}>
                    <Icon name="ph-sparkle" />Root cause
                  </div>
                  <div className="muted" style={{ fontSize: 12.5, maxWidth: '78ch' }}>
                    Not diagnosed here — diagnosis reads the build log and asks Claude, so it only runs when you ask.
                    Builds reported by the Jenkins webhook are diagnosed automatically and posted to Slack.
                  </div>
                </div>
                <div style={{ display: 'flex', flexDirection: 'column', gap: 6, minWidth: 150 }}>
                  <button className="btn btn-primary" onClick={() => onAsk(`Diagnose Jenkins build ${b.job} #${b.number}. What's the root cause and can it be fixed automatically?`)}>
                    <Icon name="ph-sparkle" />Diagnose
                  </button>
                  {d.url && (
                    <a className="btn btn-secondary" href={`${d.url.replace(/\/$/, '')}/job/${b.job.split('/').map(encodeURIComponent).join('/job/')}/${b.number}/console`} target="_blank" rel="noreferrer">
                      <Icon name="ph-file-text" />Console log
                    </a>
                  )}
                </div>
              </article>
            </div>
          )
        })}
      </Section>

      <Section>
        <SectionHead title="Jobs" note={`${d.jobs.length} jobs · ${d.url || ''}`} />
        <TableCard>
          <table className="table">
            <thead><tr><th>Job</th><th>Last build</th><th>Status</th><th>Duration</th><th>Finished</th><th>Last 10</th></tr></thead>
            <tbody>
              {d.jobs.map(j => (
                <tr key={j.name}>
                  <td className="mono" style={{ fontSize: 12 }}>{j.name}</td>
                  <td className="mono muted" style={{ fontSize: 12 }}>{j.last_number ? `#${j.last_number}` : '—'}</td>
                  <td><Pill t={BUILD_TONE[j.status] || 'neutral'} label={j.status} icon={j.status === 'RUNNING' ? 'ph-circle-notch' : undefined} /></td>
                  <td className="mono" style={{ fontSize: 11.5 }}>{dur(j.duration_ms)}</td>
                  <td className="muted" style={{ fontSize: 12 }}>{ago(j.timestamp)}</td>
                  <td>
                    <div style={{ display: 'flex', gap: 2 }} aria-label={`last ${j.trend.length} builds: ${j.trend.join(', ')}`}>
                      {j.trend.map((s, i) => <span key={i} title={s} style={{ width: 7, height: 14, borderRadius: 2, background: TREND_C[s] || 'var(--muted)' }} />)}
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </TableCard>
      </Section>
    </div>
  )
}
