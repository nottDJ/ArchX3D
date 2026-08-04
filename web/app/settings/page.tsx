import type { Metadata } from "next";

import { SettingsView } from "@/components/settings/SettingsView";

export const metadata: Metadata = {
  title: "Settings",
  description: "Appearance, viewer defaults and the local project index.",
};

export default function SettingsPage() {
  return <SettingsView />;
}
