import { useState, useEffect } from 'react'

const CAT = {
  pod_heal: { color:'#22c55e', icon:'✓', label:'HEALED' },
  cost:     { color:'#f59e0b', icon:'$', label:'COST'   },
  deploy:   { color:'#60a5fa', icon:'↑', label:'DEPLOY' },
  scale:    { color:'#a78bfa', icon:'⤢', label:'SCALE'  },
  incident: { color:'#ef4444', icon:'⚡', label:'INCIDENT'},
}
const def = { color:'#9a9a9a', icon:'●', label:'INFO' }

export default function ActivityFeed() {
  const [actions, setActions] = useState([])

  useEffect(() => {
    const load = () => fetch('/api/actions').then(r => r.json()).then(setActions).catch(() => {})
    load()
    const t = setInterval(load, 10000)
    return () => clearInterval(t)
  }, [])

  return (
    <div style={{ background:'#111', border:'1px solid #1e1e1e', padding:22, display:'flex', flexDirection:'column', gap:0 }}>
      <div style={{ fontSize:14, letterSpacing:2, color:'#a6a6a6', marginBottom:16, flexShrink:0 }}>AUTONOMOUS ACTIONS</div>

      <div style={{ flex:1, overflow:'auto', maxHeight:380, display:'flex', flexDirection:'column', gap:2 }}>
        {actions.length === 0 ? (
          <div style={{ fontSize:14, color:'#757575', textAlign:'center', padding:32 }}>
            No actions yet — daemon will populate this
          </div>
        ) : actions.map((a, i) => {
          const c = CAT[a.category] || def
          return (
            <div key={i} style={{ display:'flex', gap:12, padding:'10px 0', borderBottom:'1px solid #141414' }}>
              <div style={{ width:32, height:32, borderRadius:3, flexShrink:0, background:`${c.color}15`, border:`1px solid ${c.color}25`, display:'flex', alignItems:'center', justifyContent:'center', fontSize:15, color:c.color }}>
                {c.icon}
              </div>
              <div style={{ flex:1, minWidth:0 }}>
                <div style={{ display:'flex', justifyContent:'space-between', alignItems:'flex-start' }}>
                  <span style={{ fontSize:14, color: c.color, overflow:'hidden', textOverflow:'ellipsis', whiteSpace:'nowrap', maxWidth:180 }}>
                    {a.action}
                  </span>
                  <span style={{ fontSize:12, color:'#858585', flexShrink:0, marginLeft:8 }}>
                    {(a.timestamp || '').slice(11,19)}
                  </span>
                </div>
                <div style={{ fontSize:13, color:'#858585', marginTop:3 }}>
                  {a.resource}{a.namespace ? ` · ${a.namespace}` : ''}
                  {!a.success && <span style={{ color:'#ef4444', marginLeft:8 }}>✗ failed</span>}
                </div>
              </div>
            </div>
          )
        })}
      </div>
    </div>
  )
}
