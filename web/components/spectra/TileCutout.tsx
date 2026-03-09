'use client';

import React, { useEffect, useRef, useMemo, useState } from 'react';
import Link from 'next/link';
import L from 'leaflet';
import type { MapLayer, Shutter } from '@/lib/actions/map';
import { skyToPixel } from '@/lib/utils/wcs';

import 'leaflet/dist/leaflet.css';

// ============================================
// Constants
// ============================================

const SHUTTER_WIDTH_ARCSEC = 0.22;
const SHUTTER_HEIGHT_ARCSEC = 0.46;

// ============================================
// CRS factory (matches MapViewer)
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
// Types
// ============================================

interface TileCutoutProps {
  objectId: string;
  ra: number;
  dec: number;
  field: string;
  mapLayer: MapLayer | null;
  shutters: Shutter[];
  size?: number;
}

// ============================================
// Component
// ============================================

export function TileCutout({
  objectId,
  ra,
  dec,
  field,
  mapLayer,
  shutters,
  size = 300,
}: TileCutoutProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const mapRef = useRef<L.Map | null>(null);
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const [ready, setReady] = useState(false);

  // Compute map config from layer
  const mapConfig = useMemo(() => {
    if (!mapLayer) return null;
    const wcs = mapLayer.wcs_params;
    const crs = createFitsMapCRS(mapLayer.max_zoom, wcs.naxis2);
    const pixel = skyToPixel(wcs, ra, dec);
    const center: L.LatLngExpression = [pixel.y, pixel.x];
    // Zoom level that gives ~5" field of view (comparable to original 3" cutouts
    // but with some surrounding context for the shutter overlay)
    const pixPerArcsec = 1 / (Math.abs(wcs.cd2_2) * 3600);
    const viewportPx = size;
    const desiredArcsec = 3;
    const desiredPixels = desiredArcsec * pixPerArcsec;
    const scaleFactor = viewportPx / desiredPixels;
    const zoom = mapLayer.max_zoom + Math.log2(scaleFactor);
    const clampedZoom = Math.max(mapLayer.min_zoom, Math.min(mapLayer.max_zoom + 3, zoom));

    return { center, zoom: clampedZoom, crs, wcs };
  }, [mapLayer, ra, dec, size]);

  // Initialize/update Leaflet map
  useEffect(() => {
    if (!containerRef.current || !mapLayer || !mapConfig) return;

    // Clean up previous map
    if (mapRef.current) {
      mapRef.current.remove();
      mapRef.current = null;
    }

    const map = L.map(containerRef.current, {
      crs: mapConfig.crs,
      center: mapConfig.center,
      zoom: mapConfig.zoom,
      dragging: false,
      zoomControl: false,
      scrollWheelZoom: false,
      doubleClickZoom: false,
      touchZoom: false,
      boxZoom: false,
      keyboard: false,
      attributionControl: false,
      fadeAnimation: false,
      zoomAnimation: false,
    });

    const tileUrl = `${mapLayer.tile_base_url}/{z}/{x}/{y}.png?v=${mapLayer.tile_version}`;
    L.tileLayer(tileUrl, {
      tms: false,
      minZoom: mapLayer.min_zoom,
      maxZoom: mapLayer.max_zoom + 3,
      maxNativeZoom: mapLayer.max_zoom,
      minNativeZoom: mapLayer.min_zoom,
      noWrap: true,
      errorTileUrl: '',
    }).addTo(map);

    mapRef.current = map;

    // Create shutter overlay canvas
    const canvas = document.createElement('canvas');
    canvas.style.pointerEvents = 'none';
    canvas.style.position = 'absolute';
    canvas.style.top = '0';
    canvas.style.left = '0';
    canvas.style.width = '100%';
    canvas.style.height = '100%';
    canvas.style.zIndex = '400';
    containerRef.current.appendChild(canvas);
    canvasRef.current = canvas;

    // Mark ready after tiles start loading
    map.whenReady(() => setReady(true));

    return () => {
      if (canvasRef.current?.parentNode) {
        canvasRef.current.parentNode.removeChild(canvasRef.current);
      }
      canvasRef.current = null;
      map.remove();
      mapRef.current = null;
      setReady(false);
    };
  }, [mapLayer, mapConfig]);

  // Draw shutter overlay
  useEffect(() => {
    if (!ready || !mapRef.current || !canvasRef.current || !mapConfig) return;

    const map = mapRef.current;
    const canvas = canvasRef.current;
    const wcs = mapConfig.wcs;
    const retina = window.devicePixelRatio || 1;

    canvas.width = size * retina;
    canvas.height = size * retina;
    canvas.style.width = size + 'px';
    canvas.style.height = size + 'px';

    const ctx = canvas.getContext('2d');
    if (!ctx) return;
    ctx.scale(retina, retina);

    // Compute pixels-per-arcsec at current zoom
    const refPx = skyToPixel(wcs, wcs.crval1, wcs.crval2);
    const offPx = skyToPixel(wcs, wcs.crval1, wcs.crval2 + 1 / 3600);
    const refPt = map.latLngToContainerPoint(L.latLng(refPx.y, refPx.x));
    const offPt = map.latLngToContainerPoint(L.latLng(offPx.y, offPx.x));
    const pxPerArcsec = Math.hypot(offPt.x - refPt.x, offPt.y - refPt.y);

    const widthPx = SHUTTER_WIDTH_ARCSEC * pxPerArcsec;
    const heightPx = SHUTTER_HEIGHT_ARCSEC * pxPerArcsec;

    if (widthPx < 1) return;

    const halfW = widthPx / 2;
    const halfH = heightPx / 2;

    for (const shutter of shutters) {
      const pixel = skyToPixel(wcs, shutter.center_ra, shutter.center_dec);
      const pt = map.latLngToContainerPoint(L.latLng(pixel.y, pixel.x));
      const paRad = shutter.position_angle * (Math.PI / 180);

      const isCurrentObject = shutter.object_id === objectId;
      const isStuck = shutter.shutter_state === 'stuck_closed';

      ctx.save();
      ctx.translate(pt.x, pt.y);
      ctx.rotate(-paRad);

      if (isStuck) {
        // Red dashed outline for stuck shutters
        ctx.strokeStyle = '#ef4444';
        ctx.lineWidth = 1.5;
        ctx.setLineDash([3, 2]);
        ctx.globalAlpha = 1.0;
        ctx.strokeRect(-halfW, -halfH, widthPx, heightPx);
      } else if (isCurrentObject) {
        // Lime fill + stroke for current object
        ctx.fillStyle = '#00ff00';
        ctx.globalAlpha = 0.2;
        ctx.fillRect(-halfW, -halfH, widthPx, heightPx);
        ctx.strokeStyle = '#00ff00';
        ctx.globalAlpha = 1.0;
        ctx.lineWidth = 1;
        ctx.setLineDash([]);
        ctx.strokeRect(-halfW, -halfH, widthPx, heightPx);
      } else {
        // Grey for other objects' shutters
        ctx.fillStyle = '#888888';
        ctx.globalAlpha = 0.05;
        ctx.fillRect(-halfW, -halfH, widthPx, heightPx);
        ctx.strokeStyle = '#888888';
        ctx.globalAlpha = 0.3;
        ctx.lineWidth = 0.5;
        ctx.setLineDash([]);
        ctx.strokeRect(-halfW, -halfH, widthPx, heightPx);
      }

      ctx.restore();
    }
  }, [ready, shutters, objectId, mapConfig, size]);

  // Show placeholder if no map layer available
  if (!mapLayer) {
    return (
      <div
        className="bg-gray-200 dark:bg-slate-700 rounded-lg flex items-center justify-center"
        style={{ width: size, height: size }}
      >
        <p className="text-gray-500 dark:text-slate-400 text-sm text-center px-4">
          No map tiles available
        </p>
      </div>
    );
  }

  return (
    <Link
      href={`/map?field=${encodeURIComponent(field)}&ra=${ra}&dec=${dec}&z=8&highlight=${encodeURIComponent(objectId)}`}
      className="block rounded-lg overflow-hidden border border-gray-300 dark:border-slate-600 hover:border-primary dark:hover:border-primary transition-colors"
      style={{ width: size, height: size }}
      title="View on map"
    >
      <div
        ref={containerRef}
        className="relative"
        style={{
          width: size,
          height: size,
          imageRendering: 'pixelated',
        }}
      />
    </Link>
  );
}
