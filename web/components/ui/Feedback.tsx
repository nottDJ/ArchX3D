"use client";

/**
 * ArchX3D — Progress, Skeleton, Toast, EmptyState, Alert
 * ======================================================
 * Everything that tells the user what is happening, what went wrong, or what
 * is not there yet.
 *
 * The loading ladder
 * ------------------
 * Which indicator to use is decided by *how long* and *how much is known*:
 *
 *   < 300ms          nothing. A flash of spinner is worse than a brief pause.
 *   300ms – 1s       spinner, in place
 *   > 1s, known %    determinate Progress
 *   > 1s, unknown    indeterminate Progress
 *   page or region   Skeleton in the shape of the content
 *
 * A skeleton is only honest if it matches the layout that replaces it. One
 * that does not causes a visible reflow the moment content lands, which is
 * worse than a plain spinner — it promises a shape and then breaks it.
 *
 * Empty is not the same as loading, and neither is the same as broken. Three
 * states, three components: `Skeleton`, `EmptyState`, `Alert`.
 */

import * as ToastPrimitive from "@radix-ui/react-toast";
import { createContext, useCallback, useContext, useMemo, useState } from "react";

import { Button } from "./Button";
import { cn } from "./cn";
import {
  CheckCircleIcon,
  CloseIcon,
  ErrorIcon,
  InfoIcon,
  SpinnerIcon,
  WarningIcon,
} from "./icons";

/* -------------------------------------------------------------------------- */
/* Spinner                                                                    */
/* -------------------------------------------------------------------------- */

export function Spinner({
  size = "md",
  label = "Loading",
  className,
}: {
  size?: "sm" | "md" | "lg";
  /** Announced to screen readers; visually hidden. */
  label?: string;
  className?: string;
}) {
  const SIZES = { sm: "size-3.5", md: "size-4", lg: "size-6" } as const;
  return (
    <span role="status" className={cn("inline-flex", className)}>
      <SpinnerIcon className={cn("animate-spin-slow", SIZES[size])} />
      <span className="sr-only">{label}</span>
    </span>
  );
}

/* -------------------------------------------------------------------------- */
/* Progress                                                                   */
/* -------------------------------------------------------------------------- */

export function Progress({
  value,
  label,
  hint,
  tone = "accent",
  size = "md",
  className,
}: {
  /** `0..1`, or `null` for indeterminate. */
  value: number | null;
  label?: React.ReactNode;
  hint?: React.ReactNode;
  tone?: "accent" | "success" | "danger";
  size?: "sm" | "md";
  className?: string;
}) {
  const percent = value === null ? null : Math.round(Math.max(0, Math.min(1, value)) * 100);

  const TONES = {
    accent: "bg-accent-solid",
    success: "bg-success-solid",
    danger: "bg-danger-solid",
  } as const;

  return (
    <div className={cn("min-w-0", className)}>
      {(label || hint) && (
        <div className="mb-1.5 flex items-baseline justify-between gap-3">
          {label && <span className="text-xs text-secondary">{label}</span>}
          {hint ?? (
            percent !== null && (
              <span className="font-mono text-2xs text-tertiary tabular-nums">
                {percent}%
              </span>
            )
          )}
        </div>
      )}

      <div
        role="progressbar"
        aria-valuemin={0}
        aria-valuemax={100}
        // Omitted entirely when indeterminate — reporting 0 would announce
        // "0 percent", which claims no progress rather than unknown progress.
        aria-valuenow={percent ?? undefined}
        aria-label={typeof label === "string" ? label : "Progress"}
        className={cn(
          "relative overflow-hidden rounded-full bg-surface-active",
          size === "sm" ? "h-1" : "h-1.5",
        )}
      >
        {percent === null ? (
          <div
            className={cn("absolute inset-y-0 w-1/3 rounded-full", TONES[tone])}
            style={{ animation: "indeterminate 1.4s var(--ease-standard) infinite" }}
          />
        ) : (
          <div
            className={cn(
              "h-full rounded-full transition-[width] duration-[--duration-slow] ease-[--ease-standard]",
              TONES[tone],
            )}
            // A 0% bar is indistinguishable from a broken one, so the fill
            // never goes below a visible sliver once work has started.
            style={{ width: `${Math.max(percent, 1.5)}%` }}
          />
        )}
      </div>
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* Skeleton                                                                   */
/* -------------------------------------------------------------------------- */

export function Skeleton({
  className,
  ...props
}: React.HTMLAttributes<HTMLDivElement>) {
  return (
    <div
      aria-hidden
      className={cn("skeleton rounded-sm", className)}
      {...props}
    />
  );
}

/**
 * A block of skeleton text.
 *
 * The last line is short, because real paragraphs end mid-line. A stack of
 * equal-length bars reads as a loading graphic; ragged ones read as text.
 */
export function SkeletonText({
  lines = 3,
  className,
}: {
  lines?: number;
  className?: string;
}) {
  return (
    <div className={cn("space-y-2", className)}>
      {Array.from({ length: lines }, (_, index) => (
        <Skeleton
          key={index}
          className={cn("h-3", index === lines - 1 ? "w-3/5" : "w-full")}
        />
      ))}
    </div>
  );
}

/** Placeholder in the shape of a project card. */
export function SkeletonCard({ className }: { className?: string }) {
  return (
    <div
      className={cn(
        "rounded-lg border border-line-subtle bg-surface p-4",
        className,
      )}
    >
      <Skeleton className="mb-4 aspect-[4/3] w-full rounded-md" />
      <Skeleton className="h-3.5 w-2/5" />
      <Skeleton className="mt-2 h-2.5 w-3/5" />
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* Alert                                                                      */
/* -------------------------------------------------------------------------- */

export type AlertTone = "info" | "success" | "warning" | "danger";

const ALERT_TONES: Record<
  AlertTone,
  { surface: string; icon: React.ReactNode; role: "status" | "alert" }
> = {
  info: {
    surface: "border-accent-border bg-accent-surface text-accent-text",
    icon: <InfoIcon />,
    role: "status",
  },
  success: {
    surface: "border-success-border bg-success-surface text-success-text",
    icon: <CheckCircleIcon />,
    role: "status",
  },
  warning: {
    surface: "border-warning-border bg-warning-surface text-warning-text",
    icon: <WarningIcon />,
    role: "status",
  },
  danger: {
    surface: "border-danger-border bg-danger-surface text-danger-text",
    icon: <ErrorIcon />,
    role: "alert",
  },
};

/**
 * An inline message about the thing it sits next to.
 *
 * `role` follows severity: `alert` for danger — which interrupts a screen
 * reader, appropriate when something has failed — and `status` for the rest,
 * which is announced politely at the next pause. Marking every message as an
 * alert is how screen-reader users end up muting a product.
 */
export function Alert({
  tone = "info",
  title,
  children,
  action,
  onDismiss,
  className,
}: {
  tone?: AlertTone;
  title?: React.ReactNode;
  children?: React.ReactNode;
  action?: React.ReactNode;
  onDismiss?: () => void;
  className?: string;
}) {
  const meta = ALERT_TONES[tone];

  return (
    <div
      role={meta.role}
      className={cn(
        "flex items-start gap-3 rounded-lg border p-3.5",
        meta.surface,
        className,
      )}
    >
      <span aria-hidden className="mt-px shrink-0 [&_svg]:size-4">
        {meta.icon}
      </span>

      <div className="min-w-0 flex-1">
        {title && <p className="text-sm font-medium">{title}</p>}
        {children && (
          <div className={cn("text-sm leading-relaxed", title && "mt-1 opacity-90")}>
            {children}
          </div>
        )}
        {action && <div className="mt-3 flex gap-2">{action}</div>}
      </div>

      {onDismiss && (
        <button
          type="button"
          onClick={onDismiss}
          aria-label="Dismiss"
          className="-m-1 shrink-0 rounded-sm p-1 opacity-60 transition-opacity hover:opacity-100"
        >
          <CloseIcon className="size-3.5" />
        </button>
      )}
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* Empty state                                                                */
/* -------------------------------------------------------------------------- */

/**
 * Nothing here yet — and what to do about it.
 *
 * An empty state without an action is a dead end. Every one in this product
 * offers the next step, because the moment a user finds an empty list is
 * exactly the moment they are willing to fill it.
 */
export function EmptyState({
  icon,
  title,
  description,
  action,
  secondaryAction,
  className,
}: {
  icon?: React.ReactNode;
  title: React.ReactNode;
  description?: React.ReactNode;
  action?: React.ReactNode;
  secondaryAction?: React.ReactNode;
  className?: string;
}) {
  return (
    <div
      className={cn(
        "flex flex-col items-center justify-center rounded-lg border border-dashed border-line px-6 py-12 text-center",
        className,
      )}
    >
      {icon && (
        <span className="mb-4 flex size-11 items-center justify-center rounded-xl border border-line-subtle bg-surface text-tertiary [&_svg]:size-5">
          {icon}
        </span>
      )}
      <h3 className="text-sm font-semibold text-primary">{title}</h3>
      {description && (
        <p className="mt-1.5 max-w-sm text-sm leading-relaxed text-tertiary">
          {description}
        </p>
      )}
      {(action || secondaryAction) && (
        <div className="mt-5 flex flex-wrap items-center justify-center gap-2">
          {action}
          {secondaryAction}
        </div>
      )}
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* Toast                                                                      */
/* -------------------------------------------------------------------------- */

export interface ToastMessage {
  id: number;
  tone: AlertTone;
  title: string;
  description?: string;
  action?: { label: string; onClick: () => void };
}

interface ToastContextValue {
  toast: (message: Omit<ToastMessage, "id">) => void;
}

const ToastContext = createContext<ToastContextValue | null>(null);

/**
 * Toasts for outcomes the user should know about but need not act on.
 *
 * The rule for choosing between a toast and an `Alert`: a toast is for
 * something that *happened* ("Screenshot saved"); an alert is for something
 * that *is true* ("This project has no reference images"). Transient state in
 * an alert nags; persistent state in a toast vanishes before it is read.
 *
 * Radix handles the part that is easy to get wrong: toasts go in a live
 * region, hovering pauses the dismiss timer, and F6 reaches the region from
 * the keyboard.
 */
export function ToastProvider({ children }: { children: React.ReactNode }) {
  const [messages, setMessages] = useState<ToastMessage[]>([]);

  const toast = useCallback((message: Omit<ToastMessage, "id">) => {
    setMessages((current) => [...current, { ...message, id: Date.now() + Math.random() }]);
  }, []);

  const value = useMemo(() => ({ toast }), [toast]);

  return (
    <ToastContext.Provider value={value}>
      <ToastPrimitive.Provider swipeDirection="right" duration={4500}>
        {children}

        {messages.map((message) => (
          <ToastPrimitive.Root
            key={message.id}
            onOpenChange={(open) => {
              if (!open) {
                setMessages((current) => current.filter((m) => m.id !== message.id));
              }
            }}
            className={cn(
              "flex items-start gap-3 rounded-lg border border-line bg-raised p-3.5 shadow-lg edge-highlight",
              "data-[state=open]:animate-slide-right",
              "data-[swipe=move]:translate-x-[--radix-toast-swipe-move-x]",
              "data-[swipe=cancel]:translate-x-0 data-[swipe=cancel]:transition-transform",
            )}
          >
            <span
              aria-hidden
              className={cn(
                "mt-px shrink-0 [&_svg]:size-4",
                message.tone === "success" && "text-success-text",
                message.tone === "danger" && "text-danger-text",
                message.tone === "warning" && "text-warning-text",
                message.tone === "info" && "text-accent-text",
              )}
            >
              {ALERT_TONES[message.tone].icon}
            </span>

            <div className="min-w-0 flex-1">
              <ToastPrimitive.Title className="text-sm font-medium text-primary">
                {message.title}
              </ToastPrimitive.Title>
              {message.description && (
                <ToastPrimitive.Description className="mt-0.5 text-xs leading-relaxed text-tertiary">
                  {message.description}
                </ToastPrimitive.Description>
              )}
              {message.action && (
                <ToastPrimitive.Action
                  asChild
                  altText={message.action.label}
                  className="mt-2 inline-block"
                >
                  <Button variant="secondary" size="sm" onClick={message.action.onClick}>
                    {message.action.label}
                  </Button>
                </ToastPrimitive.Action>
              )}
            </div>

            <ToastPrimitive.Close asChild>
              <button
                type="button"
                aria-label="Dismiss"
                className="-m-1 shrink-0 rounded-sm p-1 text-tertiary transition-colors hover:text-primary"
              >
                <CloseIcon className="size-3.5" />
              </button>
            </ToastPrimitive.Close>
          </ToastPrimitive.Root>
        ))}

        <ToastPrimitive.Viewport
          className={cn(
            "fixed right-0 bottom-0 z-[60] flex w-full max-w-sm flex-col gap-2 p-4 outline-none",
            // Above the viewer's floating toolbar, which is bottom-centred.
            "sm:bottom-2",
          )}
        />
      </ToastPrimitive.Provider>
    </ToastContext.Provider>
  );
}

export function useToast(): ToastContextValue {
  const context = useContext(ToastContext);
  if (!context) {
    // A missing provider is a wiring bug, and returning a no-op would hide it
    // until someone noticed that confirmations had stopped appearing.
    throw new Error("useToast must be used inside <ToastProvider>");
  }
  return context;
}
