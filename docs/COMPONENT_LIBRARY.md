# ArchX3D — Component library (v1.0)

Every component in `web/components/ui`, its variants, and when to use it —
and, as importantly, when not to.

**Import from the barrel**, `@/components/ui`, not from the file inside it. The
grouping into files is an implementation detail and will change; the names will
not.

```tsx
import { Button, Card, Field, Input, useToast } from "@/components/ui";
```

**Exception:** static server pages (`/`, `/docs`, `not-found`, `loading`)
import directly from the specific file. The barrel re-exports client components
with Radix dependencies, and pulling those into an otherwise-static route costs
~50 kB of JavaScript it never executes. Measured: `/` was 167 kB through the
barrel, 120 kB direct.

---

## Contents

1. [The house rules](#1-the-house-rules)
2. [Button](#2-button)
3. [Card, Section, Stat, Divider](#3-card-section-stat-divider)
4. [Badge, StatusDot, StatusBadge, Kbd](#4-badge-statusdot-statusbadge-kbd)
5. [Field, Input, Textarea, Select, Switch, Slider](#5-field-input-textarea-select-switch-slider)
6. [Dialog, ConfirmDialog, Tooltip, Menu, Popover](#6-dialog-confirmdialog-tooltip-menu-popover)
7. [Spinner, Progress, Skeleton, Alert, EmptyState, Toast](#7-spinner-progress-skeleton-alert-emptystate-toast)
8. [Breadcrumbs, Segmented, Tabs, Table](#8-breadcrumbs-segmented-tabs-table)
9. [Icons](#9-icons)
10. [App shell](#10-app-shell)
11. [Adding a component](#11-adding-a-component)

---

## 1. The house rules

**A component owns its appearance; the caller owns its position.**
`className` takes layout utilities — `w-full`, `mt-4`, `col-span-2`. It does
not take colours or typography. If a component needs to look different, that
is a variant, and a variant is reviewable in a way an ad-hoc override is not.

**Every interactive component is keyboard-operable and labelled.** Not as a
later pass — a component that cannot be reached by Tab is not finished.

**Radix underneath anything with focus management.** Dialog, Tooltip, Menu,
Popover, Toast, Switch, Slider and Tabs are Radix primitives with our
appearance on top. A hand-written focus trap is the single most reliable source
of WCAG failures, and none of the behaviour is ours to invent.

**Composition over configuration.** `<Card>` + `<CardHeader>` + `<CardBody>`
rather than `<Card title= description= footer= />`. A prop-configured component
reaches twenty props and then someone needs the twenty-first.

---

## 2. Button

The most-used control, and the one whose hierarchy the audit found most broken.

```tsx
<Button variant="primary" size="md" onClick={save}>Save</Button>
<Button variant="secondary" icon={<PlanIcon />}>Choose file</Button>
<Button variant="ghost" iconOnly aria-label="More"><MoreIcon /></Button>
<Button variant="danger" loading={deleting}>Delete project</Button>
<Button asChild variant="primary"><Link href="/new">Start</Link></Button>
```

### Variants

| Variant | Appearance | Use | Limit |
| --- | --- | --- | --- |
| `primary` | Solid accent | The one thing this screen is for | **≤ 1 per view** |
| `secondary` | Bordered | A real alternative | any |
| `ghost` | No chrome at rest | Toolbars, dense rows, tertiary | any |
| `danger` | Solid red | Destructive and irreversible | ≤ 1 per view |
| `link` | Underlined text | Inside a sentence | any |

**Two primaries is not emphasis, it is the absence of it.** The user has to
read both to find the way forward — exactly the work the hierarchy exists to
save them.

`danger` is solid, never bordered: a destructive action must not be one visual
step away from `secondary`, because that is how people delete things.

### Sizes

`sm` 28 px · `md` 32 px (default) · `lg` 40 px.

`md` matches `Input` md, so a field and its button sit on one line with no
shim. `lg` clears the 44 px iOS touch convention with 4 px of surrounding
space.

### Props

| Prop | Type | Notes |
| --- | --- | --- |
| `variant` | see above | default `secondary` |
| `size` | `sm \| md \| lg` | default `md` |
| `loading` | `boolean` | Spinner over the label; **width is held** |
| `icon` | `ReactNode` | Leading |
| `iconTrailing` | `ReactNode` | Chevrons, external-link marks |
| `iconOnly` | `boolean` | Square. **`aria-label` required** |
| `asChild` | `boolean` | Render as the child — for `<Link>` |

### Two details worth knowing

**Loading holds the width.** The spinner is absolutely positioned and the label
fades rather than unmounting. A button that resizes when clicked shifts every
control beside it.

**`asChild` uses `Slottable`.** That is what lets `asChild` compose with
leading and trailing icons — without it Radix's `Slot` sees three children and
throws at render. This was a real build failure, not a theoretical one.

### ButtonGroup

Joins related buttons into one object: collapsed inner corners, shared borders,
hovered member paints over its neighbour.

---

## 3. Card, Section, Stat, Divider

```tsx
<Card elevation="raised" interactive onClick={open}>
  <CardHeader title="Riverside" description="5 rooms" icon={<PlanIcon />}
              actions={<Button variant="ghost" size="sm" iconOnly …/>} />
  <CardBody>…</CardBody>
  <CardFooter><Button variant="primary">Open</Button></CardFooter>
</Card>
```

### Elevation is a job, not a number

| `elevation` | Job |
| --- | --- |
| `flat` | Content grouped on the canvas |
| `raised` | A discrete object you can act on *(default)* |
| `floating` | Temporarily above the page |

`interactive` adds hover feedback — **only for cards that are actually
clickable.** A card that lifts and does nothing is a broken promise.

### Section

A labelled region of a page, one level above a card, where a border would add
weight the content does not need.

### Stat

One number and its label. **Deliberately not a chart** — wrapping a single
value in a sparkline adds ink without information and invites the reader to
interpret noise as trend.

### Divider

`role="presentation"` when unlabelled: announcing "separator" for every visual
divider makes a screen-reader walk-through of a dense page unbearable.

---

## 4. Badge, StatusDot, StatusBadge, Kbd

### The rule that shapes all of these

**Colour is never the only signal.** Roughly 1 in 12 men has a colour-vision
deficiency and red/green is the commonest confusion — precisely the pair a
status system reaches for first. So every status carries a **shape** as well as
a hue:

| Status | Shape | Tone |
| --- | --- | --- |
| `idle` | Hollow ring | neutral |
| `queued` | Dashed ring | neutral |
| `running` | Filled dot + spinner | accent |
| `complete` | Filled dot + tick | success |
| `failed` | Filled dot + cross | danger |
| `warning` | Filled dot + triangle | warning |

Turn the page greyscale and it still reads.

### StatusDot always renders text

`hideLabel` keeps it for screen readers but is only for the case where the
label is already adjacent in the same cell. **A bare coloured dot in a table is
a puzzle, not information.**

### statusFromStage

Maps a backend pipeline stage (`analysed`, `generated`, `dxf_uploaded`) onto one
of four states a person recognises. One function, so a new backend stage needs
one edit rather than one per surface.

### Kbd / KbdChord

Keyboard hints, inline in menus and the command palette. Shortcuts are only
useful if they are visible, and a tooltip nobody opens is not visible.

---

## 5. Field, Input, Textarea, Select, Switch, Slider

```tsx
<Field label="Project name" hint="Shown in your project list" error={error}>
  <Input value={name} onChange={…} placeholder="Riverside apartment" />
</Field>
```

### Field does the wiring

Generates an id, points the `<label>` at it, links hint and error through
`aria-describedby`, sets `aria-invalid`. That is its entire reason to exist: **a
placeholder is not a label** — it disappears on focus, exactly when someone who
paused to think needs it — and a `<div>` above an input is not a label to
anything that cannot see.

The message region is a live region and is **always rendered**, even empty: a
region that appears and disappears is not reliably announced, one that exists
and gains content is.

`optional` marks the few optional fields rather than the many required ones.

### Input

`size` (`sm`/`md`/`lg`), `icon` (leading, decorative), `trailing` (clear button,
unit, shortcut hint).

### Select — native, deliberately

A native `<select>`, styled. It gets platform keyboard behaviour, type-ahead,
and — decisively — the platform's own picker on mobile, which is far better
than any `div`-based menu on a touch screen. Where a menu needs icons or
descriptions, use `Menu`.

### Switch vs checkbox

**A switch means "on now". A checkbox means "will apply when you submit."** A
switch inside a form with a Save button is a small lie, and users respond by
clicking Save and then checking whether it worked.

### Slider

Value always visible. A slider whose value only appears on drag is unreadable
at rest — the user has to grab it to find out what it says, which for a
settings panel is backwards.

---

## 6. Dialog, ConfirmDialog, Tooltip, Menu, Popover

### Why these are Radix

A correct dialog needs: a focus trap that survives DOM changes, focus restored
to the trigger on close, `aria-modal` with the page inert, Escape and
outside-click dismissal, scroll locking that does not shift layout, and
portalling that does not break z-index or event bubbling. Each is a few lines;
together they are a library, and every one is a WCAG failure when subtly wrong.

### Dialog

```tsx
<Dialog open={open} onOpenChange={setOpen} title="Rename project"
        footer={<><Button variant="ghost">Cancel</Button>
                  <Button variant="primary">Save</Button></>}>
  <Field label="Name"><Input … /></Field>
</Dialog>
```

Sizes `sm` / `md` / `lg`.

### ConfirmDialog

Separate from `Dialog` so the pattern is **enforced rather than remembered**:
the destructive verb is on the button ("Delete project", not "OK"), Cancel is
the safe default, and the consequence is spelled out.

A dialog whose buttons say OK and Cancel makes the user reconstruct which is
which from the title.

### Tooltip

**Hints, never the only source.** A tooltip does not appear on touch and is not
reliably keyboard-reachable on every platform, so nothing essential may live in
one: it carries the shortcut and the elaboration, while the *label* lives in
`aria-label`. If a control cannot be understood without its tooltip, it needs a
visible label.

`TooltipProvider` wraps the app once — the shared delay only works across one
provider, so moving between toolbar buttons does not re-wait.

### Menu

`Menu` + `MenuItem` + `MenuSeparator` + `MenuLabel`. Items take `icon`,
`shortcut` and `destructive`.

### Popover

Rich transient content — a filter form, a help card.

---

## 7. Spinner, Progress, Skeleton, Alert, EmptyState, Toast

### The loading ladder

| Wait | Component |
| --- | --- |
| < 300 ms | nothing |
| 300 ms – 1 s | `Spinner` |
| > 1 s, known % | `Progress` determinate |
| > 1 s, unknown | `Progress` indeterminate |
| Page or region | `Skeleton` |

### Progress

`value` is `0..1`, or `null` for indeterminate. When indeterminate,
`aria-valuenow` is **omitted entirely** — reporting 0 would announce "0
percent", which claims no progress rather than unknown progress.

A determinate bar never renders below 1.5%: a 0% bar is indistinguishable from
a broken one.

### Skeleton

`Skeleton`, `SkeletonText`, `SkeletonCard`. **A skeleton is only honest if it
matches the layout that replaces it** — one that does not causes a visible
reflow the moment content lands, which is worse than a plain spinner because it
promised a shape and broke it.

`SkeletonText`'s last line is short, because real paragraphs end mid-line.

### Alert vs Toast

| | Use |
| --- | --- |
| `Alert` | Something that **is true** — "this project has no reference images" |
| `Toast` | Something that **happened** — "screenshot saved" |

Transient state in an alert nags; persistent state in a toast vanishes unread.

`role` follows severity: `alert` for danger (interrupts a screen reader,
appropriate for a failure), `status` for the rest. Marking everything as an
alert is how screen-reader users end up muting a product.

### EmptyState

**Every empty state carries an action.** An empty state without one is a dead
end, and the moment a user finds an empty list is exactly the moment they are
willing to fill it.

### Toast

```tsx
const { toast } = useToast();
toast({ tone: "success", title: "Screenshot saved" });
```

Radix handles the parts that are easy to get wrong: live region, hover pauses
the dismiss timer, F6 reaches the region from the keyboard.

`useToast` throws without a provider rather than returning a no-op — a wiring
bug should surface immediately, not as confirmations that silently stopped
appearing.

---

## 8. Breadcrumbs, Segmented, Tabs, Table

### Tabs vs Segmented — they mean different things

| | Meaning |
| --- | --- |
| **Tabs** | Switch between *panels of content*. Different things. |
| **Segmented** | Switch the *mode* of one thing. Same content, different view. |

The viewer's Orbit/Walk is segmented — one model, two ways of moving through
it. Dashboard/Projects would be tabs. Getting this wrong teaches users the
wrong mental model of what a control will do.

### Breadcrumbs

`aria-current="page"` on the last crumb. Without it a screen reader reads a
list of links with no indication of where you are.

### Table — a real `<table>`

Not a grid of divs. Screen readers announce row and column position, "row 4 of
12", and allow cell-by-cell navigation — none of which is recoverable once the
semantics are thrown away for layout convenience.

`TH` takes `sortable`, `sorted` and `onSort`, and sets `aria-sort`. The arrow
glyph conveys nothing to a screen reader; `aria-sort` is what makes a sorted
table comprehensible without sight.

---

## 9. Icons

One set, `components/ui/icons.tsx`. 24-unit grid, 1.5 stroke, round caps and
joins, drawn for 16 px.

```tsx
import { PlanIcon, WalkIcon } from "@/components/ui/icons";
<PlanIcon className="size-4" />
```

- **Decorative by default** — `aria-hidden`, `focusable="false"`. Meaning comes
  from an adjacent label or the container's `aria-label`.
- **`currentColor`** — a button's text colour drives its icon.
- **Sized by the caller** with `size-*`.

`components/{wizard,generate,viewer}/icons.tsx` are re-export shims kept so
~40 existing import sites keep working. New code imports from `ui/icons`
directly.

---

## 10. App shell

### AppShell

```tsx
<AppShell title="Projects" breadcrumbs={[{ label: "Projects" }]}
          description="12 projects" actions={<Button…/>} width="wide">
  {children}
</AppShell>
```

`width`: `default` (68 rem, forms) · `wide` (90 rem, tables and grids) ·
`full` (viewer).

Provides the sidebar, header, skip link, command palette and ⌘K binding.

**Not used by** `/` (marketing, not application) and `/viewer` (a 3D canvas
wants every pixel and has its own floating chrome). Both link back explicitly.

### CommandMenu

⌘K / Ctrl-K. Searches projects and commands, groups results, full keyboard
navigation with `aria-activedescendant`.

Built on Radix Dialog rather than a palette library — the hard parts are the
focus trap and dismissal, which Dialog already solves, and the filtering is
twenty lines.

### ThemeToggle / ThemeSegmented

Three options, not two. A two-state toggle forces a user to pick a side and
re-pick it whenever their environment changes; "system" is the honest default.

A menu rather than a cycling button, because a cycling button cannot show what
the *next* press will do.

---

## 11. Adding a component

1. **Does an existing one cover it?** Usually yes. A variant beats a new
   component.
2. **Does it need focus management?** Use Radix.
3. **Write the docstring first** — what it is for, and what it refuses to do.
4. **Tokens only.** No literals.
5. **Keyboard and screen reader before styling.** Retrofitting is harder and
   usually incomplete.
6. **Both themes.**
7. **Export from `index.ts`.**
8. **Document here**, including when *not* to use it.

### Never

- A literal colour
- An arbitrary spacing or type value where a scale step exists
- A hand-written focus trap
- A `div` with `onClick` where a `button` belongs
- An icon-only control without `aria-label`
- A component that duplicates one that exists

---

## Related

- [`DESIGN_SYSTEM.md`](DESIGN_SYSTEM.md) — the tokens these are built from
- [`UI_GUIDELINES.md`](UI_GUIDELINES.md) — when to use what
- [`ACCESSIBILITY.md`](ACCESSIBILITY.md) — the conformance these components carry
- [`FRONTEND_ARCHITECTURE.md`](FRONTEND_ARCHITECTURE.md) — where they live
