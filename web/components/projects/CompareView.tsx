"use client";

/**
 * ArchX3D — comparison view
 * ==========================
 * Two generated models side by side, at `/compare?a=<project_id>&b=<project_id>`.
 *
 * Independent cameras, not synchronised
 * ---------------------------------------
 * `docs/FRONTEND_ARCHITECTURE.md`'s roadmap names this "buildable on the
 * current API" — it does not claim synchronised cameras exist, and they do
 * not: `Viewer` has no prop through which a sibling instance could drive its
 * camera. Building that is real engineering (a shared camera store plus a
 * frame-loop bridge between two independent R3F canvases), not a viewer
 * option to flip. This ships the honest version — two fully independent,
 * fully controllable viewers — rather than a fake "synced" toggle that does
 * not sync.
 *
 * Full-bleed, no shell
 * ---------------------
 * Matches `/viewer`: a 3D canvas wants every pixel, and each embedded
 * `ViewerClient` already carries its own header (label, download, back link)
 * via the `fill` prop — see `components/viewer/Viewer.tsx`. The outer bar
 * here only adds what neither panel can: the exit action and a combined
 * title.
 */

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";

import { ViewerClient } from "@/components/viewer/ViewerClient";
import { Button, EmptyState, Field, Select } from "@/components/ui";
import { CloseIcon, LayersIcon } from "@/components/ui/icons";
import { useProjects, type Project } from "@/hooks/useProjects";
import { projectModelUrl } from "@/lib/api";
import type { ViewerSource } from "@/types/viewer";

type Side = "a" | "b";

export function CompareView() {
  const { projects, loading } = useProjects();
  const router = useRouter();
  const searchParams = useSearchParams();

  const a = searchParams.get("a");
  const b = searchParams.get("b");
  const ready = projects.filter((project) => project.stage === "generated");

  const setSide = (side: Side, value: string) => {
    const params = new URLSearchParams(searchParams.toString());
    if (value) params.set(side, value);
    else params.delete(side);
    router.replace(`/compare?${params.toString()}`, { scroll: false });
  };

  if (!a || !b) {
    return <Picker projects={ready} loading={loading} a={a} b={b} onPick={setSide} />;
  }

  const sourceA: ViewerSource = {
    url: projectModelUrl(a),
    projectId: a,
    label: labelFor(projects, a),
  };
  const sourceB: ViewerSource = {
    url: projectModelUrl(b),
    projectId: b,
    label: labelFor(projects, b),
  };

  return (
    <main className="flex h-screen flex-col overflow-hidden bg-canvas">
      <header className="flex h-12 shrink-0 items-center justify-between gap-3 border-b border-line-subtle bg-canvas px-4">
        <div className="flex min-w-0 items-center gap-2 text-sm text-secondary">
          <LayersIcon className="size-4 shrink-0 text-tertiary" />
          <span className="truncate">{sourceA.label}</span>
          <span aria-hidden className="shrink-0 text-tertiary">
            vs
          </span>
          <span className="truncate">{sourceB.label}</span>
        </div>
        <Link
          href="/projects"
          className="flex h-8 shrink-0 items-center gap-1.5 rounded-md border border-line px-3 text-xs text-secondary transition-colors hover:border-line-strong hover:text-primary"
        >
          <CloseIcon className="size-3.5" />
          Exit compare
        </Link>
      </header>

      <div className="relative flex min-h-0 flex-1 flex-col md:flex-row">
        <div className="relative h-1/2 w-full border-b border-line-subtle md:h-full md:w-1/2 md:border-r md:border-b-0">
          <ViewerClient source={sourceA} fill backHref="/projects" backLabel="Projects" />
        </div>
        <div className="relative h-1/2 w-full md:h-full md:w-1/2">
          <ViewerClient source={sourceB} fill backHref="/projects" backLabel="Projects" />
        </div>
      </div>
    </main>
  );
}

function labelFor(projects: readonly Project[], id: string): string {
  return projects.find((project) => project.id === id)?.name ?? `Project ${id.slice(0, 8)}`;
}

/**
 * Shown until both `a` and `b` are chosen — by URL, or by picking here.
 */
function Picker({
  projects,
  loading,
  a,
  b,
  onPick,
}: {
  projects: readonly Project[];
  loading: boolean;
  a: string | null;
  b: string | null;
  onPick: (side: Side, value: string) => void;
}) {
  const notEnough = !loading && projects.length < 2;

  return (
    <main className="relative flex min-h-screen items-center justify-center overflow-hidden bg-canvas px-5">
      <div aria-hidden className="pattern-glow pointer-events-none absolute inset-0" />
      <div aria-hidden className="pattern-grid pointer-events-none absolute inset-0" />

      <div className="relative w-full max-w-md">
        {notEnough ? (
          <EmptyState
            icon={<LayersIcon />}
            title="Need two finished builds"
            description="Compare mounts two generated models side by side. Generate at least two projects first."
            action={
              <Button asChild variant="primary">
                <Link href="/projects">Browse projects</Link>
              </Button>
            }
            className="border-line bg-surface/60 backdrop-blur-sm"
          />
        ) : (
          <div className="rounded-xl border border-line bg-raised p-6 shadow-lg edge-highlight">
            <div className="mb-5 flex items-center gap-3">
              <span className="flex size-9 shrink-0 items-center justify-center rounded-md border border-line-subtle bg-sunken text-accent-text">
                <LayersIcon className="size-4" />
              </span>
              <div className="min-w-0">
                <h1 className="text-sm font-semibold text-primary">Compare two builds</h1>
                <p className="text-xs text-tertiary">
                  Pick two finished projects to view side by side.
                </p>
              </div>
            </div>

            <div className="grid gap-3 sm:grid-cols-2">
              <Field label="Left">
                <Select value={a ?? ""} onChange={(event) => onPick("a", event.target.value)}>
                  <option value="">Choose a project…</option>
                  {projects.map((project) => (
                    <option key={project.id} value={project.id} disabled={project.id === b}>
                      {project.name}
                    </option>
                  ))}
                </Select>
              </Field>
              <Field label="Right">
                <Select value={b ?? ""} onChange={(event) => onPick("b", event.target.value)}>
                  <option value="">Choose a project…</option>
                  {projects.map((project) => (
                    <option key={project.id} value={project.id} disabled={project.id === a}>
                      {project.name}
                    </option>
                  ))}
                </Select>
              </Field>
            </div>

            <div className="mt-5 flex justify-end">
              <Link
                href="/projects"
                className="text-xs text-tertiary transition-colors hover:text-primary hover:underline"
              >
                Back to projects
              </Link>
            </div>
          </div>
        )}
      </div>
    </main>
  );
}
