"use client";

/**
 * ArchX3D — project card
 * =======================
 * One project, summarised. Used by both `DashboardView` and `ProjectsView` so
 * a project looks and behaves identically wherever it appears.
 *
 * No thumbnail
 * ------------
 * The API has no endpoint that renders a preview image for a project — see
 * `docs/FRONTEND_ARCHITECTURE.md` §12, "Needs a backend change". Showing a
 * generic icon instead of inventing a placeholder photo is the same rule the
 * rest of the product follows: nothing is shown that was not actually
 * measured or produced.
 *
 * Only "generated" projects are openable
 * ---------------------------------------
 * The wizard (`components/wizard/Wizard.tsx`) does not yet read a project id
 * from the URL to resume an in-progress upload — it only ever starts fresh.
 * So a card for a project earlier than "generated" has nowhere honest to
 * link to; it shows its status and offers rename/pin/remove, but not "Open".
 * Building a resume flow is a real feature, not a card-level fix, and is out
 * of scope here.
 */

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";

import {
  Badge,
  Button,
  Card,
  ConfirmDialog,
  Dialog,
  Field,
  Input,
  Menu,
  MenuItem,
  MenuSeparator,
  StatusBadge,
  statusFromStage,
  useToast,
} from "@/components/ui";
import {
  ClockIcon,
  CubeIcon,
  ImageIcon,
  MoreIcon,
  PinIcon,
  TrashIcon,
} from "@/components/ui/icons";
import type { Project } from "@/hooks/useProjects";
import { projectViewerUrl } from "@/lib/api";
import { formatBytes, formatRelative, pluralise } from "@/lib/format";
import { forget, rename, setPinned, touch } from "@/lib/projects";

export interface ProjectCardProps {
  readonly project: Project;
}

export function ProjectCard({ project }: ProjectCardProps) {
  const router = useRouter();
  const { toast } = useToast();

  const [renaming, setRenaming] = useState(false);
  const [nameDraft, setNameDraft] = useState(project.name);
  const [confirmRemove, setConfirmRemove] = useState(false);

  const ready = project.stage === "generated";
  const openHref = ready ? projectViewerUrl(project.id) : undefined;

  // "missing" (server no longer has it) outranks the stage-derived status —
  // a project that vanished server-side is not usefully described as "Draft".
  const status = project.missing ? "failed" : statusFromStage(project.stage);
  const statusLabel = project.missing ? "Not found" : undefined;

  const openRename = () => {
    setNameDraft(project.name);
    setRenaming(true);
  };

  const commitRename = () => {
    rename(project.id, nameDraft);
    setRenaming(false);
  };

  const handleOpen = () => {
    if (!openHref) return;
    touch(project.id);
    router.push(openHref);
  };

  return (
    <Card
      as="li"
      elevation="raised"
      interactive={ready}
      className="flex flex-col overflow-hidden p-0"
    >
      {openHref ? (
        <Link href={openHref} onClick={() => touch(project.id)} aria-label={`Open ${project.name} in the viewer`}>
          <Preview status={status} />
        </Link>
      ) : (
        <Preview status={status} />
      )}

      <div className="flex flex-1 flex-col gap-2.5 p-4">
        <div className="flex items-start justify-between gap-2">
          <div className="min-w-0">
            {openHref ? (
              <Link
                href={openHref}
                onClick={() => touch(project.id)}
                className="block truncate text-sm font-semibold text-primary hover:underline"
              >
                {project.name}
              </Link>
            ) : (
              <p className="truncate text-sm font-semibold text-primary">{project.name}</p>
            )}
            <p className="mt-0.5 truncate text-xs text-tertiary">
              {project.dxfName ?? "No plan uploaded"}
            </p>
          </div>

          <div className="flex shrink-0 items-center gap-0.5">
            <Button
              variant="ghost"
              size="sm"
              iconOnly
              aria-label={project.pinned ? "Unpin project" : "Pin project"}
              onClick={() => setPinned(project.id, !project.pinned)}
              className={project.pinned ? "text-accent-text" : undefined}
            >
              <PinIcon />
            </Button>

            <Menu
              trigger={
                <Button variant="ghost" size="sm" iconOnly aria-label={`Actions for ${project.name}`}>
                  <MoreIcon />
                </Button>
              }
            >
              {ready && (
                <>
                  <MenuItem icon={<CubeIcon />} onSelect={handleOpen}>
                    Open in 3D
                  </MenuItem>
                  <MenuSeparator />
                </>
              )}
              <MenuItem onSelect={openRename}>Rename</MenuItem>
              <MenuItem
                icon={<PinIcon />}
                onSelect={() => setPinned(project.id, !project.pinned)}
              >
                {project.pinned ? "Unpin" : "Pin"}
              </MenuItem>
              <MenuSeparator />
              <MenuItem
                destructive
                icon={<TrashIcon />}
                onSelect={() => setConfirmRemove(true)}
              >
                Remove from list
              </MenuItem>
            </Menu>
          </div>
        </div>

        <div className="flex flex-wrap items-center gap-1.5">
          <StatusBadge status={status} label={statusLabel} />
          {project.imageCount ? (
            <Badge tone="neutral" icon={<ImageIcon />}>
              {pluralise(project.imageCount, "photo")}
            </Badge>
          ) : null}
        </div>

        <div className="mt-auto flex items-center justify-between gap-3 pt-1 text-2xs text-tertiary">
          <span className="inline-flex items-center gap-1" title={project.openedAt}>
            <ClockIcon className="size-3 shrink-0" />
            {formatRelative(project.openedAt)}
          </span>
          <span className="font-mono tabular-nums">{formatBytes(project.bytes)}</span>
        </div>
      </div>

      <Dialog
        open={renaming}
        onOpenChange={setRenaming}
        title="Rename project"
        size="sm"
        footer={
          <>
            <Button variant="ghost" onClick={() => setRenaming(false)}>
              Cancel
            </Button>
            <Button variant="primary" onClick={commitRename} disabled={!nameDraft.trim()}>
              Save
            </Button>
          </>
        }
      >
        <form
          onSubmit={(event) => {
            event.preventDefault();
            commitRename();
          }}
        >
          <Field label="Name">
            <Input
              autoFocus
              value={nameDraft}
              onChange={(event) => setNameDraft(event.target.value)}
              maxLength={80}
            />
          </Field>
        </form>
      </Dialog>

      <ConfirmDialog
        open={confirmRemove}
        onOpenChange={setConfirmRemove}
        title="Remove from list?"
        description="This only removes the project from this browser's list. Nothing is deleted from the server, and a direct link still works."
        confirmLabel="Remove"
        onConfirm={() => {
          forget(project.id);
          setConfirmRemove(false);
          toast({ tone: "info", title: "Removed from list" });
        }}
      />
    </Card>
  );
}

function Preview({ status }: { status: ReturnType<typeof statusFromStage> }) {
  return (
    <div className="relative aspect-4/3 w-full overflow-hidden bg-sunken">
      <div aria-hidden className="pattern-grid absolute inset-0 opacity-40" />
      <div className="absolute inset-0 flex items-center justify-center">
        <span
          className={
            "flex size-11 items-center justify-center rounded-xl border border-line-subtle bg-surface [&_svg]:size-5 " +
            (status === "complete" ? "text-accent-text" : "text-tertiary")
          }
        >
          <CubeIcon />
        </span>
      </div>
    </div>
  );
}
