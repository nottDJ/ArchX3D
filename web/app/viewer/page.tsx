import type { Metadata } from "next";
import { Suspense } from "react";

import { ViewerPageClient } from "@/components/viewer/ViewerPageClient";

export const metadata: Metadata = {
  title: "Viewer — ArchX3D",
  description: "Explore your generated building in the browser.",
};

/**
 * `/viewer?project_id=…` or `/viewer?job_id=…`
 *
 * Suspense boundary because `ViewerPageClient` reads `useSearchParams`;
 * without one the whole route deopts to client-side rendering at build time.
 * See `ViewerPageClient.tsx` for the source-resolution logic.
 */
export default function ViewerPage() {
  return (
    <Suspense>
      <ViewerPageClient />
    </Suspense>
  );
}
