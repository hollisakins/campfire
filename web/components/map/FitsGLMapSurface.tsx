'use client';

/**
 * FitsGL map surface (epic #337, Phase 4.5). Renders a deployed FitsGL tile-pyramid
 * dataset in `<FitsViewer>` as the map for a field that has one, replacing the
 * Leaflet + PNG-tile path (`MapViewer` dispatches to it per field). The full CAMPFIRE
 * "cloud DS9" control surface (`docs/design-fitsgl-map-ux.md`): the map is the hero,
 * full-bleed, with glass/blur chrome floating over it — a top-center band rail
 * (field + band/RGB) and a collapsible right dock (Display panel). Object markers go
 * through the viewer's ref handle (FitsGL owns culling/hit-test), and the shared
 * `CoordinateOverlay` / `MapContextMenu` are fed via callbacks. Shutters + the Layers
 * panel + tool rail + status pill land in later chunks; the NIRSpec Filters slide-over
 * reuses the existing `AdvancedFiltersPanel`.
 *
 * `onCursor`/`onFrame` are fixed at viewer construction, so everything they touch is
 * read through refs (never stale closures).
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import Link from 'next/link';
import {
  FitsViewer,
  deriveViewerConfig,
  explorerBandsFromConfig,
  defaultViewFromConfig,
  defaultExplorerState,
  loadFitsglConfig,
  type FitsViewerHandle,
  type ExplorerBand,
  type ExplorerState,
  type FitsglConfig,
} from '@fitsgl/core/react';
import {
  pixToSky,
  skyToPix,
  type ColormapName,
  type StretchMode,
  type CursorInfo,
  type MarkerEvent,
  type MarkerInput,
  type ViewerConfig,
  type ViewerFrameInfo,
} from '@fitsgl/core';
import type { FitsglDataset, MapObjectMarker } from '@/lib/actions/map';
import { MARKER_QUALITY_COLORS, QUALITY_LABELS } from '@/lib/types';
import { makeFitsglWorker } from '@/lib/fits/fitsglWorker';
import { BandRail } from './fitsgl/BandRail';
import { DisplayPanel } from './fitsgl/DisplayPanel';
import { LayersPanel } from './fitsgl/LayersPanel';
import { ToolRail } from './fitsgl/ToolRail';
import { StatusPill } from './fitsgl/StatusPill';
import { FitsglOverlays, type FitsglOverlaysHandle } from './fitsgl/FitsglOverlays';
import { useDisplayStretch, type ChannelKey } from './fitsgl/useDisplayStretch';
import { useColormap } from './fitsgl/useColormap';
import type { RulerMeasurement } from './fitsgl/ruler';
import { GLASS } from './fitsgl/glass';

/** Ruler/graticule colours drawn over the always-dark map well (theme-independent). */
const RULER_ACCENT = '#fb923c';
const GRID_LINE = 'rgba(148,163,184,0.35)';
const GRID_LABEL = 'rgba(203,213,225,0.8)';

interface FitsGLMapSurfaceProps {
  dataset: FitsglDataset;
  markers: MapObjectMarker[];
  showMarkers: boolean;
  highlightObjectId?: string;
  markerFilter?: (marker: MapObjectMarker) => boolean;
  initialCenter?: { ra: number; dec: number };
  initialZoom?: number;
  /** Field switcher (shared across the Leaflet + FitsGL surfaces). */
  fields: string[];
  selectedField: string;
  onFieldChange: (field: string) => void;
  onToggleMarkers: (visible: boolean) => void;
  markerCount: number;
  /** Live RA/Dec under the cursor → shared CoordinateOverlay (null on leave). */
  onCursorCoords: (coords: { ra: number; dec: number } | null) => void;
  /** Right-click at a sky position → shared MapContextMenu. */
  onContextMenu: (data: { coords: { ra: number; dec: number }; position: { x: number; y: number } }) => void;
  /** Opens the shared NIRSpec-filters slide-over (page-level AdvancedFiltersPanel). */
  onOpenFilters?: () => void;
  hasActiveFilters?: boolean;
}

function hexToRgba(hex: string, a: number): [number, number, number, number] {
  const h = hex.replace('#', '');
  return [
    parseInt(h.slice(0, 2), 16) / 255,
    parseInt(h.slice(2, 4), 16) / 255,
    parseInt(h.slice(4, 6), 16) / 255,
    a,
  ];
}

function updateMapUrl(params: Record<string, string | undefined>) {
  const url = new URL(window.location.href);
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined) url.searchParams.set(key, value);
    else url.searchParams.delete(key);
  }
  window.history.replaceState(null, '', url.toString());
}

/** Strict-3 rainbow: reddest→R, bluest→B, middle→G by pivot wavelength (falls back
 *  to declaration order when wavelengths are absent). The >3-band weighted-trilogy
 *  rainbow is a later refinement (needs the core trilogy-weight primitives). */
function rainbowRgb(bands: ExplorerBand[]): { r: string; g: string; b: string } | null {
  if (bands.length < 3) return null;
  const withWl = bands.filter((b) => b.wavelengthMicron != null);
  const pool = withWl.length >= 3
    ? [...withWl].sort((a, b) => a.wavelengthMicron! - b.wavelengthMicron!)
    : bands;
  return {
    b: pool[0].name,
    g: pool[Math.floor((pool.length - 1) / 2)].name,
    r: pool[pool.length - 1].name,
  };
}

export function FitsGLMapSurface({
  dataset,
  markers,
  showMarkers,
  highlightObjectId,
  markerFilter,
  initialCenter,
  initialZoom,
  fields,
  selectedField,
  onFieldChange,
  onToggleMarkers,
  markerCount,
  onCursorCoords,
  onContextMenu,
  onOpenFilters,
  hasActiveFilters,
}: FitsGLMapSurfaceProps) {
  const [config, setConfig] = useState<FitsglConfig | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [bands, setBands] = useState<ExplorerBand[]>([]);
  const [viewState, setViewState] = useState<ExplorerState | null>(null);
  const [dockOpen, setDockOpen] = useState(true);
  const [readyTick, setReadyTick] = useState(0);
  // Colormap is driven imperatively (reverse needs a LUT, which the controlled
  // config can't carry), so it lives outside `viewState` (whose `colormap` stays
  // pinned to 'gray' to keep deriveViewerConfig off the colormap path).
  const [colormapName, setColormapName] = useState<ColormapName>('gray');
  const [colormapReversed, setColormapReversed] = useState(false);
  // Layers + cursor tools + live readouts (status pill).
  const [graticule, setGraticule] = useState(false);
  const [tool, setTool] = useState<'pan' | 'ruler'>('pan');
  const [cursor, setCursor] = useState<{ ra: number | null; dec: number | null; values: ReadonlyArray<number | null> | null; native: boolean } | null>(null);
  const [zoom, setZoom] = useState<number | null>(null);
  const [rulerMeasure, setRulerMeasure] = useState<RulerMeasurement | null>(null);

  const handleRef = useRef<FitsViewerHandle | null>(null);
  const overlaysRef = useRef<FitsglOverlaysHandle | null>(null);
  const rootRef = useRef<HTMLDivElement | null>(null);
  const lastZoom = useRef(0);
  // Initial view read through refs: onReady is fixed at construction but re-fires
  // on a dataset reload (field switch), so it must see the CURRENT initial props.
  const initialCenterRef = useRef(initialCenter);
  const initialZoomRef = useRef(initialZoom);
  useEffect(() => { initialCenterRef.current = initialCenter; }, [initialCenter]);
  useEffect(() => { initialZoomRef.current = initialZoom; }, [initialZoom]);
  const lastCursorSky = useRef<{ ra: number; dec: number } | null>(null);
  const initialApplied = useRef(false);
  // Popup: the clicked marker + its world position (repositioned every frame).
  const popupWorld = useRef<{ worldX: number; worldY: number } | null>(null);
  const [popup, setPopup] = useState<{ marker: MapObjectMarker; x: number; y: number } | null>(null);
  const urlDebounce = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);

  // Display (stretch / limits / trilogy) imperative bridge.
  const display = useDisplayStretch({ handleRef, bands, state: viewState, readyTick });

  // Colormap is applied imperatively (single-band only; reverse → reversed LUT).
  const sourceKey = viewState
    ? viewState.mode === 'rgb'
      ? `rgb:${viewState.rgb.r}|${viewState.rgb.g}|${viewState.rgb.b}`
      : `single:${viewState.band}`
    : 'none';
  useColormap({
    handleRef,
    name: colormapName,
    reversed: colormapReversed,
    single: viewState?.mode !== 'rgb',
    sourceKey,
    readyTick,
  });

  // Latest prop/hook callbacks read through refs (onCursor/onFrame are fixed at
  // construction, so their closures must never capture stale props).
  const onCursorCoordsRef = useRef(onCursorCoords);
  const onContextMenuRef = useRef(onContextMenu);
  const seedFromFrameRef = useRef(display.seedFromFrame);
  useEffect(() => { onCursorCoordsRef.current = onCursorCoords; }, [onCursorCoords]);
  useEffect(() => { onContextMenuRef.current = onContextMenu; }, [onContextMenu]);
  useEffect(() => { seedFromFrameRef.current = display.seedFromFrame; }, [display.seedFromFrame]);

  // Load fitsgl.json → inventory + default view → initial explorer state.
  // North-up is forced on and not exposed (locked decision 4).
  useEffect(() => {
    let cancelled = false;
    setConfig(null);
    setLoadError(null);
    initialApplied.current = false;
    loadFitsglConfig(dataset.fitsgl_json_url)
      .then((cfg) => {
        if (cancelled) return;
        const b = explorerBandsFromConfig(cfg);
        const dv = defaultViewFromConfig(cfg);
        setConfig(cfg);
        setBands(b);
        // north-up forced on (decision 4); colormap pinned to 'gray' so it is driven
        // imperatively (useColormap) rather than through the config.
        setViewState({ ...defaultExplorerState(b, dv), northUp: true, colormap: 'gray' });
        setColormapName(dv.colormap ?? 'gray');
        setColormapReversed(false);
      })
      .catch((e) => { if (!cancelled) setLoadError(String(e?.message ?? e)); });
    return () => { cancelled = true; };
  }, [dataset.fitsgl_json_url]);

  const viewerConfig: ViewerConfig | null = useMemo(
    () => (bands.length && viewState ? deriveViewerConfig(bands, viewState) : null),
    [bands, viewState],
  );

  // Marker inputs (sky-positioned; FitsGL projects via the manifest WCS). A filter
  // dims non-matching objects rather than hiding them; the highlighted object gets
  // a larger white glyph, matching the Leaflet layer's emphasis.
  const markerInputs = useMemo<MarkerInput[]>(() => {
    return markers.map((m) => {
      const matches = !markerFilter || markerFilter(m);
      const highlighted = m.object_id === highlightObjectId;
      const base = MARKER_QUALITY_COLORS[m.redshift_quality] ?? MARKER_QUALITY_COLORS[0];
      return {
        id: m.object_id,
        ra: m.ra,
        dec: m.dec,
        shape: 'point',
        size: highlighted ? 16 : matches ? 10 : 6,
        color: highlighted ? '#ffffff' : matches ? base : hexToRgba(base, 0.25),
        data: { objectId: m.object_id },
      };
    });
  }, [markers, markerFilter, highlightObjectId]);

  // Push markers whenever they (or the toggle) change and the viewer is ready.
  useEffect(() => {
    const h = handleRef.current;
    if (!h) return;
    h.setMarkers(showMarkers ? markerInputs : []);
  }, [markerInputs, showMarkers]);

  const markerById = useMemo(() => {
    const map = new Map<string, MapObjectMarker>();
    for (const m of markers) map.set(m.object_id, m);
    return map;
  }, [markers]);

  const onReady = useCallback((handle: FitsViewerHandle) => {
    handleRef.current = handle;
    handle.setMarkers(showMarkers ? markerInputs : []);
    setReadyTick((t) => t + 1);
    if (!initialApplied.current) {
      initialApplied.current = true;
      handle.fitToImage();
      const center = initialCenterRef.current;
      const zoom = initialZoomRef.current;
      const wcs = handle.getViewer()?.getWcs() ?? null;
      if (center && wcs) {
        const p = skyToPix(wcs, center.ra, center.dec);
        handle.setCenter(p.x, p.y);
      }
      if (zoom !== undefined) handle.setZoom(zoom);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const onCursor = useCallback((info: CursorInfo | null) => {
    const sky = info && info.ra !== null && info.dec !== null ? { ra: info.ra, dec: info.dec } : null;
    lastCursorSky.current = sky;
    onCursorCoordsRef.current(sky);
    setCursor(info ? { ra: info.ra, dec: info.dec, values: info.values, native: info.native } : null);
  }, []);

  const onFrame = useCallback((info: ViewerFrameInfo) => {
    const h = handleRef.current;
    // Seed the Display histogram handles from the viewer's auto-stretch once a
    // freshly-switched source has drawn (no-op until a seed is pending).
    seedFromFrameRef.current();
    // Reproject the Canvas2D overlays (graticule / ruler) for the new view.
    overlaysRef.current?.redraw();
    // Track zoom for the status pill (only on meaningful change → fewer renders).
    if (!lastZoom.current || Math.abs(info.zoom - lastZoom.current) / info.zoom > 0.01) {
      lastZoom.current = info.zoom;
      setZoom(info.zoom);
    }
    // Reposition the open popup to track its marker across pan/zoom. imageToScreen
    // returns viewport-client coords; the popup is absolute inside the map root, so
    // localise to the root (matching the canvas-relative click position).
    if (popupWorld.current && h) {
      const s = h.imageToScreen(popupWorld.current.worldX, popupWorld.current.worldY);
      const rect = rootRef.current?.getBoundingClientRect();
      setPopup((prev) => (prev && s && rect ? { ...prev, x: s.x - rect.left, y: s.y - rect.top } : prev));
    }
    // Debounced URL sync (centre → sky via the manifest WCS + FitsGL zoom).
    const wcs = h?.getViewer()?.getWcs() ?? null;
    if (urlDebounce.current) clearTimeout(urlDebounce.current);
    urlDebounce.current = setTimeout(() => {
      const params: Record<string, string> = { z: info.zoom.toFixed(3) };
      if (wcs) {
        const sky = pixToSky(wcs, info.centerX, info.centerY);
        params.ra = sky.ra.toFixed(4);
        params.dec = sky.dec.toFixed(4);
      }
      updateMapUrl(params);
    }, 300);
  }, []);

  const onMarkerClick = useCallback((e: MarkerEvent) => {
    const objectId = e.marker.data?.objectId as string | undefined;
    const marker = objectId ? markerById.get(objectId) : undefined;
    if (!marker) return;
    popupWorld.current = { worldX: e.worldX, worldY: e.worldY };
    setPopup({ marker, x: e.screenX, y: e.screenY });
  }, [markerById]);

  const closePopup = useCallback(() => {
    popupWorld.current = null;
    setPopup(null);
  }, []);

  // Right-click → context menu at the cursor's sky position. Passes ABSOLUTE
  // client coords; the parent (MapViewer.handleContextMenu) subtracts the map
  // wrapper rect itself, exactly as the Leaflet MapEvents path does.
  const handleContextMenu = useCallback((ev: React.MouseEvent<HTMLDivElement>) => {
    const coords = lastCursorSky.current;
    if (!coords) return;
    ev.preventDefault();
    onContextMenuRef.current({
      coords,
      position: { x: ev.clientX, y: ev.clientY },
    });
  }, []);

  // Band-rail intent handlers (all pure state updates → deriveViewerConfig).
  // trilogy is RGB-only, so leaving RGB coerces the curve back to a single-band one.
  const canComposite = bands.length >= 2;
  const leaveTrilogy = (stretch: StretchMode): StretchMode => (stretch === 'trilogy' ? 'asinh' : stretch);
  const onSelectBand = useCallback((name: string) =>
    setViewState((s) => (s ? { ...s, mode: 'single', band: name, stretch: leaveTrilogy(s.stretch) } : s)), []);
  const onToggleRgb = useCallback(() =>
    setViewState((s) => {
      if (!s) return s;
      const mode = s.mode === 'rgb' ? 'single' : 'rgb';
      return { ...s, mode, stretch: mode === 'single' ? leaveTrilogy(s.stretch) : s.stretch };
    }), []);
  const onSetRgbRole = useCallback((role: 'r' | 'g' | 'b', band: string) =>
    setViewState((s) => (s ? { ...s, rgb: { ...s.rgb, [role]: band } } : s)), []);
  const onRainbow = useCallback(() =>
    setViewState((s) => {
      if (!s) return s;
      const rgb = rainbowRgb(bands);
      return rgb ? { ...s, mode: 'rgb', rgb } : s;
    }), [bands]);

  // Display-panel intent handlers.
  const onSetStretch = useCallback((mode: StretchMode) =>
    setViewState((s) => (s ? { ...s, stretch: mode } : s)), []);
  const onSetHandle = useCallback((key: ChannelKey, min: number, max: number) =>
    display.setHandle(key, min, max), [display]);

  // Tool-rail actions.
  const onFit = useCallback(() => handleRef.current?.fitToImage(), []);
  const onExport = useCallback(() => {
    const url = handleRef.current?.exportPNG();
    if (!url) return;
    const a = document.createElement('a');
    a.href = url;
    a.download = `${selectedField}-fitsgl.png`;
    a.click();
  }, [selectedField]);

  if (loadError) {
    return (
      <div className="flex items-center justify-center h-full bg-surface-2">
        <div className="text-center text-text-secondary">
          <p className="text-lg font-medium mb-2">Could not load the FitsGL map</p>
          <p className="text-sm font-mono">{loadError}</p>
        </div>
      </div>
    );
  }

  return (
    <div ref={rootRef} className="fitsgl-chrome relative h-full w-full" onContextMenu={handleContextMenu}>
      {viewerConfig && (
        <FitsViewer
          config={viewerConfig}
          tileOptions={{ workerFactory: makeFitsglWorker }}
          onReady={onReady}
          onCursor={onCursor}
          onFrame={onFrame}
          onMarkerClick={onMarkerClick}
          onError={(err) => setLoadError(String((err as Error)?.message ?? err))}
          className="h-full w-full"
          style={{ background: 'var(--header)' }}
        />
      )}
      {viewerConfig && (
        <FitsglOverlays
          ref={overlaysRef}
          handleRef={handleRef}
          graticule={graticule}
          tool={tool}
          readyTick={readyTick}
          accent={RULER_ACCENT}
          gridLine={GRID_LINE}
          gridLabel={GRID_LABEL}
          onMeasure={setRulerMeasure}
        />
      )}
      {!config && !loadError && (
        <div className="absolute inset-0 flex items-center justify-center bg-surface-2 text-text-secondary">
          Loading FitsGL map…
        </div>
      )}

      {/* Left tool rail — modal tools + actions + filters launcher. */}
      {viewState && (
        <ToolRail
          tool={tool}
          onSetTool={setTool}
          onFit={onFit}
          onExport={onExport}
          onOpenFilters={onOpenFilters}
          hasActiveFilters={hasActiveFilters}
        />
      )}

      {/* Band rail — merged field select + band/RGB (top-center). */}
      {viewState && (fields.length > 1 || bands.length > 1) && (
        <BandRail
          fields={fields}
          selectedField={selectedField}
          onFieldChange={onFieldChange}
          bands={bands}
          state={viewState}
          canComposite={canComposite}
          onSelectBand={onSelectBand}
          onToggleRgb={onToggleRgb}
          onSetRgbRole={onSetRgbRole}
          onRainbow={onRainbow}
        />
      )}

      {/* Right control dock — Display panel (+ objects toggle placeholder until the
          Layers panel lands in chunk 3). Collapsible via the edge chevron. */}
      {viewState && dockOpen && (
        <div className={`absolute right-0 top-16 bottom-16 z-[500] w-72 overflow-y-auto rounded-l-xl ${GLASS} p-3`}>
          <DisplayPanel
            state={viewState}
            channels={display.channels}
            hasZscale={display.hasZscale}
            hasTrilogy={display.hasTrilogy}
            colormap={colormapName}
            colormapReversed={colormapReversed}
            onSetStretch={onSetStretch}
            onSetColormap={setColormapName}
            onToggleReverseColormap={setColormapReversed}
            onSetHandle={onSetHandle}
            onApplyPreset={display.applyPreset}
          />
          <div className="mt-3 border-t border-border pt-3">
            <LayersPanel
              showMarkers={showMarkers}
              onToggleMarkers={onToggleMarkers}
              markerCount={markerCount}
              graticule={graticule}
              onToggleGraticule={setGraticule}
            />
          </div>
        </div>
      )}
      {viewState && (
        <button
          type="button"
          onClick={() => setDockOpen((o) => !o)}
          className={`absolute top-20 z-[501] flex h-11 w-4 items-center justify-center rounded-l-lg ${GLASS} text-text-tertiary hover:text-text-primary`}
          style={{ right: dockOpen ? '18rem' : 0 }}
          aria-label={dockOpen ? 'Collapse controls' : 'Expand controls'}
          title={dockOpen ? 'Collapse controls' : 'Expand controls'}
        >
          {dockOpen ? '›' : '‹'}
        </button>
      )}

      {/* Status pill — dual RA/Dec + value + zoom + band·stretch (+ ruler). */}
      {viewState && (
        <StatusPill
          ra={cursor?.ra ?? null}
          dec={cursor?.dec ?? null}
          values={cursor?.values ?? null}
          native={cursor?.native ?? false}
          zoom={zoom}
          bandLabel={viewState.mode === 'rgb' ? 'RGB' : viewState.band}
          stretch={viewState.stretch}
          ruler={tool === 'ruler' ? rulerMeasure : null}
        />
      )}

      {/* Clicked-marker popup (custom; FitsGL has no Leaflet Popup). */}
      {popup && (() => {
        const q = QUALITY_LABELS.find((l) => l.value === popup.marker.redshift_quality);
        return (
          <div
            className="absolute z-[600] min-w-[200px] -translate-x-1/2 -translate-y-full rounded-lg border border-border bg-card p-3 text-sm shadow-lg"
            style={{ left: popup.x, top: popup.y - 12 }}
          >
            <button
              onClick={closePopup}
              className="absolute right-2 top-1 text-text-tertiary hover:text-text-primary"
              aria-label="Close"
            >
              ×
            </button>
            <div className="mb-1 font-mono font-bold">
              <Link
                href={`/nirspec/objects/${encodeURIComponent(popup.marker.object_id)}`}
                className="text-primary-text underline hover:text-primary"
                onClick={() => sessionStorage.setItem('campfire-map-return-url', window.location.href)}
              >
                {popup.marker.object_id}
              </Link>
            </div>
            <div className="space-y-0.5 text-xs">
              {popup.marker.redshift !== null && <div>z = {popup.marker.redshift.toFixed(4)}</div>}
              <div>Quality: {q?.icon} {q?.label ?? 'Unknown'}</div>
              <div className="text-text-tertiary">
                {popup.marker.n_targets} target{popup.marker.n_targets !== 1 ? 's' : ''}, {popup.marker.n_spectra} spectr{popup.marker.n_spectra !== 1 ? 'a' : 'um'}
              </div>
              <div className="text-text-tertiary">
                RA: {popup.marker.ra.toFixed(5)}, Dec: {popup.marker.dec.toFixed(5)}
              </div>
            </div>
          </div>
        );
      })()}
    </div>
  );
}
