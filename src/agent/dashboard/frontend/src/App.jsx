import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { getJSON, postJSON, runCommand, usePoll } from './lib/api'
import { DemoCtx, demoLive, useData } from './lib/demo'
import About from './ui/About'
import Approvals from './ui/Approvals'
import ChatWidget from './ui/ChatWidget'
import Cluster from './ui/Cluster'
import Commands from './ui/Commands'
import { Icon } from './ui/common'
import Incidents from './ui/Incidents'
import Jenkins from './ui/Jenkins'
import Aws from './ui/Aws'
import GitHub from './ui/GitHub'
import Inbox from './ui/Inbox'
import Login from './ui/Login'
import LogViewer from './ui/LogViewer'
import Activity from './ui/Activity'
import Automation from './ui/Automation'
import Databases from './ui/Databases'
import Deploys from './ui/Deploys'
import Domains from './ui/Domains'
import ErrorBoundary from './ui/ErrorBoundary'
import Settings from './ui/Settings'
import SetupChecklist from './ui/SetupChecklist'
import System from './ui/System'
import Overview, { QUICK } from './ui/Overview'
import Palette from './ui/Palette'
import RunDrawer from './ui/RunDrawer'
import Sidebar, { NAV } from './ui/Sidebar'

const TOPBAR_H = 56

const TITLES = {
  overview: ['Overview', 'Everything AtlasOS watches, at a glance'],
  cluster: ['Cluster', 'Nodes, problem pods, and what the daemon healed'],
  jenkins: ['Jenkins', 'Builds and failures'],
  github: ['GitHub Actions', 'Workflow runs, failures and their logs'],
  aws: ['AWS', 'Spend, and every resource in your region'],
  databases: ['Databases', 'RDS and Aurora health — CPU, storage, connections, latency'],
  domains: ['Domains & HTTPS', 'Is every site live? Turn on HTTPS in one step'],
  deploys: ['Deploys', 'GitHub pushes become risk-checked deploys — set it up here'],
  automation: ['Automation', 'Runbooks, auto-scaling schedules and the daily summary'],
  activity: ['Activity', 'Everything AtlasOS and people did — the audit trail'],
  approvals: ['Approvals', 'Fixes waiting for a human — the same queue as Slack'],
  incidents: ['Incidents & SLOs', 'Open incidents and error budgets'],
  commands: ['Command Runner', 'Every agent CLI command, with its options'],
  system: ['System', "AtlasOS's own services, event queue, backups and logs"],
  settings: ['Settings', 'Connect your tools and protect the dashboard'],
  about: ['About AtlasOS', 'Skills, and everything the assistant can run'],
}

function load(key, fallback) { try { return localStorage.getItem(key) ?? fallback } catch { return fallback } }
function save(key, v) { try { localStorage.setItem(key, v) } catch { /* private window */ } }

/** Live cluster snapshot over /ws/live (reconnects). Demo mode: sample data. */
function useLive(demo) {
  const [live, setLive] = useState(null)
  useEffect(() => {
    if (demo) { setLive(demoLive()); return undefined }
    setLive(null)
    let ws, retry, dead = false
    const connect = () => {
      const proto = location.protocol === 'https:' ? 'wss' : 'ws'
      ws = new WebSocket(`${proto}://${location.host}/ws/live`)
      ws.onmessage = e => { try { setLive(JSON.parse(e.data)) } catch { /* ignore */ } }
      ws.onclose = () => { if (!dead) retry = setTimeout(connect, 3000) }
    }
    connect()
    return () => { dead = true; ws?.close(); clearTimeout(retry) }
  }, [demo])
  return live
}

function useWidth() {
  const [w, setW] = useState(() => window.innerWidth)
  useEffect(() => {
    const on = () => setW(window.innerWidth)
    window.addEventListener('resize', on)
    return () => window.removeEventListener('resize', on)
  }, [])
  return w
}

/** So something needing attention is visible even when this tab isn't the
 *  focused one: the tab title gets a "(N)" prefix and the favicon gets a
 *  small red badge drawn over it. The base icon is loaded once and cached;
 *  drawing is best-effort — a browser that can't do this just keeps the
 *  plain title, which is still correct, just less visible. */
function useTitleBadge(count) {
  const baseImg = useRef(null)
  const baseTitle = useRef(document.title)
  useEffect(() => {
    document.title = count > 0 ? `(${count > 99 ? '99+' : count}) ${baseTitle.current}` : baseTitle.current
  }, [count])
  useEffect(() => {
    const link = document.getElementById('favicon')
    if (!link) return
    if (count === 0) { link.href = '/favicon.svg'; return }
    const draw = img => {
      try {
        const size = 64
        const canvas = document.createElement('canvas')
        canvas.width = size; canvas.height = size
        const ctx = canvas.getContext('2d')
        ctx.drawImage(img, 0, 0, size, size)
        const r = 15, cx = size - r - 2, cy = size - r - 2
        ctx.beginPath(); ctx.arc(cx, cy, r, 0, Math.PI * 2)
        ctx.fillStyle = '#e5484d'; ctx.fill()
        ctx.lineWidth = 3; ctx.strokeStyle = '#161826'; ctx.stroke()
        ctx.fillStyle = '#fff'; ctx.font = 'bold 24px sans-serif'
        ctx.textAlign = 'center'; ctx.textBaseline = 'middle'
        ctx.fillText(count > 9 ? '9+' : String(count), cx, cy + 1)
        link.href = canvas.toDataURL('image/png')
      } catch { /* canvas unavailable in this context — title badge still works */ }
    }
    if (baseImg.current) { draw(baseImg.current); return }
    const img = new Image()
    img.onload = () => { baseImg.current = img; draw(img) }
    img.onerror = () => {}
    img.src = '/favicon.svg'
  }, [count])
}

const CHAT_W_MIN = 320, CHAT_W_MAX = 640, CHAT_W_DEFAULT = 388

/** Draggable width for the docked assistant panel: drag the handle on its
 *  left edge, double-click to reset, remembered across visits. */
function useResizableWidth(storageKey, initial, min, max) {
  const [width, setWidth] = useState(() => {
    const v = parseInt(load(storageKey, String(initial)), 10)
    return Number.isFinite(v) ? Math.min(max, Math.max(min, v)) : initial
  })
  useEffect(() => { save(storageKey, String(width)) }, [storageKey, width])
  const onMouseDown = useCallback(e => {
    e.preventDefault()
    const startX = e.clientX, startWidth = width
    const handle = e.currentTarget
    handle.classList.add('dragging')
    document.body.style.cursor = 'col-resize'
    document.body.style.userSelect = 'none'
    const onMove = ev => setWidth(Math.min(max, Math.max(min, startWidth + (startX - ev.clientX))))
    const onUp = () => {
      handle.classList.remove('dragging')
      document.body.style.cursor = ''
      document.body.style.userSelect = ''
      window.removeEventListener('mousemove', onMove)
      window.removeEventListener('mouseup', onUp)
    }
    window.addEventListener('mousemove', onMove)
    window.addEventListener('mouseup', onUp)
  }, [width, min, max])
  return [width, onMouseDown, setWidth]
}

/** Re-fetch whenever the header Refresh button bumps `refreshKey`. */
function useRefresh(poll, refreshKey) {
  const first = useRef(true)
  useEffect(() => {
    if (first.current) { first.current = false; return }
    poll.reload()
  }, [refreshKey]) // eslint-disable-line react-hooks/exhaustive-deps
}

function ClusterPanel({ refreshKey, onAsk, onConnected, onLogs }) {
  const cluster = useData('cluster', '/api/cluster', 30000)
  useRefresh(cluster, refreshKey)
  return <Cluster cluster={cluster} onAsk={onAsk} onConnected={onConnected} onLogs={onLogs} />
}
function JenkinsPanel({ refreshKey, onAsk, onLogs }) {
  const builds = useData('jenkins', '/api/jenkins/builds', 60000)
  useRefresh(builds, refreshKey)
  return <Jenkins builds={builds} onAsk={onAsk} onLogs={onLogs} />
}
function GitHubPanel({ refreshKey, onAsk, onLogs }) {
  const [repo, setRepo] = useState(() => load('atlas-gh-repo', ''))
  useEffect(() => { save('atlas-gh-repo', repo) }, [repo])
  const runs = useData('github', `/api/github/runs?repo=${encodeURIComponent(repo)}&limit=40`, 60000)
  useRefresh(runs, refreshKey)
  return <GitHub runs={runs} repo={repo} setRepo={setRepo} onAsk={onAsk} onLogs={onLogs} />
}
function AwsPanel({ refreshKey, onAsk }) {
  const aws = useData('aws', '/api/aws/overview')
  useRefresh(aws, refreshKey)
  return <Aws aws={aws} onAsk={onAsk} onRefresh={() => aws.reload('/api/aws/overview?force=true')} />
}
function IncidentsPanel({ refreshKey, onAsk }) {
  const incidents = useData('incidents', '/api/incidents', 30000)
  const slos = useData('slos', '/api/slos', 60000)
  useRefresh(incidents, refreshKey); useRefresh(slos, refreshKey)
  return <Incidents incidents={incidents} slos={slos} onAsk={onAsk} onChanged={() => { incidents.reload(); slos.reload() }} />
}
function OverviewPanel({ refreshKey, summary, actions, chart, spend, live, incidents, approvals, deploys, onNav, onRun, onAsk, onOpenChat, daemonRunning, demo }) {
  const clusterMini = useData('cluster', '/api/cluster', 30000, demo)
  const notifications = useData('notifications', '/api/notifications?limit=12', 20000, demo)
  useRefresh(clusterMini, refreshKey); useRefresh(notifications, refreshKey)
  return (
    <Overview summary={summary} actions={actions} chart={chart} spend={spend} live={live} incidents={incidents}
      approvals={approvals} deploys={deploys} cluster={clusterMini} notifications={notifications}
      onNav={onNav} onRun={onRun} onAsk={onAsk} onOpenChat={onOpenChat} daemonRunning={daemonRunning} />
  )
}
function AboutPanel({ refreshKey, about, chatInfo }) {
  useRefresh(about, refreshKey)
  return <About about={about} chatInfo={chatInfo} />
}

function ContextSwitcher({ clusters, onSwitched, demo }) {
  const [open, setOpen] = useState(false)
  const [err, setErr] = useState(null)
  const list = clusters.data?.clusters || []
  const cur = clusters.data?.current
  const pick = async name => {
    setOpen(false); setErr(null)
    if (demo) return
    try { await postJSON('/api/clusters/switch', { name }); onSwitched() } catch (e) { setErr(e.message) }
  }
  return (
    <div style={{ position: 'relative' }}>
      <button className="hoverable" onClick={() => setOpen(o => !o)} style={{ display: 'flex', alignItems: 'center', gap: 8, minHeight: 34, maxWidth: 320, padding: '6px 10px', borderRadius: 'var(--radius-md)', border: '1px solid var(--color-divider)', background: 'var(--color-surface)', fontSize: 12.5 }}>
        <Icon name="ph-cube" style={{ color: 'var(--color-accent)' }} />
        <span className="mono" style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{cur || 'no context'}</span>
        <Icon name="ph-caret-up-down" style={{ color: 'var(--muted)' }} />
      </button>
      {err && <div style={{ position: 'absolute', right: 0, top: 'calc(100% + 4px)', fontSize: 11.5, color: 'var(--st-crit)' }}>{err}</div>}
      {open && (
        <div style={{ position: 'absolute', right: 0, top: 'calc(100% + 6px)', width: 300, zIndex: 30, padding: 4, borderRadius: 'var(--radius-md)', background: 'var(--color-surface)', boxShadow: 'var(--shadow-md)' }}>
          <div className="kicker" style={{ padding: '6px 8px 2px', fontSize: 10.5 }}>kube contexts</div>
          <div className="muted" style={{ padding: '0 8px 6px', fontSize: 11 }}>
            {demo ? 'Switching is disabled in demo mode.' : "Switching changes kubectl's current context for everything on this machine, including the daemon."}
          </div>
          {list.length === 0 && <div className="muted" style={{ padding: 8, fontSize: 12 }}>No contexts in your kubeconfig.</div>}
          {list.map(c => (
            <button key={c.name} className="rowbtn" onClick={() => pick(c.name)} disabled={demo} style={{ display: 'flex', alignItems: 'center', gap: 9, width: '100%', padding: '7px 8px', border: 0, borderRadius: 6, background: 'transparent', cursor: demo ? 'default' : 'pointer', textAlign: 'left' }}>
              <Icon name={c.health === 'healthy' ? 'ph-check-circle' : c.health === 'unreachable' ? 'ph-plugs' : 'ph-question'}
                style={{ color: c.health === 'healthy' ? 'var(--st-ok)' : 'var(--st-unk)' }} />
              <div style={{ flex: 1, minWidth: 0 }}>
                <div className="mono" style={{ fontSize: 12, overflow: 'hidden', textOverflow: 'ellipsis' }}>{c.name}</div>
                <div className="muted" style={{ fontSize: 11 }}>{c.health === 'healthy' ? `${c.node_count} nodes` : c.health === 'unreachable' ? 'unreachable' : 'not checked'}</div>
              </div>
              {c.name === cur && <Icon name="ph-check" style={{ color: 'var(--color-accent)' }} />}
            </button>
          ))}
        </div>
      )}
    </div>
  )
}

function DemoSwitch({ demo, onToggle }) {
  return (
    <button role="switch" aria-checked={demo} onClick={onToggle} title="Show realistic sample data instead of your live systems"
      style={{ display: 'inline-flex', alignItems: 'center', gap: 8, padding: '5px 10px', borderRadius: 'var(--radius-md)', border: `1px solid ${demo ? 'var(--st-warn)' : 'var(--color-divider)'}`, background: demo ? 'color-mix(in srgb, var(--st-warn) 12%, transparent)' : 'transparent', cursor: 'pointer', fontSize: 12.5, color: demo ? 'var(--st-warn)' : 'var(--color-text)' }}>
      <span style={{ position: 'relative', width: 26, height: 14, borderRadius: 7, background: demo ? 'var(--st-warn)' : 'color-mix(in srgb, var(--color-text) 18%, transparent)' }}>
        <span style={{ position: 'absolute', top: 2, left: demo ? 14 : 2, width: 10, height: 10, borderRadius: '50%', background: 'var(--color-surface)', transition: 'left .15s' }} />
      </span>
      Demo data
    </button>
  )
}

/** Full-width bar above the sidebar/main/chat grid: brand, global search
 *  (opens the Ctrl+K palette — same live /api/search it always used), the
 *  cluster context, notifications, and a settings shortcut in place of a
 *  fabricated user identity (the dashboard has one shared login, not
 *  per-user accounts). */
function TopBar({ mobile, onOpenNav, onOpenPalette, version, clusters, demo, onClustersChanged, onNav }) {
  return (
    <header style={{
      position: 'sticky', top: 0, zIndex: 50, height: 'var(--topbar-h)', display: 'flex', alignItems: 'center', gap: 14,
      padding: '0 16px', background: 'var(--color-bg-2)', boxShadow: '0 1px 0 var(--color-divider)',
    }}>
      {mobile && (
        <button className="btn btn-secondary btn-icon" onClick={onOpenNav} aria-label="Open menu"><Icon name="ph-list" size={18} /></button>
      )}
      <div style={{ display: 'flex', alignItems: 'center', gap: 9, flex: mobile ? '0 0 auto' : '0 0 190px' }}>
        <div style={{
          width: 30, height: 30, borderRadius: 9, border: '1px solid var(--color-accent)', display: 'grid', placeItems: 'center',
          color: 'var(--color-accent)', boxShadow: '0 0 14px color-mix(in srgb, var(--color-accent) 30%, transparent)', flex: 'none',
        }}><Icon name="ph-globe-simple" size={16} /></div>
        {!mobile && (
          <div style={{ display: 'flex', flexDirection: 'column', lineHeight: 1.15, minWidth: 0 }}>
            <span style={{ fontSize: 14.5, fontWeight: 500, letterSpacing: '-0.01em' }}>AtlasOS</span>
            <span className="mono muted" style={{ fontSize: 10 }}>{version ? `v${version} · local` : 'local'}</span>
          </div>
        )}
      </div>

      <button onClick={onOpenPalette} style={{
        flex: '1 1 auto', display: 'flex', alignItems: 'center', gap: 9, minHeight: 36, maxWidth: 640, margin: '0 auto', padding: '0 12px',
        borderRadius: 'var(--radius-md)', border: '1px solid var(--color-divider)', background: 'var(--color-surface)', cursor: 'pointer',
        color: 'var(--muted)', fontSize: 13, textAlign: 'left',
      }}>
        <Icon name="ph-magnifying-glass" size={15} />
        {!mobile && <span style={{ flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>Search incidents, pods, builds, AWS, domains…</span>}
        {!mobile && <kbd className="mono" style={{ fontSize: 10.5, padding: '1px 6px', borderRadius: 4, border: '1px solid var(--color-divider)', flex: 'none' }}>Ctrl K</kbd>}
      </button>

      <div style={{ display: 'flex', alignItems: 'center', gap: 10, flex: '0 0 auto' }}>
        {!mobile && <ContextSwitcher clusters={clusters} demo={demo} onSwitched={onClustersChanged} />}
        <Inbox onNav={onNav} demo={demo} />
        <button className="btn btn-ghost btn-icon" onClick={() => onNav('settings')} aria-label="Settings" title="Settings"
          style={{ width: 32, height: 32, borderRadius: '50%', border: '1px solid var(--color-divider)', color: 'var(--muted)' }}>
          <Icon name="ph-user-circle" size={19} />
        </button>
      </div>
    </header>
  )
}

function Shell() {
  const [panel, setPanel] = useState(() => {
    const h = location.hash.slice(1)
    return NAV.some(n => n.id === h) ? h : 'overview'
  })
  const [demo, setDemo] = useState(() => load('atlas-demo', '0') === '1')
  const [theme, setTheme] = useState(() => load('atlas-theme', 'dark'))
  const [chatOpen, setChatOpen] = useState(() => load('atlas-chat-open', '1') === '1')
  const [askRequest, setAskRequest] = useState(null)
  const [run, setRun] = useState(null)
  const [refreshKey, setRefreshKey] = useState(0)
  const [paletteOpen, setPaletteOpen] = useState(false)
  const [navOpen, setNavOpen] = useState(false)
  const [cmdSel, setCmdSel] = useState(null)
  const [toast, setToast] = useState(null)
  const [logSource, setLogSource] = useState(null)
  const lastQuick = useRef(null)
  const width = useWidth()
  const mobile = width < 900
  const dock = width >= 1340 && !mobile   // wide enough to dock the assistant as a third column
  const [chatWidth, onChatResizeStart, setChatWidth] = useResizableWidth('atlas-chat-width', CHAT_W_DEFAULT, CHAT_W_MIN, CHAT_W_MAX)

  const live = useLive(demo)
  // Shell renders the DemoCtx provider, so its own hooks get `demo` passed in.
  const summary = useData('summary', '/api/summary', 300000, demo)
  const actions = useData('actions', '/api/actions', 30000, demo)
  const approvals = useData('approvals', '/api/approvals', 30000, demo)
  const deploys = useData('deploys', '/api/pending-deploys', 30000, demo)
  const clusters = useData('clusters', '/api/clusters', 0, demo)
  const chart = useData('chart', '/api/chart', 300000, demo)
  const spend = useData('spend', '/api/spend', 3600000, demo)
  const incidents = useData('incidents', '/api/incidents', 30000, demo)
  const commands = usePoll('/api/commands')
  const chatInfo = usePoll('/api/chat/info')
  const about = usePoll('/api/about')

  useEffect(() => { document.documentElement.dataset.theme = theme; save('atlas-theme', theme) }, [theme])
  useEffect(() => { save('atlas-demo', demo ? '1' : '0') }, [demo])
  useEffect(() => { save('atlas-chat-open', chatOpen ? '1' : '0') }, [chatOpen])
  useEffect(() => { history.replaceState(null, '', `#${panel}`); setNavOpen(false) }, [panel])
  useEffect(() => {
    const onKey = e => { if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') { e.preventDefault(); setPaletteOpen(o => !o) } }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])
  useEffect(() => { if (!toast) return undefined; const t = setTimeout(() => setToast(null), 3500); return () => clearTimeout(t) }, [toast])

  const ask = useCallback(text => { setChatOpen(true); setAskRequest({ text, at: Date.now() }) }, [])

  const refresh = () => {
    setRefreshKey(k => k + 1)
    if (demo) return
    if (panel === 'overview') { summary.reload('/api/summary?force=true'); actions.reload(); chart.reload(); spend.reload() }
    if (panel === 'approvals') { approvals.reload(); deploys.reload() }
    if (panel === 'overview') { incidents.reload(); approvals.reload(); deploys.reload() }
    clusters.reload()
  }

  const runQuick = useCallback(async q => {
    if (demo) { setToast('Actions are disabled in demo mode — switch Demo data off to run it for real.'); return }
    lastQuick.current = q
    if (q.id === 'daemon') {
      const stopping = !!live?.stats?.daemon_running
      const cmd = `agent daemon ${stopping ? 'stop' : 'start'}`
      setRun({ name: stopping ? 'Stop Daemon' : 'Start Daemon', cmd, icon: 'ph-heartbeat', lines: [`$ ${cmd}`], status: 'running' })
      try {
        const r = await postJSON(`/api/daemon/${stopping ? 'stop' : 'start'}`)
        const ok = !r.error && r.status !== 'error'
        const msg = { started: `✓ daemon started · pid ${r.pid}`, already_running: `! already running · pid ${r.pid}`,
          stopped: `✓ daemon stopped · pid ${r.pid} · auto-healing paused`, not_running: '! daemon was not running' }[r.status] || `✗ ${r.error || r.status}`
        setRun(x => ({ ...x, lines: [...x.lines, msg], status: ok ? 'ok' : 'failed' }))
      } catch (e) {
        setRun(x => ({ ...x, lines: [...x.lines, `✗ ${e.message}`], status: 'failed' }))
      }
      return
    }
    const cmd = `agent ${q.cmd}`
    setRun({ name: q.name, cmd, icon: q.icon, lines: [`$ ${cmd}`], status: 'running' })
    try {
      const code = await runCommand(q.cmd, {}, !!q.confirmed, ln => setRun(x => (x ? { ...x, lines: [...x.lines, ln] } : x)))
      setRun(x => (x ? { ...x, status: code === 0 ? 'ok' : 'failed' } : x))
    } catch (e) {
      setRun(x => (x ? { ...x, lines: [...x.lines, `✗ ${e.message}`], status: 'failed' } : x))
    }
    actions.reload()
  }, [demo, live, actions])

  const askAboutRun = r => {
    const out = r.lines.slice(1).join('\n')
    const clipped = out.length > 3500 ? '…' + out.slice(-3500) : out
    setRun(null)
    ask(`I ran \`${r.cmd}\` from the dashboard. Output:\n${clipped}\n\nWhat does this mean, and is there anything I should do?`)
  }

  const context = clusters.data?.current || ''
  const pendingCount = (approvals.data?.pending.length || 0) + (deploys.data?.length || 0)
  const badges = {
    approvals: pendingCount ? { n: pendingCount, t: 'warn' } : null,
    incidents: live?.stats?.incidents_open ? { n: live.stats.incidents_open, t: 'crit' } : null,
  }
  useTitleBadge(demo ? 0 : (badges.approvals?.n || 0) + (badges.incidents?.n || 0))
  const [title, sub] = TITLES[panel]
  const showBanner = !demo && live?.offline && (panel === 'overview' || panel === 'cluster')
  const allCommands = useMemo(() => Object.values(commands.data || {}).flat().filter(c => !c.is_eval), [commands.data])
  const quickItems = useMemo(() => [...QUICK, { id: 'daemon', name: live?.stats?.daemon_running ? 'Stop Daemon' : 'Start Daemon', icon: 'ph-heartbeat' }], [live])
  const toggles = useMemo(() => [
    { label: `Switch to ${theme === 'dark' ? 'light' : 'dark'} theme`, icon: theme === 'dark' ? 'ph-sun' : 'ph-moon', run: () => setTheme(t => (t === 'dark' ? 'light' : 'dark')) },
    { label: demo ? 'Turn off demo data' : 'Turn on demo data', icon: 'ph-presentation-chart', run: () => setDemo(d => !d) },
    { label: 'Open the assistant', icon: 'ph-chat-circle-dots', run: () => setChatOpen(true) },
  ], [theme, demo])

  const sidebar = (
    <ErrorBoundary label="sidebar">
      <Sidebar panel={panel} onNav={setPanel} badges={badges} live={live} context={context}
        theme={theme} onToggleTheme={() => setTheme(t => (t === 'dark' ? 'light' : 'dark'))} />
    </ErrorBoundary>
  )
  const showChatPanel = dock && chatOpen

  return (
    <DemoCtx.Provider value={demo}>
      <div style={{ '--topbar-h': `${TOPBAR_H}px`, minHeight: '100vh', display: 'flex', flexDirection: 'column' }}>
        <TopBar mobile={mobile} onOpenNav={() => setNavOpen(true)} onOpenPalette={() => setPaletteOpen(true)}
          version={about.data?.version} clusters={clusters} demo={demo} onNav={setPanel}
          onClustersChanged={() => { clusters.reload(); setRefreshKey(k => k + 1) }} />

        <div style={{ display: 'grid', gridTemplateColumns: mobile ? 'minmax(0, 1fr)' : showChatPanel ? `216px minmax(0, 1fr) ${chatWidth}px` : '216px minmax(0, 1fr)', flex: 1, minHeight: 0 }}>
          {!mobile && sidebar}
          {mobile && navOpen && (
            <>
              <div onClick={() => setNavOpen(false)} style={{ position: 'fixed', inset: 0, zIndex: 54, background: 'color-mix(in srgb, var(--color-bg) 60%, transparent)' }} />
              <div style={{ position: 'fixed', top: 0, left: 0, bottom: 0, width: 240, zIndex: 55, boxShadow: 'var(--shadow-lg)' }}>{sidebar}</div>
            </>
          )}

          <main style={{ minWidth: 0, padding: mobile ? '12px 16px 96px' : '18px 28px 96px', display: 'flex', flexDirection: 'column', gap: 18 }}>
            <header style={{ display: 'flex', alignItems: 'flex-end', gap: '12px 12px', flexWrap: 'wrap' }}>
              <div style={{ minWidth: 0, flex: '1 1 220px' }}>
                <h1 style={{ fontSize: 22, marginBottom: 3 }}>{title}</h1>
                <div className="muted" style={{ fontSize: 12.5, overflow: 'hidden', textOverflow: 'ellipsis' }}>{panel === 'cluster' ? context || sub : sub}</div>
              </div>
              {panel === 'cluster' && <ContextSwitcher clusters={clusters} demo={demo} onSwitched={() => { clusters.reload(); setRefreshKey(k => k + 1) }} />}
              <DemoSwitch demo={demo} onToggle={() => setDemo(d => !d)} />
              <button className="btn btn-secondary" onClick={refresh} style={{ padding: '5px 10px', fontSize: 12.5 }}><Icon name="ph-arrow-clockwise" />{mobile ? '' : 'Refresh'}</button>
              {dock && !chatOpen && (
                <button className="btn btn-primary" onClick={() => setChatOpen(true)} style={{ padding: '5px 10px', fontSize: 12.5 }}>
                  <Icon name="ph-sparkle" />Assistant
                </button>
              )}
            </header>

          {demo && (
            <div role="status" style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '9px 14px', borderRadius: 'var(--radius-md)', border: '1px solid color-mix(in srgb, var(--st-warn) 45%, transparent)', background: 'color-mix(in srgb, var(--st-warn) 10%, transparent)', fontSize: 12.5 }}>
              <Icon name="ph-presentation-chart" size={17} style={{ color: 'var(--st-warn)' }} />
              <span><strong style={{ fontWeight: 600 }}>Demo data.</strong> Everything below is sample data, not your systems. Actions are disabled. The assistant still talks to your live systems.</span>
              <button className="btn btn-ghost" onClick={() => setDemo(false)} style={{ marginLeft: 'auto', fontSize: 12 }}>Show live data</button>
            </div>
          )}

          {showBanner && (
            <div role="alert" style={{ display: 'flex', gap: 14, alignItems: 'flex-start', flexWrap: 'wrap', padding: '14px 16px', borderRadius: 'var(--radius-md)', border: '1px dashed var(--st-unk)', background: 'var(--hatch), color-mix(in srgb, var(--st-unk) 8%, var(--color-surface))' }}>
              <div style={{ width: 32, height: 32, borderRadius: '50%', display: 'grid', placeItems: 'center', border: '1.5px solid var(--st-unk)', color: 'var(--st-unk)', flex: 'none' }}><Icon name="ph-plugs" size={17} /></div>
              <div style={{ flex: '1 1 320px', minWidth: 0, display: 'flex', flexDirection: 'column', gap: 4 }}>
                <div style={{ fontSize: 14.5, fontWeight: 500 }}>Cluster unreachable — showing no data, not "no problems"</div>
                <div className="muted" style={{ fontSize: 12.5 }}>
                  The Kubernetes API server for <span className="mono">{context || 'the current context'}</span> didn't answer.
                  Cluster numbers are unknown until it does, and the daemon can't heal anything meanwhile.
                  {panel === 'overview' && <> Connect another cluster from the <a href="#cluster" onClick={e => { e.preventDefault(); setPanel('cluster') }}>Cluster</a> page.</>}
                </div>
              </div>
              <button className="btn btn-secondary" onClick={refresh}><Icon name="ph-arrow-clockwise" />Retry</button>
            </div>
          )}

          <ErrorBoundary key={panel} label="page" onReset={() => setPanel('overview')}>
          {panel === 'overview' && <SetupChecklist live={live} context={context} onNav={setPanel} />}
          {panel === 'overview' && (
            <OverviewPanel refreshKey={refreshKey} summary={summary} actions={actions} chart={chart} spend={spend} live={live}
              incidents={incidents} approvals={approvals} deploys={deploys} onNav={setPanel} onRun={runQuick} onAsk={ask}
              onOpenChat={() => setChatOpen(true)} daemonRunning={!!live?.stats?.daemon_running} demo={demo} />
          )}
          {panel === 'cluster' && <ClusterPanel refreshKey={refreshKey} onAsk={ask} onConnected={() => clusters.reload()} onLogs={setLogSource} />}
          {panel === 'jenkins' && <JenkinsPanel refreshKey={refreshKey} onAsk={ask} onLogs={setLogSource} />}
          {panel === 'github' && <GitHubPanel refreshKey={refreshKey} onAsk={ask} onLogs={setLogSource} />}
          {panel === 'aws' && <AwsPanel refreshKey={refreshKey} onAsk={ask} />}
          {panel === 'databases' && <Databases key={refreshKey} onAsk={ask} />}
          {panel === 'domains' && <Domains key={refreshKey} onAsk={ask} />}
          {panel === 'deploys' && <Deploys key={refreshKey} onNav={setPanel} />}
          {panel === 'automation' && <Automation key={refreshKey} summary={summary} onLogs={setLogSource} />}
          {panel === 'activity' && <Activity key={refreshKey} />}
          {panel === 'settings' && <Settings key={refreshKey} live={live} context={context} onNav={setPanel} />}
          {panel === 'system' && <System key={refreshKey} onLogs={setLogSource} onDaemon={() => runQuick({ id: 'daemon' })} />}
          {panel === 'approvals' && <Approvals approvals={approvals} deploys={deploys} onChanged={() => { approvals.reload(); deploys.reload() }} />}
          {panel === 'incidents' && <IncidentsPanel refreshKey={refreshKey} onAsk={ask} />}
          {panel === 'commands' && <Commands key={cmdSel || 'none'} commands={commands} context={context} initial={cmdSel} />}
          {panel === 'about' && <AboutPanel refreshKey={refreshKey} about={about} chatInfo={chatInfo} />}
          </ErrorBoundary>
          </main>

          {showChatPanel && (
            <div style={{ position: 'sticky', top: 'var(--topbar-h)', height: 'calc(100vh - var(--topbar-h))', minWidth: 0 }}>
              <div className="chat-resize-handle" role="separator" aria-orientation="vertical" aria-label="Resize assistant panel"
                aria-valuenow={chatWidth} aria-valuemin={CHAT_W_MIN} aria-valuemax={CHAT_W_MAX} tabIndex={0}
                onMouseDown={onChatResizeStart} onDoubleClick={() => setChatWidth(CHAT_W_DEFAULT)}
                onKeyDown={e => {
                  if (e.key === 'ArrowLeft') setChatWidth(w => Math.min(CHAT_W_MAX, w + 24))
                  if (e.key === 'ArrowRight') setChatWidth(w => Math.max(CHAT_W_MIN, w - 24))
                }}
                style={{ position: 'absolute', left: -4, top: 0, bottom: 0, width: 8, cursor: 'col-resize', zIndex: 20 }}
                title="Drag to resize · double-click to reset" />
              <ErrorBoundary label="assistant" inline>
                <ChatWidget variant="panel" open setOpen={setChatOpen} askRequest={askRequest} context={context} info={chatInfo}
                  incidents={incidents} onNav={setPanel} />
              </ErrorBoundary>
            </div>
          )}
        </div>

        <LogViewer source={logSource} onClose={() => setLogSource(null)} onAsk={ask} />
        <RunDrawer run={run}onClose={() => setRun(null)} onRerun={lastQuick.current ? () => runQuick(lastQuick.current) : null} onAsk={askAboutRun} />
        {!showChatPanel && (
          <ErrorBoundary label="assistant" inline>
            <ChatWidget variant="overlay" open={chatOpen} setOpen={setChatOpen} askRequest={askRequest} context={context} info={chatInfo}
              incidents={incidents} onNav={setPanel} />
          </ErrorBoundary>
        )}
        <Palette open={paletteOpen} onClose={() => setPaletteOpen(false)} nav={NAV} quick={quickItems} commands={allCommands}
          onNav={setPanel} onRun={runQuick} onPickCommand={c => { setCmdSel(c); setPanel('commands') }} onAsk={ask} toggles={toggles} />
        {toast && (
          <div role="status" style={{ position: 'fixed', left: '50%', bottom: 24, transform: 'translateX(-50%)', zIndex: 70, maxWidth: 'calc(100vw - 32px)', padding: '10px 14px', borderRadius: 'var(--radius-md)', background: 'var(--color-surface)', boxShadow: 'var(--shadow-lg)', fontSize: 12.5 }}>
            {toast}
          </div>
        )}
      </div>
    </DemoCtx.Provider>
  )
}

/** Gate on the optional dashboard login (DASHBOARD_TOKEN). */
export default function App() {
  const [auth, setAuth] = useState(null)   // null = checking
  const check = useCallback(() => {
    getJSON('/api/auth/status').then(setAuth).catch(() => setAuth({ required: false, ok: true }))
  }, [])
  useEffect(() => {
    check()
    const on = () => setAuth(a => (a ? { ...a, ok: false, required: true } : a))
    window.addEventListener('atlas-auth-required', on)
    return () => window.removeEventListener('atlas-auth-required', on)
  }, [check])
  if (!auth) return null
  if (auth.required && !auth.ok) return <Login onDone={check} />
  return <Shell />
}
