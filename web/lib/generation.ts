/**
 * ArchX3D — Generation domain model
 * =================================
 * Single source of truth for the job lifecycle emitted by the FastAPI SSE
 * stream at `GET /api/jobs/{job_id}/stream`.
 *
 * Everything downstream (timeline, terminal, routing) derives from the
 * constants here, so adding a pipeline stage only requires touching this file.
 */

/**
 * The ordered "happy path" of the pipeline. Order is significant — the
 * timeline uses array position to decide what is done / active / upcoming.
 */
export const GENERATION_STEPS = [
  "QUEUED",
  "EXTRACTING_DXF",
  "GENERATING_GEOMETRY",
  "BUILDING_SCENE",
  "EXPORTING_GLB",
  "COMPLETED",
] as const;

/** A step on the happy path. */
export type GenerationStep = (typeof GENERATION_STEPS)[number];

/** The terminal failure state, which sits outside the ordered progression. */
export const FAILED = "FAILED" as const;

/** Every status the backend may emit. */
export type JobStatus = GenerationStep | typeof FAILED;

/** The shape of a single `event.data` payload on the SSE stream. */
export interface JobStatusEvent {
  status: JobStatus;
  message: string;
}

/** The step a job sits in before the first event arrives. */
export const FIRST_STEP: GenerationStep = GENERATION_STEPS[0];

/** Statuses after which the backend will send nothing further. */
const TERMINAL_STATUSES: ReadonlySet<JobStatus> = new Set<JobStatus>([
  "COMPLETED",
  FAILED,
]);

/** Human-facing copy for each step of the timeline. */
export interface StepMeta {
  readonly id: GenerationStep;
  /** Short title shown on the timeline node. */
  readonly label: string;
  /** Secondary line shown while the step is pending or complete. */
  readonly hint: string;
}

export const STEP_META: readonly StepMeta[] = [
  {
    id: "QUEUED",
    label: "Queued",
    hint: "Waiting for an available pipeline worker",
  },
  {
    id: "EXTRACTING_DXF",
    label: "Extracting DXF",
    hint: "Reading layers, polylines and structural entities",
  },
  {
    id: "GENERATING_GEOMETRY",
    label: "Generating geometry",
    hint: "Normalising coordinates and solving wall segments",
  },
  {
    id: "BUILDING_SCENE",
    label: "Building scene",
    hint: "Extruding volumes, applying materials and lighting",
  },
  {
    id: "EXPORTING_GLB",
    label: "Exporting GLB",
    hint: "Packing an optimised model for the web viewer",
  },
  {
    id: "COMPLETED",
    label: "Completed",
    hint: "Your 3D model is ready to explore",
  },
] as const;

/** Runtime guard — the network is untrusted, even when it is our own backend. */
export function isJobStatus(value: unknown): value is JobStatus {
  return (
    typeof value === "string" &&
    (value === FAILED || (GENERATION_STEPS as readonly string[]).includes(value))
  );
}

/** True once the stream is guaranteed to be finished. */
export function isTerminalStatus(status: JobStatus): boolean {
  return TERMINAL_STATUSES.has(status);
}

/** Position of a step on the happy path (`-1` for unknown values). */
export function stepIndex(step: GenerationStep): number {
  return GENERATION_STEPS.indexOf(step);
}

/** Completion ratio in the range `[0, 1]`, used for the header progress bar. */
export function stepProgress(step: GenerationStep): number {
  const index = stepIndex(step);
  if (index <= 0) return 0;
  return index / (GENERATION_STEPS.length - 1);
}

/**
 * Parse one raw `event.data` string into a validated event.
 *
 * Returns `null` for anything malformed or unrecognised so the caller can
 * surface the raw line in the console instead of crashing the dashboard.
 */
export function parseJobEvent(raw: string): JobStatusEvent | null {
  let payload: unknown;

  try {
    payload = JSON.parse(raw);
  } catch {
    return null;
  }

  if (typeof payload !== "object" || payload === null) return null;

  const { status, message } = payload as Record<string, unknown>;

  if (!isJobStatus(status)) return null;

  return {
    status,
    // A missing message is tolerable; an invalid status is not.
    message: typeof message === "string" ? message : "",
  };
}
