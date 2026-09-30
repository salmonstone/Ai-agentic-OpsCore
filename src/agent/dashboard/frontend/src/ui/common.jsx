import { tone } from '../lib/tone'

export const Icon = ({ name, size, style, className, ...rest }) => (
  <i className={`ph ${name}${className ? ` ${className}` : ''}`} style={{ fontSize: size, ...style }} aria-hidden="true" {...rest} />
)

/** Status pill: <Pill t="crit" label="CrashLoopBackOff" mono /> */
export function Pill({ t, label, icon, mono, style }) {
  const x = tone(t)
  return (
    <span style={{
      display: 'inline-flex', alignItems: 'center', gap: 4, padding: '1px 7px', borderRadius: 6,
      fontSize: 11, fontWeight: 500, whiteSpace: 'nowrap', fontFamily: mono ? 'var(--font-mono)' : undefined,
      color: x.c, background: x.tint, border: `1px ${x.bs} ${x.line}`, ...style,
    }}>
      {icon !== false && <Icon name={icon || x.icon} />}{label}
    </span>
  )
}

export function SectionHead({ title, note, children }) {
  return (
    <div style={{ display: 'flex', alignItems: 'baseline', gap: 10, flexWrap: 'wrap' }}>
      <h2 className="kicker">{title}</h2>
      {note && <span className="muted" style={{ fontSize: 12 }}>{note}</span>}
      {children && <div style={{ marginLeft: 'auto', display: 'flex', gap: 8, alignItems: 'center' }}>{children}</div>}
    </div>
  )
}

export function Section({ children, gap = 10 }) {
  return <section style={{ display: 'flex', flexDirection: 'column', gap }}>{children}</section>
}

/** Checked, and it's fine. */
export function OkEmpty({ title, sub }) {
  return (
    <div className="surface" style={{ display: 'flex', alignItems: 'center', gap: 12, padding: 16 }}>
      <Icon name="ph-check-circle" size={22} style={{ color: 'var(--st-ok)' }} />
      <div><div style={{ fontSize: 13.5, fontWeight: 500 }}>{title}</div>{sub && <div className="muted" style={{ fontSize: 12 }}>{sub}</div>}</div>
    </div>
  )
}

/** Couldn't check. Deliberately looks nothing like OkEmpty. */
export function NoData({ label, note, error, onRetry }) {
  return (
    <div className="nodata" role="status">
      {label && <div className="kicker">{label}</div>}
      <div style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 15, fontWeight: 500, color: 'var(--st-unk)' }}>
        <Icon name="ph-question" size={18} />No data
      </div>
      <div className="muted" style={{ fontSize: 12 }}>{note}</div>
      {error && <code className="term" style={{ fontSize: 11.5, padding: '6px 9px' }}>{error}</code>}
      {onRetry && <button className="btn btn-secondary" style={{ alignSelf: 'flex-start' }} onClick={onRetry}><Icon name="ph-arrow-clockwise" />Retry</button>}
    </div>
  )
}

export function SkeletonRows({ rows = 4 }) {
  return (
    <div className="surface" aria-busy="true" style={{ display: 'flex', flexDirection: 'column', gap: 12, padding: 14 }}>
      {Array.from({ length: rows }).map((_, i) => (
        <div key={i} style={{ display: 'flex', gap: 14 }}>
          {[14, 32, 12, 8].map((w, j) => <div key={j} className="skel" style={{ width: `${w}%`, height: 10 }} />)}
        </div>
      ))}
    </div>
  )
}

export function Bar({ pct, color }) {
  const w = Math.max(0, Math.min(100, pct ?? 0))
  return (
    <div style={{ flex: 1, maxWidth: 120, height: 6, borderRadius: 3, background: 'color-mix(in srgb, var(--color-text) 9%, transparent)', overflow: 'hidden' }}>
      <div style={{ height: '100%', width: `${w}%`, background: color, borderRadius: 3 }} />
    </div>
  )
}

export function TableCard({ children }) {
  return <div className="surface" style={{ padding: '2px 8px', overflowX: 'auto' }}>{children}</div>
}
