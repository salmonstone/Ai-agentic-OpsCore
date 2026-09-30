import { Icon } from './common'

export const NAV = [
  { id: 'overview', label: 'Overview', icon: 'ph-squares-four' },
  { id: 'cluster', label: 'Cluster', icon: 'ph-cube' },
  { id: 'jenkins', label: 'Jenkins', icon: 'ph-hammer' },
  { id: 'approvals', label: 'Approvals', icon: 'ph-seal-check' },
  { id: 'incidents', label: 'Incidents & SLOs', icon: 'ph-siren' },
  { id: 'commands', label: 'Command Runner', icon: 'ph-terminal-window' },
  { id: 'about', label: 'About', icon: 'ph-info' },
]

export default function Sidebar({ panel, onNav, badges, live, context, theme, onToggleTheme, version }) {
  const daemonOn = live?.stats?.daemon_running
  const offline = live?.offline
  const known = live != null
  return (
    <aside style={{
      position: 'sticky', top: 0, height: '100vh', display: 'flex', flexDirection: 'column', gap: 14,
      padding: '16px 12px', background: 'color-mix(in srgb, var(--color-surface) 40%, var(--color-bg))',
      boxShadow: 'inset -1px 0 0 var(--color-divider)',
    }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '2px 6px 8px' }}>
        <div style={{
          width: 28, height: 28, borderRadius: 8, border: '1px solid var(--color-accent)', display: 'grid', placeItems: 'center',
          color: 'var(--color-accent)', boxShadow: '0 0 14px color-mix(in srgb, var(--color-accent) 30%, transparent)',
        }}><Icon name="ph-globe-simple" size={16} /></div>
        <div style={{ display: 'flex', flexDirection: 'column', lineHeight: 1.2 }}>
          <span style={{ fontSize: 15, fontWeight: 500, letterSpacing: '-0.01em' }}>AtlasOS</span>
          <span className="mono muted" style={{ fontSize: 10.5 }}>{version ? `v${version} · local` : 'local'}</span>
        </div>
      </div>

      <nav style={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
        {NAV.map(n => {
          const cur = panel === n.id
          const b = badges[n.id]
          return (
            <button key={n.id} className={cur ? '' : 'navbtn'} onClick={() => onNav(n.id)} aria-current={cur ? 'page' : undefined}
              style={{
                display: 'flex', alignItems: 'center', gap: 10, width: '100%', padding: '7px 10px', border: 0,
                borderRadius: 'var(--radius-md)', cursor: 'pointer', textAlign: 'left', fontSize: 13,
                background: cur ? 'color-mix(in srgb, var(--color-accent) 16%, transparent)' : 'transparent',
                color: cur ? 'var(--color-text)' : 'color-mix(in srgb, var(--color-text) 80%, transparent)',
              }}>
              <Icon name={n.icon} size={16} style={{ color: cur ? 'var(--color-accent)' : 'var(--muted)' }} />
              <span style={{ flex: 1 }}>{n.label}</span>
              {b ? (
                <span className="mono" style={{
                  minWidth: 18, height: 18, padding: '0 5px', borderRadius: 9, display: 'inline-grid', placeItems: 'center', fontSize: 10.5,
                  color: b.t === 'crit' ? 'var(--st-crit)' : 'var(--st-warn)',
                  background: `color-mix(in srgb, ${b.t === 'crit' ? 'var(--st-crit)' : 'var(--st-warn)'} 14%, transparent)`,
                }}>{b.n}</span>
              ) : null}
            </button>
          )
        })}
      </nav>

      <div className="surface" style={{ marginTop: 'auto', display: 'flex', flexDirection: 'column', gap: 10, padding: 10 }}>
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
