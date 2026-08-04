/**
 * ArchX3D — display formatting
 * ============================
 * One place for every number and date the interface shows.
 *
 * Formatting was scattered before: `formatBytes` in `lib/wizard.ts`, a second
 * one in the viewer's loading overlay with different rounding, and inline
 * `toFixed` calls in half a dozen components. So the same file appeared as
 * "1.4 MB" in one panel and "1.44 MB" in another, which reads as a bug in the
 * data rather than in the display.
 */

/**
 * Bytes at human scale.
 *
 * Decimal units (1000), not binary (1024): file managers on macOS and Windows
 * both report decimal, so a 14.0 MB GLB here matching 14.0 MB in Finder is
 * what a user expects. One decimal place above a megabyte, none below —
 * "847 KB" is precise enough and "846.9 KB" is noise.
 */
export function formatBytes(bytes: number | null | undefined): string {
  if (bytes === null || bytes === undefined || !Number.isFinite(bytes)) return "—";
  if (bytes < 1000) return `${Math.round(bytes)} B`;
  if (bytes < 1_000_000) return `${Math.round(bytes / 1000)} KB`;
  if (bytes < 1_000_000_000) return `${(bytes / 1_000_000).toFixed(1)} MB`;
  return `${(bytes / 1_000_000_000).toFixed(2)} GB`;
}

/** Large counts, thinned: 1,204 → "1,204"; 18,400 → "18.4k". */
export function formatCount(value: number): string {
  if (!Number.isFinite(value)) return "—";
  if (Math.abs(value) < 10_000) return value.toLocaleString();
  if (Math.abs(value) < 1_000_000) return `${(value / 1000).toFixed(1)}k`;
  return `${(value / 1_000_000).toFixed(1)}M`;
}

/**
 * Relative time, in the units people actually use.
 *
 * Stops at "yesterday" and switches to a date. "8 days ago" is harder to place
 * than "12 Mar" — beyond about a week, relative time stops being a shortcut
 * and starts being arithmetic.
 */
export function formatRelative(iso: string | null | undefined): string {
  if (!iso) return "—";
  const then = new Date(iso).getTime();
  if (!Number.isFinite(then)) return "—";

  const seconds = Math.round((Date.now() - then) / 1000);

  if (seconds < 45) return "just now";
  if (seconds < 90) return "a minute ago";
  if (seconds < 3600) return `${Math.round(seconds / 60)} min ago`;
  if (seconds < 7200) return "an hour ago";
  if (seconds < 86_400) return `${Math.round(seconds / 3600)} hours ago`;
  if (seconds < 172_800) return "yesterday";
  if (seconds < 604_800) return `${Math.round(seconds / 86_400)} days ago`;

  return formatDate(iso);
}

/** `12 Mar 2026`, or `12 Mar` inside the current year. */
export function formatDate(iso: string | null | undefined): string {
  if (!iso) return "—";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "—";

  const sameYear = date.getFullYear() === new Date().getFullYear();
  return date.toLocaleDateString(undefined, {
    day: "numeric",
    month: "short",
    ...(sameYear ? {} : { year: "numeric" }),
  });
}

/** Full timestamp for a `title` attribute, where precision is free. */
export function formatExact(iso: string | null | undefined): string {
  if (!iso) return "";
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? "" : date.toLocaleString();
}

/**
 * `mm:ss`, or `h:mm:ss` past an hour.
 *
 * Used for elapsed job time, where a monotonically rising number is the whole
 * point — so it never rounds to "about a minute".
 */
export function formatDuration(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds < 0) return "0:00";

  const whole = Math.floor(seconds);
  const hours = Math.floor(whole / 3600);
  const minutes = Math.floor((whole % 3600) / 60);
  const secs = whole % 60;

  if (hours > 0) {
    return `${hours}:${String(minutes).padStart(2, "0")}:${String(secs).padStart(2, "0")}`;
  }
  return `${minutes}:${String(secs).padStart(2, "0")}`;
}

/** `0.62` → `62%`. */
export function formatPercent(fraction: number | null | undefined): string {
  if (fraction === null || fraction === undefined || !Number.isFinite(fraction)) {
    return "—";
  }
  return `${Math.round(fraction * 100)}%`;
}

/** `living_room` → `Living room`. */
export function humanise(value: string | null | undefined): string {
  if (!value) return "—";
  const spaced = value.replace(/[_-]+/g, " ").trim();
  return spaced.charAt(0).toUpperCase() + spaced.slice(1);
}

/**
 * Shorten an id for display, keeping enough to be distinguishable.
 *
 * Eight characters of hex is ~4 billion combinations — plenty to tell a
 * handful of local projects apart, and short enough to sit in a table cell.
 */
export function shortId(id: string, length = 8): string {
  return id.length <= length ? id : id.slice(0, length);
}

/** `1 image` / `2 images`, without the caller writing the conditional. */
export function pluralise(count: number, singular: string, plural?: string): string {
  return `${count} ${count === 1 ? singular : (plural ?? `${singular}s`)}`;
}
