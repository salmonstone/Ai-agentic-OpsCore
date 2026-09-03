import { useState, useEffect, useCallback } from 'react'
import Sidebar from './components/Sidebar'
import LiveClusterPanel from './components/LiveClusterPanel'
import DaemonControl from './components/DaemonControl'
import ActivityFeed from './components/ActivityFeed'
import CommandRunner from './components/CommandRunner'
import PendingApprovals from './components/PendingApprovals'
import IncidentsPanel from './components/IncidentsPanel'
import SLOPanel from './components/SLOPanel'
import ClustersPanel from './components/ClustersPanel'
import AboutPanel from './components/AboutPanel'

export default function App() {
  const [commandGroups, setCommandGroups] = useState({})
  const [activeCmd, setActiveCmd]         = useState(null)
  const [activeSection, setActiveSection] = useState('overview')
  const [liveData, setLiveData]           = useState(null)
  const [cmdCount, setCmdCount]           = useState(0)
  const [aboutOpen, setAboutOpen]         = useState(false)

  useEffect(() => {
    fetch('/api/commands')
      .then(r => r.json())
      .then(data => {
        setCommandGroups(data)
        setCmdCount(Object.values(data).reduce((s, c) => s + c.length, 0))
      }).catch(() => {})
  }, [])

  useEffect(() => {
    let ws, retryTimer
    const connect = () => {
      const proto = location.protocol === 'https:' ? 'wss' : 'ws'
      ws = new WebSocket(`${proto}://${location.host}/ws/live`)
      ws.onmessage = e => { try { setLiveData(JSON.parse(e.data)) } catch (_) {} }
      ws.onclose   = () => { retryTimer = setTimeout(connect, 3000) }
    }
    connect()
    return () => { ws?.close(); clearTimeout(retryTimer) }
  }, [])

  const handleSelectCmd = useCallback((cmd) => setActiveCmd(cmd), [])
  const handleNav = useCallback((s) => { setActiveSection(s); setActiveCmd(null) }, [])

  const stats = liveData?.stats || {}

  return (
    <div style={{ display:'flex', flexDirection:'column', height:'100vh', background:'#080808', fontFamily:"'Courier New',monospace", overflow:'hidden' }}>

      {/* ── TOP BAR ─────────────────────────────────────────────── */}
      <header style={{
        display:'flex', alignItems:'center', justifyContent:'space-between',
        padding:'0 28px', height:66, borderBottom:'1px solid #1e1e1e',
        background:'linear-gradient(180deg,#0e0e0e 0%,#0a0a0a 100%)',
        flexShrink:0, zIndex:10,
      }}>
        <div style={{ display:'flex', alignItems:'center', gap:24 }}>
          {/* logo */}
          <div style={{ display:'flex', alignItems:'center', gap:11 }}>
            <div style={{ width:28, height:28, background:'#e05020', clipPath:'polygon(0 0,100% 0,100% 65%,65% 100%,0 100%)', flexShrink:0, boxShadow:'0 0 16px #e0502055' }} />
            <span style={{ color:'#fff', fontSize:19, fontWeight:700, letterSpacing:5 }}>ATLASOS</span>
          </div>
          <div style={{ width:1, height:32, background:'#1e1e1e' }} />
          {/* live chips */}
          <div style={{ display:'flex', gap:9 }}>
            <StatChip label="HEALED" value={stats.pods_healed_today ?? 0} color="#22c55e" />
            <StatChip label="INCIDENTS" value={stats.incidents_open ?? 0} color={stats.incidents_open > 0 ? '#ef4444' : '#22c55e'} />
            <StatChip label="SAVED/MO" value={`$${(stats.cost_saved_month ?? 0).toFixed(0)}`} color="#f59e0b" />
            <StatChip label="CMDS" value={cmdCount} color="#818cf8" />
          </div>
        </div>
        <div style={{ display:'flex', alignItems:'center', gap:14 }}>
          <button
            onClick={() => setAboutOpen(true)}
            title="About AtlasOS — architecture & live project scan"
            aria-label="About AtlasOS"
            style={{
              width:32, height:32, borderRadius:'50%', flexShrink:0,
              background:'transparent', border:'1px solid #2a2a2a', color:'#a6a6a6',
              cursor:'pointer', fontSize:16, fontWeight:700, lineHeight:1,
              display:'flex', alignItems:'center', justifyContent:'center', transition:'all .15s',
            }}
            onMouseEnter={e => { e.currentTarget.style.borderColor='#e05020'; e.currentTarget.style.color='#e05020' }}
            onMouseLeave={e => { e.currentTarget.style.borderColor='#2a2a2a'; e.currentTarget.style.color='#a6a6a6' }}>
            ?
          </button>
          <DaemonControl liveData={liveData} />
        </div>
      </header>

      <AboutPanel open={aboutOpen} onClose={() => setAboutOpen(false)} />

      {/* ── BODY ────────────────────────────────────────────────── */}
      <div style={{ display:'flex', flex:1, overflow:'hidden' }}>

        {/* Sidebar */}
        <Sidebar
          commandGroups={commandGroups}
          activeSection={activeSection}
          activeCmd={activeCmd}
          onSelectCmd={handleSelectCmd}
          onNav={handleNav}
        />

        {/* Main content */}
        <main style={{ flex:1, overflow:'auto', padding:24, display:'flex', flexDirection:'column', gap:20 }}>
          {activeSection === 'overview'  && <OverviewPage liveData={liveData} />}
          {activeSection === 'clusters'  && <ClustersPanel />}
          {activeSection === 'incidents' && <IncidentsPanel />}
          {activeSection === 'slos'      && <SLOPanel />}
          {activeSection === 'approvals' && <PendingApprovals />}
        </main>

        {/* ── COMMAND PANEL (slides in from right) ─────────────── */}
        <div style={{
          width: activeCmd ? 460 : 0,
          minWidth: activeCmd ? 460 : 0,
          overflow: 'hidden',
          transition: 'width .22s cubic-bezier(.4,0,.2,1), min-width .22s cubic-bezier(.4,0,.2,1)',
          borderLeft: activeCmd ? '1px solid #1e1e1e' : 'none',
          background: '#0a0a0a',
          flexShrink: 0,
          display: 'flex',
          flexDirection: 'column',
        }}>
          {activeCmd && (
            <div style={{ width:460, height:'100%', overflow:'auto', padding:24, display:'flex', flexDirection:'column', gap:0 }}>
              <div style={{ display:'flex', alignItems:'center', justifyContent:'space-between', marginBottom:22, flexShrink:0 }}>
                <div>
                  <div style={{ fontSize:12, letterSpacing:2, color:'#9a9a9a', marginBottom:5 }}>RUN COMMAND</div>
                  <div style={{ fontSize:17, color:'#e05020', letterSpacing:1 }}>{activeCmd.full_command}</div>
                </div>
                <button onClick={() => setActiveCmd(null)} style={{
                  background:'transparent', border:'1px solid #222', color:'#a6a6a6',
                  cursor:'pointer', fontSize:18, lineHeight:1, padding:'4px 8px',
                  transition:'all .15s',
                }}
                  onMouseEnter={e => { e.currentTarget.style.borderColor='#e05020'; e.currentTarget.style.color='#e05020' }}
                  onMouseLeave={e => { e.currentTarget.style.borderColor='#222'; e.currentTarget.style.color='#a6a6a6' }}>
                  ✕
                </button>
              </div>
              <CommandRunner cmd={activeCmd} key={activeCmd.full_command} />
            </div>
          )}
        </div>
      </div>
    </div>
  )
}

function StatChip({ label, value, color }) {
  return (
    <div style={{ display:'flex', alignItems:'center', gap:8, padding:'6px 15px', background:`${color}0f`, border:`1px solid ${color}28`, borderRadius:2 }}>
      <span style={{ fontSize:12, letterSpacing:1.5, color:'#9a9a9a' }}>{label}</span>
      <span style={{ fontSize:16, fontWeight:700, color, letterSpacing:0.5 }}>{value}</span>
    </div>
  )
}

function OverviewPage({ liveData }) {
  const stats = liveData?.stats || {}
  return (
    <div style={{ display:'flex', flexDirection:'column', gap:20 }}>
      <div style={{ display:'grid', gridTemplateColumns:'repeat(4,1fr)', gap:14 }}>
        <BigStatCard label="PODS HEALED TODAY" value={stats.pods_healed_today ?? '—'} sub="auto-fixed by daemon" accent="#22c55e" />
        <BigStatCard label="OPEN INCIDENTS"    value={stats.incidents_open ?? '—'}    sub="active right now"    accent={stats.incidents_open > 0 ? '#ef4444' : '#22c55e'} />
        <BigStatCard label="COST SAVED / 30D"  value={`$${(stats.cost_saved_month ?? 0).toFixed(0)}`} sub="AWS waste eliminated" accent="#f59e0b" />
        <BigStatCard label="ACTIONS / 30D"     value={stats.actions_30d ?? '—'}       sub="autonomous operations" accent="#818cf8" />
      </div>
      <div style={{ display:'grid', gridTemplateColumns:'1.4fr 1fr', gap:14 }}>
        <LiveClusterPanel liveData={liveData} />
        <ActivityFeed />
      </div>
    </div>
  )
}

function BigStatCard({ label, value, sub, accent }) {
  return (
    <div style={{
      background:'#111', border:'1px solid #1e1e1e', borderLeft:`3px solid ${accent}`,
      padding:'22px 24px', cursor:'default', transition:'box-shadow .2s, border-color .2s',
    }}
      onMouseEnter={e => { e.currentTarget.style.boxShadow = `0 0 0 1px ${accent}30`; e.currentTarget.style.borderColor = `${accent}60` }}
      onMouseLeave={e => { e.currentTarget.style.boxShadow = 'none'; e.currentTarget.style.borderTopColor = '#1e1e1e'; e.currentTarget.style.borderRightColor = '#1e1e1e'; e.currentTarget.style.borderBottomColor = '#1e1e1e' }}>
      <div style={{ fontSize:13, letterSpacing:2, color:'#9a9a9a', marginBottom:14 }}>{label}</div>
      <div style={{ fontSize:40, fontWeight:700, color:'#fff', letterSpacing:1, lineHeight:1 }}>{value}</div>
      <div style={{ fontSize:14, color:'#858585', marginTop:12 }}>{sub}</div>
    </div>
  )
}
