/**
 * ArchX3D — Environment maps, shipped with the app
 * ================================================
 * Which HDRI each environment preset uses, and where it is served from.
 *
 * Why not drei's `preset`
 * -----------------------
 * `<Environment preset="studio">` downloads its HDRI from a CDN
 * (raw.githack.com) the first time a model is opened. ArchX3D is a desktop app
 * that must work with no network at all, and on such a machine that download
 * fails — and the failure did not stay in the lighting. Measured in the desktop
 * app with the CDN refused: the viewer route threw "Could not load
 * studio_small_03_1k.hdr: Failed to fetch" and showed "Something went wrong"
 * instead of the building. With the request left hanging, as a firewall does,
 * the scene stayed empty behind the walk prompt for as long as it hung.
 *
 * So the files live in `public/hdri` and are served from the app's own origin.
 * They are the same files drei's presets point at (drei-assets commit
 * 456060a), all from Poly Haven under CC0.
 */

import type { EnvironmentPreset } from "../../types/viewer";

/** The HDRI behind each preset: drei's own choice, so the look is unchanged. */
export const ENVIRONMENT_FILES: Readonly<Record<EnvironmentPreset, string>> = {
  apartment: "lebombo_1k.hdr",
  city: "potsdamer_platz_1k.hdr",
  dawn: "kiara_1_dawn_1k.hdr",
  forest: "forest_slope_1k.hdr",
  lobby: "st_fagans_interior_1k.hdr",
  night: "dikhololo_night_1k.hdr",
  park: "rooitou_park_1k.hdr",
  studio: "studio_small_03_1k.hdr",
  sunset: "venice_sunset_1k.hdr",
  warehouse: "empty_warehouse_01_1k.hdr",
};

/** Directory under `public/` the files are served from. */
export const ENVIRONMENT_DIRECTORY = "hdri";

/** Same-origin URL of a preset's HDRI; an unknown preset gets the studio. */
export function environmentUrl(preset: EnvironmentPreset): string {
  const file = ENVIRONMENT_FILES[preset] ?? ENVIRONMENT_FILES.studio;
  return `/${ENVIRONMENT_DIRECTORY}/${file}`;
}
