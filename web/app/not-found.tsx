import Link from "next/link";

import { Button } from "@/components/ui/Button";
import { EmptyState } from "@/components/ui/Feedback";
import { SearchIcon } from "@/components/ui/icons";

/**
 * 404.
 *
 * Two ways forward rather than a dead end, and no illustration — a large
 * graphic on an error page is a moment of whimsy that costs the user time
 * while they look past it for the exit.
 */
export default function NotFound() {
  return (
    <main className="relative flex min-h-screen items-center justify-center overflow-hidden bg-canvas px-5">
      <div aria-hidden className="pattern-grid pointer-events-none absolute inset-0" />
      <div className="relative w-full max-w-md">
        <p className="mb-3 text-center font-mono text-xs text-tertiary">404</p>
        <EmptyState
          icon={<SearchIcon />}
          title="Page not found"
          description="That address does not exist. It may have been a stale bookmark, or a link that has since changed."
          action={
            <Button asChild variant="primary">
              <Link href="/dashboard">Go to dashboard</Link>
            </Button>
          }
          secondaryAction={
            <Button asChild variant="ghost">
              <Link href="/docs">Documentation</Link>
            </Button>
          }
          className="bg-surface/60 backdrop-blur-sm"
        />
      </div>
    </main>
  );
}
