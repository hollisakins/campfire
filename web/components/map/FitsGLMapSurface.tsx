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
  rainbowAction,
  rgbActiveGroup,
  type FitsViewerHandle,
  type ExplorerBand,
  type ExplorerState,
  type FitsglConfig,
} from '@fitsgl/core/react';
import {
  pixToSky,
  skyToPix,
  type BandWeight,
  type ColormapName,
  type StretchMode,
  type CursorInfo,
  type MarkerEvent,
  type MarkerInput,
  type RegionInput,
  type ResolvedRegion,
  type TrilogyParams,
  type ViewerConfig,
  type ViewerFrameInfo,
} from '@fitsgl/core';
import type { FitsglDataset, MapObjectMarker, SlitRegion, Shutter } from '@/lib/actions/map';
import { MARKER_QUALITY_COLORS, QUALITY_LABELS } from '@/lib/types';
import { makeFitsglWorker } from '@/lib/fits/fitsglWorker';
import { getObservationColor } from './observation-colors';
import { BandRail, RGB_OPTION } from './fitsgl/BandRail';
import { DisplayPanel } from './fitsgl/DisplayPanel';
import { LayersPanel } from './fitsgl/LayersPanel';
import { ToolRail } from './fitsgl/ToolRail';
import { StatusPill } from './fitsgl/StatusPill';
import { FitsglOverlays, type FitsglOverlaysHandle } from './fitsgl/FitsglOverlays';
import { useDisplayStretch, type ChannelKey, type LimitPreset } from './fitsgl/useDisplayStretch';
import { useColormap } from './fitsgl/useColormap';
import type { RulerMeasurement } from './fitsgl/ruler';
import { GLASS } from './fitsgl/glass';

/** Ruler/graticule colours drawn over the always-dark map well (theme-independent). */
const RULER_ACCENT = '#fb923c';
const GRID_LINE = 'rgba(148,163,184,0.35)';
const GRID_LABEL = 'rgba(203,213,225,0.8)';

/** Default MSA shutter extents (arcsec); fixed slits carry their own aperture dims.
 *  Mirrors CanvasSlitLayer's constants so the two map surfaces render identically. */
const SHUTTER_WIDTH_ARCSEC = 0.22;
const SHUTTER_HEIGHT_ARCSEC = 0.46;
/** Stuck-closed shutters read red-dashed on both surfaces. */
const SHUTTER_STUCK_COLOR = '#ef4444';

interface FitsGLMapSurfaceProps {
  dataset: FitsglDataset;
  markers: MapObjectMarker[];
  showMarkers: boolean;
  highlightObjectId?: string;
  markerFilter?: (marker: MapObjectMarker) => boolean;
  initialCenter?: { ra: number; dec: number };
  initialZoom?: number;
  /** Band to open on (e.g. from /map?filter=...); falls back to the dataset
   *  default view when absent or not in the band inventory. */
  initialBand?: string;
  /** Field switcher (shared across the Leaflet + FitsGL surfaces). */
  fields: string[];
  selectedField: string;
  onFieldChange: (field: string) => void;
  onToggleMarkers: (visible: boolean) => void;
  markerCount: number;
  /** NIRSpec MSA shutters for the field (rendered as FitsGL sky-polygon regions). */
  shutters: (SlitRegion | Shutter)[];
  showShutters: boolean;
  onToggleShutters: (visible: boolean) => void;
  /** Restricts the drawn shutters to those whose target belongs to a filtered object. */
  shutterFilter?: (shutter: SlitRegion | Shutter) => boolean;
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


/**
 * The four sky corners (ICRS deg) of a shutter, from its centre + position angle +
 * full angular extents. Worked in a local tangent plane of (East, North) arcsec
 * offsets: the along-slit (height) axis points `paDeg` East of North, the across-slit
 * (width) axis is perpendicular; East offsets divide by cos(dec) to become ΔRA.
 * Returned CCW, non-self-intersecting.
 */
function shutterCorners(
  ra: number,
  dec: number,
  paDeg: number,
  widthArcsec: number,
  heightArcsec: number,
): Array<{ ra: number; dec: number }> {
  const pa = (paDeg * Math.PI) / 180;
  const sinPa = Math.sin(pa);
  const cosPa = Math.cos(pa);
  const hw = widthArcsec / 2;
  const hh = heightArcsec / 2;
  const cosDec = Math.cos((dec * Math.PI) / 180) || 1e-8;
  // width axis û_w = (cosPa, -sinPa); height axis û_h = (sinPa, cosPa) in (East, North).
  return [
    [-1, -1],
    [1, -1],
    [1, 1],
    [-1, 1],
  ].map(([i, j]) => {
    const dEast = i * hw * cosPa + j * hh * sinPa;
    const dNorth = -i * hw * sinPa + j * hh * cosPa;
    return { ra: ra + dEast / 3600 / cosDec, dec: dec + dNorth / 3600 };
  });
}

/**
 * Map NIRSpec shutters to FitsGL sky-polygon regions (epic #337, Phase 4b). Each
 * shutter is a `center_ra`/`center_dec` + `position_angle` (deg East of North) +
 * angular extents; we project its four corners to ICRS `vertices` and let FitsGL
 * place them through the mosaic WCS (a footprint that scales with zoom / rotates with
 * the display). Colour matches the Leaflet `CanvasSlitLayer`: per-observation stroke
 * with a faint fill, stuck-closed = red dashed. Non-matching shutters are dropped.
 *
 * Polygons (not the instanced sky-*rect* path) because `@fitsgl/core` 0.2.0's sky-rect
 * `skyBasis` guard mis-measures the East pixel scale by a factor of cos(dec) and drops
 * every rect on a field more than a few degrees off the equator (bug reported upstream;
 * a sky polygon projects each vertex independently and is unaffected).
 */
function shuttersToRegions(
  shutters: readonly (SlitRegion | Shutter)[],
  filter?: (s: SlitRegion | Shutter) => boolean,
): RegionInput[] {
  const visible = filter ? shutters.filter(filter) : shutters;
  // Stable per-observation colours from the FULL set (so a filter never reshuffles them).
  const observations = [...new Set(shutters.map((s) => s.observation))].sort();
  return visible.map((s, i): RegionInput => {
    const isShutter = 'shutter_state' in s;
    const stuck = isShutter && (s as Shutter).shutter_state === 'stuck_closed';
    const color = stuck ? SHUTTER_STUCK_COLOR : getObservationColor(s.observation, observations);
    const sh = s as Shutter;
    return {
      id: `shutter-${i}`,
      shape: 'polygon',
      vertices: shutterCorners(
        s.center_ra,
        s.center_dec,
        s.position_angle,
        sh.aperture_width_arcsec ?? SHUTTER_WIDTH_ARCSEC,
        sh.aperture_height_arcsec ?? SHUTTER_HEIGHT_ARCSEC,
      ),
      stroke: color,
      fill: `${color}22`, // ~13% — the faint footprint tint (CanvasSlitLayer uses 0.08 alpha)
      strokeWidth: stuck ? 1.5 : 1,
      dash: stuck ? ([3, 2] as const) : undefined,
      data: {
        targetId: s.object_id,
        observation: s.observation,
        state: isShutter ? sh.shutter_state : 'slit',
      },
    };
  });
}

export function FitsGLMapSurface({
  dataset,
  markers,
  showMarkers,
  highlightObjectId,
  markerFilter,
  initialCenter,
  initialZoom,
  initialBand,
  fields,
  selectedField,
  onFieldChange,
  onToggleMarkers,
  markerCount,
  shutters,
  showShutters,
  onToggleShutters,
  shutterFilter,
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
  // Simple-RGB color tuning, driven imperatively like the colormap: contrast
  // narrows/widens the shared limits about their midpoint; saturation is the
  // viewer's post-stretch composite uniform. Both reset on a source change.
  const [contrast, setContrast] = useState(1);
  const [saturation, setSaturation] = useState(1);
  // The contrast=1 baseline the slider scales from (null ⇒ the hook's current
  // shared range, i.e. whatever the last preset/auto-stretch produced).
  const rgbBaseRef = useRef<{ min: number; max: number } | null>(null);
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
  const initialBandRef = useRef(initialBand);
  useEffect(() => { initialCenterRef.current = initialCenter; }, [initialCenter]);
  useEffect(() => { initialZoomRef.current = initialZoom; }, [initialZoom]);
  useEffect(() => { initialBandRef.current = initialBand; }, [initialBand]);
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
        let state: ExplorerState = { ...defaultExplorerState(b, dv), northUp: true, colormap: 'gray' };
        // Deep link: /map?filter=<band> opens single-band on that band instead
        // of the dataset default (which may be RGB or a different band).
        const wanted = initialBandRef.current?.toLowerCase();
        const match = wanted ? b.find((bb) => bb.name.toLowerCase() === wanted) : undefined;
        if (match) {
          state = { ...state, mode: 'single', band: match.name,
                    stretch: state.stretch === 'trilogy' ? 'asinh' : state.stretch };
        }
        setViewState(state);
        setColormapName(dv.colormap ?? 'gray');
        setColormapReversed(false);
        setContrast(1);
        setSaturation(1);
        rgbBaseRef.current = null;
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

  // Shutter region inputs (sky-polygon footprints; FitsGL projects via the manifest WCS).
  const shutterRegions = useMemo<RegionInput[]>(
    () => shuttersToRegions(shutters, shutterFilter),
    [shutters, shutterFilter],
  );

  // Push shutter regions on change, mirroring the marker path.
  useEffect(() => {
    const h = handleRef.current;
    if (!h) return;
    h.setRegions(showShutters ? shutterRegions : []);
  }, [shutterRegions, showShutters]);

  // Re-apply both overlays after a (re)ready. onReady is fixed at construction and
  // re-fires on every band reload; reading the latest marker/shutter state through a
  // ref keeps a reload (e.g. an RGB toggle) from restoring stale overlay visibility.
  const applyOverlaysRef = useRef<(h: FitsViewerHandle) => void>(() => {});
  useEffect(() => {
    applyOverlaysRef.current = (h) => {
      h.setMarkers(showMarkers ? markerInputs : []);
      h.setRegions(showShutters ? shutterRegions : []);
    };
  }, [showMarkers, markerInputs, showShutters, shutterRegions]);

  const markerById = useMemo(() => {
    const map = new Map<string, MapObjectMarker>();
    for (const m of markers) map.set(m.object_id, m);
    return map;
  }, [markers]);

  const onReady = useCallback((handle: FitsViewerHandle) => {
    handleRef.current = handle;
    applyOverlaysRef.current(handle);
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

  // Band-rail intent handler (a pure state update → deriveViewerConfig): the
  // band dropdown's RGB_OPTION enters composite mode, a band name drops to
  // single mode on it. trilogy is RGB-only, so leaving RGB coerces the curve.
  const canComposite = bands.length >= 2;
  const leaveTrilogy = (stretch: StretchMode): StretchMode => (stretch === 'trilogy' ? 'asinh' : stretch);
  const onSelectDisplay = useCallback((value: string) =>
    setViewState((s) => {
      if (!s) return s;
      if (value === RGB_OPTION) return s.mode === 'rgb' ? s : { ...s, mode: 'rgb' };
      return { ...s, mode: 'single', band: value, stretch: leaveTrilogy(s.stretch) };
    }), []);
  // Display-panel intent handlers. RGB construction lives entirely in the panel
  // (revised decision 1): channel assignment, simple|trilogy sub-mode, weights,
  // trilogy knobs, and the simple-mode shared range / contrast / saturation.
  const onSetRgbRole = useCallback((role: 'r' | 'g' | 'b', band: string) =>
    setViewState((s) => (s ? { ...s, rgb: { ...s.rgb, [role]: band } } : s)), []);
  const onSetStretch = useCallback((mode: StretchMode) =>
    setViewState((s) => (s ? { ...s, stretch: mode } : s)), []);
  const onSetRgbSubMode = useCallback((m: 'simple' | 'trilogy') =>
    setViewState((s) => {
      if (!s) return s;
      const stretch = m === 'trilogy' ? 'trilogy' : leaveTrilogy(s.stretch);
      return stretch === s.stretch ? s : { ...s, stretch };
    }), []);
  const onApplyWeights = useCallback((entries: Array<{ band: string; weight: BandWeight }>) =>
    setViewState((s) => {
      if (!s) return s;
      const weights: Record<string, BandWeight> = {};
      for (const e of entries) weights[e.band] = e.weight;
      return { ...s, weights, weightBands: entries.map((e) => e.band) };
    }), []);
  const onRainbowWeights = useCallback(() =>
    setViewState((s) => (s ? { ...s, ...rainbowAction(bands, rgbActiveGroup(bands, s.rgb)) } : s)), [bands]);
  const onSetTrilogyParams = useCallback((patch: Partial<TrilogyParams>) =>
    setViewState((s) => (s ? { ...s, trilogyParams: { ...s.trilogyParams, ...patch } } : s)), []);
  const onSetHandle = useCallback((key: ChannelKey, min: number, max: number) =>
    display.setHandle(key, min, max), [display]);

  // Simple-RGB shared range + contrast + saturation. Dragging the shared handles
  // establishes a new contrast=1 baseline; the contrast slider then scales the
  // limits about their midpoint; presets reset both to the fresh envelope.
  const onSetSharedRange = useCallback((min: number, max: number) => {
    rgbBaseRef.current = { min, max };
    setContrast(1);
    display.setSharedHandle(min, max);
  }, [display]);
  const onSetContrast = useCallback((c: number) => {
    setContrast(c);
    const base = rgbBaseRef.current ?? display.sharedRange;
    if (!base) return;
    const mid = (base.min + base.max) / 2;
    const half = (base.max - base.min) / 2 / Math.max(c, 1e-6);
    display.setSharedHandle(mid - half, mid + half);
  }, [display]);
  const onApplyPreset = useCallback(async (preset: LimitPreset) => {
    await display.applyPreset(preset);
    rgbBaseRef.current = null; // next contrast change scales the fresh envelope
    setContrast(1);
  }, [display]);
  // Saturation is a live viewer uniform (identity for single-band); re-pushed
  // after every viewer (re)ready since a rebuilt viewer starts at 1.
  useEffect(() => {
    handleRef.current?.getViewer()?.setSaturation(saturation);
  }, [saturation, readyTick]);
  // A source-identity change invalidates the simple-RGB tuning baseline.
  useEffect(() => {
    rgbBaseRef.current = null;
    setContrast(1);
  }, [sourceKey]);

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

  // Hover tooltip for a shutter region (FitsGL's built-in popup).
  const regionTooltip = useCallback((r: ResolvedRegion) => {
    const d = r.data as { targetId?: string; observation?: string; state?: string };
    if (!d?.targetId) return null;
    const stuck = d.state === 'stuck_closed';
    return `${d.targetId} · ${d.observation ?? ''}${stuck ? ' · stuck' : ''}`;
  }, []);

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
    <div ref={rootRef} className="fitsgl-chrome relative h-full w-full overflow-hidden" onContextMenu={handleContextMenu}>
      {viewerConfig && (
        <FitsViewer
          config={viewerConfig}
          tileOptions={{ workerFactory: makeFitsglWorker }}
          onReady={onReady}
          onCursor={onCursor}
          onFrame={onFrame}
          onMarkerClick={onMarkerClick}
          regionTooltip={regionTooltip}
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
          onSelectDisplay={onSelectDisplay}
        />
      )}

      {/* Right control dock — Display + Layers panels. Height hugs the content
          (up to the old fixed extent, then scrolls); collapse slides it off the
          right edge (kept mounted so the panel state survives + animates). */}
      {viewState && (
        <div
          className={`absolute right-0 top-16 z-[500] max-h-[calc(100%-8rem)] w-[13.5rem] overflow-y-auto rounded-l-xl ${GLASS} p-3 transition-transform duration-200 ease-out ${
            dockOpen ? 'translate-x-0' : 'pointer-events-none translate-x-full'
          }`}
          aria-hidden={!dockOpen}
        >
          <DisplayPanel
            state={viewState}
            bands={bands}
            channels={display.channels}
            hasZscale={display.hasZscale}
            hasTrilogy={display.hasTrilogy}
            colormap={colormapName}
            colormapReversed={colormapReversed}
            sharedRange={display.sharedRange}
            contrast={contrast}
            saturation={saturation}
            onSetStretch={onSetStretch}
            onSetRgbSubMode={onSetRgbSubMode}
            onSetRgbRole={onSetRgbRole}
            onApplyWeights={onApplyWeights}
            onRainbowWeights={onRainbowWeights}
            onSetTrilogyParams={onSetTrilogyParams}
            onSetColormap={setColormapName}
            onToggleReverseColormap={setColormapReversed}
            onSetHandle={onSetHandle}
            onSetSharedRange={onSetSharedRange}
            onSetContrast={onSetContrast}
            onSetSaturation={setSaturation}
            onApplyPreset={onApplyPreset}
          />
          <div className="mt-3 border-t border-border pt-3">
            <LayersPanel
              showMarkers={showMarkers}
              onToggleMarkers={onToggleMarkers}
              markerCount={markerCount}
              graticule={graticule}
              onToggleGraticule={setGraticule}
              shuttersAvailable={shutters.length > 0}
              showShutters={showShutters}
              onToggleShutters={onToggleShutters}
              shutterCount={shutters.length}
            />
          </div>
        </div>
      )}
      {viewState && (
        <button
          type="button"
          onClick={() => setDockOpen((o) => !o)}
          className={`absolute top-20 z-[501] flex h-11 w-4 items-center justify-center rounded-l-lg ${GLASS} text-text-tertiary transition-[right] duration-200 ease-out hover:text-text-primary`}
          style={{ right: dockOpen ? '13.5rem' : 0 }}
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
          showValue={!(viewState.mode === 'rgb' && viewState.stretch === 'trilogy')}
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
