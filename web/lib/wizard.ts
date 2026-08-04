/**
 * ArchX3D — Generation wizard types and API client
 * ================================================
 * Mirrors the FastAPI surface in `server.py` (§ Generation Wizard API).
 *
 * Every shape here is the *response* contract, so a backend change surfaces as
 * a type error rather than a runtime `undefined`.
 */

import { API_BASE_URL } from "./api";

// ---------------------------------------------------------------------------
// Wizard steps
// ---------------------------------------------------------------------------

export const WIZARD_STEPS = ["plan", "images", "review", "generate", "walkthrough"] as const;
export type WizardStep = (typeof WIZARD_STEPS)[number];

export interface StepMeta {
  readonly id: WizardStep;
  readonly label: string;
  readonly hint: string;
  /** Steps the user may reach without completing this one. */
  readonly optional: boolean;
}

export const WIZARD_STEP_META: readonly StepMeta[] = [
  { id: "plan", label: "Floor plan", hint: "Upload the DXF", optional: false },
  { id: "images", label: "Reference images", hint: "Photos, renders or plans", optional: true },
  { id: "review", label: "AI analysis", hint: "Check what was detected", optional: false },
  { id: "generate", label: "Generate", hint: "Build the 3D scene", optional: false },
  { id: "walkthrough", label: "Walkthrough", hint: "Explore the model", optional: false },
];

// ---------------------------------------------------------------------------
// API payloads
// ---------------------------------------------------------------------------

export interface ProjectManifest {
  project_id: string;
  created_at: string;
  dxf: { filename: string; bytes: number } | null;
  images: Array<{ filename: string; bytes: number }>;
  stage: string;
  outputs?: string[];
}

export interface UploadResult {
  manifest: ProjectManifest;
  accepted: Array<{ filename: string; bytes: number }>;
  rejected: Array<{ filename: string; reason: string }>;
}

export type JobStatus =
  | "QUEUED"
  | "EXTRACTING_DXF"
  | "ANALYSING"
  | "READY_FOR_REVIEW"
  | "GENERATING_GEOMETRY"
  | "BUILDING_SCENE"
  | "EXPORTING_GLB"
  | "COMPLETED"
  | "FAILED";

export interface JobState {
  job_id: string;
  project_id: string;
  kind: "analyse" | "generate";
  status: JobStatus;
  message: string;
  events: Array<{ status: JobStatus; message: string; at: number }>;
  error: string | null;
  elapsed_s: number;
}

export type ConfidenceBand = "accept" | "uncertain" | "discard";

export interface ReviewObject {
  id: string;
  category: string;
  label: string;
  group: string;
  room_id: string;
  position: { x: number; y: number; z: number };
  rotation_z: number;
  dimensions: { width: number; depth: number; height: number };
  material: string;
  /** Generic family the material reduces to, e.g. walnut → wood. */
  material_family: string;
  color_hex: string;
  asset: string;
  /** How well the chosen procedural variant matched, 0–1. */
  asset_score: number;
  /** close | fair | approximate | none — `none` means no variant exists. */
  asset_quality: "close" | "fair" | "approximate" | "none";
  confidence: number;
  band: ConfidenceBand;
  uncertain: boolean;
  /** Pinned by the user: transforms are refused and auto-correction skips it. */
  locked: boolean;
  /** floor | wall | ceiling | on_object — what physically carries this object. */
  support: string;
  /** When support is "on_object", the id of the carrier. */
  support_id: string;
  will_build: boolean;
  source_images: string[];
  observation_count: number;
  flags: string[];
  distance_to_nearest_wall: number;
}

export interface ReviewLight {
  id: string;
  kind: string;
  mounting: string;
  position: { x: number; y: number; z: number };
  color_temperature_k: number;
  power_w: number;
  confidence: number;
  uncertain: boolean;
  source_images: string[];
}

export interface ReviewFinish {
  material: string;
  color_hex: string;
  roughness: number;
  metallic: number;
  finish: string;
  description: string;
  confidence: number;
}

/** A room's characteristic colours, by role. */
export interface ColourPalette {
  primary: string;
  secondary: string;
  accent: string;
  lighting: string;
  furniture: string;
  decor: string;
  /** observed | style_prior | mixed — a style-derived palette is a guess. */
  source: string;
  confidence: number;
}

/** Room-scale lighting conditions, distinct from the fixture list. */
export interface LightingEnvironment {
  ambient: number;
  /** Plan heading the daylight arrives from; -1 when unknown. */
  daylight_direction: number;
  daylight_elevation: number;
  window_contribution: number;
  color_temperature_k: number;
  shadow_softness: number;
  time_of_day: "day" | "evening" | "night" | "overcast";
  source: "observed" | "inferred" | "default";
  confidence: number;
}

export interface ReviewRoom {
  id: string;
  room_type: string;
  style: string;
  style_confidence: number;
  palette: ColourPalette | null;
  lighting_environment: LightingEnvironment | null;
  area: number;
  width: number;
  depth: number;
  ceiling_height: number;
  confidence: number;
  polygon: Array<[number, number]>;
  bounds_min: [number, number];
  bounds_max: [number, number];
  connected_to: string[];
  source_images: string[];
  has_imagery: boolean;
  finishes: {
    wall: ReviewFinish | null;
    floor: ReviewFinish;
    ceiling: ReviewFinish;
    ceiling_type: string;
  };
  object_count: number;
  objects: ReviewObject[];
  lights: ReviewLight[];
  openings: Array<{
    id: string;
    kind: string;
    width: number;
    height: number;
    confidence: number;
  }>;
}

export interface ImageProfileView {
  image_id: string;
  file: string;
  image_class: string;
  analysis_mode: "full" | "layout" | "geometry" | "skip";
  medium: "photo" | "render" | "drawing";
  synthetic: boolean;
  room_type_hint: string;
  geometry_trust: number;
  confidence: number;
  source: string;
  contributes_appearance: boolean;
  notes: string[];
}

export interface ReviewPayload {
  schema_version: string;
  generated_at: string | null;
  images: ImageProfileView[];
  image_summary: Record<string, unknown>;
  rooms: ReviewRoom[];
  unassigned_objects: ReviewObject[];
  relationships: Array<{
    subject: string;
    predicate: string;
    object: string;
    confidence: number;
    satisfied: boolean;
  }>;
  totals: {
    rooms: number;
    rooms_with_imagery: number;
    objects: number;
    buildable: number;
    uncertain: number;
    lights: number;
    openings: number;
  };
  confidence: { mean?: number; median?: number; accepted?: number; uncertain?: number };
  warnings: string[];
  ignored: Array<{ reason: string; count: number; explanation: string }>;
  validation: {
    corrected: number;
    uncorrected: number;
    by_kind: Record<string, number>;
    issues: Array<{ kind: string; subject: string; detail: string; corrected: boolean }>;
  };
  /** Vocabularies the edit endpoint accepts, served so dropdowns cannot drift. */
  vocabulary: {
    room_types: string[];
    categories: Array<{
      category: string;
      group: string;
      typical: [number, number, number] | number[];
      support: string;
    }>;
    materials: Array<{
      material: string;
      color_hex: string;
      roughness: number;
      metallic: number;
      applies_to: string[];
    }>;
    ceiling_types: string[];
    light_kinds: Array<{
      kind: string;
      mounting: string;
      power_w: number;
      color_temperature_k: number;
    }>;
    assets: Array<{
      key: string;
      category: string;
      styles: string[];
      materials: string[];
      signature: number[];
    }>;
  };
  elapsed_s: number | null;
}

/**
 * Everything one object override may carry.
 *
 * The transform keys are validated together on the server against the object's
 * final state, so sending a move and a resize in one entry is correct and is
 * judged on the result rather than on either half.
 */
export interface ObjectOverride {
  category?: string;
  room_id?: string;
  label?: string;
  position?: { x: number; y: number };
  rotation_z?: number;
  dimensions?: { width?: number; depth?: number; height?: number };
  locked?: boolean;
  /** Procedural variant key from `vocabulary.assets`. */
  asset?: string;
  material?: string;
  color_hex?: string;
}

export interface FinishPatch {
  material?: string;
  color_hex?: string;
  roughness?: number;
  metallic?: number;
  finish?: string;
}

export interface RoomFinishPatch {
  wall?: FinishPatch;
  floor?: FinishPatch;
  ceiling?: FinishPatch;
  ceiling_type?: string;
}

export interface LightPatch {
  kind?: string;
  mounting?: string;
  position?: { x: number; y: number; z?: number };
  color_temperature_k?: number;
  power_w?: number;
  size?: number;
  length?: number;
}

/** The user's review decisions, sent back before generation. */
export interface ReviewEdits {
  remove_objects?: string[];
  keep_objects?: string[];
  object_overrides?: Record<string, ObjectOverride>;
  room_types?: Record<string, string>;
  remove_lights?: string[];
  room_finishes?: Record<string, RoomFinishPatch>;
  light_overrides?: Record<string, LightPatch>;
  add_objects?: Array<ObjectOverride & { source_id?: string; category?: string; room_id?: string }>;
  add_lights?: Array<LightPatch & { kind: string; room_id: string }>;
}

/** Deterministic re-check of the edited graph, from `POST .../validate`. */
export interface ValidationReport {
  total_issues: number;
  errors: number;
  warnings: number;
  applied: number;
  correctable: number;
  by_kind: Record<string, number>;
  protected_objects: string[];
  issues: Array<{
    kind: string;
    severity: "error" | "warning";
    subject: string;
    detail: string;
    room_id: string;
    applied: boolean;
    target?: string;
    suggestion?: {
      position?: { x: number; y: number; z: number };
      rotation_z?: number;
      dimensions?: { width?: number; depth?: number; height?: number };
    };
  }>;
}

export interface EditResponse {
  report: {
    removed_objects: string[];
    kept_uncertain_objects: string[];
    recategorised: string[];
    moved_between_rooms: string[];
    room_types_changed: string[];
    lights_removed: string[];
    transformed: string[];
    lock_changed: string[];
    added_objects: string[];
    restyled: string[];
    finishes_changed: string[];
    lights_changed: string[];
    lights_added: string[];
    rejected_edits: string[];
    /** Accepted edits with a questionable result, e.g. a deliberate overlap. */
    warnings: string[];
    total_changes: number;
  };
  review: ReviewPayload;
  /** Deterministic re-check run immediately after the edits were applied. */
  validation: ValidationReport;
}

// ---------------------------------------------------------------------------
// Client
// ---------------------------------------------------------------------------

export class WizardApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
    this.name = "WizardApiError";
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${API_BASE_URL}${path}`, init);
  } catch {
    // A network-level failure is almost always "the backend is not running",
    // which is far more useful to say than "Failed to fetch".
    throw new WizardApiError(
      `Cannot reach the ArchX3D server at ${API_BASE_URL}. Is it running?`,
      0,
    );
  }

  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`;
    try {
      const body = await response.json();
      if (typeof body?.detail === "string") detail = body.detail;
    } catch {
      /* keep the status line */
    }
    throw new WizardApiError(detail, response.status);
  }

  return (await response.json()) as T;
}

export const wizardApi = {
  /** Step 1 — create a project from a DXF. */
  async createProject(dxf: File): Promise<ProjectManifest> {
    const body = new FormData();
    body.append("file", dxf);
    return request<ProjectManifest>("/api/projects", { method: "POST", body });
  },

  /** Step 2 — upload any number of reference images in one request. */
  async uploadImages(projectId: string, images: File[]): Promise<UploadResult> {
    const body = new FormData();
    for (const image of images) body.append("files", image);
    return request<UploadResult>(`/api/projects/${projectId}/images`, {
      method: "POST",
      body,
    });
  },

  async deleteImage(projectId: string, filename: string): Promise<ProjectManifest> {
    return request<ProjectManifest>(
      `/api/projects/${projectId}/images/${encodeURIComponent(filename)}`,
      { method: "DELETE" },
    );
  },

  /** Step 3 — start analysis; poll `job` for progress. */
  async analyse(projectId: string, options: Record<string, unknown> = {}): Promise<JobState> {
    return request<JobState>(`/api/projects/${projectId}/analyse`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(options),
    });
  },

  async job(jobId: string): Promise<JobState> {
    return request<JobState>(`/api/jobs/${jobId}`);
  },

  async review(projectId: string): Promise<ReviewPayload> {
    return request<ReviewPayload>(`/api/projects/${projectId}/review`);
  },

  async applyEdits(projectId: string, edits: ReviewEdits): Promise<EditResponse> {
    return request<EditResponse>(`/api/projects/${projectId}/edits`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(edits),
    });
  },

  /**
   * Step 3 — deterministic re-check of the edited graph.
   *
   * Runs no model and no network call beyond this one, so it is cheap enough
   * to invoke after every edit. Report-only unless `apply_corrections` is set.
   */
  async validate(
    projectId: string,
    options: { apply_corrections?: boolean; respect_user_edits?: boolean } = {},
  ): Promise<ValidationReport> {
    return request<ValidationReport>(`/api/projects/${projectId}/validate`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(options),
    });
  },

  /** Step 4 — build the Blender scene from the reviewed graph. */
  async generate(projectId: string, options: Record<string, unknown> = {}): Promise<JobState> {
    return request<JobState>(`/api/projects/${projectId}/generate`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(options),
    });
  },

  imageUrl(projectId: string, filename: string): string {
    return `${API_BASE_URL}/projects/${projectId}/images/${encodeURIComponent(filename)}`;
  },

  modelUrl(projectId: string): string {
    return `${API_BASE_URL}/api/projects/${projectId}/model.glb`;
  },
};

// ---------------------------------------------------------------------------
// Presentation helpers
// ---------------------------------------------------------------------------

/** Terminal job statuses, after which polling should stop. */
export function isJobFinished(status: JobStatus): boolean {
  return status === "COMPLETED" || status === "FAILED" || status === "READY_FOR_REVIEW";
}

export function formatRoomType(value: string): string {
  if (!value || value === "unknown") return "Unidentified";
  return value.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

export function formatCategory(value: string): string {
  return value.replace(/_/g, " ");
}

export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

/** Colour band for a confidence value, matching the backend's thresholds. */
export function confidenceTone(confidence: number): "high" | "medium" | "low" {
  if (confidence >= 0.65) return "high";
  if (confidence >= 0.4) return "medium";
  return "low";
}

/**
 * The object as it would be after an uncommitted override.
 *
 * Edits are shown immediately in the plan while only being sent on Apply, so
 * every consumer needs the same merged view. Doing it here keeps the drag
 * preview, the object list and the inspector from drifting apart.
 */
export function withOverride(object: ReviewObject, override?: ObjectOverride): ReviewObject {
  if (!override) return object;
  return {
    ...object,
    category: override.category ?? object.category,
    room_id: override.room_id ?? object.room_id,
    label: override.label ?? object.label,
    locked: override.locked ?? object.locked,
    rotation_z: override.rotation_z ?? object.rotation_z,
    position: override.position
      ? { ...object.position, x: override.position.x, y: override.position.y }
      : object.position,
    dimensions: override.dimensions
      ? { ...object.dimensions, ...override.dimensions }
      : object.dimensions,
  };
}

/** Bounds the server enforces on a hand-edited dimension, in metres. */
export const MIN_DIMENSION = 0.05;
export const MAX_DIMENSION = 20;

/**
 * Keep a point inside a polygon, mirroring the server's containment rule so a
 * drag cannot produce a placement that Apply would then reject.
 *
 * Only the centre is constrained, which is exactly what the backend checks.
 */
export function clampToPolygon(
  point: { x: number; y: number },
  polygon: number[][],
): { x: number; y: number } {
  if (polygon.length < 3 || pointInPolygon(point, polygon)) return point;

  let best = point;
  let bestDistance = Infinity;
  for (let i = 0; i < polygon.length; i += 1) {
    const a = polygon[i];
    const b = polygon[(i + 1) % polygon.length];
    const candidate = closestPointOnSegment(point, a, b);
    const distance = Math.hypot(candidate.x - point.x, candidate.y - point.y);
    if (distance < bestDistance) {
      bestDistance = distance;
      best = candidate;
    }
  }

  // Step a hair inside the edge; landing exactly on it can read as outside.
  const centroid = polygonCentroid(polygon);
  const toward = Math.hypot(centroid.x - best.x, centroid.y - best.y) || 1;
  return {
    x: best.x + ((centroid.x - best.x) / toward) * 0.01,
    y: best.y + ((centroid.y - best.y) / toward) * 0.01,
  };
}

function pointInPolygon(point: { x: number; y: number }, polygon: number[][]): boolean {
  let inside = false;
  for (let i = 0, j = polygon.length - 1; i < polygon.length; j = i, i += 1) {
    const [xi, yi] = polygon[i];
    const [xj, yj] = polygon[j];
    if (yi > point.y !== yj > point.y) {
      const cross = ((xj - xi) * (point.y - yi)) / (yj - yi) + xi;
      if (point.x < cross) inside = !inside;
    }
  }
  return inside;
}

function closestPointOnSegment(
  point: { x: number; y: number },
  a: number[],
  b: number[],
): { x: number; y: number } {
  const dx = b[0] - a[0];
  const dy = b[1] - a[1];
  const lengthSquared = dx * dx + dy * dy;
  if (lengthSquared === 0) return { x: a[0], y: a[1] };
  const t = Math.max(
    0,
    Math.min(1, ((point.x - a[0]) * dx + (point.y - a[1]) * dy) / lengthSquared),
  );
  return { x: a[0] + t * dx, y: a[1] + t * dy };
}

function polygonCentroid(polygon: number[][]): { x: number; y: number } {
  let x = 0;
  let y = 0;
  for (const [px, py] of polygon) {
    x += px;
    y += py;
  }
  return { x: x / polygon.length, y: y / polygon.length };
}

/** Human-readable explanation of what an image class contributes. */
export const ANALYSIS_MODE_COPY: Record<
  ImageProfileView["analysis_mode"],
  { label: string; detail: string }
> = {
  full: {
    label: "Full analysis",
    detail: "Furniture, materials, lighting and relationships",
  },
  layout: {
    label: "Layout only",
    detail: "Furniture positions from the plan; lighting ignored",
  },
  geometry: {
    label: "Geometry only",
    detail: "Openings and structure; no materials or furniture taken from a drawing",
  },
  skip: {
    label: "Not used",
    detail: "Exterior or site image; contributes nothing to the interior",
  },
};
