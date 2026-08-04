import type { Metadata } from "next";
import Link from "next/link";

import { ViewerClient } from "@/components/viewer/ViewerClient";
import { Button } from "@/components/ui/Button";
import { EmptyState } from "@/components/ui/Feedback";
import { CubeIcon } from "@/components/ui/icons";
import { jobModelUrl, projectModelUrl } from "@/lib/api";
import type { ViewerSource } from "@/types/viewer";

/**
 * `/viewer` — the interactive architectural viewer.
 *
 * Two ways in, because there are two pipelines:
 *
 * * `?project_id=…` — the wizard, which builds into a per-project directory.
 * * `?job_id=…`     — the one-shot `/api/generate` run, which writes to the
 *                     shared `output/` directory.
 *
 * Both resolve to a GLB URL and nothing else; the viewer does not care which
 * produced it, and adding a third source means adding a branch here and
 * touching nothing downstream.
 *
 * A server component so the query string is read without a client round trip
 * and without wrapping the whole viewer in a Suspense boundary for
 * `useSearchParams`.
 */

export const metadata: Metadata = {
  title: "Viewer — ArchX3D",
  description: "Explore your generated building in the browser.",
};

interface ViewerPageProps {
  searchParams: Promise<{
    project_id?: string | string[];
    job_id?: string | string[];
  }>;
}

/** Query values arrive as `string | string[]`; take the first meaningful one. */
function first(value: string | string[] | undefined): string | undefined {
  if (Array.isArray(value)) return value.find((entry) => entry.length > 0);
  return value && value.length > 0 ? value : undefined;
}

export default async function ViewerPage({ searchParams }: ViewerPageProps) {
  const params = await searchParams;
  const projectId = first(params.project_id);
  const jobId = first(params.job_id);

  if (!projectId && !jobId) return <MissingSource />;

  const source: ViewerSource = projectId
    ? {
        url: projectModelUrl(projectId),
        projectId,
        label: `Project ${projectId.slice(0, 8)}`,
      }
    : {
        url: jobModelUrl(jobId!),
        jobId,
        label: `Build ${jobId!.slice(0, 8)}`,
      };

  return (
    <ViewerClient
      source={source}
      backHref={projectId ? "/new" : "/"}
      backLabel={projectId ? "Wizard" : "Home"}
    />
  );
}

/**
 * Reached by opening `/viewer` with no model to show — a stale bookmark, or a
 * hand-typed URL. Says what is missing and offers the two ways to get a model,
 * rather than 404-ing on a route that legitimately exists.
 */
function MissingSource() {
  return (
    <main className="relative flex min-h-screen items-center justify-center overflow-hidden bg-canvas px-5">
      <div aria-hidden className="pattern-glow pointer-events-none absolute inset-0" />
      <div aria-hidden className="pattern-grid pointer-events-none absolute inset-0" />

      <div className="relative w-full max-w-md">
        <EmptyState
          icon={<CubeIcon />}
          title="No model to show"
          description="The viewer opens a generated building. Start a generation, or open this page with a project or job ID."
          action={
            <Button asChild variant="primary">
              <Link href="/new">Start a generation</Link>
            </Button>
          }
          secondaryAction={
            <Button asChild variant="secondary">
              <Link href="/projects">Browse projects</Link>
            </Button>
          }
          className="border-line bg-surface/60 backdrop-blur-sm"
        />
      </div>
    </main>
  );
}
