import { useState, useEffect } from 'react'

export default function SLOPanel() {
  const [slos, setSlos] = useState([])

  useEffect(() => {
    const load = () => fetch('/api/slos').then(r => r.json()).then(setSlos).catch(() => {})
    load(); const t = setInterval(load, 30000); return () => clearInterval(t)
  }, [])

  return (
    <div style={{ display:'flex', flexDirection:'column', gap:22 }}>
      <div style={{ display:'flex', alignItems:'center', gap:16 }}>
        <h2 style={{ fontSize:22, color:'#e8e8e8', letterSpacing:2, fontWeight:400, margin:0 }}>SLO STATUS</h2>
        <span style={{ fontSize:14, color:'#9a9a9a' }}>{slos.length} tracked</span>
      </div>

      {slos.length === 0 ? (
        <div style={{ background:'#111', border:'1px solid #1e1e1e', padding:44, textAlign:'center' }}>
          <div style={{ fontSize:16, color:'#858585', marginBottom:10 }}>No SLOs configured</div>
          <div style={{ fontSize:14, color:'#6a6a6a' }}>Run <span style={{ color:'#e05020' }}>agent slo add</span> to create one</div>
        </div>
      ) : slos.map((s, i) => <SLOCard key={i} slo={s} />)}
    </div>
  )
}

function SLOCard({ slo }) {
  const budget    = slo.budget_minutes ?? 0
  const remaining = slo.budget_remaining ?? budget
  const used      = Math.max(0, budget - remaining)
  const pct       = budget > 0 ? Math.min(100, (used / budget) * 100) : 0
  const critical  = pct >= 100
  const atRisk    = pct >= 80

  const barColor = critical ? '#ef4444' : atRisk ? '#f59e0b' : '#22c55e'
  const statusLabel = critical ? 'BREACHED' : atRisk ? 'AT RISK' : 'HEALTHY'

  return (
    <div style={{ background:'#111', border:'1px solid #1e1e1e', padding:'22px 24px', transition:'border-color .2s' }}
      onMouseEnter={e => e.currentTarget.style.borderColor = `${barColor}40`}
      onMouseLeave={e => e.currentTarget.style.borderColor = '#1e1e1e'}>

      <div style={{ display:'flex', justifyContent:'space-between', alignItems:'flex-start', marginBottom:18 }}>
        <div>
          <div style={{ fontSize:18, color:'#f0f0f0', marginBottom:5 }}>{slo.name}</div>
          <div style={{ fontSize:14, color:'#9a9a9a' }}>
            {slo.service} · {slo.window_days}d window · target {slo.target_pct}%
          </div>
        </div>
        <span style={{ fontSize:13, letterSpacing:1.5, color:barColor, padding:'5px 14px', border:`1px solid ${barColor}35`, background:`${barColor}0a` }}>
          {statusLabel}
        </span>
      </div>

      {/* budget bar */}
      <div style={{ marginBottom:14 }}>
        <div style={{ display:'flex', justifyContent:'space-between', marginBottom:8, fontSize:14, color:'#9a9a9a' }}>
          <span>Error budget consumed</span>
          <span style={{ color:barColor, fontWeight:700, fontSize:15 }}>{pct.toFixed(1)}%</span>
        </div>
        <div style={{ background:'#1a1a1a', height:10, borderRadius:1, overflow:'hidden' }}>
          <div style={{ height:'100%', width:`${Math.min(100,pct)}%`, background:barColor, transition:'width .5s ease', borderRadius:1 }} />
        </div>
      </div>

      <div style={{ display:'flex', gap:24, fontSize:14, color:'#858585' }}>
        <span>Budget <strong style={{ color:'#c8c8c8' }}>{budget.toFixed(0)} min</strong></span>
        <span>Used <strong style={{ color:'#c8c8c8' }}>{used.toFixed(0)} min</strong></span>
        <span>Remaining <strong style={{ color:barColor }}>{Math.max(0,remaining).toFixed(0)} min</strong></span>
      </div>
    </div>
  )
}
