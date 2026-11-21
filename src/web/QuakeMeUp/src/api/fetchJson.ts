import { ApiError } from './types'

const REQUEST_TIMEOUT_MS = 8000

/** GET a JSON document, with a timeout and messages the dashboard can show as-is. */
export async function fetchJson<T>(url: string, signal: AbortSignal, label = url): Promise<T> {
  let res: Response
  try {
    res = await fetch(url, {
      headers: { Accept: 'application/json' },
      signal: AbortSignal.any([signal, AbortSignal.timeout(REQUEST_TIMEOUT_MS)]),
    })
  } catch (err) {
    if (signal.aborted) throw err
    if (err instanceof DOMException && err.name === 'TimeoutError') {
      throw new ApiError(`API-ul nu a răspuns în ${REQUEST_TIMEOUT_MS / 1000} secunde (${label}).`)
    }
    throw new ApiError(`API-ul nu poate fi contactat (${label}).`)
  }
  if (!res.ok) {
    throw new ApiError(`API-ul a răspuns cu ${res.status} pentru ${label}.`, res.status)
  }
  try {
    return (await res.json()) as T
  } catch {
    throw new ApiError(`Răspunsul de la ${label} nu este JSON valid.`, res.status)
  }
}
