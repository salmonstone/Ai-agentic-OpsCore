import { useCallback, useEffect, useRef, useState } from 'react'
import { postJSON, runCommand, usePoll } from './lib/api'
import About from './ui/About'
import Approvals from './ui/Approvals'
import ChatWidget from './ui/ChatWidget'
import Cluster from './ui/Cluster'
import Commands from './ui/Commands'
import { Icon } from './ui/common'
import Incidents from './ui/Incidents'
import Jenkins from './ui/Jenkins'
import Overview from './ui/Overview'
import RunDrawer from './ui/RunDrawer'
import Sidebar, { NAV } from './ui/Sidebar'

const TITLES = {
  overview: ['Overview', 'Everything AtlasOS watches, at a glance'],
  cluster: ['Cluster', 'Nodes, problem pods, and what the daemon healed'],
  jenkins: ['Jenkins', 'Builds and failures'],
  approvals: ['Approvals', 'Fixes waiting for a human — the same queue as Slack'],
  incidents: ['Incidents & SLOs', 'Open incidents and error budgets'],
  commands: ['Command Runner', 'Every agent CLI command, with its options'],
  about: ['About AtlasOS', 'Skills, and everything the assistant can run'],
}

function load(key, fallback) { try { return localStorage.getItem(key) || fallback } catch { return fallback } }
function save(key, v) { try { localStorage.setItem(key, v) } catch { /* private window */ } }

/** Live cluster snapshot over /ws/live (reconnects). */
function useLive() {
  const [live, setLive] = useState(null)
  useEffect(() => {
    let ws, retry, dead = false
    const connect = () => {
      const proto = location.protocol === 'https:' ? 'wss' : 'ws'
      ws = new WebSocket(`${proto}://${location.host}/ws/live`)
      ws.onmessage = e => { try { setLive(JSON.parse(e.data)) } catch { /* ignore */ } }
      ws.onclose = () => { if (!dead) retry = setTimeout(connect, 3000) }
    }
    connect()
    return () => { dead = true; ws?.close(); clearTimeout(retry) }
  }, [])
  return live
}

/** Re-fetch `poll` whenever the header Refresh button bumps `refreshKey`. */
function useRefresh(poll, refreshKey, url) {
  const first = useRef(true)
  useEffect(() => {
    if (first.current) { first.current = false; return }
    poll.reload(url)
  }, [refreshKey]) // eslint-disable-line react-hooks/exhaustive-deps
}

function ClusterPanel({ refreshKey, onAsk }) {
  const cluster = usePoll('/api/cluster', 30000)
  useRefresh(cluster, refreshKey)
  return <Cluster cluster={cluster} onAsk={onAsk} />
}
function JenkinsPanel({ refreshKey, onAsk }) {
  const builds = usePoll('/api/jenkins/builds', 60000)
  useRefresh(builds, refreshKey)
  return <Jenkins builds={builds} onAsk={onAsk} />
}
function IncidentsPanel({ refreshKey, onAsk }) {
  const incidents = usePoll('/api/incidents', 30000)
  const slos = usePoll('/api/slos', 60000)
  useRefresh(incidents, refreshKey); useRefresh(slos, refreshKey)
  return <Incidents incidents={incidents} slos={slos} onAsk={onAsk} />
}
function CommandsPanel({ refreshKey, context }) {
  const commands = usePoll('/api/commands')
  useRefresh(commands, refreshKey)
  return <Commands commands={commands} context={context} />
}
function AboutPanel({ refreshKey, chatInfo }) {
  const about = usePoll('/api/about')
  useRefresh(about, refreshKey)
  return <About about={about} chatInfo={chatInfo} />
}

function ContextSwitcher({ clusters, onSwitched }) {
  const [open, setOpen] = useState(false)
  const [err, setErr] = useState(null)
  const list = clusters.data?.clusters || []
  const cur = clusters.data?.current
  const pick = async name => {
    setOpen(false); setErr(null)
    try { await postJSON('/api/clusters/switch', { name }); onSwitched() } catch (e) { setErr(e.message) }
  }
  return (
    <div style={{ position: 'relative' }}>
      <button className="hoverable" onClick={() => setOpen(o => !o)} style={{ display: 'flex', alignItems: 'center', gap: 8, minHeight: 34, padding: '6px 10px', borderRadius: 'var(--radius-md)', border: '1px solid var(--color-divider)', background: 'var(--color-surface)', fontSize: 12.5 }}>
        <Icon name="ph-cube" style={{ color: 'var(--color-accent)' }} /><span className="mono">{cur || 'no context'}</span><Icon name="ph-caret-up-down" style={{ color: 'var(--muted)' }} />
      </button>
      {err && <div style={{ position: 'absolute', right: 0, top: 'calc(100% + 4px)', fontSize: 11.5, color: 'var(--st-crit)' }}>{err}</div>}
      {open && (
        <div style={{ position: 'absolute', right: 0, top: 'calc(100% + 6px)', width: 290, zIndex: 30, padding: 4, borderRadius: 'var(--radius-md)', background: 'var(--color-surface)', boxShadow: 'var(--shadow-md)' }}>
          <div className="kicker" style={{ padding: '6px 8px 2px', fontSize: 10.5 }}>kube contexts</div>
          <div className="muted" style={{ padding: '0 8px 6px', fontSize: 11 }}>Switching changes kubectl's current context for everything on this machine, including the daemon.</div>
          {list.length === 0 && <div className="muted" style={{ padding: 8, fontSize: 12 }}>No contexts in your kubeconfig.</div>}
          {list.map(c => (
            <button key={c.name} className="rowbtn" onClick={() => pick(c.name)} style={{ display: 'flex', alignItems: 'center', gap: 9, width: '100%', padding: '7px 8px', border: 0, borderRadius: 6, background: 'transparent', cursor: 'pointer', textAlign: 'left' }}>
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

export default function App() {
  const [panel, setPanel] = useState(() => {
    const h = location.hash.slice(1)
    return NAV.some(n => n.id === h) ? h : 'overview'
  })
  const [theme, setTheme] = useState(() => load('atlas-theme', 'dark'))
  const [chatOpen, setChatOpen] = useState(false)
  const [askRequest, setAskRequest] = useState(null)
  const [run, setRun] = useState(null)
  const [refreshKey, setRefreshKey] = useState(0)
  const [wide, setWide] = useState(() => window.innerWidth >= 1180)
  const lastQuick = useRef(null)

  const live = useLive()
  const summary = usePoll('/api/summary', 300000)
  const actions = usePoll('/api/actions', 30000)
  const approvals = usePoll('/api/approvals', 30000)
  const deploys = usePoll('/api/pending-deploys', 30000)
  const clusters = usePoll('/api/clusters')
  const chatInfo = usePoll('/api/chat/info')
  const about = usePoll('/api/about')

  useEffect(() => { document.documentElement.dataset.theme = theme; save('atlas-theme', theme) }, [theme])
  useEffect(() => { history.replaceState(null, '', `#${panel}`) }, [panel])
  useEffect(() => {
    const onResize = () => setWide(window.innerWidth >= 1180)
    window.addEventListener('resize', onResize)
    return () => window.removeEventListener('resize', onResize)
  }, [])

  const ask = useCallback(text => { setChatOpen(true); setAskRequest({ text, at: Date.now() }) }, [])

  const refresh = () => {
    setRefreshKey(k => k + 1)
    if (panel === 'overview') { summary.reload('/api/summary?force=true'); actions.reload() }
    if (panel === 'approvals') { approvals.reload(); deploys.reload() }
    clusters.reload()
  }

  const runQuick = useCallback(async q => {
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
  }, [live, actions])

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
  const [title, sub] = TITLES[panel]
  const showBanner = live?.offline && (panel === 'overview' || panel === 'cluster')

  return (
    <div style={{ display: 'grid', gridTemplateColumns: '216px minmax(0, 1fr)', minHeight: '100vh' }}>
      <Sidebar panel={panel} onNav={setPanel} badges={badges} live={live} context={context}
        theme={theme} onToggleTheme={() => setTheme(t => (t === 'dark' ? 'light' : 'dark'))} version={about.data?.version} />

      <main style={{ minWidth: 0, padding: `18px ${chatOpen && wide ? 440 : 28}px 96px 28px`, display: 'flex', flexDirection: 'column', gap: 18, transition: 'padding .2s' }}>
        <header style={{ display: 'flex', alignItems: 'flex-end', gap: '12px 16px', flexWrap: 'wrap' }}>
          <div style={{ minWidth: 0, flex: '1 1 260px' }}>
            <h1 style={{ fontSize: 22, marginBottom: 3 }}>{title}</h1>
            <div className="muted" style={{ fontSize: 12.5 }}>{panel === 'cluster' ? context || sub : sub}</div>
          </div>
          {panel === 'cluster' && <ContextSwitcher clusters={clusters} onSwitched={() => { clusters.reload(); setRefreshKey(k => k + 1) }} />}
          <button className="btn btn-secondary" onClick={refresh} style={{ padding: '5px 10px', fontSize: 12.5 }}><Icon name="ph-arrow-clockwise" />Refresh</button>
        </header>

        {showBanner && (
          <div role="alert" style={{ display: 'flex', gap: 14, alignItems: 'flex-start', flexWrap: 'wrap', padding: '14px 16px', borderRadius: 'var(--radius-md)', border: '1px dashed var(--st-unk)', background: 'var(--hatch), color-mix(in srgb, var(--st-unk) 8%, var(--color-surface))' }}>
            <div style={{ width: 32, height: 32, borderRadius: '50%', display: 'grid', placeItems: 'center', border: '1.5px solid var(--st-unk)', color: 'var(--st-unk)', flex: 'none' }}><Icon name="ph-plugs" size={17} /></div>
            <div style={{ flex: '1 1 320px', minWidth: 0, display: 'flex', flexDirection: 'column', gap: 4 }}>
              <div style={{ fontSize: 14.5, fontWeight: 500 }}>Cluster unreachable — showing no data, not "no problems"</div>
              <div className="muted" style={{ fontSize: 12.5 }}>
                The Kubernetes API server for <span className="mono">{context || 'the current context'}</span> didn't answer.
                Cluster numbers below are unknown until it does. The daemon can't heal anything while this lasts.
              </div>
            </div>
            <button className="btn btn-secondary" onClick={refresh}><Icon name="ph-arrow-clockwise" />Retry</button>
          </div>
        )}

        {panel === 'overview' && <Overview summary={summary} actions={actions} onNav={setPanel} onRun={runQuick} daemonRunning={!!live?.stats?.daemon_running} />}
        {panel === 'cluster' && <ClusterPanel refreshKey={refreshKey} onAsk={ask} />}
        {panel === 'jenkins' && <JenkinsPanel refreshKey={refreshKey} onAsk={ask} />}
        {panel === 'approvals' && <Approvals approvals={approvals} deploys={deploys} onChanged={() => { approvals.reload(); deploys.reload() }} />}
        {panel === 'incidents' && <IncidentsPanel refreshKey={refreshKey} onAsk={ask} />}
        {panel === 'commands' && <CommandsPanel refreshKey={refreshKey} context={context} />}
        {panel === 'about' && <AboutPanel refreshKey={refreshKey} chatInfo={chatInfo} />}
      </main>

      <RunDrawer run={run} onClose={() => setRun(null)} onRerun={lastQuick.current ? () => runQuick(lastQuick.current) : null} onAsk={askAboutRun} />
      <ChatWidget open={chatOpen} setOpen={setChatOpen} askRequest={askRequest} context={context} info={chatInfo} />
    </div>
  )
}
