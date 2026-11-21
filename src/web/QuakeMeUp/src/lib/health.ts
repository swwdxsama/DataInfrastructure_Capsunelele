import type { SystemStatus } from '../types'
import { formatElapsed } from './format'

export type Health = 'online' | 'degraded' | 'offline' | 'unknown'

export interface HealthReport {
  state: Health
  detail: string
}

export const healthText: Record<Health, string> = {
  online: 'Online',
  degraded: 'Degradat',
  offline: 'Offline',
  unknown: 'Necunoscut',
}

/** After this long without a successful response the API counts as offline. */
const API_OFFLINE_AFTER_MS = 15_000
/** Newest stored reading older than this, with devices online, means writes are lagging. */
const STALE_WRITE_MS = 30_000
/** Device-to-database latency (p95) above this counts as degraded. */
const SLOW_INGEST_MS = 2_000

interface ApiInput {
  apiConfigured: boolean
  hasData: boolean
  error: Error | null
  failingSince: number | null
  now: number
}

/** Backend/API state, from the dashboard's own polling. */
export function apiHealth({ apiConfigured, hasData, error, failingSince, now }: ApiInput): HealthReport {
  if (!apiConfigured) return { state: 'unknown', detail: 'VITE_EVENTS_URL nu este setat' }
  if (error && failingSince) {
    const failingFor = now - failingSince
    if (!hasData || failingFor >= API_OFFLINE_AFTER_MS) {
      return { state: 'offline', detail: `fără răspuns de ${formatElapsed(failingSince, now)}` }
    }
    return { state: 'degraded', detail: 'ultima cerere a eșuat, reîncercăm' }
  }
  if (!hasData) return { state: 'unknown', detail: 'se conectează' }
  return { state: 'online', detail: 'răspunde normal' }
}

interface DbInput {
  status: SystemStatus | null
  api: HealthReport
  /** Server clock (ms) of the snapshot. */
  serverNow: number | null
  /** Newest `receivedAt` across all devices (ms), null when nothing was stored yet. */
  lastWriteAt: number | null
}

/**
 * Database state, deduced: there is no health endpoint for DynamoDB, so this
 * reads the signals the API already returns. /status answering means reads
 * work; fresh `receivedAt` values mean writes work.
 */
export function dbHealth({ status, api, serverNow, lastWriteAt }: DbInput): HealthReport {
  if (api.state === 'offline' || !status || serverNow === null) {
    return { state: 'unknown', detail: 'nu poate fi verificată fără API' }
  }
  if (status.devicesOnline > 0) {
    if (lastWriteAt === null) return { state: 'degraded', detail: 'nicio citire stocată încă' }
    if (serverNow - lastWriteAt > STALE_WRITE_MS) {
      return { state: 'degraded', detail: `ultima scriere acum ${formatElapsed(lastWriteAt, serverNow)}` }
    }
  }
  if (status.latencyP95Ms !== null && status.latencyP95Ms > SLOW_INGEST_MS) {
    return { state: 'degraded', detail: `scrieri lente, p95 ${Math.round(status.latencyP95Ms)} ms` }
  }
  if (status.devicesOnline === 0) return { state: 'online', detail: 'citirile răspund; niciun senzor nu scrie acum' }
  return { state: 'online', detail: 'citiri și scrieri la zi' }
}

/** Sensor network: none online is offline, under half online is degraded. */
export function devicesHealth(status: SystemStatus | null): HealthReport {
  if (!status) return { state: 'unknown', detail: '' }
  const { devicesOnline: on, devicesTotal: total } = status
  if (total === 0) return { state: 'unknown', detail: 'niciun dispozitiv înregistrat' }
  if (on === 0) return { state: 'offline', detail: 'niciun senzor nu transmite' }
  if (on < total / 2) return { state: 'degraded', detail: `${total - on} offline` }
  return { state: 'online', detail: on < total ? `${total - on} offline` : 'toate transmit' }
}
