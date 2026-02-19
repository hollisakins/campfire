'use client';

import React, { useState, useEffect, useCallback, useRef, useMemo } from 'react';
import L from 'leaflet';
import {
  MapContainer,
  TileLayer,
  CircleMarker,
  Popup,
  useMap,
  useMapEvents,
} from 'react-leaflet';
import Link from 'next/link';
import type { MapLayer, MapMarker } from '@/lib/actions/map';
import type { WCSParams } from '@/lib/utils/wcs';
import { leafletToSky, skyToLeaflet, formatRA, formatDec } from '@/lib/utils/wcs';
import { QUALITY_LABELS } from '@/lib/types';
import { LayerControl } from './LayerControl';
import { CoordinateOverlay } from './CoordinateOverlay';

import 'leaflet/dist/leaflet.css';

// ============================================
// Quality color mapping
// ============================================

const QUALITY_COLORS: Record<number, string> = {
  0: '#9ca3af', // Not inspected - gray
  1: '#ef4444', // Impossible - red
  2: '#f59e0b', // Tentative - amber
  3: '#f97316', // Probable - orange
  4: '#22c55e', // Secure - green
};

// ============================================
// Custom CRS for pixel-based tile coordinates
// ============================================

function createFitsMapCRS(maxZoom: number, naxis2: number, tileSize: number = 256): L.CRS {
  const scale = Math.pow(2, maxZoom);
  const nTilesY = Math.ceil(naxis2 / tileSize);
  const d = (nTilesY * tileSize - 1) / scale;
  return L.Util.extend({}, L.CRS.Simple, {
    transformation: new L.Transformation(1 / scale, 0, -1 / scale, d),
  }) as L.CRS;
}

// ============================================
// Sub-components that use map hooks
// ============================================

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
  onViewChange: (view: { ra: number; dec: number; zoom: number }) => void;
}

function MapEvents({ wcs, onMouseMove, onViewChange }: MapEventsProps) {
  const debounceRef = useRef<ReturnType<typeof setTimeout>>(undefined);

  useMapEvents({
    moveend: (e) => {
      if (!wcs) return;
      const center = e.target.getCenter();
      const sky = leafletToSky(wcs, center.lat, center.lng);
      const zoom = e.target.getZoom();
      // Update ref immediately (for preserving view across filter switches)
      onViewChange({ ...sky, zoom });
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
  });

  return null;
}

interface SetViewProps {
  center: L.LatLngExpression;
  zoom: number;
}

function SetView({ center, zoom }: SetViewProps) {
  const map = useMap();
  useEffect(() => {
    map.setView(center, zoom);
  }, [map, center, zoom]);
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
}

export function MapViewer({
  layers,
  initialField,
  initialFilter,
  initialCenter,
  initialZoom,
  highlightObjectId,
}: MapViewerProps) {
  // Group layers by field
  const fieldGroups = useMemo(() => {
    const groups: Record<string, MapLayer[]> = {};
    for (const layer of layers) {
      if (!groups[layer.field]) groups[layer.field] = [];
      groups[layer.field].push(layer);
    }
    return groups;
  }, [layers]);

  const fields = useMemo(() => Object.keys(fieldGroups).sort(), [fieldGroups]);

  // State
  const [selectedField, setSelectedField] = useState<string>(
    initialField || fields[0] || ''
  );
  const [activeLayer, setActiveLayer] = useState<MapLayer | null>(null);
  const [markers, setMarkers] = useState<MapMarker[]>([]);
  const [showMarkers, setShowMarkers] = useState(true);
  const [cursorCoords, setCursorCoords] = useState<{ ra: number; dec: number } | null>(null);
  const [isLoadingMarkers, setIsLoadingMarkers] = useState(false);

  // Track current viewport (RA/Dec/zoom) for preserving view across filter switches
  const currentViewRef = useRef<{ ra: number; dec: number; zoom: number } | null>(null);
  const handleViewChange = useCallback((view: { ra: number; dec: number; zoom: number }) => {
    currentViewRef.current = view;
  }, []);

  // Track whether we've applied the initialFilter (only for first render)
  const initialFilterApplied = useRef(false);

  // Set initial active layer when field changes
  useEffect(() => {
    // Clear saved viewport when switching fields (different image area)
    currentViewRef.current = null;

    const fieldLayers = fieldGroups[selectedField] || [];
    let defaultLayer: MapLayer | null;

    // Use initialFilter only on first render for the initial field
    if (!initialFilterApplied.current && initialFilter && selectedField === initialField) {
      defaultLayer = fieldLayers.find(l => l.filter === initialFilter)
        || fieldLayers.find(l => l.is_default)
        || fieldLayers[0] || null;
      initialFilterApplied.current = true;
    } else {
      defaultLayer = fieldLayers.find(l => l.is_default) || fieldLayers[0] || null;
    }

    setActiveLayer(defaultLayer);
    // Ensure field/filter are always in the URL (so stored map URLs are complete)
    if (defaultLayer) {
      updateMapUrl({ field: defaultLayer.field, filter: defaultLayer.filter });
    }
  }, [selectedField, fieldGroups, initialFilter, initialField]);

  // Load all markers for the selected field
  useEffect(() => {
    if (!selectedField) return;
    let cancelled = false;

    async function loadMarkers() {
      setIsLoadingMarkers(true);
      try {
        const { getFieldMarkers } = await import('@/lib/actions/map');
        const result = await getFieldMarkers(selectedField);
        if (!cancelled) setMarkers(result.markers);
      } catch (err) {
        console.error('Failed to load map markers:', err);
      } finally {
        if (!cancelled) setIsLoadingMarkers(false);
      }
    }

    loadMarkers();
    return () => { cancelled = true; };
  }, [selectedField]);

  // Compute initial map center and zoom
  const mapConfig = useMemo(() => {
    if (!activeLayer) return null;

    const wcs = activeLayer.wcs_params;
    const crs = createFitsMapCRS(activeLayer.max_zoom, wcs.naxis2);

    let center: L.LatLngExpression;
    let zoom: number;

    const currentView = currentViewRef.current;
    if (currentView) {
      // Restore current viewport (e.g. after filter switch within same field)
      const leafletPos = skyToLeaflet(wcs, currentView.ra, currentView.dec);
      center = [leafletPos.lat, leafletPos.lng];
      zoom = currentView.zoom;
    } else if (initialCenter && (!initialField || initialField === selectedField)) {
      // Initial load with URL params
      const leafletPos = skyToLeaflet(wcs, initialCenter.ra, initialCenter.dec);
      center = [leafletPos.lat, leafletPos.lng];
      zoom = initialZoom ?? Math.max(activeLayer.min_zoom, activeLayer.max_zoom - 3);
    } else {
      // Default: center on image
      center = [wcs.naxis2 / 2, wcs.naxis1 / 2];
      zoom = activeLayer.min_zoom + 1;
    }

    // Bounds for the full image
    const bounds = L.latLngBounds(
      L.latLng(0, 0),
      L.latLng(wcs.naxis2, wcs.naxis1)
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
    <div className="relative h-full w-full">
      <MapContainer
        key={`${selectedField}-${activeLayer.filter}`}
        center={mapConfig.center}
        zoom={mapConfig.zoom}
        crs={mapConfig.crs}
        maxBounds={mapConfig.bounds}
        maxBoundsViscosity={0.8}
        minZoom={activeLayer.min_zoom}
        maxZoom={activeLayer.max_zoom + 2}
        style={{ height: '100%', width: '100%', background: '#0f172a' }}
        attributionControl={false}
      >
        <TileLayer
          url={tileUrl}
          tms={false}
          minZoom={activeLayer.min_zoom}
          maxZoom={activeLayer.max_zoom + 2}
          maxNativeZoom={activeLayer.max_zoom}
          minNativeZoom={activeLayer.min_zoom}
          noWrap={true}
          bounds={mapConfig.bounds}
          errorTileUrl=""
        />

        <MapEvents
          wcs={activeLayer.wcs_params}
          onMouseMove={setCursorCoords}
          onViewChange={handleViewChange}
        />

        {/* Object markers */}
        {showMarkers && markers.map((marker) => {
          const pos = skyToLeaflet(activeLayer.wcs_params, marker.ra, marker.dec);
          const color = QUALITY_COLORS[marker.redshift_quality] || QUALITY_COLORS[0];
          const isHighlighted = marker.object_id === highlightObjectId;
          const qualityLabel = QUALITY_LABELS.find(q => q.value === marker.redshift_quality);

          return (
            <CircleMarker
              key={marker.object_id}
              center={[pos.lat, pos.lng]}
              radius={isHighlighted ? 10 : 6}
              pathOptions={{
                color: isHighlighted ? '#ffffff' : color,
                weight: isHighlighted ? 3 : 2,
                opacity: 0.9,
                fillColor: color,
                fillOpacity: isHighlighted ? 0.5 : 0.1,
              }}
            >
              <Popup>
                <div className="text-sm min-w-[180px]">
                  <div className="font-mono font-bold mb-1">
                    <Link
                      href={`/spectra/${encodeURIComponent(marker.object_id)}`}
                      className="text-blue-600 hover:text-blue-800 underline"
                      onClick={() => sessionStorage.setItem('campfire-map-return-url', window.location.href)}
                    >
                      {marker.object_id}
                    </Link>
                  </div>
                  <div className="space-y-0.5 text-xs">
                    {marker.redshift !== null && (
                      <div>z = {marker.redshift.toFixed(4)}</div>
                    )}
                    <div>
                      Quality: {qualityLabel?.icon} {qualityLabel?.label || 'Unknown'}
                    </div>
                    <div className="text-gray-500">
                      RA: {marker.ra.toFixed(5)}, Dec: {marker.dec.toFixed(5)}
                    </div>
                  </div>
                </div>
              </Popup>
            </CircleMarker>
          );
        })}
      </MapContainer>

      {/* Coordinate overlay */}
      <CoordinateOverlay coords={cursorCoords} />

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
      />
    </div>
  );
}
