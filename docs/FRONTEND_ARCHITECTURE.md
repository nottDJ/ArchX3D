# ArchX3D — Frontend architecture (v1.0)

How the web app is organised, where state lives, how data flows, and what it
costs.

**Stack:** Next.js 15 (App Router) · React 19 · TypeScript strict · Tailwind
CSS 4 · Radix primitives · three.js + React Three Fiber.

---

## Contents

1. [Structure](#1-structure)
2. [Layering and dependency rules](#2-layering-and-dependency-rules)
3. [State](#3-state)
4. [Data flow](#4-data-flow)
5. [The project registry](#5-the-project-registry)
6. [Routing](#6-routing)
7. [Server and client components](#7-server-and-client-components)
8. [Performance](#8-performance)
9. [Code quality review](#9-code-quality-review)
10. [Testing](#10-testing)
11. [Conventions](#11-conventions)
12. [Roadmap](#12-roadmap)

---

## 1. Structure

```
web/
├── app/                          routes only — thin
│   ├── layout.tsx                fonts, theme script, providers
│   ├── globals.css               ← every design token
│   ├── page.tsx                  landing (static, no shell)
│   ├── loading.tsx  error.tsx  not-found.tsx
│   ├── dashboard/  projects/  settings/  docs/
│   ├── new/                      wizard
│   ├── generate/[job_id]/        one-shot pipeline progress
│   └── viewer/                   3D viewer
│
├── components/
│   ├── ui/                       ← the design system. Knows nothing of ArchX3D.
│   │   ├── cn.ts icons.tsx Button Card Badge Field
│   │   ├── Overlay Feedback Navigation
│   │   └── index.ts              the public surface
│   ├── shell/                    AppShell · CommandMenu · ThemeToggle · Providers
│   ├── projects/                 ProjectCard · DashboardView · ProjectsView
│   ├── settings/                 SettingsView
│   ├── wizard/                   the generation flow
│   ├── generate/                 job progress
│   └── viewer/                   3D — see docs/VIEWER.md
│
├── hooks/                        useProjects · useTheme · useViewerSettings
│                                 useGLTFModel · useRoofDetection · useJobStream
├── lib/                          pure logic, no React
│   ├── api.ts  wizard.ts         backend contract
│   ├── projects.ts               local registry (external store)
│   ├── theme.ts  format.ts
│   ├── editor.ts  snapping.ts    review-step logic
│   └── viewer/                   classify · bounds · movement · manifest · settings
├── types/                        shared type vocabulary
└── tests/                        node:test over compiled lib/
```

### The rule that shapes it

**`components/ui` knows nothing about ArchX3D.** It has no import of `lib/api`,
no concept of a project or a job. That is what makes it a design system rather
than a folder of this app's components — it can be reviewed, tested and reused
independently, and a change to the backend cannot reach it.

Everything above it is domain code, and the split is enforced by review.

---

## 2. Layering and dependency rules

```
app/                    routes, metadata            → everything
  ↓
components/{domain}/    feature UI                  → ui, hooks, lib, types
  ↓
components/shell/       app chrome                  → ui, hooks, lib
  ↓
hooks/                  React state                 → lib, types
  ↓
components/ui/          design system               → cn, icons only
  ↓
lib/                    pure logic                  → types only
  ↓
types/                  vocabulary                  → nothing
```

**Forbidden**

| | Why |
| --- | --- |
| `ui/` → `lib/api`, `lib/projects`, `lib/wizard` | Couples the design system to this product |
| `lib/` → React | `lib` must be testable in `node:test` with no renderer |
| `lib/viewer/` → `three` | Same — 117 tests run in 175 ms because of this rule |
| Any component → another feature's internals | Cross-feature reuse goes through `ui/` |
| Deep import into `components/ui/*` from a client page | Use the barrel; see §8 for the static-page exception |

**`lib/viewer/*` importing no `three`** is the highest-leverage constraint in
the codebase. It is why the classifier, the framing maths, the movement feel
and the manifest parser are all unit-tested without a GPU, a canvas or a model.

---

## 3. State

Five mechanisms, each for a different lifetime. Choosing the wrong one is the
usual cause of "why did this re-render".

| Mechanism | Lifetime | Used for |
| --- | --- | --- |
| `useState` | One component | Local UI — panel open, hover |
| URL search params | Navigation, shareable | Filter, sort, search, layout |
| External store (`useSyncExternalStore`) | Session, cross-component | Viewer settings, project registry |
| `localStorage` | Across sessions | Theme, settings, camera pose, registry |
| Server | Permanent | Projects, jobs, models |

### No global state library

No Redux, Zustand or Jotai. Two reasons, both concrete:

**The shared state is tiny.** Theme, viewer settings and the project index.
Each is a ~40-line external store with `useSyncExternalStore` — a standard
React 18+ API — and a library would add a dependency and a mental model for no
capability.

**The frame loop must not re-render.** The viewer's walk controller and
collision solver read settings 60–144 times a second. Through React state that
would either re-render the canvas tree on every slider nudge, or require a ref
manually kept in sync. A module-level store gives frame code `getSettings()`
with no subscription, while the toolbar subscribes normally.

```ts
// lib/viewer/settings.ts — the pattern, used three times
export function getSettings(): ViewerSettings { return current; }
export function subscribeSettings(fn: () => void): () => void { … }
export function getServerSettings(): ViewerSettings { return DEFAULT_SETTINGS; }
```

`getServerSettings` returns defaults rather than stored values: reading
`localStorage` during render would make server and client markup disagree.
Stored values are applied in an effect after mount.

### URL as state

Search, filter, sort and layout on `/projects` are query parameters. That makes
a filtered view **linkable and survivable**: filter to "needs review", open
one, press Back, and you return to the filtered list rather than an unfiltered
one. A few more lines than `useState`, and it removes an entire class of "the
app forgot what I was doing".

---

## 4. Data flow

```
                       ┌──────────────────────────┐
   FastAPI  ◄────────► │  lib/api · lib/wizard    │  the only fetch layer
   :8000               └────────────┬─────────────┘
                                    │
                       ┌────────────▼─────────────┐
                       │  hooks/useProjects       │  joins server + local
                       │  hooks/useJobStream      │  SSE
                       └────────────┬─────────────┘
                                    │
   localStorage ◄──► lib/projects ──┤
   (index only)                     │
                       ┌────────────▼─────────────┐
                       │  components/{domain}/    │
                       └──────────────────────────┘
```

**Every network call goes through `lib/api` or `lib/wizard`.** No component
calls `fetch`. One place knows the base URL, the error shape and the retry
policy.

**No backend change was made.** All thirteen endpoints are used exactly as they
were.

---

## 5. The project registry

The one piece of architecture that exists because of a missing endpoint.

### The problem

The API can create a project and return one by id. There is no
`GET /api/projects`. So there is no server-side answer to "what have I made?"
— and a dashboard needs one.

### The options, and the choice

| Option | Verdict |
| --- | --- |
| Change the backend | Out of scope for a frontend redesign |
| Show plausible sample data | **Never.** Fabricated data in a product is a lie |
| Record locally what the client already knows | **Chosen** |

### How it works

`lib/projects.ts` keeps an index in `localStorage`: id, name, created, last
opened, pinned, and a cached summary. Every id in it was returned by the server
to this browser.

`useProjects` renders the cached summary **immediately**, then re-fetches each
project from `GET /api/projects/{id}` and reconciles. So the dashboard paints
instantly from local data and corrects itself a moment later — rather than
showing a spinner over a list the browser already has.

Beyond the id, nothing is trusted from cache. Status, image count and byte
totals all come from the server's current manifest.

### What it costs, stated in the UI

Per-browser. Clearing site data hides projects; a second machine does not see
them. **Nothing is lost** — the projects still exist on the server and a direct
link still works. Only discovery is local, and the dashboard and settings pages
both say so.

`forget()` is deliberately not called `delete`: there is no endpoint to remove
a project, and presenting removal as deletion would be a lie the user
discovers when their disk fills up.

### The fix, when it comes

One endpoint:

```
GET /api/projects → [{ project_id, created_at, stage, dxf, images }]
```

`useProjects` then reads from it and the local index becomes a cache. No
component changes.

---

## 6. Routing

| Route | Rendering | Shell | Notes |
| --- | --- | --- | --- |
| `/` | Static | ✗ | Marketing. Zero client JS. |
| `/dashboard` | Static + client | ✓ | |
| `/projects` | Static + client | ✓ | Suspense boundary for `useSearchParams` |
| `/new` | Static + client | ✓ | Wizard |
| `/generate/[job_id]` | Dynamic | ✗ | Own full-page layout |
| `/viewer` | Dynamic | ✗ | Full-bleed canvas |
| `/settings`, `/docs` | Static | ✓ | |
| `not-found`, `error`, `loading` | — | ✗ | |

**`/projects` needs its Suspense boundary.** `useSearchParams` opts a component
into client rendering, and without a boundary Next deopts the *whole route* to
client-side rendering at build time.

---

## 7. Server and client components

Default to server. `"use client"` only where there is state, an effect, a
browser API or an event handler.

**Fully static, no client JS:** `/`, `/docs`, `layout`, `loading`,
`not-found`.

`AppShell` is a client component because it owns the mobile drawer, the ⌘K
listener and `usePathname`. Its *children* are usually server-rendered and
passed through — a server page can render `<AppShell>` and everything inside
stays server-side.

---

## 8. Performance

### Measured

```
Route                        Size    First Load JS
/                           1.5 kB      120 kB      static, no shell
/_not-found                 123 B       103 kB
/dashboard                  3.7 kB      177 kB
/docs                       198 B       173 kB
/generate/[job_id]          5.2 kB      114 kB
/new                       21.5 kB      195 kB      wizard + review editor
/projects                   3.5 kB      177 kB
/settings                   3.9 kB      177 kB
/viewer                     1.9 kB      135 kB      3D lazy-loaded behind this
Shared                                  103 kB
```

### The techniques that produced those numbers

**The 3D stack is lazy and client-only.** `next/dynamic` with `ssr: false`
keeps ~350 kB of three.js, drei and three-mesh-bvh out of the route's initial
bundle. `/viewer` was 461 kB statically imported; it is 135 kB now, and the 3D
engine streams in behind a skeleton. There is no WebGL, `localStorage` or
pointer lock on the server, so SSR of the canvas was pure waste.

**Static pages import components directly, not through the barrel.** The barrel
re-exports client components with Radix dependencies; pulling those into an
otherwise-static route costs ~50 kB of JavaScript it never runs. Measured: `/`
was 167 kB through the barrel, 120 kB direct.

**Fonts do not reflow.** `next/font` with `display: swap` and
`adjustFontFallback`, so the fallback's metrics match Inter and the swap does
not shift the page. JetBrains Mono loads only weights 400 and 500 — the full
family is ~180 kB of faces nothing renders.

**No theme flash.** An inline pre-paint script sets `data-theme` before first
paint (see [`DESIGN_SYSTEM.md`](DESIGN_SYSTEM.md#5-theming)).

**The frame loop causes zero React renders.** Settings are read imperatively;
FPS is sampled at 2 Hz; the minimap pose at 15 Hz and only when the camera has
actually moved. Walking around a building re-renders nothing.

**On-demand rendering in orbit mode.** `frameloop="demand"` — a model being
*looked at* renders once, not 60 times a second. The managers invalidate
explicitly while animating.

**Indexed once at load.** The viewer walks the model once and builds
`byKind` / `byRoom` / `colliders` / `bounds`. Roof toggle, view modes, room
navigation and collider construction are then lookups rather than four more
traversals of a 100,000-object scene.

### Budgets

| Metric | Budget |
| --- | --- |
| Shared JS | ≤ 110 kB |
| Any route First Load | ≤ 200 kB |
| Static marketing route | ≤ 125 kB |
| LCP (broadband) | < 1.5 s |
| CLS | < 0.05 |
| Viewer at 100k objects | ≥ 30 fps |

### Not done, and worth doing

- **`next/image`** is unused; the wizard's reference thumbnails are raw `<img>`
  with `loading="lazy"`. The API serves originals with no resizing endpoint, so
  `next/image` would need a loader configured against it.
- **Virtualised project grid.** Fine to a few hundred; a workspace with
  thousands needs windowing.
- **Route prefetch on card hover.**

---

## 9. Code quality review

Honest assessment, including what is still wrong.

### Fixed in this pass

| Problem | Was | Now |
| --- | --- | --- |
| No design system | 511 hard-coded colour utilities | ~0; semantic tokens |
| Duplicate icons | 3 files, 530 lines, 6 icons twice | 1 set, 3 re-export shims |
| Duplicate formatting | `formatBytes` twice, different rounding | one `lib/format.ts` |
| No shared primitives | Every button hand-rolled | 25 components |
| No navigation | 4 unlinked pages | shell + palette + breadcrumbs |
| Unreachable projects | React state only | local registry + dashboard |
| `iconOnly` rendered nothing | — | fixed |
| `asChild` + icons threw | — | fixed with `Slottable` |

### Still wrong

| Problem | Size | Plan |
| --- | --- | --- |
| **`ReviewStep.tsx` — 910 lines.** Payload rendering, edit dispatch, validation display and generation trigger in one component. | Large | Split into `ReviewSummary`, `RoomList`, `ObjectTable`, `ValidationPanel`, with edit state in a `useReviewEditor` hook |
| **`PlanMap.tsx` — 718 lines**, canvas drag editor. Likely mouse-only. | Large | Needs an accessibility audit *and* a split |
| **`Inspector.tsx` — 571 lines.** | Medium | Extract per-type panels |
| **`Wizard.tsx` — 500 lines** and holds all flow state. | Medium | Extract `useWizardFlow`; the step components are already separate |
| **`CameraController.tsx` — 500 lines.** Modes, framing, flight, persistence, commands. | Medium | Extract `useCameraPersistence` and `useCameraFlight` |
| **Editor components migrated onto tokens but not redesigned.** They theme correctly; their layout and interaction design is unchanged. | Large | Scoped in `UI_GUIDELINES.md` §2 |

**None of these is a correctness bug** — they are maintainability debt, listed
so the next contributor does not have to rediscover them.

### Conventions that hold

- Every module opens with a docstring stating what it is for and what it
  refuses to do.
- Non-obvious decisions carry the alternative that was rejected.
- Constants carry units and reasons.
- British spelling throughout.
- TypeScript strict; no `any` outside untrusted-input parsing.

---

## 10. Testing

```bash
npm run typecheck     # tsc --noEmit, strict
npm test              # 117 tests, ~175 ms
npm run build         # the real gate — catches SSR and Slot errors
```

### What is tested

`lib/viewer/*` — classification, framing, movement, manifest parsing, settings
validation. All pure, all fast.

Two worth pointing at:

- **Frame-rate independence.** One 1/30 s damping step must land exactly where
  two 1/60 s steps do. Without it the camera accelerates faster on a 144 Hz
  display.
- **The mezzanine.** A flat, broad plate at mid height must *not* classify as a
  roof — the false positive that would hide a floor the user is standing on.

### What is not

No component tests. The library has no test runner for React — adding one
(Vitest + Testing Library) is the single highest-value testing improvement, and
`Button`'s two bugs in this pass (`iconOnly`, `asChild`) would both have been
caught by it. They were caught by the build instead, which worked but is late.

`npm run build` is the practical gate: it typechecks, lints and prerenders every
static route, which surfaces SSR-only failures.

---

## 11. Conventions

**Files** — `PascalCase.tsx` components, `camelCase.ts` logic, `useThing.ts`
hooks.

**Imports** — external, then `@/` absolute, then relative. `@/` everywhere
except within a folder.

**Exports** — named. Default only for Next's required exports (`page`,
`layout`, `loading`, `error`, `not-found`).

**Props** — an exported `interface` per component. `readonly` on data,
`?` for optional with a documented default.

**Comments** — explain *why*, and name the rejected alternative. Never restate
the line below.

---

## 12. Roadmap

### Next

1. **Component tests** (Vitest + Testing Library). Highest value; see §10.
2. **Split `ReviewStep` and `PlanMap`**, and audit the editor for keyboard use.
3. **axe in CI** across the five shell routes.

### Needs a backend change

| Feature | Endpoint |
| --- | --- |
| Server-side project list | `GET /api/projects` |
| Generation history | `GET /api/projects/{id}/jobs` |
| Real storage figures | project size in the manifest |
| Determinate progress | percentage in the job payload |
| Project thumbnails | `GET /api/projects/{id}/thumbnail.png` |

### Comparison view

Two viewers side by side, synchronised cameras. Buildable on the current API —
`/compare?a=…&b=…` mounting two `ViewerClient`s with a shared camera store.
Cut from this pass to finish the foundation properly; the viewer already
exposes the imperative camera commands it would need.

### Authentication, when it arrives

The backend has no auth and no user model, so **no login screen was built** — a
form that authenticates nothing implies security that does not exist.

When the backend gains it: `middleware.ts` for route protection, an
`AuthProvider` in `components/shell/Providers.tsx`, a token in `lib/api`'s
fetch wrapper, and `/login` + `/signup` routes. The shell already has the
sidebar footer slot a user menu belongs in. No other component changes.

---

## Related

- [`UI_GUIDELINES.md`](UI_GUIDELINES.md) — the audit and the design rules
- [`DESIGN_SYSTEM.md`](DESIGN_SYSTEM.md) — tokens
- [`COMPONENT_LIBRARY.md`](COMPONENT_LIBRARY.md) — components
- [`ACCESSIBILITY.md`](ACCESSIBILITY.md) — conformance and gaps
- [`VIEWER.md`](VIEWER.md) — the 3D subsystem
