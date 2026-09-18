/**
 * ArchX3D — joining the on-disk project list with local preferences
 * =================================================================
 * The backend's projects directory is the authority on which projects exist
 * (`GET /api/projects`). The browser only adds preferences a person set on
 * top: a custom name, a pin, when they last opened it, and whether they asked
 * for a project to be hidden.
 *
 * That split is the P2 fix. The dashboard used to learn a project existed only
 * from `localStorage`, so wiping the WebView profile left every project folder
 * intact on disk and unreachable from the app. Now any project on disk that the
 * browser has no record of is adopted into the list; losing the browser's data
 * loses pins and custom names, never projects.
 *
 * Pure and dependency-free so the merge rules are tested in Node.
 */

/** The subset of a server manifest the index needs. */
export interface ManifestSummary {
  project_id: string;
  created_at?: string;
  updated_at?: string;
  dxf?: { filename: string; bytes?: number } | null;
  images?: Array<{ filename: string; bytes?: number }>;
  stage?: string;
}

export interface IndexRecord {
  id: string;
  name: string;
  createdAt: string;
  openedAt: string;
  pinned: boolean;
  stage?: string;
  dxfName?: string;
  imageCount?: number;
  bytes?: number;
}

/**
 * A name a person would recognise.
 *
 * The DXF filename minus its extension: it is what the user called the file,
 * so it is what they think the project is. A hex id is not a name.
 */
export function defaultName(manifest: ManifestSummary): string {
  const filename = manifest.dxf?.filename;
  if (!filename) return "Untitled project";
  return filename.replace(/\.[^.]+$/, "").replace(/[_-]+/g, " ").trim() || "Untitled project";
}

export function summarise(manifest: ManifestSummary): Partial<IndexRecord> {
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

/** A record for a project found on disk that this browser has never seen. */
export function recordFromManifest(manifest: ManifestSummary, now: string): IndexRecord {
  const created = manifest.created_at || now;
  return {
    id: manifest.project_id,
    name: defaultName(manifest),
    createdAt: created,
    // Never opened in this browser; the last time it changed on disk is the
    // honest stand-in, and keeps a freshly rediscovered list in a sane order.
    openedAt: manifest.updated_at || created,
    pinned: false,
    ...summarise(manifest),
  };
}

/**
 * Local records, plus every server project not already among them.
 *
 * Existing records keep their order and every preference on them. Hidden ids
 * are not re-added — "remove from list" has to stay removed even though the
 * folder is still on disk. Returns the input array itself when nothing changed,
 * so a caller can skip a write.
 */
export function adoptServerProjects<T extends IndexRecord>(
  records: readonly T[],
  manifests: readonly ManifestSummary[],
  hidden: ReadonlySet<string>,
  now: string,
): readonly (T | IndexRecord)[] {
  const known = new Set(records.map((record) => record.id));
  const adopted: IndexRecord[] = [];
  for (const manifest of manifests) {
    const id = manifest?.project_id;
    if (typeof id !== "string" || id.length === 0) continue;
    if (known.has(id) || hidden.has(id)) continue;
    known.add(id);
    adopted.push(recordFromManifest(manifest, now));
  }
  return adopted.length === 0 ? records : [...records, ...adopted];
}
