import type { Metadata } from "next";

import { DashboardView } from "@/components/projects/DashboardView";

export const metadata: Metadata = {
  title: "Dashboard",
  description: "Your recent projects, running work and storage.",
};

export default function DashboardPage() {
  return <DashboardView />;
}
