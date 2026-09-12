import path from "node:path";
import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  reactStrictMode: true,
  // The repo root also contains Python tooling; pin tracing to this app so
  // Next.js does not walk up and pick an unrelated lockfile as the root.
  outputFileTracingRoot: path.join(__dirname),
  // Static HTML/JS/CSS, no Node server at runtime. Tauri's asset protocol
  // serves this directly from disk — there is no per-request server to run
  // route handlers or resolve dynamic segments against, which is also why
  // `/generate/[job_id]` and `/viewer`'s server-side `searchParams` read were
  // converted to client-side `useSearchParams` (see those components).
  output: "export",
  // Tauri's asset protocol resolves `/dashboard` against `dashboard/index.html`
  // reliably; without a trailing slash a nested static route can 404 on a
  // direct/deep load even though client-side navigation to it works fine.
  trailingSlash: true,
  // Defensive: next/image is unused (see FRONTEND_ARCHITECTURE.md), but the
  // optimizer requires this under `output: "export"` and costs nothing if
  // nothing actually calls it.
  images: { unoptimized: true },
};

export default nextConfig;
