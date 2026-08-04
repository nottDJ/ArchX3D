"use client";

/**
 * ArchX3D — generation wizard
 * ===========================
 * Five steps from a DXF to a walkable model.
 *
 * What changed in the redesign, and why
 * ------------------------------------
 * **The stepper is now a progress indicator, not five equal cards.** Before,
 * each step was a bordered box of the same size and weight, so a user could
 * not tell at a glance which was current — the only difference was a tint, and
 * "done" and "current" used two greens a colour-blind user reads as identical.
 * Now the current step is the only one with a label at full contrast, done
 * steps carry a tick, and the connector line fills as you advance.
 *
 * **The forward action is the only primary button on screen.** Previously
 * "Continue without images" and "Analyse images" were the same white button,
 * so skipping and proceeding looked equally intended. Now the recommended path
 * is primary and the skip is a ghost.
 *
 * **Errors are typed and dismissible in place.** A single red bar at the top
 * for every failure meant a validation warning about one image looked
 * identical to the backend being down. The alert now carries a tone and, where
 * one exists, a retry.
 *
 * **Projects register locally as soon as they exist**, so the dashboard can
 * find them. Previously a created project was reachable only through the tab
 * that made it.
 */

import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";

import { DropZone } from "./DropZone";
import { ReviewStep } from "./ReviewStep";
import { AppShell } from "@/components/shell/AppShell";
import {
  Alert,
  Badge,
  Button,
  Card,
  EmptyState,
  Progress,
  Spinner,
  cn,
  useToast,
} from "@/components/ui";
import {
  ArrowRightIcon,
  CheckIcon,
  CubeIcon,
  DownloadIcon,
  ImageIcon,
  PlanIcon,
  SparkIcon,
  TerminalIcon,
  TrashIcon,
  WalkIcon,
  WarningIcon,
} from "@/components/ui/icons";
import { projectViewerUrl } from "@/lib/api";
import { formatBytes, formatDuration, pluralise } from "@/lib/format";
import { register, touch } from "@/lib/projects";
import {
  isJobFinished,
  wizardApi,
  WizardApiError,
  WIZARD_STEP_META,
  type JobState,
  type ProjectManifest,
  type ReviewEdits,
  type ReviewPayload,
  type WizardStep,
} from "@/lib/wizard";

/** How often to poll a running job. Analysis is slow; this is not a hot loop. */
const POLL_INTERVAL_MS = 1500;

interface Problem {
  tone: "warning" | "danger";
  title: string;
  detail: string;
}

export function Wizard() {
  const [step, setStep] = useState<WizardStep>("plan");
  const [manifest, setManifest] = useState<ProjectManifest | null>(null);
  const [review, setReview] = useState<ReviewPayload | null>(null);
  const [job, setJob] = useState<JobState | null>(null);
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<Problem | null>(null);
  const [rejected, setRejected] = useState<Array<{ filename: string; reason: string }>>([]);

  const { toast } = useToast();
  const projectId = manifest?.project_id ?? null;

  // ---- Job polling -------------------------------------------------------
  const pollTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    if (!job || isJobFinished(job.status)) return;

    let cancelled = false;
    const tick = async () => {
      try {
        const next = await wizardApi.job(job.job_id);
        if (cancelled) return;
        setJob(next);
        if (!isJobFinished(next.status)) {
          pollTimer.current = setTimeout(tick, POLL_INTERVAL_MS);
        }
      } catch (exc) {
        if (!cancelled) {
          setProblem({
            tone: "danger",
            title: "Lost contact with the server",
            detail: exc instanceof Error ? exc.message : String(exc),
          });
        }
      }
    };

    pollTimer.current = setTimeout(tick, POLL_INTERVAL_MS);
    return () => {
      cancelled = true;
      if (pollTimer.current) clearTimeout(pollTimer.current);
    };
  }, [job]);

  // When analysis finishes, pull the review payload and advance.
  useEffect(() => {
    if (!projectId || job?.kind !== "analyse" || job.status !== "READY_FOR_REVIEW") return;
    let cancelled = false;
    (async () => {
      try {
        const payload = await wizardApi.review(projectId);
        if (!cancelled) {
          setReview(payload);
          setStep("review");
        }
      } catch (exc) {
        if (!cancelled) {
          setProblem({
            tone: "danger",
            title: "Could not load the analysis",
            detail: exc instanceof Error ? exc.message : String(exc),
          });
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [projectId, job?.kind, job?.status]);

  useEffect(() => {
    if (job?.kind === "generate" && job.status === "COMPLETED") {
      setStep("walkthrough");
      if (projectId) touch(projectId);
    }
  }, [job?.kind, job?.status, projectId]);

  // ---- Actions -----------------------------------------------------------

  const run = useCallback(
    async <T,>(action: () => Promise<T>, failure: string): Promise<T | null> => {
      setBusy(true);
      setProblem(null);
      try {
        return await action();
      } catch (exc) {
        setProblem({
          tone: "danger",
          title: failure,
          detail:
            exc instanceof WizardApiError || exc instanceof Error
              ? exc.message
              : String(exc),
        });
        return null;
      } finally {
        setBusy(false);
      }
    },
    [],
  );

  const handleDxf = (files: File[]) =>
    run(async () => {
      const created = await wizardApi.createProject(files[0]);
      setManifest(created);
      // Index it immediately: a project that only the creating tab knows about
      // is invisible from the dashboard, which is where users look for it.
      register(created);
      setStep("images");
      return created;
    }, "Could not create the project");

  const handleImages = (files: File[]) =>
    run(async () => {
      if (!projectId) return null;
      const result = await wizardApi.uploadImages(projectId, files);
      setManifest(result.manifest);
      setRejected(result.rejected);
      if (result.accepted.length > 0) {
        toast({
          tone: "success",
          title: `${pluralise(result.accepted.length, "image")} added`,
        });
      }
      return result;
    }, "Could not upload the images");

  const handleRemoveImage = (filename: string) =>
    run(async () => {
      if (!projectId) return null;
      setManifest(await wizardApi.deleteImage(projectId, filename));
      return null;
    }, "Could not remove the image");

  const handleAnalyse = () =>
    run(async () => {
      if (!projectId) return null;
      const started = await wizardApi.analyse(projectId);
      setJob(started);
      return started;
    }, "Could not start the analysis");

  const handleApplyEdits = async (edits: ReviewEdits) => {
    await run(async () => {
      if (!projectId) return null;
      const response = await wizardApi.applyEdits(projectId, edits);
      setReview(response.review);

      if (response.report.rejected_edits.length > 0) {
        setProblem({
          tone: "danger",
          title: "Some changes were not applied",
          detail: response.report.rejected_edits.join("; "),
        });
      } else if (response.report.warnings.length > 0) {
        // Accepted, but worth saying out loud — a deliberate overlap is the
        // user's call, and silently building it would be a surprise later.
        setProblem({
          tone: "warning",
          title: "Applied, with warnings",
          detail: response.report.warnings.join("; "),
        });
      } else {
        toast({ tone: "success", title: "Changes applied" });
      }
      return response;
    }, "Could not apply your changes");
  };

  const handleGenerate = () =>
    run(async () => {
      if (!projectId) return null;
      const started = await wizardApi.generate(projectId);
      setJob(started);
      setStep("generate");
      return started;
    }, "Could not start the build");

  // ---- Render ------------------------------------------------------------

  const currentIndex = WIZARD_STEP_META.findIndex((meta) => meta.id === step);

  return (
    <AppShell
      title="New generation"
      breadcrumbs={[
        { label: "Projects", href: "/projects" },
        { label: manifest ? "New generation" : "New generation" },
      ]}
      description="Turn a DXF floor plan and reference imagery into an explorable 3D model."
      width={step === "review" ? "wide" : "default"}
    >
      <div className="space-y-6">
        <Stepper current={step} />

        {problem && (
          <Alert
            tone={problem.tone}
            title={problem.title}
            onDismiss={() => setProblem(null)}
          >
            {problem.detail}
          </Alert>
        )}

        {step === "plan" && <PlanStep busy={busy} onFiles={handleDxf} />}

        {step === "images" && manifest && (
          <>
            <ImagesStep
              manifest={manifest}
              projectId={manifest.project_id}
              busy={busy}
              rejected={rejected}
              onFiles={handleImages}
              onRemove={handleRemoveImage}
              onContinue={handleAnalyse}
              analysing={job?.kind === "analyse" && !isJobFinished(job.status)}
            />
            {job?.kind === "analyse" && !isJobFinished(job.status) && (
              <JobProgress
                job={job}
                title="Analysing your project"
                hint="Reading the plan and interpreting the reference imagery."
              />
            )}
          </>
        )}

        {step === "review" && review && manifest && (
          <ReviewStep
            projectId={manifest.project_id}
            review={review}
            busy={busy}
            onApply={handleApplyEdits}
            onGenerate={handleGenerate}
          />
        )}

        {step === "generate" && job && (
          <JobProgress
            job={job}
            title="Building the 3D scene"
            hint="Extruding geometry, applying materials and lighting, exporting the model."
          />
        )}

        {step === "walkthrough" && manifest && (
          <WalkthroughStep manifest={manifest} />
        )}

        {/* Position in the flow, for orientation on a long page. */}
        {step !== "walkthrough" && (
          <p className="text-center text-xs text-tertiary">
            Step {currentIndex + 1} of {WIZARD_STEP_META.length}
          </p>
        )}
      </div>
    </AppShell>
  );
}

/* -------------------------------------------------------------------------- */
/* Stepper                                                                    */
/* -------------------------------------------------------------------------- */

/**
 * Progress through the five steps.
 *
 * Only the current step shows its hint at full contrast — a stepper where
 * every label is equally legible is a list, not an indicator of position. Done
 * steps collapse to a tick and a name; upcoming steps dim.
 *
 * The connector fills between completed steps, so progress reads horizontally
 * even at a glance, and the shape (tick / filled ring / hollow ring) carries
 * the state without relying on the tint.
 */
function Stepper({ current }: { current: WizardStep }) {
  const currentIndex = WIZARD_STEP_META.findIndex((meta) => meta.id === current);

  return (
    <nav aria-label="Progress">
      <ol className="flex items-center gap-1">
        {WIZARD_STEP_META.map((meta, index) => {
          const state =
            index < currentIndex ? "done" : index === currentIndex ? "active" : "todo";

          return (
            <li
              key={meta.id}
              className={cn("flex min-w-0 items-center gap-2", index > 0 && "flex-1")}
            >
              {index > 0 && (
                <span
                  aria-hidden
                  className={cn(
                    "h-px min-w-3 flex-1 transition-colors duration-[--duration-slow]",
                    index <= currentIndex ? "bg-accent-solid" : "bg-line",
                  )}
                />
              )}

              <span
                aria-current={state === "active" ? "step" : undefined}
                className="flex min-w-0 items-center gap-2"
              >
                <span
                  className={cn(
                    "flex size-6 shrink-0 items-center justify-center rounded-full border text-2xs font-medium",
                    "transition-colors duration-[--duration-normal]",
                    state === "done" && "border-accent-solid bg-accent-solid text-on-solid",
                    state === "active" && "border-accent-solid text-accent-text",
                    state === "todo" && "border-line text-disabled",
                  )}
                >
                  {state === "done" ? <CheckIcon className="size-3" /> : index + 1}
                </span>

                <span className="min-w-0">
                  <span
                    className={cn(
                      "block truncate text-xs font-medium",
                      state === "active" ? "text-primary" : "text-tertiary",
                    )}
                  >
                    {meta.label}
                  </span>
                  {state === "active" && (
                    <span className="hidden truncate text-2xs text-tertiary sm:block">
                      {meta.hint}
                    </span>
                  )}
                </span>
              </span>
            </li>
          );
        })}
      </ol>
    </nav>
  );
}

/* -------------------------------------------------------------------------- */
/* Step 1 — plan                                                              */
/* -------------------------------------------------------------------------- */

function PlanStep({
  busy,
  onFiles,
}: {
  busy: boolean;
  onFiles: (files: File[]) => void;
}) {
  return (
    <div className="space-y-4">
      <Alert tone="info" title="Geometry always comes from the DXF">
        Room sizes, walls and openings are read from the drawing. Reference
        photographs supply furniture, materials and lighting — they never move a
        wall.
      </Alert>

      <DropZone
        accept=".dxf"
        disabled={busy}
        title="Upload your DXF floor plan"
        hint="A single .dxf file, up to 120 MB"
        onFiles={onFiles}
      />

      {busy && (
        <p className="flex items-center justify-center gap-2 text-xs text-tertiary">
          <Spinner size="sm" label="Creating project" />
          Creating the project…
        </p>
      )}
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* Step 2 — images                                                            */
/* -------------------------------------------------------------------------- */

function ImagesStep({
  manifest,
  projectId,
  busy,
  rejected,
  onFiles,
  onRemove,
  onContinue,
  analysing,
}: {
  manifest: ProjectManifest;
  projectId: string;
  busy: boolean;
  rejected: Array<{ filename: string; reason: string }>;
  onFiles: (files: File[]) => void;
  onRemove: (filename: string) => void;
  onContinue: () => void;
  analysing: boolean;
}) {
  const images = manifest.images ?? [];
  const hasImages = images.length > 0;

  return (
    <div className="space-y-4">
      <Card elevation="flat" className="flex items-center gap-3 p-3.5">
        <span className="flex size-8 shrink-0 items-center justify-center rounded-md border border-line-subtle bg-sunken text-accent-text [&_svg]:size-4">
          <PlanIcon />
        </span>
        <div className="min-w-0 flex-1">
          <p className="truncate text-sm font-medium text-primary">
            {manifest.dxf?.filename ?? "Floor plan"}
          </p>
          <p className="text-xs text-tertiary">
            {formatBytes(manifest.dxf?.bytes)} · Plan uploaded
          </p>
        </div>
        <Badge tone="success" icon={<CheckIcon />}>
          Ready
        </Badge>
      </Card>

      <DropZone
        accept="image/*"
        multiple
        disabled={busy || analysing}
        title="Add reference photographs"
        hint="JPEG, PNG or WebP — up to 12 images, 25 MB each"
        onFiles={onFiles}
      />

      {rejected.length > 0 && (
        <Alert tone="warning" title={`${pluralise(rejected.length, "file")} not accepted`}>
          <ul className="mt-1 space-y-0.5">
            {rejected.map((item) => (
              <li key={item.filename} className="font-mono text-xs">
                {item.filename} — {item.reason}
              </li>
            ))}
          </ul>
        </Alert>
      )}

      {hasImages ? (
        <ul className="grid grid-cols-2 gap-3 sm:grid-cols-4">
          {images.map((image) => (
            <li
              key={image.filename}
              className="group relative overflow-hidden rounded-lg border border-line-subtle bg-surface"
            >
              {/* eslint-disable-next-line @next/next/no-img-element */}
              <img
                src={wizardApi.imageUrl(projectId, image.filename)}
                alt=""
                loading="lazy"
                decoding="async"
                className="aspect-4/3 w-full bg-sunken object-cover"
              />
              <p className="truncate px-2 py-1.5 font-mono text-2xs text-tertiary">
                {image.filename}
              </p>
              <Button
                variant="secondary"
                size="sm"
                iconOnly
                aria-label={`Remove ${image.filename}`}
                onClick={() => onRemove(image.filename)}
                disabled={busy || analysing}
                // Visible on hover *and* on focus, so it is reachable by
                // keyboard rather than being a mouse-only affordance.
                className="absolute top-1.5 right-1.5 opacity-0 transition-opacity group-hover:opacity-100 focus-visible:opacity-100"
              >
                <TrashIcon />
              </Button>
            </li>
          ))}
        </ul>
      ) : (
        <EmptyState
          icon={<ImageIcon />}
          title="No reference images"
          description="Without photographs the model is built as an unfurnished architectural shell — correct geometry, no furniture or materials."
        />
      )}

      <div className="flex flex-wrap items-center justify-between gap-3 border-t border-line-subtle pt-4">
        <p className="text-xs text-tertiary">
          {hasImages
            ? `${pluralise(images.length, "image")} attached.`
            : "You can add images later by starting again."}
        </p>

        <Button
          variant="primary"
          onClick={onContinue}
          loading={busy || analysing}
          iconTrailing={<ArrowRightIcon />}
        >
          {hasImages ? "Analyse images" : "Continue without images"}
        </Button>
      </div>
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* Job progress                                                               */
/* -------------------------------------------------------------------------- */

/**
 * A running job.
 *
 * The redesign separates the *headline* from the *log*: the current stage and
 * elapsed time are large and legible, and the event stream is a collapsed
 * console beneath. Before, the only indication of progress was a scrolling
 * list of terse status codes, which reads as "something is happening" rather
 * than "this is what is happening and how far along it is".
 */
function JobProgress({
  job,
  title,
  hint,
}: {
  job: JobState;
  title: string;
  hint?: string;
}) {
  const failed = job.status === "FAILED";
  const logRef = useRef<HTMLDivElement>(null);

  // Follow the tail as events arrive.
  useEffect(() => {
    const element = logRef.current;
    if (element) element.scrollTop = element.scrollHeight;
  }, [job.events.length]);

  return (
    <Card elevation="raised" className="overflow-hidden">
      <div className="flex items-start gap-3 p-4">
        <span
          className={cn(
            "flex size-8 shrink-0 items-center justify-center rounded-md border [&_svg]:size-4",
            failed
              ? "border-danger-border bg-danger-surface text-danger-text"
              : "border-accent-border bg-accent-surface text-accent-text",
          )}
        >
          {failed ? <WarningIcon /> : <Spinner />}
        </span>

        <div className="min-w-0 flex-1">
          <div className="flex items-baseline justify-between gap-3">
            <h2 className="truncate text-sm font-semibold text-primary">
              {failed ? "Generation failed" : title}
            </h2>
            <span className="shrink-0 font-mono text-xs text-tertiary">
              {formatDuration(job.elapsed_s)}
            </span>
          </div>

          <p className="mt-0.5 truncate text-xs text-tertiary">
            {job.message || hint}
          </p>

          {!failed && (
            <Progress value={null} className="mt-3" />
          )}
        </div>
      </div>

      {job.error && (
        <div className="px-4 pb-4">
          <Alert tone="danger" title="What went wrong">
            {job.error}
          </Alert>
        </div>
      )}

      <details className="border-t border-line-subtle">
        <summary className="flex cursor-pointer items-center gap-2 px-4 py-2.5 text-xs text-tertiary transition-colors hover:text-secondary">
          <TerminalIcon className="size-3.5" />
          Pipeline log
          <span className="ml-auto font-mono text-2xs">
            {pluralise(job.events.length, "event")}
          </span>
        </summary>

        <div
          ref={logRef}
          className="scroll-slim max-h-56 overflow-y-auto bg-sunken px-4 py-3 font-mono text-xs leading-6"
          // A live region so a screen-reader user hears progress without
          // repeatedly navigating back to the log.
          aria-live="polite"
        >
          {job.events.map((event, index) => (
            <p key={index} className="flex gap-2.5">
              <span className="shrink-0 text-disabled">{event.status}</span>
              <span
                className={
                  failed && index === job.events.length - 1
                    ? "text-danger-text"
                    : "text-secondary"
                }
              >
                {event.message}
              </span>
            </p>
          ))}
        </div>
      </details>
    </Card>
  );
}

/* -------------------------------------------------------------------------- */
/* Step 5 — result                                                            */
/* -------------------------------------------------------------------------- */

function WalkthroughStep({ manifest }: { manifest: ProjectManifest }) {
  const projectId = manifest.project_id;

  return (
    <div className="space-y-4">
      <Card elevation="raised" className="overflow-hidden">
        <div className="flex items-start gap-3 border-b border-line-subtle bg-success-surface p-4">
          <span className="flex size-8 shrink-0 items-center justify-center rounded-md border border-success-border text-success-text [&_svg]:size-4">
            <CheckIcon />
          </span>
          <div className="min-w-0">
            <h2 className="text-sm font-semibold text-success-text">
              Your model is ready
            </h2>
            <p className="mt-0.5 text-xs text-success-text opacity-85">
              The reviewed scene graph has been built into a 3D model.
            </p>
          </div>
        </div>

        <div className="p-4">
          <div className="flex flex-wrap gap-2.5">
            <Button
              asChild
              variant="primary"
              size="lg"
              icon={<WalkIcon />}
              onClick={() => touch(projectId)}
            >
              <Link href={projectViewerUrl(projectId)}>Explore in 3D</Link>
            </Button>

            <Button asChild variant="secondary" size="lg" icon={<DownloadIcon />}>
              <a href={wizardApi.modelUrl(projectId)} download>
                Download GLB
              </a>
            </Button>
          </div>

          <ul className="mt-5 grid gap-3 sm:grid-cols-3">
            {[
              { icon: <WalkIcon />, text: "Walk through it in first person, with collision" },
              { icon: <CubeIcon />, text: "Hide the roof to see the interior" },
              { icon: <PlanIcon />, text: "Jump to any room from the plan" },
            ].map((item) => (
              <li key={item.text} className="flex items-start gap-2.5 text-xs text-tertiary">
                <span className="mt-px shrink-0 text-accent-text [&_svg]:size-3.5">
                  {item.icon}
                </span>
                {item.text}
              </li>
            ))}
          </ul>
        </div>
      </Card>

      <div className="flex flex-wrap items-center justify-between gap-3">
        <Button asChild variant="ghost" size="sm">
          <Link href="/projects">All projects</Link>
        </Button>
        <Button asChild variant="secondary" size="sm" icon={<SparkIcon />}>
          <Link href="/new">Start another</Link>
        </Button>
      </div>
    </div>
  );
}
