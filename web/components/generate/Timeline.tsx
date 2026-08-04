"use client";

import { STEP_META, stepIndex, type GenerationStep } from "@/lib/generation";
import { CheckIcon, CrossIcon, SpinnerIcon } from "./icons";

/** Visual state of a single node, derived from the job's current position. */
type NodeState = "complete" | "active" | "failed" | "pending";

export interface TimelineProps {
  /** Last step reached on the happy path. */
  currentStep: GenerationStep;
  /** When true, `currentStep` is rendered as the point of failure. */
  failed: boolean;
  /**
   * Latest message from the stream, shown beneath the active node so the
   * headline detail is visible without reading the console.
   */
  activeMessage?: string;
}

function resolveNodeState(
  index: number,
  activeIndex: number,
  failed: boolean,
): NodeState {
  if (index < activeIndex) return "complete";
  if (index > activeIndex) return "pending";
  return failed ? "failed" : "active";
}

/** Tailwind classes for the circular node, keyed by state. */
const NODE_STYLES: Record<NodeState, string> = {
  complete: "border-success-border bg-success-surface text-success-text",
  active: "border-accent-border bg-accent-surface text-accent-text",
  failed: "border-danger-border bg-danger-surface text-danger-text",
  pending: "border-line bg-surface text-tertiary",
};

const LABEL_STYLES: Record<NodeState, string> = {
  complete: "text-secondary",
  active: "text-primary",
  failed: "text-danger-text",
  pending: "text-tertiary",
};

export function Timeline({ currentStep, failed, activeMessage }: TimelineProps) {
  const activeIndex = stepIndex(currentStep);
  const isFinished = !failed && currentStep === "COMPLETED";

  return (
    <ol className="relative flex flex-col" aria-label="Generation progress">
      {STEP_META.map((step, index) => {
        // The final step reads as "complete", not "in progress", once reached.
        const state =
          isFinished && index === activeIndex
            ? "complete"
            : resolveNodeState(index, activeIndex, failed);

        const isLast = index === STEP_META.length - 1;
        // The connector below a node is filled once that node is behind us.
        const railFilled = index < activeIndex;

        return (
          <li
            key={step.id}
            className="relative flex gap-4 pb-6 last:pb-0"
            aria-current={state === "active" ? "step" : undefined}
          >
            {/* ---- Rail: node + connector ------------------------------- */}
            <div className="relative flex w-9 shrink-0 flex-col items-center">
              <span
                className={[
                  "relative z-10 flex h-9 w-9 items-center justify-center rounded-full border",
                  "transition-colors duration-500 ease-out",
                  NODE_STYLES[state],
                ].join(" ")}
              >
                {/* Soft halo that breathes while the step is running. */}
                {state === "active" && (
                  <span className="animate-pulse-soft absolute inset-0 rounded-full bg-accent-solid/25" />
                )}

                {state === "complete" && (
                  <CheckIcon className="animate-scale-in h-4 w-4" />
                )}
                {state === "failed" && <CrossIcon className="h-4 w-4" />}
                {state === "active" && (
                  <SpinnerIcon className="animate-spin-slow h-4.5 w-4.5" />
                )}
                {state === "pending" && (
                  <span className="h-1.5 w-1.5 rounded-full bg-current" />
                )}
              </span>

              {!isLast && (
                <span className="absolute top-9 bottom-0 w-px bg-surface-hover">
                  {/* Overlay grows to fill the rail as steps complete. */}
                  <span
                    className={[
                      "absolute inset-x-0 top-0 origin-top transition-[height] duration-700 ease-out",
                      failed && index === activeIndex
                        ? "bg-danger-solid/50"
                        : "bg-success-surface",
                      railFilled ? "h-full" : "h-0",
                    ].join(" ")}
                  />
                </span>
              )}
            </div>

            {/* ---- Copy ------------------------------------------------- */}
            <div className="min-w-0 flex-1 pt-1.5">
              <p
                className={[
                  "text-sm font-medium transition-colors duration-500",
                  LABEL_STYLES[state],
                ].join(" ")}
              >
                {step.label}
              </p>

              <p
                className={[
                  "mt-1 truncate text-xs transition-colors duration-500",
                  state === "active"
                    ? "text-accent-text/70"
                    : state === "failed"
                      ? "text-danger-text"
                      : state === "complete"
                        ? "text-tertiary"
                        : "text-disabled",
                ].join(" ")}
                title={state === "active" ? activeMessage : step.hint}
              >
                {state === "active" && activeMessage ? activeMessage : step.hint}
              </p>
            </div>
          </li>
        );
      })}
    </ol>
  );
}
