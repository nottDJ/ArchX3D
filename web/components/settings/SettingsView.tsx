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
 * No account, no billing, no team. The ArchX3D backend has no authentication
 * and no notion of a user, so every one of those would be a form that does
 * nothing. Building them would be building a facade; the "Connection" section
 * says plainly what the app talks to instead.
 *
 * The Gemini API key is the one exception, and it is not a facade: it is
 * stored by the server and read by the pipeline. It lives here because the
 * desktop build has no shell to export an environment variable from, so
 * without it the AI features would be unreachable to anyone who installed the
 * app rather than cloning the repository.
 */

import { useEffect, useState } from "react";

import { AppShell } from "@/components/shell/AppShell";
import { ThemeSegmented } from "@/components/shell/ThemeToggle";
import {
  Alert,
  Badge,
  Button,
  Card,
  Field,
  Input,
  ConfirmDialog,
  Divider,
  Section,
  Slider,
  Switch,
  useToast,
} from "@/components/ui";
import { DatabaseIcon, SparkIcon, TrashIcon } from "@/components/ui/icons";
import { useProjects } from "@/hooks/useProjects";
import { useViewerSettings } from "@/hooks/useViewerSettings";
import {
  API_BASE_URL,
  clearApiKey,
  fetchApiKeyStatus,
  saveApiKey,
  type ApiKeyStatus,
} from "@/lib/api";
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
                label="Adapt quality to model size"
                checked={settings.autoQuality}
                onCheckedChange={(autoQuality) => update({ autoQuality })}
                hint="Large plans have hundreds of separate objects, and drawing them twice — once for shadows — is what makes a walkthrough stutter. This turns shadows off and lowers the render resolution on those models, and says so when it does."
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

        {/* ---- AI ------------------------------------------------------- */}
        <Section
          title="AI analysis"
          description="Reference photographs are read by Google Gemini to recover furniture, materials and lighting."
        >
          <ApiKeyCard />
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

/* -------------------------------------------------------------------------- */
/* Gemini API key                                                             */
/* -------------------------------------------------------------------------- */

/**
 * Enter, replace or remove the Gemini API key.
 *
 * The key is write-only from here: the server returns whether one is
 * configured and a masked hint, never the value. So the field is always blank
 * on load — showing dots that are not the real key would invite a user to
 * "correct" them, and showing the real one puts a credential on screen for no
 * reason.
 *
 * Without a key the pipeline still runs: DXF geometry, rooms, walls, doors and
 * the 3D model are all deterministic. Only the furniture and finishes read from
 * photographs need it, which is what the empty state says rather than implying
 * the app is broken.
 */
function ApiKeyCard() {
  const { toast } = useToast();
  const [status, setStatus] = useState<ApiKeyStatus | null>(null);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [unreachable, setUnreachable] = useState(false);

  useEffect(() => {
    let cancelled = false;
    fetchApiKeyStatus()
      .then((next) => {
        if (!cancelled) setStatus(next);
      })
      .catch(() => {
        if (!cancelled) setUnreachable(true);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const run = async (action: () => Promise<ApiKeyStatus>, message: string) => {
    setBusy(true);
    setError(null);
    try {
      setStatus(await action());
      setDraft("");
      toast({ tone: "success", title: message });
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Something went wrong");
    } finally {
      setBusy(false);
    }
  };

  if (unreachable) {
    return (
      <Card elevation="flat" className="p-4">
        <Alert tone="warning" title="Backend not reachable">
          The API key is stored by the ArchX3D server, which is not responding.
          Start it and reload this page.
        </Alert>
      </Card>
    );
  }

  const configured = status?.configured ?? false;
  const fromEnvironment = status?.source === "environment";

  return (
    <Card elevation="flat">
      <div className="flex items-start gap-3 p-4">
        <span className="flex size-8 shrink-0 items-center justify-center rounded-md border border-line-subtle bg-sunken text-tertiary [&_svg]:size-4">
          <SparkIcon />
        </span>
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <p className="text-sm text-primary">Gemini API key</p>
            {status && (
              <Badge tone={configured ? "success" : "neutral"}>
                {configured ? "Configured" : "Not set"}
              </Badge>
            )}
          </div>
          <p className="mt-0.5 text-xs text-tertiary">
            {configured
              ? `Using ${status?.hint} from ${
                  fromEnvironment ? "the GEMINI_API_KEY environment variable" : "this machine"
                }.`
              : "Without a key, plans still build — walls, rooms and the 3D model are read from the DXF. Only furniture and finishes from photographs are skipped."}
          </p>
        </div>
      </div>

      {fromEnvironment ? (
        <div className="border-t border-line-subtle p-4">
          <Alert tone="info" title="Set by the environment">
            <code className="rounded-xs bg-sunken px-1 py-0.5 font-mono">
              GEMINI_API_KEY
            </code>{" "}
            is set for this process and takes precedence over anything saved
            here. Unset it and restart to manage the key from this page.
          </Alert>
        </div>
      ) : (
        <div className="border-t border-line-subtle p-4">
          <form
            onSubmit={(event) => {
              event.preventDefault();
              if (draft.trim()) void run(() => saveApiKey(draft), "API key saved");
            }}
          >
            <Field
              label={configured ? "Replace the key" : "API key"}
              error={error}
              hint={
                error
                  ? undefined
                  : "Stored on this machine in plain text, readable by your Windows account. Get one from aistudio.google.com."
              }
            >
              <Input
                type="password"
                value={draft}
                onChange={(event) => setDraft(event.target.value)}
                placeholder={configured ? "Enter a new key to replace it" : "Paste your key"}
                autoComplete="off"
                spellCheck={false}
                disabled={busy}
              />
            </Field>

            <div className="mt-3 flex flex-wrap gap-2">
              <Button
                type="submit"
                variant="primary"
                size="sm"
                loading={busy}
                disabled={!draft.trim()}
              >
                {configured ? "Replace key" : "Save key"}
              </Button>
              {configured && (
                <Button
                  type="button"
                  variant="secondary"
                  size="sm"
                  icon={<TrashIcon />}
                  disabled={busy}
                  onClick={() => void run(clearApiKey, "API key removed")}
                >
                  Remove
                </Button>
              )}
            </div>
          </form>
        </div>
      )}
    </Card>
  );
}
