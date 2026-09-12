import type { Metadata } from "next";
import { Suspense } from "react";

import { ProjectsView } from "@/components/projects/ProjectsView";

export const metadata: Metadata = {
  title: "Projects",
  description: "Every project this browser has created.",
};

/**
 * `/projects?q=…&filter=…&sort=…`
 *
 * Suspense boundary because `ProjectsView` reads `useSearchParams`; without
 * one the whole route deopts to client-side rendering at build time.
 */
export default function ProjectsPage() {
  return (
    <Suspense>
      <ProjectsView />
    </Suspense>
  );
}
