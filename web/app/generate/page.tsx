import type { Metadata } from "next";
import { Suspense } from "react";

import { GenerateClient } from "@/components/generate/GenerateClient";

export const metadata: Metadata = {
  title: "Generating your model — ArchX3D",
  description: "Live progress for your DXF to 3D conversion.",
};

/**
 * `/generate?job_id=…` — the real-time generation dashboard.
 *
 * Suspense boundary because `GenerateClient` reads `useSearchParams`; without
 * one the whole route deopts to client-side rendering at build time. See
 * `GenerateClient.tsx` for why this is a query parameter and not the former
 * `[job_id]` path segment.
 */
export default function GeneratePage() {
  return (
    <Suspense>
      <GenerateClient />
    </Suspense>
  );
}
