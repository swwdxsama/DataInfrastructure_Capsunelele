import { ACCEL_UNIT } from '../config'
import { formatDate, formatDuration, formatNumber, formatTime } from '../lib/format'
import type { Event } from '../types'
import './QuakeList.css'

interface Props {
  events: Event[]
  serverNow: number
}

export function QuakeList({ events, serverNow }: Props) {
  if (events.length === 0) {
    return <p className="empty">Rețeaua nu a confirmat încă niciun cutremur.</p>
  }
  const ordered = events.toSorted(
    (a, b) =>
      Number(b.status === 'active') - Number(a.status === 'active') ||
      b.startedAt.localeCompare(a.startedAt),
  )
  return (
    <div className="table-wrap">
      <table className="table quake-list">
        <thead>
          <tr>
            <th scope="col">Început (UTC)</th>
            <th scope="col">Zonă</th>
            <th scope="col" className="num">
              Durată
            </th>
            <th scope="col" className="num">
              Senzori
            </th>
            <th scope="col" className="num">
              Maxim ({ACCEL_UNIT})
            </th>
          </tr>
        </thead>
        <tbody>
          {ordered.map((e) => {
            const active = e.status === 'active'
            const end = e.endedAt ? Date.parse(e.endedAt) : serverNow
            return (
              <tr key={e.id} data-active={active || undefined}>
                <td>
                  <span className="quake-list-date">{formatDate(e.startedAt)}</span>{' '}
                  {formatTime(e.startedAt)}
                  {active && <span className="quake-list-live">În desfășurare</span>}
                </td>
                <td>{e.region ?? <span className="secondary">nedeterminată</span>}</td>
                <td className="num">{formatDuration(end - Date.parse(e.startedAt))}</td>
                <td className="num">{e.deviceIds.length}</td>
                <td className="num quake-list-peak">{formatNumber(e.peakAccel, 2)}</td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}
