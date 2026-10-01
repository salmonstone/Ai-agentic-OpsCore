import { Sparkline } from './charts'
import { Icon } from './common'

function Stat({ icon, value, label, sub, spark }) {
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 14, padding: '14px 16px', background: 'var(--color-surface)', minWidth: 0 }}>
      <div style={{ width: 36, height: 36, borderRadius: 10, display: 'grid', placeItems: 'center', flex: 'none', color: 'var(--color-accent)', background: 'color-mix(in srgb, var(--color-accent) 12%, transparent)' }}>
        <Icon name={icon} size={19} />
      </div>
      <div style={{ minWidth: 0, flex: 1 }}>
        <div style={{ fontSize: 24, fontWeight: 600, letterSpacing: '-0.02em', lineHeight: 1.1, fontVariantNumeric: 'tabular-nums' }}>{value}</div>
        <div style={{ fontSize: 12, fontWeight: 500 }}>{label}</div>
        {sub && <div className="muted" style={{ fontSize: 11 }}>{sub}</div>}
      </div>
      {spark}
    </div>
  )
}

/** What AtlasOS did on its own — the numbers the daemon already records. */
export default function ImpactStrip({ live, chart }) {
  const s = live?.stats
  const heals = chart.data?.data || []
  const heals30 = heals.reduce((a, b) => a + b, 0)
  const fmt = v => (s ? v : '—')
  return (
    <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(220px, 1fr))', gap: 1, borderRadius: 'var(--radius-md)', overflow: 'hidden', background: 'var(--color-divider)', boxShadow: 'var(--shadow-sm)' }}>
      <Stat icon="ph-first-aid-kit" value={fmt(s?.pods_healed_today)} label="fixes today" sub="applied by the daemon, no human" />
      <Stat icon="ph-currency-dollar" value={s ? `$${Math.round(s.cost_saved_month).toLocaleString()}` : '—'} label="saved this month" sub="idle resources removed or resized" />
      <Stat icon="ph-robot" value={fmt(s?.actions_30d)} label="actions in 30 days" sub="healing, scaling, cost" />
      <Stat icon="ph-chart-line-up" value={chart.data ? heals30 : '—'} label="heals in 30 days"
        sub={chart.data ? `busiest day: ${Math.max(...heals, 0)}` : 'no history yet'} spark={<Sparkline data={heals} />} />
    </div>
  )
}
