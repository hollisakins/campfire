# CAMPFIRE — Design System

> **Status: committed.** This is the single source of truth for CAMPFIRE's visual language.
> The brand direction is decided (Direction 2 — *Ember & Dusk*); the values below are the
> implemented target. All chrome color/contrast values are **WCAG AA verified** (see the
> contrast notes per token). Companion docs: `docs/token-migration-plan.md` (how we get the
> codebase there) and `docs/design-mockups/` (visual reference).

---

## 1. Product
**CAMPFIRE** — *COSMOS Archive of MultiPle-Field Internal Reductions & Extractions.* A web
portal where professional astronomers browse, inspect, and quality-assess JWST NIRSpec/
NIRCam reductions. Expert users, data-dense work, trust-critical scientific judgments.

## 2. Personality — *subtle warmth, felt not noticed*
The product should feel calm, precise, and quietly confident. Warmth lives in the
*temperature* of the neutrals and in craft, **never** in literal flame/campfire imagery.
Lean slightly warm of clinical; dense over airy; serious but not cold; restrained — the
data is the hero.

## 3. Principles
1. Data is the hero; chrome recedes. 2. Legible at density. 3. Calm, not cold. 4. Earned
color — accent and status mean something. 5. Dark mode is first-class (designed, not
inverted). 6. Accessible by default (WCAG AA, visible focus, never color-only).

## 4. Decision log
- **Chosen:** Direction 2 — *Ember & Dusk*. Warm-paper light mode, plum-tinted "dusk" dark
  mode, ember accent, Inter + JetBrains Mono.
- **Rejected:** D1 *Warm Clinical* (too timid — keeps generic Roboto); D3 *Observatory*
  (serif display too expressive for a dense data tool — but its serif-for-object-names idea
  is parked for possible later use on detail headers).

---

## 5. Color — semantic tokens (the contract)

Components consume **these tokens only** for chrome. Every token carries a light and a dark
("dusk") value so a single class is correct in both modes — migrating to tokens *removes*
the manual `dark:` pairs. Source of truth: `web/app/globals.css`, exposed via
`web/tailwind.config.ts`.

### Surfaces
**Elevation moves *toward the ink color* in both themes** — surfaces lift off the page by
getting *lighter* in dusk and *warmer/darker* in light. The near-white `--background` is the
legible base, reused for plot wells and inputs; the warm tint lives in the cards (mirroring the
dusk panels). `--surface-2` is the most-elevated surface in both modes (consistent semantics).

| Token | Light | Dark (dusk) | Role |
|---|---|---|---|
| `--background` | `#fbf9f6` | `#16131c` | near-white page; legible base for plot data wells & inputs |
| `--card` | `#f8f4ef` | `#1f1b27` | primary surface — warm panels; also plot paper/edges |
| `--surface-2` | `#f3ede5` | `#241f2e` | card/section headers, insets (most elevated) |
| `--table-header` | `#f6f0ea` | `#221d2a` | table column headers — independent, sits between card & surface-2 |
| `--card-hover` | `#ede6db` | `#2a2435` | row & control hover |

### Borders
| Token | Light | Dark | Role |
|---|---|---|---|
| `--border` | `#e6ddce` | `#332c40` | hairlines, dividers |
| `--border-strong` | `#d7cbb9` | `#423a52` | emphasized edges, inputs |

### Text  *(AA verified on `--card`, the worst-case chrome surface)*
| Token | Light | Dark | Contrast L/D | Role |
|---|---|---|---|---|
| `--text-primary` | `#211c17` | `#f1ecf6` | 15.4 / 15.8 | body, headings |
| `--text-secondary` | `#5c5346` | `#b8aec6` | 6.9 / 8.7 | labels, meta |
| `--text-tertiary` | `#6c6150` | `#8b8398` | 5.5 / 5.1 | muted, placeholder, icons |

### Accent — ember  *(all 8 user accents still selectable; ember is the default)*
| Token | Light | Dark | Notes |
|---|---|---|---|
| `--primary` | `#c63f0c` | `#fb923c` | brand accent; AA as text (4.8/8.1) & as fill w/ white (5.1) |
| `--primary-hover` | `#ad3408` | `#fdba74` | hover |
| `--primary-text` | `#b8390a` | `#fb923c` | ember used as inline text/links (AA 5.4) |
| `--on-primary` | `#ffffff` | `#1a1206` | text/icon ON a primary fill |
| `--primary-soft` | `rgba(198,63,12,.10)` | `rgba(251,146,60,.14)` | active-chip / selected bg |

> The Direction-2 mockup used `#d9480f`; the implemented `--primary` is nudged one
> imperceptible step deeper to `#c63f0c` so white button labels and inline links clear AA.
> **Accent system:** the user's accent choice swaps `--primary`/`--primary-hover` (see
> `applyAccentColorCSS`); `--primary-text`/`--on-primary`/`--primary-soft` currently track the
> ember default and are *not* yet recomputed per accent — a known follow-up for non-ember
> picks. `ember` is now a dedicated entry in `web/lib/types.ts` and the default; the prior
> 8 accents remain selectable.

### Status — semantic, both modes  *(meaning, not decoration)*
| Token | Light | Dark | Use |
|---|---|---|---|
| `--success` | `#4d7c0f` | `#a3e635` | secure / success |
| `--warning` | `#b45309` | `#fbbf24` | tentative / caution |
| `--danger` | `#be123c` | `#fb7185` | uncertain / destructive |
| `--info` | `#1d4ed8` | `#93c5fd` | informational / links-as-info |

> **Not theme color (do not tokenize as chrome):** spectral emission-line colors
> (`plotting-utils.ts`), wavelength-domain SED colors (`PhotometrySED.tsx`), spectrum model/
> profile overlays (`SpectrumPlot.tsx`), redshift-quality + DQ flag colors (`lib/flags.ts`),
> map markers, the list color picker. These are **scientific encodings** — out of scope for
> the brand system. (They may later move to a separate `--viz-*` namespace; not now.)

---

## 6. Typography
- **UI / body:** **Inter** (variable), loaded via `next/font` as `--font-sans`, used through
  `font-sans` (Tailwind). The CSS var name is font-agnostic so swaps don't touch components.
- **Data / code / all numbers:** **JetBrains Mono**, `--font-mono` / `font-mono`, tabular figures.
  RA/Dec, redshift, S/N, wavelengths are **always** mono.
- **Scale (rem @16):** `xs .75 · sm .875 · base 1 · lg 1.125 · xl 1.25 · 2xl 1.5 · 3xl 1.875`.
  Body line-height 1.55; dense table rows ~1.35. Heading letter-spacing ≈ −0.02em.

## 7. Shape · space · elevation · motion
- **Radius:** cards `1rem` (`rounded-card`), controls/buttons/tabs `0.625rem`, filter chips
  pill (`rounded-full`).
- **Spacing:** tight, consistent rhythm `4/6/8/12/16/24`. Chrome gets slightly more air than
  today; tables stay dense.
- **Elevation:** soft layered shadow on cards/popovers; in dark mode prefer border + lighter
  surface over heavy shadow. Subtle accent glow (`0 0 0 3px var(--primary-soft)`) on active.
- **Motion:** 120–200ms ease-out; fade/zoom only, no bounce. Respect `prefers-reduced-motion`.

## 8. Components & conventions (preserve)
Custom components on Tailwind (no shadcn), in `web/components/ui/`. Keep: pill filter chips
(active = `primary-soft` bg + `primary` border + glow); underline active tabs (`primary`);
hairline-bordered cards; uppercase tracked micro-labels for metadata; visible focus rings
everywhere; **accent only on active/selected state**; lucide icons w/ consistent stroke.
Buttons: `primary | secondary | ghost` × `sm | md | lg`.

## 9. The token contract (rules for all new & migrated code)
1. **Chrome uses semantic tokens only** — `bg-card`, `text-text-secondary`, `border-border`,
   etc. **Never** raw `slate-*` / `gray-*` / hex for chrome.
2. **One class, both modes.** Tokens carry light + dusk values; do not add `dark:` color
   variants for chrome (delete them on migration).
3. **Status → status tokens** (`--success/--warning/--danger/--info`), not raw `red-*` etc.
4. **Scientific/data colors are exempt** (§5 list) and live in their own files; never reused
   as chrome.
5. **New color?** Add a semantic token (light + dusk) to `globals.css` + `tailwind.config.ts`
   — don't inline a hex in a component.
6. A lint rule enforces 1 & 3 (see migration plan §Guardrail).

## 10. Accessibility
WCAG AA for text & meaningful UI in both themes (table §5 — re-verify if values change);
visible focus rings; never color-only signaling (quality also uses label/shape); respect
`prefers-reduced-motion`; hit targets ≥ ~32px even when dense.

---
*Implementation status & how the codebase reaches this system:* see
`docs/token-migration-plan.md`.
