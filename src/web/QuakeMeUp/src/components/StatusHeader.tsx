import { useNow } from '../hooks/useNow'
import { formatAccel, formatAgo, formatTime } from '../lib/format'
import type { Event } from '../types'
import './StatusHeader.css'

interface Props {
  activeEvent: Event | null
  /** Shown when no earthquake is active. */
  calmText: string
}

function plainCount(n: number) {
  return n === 1 ? 'Un senzor a depășit pragul' : `${n} senzori au depășit pragul`
}

export function StatusHeader({ activeEvent, calmText }: Props) {
  const alert = activeEvent !== null
  return (
    <header className="status-header" data-alert={alert || undefined}>
      <div className="status-header-brand">
        <Mark />
        <div>
          <p className="status-header-name">QuakeMeUp</p>
          <p className="status-header-sub">Rețeaua de senzori seismici</p>
        </div>
      </div>

      <div className="status-header-state" role="status" aria-live="assertive">
        {activeEvent ? (
          <ActiveEvent event={activeEvent} />
        ) : (
          <p className="status-header-calm">{calmText}</p>
        )}
      </div>

      <UtcClock />
    </header>
  )
}

function ActiveEvent({ event }: { event: Event }) {
  const now = useNow(1000)
  const parts = [
    event.region ?? 'Zonă nedeterminată',
    plainCount(event.deviceIds.length),
    `Accelerație maximă ${formatAccel(event.peakAccel)}`,
    `A început la ${formatTime(event.startedAt)} UTC, ${formatAgo(event.startedAt, now)}`,
  ]
  return (
    <div className="status-header-alert">
      <p className="status-header-alert-title">Cutremur detectat</p>
      <p className="status-header-alert-detail">{parts.join('. ')}.</p>
    </div>
  )
}

function UtcClock() {
  const now = useNow(1000)
  return (
    <p className="status-header-clock">
      <time dateTime={new Date(now).toISOString()}>{formatTime(now)}</time>
      <span className="status-header-clock-zone">UTC</span>
    </p>
  )
}

/** A short seismogram trace. */
function Mark() {
  return (
    <svg className="status-header-mark" viewBox="0 0 32 32" aria-hidden="true">
      <rect x="0.75" y="0.75" width="30.5" height="30.5" rx="3" fill="none" stroke="currentColor" strokeWidth="1.5" />
      <polyline
        points="4,17 9,17 11,13 13,21 15,7 17,26 19,11 21,19 23,15 25,17 28,17"
        fill="none"
        stroke="currentColor"
        strokeWidth="2"
        strokeLinejoin="round"
        strokeLinecap="round"
      />
    </svg>
  )
}
