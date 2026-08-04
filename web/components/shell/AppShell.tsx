"use client";

/**
 * ArchX3D — application shell
 * ===========================
 * Persistent navigation, a page header, and the content region.
 *
 * The gap this fills
 * ------------------
 * There was no navigation. Every page was a standalone screen with a logo in
 * the corner and, at best, one link back. A user in the wizard could not reach
 * the viewer; a user in the viewer could not reach their projects; nobody
 * could see what else existed. The product was a set of pages that happened to
 * share a domain.
 *
 * A persistent sidebar fixes discoverability at the cost of ~15rem of width.
 * That trade is right here because the destinations are few and stable — five
 * items that never change — which is exactly the case a sidebar suits and a
 * top-nav-with-dropdowns does not.
 *
 * Where the shell is *not* used
 * -----------------------------
 * The viewer and the landing page render full-bleed without it. The viewer
 * because a 3D canvas wants every pixel and has its own floating chrome; the
 * landing page because it is marketing, not application. Both link back into
 * the shell explicitly.
 */

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useState } from "react";

import { CommandMenu } from "./CommandMenu";
import { ThemeToggle } from "./ThemeToggle";
import { Breadcrumbs, Button, Kbd, Tooltip, cn, type Crumb } from "@/components/ui";
import {
  CubeIcon,
  GridIcon,
  MenuIcon,
  PlanIcon,
  SearchIcon,
  SettingsIcon,
  SparkIcon,
  BookIcon,
  LayersIcon,
  CloseIcon,
} from "@/components/ui/icons";

interface NavItem {
  href: string;
  label: string;
  icon: React.ReactNode;
  /** Matches sub-routes too, e.g. `/projects/abc`. */
  prefix?: boolean;
}

const PRIMARY_NAV: readonly NavItem[] = [
  { href: "/dashboard", label: "Dashboard", icon: <GridIcon /> },
  { href: "/projects", label: "Projects", icon: <PlanIcon />, prefix: true },
  { href: "/compare", label: "Compare", icon: <LayersIcon /> },
];

const SECONDARY_NAV: readonly NavItem[] = [
  { href: "/docs", label: "Documentation", icon: <BookIcon />, prefix: true },
  { href: "/settings", label: "Settings", icon: <SettingsIcon /> },
];

export interface AppShellProps {
  children: React.ReactNode;
  /** Page title, rendered in the header. */
  title?: React.ReactNode;
  description?: React.ReactNode;
  breadcrumbs?: readonly Crumb[];
  /** Actions on the right of the page header. */
  actions?: React.ReactNode;
  /** Constrain content width. `wide` for tables, `default` for forms. */
  width?: "default" | "wide" | "full";
}

const WIDTHS = {
  default: "max-w-(--container-content)",
  wide: "max-w-[90rem]",
  full: "max-w-none",
} as const;

export function AppShell({
  children,
  title,
  description,
  breadcrumbs,
  actions,
  width = "default",
}: AppShellProps) {
  const pathname = usePathname();
  const [mobileOpen, setMobileOpen] = useState(false);
  const [commandOpen, setCommandOpen] = useState(false);

  // Navigating must close the mobile drawer, or the user lands on the new page
  // with the menu still covering it.
  useEffect(() => setMobileOpen(false), [pathname]);

  useEffect(() => {
    const handle = (event: KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        setCommandOpen((open) => !open);
      }
    };
    window.addEventListener("keydown", handle);
    return () => window.removeEventListener("keydown", handle);
  }, []);

  return (
    <div className="flex min-h-screen bg-canvas">
      {/*
        Skip link. First tabbable element on the page, visually hidden until
        focused — without it a keyboard user traverses the entire sidebar on
        every navigation to reach the content.
      */}
      <a
        href="#main"
        className={cn(
          "sr-only focus:not-sr-only",
          "focus:fixed focus:top-3 focus:left-3 focus:z-[70]",
          "focus:rounded-md focus:bg-accent-solid focus:px-3 focus:py-2",
          "focus:text-sm focus:font-medium focus:text-on-solid",
        )}
      >
        Skip to content
      </a>

      {/* ---- Sidebar --------------------------------------------------- */}
      <Sidebar
        pathname={pathname}
        mobileOpen={mobileOpen}
        onCloseMobile={() => setMobileOpen(false)}
        onOpenCommand={() => setCommandOpen(true)}
      />

      {/* ---- Content --------------------------------------------------- */}
      <div className="flex min-w-0 flex-1 flex-col">
        <header
          className={cn(
            "sticky top-0 z-30 flex h-(--spacing-topbar) shrink-0 items-center gap-3",
            "border-b border-line-subtle bg-canvas/85 px-4 backdrop-blur-xl lg:px-6",
          )}
        >
          <Button
            variant="ghost"
            size="sm"
            iconOnly
            aria-label="Open navigation"
            onClick={() => setMobileOpen(true)}
            className="lg:hidden"
          >
            <MenuIcon />
          </Button>

          <div className="min-w-0 flex-1">
            {breadcrumbs ? (
              <Breadcrumbs items={breadcrumbs} />
            ) : (
              title && (
                <h1 className="truncate text-sm font-semibold text-primary">{title}</h1>
              )
            )}
          </div>

          <div className="flex shrink-0 items-center gap-1.5">
            <Tooltip content="Search and commands" shortcut={["⌘", "K"]}>
              <Button
                variant="ghost"
                size="sm"
                iconOnly
                aria-label="Search and commands"
                onClick={() => setCommandOpen(true)}
              >
                <SearchIcon />
              </Button>
            </Tooltip>
            <ThemeToggle />
            <Button asChild size="sm" variant="primary" icon={<SparkIcon />}>
              <Link href="/new">New</Link>
            </Button>
          </div>
        </header>

        <main id="main" className="min-w-0 flex-1 px-4 py-6 lg:px-6 lg:py-8">
          <div className={cn("mx-auto min-w-0", WIDTHS[width])}>
            {(title || description || actions) && breadcrumbs && (
              <div className="mb-6 flex flex-wrap items-end justify-between gap-4">
                <div className="min-w-0">
                  {title && (
                    <h1 className="truncate text-xl font-semibold tracking-tight text-primary">
                      {title}
                    </h1>
                  )}
                  {description && (
                    <p className="mt-1 text-sm text-tertiary">{description}</p>
                  )}
                </div>
                {actions && (
                  <div className="flex shrink-0 items-center gap-2">{actions}</div>
                )}
              </div>
            )}
            {!breadcrumbs && (description || actions) && (
              <div className="mb-6 flex flex-wrap items-end justify-between gap-4">
                {description && (
                  <p className="min-w-0 text-sm text-tertiary">{description}</p>
                )}
                {actions && (
                  <div className="flex shrink-0 items-center gap-2">{actions}</div>
                )}
              </div>
            )}
            {children}
          </div>
        </main>
      </div>

      <CommandMenu open={commandOpen} onOpenChange={setCommandOpen} />
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* Sidebar                                                                    */
/* -------------------------------------------------------------------------- */

function Sidebar({
  pathname,
  mobileOpen,
  onCloseMobile,
  onOpenCommand,
}: {
  pathname: string;
  mobileOpen: boolean;
  onCloseMobile: () => void;
  onOpenCommand: () => void;
}) {
  return (
    <>
      {/* Scrim, mobile only. */}
      {mobileOpen && (
        <div
          className="fixed inset-0 z-40 bg-overlay backdrop-blur-[2px] lg:hidden"
          onClick={onCloseMobile}
          aria-hidden
        />
      )}

      <nav
        aria-label="Main"
        className={cn(
          "fixed inset-y-0 left-0 z-50 flex w-(--spacing-sidebar) flex-col",
          "border-r border-line-subtle bg-subtle",
          "transition-transform duration-[--duration-normal] ease-[--ease-standard]",
          mobileOpen ? "translate-x-0" : "-translate-x-full",
          // Above `lg` it is permanent, so the transform never applies.
          "lg:sticky lg:top-0 lg:h-screen lg:translate-x-0",
        )}
      >
        <div className="flex h-(--spacing-topbar) shrink-0 items-center justify-between gap-2 border-b border-line-subtle px-3">
          <Link
            href="/dashboard"
            className="flex min-w-0 items-center gap-2 rounded-md px-1 py-1"
          >
            <span className="flex size-6 shrink-0 items-center justify-center rounded-md bg-accent-solid text-on-solid">
              <CubeIcon className="size-3.5" />
            </span>
            <span className="truncate text-sm font-semibold tracking-tight text-primary">
              ArchX3D
            </span>
          </Link>

          <Button
            variant="ghost"
            size="sm"
            iconOnly
            aria-label="Close navigation"
            onClick={onCloseMobile}
            className="lg:hidden"
          >
            <CloseIcon />
          </Button>
        </div>

        <div className="p-3">
          {/*
            A search *button* that opens the palette, not an input. An input
            here implies typing filters this page; the palette searches
            everything. Showing the shortcut on the control is what teaches it.
          */}
          <button
            type="button"
            onClick={onOpenCommand}
            className={cn(
              "flex h-8 w-full items-center gap-2 rounded-md border border-line bg-sunken px-2.5",
              "text-xs text-tertiary transition-colors hover:border-line-strong hover:text-secondary",
            )}
          >
            <SearchIcon className="size-3.5 shrink-0" />
            <span className="flex-1 text-left">Search…</span>
            <Kbd>⌘K</Kbd>
          </button>
        </div>

        <div className="scroll-slim flex-1 overflow-y-auto px-3 pb-3">
          <ul className="space-y-0.5">
            {PRIMARY_NAV.map((item) => (
              <li key={item.href}>
                <NavLink item={item} pathname={pathname} />
              </li>
            ))}
          </ul>

          <div className="my-3 h-px bg-line-subtle" />

          <ul className="space-y-0.5">
            {SECONDARY_NAV.map((item) => (
              <li key={item.href}>
                <NavLink item={item} pathname={pathname} />
              </li>
            ))}
          </ul>
        </div>

        <div className="shrink-0 border-t border-line-subtle p-3">
          <p className="text-2xs leading-relaxed text-disabled">
            Projects are indexed in this browser.{" "}
            <Link href="/settings" className="underline underline-offset-2 hover:text-tertiary">
              Learn more
            </Link>
          </p>
        </div>
      </nav>
    </>
  );
}

function NavLink({ item, pathname }: { item: NavItem; pathname: string }) {
  const active = item.prefix
    ? pathname === item.href || pathname.startsWith(`${item.href}/`)
    : pathname === item.href;

  return (
    <Link
      href={item.href}
      // `aria-current` is how a screen reader knows which page it is on; the
      // background colour is invisible to it.
      aria-current={active ? "page" : undefined}
      className={cn(
        "flex h-8 items-center gap-2.5 rounded-md px-2.5 text-sm transition-colors",
        "[&_svg]:size-4 [&_svg]:shrink-0",
        active
          ? "bg-surface-hover font-medium text-primary"
          : "text-secondary hover:bg-surface-hover hover:text-primary",
      )}
    >
      {item.icon}
      <span className="truncate">{item.label}</span>
    </Link>
  );
}
