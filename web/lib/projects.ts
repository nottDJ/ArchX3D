/**
 * ArchX3D — local project registry
 * ================================
 * The index of projects this browser has created.
 *
 * Why this exists
 * ---------------
 * The backend has no list endpoint. It can create a project, and it can return
 * one by id (`GET /api/projects/{id}`), but there is no `GET /api/projects` —
 * so there is no server-side answer to "what have I made?". A dashboard needs
 * one.
 *
 * The honest options were: change the backend (out of scope), invent plausible
 * data (never), or record locally what the client already knows. This is the
 * third. Every id here was returned by the server to this browser, and every
 * field beyond the id is re-fetched from the server rather than trusted from
 * cache — so the dashboard shows real state, not a local guess at it.
 *
 * What that costs, stated plainly
 * -------------------------------
 * The index is per-browser. Clearing site data loses it; a second machine does
 * not see it; a colleague does not see it. The UI says so rather than
 * pretending otherwise, and `docs/FRONTEND_ARCHITECTURE.md` records the single
 * endpoint that would make it server-side.
 *
 * Nothing is lost when the index is: the projects still exist on the server
 * and a direct link still works. Only discovery is local.
 */

import { API_BASE_URL } from "./api";
import type { ProjectManifest } from "./wizard";

const STORAGE_KEY = "archx3d.projects.v1";

/** What the client records at creation time and the server does not know. */
export interface ProjectRecord {
  id: string;
  /** User-editable. Defaults to the DXF filename, which is what they'd call it. */
  name: string;
  createdAt: string;
  /** Last time the user opened it — drives "Recent". */
  openedAt: string;
  pinned: boolean;
  /** Cached so the list can render before the server responds. */
  stage?: string;
  dxfName?: string;
  imageCount?: number;
  /** Total bytes uploaded, from the manifest. Real, not estimated. */
  bytes?: number;
}

/** A record joined with whatever the server currently says. */
export interface Project extends ProjectRecord {
  manifest: ProjectManifest | null;
  /** True once the server has been asked and answered. */
  synced: boolean;
  /** Set when the server no longer has this project — deleted, or a fresh DB. */
  missing: boolean;
}

/* -------------------------------------------------------------------------- */
/* Storage                                                                    */
/* -------------------------------------------------------------------------- */

function read(): ProjectRecord[] {
  if (typeof window === "undefined") return [];
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (!raw) return [];
    const parsed: unknown = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return parsed.filter(isRecord);
  } catch {
    return [];
  }
}

function write(records: readonly ProjectRecord[]): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(records));
  } catch {
    // Quota or private mode. The session still works; discovery is lost on
    // reload, which is a degradation rather than a failure.
  }
  notify();
}

/** Untrusted input — this survives across versions and is user-editable. */
function isRecord(value: unknown): value is ProjectRecord {
  if (typeof value !== "object" || value === null) return false;
  const record = value as Record<string, unknown>;
  return typeof record.id === "string" && record.id.length > 0;
}

/* -------------------------------------------------------------------------- */
/* Subscription                                                               */
/* -------------------------------------------------------------------------- */

type Listener = () => void;
const listeners = new Set<Listener>();

/** Cached so `useSyncExternalStore` gets a stable reference between renders. */
let snapshot: ProjectRecord[] = [];
let hydrated = false;

function notify(): void {
  snapshot = read();
  for (const listener of listeners) listener();
}

export function subscribe(listener: Listener): () => void {
  listeners.add(listener);

  // Another tab writing the index must update this one — a user who creates a
  // project in a second tab should see it here without a reload.
  if (listeners.size === 1 && typeof window !== "undefined") {
    window.addEventListener("storage", onStorage);
  }

  return () => {
    listeners.delete(listener);
    if (listeners.size === 0 && typeof window !== "undefined") {
      window.removeEventListener("storage", onStorage);
    }
  };
}

function onStorage(event: StorageEvent): void {
  if (event.key === STORAGE_KEY) notify();
}

export function getSnapshot(): ProjectRecord[] {
  if (!hydrated && typeof window !== "undefined") {
    hydrated = true;
    snapshot = read();
  }
  return snapshot;
}

/**
 * Server render has no storage; an empty list is the truthful answer.
 *
 * Must return the *same* array reference on every call — useSyncExternalStore
 * compares with Object.is, and a fresh `[]` literal here is a new reference
 * each time, which React reads as "the store never stops changing" and warns
 * ("getServerSnapshot should be cached") or loops.
 */
const EMPTY_RECORDS: ProjectRecord[] = [];

export function getServerSnapshot(): ProjectRecord[] {
  return EMPTY_RECORDS;
}

/* -------------------------------------------------------------------------- */
/* Mutations                                                                  */
/* -------------------------------------------------------------------------- */

/**
 * Record a project the wizard has just created.
 *
 * Idempotent: re-registering an existing id updates it rather than duplicating,
 * because the wizard can legitimately call this again after a re-upload.
 */
export function register(
  manifest: ProjectManifest,
  name?: string,
): ProjectRecord {
  const now = new Date().toISOString();
  const records = read();
  const existing = records.find((record) => record.id === manifest.project_id);

  const record: ProjectRecord = {
    id: manifest.project_id,
    name: name ?? existing?.name ?? defaultName(manifest),
    createdAt: existing?.createdAt ?? manifest.created_at ?? now,
    openedAt: now,
    pinned: existing?.pinned ?? false,
    ...summarise(manifest),
  };

  write([record, ...records.filter((r) => r.id !== record.id)]);
  return record;
}

/** Refresh the cached summary from a manifest the caller already has. */
export function sync(manifest: ProjectManifest): void {
  const records = read();
  const index = records.findIndex((r) => r.id === manifest.project_id);
  if (index === -1) return;

  records[index] = { ...records[index], ...summarise(manifest) };
  write(records);
}

export function touch(id: string): void {
  const records = read();
  const index = records.findIndex((r) => r.id === id);
  if (index === -1) return;
  records[index] = { ...records[index], openedAt: new Date().toISOString() };
  write(records);
}

export function rename(id: string, name: string): void {
  const trimmed = name.trim().slice(0, 80);
  if (!trimmed) return;
  update(id, { name: trimmed });
}

export function setPinned(id: string, pinned: boolean): void {
  update(id, { pinned });
}

/**
 * Remove from the local index.
 *
 * Deliberately not called "delete": the project still exists on the server,
 * and there is no endpoint to remove it. Presenting this as deletion would be
 * a lie the user only discovers when their disk fills up. The UI says
 * "Remove from list" and explains where the files remain.
 */
export function forget(id: string): void {
  write(read().filter((record) => record.id !== id));
}

export function forgetAll(): void {
  write([]);
}

function update(id: string, patch: Partial<ProjectRecord>): void {
  const records = read();
  const index = records.findIndex((r) => r.id === id);
  if (index === -1) return;
  records[index] = { ...records[index], ...patch };
  write(records);
}

/* -------------------------------------------------------------------------- */
/* Derivation                                                                 */
/* -------------------------------------------------------------------------- */

function summarise(manifest: ProjectManifest): Partial<ProjectRecord> {
  const images = manifest.images ?? [];
  return {
    stage: manifest.stage,
    dxfName: manifest.dxf?.filename,
    imageCount: images.length,
    bytes:
      (manifest.dxf?.bytes ?? 0) +
      images.reduce((total, image) => total + (image.bytes ?? 0), 0),
  };
}

/**
 * A name a person would recognise.
 *
 * The DXF filename minus its extension: it is what the user called the file,
 * so it is what they think the project is. A hex id is not a name.
 */
function defaultName(manifest: ProjectManifest): string {
  const filename = manifest.dxf?.filename;
  if (!filename) return "Untitled project";
  return filename.replace(/\.[^.]+$/, "").replace(/[_-]+/g, " ").trim() || "Untitled project";
}

/* -------------------------------------------------------------------------- */
/* Server join                                                                */
/* -------------------------------------------------------------------------- */

/**
 * Fetch the current manifest for one project.
 *
 * A 404 means the server no longer has it — the projects directory was cleared,
 * or this index came from a different backend. That is reported as `missing`
 * rather than thrown, so one stale entry cannot break the whole dashboard.
 */
export async function fetchManifest(
  id: string,
  signal?: AbortSignal,
): Promise<ProjectManifest | null> {
  const response = await fetch(`${API_BASE_URL}/api/projects/${encodeURIComponent(id)}`, {
    signal,
    headers: { Accept: "application/json" },
  });

  if (response.status === 404) return null;
  if (!response.ok) throw new Error(`Could not load project ${id}`);
  return (await response.json()) as ProjectManifest;
}

/* -------------------------------------------------------------------------- */
/* Sorting and filtering                                                      */
/* -------------------------------------------------------------------------- */

export type ProjectSort = "recent" | "created" | "name" | "size";
export type ProjectFilter = "all" | "ready" | "in-progress" | "pinned";

export const SORT_LABELS: Record<ProjectSort, string> = {
  recent: "Last opened",
  created: "Date created",
  name: "Name",
  size: "Size",
};

export const FILTER_LABELS: Record<ProjectFilter, string> = {
  all: "All projects",
  ready: "Ready to view",
  "in-progress": "In progress",
  pinned: "Pinned",
};

export function matchesFilter(project: Project, filter: ProjectFilter): boolean {
  switch (filter) {
    case "pinned":
      return project.pinned;
    case "ready":
      return project.stage === "generated";
    case "in-progress":
      return project.stage !== "generated";
    default:
      return true;
  }
}

/**
 * Free-text search over name, DXF filename and id.
 *
 * The id is searchable because it is what appears in a URL, so a user pasting
 * one from a colleague's message should find the project.
 */
export function matchesQuery(project: Project, query: string): boolean {
  const needle = query.trim().toLowerCase();
  if (!needle) return true;
  return (
    project.name.toLowerCase().includes(needle) ||
    (project.dxfName ?? "").toLowerCase().includes(needle) ||
    project.id.toLowerCase().includes(needle)
  );
}

/**
 * Sort, pinned first.
 *
 * Pinning outranks every sort: a user who pinned something wants it at the
 * top, and a sort that buries it has ignored an explicit instruction. Ties
 * break on id so the order never wobbles between renders.
 */
export function sortProjects(
  projects: readonly Project[],
  sort: ProjectSort,
): Project[] {
  return [...projects].sort((a, b) => {
    if (a.pinned !== b.pinned) return a.pinned ? -1 : 1;

    switch (sort) {
      case "name":
        return a.name.localeCompare(b.name) || a.id.localeCompare(b.id);
      case "size":
        return (b.bytes ?? 0) - (a.bytes ?? 0) || a.id.localeCompare(b.id);
      case "created":
        return b.createdAt.localeCompare(a.createdAt) || a.id.localeCompare(b.id);
      default:
        return b.openedAt.localeCompare(a.openedAt) || a.id.localeCompare(b.id);
    }
  });
}
