/**
 * ArchX3D — Badge, status and keyboard hints
 * ==========================================
 * Small pieces of labelled state.
 *
 * Colour is never the only signal
 * ------------------------------
 * Roughly 1 in 12 men has a colour-vision deficiency, and red/green is the
 * commonest confusion — exactly the pair a status system reaches for first. So
 * every status here carries a **shape** as well as a hue: a filled dot for
 * running, a ring for idle, a cross for failed, a tick for complete. Turn the
 * page greyscale and it still reads.
 *
 * That is also why `StatusDot` always renders text beside it in this product.
 * A bare coloured dot in a table is a puzzle, not information.
 */

import { cn } from "./cn";
import { CheckIcon, CloseIcon, SpinnerIcon, WarningIcon } from "./icons";

/* -------------------------------------------------------------------------- */
/* Badge                                                                      */
/* -------------------------------------------------------------------------- */

export type BadgeTone =
  | "neutral"
  | "accent"
  | "success"
  | "warning"
  | "danger";

const BADGE_TONES: Record<BadgeTone, string> = {
  neutral: "bg-surface-hover text-secondary border-line",
  accent: "bg-accent-surface text-accent-text border-accent-border",
  success: "bg-success-surface text-success-text border-success-border",
  warning: "bg-warning-surface text-warning-text border-warning-border",
  danger: "bg-danger-surface text-danger-text border-danger-border",
};

export interface BadgeProps extends React.HTMLAttributes<HTMLSpanElement> {
  tone?: BadgeTone;
  size?: "sm" | "md";
  icon?: React.ReactNode;
}

export function Badge({
  tone = "neutral",
  size = "md",
  icon,
  className,
  children,
  ...props
}: BadgeProps) {
  return (
    <span
      className={cn(
        "inline-flex shrink-0 items-center gap-1 rounded-xs border font-medium whitespace-nowrap",
        size === "sm" ? "h-4 px-1 text-2xs" : "h-5 px-1.5 text-xs",
        "[&_svg]:size-3",
        BADGE_TONES[tone],
        className,
      )}
      {...props}
    >
      {icon}
      {children}
    </span>
  );
}

/* -------------------------------------------------------------------------- */
/* Status                                                                     */
/* -------------------------------------------------------------------------- */

export type StatusKind =
  | "idle"
  | "queued"
  | "running"
  | "complete"
  | "failed"
  | "warning";

const STATUS: Record<
  StatusKind,
  { label: string; tone: BadgeTone; dot: string; icon: React.ReactNode | null }
> = {
  idle: {
    label: "Draft",
    tone: "neutral",
    // Hollow ring — nothing is happening, and the shape says so without hue.
    dot: "border-2 border-current bg-transparent",
    icon: null,
  },
  queued: {
    label: "Queued",
    tone: "neutral",
    dot: "border-2 border-dashed border-current bg-transparent",
    icon: null,
  },
  running: {
    label: "Running",
    tone: "accent",
    dot: "bg-current",
    icon: <SpinnerIcon className="animate-spin-slow" />,
  },
  complete: {
    label: "Ready",
    tone: "success",
    dot: "bg-current",
    icon: <CheckIcon />,
  },
  failed: {
    label: "Failed",
    tone: "danger",
    dot: "bg-current",
    icon: <CloseIcon />,
  },
  warning: {
    label: "Attention",
    tone: "warning",
    dot: "bg-current",
    icon: <WarningIcon />,
  },
};

/**
 * A status dot with its label.
 *
 * The label is not optional by accident — see the note at the top of the file.
 * `hideLabel` exists for the one case where the label is already adjacent in
 * the same cell, and it keeps the text for screen readers.
 */
export function StatusDot({
  status,
  label,
  hideLabel = false,
  pulse = false,
  className,
}: {
  status: StatusKind;
  label?: React.ReactNode;
  hideLabel?: boolean;
  /** Only for `running` — a resting state must not animate. */
  pulse?: boolean;
  className?: string;
}) {
  const meta = STATUS[status];
  const TONE_TEXT: Record<BadgeTone, string> = {
    neutral: "text-tertiary",
    accent: "text-accent-text",
    success: "text-success-text",
    warning: "text-warning-text",
    danger: "text-danger-text",
  };

  return (
    <span
      className={cn("inline-flex items-center gap-1.5", TONE_TEXT[meta.tone], className)}
    >
      <span
        aria-hidden
        className={cn(
          "size-2 shrink-0 rounded-full",
          meta.dot,
          pulse && status === "running" && "animate-pulse-soft",
        )}
      />
      <span className={cn("text-xs font-medium", hideLabel && "sr-only")}>
        {label ?? meta.label}
      </span>
    </span>
  );
}

/** The same state as a badge, for cards and table cells. */
export function StatusBadge({
  status,
  label,
  className,
}: {
  status: StatusKind;
  label?: React.ReactNode;
  className?: string;
}) {
  const meta = STATUS[status];
  return (
    <Badge tone={meta.tone} icon={meta.icon} className={className}>
      {label ?? meta.label}
    </Badge>
  );
}

/**
 * Map a backend stage or job status onto a display status.
 *
 * The API speaks in pipeline stages (`dxf_uploaded`, `EXTRACTING_DXF`); the UI
 * speaks in four states a person recognises. Keeping the translation in one
 * function means a new backend stage needs one edit, not one per surface that
 * displays it.
 */
export function statusFromStage(stage: string | undefined): StatusKind {
  switch (stage) {
    case "generated":
      return "complete";
    case "analysed":
    case "reviewed":
      return "warning"; // needs the user before it can proceed
    case "created":
    case "dxf_uploaded":
    case "images_uploaded":
      return "idle";
    default:
      return "idle";
  }
}

/* -------------------------------------------------------------------------- */
/* Keyboard hint                                                              */
/* -------------------------------------------------------------------------- */

/**
 * A keyboard key.
 *
 * Shortcuts are only useful if they are visible, and a tooltip nobody opens is
 * not visible. These appear inline in menus, in the command palette and in the
 * viewer's help overlay.
 */
export function Kbd({
  children,
  className,
}: {
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <kbd
      className={cn(
        "inline-flex h-5 min-w-5 items-center justify-center rounded-xs border border-line bg-sunken px-1",
        "font-mono text-2xs font-medium text-tertiary",
        className,
      )}
    >
      {children}
    </kbd>
  );
}

/** Several keys as one chord: ⌘ K, or Shift + Enter. */
export function KbdChord({
  keys,
  className,
}: {
  keys: readonly string[];
  className?: string;
}) {
  return (
    <span className={cn("inline-flex items-center gap-0.5", className)}>
      {keys.map((key) => (
        <Kbd key={key}>{key}</Kbd>
      ))}
    </span>
  );
}
