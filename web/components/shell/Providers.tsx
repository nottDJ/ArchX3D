"use client";

/**
 * ArchX3D — client providers
 * ==========================
 * The context every page needs, mounted once at the root.
 *
 * Kept to two, deliberately. A provider stack is a tax on every render and a
 * tempting place to put state that belongs closer to where it is used — the
 * settings store and the project registry are both external stores rather than
 * context for exactly that reason, so neither appears here.
 *
 * `TooltipProvider` is global because Radix's shared delay only works across
 * one provider: moving between two toolbar buttons should not re-wait the
 * open delay, and it will if each button carries its own provider.
 */

import { ToastProvider, TooltipProvider } from "@/components/ui";

export function Providers({ children }: { children: React.ReactNode }) {
  return (
    <TooltipProvider>
      <ToastProvider>{children}</ToastProvider>
    </TooltipProvider>
  );
}
