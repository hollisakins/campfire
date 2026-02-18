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

function createFitsMapCRS(maxZoom: number): L.CRS {
  const scale = Math.pow(2, maxZoom);
  return L.Util.extend({}, L.CRS.Simple, {
    transformation: new L.Transformation(1 / scale, 0, -1 / scale, 256),
  }) as L.CRS;
}

// ============================================
// Sub-components that use map hooks
// ============================================

interface MapEventsProps {
  wcs: WCSParams | null;
  onViewportChange: (bounds: L.LatLngBounds) => void;
  onMouseMove: (coords: { ra: number; dec: number } | null) => void;
}

function MapEvents({ wcs, onViewportChange, onMouseMove }: MapEventsProps) {
  const debounceRef = useRef<ReturnType<typeof setTimeout>>(undefined);

  useMapEvents({
    moveend: (e) => {
      if (debounceRef.current) clearTimeout(debounceRef.current);
      debounceRef.current = setTimeout(() => {
        onViewportChange(e.target.getBounds());
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
  initialCenter?: { ra: number; dec: number };
  initialZoom?: number;
  highlightObjectId?: string;
}

export function MapViewer({
  layers,
  initialField,
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

  // Set initial active layer when field changes
  useEffect(() => {
    const fieldLayers = fieldGroups[selectedField] || [];
    const defaultLayer = fieldLayers.find(l => l.is_default) || fieldLayers[0] || null;
    setActiveLayer(defaultLayer);
    setMarkers([]);
  }, [selectedField, fieldGroups]);

  // Compute initial map center and zoom
  const mapConfig = useMemo(() => {
    if (!activeLayer) return null;

    const wcs = activeLayer.wcs_params;
    const crs = createFitsMapCRS(activeLayer.max_zoom);

    let center: L.LatLngExpression;
    let zoom: number;

    if (initialCenter && initialField === selectedField) {
      const leafletPos = skyToLeaflet(wcs, initialCenter.ra, initialCenter.dec);
      center = [leafletPos.lat, leafletPos.lng];
      zoom = initialZoom || Math.max(activeLayer.min_zoom, activeLayer.max_zoom - 3);
    } else {
      // Center on image center
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

  // Load markers when viewport changes
  const handleViewportChange = useCallback(
    async (bounds: L.LatLngBounds) => {
      if (!activeLayer || !showMarkers) return;

      const wcs = activeLayer.wcs_params;
      const sw = leafletToSky(wcs, bounds.getSouth(), bounds.getWest());
      const ne = leafletToSky(wcs, bounds.getNorth(), bounds.getEast());

      // RA might be inverted (CD1_1 is negative)
      const raMin = Math.min(sw.ra, ne.ra);
      const raMax = Math.max(sw.ra, ne.ra);
      const decMin = Math.min(sw.dec, ne.dec);
      const decMax = Math.max(sw.dec, ne.dec);

      setIsLoadingMarkers(true);
      try {
        const { getMapMarkers } = await import('@/lib/actions/map');
        const result = await getMapMarkers(raMin, raMax, decMin, decMax, selectedField);
        setMarkers(result.markers);
      } catch (err) {
        console.error('Failed to load map markers:', err);
      } finally {
        setIsLoadingMarkers(false);
      }
    },
    [activeLayer, showMarkers, selectedField]
  );

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

  const tileUrl = `${activeLayer.tile_base_url}/{z}/{x}/{y}.png`;

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
          maxZoom={activeLayer.max_zoom}
          maxNativeZoom={activeLayer.max_zoom}
          minNativeZoom={activeLayer.min_zoom}
          noWrap={true}
          bounds={mapConfig.bounds}
          errorTileUrl=""
        />

        <MapEvents
          wcs={activeLayer.wcs_params}
          onViewportChange={handleViewportChange}
          onMouseMove={setCursorCoords}
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
        onFieldChange={setSelectedField}
        layers={fieldGroups[selectedField] || []}
        activeLayer={activeLayer}
        onLayerChange={setActiveLayer}
        showMarkers={showMarkers}
        onToggleMarkers={setShowMarkers}
        markerCount={markers.length}
        isLoadingMarkers={isLoadingMarkers}
      />
    </div>
  );
}
