# QuakeMeUp, dashboard

Dashboard-ul rețelei de detectare a cutremurelor. Telefoanele Android trimit accelerometru, giroscop, GPS și baterie prin AWS IoT Core → Lambda → DynamoDB; dashboard-ul citește un API REST la fiecare 3 secunde.

## Pornire

```bash
npm install
npm run dev        # http://localhost:5173
npm run build      # verificare tipuri + build de producție în dist/
```

Fără `VITE_API_URL`, dashboard-ul arată structura goală și un mesaj de configurare.

`#dispozitiv=<id>` în URL deschide direct profilul unui dispozitiv.

## Conectare la API-ul real

Copiază `.env.example` în `.env.local` și setează:

```
VITE_API_URL=https://<id>.execute-api.<regiune>.amazonaws.com/prod
```

### Rute așteptate

Toate răspund cu JSON; formele exacte sunt în `src/types.ts`. Orele sunt ISO 8601 în UTC, accelerațiile în m/s² fără gravitație.

| Rută | Răspuns |
|---|---|
| `GET /devices` | `Device[]` |
| `GET /telemetry/latest` | `Telemetry[]`, ultimul mesaj al fiecărui dispozitiv |
| `GET /telemetry/history?windowSec=300[&deviceId=]` | `AccelPoint[]`, maximul pe fiecare secundă |
| `GET /triggers?limit=50` | `Trigger[]`, cele mai recente primele |
| `GET /events?limit=20` | `Event[]` |
| `GET /status` | `SystemStatus` (include pragul de declanșare) |

API Gateway trebuie să permită CORS pentru originea dashboard-ului.

## Ce afișează

1. **Valori curente** (sus, fără scroll): accelerația maximă din rețea acum (normal / ridicat de la 50% din prag / alertă de la prag), ultimul cutremur cu intensitatea estimată, locația lui, ora ultimei citiri stocate și vechimea ei.
2. **Istoric**: graficul accelerației pe 5 min / 1 h / 24 h / 7 zile cu cutremurele marcate, tabelul sortabil al cutremurelor, harta cu senzorii și epicentrele.
3. **Starea sistemului**: senzori online, API (din polling), baza de date (dedusă), ultimul răspuns, ingestie. Fiecare widget are stări de încărcare, gol și eroare.

**Intensitatea** e estimată din PGA (scara Mercalli, Wald et al. 1999), în `src/lib/intensity.ts`. Backend-ul nu trimite magnitudine.

**Starea DynamoDB** e dedusă (nu există endpoint de health): `/status` răspunde → citirile merg; `receivedAt` recent → scrierile merg. Regulile sunt în `src/lib/health.ts`.

## Date mock

Tot ce e simulat e în `src/api/mock.ts`, marcat `MOCK:`, iar în interfață apare cu eticheta **Date demo**.

| Ce | Sursa acum | Cum se înlocuiește |
|---|---|---|
| Istoric 5 min | real, din snapshot | – |
| Istoric 1 h | real, `windowSec=3600`, agregat pe 10 s în browser | – |
| Istoric 24 h, 7 zile | fond simulat + cutremurele reale din `/events` | un endpoint agregat, de ex. `GET /telemetry/history?windowSec=86400&bucketSec=300`; apoi `source: 'api'` în `HISTORY_RANGES` (`src/config.ts`) |

## Structură

```
src/
  types.ts           contractul de date: Device, Telemetry, Trigger, Event
  config.ts          unitate, interval de polling, fereastra graficelor
  api/               singurul strat de date (client REST)
  hooks/usePolling   polling fără suprapuneri, oprit când tab-ul e ascuns
  lib/               formatare ro-RO în UTC, date derivate, paleta pentru grafice
  api/mock.ts        date simulate, până există endpoint-urile reale
  components/        LatestValues, SystemStatus, HistoryChart, EventsTable, DeviceMap,
                     DeviceTable, TriggerTimeline, DevicePanel (sertar), WidgetState
```
