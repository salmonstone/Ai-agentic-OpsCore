import { useState } from 'react'
import { ago } from '../lib/api'
import { POD_TONE, TC } from '../lib/tone'
import { Bar, Icon, NoData, OkEmpty, Pill, Section, SectionHead, SkeletonRows, TableCard } from './common'
import ConnectCluster from './ConnectCluster'

const pctTone = p => (p == null ? TC.unk : p >= 90 ? TC.crit : p >= 75 ? TC.warn : TC.ok)

function Pct({ v }) {
  if (v == null) return <span className="muted" style={{ fontSize: 11.5 }}>no metrics</span>
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
      <Bar pct={v} color={pctTone(v)} />
      <span className="mono" style={{ fontSize: 11.5, color: v >= 90 ? TC.crit : undefined }}>{v}%</span>
    </div>
  )
}

export default function Cluster({ cluster, onAsk, onConnected }) {
  const [healedOpen, setHealedOpen] = useState(true)
  const d = cluster.data

  if (!d && cluster.loading) return <SkeletonRows rows={5} />
  if (!d) return <NoData label="Cluster" note="Not &quot;no problems&quot; — the dashboard server didn't answer." error={cluster.error} onRetry={() => cluster.reload()} />

  if (!d.reachable) {
    return (
      <div data-screen-label="02 Cluster (unreachable)" style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(280px, 1fr))', gap: 12 }}>
          {[['Nodes', 'Not "0 nodes" — nothing was returned.'],
            ['Problem pods', 'Not "no problem pods" — AtlasOS couldn\'t look.'],
            ['Healed in last 24h', `${d.healed.length} recorded by the daemon (from its own log, not the cluster).`]].map(([label, note]) => (
            <NoData key={label} label={label} note={note} />
          ))}
        </div>
        {d.error && <code className="term" style={{ fontSize: 11.5, padding: '6px 9px' }}>{d.error}</code>}
        <ConnectCluster onConnected={() => { cluster.reload(); onConnected?.() }} />
      </div>
    )
  }

  const nodesReady = d.nodes.filter(n => n.status === 'Ready').length
  return (
    <div data-screen-label="02 Cluster" style={{ display: 'flex', flexDirection: 'column', gap: 22 }}>
      <Section>
        <SectionHead title="Nodes" note={`${nodesReady}/${d.nodes.length} ready${d.metrics_available ? '' : ' · metrics-server not available, so no CPU/memory'}`} />
        <TableCard>
          <table className="table">
            <thead><tr><th>Node</th><th>Status</th><th>Instance</th><th style={{ width: '22%' }}>CPU</th><th style={{ width: '22%' }}>Memory</th><th>Age</th></tr></thead>
            <tbody>
              {d.nodes.map(n => (
                <tr key={n.name}>
                  <td className="mono" style={{ fontSize: 12, whiteSpace: 'nowrap' }}>{n.name}</td>
                  <td><Pill t={n.status === 'Ready' ? 'ok' : 'crit'} label={n.status} /></td>
                  <td className="mono muted" style={{ fontSize: 11.5 }}>{n.instance_type || '—'}</td>
                  <td><Pct v={n.cpu_percent} /></td>
                  <td><Pct v={n.memory_percent} /></td>
                  <td className="mono muted" style={{ fontSize: 11.5 }}>{n.age}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </TableCard>
      </Section>

      <Section>
        <SectionHead title="Problem pods" note={d.problem_pods.length ? `${d.problem_pods.length} need attention · checked ${ago(d.checked_at)}` : null} />
        {d.problem_pods.length === 0
          ? <OkEmpty title="No problem pods" sub={`Every pod is Running or Completed · checked ${ago(d.checked_at)}`} />
          : (
            <TableCard>
              <table className="table">
                <thead><tr><th>Namespace</th><th>Pod</th><th>Status</th><th>Ready</th><th>Restarts</th><th>Age</th><th /></tr></thead>
                <tbody>
                  {d.problem_pods.map(p => (
                    <tr key={`${p.namespace}/${p.name}`}>
                      <td className="mono muted" style={{ fontSize: 12 }}>{p.namespace}</td>
                      <td className="mono" style={{ fontSize: 12, whiteSpace: 'nowrap' }}>{p.name}</td>
                      <td><Pill t={POD_TONE[p.status] || 'warn'} label={p.status} mono /></td>
                      <td className="mono" style={{ fontSize: 12 }}>{p.ready}</td>
                      <td className="mono" style={{ fontSize: 12, color: p.restarts > 5 ? TC.crit : undefined }}>{p.restarts}</td>
                      <td className="mono muted" style={{ fontSize: 11.5 }}>{p.age}</td>
                      <td style={{ textAlign: 'right' }}>
                        <button className="btn btn-ghost" style={{ fontSize: 12, padding: '3px 6px' }}
                          onClick={() => onAsk(`Why is pod ${p.name} in namespace ${p.namespace} ${p.status}? Diagnose it.`)}>
                          <Icon name="ph-sparkle" />Diagnose
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </TableCard>
          )}
      </Section>

      <Section>
        <button onClick={() => setHealedOpen(o => !o)} style={{ display: 'flex', alignItems: 'center', gap: 8, alignSelf: 'flex-start', border: 0, background: 'transparent', padding: 0, cursor: 'pointer' }}>
          <Icon name={healedOpen ? 'ph-caret-down' : 'ph-caret-right'} style={{ color: 'var(--muted)' }} />
          <span className="kicker">Healed in last 24h</span>
          <span className="muted" style={{ fontSize: 12 }}>{d.healed.length} action{d.healed.length === 1 ? '' : 's'}</span>
        </button>
        {healedOpen && (d.healed.length === 0
          ? <div className="muted" style={{ fontSize: 12.5, paddingLeft: 22 }}>The daemon hasn't fixed anything in the last 24 hours.</div>
          : (
            <TableCard>
              <table className="table">
                <thead><tr><th>When</th><th>Namespace</th><th>Resource</th><th>Fix applied</th><th>Note</th></tr></thead>
                <tbody>
                  {d.healed.map((h, i) => (
                    <tr key={i}>
                      <td className="mono muted" style={{ fontSize: 11.5, whiteSpace: 'nowrap' }}>{ago(h.timestamp)}</td>
                      <td className="mono muted" style={{ fontSize: 12 }}>{h.namespace || '—'}</td>
                      <td className="mono" style={{ fontSize: 12 }}>{h.resource}</td>
                      <td style={{ fontSize: 12 }}><span style={{ display: 'inline-flex', alignItems: 'center', gap: 5 }}><Icon name="ph-check" style={{ color: 'var(--st-ok)' }} />{h.action}</span></td>
                      <td className="muted" style={{ fontSize: 12 }}>{h.note}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </TableCard>
          ))}
      </Section>
    </div>
  )
}
