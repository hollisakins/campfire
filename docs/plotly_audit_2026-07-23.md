# Plotly Integration Audit — 2026-07-23

Scope: the interactive (Plotly-based) plotting stack in `web/`:

| File | Lines | Role |
|---|---|---|
| `components/spectra/SpectrumPlot.tsx` | 902 | 2D heatmap + 1D spectrum + profile + rest-frame axis + model/χ² panel; used by `SpectrumDetailCard` and `InspectionModeOverlay` |
| `components/spectra/MultiSpectrumViewer.tsx` | 425 | Section-1 comparison plot on the unified object page |
| `components/spectra/RedshiftFitPlot.tsx` | 407 | **Dead code — zero importers** |
| `components/spectra/PhotometrySED.tsx` | 423 | SED + P(z) panels |
| `components/spectra/plotting-utils.ts` | 403 | Shared helpers (line list, y-range, rest ticks, colors) |
| `components/spectra/PlottingControls.tsx` | 184 | Shared control widgets |

Consumers reviewed: `UnifiedObjectPage`, `SpectrumDetailCard`, `InspectionModeOverlay`, `PreferencesContext`, `ThemeContext`, `useSpectrumDataCache`, `/api/spectrum` route.

The two reported symptoms — flaky rest-frame tick positions on zoom, and toggles that break depending on other toggles — both trace to identifiable defects below. The "bloat" impression is also justified: one entire component is unreferenced, the same three features are implemented two or three times each with drifted behavior, and the client ships the full ~4.5 MB plotly.js bundle for two trace types.

---

## 1. Bugs matching the reported symptoms

### 1.1 Rest-frame tick weirdness on zoom (SpectrumPlot `xaxis3`, MultiSpectrumViewer `xaxis2`)

The rest-frame Å axis is a relabeled overlay (`overlaying: 'x'`, `matches: 'x'`, `tickmode: 'array'`) whose `tickvals` are recomputed in React from a zoom range captured via `onRelayout`. This design has four independent failure modes, and it exists **twice** with different semantics:

1. **`matches` vs. per-axis `uirevision` conflict** (`SpectrumPlot.tsx:492-528`). `xaxis` carries `uirevision: 'constant'` (zoom preserved); `xaxis3` deliberately carries none so new tickvals apply (the code comment admits this is a workaround for stale tick caching). Consequence: *every* unrelated state change (color-scale min/max edit, emission-line toggle, redshift slider tick, theme flip) runs `Plotly.react`, which resets `xaxis3` while `xaxis` restores the user's zoom from `uirevision`. Reconciling a `matches` group where one member was reset and the other restored is unspecified Plotly behavior — this is the intermittent tick misplacement.

2. **One-frame stale ticks by construction.** Zoom → Plotly paints immediately with the *old* `tickvals` → `onRelayout` → `setObsRange` → second render with correct ticks. Any dropped or unrecognized relayout event leaves the axis permanently stale (see 3).

3. **Event parsing differs between the two copies.** `SpectrumPlot.handleRelayout` (`:678-699`) handles `xaxis.range[0]/[1]`, array-form `xaxis.range`, and `xaxis.autorange`. `MultiSpectrumViewer.handleRelayout` (`:342-356`) omits the array form, and on autorange it recomputes a range from data instead of using a "full range" sentinel — a third semantic for the same state.

4. **Axis appears/disappears with the redshift slider** (`MultiSpectrumViewer.tsx:309-335`). The overlay axis, its invisible activation trace, and 20 px of top margin are all conditional on `redshift > 0 && restTicks.length > 0`. Dragging the slider across 0 adds/removes layout axes mid-interaction — another documented source of tick/layout artifacts in `Plotly.react`.

Additional inconsistencies in the same feature: tick labels are `"6500 Å"` in SpectrumPlot vs. bare `"6500"` + axis title in MultiSpectrumViewer; SpectrumPlot's `obsRange` is `null`-means-full and reset on `fitsPath` change, MultiSpectrumViewer eagerly initializes `observedRange` from data in a separate effect (`:359-368`).

**Recommendation:** one shared implementation (e.g. a `useRestFrameAxis(gdRef, redshift)` hook). Two robust shapes:
- Keep the declarative approach but make it single-sourced: one relayout parser handling all event forms, one null-means-full convention, axis always present (harmless at z=0), and drive tick updates via `Plotly.relayout(gd, {'xaxis3.tickvals': …, 'xaxis3.ticktext': …})` inside the relayout handler so the ticks update in the same frame as the zoom instead of round-tripping through React state and a full `Plotly.react`.
- Or give `xaxis3` the *same* `uirevision` as `xaxis` and bump both together (e.g. `uirevision: `x-${redshift}``) so zoom is preserved except when redshift changes — eliminating the reset/restore mismatch inside the `matches` group.

### 1.2 Stale y-range after flux-unit toggle (all three spectrum plots)

`yaxis.uirevision` never encodes `fluxUnit`:

- `SpectrumPlot.tsx:551` — `uirevision: autoStretch ? 'y-auto' : 'y-full'`
- `MultiSpectrumViewer.tsx:288-296` — `uirevision: 'constant'`
- `RedshiftFitPlot.tsx:283` — top-level `uirevision: 'constant'` (dead code, same bug)

If the user has zoomed/panned the y-axis and then toggles fν ↔ fλ, `uirevision` preserves the user's numeric y-range — which is now ~19 orders of magnitude off (μJy vs. erg/s/cm²/Å). The spectrum renders as a flat line/blank until a double-click reset. This is the most likely root of "broken functionality depending on which toggles are enabled."

**Fix:** include the unit in the revision key, e.g. `uirevision: `${fluxUnit}-${autoStretch}``.

### 1.3 Emission-lines toggle is dead at z = 0 in MultiSpectrumViewer

`MultiSpectrumViewer.tsx:228` gates line traces on `showEmissionLines && redshift > 0`. For objects with no catalog redshift (`initialRedshift` null → `redshift` 0), checking "Emission lines" does nothing until the slider moves — while `SpectrumPlot` happily renders lines at z=0. The toggle appears broken. The rest-frame axis is gated the same way (`:263`, `:309`). Remove the `> 0` gates (z=0 is a perfectly valid rest frame).

### 1.4 Auto-y range tracks the model even when the Model toggle is off

`SpectrumPlot.tsx:453-457` passes `modelFlux`/`modelWave` into `computeYRange` unconditionally; those are non-null whenever `/api/redshift-fit` returned data (fetched in *all* modes, `:206-224`). So with "Model" unchecked:
- the y-range is computed from an invisible model (Path A in `computeYRange`) rather than from the data;
- toggling Model on/off never rescales despite adding/removing the dominant orange trace;
- the same spectrum auto-stretches differently depending on whether the fit sidecar happened to exist (404 → data-driven Path B).

**Fix:** pass model arrays only when `showModel` is true (or when in inspection mode, if the intent is "scale to features"). Either way, make it deterministic w.r.t. what is drawn.

### 1.5 Comparison-plot zoom leaks across object navigation

`UnifiedObjectPage` stays mounted while navigating between objects (state resets via the `prevObjectIdRef` effect, `:201-211`), and `MultiSpectrumViewer` is not keyed on the object. With `uirevision: 'constant'` on both axes, the previous object's zoom/pan is preserved onto the next object's completely different wavelength/flux ranges. `SpectrumDetailCard` plots escape this only because cards collapse (unmount) on navigation. **Fix:** key the viewer's `uirevision` on the object id (or `key` the component).

### 1.6 Emission lines participate in autoscale in RedshiftFitPlot (dead code, but the pattern matters)

`SpectrumPlot` solved this with a hidden fixed-range overlay `yaxis4` (`:574-585`) precisely so line traces "never affect auto-scaling or double-click reset". `MultiSpectrumViewer` re-implements the same overlay as `yaxis2`. `RedshiftFitPlot` + `createEmissionLineTraces` instead draw lines on the data axis spanning `[min·0.9, max·1.1]`, which *does* distort autoscale. Three implementations of one feature; the only user of the util does it the wrong way.

### 1.7 Minor state/UI mismatches

- Inspection mode defaults `showModel = true` (`SpectrumPlot.tsx:119`); when `fitData` is null the checkbox renders unchecked (`checked={showModel && !!fitData}`, `:830`) while state stays `true` — harmless today, but a trap for anything else reading `showModel`.
- The redshift slider is only rendered when "Emission lines" is on (`SpectrumPlot.tsx:854`, `MultiSpectrumViewer.tsx:387`), yet redshift also drives the always-visible rest-frame axis. With lines off there is no way to correct the redshift the top axis is using.
- `SpectrumPlot`'s fetch effect depends on `grating` (`:233`) which it never uses — a redundant refetch trigger.

---

## 2. React state-management assessment

### 2.1 Preference mirroring (SpectrumPlot)

`fluxUnit`, `colorscale`, `colorMin`, `colorMax` are copied from `spectrumPreferences` into local state (`:115-122`) plus a resync effect (`:137-142`). This is the classic derived-state duplication: any change to the preferences object identity (profile load, saving *any* preference from the Settings page in another tab/panel) silently clobbers in-plot overrides. In-plot changes are intentionally session-local (only `SettingsCard` writes back), but the current shape makes that contract implicit and fragile. Prefer: initialize once via `useState(() => prefs.x)` and drop the resync effect (remount on navigation already re-reads prefs), or add an explicit "session override" wrapper.

### 2.2 MultiSpectrumViewer's visibility model is dead and defeats its own design

`SpectrumSource.visible` exists so the viewer can keep *hidden* spectra loaded and hold the y-range stable ("Collect flux values from ALL loaded sources (not just visible)", `:142`). But `UnifiedObjectPage.tsx:155-169` pre-filters sources to visible ones and hardcodes `visible: true`. Result:

- every `s.visible` filter in the viewer is dead code;
- toggling a checkbox changes the `sources` array itself → the fetch effect re-runs, `loadedData` is rebuilt, the y-range *does* recompute — exactly what the "stable y-range" design was meant to avoid (and after any manual zoom, `uirevision: 'constant'` ignores the recomputed range anyway, so behavior differs before/after first zoom);
- the fetch effect keys on `sources` identity, so unrelated parent re-renders re-run it (cheap due to the `dataCache` ref, but it churns `loadedData` and the whole trace memo).

Pick one owner: either pass *all* spectra with real `visible` flags and let the viewer filter (restores the stable-y design), or delete the `visible` field and the all-loaded-sources comment.

### 2.3 y-range math on concatenated spectra

`MultiSpectrumViewer.tsx:255-257` feeds the concatenation of every spectrum into `computeYRange` with `edgeTrim: min(20, 2%)`. Edge-trimming a concatenated array only trims the first spectrum's start and last spectrum's end; the MAD/median is computed across gratings with different flux scales and the "edges" between spectra are untouched. Works by accident, not by design — compute per-spectrum ranges and merge, or trim per-source before concatenating.

### 2.4 Robustness nits

- `Math.min(...allWave)` / `Math.max(...allWave)` over the concatenation of all spectra (`MultiSpectrumViewer.tsx:224-225`, `:229-235`, `:353`) — argument spread over large arrays risks `RangeError: Maximum call stack size exceeded` once total points approach ~10⁵ (several medium-resolution gratings). Use a loop (SpectrumPlot already does, `:293-299`).
- `chi2Min * 0.9` inside `Math.log10` (`SpectrumPlot.tsx:662`) yields `-Infinity`/NaN if `chi2_min ≤ 0`.
- `parseFloat(e.target.value) || 0` on the color-scale inputs (`SpectrumPlot.tsx:769,778`) turns a cleared field into 0, momentarily flattening the heatmap contrast while typing a negative number.

---

## 3. Bloat & duplication inventory

### 3.1 Dead code

- **`RedshiftFitPlot.tsx` (407 lines) has zero importers.** Its role was absorbed by SpectrumPlot's Model toggle + χ² panel. `createEmissionLineTraces` and `getHoverLabel` in `plotting-utils.ts` are used *only* by it, so they are transitively dead. Delete all three (git history preserves them).
- `SpectrumSource.visible` (see 2.2).
- The array-form `xaxis.range` branch in `SpectrumPlot.handleRelayout` is speculative (no current Plotly interaction emits it with these configs) — fine to keep, but it should live in the one shared parser, not be present in one copy and missing in the other.

### 3.2 Same feature, N implementations

| Feature | Implementations |
|---|---|
| Emission-line traces | SpectrumPlot inline (`:460-482`), MultiSpectrumViewer inline (`:239-251`), `createEmissionLineTraces` (dead) — three, with different y-axis strategies |
| Rest-frame overlay axis | SpectrumPlot `xaxis3`, MultiSpectrumViewer `xaxis2` — different tick text, uirevision, gating, init semantics |
| χ²(z) panel | SpectrumPlot `chi2PlotData` (`:609-672`), RedshiftFitPlot bottom subplot (dead) — different best-fit colors (#f97316 vs #ef4444) |
| f_ν→f_λ conversion | `plotting-utils.convertToFlambda` + verbatim local copy in SpectrumPlot (`:185-187`) including the 9-line derivation comment duplicated |
| Flux/hover labels | `getFluxLabel`/`getHoverLabel` utils + inline ternaries in SpectrumPlot (`:279-280`) |
| Flux-unit toggle & emission-lines checkbox UI | `PlottingControls.tsx` components + re-implemented inline in SpectrumPlot (`:734-816`) with drifted styling (`bg-card` vs `bg-background` inactive state) |
| `FluxUnit` type | defined in both `lib/types.ts:27` and `plotting-utils.ts:6`; SpectrumPlot imports one, the other components import the other |
| Dynamic `Plot` component | four separate `dynamic(() => import('react-plotly.js'))` definitions with four slightly different spinners |
| Relayout parsing | two divergent copies (see 1.1) |

`COLORSCALE_OPTIONS` in SpectrumPlot also re-enumerates the `Colorscale2D` union by hand.

### 3.3 Bundle weight (biggest single win)

`react-plotly.js`'s default export bundles **all of plotly.js** (`plotly.js/dist/plotly` — ~4.5 MB minified, ~1.3 MB gzipped: WebGL traces, geo, 3D, finance, everything). The app uses exactly two trace types: `scatter` and `heatmap`. Switching to the factory pattern with a partial bundle cuts the plot chunk by ~70–80%:

```ts
// components/plot/LazyPlot.tsx  (the single shared dynamic Plot)
import dynamic from 'next/dynamic';
export const LazyPlot = dynamic(
  async () => {
    const [createPlotlyComponent, Plotly] = await Promise.all([
      import('react-plotly.js/factory'),
      import('plotly.js-cartesian-dist-min'),   // or a custom scatter+heatmap bundle
    ]);
    return createPlotlyComponent.default(Plotly.default);
  },
  { ssr: false, loading: PlotSkeleton }
);
```

This also collapses the four dynamic-import definitions into one place with one skeleton.

Note: `react-plotly.js` (2.6.0, 2022) is effectively unmaintained; it works with plotly.js v3 via the factory, but worth a periodic check — the component is thin enough to vendor if it ever breaks.

### 3.4 Typing

Traces/layouts are `any` (with eslint-disables) in SpectrumPlot, MultiSpectrumViewer, and RedshiftFitPlot; PhotometrySED uses `Plotly.Data`/`Partial<Plotly.Layout>` correctly. Use the typed forms everywhere — several of the bugs above (axis key typos, event shapes) are exactly what the types catch.

---

## 4. Performance notes

- **Redshift slider → full figure rebuild per input event.** `RedshiftSliderControl` fires `onChange` continuously while dragging; in SpectrumPlot every tick rebuilds the entire `plotData` memo (error-band spreads, step-function arrays, ~40 line traces) and runs `Plotly.react` on a figure containing the 2D heatmap. The inspection-mode debounce (`InspectionModeOverlay.tsx:357-362`) only debounces the *save*, not the plot. Mitigations, in order of value: split redshift-dependent traces (lines) from static traces (heatmap/spectrum/profile) so the memo churn is small; rAF-throttle the slider; the relayout-based tick update from 1.1 removes the layout churn too.
- `hovermode: 'x unified'` + up to ~40 emission-line traces makes the unified hover box balloon near line-dense regions (each dashed line contributes an entry). Consider `hoverinfo: 'skip'` on line traces (the legend + top axis already identify them) or a spike-line approach.
- SpectrumPlot fetches `/api/redshift-fit` for every card expansion even though the fit is only rendered when Model is toggled (default off outside inspection). Lazy-fetch on first toggle would halve the requests on the unified page.
- The error band rebuilds `[...wave, ...reversed]` arrays on every memo run; cheap relative to the above but free to hoist into `processedData`.

---

## 5. Prioritized remediation plan

1. **Delete `RedshiftFitPlot.tsx`** + `createEmissionLineTraces` + `getHoverLabel` (pure win, removes one full copy of every bug). ✅ *implemented*
2. **Shared `LazyPlot` + partial plotly bundle** (§3.3) — one small change, ~70% cut to the plot-page JS. ✅ *implemented (`components/plot/LazyPlot.tsx`, plot chunk 4.7 MB → 1.4 MB; `getHoverLabel` kept, reused by SpectrumPlot)*
3. **Fix the toggle bugs**: `uirevision` keyed on `fluxUnit` (1.2); drop `redshift > 0` gates (1.3); gate model input to `computeYRange` on `showModel` (1.4); key MultiSpectrumViewer uirevision on object id (1.5). ✅ *implemented (viewer keyed via React `key` on object id)*
4. **Extract one rest-frame-axis + relayout implementation** (1.1) used by both plots. ✅ *implemented declaratively: `buildRestFrameAxis` keys the overlay axis's uirevision to its tick content, so it is no longer reset on unrelated re-renders (the reset/restore mismatch inside the `matches` group) but still re-applies ticks on zoom/redshift; one `parseXRangeFromRelayout` handles all event shapes. The same-frame `Plotly.relayout` variant remains available if the one-frame declarative lag proves visible.*
5. **Unify emission-line rendering** on the hidden-overlay-axis pattern via a single `buildEmissionLineTraces` util. ✅ *implemented (+ `buildEmissionLineOverlayAxis`)*
6. **Resolve the visibility-model ownership** in MultiSpectrumViewer (2.2) and fix the concatenated y-range math (2.3). ✅ *implemented: UnifiedObjectPage passes all spectra with real `visible` flags; y-range is per-source, merged, over all loaded sources*
7. **Deduplicate**: single `FluxUnit` type, delete SpectrumPlot's local `convertToFlambda`/label ternaries/inline controls in favor of `plotting-utils` + `PlottingControls`. ✅ *implemented (+ shared `PlotCheckbox`, `COLORSCALE_2D_OPTIONS`)*
8. Typed traces/layouts; loop-based min/max; the robustness nits in 2.4. ◐ *partial: loop-based min/max, χ² log-range guard, and a stuck-loading fix landed; trace/layout typing and redshift-slider throttling (§4) remain open*

Manual testing still recommended for item 4 (zoom, pan, double-click, slider, toggles, object navigation) in both plots — the mechanism changed even though behavior should only improve.
