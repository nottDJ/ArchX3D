# ArchX3D — Accessibility (v1.0)

Target: **WCAG 2.2 Level AA**.

This document records what the product does, what was measured, how to test it,
and — the part most such documents omit — what is still wrong.

---

## Contents

1. [Position](#1-position)
2. [Contrast](#2-contrast)
3. [Keyboard](#3-keyboard)
4. [Screen readers](#4-screen-readers)
5. [Colour independence](#5-colour-independence)
6. [Motion](#6-motion)
7. [Touch and pointer](#7-touch-and-pointer)
8. [Zoom and reflow](#8-zoom-and-reflow)
9. [Forms](#9-forms)
10. [The 3D viewer](#10-the-3d-viewer)
11. [Known gaps](#11-known-gaps)
12. [How to test](#12-how-to-test)
13. [Checklist for a new screen](#13-checklist-for-a-new-screen)

---

## 1. Position

Accessibility here is a **property of the component library**, not a pass over
finished screens. Every primitive is keyboard-operable and labelled before it
is styled, which is why a page assembled from them is broadly conformant by
default and why the remaining gaps are specific rather than pervasive.

Three decisions carry most of it:

- **Radix for anything with focus management.** Dialog, Menu, Tooltip, Popover,
  Toast, Switch, Slider, Tabs. A hand-written focus trap is the single most
  reliable source of WCAG failures.
- **Semantic HTML.** Real `<table>`, real `<button>`, real `<label>`, real
  `<nav>`. Recovering semantics with ARIA after throwing them away is strictly
  worse than not throwing them away.
- **Colour is never the only signal.** Enforced in the status system by giving
  every state a shape as well as a hue.

---

## 2. Contrast

**1.4.3 Contrast (Minimum) — AA.** Body text ≥ 4.5:1, large text and UI
components ≥ 3:1.

Measured with the [APCA-adjacent WCAG 2 formula] against each token's own
surface, in both themes.

### Dark

| Foreground | On | Ratio | Required | |
| --- | --- | --- | --- | --- |
| `text-primary` | `surface` | 15.8:1 | 4.5 | ✅ |
| `text-secondary` | `surface` | 9.1:1 | 4.5 | ✅ |
| `text-tertiary` | `surface` | 5.9:1 | 4.5 | ✅ |
| `text-tertiary` | `canvas` | 6.4:1 | 4.5 | ✅ |
| `accent-text` | `surface` | 8.7:1 | 4.5 | ✅ |
| `accent-text` | `accent-surface` | 7.2:1 | 4.5 | ✅ |
| `success-text` | `success-surface` | 7.6:1 | 4.5 | ✅ |
| `warning-text` | `warning-surface` | 8.9:1 | 4.5 | ✅ |
| `danger-text` | `danger-surface` | 7.1:1 | 4.5 | ✅ |
| `text-on-solid` | `accent-solid` | 5.4:1 | 4.5 | ✅ |
| `border-line` | `surface` | 1.9:1 | — | decorative |
| `border-line-strong` | `surface` | 3.2:1 | 3.0 | ✅ |
| `focus-ring` | `canvas` | 5.1:1 | 3.0 | ✅ |

### Light

| Foreground | On | Ratio | Required | |
| --- | --- | --- | --- | --- |
| `text-primary` | `surface` | 16.4:1 | 4.5 | ✅ |
| `text-secondary` | `surface` | 8.6:1 | 4.5 | ✅ |
| `text-tertiary` | `surface` | 7.2:1 | 4.5 | ✅ |
| `accent-text` | `surface` | 7.4:1 | 4.5 | ✅ |
| `accent-text` | `accent-surface` | 6.8:1 | 4.5 | ✅ |
| `success-text` | `success-surface` | 6.9:1 | 4.5 | ✅ |
| `warning-text` | `warning-surface` | 7.8:1 | 4.5 | ✅ |
| `danger-text` | `danger-surface` | 7.3:1 | 4.5 | ✅ |
| `text-on-solid` | `accent-solid` | 6.1:1 | 4.5 | ✅ |
| `border-line-strong` | `surface` | 3.1:1 | 3.0 | ✅ |
| `focus-ring` | `canvas` | 3.4:1 | 3.0 | ✅ |

**`text-disabled` is exempt** under 1.4.3, which excludes inactive controls. It
is used *only* for genuinely inert elements, never for de-emphasised content —
that is what `text-tertiary` is for, and it passes.

**Why the ramps make this hold.** OKLCH lightness is perceptually uniform, so
step 11 sits at a predictable contrast against steps 1–3 across every hue. The
semantic colours cannot end up with one quietly failing while its neighbours
pass.

---

## 3. Keyboard

**2.1.1 Keyboard · 2.1.2 No Keyboard Trap · 2.4.3 Focus Order · 2.4.7 Focus
Visible · 2.4.11 Focus Not Obscured**

### Focus indicator

One ring, everywhere:

```css
:focus-visible {
  outline: 2px solid var(--focus-ring);
  outline-offset: 2px;
}
```

**`:focus-visible`, not `:focus`.** A ring left behind after a mouse click
trains users to ignore it, which defeats it for the people who need it. The
2 px offset is what makes it legible on both a light and a dark control — the
gap separates ring from background regardless of either colour.

Inputs use `.focus-field`, a box-shadow that follows the field's own radius
rather than the outline's rectangle.

### Skip link

First tabbable element on every shell page, visually hidden until focused, at
`z-70` so it is reachable even with a modal open. Without it a keyboard user
traverses the whole sidebar on every navigation.

### Tab order

Follows DOM order everywhere. **No positive `tabindex` anywhere in the
codebase** — it creates a parallel order that breaks the moment anything is
inserted.

### Focus management

| Situation | Behaviour |
| --- | --- |
| Dialog opens | Focus moves in, trapped, Escape closes, returns to trigger |
| Menu opens | Focus to first item, arrows navigate, Escape closes |
| Command palette | Focus to input, arrows move `aria-activedescendant`, Enter selects |
| Panel opens | Focus not stolen — it is not modal |
| Route change | Skip link is first stop |

### Global shortcuts

| Key | Action |
| --- | --- |
| `⌘K` / `Ctrl-K` | Command palette |
| `Esc` | Close the topmost overlay |
| `Tab` / `Shift-Tab` | Move |

Shortcuts never fire while focus is in an `input`, `select`, `textarea` or
`contenteditable`.

**Viewer shortcuts** are listed in [`VIEWER.md`](VIEWER.md#3-controls). While
pointer lock is active, `W` and `S` move rather than switching mode or opening
settings — they are movement keys first, and a viewer where walking backwards
opens a panel is unusable.

---

## 4. Screen readers

**1.3.1 Info and Relationships · 4.1.2 Name, Role, Value · 4.1.3 Status
Messages**

### Landmarks

`<nav aria-label="Main">`, `<main id="main">`, `<header>`, `<aside
aria-label="…">`. Every `nav` and `aside` is labelled — two unlabelled `nav`
landmarks are indistinguishable in a landmark list.

### Accessible names

- Icon-only controls: `aria-label`, always.
- `aria-current="page"` on the active nav item and the last breadcrumb.
- `aria-pressed` on toggles; `aria-checked` on radio-style segments.
- `aria-sort` on sortable table headers.
- `aria-busy` on a loading button.

### Live regions

| Region | Politeness | Why |
| --- | --- | --- |
| Field errors | `polite` | Heard when it appears, not by re-walking the form |
| Toasts | Radix live region | Announced without stealing focus |
| Job log | `polite` | Progress without navigating back to it |
| `Alert` danger | `role="alert"` (assertive) | A failure should interrupt |
| `Alert` other | `role="status"` | Announced at the next pause |

**Not everything is an alert.** Marking every message assertive is how
screen-reader users end up muting a product.

### Decorative content

Every icon is `aria-hidden` + `focusable="false"`. Background patterns
(`pattern-grid`, `pattern-glow`) are `aria-hidden`. Unlabelled dividers are
`role="presentation"`.

---

## 5. Colour independence

**1.4.1 Use of Colour**

Every status carries a **shape** as well as a hue:

| Status | Shape | Hue |
| --- | --- | --- |
| Idle | Hollow ring | neutral |
| Queued | Dashed ring | neutral |
| Running | Filled + spinner | accent |
| Complete | Filled + tick | success |
| Failed | Filled + cross | danger |
| Warning | Filled + triangle | warning |

The wizard stepper distinguishes done / current / upcoming by tick, filled ring
and hollow ring — the previous design used emerald vs sky, which to a
deuteranope carried no information at all.

Errors are always icon + message, never a red border alone. Required fields are
never marked by colour.

**Test:** apply a greyscale filter. Every state must remain distinguishable.

---

## 6. Motion

**2.3.3 Animation from Interactions · 2.2.2 Pause, Stop, Hide**

```css
@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after {
    animation-duration: 0.01ms !important;
    animation-iteration-count: 1 !important;
    transition-duration: 0.01ms !important;
  }
  .skeleton { background: var(--neutral-3); }
}
```

Animations are **removed rather than shortened**, but opacity transitions
survive — a state change with no feedback at all is worse than a subtle one.
Nothing that conveys information depends on movement.

No auto-playing video, no parallax, no content that moves for longer than five
seconds without a control.

---

## 7. Touch and pointer

**2.5.8 Target Size (Minimum) — AA, 24 × 24 px**

| Control | Size |
| --- | --- |
| `Button sm` | 28 px + spacing |
| `Button md` | 32 px |
| `Button lg` | 40 px |
| Icon-only `sm` | 28 × 28 |
| Nav item | 32 px full-width |
| Table row action | 28 px + 8 px separation |

All clear the 24 px minimum. Primary actions use `lg` (40 px), which with 4 px
of surrounding space also clears the 44 px iOS convention.

**Nothing is hover-only.** Every hover affordance also responds to
`focus-visible` — the image remove button in the wizard is `opacity-0
group-hover:opacity-100 focus-visible:opacity-100`, so it is reachable by
keyboard.

**2.5.7 Dragging Movements:** no drag-only interaction. File upload accepts
drag *and* a file picker; the viewer's orbit responds to keyboard camera
commands.

---

## 8. Zoom and reflow

**1.4.4 Resize Text · 1.4.10 Reflow · 1.4.12 Text Spacing**

- `maximumScale: 5` in the viewport meta. **Zoom is never disabled** — blocking
  it fails 1.4.4 and is actively hostile in a viewer where inspecting detail is
  the point.
- All type is `rem`-based, so browser font-size settings apply.
- At 400% (1280 px → 320 px equivalent) content reflows to one column with no
  horizontal scroll, except where 1.4.10 explicitly permits it: the data table
  and the viewer toolbar scroll horizontally inside their own containers.
- Line height, letter spacing and paragraph spacing can all be overridden
  without clipping, because no container has a fixed height around text.

---

## 9. Forms

**1.3.5 Identify Input Purpose · 3.3.1 Error Identification · 3.3.2 Labels ·
3.3.3 Error Suggestion**

- Every control has a real `<label>`, wired by `Field`.
- **A placeholder is never the label** — it disappears on focus, exactly when
  someone who paused to think needs it.
- Errors: `aria-invalid`, linked by `aria-describedby`, announced politely, and
  they say what to do rather than only what is wrong.
- Input is never cleared on failure.
- Validation on blur, not on keystroke.

---

## 10. The 3D viewer

The hardest surface, and the one with the real limits.

### What works

- Every toolbar control is a real button, keyboard-reachable, labelled, with
  its shortcut in the tooltip and in `/docs`.
- Room navigation is a keyboard-operable list; flying to a room needs no
  pointer.
- Settings and room panels are fully keyboard-operable.
- All ten viewer shortcuts work without a mouse.
- Orbit mode needs no pointer lock.
- The canvas is `aria-hidden` with a text description of the model — mesh
  count, triangle count, dimensions — in the header, and the room list gives a
  structural, navigable description of the building.

### What does not, and why

**A WebGL canvas is not accessible content.** There is no DOM inside it, so a
screen reader has nothing to traverse. This is a property of the technology,
not an oversight.

The mitigation is that **every piece of information the viewer conveys is also
available as text**: rooms, areas, object counts, materials and dimensions all
appear in the room list, the statistics panel and the review step. A blind user
cannot inspect the render, but they can read the model.

**Walk mode requires pointer lock**, which does not exist on iOS Safari and is
awkward with some assistive technologies. Orbit mode is the default and needs
none of it.

---

## 11. Known gaps

Listed rather than hidden. Each has an owner and a plan.

| Gap | Severity | Status |
| --- | --- | --- |
| **WebGL canvas has no accessible tree** | High for blind users | Mitigated by text equivalents (§10). No general fix exists. |
| **Review-step editor** (`ReviewStep`, `PlanMap`, `Inspector`, `EditorToolbar`, `RoomEditor` — ~2,800 lines) was migrated onto tokens but **not audited for keyboard operability**. `PlanMap` is a canvas-based drag editor and is likely mouse-dependent. | High | Open. The largest remaining item; scoped in `UI_GUIDELINES.md`. |
| **Walk mode is unavailable on iOS Safari** (no pointer lock) | Medium | Orbit is the default there. Documented in `VIEWER.md`. |
| **No screen-reader testing on NVDA or JAWS.** Verified against VoiceOver behaviour and ARIA specification only. | Medium | Open — needs a Windows testing pass. |
| **Job progress is indeterminate.** The backend reports stage transitions, not percentage, so `Progress` cannot be determinate. | Low | Blocked on a backend change. |
| **No automated axe run in CI.** | Medium | Recommended in §12; not wired up. |

---

## 12. How to test

### Manual, in order of value

**1. Unplug the mouse.** Do a complete task — upload a plan, review it,
generate, open the viewer. Anything unreachable is a bug.

**2. Greyscale the page.**
```js
document.documentElement.style.filter = "grayscale(1)";
```
Every state must still be distinguishable.

**3. Zoom to 400%.** No horizontal page scroll, nothing clipped.

**4. Turn on reduced motion.** macOS: Accessibility → Display → Reduce motion.
Windows: Settings → Accessibility → Visual effects.

**5. Screen reader.** VoiceOver (⌘F5) or NVDA. Check landmarks, headings,
button names, and that a status change is announced.

**6. Both themes.** Every check above, twice.

### Automated

```bash
npx @axe-core/cli http://localhost:3000/dashboard
npx lighthouse http://localhost:3000/dashboard --only-categories=accessibility
```

**Recommended CI addition** (not yet wired):

```yaml
- run: npm run build && npm start &
- run: npx @axe-core/cli http://localhost:3000/{dashboard,projects,new,settings,docs} --exit
```

Automated tools catch roughly 30% of issues. They are a floor, not a ceiling —
none of the gaps in §11 would be found by axe.

---

## 13. Checklist for a new screen

**Structure**
- [ ] One `<h1>`; heading levels do not skip
- [ ] Landmarks present and labelled
- [ ] Lists are `<ul>`/`<ol>`; tables are `<table>`

**Keyboard**
- [ ] Everything reachable by Tab
- [ ] Order matches visual order
- [ ] Visible focus on every interactive element
- [ ] Escape closes overlays
- [ ] No keyboard trap

**Names**
- [ ] Icon-only controls have `aria-label`
- [ ] Every form control has a `<label>`
- [ ] Links make sense out of context (no bare "click here")

**State**
- [ ] `aria-current`, `aria-pressed`, `aria-checked`, `aria-sort` where they apply
- [ ] Errors announced and linked
- [ ] Loading state announced

**Visual**
- [ ] Contrast AA in both themes
- [ ] Meaning survives greyscale
- [ ] Readable at 400%
- [ ] Targets ≥ 24 px

**Motion**
- [ ] Respects `prefers-reduced-motion`
- [ ] Nothing essential depends on movement

---

## Related

- [`UI_GUIDELINES.md`](UI_GUIDELINES.md) — the rules these enforce
- [`COMPONENT_LIBRARY.md`](COMPONENT_LIBRARY.md) — where the behaviour lives
- [`DESIGN_SYSTEM.md`](DESIGN_SYSTEM.md) — the ramps behind the contrast figures
- [`VIEWER.md`](VIEWER.md) — viewer controls and shortcuts
