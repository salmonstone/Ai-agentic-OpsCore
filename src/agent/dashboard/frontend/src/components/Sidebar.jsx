import { useState } from 'react'

const NAV_ITEMS = [
  { id: 'overview',  label: 'Overview',  icon: '◈' },
  { id: 'clusters',  label: 'Clusters',  icon: '☸' },
  { id: 'incidents', label: 'Incidents', icon: '⚡' },
  { id: 'slos',      label: 'SLOs',      icon: '◎' },
  { id: 'approvals', label: 'Approvals', icon: '✦' },
]

const ICON_MAP = {
  k8s: '☸', resources: '◈', aws: '☁', cost: '$', deploy: '↑',
  daemon: '⚙', incident: '⚡', slo: '◎', db: '◫', scale: '⤢',
  runbook: '▶', page: '⟳', monitor: '◉', tls: '🔒', dns: '◦',
  ingress: '→', network: '⟷', memory: '▦', gmail: '✉', domain: '◎',
  eval: '✓', root: '●', dashboard: '⊞',
}

const GROUP_COLOR = {
  k8s: '#60a5fa', cost: '#f59e0b', incident: '#ef4444', slo: '#22c55e',
  deploy: '#a78bfa', db: '#34d399', scale: '#f97316', runbook: '#e05020',
  daemon: '#818cf8', aws: '#f59e0b', page: '#ef4444',
}

export default function Sidebar({ commandGroups, activeSection, activeCmd, onSelectCmd, onNav }) {
  const [expanded, setExpanded] = useState({})
  const [search, setSearch]     = useState('')

  const toggle = (g) => setExpanded(p => ({ ...p, [g]: !p[g] }))

  const filterCmd = (cmd) =>
    !search || cmd.name.includes(search.toLowerCase()) ||
    (cmd.help_text || '').toLowerCase().includes(search.toLowerCase())

  return (
    <nav style={{ width:256, background:'#0a0a0a', borderRight:'1px solid #1e1e1e', display:'flex', flexDirection:'column', flexShrink:0, overflow:'hidden' }}>

      {/* search */}
      <div style={{ padding:'14px 16px', borderBottom:'1px solid #1e1e1e' }}>
        <div style={{ position:'relative' }}>
          <span style={{ position:'absolute', left:11, top:'50%', transform:'translateY(-50%)', fontSize:15, color:'#757575', pointerEvents:'none' }}>⌕</span>
          <input
            value={search}
            onChange={e => setSearch(e.target.value)}
            placeholder="Search commands…"
            style={{
              width:'100%', background:'#111', border:'1px solid #1e1e1e', color:'#ccc',
              padding:'9px 10px 9px 32px', fontSize:14, fontFamily:'inherit',
              outline:'none', boxSizing:'border-box', transition:'border-color .15s',
            }}
            onFocus={e => e.target.style.borderColor = '#e05020'}
            onBlur={e => e.target.style.borderColor = '#1e1e1e'}
          />
        </div>
      </div>

      {/* fixed nav */}
      <div style={{ padding:'8px 0', borderBottom:'1px solid #1e1e1e' }}>
        {NAV_ITEMS.map(n => (
          <NavItem key={n.id} item={n} active={activeSection === n.id && !activeCmd} onClick={() => onNav(n.id)} />
        ))}
      </div>

      {/* command groups */}
      <div style={{ flex:1, overflowY:'auto', padding:'6px 0' }}>
        {Object.entries(commandGroups).map(([group, cmds]) => {
          const filtered = cmds.filter(filterCmd)
          if (filtered.length === 0) return null
          const isOpen = expanded[group] !== false
          const icon   = ICON_MAP[group] || '●'
          const gColor = GROUP_COLOR[group] || '#9a9a9a'

          return (
            <div key={group}>
              <button
                onClick={() => toggle(group)}
                style={{
                  width:'100%', display:'flex', alignItems:'center', justifyContent:'space-between',
                  padding:'9px 16px', background:'transparent', border:'none', cursor:'pointer',
                }}
                onMouseEnter={e => e.currentTarget.querySelector('span').style.color = '#e0e0e0'}
                onMouseLeave={e => e.currentTarget.querySelector('span').style.color = isOpen ? '#b4b4b4' : '#858585'}
              >
                <span style={{ display:'flex', alignItems:'center', gap:9, fontSize:13, letterSpacing:1.5, color: isOpen ? '#b4b4b4' : '#858585', transition:'color .15s' }}>
                  <span style={{ fontSize:16, color: gColor }}>{icon}</span>
                  <span style={{ textTransform:'uppercase' }}>{group}</span>
                  <span style={{ fontSize:12, color:'#6a6a6a' }}>({filtered.length})</span>
                </span>
                <span style={{ fontSize:11, color:'#858585' }}>{isOpen ? '▾' : '▸'}</span>
              </button>

              {isOpen && (
                <div>
                  {filtered.map(cmd => {
                    const isActive = activeCmd?.full_command === cmd.full_command
                    return (
                      <button
                        key={cmd.full_command}
                        onClick={() => onSelectCmd(cmd)}
                        title={cmd.help_text}
                        style={{
                          width:'100%', display:'flex', alignItems:'center', gap:8,
                          padding:'8px 16px 8px 34px',
                          background: isActive ? '#151515' : 'transparent',
                          border:'none', borderLeft:`2px solid ${isActive ? gColor : 'transparent'}`,
                          cursor:'pointer', textAlign:'left', transition:'all .12s',
                        }}
                        onMouseEnter={e => { if (!isActive) { e.currentTarget.style.background='#111'; e.currentTarget.style.borderLeftColor='#252525' } }}
                        onMouseLeave={e => { if (!isActive) { e.currentTarget.style.background='transparent'; e.currentTarget.style.borderLeftColor='transparent' } }}
                      >
                        {cmd.is_destructive && (
                          <span style={{ fontSize:11, color:'#f59e0b', flexShrink:0 }} title="Destructive">⚠</span>
                        )}
                        <span style={{ fontSize:14, letterSpacing:.2, color: isActive ? '#fff' : '#a8a8a8', transition:'color .12s', overflow:'hidden', textOverflow:'ellipsis', whiteSpace:'nowrap' }}>
                          {cmd.name}
                        </span>
                      </button>
                    )
                  })}
                </div>
              )}
            </div>
          )
        })}
      </div>

      <div style={{ padding:'12px 16px', borderTop:'1px solid #1e1e1e', fontSize:13, color:'#757575', letterSpacing:1 }}>
        ATLASOS v0.1
      </div>
    </nav>
  )
}

function NavItem({ item, active, onClick }) {
  return (
    <button
      onClick={onClick}
      style={{
        width:'100%', display:'flex', alignItems:'center', gap:11,
        padding:'11px 18px',
        background: active ? '#161616' : 'transparent',
        border:'none', borderLeft:`2px solid ${active ? '#e05020' : 'transparent'}`,
        cursor:'pointer', transition:'all .12s', textAlign:'left',
      }}
      onMouseEnter={e => { if (!active) e.currentTarget.style.background = '#111' }}
      onMouseLeave={e => { if (!active) e.currentTarget.style.background = 'transparent' }}
    >
      <span style={{ fontSize:18, color: active ? '#e05020' : '#858585', transition:'color .12s' }}>{item.icon}</span>
      <span style={{ fontSize:15, letterSpacing:.5, color: active ? '#fff' : '#a8a8a8', transition:'color .12s' }}>{item.label}</span>
    </button>
  )
}
