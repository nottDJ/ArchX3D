/**
 * Mock SSE backend for local frontend development.
 * =================================================
 * Stands in for `GET /api/jobs/{job_id}/stream` until the Celery-backed
 * FastAPI endpoint is available, so the dashboard can be exercised end to end.
 *
 *   node scripts/mock-sse-server.mjs            # happy path
 *   node scripts/mock-sse-server.mjs --fail     # fail midway through
 *
 * Then open http://localhost:3000/generate/demo-job-123
 */

import { createServer } from "node:http";

const PORT = Number(process.env.PORT ?? 8000);
const SHOULD_FAIL = process.argv.includes("--fail");

/** [status, message, delay-before-emitting-ms] */
const SCRIPT = [
  ["QUEUED", "Job accepted — waiting for a pipeline worker...", 300],
  ["QUEUED", "Worker acquired (celery@render-01).", 900],
  ["EXTRACTING_DXF", "Parsing DXF layers and geometry...", 700],
  ["EXTRACTING_DXF", "Found 4 layers: WALLS, DOORS, WINDOWS, TEXT.", 900],
  ["EXTRACTING_DXF", "Extracted 312 line segments from layer WALLS.", 800],
  ["GENERATING_GEOMETRY", "Normalising coordinates to metres...", 900],
  ["GENERATING_GEOMETRY", "Solved 47 wall segments, 9 openings.", 1100],
  ["BUILDING_SCENE", "Launching headless Blender 5.0...", 1000],
  ["BUILDING_SCENE", "Extruding walls to 2.7m...", 1200],
  ["BUILDING_SCENE", "Applying materials and HDRI lighting...", 1300],
  ["EXPORTING_GLB", "Packing optimised glTF binary...", 1100],
  ["EXPORTING_GLB", "Wrote model.glb (14.0 MB).", 900],
  ["COMPLETED", "Generation complete.", 700],
];

const FAIL_AT = 8;

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

const server = createServer(async (req, res) => {
  const url = new URL(req.url ?? "/", `http://localhost:${PORT}`);

  // Permissive CORS — this is a throwaway dev server.
  res.setHeader("Access-Control-Allow-Origin", "*");

  if (req.method === "OPTIONS") {
    res.writeHead(204).end();
    return;
  }

  const match = url.pathname.match(/^\/api\/jobs\/([^/]+)\/stream$/);
  if (!match) {
    res.writeHead(404, { "Content-Type": "application/json" });
    res.end(JSON.stringify({ detail: "Not found" }));
    return;
  }

  console.log(`[mock] stream opened for job "${decodeURIComponent(match[1])}"`);

  res.writeHead(200, {
    "Content-Type": "text/event-stream",
    "Cache-Control": "no-cache, no-transform",
    Connection: "keep-alive",
    // Disable proxy buffering so events arrive as they are written.
    "X-Accel-Buffering": "no",
  });

  const send = (payload) => res.write(`data: ${JSON.stringify(payload)}\n\n`);

  let closed = false;
  req.on("close", () => {
    closed = true;
    console.log("[mock] client disconnected");
  });

  for (const [index, [status, message, delay]] of SCRIPT.entries()) {
    if (closed) return;
    await sleep(delay);
    if (closed) return;

    if (SHOULD_FAIL && index === FAIL_AT) {
      send({
        status: "FAILED",
        message: "Blender exited with code 1: boolean solver failed on wall #23.",
      });
      res.end();
      return;
    }

    send({ status, message });
  }

  res.end();
});

server.listen(PORT, () => {
  console.log(
    `[mock] ArchX3D SSE mock listening on http://localhost:${PORT}` +
      (SHOULD_FAIL ? " (failure scenario)" : ""),
  );
});
