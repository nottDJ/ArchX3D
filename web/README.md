# ArchX3D — Web Frontend

Next.js (App Router) frontend for ArchX3D. Phase 2 delivers the real-time
generation dashboard at `/generate/[job_id]`.

## Setup

```bash
npm install
cp .env.local.example .env.local   # point NEXT_PUBLIC_API_BASE_URL at FastAPI
npm run dev
```

## The generation dashboard

After `POST /api/generate` returns a `job_id`, redirect the user to
`/generate/<job_id>`. The page opens an `EventSource` against
`GET {API_BASE}/api/jobs/{job_id}/stream` and expects `data:` payloads of:

```json
{ "status": "EXTRACTING_DXF", "message": "Parsing DXF layers and geometry..." }
```

Valid `status` values:
`QUEUED` → `EXTRACTING_DXF` → `GENERATING_GEOMETRY` → `BUILDING_SCENE` →
`EXPORTING_GLB` → `COMPLETED`, plus the terminal `FAILED`.

On `COMPLETED` the page pauses ~1.6s on the success state, then pushes to
`/viewer?job_id=<job_id>`. On `FAILED` it shows the error with a **Try again**
action that re-opens the stream.

### Developing without the backend

A scripted mock of the SSE endpoint is included:

```bash
npm run mock:sse         # happy path, port 8000
npm run mock:sse:fail    # fails partway through BUILDING_SCENE
```

Then open <http://localhost:3000/generate/demo-job-123>.

## Layout

| Path                                        | Purpose                                       |
| ------------------------------------------- | --------------------------------------------- |
| `app/generate/[job_id]/page.tsx`            | Server shell — resolves + decodes the route param |
| `components/generate/GenerationDashboard.tsx` | Client dashboard: layout, redirect, states  |
| `components/generate/Timeline.tsx`          | Six-step vertical stepper                     |
| `components/generate/TerminalLog.tsx`       | Auto-scrolling console                        |
| `hooks/useJobStream.ts`                     | `EventSource` lifecycle, retries, log buffer  |
| `lib/generation.ts`                         | Status types, step metadata, payload parsing  |
| `lib/api.ts`                                | Backend URL construction                      |

Adding a pipeline stage means editing `GENERATION_STEPS` and `STEP_META` in
`lib/generation.ts` — nothing else hardcodes the step list.
