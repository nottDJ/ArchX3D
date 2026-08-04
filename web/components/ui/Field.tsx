"use client";

/**
 * ArchX3D — Form controls
 * =======================
 * Field, Input, Textarea, Select, Switch, Slider.
 *
 * Every control is labelled, and the label is wired up
 * ---------------------------------------------------
 * `Field` generates an id, points the `<label>` at it, and links help text and
 * errors through `aria-describedby`. That is the whole reason it exists: a
 * placeholder is not a label (it disappears on focus, exactly when a user who
 * paused to think needs it), and a `<div>` above an input is not a label to
 * anything that cannot see.
 *
 * Errors are announced, not just coloured
 * ---------------------------------------
 * An invalid field sets `aria-invalid`, links its message, and the message
 * region is a live region — so a screen-reader user hears the problem when it
 * appears rather than discovering it by walking back through the form.
 *
 * Sizes
 * -----
 * 32px (`md`) matches `Button` md, so a field and its button sit on one line
 * without a shim. 40px (`lg`) is for a form that is the whole page.
 */

import * as SliderPrimitive from "@radix-ui/react-slider";
import * as SwitchPrimitive from "@radix-ui/react-switch";
import { createContext, forwardRef, useContext, useId } from "react";

import { cn } from "./cn";
import { ChevronDownIcon } from "./icons";

/* -------------------------------------------------------------------------- */
/* Field                                                                      */
/* -------------------------------------------------------------------------- */

interface FieldContextValue {
  id: string;
  describedBy: string | undefined;
  invalid: boolean;
}

const FieldContext = createContext<FieldContextValue | null>(null);

function useField() {
  return useContext(FieldContext);
}

export interface FieldProps {
  label?: React.ReactNode;
  /** Guidance shown under the control. Replaced by `error` when invalid. */
  hint?: React.ReactNode;
  error?: React.ReactNode;
  /** Renders "Optional" beside the label. Marking the *few* optional fields
   *  is less visual noise than marking the many required ones. */
  optional?: boolean;
  children: React.ReactNode;
  className?: string;
}

export function Field({
  label,
  hint,
  error,
  optional,
  children,
  className,
}: FieldProps) {
  const id = useId();
  const hintId = `${id}-hint`;
  const errorId = `${id}-error`;
  const describedBy = error ? errorId : hint ? hintId : undefined;

  return (
    <FieldContext.Provider value={{ id, describedBy, invalid: Boolean(error) }}>
      <div className={cn("min-w-0", className)}>
        {label && (
          <label
            htmlFor={id}
            className="mb-1.5 flex items-center gap-1.5 text-xs font-medium text-secondary"
          >
            {label}
            {optional && (
              <span className="font-normal text-tertiary">Optional</span>
            )}
          </label>
        )}

        {children}

        {/*
          Always rendered, even when empty. A region that appears and
          disappears is not reliably announced by every screen reader; one that
          exists and gains content is.
        */}
        <div aria-live="polite" className="min-h-0">
          {error ? (
            <p id={errorId} className="mt-1.5 text-xs text-danger-text">
              {error}
            </p>
          ) : hint ? (
            <p id={hintId} className="mt-1.5 text-xs text-tertiary">
              {hint}
            </p>
          ) : null}
        </div>
      </div>
    </FieldContext.Provider>
  );
}

/* -------------------------------------------------------------------------- */
/* Shared control styling                                                     */
/* -------------------------------------------------------------------------- */

const CONTROL_BASE = cn(
  "w-full min-w-0 rounded-md border bg-sunken text-primary",
  "border-line placeholder:text-disabled",
  "transition-[border-color,box-shadow] duration-[--duration-fast] ease-[--ease-standard]",
  "hover:border-line-strong",
  "focus-field",
  "disabled:cursor-not-allowed disabled:bg-surface disabled:text-disabled",
  "aria-[invalid=true]:border-danger-border",
);

const CONTROL_SIZES = {
  sm: "h-7 px-2 text-xs",
  md: "h-8 px-2.5 text-sm",
  lg: "h-10 px-3 text-base",
} as const;

export type ControlSize = keyof typeof CONTROL_SIZES;

/* -------------------------------------------------------------------------- */
/* Input                                                                      */
/* -------------------------------------------------------------------------- */

export interface InputProps
  extends Omit<React.InputHTMLAttributes<HTMLInputElement>, "size"> {
  size?: ControlSize;
  /** Icon inside the field, leading. Decorative only. */
  icon?: React.ReactNode;
  /** Content at the trailing edge — a clear button, a unit, a shortcut hint. */
  trailing?: React.ReactNode;
}

export const Input = forwardRef<HTMLInputElement, InputProps>(function Input(
  { size = "md", icon, trailing, className, ...props },
  ref,
) {
  const field = useField();

  const input = (
    <input
      ref={ref}
      id={props.id ?? field?.id}
      aria-describedby={props["aria-describedby"] ?? field?.describedBy}
      aria-invalid={props["aria-invalid"] ?? (field?.invalid || undefined)}
      className={cn(
        CONTROL_BASE,
        CONTROL_SIZES[size],
        icon && (size === "lg" ? "pl-10" : "pl-8"),
        trailing && "pr-10",
        className,
      )}
      {...props}
    />
  );

  if (!icon && !trailing) return input;

  return (
    <div className="relative flex items-center">
      {icon && (
        <span
          aria-hidden
          className={cn(
            "pointer-events-none absolute text-tertiary [&_svg]:size-4",
            size === "lg" ? "left-3" : "left-2.5",
          )}
        >
          {icon}
        </span>
      )}
      {input}
      {trailing && (
        <span className="absolute right-2 flex items-center gap-1">{trailing}</span>
      )}
    </div>
  );
});

/* -------------------------------------------------------------------------- */
/* Textarea                                                                   */
/* -------------------------------------------------------------------------- */

export const Textarea = forwardRef<
  HTMLTextAreaElement,
  React.TextareaHTMLAttributes<HTMLTextAreaElement>
>(function Textarea({ className, rows = 3, ...props }, ref) {
  const field = useField();
  return (
    <textarea
      ref={ref}
      rows={rows}
      id={props.id ?? field?.id}
      aria-describedby={props["aria-describedby"] ?? field?.describedBy}
      aria-invalid={props["aria-invalid"] ?? (field?.invalid || undefined)}
      className={cn(CONTROL_BASE, "resize-y px-2.5 py-2 text-sm", className)}
      {...props}
    />
  );
});

/* -------------------------------------------------------------------------- */
/* Select                                                                     */
/* -------------------------------------------------------------------------- */

/**
 * A native `<select>`, styled.
 *
 * Deliberately native rather than a custom listbox. The native control gets
 * platform keyboard behaviour, type-ahead, and — the decisive one — the
 * platform's own picker on mobile, which is far better than anything a
 * `div`-based menu manages on a touch screen. Where a menu needs icons,
 * descriptions or grouping beyond `<optgroup>`, use `Menu` instead.
 */
export const Select = forwardRef<
  HTMLSelectElement,
  // `size` is omitted from the native attributes and redefined: on a `<select>`
  // it means "rows visible when open" and is a number, which would collide
  // with the design system's t-shirt sizing.
  Omit<React.SelectHTMLAttributes<HTMLSelectElement>, "size"> & {
    size?: ControlSize;
  }
>(function Select({ size = "md", className, children, ...props }, ref) {
  const field = useField();
  return (
    <div className="relative flex items-center">
      <select
        ref={ref}
        id={props.id ?? field?.id}
        aria-describedby={props["aria-describedby"] ?? field?.describedBy}
        aria-invalid={props["aria-invalid"] ?? (field?.invalid || undefined)}
        className={cn(
          CONTROL_BASE,
          CONTROL_SIZES[size],
          "cursor-pointer appearance-none pr-8",
          className,
        )}
        {...props}
      >
        {children}
      </select>
      <ChevronDownIcon
        aria-hidden
        className="pointer-events-none absolute right-2.5 size-3.5 text-tertiary"
      />
    </div>
  );
});

/* -------------------------------------------------------------------------- */
/* Switch                                                                     */
/* -------------------------------------------------------------------------- */

/**
 * A toggle for something that takes effect immediately.
 *
 * A switch means "on now". A checkbox means "will be included when you
 * submit". Using a switch inside a form with a Save button is a small lie, and
 * users respond to it by clicking Save and then checking whether it worked.
 */
export function Switch({
  checked,
  onCheckedChange,
  disabled,
  label,
  hint,
  id,
  className,
}: {
  checked: boolean;
  onCheckedChange: (checked: boolean) => void;
  disabled?: boolean;
  label: React.ReactNode;
  hint?: React.ReactNode;
  id?: string;
  className?: string;
}) {
  const generated = useId();
  const controlId = id ?? generated;
  const hintId = `${controlId}-hint`;

  return (
    <div className={cn("flex items-start justify-between gap-4", className)}>
      <div className="min-w-0">
        <label
          htmlFor={controlId}
          className={cn(
            "block text-sm text-primary",
            disabled ? "cursor-not-allowed text-disabled" : "cursor-pointer",
          )}
        >
          {label}
        </label>
        {hint && (
          <p id={hintId} className="mt-0.5 text-xs leading-relaxed text-tertiary">
            {hint}
          </p>
        )}
      </div>

      <SwitchPrimitive.Root
        id={controlId}
        checked={checked}
        onCheckedChange={onCheckedChange}
        disabled={disabled}
        aria-describedby={hint ? hintId : undefined}
        className={cn(
          "relative inline-flex h-5 w-9 shrink-0 cursor-pointer items-center rounded-full",
          "border border-transparent transition-colors duration-[--duration-fast]",
          "data-[state=unchecked]:bg-surface-active data-[state=checked]:bg-accent-solid",
          "disabled:cursor-not-allowed disabled:opacity-45",
        )}
      >
        <SwitchPrimitive.Thumb
          className={cn(
            "block size-4 rounded-full bg-white shadow-sm",
            "transition-transform duration-[--duration-fast] ease-[--ease-standard]",
            "translate-x-0.5 data-[state=checked]:translate-x-[18px]",
          )}
        />
      </SwitchPrimitive.Root>
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* Slider                                                                     */
/* -------------------------------------------------------------------------- */

/**
 * A slider with its value always visible.
 *
 * A slider whose value only appears on drag is unreadable at rest — the user
 * has to grab it to find out what it says, which for a settings panel is
 * exactly backwards.
 */
export function Slider({
  label,
  value,
  onValueChange,
  min,
  max,
  step,
  format,
  unit,
  hint,
  disabled,
  className,
}: {
  label: React.ReactNode;
  value: number;
  onValueChange: (value: number) => void;
  min: number;
  max: number;
  step: number;
  format?: (value: number) => string;
  unit?: string;
  hint?: React.ReactNode;
  disabled?: boolean;
  className?: string;
}) {
  const id = useId();
  const display = format ? format(value) : value.toFixed(2);

  return (
    <div className={cn("min-w-0", className)}>
      <div className="mb-2 flex items-baseline justify-between gap-3">
        <label htmlFor={id} className="text-sm text-primary">
          {label}
        </label>
        <span className="font-mono text-xs text-tertiary tabular-nums">
          {display}
          {unit && ` ${unit}`}
        </span>
      </div>

      <SliderPrimitive.Root
        id={id}
        value={[value]}
        onValueChange={([next]) => onValueChange(next)}
        min={min}
        max={max}
        step={step}
        disabled={disabled}
        aria-label={typeof label === "string" ? label : undefined}
        className={cn(
          "relative flex h-4 w-full touch-none items-center select-none",
          disabled && "opacity-45",
        )}
      >
        <SliderPrimitive.Track className="relative h-1 w-full grow rounded-full bg-surface-active">
          <SliderPrimitive.Range className="absolute h-full rounded-full bg-accent-solid" />
        </SliderPrimitive.Track>
        <SliderPrimitive.Thumb
          className={cn(
            "block size-3.5 rounded-full border-2 border-accent-solid bg-surface shadow-sm",
            "transition-transform duration-[--duration-instant]",
            "hover:scale-110 focus-visible:scale-110",
            "disabled:pointer-events-none",
          )}
        />
      </SliderPrimitive.Root>

      {hint && <p className="mt-1.5 text-xs leading-relaxed text-tertiary">{hint}</p>}
    </div>
  );
}
