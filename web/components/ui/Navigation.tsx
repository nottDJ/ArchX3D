"use client";

/**
 * ArchX3D — Table, Tabs, Segmented, Breadcrumbs
 * =============================================
 * Structure and wayfinding.
 *
 * Tabs vs Segmented
 * -----------------
 * They look similar and mean different things, and mixing them is a common
 * source of confusion:
 *
 *   Tabs        switch between *panels of content*. Different things.
 *   Segmented   switch the *mode* of one thing. Same content, different view.
 *
 * So the viewer's Orbit/Walk is segmented — it is one model, two ways of
 * moving through it — while Dashboard/Projects would be tabs. Getting this
 * wrong teaches users the wrong mental model of what a control will do.
 *
 * Tables
 * ------
 * A real `<table>`, not a grid of divs. Screen readers announce row and column
 * position, "row 4 of 12", and let a user navigate by cell — none of which is
 * recoverable once the semantics are thrown away for layout convenience.
 */

import * as TabsPrimitive from "@radix-ui/react-tabs";
import Link from "next/link";
import { Fragment } from "react";

import { cn } from "./cn";
import { ChevronRightIcon } from "./icons";

/* -------------------------------------------------------------------------- */
/* Breadcrumbs                                                                */
/* -------------------------------------------------------------------------- */

export interface Crumb {
  label: string;
  href?: string;
}

export function Breadcrumbs({
  items,
  className,
}: {
  items: readonly Crumb[];
  className?: string;
}) {
  return (
    <nav aria-label="Breadcrumb" className={cn("min-w-0", className)}>
      <ol className="flex items-center gap-1 text-xs">
        {items.map((item, index) => {
          const isLast = index === items.length - 1;
          return (
            <Fragment key={`${item.label}-${index}`}>
              <li className="min-w-0">
                {item.href && !isLast ? (
                  <Link
                    href={item.href}
                    className="block truncate rounded-xs px-1 py-0.5 text-tertiary transition-colors hover:text-primary"
                  >
                    {item.label}
                  </Link>
                ) : (
                  <span
                    // The current page is the accessible landmark of the trail;
                    // without this a screen reader reads a list of links with no
                    // indication of where you are.
                    aria-current={isLast ? "page" : undefined}
                    className={cn(
                      "block truncate px-1 py-0.5",
                      isLast ? "font-medium text-primary" : "text-tertiary",
                    )}
                  >
                    {item.label}
                  </span>
                )}
              </li>
              {!isLast && (
                <li aria-hidden className="text-disabled">
                  <ChevronRightIcon className="size-3" />
                </li>
              )}
            </Fragment>
          );
        })}
      </ol>
    </nav>
  );
}

/* -------------------------------------------------------------------------- */
/* Segmented control                                                          */
/* -------------------------------------------------------------------------- */

export interface SegmentOption<T extends string> {
  value: T;
  label: React.ReactNode;
  icon?: React.ReactNode;
  disabled?: boolean;
  /** Announced and shown on hover. */
  hint?: string;
}

export function Segmented<T extends string>({
  value,
  onChange,
  options,
  size = "md",
  label,
  className,
}: {
  value: T;
  onChange: (value: T) => void;
  options: readonly SegmentOption<T>[];
  size?: "sm" | "md";
  /** Names the group for assistive tech, e.g. "Camera mode". */
  label: string;
  className?: string;
}) {
  return (
    <div
      role="radiogroup"
      aria-label={label}
      className={cn(
        "inline-flex items-center gap-0.5 rounded-md bg-sunken p-0.5",
        className,
      )}
    >
      {options.map((option) => {
        const active = option.value === value;
        return (
          <button
            key={option.value}
            type="button"
            role="radio"
            aria-checked={active}
            disabled={option.disabled}
            title={option.hint}
            onClick={() => onChange(option.value)}
            className={cn(
              "inline-flex items-center gap-1.5 rounded-sm font-medium whitespace-nowrap",
              "transition-[background-color,color,box-shadow] duration-[--duration-fast]",
              "[&_svg]:size-4 [&_svg]:shrink-0",
              size === "sm" ? "h-6 px-2 text-xs" : "h-7 px-2.5 text-sm",
              active
                ? "bg-surface text-primary shadow-xs"
                : "text-tertiary hover:text-primary",
              option.disabled && "cursor-not-allowed text-disabled hover:text-disabled",
            )}
          >
            {option.icon}
            {option.label}
          </button>
        );
      })}
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* Tabs                                                                       */
/* -------------------------------------------------------------------------- */

export function Tabs({
  value,
  onValueChange,
  children,
  className,
}: {
  value: string;
  onValueChange: (value: string) => void;
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <TabsPrimitive.Root
      value={value}
      onValueChange={onValueChange}
      className={cn("min-w-0", className)}
    >
      {children}
    </TabsPrimitive.Root>
  );
}

export function TabList({
  children,
  className,
}: {
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <TabsPrimitive.List
      className={cn(
        "flex items-center gap-1 border-b border-line-subtle",
        className,
      )}
    >
      {children}
    </TabsPrimitive.List>
  );
}

export function Tab({
  value,
  icon,
  count,
  children,
}: {
  value: string;
  icon?: React.ReactNode;
  count?: number;
  children: React.ReactNode;
}) {
  return (
    <TabsPrimitive.Trigger
      value={value}
      className={cn(
        "relative -mb-px inline-flex items-center gap-1.5 border-b-2 border-transparent px-3 py-2",
        "text-sm font-medium text-tertiary transition-colors",
        "hover:text-primary",
        "data-[state=active]:border-accent-solid data-[state=active]:text-primary",
        "[&_svg]:size-4",
      )}
    >
      {icon}
      {children}
      {count !== undefined && (
        <span className="rounded-xs bg-surface-hover px-1 font-mono text-2xs text-tertiary tabular-nums">
          {count}
        </span>
      )}
    </TabsPrimitive.Trigger>
  );
}

export function TabPanel({
  value,
  children,
  className,
}: {
  value: string;
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <TabsPrimitive.Content
      value={value}
      className={cn("focus-visible:outline-none", className)}
    >
      {children}
    </TabsPrimitive.Content>
  );
}

/* -------------------------------------------------------------------------- */
/* Table                                                                      */
/* -------------------------------------------------------------------------- */

export function Table({
  children,
  className,
  ...props
}: React.TableHTMLAttributes<HTMLTableElement>) {
  return (
    <div className="scroll-slim w-full overflow-x-auto rounded-lg border border-line">
      <table
        className={cn("w-full border-collapse text-sm", className)}
        {...props}
      >
        {children}
      </table>
    </div>
  );
}

export function THead({ children }: { children: React.ReactNode }) {
  return (
    <thead className="border-b border-line-subtle bg-sunken">{children}</thead>
  );
}

export function TBody({ children }: { children: React.ReactNode }) {
  return <tbody className="divide-y divide-line-subtle">{children}</tbody>;
}

export type SortDirection = "asc" | "desc" | null;

/**
 * A header cell, optionally sortable.
 *
 * `aria-sort` is what makes a sorted table comprehensible without sight — the
 * arrow glyph alone conveys nothing to a screen reader, and "sorted ascending"
 * is exactly the information a sighted user gets from the arrow.
 */
export function TH({
  children,
  sortable = false,
  sorted = null,
  onSort,
  align = "left",
  className,
  ...props
}: React.ThHTMLAttributes<HTMLTableCellElement> & {
  sortable?: boolean;
  sorted?: SortDirection;
  onSort?: () => void;
  align?: "left" | "right" | "center";
}) {
  return (
    <th
      scope="col"
      aria-sort={
        !sortable ? undefined : sorted === "asc" ? "ascending"
        : sorted === "desc" ? "descending" : "none"
      }
      className={cn(
        "px-3 py-2 text-2xs font-medium tracking-wider text-tertiary uppercase",
        align === "right" && "text-right",
        align === "center" && "text-center",
        align === "left" && "text-left",
        className,
      )}
      {...props}
    >
      {sortable ? (
        <button
          type="button"
          onClick={onSort}
          className={cn(
            "inline-flex items-center gap-1 rounded-xs transition-colors hover:text-primary",
            sorted && "text-primary",
          )}
        >
          {children}
          <span aria-hidden className="text-[9px] leading-none">
            {sorted === "asc" ? "▲" : sorted === "desc" ? "▼" : "⇅"}
          </span>
        </button>
      ) : (
        children
      )}
    </th>
  );
}

export function TR({
  children,
  interactive = false,
  className,
  ...props
}: React.HTMLAttributes<HTMLTableRowElement> & { interactive?: boolean }) {
  return (
    <tr
      className={cn(
        interactive &&
          "cursor-pointer transition-colors hover:bg-surface-hover focus-within:bg-surface-hover",
        className,
      )}
      {...props}
    >
      {children}
    </tr>
  );
}

export function TD({
  children,
  align = "left",
  className,
  ...props
}: React.TdHTMLAttributes<HTMLTableCellElement> & {
  align?: "left" | "right" | "center";
}) {
  return (
    <td
      className={cn(
        "px-3 py-2.5 text-secondary",
        align === "right" && "text-right",
        align === "center" && "text-center",
        className,
      )}
      {...props}
    >
      {children}
    </td>
  );
}
