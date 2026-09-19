import {
  Bar,
  BarChart,
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import type { ChartSpec } from '../types'

// Categorical color assignment is BY ENTITY, never by order (the dataviz
// skill's core rule: "Color follows the entity, never its rank") — HDFC
// Life is always slot 1 blue, Axis Max Life always slot 2 orange, in every
// chart across the whole session. Any other series key (e.g. a segment
// breakdown) falls back to the same validated ordering from slot 3 on.
const ENTITY_COLORS: Record<string, string> = {
  hdfc_life: 'var(--series-1)',
  axis_max_life: 'var(--series-2)',
}
const FALLBACK_SLOTS = ['var(--series-3)', 'var(--series-4)']

function colorForSeries(key: string, fallbackIndex: number): string {
  return ENTITY_COLORS[key] ?? FALLBACK_SLOTS[fallbackIndex % FALLBACK_SLOTS.length]
}

const numberFormatter = new Intl.NumberFormat('en-IN', {
  notation: 'compact',
  maximumFractionDigits: 1,
})

function formatValue(v: number): string {
  if (Math.abs(v) < 1000) {
    return Number.isInteger(v) ? String(v) : v.toFixed(2)
  }
  return numberFormatter.format(v)
}

// Recharts' Tooltip formatter type is intentionally loose (string, number,
// or a readonly array of either) — narrow to the number case we actually
// produce and pass everything else through untouched, via `unknown` rather
// than fighting the exact (readonly) array variance.
function formatTooltipValue(value: unknown): string {
  if (typeof value === 'number') return formatValue(value)
  return String(value ?? '')
}

export function ChartRenderer({ spec }: { spec: ChartSpec }) {
  const unknownKeys = spec.series.filter((s) => !(s.key in ENTITY_COLORS)).map((s) => s.key)
  const seriesWithColor = spec.series.map((s) => ({
    ...s,
    color: colorForSeries(s.key, unknownKeys.indexOf(s.key)),
  }))
  const showLegend = seriesWithColor.length > 1

  const commonAxisProps = {
    stroke: 'var(--baseline)',
    tick: { fill: 'var(--text-muted)', fontSize: 12 },
  }

  return (
    <figure className="chart-figure">
      <figcaption className="chart-title">{spec.title}</figcaption>
      <ResponsiveContainer width="100%" height={260}>
        {spec.type === 'line' ? (
          <LineChart data={spec.data} margin={{ top: 8, right: 16, bottom: 8, left: 8 }}>
            <CartesianGrid stroke="var(--gridline)" vertical={false} />
            <XAxis dataKey={spec.x_key} {...commonAxisProps} tickLine={false} />
            <YAxis {...commonAxisProps} tickLine={false} axisLine={false} tickFormatter={formatValue} width={56} />
            <Tooltip
              contentStyle={{
                background: 'var(--surface-1)',
                border: '1px solid var(--border)',
                borderRadius: 8,
                color: 'var(--text-primary)',
                fontSize: 13,
              }}
              labelStyle={{ color: 'var(--text-secondary)' }}
              formatter={formatTooltipValue}
            />
            {showLegend && <Legend wrapperStyle={{ fontSize: 12, color: 'var(--text-secondary)' }} />}
            {seriesWithColor.map((s) => (
              <Line
                key={s.key}
                type="monotone"
                dataKey={s.key}
                name={s.label}
                stroke={s.color}
                strokeWidth={2}
                dot={{ r: 3 }}
                activeDot={{ r: 5 }}
                connectNulls
              />
            ))}
          </LineChart>
        ) : (
          <BarChart data={spec.data} margin={{ top: 8, right: 16, bottom: 8, left: 8 }}>
            <CartesianGrid stroke="var(--gridline)" vertical={false} />
            <XAxis dataKey={spec.x_key} {...commonAxisProps} tickLine={false} />
            <YAxis {...commonAxisProps} tickLine={false} axisLine={false} tickFormatter={formatValue} width={56} />
            <Tooltip
              contentStyle={{
                background: 'var(--surface-1)',
                border: '1px solid var(--border)',
                borderRadius: 8,
                color: 'var(--text-primary)',
                fontSize: 13,
              }}
              labelStyle={{ color: 'var(--text-secondary)' }}
              formatter={formatTooltipValue}
            />
            {showLegend && <Legend wrapperStyle={{ fontSize: 12, color: 'var(--text-secondary)' }} />}
            {seriesWithColor.map((s) => (
              <Bar key={s.key} dataKey={s.key} name={s.label} fill={s.color} radius={[3, 3, 0, 0]} maxBarSize={40} />
            ))}
          </BarChart>
        )}
      </ResponsiveContainer>
    </figure>
  )
}
