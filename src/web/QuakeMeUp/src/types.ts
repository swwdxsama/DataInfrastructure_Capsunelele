/**
 * Data contract between the dashboard and the REST API.
 * All timestamps are ISO 8601 strings in UTC. Accelerations are in m/s²
 * with gravity removed (Android linear acceleration).
 */

export type DeviceKind = 'phone' | 'emulator' | 'simulated'

export interface GeoPoint {
  lat: number
  lon: number
}

export interface Device {
  id: string
  name: string
  kind: DeviceKind
  /** Hardware model reported by the phone, e.g. "Pixel 7". */
  model: string | null
  /** Registered position of the sensor. */
  location: GeoPoint
  /** Human-readable place, e.g. "Focșani". */
  locality: string
  online: boolean
  lastSeenAt: string | null
  registeredAt: string
}

export interface Vector3 {
  x: number
  y: number
  z: number
}

/** Latest message received from one device. */
export interface Telemetry {
  deviceId: string
  /** Clock of the phone when the reading was taken. */
  recordedAt: string
  /** Clock of the backend when the message was stored. */
  receivedAt: string
  accel: Vector3
  /** Euclidean norm of `accel`. */
  accelMagnitude: number
  /** Angular velocity, rad/s. */
  gyro: Vector3
  battery: {
    /** 0..1 */
    level: number
    charging: boolean
  }
  gps: GeoPoint & {
    /** Horizontal accuracy in metres. */
    accuracy: number
  }
}

/** A single device crossing the acceleration threshold. */
export interface Trigger {
  id: string
  deviceId: string
  at: string
  peakAccel: number
  threshold: number
  /** Set when the network confirmed the trigger as part of an earthquake. */
  eventId: string | null
}

export type EventStatus = 'active' | 'ended'

/** An earthquake confirmed by several devices triggering together. */
export interface Event {
  id: string
  status: EventStatus
  startedAt: string
  endedAt: string | null
  deviceIds: string[]
  triggerCount: number
  peakAccel: number
  epicenter: GeoPoint | null
  region: string | null
}

export interface SystemStatus {
  /** Server time when the status was computed. */
  generatedAt: string
  devicesTotal: number
  devicesOnline: number
  messagesPerSecond: number
  latencyP50Ms: number
  latencyP95Ms: number
  triggerThreshold: number
}

/** One second of acceleration history: the largest magnitude seen in that second. */
export interface AccelPoint {
  t: string
  peak: number
}

export interface DashboardSnapshot {
  devices: Device[]
  latest: Telemetry[]
  /** Network-wide history: max over all devices, per second. */
  history: AccelPoint[]
  triggers: Trigger[]
  events: Event[]
  status: SystemStatus
}
