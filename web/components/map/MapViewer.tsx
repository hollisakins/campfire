'use client';

import React, { useState, useEffect, useCallback, useRef, useMemo } from 'react';
import L from 'leaflet';
import {
  MapContainer,
  TileLayer,
  Popup,
  useMap,
  useMapEvents,
} from 'react-leaflet';
import Link from 'next/link';
import type { MapLayer, MapObjectMarker } from '@/lib/actions/map';
import { useFieldObjectMarkers } from '@/lib/hooks/useFieldObjectMarkers';
import { useFieldSlits } from '@/lib/hooks/useFieldSlits';
import type { WCSParams } from '@/lib/utils/wcs';
import { leafletToSky, skyToLeaflet } from '@/lib/utils/wcs';
import { QUALITY_LABELS } from '@/lib/types';
import { LayerControl } from './LayerControl';
import { CoordinateOverlay } from './CoordinateOverlay';
import { MapContextMenu } from './MapContextMenu';
import { CanvasMarkerLayer } from './CanvasMarkerLayer';
import { CanvasSlitLayer } from './CanvasSlitLayer';
import type { SlitRegion, Shutter } from '@/lib/actions/map';

import 'leaflet/dist/leaflet.css';

// Nearest-neighbor rendering for FITS tile images (crisp pixels when zoomed past native level)
// Matches FITSMap's TileNearestNeighbor.css approach
const pixelatedTileStyle = `
.leaflet-container .leaflet-tile-pane img {
  image-rendering: pixelated;
  image-rendering: crisp-edges;
}`;

// ============================================
// Custom CRS for pixel-based tile coordinates
// ============================================

function createFitsMapCRS(maxZoom: number, naxis2: number, tileSize: number = 256): L.CRS {
  const scale = Math.pow(2, maxZoom);
  const nTilesY = Math.ceil(naxis2 / tileSize);
  const d = (nTilesY * tileSize) / scale;
  return L.Util.extend({}, L.CRS.Simple, {
    transformation: new L.Transformation(1 / scale, 0, -1 / scale, d),
  }) as L.CRS;
}

// ============================================
// URL sync helper
// ============================================

function updateMapUrl(params: Record<string, string | undefined>) {
  const url = new URL(window.location.href);
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined) url.searchParams.set(key, value);
    else url.searchParams.delete(key);
  }
  window.history.replaceState(null, '', url.toString());
}

// ============================================
// Sub-components that use map hooks
// ============================================

interface MapEventsProps {
  wcs: WCSParams | null;
  onMouseMove: (coords: { ra: number; dec: number } | null) => void;
  onContextMenu: (data: { coords: { ra: number; dec: number }; position: { x: number; y: number } }) => void;
  onMoveStart: () => void;
}

function MapEvents({ wcs, onMouseMove, onContextMenu, onMoveStart }: MapEventsProps) {
  const debounceRef = useRef<ReturnType<typeof setTimeout>>(undefined);

  useMapEvents({
    moveend: (e) => {
      if (!wcs) return;
      const center = e.target.getCenter();
      const sky = leafletToSky(wcs, center.lat, center.lng);
      const zoom = e.target.getZoom();
      // Debounce URL updates
      if (debounceRef.current) clearTimeout(debounceRef.current);
      debounceRef.current = setTimeout(() => {
        updateMapUrl({
          ra: sky.ra.toFixed(4),
          dec: sky.dec.toFixed(4),
          z: String(zoom),
        });
      }, 300);
    },
    mousemove: (e) => {
      if (!wcs) return;
      const coords = leafletToSky(wcs, e.latlng.lat, e.latlng.lng);
      onMouseMove(coords);
    },
    mouseout: () => {
      onMouseMove(null);
    },
    contextmenu: (e) => {
      if (!wcs) return;
      e.originalEvent.preventDefault();
      const skyCoords = leafletToSky(wcs, e.latlng.lat, e.latlng.lng);
      onContextMenu({
        coords: skyCoords,
        position: { x: e.originalEvent.clientX, y: e.originalEvent.clientY },
      });
    },
    movestart: () => {
      onMoveStart();
    },
  });

  return null;
}

// ============================================
// MapUpdater: imperatively sync map bounds/zoom
// when active layer changes (MapContainer props
// are immutable after creation)
// ============================================

function MapUpdater({ activeLayer, bounds }: { activeLayer: MapLayer; bounds: L.LatLngBounds }) {
  const map = useMap();
  useEffect(() => {
    map.setMaxBounds(bounds);
    map.setMinZoom(activeLayer.min_zoom);
    map.setMaxZoom(activeLayer.max_zoom + 3);
  }, [map, activeLayer.min_zoom, activeLayer.max_zoom, bounds]);
  return null;
}

// ============================================
// Main MapViewer Component
// ============================================

interface MapViewerProps {
  layers: MapLayer[];
  initialField?: string;
  initialFilter?: string;
  initialCenter?: { ra: number; dec: number };
  initialZoom?: number;
  highlightObjectId?: string;
  markerFilter?: (marker: MapObjectMarker) => boolean;
  filteredIdSet?: Set<string> | null;
  onOpenFilters?: () => void;
  hasActiveFilters?: boolean;
  onFieldChange?: (field: string) => void;
}

export function MapViewer({
  layers,
  initialField,
  initialFilter,
  initialCenter,
  initialZoom,
  highlightObjectId,
  markerFilter,
  filteredIdSet,
  onOpenFilters,
  hasActiveFilters,
  onFieldChange: onFieldChangeProp,
}: MapViewerProps) {
  // Group layers by field, with RGB sorted first
  const fieldGroups = useMemo(() => {
    const groups: Record<string, MapLayer[]> = {};
    for (const layer of layers) {
      if (!groups[layer.field]) groups[layer.field] = [];
      groups[layer.field].push(layer);
    }
    // Sort each field's layers: RGB first, then alphabetical
    for (const field of Object.keys(groups)) {
      groups[field].sort((a, b) => {
        if (a.filter === 'rgb') return -1;
        if (b.filter === 'rgb') return 1;
        return a.filter.localeCompare(b.filter);
      });
    }
    return groups;
  }, [layers]);

  const fields = useMemo(() => Object.keys(fieldGroups).sort(), [fieldGroups]);

  // State
  const [selectedField, setSelectedField] = useState<string>(
    initialField || fields[0] || ''
  );
  const [activeLayer, setActiveLayer] = useState<MapLayer | null>(null);
  const [showMarkers, setShowMarkers] = useState(true);
  const [showSlits, setShowSlits] = useState(false);
  const [cursorCoords, setCursorCoords] = useState<{ ra: number; dec: number } | null>(null);

  // Fetch markers and slits via React Query (cached per field)
  const { data: markers = [], isLoading: isLoadingMarkers } = useFieldObjectMarkers(selectedField);
  const { data: slits = [], isLoading: isLoadingSlits } = useFieldSlits(selectedField);
  const [popupState, setPopupState] = useState<{
    marker: MapObjectMarker; latLng: L.LatLng;
  } | null>(null);
  const [contextMenu, setContextMenu] = useState<{
    coords: { ra: number; dec: number };
    position: { x: number; y: number };
  } | null>(null);
  const mapWrapperRef = useRef<HTMLDivElement>(null);

  // Notify parent when field changes
  useEffect(() => {
    if (!selectedField) return;
    onFieldChangeProp?.(selectedField);
  }, [selectedField, onFieldChangeProp]);

  // Compute filtered marker count from field-specific markers + filter
  const filteredMarkerCount = useMemo(() => {
    if (!markerFilter) return undefined;
    return markers.filter(markerFilter).length;
  }, [markers, markerFilter]);

  // Bridge: map target_ids (used in shutters.object_id) → object_ids
  const targetIdToObjectId = useMemo(() => {
    const map = new Map<string, string>();
    for (const m of markers) {
      for (const tid of m.member_target_ids) {
        map.set(tid, m.object_id);
      }
    }
    return map;
  }, [markers]);

  // Build slit filter from filteredIdSet via target→object bridge
  const slitFilter = useMemo(() => {
    if (!filteredIdSet) return undefined;
    return (slit: SlitRegion | Shutter) => {
      const objectId = targetIdToObjectId.get(slit.object_id);
      return objectId ? filteredIdSet.has(objectId) : false;
    };
  }, [filteredIdSet, targetIdToObjectId]);

  // Compute filtered slit count for LayerControl display
  const filteredSlitCount = useMemo(() => {
    if (!slitFilter) return undefined;
    return slits.filter(slitFilter).length;
  }, [slits, slitFilter]);

  // Track whether we've applied the initialFilter (only for first render)
  const initialFilterApplied = useRef(false);
  // Track the last field we set a default layer for, so we don't reset
  // the active layer on fieldGroups recomputation (e.g. from router.replace)
  const prevFieldRef = useRef<string | null>(null);

  // Set initial active layer when field changes
  useEffect(() => {
    const fieldLayers = fieldGroups[selectedField] || [];
    if (fieldLayers.length === 0) return;

    // Only run default selection when the field actually changes
    if (prevFieldRef.current === selectedField) return;
    prevFieldRef.current = selectedField;

    let defaultLayer: MapLayer | null;

    // Use initialFilter only on first render for the initial field
    if (!initialFilterApplied.current && initialFilter && selectedField === initialField) {
      defaultLayer = fieldLayers.find(l => l.filter === initialFilter)
        || fieldLayers.find(l => l.filter === 'rgb')
        || fieldLayers.find(l => l.is_default)
        || fieldLayers[0] || null;
      initialFilterApplied.current = true;
    } else {
      defaultLayer = fieldLayers.find(l => l.filter === 'rgb')
        || fieldLayers.find(l => l.is_default) || fieldLayers[0] || null;
    }

    setActiveLayer(defaultLayer);
    // Ensure field/filter are always in the URL (so stored map URLs are complete)
    if (defaultLayer) {
      updateMapUrl({ field: defaultLayer.field, filter: defaultLayer.filter });
    }
  }, [selectedField, fieldGroups, initialFilter, initialField]);

  // Close popup when field changes
  useEffect(() => { setPopupState(null); }, [selectedField]);


  // Compute initial map center and zoom
  const mapConfig = useMemo(() => {
    if (!activeLayer) return null;

    const wcs = activeLayer.wcs_params;
    const crs = createFitsMapCRS(activeLayer.max_zoom, wcs.naxis2);

    let center: L.LatLngExpression;
    let zoom: number;

    if (initialCenter && (!initialField || initialField === selectedField)) {
      // Initial load with URL params
      const leafletPos = skyToLeaflet(wcs, initialCenter.ra, initialCenter.dec);
      center = [leafletPos.lat, leafletPos.lng];
      zoom = initialZoom ?? Math.max(activeLayer.min_zoom, activeLayer.max_zoom - 3);
    } else {
      // Default: center on image
      center = [wcs.naxis2 / 2, wcs.naxis1 / 2];
      zoom = activeLayer.min_zoom + 1;
    }

    // Bounds for panning — pad generously so the image can be placed anywhere
    const padY = wcs.naxis2 * 0.5;
    const padX = wcs.naxis1 * 0.5;
    const bounds = L.latLngBounds(
      L.latLng(-padY, -padX),
      L.latLng(wcs.naxis2 + padY, wcs.naxis1 + padX)
    );

    return { center, zoom, bounds, crs };
  }, [activeLayer, initialCenter, initialField, initialZoom, selectedField]);

  // Sync field/filter to URL when layer changes
  const handleFieldChange = useCallback((field: string) => {
    setSelectedField(field);
    // Layer will be set by the useEffect; URL field/filter updated there
    updateMapUrl({ field });
  }, []);

  const handleLayerChange = useCallback((layer: MapLayer) => {
    setActiveLayer(layer);
    updateMapUrl({ field: layer.field, filter: layer.filter });
  }, []);

  const handleContextMenu = useCallback((data: { coords: { ra: number; dec: number }; position: { x: number; y: number } }) => {
    const wrapper = mapWrapperRef.current;
    if (!wrapper) return;
    const rect = wrapper.getBoundingClientRect();
    setContextMenu({
      coords: data.coords,
      position: { x: data.position.x - rect.left, y: data.position.y - rect.top },
    });
  }, []);

  const closeContextMenu = useCallback(() => setContextMenu(null), []);

  if (!activeLayer || !mapConfig) {
    if (layers.length === 0) {
      return (
        <div className="flex items-center justify-center h-full bg-surface dark:bg-slate-900">
          <div className="text-center text-text-secondary dark:text-slate-400">
            <p className="text-lg font-medium mb-2">No map layers available</p>
            <p className="text-sm">Map tiles have not been generated yet.</p>
          </div>
        </div>
      );
    }
    return null;
  }

  const tileUrl = `${activeLayer.tile_base_url}/{z}/{x}/{y}.png?v=${activeLayer.tile_version}`;

  return (
    <div ref={mapWrapperRef} className="relative h-full w-full">
      <style dangerouslySetInnerHTML={{ __html: pixelatedTileStyle }} />
      <MapContainer
        key={`${selectedField}-${activeLayer.max_zoom}-${activeLayer.wcs_params.naxis2}`}
        center={mapConfig.center}
        zoom={mapConfig.zoom}
        crs={mapConfig.crs}
        maxBounds={mapConfig.bounds}
        maxBoundsViscosity={0.0}
        minZoom={activeLayer.min_zoom}
        maxZoom={activeLayer.max_zoom + 3}
        preferCanvas={true}
        style={{ height: '100%', width: '100%', background: '#0f172a' }}
        attributionControl={false}
      >
        <TileLayer
          key={activeLayer.filter}
          url={tileUrl}
          tms={false}
          minZoom={activeLayer.min_zoom}
          maxZoom={activeLayer.max_zoom + 3}
          maxNativeZoom={activeLayer.max_zoom}
          minNativeZoom={activeLayer.min_zoom}
          noWrap={true}
          bounds={mapConfig.bounds}
          errorTileUrl=""
        />

        <MapUpdater activeLayer={activeLayer} bounds={mapConfig.bounds} />

        <MapEvents
          wcs={activeLayer.wcs_params}
          onMouseMove={setCursorCoords}
          onContextMenu={handleContextMenu}
          onMoveStart={closeContextMenu}
        />

        {/* Canvas-rendered slit overlay (below markers) */}
        <CanvasSlitLayer
          slits={slits}
          wcs={activeLayer.wcs_params}
          visible={showSlits}
          slitFilter={slitFilter}
        />

        {/* Canvas-rendered object markers */}
        <CanvasMarkerLayer
          markers={markers}
          wcs={activeLayer.wcs_params}
          visible={showMarkers}
          highlightObjectId={highlightObjectId}
          markerFilter={markerFilter}
          onMarkerClick={(marker, latLng) => setPopupState({ marker, latLng })}
        />

        {/* Popup for clicked marker (standalone, rendered by React) */}
        {popupState && (() => {
          const qualityLabel = QUALITY_LABELS.find(q => q.value === popupState.marker.redshift_quality);
          return (
            <Popup
              position={popupState.latLng}
              eventHandlers={{ remove: () => setPopupState(null) }}
            >
              <div className="text-sm min-w-[200px]">
                <div className="font-mono font-bold mb-1">
                  <Link
                    href={`/nirspec/objects/${encodeURIComponent(popupState.marker.object_id)}`}
                    className="text-blue-600 hover:text-blue-800 underline"
                    onClick={() => sessionStorage.setItem('campfire-map-return-url', window.location.href)}
                  >
                    {popupState.marker.object_id}
                  </Link>
                </div>
                <div className="space-y-0.5 text-xs">
                  {popupState.marker.redshift !== null && (
                    <div>z = {popupState.marker.redshift.toFixed(4)}</div>
                  )}
                  <div>
                    Quality: {qualityLabel?.icon} {qualityLabel?.label || 'Unknown'}
                  </div>
                  <div className="text-gray-500">
                    {popupState.marker.n_targets} target{popupState.marker.n_targets !== 1 ? 's' : ''}, {popupState.marker.n_spectra} spectr{popupState.marker.n_spectra !== 1 ? 'a' : 'um'}
                  </div>
                  <div className="text-gray-500">
                    RA: {popupState.marker.ra.toFixed(5)}, Dec: {popupState.marker.dec.toFixed(5)}
                  </div>
                </div>
              </div>
            </Popup>
          );
        })()}
      </MapContainer>

      {/* Coordinate overlay */}
      <CoordinateOverlay coords={cursorCoords} />

      {/* Right-click context menu */}
      {contextMenu && (
        <MapContextMenu
          coords={contextMenu.coords}
          position={contextMenu.position}
          onClose={closeContextMenu}
        />
      )}

      {/* Layer control */}
      <LayerControl
        fields={fields}
        selectedField={selectedField}
        onFieldChange={handleFieldChange}
        layers={fieldGroups[selectedField] || []}
        activeLayer={activeLayer}
        onLayerChange={handleLayerChange}
        showMarkers={showMarkers}
        onToggleMarkers={setShowMarkers}
        markerCount={markers.length}
        isLoadingMarkers={isLoadingMarkers}
        filteredMarkerCount={filteredMarkerCount}
        showSlits={showSlits}
        onToggleSlits={setShowSlits}
        slitCount={slits.length}
        isLoadingSlits={isLoadingSlits}
        filteredSlitCount={filteredSlitCount}
        onOpenFilters={onOpenFilters}
        hasActiveFilters={hasActiveFilters}
      />
    </div>
  );
}
