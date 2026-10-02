import { useState } from 'react'
import { Icon } from './common'

// A tree for the sidebar's collapsible groups. Flattened to NAV below for
// everything else (Palette's "Go to", page titles, nav badges) — same ids,
// same order they were in before, just visually grouped here.
export const NAV_TREE = [
  { id: 'overview', label: 'Overview', icon: 'ph-squares-four' },
  { group: 'Infrastructure', icon: 'ph-cube', items: [
    { id: 'cluster', label: 'Cluster', icon: 'ph-cube' },
    { id: 'databases', label: 'Databases', icon: 'ph-database' },
    { id: 'network', label: 'Network', icon: 'ph-share-network' },
    { id: 'costwaste', label: 'Cost & Waste', icon: 'ph-coins' },
  ] },
  { group: 'CI/CD', icon: 'ph-git-branch', items: [
    { id: 'jenkins', label: 'Jenkins', icon: 'ph-hammer' },
    { id: 'github', label: 'GitHub Actions', icon: 'ph-github-logo' },
    { id: 'deploys', label: 'Deploys', icon: 'ph-rocket-launch' },
  ] },
  { group: 'Cloud', icon: 'ph-cloud', items: [
    { id: 'aws', label: 'AWS', icon: 'ph-cloud' },
    { id: 'domains', label: 'Domains & HTTPS', icon: 'ph-globe-hemisphere-west' },
  ] },
  { group: 'Operations', icon: 'ph-gear-six', items: [
    { id: 'automation', label: 'Automation', icon: 'ph-robot' },
    { id: 'commands', label: 'Command Runner', icon: 'ph-terminal-window' },
    { id: 'system', label: 'System', icon: 'ph-cpu' },
  ] },
  { id: 'incidents', label: 'Incidents & SLOs', icon: 'ph-siren' },
  { id: 'approvals', label: 'Approvals', icon: 'ph-seal-check' },
  { id: 'activity', label: 'Activity', icon: 'ph-clock-counter-clockwise' },
  { id: 'settings', label: 'Settings', icon: 'ph-gear' },
  { id: 'about', label: 'About', icon: 'ph-info' },
]
export const NAV = NAV_TREE.flatMap(n => (n.items ? n.items : [n]))

function loadCollapsed() {
  try { return new Set(JSON.parse(localStorage.getItem('atlas-nav-collapsed') || '[]')) } catch { return new Set() }
}
function saveCollapsed(set) {
  try { localStorage.setItem('atlas-nav-collapsed', JSON.stringify([...set])) } catch { /* private window */ }
}

function NavButton({ n, cur, badge, onNav, indent }) {
  return (
    <button className={cur ? '' : 'navbtn'} onClick={() => onNav(n.id)} aria-current={cur ? 'page' : undefined}
      style={{
        display: 'flex', alignItems: 'center', gap: 10, width: '100%', padding: indent ? '6px 10px 6px 26px' : '6px 10px', border: 0,
        borderRadius: 'var(--radius-md)', cursor: 'pointer', textAlign: 'left', fontSize: 13,
        background: cur ? 'color-mix(in srgb, var(--color-accent) 16%, transparent)' : 'transparent',
        color: cur ? 'var(--color-text)' : 'color-mix(in srgb, var(--color-text) 80%, transparent)',
      }}>
      <Icon name={n.icon} size={16} style={{ color: cur ? 'var(--color-accent)' : 'var(--muted)' }} />
      <span style={{ flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{n.label}</span>
      {badge ? (
        <span className="mono" style={{
          minWidth: 18, height: 18, padding: '0 5px', borderRadius: 9, display: 'inline-grid', placeItems: 'center', fontSize: 10.5,
          color: badge.t === 'crit' ? 'var(--st-crit)' : 'var(--st-warn)',
          background: `color-mix(in srgb, ${badge.t === 'crit' ? 'var(--st-crit)' : 'var(--st-warn)'} 14%, transparent)`,
        }}>{badge.n}</span>
      ) : null}
    </button>
  )
}

export default function Sidebar({ panel, onNav, badges, live, context, theme, onToggleTheme }) {
  const [collapsed, setCollapsed] = useState(loadCollapsed)
  const toggleGroup = label => setCollapsed(c => {
    const next = new Set(c); next.has(label) ? next.delete(label) : next.add(label); saveCollapsed(next); return next
  })
  const daemonOn = live?.stats?.daemon_running
  const offline = live?.offline
  const known = live != null

  return (
    <aside style={{
      position: 'sticky', top: 'var(--topbar-h)', height: 'calc(100vh - var(--topbar-h))', display: 'flex', flexDirection: 'column',
      padding: '10px 10px 12px', background: 'color-mix(in srgb, var(--color-surface) 35%, var(--color-bg))',
      boxShadow: 'inset -1px 0 0 var(--color-divider)', overflowY: 'auto',
    }}>
      <nav style={{ display: 'flex', flexDirection: 'column', gap: 1, flex: 1 }}>
        {NAV_TREE.map(n => {
          if (!n.items) return <NavButton key={n.id} n={n} cur={panel === n.id} badge={badges[n.id]} onNav={onNav} />
          const isCollapsed = collapsed.has(n.group)
          const groupHasCurrent = n.items.some(i => i.id === panel)
          return (
            <div key={n.group} style={{ marginTop: 10 }}>
              <button onClick={() => toggleGroup(n.group)} aria-expanded={!isCollapsed} style={{
                display: 'flex', alignItems: 'center', gap: 8, width: '100%', padding: '4px 10px', border: 0, background: 'transparent',
                cursor: 'pointer', textAlign: 'left', color: groupHasCurrent ? 'var(--color-text)' : 'var(--muted)',
              }}>
                <Icon name={n.icon} size={13} />
                <span className="kicker" style={{ flex: 1, fontSize: 10.5, color: 'inherit' }}>{n.group}</span>
                <Icon name={isCollapsed ? 'ph-caret-right' : 'ph-caret-down'} size={11} />
              </button>
              {!isCollapsed && (
                <div style={{ display: 'flex', flexDirection: 'column', gap: 1, marginTop: 2 }}>
                  {n.items.map(i => <NavButton key={i.id} n={i} cur={panel === i.id} badge={badges[i.id]} onNav={onNav} indent />)}
                </div>
              )}
            </div>
          )
        })}
      </nav>

      <div className="surface" style={{ marginTop: 10, display: 'flex', flexDirection: 'column', gap: 9, padding: 10 }}>
        <div style={{ display: 'flex', alignItems: 'flex-start', gap: 8 }}>
          <Icon name={!known ? 'ph-question' : daemonOn ? 'ph-heartbeat' : 'ph-pause-circle'} size={15}
            style={{ marginTop: 1, color: !known ? 'var(--st-unk)' : daemonOn ? 'var(--st-ok)' : 'var(--st-warn)' }} />
          <div style={{ minWidth: 0 }}>
            <div style={{ fontSize: 12, fontWeight: 500 }}>Daemon · {!known ? 'unknown' : daemonOn ? 'running' : 'stopped'}</div>
            <div className="muted" style={{ fontSize: 11 }}>{!known ? 'waiting for the server' : daemonOn ? 'auto-healing on' : 'auto-healing paused'}</div>
          </div>
        </div>
        <div style={{ display: 'flex', alignItems: 'flex-start', gap: 8 }}>
          <Icon name={!known ? 'ph-question' : offline ? 'ph-plugs' : 'ph-cube'} size={15}
            style={{ marginTop: 1, color: !known || offline ? 'var(--st-unk)' : 'var(--st-ok)' }} />
          <div style={{ minWidth: 0 }}>
            <div className="mono" style={{ fontSize: 11.5, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{context || 'no kube context'}</div>
            <div style={{ fontSize: 11, color: !known || offline ? 'var(--st-unk)' : 'var(--muted)' }}>
              {!known ? 'not checked yet' : offline ? "unreachable — couldn't check" : 'API server reachable'}
            </div>
          </div>
        </div>
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', fontSize: 11, paddingTop: 8, background: 'var(--rule) no-repeat top / 100% 1px' }}>
          <span className="muted">{live?.timestamp ? `live · ${new Date(live.timestamp).toLocaleTimeString()}` : 'connecting…'}</span>
          <button className="btn btn-ghost btn-icon" onClick={onToggleTheme} aria-label="Toggle theme" title={`Switch to ${theme === 'dark' ? 'light' : 'dark'} theme`} style={{ width: 26, height: 26 }}>
            <Icon name={theme === 'dark' ? 'ph-sun' : 'ph-moon'} size={15} />
          </button>
        </div>
      </div>
    </aside>
  )
}
