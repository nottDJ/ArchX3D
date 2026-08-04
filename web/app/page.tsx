import type { Metadata } from "next";
import Link from "next/link";

import { Button } from "@/components/ui/Button";
import { Card } from "@/components/ui/Card";
import {
  ArrowRightIcon,
  CubeIcon,
  ImageIcon,
  PlanIcon,
  RoofOffIcon,
  SparkIcon,
  WalkIcon,
} from "@/components/ui/icons";

/**
 * `/` — the landing page.
 *
 * Rendered without the app shell. This is the one page a visitor may reach
 * before they have a project, and a sidebar full of destinations they cannot
 * use yet is noise. It is also a fully static server component — zero client
 * JavaScript — so it paints from the HTML on any connection.
 *
 * Restraint, deliberately
 * -----------------------
 * One accent colour, one glow, no gradient text, no animated mesh, no floating
 * 3D hero. The audience is architects evaluating a tool, and the visual
 * language they trust is the one their own drawings use: precise, quiet,
 * mostly monochrome. Every effect here explains the product rather than
 * decorating it.
 */

export const metadata: Metadata = {
  title: "ArchX3D — floor plans into explorable 3D",
  description:
    "Turn a DXF floor plan and a handful of reference photographs into a furnished, explorable 3D model you can walk through in the browser.",
};

const STEPS = [
  {
    icon: <PlanIcon />,
    title: "Upload a floor plan",
    body: "A standard DXF. Walls, doors and windows are read from the layers you already draw on — geometry always comes from the plan, never from a guess.",
  },
  {
    icon: <ImageIcon />,
    title: "Add reference photographs",
    body: "Optional. Photographs supply furniture, materials and lighting. Without them you get the architectural shell, correctly and quickly.",
  },
  {
    icon: <SparkIcon />,
    title: "Review before it builds",
    body: "Every detection is shown with its confidence, including the ones that were discarded. You correct what is wrong before render time is spent on it.",
  },
  {
    icon: <WalkIcon />,
    title: "Walk through the result",
    body: "Orbit the building or explore it in first person. Take the roof off, isolate the structure, jump to any room.",
  },
] as const;

const CAPABILITIES = [
  {
    title: "Geometry from the drawing",
    body: "Wall runs, thicknesses, openings and room boundaries are extracted from the DXF itself, so the model matches the plan it came from rather than approximating it.",
  },
  {
    title: "Materials and light from photographs",
    body: "Surface finishes, colour palettes and each room's lighting conditions are recovered from reference imagery and applied per room, not globally.",
  },
  {
    title: "Nothing invented",
    body: "A detection below the confidence threshold is recorded and withheld rather than built. A missing object is far easier to correct than an imaginary one.",
  },
  {
    title: "Measured, not asserted",
    body: "The reconstruction is scored against the photographs it came from, axis by axis, and the report names which subsystem to change.",
  },
] as const;

export default function LandingPage() {
  return (
    <div className="min-h-screen bg-canvas">
      {/* ---- Header ------------------------------------------------------ */}
      <header className="sticky top-0 z-30 border-b border-line-subtle bg-canvas/85 backdrop-blur-xl">
        <div className="mx-auto flex h-(--spacing-topbar) max-w-(--container-content) items-center gap-3 px-5">
          <Link href="/" className="flex items-center gap-2 rounded-md">
            <span className="flex size-6 items-center justify-center rounded-md bg-accent-solid text-on-solid">
              <CubeIcon className="size-3.5" />
            </span>
            <span className="text-sm font-semibold tracking-tight text-primary">
              ArchX3D
            </span>
          </Link>

          <nav aria-label="Primary" className="ml-auto flex items-center gap-1">
            <Button asChild variant="ghost" size="sm" className="hidden sm:inline-flex">
              <Link href="/docs">Documentation</Link>
            </Button>
            <Button asChild variant="ghost" size="sm">
              <Link href="/dashboard">Dashboard</Link>
            </Button>
            <Button asChild variant="primary" size="sm">
              <Link href="/new">Start</Link>
            </Button>
          </nav>
        </div>
      </header>

      {/* ---- Hero -------------------------------------------------------- */}
      <section className="relative overflow-hidden border-b border-line-subtle">
        <div aria-hidden className="pattern-glow pointer-events-none absolute inset-0" />
        <div aria-hidden className="pattern-grid pointer-events-none absolute inset-0" />

        <div className="relative mx-auto max-w-(--container-content) px-5 py-20 sm:py-28">
          <div className="max-w-2xl">
            <p className="mb-5 inline-flex items-center gap-2 rounded-full border border-line bg-surface px-3 py-1 text-xs text-secondary">
              <span className="size-1.5 rounded-full bg-accent-solid" aria-hidden />
              DXF · Photographs · Blender · glTF
            </p>

            <h1 className="text-3xl font-semibold tracking-tight text-primary sm:text-4xl">
              Floor plans into buildings you can walk through.
            </h1>

            <p className="mt-5 max-w-xl text-md leading-relaxed text-secondary">
              ArchX3D reads a DXF and a handful of interior photographs, reconstructs
              the space as a furnished 3D model, and lets you explore it in the
              browser — no download, no CAD licence, no manual modelling.
            </p>

            <div className="mt-8 flex flex-wrap items-center gap-3">
              <Button asChild size="lg" variant="primary" iconTrailing={<ArrowRightIcon />}>
                <Link href="/new">Start a generation</Link>
              </Button>
              <Button asChild size="lg" variant="secondary">
                <Link href="/docs">Read the documentation</Link>
              </Button>
            </div>

            <p className="mt-4 text-xs text-tertiary">
              Runs against your own ArchX3D backend. No account required.
            </p>
          </div>
        </div>
      </section>

      {/* ---- How it works ------------------------------------------------ */}
      <section className="mx-auto max-w-(--container-content) px-5 py-16 sm:py-20">
        <div className="mb-10 max-w-xl">
          <h2 className="text-xl font-semibold tracking-tight text-primary">
            Four steps, and you review the third
          </h2>
          <p className="mt-2 text-sm leading-relaxed text-secondary">
            The pipeline shows you what it understood before it spends render time
            on it. Correcting a misread sofa takes a moment; discovering it in a
            finished model takes a rebuild.
          </p>
        </div>

        <ol className="grid gap-4 sm:grid-cols-2">
          {STEPS.map((step, index) => (
            <Card key={step.title} as="li" elevation="flat" className="p-5">
              <div className="flex items-start gap-3.5">
                <span className="flex size-8 shrink-0 items-center justify-center rounded-md border border-line-subtle bg-sunken text-accent-text [&_svg]:size-4">
                  {step.icon}
                </span>
                <div className="min-w-0">
                  <h3 className="flex items-baseline gap-2 text-sm font-semibold text-primary">
                    <span className="font-mono text-2xs text-tertiary">
                      {String(index + 1).padStart(2, "0")}
                    </span>
                    {step.title}
                  </h3>
                  <p className="mt-1.5 text-sm leading-relaxed text-tertiary">
                    {step.body}
                  </p>
                </div>
              </div>
            </Card>
          ))}
        </ol>
      </section>

      {/* ---- Viewer ------------------------------------------------------ */}
      <section className="border-y border-line-subtle bg-subtle">
        <div className="mx-auto max-w-(--container-content) px-5 py-16 sm:py-20">
          <div className="grid items-center gap-10 lg:grid-cols-2">
            <div>
              <h2 className="text-xl font-semibold tracking-tight text-primary">
                An architectural viewer, not a model preview
              </h2>
              <p className="mt-3 text-sm leading-relaxed text-secondary">
                Generated buildings have ceilings, which is correct and makes the
                interior invisible from outside. So the viewer knows which mesh is
                the roof and takes it off — along with isolating structure,
                furniture or lighting, and flying to any room in the plan.
              </p>

              <ul className="mt-6 space-y-3">
                {[
                  { icon: <RoofOffIcon />, text: "Hide the roof to see inside, in one keystroke" },
                  { icon: <WalkIcon />, text: "First-person walkthrough with real collision — no clipping through walls" },
                  { icon: <PlanIcon />, text: "Room list and plan minimap, driven by the model's own metadata" },
                ].map((item) => (
                  <li key={item.text} className="flex items-start gap-3 text-sm text-secondary">
                    <span className="mt-px shrink-0 text-accent-text [&_svg]:size-4">
                      {item.icon}
                    </span>
                    {item.text}
                  </li>
                ))}
              </ul>
            </div>

            {/*
              A schematic rather than a screenshot. A screenshot dates the
              moment the UI changes and a video is megabytes the visitor did
              not ask for; this describes the viewer's layout honestly, scales
              to any width, themes itself, and costs nothing.
            */}
            <Card elevation="raised" className="overflow-hidden p-0">
              <div className="flex items-center gap-1.5 border-b border-line-subtle bg-sunken px-3 py-2">
                <span className="size-2 rounded-full bg-line-strong" aria-hidden />
                <span className="size-2 rounded-full bg-line-strong" aria-hidden />
                <span className="size-2 rounded-full bg-line-strong" aria-hidden />
                <span className="ml-2 font-mono text-2xs text-tertiary">
                  archx3d / viewer
                </span>
              </div>

              <div className="relative aspect-4/3 bg-canvas">
                <div aria-hidden className="pattern-grid absolute inset-0 opacity-70" />

                <svg
                  viewBox="0 0 200 150"
                  className="absolute inset-0 size-full p-8 text-line-strong"
                  role="img"
                  aria-label="Schematic of a floor plan as shown in the viewer, with a camera position marked"
                >
                  <g fill="none" stroke="currentColor" strokeWidth="1.5">
                    <rect x="20" y="24" width="160" height="102" rx="2" />
                    <path d="M20 78h72M92 24v54M92 96h88" />
                  </g>
                  <g fill="currentColor" opacity="0.14">
                    <rect x="26" y="30" width="60" height="42" rx="1" />
                    <rect x="98" y="102" width="76" height="18" rx="1" />
                  </g>
                  <g stroke="var(--accent-solid)" strokeWidth="2" fill="none">
                    <circle cx="56" cy="100" r="4" />
                    <path d="M56 100l-16-9M56 100l16-9" opacity="0.55" />
                  </g>
                </svg>

                <div className="absolute inset-x-0 bottom-0 flex justify-center p-3">
                  <div className="flex items-center gap-1 rounded-lg border border-line bg-raised/90 p-1 shadow-md backdrop-blur">
                    {[<CubeIcon key="a" />, <WalkIcon key="b" />, <RoofOffIcon key="c" />].map(
                      (icon, index) => (
                        <span
                          key={index}
                          className="flex size-6 items-center justify-center rounded-sm text-tertiary [&_svg]:size-3.5"
                        >
                          {icon}
                        </span>
                      ),
                    )}
                  </div>
                </div>
              </div>
            </Card>
          </div>
        </div>
      </section>

      {/* ---- Capabilities ------------------------------------------------ */}
      <section className="mx-auto max-w-(--container-content) px-5 py-16 sm:py-20">
        <h2 className="mb-8 text-xl font-semibold tracking-tight text-primary">
          What the pipeline promises
        </h2>

        <dl className="grid gap-x-10 gap-y-8 sm:grid-cols-2">
          {CAPABILITIES.map((item) => (
            <div key={item.title}>
              <dt className="text-sm font-semibold text-primary">{item.title}</dt>
              <dd className="mt-1.5 text-sm leading-relaxed text-tertiary">
                {item.body}
              </dd>
            </div>
          ))}
        </dl>
      </section>

      {/* ---- Call to action ---------------------------------------------- */}
      <section className="border-t border-line-subtle">
        <div className="mx-auto max-w-(--container-content) px-5 py-16 text-center sm:py-20">
          <h2 className="text-xl font-semibold tracking-tight text-primary">
            Start with a plan you already have
          </h2>
          <p className="mx-auto mt-2 max-w-md text-sm leading-relaxed text-secondary">
            A DXF is enough. Add photographs when you want the interior furnished.
          </p>
          <Button
            asChild
            size="lg"
            variant="primary"
            className="mt-7"
            iconTrailing={<ArrowRightIcon />}
          >
            <Link href="/new">Start a generation</Link>
          </Button>
        </div>
      </section>

      {/* ---- Footer ------------------------------------------------------ */}
      <footer className="border-t border-line-subtle bg-subtle">
        <div className="mx-auto flex max-w-(--container-content) flex-wrap items-center justify-between gap-4 px-5 py-6">
          <p className="text-xs text-tertiary">ArchX3D — DXF to explorable 3D.</p>
          <nav aria-label="Footer" className="flex items-center gap-4 text-xs">
            <Link href="/docs" className="text-tertiary transition-colors hover:text-primary">
              Documentation
            </Link>
            <Link href="/dashboard" className="text-tertiary transition-colors hover:text-primary">
              Dashboard
            </Link>
            <Link href="/settings" className="text-tertiary transition-colors hover:text-primary">
              Settings
            </Link>
          </nav>
        </div>
      </footer>
    </div>
  );
}
