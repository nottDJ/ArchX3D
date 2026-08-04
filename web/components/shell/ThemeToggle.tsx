"use client";

/**
 * ArchX3D — theme toggle
 * ======================
 * Light, dark, system.
 *
 * Three options, not two
 * ----------------------
 * A two-state toggle forces a user to pick a side and then re-pick it every
 * time their environment changes. "System" is the honest default: the OS
 * already knows whether it is night, and a professional tool should not
 * override that until asked.
 *
 * A menu rather than a cycling button, because a button that cycles through
 * three states cannot show what the *next* press will do, so the only way to
 * find out is to press it — twice, if you overshoot.
 */

import { Button, Menu, MenuItem, Tooltip } from "@/components/ui";
import { CheckIcon, MonitorIcon, MoonIcon, SunIcon } from "@/components/ui/icons";
import { useTheme } from "@/hooks/useTheme";
import type { Theme } from "@/lib/theme";

const OPTIONS: ReadonlyArray<{
  value: Theme;
  label: string;
  icon: React.ReactNode;
}> = [
  { value: "light", label: "Light", icon: <SunIcon /> },
  { value: "dark", label: "Dark", icon: <MoonIcon /> },
  { value: "system", label: "System", icon: <MonitorIcon /> },
];

export function ThemeToggle() {
  const { theme, resolved, setTheme, mounted } = useTheme();

  return (
    <Menu
      trigger={
        <Button variant="ghost" size="sm" iconOnly aria-label="Change theme">
          {/*
            Until mounted, the stored preference is unknown and rendering the
            wrong icon would flip on hydration. The moon is the SSR default,
            matching the pre-paint script's fallback.
          */}
          {!mounted || resolved === "dark" ? <MoonIcon /> : <SunIcon />}
        </Button>
      }
    >
      {OPTIONS.map((option) => (
        <MenuItem
          key={option.value}
          icon={option.icon}
          onSelect={() => setTheme(option.value)}
        >
          <span className="flex items-center justify-between gap-4">
            {option.label}
            {mounted && theme === option.value && (
              <CheckIcon className="size-3.5 text-accent-text" />
            )}
          </span>
        </MenuItem>
      ))}
    </Menu>
  );
}

/**
 * A compact variant for surfaces with no room for a menu — the viewer's
 * settings panel, where the control sits in a list of other settings.
 */
export function ThemeSegmented() {
  const { theme, setTheme, mounted } = useTheme();

  return (
    <div
      role="radiogroup"
      aria-label="Theme"
      className="inline-flex items-center gap-0.5 rounded-md bg-sunken p-0.5"
    >
      {OPTIONS.map((option) => {
        const active = mounted && theme === option.value;
        return (
          <Tooltip key={option.value} content={option.label}>
            <button
              type="button"
              role="radio"
              aria-checked={active}
              aria-label={option.label}
              onClick={() => setTheme(option.value)}
              className={[
                "inline-flex size-7 items-center justify-center rounded-sm transition-colors",
                "[&_svg]:size-3.5",
                active
                  ? "bg-surface text-primary shadow-xs"
                  : "text-tertiary hover:text-primary",
              ].join(" ")}
            >
              {option.icon}
            </button>
          </Tooltip>
        );
      })}
    </div>
  );
}
