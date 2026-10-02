import { useState } from 'react'
import { ago } from '../lib/api'
import { useData } from '../lib/demo'
import { Card, Icon, NoData, OkEmpty, Pill, Section, SectionHead, SkeletonRows, TableCard } from './common'

const WASTE_T = { OK: 'ok', 'over-provisioned': 'warn', 'under-provisioned': 'crit', unknown: 'neutral' }

function StatTile({ icon, label, value, sub }) {
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 12, padding: '12px 14px', background: 'var(--color-surface)', minWidth: 0 }}>
      <div style={{ width: 32, height: 32, borderRadius: 9, display: 'grid', placeItems: 'center', flex: 'none', color: 'var(--color-accent)', background: 'color-mix(in srgb, var(--color-accent) 12%, transparent)' }}>
        <Icon name={icon} size={16} />
      </div>
      <div style={{ minWidth: 0 }}>
        <div style={{ fontSize: 19, fontWeight: 600, letterSpacing: '-0.01em', fontVariantNumeric: 'tabular-nums' }}>{value}</div>
        <div style={{ fontSize: 11.5 }}>{label}</div>
        {sub && <div className="muted" style={{ fontSize: 11 }}>{sub}</div>}
      </div>
    </div>
  )
}

export default function CostWaste({ onAsk }) {
  const [usage, setUsage] = useState(false)
  const [wantAi, setWantAi] = useState(false)
  const url = `/api/cost/k8s?usage=${usage}&ai=${wantAi}`
  const cost = useData('cost_k8s', url, usage || wantAi ? 0 : 45000)
  const d = cost.data

  const deepScan = () => { setUsage(true); cost.reload(`/api/cost/k8s?usage=true&ai=${wantAi}`) }
  const analyze = () => { setWantAi(true); cost.reload(`/api/cost/k8s?usage=${usage}&ai=true`) }

  const waste = d?.deployments?.filter(x => x.waste_label === 'over-provisioned') || []
  const fixPrompt = waste.length
    ? `These Kubernetes deployments look over-provisioned: ${waste.slice(0, 5).map(w => `${w.deployment} (${w.namespace}, ${w.waste_percent}% waste, $${w.est_monthly_cost.toFixed(2)}/mo)`).join('; ')}. Recommend right-sized resource requests and show the exact kubectl command for each.`
    : ''

  return (
    <div data-screen-label="Cost & Waste" style={{ display: 'flex', flexDirection: 'column', gap: 22 }}>
      <Section>
        <SectionHead title="Cluster cost" note={d?.reachable ? `${d.cluster_name} · from resource requests, not your AWS bill · built ${ago(d.generated_at)}` : null}>
          <button className="btn btn-secondary" onClick={deepScan} disabled={usage || cost.loading}><Icon name={usage && cost.loading ? 'ph-circle-notch' : 'ph-gauge'} className={usage && cost.loading ? 'spin' : undefined} />{usage ? 'Actual usage on' : 'Check actual usage'}</button>
          <button className="btn btn-secondary" onClick={() => cost.reload(url)} disabled={cost.loading}><Icon name="ph-arrow-clockwise" className={cost.loading ? 'spin' : undefined} />Re-check</button>
        </SectionHead>
        {!d && cost.loading && <SkeletonRows rows={3} />}
        {!d && cost.error && <NoData note="Couldn't check cluster cost." error={cost.error} onRetry={() => cost.reload(url)} />}
        {d && !d.reachable && <NoData label="Cluster" note={'The cluster isn\'t reachable, so cost couldn\'t be estimated — this is not "no cost."'} onRetry={() => cost.reload(url)} />}
        {d?.reachable && d.error && <NoData note="The scan failed." error={d.error} onRetry={() => cost.reload(url)} />}
        {d?.reachable && !d.error && (
          <>
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(200px, 1fr))', gap: 1, borderRadius: 'var(--radius-md)', overflow: 'hidden', background: 'var(--color-divider)', boxShadow: 'var(--shadow-sm)' }}>
              <StatTile icon="ph-hard-drives" value={d.total_nodes} label="nodes" sub={d.total_nodes === 0 ? 'Fargate / managed?' : undefined} />
              <StatTile icon="ph-stack" value={`$${d.total_monthly_node_cost.toFixed(0)}`} label="node cost / mo" />
              <StatTile icon="ph-cube" value={`$${d.total_requested_cost.toFixed(0)}`} label="requested / mo" sub="what pods ask for" />
              <StatTile icon="ph-trash" value={`$${d.total_waste_cost.toFixed(0)}`} label={`waste / mo${usage ? '' : ' (needs usage check)'}`} sub={usage ? `${d.waste_percent}% of requested` : 'turn on actual usage above'} />
            </div>
            {!usage && <div className="muted" style={{ fontSize: 12 }}><Icon name="ph-info" /> Costs below are from what pods request. Turn on actual usage (adds ~10–20s, needs metrics-server) to see real waste per deployment.</div>}
          </>
        )}
      </Section>

      {d?.reachable && !d.error && (
        <Section>
          <SectionHead title="By deployment" note={`${d.deployments.length} deployment(s)`}>
            {!d.claude_analysis && <button className="btn btn-secondary" onClick={analyze} disabled={wantAi}><Icon name={wantAi && cost.loading ? 'ph-circle-notch' : 'ph-sparkle'} className={wantAi && cost.loading ? 'spin' : undefined} />AI analysis</button>}
            {waste.length > 0 && <button className="btn btn-primary" onClick={() => onAsk(fixPrompt)}><Icon name="ph-wrench" />Right-size the worst ones</button>}
          </SectionHead>
          {d.deployments.length === 0 && <OkEmpty title="No deployments found" sub="Nothing with resource requests in this cluster yet." />}
          {d.deployments.length > 0 && (
            <TableCard>
              <table className="table">
                <thead><tr><th>Deployment</th><th>Pods</th><th>CPU requested</th><th>CPU actual</th><th>Est. cost</th><th>Waste</th></tr></thead>
                <tbody>
                  {d.deployments.map(dep => (
                    <tr key={`${dep.namespace}/${dep.deployment}`}>
                      <td className="mono" style={{ fontSize: 12 }}>{dep.deployment}<span className="muted">/{dep.namespace}</span></td>
                      <td className="mono" style={{ fontSize: 12 }}>{dep.pod_count}</td>
                      <td className="mono" style={{ fontSize: 12 }}>{dep.total_cpu_request.toFixed(2)}</td>
                      <td className="mono" style={{ fontSize: 12 }}>{usage ? dep.total_cpu_actual.toFixed(2) : '—'}</td>
                      <td className="mono" style={{ fontSize: 12 }}>${dep.est_monthly_cost.toFixed(2)}/mo</td>
                      <td><Pill t={WASTE_T[dep.waste_label] || 'neutral'} label={dep.waste_label === 'unknown' ? "couldn't check" : `${dep.waste_label} (${dep.waste_percent}%)`} icon={false} /></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </TableCard>
          )}
          {d.claude_analysis && (
            <Card>
              <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}><Icon name="ph-sparkle" style={{ color: 'var(--color-accent)' }} /><span className="kicker" style={{ fontSize: 10.5 }}>AI analysis</span></div>
              <div style={{ fontSize: 13, lineHeight: 1.6, whiteSpace: 'pre-wrap' }}>{d.claude_analysis}</div>
            </Card>
          )}
        </Section>
      )}

      {d?.reachable && !d.error && usage && d.top_wasteful_pods.length > 0 && (
        <Section>
          <SectionHead title="Most over-provisioned pods" note={`top ${d.top_wasteful_pods.length}`} />
          <TableCard>
            <table className="table">
              <thead><tr><th>Pod</th><th>Instance type</th><th>CPU req → actual</th><th>Est. cost</th><th>Waste</th></tr></thead>
              <tbody>
                {d.top_wasteful_pods.map(p => (
                  <tr key={`${p.namespace}/${p.pod}`}>
                    <td className="mono" style={{ fontSize: 12 }}>{p.pod}<span className="muted">/{p.namespace}</span></td>
                    <td className="mono muted" style={{ fontSize: 11.5 }}>{p.node_instance_type}</td>
                    <td className="mono" style={{ fontSize: 12 }}>{p.cpu_request.toFixed(2)} → {p.cpu_actual.toFixed(2)}</td>
                    <td className="mono" style={{ fontSize: 12 }}>${p.est_monthly_cost.toFixed(2)}/mo</td>
                    <td><Pill t="warn" label={`${p.waste_percent}%`} icon={false} /></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </TableCard>
        </Section>
      )}
    </div>
  )
}
