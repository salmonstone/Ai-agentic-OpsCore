import { useState, useEffect } from 'react'

export default function PendingApprovals() {
  const [deploys, setDeploys] = useState([])
  const [busy, setBusy]       = useState({})

  const load = () => fetch('/api/pending-deploys').then(r => r.json()).then(setDeploys).catch(() => {})

  useEffect(() => {
    load(); const t = setInterval(load, 10000); return () => clearInterval(t)
  }, [])

  const act = async (id, action) => {
    setBusy(p => ({ ...p, [id]:true }))
    try { await fetch(`/api/deploys/${id}/${action}`, { method:'POST' }); await load() }
    catch (_) {}
    setBusy(p => ({ ...p, [id]:false }))
  }

  return (
    <div style={{ display:'flex', flexDirection:'column', gap:22 }}>
      <div style={{ display:'flex', alignItems:'center', gap:16 }}>
        <h2 style={{ fontSize:22, color:'#e8e8e8', letterSpacing:2, fontWeight:400, margin:0 }}>PENDING APPROVALS</h2>
        <span style={{ fontSize:14, color:'#9a9a9a' }}>{deploys.length} waiting</span>
      </div>

      {deploys.length === 0 ? (
        <div style={{ background:'#111', border:'1px solid #1e1e1e', padding:44, textAlign:'center' }}>
          <div style={{ fontSize:34, color:'#22c55e', marginBottom:10 }}>✓</div>
          <div style={{ fontSize:16, color:'#858585' }}>No deployments waiting for approval</div>
        </div>
      ) : deploys.map(d => (
        <div key={d.id} style={{ background:'#111', border:'1px solid #1e1e1e', padding:'22px 24px' }}>
          <div style={{ display:'flex', justifyContent:'space-between', alignItems:'flex-start', marginBottom:16 }}>
            <div>
              <div style={{ fontSize:18, color:'#f0f0f0', marginBottom:5 }}>
                {d.service}
                <span style={{ color:'#9a9a9a', fontSize:14, marginLeft:12 }}>{d.namespace}</span>
              </div>
              <div style={{ display:'flex', alignItems:'center', gap:10, fontSize:14 }}>
                <span style={{ color:'#a6a6a6' }}>{d.old_image || 'unknown'}</span>
                <span style={{ color:'#757575' }}>→</span>
                <span style={{ color:'#e05020' }}>{d.new_image}</span>
              </div>
            </div>
            <span style={{ fontSize:13, color:'#858585' }}>{(d.requested_at||'').slice(0,16).replace('T',' ')}</span>
          </div>

          {d.reason && (
            <div style={{ fontSize:14, color:'#b0b0b0', marginBottom:18, padding:'10px 14px', background:'#0d0d0d', borderLeft:'2px solid #2a2a2a', lineHeight:1.6 }}>
              {d.reason}
            </div>
          )}

          <div style={{ display:'flex', gap:10 }}>
            <ActionBtn label="APPROVE" color="#22c55e" onClick={() => act(d.id,'approve')} disabled={busy[d.id]} />
            <ActionBtn label="REJECT"  color="#ef4444" onClick={() => act(d.id,'reject')}  disabled={busy[d.id]} />
          </div>
        </div>
      ))}
    </div>
  )
}

function ActionBtn({ label, color, onClick, disabled }) {
  return (
    <button onClick={onClick} disabled={disabled} style={{
      background:'transparent', border:`1px solid ${color}50`, color,
      padding:'9px 26px', cursor: disabled ? 'not-allowed' : 'pointer',
      fontSize:14, letterSpacing:1.5, fontFamily:'inherit',
      opacity: disabled ? 0.5 : 1, transition:'all .15s',
    }}
      onMouseEnter={e => { if (!disabled) e.currentTarget.style.background = `${color}15` }}
      onMouseLeave={e => { e.currentTarget.style.background = 'transparent' }}>
      {disabled ? '···' : label}
    </button>
  )
}
