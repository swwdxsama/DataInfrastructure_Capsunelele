import {
  GRAVITY_MS2,
  HISTORY_WINDOW_S,
  ONLINE_WITHIN_MS,
  QUAKE_MIN_DEVICES,
  QUAKE_WINDOW_MS,
  READING_BUFFER_MS,
  TRIGGER_THRESHOLD_MS2,
} from '../config'
import type {
  AccelPoint,
  DashboardSnapshot,
  Device,
  Event,
  GeoPoint,
  SystemStatus,
  Telemetry,
  Trigger,
} from '../types'
import { fetchJson } from './fetchJson'
import type { DashboardApi } from './types'

/**
 * Client for the single endpoint the backend has today:
 *
 *   GET /events  →  { "events": [{ "device_id", "timestamp", "value", ... }] }
 *
 * Each item is one raw reading stored by POST /telemetry: `value` is the
 * magnitude of the phone's accelerometer, gravity included (≈ 9.81 at rest).
 * Despite the name, these are not earthquakes.
 *
 * The rest of the dashboard expects devices, triggers and confirmed
 * earthquakes, so they are derived here from the readings:
 * - readings are merged into an in-memory buffer across polls (the endpoint
 *   returns at most 50 items), which is also where history comes from;
 * - acceleration is shown without gravity: | value − g |;
 * - a trigger is a reading at or above TRIGGER_THRESHOLD_MS2;
 * - an earthquake is QUAKE_MIN_DEVICES devices triggering within QUAKE_WINDOW_MS.
 * Thresholds live in src/config.ts.
 */

interface RawLocation {
  lat: number
  lon: number
  accuracy?: number | null
}

interface RawReading {
  device_id?: string
  deviceId?: string
  timestamp?: string
  value?: number | string
  metadata?: {
    platform?: string
    location?: RawLocation | null
  } | null
}

interface Reading {
  deviceId: string
  t: number
  /** Dynamic acceleration, gravity removed. */
  accel: number
}

/** Readings older than this count as finished when deciding if an earthquake is still active. */
const QUAKE_ACTIVE_MS = 30_000

export function createEventsApi(url: string): DashboardApi {
  const readings = new Map<string, Reading>()
  const firstSeen = new Map<string, number>()
  const lastLocation = new Map<string, RawLocation>()

  async function poll(signal: AbortSignal) {
    const body = await fetchJson<{ events?: RawReading[] } | RawReading[]>(url, signal, 'GET /events')
    const items = Array.isArray(body) ? body : (body.events ?? [])

    for (const raw of items) {
      const deviceId = raw.device_id ?? raw.deviceId
      const t = raw.timestamp ? Date.parse(raw.timestamp) : NaN
      const value = Number(raw.value)
      if (!deviceId || !Number.isFinite(t) || !Number.isFinite(value)) continue

      const key = `${deviceId}|${t}`
      if (!readings.has(key)) readings.set(key, { deviceId, t, accel: Math.abs(value - GRAVITY_MS2) })
      if (!firstSeen.has(deviceId) || t < firstSeen.get(deviceId)!) firstSeen.set(deviceId, t)

      const loc = raw.metadata?.location
      if (loc && Number.isFinite(loc.lat) && Number.isFinite(loc.lon)) lastLocation.set(deviceId, loc)
    }

    // Keep memory bounded: drop what is older than the buffer, relative to the newest reading.
    let newest = 0
    for (const r of readings.values()) if (r.t > newest) newest = r.t
    for (const [key, r] of readings) if (r.t < newest - READING_BUFFER_MS) readings.delete(key)
  }

  function sorted(deviceId?: string) {
    const list = [...readings.values()].filter((r) => !deviceId || r.deviceId === deviceId)
    return list.sort((a, b) => a.t - b.t)
  }

  function snapshot(now: number): DashboardSnapshot {
    const all = sorted()
    const byDevice = new Map<string, Reading[]>()
    for (const r of all) {
      const list = byDevice.get(r.deviceId)
      if (list) list.push(r)
      else byDevice.set(r.deviceId, [r])
    }

    const devices: Device[] = []
    const latest: Telemetry[] = []
    for (const [id, list] of byDevice) {
      const last = list[list.length - 1]
      const loc = lastLocation.get(id)
      const location: GeoPoint | null = loc ? { lat: loc.lat, lon: loc.lon } : null
      devices.push({
        id,
        name: id,
        kind: 'phone',
        model: null,
        location,
        locality: location ? `${location.lat.toFixed(2)}°, ${location.lon.toFixed(2)}°` : 'fără GPS',
        online: now - last.t <= ONLINE_WITHIN_MS,
        lastSeenAt: new Date(last.t).toISOString(),
        registeredAt: new Date(firstSeen.get(id) ?? last.t).toISOString(),
      })
      latest.push({
        deviceId: id,
        recordedAt: new Date(last.t).toISOString(),
        receivedAt: null,
        accel: null,
        accelMagnitude: last.accel,
        gyro: null,
        battery: null,
        gps: loc ? { lat: loc.lat, lon: loc.lon, accuracy: loc.accuracy ?? null } : null,
      })
    }

    const { triggers, events } = detect(all, now)
    const recentCount = all.filter((r) => r.t > now - 60_000).length
    const status: SystemStatus = {
      generatedAt: new Date(now).toISOString(),
      devicesTotal: devices.length,
      devicesOnline: devices.filter((d) => d.online).length,
      messagesPerSecond: recentCount / 60,
      latencyP50Ms: null,
      latencyP95Ms: null,
      triggerThreshold: TRIGGER_THRESHOLD_MS2,
    }

    return {
      devices,
      latest,
      history: perSecondMax(all, now - HISTORY_WINDOW_S * 1000),
      triggers,
      events,
      status,
    }
  }

  function detect(all: Reading[], now: number): { triggers: Trigger[]; events: Event[] } {
    const above = all.filter((r) => r.accel >= TRIGGER_THRESHOLD_MS2)
    const triggers: Trigger[] = above.map((r) => ({
      id: `${r.deviceId}|${r.t}`,
      deviceId: r.deviceId,
      at: new Date(r.t).toISOString(),
      peakAccel: r.accel,
      threshold: TRIGGER_THRESHOLD_MS2,
      eventId: null,
    }))

    // Group triggers that start within QUAKE_WINDOW_MS of each other.
    const events: Event[] = []
    let i = 0
    while (i < triggers.length) {
      const start = Date.parse(triggers[i].at)
      let j = i
      while (j + 1 < triggers.length && Date.parse(triggers[j + 1].at) - start <= QUAKE_WINDOW_MS) j++
      const group = triggers.slice(i, j + 1)
      const deviceIds = [...new Set(group.map((g) => g.deviceId))]
      if (deviceIds.length >= QUAKE_MIN_DEVICES) {
        const id = `q-${start}`
        const end = Date.parse(group[group.length - 1].at)
        const active = now - end < QUAKE_ACTIVE_MS
        for (const g of group) g.eventId = id
        events.push({
          id,
          status: active ? 'active' : 'ended',
          startedAt: new Date(start).toISOString(),
          endedAt: active ? null : new Date(end).toISOString(),
          deviceIds,
          triggerCount: group.length,
          peakAccel: Math.max(...group.map((g) => g.peakAccel)),
          epicenter: centroid(deviceIds.map((id) => lastLocation.get(id)).filter((l) => l !== undefined)),
          region: null,
        })
      }
      i = j + 1
    }

    return { triggers: triggers.reverse().slice(0, 50), events: events.reverse().slice(0, 20) }
  }

  return {
    async getSnapshot(signal) {
      await poll(signal)
      return snapshot(Date.now())
    },

    // History comes from the buffer: the endpoint has no time-range query.
    async getAccelHistory({ deviceId, windowSec }) {
      return perSecondMax(sorted(deviceId), Date.now() - windowSec * 1000)
    },
  }
}

function perSecondMax(list: Reading[], from: number): AccelPoint[] {
  const bySecond = new Map<number, number>()
  for (const r of list) {
    if (r.t < from) continue
    const s = Math.floor(r.t / 1000) * 1000
    const prev = bySecond.get(s)
    if (prev === undefined || r.accel > prev) bySecond.set(s, r.accel)
  }
  return [...bySecond]
    .sort((a, b) => a[0] - b[0])
    .map(([t, peak]) => ({ t: new Date(t).toISOString(), peak }))
}

/** Rough epicenter: the middle of the devices that confirmed the event. */
function centroid(points: RawLocation[]): GeoPoint | null {
  if (points.length === 0) return null
  const lat = points.reduce((s, p) => s + p.lat, 0) / points.length
  const lon = points.reduce((s, p) => s + p.lon, 0) / points.length
  return { lat, lon }
}
