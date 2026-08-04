import path from "node:path";
import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  reactStrictMode: true,
  // The repo root also contains Python tooling; pin tracing to this app so
  // Next.js does not walk up and pick an unrelated lockfile as the root.
  outputFileTracingRoot: path.join(__dirname),
};

export default nextConfig;
