import type { AccelPoint, DashboardSnapshot } from '../types'

export interface HistoryQuery {
  deviceId?: string
  windowSec: number
}

/** Everything the dashboard reads from the backend. */
export interface DashboardApi {
  getSnapshot(signal: AbortSignal): Promise<DashboardSnapshot>
  getAccelHistory(query: HistoryQuery, signal: AbortSignal): Promise<AccelPoint[]>
}

export class ApiError extends Error {
  readonly status: number | null

  constructor(message: string, status: number | null = null) {
    super(message)
    this.name = 'ApiError'
    this.status = status
  }
}
