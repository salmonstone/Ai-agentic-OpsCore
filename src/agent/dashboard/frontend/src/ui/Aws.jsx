import { ago } from '../lib/api'
import { SpendChart } from './charts'
import { Icon, NoData, Pill, Section, SectionHead, SkeletonRows, TableCard } from './common'

const money = v => `$${(v ?? 0).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
const errorOf = list => (Array.isArray(list) && list.length === 1 && list[0]?.error ? list[0].error : null)

function Inventory({ title, rows, empty, cols }) {
  const err = errorOf(rows)
  return (
    <Section>
      <SectionHead title={title} note={err ? null : `${rows.length}`} />
      {err ? <NoData note={`Couldn't list ${title.toLowerCase()}.`} error={err} />
        : rows.length === 0 ? <div className="muted" style={{ fontSize: 12.5 }}>{empty}</div>
          : (
            <TableCard>
              <table className="table">
                <thead><tr>{cols.map(([h]) => <th key={h}>{h}</th>)}</tr></thead>
                <tbody>{rows.map((r, i) => <tr key={r.id || r.arn || r.allocation_id || i}>{cols.map(([h, render]) => <td key={h}>{render(r)}</td>)}</tr>)}</tbody>
              </table>
            </TableCard>
          )}
    </Section>
  )
}

const mono = (v, muted) => <span className={`mono${muted ? ' muted' : ''}`} style={{ fontSize: 12 }}>{v || '—'}</span>
const statePill = (ok, label) => <Pill t={ok ? 'ok' : 'warn'} label={label} />

export default function Aws({ aws, onAsk, onRefresh }) {
  const d = aws.data
  if (!d && aws.loading) return <SkeletonRows rows={6} />
  if (!d) return <NoData label="AWS" note="Couldn't ask the dashboard server." error={aws.error} onRetry={() => aws.reload()} />
  if (!d.reachable) return <NoData label="AWS" note={'Not "no resources" — AtlasOS couldn\'t sign in to AWS. Check AWS in Settings.'} error={d.error} onRetry={onRefresh} />

  const c = d.cost
  const services = c ? Object.entries(c.by_service || {}).sort((a, b) => b[1] - a[1]) : []
  const top = services.slice(0, 8)
  const maxSvc = top[0]?.[1] || 1
  const daily = c?.daily || []
  const mean = daily.length ? daily.reduce((s, x) => s + x.amount, 0) / daily.length : 0
  const inv = d.inventory
  const loose = (inv.elastic_ips || []).filter(e => !e.error && !e.is_attached)

  return (
    <div data-screen-label="AWS" style={{ display: 'flex', flexDirection: 'column', gap: 22 }}>
      <div className="surface" style={{ display: 'flex', alignItems: 'center', gap: 12, flexWrap: 'wrap', padding: '12px 16px' }}>
        <Icon name="ph-cloud" size={20} style={{ color: 'var(--color-accent)' }} />
        <div style={{ flex: '1 1 300px', minWidth: 0 }}>
          <div style={{ fontSize: 13.5, fontWeight: 500 }}>Account {d.identity.account} · <span className="mono">{d.region}</span></div>
          <div className="mono muted" style={{ fontSize: 11.5, overflow: 'hidden', textOverflow: 'ellipsis' }}>{d.identity.arn}</div>
        </div>
        <span className="muted" style={{ fontSize: 11.5 }}>checked {ago(d.generated_at)} · cached 1h (Cost Explorer is $0.01 a call)</span>
        <button className="btn btn-secondary" onClick={onRefresh} disabled={aws.loading}><Icon name="ph-arrow-clockwise" className={aws.loading ? 'spin' : undefined} />Re-check</button>
      </div>

      <Section>
        <SectionHead title="Spend · last 30 days">
          <button className="btn btn-primary" onClick={() => onAsk('Find AWS waste and savings opportunities in my account, biggest first.')}><Icon name="ph-sparkle" />Find savings</button>
        </SectionHead>
        {!c ? <NoData note="Not $0 — Cost Explorer gave no data." error={d.cost_error} /> : (
          <>
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(170px, 1fr))', gap: 1, borderRadius: 'var(--radius-md)', overflow: 'hidden', background: 'var(--color-divider)', boxShadow: 'var(--shadow-sm)' }}>
              {/* month_change_pct compares a partial month with a whole one (always -100% on the 1st),
                  so show month-to-date as a share of last month instead */}
              {[['Last 30 days', money(c.total), `${money(mean)}/day avg`],
                ['Month to date', money(c.month_to_date),
                  c.last_month > 0 ? `${Math.round(((c.month_to_date || 0) / c.last_month) * 100)}% of last month's total so far` : ''],
                ['Last month', money(c.last_month), c.forecast > (c.month_to_date || 0) ? `this month forecast: ${money(c.forecast)}` : 'full calendar month'],
              ].map(([k, v, s]) => (
                <div key={k} style={{ display: 'flex', flexDirection: 'column', gap: 2, padding: '12px 14px', background: 'var(--color-surface)' }}>
                  <span className="kicker" style={{ fontSize: 10.5 }}>{k}</span>
                  <span style={{ fontSize: 22, fontWeight: 600, fontVariantNumeric: 'tabular-nums' }}>{v}</span>
                  <span className="muted" style={{ fontSize: 11.5 }}>{s}</span>
                </div>
              ))}
            </div>
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(340px, 1fr))', gap: 10 }}>
              <div className="surface" style={{ padding: '12px 14px 8px' }}>
                <div style={{ fontSize: 13, fontWeight: 500, marginBottom: 6 }}>Per day</div>
                {daily.length ? <SpendChart daily={daily} mean={mean} /> : <div className="muted" style={{ fontSize: 12 }}>No daily data.</div>}
              </div>
              <div className="surface" style={{ padding: '12px 14px', display: 'flex', flexDirection: 'column', gap: 8 }}>
                <div style={{ fontSize: 13, fontWeight: 500 }}>By service</div>
                {top.map(([svc, amt]) => (
                  <div key={svc} style={{ display: 'grid', gridTemplateColumns: 'minmax(0, 1fr) 70px', gap: 10, alignItems: 'center' }}>
                    <div style={{ minWidth: 0 }}>
                      <div style={{ fontSize: 12, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={svc}>{svc}</div>
                      <div style={{ height: 5, borderRadius: 3, marginTop: 3, background: 'color-mix(in srgb, var(--color-text) 8%, transparent)' }}>
                        <div style={{ height: '100%', width: `${(amt / maxSvc) * 100}%`, borderRadius: 3, background: 'var(--color-accent)' }} />
                      </div>
                    </div>
                    <span className="mono" style={{ fontSize: 12, textAlign: 'right' }}>{money(amt)}</span>
                  </div>
                ))}
                {services.length > top.length && <div className="muted" style={{ fontSize: 11.5 }}>+ {services.length - top.length} more services</div>}
              </div>
            </div>
          </>
        )}
      </Section>

      {loose.length > 0 && (
        <div style={{ display: 'flex', gap: 10, alignItems: 'center', padding: '10px 14px', borderRadius: 'var(--radius-md)', border: '1px solid color-mix(in srgb, var(--st-warn) 45%, transparent)', background: 'color-mix(in srgb, var(--st-warn) 8%, transparent)', fontSize: 12.5 }}>
          <Icon name="ph-warning" style={{ color: 'var(--st-warn)' }} />
          <span>{loose.length} Elastic IP{loose.length > 1 ? 's are' : ' is'} not attached to anything — AWS bills idle Elastic IPs.</span>
        </div>
      )}

      <Inventory title="EC2 instances" rows={inv.ec2 || []} empty="No EC2 instances in this region."
        cols={[['Name', r => r.name], ['ID', r => mono(r.id, true)], ['Type', r => mono(r.instance_type)],
          ['State', r => statePill(r.state === 'running', r.state)], ['Private IP', r => mono(r.private_ip, true)],
          ['Public IP', r => mono(r.public_ip, true)], ['Zone', r => mono(r.availability_zone, true)]]} />
      <Inventory title="RDS databases" rows={inv.rds || []} empty="No RDS databases in this region."
        cols={[['ID', r => mono(r.id)], ['Engine', r => `${r.engine} ${r.engine_version || ''}`], ['Class', r => mono(r.instance_class)],
          ['Status', r => statePill(r.is_healthy, r.status)], ['Storage', r => `${r.allocated_storage} GB`], ['Multi-AZ', r => (r.multi_az ? 'yes' : 'no')]]} />
      <Inventory title="Load balancers" rows={inv.load_balancers || []} empty="No load balancers in this region."
        cols={[['Name', r => r.name], ['Type', r => r.type], ['State', r => statePill(r.is_healthy, r.state)],
          ['Healthy targets', r => `${r.healthy_targets ?? '—'} / ${r.total_targets ?? '—'}`], ['DNS', r => mono(r.dns_name, true)]]} />
      <Inventory title="Elastic IPs" rows={inv.elastic_ips || []} empty="No Elastic IPs in this region."
        cols={[['IP', r => mono(r.public_ip)], ['Name', r => r.name], ['Attached to', r => (r.is_attached ? mono(r.instance_id || r.network_interface_id) : <Pill t="warn" label="not attached" />)]]} />
    </div>
  )
}
