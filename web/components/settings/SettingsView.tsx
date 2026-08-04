"use client";

/**
 * ArchX3D — settings
 * ==================
 * Appearance, viewer defaults, connection, and the local project index.
 *
 * Every setting here is real and takes effect immediately
 * ------------------------------------------------------
 * There is no Save button, because there is nothing to save to — these are
 * client preferences held in `localStorage`, and a Save button would imply a
 * server round trip that does not happen. Switches take effect on change,
 * which is what a switch means.
 *
 * What is deliberately absent
 * ---------------------------
 * No account, no billing, no team, no API keys. The ArchX3D backend has no
 * authentication and no notion of a user, so every one of those would be a
 * form that does nothing. Building them would be building a facade; the
 * "Connection" section says plainly what the app talks to instead.
 */

import { useState } from "react";

import { AppShell } from "@/components/shell/AppShell";
import { ThemeSegmented } from "@/components/shell/ThemeToggle";
import {
  Alert,
  Button,
  Card,
  ConfirmDialog,
  Divider,
  Section,
  Slider,
  Switch,
  useToast,
} from "@/components/ui";
import { DatabaseIcon, TrashIcon } from "@/components/ui/icons";
import { useProjects } from "@/hooks/useProjects";
import { useViewerSettings } from "@/hooks/useViewerSettings";
import { API_BASE_URL } from "@/lib/api";
import { formatBytes, pluralise } from "@/lib/format";
import { forgetAll } from "@/lib/projects";
import { SETTING_BOUNDS } from "@/lib/viewer/settings";

export function SettingsView() {
  const { settings, update, toggle, reset } = useViewerSettings();
  const { projects } = useProjects();
  const { toast } = useToast();
  const [confirmClear, setConfirmClear] = useState(false);

  const indexBytes = projects.reduce((total, p) => total + (p.bytes ?? 0), 0);

  return (
    <AppShell
      title="Settings"
      breadcrumbs={[{ label: "Settings" }]}
      description="Preferences are stored in this browser."
    >
      <div className="max-w-(--container-prose) space-y-8">
        {/* ---- Appearance ---------------------------------------------- */}
        <Section title="Appearance">
          <Card elevation="flat" className="divide-y divide-line-subtle">
            <div className="flex items-center justify-between gap-4 p-4">
              <div className="min-w-0">
                <p className="text-sm text-primary">Theme</p>
                <p className="mt-0.5 text-xs text-tertiary">
                  System follows your operating system, including when it changes at
                  sunset.
                </p>
              </div>
              <ThemeSegmented />
            </div>
          </Card>
        </Section>

        {/* ---- Viewer --------------------------------------------------- */}
        <Section
          title="Viewer defaults"
          description="Applied to every model you open. Changeable per session from the viewer's own panel."
        >
          <Card elevation="flat" className="divide-y divide-line-subtle">
            <div className="space-y-5 p-4">
              <Slider
                label="Walking speed"
                value={settings.walkSpeed}
                onValueChange={(walkSpeed) => update({ walkSpeed })}
                min={SETTING_BOUNDS.walkSpeed.min}
                max={SETTING_BOUNDS.walkSpeed.max}
                step={SETTING_BOUNDS.walkSpeed.step}
                unit="m/s"
                hint="A relaxed indoor pace is around 2.6 m/s in this scale."
              />
              <Slider
                label="Eye height"
                value={settings.eyeHeight}
                onValueChange={(eyeHeight) => update({ eyeHeight })}
                min={SETTING_BOUNDS.eyeHeight.min}
                max={SETTING_BOUNDS.eyeHeight.max}
                step={SETTING_BOUNDS.eyeHeight.step}
                unit="m"
              />
            </div>

            <div className="space-y-4 p-4">
              <Switch
                label="Collision"
                checked={settings.collisionEnabled}
                onCheckedChange={(collisionEnabled) => update({ collisionEnabled })}
                hint="Stops the camera passing through walls. Turning it off enables free flight."
              />
              <Switch
                label="Jumping"
                checked={settings.jumpEnabled}
                onCheckedChange={(jumpEnabled) => update({ jumpEnabled })}
                disabled={!settings.collisionEnabled}
                hint="Off by default — an architectural walkthrough is not a platformer."
              />
              <Switch
                label="Shadows"
                checked={settings.shadows}
                onCheckedChange={(shadows) => update({ shadows })}
                hint="The most expensive setting. Turn it off if the frame rate drops."
              />
              <Switch
                label="Ground grid"
                checked={settings.showGrid}
                onCheckedChange={(showGrid) => update({ showGrid })}
              />
              <Switch
                label="Minimap"
                checked={settings.showMinimap}
                onCheckedChange={(showMinimap) => update({ showMinimap })}
                hint="Shown when the model carries room metadata."
              />
            </div>

            <div className="flex justify-end p-4">
              <Button variant="ghost" size="sm" onClick={reset}>
                Reset viewer defaults
              </Button>
            </div>
          </Card>
        </Section>

        {/* ---- Connection ----------------------------------------------- */}
        <Section
          title="Connection"
          description="Where the app looks for the ArchX3D pipeline."
        >
          <Card elevation="flat" className="p-4">
            <dl className="space-y-3 text-sm">
              <div className="flex items-baseline justify-between gap-4">
                <dt className="text-tertiary">API base URL</dt>
                <dd className="truncate font-mono text-xs text-primary">
                  {API_BASE_URL}
                </dd>
              </div>
              <Divider />
              <div className="flex items-baseline justify-between gap-4">
                <dt className="text-tertiary">Authentication</dt>
                <dd className="text-xs text-secondary">None</dd>
              </div>
            </dl>

            <p className="mt-4 text-xs leading-relaxed text-tertiary">
              The address is set at build time by{" "}
              <code className="rounded-xs bg-sunken px-1 py-0.5 font-mono">
                NEXT_PUBLIC_API_BASE_URL
              </code>
              . The ArchX3D backend has no authentication, so this app never asks
              you to sign in — anyone who can reach the API can use it.
            </p>
          </Card>
        </Section>

        {/* ---- Local index ---------------------------------------------- */}
        <Section
          title="Project index"
          description="How this browser knows which projects exist."
        >
          <Card elevation="flat">
            <div className="flex items-start gap-3 p-4">
              <span className="flex size-8 shrink-0 items-center justify-center rounded-md border border-line-subtle bg-sunken text-tertiary [&_svg]:size-4">
                <DatabaseIcon />
              </span>
              <div className="min-w-0 flex-1">
                <p className="text-sm text-primary">
                  {pluralise(projects.length, "project")} indexed
                </p>
                <p className="mt-0.5 text-xs text-tertiary">
                  {formatBytes(indexBytes)} of plans and images uploaded.
                </p>
              </div>
              <Button
                variant="secondary"
                size="sm"
                icon={<TrashIcon />}
                onClick={() => setConfirmClear(true)}
                disabled={projects.length === 0}
              >
                Clear index
              </Button>
            </div>

            <div className="border-t border-line-subtle p-4">
              <Alert tone="info" title="Why the list lives in your browser">
                The ArchX3D API can create a project and return one by ID, but it has
                no endpoint that lists them. Rather than invent data, this app records
                the IDs it created locally and re-reads each one from the server.
                <br />
                <br />
                Clearing the index hides projects here but does not delete anything —
                the files stay on the server, and a direct project link still works.
              </Alert>
            </div>
          </Card>
        </Section>
      </div>

      <ConfirmDialog
        open={confirmClear}
        onOpenChange={setConfirmClear}
        title="Clear the project index?"
        description="This browser will forget every project. Nothing is deleted from the server, and direct links continue to work — but you will not be able to browse them here."
        confirmLabel="Clear index"
        onConfirm={() => {
          forgetAll();
          setConfirmClear(false);
          toast({
            tone: "info",
            title: "Index cleared",
            description: "Project files remain on the server.",
          });
        }}
      />
    </AppShell>
  );
}
