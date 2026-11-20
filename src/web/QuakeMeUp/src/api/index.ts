import { createHttpApi } from './http'
import { ApiError, type DashboardApi } from './types'

export type { DashboardApi, HistoryQuery } from './types'
export { ApiError } from './types'

const baseUrl = import.meta.env.VITE_API_URL?.trim() || null

/** False until VITE_API_URL is set; the dashboard then shows its empty layout. */
export const apiConfigured = baseUrl !== null

const notConfigured = () =>
  Promise.reject(new ApiError('API-ul nu este configurat. Setează VITE_API_URL în .env.local.'))

/** The single data access point used by the UI. */
export const api: DashboardApi = baseUrl
  ? createHttpApi(baseUrl)
  : { getSnapshot: notConfigured, getAccelHistory: notConfigured }
