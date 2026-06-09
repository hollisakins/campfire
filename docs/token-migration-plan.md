# CAMPFIRE — Token Migration Plan

> One-time refactor to put the codebase on the semantic-token contract in
> `design-system.md`, so future design iteration is a values change, not a hunt-and-replace.
> **Companion:** `design-system.md` (the target system + token table).

## Why full migration (not a palette override)
The neutral chrome is ~96% one Tailwind palette: **`slate` 1,957 usages, `gray` 182**,
`zinc/neutral/stone` 0 (measured). Tempting to just redefine the `slate` ramp — but the ramp
is **overloaded across modes**: e.g. `slate-700` is *body text* in light mode and a *dark
surface* in dark mode. One ramp can't be both warm-brown text and plum-dusk surface. Only
**semantic tokens**, with independent light/dusk values per role, resolve this. Hence: migrate.

## Strategy: refactor first, restyle second
Separate the invisible refactor from the visible restyle so each is trivially reviewable.

| Phase | What | Visual effect | Review |
|---|---|---|---|
| **1 — Vocabulary** ✅ | Add all new tokens (`--surface-2`, `--border-strong`, `--text-tertiary`, `--primary-text`, `--on-primary`, `--primary-soft`, `--success/--warning/--danger/--info`) to `globals.css` + `tailwind.config.ts`, **set to today's cool values** | none | tiny |
| **2 — Migrate** ✅ | Replace raw `slate-*`/`gray-*`/`white`/`black` chrome usages → tokens, directory by directory; **delete now-redundant `dark:` color pairs** | **none** (no-op) — tokens still hold old values | easy: diff should show no visual change |
| **3 — Restyle** ✅ | Flip token *values* to Direction-2 warm/dusk; swap fonts (Inter + JetBrains Mono); set ember default accent | the whole new look, at once | small, high-impact, easy to revert |
| **4 — Guardrail** | Lint rule + docs so it stays clean | none | tiny |

> **Status:** Phases 1–2 merged (#166). Phase 3 is this branch (`design/direction-2-restyle`):
> token values flipped to warm/dusk in `globals.css`, fonts → Inter + JetBrains Mono in
> `layout.tsx` (CSS vars renamed to font-agnostic `--font-sans`/`--font-mono`), plot chart
> chrome (`--plot-*`) and `SpectrumPlot` axis font follow the theme, and an `ember` accent
> added + defaulted in `lib/types.ts`. Phase 4 (lint guardrail) is the remaining follow-up.

This is the professional sequence: Phase 2 is a large diff but a visual no-op (low risk);
Phase 3 is a small diff but the entire restyle (high impact, easy rollback).

## Scope

**In scope (migrate to tokens):** all chrome in `web/components/**`, `web/app/**`,
`web/lib/**` — backgrounds, surfaces, borders, text, hover, focus, dividers, and *UI* status
colors.

**Out of scope (leave as-is — scientific/data encodings):**
- `components/spectra/plotting-utils.ts` — `EMISSION_LINES` (49 line colors)
- `components/spectra/PhotometrySED.tsx` — wavelength-domain colors
- `components/spectra/SpectrumPlot.tsx` — model/profile overlay colors (keep accent-derived ones)
- `lib/flags.ts` — `REDSHIFT_QUALITY`, `DQ_FLAGS` colors
- `components/map/*` marker colors, `components/lists/ListColorPicker.tsx`

Classify each raw color before migrating: **chrome** → token; **UI status** → status token;
**scientific/data** → leave. When unsure, it's chrome.

## Mapping table (property × shade → token)
Mechanical for most usages. Keyed on the CSS property, because the same shade means different
things as bg vs text vs border. `dark:` counterparts collapse into the single token.

**Surfaces — `bg-*`**
| Raw (light / its dark pair) | → Token |
|---|---|
| `bg-white`, `bg-slate-50` / `dark:bg-slate-900` | `bg-background` |
| `bg-slate-50/100` cards / `dark:bg-slate-800` | `bg-card` |
| `bg-slate-100/200` headers, insets / `dark:bg-slate-800/700` | `bg-surface-2` |
| `bg-slate-100` hover / `dark:bg-slate-700` | `bg-card-hover` |

**Borders — `border-*`, `divide-*`, `ring-*`**
| `border-slate-200` / `dark:border-slate-700` | `border-border` |
| `border-slate-300` / `dark:border-slate-600` | `border-border-strong` |

**Text — `text-*`**
| `text-slate-900/800`, `text-black` / `dark:text-slate-100` | `text-text-primary` |
| `text-slate-600/500` / `dark:text-slate-300/400` | `text-text-secondary` |
| `text-slate-400` (placeholder/icons) / `dark:text-slate-500` | `text-text-tertiary` |

**UI status (only when it's status, not data)**
| `text/bg-green-*` (success) | `*-success` |
| `text/bg-amber/yellow-*` (caution) | `*-warning` |
| `text/bg-red-*` (error/destructive) | `*-danger` |
| `text/bg-blue-*` (info/link) | `*-info` or `*-primary` (if it's an action) |

**Disambiguation rules**
- `slate-600` text: secondary by default; promote to `text-primary` only if it's a heading/
  emphasized label.
- A `bg-slate-50 dark:bg-slate-900` **pair** → one `bg-background` (drop the `dark:`).
- `white`/`black` on an accent (e.g. button label) → `text-on-primary`, not `text-text-*`.

## Execution order (most leverage first)
1. **`components/ui/`** — foundational primitives (Button, Card, Badge, Tabs, chips…). Once
   these are tokenized, everything that composes them inherits correct theming. *(~149 raw)*
2. **`components/layout/`** — Navigation, Footer, headers *(nav has 12 white/black + 16 raw)*.
3. **`components/spectra/`** — largest surface *(488 raw across 49 files)*; do sub-batches,
   skipping the scientific-color files above.
4. **`metadata/`, `lists/`, `docs/`, `settings/`, `map/`, `auth/`, `app/**`** — remaining.
5. **`app/prototype/**`** — last / optional (throwaway prototype pages, 72+37 raw).

**Per-file checklist:** classify each color → apply mapping → delete redundant `dark:` pairs
→ `npm run build` → eyeball the screen in **both** themes. Batch ≈ one directory per PR.

> Tooling: this is a good fit for an agent-per-file or a `Workflow` pipeline using the mapping
> table as the spec (transform → build-check → visual diff), but each file still needs the
> chrome-vs-status-vs-data judgment — don't blind-sed.

## Accent decision (do in Phase 3)
Ember `#c63f0c` isn't one of the current 8 accents (closest is `orange #ea580c`). Either:
(a) **add** an `ember` entry to `ACCENT_COLORS` and set `DEFAULT_ACCENT_COLOR = 'ember'`, or
(b) **retune** `orange` to the ember values and default to it. Recommend (a) — keeps the
existing orange option intact.

## Verification (definition of done)
- `cd web && npm run build` passes.
- `rg -o '[a-z-]*slate-[0-9]+' components app lib | wc -l` trends to ~0 (remaining hits are
  inside the out-of-scope scientific files only).
- No `dark:(bg|text|border)-(slate|gray)-*` chrome variants remain.
- Key screens screenshotted light **and** dark (Targets list, object/spectrum page, settings,
  an admin table) — no regressions in Phase 2; intended new look in Phase 3.
- Contrast spot-check matches the AA table in `design-system.md`.

## Guardrail (Phase 4)
Add an ESLint rule (e.g. `eslint-plugin-tailwindcss` `no-restricted` or a custom
`no-restricted-syntax` on className literals) banning `slate-`/`gray-` and raw hex in
`className`, with an allowlist for the out-of-scope scientific files. Prevents regression and
makes the contract self-enforcing.

## Rough sizing
217 files touched, but heavily front-loaded: `ui/` + `layout/` deliver most of the visible
win. Phases 1, 3, 4 are each small/hours. Phase 2 is the bulk — parallelizable by directory,
and the app stays shippable throughout because tokens hold sensible values at every step.
