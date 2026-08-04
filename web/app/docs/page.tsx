import type { Metadata } from "next";
import Link from "next/link";

import { AppShell } from "@/components/shell/AppShell";
import { Card, Section } from "@/components/ui/Card";
import { Kbd } from "@/components/ui/Badge";
import { Table, TBody, TD, TH, THead, TR } from "@/components/ui/Navigation";
import {
  BookIcon,
  ImageIcon,
  PlanIcon,
  SparkIcon,
  WalkIcon,
} from "@/components/ui/icons";

export const metadata: Metadata = {
  title: "Documentation",
  description: "How ArchX3D works, and every keyboard shortcut.",
};

/**
 * `/docs`
 *
 * The in-app reference: enough to use the product without leaving it, with
 * links to the deep technical documents in the repository for everything else.
 *
 * A static server component. Documentation that needs JavaScript to render is
 * documentation that is unavailable when something is broken — which is
 * exactly when people read it.
 */

const CONCEPTS = [
  {
    icon: <PlanIcon />,
    title: "The DXF is the only source of geometry",
    body: "Wall runs, thicknesses, openings and room boundaries all come from the drawing. Reference photographs never move a wall — they supply what the plan cannot: furniture, materials and lighting.",
  },
  {
    icon: <ImageIcon />,
    title: "Photographs are optional",
    body: "Without them you get a correct, unfurnished architectural shell in a fraction of the time. With them, each room is furnished from what was actually observed in it.",
  },
  {
    icon: <SparkIcon />,
    title: "Nothing is invented",
    body: "A detection the pipeline is not confident about is recorded and withheld rather than built, and the review step shows you what was discarded so you can put it back.",
  },
  {
    icon: <WalkIcon />,
    title: "The roof comes off",
    body: "Generated buildings have ceilings, which is correct and hides the interior. The viewer knows which mesh is the roof and can remove it — from metadata where the model has it, by inference where it does not.",
  },
] as const;

const VIEWER_SHORTCUTS = [
  { keys: ["O"], action: "Orbit mode" },
  { keys: ["W"], action: "Walk mode" },
  { keys: ["R"], action: "Show or hide the roof" },
  { keys: ["V"], action: "Cycle view mode" },
  { keys: ["F"], action: "Toggle wireframe" },
  { keys: ["H"], action: "Reset the camera" },
  { keys: ["P"], action: "Screenshot" },
  { keys: ["M"], action: "Room list" },
  { keys: ["S"], action: "Settings panel" },
  { keys: ["Enter"], action: "Fullscreen" },
] as const;

const WALK_CONTROLS = [
  { keys: ["W", "A", "S", "D"], action: "Move" },
  { keys: ["Mouse"], action: "Look" },
  { keys: ["Shift"], action: "Run" },
  { keys: ["Esc"], action: "Release the cursor" },
] as const;

const APP_SHORTCUTS = [
  { keys: ["⌘", "K"], action: "Search and commands" },
  { keys: ["Ctrl", "K"], action: "Search and commands (Windows / Linux)" },
] as const;

export default function DocsPage() {
  return (
    <AppShell
      title="Documentation"
      breadcrumbs={[{ label: "Documentation" }]}
      description="How the pipeline works, and every shortcut."
    >
      <div className="max-w-(--container-prose) space-y-10">
        {/* ---- Concepts ------------------------------------------------- */}
        <Section
          title="How it works"
          description="Four things worth knowing before your first generation."
        >
          <ul className="space-y-3">
            {CONCEPTS.map((concept) => (
              <Card as="li" key={concept.title} elevation="flat" className="p-4">
                <div className="flex items-start gap-3.5">
                  <span className="flex size-8 shrink-0 items-center justify-center rounded-md border border-line-subtle bg-sunken text-accent-text [&_svg]:size-4">
                    {concept.icon}
                  </span>
                  <div className="min-w-0">
                    <h3 className="text-sm font-semibold text-primary">
                      {concept.title}
                    </h3>
                    <p className="mt-1.5 text-sm leading-relaxed text-tertiary">
                      {concept.body}
                    </p>
                  </div>
                </div>
              </Card>
            ))}
          </ul>
        </Section>

        {/* ---- Shortcuts ------------------------------------------------ */}
        <Section title="Keyboard shortcuts">
          <div className="space-y-6">
            <ShortcutTable caption="Anywhere in the app" rows={APP_SHORTCUTS} />
            <ShortcutTable caption="Viewer" rows={VIEWER_SHORTCUTS} />
            <ShortcutTable caption="Walk mode" rows={WALK_CONTROLS} />
          </div>

          <p className="mt-4 text-xs leading-relaxed text-tertiary">
            While the pointer is locked in walk mode, <Kbd>W</Kbd> and <Kbd>S</Kbd>{" "}
            move rather than switching mode or opening settings — they are movement
            keys first. Every other shortcut works in both modes.
          </p>
        </Section>

        {/* ---- Deep reference ------------------------------------------- */}
        <Section
          title="Technical reference"
          description="The full specifications live in the repository."
        >
          <Card elevation="flat" className="divide-y divide-line-subtle">
            {[
              { file: "VIEWER.md", text: "Camera system, roof detection, BVH collision, view modes, performance" },
              { file: "DESIGN_SYSTEM.md", text: "Tokens, scales, colour ramps, light and dark" },
              { file: "COMPONENT_LIBRARY.md", text: "Every component, its variants and when to use it" },
              { file: "ACCESSIBILITY.md", text: "Conformance, testing and known gaps" },
              { file: "FRONTEND_ARCHITECTURE.md", text: "Folder structure, state, data flow, performance budgets" },
              { file: "UI_GUIDELINES.md", text: "Writing, layout, hierarchy and interaction rules" },
            ].map((doc) => (
              <div key={doc.file} className="flex items-start gap-3 p-3.5">
                <BookIcon className="mt-0.5 size-4 shrink-0 text-tertiary" />
                <div className="min-w-0">
                  <p className="font-mono text-xs text-primary">docs/{doc.file}</p>
                  <p className="mt-0.5 text-xs text-tertiary">{doc.text}</p>
                </div>
              </div>
            ))}
          </Card>
        </Section>

        <p className="text-xs text-tertiary">
          Ready to try it?{" "}
          <Link
            href="/new"
            className="text-accent-text underline underline-offset-2"
          >
            Start a generation
          </Link>
          .
        </p>
      </div>
    </AppShell>
  );
}

function ShortcutTable({
  caption,
  rows,
}: {
  caption: string;
  rows: ReadonlyArray<{ keys: readonly string[]; action: string }>;
}) {
  return (
    <div>
      <h3 className="mb-2 text-xs font-medium text-secondary">{caption}</h3>
      <Table>
        <THead>
          <tr>
            <TH className="w-40">Keys</TH>
            <TH>Action</TH>
          </tr>
        </THead>
        <TBody>
          {rows.map((row) => (
            <TR key={row.action}>
              <TD>
                <span className="flex items-center gap-1">
                  {row.keys.map((key) => (
                    <Kbd key={key}>{key}</Kbd>
                  ))}
                </span>
              </TD>
              <TD className="text-secondary">{row.action}</TD>
            </TR>
          ))}
        </TBody>
      </Table>
    </div>
  );
}
