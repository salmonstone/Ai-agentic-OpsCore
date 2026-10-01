// Small SVG charts — theme tokens for every colour, one scale per chart,
// labels only at values the data actually reaches. No chart library.
const W = 600, H = 170, PAD = { l: 38, r: 10, t: 12, b: 24 }
const IW = W - PAD.l - PAD.r, IH = H - PAD.t - PAD.b

function niceMax(v) {
  if (v <= 0) return 1
  const p = 10 ** Math.floor(Math.log10(v))
  const n = v / p
  return (n <= 1 ? 1 : n <= 2 ? 2 : n <= 5 ? 5 : 10) * p
}

function Axis({ max, fmt, integer }) {
  // counts never get fractional ticks: 0/1 for max 1, 0/1/2 for max 2, …
  const ticks = integer && max <= 2 ? Array.from({ length: max + 1 }, (_, i) => i)
    : integer ? [0, Math.round(max / 2), max] : [0, max / 2, max]
  return (
    <g>
      {ticks.map(t => {
        const y = PAD.t + IH - (t / max) * IH
        return (
          <g key={t}>
            <line x1={PAD.l} x2={W - PAD.r} y1={y} y2={y} stroke="var(--color-divider)" strokeWidth="1" strokeDasharray={t ? '2 4' : undefined} />
            <text x={PAD.l - 6} y={y + 3.5} textAnchor="end" fontSize="10" fill="var(--muted)" fontFamily="var(--font-mono)">{fmt(t)}</text>
          </g>
        )
      })}
    </g>
  )
}

function XLabels({ labels, every }) {
  const bw = IW / labels.length
  const last = labels.length - 1
  // label every `every`th day, plus the last day unless it would sit on top of the previous label
  const show = i => i % every === 0 || (i === last && last % every >= Math.ceil(every / 2))
  return labels.map((l, i) => show(i) && (
    <text key={i} x={PAD.l + i * bw + bw / 2} y={H - 7} textAnchor="middle" fontSize="10" fill="var(--muted)" fontFamily="var(--font-mono)">{l}</text>
  ))
}

/** Daily bars, e.g. heals per day. */
export function DailyBars({ labels, data, color = 'var(--color-accent)', unit = '', empty = 'Nothing in this period.' }) {
  const total = data.reduce((a, b) => a + b, 0)
  if (!total) {
    return <div className="muted" style={{ display: 'grid', placeItems: 'center', minHeight: 140, fontSize: 12.5 }}>{empty}</div>
  }
  const max = Math.max(1, Math.ceil(niceMax(Math.max(...data))))
  const bw = IW / data.length
  const last = data.length - 1
  return (
    <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label={`${total} total over ${data.length} days`} style={{ width: '100%', height: 'auto', display: 'block' }}>
      <Axis max={max} fmt={v => v} integer />
      {data.map((v, i) => {
        const h = (v / max) * IH
        return (
          <rect key={i} x={PAD.l + i * bw + bw * 0.18} y={PAD.t + IH - h} width={bw * 0.64} height={Math.max(h, v ? 1.5 : 0)} rx="1.5"
            fill={color} opacity={i === last ? 1 : 0.55}>
            <title>{`${labels[i]}: ${v}${unit}`}</title>
          </rect>
        )
      })}
      <XLabels labels={labels} every={7} />
    </svg>
  )
}

/** Daily spend with the average line and statistical spike days marked. */
export function SpendChart({ daily, mean, anomalies = [] }) {
  const vals = daily.map(d => d.amount)
  const max = niceMax(Math.max(...vals, mean || 0))
  const bw = IW / daily.length
  const spikes = new Set(anomalies)
  const my = PAD.t + IH - ((mean || 0) / max) * IH
  return (
    <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label={`Daily AWS spend, average $${(mean || 0).toFixed(2)}`} style={{ width: '100%', height: 'auto', display: 'block' }}>
      <Axis max={max} fmt={v => `$${v % 1 ? v.toFixed(1) : v}`} />
      {daily.map((d, i) => {
        const h = (d.amount / max) * IH
        const spike = spikes.has(d.date)
        return (
          <rect key={d.date} x={PAD.l + i * bw + bw * 0.18} y={PAD.t + IH - h} width={bw * 0.64} height={h} rx="1.5"
            fill={spike ? 'var(--st-warn)' : 'var(--color-accent)'} opacity={spike ? 1 : 0.5}>
            <title>{`${d.date}: $${d.amount.toFixed(2)}${spike ? ' — spike' : ''}`}</title>
          </rect>
        )
      })}
      {mean > 0 && (
        <g>
          <line x1={PAD.l} x2={W - PAD.r} y1={my} y2={my} stroke="var(--color-text)" strokeOpacity="0.55" strokeDasharray="4 4" />
          <text x={W - PAD.r} y={my - 4} textAnchor="end" fontSize="10" fill="var(--muted)" fontFamily="var(--font-mono)">avg ${mean.toFixed(2)}</text>
        </g>
      )}
      <XLabels labels={daily.map(d => d.date.slice(5))} every={7} />
    </svg>
  )
}

/** Tiny trend line for tiles. */
export function Sparkline({ data, color = 'var(--color-accent)', w = 90, h = 24 }) {
  if (!data?.length) return null
  const max = Math.max(...data), min = Math.min(...data)
  const span = max - min || 1
  const pts = data.map((v, i) => [(i / (data.length - 1 || 1)) * (w - 4) + 2, h - 3 - ((v - min) / span) * (h - 6)])
  const [lx, ly] = pts[pts.length - 1]
  return (
    <svg width={w} height={h} viewBox={`0 0 ${w} ${h}`} aria-hidden="true" style={{ display: 'block' }}>
      <polyline points={pts.map(p => p.join(',')).join(' ')} fill="none" stroke={color} strokeWidth="1.5" strokeLinejoin="round" strokeLinecap="round" opacity="0.8" />
      <circle cx={lx} cy={ly} r="2.5" fill={color} />
    </svg>
  )
}
