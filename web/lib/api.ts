/**
 * ArchX3D — API surface
 * =====================
 * The FastAPI backend runs on its own origin (default `localhost:8000`), so
 * every URL is built from a single configurable base.
 */

/**
 * Base URL of the FastAPI server, without a trailing slash.
 *
 * Configure via `NEXT_PUBLIC_API_BASE_URL` in `.env.local`. It must be a
 * `NEXT_PUBLIC_` variable because `EventSource` is created in the browser.
 */
export const API_BASE_URL: string = (
  process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000"
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
