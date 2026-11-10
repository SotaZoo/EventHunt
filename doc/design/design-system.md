# Event Hunt web design system

The approved discovery flow uses Apple-HIG-informed web styling, not an Apple
native web SDK. Ionic Core controls run in iOS mode. The Penpot file owns layout,
interaction intent and the shared visual system. Its **Design system** page
contains reusable foundations and primary-button states; the mobile page uses
the same semantic tokens across discovery, filters, Account and AI access.
Production tokens live in `web/discovery/styles.css`, not the scratch preview.
Penpot changes are design specifications, not evidence of a deployed update.

| Role | Light | Dark |
|---|---|---|
| Background | `#f2f2f7` | `#141416` |
| Surface | `#ffffff` | `#242427` |
| Primary text | `#1c1c1e` | `#f5f5f7` |
| Secondary text | `#63636c` | `#c0c0c8` |
| Action/selection | `#0066cc` | `#73b7ff` |

Follow system appearance. Use the system font and four rem-based roles:
title 22px, body/control 17px, secondary/facts 15px, caption 13px at default text
size. Do not shrink roles at mobile breakpoints. Keep at least 44px targets,
visible keyboard focus and reduced-motion support.

## Materials and shared controls

Follow [Apple's materials guidance](https://developer.apple.com/design/human-interface-guidelines/materials):
reserve the glass treatment for navigation and controls. Event content,
setup instructions and authorization permissions stay on solid surfaces.
This is a web adaptation, not Apple's native Liquid Glass implementation.

The current light Penpot system uses semantic `eh.ui.*` tokens referencing
`eh.base.*` primitives. It retains the four text roles above, a 4px spacing
scale (including 20px content insets), 16px grouped surfaces and 48px-high
primary buttons with 24px radii. Primary buttons include default, hover,
pressed, keyboard-focus and disabled states. Penpot uses Inter as the design
font; the web uses the system font.

Navigation uses a restrained 92%-white surface and subtle elevation. Penpot
2.16 represents that material statically; background blur is not simulated.
The web must retain an opaque, legible fallback when backdrop filtering is
unavailable or increased contrast/reduced transparency is requested. Motion
must respect reduced-motion preferences. Dark appearance retains the semantic
roles and colors above; the current six-screen Penpot review covers light.

Calendar cells keep equal seven-column geometry. Slider thumbs may be smaller
visually, but their interactive area must remain at least 44px. Calendar gestures,
filter values, clipboard actions and external OAuth need real-browser tests;
Penpot annotations describe what its native prototype cannot execute.

## Event cards

Card C retains the time column. Content order is title, location/Online, food
when provided, fit score followed by a positive highlight in one text flow,
then urgency when present. The highlight may wrap naturally; do not shrink or
clip it to force one line. Day headings group events chronologically.

Fit icons retain the Discord rule: 0.90+ 🔥, 0.80+ ⭐, 0.70+ ✨, lower 🔅;
missing score is “Fit —” without an icon. Every score >= 0.90 uses the selected
**Fit signal (variant C)**: a compact warm-neutral capsule around icon and score.
The highlight sits outside the capsule, after the score. This is passive, not a
button. Rows stay unboxed; titles and highlights do not gain size or weight.

| Fit signal role | Light | Dark |
|---|---|---|
| Surface | `#f1ece3` | `#332e25` |
| Text | `#54452f` | `#e5d6bb` |
| Border | `#e0d6c5` | `#665942` |

Use the coherent Lucide map-pin, upright utensils, monitor and clock icons.
There is no separate highlight icon. A highlight is one concrete, source-backed
positive draw, preserving names, roles and conditions; omit unsupported claims.
Urgency is only a registration deadline or scarcity observation, not proximity
to the event. Show it last and do not imply live remaining-seat counts.

Whole cards open the original listing. A **Waitlist** label describes source
availability, not personal registration. There are no personal-status,
source-label, registration or detail-page controls. The web implements this
contract; Penpot and production must be verified separately when updated.

## Desktop adaptation

At 1024 CSS pixels and above, discovery uses a wide agenda and a right-hand
month calendar under a shallow skyline banner. Below that width the mobile
calendar and sheet layout remain. Both use one calendar controller, event
renderer and filter state. Desktop month arrows browse without selecting;
the month chooser selects the first day, as on mobile. Desktop expansion does
not overwrite the mobile fold preference. Resizing and source/Account returns
preserve the visible event and selected date; only navigation state is stored
in the tab, never catalog data or credentials.

Desktop card titles use the existing 22px title role; facts and highlights keep
the 15px supporting role. Account and AI authorization use a 560px readable
column; dialogs are centered. Public entry uses the same cards, a 48px hero
title, a horizontal invitation form and an inline receipt. The decorative
skyline is the approved generated asset, served locally as a compressed WebP.
