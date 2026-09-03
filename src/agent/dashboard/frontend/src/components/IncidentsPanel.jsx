import { useState, useEffect } from 'react'

const SEV_COLOR = { critical:'#ef4444', high:'#f97316', warning:'#f59e0b', medium:'#f59e0b', low:'#22c55e', info:'#60a5fa' }

export default function IncidentsPanel() {
  const [incidents, setIncidents] = useState([])

  useEffect(() => {
    const load = () => fetch('/api/incidents').then(r => r.json()).then(setIncidents).catch(() => {})
    load(); const t = setInterval(load, 15000); return () => clearInterval(t)
  }, [])

  const open   = incidents.filter(i => i.status === 'open')
  const closed = incidents.filter(i => i.status !== 'open')

  return (
    <div style={{ display:'flex', flexDirection:'column', gap:22 }}>
      <div style={{ display:'flex', alignItems:'center', gap:16 }}>
        <h2 style={{ fontSize:22, color:'#e8e8e8', letterSpacing:2, fontWeight:400, margin:0 }}>INCIDENTS</h2>
        <span style={{ fontSize:14, padding:'4px 14px', background:'#ef444415', border:'1px solid #ef444430', color:'#ef4444' }}>{open.length} OPEN</span>
        <span style={{ fontSize:14, padding:'4px 14px', background:'#22c55e0a', border:'1px solid #22c55e20', color:'#22c55e' }}>{closed.length} RESOLVED</span>
      </div>

      {open.length === 0 && (
        <div style={{ background:'#111', border:'1px solid #1e1e1e', padding:44, textAlign:'center' }}>
          <div style={{ fontSize:34, color:'#22c55e', marginBottom:10 }}>✓</div>
          <div style={{ fontSize:16, color:'#858585' }}>All clear — no open incidents</div>
        </div>
      )}

      {open.length > 0 && <IncTable title="ACTIVE" rows={open} />}
      {closed.length > 0 && <IncTable title="RECENTLY RESOLVED" rows={closed.slice(0,20)} muted />}
    </div>
  )
}

function IncTable({ title, rows, muted }) {
  return (
    <div style={{ background:'#111', border:'1px solid #1e1e1e' }}>
      <div style={{ padding:'13px 20px', borderBottom:'1px solid #1e1e1e', fontSize:13, letterSpacing:2, color: muted ? '#757575' : '#a6a6a6' }}>{title}</div>
      <table style={{ width:'100%', borderCollapse:'collapse' }}>
        <thead>
          <tr>
            {['SEVERITY','TITLE','SERVICE','TIME','STATUS'].map(h => (
              <th key={h} style={{ textAlign:'left', padding:'10px 16px', color:'#858585', fontSize:12, letterSpacing:1.5, fontWeight:'normal', borderBottom:'1px solid #1a1a1a' }}>{h}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((inc, i) => {
            const c = SEV_COLOR[inc.severity] || '#9a9a9a'
            return (
              <tr key={i} style={{ borderBottom:'1px solid #141414' }}
                onMouseEnter={e => e.currentTarget.style.background='#141414'}
                onMouseLeave={e => e.currentTarget.style.background='transparent'}>
                <td style={{ padding:'11px 16px' }}>
                  <span style={{ fontSize:12, color:c, padding:'3px 10px', border:`1px solid ${c}30`, background:`${c}0a`, letterSpacing:1 }}>
                    {(inc.severity||'LOW').toUpperCase()}
                  </span>
                </td>
                <td style={{ padding:'11px 16px', color: muted ? '#858585' : '#e0e0e0', fontSize:15, maxWidth:280, overflow:'hidden', textOverflow:'ellipsis', whiteSpace:'nowrap' }}>
                  {inc.title}
                </td>
                <td style={{ padding:'11px 16px', color:'#a6a6a6', fontSize:14 }}>{inc.service || '—'}</td>
                <td style={{ padding:'11px 16px', color:'#858585', fontSize:13 }}>{(inc.opened_at||'').slice(0,16).replace('T',' ')}</td>
                <td style={{ padding:'11px 16px' }}>
                  <span style={{ fontSize:12, letterSpacing:1, color: inc.status === 'open' ? '#ef4444' : '#22c55e' }}>
                    {(inc.status||'').toUpperCase()}
                  </span>
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}
