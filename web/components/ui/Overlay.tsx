"use client";

/**
 * ArchX3D — Dialog, Tooltip, Menu, Popover
 * ========================================
 * Everything that appears above the page.
 *
 * Why these are Radix and not hand-written
 * ----------------------------------------
 * A correct dialog needs: a focus trap that survives DOM changes, focus
 * restored to the trigger on close, `aria-modal` with the rest of the page
 * inert, Escape and outside-click dismissal, scroll locking that does not
 * shift the layout, and portalling that does not break z-index or event
 * bubbling. Each is a few lines; together they are a library, and every one
 * of them is a WCAG failure when it is subtly wrong.
 *
 * Radix does all of it and is the primitive layer most products in this
 * category use. What is written here is the *appearance* — which is the part
 * that should be ours — and none of the behaviour.
 *
 * Tooltips are hints, never the only source
 * -----------------------------------------
 * A tooltip does not appear on touch and is not reliably reachable by
 * keyboard-only users on every platform. So nothing here may put essential
 * information in one: it holds the shortcut and the elaboration, while the
 * label lives in `aria-label`. If a control cannot be understood without its
 * tooltip, the control needs a visible label.
 */

import * as DialogPrimitive from "@radix-ui/react-dialog";
import * as DropdownPrimitive from "@radix-ui/react-dropdown-menu";
import * as PopoverPrimitive from "@radix-ui/react-popover";
import * as TooltipPrimitive from "@radix-ui/react-tooltip";

import { Button } from "./Button";
import { cn } from "./cn";
import { CloseIcon } from "./icons";

/* -------------------------------------------------------------------------- */
/* Tooltip                                                                    */
/* -------------------------------------------------------------------------- */

/**
 * Wrap the app once.
 *
 * `delayDuration` of 400ms: long enough that moving the cursor across a
 * toolbar does not fire six tooltips, short enough that a deliberate hover
 * feels answered. `skipDelayDuration` then makes neighbouring tooltips
 * instant, so exploring a toolbar is fast once you have started.
 */
export function TooltipProvider({ children }: { children: React.ReactNode }) {
  return (
    <TooltipPrimitive.Provider delayDuration={400} skipDelayDuration={200}>
      {children}
    </TooltipPrimitive.Provider>
  );
}

export function Tooltip({
  content,
  shortcut,
  side = "top",
  align = "center",
  children,
}: {
  content: React.ReactNode;
  /** Rendered as keys on the right. */
  shortcut?: readonly string[];
  side?: "top" | "right" | "bottom" | "left";
  align?: "start" | "center" | "end";
  children: React.ReactNode;
}) {
  if (!content) return <>{children}</>;

  return (
    <TooltipPrimitive.Root>
      <TooltipPrimitive.Trigger asChild>{children}</TooltipPrimitive.Trigger>
      <TooltipPrimitive.Portal>
        <TooltipPrimitive.Content
          side={side}
          align={align}
          sideOffset={6}
          collisionPadding={8}
          className={cn(
            "z-50 flex items-center gap-2 rounded-md border border-line bg-raised px-2 py-1.5 shadow-lg",
            "text-xs text-primary",
            "origin-[--radix-tooltip-content-transform-origin]",
            "data-[state=delayed-open]:animate-scale-in",
          )}
        >
          <span>{content}</span>
          {shortcut && (
            <span className="flex items-center gap-0.5">
              {shortcut.map((key) => (
                <kbd
                  key={key}
                  className="inline-flex h-4 min-w-4 items-center justify-center rounded-xs bg-surface-active px-1 font-mono text-2xs text-tertiary"
                >
                  {key}
                </kbd>
              ))}
            </span>
          )}
        </TooltipPrimitive.Content>
      </TooltipPrimitive.Portal>
    </TooltipPrimitive.Root>
  );
}

/* -------------------------------------------------------------------------- */
/* Dialog                                                                     */
/* -------------------------------------------------------------------------- */

export interface DialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: React.ReactNode;
  description?: React.ReactNode;
  children?: React.ReactNode;
  footer?: React.ReactNode;
  size?: "sm" | "md" | "lg";
}

const DIALOG_SIZES = {
  sm: "max-w-sm",
  md: "max-w-lg",
  lg: "max-w-2xl",
} as const;

export function Dialog({
  open,
  onOpenChange,
  title,
  description,
  children,
  footer,
  size = "md",
}: DialogProps) {
  return (
    <DialogPrimitive.Root open={open} onOpenChange={onOpenChange}>
      <DialogPrimitive.Portal>
        <DialogPrimitive.Overlay
          className={cn(
            "fixed inset-0 z-50 bg-overlay backdrop-blur-[2px]",
            "data-[state=open]:animate-fade-in",
          )}
        />
        <DialogPrimitive.Content
          className={cn(
            "fixed top-1/2 left-1/2 z-50 w-[calc(100vw-2rem)] -translate-x-1/2 -translate-y-1/2",
            DIALOG_SIZES[size],
            "rounded-xl border border-line bg-raised shadow-xl edge-highlight",
            "data-[state=open]:animate-scale-in",
            "focus:outline-none",
          )}
        >
          <div className="flex items-start justify-between gap-4 p-5 pb-0">
            <div className="min-w-0">
              <DialogPrimitive.Title className="text-base font-semibold text-primary">
                {title}
              </DialogPrimitive.Title>
              {description && (
                <DialogPrimitive.Description className="mt-1 text-sm leading-relaxed text-secondary">
                  {description}
                </DialogPrimitive.Description>
              )}
            </div>
            <DialogPrimitive.Close asChild>
              <Button variant="ghost" size="sm" iconOnly aria-label="Close">
                <CloseIcon />
              </Button>
            </DialogPrimitive.Close>
          </div>

          {children && <div className="p-5">{children}</div>}

          {footer && (
            <div className="flex items-center justify-end gap-2 rounded-b-xl border-t border-line-subtle bg-sunken px-5 py-3.5">
              {footer}
            </div>
          )}
        </DialogPrimitive.Content>
      </DialogPrimitive.Portal>
    </DialogPrimitive.Root>
  );
}

/**
 * A destructive confirmation.
 *
 * Separate from `Dialog` so the pattern is enforced rather than remembered:
 * the destructive verb is on the button ("Delete project", not "OK"), the
 * cancel is the safe default, and the consequence is spelled out. A dialog
 * whose buttons say OK and Cancel makes the user reconstruct which one is
 * which from the title.
 */
export function ConfirmDialog({
  open,
  onOpenChange,
  title,
  description,
  confirmLabel,
  cancelLabel = "Cancel",
  onConfirm,
  destructive = true,
  loading = false,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: React.ReactNode;
  description: React.ReactNode;
  confirmLabel: string;
  cancelLabel?: string;
  onConfirm: () => void;
  destructive?: boolean;
  loading?: boolean;
}) {
  return (
    <Dialog
      open={open}
      onOpenChange={onOpenChange}
      title={title}
      description={description}
      size="sm"
      footer={
        <>
          <Button variant="ghost" onClick={() => onOpenChange(false)}>
            {cancelLabel}
          </Button>
          <Button
            variant={destructive ? "danger" : "primary"}
            onClick={onConfirm}
            loading={loading}
          >
            {confirmLabel}
          </Button>
        </>
      }
    />
  );
}

/* -------------------------------------------------------------------------- */
/* Menu                                                                       */
/* -------------------------------------------------------------------------- */

export function Menu({
  trigger,
  children,
  align = "end",
  side = "bottom",
}: {
  trigger: React.ReactNode;
  children: React.ReactNode;
  align?: "start" | "center" | "end";
  side?: "top" | "right" | "bottom" | "left";
}) {
  return (
    <DropdownPrimitive.Root>
      <DropdownPrimitive.Trigger asChild>{trigger}</DropdownPrimitive.Trigger>
      <DropdownPrimitive.Portal>
        <DropdownPrimitive.Content
          align={align}
          side={side}
          sideOffset={6}
          collisionPadding={8}
          className={cn(
            "z-50 min-w-[11rem] rounded-lg border border-line bg-raised p-1 shadow-lg edge-highlight",
            "origin-[--radix-dropdown-menu-content-transform-origin]",
            "data-[state=open]:animate-scale-in",
          )}
        >
          {children}
        </DropdownPrimitive.Content>
      </DropdownPrimitive.Portal>
    </DropdownPrimitive.Root>
  );
}

export function MenuItem({
  icon,
  shortcut,
  destructive = false,
  children,
  ...props
}: React.ComponentPropsWithoutRef<typeof DropdownPrimitive.Item> & {
  icon?: React.ReactNode;
  shortcut?: string;
  destructive?: boolean;
}) {
  return (
    <DropdownPrimitive.Item
      className={cn(
        "flex cursor-pointer items-center gap-2 rounded-sm px-2 py-1.5 text-sm outline-none select-none",
        "[&_svg]:size-4 [&_svg]:shrink-0",
        destructive
          ? "text-danger-text data-[highlighted]:bg-danger-surface"
          : "text-secondary data-[highlighted]:bg-surface-hover data-[highlighted]:text-primary",
        "data-[disabled]:pointer-events-none data-[disabled]:text-disabled",
      )}
      {...props}
    >
      {icon}
      <span className="flex-1 truncate">{children}</span>
      {shortcut && (
        <span className="font-mono text-2xs text-tertiary">{shortcut}</span>
      )}
    </DropdownPrimitive.Item>
  );
}

export function MenuSeparator() {
  return <DropdownPrimitive.Separator className="my-1 h-px bg-line-subtle" />;
}

export function MenuLabel({ children }: { children: React.ReactNode }) {
  return (
    <DropdownPrimitive.Label className="px-2 py-1.5 text-2xs font-medium tracking-wider text-tertiary uppercase">
      {children}
    </DropdownPrimitive.Label>
  );
}

/* -------------------------------------------------------------------------- */
/* Popover                                                                    */
/* -------------------------------------------------------------------------- */

/** For rich transient content — a filter form, a colour picker, a help card. */
export function Popover({
  trigger,
  children,
  align = "end",
  side = "bottom",
  className,
}: {
  trigger: React.ReactNode;
  children: React.ReactNode;
  align?: "start" | "center" | "end";
  side?: "top" | "right" | "bottom" | "left";
  className?: string;
}) {
  return (
    <PopoverPrimitive.Root>
      <PopoverPrimitive.Trigger asChild>{trigger}</PopoverPrimitive.Trigger>
      <PopoverPrimitive.Portal>
        <PopoverPrimitive.Content
          align={align}
          side={side}
          sideOffset={6}
          collisionPadding={8}
          className={cn(
            "z-50 rounded-lg border border-line bg-raised p-3 shadow-lg edge-highlight",
            "origin-[--radix-popover-content-transform-origin]",
            "data-[state=open]:animate-scale-in",
            "focus:outline-none",
            className,
          )}
        >
          {children}
        </PopoverPrimitive.Content>
      </PopoverPrimitive.Portal>
    </PopoverPrimitive.Root>
  );
}
