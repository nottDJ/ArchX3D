/**
 * ArchX3D — Punctual light units
 * ==============================
 * Converts the model's own lights from the units the file carries into the
 * units the viewer's lighting rig is calibrated in.
 *
 * The mismatch
 * ------------
 * glTF's `KHR_lights_punctual` defines point and spot intensity in candela, and
 * Blender's exporter honours that: a luminaire's watts become
 * `W × 683 / 4π` cd, so an 18 W table lamp arrives as 978 cd and a 35 W floor
 * lamp as 1902 cd. three.js reads those numbers physically.
 *
 * Nothing else in the scene is physical. The viewer's sun is 1.15, its ambient
 * fill a fraction of one, its exposure about one (see `Lighting.tsx`). Against
 * that scale a lamp two metres from a wall lit it roughly two hundred times
 * harder than daylight did, and ACES clipped every interior to flat white. Walk
 * mode, which is always indoors, showed nothing at all.
 *
 * The scale
 * ---------
 * Dividing by `683 / 4π` undoes the exporter's conversion, so a lamp's intensity
 * in the viewer is its power in watts. That is not a physical claim; it is the
 * value measured to fit the rest of the rig. On four generated models in the
 * desktop app (single-storey, two-storey, a duplex, a 253-room clinic), with the
 * camera held still and only these lights rescaled:
 *
 *     scale        white-clipped share of the walk view
 *     as filed     37–93 %
 *     ÷ 683/4π     within 1 point of the same view with every lamp off
 *     ÷ 683        likewise, but the lamps add under 3 grey levels — unlit
 *
 * So this is the brightest scale at which the lamps still add no clipping of
 * their own.
 *
 * Directional lights are left alone: glTF gives them in lux, the generator
 * exports none, and the viewer supplies its own sun.
 */

import type * as THREE from "three";

/** glTF candela per unit of viewer light intensity: the exporter's W→cd factor. */
export const CANDELA_PER_VIEWER_UNIT = 683 / (4 * Math.PI);

/**
 * Each light's intensity as the file gave it.
 *
 * Kept aside rather than recomputed so converting is idempotent: the same scene
 * can pass through here twice without its lamps dimming again. A `WeakMap`
 * rather than `userData`, which glTF populates from `extras` and the classifier
 * reads.
 */
const fileIntensity = new WeakMap<THREE.Object3D, number>();

/**
 * Rescale every point and spot light under `root` to viewer units.
 *
 * Returns how many lights were converted.
 */
export function toViewerLightUnits(root: THREE.Object3D): number {
  let converted = 0;
  root.traverse((object) => {
    const light = object as THREE.PointLight | THREE.SpotLight;
    if (!(light as THREE.PointLight).isPointLight && !(light as THREE.SpotLight).isSpotLight) return;

    let candela = fileIntensity.get(light);
    if (candela === undefined) {
      candela = light.intensity;
      fileIntensity.set(light, candela);
    }
    light.intensity = candela / CANDELA_PER_VIEWER_UNIT;
    converted += 1;
  });
  return converted;
}
