import type { Metadata } from "next";
import { Suspense } from "react";

import { CompareView } from "@/components/projects/CompareView";

export const metadata: Metadata = {
  title: "Compare",
  description: "Two generated models side by side.",
};

/**
 * `/compare?a=<project_id>&b=<project_id>`
 *
 * Suspense boundary because `CompareView` reads `useSearchParams`; without one
 * the whole route deopts to client-side rendering at build time.
 */
export default function ComparePage() {
  return (
    <Suspense>
      <CompareView />
    </Suspense>
  );
}
