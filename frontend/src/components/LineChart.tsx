import { money, shortDate } from '../format'

type Point = { label: string; value: number | null; title?: string }

/** Minimal blueprint-style line chart (usage over statement periods), matching the design prototype. `extremes` marks
 * and labels the highest and lowest points. */
export function LineChart({ points, height = 180, width = 640, fit = false, extremes = false, format = (v: number) => v.toLocaleString() }: {
  points: Point[]; height?: number; width?: number; fit?: boolean; extremes?: boolean; format?: (v: number) => string
}) {
  const vals = points.map((p) => p.value).filter((v): v is number => v !== null)
  if (vals.length === 0) return <div className="text-muted small">No usage values yet.</div>
  const w = width
  const base = height - 20
  const hi = Math.max(...vals)
  const lo = fit ? Math.min(...vals) : 0
  // Extra headroom keeps the high and low labels inside the plot.
  const pad = fit ? (hi - lo) * (extremes ? 0.25 : 0.1) || Math.abs(hi) * 0.05 || 1 : 0
  const max = fit ? hi + pad : hi * 1.15 || 1
  const min = fit ? lo - pad : 0
  const avg = vals.reduce((a, b) => a + b, 0) / vals.length
  const step = points.length > 1 ? (w - 40) / (points.length - 1) : 0
  const x = (i: number) => 20 + i * step
  const y = (v: number) => base - ((v - min) / (max - min)) * (base - 20)
  const dots = points.map((p, i) => ({ ...p, x: x(i), y: p.value === null ? null : y(p.value) }))
  const line = dots.filter((d) => d.y !== null).map((d) => `${d.x},${d.y}`).join(' ')
  const every = Math.ceil(points.length / 12)
  // The latest high and low; nothing to mark on a flat line.
  const at = (v: number) => dots.findLastIndex((d) => d.value === v)
  const marks = extremes && Math.max(...vals) !== Math.min(...vals)
    ? [{ i: at(Math.max(...vals)), k: 'High', above: true }, { i: at(Math.min(...vals)), k: 'Low', above: false }] : []
  return (
    <svg viewBox={`0 0 ${w} ${height}`} width="100%" style={{ display: 'block' }}>
      <line x1="0" y1={base} x2={w} y2={base} stroke="var(--color-divider)" />
      <line x1="0" y1={y(avg)} x2={w} y2={y(avg)} stroke="var(--color-accent-500)" strokeDasharray="3 4" />
      <text x={w - 4} y={y(avg) - 4} fontSize="10" textAnchor="end" fill="var(--color-neutral-600)" fontFamily="Barlow">
        avg {format(avg)}
      </text>
      <polyline points={line} fill="none" stroke="var(--color-accent-700)" strokeWidth="1.5" />
      {dots.map((d, i) => (
        <g key={i}>
          {d.y !== null && (
            <circle cx={d.x} cy={d.y} r="3" fill="var(--color-bg)" stroke="var(--color-accent-700)" strokeWidth="1.5">
              <title>{d.title ?? `${d.label}: ${format(d.value!)}`}</title>
            </circle>
          )}
          {i % every === 0 && (
            <text x={d.x} y={height - 4} fontSize="10" textAnchor="middle" fill="var(--color-neutral-600)" fontFamily="Barlow">
              {d.label}
            </text>
          )}
        </g>
      ))}
      {marks.map(({ i, k, above }) => {
        const d = dots[i]
        // Labels sit outside the line, flipping when they'd leave the plot, and turn inward at the edges.
        const ly = above ? (d.y! - 9 < 10 ? d.y! + 16 : d.y! - 9) : (d.y! + 16 > base - 3 ? d.y! - 9 : d.y! + 16)
        const anchor = d.x < 90 ? 'start' : d.x > w - 90 ? 'end' : 'middle'
        return (
          <g key={k}>
            <circle cx={d.x} cy={d.y!} r="3.5" fill="var(--color-accent-700)" stroke="var(--color-bg)" strokeWidth="1">
              <title>{d.title ?? `${d.label}: ${format(d.value!)}`}</title>
            </circle>
            <text x={d.x} y={ly} fontSize="11" textAnchor={anchor} fill="var(--color-neutral-800)" fontFamily="Barlow"
              stroke="var(--color-bg)" strokeWidth="3" paintOrder="stroke" style={{ pointerEvents: 'none' }}>
              <tspan fontWeight="600">{k} {format(d.value!)}</tspan> · {d.label}
            </text>
          </g>
        )
      })}
    </svg>
  )
}

/** Inline balance trend for table rows; liabilities are drawn as negative so "up" always means better. */
export function BalanceSparkline({ points, liability = false, width = 96, height = 24 }: {
  points: { as_of: string; balance: number }[] | undefined; liability?: boolean; width?: number; height?: number
}) {
  if (!points || points.length < 2) return <span className="text-muted small">—</span>
  const vals = points.map((p) => (liability ? -Math.abs(p.balance) : p.balance))
  const lo = Math.min(...vals)
  const hi = Math.max(...vals)
  const span = hi - lo
  const x = (i: number) => 2 + (i / (vals.length - 1)) * (width - 4)
  const y = (v: number) => (span ? height - 3 - ((v - lo) / span) * (height - 6) : height / 2)
  const line = vals.map((v, i) => `${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(' ')
  const first = points[0]
  const last = points[points.length - 1]
  const title = `${shortDate(first.as_of)} ${money(vals[0])} → ${shortDate(last.as_of)} ${money(vals[vals.length - 1])}`
  return (
    <svg viewBox={`0 0 ${width} ${height}`} width={width} height={height} style={{ display: 'block' }} role="img" aria-label={title}>
      <title>{title}</title>
      <polyline points={line} fill="none" stroke="var(--color-accent-700)" strokeWidth="1.25" strokeLinejoin="round" />
      <circle cx={x(vals.length - 1)} cy={y(vals[vals.length - 1])} r="2" fill="var(--color-accent-700)" />
    </svg>
  )
}
