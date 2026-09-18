/**
 * ArchX3D — project registry
 * ==========================
 * The projects the dashboard lists, and the preferences a person set on them.
 *
 * Where the truth lives
 * ---------------------
 * Which projects exist is the backend's to say: `GET /api/projects` reads the
 * projects directory on disk. This module keeps, per browser, only what the
 * server does not know — a custom name, a pin, when a project was last opened,
 * and which projects the user asked to hide — and adopts any project found on
 * disk that it has no record of (see `lib/projectIndex.ts`).
 *
 * This used to be the *only* index: the backend had no list endpoint, so a
 * project was discoverable only if this browser remembered creating it.
 * Clearing site data, or a renewed WebView2 profile in the desktop app, left
 * every project folder on disk and none of them reachable. Now losing this
 * browser's data costs pins and custom names, never projects.
 *
 * Every field beyond the id is still re-fetched from the server rather than
 * trusted from cache, so the dashboard shows real state, not a local guess.
 */

import { API_BASE_URL } from "./api";
import {
  adoptServerProjects,
  defaultName,
  summarise,
  type ManifestSummary,
} from "./projectIndex";
import type { ProjectManifest } from "./wizard";

const STORAGE_KEY = "archx3d.projects.v1";
/** Ids the user removed from the list. Their folders are still on disk. */
const HIDDEN_KEY = "archx3d.projects.hidden.v1";

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

function readHidden(): Set<string> {
  if (typeof window === "undefined") return new Set();
  try {
    const parsed: unknown = JSON.parse(window.localStorage.getItem(HIDDEN_KEY) ?? "[]");
    return new Set(Array.isArray(parsed) ? parsed.filter((v): v is string => typeof v === "string") : []);
  } catch {
    return new Set();
  }
}

function writeHidden(hidden: ReadonlySet<string>): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(HIDDEN_KEY, JSON.stringify([...hidden]));
  } catch {
    // Same degradation as the index itself.
  }
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

  // Creating (or re-uploading into) a project the user once hid brings it back.
  const hidden = readHidden();
  if (hidden.delete(record.id)) writeHidden(hidden);

  write([record, ...records.filter((r) => r.id !== record.id)]);
  return record;
}

/**
 * Add every project the server has on disk that this browser does not know.
 *
 * Writes only when something was actually adopted, so calling it on every
 * dashboard load does not churn storage or re-render subscribers.
 */
export function adopt(manifests: readonly ManifestSummary[]): number {
  const records = read();
  const merged = adoptServerProjects(records, manifests, readHidden(), new Date().toISOString());
  if (merged === records) return 0;
  write(merged as ProjectRecord[]);
  return merged.length - records.length;
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
 * Hide a project from this list.
 *
 * Deliberately not called "delete": the project's folder stays on disk.
 * Presenting this as deletion would be a lie the user only discovers when
 * their disk fills up. Because the list is now rebuilt from what is on disk,
 * the id is remembered as hidden — otherwise the project would reappear on the
 * next load.
 */
export function forget(id: string): void {
  const hidden = readHidden();
  hidden.add(id);
  writeHidden(hidden);
  write(read().filter((record) => record.id !== id));
}

export function forgetAll(): void {
  const hidden = readHidden();
  for (const record of read()) hidden.add(record.id);
  writeHidden(hidden);
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

// `summarise` and `defaultName` live in `lib/projectIndex.ts`, shared with the
// on-disk adoption so a rediscovered project is named exactly as a new one is.

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

/**
 * Every project the backend has on disk.
 *
 * Returns `null` when the backend predates the list endpoint (404/405), so an
 * older server degrades to the browser's own index rather than an empty list.
 */
export async function fetchProjectList(
  signal?: AbortSignal,
): Promise<ProjectManifest[] | null> {
  const response = await fetch(`${API_BASE_URL}/api/projects`, {
    signal,
    headers: { Accept: "application/json" },
  });
  if (response.status === 404 || response.status === 405) return null;
  if (!response.ok) throw new Error("Could not list projects");
  const body = (await response.json()) as { projects?: ProjectManifest[] };
  return Array.isArray(body.projects) ? body.projects : [];
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
