/**
 * ArchX3D — wizard icons (compatibility shim)
 * ===========================================
 * Re-exports from the unified set in `@/components/ui/icons`.
 *
 * This file used to define 22 icons of its own, six of which were also defined
 * in `generate/icons.tsx` or `viewer/icons.tsx` at different stroke weights and
 * radii — so the same idea was drawn two ways depending on which screen you
 * were on.
 *
 * Kept as a shim rather than deleted so the ~40 existing import sites keep
 * working; there is now exactly one definition behind them. New code should
 * import from `@/components/ui/icons` directly.
 */

export {
  AlignLeftIcon,
  BulbIcon,
  CheckIcon,
  CopyIcon,
  CubeIcon,
  DistributeIcon,
  GridIcon,
  ImageIcon,
  InfoIcon,
  LockIcon,
  PaletteIcon,
  PlanIcon,
  RedoIcon,
  SpinnerIcon,
  TrashIcon,
  UndoIcon,
  UnlockIcon,
  UploadIcon,
  WallIcon,
  WarningIcon,
  // Renamed in the unified set for precision — `Chevron` alone did not say
  // which way it pointed, and `Reset` was indistinguishable from `Retry`.
  ChevronDownIcon as ChevronIcon,
  RetryIcon as ResetIcon,
} from "@/components/ui/icons";
