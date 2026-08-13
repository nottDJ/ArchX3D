"use client";

/**
 * ArchX3D — dashboard
 * ====================
 * The landing page inside the app: what exists, what is still running, and a
 * way into the rest of the projects.
 *
 * Two-phase paint
 * ---------------
 * `useProjects` renders the cached local index immediately and corrects it a
 * moment later against the server — see `lib/projects.ts`. The skeleton grid
 * here only appears on a genuinely first load, with no cache to paint from;
 * once anything is cached, real cards show immediately and update in place.
 */

import Link from "next/link";

import { AppShell } from "@/components/shell/AppShell";
import { Alert, Button, EmptyState, Section, SkeletonCard, Stat } from "@/components/ui";
import {
  ArrowRightIcon,
  CubeIcon,
  DatabaseIcon,
  PlanIcon,
  SparkIcon,
} from "@/components/ui/icons";
import { useProjects, useProjectStats, type Project } from "@/hooks/useProjects";
import { formatBytes, pluralise } from "@/lib/format";
import { sortProjects } from "@/lib/projects";

import { ProjectCard } from "./ProjectCard";

const RECENT_LIMIT = 8;

export function DashboardView() {
  const { projects, loading, offline } = useProjects();
  const stats = useProjectStats(projects);

  const recent = sortProjects(projects, "recent").slice(0, RECENT_LIMIT);
  const inProgress = sortProjects(
    projects.filter((project) => project.stage !== "generated"),
    "recent",
  ).slice(0, RECENT_LIMIT);

  const empty = !loading && projects.length === 0;

  return (
    <AppShell
      title="Dashboard"
      breadcrumbs={[{ label: "Dashboard" }]}
      description="Your recent projects, running work and storage."
      actions={
        <Button asChild variant="primary" size="sm" icon={<SparkIcon />}>
          <Link href="/new">New generation</Link>
        </Button>
      }
    >
      <div className="space-y-8">
        {offline && (
          <Alert tone="warning" title="Could not reach the ArchX3D backend">
            Showing what this browser last knew. Check that the API is running at
            the address configured in Settings.
          </Alert>
        )}

        <Section title="Overview">
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
            <Stat label="Projects" value={stats.total} icon={<PlanIcon />} />
            <Stat
              label="Ready"
              value={stats.ready}
              tone="success"
              icon={<CubeIcon />}
            />
            <Stat
              label="In progress"
              value={stats.inProgress}
              tone={stats.inProgress > 0 ? "warning" : "default"}
              icon={<SparkIcon />}
            />
            <Stat
              label="Uploaded"
              value={formatBytes(stats.bytes)}
              icon={<DatabaseIcon />}
              hint={pluralise(stats.images, "reference photo")}
            />
          </div>
        </Section>

        {empty ? (
          <EmptyState
            icon={<PlanIcon />}
            title="No projects yet"
            description="Upload a DXF floor plan to start your first generation."
            action={
              <Button asChild variant="primary" iconTrailing={<ArrowRightIcon />}>
                <Link href="/new">Start a generation</Link>
              </Button>
            }
          />
        ) : (
          <>
            {inProgress.length > 0 && (
              <Section title="In progress" description="Not yet built into a 3D model.">
                <ProjectGrid projects={inProgress} loading={loading} />
              </Section>
            )}

            <Section
              title="Recent"
              actions={
                projects.length > RECENT_LIMIT ? (
                  <Button asChild variant="ghost" size="sm" iconTrailing={<ArrowRightIcon />}>
                    <Link href="/projects">View all</Link>
                  </Button>
                ) : undefined
              }
            >
              <ProjectGrid projects={recent} loading={loading} />
            </Section>
          </>
        )}
      </div>
    </AppShell>
  );
}

function ProjectGrid({
  projects,
  loading,
}: {
  projects: readonly Project[];
  loading: boolean;
}) {
  if (loading && projects.length === 0) {
    return (
      <ul className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
        {Array.from({ length: 4 }, (_, index) => (
          <li key={index}>
            <SkeletonCard />
          </li>
        ))}
      </ul>
    );
  }

  return (
    <ul className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
      {projects.map((project) => (
        <ProjectCard key={project.id} project={project} />
      ))}
    </ul>
  );
}
