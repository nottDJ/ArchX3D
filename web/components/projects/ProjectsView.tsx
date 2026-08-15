"use client";

/**
 * ArchX3D — projects
 * ===================
 * Every project this browser has created, searchable, filterable and sortable.
 *
 * Search, filter and sort live in the URL, not `useState`
 * ---------------------------------------------------------
 * See `docs/FRONTEND_ARCHITECTURE.md` §3, "URL as state". Filtering to
 * "in progress", opening one, and pressing Back should return to the filtered
 * list, not an unfiltered one — a `useState` here would lose that. This is
 * also why the route (`app/projects/page.tsx`) wraps this component in a
 * Suspense boundary: `useSearchParams` opts a component into client
 * rendering, and without the boundary Next deopts the whole route at build
 * time.
 */

import Link from "next/link";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { useMemo } from "react";

import { AppShell } from "@/components/shell/AppShell";
import {
  Alert,
  Button,
  EmptyState,
  Field,
  Input,
  Segmented,
  Select,
  SkeletonCard,
} from "@/components/ui";
import { ArrowRightIcon, PlanIcon, SearchIcon, SparkIcon } from "@/components/ui/icons";
import { useProjects } from "@/hooks/useProjects";
import {
  FILTER_LABELS,
  SORT_LABELS,
  matchesFilter,
  matchesQuery,
  sortProjects,
  type ProjectFilter,
  type ProjectSort,
} from "@/lib/projects";

import { ProjectCard } from "./ProjectCard";

const FILTERS: readonly ProjectFilter[] = ["all", "in-progress", "ready", "pinned"];
const SORTS: readonly ProjectSort[] = ["recent", "created", "name", "size"];

/** Values equal to these are the default and are omitted from the URL. */
const DEFAULTS: Record<string, string> = { q: "", filter: "all", sort: "recent" };

export function ProjectsView() {
  const { projects, loading, offline } = useProjects();
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();

  const query = searchParams.get("q") ?? DEFAULTS.q;
  const filter = (searchParams.get("filter") as ProjectFilter | null) ?? "all";
  const sort = (searchParams.get("sort") as ProjectSort | null) ?? "recent";

  const setParam = (key: string, value: string) => {
    const params = new URLSearchParams(searchParams.toString());
    if (value === DEFAULTS[key]) params.delete(key);
    else params.set(key, value);
    const qs = params.toString();
    router.replace(qs ? `${pathname}?${qs}` : pathname, { scroll: false });
  };

  const visible = useMemo(() => {
    const filtered = projects
      .filter((project) => matchesFilter(project, filter))
      .filter((project) => matchesQuery(project, query));
    return sortProjects(filtered, sort);
  }, [projects, filter, query, sort]);

  const hasAny = projects.length > 0;
  const firstLoad = loading && !hasAny;

  return (
    <AppShell
      title="Projects"
      breadcrumbs={[{ label: "Projects" }]}
      description={`${projects.length} indexed in this browser.`}
      actions={
        <Button asChild variant="primary" size="sm" icon={<SparkIcon />}>
          <Link href="/new">New generation</Link>
        </Button>
      }
      width="wide"
    >
      <div className="space-y-5">
        {offline && (
          <Alert tone="warning" title="Could not reach the ArchX3D backend">
            Showing what this browser last knew. Check that the API is running at
            the address configured in Settings.
          </Alert>
        )}

        <div className="flex flex-wrap items-center gap-3">
          <Field label="Search" className="w-full max-w-xs sm:w-auto">
            <Input
              icon={<SearchIcon />}
              placeholder="Search by name, file or id…"
              value={query}
              onChange={(event) => setParam("q", event.target.value)}
            />
          </Field>

          <Segmented
            label="Filter projects"
            value={filter}
            onChange={(value) => setParam("filter", value)}
            options={FILTERS.map((value) => ({ value, label: FILTER_LABELS[value] }))}
          />

          <Field label="Sort" className="ml-auto w-36">
            <Select value={sort} onChange={(event) => setParam("sort", event.target.value)}>
              {SORTS.map((value) => (
                <option key={value} value={value}>
                  {SORT_LABELS[value]}
                </option>
              ))}
            </Select>
          </Field>
        </div>

        {firstLoad ? (
          <SkeletonGrid />
        ) : visible.length === 0 ? (
          <EmptyState
            icon={<PlanIcon />}
            title={hasAny ? "No projects match" : "No projects yet"}
            description={
              hasAny
                ? "Try a different search or filter."
                : "Upload a DXF floor plan to start your first generation."
            }
            action={
              hasAny ? undefined : (
                <Button asChild variant="primary" iconTrailing={<ArrowRightIcon />}>
                  <Link href="/new">Start a generation</Link>
                </Button>
              )
            }
          />
        ) : (
          <ul className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
            {visible.map((project) => (
              <ProjectCard key={project.id} project={project} />
            ))}
          </ul>
        )}
      </div>
    </AppShell>
  );
}

function SkeletonGrid() {
  return (
    <ul className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
      {Array.from({ length: 8 }, (_, index) => (
        <li key={index}>
          <SkeletonCard />
        </li>
      ))}
    </ul>
  );
}
