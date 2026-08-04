"use client";

/**
 * ArchX3D — Button
 * ================
 * The most-used control in the product, and the one whose hierarchy was most
 * broken before: the wizard had white-on-dark, emerald, and bordered buttons
 * competing on the same screen, so "the important one" was whichever was
 * brightest rather than whichever moved the user forward.
 *
 * The hierarchy, and the rule that keeps it
 * -----------------------------------------
 *
 *   primary     the one thing this screen is for      at most one, ever
 *   secondary   a real alternative                    zero or more
 *   ghost       tertiary, toolbars, dense rows        zero or more
 *   danger      destructive, and irreversible         at most one
 *   link        inline, sits inside a sentence        any
 *
 * **At most one primary per view.** Two primaries is not emphasis, it is the
 * absence of it — the user has to read both to find out which is the way
 * forward, which is exactly the work the hierarchy exists to save them.
 *
 * Sizing
 * ------
 * Three heights: 28, 32 and 40px. The 40px size exists because WCAG 2.5.8
 * asks for a 24px minimum target and iOS convention is 44px; 40 with 4px of
 * surrounding space clears both. `sm` is for toolbars and table rows where
 * density is the point and the target is still ≥ 28px with padding.
 *
 * Loading
 * -------
 * A loading button keeps its label and its width. Swapping the label for a
 * spinner makes the row reflow and, worse, hides which action is in progress
 * — which matters most precisely when something is slow.
 */

import { Slot, Slottable } from "@radix-ui/react-slot";
import { forwardRef } from "react";

import { cn } from "./cn";
import { SpinnerIcon } from "./icons";

export type ButtonVariant =
  | "primary"
  | "secondary"
  | "ghost"
  | "danger"
  | "link";

export type ButtonSize = "sm" | "md" | "lg";

export interface ButtonProps
  extends React.ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant;
  size?: ButtonSize;
  /** Shows a spinner and blocks interaction. The label stays put. */
  loading?: boolean;
  /** Leading icon. Sized automatically. */
  icon?: React.ReactNode;
  /** Trailing icon — chevrons, external-link marks. */
  iconTrailing?: React.ReactNode;
  /**
   * Square, no horizontal padding — for a button whose content is one icon.
   * The icon is passed as `children`, and `aria-label` is then **required**:
   * an icon has no accessible name of its own.
   */
  iconOnly?: boolean;
  /** Render as the child element — for `<Link>` that looks like a button. */
  asChild?: boolean;
}

const VARIANTS: Record<ButtonVariant, string> = {
  // Solid accent. Reserved for the forward action.
  primary: cn(
    "bg-accent-solid text-on-solid shadow-xs",
    "hover:bg-accent-solid-hover",
    "active:brightness-95",
    "disabled:bg-surface-active disabled:text-disabled disabled:shadow-none",
  ),
  // Bordered. Reads as available without competing.
  secondary: cn(
    "bg-surface text-primary border border-line shadow-xs edge-highlight",
    "hover:bg-surface-hover hover:border-line-strong",
    "active:bg-surface-active",
    "disabled:bg-transparent disabled:text-disabled disabled:border-line-subtle disabled:shadow-none",
  ),
  // No chrome at rest. For toolbars, where borders would produce a grid.
  ghost: cn(
    "text-secondary",
    "hover:bg-surface-hover hover:text-primary",
    "active:bg-surface-active",
    "disabled:text-disabled disabled:bg-transparent",
  ),
  // Solid red, never bordered: a destructive action should not be one visual
  // step away from `secondary`, because that is how people delete things.
  danger: cn(
    "bg-danger-solid text-on-solid shadow-xs",
    "hover:brightness-110",
    "active:brightness-95",
    "disabled:bg-surface-active disabled:text-disabled disabled:shadow-none",
  ),
  link: cn(
    "text-accent-text underline decoration-transparent underline-offset-[3px]",
    "hover:decoration-current",
    "disabled:text-disabled disabled:no-underline",
  ),
};

const SIZES: Record<ButtonSize, string> = {
  sm: "h-7 px-2.5 text-xs gap-1.5 rounded-sm",
  md: "h-8 px-3 text-sm gap-1.5 rounded-md",
  lg: "h-10 px-4 text-base gap-2 rounded-md",
};

const ICON_ONLY_SIZES: Record<ButtonSize, string> = {
  sm: "h-7 w-7 p-0 rounded-sm",
  md: "h-8 w-8 p-0 rounded-md",
  lg: "h-10 w-10 p-0 rounded-md",
};

export const Button = forwardRef<HTMLButtonElement, ButtonProps>(
  function Button(
    {
      variant = "secondary",
      size = "md",
      loading = false,
      icon,
      iconTrailing,
      iconOnly = false,
      asChild = false,
      className,
      children,
      disabled,
      type,
      ...props
    },
    ref,
  ) {
    const Component = asChild ? Slot : "button";
    const isDisabled = disabled || loading;

    return (
      <Component
        ref={ref}
        // Buttons inside a form default to `submit`, which silently submits
        // when someone presses Enter in an adjacent field. Explicit by default.
        type={asChild ? undefined : (type ?? "button")}
        disabled={asChild ? undefined : isDisabled}
        aria-busy={loading || undefined}
        // Radix's Slot forwards to a link, which cannot be `disabled`; mark it
        // so assistive tech and pointer events agree with the visuals.
        aria-disabled={asChild && isDisabled ? true : undefined}
        data-loading={loading || undefined}
        className={cn(
          "relative inline-flex shrink-0 select-none items-center justify-center",
          "font-medium whitespace-nowrap",
          size === "lg" ? "gap-2" : "gap-1.5",
          "transition-[background-color,border-color,color,box-shadow,transform]",
          "duration-[--duration-fast] ease-[--ease-standard]",
          // A 1px lift registers as a press without the row moving.
          "active:translate-y-px",
          "disabled:pointer-events-none disabled:active:translate-y-0",
          "[&_svg]:pointer-events-none [&_svg]:shrink-0",
          size === "lg" ? "[&_svg]:size-[18px]" : "[&_svg]:size-4",
          // While loading, everything except the spinner fades. Done in CSS on
          // the parent rather than by wrapping the label in a span, because a
          // wrapper breaks `asChild` — Slot needs to merge onto the child
          // element itself, not onto a div we introduced.
          "data-loading:[&>*:not([data-spinner])]:opacity-0",
          iconOnly ? ICON_ONLY_SIZES[size] : SIZES[size],
          VARIANTS[variant],
          className,
        )}
        {...props}
      >
        {/*
          Absolutely positioned over the label, which fades rather than
          unmounting, so the button keeps its exact width. A button that
          resizes when clicked shifts every control beside it.
        */}
        {loading && (
          <SpinnerIcon data-spinner className="absolute animate-spin-slow" aria-hidden />
        )}
        {icon}
        {/*
          `Slottable` tells Radix which child to merge the rendered element
          onto, so `asChild` composes with leading and trailing icons. Without
          it, Slot sees three children and throws.
        */}
        <Slottable>{children}</Slottable>
        {iconTrailing}
      </Component>
    );
  },
);

/**
 * A group of related buttons, joined into one control.
 *
 * Used where several actions share a subject — a split action, a set of
 * exclusive tools. Corners and borders are collapsed so the group reads as
 * one object rather than three adjacent ones.
 */
export function ButtonGroup({
  className,
  children,
  ...props
}: React.HTMLAttributes<HTMLDivElement>) {
  return (
    <div
      role="group"
      className={cn(
        "inline-flex items-center",
        "[&>*:not(:first-child)]:rounded-l-none [&>*:not(:last-child)]:rounded-r-none",
        "[&>*:not(:first-child)]:-ml-px",
        // The hovered/focused member must paint over its neighbour's border.
        "[&>*]:relative [&>*:hover]:z-10 [&>*:focus-visible]:z-10",
        className,
      )}
      {...props}
    >
      {children}
    </div>
  );
}
