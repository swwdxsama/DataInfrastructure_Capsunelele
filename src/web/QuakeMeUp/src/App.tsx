import { lazy, Suspense, useCallback, useEffect, useState } from 'react'
import { api, apiConfigured } from './api'
import { DevicePanel } from './components/DevicePanel'
import { LazyAccelerationChart } from './components/LazyAccelerationChart'
import { DeviceTable } from './components/DeviceTable'
import { QuakeList } from './components/QuakeList'
import { StatusHeader } from './components/StatusHeader'
import { SystemStrip } from './components/SystemStrip'
import { TriggerTimeline } from './components/TriggerTimeline'
import { HISTORY_WINDOW_S, POLL_INTERVAL_MS } from './config'
import { usePolling } from './hooks/usePolling'
import { deriveView, type DeviceState } from './lib/derive'
import type { Device } from './types'
import { formatAccel } from './lib/format'
import './App.css'

// Leaflet is the heaviest dependency; load it after the first paint.
const DeviceMap = lazy(() => import('./components/DeviceMap'))

// Stable empty values, so the map does not re-render while waiting for data.
const NO_DEVICES: Device[] = []
const NO_STATES = new Map<string, DeviceState>()

const HASH_KEY = 'dispozitiv'
function readHash() {
  return new URLSearchParams(window.location.hash.slice(1)).get(HASH_KEY)
}

export default function App() {
  const snapshot = usePolling((signal) => api.getSnapshot(signal), {
    intervalMs: POLL_INTERVAL_MS,
    enabled: apiConfigured,
  })
  const [selectedId, setSelectedId] = useState<string | null>(readHash)

  // The open profile lives in the URL (#dispozitiv=p-01), so it can be linked and survives a reload.
  useEffect(() => {
    const hash = selectedId ? `#${HASH_KEY}=${encodeURIComponent(selectedId)}` : ''
    if (window.location.hash !== hash) {
      history.replaceState(null, '', `${window.location.pathname}${window.location.search}${hash}`)
    }
  }, [selectedId])

  useEffect(() => {
    const onHash = () => setSelectedId(readHash())
    window.addEventListener('hashchange', onHash)
    return () => window.removeEventListener('hashchange', onHash)
  }, [])

  const closeProfile = useCallback(() => setSelectedId(null), [])

  const data = snapshot.data
  const view = data ? deriveView(data) : null
  const selected = view && selectedId ? view.deviceById.get(selectedId) : undefined

  const waitingText = !apiConfigured
    ? 'Apare aici când API-ul trimite date.'
    : snapshot.error
      ? snapshot.error.message
      : 'Se încarcă'

  return (
    <div className="app">
      <StatusHeader
        activeEvent={view?.activeEvent ?? null}
        calmText={
          view ? 'Nicio activitate seismică' : apiConfigured ? 'Se conectează la rețea' : 'Fără sursă de date'
        }
      />

      <main className="dashboard">
        <SystemStrip
          status={data?.status ?? null}
          lastEndedEvent={view?.lastEndedEvent ?? null}
          serverNow={view?.serverNow ?? null}
          apiConfigured={apiConfigured}
          updatedAt={snapshot.updatedAt}
          failingSince={snapshot.failingSince}
          error={snapshot.error}
        />

        <div className="dashboard-grid">
          <section className="panel area-map" aria-labelledby="h-map">
            <div className="panel-head">
              <h2 id="h-map" className="panel-title">
                Harta senzorilor
              </h2>
            </div>
            <div className="panel-body area-map-body">
              <Suspense fallback={<div className="device-map-fallback">Se încarcă harta</div>}>
                <DeviceMap
                  devices={view?.devices ?? NO_DEVICES}
                  stateById={view?.stateById ?? NO_STATES}
                  selectedId={selectedId}
                  activeEvent={view?.activeEvent ?? null}
                  onSelect={setSelectedId}
                />
              </Suspense>
            </div>
          </section>

          <div className="area-profile">
            {view && selected ? (
              <DevicePanel
                device={selected}
                telemetry={view.latestById.get(selected.id)}
                state={view.stateById.get(selected.id) ?? 'offline'}
                triggers={view.triggers}
                events={view.events}
                deviceById={view.deviceById}
                threshold={view.threshold}
                serverNow={view.serverNow}
                onClose={closeProfile}
              />
            ) : (
              <section className="panel profile-empty" aria-labelledby="h-profile">
                <div className="panel-head">
                  <h2 id="h-profile" className="panel-title">
                    Profil dispozitiv
                  </h2>
                </div>
                <p className="panel-body profile-empty-text">
                  Apasă pe un dispozitiv de pe hartă sau din tabel ca să vezi valorile curente,
                  istoricul accelerației și depășirile de prag.
                </p>
              </section>
            )}
          </div>

          <section className="panel area-chart" aria-labelledby="h-chart">
            <div className="panel-head">
              <h2 id="h-chart" className="panel-title">
                Accelerație în rețea
              </h2>
              <p className="panel-note">
                Ultimele {HISTORY_WINDOW_S / 60} minute, valoarea maximă din fiecare secundă
              </p>
            </div>
            <div className="panel-body">
              {view && data && data.history.length > 0 ? (
                <LazyAccelerationChart
                  points={data.history}
                  threshold={view.threshold}
                  end={view.serverNow}
                  events={data.events}
                  height={300}
                  label={`Accelerația maximă în rețea, ultimele ${HISTORY_WINDOW_S / 60} minute, prag ${formatAccel(view.threshold)}`}
                />
              ) : (
                <div className="placeholder" style={{ height: 300 }}>
                  {view ? 'Nicio măsurătoare în ultimele 5 minute.' : waitingText}
                </div>
              )}
            </div>
          </section>

          <section className="panel area-devices" aria-labelledby="h-devices">
            <div className="panel-head">
              <h2 id="h-devices" className="panel-title">
                Dispozitive
              </h2>
              {data && (
                <p className="panel-note">
                  {data.status.devicesOnline} din {data.status.devicesTotal} online
                </p>
              )}
            </div>
            {view && view.devices.length > 0 ? (
              <DeviceTable
                devices={view.devices}
                latestById={view.latestById}
                stateById={view.stateById}
                selectedId={selectedId}
                serverNow={view.serverNow}
                onSelect={setSelectedId}
              />
            ) : (
              <p className="empty">
                {view ? 'Niciun dispozitiv înregistrat încă.' : waitingText}
              </p>
            )}
          </section>

          <section className="panel area-quakes" aria-labelledby="h-quakes">
            <div className="panel-head">
              <h2 id="h-quakes" className="panel-title">
                Cutremure detectate
              </h2>
              <p className="panel-note">Confirmate de cel puțin 3 senzori</p>
            </div>
            {view ? (
              <QuakeList events={view.events} serverNow={view.serverNow} />
            ) : (
              <p className="empty">{waitingText}</p>
            )}
          </section>

          <section className="panel area-timeline" aria-labelledby="h-timeline">
            <div className="panel-head">
              <h2 id="h-timeline" className="panel-title">
                Depășiri ale pragului
              </h2>
            </div>
            {view ? (
              <div className="panel-body area-timeline-body">
                <TriggerTimeline
                  triggers={view.triggers}
                  events={view.events}
                  deviceById={view.deviceById}
                  serverNow={view.serverNow}
                  onSelectDevice={setSelectedId}
                />
              </div>
            ) : (
              <p className="empty">{waitingText}</p>
            )}
          </section>
        </div>
      </main>
    </div>
  )
}
