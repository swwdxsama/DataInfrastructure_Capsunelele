import type { DashboardSnapshot, Device, Event, Telemetry, Trigger } from '../types'

export type DeviceState = 'above' | 'online' | 'offline'

export interface DashboardView {
  devices: Device[]
  deviceById: Map<string, Device>
  latestById: Map<string, Telemetry>
  stateById: Map<string, DeviceState>
  triggers: Trigger[]
  events: Event[]
  activeEvent: Event | null
  lastEndedEvent: Event | null
  threshold: number
  /** Server clock at the time of the snapshot; all "x ago" labels use it. */
  serverNow: number
}

const kindOrder = { phone: 0, emulator: 1, simulated: 2 } as const

/** Everything the panels need, computed once per snapshot. */
export function deriveView(snapshot: DashboardSnapshot): DashboardView {
  const threshold = snapshot.status.triggerThreshold
  const devices = snapshot.devices.toSorted(
    (a, b) => kindOrder[a.kind] - kindOrder[b.kind] || a.name.localeCompare(b.name, 'ro'),
  )
  const deviceById = new Map(devices.map((d) => [d.id, d]))
  const latestById = new Map(snapshot.latest.map((t) => [t.deviceId, t]))
  const stateById = new Map<string, DeviceState>()
  for (const d of devices) {
    const t = latestById.get(d.id)
    stateById.set(
      d.id,
      !d.online ? 'offline' : t && t.accelMagnitude >= threshold ? 'above' : 'online',
    )
  }
  let activeEvent: Event | null = null
  let lastEndedEvent: Event | null = null
  for (const e of snapshot.events) {
    if (e.status === 'active') activeEvent ??= e
    else if (!lastEndedEvent || e.startedAt > lastEndedEvent.startedAt) lastEndedEvent = e
  }

  return {
    devices,
    deviceById,
    latestById,
    stateById,
    triggers: snapshot.triggers,
    events: snapshot.events,
    activeEvent,
    lastEndedEvent,
    threshold,
    serverNow: Date.parse(snapshot.status.generatedAt),
  }
}

export function vectorNorm(v: { x: number; y: number; z: number }) {
  return Math.hypot(v.x, v.y, v.z)
}
