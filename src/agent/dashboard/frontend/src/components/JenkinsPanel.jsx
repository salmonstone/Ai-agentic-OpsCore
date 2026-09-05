import { useState, useEffect } from 'react'

export default function JenkinsPanel() {
  const [data, setData] = useState(null)

  useEffect(() => {
    const load = () => fetch('/api/jenkins').then(r => r.json()).then(setData).catch(() => {})
    load(); const t = setInterval(load, 30000); return () => clearInterval(t)
  }, [])

  if (!data) {
    return <div style={{ color:'#858585', fontSize:14 }}>Loading…</div>
  }

  if (!data.configured) {
    return (
      <div style={{ background:'#111', border:'1px solid #1e1e1e', padding:44, textAlign:'center' }}>
        <div style={{ fontSize:16, color:'#858585', marginBottom:10 }}>Jenkins not configured</div>
        <div style={{ fontSize:14, color:'#6a6a6a' }}>Run <span style={{ color:'#e05020' }}>agent setup</span> to connect Jenkins</div>
      </div>
    )
  }

  if (!data.connected) {
    return (
      <div style={{ background:'#111', border:'1px solid #ef444440', padding:44, textAlign:'center' }}>
        <div style={{ fontSize:16, color:'#ef4444', marginBottom:10 }}>Jenkins connection failed</div>
        <div style={{ fontSize:14, color:'#9a9a9a' }}>{data.error}</div>
      </div>
    )
  }

  const score = data.health_score ?? 100
  const scoreColor = score >= 80 ? '#22c55e' : score >= 50 ? '#f59e0b' : '#ef4444'
  const statusLabel = score >= 80 ? 'HEALTHY' : score >= 50 ? 'DEGRADED' : 'CRITICAL'

  return (
    <div style={{ display:'flex', flexDirection:'column', gap:22 }}>
      <div style={{ display:'flex', alignItems:'center', gap:16 }}>
        <h2 style={{ fontSize:22, color:'#e8e8e8', letterSpacing:2, fontWeight:400, margin:0 }}>JENKINS CI/CD</h2>
        <span style={{ fontSize:12, letterSpacing:1.5, color:'#22c55e' }}>● LIVE</span>
      </div>

      <div style={{ background:'#111', border:'1px solid #1e1e1e', padding:'22px 24px' }}>
        <div style={{ display:'flex', justifyContent:'space-between', alignItems:'center', marginBottom:18 }}>
          <span style={{ fontSize:14, color:'#9a9a9a' }}>Health Score</span>
          <span style={{ fontSize:13, letterSpacing:1.5, color:scoreColor, padding:'5px 14px', border:`1px solid ${scoreColor}35`, background:`${scoreColor}0a` }}>
            {score}/100 {statusLabel}
          </span>
        </div>
        <div style={{ display:'flex', gap:24, fontSize:14, color:'#858585' }}>
          <span>Jobs <strong style={{ color:'#c8c8c8' }}>{data.total_jobs}</strong></span>
          <span>Failing <strong style={{ color: data.failing_jobs.length ? '#ef4444' : '#c8c8c8' }}>{data.failing_jobs.length}</strong></span>
          <span>Agents offline <strong style={{ color: data.offline_nodes.length ? '#ef4444' : '#c8c8c8' }}>{data.offline_nodes.length}</strong></span>
          <span>Queue stuck <strong style={{ color: data.stuck_queue ? '#f59e0b' : '#c8c8c8' }}>{data.stuck_queue}</strong></span>
        </div>
      </div>

      {data.failing_jobs.length === 0 ? (
        <div style={{ background:'#111', border:'1px solid #1e1e1e', padding:30, textAlign:'center', color:'#22c55e', fontSize:15 }}>
          All jobs passing
        </div>
      ) : (
        <div style={{ display:'flex', flexDirection:'column', gap:10 }}>
          {data.failing_jobs.map((j, i) => (
            <div key={i} style={{
              background:'#111', border:'1px solid #1e1e1e', padding:'14px 18px',
              display:'flex', justifyContent:'space-between', alignItems:'center',
            }}>
              <span style={{ fontSize:14, color:'#e8e8e8' }}>{j.name}</span>
              <code style={{ fontSize:12, color:'#9a9a9a' }}>agent jenkins diagnose {j.name}</code>
            </div>
          ))}
        </div>
      )}

      {data.offline_nodes.length > 0 && (
        <div>
          <div style={{ fontSize:13, letterSpacing:1, color:'#9a9a9a', marginBottom:8 }}>OFFLINE AGENTS</div>
          {data.offline_nodes.map((n, i) => (
            <div key={i} style={{ fontSize:14, color:'#ef4444', padding:'6px 0' }}>{n}</div>
          ))}
        </div>
      )}

      <div style={{ fontSize:13, color:'#6a6a6a' }}>
        Run <span style={{ color:'#e05020' }}>agent jenkins heal</span> to fix auto-fixable issues, or{' '}
        <span style={{ color:'#e05020' }}>agent jenkins watch</span> for continuous monitoring.
      </div>
    </div>
  )
}
