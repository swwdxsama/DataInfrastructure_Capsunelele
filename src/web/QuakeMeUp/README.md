# QuakeMeUp, dashboard

Dashboard-ul rețelei de detectare a cutremurelor. Telefoanele Android trimit accelerometru, giroscop, GPS și baterie prin AWS IoT Core → Lambda → PostgreSQL; dashboard-ul citește un API REST la fiecare 3 secunde.

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

## Structură

```
src/
  types.ts           contractul de date: Device, Telemetry, Trigger, Event
  config.ts          unitate, interval de polling, fereastra graficelor
  api/               singurul strat de date (client REST)
  hooks/usePolling   polling fără suprapuneri, oprit când tab-ul e ascuns
  lib/               formatare ro-RO în UTC, date derivate, paleta pentru grafice
  components/        antet de stare, bara sistemului, grafic, hartă, tabele, profil
```
