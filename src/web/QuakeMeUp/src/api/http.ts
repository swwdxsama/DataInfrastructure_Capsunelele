import { HISTORY_WINDOW_S } from '../config'
import type {
  AccelPoint,
  Device,
  Event,
  SystemStatus,
  Telemetry,
  Trigger,
} from '../types'
import { ApiError, type DashboardApi, type HistoryQuery } from './types'

const REQUEST_TIMEOUT_MS = 8000

/**
 * REST client for the backend behind API Gateway.
 *
 * Endpoints:
 *   GET /devices
 *   GET /telemetry/latest
 *   GET /telemetry/history?windowSec=300[&deviceId=]
 *   GET /triggers?limit=50
 *   GET /events?limit=20
 *   GET /status
 */
export function createHttpApi(baseUrl: string): DashboardApi {
  const root = baseUrl.replace(/\/+$/, '')

  async function get<T>(path: string, signal: AbortSignal): Promise<T> {
    let res: Response
    try {
      res = await fetch(`${root}${path}`, {
        headers: { Accept: 'application/json' },
        signal: AbortSignal.any([signal, AbortSignal.timeout(REQUEST_TIMEOUT_MS)]),
      })
    } catch (err) {
      if (signal.aborted) throw err
      if (err instanceof DOMException && err.name === 'TimeoutError') {
        throw new ApiError(`API-ul nu a răspuns în ${REQUEST_TIMEOUT_MS / 1000} secunde (${path}).`)
      }
      throw new ApiError(`API-ul nu poate fi contactat (${path}).`)
    }
    if (!res.ok) {
      throw new ApiError(`API-ul a răspuns cu ${res.status} pentru ${path}.`, res.status)
    }
    return (await res.json()) as T
  }

  function historyPath({ deviceId, windowSec }: HistoryQuery) {
    const params = new URLSearchParams({ windowSec: String(windowSec) })
    if (deviceId) params.set('deviceId', deviceId)
    return `/telemetry/history?${params}`
  }

  return {
    async getSnapshot(signal) {
      // Independent requests, fired together.
      const [devices, latest, history, triggers, events, status] = await Promise.all([
        get<Device[]>('/devices', signal),
        get<Telemetry[]>('/telemetry/latest', signal),
        get<AccelPoint[]>(historyPath({ windowSec: HISTORY_WINDOW_S }), signal),
        get<Trigger[]>('/triggers?limit=50', signal),
        get<Event[]>('/events?limit=20', signal),
        get<SystemStatus>('/status', signal),
      ])
      return { devices, latest, history, triggers, events, status }
    },

    getAccelHistory(query, signal) {
      return get<AccelPoint[]>(historyPath(query), signal)
    },
  }
}
