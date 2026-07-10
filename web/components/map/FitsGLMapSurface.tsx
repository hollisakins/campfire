'use client';

/**
 * FitsGL map surface (epic #337, Phase 4). Renders a deployed FitsGL tile-pyramid
 * dataset in `<FitsViewer>` as the map for a field that has one, replacing the
 * Leaflet + PNG-tile path (`MapViewer` dispatches to it per field). Self-contained:
 * loads the dataset's `fitsgl.json`, owns band/RGB switching, pushes NIRSpec object
 * markers through the viewer's ref handle (FitsGL owns culling/hit-test/tooltip —
 * no `CanvasMarkerLayer` here), and feeds the shared `CoordinateOverlay` /
 * `MapContextMenu` via callbacks. Shutters are Phase 4b (the region primitive).
 *
 * `onCursor`/`onFrame` are fixed at viewer construction, so everything they touch
 * is read through refs (never stale closures).
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
  type CursorInfo,
  type MarkerEvent,
  type MarkerInput,
  type ViewerConfig,
  type ViewerFrameInfo,
} from '@fitsgl/core';
import type { FitsglDataset, MapObjectMarker } from '@/lib/actions/map';
import { MARKER_QUALITY_COLORS, QUALITY_LABELS } from '@/lib/types';
import { makeFitsglWorker } from '@/lib/fits/fitsglWorker';

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
}: FitsGLMapSurfaceProps) {
  const [config, setConfig] = useState<FitsglConfig | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [bands, setBands] = useState<ExplorerBand[]>([]);
  const [viewState, setViewState] = useState<ExplorerState | null>(null);

  const handleRef = useRef<FitsViewerHandle | null>(null);
  const lastCursorSky = useRef<{ ra: number; dec: number } | null>(null);
  const initialApplied = useRef(false);
  // Popup: the clicked marker + its world position (repositioned every frame).
  const popupWorld = useRef<{ worldX: number; worldY: number } | null>(null);
  const [popup, setPopup] = useState<{ marker: MapObjectMarker; x: number; y: number } | null>(null);
  const urlDebounce = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);

  // Latest prop callbacks read through refs (onCursor/onFrame are fixed at
  // construction, so their closures must never capture stale props).
  const onCursorCoordsRef = useRef(onCursorCoords);
  const onContextMenuRef = useRef(onContextMenu);
  useEffect(() => { onCursorCoordsRef.current = onCursorCoords; }, [onCursorCoords]);
  useEffect(() => { onContextMenuRef.current = onContextMenu; }, [onContextMenu]);

  // Load fitsgl.json → inventory + default view → initial explorer state.
  useEffect(() => {
    let cancelled = false;
    setConfig(null);
    setLoadError(null);
    loadFitsglConfig(dataset.fitsgl_json_url)
      .then((cfg) => {
        if (cancelled) return;
        const b = explorerBandsFromConfig(cfg);
        setConfig(cfg);
        setBands(b);
        setViewState(defaultExplorerState(b, defaultViewFromConfig(cfg)));
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
    if (!initialApplied.current) {
      initialApplied.current = true;
      handle.fitToImage();
      const wcs = handle.getViewer()?.getWcs() ?? null;
      if (initialCenter && wcs) {
        const p = skyToPix(wcs, initialCenter.ra, initialCenter.dec);
        handle.setCenter(p.x, p.y);
      }
      if (initialZoom !== undefined) handle.setZoom(initialZoom);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const onCursor = useCallback((info: CursorInfo | null) => {
    const sky = info && info.ra !== null && info.dec !== null ? { ra: info.ra, dec: info.dec } : null;
    lastCursorSky.current = sky;
    onCursorCoordsRef.current(sky);
  }, []);

  const onFrame = useCallback((info: ViewerFrameInfo) => {
    const h = handleRef.current;
    // Reposition the open popup to track its marker across pan/zoom.
    if (popupWorld.current && h) {
      const s = h.imageToScreen(popupWorld.current.worldX, popupWorld.current.worldY);
      setPopup((prev) => (prev && s ? { ...prev, x: s.x, y: s.y } : prev));
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

  // Band control model.
  const single = viewState?.mode !== 'rgb';
  const canRgb = bands.length >= 3;
  const setSingleBand = (name: string) =>
    setViewState((s) => (s ? { ...s, mode: 'single', band: name } : s));
  const toggleRgb = () =>
    setViewState((s) => (s ? { ...s, mode: s.mode === 'rgb' ? 'single' : 'rgb' } : s));

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
    <div className="relative h-full w-full" onContextMenu={handleContextMenu}>
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
      {!config && !loadError && (
        <div className="absolute inset-0 flex items-center justify-center bg-surface-2 text-text-secondary">
          Loading FitsGL map…
        </div>
      )}

      {/* Control panel: field switcher + band/RGB switcher + marker toggle. */}
      <div className="absolute top-3 left-3 z-[500] w-56 space-y-2 rounded-lg border border-border bg-card/90 p-2 backdrop-blur">
        <div className="flex items-center gap-2">
          <select
            value={selectedField}
            onChange={(e) => onFieldChange(e.target.value)}
            className="min-w-0 flex-1 rounded border border-border bg-card px-2 py-1 text-xs"
          >
            {fields.map((f) => (
              <option key={f} value={f}>{f}</option>
            ))}
          </select>
          <span className="shrink-0 rounded bg-surface-2 px-1.5 py-0.5 text-[10px] font-medium text-text-tertiary">FitsGL</span>
        </div>

        {bands.length > 1 && viewState && (
          <div className="flex flex-wrap items-center gap-1">
            {canRgb && (
              <button
                onClick={toggleRgb}
                className={`rounded px-2 py-1 text-xs font-medium ${!single ? 'bg-primary text-on-primary' : 'text-text-secondary hover:bg-card-hover'}`}
              >
                RGB
              </button>
            )}
            {bands.map((b) => (
              <button
                key={b.name}
                onClick={() => setSingleBand(b.name)}
                className={`rounded px-2 py-1 font-mono text-xs ${single && viewState.band === b.name ? 'bg-primary text-on-primary' : 'text-text-secondary hover:bg-card-hover'}`}
              >
                {b.label ?? b.name}
              </button>
            ))}
          </div>
        )}

        <label className="flex items-center justify-between text-xs text-text-secondary">
          <span>Objects ({markerCount})</span>
          <input
            type="checkbox"
            checked={showMarkers}
            onChange={(e) => onToggleMarkers(e.target.checked)}
          />
        </label>
      </div>

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
