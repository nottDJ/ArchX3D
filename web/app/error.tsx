"use client";

import Link from "next/link";
import { useEffect } from "react";

import { Alert, Button, EmptyState } from "@/components/ui";
import { ErrorIcon } from "@/components/ui/icons";

/**
 * Route-level error boundary.
 *
 * `reset()` re-renders the segment, which recovers from a transient failure —
 * a dropped fetch, a race on first paint — without a full reload. It is the
 * first action offered because it is the one most likely to work.
 *
 * The message is shown rather than hidden behind "something went wrong":
 * users report what they can read, and a digest with no text is unreportable.
 */
export default function GlobalError({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  useEffect(() => {
    // Surfaced for whatever collects console output in this deployment. There
    // is no error-reporting backend to send it to.
    console.error("ArchX3D route error:", error);
  }, [error]);

  return (
    <main className="relative flex min-h-screen items-center justify-center overflow-hidden bg-canvas px-5">
      <div aria-hidden className="pattern-grid pointer-events-none absolute inset-0" />

      <div className="relative w-full max-w-lg space-y-4">
        <EmptyState
          icon={<ErrorIcon />}
          title="Something went wrong"
          description="This page failed to render. Trying again often clears a transient failure."
          action={
            <Button variant="primary" onClick={reset}>
              Try again
            </Button>
          }
          secondaryAction={
            <Button asChild variant="ghost">
              <Link href="/dashboard">Go to dashboard</Link>
            </Button>
          }
          className="bg-surface/60 backdrop-blur-sm"
        />

        {error.message && (
          <Alert tone="danger" title="Error detail">
            <p className="font-mono text-xs break-words">{error.message}</p>
            {error.digest && (
              <p className="mt-2 font-mono text-2xs text-tertiary">
                Digest: {error.digest}
              </p>
            )}
          </Alert>
        )}
      </div>
    </main>
  );
}
