import { useState } from 'react'
import { postJSON } from '../lib/api'
import { useDemo } from '../lib/demo'
import { Icon } from './common'

/** Register an EKS cluster (aws eks update-kubeconfig via MultiClusterSkill)
 *  — shown when the current cluster is unreachable. */
export default function ConnectCluster({ onConnected }) {
  const demo = useDemo()
  const [f, setF] = useState({ cluster_name: '', region: 'ap-south-1', profile: 'default' })
  const [state, setState] = useState({ busy: false, result: null })
  const set = k => e => setF(v => ({ ...v, [k]: e.target.value }))

  const submit = async e => {
    e.preventDefault()
    setState({ busy: true, result: null })
    try {
      const r = await postJSON('/api/clusters/add-eks', f)
      setState({ busy: false, result: { ok: r.success !== false, text: r.success === false ? (r.error || 'failed') : `Connected ${r.context} · ${r.node_count} nodes` } })
      if (r.success !== false) onConnected()
    } catch (err) {
      setState({ busy: false, result: { ok: false, text: err.message } })
    }
  }

  return (
    <form onSubmit={submit} className="surface" style={{ display: 'flex', flexDirection: 'column', gap: 12, padding: 16 }}>
      <div style={{ display: 'flex', alignItems: 'flex-start', gap: 10 }}>
        <Icon name="ph-plugs-connected" size={20} style={{ color: 'var(--color-accent)', marginTop: 1 }} />
        <div>
          <div style={{ fontSize: 14, fontWeight: 500 }}>Connect an EKS cluster</div>
          <div className="muted" style={{ fontSize: 12 }}>Runs <code>aws eks update-kubeconfig</code> with your AWS credentials and switches kubectl to it.</div>
        </div>
      </div>
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(170px, 1fr))', gap: 10 }}>
        {[['cluster_name', 'Cluster name', 'infragpt-cluster'], ['region', 'Region', 'ap-south-1'], ['profile', 'AWS profile', 'default']].map(([k, label, ph]) => (
          <label key={k} htmlFor={`cc-${k}`} style={{ display: 'flex', flexDirection: 'column', gap: 5, fontSize: 11.5 }}>
            <span className="muted">{label}</span>
            <input id={`cc-${k}`} className="input mono" value={f[k]} placeholder={ph} onChange={set(k)} style={{ fontSize: 12.5 }} />
          </label>
        ))}
      </div>
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
        <button className="btn btn-primary" type="submit" disabled={demo || state.busy || !f.cluster_name.trim() || !f.region.trim()}>
          <Icon name={state.busy ? 'ph-circle-notch' : 'ph-plug'} className={state.busy ? 'spin' : undefined} />{state.busy ? 'Connecting…' : 'Connect'}
        </button>
        {demo && <span className="muted" style={{ fontSize: 12 }}>Disabled in demo mode.</span>}
        {state.result && (
          <span style={{ fontSize: 12, color: state.result.ok ? 'var(--st-ok)' : 'var(--st-crit)' }}>
            <Icon name={state.result.ok ? 'ph-check-circle' : 'ph-x-circle'} /> {state.result.text}
          </span>
        )}
      </div>
    </form>
  )
}
