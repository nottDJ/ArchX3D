# ArchX3D — UI guidelines (v1.0)

The UX audit that motivated the redesign, and the rules that came out of it.

**Who this is for.** Anyone adding a screen, a control or a piece of copy.
Read [`DESIGN_SYSTEM.md`](DESIGN_SYSTEM.md) for the tokens and
[`COMPONENT_LIBRARY.md`](COMPONENT_LIBRARY.md) for the components; this
document is the judgement that sits above both.

---

## Contents

**Part 1 — The audit**
1. [Method and severity scale](#1-method-and-severity-scale)
2. [Findings, ranked](#2-findings-ranked)
3. [What was already right](#3-what-was-already-right)

**Part 2 — The rules**
4. [Visual hierarchy](#4-visual-hierarchy)
5. [Layout and spacing](#5-layout-and-spacing)
6. [Typography](#6-typography)
7. [Colour](#7-colour)
8. [Iconography](#8-iconography)
9. [Writing](#9-writing)
10. [State: loading, empty, error, success](#10-state-loading-empty-error-success)
11. [Motion and microinteractions](#11-motion-and-microinteractions)
12. [Forms and validation](#12-forms-and-validation)
13. [Responsive behaviour](#13-responsive-behaviour)
14. [Review checklist](#14-review-checklist)

---

# Part 1 — The audit

## 1. Method and severity scale

Every page in the app was walked through end to end at three widths (390 px,
768 px, 1440 px), in both colour schemes, with a keyboard only, and with the
axe rule set. Findings are ranked by what they cost a user, not by how hard
they are to fix.

| Severity | Meaning |
| --- | --- |
| **S1 — Blocking** | Makes a task impossible for some users, or loses their work |
| **S2 — Serious** | Costs time on every use, or produces a wrong mental model |
| **S3 — Moderate** | Noticeable friction; the user gets there anyway |
| **S4 — Polish** | Inconsistency a professional notices and a novice does not |

Status: **Fixed** in this pass, **Partial** where the mechanism landed but not
every surface, **Open** where it is documented and deliberately deferred.

---

## 2. Findings, ranked

### S1 — Blocking

| # | Finding | Evidence | Status |
| --- | --- | --- | --- |
| 1 | **No navigation existed.** Every page was an island. From the wizard there was no route to the viewer, from the viewer none to the projects, and nothing anywhere listed what else the product could do. Discovery depended on the user keeping a tab open. | `app/` had 4 routes, no shared shell, no nav component | **Fixed** — persistent sidebar, command palette, breadcrumbs |
| 2 | **A finished project was unreachable once its tab closed.** The wizard held `project_id` in React state only. Close the tab and the model existed on the server, permanently unreachable through the UI. | `Wizard.tsx` — `manifest` in `useState`, never persisted | **Fixed** — local project registry, dashboard, project list |
| 3 | **Light mode was impossible.** Every page forced `data-theme="dark"` and `colorScheme: "dark"` on its own root, with 511 hard-coded colour utilities beneath. A user in a bright studio had no recourse. | 353 palette literals + 158 white/black alpha utilities | **Fixed** — full token system, three-way theme control |
| 4 | **`iconOnly` buttons rendered nothing.** The variant suppressed its own children, so the dialog close button was an empty square. | Introduced during the component pass; caught by build | **Fixed** |

### S2 — Serious

| # | Finding | Evidence | Status |
| --- | --- | --- | --- |
| 5 | **Button hierarchy was flat.** "Continue without images" and "Analyse images" were both `bg-white text-zinc-900`. Skipping the AI step and using it looked equally recommended. Emerald, white and bordered buttons competed on the same screen with no rule. | `Wizard.tsx`, `GenerationDashboard.tsx` | **Fixed** — five variants, at most one primary per view |
| 6 | **Status relied on hue alone.** "Done" and "active" steps differed only by emerald vs sky. To a user with deuteranopia — 1 in 12 men — the stepper had no current position. | `Stepper`, `Timeline` | **Fixed** — shape carries state (tick / filled / hollow), hue is secondary |
| 7 | **Three icon sets, six icons drawn twice.** `CheckIcon`, `CopyIcon`, `CubeIcon`, `RetryIcon`, `SpinnerIcon` and `WarningIcon` existed in two files at different weights and radii, so the same idea looked different depending on the screen. | 530 lines across `wizard/`, `generate/`, `viewer/icons.tsx` | **Fixed** — one 60-icon set, old paths kept as shims |
| 8 | **Every error looked identical.** One red bar at the top of the wizard for all failures: a rejected image, a validation warning the user could ignore, and the backend being unreachable rendered the same. | `Wizard.tsx` — single `error: string` | **Fixed** — typed problems with tone, title, detail and an action |
| 9 | **No empty states.** Zero images showed a bare `<p>`. No projects was unreachable (see #2), so it had never been designed. | `ImagesStep` | **Fixed** — `EmptyState` everywhere, always with an action |
| 10 | **The only progress signal was a scrolling log.** A user watching a 20-minute analysis saw terse status codes and no indication of how far through it was. | `JobProgress` | **Partial** — headline stage, elapsed clock, indeterminate bar, log collapsed. True percentage needs a backend change |
| 11 | **Formatting was inconsistent across surfaces.** `formatBytes` existed twice with different rounding, so the same file read "1.4 MB" in the wizard and "1.44 MB" in the viewer. | `lib/wizard.ts`, `viewer/LoadingOverlay.tsx` | **Fixed** — one `lib/format.ts` |
| 12 | **Focus was invisible in places and permanent in others.** Some controls used `:focus` (ring left after a mouse click, training users to ignore it), others had no visible ring at all. | mixed across components | **Fixed** — one `:focus-visible` ring, globally |

### S3 — Moderate

| # | Finding | Status |
| --- | --- | --- |
| 13 | **Spacing had no scale.** `p-4`, `p-5`, `p-6`, `p-7` and arbitrary `py-2.5` appeared with no rule; card padding varied between adjacent cards. | **Fixed** — documented 4 px scale, `p-4` for cards |
| 14 | **Radii were not concentric.** `rounded-xl` controls inside `rounded-xl` cards, so nesting read as flat. | **Fixed** — 6-step scale, nested radii step down |
| 15 | **Type sizes were arbitrary.** `text-[13px]`, `text-[11px]`, `text-[10px]` inline alongside `text-sm`/`text-xs`, with no ratio. | **Fixed** — 10-step scale on a 1.20 ratio |
| 16 | **The wizard could not be resumed.** Reloading mid-flow returned to step 1 with the project orphaned. | **Partial** — the project is now recoverable from the dashboard; in-flight step is not restored |
| 17 | **Destructive actions had no confirmation.** Removing an image was immediate and irreversible. | **Partial** — `ConfirmDialog` exists and guards index clearing; image removal is still immediate (it is re-uploadable) |
| 18 | **Tables were `div` grids.** No row/column semantics for screen readers. | **Fixed** — real `<table>` with `aria-sort` |
| 19 | **No skip link.** A keyboard user traversed the full page chrome on every navigation. | **Fixed** |
| 20 | **Loading was a spinner or nothing.** No skeletons, so content landed with a reflow. | **Fixed** — skeletons shaped like their content |

### S4 — Polish

| # | Finding | Status |
| --- | --- | --- |
| 21 | Numbers used proportional figures and jittered as they updated | **Fixed** — `tabular-nums` globally |
| 22 | Headings had no `text-wrap: balance`; titles broke with one orphan word | **Fixed** |
| 23 | Hover-only affordances (image remove button) were unreachable by keyboard | **Fixed** — `focus-visible:opacity-100` |
| 24 | Shadow use was decorative and inconsistent between adjacent surfaces | **Fixed** — three elevations, each tied to a job |
| 25 | JetBrains Mono loaded all weights; only two render | **Fixed** — `weight: ["400", "500"]` |
| 26 | No `title`/`aria-label` on several icon buttons | **Fixed** |

### Open — deliberately deferred

| Finding | Why |
| --- | --- |
| **Authentication** | The backend has no auth and no user model. A login form would be a facade that implies security which does not exist. Specified in [`FRONTEND_ARCHITECTURE.md`](FRONTEND_ARCHITECTURE.md#authentication-when-it-arrives), not built. |
| **Server-side project list, history and storage figures** | Needs `GET /api/projects` and a job-history endpoint. Out of scope for a frontend-only change; the local registry is the honest substitute and says so in the UI. |
| **Comparison view** | Two viewers side by side is buildable on the current API, but was cut to finish the foundation properly. Sketched in `FRONTEND_ARCHITECTURE.md`. |
| **Review step full redesign** | `ReviewStep`, `PlanMap`, `Inspector`, `EditorToolbar` and `RoomEditor` are ~2,800 lines of specialist editor UI. They were migrated onto tokens (so they theme correctly) but their layout and interaction design is unchanged. |

---

## 3. What was already right

Recorded because a redesign that discards good work is not an improvement.

- **The review step exists at all.** Showing detections *with their confidence,
  including discarded ones*, before spending render time is the product's best
  idea and the redesign amplifies it rather than touching it.
- **Deterministic, honest copy.** "No reference images yet — the model will be
  built unfurnished" tells the user the consequence, not just the state. Most
  of the existing microcopy survives verbatim.
- **The DropZone's drag-depth counter.** A correct solution to a real bug
  (nested `dragleave` flicker), with a comment explaining why.
- **Documented reasoning throughout.** Every existing module explains its
  decisions. That standard is why this audit could be done from the code.

---

# Part 2 — The rules

## 4. Visual hierarchy

**One primary action per view.** Two primaries is not emphasis, it is the
absence of it — the user has to read both to find the way forward.

The ladder, in order of weight:

```
primary      solid accent      the one thing this screen is for
secondary    bordered          a real alternative
ghost        no chrome         tertiary, toolbars, dense rows
danger       solid red         destructive, at most one
link         inline            sits inside a sentence
```

**Weight comes from position, size and contrast — in that order.** Reach for
colour last. A heading that needs to be brighter usually needs to be *higher*
or *larger*, and a screen where three things are brightest has no hierarchy at
all.

**Three levels of heading per page, maximum.** Page title, section, card
title. A fourth means the page is two pages.

---

## 5. Layout and spacing

### The scale

4 px base. Only these values:

```
1  = 4px    gaps inside a control (icon to label)
2  = 8px    related items in a row
3  = 12px   items within a group
4  = 16px   card padding, list rows            ← the default
5  = 20px   dialog padding
6  = 24px   between sections
8  = 32px   between major regions
12 = 48px   page top margin
```

If a value is not on this list, the layout is wrong rather than the scale.
`gap-[7px]` never survives review.

### Containers

| Token | Width | Use |
| --- | --- | --- |
| `--container-prose` | 44 rem | Documentation, settings — anything read as prose |
| `--container-content` | 68 rem | Forms, the wizard, landing sections |
| `wide` | 90 rem | Tables and card grids, where density is the point |
| `full` | none | The viewer only |

**Prose never exceeds 44 rem.** Above roughly 75 characters per line the eye
loses its place returning to the left margin.

### Alignment

- One left edge per column. A page with three different left margins reads as
  three pages.
- Optical alignment beats mathematical: an icon beside text is centred on the
  text's cap height, not its line box.
- Numbers right-align in tables; text left-aligns; nothing centres except in an
  empty state.

---

## 6. Typography

Ten sizes on a 1.20 ratio, rounded to whole pixels. The full scale is in
[`DESIGN_SYSTEM.md`](DESIGN_SYSTEM.md#typography).

**`text-sm` (13 px) is the product's default.** Not 14 or 16 — this is a dense
professional tool where a table row and a settings panel both need to fit, and
13 px at Inter's x-height is comfortable at arm's length on a laptop.

| Role | Size | Weight |
| --- | --- | --- |
| Page title | `text-xl` | 600 |
| Section title | `text-sm` | 600 |
| Card title | `text-sm` | 500–600 |
| Body | `text-sm` | 400 |
| Secondary | `text-xs` | 400 |
| Metadata, keys | `text-2xs` | 400–500 |

**Rules**

- **Never below 11 px.** Below that, hinting collapses and the scale becomes a
  rendering accident.
- **Monospace for anything the user might copy or compare** — IDs, filenames,
  byte counts, coordinates, log lines.
- **`tabular-nums` everywhere** (it is the body default). A count that changes
  width as it updates makes a row twitch.
- **Headings opt back into proportional figures** — a title is prose.
- **`text-wrap: balance` on headings, `pretty` on paragraphs.** Removes orphans
  without a manual `<br>`.

---

## 7. Colour

### The rule

**Colour is never the only carrier of meaning.** Every status pairs a hue with
a shape; every error pairs red with an icon and a message. Turn the interface
greyscale and it must still be usable — this is WCAG 1.4.1 and it is also just
correct, because a hue that means "failed" in one product means "record" in
another.

### Semantic use only

Components reference `text-secondary`, `bg-surface`, `border-line` — never
`text-zinc-400`. A literal in a component is a token that has not been named
yet.

| Intent | Means | Never |
| --- | --- | --- |
| **accent** | The forward path, the current selection | Decoration, or "this is nice" |
| **success** | Something completed | A resting state that happens to be fine |
| **warning** | Needs a human, but nothing is broken | Mild disapproval |
| **danger** | Failed, or will destroy something | Emphasis |

**Neutral is the default and most of the interface.** A screen where every
element is tinted has no accent left to spend.

### Contrast

All four text levels clear WCAG AA against their own surfaces in both themes.
Measured values in [`ACCESSIBILITY.md`](ACCESSIBILITY.md#contrast).
`text-disabled` is exempt by specification and is used only for genuinely
inert controls.

---

## 8. Iconography

One set: `components/ui/icons.tsx`. 24-unit grid, 1.5 stroke, round caps.

- **Icons are decorative by default** — `aria-hidden`, sized by the caller.
  Meaning comes from the label beside them, or from `aria-label` on the
  container.
- **An icon-only control needs `aria-label`.** No exceptions; a tooltip is not
  a substitute because it does not exist on touch.
- **Never invent a local icon.** Add it to the set, drawn on the same grid, so
  it cannot drift from its neighbours.
- **Never use an icon to mean two things.** `CheckIcon` is "done", not "select"
  and not "correct".

---

## 9. Writing

Copy is interface. It is reviewed like code.

**Say the consequence, not the state.**

> ✗ No images uploaded.
> ✓ Without photographs the model is built as an unfurnished shell — correct
>   geometry, no furniture or materials.

**Name what will happen, on the button.**

> ✗ OK / Cancel
> ✓ Clear index / Cancel

**Errors: what happened, why, what to do.**

> ✗ Failed to load.
> ✓ Could not reach localhost:8000. Check that the ArchX3D server is running
>   and that it allows requests from this page.

**Do not apologise, do not blame.** "We're sorry, something went wrong" wastes
the line that could have said which thing.

**Be specific about limits.** "Projects are indexed in this browser" is better
than "Local storage" and far better than silence.

**Sentence case for everything** — headings, buttons, labels. Title Case
belongs to proper nouns.

**British spelling**, matching the rest of the codebase: *colour, behaviour,
optimise, analyse, centre*.

---

## 10. State: loading, empty, error, success

Four states, four components, no overlap.

### Loading — pick by duration and knowledge

| Wait | Indicator |
| --- | --- |
| < 300 ms | Nothing. A flash of spinner is worse than a pause. |
| 300 ms – 1 s | `Spinner`, in place |
| > 1 s, known % | `Progress` determinate |
| > 1 s, unknown | `Progress` indeterminate |
| Page or region | `Skeleton`, shaped like the content |

**A skeleton must match the layout it replaces.** One that does not causes a
reflow the moment content lands — worse than a plain spinner, because it
promised a shape and broke it.

### Empty — never a dead end

Every empty state carries an action. The moment a user finds an empty list is
exactly the moment they will fill it.

### Error — `Alert` inline, `Toast` for outcomes

- `Alert` for something that **is true** ("this project has no images").
- `Toast` for something that **happened** ("screenshot saved").
- Transient state in an alert nags; persistent state in a toast vanishes
  unread.
- `role="alert"` for danger only. Marking everything as an alert is how
  screen-reader users end up muting a product.

### Success — confirm, then get out of the way

A toast that auto-dismisses, or a state change the user can see. Never a
dialog that must be dismissed to continue.

---

## 11. Motion and microinteractions

**Motion explains change. It never announces itself.**

| Duration | Use |
| --- | --- |
| 80 ms | Hover, focus |
| 120 ms | Button press, toggle, tooltip |
| 180 ms | Panel, menu, roof fade |
| 260 ms | Page-level transition, toast |
| 400 ms | Camera flight — the only long one, because it is *travel* |

Nothing in a productivity tool exceeds 300 ms except literal movement through
space. A user repeating an action fifty times a day comes to resent 400 ms.

### The catalogue

| Interaction | Behaviour | Why |
| --- | --- | --- |
| Button hover | Background lightens, 120 ms | Confirms the target before commitment |
| Button press | 1 px down | Physical without moving the row |
| Button loading | Spinner over label, width held | A resizing button shifts its neighbours |
| Card hover | 1 px lift + border strengthens | Says "liftable", not "bouncy" |
| Focus | 2 px ring, 2 px offset, no transition | Focus must be instant |
| Panel open | Slide 180 ms from its own edge | The direction says where it came from |
| Toast | Slide from right, dismissible, pauses on hover | Never steals focus |
| List appearance | 30 ms stagger, capped at 6 | Reads as a list being built |
| Error | 320 ms shake, one cycle | Draws the eye without scolding |
| Roof toggle | 180 ms opacity fade | A hard cut reads as a glitch |
| Skeleton | 1.8 s shimmer | Slow on purpose; fast reads as urgency |

### Reduced motion

`prefers-reduced-motion` removes animation but **keeps opacity transitions** —
a state change with no feedback at all is worse than a subtle one. Nothing
that conveys information depends on movement.

---

## 12. Forms and validation

- **Every control has a real `<label>`.** A placeholder is not a label: it
  disappears on focus, exactly when someone who paused to think needs it.
- **Mark the optional fields, not the required ones.** In a form where most
  fields are required, marking them is noise.
- **Validate on blur, not on keystroke.** Telling someone their email is
  invalid while they are typing the third character is hostile.
- **Errors sit under the field, are linked by `aria-describedby`, and are
  announced.** Colour alone is not a message.
- **Keep the user's input on failure.** Always.
- **A switch means "on now".** A checkbox means "will apply on save". A switch
  inside a form with a Save button is a small lie.

---

## 13. Responsive behaviour

Three breakpoints. More is a sign the layout is fighting itself.

| Range | Layout |
| --- | --- |
| < 640 px | Single column. Sidebar is a drawer. Tables drop non-essential columns. Toolbar scrolls horizontally. |
| 640 – 1024 px | Two-column grids. Sidebar still a drawer. |
| > 1024 px | Sidebar permanent. Three- and four-column grids. |

**Rules**

- **Design the narrow case first.** Widening is easy; the reverse is a rewrite.
- **Drop columns, never shrink text.** A 10 px table is unreadable on the
  device where it matters most.
- **44 px minimum touch target** with 8 px of separation.
- **Nothing is hover-only.** Every hover affordance also responds to focus, and
  on touch is either always visible or reachable from a menu.
- **The viewer is desktop-first and says so** — pointer lock does not exist on
  iOS Safari, so walk mode is unavailable there and orbit is the default.

---

## 14. Review checklist

**Hierarchy**
- [ ] Exactly one primary action
- [ ] Three heading levels or fewer
- [ ] The most important thing is highest and largest, not just brightest

**System**
- [ ] No literal colours — tokens only
- [ ] Spacing values from the scale
- [ ] Type sizes from the scale
- [ ] Icons from `components/ui/icons.tsx`
- [ ] No new component that an existing one covers

**States**
- [ ] Loading, empty and error all designed
- [ ] Empty state offers an action
- [ ] Error says what to do
- [ ] Destructive action confirms, and names what it destroys

**Accessibility**
- [ ] Keyboard-reachable, in a sensible order
- [ ] Visible focus on every interactive element
- [ ] Icon-only controls have `aria-label`
- [ ] Meaning survives greyscale
- [ ] Contrast passes AA
- [ ] Works at 200% zoom

**Copy**
- [ ] Sentence case
- [ ] British spelling
- [ ] Buttons name their action
- [ ] No apology, no blame
- [ ] Consequences stated, not just state

**Responsive**
- [ ] 390 px, 768 px, 1440 px all checked
- [ ] Touch targets ≥ 44 px
- [ ] No hover-only affordance

---

## Related

- [`DESIGN_SYSTEM.md`](DESIGN_SYSTEM.md) — tokens, scales, ramps
- [`COMPONENT_LIBRARY.md`](COMPONENT_LIBRARY.md) — every component and its variants
- [`ACCESSIBILITY.md`](ACCESSIBILITY.md) — conformance, testing, known gaps
- [`FRONTEND_ARCHITECTURE.md`](FRONTEND_ARCHITECTURE.md) — structure, state, performance
- [`VIEWER.md`](VIEWER.md) — the 3D viewer's own interaction design
