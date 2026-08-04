/**
 * ArchX3D — icon set
 * ==================
 * One set, one geometry, one stroke weight.
 *
 * Why one file
 * ------------
 * There were three: `wizard/icons.tsx`, `generate/icons.tsx` and
 * `viewer/icons.tsx`, 530 lines between them, with six icons defined twice —
 * `CheckIcon`, `CopyIcon`, `CubeIcon`, `RetryIcon`, `SpinnerIcon`,
 * `WarningIcon` — at different weights and radii. A user moving from the
 * wizard to the viewer met the same idea drawn two ways, which is the kind of
 * inconsistency nobody reports and everybody feels.
 *
 * The grid
 * --------
 * 24 units, 1.5 stroke, round caps and joins, 2-unit minimum padding from the
 * edge. Optical weight is matched by eye rather than by rule: a circle at the
 * same stroke as a square reads lighter, so round forms sit fractionally
 * larger.
 *
 * Sizing
 * ------
 * Icons inherit `currentColor` and are sized by the caller with `h-*`/`w-*`.
 * The set is drawn for 16px (`h-4 w-4`), which is the product's default; at
 * 20px and above the same paths hold, below 14px they blur and a 12px variant
 * would need its own drawing — so nothing in the UI asks for one.
 */

export type IconProps = React.SVGProps<SVGSVGElement>;

function Icon({ children, ...props }: IconProps & { children: React.ReactNode }) {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.5}
      strokeLinecap="round"
      strokeLinejoin="round"
      // Icons are decorative by default; a meaningful icon gets a label from
      // its container (a button's `aria-label`), never from the SVG itself.
      aria-hidden="true"
      focusable="false"
      {...props}
    >
      {children}
    </svg>
  );
}

/* -- Brand ---------------------------------------------------------------- */

export function CubeIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="M12 3l8 4.5v9L12 21l-8-4.5v-9L12 3z" />
      <path d="M4 7.5l8 4.5 8-4.5M12 12v9" />
    </Icon>
  );
}

/* -- Navigation ----------------------------------------------------------- */

export function HomeIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="M4 10.5L12 4l8 6.5" />
      <path d="M6 9.8V19a1 1 0 001 1h10a1 1 0 001-1V9.8" />
      <path d="M10 20v-5h4v5" />
    </Icon>
  );
}

export function GridIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <rect x="4" y="4" width="7" height="7" rx="1.5" />
      <rect x="13" y="4" width="7" height="7" rx="1.5" />
      <rect x="4" y="13" width="7" height="7" rx="1.5" />
      <rect x="13" y="13" width="7" height="7" rx="1.5" />
    </Icon>
  );
}

export function ListIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="M9 6h11M9 12h11M9 18h11" />
      <path d="M4.5 6h.01M4.5 12h.01M4.5 18h.01" />
    </Icon>
  );
}

export function PlanIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <rect x="3" y="4" width="18" height="16" rx="1.5" />
      <path d="M3 12h9V4" />
      <path d="M12 15h9" />
    </Icon>
  );
}

export function BookIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="M4 5.5A1.5 1.5 0 015.5 4H10a2 2 0 012 2v13a1.5 1.5 0 00-1.5-1.5H5.5A1.5 1.5 0 014 16V5.5z" />
      <path d="M20 5.5A1.5 1.5 0 0018.5 4H14a2 2 0 00-2 2v13a1.5 1.5 0 011.5-1.5h5A1.5 1.5 0 0020 16V5.5z" />
    </Icon>
  );
}

export function SettingsIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <circle cx="12" cy="12" r="3" />
      <path d="M19.4 15a1.7 1.7 0 00.3 1.9l.1.1a2 2 0 11-2.8 2.8l-.1-.1a1.7 1.7 0 00-1.9-.3 1.7 1.7 0 00-1 1.5v.2a2 2 0 11-4 0v-.1a1.7 1.7 0 00-1.1-1.5 1.7 1.7 0 00-1.9.3l-.1.1a2 2 0 11-2.8-2.8l.1-.1a1.7 1.7 0 00.3-1.9 1.7 1.7 0 00-1.5-1H3a2 2 0 010-4h.1A1.7 1.7 0 004.6 9a1.7 1.7 0 00-.3-1.9l-.1-.1a2 2 0 112.8-2.8l.1.1a1.7 1.7 0 001.9.3H9a1.7 1.7 0 001-1.5V3a2 2 0 014 0v.1a1.7 1.7 0 001 1.5 1.7 1.7 0 001.9-.3l.1-.1a2 2 0 112.8 2.8l-.1.1a1.7 1.7 0 00-.3 1.9V9a1.7 1.7 0 001.5 1H21a2 2 0 010 4h-.1a1.7 1.7 0 00-1.5 1z" />
    </Icon>
  );
}

export function SearchIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <circle cx="11" cy="11" r="6.5" />
      <path d="M16 16l4 4" />
    </Icon>
  );
}

/* -- Chevrons and arrows -------------------------------------------------- */

export function ChevronRightIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="M9.5 5.5l6 6.5-6 6.5" />
    </Icon>
  );
}

export function ChevronLeftIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="M14.5 5.5l-6 6.5 6 6.5" />
    </Icon>
  );
}

export function ChevronDownIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="M5.5 9l6.5 6 6.5-6" />
    </Icon>
  );
}

export function ChevronUpDownIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="M8 9.5L12 5.5l4 4" />
      <path d="M16 14.5L12 18.5l-4-4" />
    </Icon>
  );
}

export function ArrowRightIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="M4.5 12h15" />
      <path d="M13.5 6l6 6-6 6" />
    </Icon>
  );
}

export function ArrowUpIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="M12 19.5v-15" />
      <path d="M6 10.5l6-6 6 6" />
    </Icon>
  );
}

export function ArrowDownIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="M12 4.5v15" />
      <path d="M18 13.5l-6 6-6-6" />
    </Icon>
  );
}

export function ExternalIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="M13 4.5h6.5V11" />
      <path d="M19.5 4.5L11 13" />
      <path d="M18 14.5v4a1.5 1.5 0 01-1.5 1.5h-11A1.5 1.5 0 014 18.5v-11A1.5 1.5 0 015.5 6h4" />
    </Icon>
  );
}

/* -- Status --------------------------------------------------------------- */

export function CheckIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="M4.5 12.5l5 5 10-11" />
    </Icon>
  );
}

export function CheckCircleIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <circle cx="12" cy="12" r="8.5" />
      <path d="M8.5 12.2l2.4 2.4 4.6-5" />
    </Icon>
  );
}

export function WarningIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="M12 4.2l8.6 15.1a1 1 0 01-.87 1.5H4.27a1 1 0 01-.87-1.5L12 4.2z" />
      <path d="M12 10v4" />
      <path d="M12 17.3v.1" />
    </Icon>
  );
}

export function ErrorIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <circle cx="12" cy="12" r="8.5" />
      <path d="M12 8v4.5" />
      <path d="M12 15.8v.1" />
    </Icon>
  );
}

export function InfoIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <circle cx="12" cy="12" r="8.5" />
      <path d="M12 11v5" />
      <path d="M12 8.2v.1" />
    </Icon>
  );
}

export function SpinnerIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="M12 3a9 9 0 019 9" />
      <path d="M21 12a9 9 0 01-9 9" opacity={0.3} />
      <path d="M12 21a9 9 0 01-9-9" opacity={0.3} />
      <path d="M3 12a9 9 0 019-9" opacity={0.3} />
    </Icon>
  );
}

export function CloseIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="M6.5 6.5l11 11M17.5 6.5l-11 11" />
    </Icon>
  );
}

export function RetryIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="M20 12a8 8 0 11-2.6-5.9" />
      <path d="M20 4v4.5h-4.5" />
    </Icon>
  );
}

/* -- Files and data ------------------------------------------------------- */

export function UploadIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="M12 16V4.5" />
      <path d="M8 8.5l4-4 4 4" />
      <path d="M4.5 15.5v3A1.5 1.5 0 006 20h12a1.5 1.5 0 001.5-1.5v-3" />
    </Icon>
  );
}

export function DownloadIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="M12 4.5V16" />
      <path d="M8 12l4 4 4-4" />
      <path d="M4.5 15.5v3A1.5 1.5 0 006 20h12a1.5 1.5 0 001.5-1.5v-3" />
    </Icon>
  );
}

export function ImageIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <rect x="3.5" y="5" width="17" height="14" rx="1.5" />
      <circle cx="8.5" cy="10" r="1.5" />
      <path d="M4 16.5l4.2-4a1.5 1.5 0 012 0L15 17" />
      <path d="M14 15l1.8-1.7a1.5 1.5 0 012 0L20.5 16" />
    </Icon>
  );
}

export function FileIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="M13.5 3.5H7A1.5 1.5 0 005.5 5v14A1.5 1.5 0 007 20.5h10a1.5 1.5 0 001.5-1.5V8.5l-5-5z" />
      <path d="M13.5 3.5v5h5" />
    </Icon>
  );
}

export function TrashIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="M4.5 6.5h15" />
      <path d="M9.5 6.5V5a1 1 0 011-1h3a1 1 0 011 1v1.5" />
      <path d="M6.5 6.5l.8 12a1.5 1.5 0 001.5 1.4h6.4a1.5 1.5 0 001.5-1.4l.8-12" />
      <path d="M10.5 10.5v6M13.5 10.5v6" />
    </Icon>
  );
}

export function CopyIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <rect x="9" y="9" width="11" height="11" rx="1.5" />
      <path d="M15 6.5V5.5A1.5 1.5 0 0013.5 4h-8A1.5 1.5 0 004 5.5v8A1.5 1.5 0 005.5 15h1" />
    </Icon>
  );
}

export function FolderIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="M3.5 7A1.5 1.5 0 015 5.5h3.8a1.5 1.5 0 011.2.6l1 1.4h8A1.5 1.5 0 0120.5 9v8.5A1.5 1.5 0 0119 19H5a1.5 1.5 0 01-1.5-1.5V7z" />
    </Icon>
  );
}

/* -- Viewer --------------------------------------------------------------- */

export function OrbitIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <circle cx="12" cy="12" r="3.2" />
      <ellipse cx="12" cy="12" rx="9.5" ry="4.2" transform="rotate(-24 12 12)" />
    </Icon>
  );
}

export function WalkIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <circle cx="13" cy="4.2" r="1.8" />
      <path d="M12.5 21l-1-5.5-2.5-2 1-5 3.5 1.5 2 2.5 2.5.8" />
      <path d="M10 13.5L7 17" />
      <path d="M11.5 15.5L14 21" />
    </Icon>
  );
}

export function RoofIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="M3 11L12 4l9 7" />
      <path d="M5.5 12.5V20h13v-7.5" />
    </Icon>
  );
}

export function RoofOffIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="M3 9L12 2l9 7" strokeDasharray="2.5 2.5" opacity={0.5} />
      <path d="M5.5 13.5V20h13v-6.5" />
      <path d="M4 13.5h16" />
    </Icon>
  );
}

export function FitIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="M4 9V5.5A1.5 1.5 0 015.5 4H9" />
      <path d="M15 4h3.5A1.5 1.5 0 0120 5.5V9" />
      <path d="M20 15v3.5a1.5 1.5 0 01-1.5 1.5H15" />
      <path d="M9 20H5.5A1.5 1.5 0 014 18.5V15" />
      <rect x="8.5" y="8.5" width="7" height="7" rx="1" />
    </Icon>
  );
}

export function FullscreenIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="M4 9V5.5A1.5 1.5 0 015.5 4H9" />
      <path d="M15 4h3.5A1.5 1.5 0 0120 5.5V9" />
      <path d="M20 15v3.5a1.5 1.5 0 01-1.5 1.5H15" />
      <path d="M9 20H5.5A1.5 1.5 0 014 18.5V15" />
    </Icon>
  );
}

export function FullscreenExitIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="M9 4v3.5A1.5 1.5 0 017.5 9H4" />
      <path d="M20 9h-3.5A1.5 1.5 0 0115 7.5V4" />
      <path d="M15 20v-3.5a1.5 1.5 0 011.5-1.5H20" />
      <path d="M4 15h3.5A1.5 1.5 0 019 16.5V20" />
    </Icon>
  );
}

export function CameraIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="M4 8.5h3l1.5-2h7L17 8.5h3a1 1 0 011 1v8a1 1 0 01-1 1H4a1 1 0 01-1-1v-8a1 1 0 011-1z" />
      <circle cx="12" cy="13.5" r="3.2" />
    </Icon>
  );
}

export function WireframeIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="M12 3l8 4.5v9L12 21l-8-4.5v-9L12 3z" />
      <path d="M4 7.5l8 4.5 8-4.5" />
      <path d="M12 12v9" />
    </Icon>
  );
}

export function LayersIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="M12 3l9 5-9 5-9-5 9-5z" />
      <path d="M3 13l9 5 9-5" />
      <path d="M3 17l9 4 9-4" opacity={0.45} />
    </Icon>
  );
}

export function RoomsIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <rect x="3" y="4" width="18" height="16" rx="1.5" />
      <path d="M3 12h9V4" />
      <path d="M12 15h9" />
    </Icon>
  );
}

/* -- Editing -------------------------------------------------------------- */

export function UndoIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="M4 9h10.5a5 5 0 010 10H9" />
      <path d="M7.5 5.5L4 9l3.5 3.5" />
    </Icon>
  );
}

export function RedoIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="M20 9H9.5a5 5 0 000 10H15" />
      <path d="M16.5 5.5L20 9l-3.5 3.5" />
    </Icon>
  );
}

export function LockIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <rect x="5" y="10.5" width="14" height="9.5" rx="1.5" />
      <path d="M8.2 10.5V7.8a3.8 3.8 0 017.6 0v2.7" />
    </Icon>
  );
}

export function UnlockIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <rect x="5" y="10.5" width="14" height="9.5" rx="1.5" />
      <path d="M8.2 10.5V7.8a3.8 3.8 0 017.03-2" />
    </Icon>
  );
}

export function WallIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <rect x="3.5" y="5" width="17" height="14" rx="1" />
      <path d="M3.5 9.7h17M3.5 14.3h17" />
      <path d="M9 5v4.7M15 9.7v4.6M9 14.3V19" />
    </Icon>
  );
}

export function PaletteIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="M12 3.5a8.5 8.5 0 000 17c1.1 0 1.7-.8 1.7-1.6 0-.5-.2-.9-.5-1.2-.3-.3-.5-.7-.5-1.2 0-.9.7-1.6 1.6-1.6h1.6A4.6 4.6 0 0020.5 10c0-3.6-3.8-6.5-8.5-6.5z" />
      <circle cx="8" cy="11" r="1.1" />
      <circle cx="12" cy="8" r="1.1" />
      <circle cx="16" cy="11" r="1.1" />
    </Icon>
  );
}

export function BulbIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="M9.2 17.5a6 6 0 115.6 0" />
      <path d="M9.5 20.5h5" />
      <path d="M10 17.5v3M14 17.5v3" />
    </Icon>
  );
}

export function AlignLeftIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="M4 4v16" />
      <rect x="7.5" y="6.5" width="11" height="4" rx="1" />
      <rect x="7.5" y="13.5" width="7" height="4" rx="1" />
    </Icon>
  );
}

export function DistributeIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="M4 4v16M20 4v16" />
      <rect x="9.5" y="9" width="5" height="6" rx="1" />
    </Icon>
  );
}

/* -- Theme ---------------------------------------------------------------- */

export function SunIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <circle cx="12" cy="12" r="4" />
      <path d="M12 2.5v2M12 19.5v2M2.5 12h2M19.5 12h2M5.2 5.2l1.4 1.4M17.4 17.4l1.4 1.4M18.8 5.2l-1.4 1.4M6.6 17.4l-1.4 1.4" />
    </Icon>
  );
}

export function MoonIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="M20 14.2A8.3 8.3 0 019.8 4a8.5 8.5 0 1010.2 10.2z" />
    </Icon>
  );
}

export function MonitorIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <rect x="3" y="4.5" width="18" height="12" rx="1.5" />
      <path d="M8.5 20h7M12 16.5V20" />
    </Icon>
  );
}

/* -- Misc ----------------------------------------------------------------- */

export function PinIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="M9 4h6l-.7 5.2 3 2.3v1.5H6.7V11.5l3-2.3L9 4z" />
      <path d="M12 13v7" />
    </Icon>
  );
}

export function ClockIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <circle cx="12" cy="12" r="8.5" />
      <path d="M12 7.5V12l3 1.8" />
    </Icon>
  );
}

export function SparkIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="M12 3.5l1.7 4.8 4.8 1.7-4.8 1.7L12 16.5l-1.7-4.8L5.5 10l4.8-1.7L12 3.5z" />
      <path d="M18.5 16.5l.7 1.8 1.8.7-1.8.7-.7 1.8-.7-1.8-1.8-.7 1.8-.7.7-1.8z" />
    </Icon>
  );
}

export function MoreIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <circle cx="12" cy="5.5" r="1.2" fill="currentColor" stroke="none" />
      <circle cx="12" cy="12" r="1.2" fill="currentColor" stroke="none" />
      <circle cx="12" cy="18.5" r="1.2" fill="currentColor" stroke="none" />
    </Icon>
  );
}

export function MenuIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="M4 7h16M4 12h16M4 17h16" />
    </Icon>
  );
}

export function TerminalIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <rect x="3" y="4.5" width="18" height="15" rx="1.5" />
      <path d="M7.5 9.5l3 2.5-3 2.5" />
      <path d="M12.5 15h4" />
    </Icon>
  );
}

export function DatabaseIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <ellipse cx="12" cy="6" rx="7.5" ry="2.8" />
      <path d="M4.5 6v12c0 1.55 3.36 2.8 7.5 2.8s7.5-1.25 7.5-2.8V6" />
      <path d="M4.5 12c0 1.55 3.36 2.8 7.5 2.8s7.5-1.25 7.5-2.8" />
    </Icon>
  );
}
