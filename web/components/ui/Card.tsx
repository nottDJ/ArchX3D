/**
 * ArchX3D — Card, Panel and surfaces
 * ==================================
 * The containers everything else sits in.
 *
 * Elevation is a job, not a number
 * --------------------------------
 * Three levels, each tied to what the surface is *for*:
 *
 *   flat        content grouped on the canvas          border, no shadow
 *   raised      a discrete object you can act on       border + shadow-sm
 *   floating    temporarily above the page             border + shadow-lg
 *
 * Picking a shadow because it "looks nicer" is how an interface ends up with
 * five depths and no meaning. If a card needs to look more important than its
 * neighbour, that is a hierarchy problem, not a shadow problem.
 *
 * Dark mode
 * ---------
 * Shadows barely read on a dark canvas — there is nothing to cast onto — so
 * elevation is carried by surface lightness plus a 1px top highlight
 * (`edge-highlight`). The shadow stays because it still anchors the element
 * against the background; it is just no longer doing the work.
 */

import { cn } from "./cn";

export type CardElevation = "flat" | "raised" | "floating";

const ELEVATION: Record<CardElevation, string> = {
  flat: "bg-surface border border-line-subtle",
  raised: "bg-surface border border-line shadow-sm edge-highlight",
  floating: "bg-raised border border-line shadow-lg edge-highlight",
};

/**
 * `HTMLElement` rather than `HTMLDivElement`, because `as` makes this
 * polymorphic: a `<li>` and a `<div>` have incompatible event handler types,
 * and the narrower one would reject every `<li>` card in a project grid.
 */
export interface CardProps extends React.HTMLAttributes<HTMLElement> {
  elevation?: CardElevation;
  /** Adds hover feedback. Only for cards that are actually clickable. */
  interactive?: boolean;
  as?: "div" | "article" | "section" | "li";
}

export function Card({
  elevation = "raised",
  interactive = false,
  as: Component = "div",
  className,
  children,
  ...props
}: CardProps) {
  return (
    <Component
      className={cn(
        "rounded-lg",
        ELEVATION[elevation],
        interactive && [
          "transition-[border-color,box-shadow,transform]",
          "duration-[--duration-fast] ease-[--ease-standard]",
          "hover:border-line-strong hover:shadow-md",
          // 1px. Enough to register as liftable; more reads as a toy.
          "hover:-translate-y-px",
          "focus-within:border-accent-border",
        ],
        className,
      )}
      {...props}
    >
      {children}
    </Component>
  );
}

/**
 * Card header.
 *
 * `actions` sits opposite the title on the same baseline rather than below it,
 * so a row of cards keeps a consistent title line whatever each card's actions
 * are.
 */
export function CardHeader({
  title,
  description,
  actions,
  icon,
  className,
  ...props
}: {
  title: React.ReactNode;
  description?: React.ReactNode;
  actions?: React.ReactNode;
  icon?: React.ReactNode;
} & Omit<React.HTMLAttributes<HTMLElement>, "title">) {
  return (
    <div
      className={cn("flex items-start justify-between gap-4 p-4", className)}
      {...props}
    >
      <div className="flex min-w-0 items-start gap-3">
        {icon && (
          <span className="mt-0.5 flex size-8 shrink-0 items-center justify-center rounded-md border border-line-subtle bg-sunken text-accent-text [&_svg]:size-4">
            {icon}
          </span>
        )}
        <div className="min-w-0">
          <h3 className="truncate text-sm font-semibold text-primary">{title}</h3>
          {description && (
            <p className="mt-0.5 text-xs leading-relaxed text-tertiary">
              {description}
            </p>
          )}
        </div>
      </div>
      {actions && <div className="flex shrink-0 items-center gap-1.5">{actions}</div>}
    </div>
  );
}

export function CardBody({
  className,
  ...props
}: React.HTMLAttributes<HTMLDivElement>) {
  return <div className={cn("px-4 pb-4", className)} {...props} />;
}

/**
 * Card footer.
 *
 * Sunken rather than raised: a footer is a base the card rests on, and a
 * lighter strip at the bottom makes the card look like it is floating off the
 * page.
 */
export function CardFooter({
  className,
  ...props
}: React.HTMLAttributes<HTMLDivElement>) {
  return (
    <div
      className={cn(
        "flex items-center justify-end gap-2 rounded-b-lg border-t border-line-subtle bg-sunken px-4 py-3",
        className,
      )}
      {...props}
    />
  );
}

/**
 * A labelled region of a page — the level above a card.
 *
 * Used for the sections a page is divided into, where a full card would add a
 * border the content does not need.
 */
export function Section({
  title,
  description,
  actions,
  children,
  className,
  ...props
}: {
  title?: React.ReactNode;
  description?: React.ReactNode;
  actions?: React.ReactNode;
} & Omit<React.HTMLAttributes<HTMLElement>, "title">) {
  return (
    <section className={cn("min-w-0", className)} {...props}>
      {(title || actions) && (
        <div className="mb-3 flex items-end justify-between gap-4">
          <div className="min-w-0">
            {title && (
              <h2 className="text-sm font-semibold tracking-tight text-primary">
                {title}
              </h2>
            )}
            {description && (
              <p className="mt-0.5 text-xs text-tertiary">{description}</p>
            )}
          </div>
          {actions && (
            <div className="flex shrink-0 items-center gap-1.5">{actions}</div>
          )}
        </div>
      )}
      {children}
    </section>
  );
}

/**
 * A single statistic.
 *
 * Deliberately not a chart. Most "dashboard" numbers are one value and a
 * label; wrapping that in a sparkline adds ink without adding information,
 * and invites the reader to interpret noise as trend.
 */
export function Stat({
  label,
  value,
  hint,
  icon,
  tone = "default",
  className,
}: {
  label: React.ReactNode;
  value: React.ReactNode;
  hint?: React.ReactNode;
  icon?: React.ReactNode;
  tone?: "default" | "accent" | "success" | "warning" | "danger";
  className?: string;
}) {
  const TONES = {
    default: "text-primary",
    accent: "text-accent-text",
    success: "text-success-text",
    warning: "text-warning-text",
    danger: "text-danger-text",
  } as const;

  return (
    <div
      className={cn(
        "rounded-lg border border-line-subtle bg-surface p-4",
        className,
      )}
    >
      <div className="flex items-center gap-2">
        {icon && <span className="text-tertiary [&_svg]:size-3.5">{icon}</span>}
        <p className="text-2xs font-medium tracking-wider text-tertiary uppercase">
          {label}
        </p>
      </div>
      <p
        className={cn(
          "mt-2 text-xl font-semibold tracking-tight tabular-nums",
          TONES[tone],
        )}
      >
        {value}
      </p>
      {hint && <p className="mt-1 text-xs text-tertiary">{hint}</p>}
    </div>
  );
}

/**
 * A horizontal rule with optional label.
 *
 * `role="presentation"` when unlabelled — an undecorated divider is visual
 * grouping, and announcing "separator" for each one makes a screen reader
 * walk-through of a dense page unbearable.
 */
export function Divider({
  label,
  className,
}: {
  label?: React.ReactNode;
  className?: string;
}) {
  if (!label) {
    return (
      <hr role="presentation" className={cn("border-t border-line-subtle", className)} />
    );
  }

  return (
    <div className={cn("flex items-center gap-3", className)}>
      <hr role="presentation" className="flex-1 border-t border-line-subtle" />
      <span className="text-2xs font-medium tracking-wider text-tertiary uppercase">
        {label}
      </span>
      <hr role="presentation" className="flex-1 border-t border-line-subtle" />
    </div>
  );
}
