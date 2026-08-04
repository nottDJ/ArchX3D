import type { Metadata } from "next";

import { Wizard } from "@/components/wizard/Wizard";

/**
 * `/new` — the generation wizard.
 *
 * Five steps: upload the DXF, add reference images, review what the AI
 * understood, generate, then open the result. The review step exists so the
 * user sees the reconstruction *before* spending render time on it.
 */

export const metadata: Metadata = {
  title: "New generation — ArchX3D",
  description: "Turn a DXF floor plan and reference images into a 3D model.",
};

export default function NewProjectPage() {
  return <Wizard />;
}
