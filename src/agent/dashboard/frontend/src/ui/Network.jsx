import { useState } from 'react'
import { getJSON, postJSON } from '../lib/api'
import { demoNetwork, useDemo } from '../lib/demo'
import { tone } from '../lib/tone'
import { Card, ConfirmButton, CopyButton, Icon, Msg, NoData, Pill, Section, SectionHead, SkeletonRows } from './common'

const SEV_T = { critical: 'crit', warning: 'warn', info: 'neutral' }
const CNI_CHOICES = ['calico', 'flannel', 'cilium', 'weave']

function StatTile({ icon, label, value, t }) {
  const x = t ? tone(t) : null
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 12, padding: '12px 14px', background: 'var(--color-surface)', minWidth: 0 }}>
      <div style={{ width: 32, height: 32, borderRadius: 9, display: 'grid', placeItems: 'center', flex: 'none', color: x ? x.c : 'var(--color-accent)', background: x ? x.tint : 'color-mix(in srgb, var(--color-accent) 12%, transparent)' }}>
        <Icon name={icon} size={16} />
      </div>
      <div style={{ minWidth: 0 }}>
        <div style={{ fontSize: 15, fontWeight: 500 }}>{value}</div>
        <div className="muted" style={{ fontSize: 11.5 }}>{label}</div>
      </div>
    </div>
  )
}

export default function Network({ onAsk }) {
  const demo = useDemo()
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(null)
  const [msg, setMsg] = useState(null)
  const [cni, setCni] = useState('calico')

  const scan = async () => {
    setLoading(true); setError(null); setMsg(null)
    try {
      if (demo) { await new Promise(r => setTimeout(r, 700)); setData(demoNetwork()) }
      else {
        const r = await getJSON('/api/network/scan')
        if (!r.reachable) setError('unreachable')
        else if (r.error) setError(r.error)
        else setData(r)
      }
    } catch (e) { setError(e.message) }
    setLoading(false)
  }

  const restartCni = async () => {
    setMsg(null)
    try { const r = await postJSON('/api/network/restart-cni', { cni }); setMsg(r.ok ? { ok: true, text: `Rollout restart triggered for ${cni}. It can take a minute to come back healthy — scan again to check.` } : { ok: false, text: 'The restart did not report success — check the cluster.' }) }
    catch (e) { setMsg({ ok: false, text: e.message }) }
  }

  return (
    <div data-screen-label="Network" style={{ display: 'flex', flexDirection: 'column', gap: 22 }}>
      <Section>
        <SectionHead title="Cluster networking" note="CNI, kube-proxy, NetworkPolicy, service connectivity, node health">
          <button className="btn btn-primary" onClick={scan} disabled={loading}><Icon name={loading ? 'ph-circle-notch' : 'ph-share-network'} className={loading ? 'spin' : undefined} />{loading ? 'Scanning…' : data ? 'Scan again' : 'Scan network'}</button>
        </SectionHead>
        <div className="muted" style={{ fontSize: 12 }}>Runs 7 collectors plus one AI analysis pass — not auto-refreshed, so it only runs when you ask.</div>

        {!data && !loading && !error && <div className="muted" style={{ fontSize: 12.5, padding: '8px 0' }}>Nothing scanned yet this session.</div>}
        {loading && !data && <SkeletonRows rows={3} />}
        {error === 'unreachable' && <NoData label="Cluster" note={'The cluster isn\'t reachable, so networking couldn\'t be checked — not "all healthy."'} onRetry={scan} />}
        {error && error !== 'unreachable' && <NoData note="The scan failed." error={error} onRetry={scan} />}

        {data && (
          <>
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(190px, 1fr))', gap: 1, borderRadius: 'var(--radius-md)', overflow: 'hidden', background: 'var(--color-divider)', boxShadow: 'var(--shadow-sm)' }}>
              <StatTile icon="ph-share-network" value={data.cni_detected || "couldn't tell"} label="CNI" t={data.cni_detected ? (data.cni_healthy ? 'ok' : 'crit') : 'unk'} />
              <StatTile icon="ph-heartbeat" value={data.cni_healthy ? 'Healthy' : 'Unhealthy'} label="CNI health" t={data.cni_healthy ? 'ok' : 'crit'} />
              <StatTile icon="ph-hard-drives" value={`${data.nodes_ready}/${data.nodes_total}`} label="nodes ready" t={data.nodes_ready === data.nodes_total && data.nodes_total > 0 ? 'ok' : 'warn'} />
              <StatTile icon="ph-list-checks" value={data.issues.length} label={data.issues.length === 1 ? 'issue found' : 'issues found'} t={data.issues.length === 0 ? 'ok' : 'warn'} />
            </div>
            {data.analysis && <div style={{ fontSize: 13, lineHeight: 1.6 }}>{data.analysis}</div>}
          </>
        )}
      </Section>

      {data && data.issues.length === 0 && (
        <div className="surface" style={{ display: 'flex', alignItems: 'center', gap: 12, padding: 16 }}>
          <Icon name="ph-check-circle" size={22} style={{ color: 'var(--st-ok)' }} />
          <div style={{ fontSize: 13.5, fontWeight: 500 }}>No networking issues found</div>
        </div>
      )}

      {data && data.issues.length > 0 && (
        <Section>
          <SectionHead title="Issues" />
          <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
            {data.issues.map((is, i) => (
              <Card key={i} warn={is.severity !== 'info'}>
                <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
                  <Pill t={SEV_T[is.severity] || 'neutral'} label={is.severity} icon={false} mono />
                  <span className="mono muted" style={{ fontSize: 11 }}>{is.problem_type}</span>
                  <span style={{ fontSize: 13, fontWeight: 500, flex: '1 1 160px' }}>{is.resource}{is.namespace ? <span className="muted"> · {is.namespace}</span> : null}</span>
                </div>
                <div style={{ fontSize: 12.5 }}>{is.description}</div>
                <div className="muted" style={{ fontSize: 12.5 }}>Fix: {is.fix}</div>
                <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
                  {is.fix_command && (
                    <>
                      <code className="term" style={{ padding: '5px 8px', fontSize: 11.5, wordBreak: 'break-all', flex: '1 1 260px' }}>{is.fix_command}</code>
                      <CopyButton text={is.fix_command} />
                    </>
                  )}
                  <button className="btn btn-secondary" style={{ fontSize: 12 }} onClick={() => onAsk(`My cluster network scan found this: ${is.description} (${is.problem_type}, resource ${is.resource}${is.namespace ? ` in ${is.namespace}` : ''}). Suggested fix: ${is.fix}. Diagnose it further and fix it if safe (ask me to confirm any change).`)}><Icon name="ph-sparkle" />Diagnose with AI</button>
                </div>
              </Card>
            ))}
          </div>
        </Section>
      )}

      <Section>
        <SectionHead title="Restart a CNI" note="rolling restart of its DaemonSet — pods briefly lose networking while it rolls" />
        <Card>
          <div style={{ display: 'flex', gap: 10, alignItems: 'center', flexWrap: 'wrap' }}>
            <select className="input mono" value={cni} onChange={e => setCni(e.target.value)} style={{ width: 'auto', fontSize: 12.5 }} aria-label="CNI">
              {CNI_CHOICES.map(c => <option key={c} value={c}>{c}</option>)}
            </select>
            <ConfirmButton icon="ph-arrows-clockwise" onConfirm={restartCni} disabled={demo} confirmLabel="Restart it on the cluster?">Restart {cni}</ConfirmButton>
          </div>
          <Msg msg={msg} />
        </Card>
      </Section>
    </div>
  )
}
