# CAMPFIRE — Claude Design Brief (Direction 2: Ember & Dusk)

> This is the **committed** brand brief to seed Claude Design. Unlike
> `design-system.md` (which explores options), everything here is stated as decided so
> generated UI stays consistent. Values are *directional* — final hex must pass an AA
> contrast pass before shipping to code.

## Product
**CAMPFIRE** — *COSMOS Archive of MultiPle-Field Internal Reductions & Extractions.* A web
portal where professional astronomers browse, inspect, and quality-assess JWST NIRSpec/
NIRCam data reductions. Expert users, data-dense work, trust-critical scientific judgments.

## Personality
**Subtle warmth, felt not noticed.** Calm, precise, quietly confident scientific instrument.
Warmth lives in neutral *temperature* and craft, never in literal flame/campfire imagery.
Lean slightly warm of clinical; dense over airy; serious but not cold; restrained — the data
is the hero.

## Principles
1. Data is the hero; chrome recedes. 2. Legible at density. 3. Calm, not cold.
4. Earned color — accent/status mean something. 5. Dark mode is first-class (designed, not
inverted). 6. Accessible by default (WCAG AA, visible focus, never color-only).

## Typography
- **UI / body:** Inter (400/500/600/700)
- **Data / code / all numbers:** JetBrains Mono (400/500), tabular figures — RA/Dec,
  redshift, S/N, wavelengths are ALWAYS mono.
- **Scale (rem@16):** xs .75 · sm .875 · base 1 · lg 1.125 · xl 1.25 · 2xl 1.5 · 3xl 1.875.
  Body line-height 1.55; dense table rows ~1.35. Headings letter-spacing ≈ -.02em.

## Color — LIGHT (warm paper)
| Token | Hex | Role |
|---|---|---|
| background | `#faf7f3` | page |
| card | `#ffffff` | primary surface (cards pop off the paper) |
| card-2 | `#f6f1ea` | table header / footer / inset |
| card-hover | `#f4eee6` | row & control hover |
| border | `#ece4d9` | hairlines |
| text | `#211c17` | body / headings |
| muted | `#736a5f` | labels, meta |
| primary (ember) | `#d9480f` | default accent |
| primary-hover | `#e8590c` | accent hover |
| primary-soft | `rgba(217,72,15,.09)` | active chip / selected bg |
| ok / warn / bad | `#4d7c0f` / `#b45309` / `#be123c` | quality: secure / tentative / uncertain |

## Color — DARK ("dusk": plum/indigo night, not cold navy)
| Token | Hex | Role |
|---|---|---|
| background | `#16131c` | page |
| card | `#1f1b27` | surface |
| card-2 | `#241f2e` | header / footer / inset |
| card-hover | `#2a2435` | hover |
| border | `#332c40` | hairlines |
| text | `#f1ecf6` | body |
| muted | `#a79db4` | labels, meta |
| primary (ember) | `#fb923c` | accent (brightened for dark) |
| primary-hover | `#fdba74` | accent hover |
| primary-soft | `rgba(251,146,60,.13)` | active bg |
| ok / warn / bad | `#a3e635` / `#fbbf24` / `#fb7185` | quality |

**Accent system:** ember is the *default*, but keep all user-selectable accents (the
existing 8: magenta, blue, emerald, red, orange, violet, cyan, lime). Only `--primary` /
`--primary-hover` swap; everything else is neutral so any accent works.

## Shape, space, motion
- Radius: cards 16px, controls/buttons/tabs 10–11px, filter chips fully rounded (pill).
- Elevation: soft layered shadow on cards/popovers; in dark mode lean on border + lighter
  surface over heavy shadow. Subtle accent *glow* (`0 0 0 3px primary-soft`) on active.
- Spacing: tight, consistent rhythm (4/6/8/12/16/24). Chrome gets a touch more air than
  today; tables stay dense.
- Motion: 120–200ms ease-out; fade/zoom only. No bounce. Respect `prefers-reduced-motion`.

## Components & conventions (keep these)
Pill filter chips (active = `primary-soft` bg + `primary` border + glow); underline active
tabs (`primary`); hairline-bordered cards; uppercase tracked micro-labels for metadata;
visible focus rings everywhere; accent used ONLY on active/selected state; lucide icons,
consistent stroke. Buttons: primary / secondary / ghost × sm / md / lg.

## Voice (UI copy)
Plain, precise, scientist-to-scientist. Exact terms ("redshift", "S/N", "NIRSpec G140M")
over friendly paraphrase. No exclamation marks in chrome. Empty/error states quietly human,
never cute.

## Canonical reference
The Targets browse mockup (`docs/design-mockups/direction-2-ember-dusk.html`) — light AND
dark — is the visual ground truth. Upload screenshots of both as brand reference images.

## Out of scope / do NOT
Literal campfire/flame motifs beyond the existing small logo mark; cool blue-slate
neutrals; decorative gradients on content; playful/marketing tone; color-only status.
