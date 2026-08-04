"use client";

/**
 * ArchX3D — project hooks
 * =======================
 * React's view of the local registry, joined with live server state.
 *
 * Two-phase load
 * --------------
 * The cached summary renders immediately, then each project is re-fetched and
 * reconciled. That means the dashboard paints instantly from local data and
 * *corrects itself* a moment later, rather than showing a spinner over a list
 * the browser already knows.
 *
 * The alternative — wait for every fetch before painting — makes a page that
 * has all its data feel like it does not.
 */

import { useCallback, useEffect, useMemo, useState, useSyncExternalStore } from "react";

import {
  fetchManifest,
  getServerSnapshot,
  getSnapshot,
  subscribe,
  sync,
  type Project,
  type ProjectRecord,
} from "@/lib/projects";
import type { ProjectManifest } from "@/lib/wizard";

export interface UseProjects {
  projects: Project[];
  /** True until the first server reconciliation finishes. */
  loading: boolean;
  /** Set when the backend could not be reached at all. */
  offline: boolean;
  refresh: () => void;
}

export function useProjects(): UseProjects {
  const records = useSyncExternalStore(subscribe, getSnapshot, getServerSnapshot);

  const [manifests, setManifests] = useState<Map<string, ProjectManifest | null>>(
    () => new Map(),
  );
  const [loading, setLoading] = useState(true);
  const [offline, setOffline] = useState(false);
  const [nonce, setNonce] = useState(0);

  // Depend on the id list rather than the records themselves: renaming or
  // pinning must not trigger a full re-fetch of every manifest.
  const ids = useMemo(() => records.map((record) => record.id).join(","), [records]);

  useEffect(() => {
    if (records.length === 0) {
      setLoading(false);
      setOffline(false);
      return;
    }

    const controller = new AbortController();
    let cancelled = false;

    (async () => {
      setLoading(true);

      const results = await Promise.allSettled(
        records.map((record) => fetchManifest(record.id, controller.signal)),
      );
      if (cancelled) return;

      const next = new Map<string, ProjectManifest | null>();
      let reachable = false;

      results.forEach((result, index) => {
        const id = records[index].id;
        if (result.status === "fulfilled") {
          // A 404 resolves to null — the server answered, it just does not
          // have this one. That is reachable, and a `missing` project.
          reachable = true;
          next.set(id, result.value);
          if (result.value) sync(result.value);
        } else {
          next.set(id, null);
        }
      });

      setManifests(next);
      setOffline(!reachable);
      setLoading(false);
    })();

    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [ids, nonce]); // eslint-disable-line react-hooks/exhaustive-deps

  const projects = useMemo<Project[]>(
    () =>
      records.map((record) => {
        const known = manifests.has(record.id);
        const manifest = manifests.get(record.id) ?? null;
        return {
          ...record,
          manifest,
          synced: known,
          // Only "missing" once the server has actually answered — otherwise
          // every project is briefly marked gone on first paint.
          missing: known && manifest === null && !offline,
          stage: manifest?.stage ?? record.stage,
        };
      }),
    [records, manifests, offline],
  );

  const refresh = useCallback(() => setNonce((n) => n + 1), []);

  return { projects, loading, offline, refresh };
}

/** One project, for a detail view. */
export function useProject(id: string | null): {
  project: Project | null;
  loading: boolean;
} {
  const { projects, loading } = useProjects();
  const project = useMemo(
    () => (id ? (projects.find((candidate) => candidate.id === id) ?? null) : null),
    [projects, id],
  );
  return { project, loading };
}

/**
 * Aggregate figures for the dashboard.
 *
 * Every number here is derived from manifests the server returned. Nothing is
 * estimated, and `bytes` counts only what was actually uploaded — the
 * generated `.blend` and `.glb` live server-side and are not reported by any
 * endpoint, so they are excluded rather than guessed at. The dashboard labels
 * the figure "Uploaded" for exactly that reason.
 */
export function useProjectStats(projects: readonly Project[]) {
  return useMemo(() => {
    let bytes = 0;
    let images = 0;
    let ready = 0;
    let inProgress = 0;

    for (const project of projects) {
      bytes += project.bytes ?? 0;
      images += project.imageCount ?? 0;
      if (project.stage === "generated") ready += 1;
      else inProgress += 1;
    }

    return { total: projects.length, ready, inProgress, images, bytes };
  }, [projects]);
}

export type { Project, ProjectRecord };
