"use client";

/**
 * ArchX3D — Loading and error states
 * ==================================
 * What the user looks at while a 14 MB building downloads, and what they read
 * when it does not.
 *
 * Real progress, not a spinner
 * ----------------------------
 * The model is large and the wait is measured in seconds, so an indeterminate
 * spinner is the wrong instrument — it says "something is happening" when the
 * user's question is "how much longer". A byte count and a bar answer that.
 * When the server sends no `Content-Length` — the FastAPI static mount does,
 * but a proxy may strip it — the bar falls back to an indeterminate sweep and
 * the byte counter keeps ticking, which still carries the information.
 *
 * Errors name the fix
 * -------------------
 * `describeLoadError` has already turned the loader's uninformative failure
 * into one of three actionable messages. This component's job is to present it
 * with the action attached, because "Failed to load" with a Retry button that
 * will fail identically is worse than useless.
 */

import { formatBytes } from "@/lib/format";
import type { LoadState } from "@/types/viewer";
import { Button } from "@/components/ui";
import { CubeIcon, RetryIcon, SpinnerIcon, WarningIcon } from "./icons";

export interface LoadingOverlayProps {
  readonly state: LoadState;
  readonly onRetry: () => void;
  /** Rendered under the error, e.g. a link back to the wizard. */
  readonly children?: React.ReactNode;
}

export function LoadingOverlay({ state, onRetry, children }: LoadingOverlayProps) {
  if (state.phase === "ready" || state.phase === "idle") return null;

  if (state.phase === "error") {
    return (
      <Backdrop>
        <div
          role="alert"
          className="w-full max-w-md rounded-2xl border border-danger-border bg-danger-surface p-6 backdrop-blur-xl"
        >
          <div className="mb-3 flex items-center gap-2.5">
            <WarningIcon className="h-4 w-4 shrink-0 text-danger-text" />
            <h2 className="text-sm font-semibold text-danger-text">
              The model could not be loaded
            </h2>
          </div>

          <p className="text-sm leading-relaxed text-danger-text">
            {state.error ?? "An unknown error occurred."}
          </p>

          <div className="mt-5 flex flex-wrap gap-2.5">
            <Button variant="primary" onClick={onRetry} icon={<RetryIcon />}>
              Try again
            </Button>
            {children}
          </div>
        </div>
      </Backdrop>
    );
  }

  const determinate = state.progress !== null;
  const percent = determinate ? Math.round(state.progress! * 100) : null;
  const parsing = state.phase === "parsing";

  return (
    <Backdrop>
      <div className="w-full max-w-sm text-center">
        <span className="mx-auto mb-6 flex h-11 w-11 items-center justify-center rounded-xl border border-line bg-surface text-accent-text">
          {parsing ? (
            <SpinnerIcon className="animate-spin-slow h-5 w-5" />
          ) : (
            <CubeIcon className="h-5 w-5" />
          )}
        </span>

        <h2 className="text-sm font-semibold text-primary">
          {parsing ? "Preparing the model" : "Loading the model"}
        </h2>

        <p className="mt-1.5 font-mono text-xs text-tertiary tabular-nums">
          {parsing
            ? "Building geometry and materials…"
            : state.totalBytes
              ? `${formatBytes(state.loadedBytes)} of ${formatBytes(state.totalBytes)}`
              : state.loadedBytes > 0
                ? formatBytes(state.loadedBytes)
                : "Connecting…"}
        </p>

        <div
          role="progressbar"
          aria-valuemin={0}
          aria-valuemax={100}
          aria-valuenow={percent ?? undefined}
          aria-label="Model loading progress"
          className="relative mt-6 h-1 overflow-hidden rounded-full bg-surface-hover"
        >
          {determinate ? (
            <div
              className="h-full rounded-full bg-accent-solid transition-[width] duration-200 ease-out"
              style={{ width: `${Math.max(percent ?? 0, 2)}%` }}
            />
          ) : (
            // No Content-Length: sweep instead of lying about a percentage.
            <div className="h-full w-1/3 rounded-full bg-gradient-to-r from-transparent via-accent-solid to-transparent">
              <span className="archx-sheen block h-full w-full rounded-full" />
            </div>
          )}
        </div>

        {determinate && (
          <p className="mt-2 font-mono text-[11px] text-tertiary tabular-nums">
            {percent}%
          </p>
        )}
      </div>
    </Backdrop>
  );
}

function Backdrop({ children }: { children: React.ReactNode }) {
  return (
    <div className="absolute inset-0 z-30 flex items-center justify-center bg-canvas/85 px-5 backdrop-blur-sm">
      {children}
    </div>
  );
}
