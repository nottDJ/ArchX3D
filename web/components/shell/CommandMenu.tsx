"use client";

/**
 * ArchX3D — command palette
 * =========================
 * ⌘K. Search projects, jump to a page, run an action.
 *
 * Why a product this size needs one
 * ---------------------------------
 * Not because it is fashionable — because the alternative for "open the
 * project I was working on yesterday" is dashboard → scan → click, three
 * interactions and a visual search. A palette makes it two keystrokes and no
 * searching, and once a user has learned it they stop using the navigation for
 * anything else.
 *
 * It also gives every action *one* discoverable home. A user who cannot
 * remember where the theme setting lives can type "dark" and find it, which is
 * worth more than any amount of menu organisation.
 *
 * Built on Radix Dialog rather than a palette library: the hard parts here are
 * the focus trap and dismissal, which Dialog already solves correctly, and the
 * filtering is twenty lines.
 */

import * as DialogPrimitive from "@radix-ui/react-dialog";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useMemo, useRef, useState } from "react";

import { Kbd, StatusBadge, cn, statusFromStage } from "@/components/ui";
import {
  BookIcon,
  CubeIcon,
  GridIcon,
  LayersIcon,
  MoonIcon,
  PlanIcon,
  SearchIcon,
  SettingsIcon,
  SparkIcon,
  SunIcon,
} from "@/components/ui/icons";
import { useProjects } from "@/hooks/useProjects";
import { useTheme } from "@/hooks/useTheme";
import { formatRelative } from "@/lib/format";
import { matchesQuery } from "@/lib/projects";

interface Command {
  id: string;
  label: string;
  hint?: string;
  icon: React.ReactNode;
  group: "Navigate" | "Create" | "Appearance" | "Projects";
  keywords?: string;
  run: () => void;
}

export function CommandMenu({
  open,
  onOpenChange,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const router = useRouter();
  const { setTheme } = useTheme();
  const { projects } = useProjects();

  const [query, setQuery] = useState("");
  const [active, setActive] = useState(0);
  const listRef = useRef<HTMLDivElement>(null);

  // Reopening should start clean — a stale query from last time makes the
  // palette feel like it remembers the wrong thing.
  useEffect(() => {
    if (open) {
      setQuery("");
      setActive(0);
    }
  }, [open]);

  const commands = useMemo<Command[]>(() => {
    const go = (href: string) => () => {
      onOpenChange(false);
      router.push(href);
    };

    const base: Command[] = [
      { id: "new", label: "New generation", hint: "Upload a DXF floor plan", icon: <SparkIcon />, group: "Create", keywords: "create upload dxf start", run: go("/new") },
      { id: "dashboard", label: "Dashboard", icon: <GridIcon />, group: "Navigate", keywords: "home overview", run: go("/dashboard") },
      { id: "projects", label: "Projects", icon: <PlanIcon />, group: "Navigate", keywords: "list all", run: go("/projects") },
      { id: "compare", label: "Compare models", icon: <LayersIcon />, group: "Navigate", keywords: "side by side diff versus", run: go("/compare") },
      { id: "docs", label: "Documentation", icon: <BookIcon />, group: "Navigate", keywords: "help guide manual", run: go("/docs") },
      { id: "settings", label: "Settings", icon: <SettingsIcon />, group: "Navigate", keywords: "preferences config", run: go("/settings") },
      { id: "theme-light", label: "Light theme", icon: <SunIcon />, group: "Appearance", keywords: "day bright", run: () => { setTheme("light"); onOpenChange(false); } },
      { id: "theme-dark", label: "Dark theme", icon: <MoonIcon />, group: "Appearance", keywords: "night", run: () => { setTheme("dark"); onOpenChange(false); } },
    ];

    // Projects come after the fixed commands so an empty query shows actions
    // first; once the user types, ranking is by match rather than by group.
    const projectCommands: Command[] = projects.slice(0, 40).map((project) => ({
      id: `project-${project.id}`,
      label: project.name,
      hint: formatRelative(project.openedAt),
      icon: <CubeIcon />,
      group: "Projects",
      keywords: `${project.id} ${project.dxfName ?? ""}`,
      run: go(project.stage === "generated" ? `/viewer?project_id=${project.id}` : "/new"),
    }));

    return [...base, ...projectCommands];
  }, [projects, router, setTheme, onOpenChange]);

  const filtered = useMemo(() => {
    const needle = query.trim().toLowerCase();
    if (!needle) return commands;
    return commands.filter((command) =>
      `${command.label} ${command.keywords ?? ""}`.toLowerCase().includes(needle),
    );
  }, [commands, query]);

  // Clamp rather than reset: typing a character that removes the last result
  // should not silently select the first of a different list.
  useEffect(() => {
    setActive((current) => Math.min(current, Math.max(0, filtered.length - 1)));
  }, [filtered.length]);

  const grouped = useMemo(() => {
    const groups = new Map<string, Command[]>();
    for (const command of filtered) {
      const bucket = groups.get(command.group);
      if (bucket) bucket.push(command);
      else groups.set(command.group, [command]);
    }
    return [...groups.entries()];
  }, [filtered]);

  const onKeyDown = (event: React.KeyboardEvent) => {
    if (event.key === "ArrowDown") {
      event.preventDefault();
      setActive((current) => (current + 1) % Math.max(1, filtered.length));
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      setActive((current) => (current - 1 + filtered.length) % Math.max(1, filtered.length));
    } else if (event.key === "Enter") {
      event.preventDefault();
      filtered[active]?.run();
    }
  };

  // Keep the highlighted row in view when arrowing past the fold.
  useEffect(() => {
    listRef.current
      ?.querySelector('[data-active="true"]')
      ?.scrollIntoView({ block: "nearest" });
  }, [active]);

  let flatIndex = -1;

  return (
    <DialogPrimitive.Root open={open} onOpenChange={onOpenChange}>
      <DialogPrimitive.Portal>
        <DialogPrimitive.Overlay className="fixed inset-0 z-[60] bg-overlay backdrop-blur-[2px] data-[state=open]:animate-fade-in" />
        <DialogPrimitive.Content
          onKeyDown={onKeyDown}
          className={cn(
            "fixed top-[12vh] left-1/2 z-[60] w-[calc(100vw-2rem)] max-w-lg -translate-x-1/2",
            "overflow-hidden rounded-xl border border-line bg-raised shadow-xl edge-highlight",
            "data-[state=open]:animate-scale-in",
            "focus:outline-none",
          )}
        >
          <DialogPrimitive.Title className="sr-only">
            Search and commands
          </DialogPrimitive.Title>
          <DialogPrimitive.Description className="sr-only">
            Search projects, jump to a page, or run an action. Use the arrow keys to
            move and Enter to select.
          </DialogPrimitive.Description>

          <div className="flex items-center gap-2.5 border-b border-line-subtle px-3.5">
            <SearchIcon className="size-4 shrink-0 text-tertiary" />
            <input
              autoFocus
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder="Search projects and commands…"
              aria-label="Search projects and commands"
              // The results list is the combobox's popup; announcing the count
              // is what tells a screen-reader user that typing did something.
              role="combobox"
              aria-expanded
              aria-controls="command-results"
              aria-activedescendant={filtered[active] ? `command-${filtered[active].id}` : undefined}
              className="h-11 flex-1 bg-transparent text-sm text-primary placeholder:text-disabled focus:outline-none"
            />
            <Kbd>Esc</Kbd>
          </div>

          <div
            ref={listRef}
            id="command-results"
            role="listbox"
            aria-label="Results"
            className="scroll-slim max-h-[min(24rem,60vh)] overflow-y-auto p-1.5"
          >
            {filtered.length === 0 ? (
              <p className="px-3 py-8 text-center text-sm text-tertiary">
                No matches for “{query}”
              </p>
            ) : (
              grouped.map(([group, items]) => (
                <div key={group} className="mb-1 last:mb-0">
                  <p className="px-2 py-1.5 text-2xs font-medium tracking-wider text-tertiary uppercase">
                    {group}
                  </p>
                  {items.map((command) => {
                    flatIndex += 1;
                    const index = flatIndex;
                    const isActive = index === active;
                    const project = command.group === "Projects"
                      ? projects.find((p) => `project-${p.id}` === command.id)
                      : undefined;

                    return (
                      <div
                        key={command.id}
                        id={`command-${command.id}`}
                        role="option"
                        aria-selected={isActive}
                        data-active={isActive}
                        onClick={command.run}
                        onMouseMove={() => setActive(index)}
                        className={cn(
                          "flex cursor-pointer items-center gap-2.5 rounded-md px-2 py-2 text-sm",
                          "[&_svg]:size-4 [&_svg]:shrink-0",
                          isActive
                            ? "bg-surface-hover text-primary"
                            : "text-secondary",
                        )}
                      >
                        <span className="text-tertiary">{command.icon}</span>
                        <span className="min-w-0 flex-1 truncate">{command.label}</span>
                        {project && (
                          <StatusBadge status={statusFromStage(project.stage)} />
                        )}
                        {command.hint && !project && (
                          <span className="shrink-0 text-xs text-tertiary">
                            {command.hint}
                          </span>
                        )}
                      </div>
                    );
                  })}
                </div>
              ))
            )}
          </div>

          <div className="flex items-center gap-3 border-t border-line-subtle bg-sunken px-3.5 py-2 text-2xs text-tertiary">
            <span className="flex items-center gap-1">
              <Kbd>↑</Kbd>
              <Kbd>↓</Kbd>
              Navigate
            </span>
            <span className="flex items-center gap-1">
              <Kbd>↵</Kbd>
              Open
            </span>
            <Link href="/docs" className="ml-auto hover:text-secondary">
              Shortcuts
            </Link>
          </div>
        </DialogPrimitive.Content>
      </DialogPrimitive.Portal>
    </DialogPrimitive.Root>
  );
}

export { matchesQuery };
