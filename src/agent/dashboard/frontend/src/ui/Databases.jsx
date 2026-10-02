import { ago } from '../lib/api'
import { useData } from '../lib/demo'
import { tone } from '../lib/tone'
import { Bar, Card, Icon, NoData, OkEmpty, Pill, Section, SectionHead, SkeletonRows } from './common'

const STATUS_T = { healthy: 'ok', warning: 'warn', critical: 'crit' }

function Metric({ label, value, pct, warnAt, critAt, unit = '' }) {
  const t = pct == null ? 'neutral' : pct >= critAt ? 'crit' : pct >= warnAt ? 'warn' : 'ok'
  const x = tone(t)
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 4, minWidth: 0 }}>
      <span className="kicker" style={{ fontSize: 10 }}>{label}</span>
      <span style={{ fontSize: 15, fontWeight: 500, fontVariantNumeric: 'tabular-nums' }}>{value}{unit}</span>
      {pct != null && <div style={{ display: 'flex' }}><Bar pct={pct} color={x.c} /></div>}
    </div>
  )
}

function DbCard({ db, onAsk }) {
  const i = db.instance || {}
  const m = db.metrics || {}
  const t = STATUS_T[db.status] || 'unk'
  const storageUsed = i.storage_gb ? Math.max(0, Math.min(100, 100 - (m.free_storage_gb / i.storage_gb) * 100)) : null
  return (
    <Card warn={t !== 'ok'}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
        <Icon name="ph-database" size={19} style={{ color: 'var(--color-accent)' }} />
        <div style={{ flex: '1 1 200px', minWidth: 0 }}>
          <div className="mono" style={{ fontSize: 14, fontWeight: 500 }}>{i.id}</div>
          <div className="muted" style={{ fontSize: 11.5 }}>{i.engine} · {i.class} · {i.storage_gb} GB {i.storage_type}{i.multi_az ? ' · Multi-AZ' : ''}{db.cluster ? ` · Aurora cluster, ${db.cluster.members} member(s)` : ''}</div>
        </div>
        <Pill t={i.status === 'available' ? 'ok' : 'warn'} label={i.status || 'unknown'} icon={false} />
        <Pill t={t} label={db.status} />
      </div>
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(110px, 1fr))', gap: 12 }}>
        <Metric label="CPU" value={m.cpu_pct ?? '—'} unit="%" pct={m.cpu_pct} warnAt={80} critAt={95} />
        <Metric label="Storage used" value={storageUsed == null ? '—' : Math.round(storageUsed)} unit="%" pct={storageUsed} warnAt={80} critAt={90} />
        <Metric label="Free storage" value={m.free_storage_gb ?? '—'} unit=" GB" />
        <Metric label="Connections (max)" value={m.connections_max ?? '—'} />
        <Metric label="Read latency" value={m.read_latency_ms ?? '—'} unit=" ms" />
        <Metric label="Free memory" value={m.freeable_memory_mb != null ? Math.round(m.freeable_memory_mb) : '—'} unit=" MB" />
      </div>
      {db.issues?.length > 0 && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
          {db.issues.map((is, k) => (
            <div key={k} style={{ fontSize: 12.5, display: 'flex', gap: 8, alignItems: 'flex-start' }}>
              <Icon name={is.severity === 'critical' ? 'ph-x-circle' : 'ph-warning'} style={{ color: is.severity === 'critical' ? 'var(--st-crit)' : 'var(--st-warn)', marginTop: 2 }} />
              <div><b>{is.detail}</b> <span className="muted">— {is.fix}</span></div>
            </div>
          ))}
          {db.incident_id && <div className="muted" style={{ fontSize: 11.5 }}>Incident <span className="mono">{db.incident_id.slice(0, 8)}</span> opened for the critical finding.</div>}
          <div><button className="btn btn-primary" style={{ fontSize: 12 }} onClick={() => onAsk(`My RDS database ${i.id} has these problems: ${db.issues.map(x => x.detail).join('; ')}. What should I do, step by step?`)}><Icon name="ph-sparkle" />Ask what to do</button></div>
        </div>
      )}
      {i.endpoint && <div className="mono muted" style={{ fontSize: 11 }}>{i.endpoint}:{i.port}</div>}
    </Card>
  )
}

export default function Databases({ onAsk }) {
  const dbs = useData('databases', '/api/databases')
  const d = dbs.data
  return (
    <div data-screen-label="Databases" style={{ display: 'flex', flexDirection: 'column', gap: 22 }}>
      <Section>
        <SectionHead title="RDS & Aurora" note={d?.reachable ? `${d.region} · last hour of CloudWatch · checked ${ago(d.generated_at)}` : null}>
          <button className="btn btn-secondary" onClick={() => dbs.reload('/api/databases?force=true')} disabled={dbs.loading}><Icon name="ph-arrow-clockwise" className={dbs.loading ? 'spin' : undefined} />Re-check</button>
        </SectionHead>
        {!d && dbs.loading && <SkeletonRows rows={3} />}
        {!d && dbs.error && <NoData note="Couldn't check your databases." error={dbs.error} onRetry={() => dbs.reload()} />}
        {d && !d.reachable && <NoData label="AWS" note={'AWS couldn\'t be reached, so databases weren\'t checked — this is not "no databases".'} error={d.error} onRetry={() => dbs.reload('/api/databases?force=true')} />}
        {d?.reachable && d.error && <NoData note="The scan failed." error={d.error} />}
        {d?.reachable && !d.error && d.databases.length === 0 && <OkEmpty title={`No RDS databases in ${d.region}`} sub="Checked just now. Databases in other regions aren't shown — change AWS_REGION in Settings." />}
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(440px, 1fr))', gap: 10, alignItems: 'start' }}>
          {d?.databases?.map(db => <DbCard key={db.instance?.id} db={db} onAsk={onAsk} />)}
        </div>
      </Section>
    </div>
  )
}
