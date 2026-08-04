import { clsx, type ClassValue } from "clsx";
import { extendTailwindMerge } from "tailwind-merge";

/**
 * ArchX3D — class name composition
 * ================================
 * `clsx` for conditionals, `tailwind-merge` for conflict resolution.
 *
 * Why the merge step is not optional
 * ---------------------------------
 * Tailwind resolves conflicting utilities by the order they appear in the
 * generated stylesheet, not by the order they appear on the element. So
 * `class="p-4 p-2"` does not reliably give you `p-2` — it gives you whichever
 * of the two Tailwind emitted last, which is an implementation detail of the
 * build. A component that accepts `className` and simply appends it therefore
 * cannot be overridden predictably.
 *
 * `twMerge` fixes that by removing the losing utility from the string
 * entirely, so the last one written always wins.
 *
 * The custom groups below teach it about this product's scale names. Without
 * them it treats `text-sm` (a size) and `text-secondary` (a colour) as the
 * same group and drops one, which silently strips colour from half the UI.
 */
const twMerge = extendTailwindMerge({
  extend: {
    theme: {
      radius: ["xs", "sm", "md", "lg", "xl", "2xl"],
    },
    classGroups: {
      "font-size": [
        {
          text: [
            "2xs", "xs", "sm", "base", "md", "lg", "xl", "2xl", "3xl", "4xl",
          ],
        },
      ],
      "text-color": [
        {
          text: [
            "primary", "secondary", "tertiary", "disabled", "on-solid",
            "accent-text", "success-text", "warning-text", "danger-text",
          ],
        },
      ],
      "bg-color": [
        {
          bg: [
            "canvas", "subtle", "surface", "surface-hover", "surface-active",
            "raised", "sunken", "overlay",
            "accent-surface", "accent-solid", "accent-solid-hover",
            "success-surface", "success-solid",
            "warning-surface", "warning-solid",
            "danger-surface", "danger-solid",
          ],
        },
      ],
      "border-color": [
        {
          border: [
            "line", "line-subtle", "line-strong",
            "accent-border", "success-border", "warning-border", "danger-border",
          ],
        },
      ],
      shadow: [{ shadow: ["xs", "sm", "md", "lg", "xl", "none"] }],
    },
  },
});

/**
 * Compose class names.
 *
 * **House rule:** a component owns its *appearance*; the caller owns its
 * *position*. Pass layout utilities — `w-full`, `mt-4`, `col-span-2` — through
 * `className`. Do not pass colours or typography; if a component needs to look
 * different, that is a variant, and a variant is reviewable in a way an
 * ad-hoc override is not.
 */
export function cn(...inputs: ClassValue[]): string {
  return twMerge(clsx(inputs));
}
