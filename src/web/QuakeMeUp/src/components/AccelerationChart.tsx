import {
  CartesianGrid,
  Line,
  LineChart,
  ReferenceArea,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
  type TooltipContentProps,
} from 'recharts'
import type { NameType, ValueType } from 'recharts/types/component/DefaultTooltipContent'
import { ACCEL_UNIT, HISTORY_WINDOW_S } from '../config'
import { formatAccel, formatNumber, formatShortTime, formatTime } from '../lib/format'
import { palette } from '../lib/palette'
import type { AccelPoint, Event } from '../types'
import './AccelerationChart.css'

interface Props {
  points: AccelPoint[]
  threshold: number
  /** Right edge of the time axis (server clock, ms). */
  end: number
  /** Earthquakes to shade on the chart. */
  events?: Event[]
  height: number
  /** Describes the chart for screen readers. */
  label: string
}

interface Row {
  t: number
  v: number
}

const MINUTE = 60_000

export function AccelerationChart({ points, threshold, end, events = [], height, label }: Props) {
  const start = end - HISTORY_WINDOW_S * 1000
  const rows: Row[] = points.map((p) => ({ t: Date.parse(p.t), v: p.peak }))

  let max = 0
  for (const r of rows) if (r.v > max) max = r.v
  // Keep the threshold in the lower half when quiet, grow with the signal.
  const yMax = Math.max(threshold * 2, Math.ceil(max * 1.1 * 10) / 10)

  const ticks: number[] = []
  for (let t = Math.ceil(start / MINUTE) * MINUTE; t <= end; t += MINUTE) ticks.push(t)

  const spans = events.flatMap((e) => {
    const from = Math.max(start, Date.parse(e.startedAt))
    const to = Math.min(end, e.endedAt ? Date.parse(e.endedAt) : end)
    return to > from ? [{ id: e.id, from, to }] : []
  })

  const above = rows.filter((r) => r.v >= threshold).length

  return (
    <figure className="accel-chart" style={{ height }}>
      <figcaption className="visually-hidden">
        {label}. Prag {formatAccel(threshold)}. {above > 0 ? `${above} secunde peste prag.` : 'Nicio secundă peste prag.'}
      </figcaption>
      <ResponsiveContainer width="100%" height="100%">
        <LineChart data={rows} margin={{ top: 8, right: 8, bottom: 0, left: 0 }}>
          <CartesianGrid stroke={palette.grid} strokeWidth={1} />
          {spans.map((s) => (
            <ReferenceArea
              key={s.id}
              x1={s.from}
              x2={s.to}
              fill={palette.alert}
              fillOpacity={0.09}
              stroke="none"
            />
          ))}
          <XAxis
            dataKey="t"
            type="number"
            domain={[start, end]}
            ticks={ticks}
            tickFormatter={(t: number) => formatShortTime(t)}
            stroke={palette.rule}
            tick={{ fill: palette.ink2, fontSize: 14 }}
            tickLine={false}
            allowDataOverflow
          />
          <YAxis
            domain={[0, yMax]}
            tickFormatter={(v: number) => formatNumber(v, 1)}
            stroke={palette.rule}
            tick={{ fill: palette.ink2, fontSize: 14 }}
            tickLine={false}
            width={44}
            allowDataOverflow
          />
          <ReferenceLine
            y={threshold}
            stroke={palette.threshold}
            strokeWidth={2}
            strokeDasharray="6 4"
            label={{
              value: `prag ${formatNumber(threshold, 2)}`,
              position: 'insideTopRight',
              fill: palette.threshold,
              fontSize: 14,
              fontWeight: 700,
            }}
          />
          <Tooltip
            content={ChartTooltip}
            isAnimationActive={false}
            cursor={{ stroke: palette.ink3, strokeWidth: 1 }}
          />
          <Line
            dataKey="v"
            type="linear"
            stroke={palette.trace}
            strokeWidth={1.75}
            dot={false}
            activeDot={{ r: 4, fill: palette.trace, stroke: palette.surface, strokeWidth: 2 }}
            isAnimationActive={false}
          />
        </LineChart>
      </ResponsiveContainer>
    </figure>
  )
}

function ChartTooltip({ active, payload }: TooltipContentProps<ValueType, NameType>) {
  const row = payload?.[0]?.payload as Row | undefined
  if (!active || !row) return null
  return (
    <div className="accel-chart-tip">
      <span>{formatTime(row.t)} UTC</span>
      <strong>
        {formatNumber(row.v, 2)} {ACCEL_UNIT}
      </strong>
    </div>
  )
}
