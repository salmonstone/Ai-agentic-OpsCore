import { useState, useEffect, useCallback } from 'react'

const PROVIDER_COLOR = {
  aws: '#f59e0b', eks: '#f59e0b', gcp: '#60a5fa', gke: '#60a5fa',
  azure: '#818cf8', aks: '#818cf8', minikube: '#22c55e', docker: '#22c55e',
  'docker-desktop': '#22c55e', kind: '#22c55e', local: '#22c55e', unknown: '#9a9a9a',
}
const ENV_COLOR = {
  production: '#ef4444', prod: '#ef4444', staging: '#f59e0b', stage: '#f59e0b',
  dev: '#22c55e', development: '#22c55e', test: '#60a5fa', unknown: '#9a9a9a',
}
const HEALTH_COLOR = { healthy: '#22c55e', unreachable: '#ef4444', unknown: '#9a9a9a' }

export default function ClustersPanel() {
  const [data, setData]       = useState({ clusters: [], current: '', health_checked: false })
  const [loading, setLoading] = useState(true)
  const [error, setError]     = useState('')
  const [busy, setBusy]       = useState('')          // context name currently switching

  const load = useCallback(() => {
    fetch('/api/clusters')
      .then(r => r.json())
      .then(d => { d.error ? setError(d.error) : (setData(d), setError('')) })
      .catch(e => setError(String(e)))
      .finally(() => setLoading(false))
  }, [])

  useEffect(() => {
    load()
    const t = setInterval(load, 20000)
    return () => clearInterval(t)
  }, [load])

  const switchTo = async (name) => {
    setBusy(name)
    try {
      const res = await fetch('/api/clusters/switch', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ name }),
      })
      const j = await res.json()
      if (!res.ok) setError(j.error || 'Switch failed')
      else { setError(''); load() }
    } catch (e) { setError(String(e)) }
    setBusy('')
  }

  const clusters = data.clusters || []

  return (
    <div style={{ display:'flex', flexDirection:'column', gap:24 }}>
      <div style={{ display:'flex', alignItems:'center', gap:18, flexWrap:'wrap' }}>
        <h2 style={{ fontSize:22, color:'#e8e8e8', letterSpacing:2, fontWeight:400, margin:0 }}>CLUSTERS</h2>
        <span style={{ fontSize:14, color:'#9a9a9a' }}>{clusters.length} context{clusters.length === 1 ? '' : 's'}</span>
        {!data.health_checked && clusters.length > 0 && (
          <span style={{ fontSize:13, color:'#b0b0b0', padding:'4px 12px', background:'#f59e0b12', border:'1px solid #f59e0b35' }}>
            health check skipped — active cluster offline
          </span>
        )}
      </div>

      {error && (
        <div style={{ background:'#1a0e0e', border:'1px solid #ef444440', padding:16, color:'#ef4444', fontSize:14 }}>
          {error}
        </div>
      )}

      {loading && clusters.length === 0 && !error && (
        <div style={{ fontSize:15, color:'#9a9a9a' }}>Loading kubeconfig contexts…</div>
      )}

      {!loading && clusters.length === 0 && !error && (
        <div style={{ background:'#111', border:'1px solid #1e1e1e', padding:44, textAlign:'center' }}>
          <div style={{ fontSize:36, color:'#858585', marginBottom:12 }}>☸</div>
          <div style={{ fontSize:16, color:'#b0b0b0', marginBottom:6 }}>No clusters configured</div>
          <div style={{ fontSize:14, color:'#858585' }}>Add an EKS cluster below, or run <span style={{ color:'#e05020' }}>kubectl config get-contexts</span></div>
        </div>
      )}

      {clusters.length > 0 && (
        <div style={{ display:'grid', gridTemplateColumns:'repeat(auto-fill,minmax(340px,1fr))', gap:16 }}>
          {clusters.map(c => (
            <ClusterCard key={c.name} c={c} busy={busy === c.name} onSwitch={() => switchTo(c.name)} />
          ))}
        </div>
      )}

      <AddEKSForm onAdded={load} />
    </div>
  )
}

function ClusterCard({ c, busy, onSwitch }) {
  const provColor = PROVIDER_COLOR[(c.cloud_provider || 'unknown').toLowerCase()] || '#9a9a9a'
  const envColor  = ENV_COLOR[(c.environment || 'unknown').toLowerCase()] || '#9a9a9a'
  const hColor    = HEALTH_COLOR[(c.health || 'unknown').toLowerCase()] || '#9a9a9a'
  const current   = c.is_current

  return (
    <div style={{
      background:'#111', border:`1px solid ${current ? '#e0502055' : '#1e1e1e'}`,
      borderLeft:`3px solid ${current ? '#e05020' : provColor}`,
      padding:'20px 22px', display:'flex', flexDirection:'column', gap:14,
      transition:'border-color .2s',
    }}>
      <div style={{ display:'flex', alignItems:'flex-start', justifyContent:'space-between', gap:12 }}>
        <div style={{ minWidth:0 }}>
          <div style={{ fontSize:17, color:'#fff', letterSpacing:.5, overflow:'hidden', textOverflow:'ellipsis', whiteSpace:'nowrap' }}>
            {c.name}
          </div>
          {c.cluster && c.cluster !== c.name && (
            <div style={{ fontSize:13, color:'#9a9a9a', marginTop:3, overflow:'hidden', textOverflow:'ellipsis', whiteSpace:'nowrap' }}>
              {c.cluster}
            </div>
          )}
        </div>
        {current && (
          <span style={{ fontSize:12, letterSpacing:1.5, color:'#e05020', padding:'4px 12px', border:'1px solid #e0502050', background:'#e0502012', flexShrink:0 }}>
            CURRENT
          </span>
        )}
      </div>

      <div style={{ display:'flex', gap:8, flexWrap:'wrap' }}>
        <Tag label={c.cloud_provider || 'unknown'} color={provColor} />
        <Tag label={c.environment || 'unknown'} color={envColor} />
        {c.region && <Tag label={c.region} color="#b0b0b0" />}
      </div>

      <div style={{ display:'flex', alignItems:'center', justifyContent:'space-between', gap:12, marginTop:2 }}>
        <div style={{ display:'flex', alignItems:'center', gap:9 }}>
          <span style={{ width:9, height:9, borderRadius:'50%', background:hColor, flexShrink:0 }} />
          <span style={{ fontSize:14, color:hColor, letterSpacing:.5 }}>{(c.health || 'unknown').toUpperCase()}</span>
          {c.node_count > 0 && (
            <span style={{ fontSize:14, color:'#b0b0b0' }}>· {c.node_count} node{c.node_count === 1 ? '' : 's'}</span>
          )}
        </div>
        {!current && (
          <button
            onClick={onSwitch} disabled={busy}
            style={{
              background:'transparent', border:'1px solid #e0502055', color:'#e05020',
              padding:'7px 20px', fontSize:13, letterSpacing:1.5, fontFamily:'inherit',
              cursor: busy ? 'not-allowed' : 'pointer', opacity: busy ? 0.5 : 1, transition:'all .15s',
            }}
            onMouseEnter={e => { if (!busy) e.currentTarget.style.background = '#e0502015' }}
            onMouseLeave={e => { e.currentTarget.style.background = 'transparent' }}>
            {busy ? '···' : 'SWITCH'}
          </button>
        )}
      </div>
    </div>
  )
}

function Tag({ label, color }) {
  return (
    <span style={{ fontSize:12, letterSpacing:.5, padding:'3px 10px', color, border:`1px solid ${color}35`, background:`${color}0f` }}>
      {label}
    </span>
  )
}

function AddEKSForm({ onAdded }) {
  const [open, setOpen]     = useState(false)
  const [form, setForm]     = useState({ cluster_name:'', region:'', profile:'default', rename_to:'' })
  const [busy, setBusy]     = useState(false)
  const [msg, setMsg]       = useState(null)   // { ok:bool, text:string }

  const set = (k, v) => setForm(p => ({ ...p, [k]: v }))
  const canSubmit = form.cluster_name.trim() && form.region.trim() && !busy

  const submit = async () => {
    setBusy(true); setMsg(null)
    try {
      const res = await fetch('/api/clusters/add-eks', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify(form),
      })
      const j = await res.json()
      if (res.ok && j.success) {
        setMsg({ ok:true, text:`Added ${j.context}${j.node_count ? ` · ${j.node_count} nodes` : ''}` })
        setForm({ cluster_name:'', region:'', profile:'default', rename_to:'' })
        onAdded?.()
      } else {
        setMsg({ ok:false, text: j.error || `Failed (HTTP ${res.status})` })
      }
    } catch (e) { setMsg({ ok:false, text:String(e) }) }
    setBusy(false)
  }

  return (
    <div style={{ background:'#111', border:'1px solid #1e1e1e', padding:'20px 22px', display:'flex', flexDirection:'column', gap:16 }}>
      <button
        onClick={() => setOpen(o => !o)}
        style={{ background:'transparent', border:'none', cursor:'pointer', padding:0, display:'flex', alignItems:'center', gap:10, textAlign:'left' }}>
        <span style={{ fontSize:16, color:'#e05020' }}>{open ? '▾' : '▸'}</span>
        <span style={{ fontSize:15, letterSpacing:1.5, color:'#e8e8e8' }}>ADD EKS CLUSTER</span>
        <span style={{ fontSize:13, color:'#9a9a9a' }}>runs aws eks update-kubeconfig</span>
      </button>

      {open && (
        <>
          <div style={{ display:'grid', gridTemplateColumns:'1fr 1fr', gap:14 }}>
            <Field label="Cluster name" required value={form.cluster_name} onChange={v => set('cluster_name', v)} placeholder="my-eks-cluster" />
            <Field label="AWS region"   required value={form.region}       onChange={v => set('region', v)}       placeholder="us-east-1" />
            <Field label="AWS profile"  value={form.profile}   onChange={v => set('profile', v)}   placeholder="default" />
            <Field label="Rename to (optional)" value={form.rename_to} onChange={v => set('rename_to', v)} placeholder="prod-eks" />
          </div>

          <div style={{ display:'flex', alignItems:'center', gap:16 }}>
            <button
              onClick={submit} disabled={!canSubmit}
              style={{
                background: canSubmit ? '#e05020' : '#181818', border:'none',
                color: canSubmit ? '#fff' : '#555', padding:'10px 32px',
                fontSize:14, letterSpacing:2, fontFamily:'inherit',
                cursor: canSubmit ? 'pointer' : 'not-allowed', transition:'all .15s',
              }}
              onMouseEnter={e => { if (canSubmit) e.currentTarget.style.background = '#c94018' }}
              onMouseLeave={e => { if (canSubmit) e.currentTarget.style.background = '#e05020' }}>
              {busy ? '◉  ADDING…' : '+  ADD CLUSTER'}
            </button>
            {msg && (
              <span style={{ fontSize:14, color: msg.ok ? '#22c55e' : '#ef4444' }}>
                {msg.ok ? '✓ ' : '✗ '}{msg.text}
              </span>
            )}
          </div>
          <div style={{ fontSize:13, color:'#858585', lineHeight:1.6 }}>
            Requires the AWS CLI configured with credentials for the chosen profile. The cluster is added to your kubeconfig and appears above.
          </div>
        </>
      )}
    </div>
  )
}

function Field({ label, value, onChange, placeholder, required }) {
  return (
    <div>
      <div style={{ display:'flex', alignItems:'center', gap:8, marginBottom:7 }}>
        <span style={{ fontSize:14, color: required ? '#e05020' : '#c8c8c8' }}>{label}</span>
        {required && <span style={{ fontSize:12, color:'#e05020' }}>*</span>}
      </div>
      <input
        value={value}
        onChange={e => onChange(e.target.value)}
        placeholder={placeholder}
        style={{
          width:'100%', background:'#0d0d0d', border:'1px solid #1e1e1e', color:'#e8e8e8',
          padding:'10px 12px', fontSize:14, fontFamily:'inherit', outline:'none',
          boxSizing:'border-box', transition:'border-color .15s',
        }}
        onFocus={e => e.target.style.borderColor = '#e05020'}
        onBlur={e => e.target.style.borderColor = '#1e1e1e'}
      />
    </div>
  )
}
