# ArchX3D — API specification (v1)

Every way into ArchX3D from outside the process: HTTP, GraphQL, WebSocket, the
Python SDK, the CLI, and the client libraries generated from them.

```
   Python SDK ─┐
   CLI ────────┼─► archx3d.pipelines ─► domain
   REST ───────┤        (one orchestration layer, five front doors)
   GraphQL ────┤
   WebSocket ──┘
```

**Status.** Normative for API v1, shipping with ArchX3D 2.1. v1 endpoints in the
current `server.py` are documented in §14 with their migration.

**The rule that shapes everything here:** every surface calls the same
`archx3d.pipelines` functions. A capability reachable from the CLI but not the
API, or expressed differently in GraphQL than in REST, is a defect — it means
logic leaked into an entrypoint.

---

## Contents

1. [Design decisions](#1-design-decisions)
2. [Resource model](#2-resource-model)
3. [Conventions](#3-conventions)
4. [Authentication](#4-authentication)
5. [Authorisation](#5-authorisation)
6. [REST](#6-rest)
7. [Operations and scene mutation over HTTP](#7-operations-and-scene-mutation-over-http)
8. [GraphQL](#8-graphql)
9. [WebSocket and streaming](#9-websocket-and-streaming)
10. [Errors](#10-errors)
11. [Rate limiting and quotas](#11-rate-limiting-and-quotas)
12. [Versioning and deprecation](#12-versioning-and-deprecation)
13. [The Python SDK](#13-the-python-sdk)
14. [The CLI](#14-the-cli)
15. [Client libraries](#15-client-libraries)
16. [Webhooks](#16-webhooks)
17. [Migration from v1](#17-migration-from-the-current-server)

---

## 1. Design decisions

### Why all of REST, GraphQL and WebSocket

They are not alternatives; they answer different questions, and offering only one
forces callers to misuse it.

| Surface | Best at | Chosen for |
| --- | --- | --- |
| **REST** | resources, uploads, downloads, caching, tooling | the primary API; everything is reachable here |
| **GraphQL** | one round trip for a deep, client-shaped read | the review and inspection UIs, which need a scene, its findings, its history and its artefacts together |
| **WebSocket** | bidirectional, low-latency, stateful | collaboration, live operation streams, job progress |
| **SSE** | one-way server push over plain HTTP | job progress where WebSocket is blocked; already used by `useJobStream` |

**REST is the contract; GraphQL is a read optimisation.** All writes go through
REST (or WebSocket operation frames). GraphQL mutations exist only as thin
wrappers, and there is no capability exposed in GraphQL that REST lacks. This
prevents the common failure where two APIs drift into two products.

### Why an operation-based mutation API

The scene is mutated by posting **operations** ([`SCENE_GRAPH_SPEC.md` §6](SCENE_GRAPH_SPEC.md#6-the-operation-algebra)),
not by PATCHing resources.

`PATCH /objects/{id}` looks natural and cannot express what this system needs: an
atomic multi-entity change, an exact inverse, an attribution, a base version for
conflict detection, or a client's offline queue. Operations give all five, and
they make the HTTP API, the WebSocket API and the local SDK the same API.

### Why not gRPC

Considered and deferred. gRPC is better for internal service-to-service calls,
which ArchX3D barely has — the control plane talks to Postgres, and workers pull
from a queue. It is worse for browsers (needs a proxy), worse for casual
integration (needs codegen), and worse for the ecosystem an open-source project
wants. If internal service traffic grows, gRPC is added *between services*
without changing this public surface.

---

## 2. Resource model

```
tenant
└── project
    ├── source          (uploaded DXF, IFC, photographs — immutable blobs)
    ├── scene           (the graph; one per project by default, several allowed)
    │   ├── commit      (history)
    │   ├── entity      (read-mostly; written via operations)
    │   ├── level / space
    │   └── viewpoint
    ├── job             (analyse, generate, evaluate, refine, export)
    ├── artefact        (glb, blend, usd, previews, reports)
    ├── evaluation      (scores, findings)
    └── plan            (actions, refinement history)
```

| Resource | Id | Mutable | Notes |
| --- | --- | --- | --- |
| `project` | `prj_<ulid>` | yes | the unit of sharing and billing |
| `source` | `src_<ulid>` | no | content-addressed; re-upload is a no-op |
| `scene` | `scn_<ulid>` | via operations | |
| `commit` | `cmt_<ulid>` | never | append-only |
| `entity` | `ent_<ulid>` | via operations | |
| `job` | `job_<ulid>` | status only | |
| `artefact` | `art_<ulid>` | never | content-addressed |
| `evaluation` | `evl_<ulid>` | never | derived, reproducible |
| `plan` | `pln_<ulid>` | never | |

All ids are prefixed ULIDs: sortable, opaque, self-describing in a log, and
impossible to confuse across types.

---

## 3. Conventions

| Aspect | Rule |
| --- | --- |
| Base URL | `https://api.archx3d.io/v1` (self-hosted: `/api/v1`) |
| Encoding | JSON, UTF-8; `msgpack` via `Accept: application/msgpack` for operation streams |
| Casing | `snake_case` — matches the SDK, the CLI and every document in the system |
| Timestamps | RFC 3339 UTC, always with `Z` |
| Units | SI. Lengths in metres, angles in degrees, temperature in kelvin. Field names carry the unit where ambiguous (`elevation_m`, `power_w`) |
| Durations | seconds, `_s` suffix |
| Money | integer minor units + ISO 4217 code |
| Pagination | cursor: `?limit=50&cursor=...`; response carries `next_cursor` |
| Sorting | `?sort=created_at:desc`; always applied after a total-order tie-break |
| Filtering | `?status=running&kind=render` |
| Sparse fields | `?fields=id,status,created_at` |
| Expansion | `?expand=scene,latest_evaluation` — bounded to one level |
| Idempotency | `Idempotency-Key` header on every POST; 24-hour window |
| Concurrency | `If-Match` with the resource `ETag`; `412` on mismatch |
| Compression | `gzip`, `br` |
| Correlation | `X-Request-Id` echoed; generated when absent |
| Long operations | `202 Accepted` + a job resource. Never a blocking request |

### Nothing blocks

`POST /api/generate` in the current server runs `main.py` as a subprocess with a
900-second timeout and returns when it finishes. Every proxy, load balancer and
browser between the client and the server has a shorter timeout than that.

v1 of this API has **no blocking endpoint**. Anything that can exceed one second
returns `202` with a job:

```http
POST /v1/projects/prj_01J8.../jobs
{ "kind": "generate", "options": { "backend": "blender.cycles" } }

202 Accepted
Location: /v1/jobs/job_01J8Z...
{ "id": "job_01J8Z...", "status": "queued", "kind": "generate",
  "stream_url": "wss://api.archx3d.io/v1/jobs/job_01J8Z.../stream" }
```

---

## 4. Authentication

Four mechanisms, one `Principal`.

| Mechanism | Header | For |
| --- | --- | --- |
| **API key** | `Authorization: Bearer ak_live_...` | server-to-server, CI |
| **OAuth 2.1 + PKCE** | `Authorization: Bearer <jwt>` | user-facing apps |
| **OIDC / SAML** | via the OAuth flow | enterprise SSO |
| **Session cookie** | `__Host-archx3d_session` | first-party web app only; `SameSite=Strict`, `Secure`, `HttpOnly` |

### API keys

```
ak_live_<24-char public id>_<32-char secret>
ak_test_...
```

- Only a hash is stored. The secret is shown once.
- Scoped at creation: `projects:read`, `scenes:write`, `jobs:submit`, …
- Optional expiry and IP allow-list.
- `last_used_at` recorded for rotation hygiene.
- The public id prefix makes keys greppable in logs **and** detectable by secret
  scanners — the format is published to GitHub and GitLab so a leaked key is
  auto-revoked.

### Tokens

Access tokens are 15-minute JWTs; refresh tokens are 30-day, rotating, and
single-use with reuse detection (a replayed refresh token revokes the family).

Claims:

```json
{ "iss": "https://api.archx3d.io", "sub": "usr_01J7...", "aud": "archx3d-api",
  "tid": "tnt_01J6...", "scp": "projects:read scenes:write jobs:submit",
  "exp": 1785312000, "iat": 1785311100, "jti": "01J8Z..." }
```

### Local and desktop

The desktop app runs a loopback server. It requires a token by default (a
per-launch secret passed to the WebView) rather than trusting loopback, because
any process on the machine can reach `127.0.0.1`. `--no-auth` exists for scripted
local use and prints a warning.

---

## 5. Authorisation

RBAC with resource scoping. Every request resolves to
`(principal, action, resource) → allow | deny`, evaluated by `AuthProvider`.

### Roles

| Role | Read | Edit scene | Submit jobs | Manage members | Billing | Plugins |
| --- | --- | --- | --- | --- | --- | --- |
| `viewer` | ✓ | | | | | |
| `commenter` | ✓ | annotations only | | | | |
| `editor` | ✓ | ✓ | ✓ | | | |
| `maintainer` | ✓ | ✓ | ✓ | ✓ | | ✓ |
| `owner` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| `service` | scoped | scoped | scoped | | | |

Assignable at tenant or project level; project-level grants override.

### Rules

1. **Tenant isolation is enforced in the database**, by row-level security keyed
   on the connection's tenant, not by application `WHERE` clauses. An application
   bug then cannot leak across tenants.
2. **Every resource id is checked for tenant membership** before anything else. A
   valid id from another tenant returns `404`, never `403` — a `403` confirms the
   resource exists.
3. **Scopes narrow, never widen.** An API key's scopes intersect its principal's
   role.
4. **Every denial is logged** with principal, action and resource. This is the
   security audit trail; the journal is the data audit trail.
5. **Sharing is explicit.** A project shared by link gets a scoped, expiring,
   revocable token — never an unauthenticated URL.

---

## 6. REST

### 6.1 Projects

```http
POST   /v1/projects                          create
GET    /v1/projects                          list
GET    /v1/projects/{id}                     fetch
PATCH  /v1/projects/{id}                     rename, re-tag
DELETE /v1/projects/{id}                     soft-delete (30-day recovery)
POST   /v1/projects/{id}/restore
GET    /v1/projects/{id}/members
PUT    /v1/projects/{id}/members/{user_id}   set role
```

```json
{
  "id": "prj_01J8Z1A0PQ4M2N9V6XW0RTB5FC",
  "name": "Riverside Apartment",
  "tenant_id": "tnt_01J6...",
  "scene_id": "scn_01J8Z1B2...",
  "stage": "reviewed",
  "counts": { "sources": 13, "levels": 1, "spaces": 5, "entities": 187, "jobs": 4 },
  "latest_evaluation": { "id": "evl_01J8Z...", "score": 0.71, "confidence": 0.83 },
  "created_at": "2026-07-29T09:00:00Z",
  "updated_at": "2026-07-29T11:42:07Z"
}
```

### 6.2 Sources

Uploads are direct-to-storage. The API issues a presigned URL; bytes never
traverse the API servers.

```http
POST /v1/projects/{id}/sources
{ "filename": "plan.dxf", "kind": "floorplan",
  "bytes": 402118, "digest": "b3:7f2a..." }

201 Created
{ "id": "src_01J8Z...", "status": "awaiting_upload",
  "upload": { "method": "PUT", "url": "https://...", "expires_at": "..." } }
```

```http
POST /v1/projects/{id}/sources/{src}/complete    → verifies the digest
GET  /v1/projects/{id}/sources                   → list
GET  /v1/projects/{id}/sources/{src}/content     → 302 to a presigned GET
DELETE /v1/projects/{id}/sources/{src}
```

Client-supplied `digest` means a re-upload of identical bytes is deduplicated
server-side and returns the existing source — which matters when a user re-uploads
a 200 MB IFC after a failed analysis.

| Kind | Accepts | Limit |
| --- | --- | --- |
| `floorplan` | `.dxf`, `.dwg`, `.ifc`, `.pdf` | 500 MB |
| `photograph` | `.jpg`, `.png`, `.webp`, `.heic` | 50 MB, 100 per project |
| `reference` | any | 100 MB |

### 6.3 Scenes

```http
GET /v1/scenes/{id}                      summary + head commit
GET /v1/scenes/{id}/levels
GET /v1/scenes/{id}/spaces?level=ent_...
GET /v1/scenes/{id}/entities             the query surface, below
GET /v1/scenes/{id}/entities/{ent}
GET /v1/scenes/{id}/export?format=glb|usd|ifc|arx|json   → 202 + job
```

**Entity query** — the REST projection of the typed query API:

```http
GET /v1/scenes/{id}/entities
      ?kind=furniture,decor
      &level=ent_01J8Z1A1...
      &space=ent_01J8Z1A9...
      &components=core:transform,core:appearance
      &where=core:detection.confidence>=0.65
      &bbox=0,0,6,4
      &order=core:transform.position.x
      &limit=200&cursor=...
```

```json
{
  "data": [
    { "id": "ent_01J8Z1B4...", "kind": "furniture",
      "level": "ent_01J8Z1A1...", "parent": "ent_01J8Z1A9...",
      "components": {
        "core:transform":  { "position": {"x":3.1,"y":1.8,"z":0.0}, "rotation_z":90.0 },
        "core:appearance": { "colour_hex": "#8B8B86", "material": "fabric" }
      },
      "provenance": {
        "core:transform": { "source":"observed", "confidence":0.79,
                            "agent":"gemini-2.5-pro@objects/4" }
      }
    }
  ],
  "next_cursor": "eyJoIjoxMjM0fQ",
  "commit": "cmt_01J8Z3K7...",
  "total_estimate": 187
}
```

Every collection response carries the `commit` it was read at, so a client can
detect that its view is stale without a second request.

### 6.4 History

```http
GET  /v1/scenes/{id}/commits?limit=50&cursor=...
GET  /v1/scenes/{id}/commits/{cmt}
GET  /v1/scenes/{id}/commits/{cmt}/operations
GET  /v1/scenes/{id}/diff?from=cmt_a&to=cmt_b
POST /v1/scenes/{id}/undo        { "scope": "mine" }
POST /v1/scenes/{id}/redo
POST /v1/scenes/{id}/revert      { "commit": "cmt_..." }
POST /v1/scenes/{id}/versions    { "commit": "cmt_...", "name": "Client review" }
GET  /v1/scenes/{id}/versions
POST /v1/scenes/{id}/branch      { "from": "cmt_...", "name": "Alt palette" }
```

Undo, revert and restore all create **forward** commits. Nothing removes history.

### 6.5 Jobs

```http
POST   /v1/projects/{id}/jobs
GET    /v1/jobs/{id}
GET    /v1/jobs/{id}/events?since=42          poll fallback
DELETE /v1/jobs/{id}                          cancel
POST   /v1/jobs/{id}/retry
GET    /v1/projects/{id}/jobs?status=running
```

```json
{
  "kind": "refine",
  "options": {
    "max_iterations": 8, "target_score": 0.85,
    "backend": "blender.eevee",
    "axes": ["colour", "lighting", "layout"]
  },
  "budget": { "usd": 2.50, "worker_seconds": 1800, "tokens": 200000 }
}
```

```json
{
  "id": "job_01J8Z...",
  "kind": "refine",
  "status": "running",
  "stage": "optimising",
  "progress": { "completed": 5, "total": 8, "fraction": 0.625 },
  "tasks": { "total": 41, "succeeded": 33, "running": 2, "failed": 0 },
  "consumed": { "usd": 1.12, "worker_seconds": 803, "tokens": 91204 },
  "result": null,
  "degraded": [],
  "started_at": "2026-07-29T11:20:03Z",
  "stream_url": "wss://api.archx3d.io/v1/jobs/job_01J8Z.../stream"
}
```

Job kinds: `analyse`, `generate`, `render`, `evaluate`, `plan`, `refine`,
`import`, `export`, `migrate`.

Statuses:

```
queued → running → completed
                 → completed_with_degradation
                 → failed
                 → cancelled
```

`completed_with_degradation` is a distinct terminal state, carrying `degraded[]`
with the stage, the reason and what was missing. This is
`ENGINEERING_PRINCIPLES.md` §8 surfaced to the client: a caller can tell that
vision never ran, which the current API cannot express.

### 6.6 Artefacts

```http
GET /v1/projects/{id}/artefacts?kind=glb&commit=cmt_...
GET /v1/artefacts/{id}                    metadata
GET /v1/artefacts/{id}/content            302 → presigned, immutable, CDN-cached
```

```json
{ "id": "art_01J8Z...", "kind": "glb", "name": "model.glb",
  "digest": "b3:9c1d...", "bytes": 14_002_108,
  "commit": "cmt_01J8Z3K7...", "backend": "blender.eevee",
  "metadata": { "triangles": 412_889, "materials": 34, "draco": true },
  "created_at": "2026-07-29T11:31:44Z" }
```

Artefacts are immutable and content-addressed, so
`Cache-Control: public, max-age=31536000, immutable` is correct rather than
optimistic.

### 6.7 Evaluation

```http
GET  /v1/projects/{id}/evaluations
GET  /v1/evaluations/{id}                         full result
GET  /v1/evaluations/{id}/findings?axis=lighting&min_severity=0.4
GET  /v1/evaluations/{id}/rooms/{space_id}
GET  /v1/evaluations/{id}/viewpoints/{vp_id}
GET  /v1/evaluations/{id}/report                  → HTML
GET  /v1/evaluations/compare?a=evl_x&b=evl_y      regression view
```

Findings serialise exactly as `evaluation.schema.Finding` does today, including
`why`, `evidence`, `remedy`, `objects`, `materials` and the `measured` /
`unmeasured_axes` / `weight_used` treatment. The API does not reshape them: the
document is already the right shape and reshaping it would create a second
vocabulary.

### 6.8 Planning and refinement

```http
POST /v1/scenes/{id}/plans          { "evaluation": "evl_...", "max_actions": 12 }
GET  /v1/plans/{id}
POST /v1/plans/{id}/execute         { "max_iterations": 8, "dry_run": false }
GET  /v1/plans/{id}/attempts        every attempt, including rejected ones
```

`dry_run: true` returns the plan with expected gains and changes nothing —
`optimizer/pipeline.py --dry-run` over HTTP.

### 6.9 Capabilities

```http
GET /v1/capabilities
```

```json
{
  "api_version": "v1",
  "server_version": "2.3.1",
  "schema_version": "2.1",
  "contract_version": 1,
  "render_backends": [
    { "id": "blender.eevee", "engines": ["eevee"],
      "aovs": ["beauty","albedo","depth","normal","material_id","object_id"],
      "deterministic": true, "max_resolution": [7680, 4320] },
    { "id": "blender.cycles", "…": "…" }
  ],
  "importers": ["dxf", "ifc", "gltf", "usd"],
  "exporters": ["glb", "usd", "ifc", "blend", "arx"],
  "vision_providers": [
    { "id": "gemini", "models": ["gemini-2.5-pro","gemini-2.5-flash"], "local": false }
  ],
  "evaluation_axes": ["colour","material","lighting","layout","objects"],
  "plugins": [ { "id": "acme.thermal", "version": "1.4.2", "extensions": ["evaluate.axis"] } ],
  "limits": { "max_upload_bytes": 524288000, "max_images_per_project": 100 }
}
```

Clients discover what a deployment can do rather than assuming. This is what
makes one client work against a stripped self-hosted install and a full cloud
deployment.

---

## 7. Operations and scene mutation over HTTP

The write path.

```http
POST /v1/scenes/{id}/commits
Idempotency-Key: 01J8Z4M...
Content-Type: application/json

{
  "base": "cmt_01J8Z3K7QP4M2N9V6XW0RTB5FC",
  "message": "Move sofa away from the radiator",
  "operations": [
    { "op": "translate", "entity": "ent_01J8Z1B4...", "delta": {"x":0.2,"y":0,"z":0} },
    { "op": "patch_component", "entity": "ent_01J8Z1B4...",
      "component": "core:appearance", "changes": { "colour_hex": "#8B8B86" } }
  ]
}
```

**Success:**

```json
201 Created
{ "commit": "cmt_01J8Z5N2...", "applied": 2, "digest": "b3:2f8c...",
  "inverse_available": true }
```

**Rejected by validation** — nothing applied:

```json
422 Unprocessable Content
{
  "type": "https://archx3d.io/problems/constraint-violation",
  "title": "The change violates a scene constraint",
  "status": 422,
  "violations": [
    { "rule": "LockedEntity", "entity": "ent_01J8Z1B4...",
      "message": "the position of this object was set by a user and is locked",
      "operation_index": 0 }
  ]
}
```

**Conflict** — the scene moved:

```json
409 Conflict
{
  "type": "https://archx3d.io/problems/stale-base",
  "title": "The scene has changed since your base commit",
  "status": 409,
  "head": "cmt_01J8Z5P9...",
  "commits_since": ["cmt_01J8Z5N8...", "cmt_01J8Z5P9..."],
  "rebase_url": "/v1/scenes/scn_.../commits/cmt_01J8Z3K7.../since"
}
```

The client fetches the intervening operations, rebases per
[`SCENE_GRAPH_SPEC.md` §9](SCENE_GRAPH_SPEC.md#9-collaboration-and-conflict-resolution),
and retries. That is the same code path used for live collaboration and for
offline sync — one algorithm, three callers.

### Why not PATCH on entities

| Requirement | `PATCH /entities/{id}` | Operations |
| --- | --- | --- |
| Atomic multi-entity change | several requests, no atomicity | one commit |
| Exact undo | recompute a diff, hope | materialised inverse |
| Attribution | request metadata, lost after | in the journal forever |
| Conflict detection | `If-Match` per entity | one base commit for the batch |
| Offline queue | not expressible | a list of operations |
| Relative intent ("nudge 20 cm") | lost — becomes an absolute set | preserved; commutes |

The last row is the one that decides it. Two users nudging the same sofa in
opposite directions should cancel out, not have one edit silently vanish.

`PATCH` is still offered as sugar for single-field changes from simple clients;
it compiles server-side to exactly one operation and appears in history
identically.

---

## 8. GraphQL

One endpoint: `POST /v1/graphql`. Same auth, same authorisation, same rate limits.

```graphql
type Query {
  project(id: ID!): Project
  projects(first: Int, after: String, filter: ProjectFilter): ProjectConnection!
  scene(id: ID!, at: CommitRef): Scene
  job(id: ID!): Job
  evaluation(id: ID!): Evaluation
  capabilities: Capabilities!
}

type Scene {
  id: ID!
  head: Commit!
  levels: [Level!]!
  spaces(level: ID): [Space!]!
  entities(filter: EntityFilter, first: Int, after: String): EntityConnection!
  entity(id: ID!): Entity
  commits(first: Int, after: String): CommitConnection!
  diff(from: ID!, to: ID!): SceneDiff!
}

type Entity {
  id: ID!
  kind: EntityKind!
  level: Level
  parent: Entity
  children: [Entity!]!
  components: [Component!]!
  component(type: String!): Component
  relationships(predicate: Predicate): [Relationship!]!
  history(first: Int): [Commit!]!          # blame
}

type Component {
  type: String!
  data: JSON!
  provenance: Provenance!
  updatedAt: Commit!
}

type Mutation {
  commit(scene: ID!, base: ID!, operations: [OperationInput!]!,
         message: String): CommitResult!
  submitJob(project: ID!, kind: JobKind!, options: JSON, budget: BudgetInput): Job!
  cancelJob(id: ID!): Job!
}

type Subscription {
  sceneCommits(scene: ID!): Commit!
  jobEvents(job: ID!): JobEvent!
  presence(scene: ID!): PresenceEvent!
}
```

### The query GraphQL exists for

```graphql
query ReviewStep($project: ID!) {
  project(id: $project) {
    name
    scene {
      head { id message author { name } createdAt }
      levels { id storey elevationM }
      spaces {
        id spaceType area
        palette { primary secondary accent source confidence }
        entities(filter: { kinds: [FURNITURE, DECOR] }) {
          nodes {
            id
            component(type: "core:transform") { data }
            component(type: "core:detection") { data provenance { source confidence agent } }
          }
        }
      }
    }
    latestEvaluation {
      totals { score confidence measuredAxes unmeasuredAxes weightUsed }
      findings(minSeverity: 0.3) {
        axis code summary subsystem severity why remedy
        objects { id component(type: "core:label") { data } }
      }
    }
  }
}
```

One request. The REST equivalent is six, and the client must join findings to
entities itself.

### Abuse controls

GraphQL's flexibility is its risk. Enforced:

| Control | Limit |
| --- | --- |
| Query depth | 10 |
| Query complexity | 1,000 points, computed from field costs and pagination sizes |
| Aliases per query | 50 |
| Introspection | disabled in production for unauthenticated callers |
| Batching | max 10 operations per request |
| Persisted queries | required for the first-party web app; arbitrary queries need an elevated scope |
| Timeout | 10 s |

Rate limiting is by **complexity points, not request count**, because one
GraphQL request can cost a thousand REST requests.

### Every resolver is dataloader-batched

`entities → components` must never be N+1. This is enforced by a test that runs
a representative query and asserts the database query count is under a threshold.

---

## 9. WebSocket and streaming

### Endpoints

| Endpoint | Purpose |
| --- | --- |
| `wss://…/v1/scenes/{id}/live` | collaboration: operations, presence, awareness |
| `wss://…/v1/jobs/{id}/stream` | job progress |
| `GET …/v1/jobs/{id}/events` (SSE) | progress where WebSocket is blocked |

### Handshake

```
→ CONNECT   { "token": "...", "scene": "scn_...", "have": "cmt_01J8Z3K7..." }
← WELCOME   { "session": "ses_...", "head": "cmt_01J8Z5P9...",
              "behind": 2, "peers": [ {...} ] }
← COMMIT    { commit, operations }         ×2   (catch-up)
← SYNCED    { "head": "cmt_01J8Z5P9..." }
```

The client declares what it has; the server sends what it is missing. This is the
same catch-up mechanism offline sync uses.

### Frames

| Direction | Type | Payload |
| --- | --- | --- |
| → | `COMMIT` | `{ base, operations, message, client_ref }` |
| ← | `COMMIT_ACK` | `{ client_ref, commit, digest }` |
| ← | `COMMIT_REJECT` | `{ client_ref, reason, violations }` |
| ← | `COMMIT_CONFLICT` | `{ client_ref, head, commits_since }` |
| ← | `COMMIT` | broadcast of another peer's commit |
| ↔ | `PRESENCE` | cursor, selection, viewport frustum, soft lock |
| ↔ | `PING` / `PONG` | 30 s heartbeat |
| ← | `JOB_EVENT` | `{ job, status, stage, progress, message }` |
| ← | `ERROR` | `{ code, message, fatal }` |

### Rules

- **Presence is never journalled.** It is ephemeral, over a separate channel, and
  never appears in history.
- **The server is authoritative.** A client's optimistic application is local
  until acknowledged.
- **Reconnect is a resync**, not a reload: `have` + catch-up. A 30-second network
  drop costs a few frames, not a page refresh.
- **Backpressure**: per-connection frame budget; a client that cannot keep up is
  sent a `RESYNC` and re-catches up from a commit rather than being drowned.
- **Message size cap** 1 MB; larger operation batches go over REST.

### SSE for job progress

Kept because it works everywhere, it is what the existing `useJobStream` hook
consumes, and job progress is one-directional:

```
event: status
data: {"status":"running","stage":"rendering","progress":{"fraction":0.62}}

event: log
data: {"level":"info","message":"viewpoint 5/8 rendered","cache_hit":false}

event: done
data: {"status":"completed_with_degradation","degraded":[{"stage":"vision",…}]}
```

---

## 10. Errors

RFC 9457 problem details. One shape, everywhere.

```json
{
  "type": "https://archx3d.io/problems/quota-exceeded",
  "title": "Job budget exceeded",
  "status": 402,
  "detail": "This job's USD budget of 2.50 was reached after 6 of 8 iterations.",
  "instance": "/v1/jobs/job_01J8Z...",
  "request_id": "req_01J8Z6R...",
  "consumed": { "usd": 2.50, "worker_seconds": 1642 },
  "partial_result": { "evaluation": "evl_01J8Z...", "score": 0.79 },
  "remedy": "Raise the budget and retry, or accept the partial refinement."
}
```

`remedy` is present on every error that a caller can act on. This mirrors the
evaluation engine's `Finding.remedy` and for the same reason: a diagnostic that
does not say what to do is a diagnostic nobody acts on.

`partial_result` is present whenever work completed before the failure. Losing
six iterations of successful refinement because the seventh hit a budget would be
gratuitous.

### Status codes

| Code | Meaning | Retry |
| --- | --- | --- |
| 400 | malformed request | no |
| 401 | missing or invalid credentials | no |
| 402 | quota or budget exhausted | after raising it |
| 403 | authenticated but not permitted | no |
| 404 | not found, or not visible to this tenant | no |
| 409 | conflict — stale base, duplicate idempotency key with a different body | rebase and retry |
| 412 | `If-Match` failed | re-read and retry |
| 413 | payload too large | no |
| 415 | unsupported media type | no |
| 422 | valid syntax, violates a constraint | no |
| 429 | rate limited | after `Retry-After` |
| 499 | client closed the request | — |
| 500 | bug | with backoff |
| 502/504 | upstream failure or timeout | with backoff |
| 503 | shedding load or maintenance | after `Retry-After` |

### Problem types

Stable URLs, documented, machine-matchable:

```
/problems/validation-failed        /problems/constraint-violation
/problems/stale-base               /problems/quota-exceeded
/problems/plugin-unavailable       /problems/backend-unavailable
/problems/schema-version-mismatch  /problems/unsupported-capability
/problems/rate-limited             /problems/budget-exceeded
```

Clients match on `type`, never on `title` or `detail` — those are for humans and
may be localised or reworded.

---

## 11. Rate limiting and quotas

Two distinct mechanisms, often conflated.

**Rate limits** protect the service from bursts. **Quotas** meter consumption
against a plan.

### Rate limits

Token bucket, per principal, per class:

| Class | Sustained | Burst |
| --- | --- | --- |
| Read (`GET`) | 100/s | 300 |
| Write (`POST`/`PATCH`) | 20/s | 60 |
| Job submission | 10/min | 20 |
| Upload initiation | 30/min | 60 |
| GraphQL | 5,000 complexity points/min | 15,000 |
| WebSocket frames | 100/s | 500 |
| Auth attempts | 10/min per IP | 20 |

```http
RateLimit-Limit: 100
RateLimit-Remaining: 43
RateLimit-Reset: 12
Retry-After: 12
```

Limits scale by plan tier. Enterprise deployments configure them.

### Quotas

| Quota | Free | Pro | Enterprise |
| --- | --- | --- | --- |
| Projects | 3 | 100 | unlimited |
| Storage | 5 GB | 500 GB | contract |
| Render minutes / month | 60 | 3,000 | contract |
| Model tokens / month | 500 K | 25 M | contract |
| Concurrent jobs | 1 | 10 | contract |
| Collaborators / project | 1 | 20 | unlimited |
| Retention | 30 d | 2 y | contract |

```http
GET /v1/usage?period=current
```

```json
{ "period": { "start": "2026-07-01T00:00:00Z", "end": "2026-08-01T00:00:00Z" },
  "render_minutes": { "used": 412, "limit": 3000 },
  "model_tokens":   { "used": 8_912_004, "limit": 25_000_000 },
  "storage_bytes":  { "used": 91_204_112_889, "limit": 536_870_912_000 },
  "projected_overage_usd": 0.0 }
```

### Per-job budgets

Independent of plan quotas, and the more important control in practice: a
refinement loop is a loop that spends money, and
`ENGINEERING_PRINCIPLES.md` §10 requires it to be bounded by construction.

```json
"budget": { "usd": 2.50, "worker_seconds": 1800, "tokens": 200000 }
```

On exhaustion, the job stops with `402`, returns the partial result, and reports
what it consumed. It never silently continues, and it never silently stops.

---

## 12. Versioning and deprecation

### Version in the path

`/v1/`, `/v2/`. Not a header, not a query parameter.

The argument against path versioning — that resource URLs should be stable — is
real and loses to three practical facts: a URL is visible in a log, in a curl
command and in a browser address bar; caches and proxies key on it without
configuration; and a developer can tell which version they are calling without
inspecting headers.

### What is additive (no new version)

- New endpoints, new optional query parameters.
- New fields in a response.
- New enum values in a **response** — clients must tolerate unknown values, and
  this is stated in the client-obligations section of every SDK.
- New optional fields in a request.
- New error `type` URLs.

### What requires a new version

- Removing or renaming a field.
- Changing a field's type or units.
- Changing a default.
- Adding a required request field.
- Changing an endpoint's semantics.
- Removing an enum value from a response.
- Making a previously-optional behaviour mandatory.

### Deprecation

```http
Deprecation: @1798761600
Sunset: Sat, 01 Aug 2026 00:00:00 GMT
Link: <https://docs.archx3d.io/migrate/v1-to-v2>; rel="deprecation"
Warning: 299 - "GET /v1/projects/{id}/review is deprecated; use GraphQL or
                /v2/scenes/{id}/entities. Removed 2026-08-01."
```

Timeline:

| Phase | Duration | State |
| --- | --- | --- |
| Announce | — | release notes, changelog, email to affected keys |
| Deprecated | ≥ 12 months | works; headers set; usage tracked per key |
| Sunset warning | final 90 days | in-app and email notices to callers still using it |
| Brownout | 3 windows in the final 30 days | returns `410` for 1 hour, to surface unmigrated clients |
| Removed | — | `410 Gone` with a `Link` to the migration guide |

**Two API versions live simultaneously**, never three. Shipping v3 begins v1's
final sunset.

Deprecated endpoints are **measured**. `archx3d_api_deprecated_calls_total{endpoint, key}`
tells us who to contact. Removing something nobody called is easy; removing
something without knowing who called it is how an integration breaks in
production.

---

## 13. The Python SDK

The reference client, and the same interface as embedding ArchX3D directly.

```python
from archx3d import Client

client = Client(api_key="ak_live_...")          # or Client.local() for in-process

project = client.projects.create(name="Riverside Apartment")
project.sources.upload("plan.dxf", kind="floorplan")
project.sources.upload_many("photos/", kind="photograph")

job = project.jobs.submit("analyse", budget={"usd": 1.00})
for event in job.stream():
    print(event.stage, event.message)
job.wait()

scene = project.scene
for space in scene.spaces():
    print(space.space_type, space.area_m2, len(space.entities()))

with scene.transaction(message="Confirm the review") as tx:
    for entity in scene.entities(kind="furniture", uncertain=True):
        tx.patch(entity, "core:detection", uncertain=False)
    tx.lock(scene.entity("ent_01J8Z1B4..."))

evaluation = project.jobs.submit("evaluate").wait().result
for finding in evaluation.findings(min_severity=0.4):
    print(f"{finding.severity:.2f} {finding.subsystem}: {finding.summary}")
    print(f"       → {finding.remedy}")

project.jobs.submit("refine", options={"max_iterations": 8}).wait()
project.artefacts.download("glb", to="model.glb")
```

### Design points

- **Local and remote are the same object.** `Client.local()` runs in-process
  against a `.arx` file; `Client(api_key=…)` calls the API. Identical surface.
  This is why the pipelines layer exists — both are thin wrappers over it, and a
  notebook written against a local file works unchanged against the cloud.
- **Transactions mirror the scene API**, so the SDK, the HTTP API and the
  internal store share one mental model.
- **Streaming is a generator.** Progress is iteration, not a callback.
- **Sync by default, async available.** `AsyncClient` mirrors it exactly. Most
  users of a CAD-adjacent Python API are in scripts and notebooks, not event
  loops.
- **Typed.** Full annotations, `py.typed`, checked in CI. Findings, components
  and options are typed, so an IDE can complete `finding.subsystem`.
- **Retries and backoff built in**, honouring `Retry-After`, with a total
  deadline rather than a retry count.

---

## 14. The CLI

`archx3d`, a first-class surface. Every capability of the API is reachable, and
the CLI is how the desktop and CI use the system.

```
archx3d project create "Riverside Apartment"
archx3d project list [--json]
archx3d source add plan.dxf photos/*.jpg
archx3d analyse [--model gemini-2.5-pro] [--no-cache] [--budget 1.00]
archx3d review [--open]                       # launches the local review UI
archx3d generate [--backend blender.cycles] [--skip-render]
archx3d evaluate [--images reference_images/] [--report]
archx3d plan [--dry-run] [--max-actions 12]
archx3d refine [--max-iterations 8] [--target 0.85]
archx3d export --format glb|usd|ifc|arx --out model.glb
archx3d scene query 'kind=furniture and confidence>=0.65' --format table
archx3d scene history [--limit 20] [--author me]
archx3d scene undo [--scope mine]
archx3d scene diff cmt_a cmt_b
archx3d job list|watch|cancel|retry <id>
archx3d plugin list|install|remove|doctor
archx3d config show [--explain]
archx3d doctor                                 # environment diagnosis
archx3d serve [--port 8000]                    # local API + UI
```

### Conventions

| Aspect | Rule |
| --- | --- |
| Working project | `.archx3d/` in the current directory, like `.git`; `--project` overrides |
| Output | human-readable by default; `--json` for machines; `--quiet` for scripts |
| Exit codes | `0` success · `1` failure · `2` usage error · `3` **completed with degradation** · `4` budget exhausted · `130` interrupted |
| Progress | TTY-aware; a plain log when piped |
| Colour | auto; `NO_COLOR` and `--no-color` honoured |
| Config | same layered resolution as everything else |
| Credentials | `archx3d auth login` → OS keychain; never a plaintext file |
| Interactivity | never required. Every prompt has a flag |

Exit code 3 exists because CI needs to distinguish "the model built but vision
was skipped" from "the model built correctly", and the current pipeline cannot
express that difference at all.

### `archx3d doctor`

```
ArchX3D 2.3.1

  Python              3.12.4                                    ok
  Blender             5.0.2  (/usr/bin/blender)                 ok
  GPU                 NVIDIA RTX 4070, 12 GB, CUDA 12.4         ok
  Storage             /home/u/.archx3d  (412 GB free)           ok
  Vision provider     gemini  (GEMINI_API_KEY set)              ok
  Plugins             3 active, 1 degraded
                        acme.thermal 1.4.2 — degraded: 3 timeouts in the last run
  Render cache        1.2 GB, 87% hit rate over the last 100 renders
  API                 https://api.archx3d.io  (28 ms)           ok

1 warning. Run `archx3d plugin doctor` for detail.
```

This exists because "Blender not found at `C:\Program Files\Blender Foundation\Blender 5.0\blender.exe`"
is currently a fatal error discovered three stages into a pipeline, and it should
be a diagnosis available in one second before anything starts.

---

## 15. Client libraries

| Language | Package | Generation | Support |
| --- | --- | --- | --- |
| Python | `archx3d` | hand-written (it is the reference) | first-party |
| TypeScript | `@archx3d/client` | generated from OpenAPI + hand-written scene layer | first-party |
| Go | `archx3d-go` | generated | community |
| Rust | `archx3d-rs` | generated | community |
| C# | `ArchX3D.Client` | generated | community, for Unity |

`@archx3d/schema` is separate and dependency-free: the generated types, operation
builders and validators shared by the web app, the desktop app and any embedding
integration. It is generated from `archx3d.scene`'s component and operation
definitions, and it is what makes the "one vocabulary" rule hold across the
language boundary.

### OpenAPI and SDL are generated artefacts

`openapi.json` and `schema.graphql` are generated from the implementation, checked
into the repository, and diffed in CI. An undeclared API change fails the build.
They are also the source for client generation, the documentation site, and the
mock server used in frontend tests.

---

## 16. Webhooks

For integrations that should not poll.

```http
POST /v1/webhooks
{ "url": "https://example.com/hooks/archx3d",
  "events": ["job.completed", "job.failed", "evaluation.created"],
  "secret": "whsec_..." }
```

```http
POST https://example.com/hooks/archx3d
X-ArchX3D-Event: job.completed
X-ArchX3D-Delivery: dlv_01J8Z...
X-ArchX3D-Signature: t=1785312000,v1=5257a8...

{ "event": "job.completed", "created_at": "...",
  "data": { "job": { "id": "job_01J8Z...", "status": "completed", … } } }
```

- **HMAC-SHA256 over `timestamp.body`**, with the timestamp in the signature to
  prevent replay. Receivers must reject a timestamp older than 5 minutes.
- **At-least-once delivery.** `X-ArchX3D-Delivery` is the deduplication key;
  receivers must be idempotent.
- **Retries**: 8 attempts over 24 hours, exponential. Then the endpoint is
  disabled and the owner notified.
- **Events**: `job.*`, `evaluation.created`, `scene.committed` (rate-limited and
  coalesced — a collaborative session must not emit a webhook per keystroke),
  `plugin.degraded`, `quota.threshold`.

---

## 17. Migration from the current server

`server.py` today exposes two overlapping surfaces: a one-shot
`POST /api/generate`, and a wizard flow under `/api/projects`.

| Today | v1 | Notes |
| --- | --- | --- |
| `POST /api/generate` | `POST /v1/projects` + `sources` + `jobs` | the blocking 900-second subprocess call is removed entirely |
| `GET /output/{filename}` | `GET /v1/artefacts/{id}/content` | content-addressed, presigned, immutable; the current path serves a shared directory that two concurrent runs overwrite |
| `POST /api/projects` | `POST /v1/projects` + `POST /v1/projects/{id}/sources` | project creation and DXF upload separate |
| `POST /api/projects/{id}/images` | `POST /v1/projects/{id}/sources` `kind=photograph` | direct-to-storage upload |
| `POST /api/projects/{id}/analyse` | `POST /v1/projects/{id}/jobs` `kind=analyse` | durable job |
| `GET /api/jobs/{id}` | `GET /v1/jobs/{id}` + `stream_url` | survives a restart |
| `GET /api/projects/{id}/review` | GraphQL `ReviewStep`, or `GET /v1/scenes/{id}/entities` | `review.json` was a cached denormalisation with an ad-hoc staleness check; the scene is queried directly instead |
| `POST /api/projects/{id}/edits` | `POST /v1/scenes/{id}/commits` | operations replace the `ReviewEdits` document |
| `POST /api/projects/{id}/validate` | `POST /v1/scenes/{id}/validate` | still report-only, still never corrects |
| `POST /api/projects/{id}/generate` | `POST /v1/projects/{id}/jobs` `kind=generate` | no more staging files into the repo root |
| `GET /api/projects/{id}/model.glb` | `GET /v1/artefacts/{id}/content` | |
| `GET /api/health` | `GET /v1/health`, `GET /v1/ready` | liveness and readiness separated |

### Compatibility

The legacy paths are served by a shim for **12 months** after v1 ships, marked
`Deprecated`/`Sunset`, translating to the new surface. The shim cannot preserve
one behaviour: `POST /api/generate`'s blocking semantics. It returns `202` with a
job, and the deprecation notice says so, because that endpoint's design is
incompatible with a system that scales.

### What v1 gains beyond parity

- Jobs that survive a server restart.
- Concurrent projects that do not overwrite each other's `data/` and `output/`.
- Multi-user, multi-tenant, authenticated, audited.
- Undo, history and collaboration.
- Degradation visible to the caller.
- Cost visible and bounded per job.
- Capability discovery, so one client works against any deployment.

---

## Related

- [`ARCHITECTURE.md`](ARCHITECTURE.md) — layering, why entrypoints are thin, the ports behind these endpoints.
- [`SCENE_GRAPH_SPEC.md`](SCENE_GRAPH_SPEC.md) — operations, commits, conflict resolution, the query model.
- [`PLUGIN_SPEC.md`](PLUGIN_SPEC.md) — how plugin extensions appear in `/v1/capabilities`.
- [`ENGINEERING_PRINCIPLES.md`](ENGINEERING_PRINCIPLES.md) — §5 unmeasured, §6 one vocabulary, §8 degradation, §10 cost.
