import { useState } from 'react'

export default function DaemonControl({ liveData }) {
  const [loading, setLoading] = useState(false)
  const running = liveData?.stats?.daemon_running ?? false

  const toggle = async () => {
    setLoading(true)
    try { await fetch(running ? '/api/daemon/stop' : '/api/daemon/start', { method:'POST' }) }
    catch (_) {}
    setLoading(false)
  }

  return (
    <div style={{ display:'flex', alignItems:'center', gap:16 }}>
      <div style={{ display:'flex', alignItems:'center', gap:9 }}>
        <div style={{ position:'relative', width:11, height:11, flexShrink:0 }}>
          <div style={{ width:11, height:11, borderRadius:'50%', background: running ? '#22c55e' : '#555' }} />
          {running && <div style={{ position:'absolute', inset:-4, borderRadius:'50%', border:'2px solid #22c55e33', animation:'ping 1.8s ease-out infinite' }} />}
        </div>
        <span style={{ fontSize:13, color: running ? '#22c55e' : '#9a9a9a', letterSpacing:1 }}>
          {running ? 'DAEMON RUNNING' : 'DAEMON STOPPED'}
        </span>
      </div>
      <button
        onClick={toggle} disabled={loading}
        style={{
          background:'transparent', border:`1px solid ${running ? '#ef444455' : '#22c55e55'}`,
          color: running ? '#ef4444' : '#22c55e',
          padding:'7px 22px', cursor: loading ? 'not-allowed' : 'pointer',
          fontSize:13, letterSpacing:2, transition:'all .15s', opacity: loading ? 0.5 : 1,
        }}
        onMouseEnter={e => { if (!loading) e.currentTarget.style.background = running ? '#ef444410' : '#22c55e10' }}
        onMouseLeave={e => { e.currentTarget.style.background = 'transparent' }}
      >
        {loading ? '···' : running ? 'STOP' : 'START'}
      </button>
      <style>{`@keyframes ping { 0%{transform:scale(1);opacity:.6} 100%{transform:scale(2.4);opacity:0} }`}</style>
    </div>
  )
}
