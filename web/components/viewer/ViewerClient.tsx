"use client";

/**
 * ArchX3D — Viewer entry point
 * ============================
 * Loads the viewer lazily, on the client only.
 *
 * Why not import `Viewer` directly
 * --------------------------------
 * three.js, React Three Fiber, drei and three-mesh-bvh come to roughly 350 kB
 * of JavaScript. Imported statically they sit in the route's initial bundle, so
 * the browser parses all of it before it can paint anything — including the
 * loading indicator that is supposed to explain the wait.
 *
 * Deferring it means the shell paints immediately, the user sees that something
 * is happening, and the 3D stack streams in behind it. On a cold cache that is
 * the difference between a blank page for a second and a page that responds at
 * once.
 *
 * `ssr: false` on top of that, because none of this can run on a server: there
 * is no WebGL context, no `localStorage` for the saved camera, and no pointer
 * lock. Server-rendering a canvas produces markup that is thrown away on
 * hydration, so the work is pure waste.
 */

import dynamic from "next/dynamic";

import type { ViewerProps } from "./Viewer";
import { CubeIcon } from "./icons";

const Viewer = dynamic(
  () => import("./Viewer").then((module) => module.Viewer),
  { ssr: false, loading: () => <ViewerSkeleton /> },
);

export function ViewerClient(props: ViewerProps) {
  return <Viewer {...props} />;
}

/**
 * Shown while the 3D bundle downloads.
 *
 * Deliberately the same shape and colour as `LoadingOverlay`'s model state, so
 * the two phases — code arriving, then geometry arriving — read as one
 * continuous wait rather than two separate states with a flash between them.
 *
 * Positioned `absolute inset-0` rather than `h-screen`, which covers both
 * cases: with no positioned ancestor it fills the viewport (the full-page
 * viewer), and inside the comparison view's positioned container it fills that
 * instead. A `h-screen` skeleton would make each side of a comparison a full
 * viewport tall and then collapse when the real viewer loaded.
 */
function ViewerSkeleton() {
  return (
    <div className="absolute inset-0 flex items-center justify-center overflow-hidden bg-canvas text-primary antialiased">
      <div aria-hidden className="pattern-grid pointer-events-none absolute inset-0" />

      <div className="relative text-center">
        <span className="mx-auto mb-6 flex h-11 w-11 items-center justify-center rounded-xl border border-line bg-surface text-accent-text">
          <CubeIcon className="h-5 w-5" />
        </span>
        <p className="text-sm font-semibold text-primary">Starting the viewer</p>
        <p className="mt-1.5 font-mono text-xs text-tertiary">Loading 3D engine…</p>

        <div className="relative mt-6 h-1 w-56 overflow-hidden rounded-full bg-surface-hover">
          <div className="h-full w-1/3 rounded-full bg-gradient-to-r from-transparent via-accent-solid to-transparent">
            <span className="archx-sheen block h-full w-full rounded-full" />
          </div>
        </div>
      </div>
    </div>
  );
}
