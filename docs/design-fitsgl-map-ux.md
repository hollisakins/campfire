# Design — FitsGL Map UX ("Phase 4.5")

**Status:** Design agreed with Hollis 2026-07-10; ready to build. This is a scoped
expansion of Phase 4 (#341) beyond the render+markers MVP — the full CAMPFIRE map
control surface, "cloud DS9" in CAMPFIRE aesthetics.

**Parent:** epic #337 (FitsGL integration). Companion to `docs/design-fitsgl-integration.md`.

**Wireframes** (open in a browser — self-contained HTML):
- `docs/wireframes/map-ux-refined.html` — **the target layout**.
- `docs/wireframes/map-ux-rgb-and-filters.html` — Display-panel RGB mode + the Filters slide-over.
- `docs/wireframes/map-ux-floating-pole.html`, `…-docked-pole.html` — the two poles we compared (floating won).

---

## 1. Where we are

Phase 4 landed a **minimal** FitsGL map surface on branch `feat/fitsgl-phase4-map`
(2 commits, not pushed): `web/components/map/FitsGLMapSurface.tsx` renders a deployed
pyramid in `<FitsViewer>` with object markers, a tiny control panel, cursor readout,
context menu, and URL sync; `MapViewer` dispatches per field (FitsGL if a `kind='field'`
`fitsgl_datasets` row exists, else Leaflet; `?engine=leaflet` forces Leaflet).

**Render-validated** against `rj0911/f444w/venus` (built with `campfire fitsgl build`,
served with `fitsgl serve`, local `fitsgl_datasets` row, admin login): imagery renders,
auto-stretch, native rotation, cursor RA/Dec, URL sync all confirmed.

This doc supersedes that MVP's chrome: `FitsGLMapSurface` grows into the full UI below.
The MVP commits stay as intermediate history; it all lands as the Phase 4 PR (we can
optionally ship the MVP first if we want an incremental cut — TBD).

---

## 2. Layout & principles

**The map is the hero.** Full-bleed `<FitsViewer>`; all chrome is **glass/blur panels
floating over it** (the aesthetic Hollis liked from the MVP mini-panel). Avoid the
FitsExplorer trap where four fixed edges + the CAMPFIRE header box the imagery into a
shrinking center well.

**Layout (from `map-ux-refined.html`):**
- **Band rail** — top-center glass pill. Merges the **field selector** + band chips
  (+ RGB entry). Shown only when a field has >1 band. No "FitsGL" badge.
- **Left tool rail** — docked flush-left glass column, split into *modal tools*
  (pan / ruler / fit) above a divider and *panel launchers* (Display / Layers / Filters /
  coord-search / export) below. **Collapsible** via a `‹` edge arrow.
- **Right control dock** — docked flush-right glass column holding the **Display** and
  **Layers** panels. **Collapsible** via a `›` edge arrow (map goes fully edge-to-edge
  when collapsed).
- **Status pill** — bottom-center glass pill.
- **NIRSpec Filters** — a **distinct full-height slide-over** (not a docked panel),
  because it's a different mode (querying the catalog vs. adjusting the view).

Principles: minimal nesting, minimal labels (icon tool rail + tooltips), progressive
disclosure (only the slim spine is always visible; panels collapse), group by function.

---

## 3. Locked design decisions (Hollis, 2026-07-10)

1. **The band rail stays simple; ALL RGB construction lives in the Display panel.**
   *(Revised 2026-07-13 — the map-RGB-defaults follow-up; originally RGB channel
   assignment lived in the rail.)* The rail is one chip per band + an `RGB` toggle:
   a band chip → single mode on that band; `RGB` → composite mode. The Display
   panel owns channel assignment and splits RGB into two sub-modes:
   **simple** (three band pickers, ONE shared limit range — no per-band stretch —
   a shared transfer curve, contrast + color-saturation sliders) and **trilogy**
   (FitsGL's weighted matrix: per-band R/G/B knobs + rainbow, plus
   noiselum / saturate % / noise σ / black σ sliders over precomputed stats).
   The dataset's `fitsgl.json` defaultView opens the map on the weighted trilogy
   composite when the producer shipped one.
2. **Field selector merged into the band rail**; drop the FitsGL badge.
3. **Colormap = a dropdown** (not a swatch row).
4. **North-up = always on, not exposed.** `config.northUp: true` always; no control.
   (Reverses the MVP's native-rotation default.)
5. **No object quality legend** ("secure/probable/…") in the Layers panel. Marker colors
   still apply; just no legend.
6. **Keep the FitsExplorer-style histogram fine-adjust** in the Display panel — rebuilt
   in CAMPFIRE chrome over the band's precomputed histogram.
7. **Reuse the existing NIRSpec filters** (`AdvancedFiltersPanel` slide-over + its RPCs).
   Visual refresh to glass only — do **not** rebuild the filter system.
8. **Keep the existing dual RA/Dec readout** (sexagesimal + decimal) and the **right-click
   context menu** — both are the already-reused `CoordinateOverlay` / `MapContextMenu`.

---

## 4. Component spec

### Band rail (top-center pill)
- `[<field> ▾] | [band ▾]` — two dropdowns, nothing else. `RGB` is the FIRST
  option of the band dropdown: picking it enters composite mode (tuned in the
  Display panel, revised decision 1); picking a band name → `{mode:'single', band}`.
- The band dropdown is hidden when the field has a single band.

### Display panel (docked-right, collapsible)
- **Single mode**: stretch chips (asinh / log / sqrt / linear), limit presets
  (auto / zscale / minmax / 99.5%), the histogram fine-adjust, colormap dropdown.
- **RGB mode**: a `simple | trilogy` sub-mode toggle (trilogy disabled without
  precomputed stats).
  - **simple**: three R/G/B band `<select>`s, stretch chips, limit presets +
    ONE shared histogram control (one `[min,max]` pushed to all three channels —
    deliberately no per-band stretch, so relative channel brightness stays
    physical), **contrast** (scales the shared limits about their midpoint) and
    **saturation** (the viewer's post-stretch composite uniform) sliders.
  - **trilogy**: campfire's `TrilogyWeights` matrix (per-band R/G/B weight
    `Knob`s + Rainbow; **minimized by default** to the participating bands, a
    single `+`/`−` button expands to the full co-gridded group with
    participation checkboxes) and `@fitsgl/core/react`'s `TrilogyControls`
    (noiselum / saturate % / noise σ / black σ) under the `fgl-embed` token
    class; levels derive live from each band's `stats.trilogy` + the knobs.
- **Histogram fine-adjust** (FitsExplorer-style): draw the band's `stats.histogram`
  (128 bins, `lo`/`hi`) with **draggable black/white handles**; live-drives the stretch.
- No north-up control (forced true).

### Layers panel (docked-right, collapsible)
- Toggles: **NIRSpec objects**, **MSA shutters** (Phase 4b), **Graticule (RA/Dec)**.
- No quality legend.

### Left tool rail (collapsible)
- Modal tools: **pan** (default), **ruler/measure**, **fit-to-image**.
- Panel launchers: **Display**, **Layers**, **Filters** (opens the slide-over),
  **coord search**, **export PNG**.

### Status pill (bottom-center)
- Dual **RA/Dec** (sexagesimal + decimal), **zoom**, **pixel value** (per active band),
  **band · stretch**. Grows to show separation/PA when the ruler tool is active.

### NIRSpec Filters slide-over
- The existing `AdvancedFiltersPanel`, restyled to the glass system. Same logic/RPCs.

### Context menu (reuse)
- The existing `MapContextMenu` (copy coords / copy link / search spectra near here),
  fed by the last cursor sky position (already wired in the MVP).

---

## 5. FitsGL ↔ CAMPFIRE division

**None of this needs a FitsGL change.** Everything is CAMPFIRE chrome driving the
existing engine via `config`, the ref handle, `getViewer()` (the `CoreViewer` escape
hatch), and the pure `explorer-state` helpers. Reuse the pure logic
(`deriveViewerConfig`, `defaultExplorerState`, `bandRailModel`, `rainbowAction`,
`trilogyComposite`, `isBandSelectableForRgb`, `canComposite`) rather than reinventing the
decision layer; rebuild only the React shell + styling.

| Feature | Where | API |
|---|---|---|
| Band single/RGB | CAMPFIRE → FitsGL | `config.view`; `deriveViewerConfig(bands, state)` |
| Rainbow / trilogy weights | CAMPFIRE → FitsGL | `rainbowAction`, `trilogyComposite` (pure) |
| Stretch mode | CAMPFIRE → FitsGL | `getViewer().setStretchMode()` |
| Black/white + histogram drag | CAMPFIRE → FitsGL | `getViewer().setStretch()` / `setChannelStretch()`; data = `band.stats.histogram` |
| zscale preset | CAMPFIRE → FitsGL | `band.stats.zscale` → `setStretch(z1,z2)` |
| Colormap | CAMPFIRE → FitsGL | `getViewer().setColormap()` (or `config.view.colormap`) |
| North-up (forced) | CAMPFIRE → FitsGL | `config.northUp: true` |
| Graticule | CAMPFIRE → FitsGL | via `getViewer()` — **confirm setter name** (exists in the engine / FitsExplorer) |
| Ruler / fit / export | CAMPFIRE → FitsGL | `handle.setTool()` (ruler exists), `fitToImage()`, `exportPNG()` |
| Object markers | CAMPFIRE | `handle.setMarkers()` (done) + `get_field_object_markers` |
| Cursor RA/Dec, URL sync | CAMPFIRE | `onCursor`/`onFrame` + `pixToSky` (done) |
| Right-click menu | CAMPFIRE | `MapContextMenu` (done) |
| Shutters | FitsGL primitive (shipped 0.2.0) + CAMPFIRE geometry | region overlay handle — **Phase 4b** |
| NIRSpec filters | CAMPFIRE | existing `AdvancedFiltersPanel` + RPCs |

**Two seams to confirm during build** (both exist in the engine — question is only the
exact call from a bare `<FitsViewer>` vs. FitsExplorer's own shell): the **graticule**
toggle and instantiating the **ruler** `PointerTool`. If either isn't cleanly reachable
via `handle`/`getViewer()`, it's a small ergonomic addition to the FitsGL React handle
(cross-repo, low-friction since we own it) — flag early, don't discover mid-build.

---

## 6. Verified data dependencies

The dataset `campfire fitsgl build` emits **already carries everything the UI needs** —
no producer change required (verified on `rj0911__venus/fitsgl.json`):
- per-band `stats.histogram` (128 bins + `lo`/`hi`) → the fine-adjust histogram,
- per-band `stats.zscale` → the zscale preset,
- per-band `stats.trilogy` + `pivotUm` → the trilogy rainbow blue→red ordering.

---

## 7. Build sequencing (one branch: `feat/fitsgl-phase4-map`)

1. **Band rail** — merge field select + band chips + RGB channel mode → `config.view`.
2. **Display panel** (docked, collapsible) — stretch chips + histogram fine-adjust +
   colormap dropdown; north-up forced true.
3. **Layers panel** — objects / shutters / graticule toggles (no legend).
4. **Tool rail** — modal tools + launchers + collapse arrows (`setTool`, `exportPNG`,
   `fitToImage`); dock/rail collapse behavior.
5. **Status pill** — dual RA/Dec + zoom + pixel value, bottom-center.
6. **Filters slide-over** — reuse `AdvancedFiltersPanel`, restyle to glass.
7. **(Phase 4b, separate)** — MSA shutters via the region primitive (#342).

Verify each chunk against the live `rj0911` render (`fitsgl serve` + local
`fitsgl_datasets` row + admin login on the worktree dev server). Suggested start:
**1–2** (band rail + Display panel), the core interactive surface.

---

## 8. Open items / defaults chosen

- **RGB entry interaction** — defaulted (RGB toggle → 3 pickers + rainbow); iterate on sight.
- **MVP-first vs. one PR** — TBD whether to ship the render+markers MVP as an incremental
  cut before the full UX, or land it all as one Phase 4 PR.
- **Narrow-screen behavior** — auto-collapse the rail/dock on small viewports (later).
- **Tool-rail icon set** — finalize icons during build.
- **Zoom URL param** — FitsGL zoom (float px/native) vs. Leaflet's int; a field is one
  engine so `z` is engine-scoped (acceptable; documented in the MVP).

---

## 9. References

- MVP: `web/components/map/FitsGLMapSurface.tsx`, dispatch in `MapViewer.tsx`,
  data layer in `web/lib/actions/map.ts` (`getFitsglDatasets`, `FitsglDataset`).
- Reusable pure logic: `@fitsgl/core/react` — `explorer-state` exports
  (`deriveViewerConfig`, `defaultExplorerState`, `bandRailModel`, `rainbowAction`,
  `trilogyComposite`, `isBandSelectableForRgb`, `canComposite`).
- Viewer API: `<FitsViewer>` props + `FitsViewerHandle` (`@fitsgl/core/react`),
  `getViewer()` → `CoreViewer` (`getWcs`, `setStretch*`, `setColormap`, `setNorthUp`, …),
  WCS helpers `pixToSky`/`skyToPix` (`@fitsgl/core`).
- Existing CAMPFIRE pieces to reuse: `CoordinateOverlay`, `MapContextMenu`,
  `AdvancedFiltersPanel`, `observation-colors.ts`, `MARKER_QUALITY_COLORS` (`lib/types.ts`).
- Local render-test recipe: `campfire fitsgl build --field rj0911 --tile venus` →
  `fitsgl serve $CAMPFIRE_ROOT/fitsgl/rj0911__venus -p 8765` → insert a `kind='field'`
  `fitsgl_datasets` row pointing at `http://localhost:8765/fitsgl.json` → dev server on
  a spare port → admin login → `/map?field=rj0911`. (Headless WebGL2 needs
  `--use-angle=swiftshader --enable-unsafe-swiftshader`.)
