import { useNow } from '../hooks/useNow'
import { formatAgo, formatDuration, formatElapsed, formatNumber, formatTime } from '../lib/format'
import type { Event, SystemStatus } from '../types'
import './SystemStrip.css'

interface Props {
  /** Null until the first response from the API. */
  status: SystemStatus | null
  lastEndedEvent: Event | null
  serverNow: number | null
  apiConfigured: boolean
  updatedAt: number | null
  failingSince: number | null
  error: Error | null
}

const NONE = '–'

export function SystemStrip({
  status,
  lastEndedEvent,
  serverNow,
  apiConfigured,
  updatedAt,
  failingSince,
  error,
}: Props) {
  const offline = status ? status.devicesTotal - status.devicesOnline : 0
  return (
    <section className="system-strip" aria-label="Starea sistemului">
      <dl className="system-strip-metrics">
        <div className="system-strip-item">
          <dt>Dispozitive online</dt>
          <dd>
            {status ? (
              <>
                <strong>{status.devicesOnline}</strong> din {status.devicesTotal}
                {offline > 0 && <span className="secondary"> ({offline} offline)</span>}
              </>
            ) : (
              <strong>{NONE}</strong>
            )}
          </dd>
        </div>
        <div className="system-strip-item">
          <dt>Mesaje pe secundă</dt>
          <dd>
            <strong>{status ? formatNumber(status.messagesPerSecond, 1) : NONE}</strong>
          </dd>
        </div>
        <div className="system-strip-item">
          <dt>Latență până la server</dt>
          <dd>
            {status ? (
              <>
                <strong>{Math.round(status.latencyP50Ms)} ms</strong>
                <span className="secondary"> median, {Math.round(status.latencyP95Ms)} ms la p95</span>
              </>
            ) : (
              <strong>{NONE}</strong>
            )}
          </dd>
        </div>
        <div className="system-strip-item">
          <dt>Ultimul cutremur încheiat</dt>
          <dd>
            {lastEndedEvent && serverNow !== null ? (
              <>
                <strong>{formatAgo(lastEndedEvent.startedAt, serverNow)}</strong>
                {lastEndedEvent.endedAt && (
                  <span className="secondary">
                    {' '}
                    a durat {formatDuration(Date.parse(lastEndedEvent.endedAt) - Date.parse(lastEndedEvent.startedAt))}
                  </span>
                )}
              </>
            ) : status ? (
              <span className="secondary">niciunul înregistrat</span>
            ) : (
              <strong>{NONE}</strong>
            )}
          </dd>
        </div>
      </dl>

      <Freshness apiConfigured={apiConfigured} updatedAt={updatedAt} failingSince={failingSince} error={error} />
    </section>
  )
}

function Freshness({
  apiConfigured,
  updatedAt,
  failingSince,
  error,
}: Pick<Props, 'apiConfigured' | 'updatedAt' | 'failingSince' | 'error'>) {
  const now = useNow(1000)
  if (!apiConfigured) {
    return (
      <p className="system-strip-fresh" data-setup>
        API-ul nu este configurat. Setează VITE_API_URL în .env.local ca să vezi date.
      </p>
    )
  }
  if (error && failingSince) {
    return (
      <p className="system-strip-fresh" data-problem role="alert">
        Nu primim date noi de {formatElapsed(failingSince, now)}. {error.message} Reîncercăm automat.
      </p>
    )
  }
  return (
    <p className="system-strip-fresh">
      {updatedAt ? `Actualizat la ${formatTime(updatedAt)} UTC` : 'Se încarcă'}
    </p>
  )
}
