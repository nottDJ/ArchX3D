/**
 * ArchX3D — component library
 * ===========================
 * The public surface. Import from `@/components/ui`, never from a file inside
 * it — the grouping into files is an implementation detail and will change;
 * the names will not.
 *
 * See `docs/COMPONENT_LIBRARY.md` for props, variants and usage rules.
 */

export { cn } from "./cn";

export { Button, ButtonGroup } from "./Button";
export type { ButtonProps, ButtonSize, ButtonVariant } from "./Button";

export { Card, CardBody, CardFooter, CardHeader, Divider, Section, Stat } from "./Card";
export type { CardElevation, CardProps } from "./Card";

export { Badge, Kbd, KbdChord, StatusBadge, StatusDot, statusFromStage } from "./Badge";
export type { BadgeProps, BadgeTone, StatusKind } from "./Badge";

export { Field, Input, Select, Slider, Switch, Textarea } from "./Field";
export type { ControlSize, FieldProps, InputProps } from "./Field";

export {
  ConfirmDialog,
  Dialog,
  Menu,
  MenuItem,
  MenuLabel,
  MenuSeparator,
  Popover,
  Tooltip,
  TooltipProvider,
} from "./Overlay";
export type { DialogProps } from "./Overlay";

export {
  Alert,
  EmptyState,
  Progress,
  Skeleton,
  SkeletonCard,
  SkeletonText,
  Spinner,
  ToastProvider,
  useToast,
} from "./Feedback";
export type { AlertTone, ToastMessage } from "./Feedback";

export {
  Breadcrumbs,
  Segmented,
  Tab,
  TabList,
  TabPanel,
  Table,
  Tabs,
  TBody,
  TD,
  TH,
  THead,
  TR,
} from "./Navigation";
export type { Crumb, SegmentOption, SortDirection } from "./Navigation";

export * as Icons from "./icons";
