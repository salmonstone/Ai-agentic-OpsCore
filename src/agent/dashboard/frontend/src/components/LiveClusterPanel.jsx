import { AreaChart, Area, XAxis, YAxis, ResponsiveContainer, Tooltip } from 'recharts'
import { useState, useEffect } from 'react'

const phaseColor = (phase, restarts) => {
  if (restarts > 5)          return '#ef4444'
  if (phase === 'Running')   return '#22c55e'
  if (phase === 'Pending')   return '#f59e0b'
  if (phase === 'Failed')    return '#ef4444'
  return '#9a9a9a'
}

export default function LiveClusterPanel({ liveData }) {
  const [history, setHistory] = useState([])
  const [retrying, setRetrying] = useState(false)
  const pods    = liveData?.pods    || []
  const nodes   = liveData?.nodes   || []
  const summary = liveData?.summary || {}
  const ts      = liveData?.timestamp
  const offline = liveData?.offline === true || (liveData && pods.length === 0 && nodes.length === 0)

  useEffect(() => {
    if (!liveData || offline) return
    setHistory(prev => [...prev, {
      t: new Date(ts || Date.now()).toTimeString().slice(0, 8),
      running:  summary.running  || 0,
      crashing: summary.crashing || 0,
    }].slice(-24))
  }, [ts])

  if (offline) {
    return (
      <div style={{ background:'#111', border:'1px solid #ef444430', padding:22, display:'flex', flexDirection:'column', gap:16 }}>
        {/* offline header */}
        <div style={{ display:'flex', alignItems:'center', justifyContent:'space-between' }}>
          <div style={{ display:'flex', alignItems:'center', gap:12 }}>
            <div style={{ width:11, height:11, borderRadius:'50%', background:'#ef4444', boxShadow:'0 0 8px #ef4444' }} />
            <div>
              <div style={{ fontSize:14, letterSpacing:2, color:'#ef4444' }}>CLUSTER OFFLINE</div>
              {ts && <div style={{ fontSize:12, color:'#858585', marginTop:3 }}>last checked {new Date(ts).toTimeString().slice(0,8)}</div>}
            </div>
          </div>
          <button
            onClick={() => { setRetrying(true); setTimeout(() => setRetrying(false), 3000) }}
            style={{ background:'#1a1a1a', border:'1px solid #2a2a2a', color:'#b0b0b0', padding:'7px 18px', fontSize:13, cursor:'pointer', letterSpacing:1, fontFamily:'inherit' }}
            onMouseEnter={e => e.currentTarget.style.borderColor='#555'}
            onMouseLeave={e => e.currentTarget.style.borderColor='#2a2a2a'}
          >
            {retrying ? '⟳ CHECKING…' : '↺ RETRY'}
          </button>
        </div>

        {/* status box */}
        <div style={{ background:'#0d0d0d', border:'1px solid #1e1e1e', padding:'16px 20px', display:'flex', flexDirection:'column', gap:12 }}>
          <div style={{ fontSize:14, color:'#9a9a9a' }}>kubectl cannot reach the cluster. Possible causes:</div>
          <div style={{ display:'flex', flexDirection:'column', gap:8 }}>
            {[
              ['No cluster running', 'minikube or Docker Desktop Kubernetes is not started'],
              ['Context not set', 'kubectl config use-context <name>'],
              ['Kubeconfig missing', '~/.kube/config does not exist or is empty'],
            ].map(([title, desc]) => (
              <div key={title} style={{ display:'flex', gap:10 }}>
                <span style={{ color:'#ef4444', flexShrink:0, fontSize:14 }}>›</span>
                <div>
                  <span style={{ fontSize:14, color:'#d0d0d0' }}>{title}</span>
                  <span style={{ fontSize:13, color:'#858585', marginLeft:8 }}>— {desc}</span>
                </div>
              </div>
            ))}
          </div>
        </div>

        {/* quick start commands */}
        <div style={{ background:'#090909', border:'1px solid #1a1a1a', padding:'14px 18px' }}>
          <div style={{ fontSize:12, letterSpacing:2, color:'#858585', marginBottom:12 }}>QUICK START</div>
          {[
            'minikube start --driver=docker',
            'minikube start --driver=virtualbox',
            'kubectl config get-contexts',
          ].map(cmd => (
            <div key={cmd} style={{ display:'flex', alignItems:'center', gap:10, marginBottom:8 }}>
              <span style={{ color:'#757575', fontSize:14 }}>$</span>
              <code style={{ fontSize:14, color:'#b0b0b0', fontFamily:'Courier New,monospace', letterSpacing:.3 }}>{cmd}</code>
            </div>
          ))}
        </div>

        <div style={{ fontSize:13, color:'#858585' }}>
          Non-k8s commands (incident, slo, runbook, cost, daemon) work without a cluster.
        </div>
      </div>
    )
  }

  return (
    <div style={{ background:'#111', border:'1px solid #1e1e1e', padding:22, display:'flex', flexDirection:'column', gap:18 }}>
      {/* header */}
      <div style={{ display:'flex', alignItems:'center', justifyContent:'space-between' }}>
        <div>
          <div style={{ fontSize:14, letterSpacing:2, color:'#a6a6a6' }}>LIVE CLUSTER</div>
          {ts && <div style={{ fontSize:12, color:'#858585', marginTop:3 }}>updated {new Date(ts).toTimeString().slice(0,8)}</div>}
        </div>
        <div style={{ display:'flex', gap:8 }}>
          <Badge label="RUNNING"  value={summary.running  ?? 0} color="#22c55e" />
          <Badge label="PENDING"  value={summary.pending  ?? 0} color="#f59e0b" />
          <Badge label="CRASHING" value={summary.crashing ?? 0} color="#ef4444" />
        </div>
      </div>

      {/* sparkline */}
      {history.length > 2 && (
        <div style={{ height:80, marginTop:-4 }}>
          <ResponsiveContainer width="100%" height="100%">
            <AreaChart data={history} margin={{ top:4, right:0, left:0, bottom:0 }}>
              <defs>
                <linearGradient id="gRun" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="5%"  stopColor="#e05020" stopOpacity={0.25} />
                  <stop offset="95%" stopColor="#e05020" stopOpacity={0} />
                </linearGradient>
                <linearGradient id="gCrash" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="5%"  stopColor="#ef4444" stopOpacity={0.2} />
                  <stop offset="95%" stopColor="#ef4444" stopOpacity={0} />
                </linearGradient>
              </defs>
              <XAxis dataKey="t" hide />
              <YAxis hide />
              <Tooltip
                contentStyle={{ background:'#161616', border:'1px solid #2a2a2a', fontSize:13, fontFamily:'Courier New,monospace' }}
                labelStyle={{ color:'#9a9a9a' }} itemStyle={{ color:'#e0e0e0' }}
              />
              <Area type="monotone" dataKey="running"  stroke="#e05020" fill="url(#gRun)"   strokeWidth={1.5} dot={false} name="Running" />
              <Area type="monotone" dataKey="crashing" stroke="#ef4444" fill="url(#gCrash)" strokeWidth={1}   dot={false} name="Crashing" />
            </AreaChart>
          </ResponsiveContainer>
        </div>
      )}

      {/* pod table */}
      <div style={{ overflow:'auto', maxHeight:240, border:'1px solid #1a1a1a' }}>
        <table style={{ width:'100%', borderCollapse:'collapse' }}>
          <thead>
            <tr style={{ position:'sticky', top:0, background:'#0f0f0f' }}>
              {['NAMESPACE','NAME','PHASE','RESTARTS'].map(h => (
                <th key={h} style={{ textAlign:'left', padding:'8px 14px', color:'#858585', fontSize:12, letterSpacing:1.5, fontWeight:'normal', borderBottom:'1px solid #1a1a1a' }}>{h}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {pods.slice(0, 40).map((p, i) => (
              <tr key={i} style={{ borderBottom:'1px solid #141414', cursor:'default' }}
                onMouseEnter={e => e.currentTarget.style.background='#141414'}
                onMouseLeave={e => e.currentTarget.style.background='transparent'}>
                <td style={{ padding:'7px 14px', color:'#a6a6a6', fontSize:13 }}>{p.namespace}</td>
                <td style={{ padding:'7px 14px', color:'#e0e0e0', fontSize:13, maxWidth:180, overflow:'hidden', textOverflow:'ellipsis', whiteSpace:'nowrap' }}>{p.name}</td>
                <td style={{ padding:'7px 14px' }}>
                  <span style={{ fontSize:13, color: phaseColor(p.phase, p.restarts) }}>{p.phase}</span>
                </td>
                <td style={{ padding:'7px 14px', fontSize:14, fontWeight: p.restarts > 3 ? 700 : 400, color: p.restarts > 5 ? '#ef4444' : p.restarts > 0 ? '#f59e0b' : '#a6a6a6' }}>
                  {p.restarts}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {/* nodes */}
      {nodes.length > 0 && (
        <div style={{ display:'flex', gap:8, flexWrap:'wrap' }}>
          {nodes.map((n, i) => (
            <div key={i} style={{ fontSize:13, letterSpacing:.5, padding:'5px 13px', border:`1px solid ${n.ready ? '#22c55e30' : '#ef444430'}`, color: n.ready ? '#22c55e' : '#ef4444', background: n.ready ? '#22c55e08' : '#ef444408' }}>
              {n.name} · {n.roles}
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

function Badge({ label, value, color }) {
  return (
    <div style={{ fontSize:12, letterSpacing:1, padding:'5px 13px', border:`1px solid ${color}28`, color, background:`${color}0a` }}>
      {label} <span style={{ fontWeight:700 }}>{value}</span>
    </div>
  )
}
