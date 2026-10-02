import { useState } from 'react'
import { ago, postJSON } from '../lib/api'
import { useDemo } from '../lib/demo'
import { sectionTone, tone } from '../lib/tone'
import { DailyBars, RingGauge, SpendChart } from './charts'
import { Bar, Icon, Msg, OkEmpty, Pill, Section, SectionHead } from './common'
import ImpactStrip from './ImpactStrip'

// Real CLI commands only (every one is in /api/commands). `confirmed` is set
// only for k8s scan: discovery flags it destructive from its help text, but
// the dashboard dispatches it to the read-only K8sSkill.full_cluster_scan.
export const QUICK = [
  { id: 'scan', name: 'Scan Cluster', icon: 'ph-cube-focus', cmd: 'k8s scan', confirmed: true, note: 'pods, deployments, nodes, probes, quotas' },
  { id: 'tls', name: 'Scan TLS', icon: 'ph-lock-key', cmd: 'tls scan', note: 'every Ingress certificate' },
  { id: 'jenkins', name: 'Scan Jenkins', icon: 'ph-hammer', cmd: 'jenkins scan', note: 'failing jobs · uses AI' },
  { id: 'summary', name: 'Daily Summary', icon: 'ph-note', cmd: 'summary show', note: "preview — doesn't post to Slack" },
  { id: 'cost', name: 'AWS Cost', icon: 'ph-currency-dollar', cmd: 'cost aws', note: 'spend + waste · uses AI' },
]

const TILE = {
  'Cluster': { icon: 'ph-cube', go: 'cluster' },
  'Jenkins': { icon: 'ph-hammer', go: 'jenkins' },
  'GitHub Actions': { icon: 'ph-github-logo', go: 'github' },
  'AWS spend': { icon: 'ph-currency-dollar', go: 'aws' },
  'Waiting on you': { icon: 'ph-seal-check', go: 'approvals' },
  'Backups': { icon: 'ph-archive', go: 'system' },
  'AtlasOS': { icon: 'ph-globe-simple', go: 'system' },
}

/** One status card — icon badge, headline, status pill. Clickable through to
 *  the page that owns it. */
function StatusCard({ s, onNav }) {
  const t = tone(sectionTone(s.status))
  const meta = TILE[s.title] || { icon: 'ph-circle' }
  const plain = l => l.replace(/`/g, '').replace(/^\s*❌\s*/, '').replace(/\*/g, '')
  const [value, ...rest] = s.lines.length ? s.lines.map(plain) : ['—']
  return (
    <button onClick={() => onNav(meta.go)} className="hoverable" style={{
      display: 'flex', flexDirection: 'column', gap: 10, textAlign: 'left', padding: 14, minHeight: 128,
      borderRadius: 'var(--radius-lg)', font: 'inherit', color: 'inherit',
      background: t.t === 'unk' ? 'var(--hatch), var(--color-surface)' : 'var(--color-surface)',
      border: `1px ${t.bs} ${t.t === 'ok' ? 'var(--color-divider)' : t.line}`,
    }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
        <div style={{ width: 32, height: 32, borderRadius: 9, display: 'grid', placeItems: 'center', flex: 'none', color: t.c, background: t.tint }}>
          <Icon name={meta.icon} size={16} />
        </div>
        <span className="kicker" style={{ fontSize: 10.5, flex: 1 }}>{s.title}</span>
        <span style={{ display: 'flex', alignItems: 'center', gap: 4, fontSize: 11, fontWeight: 500, color: t.c }}><Icon name={t.icon} size={13} />{t.label}</span>
      </div>
      <div title={value} style={{
        fontSize: 14, fontWeight: 500, lineHeight: 1.35, color: t.t === 'unk' ? 'var(--st-unk)' : 'var(--color-text)',
        display: '-webkit-box', WebkitLineClamp: 2, WebkitBoxOrient: 'vertical', overflow: 'hidden', wordBreak: 'break-word', flex: 1,
      }}>{value}</div>
      <div className="muted" style={{ fontSize: 11, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{rest.slice(0, 1).join(' · ')}</div>
    </button>
  )
}

/** Overall health: the share of status sections that are OK (a "couldn't
 *  check" section counts as 0, same rule as everywhere else in this app —
 *  never derived from a metric we don't actually have). */
function overallHealth(sections) {
  if (!sections?.length) return null
  const score = s => (s.status === 'ok' ? 1 : s.status === 'warn' ? 0.5 : 0)
  return Math.round((sections.reduce((a, s) => a + score(s), 0) / sections.length) * 100)
}

function HealthRing({ sections }) {
  const pct = overallHealth(sections)
  const t = pct == null ? tone('unk') : tone(pct >= 90 ? 'ok' : pct >= 60 ? 'warn' : 'crit')
  return (
    <div className="surface" style={{ display: 'flex', alignItems: 'center', gap: 16, padding: 16, minHeight: 128 }}>
      <div style={{ position: 'relative', width: 92, height: 92, flex: 'none' }}>
        <RingGauge pct={pct} color={t.c} />
        <div style={{ position: 'absolute', inset: 0, display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center' }}>
          <span style={{ fontSize: 22, fontWeight: 600, letterSpacing: '-0.02em' }}>{pct == null ? '—' : `${pct}%`}</span>
        </div>
      </div>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 4, minWidth: 0 }}>
        <span className="kicker" style={{ fontSize: 10.5 }}>Overall health</span>
        <span style={{ fontSize: 14, fontWeight: 500, color: t.c }}>{pct == null ? 'Checking…' : t.label === 'OK' ? 'Healthy' : t.label}</span>
        <span className="muted" style={{ fontSize: 11.5 }}>across {sections?.length ?? 0} watched areas</span>
      </div>
    </div>
  )
}

const SEV_T = { critical: 'crit', high: 'crit', sev1: 'crit', warning: 'warn', medium: 'warn', sev2: 'warn', low: 'neutral', info: 'neutral' }

/** Top open incident, with Acknowledge/Resolve/Diagnose — the same actions
 *  as the Incidents page, surfaced where you'll see them first. */
function IncidentsCard({ incidents, onNav, onAsk }) {
  const demo = useDemo()
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState(null)
  const open = (incidents.data || []).filter(i => i.status === 'open')
  const ack = async i => {
    setBusy(true); setMsg(null)
    try { await postJSON(`/api/incidents/${i.id}/ack`); setMsg({ ok: true, text: 'Acknowledged.' }); incidents.reload() }
    catch (e) { setMsg({ ok: false, text: e.message }) }
    setBusy(false)
  }
  return (
    <Section gap={10}>
      <SectionHead title="Active incidents" note={incidents.data ? `${open.length} open` : null}>
        <button className="btn btn-ghost" style={{ fontSize: 12 }} onClick={() => onNav('incidents')}>View all<Icon name="ph-arrow-right" /></button>
      </SectionHead>
      <div className="surface" style={{ flex: 1, display: 'flex', flexDirection: 'column', padding: open.length ? '2px 14px' : 16 }}>
        {!incidents.data && <div className="muted" style={{ fontSize: 12.5, padding: '10px 0' }}><Icon name="ph-circle-notch" className="spin" /> Loading…</div>}
        {incidents.data && open.length === 0 && <OkEmpty title="No active incidents" sub="Everything AtlasOS watches is within its normal range." />}
        {open.slice(0, 2).map(i => (
          <div key={i.id} style={{ display: 'flex', flexDirection: 'column', gap: 8, padding: '12px 0', background: 'var(--rule) no-repeat bottom / 100% 1px' }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
              <Pill t={SEV_T[(i.severity || '').toLowerCase()] || 'warn'} label={i.severity || 'unknown'} icon={false} mono />
              <span style={{ fontSize: 13.5, fontWeight: 500, flex: '1 1 160px', minWidth: 0 }}>{i.title}</span>
              <span className="muted mono" style={{ fontSize: 11 }}>{ago(i.opened_at)}</span>
            </div>
            <div className="muted mono" style={{ fontSize: 11 }}>{[i.service, i.namespace].filter(Boolean).join('/')}</div>
            <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
              <button className="btn btn-primary" style={{ fontSize: 12 }} onClick={() => onAsk(`Summarize incident ${i.id} ("${i.title}") and what's likely causing it, then recommend a fix.`)}><Icon name="ph-sparkle" />Diagnose with AI</button>
              <button className="btn btn-secondary" style={{ fontSize: 12 }} onClick={() => onNav('incidents')}><Icon name="ph-list-bullets" />View evidence</button>
              <button className="btn btn-secondary" style={{ fontSize: 12 }} disabled={demo || busy} onClick={() => ack(i)}><Icon name="ph-eye" />Acknowledge</button>
            </div>
          </div>
        ))}
        <Msg msg={msg} />
      </div>
    </Section>
  )
}

const OUT_T = { applied: 'ok', approved: 'accent', rejected: 'neutral', failed: 'crit' }

/** Top pending fixes/deploys, decided right here — same endpoints the
 *  Approvals page uses. */
function ApprovalsCard({ approvals, deploys, onNav }) {
  const demo = useDemo()
  const [busy, setBusy] = useState({})
  const [done, setDone] = useState({})
  const pending = approvals.data?.pending || []
  const pendingDeploys = deploys.data || []
  const items = [
    ...pending.map(a => ({ id: a.id, kind: 'fix', summary: a.summary, created_at: a.created_at })),
    ...pendingDeploys.map(d => ({ id: d.id, kind: 'deploy', summary: `Deploy ${d.new_image} to ${d.namespace}/${d.service}`, created_at: d.requested_at })),
  ].sort((a, b) => Date.parse(b.created_at) - Date.parse(a.created_at))
  const count = pending.length + pendingDeploys.length

  const decide = async (item, approve) => {
    setBusy(b => ({ ...b, [item.id]: true }))
    try {
      const r = item.kind === 'fix' ? await postJSON(`/api/approvals/${item.id}/decide`, { approve })
        : await postJSON(`/api/deploys/${item.id}/${approve ? 'approve' : 'reject'}`)
      setDone(d => ({ ...d, [item.id]: r.status || (approve ? 'approved' : 'rejected') }))
    } catch {
      setDone(d => ({ ...d, [item.id]: 'failed' }))
    }
    setBusy(b => ({ ...b, [item.id]: false }))
    approvals.reload(); deploys.reload()
  }

  return (
    <Section gap={10}>
      <SectionHead title="Pending approvals" note={approvals.data ? `${count} waiting` : null}>
        <button className="btn btn-ghost" style={{ fontSize: 12 }} onClick={() => onNav('approvals')}>View all<Icon name="ph-arrow-right" /></button>
      </SectionHead>
      <div className="surface" style={{ flex: 1, display: 'flex', flexDirection: 'column', padding: items.length ? '2px 14px' : 16 }}>
        {!approvals.data && <div className="muted" style={{ fontSize: 12.5, padding: '10px 0' }}><Icon name="ph-circle-notch" className="spin" /> Loading…</div>}
        {approvals.data && items.length === 0 && <OkEmpty title="Nothing waiting for you" sub="Risky fixes and deploys land here and in Slack." />}
        {items.slice(0, 3).map(item => (
          <div key={item.id} style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '11px 0', background: 'var(--rule) no-repeat bottom / 100% 1px' }}>
            <Icon name={item.kind === 'deploy' ? 'ph-rocket-launch' : 'ph-wrench'} style={{ color: 'var(--color-accent)', flex: 'none' }} />
            <div style={{ flex: 1, minWidth: 0 }}>
              <div style={{ fontSize: 12.5, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{item.summary}</div>
              <div className="muted" style={{ fontSize: 11 }}>{ago(item.created_at)}</div>
            </div>
            {done[item.id]
              ? <Pill t={OUT_T[done[item.id]] || 'neutral'} label={done[item.id]} icon={false} />
              : demo
                ? <span className="muted" style={{ fontSize: 11 }}>disabled in demo</span>
                : (
                  <div style={{ display: 'flex', gap: 6, flex: 'none' }}>
                    <button className="btn btn-icon" style={{ color: 'var(--st-ok)', border: '1px solid var(--color-divider)' }} disabled={busy[item.id]} onClick={() => decide(item, true)} aria-label="Approve"><Icon name="ph-check" /></button>
                    <button className="btn btn-icon" style={{ color: 'var(--st-crit)', border: '1px solid var(--color-divider)' }} disabled={busy[item.id]} onClick={() => decide(item, false)} aria-label="Reject"><Icon name="ph-x" /></button>
                  </div>
                )}
          </div>
        ))}
      </div>
    </Section>
  )
}

/** Quick Operations — real one-click commands plus shortcuts to the page
 *  that handles anything needing more input. Nothing here is decorative. */
function QuickOps({ onRun, onNav, onAsk, onOpenChat, daemonRunning }) {
  const [cat, setCat] = useState('all')
  const items = [
    { id: 'scan', cat: 'k8s', icon: 'ph-cube-focus', label: 'Scan Cluster', sub: 'pods, nodes, probes', run: () => onRun(QUICK[0]) },
    { id: 'daemon', cat: 'k8s', icon: 'ph-heartbeat', label: daemonRunning ? 'Stop Daemon' : 'Start Daemon', sub: daemonRunning ? 'pause auto-healing' : 'starts the watchers', run: () => onRun({ id: 'daemon' }) },
    { id: 'cluster', cat: 'k8s', icon: 'ph-cube', label: 'View Cluster', sub: 'nodes & problem pods', run: () => onNav('cluster') },
    { id: 'diag-k8s', cat: 'k8s', icon: 'ph-sparkle', label: 'AI Diagnose Cluster', sub: 'ask what\'s wrong', run: () => onAsk('Diagnose my cluster — what, if anything, needs attention?') },
    { id: 'jenkins-scan', cat: 'cicd', icon: 'ph-hammer', label: 'Scan Jenkins', sub: 'failing jobs · AI', run: () => onRun(QUICK[2]) },
    { id: 'jenkins', cat: 'cicd', icon: 'ph-wrench', label: 'View Jenkins', sub: 'builds & agents', run: () => onNav('jenkins') },
    { id: 'github', cat: 'cicd', icon: 'ph-github-logo', label: 'GitHub Actions', sub: 'workflow runs', run: () => onNav('github') },
    { id: 'deploys', cat: 'cicd', icon: 'ph-rocket-launch', label: 'Set Up Deploys', sub: 'GitHub push → deploy', run: () => onNav('deploys') },
    { id: 'aws-cost', cat: 'aws', icon: 'ph-currency-dollar', label: 'AWS Cost', sub: 'spend & waste · AI', run: () => onRun(QUICK[4]) },
    { id: 'aws', cat: 'aws', icon: 'ph-cloud', label: 'AWS Resources', sub: 'EC2, RDS, LBs', run: () => onNav('aws') },
    { id: 'databases', cat: 'aws', icon: 'ph-database', label: 'Check Databases', sub: 'RDS & Aurora health', run: () => onNav('databases') },
    { id: 'find-savings', cat: 'aws', icon: 'ph-sparkle', label: 'Find Savings', sub: 'AI waste finder', run: () => onAsk('Find AWS waste and savings opportunities in my account, biggest first.') },
    { id: 'tls', cat: 'diagnose', icon: 'ph-lock-key', label: 'Scan TLS', sub: 'every certificate', run: () => onRun(QUICK[1]) },
    { id: 'domains', cat: 'diagnose', icon: 'ph-globe-hemisphere-west', label: 'Check Domains', sub: 'DNS → TLS → HTTP', run: () => onNav('domains') },
    { id: 'summary', cat: 'diagnose', icon: 'ph-note', label: 'Daily Summary', sub: 'preview, no Slack post', run: () => onRun(QUICK[3]) },
    { id: 'logs', cat: 'diagnose', icon: 'ph-scroll', label: 'View Logs', sub: "AtlasOS's own services", run: () => onNav('system') },
    { id: 'ask', cat: 'diagnose', icon: 'ph-chat-circle-dots', label: 'Ask the Assistant', sub: 'open the AI copilot', run: () => onOpenChat() },
  ]
  const cats = [['all', 'All'], ['k8s', 'Kubernetes'], ['cicd', 'CI/CD'], ['aws', 'AWS'], ['diagnose', 'Diagnose']]
  const shown = cat === 'all' ? items : items.filter(i => i.cat === cat)
  return (
    <Section>
      <SectionHead title="Quick operations" />
      <div role="tablist" style={{ display: 'flex', gap: 2, flexWrap: 'wrap' }}>
        {cats.map(([id, label]) => (
          <button key={id} role="tab" aria-selected={cat === id} onClick={() => setCat(id)} style={{
            padding: '5px 11px', border: 0, borderRadius: 999, cursor: 'pointer', fontSize: 12,
            background: cat === id ? 'color-mix(in srgb, var(--color-accent) 16%, transparent)' : 'transparent',
            color: cat === id ? 'var(--color-accent)' : 'var(--muted)',
          }}>{label}</button>
        ))}
      </div>
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(190px, 1fr))', gap: 8 }}>
        {shown.map(it => (
          <button key={it.id} onClick={it.run} className="hoverable" style={{
            display: 'flex', alignItems: 'center', gap: 10, padding: '11px 12px', textAlign: 'left',
            borderRadius: 'var(--radius-md)', background: 'var(--color-surface)', border: '1px solid var(--color-divider)',
          }}>
            <div style={{ width: 30, height: 30, borderRadius: 8, display: 'grid', placeItems: 'center', flex: 'none', color: 'var(--color-accent)', background: 'color-mix(in srgb, var(--color-accent) 12%, transparent)' }}>
              <Icon name={it.icon} size={15} />
            </div>
            <div style={{ minWidth: 0 }}>
              <div style={{ fontSize: 12.5, fontWeight: 500 }}>{it.label}</div>
              <div className="muted" style={{ fontSize: 10.5 }}>{it.sub}</div>
            </div>
          </button>
        ))}
      </div>
    </Section>
  )
}

function MetricTile({ icon, label, value, sub, pct, color }) {
  return (
    <div className="surface" style={{ display: 'flex', flexDirection: 'column', gap: 6, padding: '11px 13px', minWidth: 0 }}>
      <div className="kicker" style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 10 }}><Icon name={icon} size={12} />{label}</div>
      <span style={{ fontSize: 19, fontWeight: 600, letterSpacing: '-0.01em', fontVariantNumeric: 'tabular-nums' }}>{value}</span>
      {pct != null ? <Bar pct={pct} color={color} /> : <span className="muted" style={{ fontSize: 11 }}>{sub}</span>}
    </div>
  )
}

/** Only metrics AtlasOS actually measures — no fabricated network/latency
 *  tiles for telemetry that isn't collected (couldn't-check must never look
 *  like a number). */
function ResourceHealth({ cluster, chart, spend }) {
  const c = cluster.data
  const nodes = c?.nodes || []
  const avg = key => nodes.length ? Math.round(nodes.reduce((a, n) => a + (n[key] || 0), 0) / nodes.length) : null
  const cpu = avg('cpu_percent'), mem = avg('memory_percent')
  const nodesReady = nodes.filter(n => n.status === 'Ready').length
  const restarts = (c?.problem_pods || []).reduce((a, p) => a + (p.restarts || 0), 0)
  const s = spend.data
  const spendTotal = s?.daily?.reduce((a, d) => a + d.amount, 0) || 0

  return (
    <Section>
      <SectionHead title="Resource health" note={c ? `from the ${c.context || 'current'} cluster` : "couldn't check the cluster"} />
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(150px, 1fr))', gap: 8 }}>
        {!c ? (
          <div className="muted" style={{ gridColumn: '1 / -1', fontSize: 12.5 }}><Icon name="ph-question" style={{ color: 'var(--st-unk)' }} /> Cluster metrics unavailable{cluster.error ? `: ${cluster.error}` : ''}.</div>
        ) : (
          <>
            <MetricTile icon="ph-cpu" label="CPU (avg)" value={cpu == null ? '—' : `${cpu}%`} pct={cpu} color={cpu > 85 ? 'var(--st-crit)' : cpu > 65 ? 'var(--st-warn)' : 'var(--st-ok)'} />
            <MetricTile icon="ph-memory" label="Memory (avg)" value={mem == null ? '—' : `${mem}%`} pct={mem} color={mem > 85 ? 'var(--st-crit)' : mem > 65 ? 'var(--st-warn)' : 'var(--st-ok)'} />
            <MetricTile icon="ph-arrows-clockwise" label="Pod restarts" value={restarts} sub={`${c.problem_pods?.length || 0} problem pod(s)`} />
            <MetricTile icon="ph-hard-drives" label="Nodes ready" value={`${nodesReady}/${nodes.length}`} sub={nodesReady === nodes.length ? 'all ready' : 'needs attention'} />
          </>
        )}
      </div>
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(340px, 1fr))', gap: 10 }}>
        <div className="surface" style={{ display: 'flex', flexDirection: 'column', gap: 8, padding: '12px 14px 8px', minWidth: 0 }}>
          <div style={{ display: 'flex', alignItems: 'baseline', gap: 8, flexWrap: 'wrap' }}><span style={{ fontSize: 13, fontWeight: 500 }}>Heals per day</span><span className="muted" style={{ fontSize: 11.5 }}>every action the daemon took on its own</span></div>
          {chart.data ? <DailyBars labels={chart.data.labels} data={chart.data.data} unit=" actions" empty="No daemon actions in the last 30 days." />
            : <div className="muted" style={{ fontSize: 12, padding: '30px 0' }}>{chart.error ? `Couldn't load: ${chart.error}` : 'Loading…'}</div>}
        </div>
        <div className="surface" style={{ display: 'flex', flexDirection: 'column', gap: 8, padding: '12px 14px 8px', minWidth: 0 }}>
          <div style={{ display: 'flex', alignItems: 'baseline', gap: 8, flexWrap: 'wrap' }}><span style={{ fontSize: 13, fontWeight: 500 }}>AWS spend per day</span>{s?.daily?.length && <span className="muted" style={{ fontSize: 11.5 }}>${spendTotal.toFixed(2)} total · amber = statistical spike</span>}</div>
          {s?.daily?.length ? <SpendChart daily={s.daily} mean={s.mean} anomalies={s.anomalies} />
            : <div style={{ fontSize: 12, padding: '30px 0', color: 'var(--st-unk)', display: 'flex', gap: 6, alignItems: 'center' }}><Icon name="ph-question" />{spend.loading ? 'Loading…' : `Couldn't get Cost Explorer data${s?.error || spend.error ? `: ${s?.error || spend.error}` : ' (check AWS access)'}`}</div>}
        </div>
      </div>
    </Section>
  )
}

const FEED_ICON = { daemon_ok: 'ph-check-circle', daemon_fail: 'ph-x-circle', critical: 'ph-x-circle', warning: 'ph-warning', ok: 'ph-check-circle', info: 'ph-info' }

/** Daemon actions + the notification inbox, merged into one chronological
 *  feed — both are real AtlasOS records, just from different stores. */
function LiveFeed({ actions, notifications }) {
  const a = (actions.data || []).map(x => ({
    id: `a-${x.timestamp}-${x.resource}`, at: x.timestamp, ok: !!x.success,
    text: `${x.action} ${x.namespace ? `${x.namespace}/` : ''}${x.resource}`, source: x.category,
  }))
  const n = (notifications.data?.items || []).map(x => ({
    id: `n-${x.id}`, at: x.created_at, ok: x.severity !== 'critical' && x.severity !== 'warning', sev: x.severity,
    text: x.title, source: x.kind,
  }))
  const feed = [...a, ...n].sort((x, y) => Date.parse(y.at) - Date.parse(x.at)).slice(0, 12)
  return (
    <Section>
      <SectionHead title="Live operations feed" note="daemon actions and alerts, merged" />
      <div className="surface" style={{ display: 'flex', flexDirection: 'column', padding: feed.length ? '2px 14px' : 16 }}>
        {!actions.data && !notifications.data && <div className="muted" style={{ fontSize: 12.5, padding: '10px 0' }}><Icon name="ph-circle-notch" className="spin" /> Loading…</div>}
        {(actions.data || notifications.data) && feed.length === 0 && <div className="muted" style={{ fontSize: 12.5, padding: '10px 0' }}>Nothing recorded yet.</div>}
        {feed.map(f => {
          const icon = f.sev === 'critical' ? 'ph-x-circle' : f.sev === 'warning' ? 'ph-warning' : FEED_ICON[f.ok ? 'ok' : 'daemon_fail']
          const color = f.sev === 'critical' ? 'var(--st-crit)' : f.sev === 'warning' ? 'var(--st-warn)' : f.ok ? 'var(--st-ok)' : 'var(--st-crit)'
          return (
            <div key={f.id} style={{ display: 'flex', gap: 10, alignItems: 'flex-start', padding: '8px 0', background: 'var(--rule) no-repeat bottom / 100% 1px' }}>
              <Icon name={icon} size={15} style={{ marginTop: 1, color, flex: 'none' }} />
              <span style={{ flex: 1, minWidth: 0, fontSize: 12.5, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{f.text}</span>
              <span className="mono muted" style={{ fontSize: 11, whiteSpace: 'nowrap' }}>{ago(f.at)}</span>
            </div>
          )
        })}
      </div>
    </Section>
  )
}

/** One stat tile in the Briefing card. Shows "couldn't check" instead of a
 *  fake zero when its sub-fetch failed — a failure in one integration must
 *  never silently read as "nothing happened" in another. */
function BriefStat({ label, go, onNav, value, flagged, error }) {
  return (
    <button onClick={() => onNav(go)} className="hoverable" style={{
      display: 'flex', flexDirection: 'column', gap: 2, padding: '10px 14px', borderRadius: 'var(--radius-md)',
      border: '1px solid var(--color-divider)', background: 'var(--color-surface)', textAlign: 'left', font: 'inherit', color: 'inherit',
    }}>
      <span className="kicker" style={{ fontSize: 10.5 }}>{label}</span>
      {error
        ? <span style={{ display: 'flex', alignItems: 'center', gap: 4, fontSize: 12.5, color: 'var(--st-unk)' }}><Icon name="ph-question" size={14} />couldn't check</span>
        : <span style={{ fontSize: 20, fontWeight: 600, fontVariantNumeric: 'tabular-nums', color: flagged ? 'var(--st-warn)' : undefined }}>{value}</span>}
    </button>
  )
}

/** "Since you were last here" — a delta, not a status snapshot: what's new
 *  rather than what's currently true (that's the Status section below it). */
function Briefing({ digest, onNav }) {
  const d = digest.data
  if (!d && digest.loading) return <div className="skel" style={{ height: 92, borderRadius: 'var(--radius-lg)' }} />
  if (!d) return null   // not critical to the page — a failed digest shouldn't block Overview
  const windowLabel = d.had_previous ? 'since you were last here' : 'in the last 24 hours'
  if (d.headline === 'All quiet — nothing new.') {
    return <OkEmpty title={`All quiet ${windowLabel}`} sub="No new incidents, deploys, or fixes." />
  }
  return (
    <Section>
      <SectionHead title="Briefing" note={`${windowLabel} · built ${ago(d.generated_at)}`} />
      <div className="surface" style={{ padding: 14, display: 'flex', flexDirection: 'column', gap: 12 }}>
        <div style={{ fontSize: 14, fontWeight: 500 }}>{d.headline}</div>
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(130px, 1fr))', gap: 8 }}>
          <BriefStat label="Incidents" go="incidents" onNav={onNav} value={d.incidents.total} error={d.incidents.error} flagged={d.incidents.open > 0} />
          <BriefStat label="Deploys" go="deploys" onNav={onNav} value={d.deploys.total} error={d.deploys.error} flagged={d.deploys.failed > 0} />
          <BriefStat label="Auto-fixes" go="activity" onNav={onNav} value={d.fixes.total} error={d.fixes.error} />
          <BriefStat label="Waiting now" go="approvals" onNav={onNav} value={d.approvals.pending_now} error={d.approvals.error} flagged={d.approvals.pending_now > 0} />
        </div>
        {d.incidents.items?.length > 0 && (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
            {d.incidents.items.map(i => (
              <button key={i.id} onClick={() => onNav('incidents')} className="hoverable" style={{
                display: 'flex', alignItems: 'center', gap: 8, padding: '6px 8px', borderRadius: 6, textAlign: 'left', font: 'inherit', color: 'inherit',
              }}>
                <Icon name="ph-siren" style={{ color: 'var(--st-crit)' }} />
                <span style={{ fontSize: 12.5, flex: 1 }}>{i.title}</span>
                <span className="muted" style={{ fontSize: 11 }}>{i.service}/{i.namespace}</span>
              </button>
            ))}
          </div>
        )}
      </div>
    </Section>
  )
}

export default function Overview({ summary, actions, chart, spend, live, incidents, approvals, deploys, digest, cluster, notifications, onNav, onRun, onAsk, onOpenChat, daemonRunning }) {
  const sections = summary.data?.sections
  return (
    <div data-screen-label="01 Overview" style={{ display: 'flex', flexDirection: 'column', gap: 22 }}>
      <Briefing digest={digest} onNav={onNav} />
      <ImpactStrip live={live} chart={chart} />

      <Section>
        <SectionHead title="Status" note={summary.data ? `${summary.data.headline} · built ${ago(summary.data.generated_at)}` : null}>
          <span style={{ display: 'flex', gap: 12, fontSize: 11 }} className="muted">
            <span style={{ display: 'flex', alignItems: 'center', gap: 4 }}><Icon name="ph-check-circle" style={{ color: 'var(--st-ok)' }} />OK</span>
            <span style={{ display: 'flex', alignItems: 'center', gap: 4 }}><Icon name="ph-warning" style={{ color: 'var(--st-warn)' }} />Needs attention</span>
            <span style={{ display: 'flex', alignItems: 'center', gap: 4 }}><Icon name="ph-question" style={{ color: 'var(--st-unk)' }} />Couldn't check — never counted as OK</span>
          </span>
        </SectionHead>
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(200px, 1fr))', gap: 10 }}>
          <HealthRing sections={sections} />
          {!sections && !summary.error && Array.from({ length: 6 }).map((_, i) => <div key={i} className="skel" style={{ height: 128, borderRadius: 'var(--radius-lg)' }} />)}
          {!sections && summary.error && (
            <div className="nodata" style={{ gridColumn: '1 / -1' }}>
              <div style={{ color: 'var(--st-unk)', fontWeight: 500 }}><Icon name="ph-question" /> Couldn't build the status summary</div>
              <code className="term" style={{ fontSize: 11.5, padding: '6px 9px' }}>{summary.error}</code>
            </div>
          )}
          {sections?.map(s => <StatusCard key={s.title} s={s} onNav={onNav} />)}
        </div>
      </Section>

      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(380px, 1fr))', gap: 22, alignItems: 'start' }}>
        <IncidentsCard incidents={incidents} onNav={onNav} onAsk={onAsk} />
        <ApprovalsCard approvals={approvals} deploys={deploys} onNav={onNav} />
      </div>

      <QuickOps onRun={onRun} onNav={onNav} onAsk={onAsk} onOpenChat={onOpenChat} daemonRunning={daemonRunning} />

      <ResourceHealth cluster={cluster} chart={chart} spend={spend} />

      <LiveFeed actions={actions} notifications={notifications} />
    </div>
  )
}
