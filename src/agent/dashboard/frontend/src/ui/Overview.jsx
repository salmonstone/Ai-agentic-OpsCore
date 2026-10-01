import { ago } from '../lib/api'
import { sectionTone, tone } from '../lib/tone'
import { DailyBars, SpendChart } from './charts'
import { Icon, Section, SectionHead } from './common'
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
  'GitHub Actions': { icon: 'ph-github-logo' },
  'AWS spend': { icon: 'ph-currency-dollar' },
  'Waiting on you': { icon: 'ph-seal-check', go: 'approvals' },
  'Backups': { icon: 'ph-archive' },
  'AtlasOS': { icon: 'ph-globe-simple' },
}

function Tile({ s, onNav }) {
  const t = tone(sectionTone(s.status))
  const meta = TILE[s.title] || { icon: 'ph-circle' }
  // the summary lines are written for Slack (`code`, ❌) — strip that formatting here
  const plain = l => l.replace(/`/g, '').replace(/^\s*❌\s*/, '').replace(/\*/g, '')
  const [value, ...rest] = s.lines.length ? s.lines.map(plain) : ['—']
  const clickable = !!meta.go
  const Tag = clickable ? 'button' : 'div'
  return (
    <Tag onClick={clickable ? () => onNav(meta.go) : undefined} className={clickable ? 'hoverable' : undefined}
      style={{
        display: 'flex', flexDirection: 'column', gap: 6, textAlign: 'left', padding: 12, minHeight: 118,
        borderRadius: 'var(--radius-md)', font: 'inherit', color: 'inherit',
        background: t.t === 'unk' ? 'var(--hatch), var(--color-surface)' : 'var(--color-surface)',
        border: `1px ${t.bs} ${t.t === 'ok' ? 'var(--color-divider)' : t.line}`,
      }}>
      <div className="kicker" style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 10.5, letterSpacing: '.07em' }}>
        <Icon name={meta.icon} size={14} />{s.title}
      </div>
      <div title={value} style={{
        fontSize: 14.5, fontWeight: 500, lineHeight: 1.3, color: t.t === 'unk' ? 'var(--st-unk)' : 'var(--color-text)',
        display: '-webkit-box', WebkitLineClamp: 3, WebkitBoxOrient: 'vertical', overflow: 'hidden', wordBreak: 'break-word',
      }}>{value}</div>
      <div className="muted" style={{ fontSize: 11.5, flex: 1 }}>{rest.slice(0, 2).join(' · ')}</div>
      <div style={{ display: 'flex', alignItems: 'center', gap: 5, fontSize: 11.5, fontWeight: 500, color: t.c }}>
        <Icon name={t.icon} size={14} />{t.label}
      </div>
    </Tag>
  )
}

function ChartCard({ title, note, children }) {
  return (
    <div className="surface" style={{ display: 'flex', flexDirection: 'column', gap: 8, padding: '12px 14px 8px', minWidth: 0 }}>
      <div style={{ display: 'flex', alignItems: 'baseline', gap: 8, flexWrap: 'wrap' }}>
        <span style={{ fontSize: 13, fontWeight: 500 }}>{title}</span>
        {note && <span className="muted" style={{ fontSize: 11.5 }}>{note}</span>}
      </div>
      {children}
    </div>
  )
}

function Charts({ chart, spend }) {
  const s = spend.data
  const total = s?.daily?.reduce((a, d) => a + d.amount, 0) || 0
  return (
    <Section>
      <SectionHead title="Last 30 days" />
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(340px, 1fr))', gap: 10 }}>
        <ChartCard title="Heals per day" note="every action the daemon took on its own">
          {chart.data ? <DailyBars labels={chart.data.labels} data={chart.data.data} unit=" actions"
              empty="No daemon actions in the last 30 days." />
            : <div className="muted" style={{ fontSize: 12, padding: '30px 0' }}>{chart.error ? `Couldn't load: ${chart.error}` : 'Loading…'}</div>}
        </ChartCard>
        <ChartCard title="AWS spend per day" note={s?.daily?.length ? `$${total.toFixed(2)} total · amber = statistical spike` : null}>
          {s?.daily?.length ? <SpendChart daily={s.daily} mean={s.mean} anomalies={s.anomalies} />
            : (
              <div style={{ fontSize: 12, padding: '30px 0', color: 'var(--st-unk)', display: 'flex', gap: 6, alignItems: 'center' }}>
                <Icon name="ph-question" />{spend.loading ? 'Loading…' : `Couldn't get Cost Explorer data${s?.error || spend.error ? `: ${s?.error || spend.error}` : ' (check AWS access)'}`}
              </div>
            )}
        </ChartCard>
      </div>
    </Section>
  )
}

export default function Overview({ summary, actions, chart, spend, live, onNav, onRun, daemonRunning }) {
  const sections = summary.data?.sections
  return (
    <div data-screen-label="01 Overview" style={{ display: 'flex', flexDirection: 'column', gap: 22 }}>
      <ImpactStrip live={live} chart={chart} />
      <Section>
        <SectionHead title="Status" note={summary.data ? `${summary.data.headline} · built ${ago(summary.data.generated_at)}` : null}>
          <span style={{ display: 'flex', gap: 12, fontSize: 11 }} className="muted">
            <span style={{ display: 'flex', alignItems: 'center', gap: 4 }}><Icon name="ph-check-circle" style={{ color: 'var(--st-ok)' }} />OK</span>
            <span style={{ display: 'flex', alignItems: 'center', gap: 4 }}><Icon name="ph-warning" style={{ color: 'var(--st-warn)' }} />Needs attention</span>
            <span style={{ display: 'flex', alignItems: 'center', gap: 4 }}><Icon name="ph-question" style={{ color: 'var(--st-unk)' }} />Couldn't check — never counted as OK</span>
          </span>
        </SectionHead>
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(190px, 1fr))', gap: 10 }}>
          {!sections && !summary.error && Array.from({ length: 7 }).map((_, i) => <div key={i} className="skel" style={{ height: 118, borderRadius: 'var(--radius-md)' }} />)}
          {!sections && summary.error && (
            <div className="nodata" style={{ gridColumn: '1 / -1' }}>
              <div style={{ color: 'var(--st-unk)', fontWeight: 500 }}><Icon name="ph-question" /> Couldn't build the status summary</div>
              <code className="term" style={{ fontSize: 11.5, padding: '6px 9px' }}>{summary.error}</code>
            </div>
          )}
          {sections?.map(s => <Tile key={s.title} s={s} onNav={onNav} />)}
        </div>
      </Section>

      <Charts chart={chart} spend={spend} />

      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(330px, 1fr))', gap: 22, alignItems: 'start' }}>
        <Section>
          <SectionHead title="Quick actions" />
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(160px, 1fr))', gap: 8 }}>
            {QUICK.map(q => (
              <button key={q.id} onClick={() => onRun(q)} className="hoverable" style={{
                display: 'flex', flexDirection: 'column', alignItems: 'flex-start', gap: 7, padding: 12, textAlign: 'left',
                borderRadius: 'var(--radius-md)', background: 'var(--color-surface)', border: '1px solid var(--color-divider)',
              }}>
                <div style={{ display: 'flex', alignItems: 'center', width: '100%', gap: 8 }}>
                  <Icon name={q.icon} size={18} style={{ color: 'var(--color-accent)' }} />
                  <span style={{ fontSize: 13.5, fontWeight: 500 }}>{q.name}</span>
                  <Icon name="ph-play" style={{ marginLeft: 'auto', color: 'var(--muted)' }} />
                </div>
                <code className="muted" style={{ fontSize: 11 }}>agent {q.cmd}</code>
                <span className="muted" style={{ fontSize: 11 }}>{q.note}</span>
              </button>
            ))}
            <button onClick={() => onRun({ id: 'daemon' })} className="hoverable" style={{
              display: 'flex', flexDirection: 'column', alignItems: 'flex-start', gap: 7, padding: 12, textAlign: 'left',
              borderRadius: 'var(--radius-md)', background: 'var(--color-surface)', border: '1px solid var(--color-divider)',
            }}>
              <div style={{ display: 'flex', alignItems: 'center', width: '100%', gap: 8 }}>
                <Icon name="ph-heartbeat" size={18} style={{ color: 'var(--color-accent)' }} />
                <span style={{ fontSize: 13.5, fontWeight: 500 }}>{daemonRunning ? 'Stop Daemon' : 'Start Daemon'}</span>
                <Icon name={daemonRunning ? 'ph-stop' : 'ph-play'} style={{ marginLeft: 'auto', color: 'var(--muted)' }} />
              </div>
              <code className="muted" style={{ fontSize: 11 }}>agent daemon {daemonRunning ? 'stop' : 'start'}</code>
              <span className="muted" style={{ fontSize: 11 }}>{daemonRunning ? 'pauses auto-healing' : 'starts the 8 watchers'}</span>
            </button>
          </div>
        </Section>

        <Section>
          <SectionHead title="Activity" note="daemon actions" />
          <div className="surface" style={{ display: 'flex', flexDirection: 'column', padding: '2px 14px' }}>
            {!actions.data && actions.loading && Array.from({ length: 4 }).map((_, i) => (
              <div key={i} style={{ display: 'flex', gap: 10, padding: '12px 0' }}><div className="skel" style={{ width: 16, height: 16, borderRadius: '50%' }} /><div className="skel" style={{ flex: 1, height: 10, marginTop: 3 }} /></div>
            ))}
            {actions.error && <div className="muted" style={{ padding: '12px 0', fontSize: 12.5 }}><Icon name="ph-question" style={{ color: 'var(--st-unk)' }} /> Couldn't load activity: {actions.error}</div>}
            {actions.data?.length === 0 && <div className="muted" style={{ padding: '12px 0', fontSize: 12.5 }}>No daemon actions recorded yet.</div>}
            {actions.data?.slice(0, 10).map((a, i) => (
              <div key={i} style={{ display: 'flex', gap: 10, alignItems: 'flex-start', padding: '9px 0', background: 'var(--rule) no-repeat bottom / 100% 1px' }}>
                <Icon name={a.success ? 'ph-check-circle' : 'ph-x-circle'} size={16} style={{ marginTop: 1, color: a.success ? 'var(--st-ok)' : 'var(--st-crit)' }} />
                <div style={{ flex: 1, minWidth: 0, fontSize: 12.5 }}>
                  {a.action} <code style={{ fontSize: '0.92em' }}>{a.namespace ? `${a.namespace}/` : ''}{a.resource}</code>
                  {a.note && <div className="muted" style={{ fontSize: 11, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{a.note}</div>}
                  <div className="muted" style={{ fontSize: 11 }}>{a.category}</div>
                </div>
                <span className="mono muted" style={{ fontSize: 11, whiteSpace: 'nowrap' }}>{ago(a.timestamp)}</span>
              </div>
            ))}
          </div>
        </Section>
      </div>
    </div>
  )
}
