import { useState } from 'react'
import { ago } from '../lib/api'
import { Icon, NoData, OkEmpty, Pill, Section, SectionHead, SkeletonRows, TableCard } from './common'
import { logSources } from './LogViewer'

const RUN_TONE = { success: 'ok', failure: 'crit', cancelled: 'neutral', skipped: 'neutral', timed_out: 'crit', action_required: 'warn', neutral: 'neutral' }

function runState(r) {
  if (r.status !== 'completed') return { t: 'accent', label: r.status === 'in_progress' ? 'running' : r.status, icon: 'ph-circle-notch' }
  return { t: RUN_TONE[r.conclusion] || 'neutral', label: r.conclusion || 'done' }
}

export default function GitHub({ runs, repo, setRepo, onAsk, onLogs }) {
  const [draft, setDraft] = useState(repo)
  const d = runs.data

  const header = (
    <form onSubmit={e => { e.preventDefault(); setRepo(draft.trim()) }} style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
      <Icon name="ph-github-logo" size={18} style={{ color: 'var(--color-accent)' }} />
      <input id="gh-repo" className="input mono" value={draft} onChange={e => setDraft(e.target.value)} placeholder={d?.repo || 'owner/repo — blank = this project'}
        style={{ maxWidth: 340, fontSize: 12.5 }} />
      <button className="btn btn-secondary" type="submit">Load</button>
      {d?.repo && <span className="muted" style={{ fontSize: 12 }}>showing <span className="mono">{d.repo}</span></span>}
    </form>
  )

  if (!d && runs.loading) return <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>{header}<SkeletonRows rows={6} /></div>
  if (!d) return <NoData label="GitHub Actions" note="Couldn't ask the dashboard server." error={runs.error} onRetry={() => runs.reload()} />
  if (!d.configured) return <NoData label="GitHub Actions" note="No GitHub token and no logged-in gh CLI — connect GitHub in Settings." />
  if (d.error) return <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>{header}<NoData label="Runs" note={'Not "no failed runs" — AtlasOS couldn\'t read them.'} error={d.error} onRetry={() => runs.reload()} /></div>

  const completed = d.runs.filter(r => r.status === 'completed')
  const failed = completed.filter(r => ['failure', 'timed_out'].includes(r.conclusion))
  const passRate = completed.length ? Math.round(((completed.length - failed.length) / completed.length) * 100) : null
  const dayAgo = Date.now() - 86400000
  const recentFails = failed.filter(r => Date.parse(r.created_at) >= dayAgo)

  return (
    <div data-screen-label="GitHub Actions" style={{ display: 'flex', flexDirection: 'column', gap: 22 }}>
      {header}
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(180px, 1fr))', gap: 1, borderRadius: 'var(--radius-md)', overflow: 'hidden', background: 'var(--color-divider)', boxShadow: 'var(--shadow-sm)' }}>
        {[
          ['Pass rate', passRate == null ? '—' : `${passRate}%`, `last ${completed.length} finished runs`],
          ['Failed in 24h', String(recentFails.length), recentFails.length ? 'needs a look' : 'all green'],
          ['Running now', String(d.runs.filter(r => r.status !== 'completed').length), 'in progress or queued'],
        ].map(([k, v, s]) => (
          <div key={k} style={{ display: 'flex', flexDirection: 'column', gap: 2, padding: '12px 14px', background: 'var(--color-surface)' }}>
            <span className="kicker" style={{ fontSize: 10.5 }}>{k}</span>
            <span style={{ fontSize: 22, fontWeight: 600, fontVariantNumeric: 'tabular-nums' }}>{v}</span>
            <span className="muted" style={{ fontSize: 11.5 }}>{s}</span>
          </div>
        ))}
      </div>

      <Section>
        <SectionHead title="Recent failures" note={failed.length ? `${failed.length} of the last ${d.runs.length} runs` : null} />
        {failed.length === 0 && <OkEmpty title="No failed runs" sub={`Last ${d.runs.length} runs of ${d.repo}`} />}
        {failed.slice(0, 5).map(r => (
          <article key={r.id} className="surface" style={{ display: 'grid', gridTemplateColumns: 'minmax(0,1fr) auto', gap: '10px 18px', padding: '14px 16px' }}>
            <div style={{ display: 'flex', flexDirection: 'column', gap: 4, minWidth: 0 }}>
              <div className="muted" style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap', fontSize: 12 }}>
                <Icon name="ph-x-circle" size={15} style={{ color: 'var(--st-crit)' }} />
                <span style={{ color: 'var(--color-text)', fontWeight: 500, fontSize: 13 }}>{r.workflow}</span>
                <span className="mono">#{r.run_number}</span><span>· {ago(r.created_at)}</span>
              </div>
              <div style={{ fontSize: 14, fontWeight: 500 }}>{r.title}</div>
              <div className="muted mono" style={{ fontSize: 11.5 }}>{r.branch} · {r.sha} · {r.event}</div>
            </div>
            <div style={{ display: 'flex', flexDirection: 'column', gap: 6, minWidth: 150 }}>
              <button className="btn btn-primary" onClick={() => onAsk(`Diagnose GitHub Actions run ${r.id} in ${d.repo}. What failed and how do I fix it?`)}><Icon name="ph-sparkle" />Diagnose</button>
              <button className="btn btn-secondary" onClick={() => onLogs(logSources.github(d.repo, r.id, r.workflow))}><Icon name="ph-file-text" />Job logs</button>
            </div>
          </article>
        ))}
      </Section>

      <Section>
        <SectionHead title="All runs" note={`${d.runs.length} most recent`} />
        <TableCard>
          <table className="table">
            <thead><tr><th>Workflow</th><th>Commit</th><th>Branch</th><th>Event</th><th>Status</th><th>When</th><th /></tr></thead>
            <tbody>
              {d.runs.map(r => {
                const st = runState(r)
                return (
                  <tr key={r.id}>
                    <td style={{ fontSize: 12.5, whiteSpace: 'nowrap' }}>{r.workflow} <span className="mono muted" style={{ fontSize: 11 }}>#{r.run_number}</span></td>
                    <td style={{ fontSize: 12, maxWidth: 320, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={r.title}>{r.title}</td>
                    <td className="mono" style={{ fontSize: 11.5 }}>{r.branch}</td>
                    <td className="muted" style={{ fontSize: 12 }}>{r.event}</td>
                    <td><Pill t={st.t} label={st.label} icon={st.icon} /></td>
                    <td className="muted" style={{ fontSize: 12, whiteSpace: 'nowrap' }}>{ago(r.created_at)}</td>
                    <td style={{ textAlign: 'right' }}>
                      {['failure', 'timed_out'].includes(r.conclusion) && (
                        <button className="btn btn-ghost" style={{ fontSize: 12, padding: '3px 6px' }} onClick={() => onLogs(logSources.github(d.repo, r.id, r.workflow))}><Icon name="ph-scroll" />Logs</button>
                      )}
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </TableCard>
      </Section>
    </div>
  )
}
