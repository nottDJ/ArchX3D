/**
 * ArchX3D — API surface
 * =====================
 * The FastAPI backend runs on its own origin (default `localhost:8000`), so
 * every URL is built from a single configurable base.
 */

declare global {
  interface Window {
    /** Injected by the desktop shell before any page script runs. */
    __ARCHX3D_API_BASE_URL__?: string;
  }
}

/**
 * Base URL of the FastAPI server, without a trailing slash.
 *
 * Three sources, in order:
 *
 * 1. `window.__ARCHX3D_API_BASE_URL__` — the desktop shell. It starts the
 *    backend on whatever port was free and injects the answer before the page
 *    loads, so a build-time constant cannot be used: the port is not known
 *    until the app launches, and hard-coding 8000 would fail whenever
 *    something else already held it.
 * 2. `NEXT_PUBLIC_API_BASE_URL` — the browser build, fixed at compile time.
 *    Must be `NEXT_PUBLIC_` because `EventSource` is created in the browser.
 * 3. The development default.
 */
export const API_BASE_URL: string = (
  (typeof window !== "undefined" && window.__ARCHX3D_API_BASE_URL__) ||
  process.env.NEXT_PUBLIC_API_BASE_URL ||
  "http://localhost:8000"
).replace(/\/+$/, "");

/** SSE endpoint streaming live status for a single generation job. */
export function jobStreamUrl(jobId: string): string {
  return `${API_BASE_URL}/api/jobs/${encodeURIComponent(jobId)}/stream`;
}

/** Destination the user is handed off to once generation succeeds. */
export function viewerUrl(jobId: string): string {
  return `/viewer?job_id=${encodeURIComponent(jobId)}`;
}

/** The viewer, opened on a wizard project's own build. */
export function projectViewerUrl(projectId: string): string {
  return `/viewer?project_id=${encodeURIComponent(projectId)}`;
}

/**
 * The GLB produced by the one-shot `/api/generate` pipeline.
 *
 * That pipeline writes to the shared `output/` directory rather than to a
 * per-project one, so the URL carries no job id — the job id identifies the
 * *run*, and the run's artefact is whatever is currently in `output/`. A cache
 * buster is therefore essential: two runs produce the same URL, and without it
 * a user's second model is served from the first one's cache.
 */
export function jobModelUrl(jobId: string): string {
  return `${API_BASE_URL}/output/model.glb?job=${encodeURIComponent(jobId)}`;
}

/** The GLB produced by the wizard for one project. */
export function projectModelUrl(projectId: string): string {
  return `${API_BASE_URL}/api/projects/${encodeURIComponent(projectId)}/model.glb`;
}

/* -------------------------------------------------------------------------- */
/* Credentials                                                                */
/* -------------------------------------------------------------------------- */

/**
 * What the server will say about the Gemini key.
 *
 * Deliberately never includes the key itself — only whether one exists, where
 * it came from, and a masked hint so a user can tell *which* key is saved.
 */
export interface ApiKeyStatus {
  readonly configured: boolean;
  /** `environment` outranks `saved`; see `modules/credentials.py`. */
  readonly source: "environment" | "saved" | null;
  /** e.g. `AIza…7f3D`, or `null` when nothing is configured. */
  readonly hint: string | null;
  /** False when the environment supplies the key, which the UI cannot change. */
  readonly editable: boolean;
}

async function credentialRequest(
  method: "GET" | "PUT" | "DELETE",
  body?: unknown,
): Promise<ApiKeyStatus> {
  const response = await fetch(`${API_BASE_URL}/api/settings/api-key`, {
    method,
    headers: body ? { "Content-Type": "application/json" } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });

  if (!response.ok) {
    // FastAPI puts the human-readable reason in `detail`; surfacing it is the
    // difference between "could not save" and "that key is 900 characters".
    const detail = await response
      .json()
      .then((payload) => (payload as { detail?: string }).detail)
      .catch(() => undefined);
    throw new Error(detail ?? `Request failed (${response.status})`);
  }

  return (await response.json()) as ApiKeyStatus;
}

export function fetchApiKeyStatus(): Promise<ApiKeyStatus> {
  return credentialRequest("GET");
}

export function saveApiKey(key: string): Promise<ApiKeyStatus> {
  return credentialRequest("PUT", { key });
}

export function clearApiKey(): Promise<ApiKeyStatus> {
  return credentialRequest("DELETE");
}
