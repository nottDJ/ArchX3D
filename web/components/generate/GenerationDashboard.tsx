"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useMemo, useRef, useState } from "react";

import { useJobStream } from "@/hooks/useJobStream";
import { viewerUrl } from "@/lib/api";
import { FAILED, stepProgress, STEP_META, stepIndex } from "@/lib/generation";
import { Timeline } from "./Timeline";
import { TerminalLog } from "./TerminalLog";
import { Button } from "@/components/ui";
import { CubeIcon, RetryIcon } from "./icons";

/**
 * Deliberate pause on the success state before navigating, so the completed
 * timeline is legible rather than flashing past.
 */
const REDIRECT_DELAY_MS = 1600;

export interface GenerationDashboardProps {
  jobId: string;
}

/** `mm:ss` elapsed clock, frozen once the job reaches a terminal state. */
function useElapsed(running: boolean): string {
  const startedAt = useRef(Date.now());
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    if (!running) return;
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, [running]);

  const seconds = Math.max(0, Math.floor((now - startedAt.current) / 1000));
  const mm = String(Math.floor(seconds / 60)).padStart(2, "0");
  const ss = String(seconds % 60).padStart(2, "0");
  return `${mm}:${ss}`;
}

export function GenerationDashboard({ jobId }: GenerationDashboardProps) {
  const router = useRouter();
  const { status, currentStep, logs, connection, error, reconnect } =
    useJobStream(jobId);

  const isFailed = status === FAILED;
  const isComplete = status === "COMPLETED";
  const isRunning = !isFailed && !isComplete;

  const elapsed = useElapsed(isRunning);
  const destination = useMemo(() => viewerUrl(jobId), [jobId]);

  /** Latest backend message, surfaced next to the active timeline node. */
  const activeMessage = useMemo(() => {
    for (let i = logs.length - 1; i >= 0; i -= 1) {
      if (logs[i].status) return logs[i].message;
    }
    return undefined;
  }, [logs]);

  // Warm the viewer route so the hand-off after completion is instant.
  useEffect(() => {
    router.prefetch(destination);
  }, [router, destination]);

  // Hand off to the 3D viewer once generation succeeds.
  useEffect(() => {
    if (!isComplete) return;
    const timer = setTimeout(() => router.push(destination), REDIRECT_DELAY_MS);
    return () => clearTimeout(timer);
  }, [isComplete, router, destination]);

  const progress = stepProgress(currentStep);

  const headline = isFailed
    ? "Generation failed"
    : isComplete
      ? "Model ready"
      : STEP_META[Math.max(0, stepIndex(currentStep))].label;

  return (
    <main
      className=" relative min-h-screen overflow-hidden bg-canvas text-primary antialiased"
    >
      {/* Ambient backdrop — a soft accent bloom plus a faint blueprint grid. */}
      <div
        aria-hidden
        className="pointer-events-none absolute inset-0 bg-[radial-gradient(60rem_40rem_at_50%_-10%,rgba(56,132,255,0.13),transparent_70%)]"
      />
      <div aria-hidden className="pattern-grid pointer-events-none absolute inset-0" />

      <div className="relative mx-auto w-full max-w-3xl px-5 py-14 sm:py-20">
        {/* ---- Header --------------------------------------------------- */}
        <header className="mb-10">
          <div className="mb-5 flex items-center gap-2.5">
            <span className="flex h-7 w-7 items-center justify-center rounded-md border border-line bg-surface text-accent-text">
              <CubeIcon className="h-4 w-4" />
            </span>
            <span className="text-[13px] font-medium tracking-tight text-secondary">
              ArchX3D
            </span>
          </div>

          <div className="flex flex-wrap items-end justify-between gap-x-6 gap-y-3">
            <div className="min-w-0">
              <h1 className="text-2xl font-semibold tracking-tight text-primary sm:text-[28px]">
                {headline}
              </h1>
              <p className="mt-1.5 text-sm text-tertiary">
                {isFailed
                  ? "The pipeline stopped before producing a model."
                  : isComplete
                    ? "Taking you to the viewer…"
                    : "Converting your floor plan into a 3D model."}
              </p>
            </div>

            <dl className="flex items-center gap-5 text-right">
              <div>
                <dt className="font-mono text-[10px] tracking-widest text-tertiary uppercase">
                  Elapsed
                </dt>
                <dd className="mt-0.5 font-mono text-sm tabular-nums text-secondary">
                  {elapsed}
                </dd>
              </div>
              <div className="min-w-0">
                <dt className="font-mono text-[10px] tracking-widest text-tertiary uppercase">
                  Job
                </dt>
                <dd
                  className="mt-0.5 max-w-[10rem] truncate font-mono text-sm text-secondary"
                  title={jobId}
                >
                  {jobId}
                </dd>
              </div>
            </dl>
          </div>

          {/* ---- Progress bar ------------------------------------------- */}
          <div
            role="progressbar"
            aria-valuemin={0}
            aria-valuemax={100}
            aria-valuenow={Math.round(progress * 100)}
            aria-label="Generation progress"
            className="relative mt-7 h-1 overflow-hidden rounded-full bg-surface-hover"
          >
            <div
              className={[
                "h-full rounded-full transition-[width] duration-700 ease-out",
                isFailed
                  ? "bg-danger-solid"
                  : "bg-accent-solid",
              ].join(" ")}
              style={{ width: `${Math.max(progress * 100, 3)}%` }}
            >
              {/* Travelling sheen so an unchanging bar still reads as "working". */}
              {isRunning && (
                <span className="archx-sheen block h-full w-full rounded-full" />
              )}
            </div>
          </div>
        </header>

        {/* ---- Timeline --------------------------------------------------- */}
        <div className="rounded-2xl border border-line-subtle bg-surface p-6 backdrop-blur-sm sm:p-7">
          <Timeline
            currentStep={currentStep}
            failed={isFailed}
            activeMessage={activeMessage}
          />
        </div>

        {/* ---- Failure panel ---------------------------------------------- */}
        {isFailed && (
          <div
            role="alert"
            className="animate-rise-in mt-5 rounded-2xl border border-danger-border bg-danger-surface p-5 sm:p-6"
          >
            <h2 className="text-sm font-semibold text-danger-text">
              Something went wrong
            </h2>
            <p className="mt-1.5 text-sm leading-relaxed text-danger-text">
              {error ?? "The generation pipeline reported an unknown failure."}
            </p>

            <div className="mt-5 flex flex-wrap gap-2.5">
              <Button variant="primary" onClick={reconnect} icon={<RetryIcon />}>
                Try again
              </Button>
              <Button asChild variant="secondary">
                <Link href="/new">Upload a different plan</Link>
              </Button>
            </div>
          </div>
        )}

        {/* ---- Success panel ---------------------------------------------- */}
        {isComplete && (
          <div className="animate-rise-in mt-5 flex flex-wrap items-center justify-between gap-4 rounded-2xl border border-success-border bg-success-surface p-5 sm:p-6">
            <div>
              <h2 className="text-sm font-semibold text-success-text">
                Your model is ready
              </h2>
              <p className="mt-1 text-sm text-success-text">
                Redirecting to the 3D viewer…
              </p>
            </div>
            {/* Manual fallback in case the automatic push is blocked. */}
            <Button asChild variant="primary">
              <Link href={destination}>Open viewer</Link>
            </Button>
          </div>
        )}

        {/* ---- Console ------------------------------------------------------ */}
        <div className="mt-5">
          <TerminalLog logs={logs} connection={connection} running={isRunning} />
        </div>

        <p className="mt-6 text-center text-xs text-disabled">
          Keep this tab open — progress streams live from the render worker.
        </p>
      </div>
    </main>
  );
}
