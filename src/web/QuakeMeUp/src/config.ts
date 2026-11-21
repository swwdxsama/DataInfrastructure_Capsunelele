/** Unit used everywhere acceleration is displayed. The API sends m/s². */
export const ACCEL_UNIT = 'm/s²'

export const POLL_INTERVAL_MS = 3000

/** Length of the acceleration charts. */
export const HISTORY_WINDOW_S = 300

/** Data age after which the latest values are flagged as stale. */
export const STALE_AFTER_MS = 15_000

export type HistorySource = 'live' | 'api' | 'mock'

export interface HistoryRange {
  id: '5m' | '1h' | '24h' | '7d'
  label: string
  windowSec: number
  /** Chart resolution: the max of each bucket is drawn. */
  bucketSec: number
  /**
   * live: the network history already in the 3-second snapshot.
   * api:  GET /telemetry/history?windowSec=…, aggregated in the browser.
   * mock: src/api/mock.ts, until the backend offers aggregated history.
   */
  source: HistorySource
}

export const HISTORY_RANGES: readonly HistoryRange[] = [
  { id: '5m', label: '5 min', windowSec: 300, bucketSec: 1, source: 'live' },
  { id: '1h', label: '1 h', windowSec: 3600, bucketSec: 10, source: 'api' },
  { id: '24h', label: '24 h', windowSec: 86_400, bucketSec: 300, source: 'mock' },
  { id: '7d', label: '7 zile', windowSec: 604_800, bucketSec: 1800, source: 'mock' },
]

/** How often the 1-hour history is refetched. */
export const HOUR_HISTORY_POLL_MS = 15_000

