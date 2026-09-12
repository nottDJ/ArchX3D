"use client";

/**
 * ArchX3D — generation dashboard entry point
 * ============================================
 * Reads `job_id` from the query string and hands off to `GenerationDashboard`.
 *
 * Query parameter, not a `[job_id]` path segment
 * -------------------------------------------------
 * A static-export build must pre-render every route at build time; a `[job_id]`
 * segment would need every possible job id enumerated via `generateStaticParams`,
 * which is impossible since ids are minted at runtime by the backend. Reading
 * the id client-side from `?job_id=…` avoids the problem entirely — the same
 * pattern `/viewer` (`?project_id=`/`?job_id=`) and `/compare` (`?a=`/`?b=`)
 * already use.
 */

import Link from "next/link";
import { useSearchParams } from "next/navigation";

import { Button } from "@/components/ui/Button";
import { EmptyState } from "@/components/ui/Feedback";
import { CubeIcon } from "@/components/ui/icons";

import { GenerationDashboard } from "./GenerationDashboard";

export function GenerateClient() {
  const searchParams = useSearchParams();
  const jobId = (searchParams.get("job_id") ?? "").trim();

  if (!jobId) return <MissingJob />;

  return <GenerationDashboard jobId={jobId} />;
}

/**
 * Reached with no job id — a stale bookmark, or a hand-typed URL. Matches the
 * `/viewer` route's `MissingSource` treatment rather than a hard 404: this is
 * a route that legitimately exists, just without its required parameter.
 */
function MissingJob() {
  return (
    <main className="relative flex min-h-screen items-center justify-center overflow-hidden bg-canvas px-5">
      <div aria-hidden className="pattern-glow pointer-events-none absolute inset-0" />
      <div aria-hidden className="pattern-grid pointer-events-none absolute inset-0" />

      <div className="relative w-full max-w-md">
        <EmptyState
          icon={<CubeIcon />}
          title="No job to show"
          description="This page tracks a generation job's progress. Start one, or open this page with a job id."
          action={
            <Button asChild variant="primary">
              <Link href="/new">Start a generation</Link>
            </Button>
          }
          className="border-line bg-surface/60 backdrop-blur-sm"
        />
      </div>
    </main>
  );
}
