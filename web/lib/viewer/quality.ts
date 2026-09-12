/**
 * ArchX3D — Adaptive render quality
 * =================================
 * Decides what to spend GPU time on, from what the model actually contains.
 *
 * Why draw calls and not triangles
 * --------------------------------
 * A generated building is the opposite shape to a game asset: very few
 * triangles, very many objects. A real 124-room plan measures
 *
 *     114,072 triangles      — trivial; a laptop GPU eats millions
 *     1,075 draw calls       — the actual cost
 *     448 meshes, 0 textures
 *
 * Each object is a separate draw call with its own state change, and in a
 * browser every one of those crosses from JavaScript into WebGL. So the budget
 * that matters is *object count*, and tuning by polygon count — the usual
 * instinct — would conclude this scene is cheap and change nothing.
 *
 * Shadows double it. A shadow-casting light re-renders every object into the
 * depth map before the visible pass, so a 1,075-object scene costs ~2,150 draw
 * calls per frame plus a 4-megapixel depth render. Turning shadows off on a
 * heavy scene is the single largest win available without changing what the
 * viewer can show.
 *
 * Why not merge the geometry
 * --------------------------
 * Merging by material would collapse 1,075 draw calls to ~26 and is by far the
 * biggest theoretical win — and it is deliberately not done. The viewer's roof
 * toggle, view modes and room navigation all work by hiding individual objects,
 * which a merged mesh cannot do. Losing the product's defining features to gain
 * frame rate is the wrong trade; reducing per-frame *passes* is the right one.
 */

/**
 * Object count above which a scene is treated as heavy.
 *
 * A furnished single room is 20-60 meshes and runs fine at full quality. The
 * 124-room plan that prompted this is 448. 200 sits between them, nearer the
 * heavy end because the cost of over-reducing (slightly softer image) is much
 * smaller than the cost of under-reducing (an unusable walkthrough).
 */
export const HEAVY_MESH_COUNT = 200;

/** Above this, even a reduced shadow map is not worth its cost. */
export const VERY_HEAVY_MESH_COUNT = 600;

export interface QualityPlan {
  /** Whether to render shadows at all. */
  readonly shadows: boolean;
  /** `[min, max]` device pixel ratio for the canvas. */
  readonly dpr: readonly [number, number];
  /** Square shadow map resolution, when shadows are on. */
  readonly shadowMapSize: number;
  /**
   * Set when quality was reduced below what the user asked for, phrased for
   * display. `null` when the user is getting exactly what they configured —
   * silently degrading quality and saying nothing is how a viewer acquires a
   * reputation for looking worse than it does.
   */
  readonly reducedReason: string | null;
}

export interface QualityInput {
  /** Meshes in the loaded model. `null` before it has been indexed. */
  readonly meshes: number | null;
  /** The user's shadow preference. */
  readonly shadows: boolean;
  /** Whether to reduce automatically at all. */
  readonly autoQuality: boolean;
}

/**
 * Full quality, used before a model has been measured and whenever the user
 * has turned automatic reduction off.
 */
function fullQuality(shadows: boolean): QualityPlan {
  return {
    shadows,
    // 2x on a HiDPI display means four times the pixels. Worth it on a small
    // scene, ruinous on a large one.
    dpr: [1, 2],
    shadowMapSize: 2048,
    reducedReason: null,
  };
}

/**
 * Choose render settings for a model of this size.
 *
 * Pure, so the thresholds can be tested without a GPU.
 */
export function planQuality(input: QualityInput): QualityPlan {
  const { meshes, shadows, autoQuality } = input;

  if (!autoQuality || meshes === null || meshes < HEAVY_MESH_COUNT) {
    return fullQuality(shadows);
  }

  if (meshes >= VERY_HEAVY_MESH_COUNT) {
    return {
      shadows: false,
      dpr: [1, 1],
      shadowMapSize: 1024,
      reducedReason: shadows
        ? `Large model (${meshes.toLocaleString()} objects) — shadows off and rendering at 1× for a usable frame rate.`
        : `Large model (${meshes.toLocaleString()} objects) — rendering at 1× for a usable frame rate.`,
    };
  }

  return {
    shadows: false,
    // 1.5 keeps text and edges crisp while costing ~44% of the pixels of 2x.
    dpr: [1, 1.5],
    shadowMapSize: 1024,
    reducedReason: shadows
      ? `Large model (${meshes.toLocaleString()} objects) — shadows turned off for performance.`
      : null,
  };
}
