import type { ReactNode } from 'react'
import { POLL_INTERVAL_MS } from '../config'
import { useNow } from '../hooks/useNow'
import type { DashboardView } from '../lib/derive'
import { formatAgo, formatElapsed, formatNumber, formatTime } from '../lib/format'
import { apiHealth, dbHealth, devicesHealth, type HealthReport } from '../lib/health'
import type { SystemStatus as Status } from '../types'
import { HealthBadge } from './StatusBadge'
import './SystemStatus.css'

interface Props {
  status: Status | null
  view: DashboardView | null
  apiConfigured: boolean
  /** Polling state of the snapshot. */
  updatedAt: number | null
  failingSince: number | null
  error: Error | null
}

const NONE = '–'

/** Requirement 3: is the pipeline healthy, and how fresh is what we see? */
export function SystemStatus({ status, view, apiConfigured, updatedAt, failingSince, error }: Props) {
  // Re-evaluated every second, so "offline after 15 s" flips without waiting for the next poll.
  const now = useNow(1000)
  const api = apiHealth({ apiConfigured, hasData: status !== null, error, failingSince, now })
  const db = dbHealth({ status, api, serverNow: view?.serverNow ?? null, lastWriteAt: view?.lastReceivedAt ?? null })
  const devices = devicesHealth(status)

  return (
    <section className="system" aria-labelledby="h-system">
      <h2 id="h-system" className="visually-hidden">
        Starea sistemului
      </h2>
      <dl className="system-items">
        <Item label="Senzori online" report={devices}>
          {status ? (
            <>
              <strong>{status.devicesOnline}</strong>
              <span className="system-of"> / {status.devicesTotal}</span>
              {status.devicesArmed != null && status.devicesUnarmed != null && (
                <span className="system-arming">
                  <span data-arm="armed">{status.devicesArmed} armate</span>
                  {' · '}
                  <span data-arm="unarmed">{status.devicesUnarmed} nearmate</span>
                </span>
              )}
            </>
          ) : (
            <strong>{NONE}</strong>
          )}
        </Item>

        <Item label="API backend" report={api} />

        <Item label="Bază de date (DynamoDB)" report={db} note="dedus din răspunsul API și ora ultimei citiri stocate" />

        <Item label="Ultimul răspuns API">
          <strong>{updatedAt ? formatAgo(updatedAt, now) : NONE}</strong>
          {updatedAt && <span className="system-detail">
              la {formatTime(updatedAt)} UTC, la fiecare {POLL_INTERVAL_MS / 1000} s
            </span>}
        </Item>

        <Item label="Ingestie">
          {status ? (
            <>
              <strong>{formatNumber(status.messagesPerSecond, 1)}</strong>
              <span className="system-of"> msg/s</span>
              <span className="system-detail">
                {status.latencyP50Ms !== null && status.latencyP95Ms !== null
                  ? `latență ${Math.round(status.latencyP50Ms)} / ${Math.round(status.latencyP95Ms)} ms (p50 / p95)`
                  : 'latență nemăsurată de API'}
              </span>
            </>
          ) : (
            <strong>{NONE}</strong>
          )}
        </Item>
      </dl>

      {!apiConfigured && (
        <p className="system-notice" data-kind="setup">
          API-ul nu este configurat. Setează VITE_EVENTS_URL în .env.local ca să vezi date.
        </p>
      )}
      {apiConfigured && error && failingSince && (
        <p className="system-notice" data-kind="error" role="alert">
          Nu primim date noi de {formatElapsed(failingSince, now)}. {error.message} Reîncercăm automat
          {status ? '; valorile de mai jos sunt ultimele primite.' : '.'}
        </p>
      )}
    </section>
  )
}

function Item({
  label,
  report,
  note,
  children,
}: {
  label: string
  report?: HealthReport
  note?: string
  children?: ReactNode
}) {
  return (
    <div className="system-item">
      <dt>
        {label}
        {note && (
          <span className="system-note" title={note}>
            {' '}
            (dedus)
          </span>
        )}
      </dt>
      <dd>
        {children}
        {report && (
          <span className="system-health">
            <HealthBadge health={report.state} />
            {report.detail && <span className="system-detail">{report.detail}</span>}
          </span>
        )}
      </dd>
    </div>
  )
}
