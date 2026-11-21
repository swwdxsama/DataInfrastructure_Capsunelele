import 'leaflet/dist/leaflet.css'
import { useEffect } from 'react'
import { CircleMarker, MapContainer, TileLayer, Tooltip, useMap } from 'react-leaflet'
import type { DeviceState } from '../lib/derive'
import { deviceKindLabel } from '../lib/format'
import { usePalette } from '../lib/palette'
import type { Device, Event } from '../types'
import './DeviceMap.css'

interface Props {
  devices: Device[]
  stateById: Map<string, DeviceState>
  selectedId: string | null
  activeEvent: Event | null
  onSelect: (id: string) => void
}

const ROMANIA: [[number, number], [number, number]] = [
  [43.6, 20.2],
  [48.3, 29.8],
]

export default function DeviceMap({ devices, stateById, selectedId, activeEvent, onSelect }: Props) {
  const palette = usePalette()
  const fillFor: Record<DeviceState, string> = {
    above: palette.alert,
    online: palette.online,
    offline: palette.surface,
  }
  return (
    <div className="device-map">
      <MapContainer
        bounds={ROMANIA}
        boundsOptions={{ padding: [4, 4] }}
        zoomSnap={0.25}
        scrollWheelZoom={false}
        className="device-map-canvas"
        attributionControl
      >
        <TileLayer
          url="https://tile.openstreetmap.org/{z}/{x}/{y}.png"
          attribution='&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>'
          maxZoom={18}
        />
        <KeepSized />

        {activeEvent?.epicenter && (
          <>
            <CircleMarker
              center={[activeEvent.epicenter.lat, activeEvent.epicenter.lon]}
              radius={22}
              interactive={false}
              pathOptions={{ color: palette.alert, weight: 2, dashArray: '5 4', fill: false }}
            />
            <CircleMarker
              center={[activeEvent.epicenter.lat, activeEvent.epicenter.lon]}
              radius={4}
              interactive={false}
              pathOptions={{ color: palette.alert, weight: 0, fillColor: palette.alert, fillOpacity: 1 }}
            />
          </>
        )}

        {devices.map((d) => {
          const state = stateById.get(d.id) ?? 'offline'
          const selected = d.id === selectedId
          return (
            <CircleMarker
              key={d.id}
              center={[d.location.lat, d.location.lon]}
              radius={selected ? 10 : 7}
              pathOptions={{
                color: selected ? palette.ink : state === 'offline' ? palette.offline : palette.surface,
                weight: selected ? 3 : 2,
                fillColor: fillFor[state],
                fillOpacity: 1,
              }}
              eventHandlers={{ click: () => onSelect(d.id) }}
            >
              <Tooltip direction="top" offset={[0, -8]} className="device-map-tip">
                <strong>{d.name}</strong> {d.locality}
                <br />
                {deviceKindLabel[d.kind]}, {state === 'offline' ? 'offline' : state === 'above' ? 'peste prag' : 'online'}
              </Tooltip>
            </CircleMarker>
          )
        })}
      </MapContainer>

      <ul className="device-map-legend">
        <li>
          <span className="dot" data-state="online" /> Online
        </li>
        <li>
          <span className="dot" data-state="above" /> Peste prag
        </li>
        <li>
          <span className="dot" data-state="offline" /> Offline
        </li>
        {activeEvent?.epicenter && (
          <li>
            <span className="device-map-legend-epi" /> Epicentru estimat
          </li>
        )}
      </ul>
    </div>
  )
}

/** Leaflet measures its container once; re-measure when the layout changes (e.g. the side panel opens). */
function KeepSized() {
  const map = useMap()
  useEffect(() => {
    const el = map.getContainer()
    const observer = new ResizeObserver(() => map.invalidateSize({ pan: false }))
    observer.observe(el)
    return () => observer.disconnect()
  }, [map])
  return null
}
