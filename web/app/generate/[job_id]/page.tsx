import type { Metadata } from "next";
import { notFound } from "next/navigation";

import { GenerationDashboard } from "@/components/generate/GenerationDashboard";

/**
 * `/generate/[job_id]` — the real-time generation dashboard.
 *
 * The user lands here immediately after `POST /api/generate` returns a
 * `job_id`. This shell stays a Server Component (so the route params are
 * resolved on the server) and delegates the live SSE connection to a Client
 * Component.
 */

export const metadata: Metadata = {
  title: "Generating your model — ArchX3D",
  description: "Live progress for your DXF to 3D conversion.",
};

/**
 * Progress is inherently live, so never serve a cached shell for this route.
 */
export const dynamic = "force-dynamic";

interface GeneratePageProps {
  // Next.js 15+ delivers route params asynchronously.
  params: Promise<{ job_id: string }>;
}

/**
 * Route segments arrive percent-encoded, so decode once here. Without this the
 * `encodeURIComponent` in `jobStreamUrl` would double-encode the id.
 * A malformed sequence is passed through untouched rather than throwing.
 */
function decodeSegment(raw: string): string {
  try {
    return decodeURIComponent(raw);
  } catch {
    return raw;
  }
}

export default async function GeneratePage({ params }: GeneratePageProps) {
  const { job_id } = await params;
  const jobId = decodeSegment(job_id ?? "").trim();

  // A blank or whitespace-only segment can never map to a real job.
  if (!jobId) notFound();

  return <GenerationDashboard jobId={jobId} />;
}
