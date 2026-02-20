'use client';

import { useEffect, useRef, useMemo } from 'react';
import L from 'leaflet';
import { useMap } from 'react-leaflet';
import type { SlitRegion } from '@/lib/actions/map';
import type { WCSParams } from '@/lib/utils/wcs';
import { skyToPixel } from '@/lib/utils/wcs';

// ============================================
// Per-observation color palette
// ============================================

const OBSERVATION_COLORS = [
  '#00ff00', // lime green
  '#00ccff', // cyan
  '#ff6600', // orange
  '#ff00ff', // magenta
  '#ffff00', // yellow
  '#00ffcc', // teal
  '#ff3399', // pink
  '#66ff33', // chartreuse
];

function getObservationColor(observation: string, observations: string[]): string {
  const idx = observations.indexOf(observation);
  return OBSERVATION_COLORS[idx % OBSERVATION_COLORS.length];
}

// ============================================
// Types
// ============================================

interface PreparedSlit {
  slit: SlitRegion;
  latLng: L.LatLng;
  paRad: number;  // pre-computed rotation in radians
  color: string;
}

export interface CanvasSlitLayerProps {
  slits: SlitRegion[];
  wcs: WCSParams;
  visible: boolean;
}

// ============================================
// Canvas padding — same as CanvasMarkerLayer
// ============================================

const PADDING = 0.3;

// ============================================
// Shutter dimensions in arcseconds
// ============================================

const SHUTTER_WIDTH_ARCSEC = 0.22;
const SHUTTER_HEIGHT_ARCSEC = 0.46;

// ============================================
// Component
// ============================================

export function CanvasSlitLayer({
  slits,
  wcs,
  visible,
}: CanvasSlitLayerProps) {
  const map = useMap();

  // Derive unique observation list for consistent color assignment
  const observations = useMemo(() => {
    return [...new Set(slits.map(s => s.observation))].sort();
  }, [slits]);

  // Pre-compute LatLng positions and rotation angles
  const prepared: PreparedSlit[] = useMemo(() => {
    return slits.map(s => {
      const { x, y } = skyToPixel(wcs, s.center_ra, s.center_dec);
      return {
        slit: s,
        latLng: L.latLng(y, x),
        paRad: s.position_angle * (Math.PI / 180),
        color: getObservationColor(s.observation, observations),
      };
    });
  }, [slits, wcs, observations]);

  // Stable refs for event handlers
  const preparedRef = useRef(prepared);
  preparedRef.current = prepared;

  const visibleRef = useRef(visible);
  visibleRef.current = visible;

  // Main layer lifecycle
  useEffect(() => {
    const canvas = document.createElement('canvas');
    canvas.classList.add('leaflet-zoom-animated');
    canvas.style.pointerEvents = 'none';

    const pane = map.getPane('overlayPane');
    if (!pane) return;
    pane.appendChild(canvas);

    const ctx = canvas.getContext('2d');
    if (!ctx) return;

    // State for zoom animation
    let drawCenter = map.getCenter();
    let drawZoom = map.getZoom();
    let boundsMin: L.Point;
    let boundsMax: L.Point;

    // ---- Drawing ----

    function redraw() {
      if (!visibleRef.current) {
        canvas.width = 0;
        canvas.height = 0;
        return;
      }

      const size = map.getSize();
      const p = PADDING;
      boundsMin = map.containerPointToLayerPoint(
        L.point(size.x * -p, size.y * -p)
      ).round();
      boundsMax = boundsMin.add(
        L.point(
          Math.round(size.x * (1 + p * 2)),
          Math.round(size.y * (1 + p * 2))
        )
      );

      drawCenter = map.getCenter();
      drawZoom = map.getZoom();

      const bWidth = boundsMax.x - boundsMin.x;
      const bHeight = boundsMax.y - boundsMin.y;
      const retina = L.Browser.retina ? 2 : 1;

      // Position canvas in layer coordinates
      L.DomUtil.setPosition(canvas, boundsMin);

      // Size canvas (retina-aware)
      canvas.width = retina * bWidth;
      canvas.height = retina * bHeight;
      canvas.style.width = bWidth + 'px';
      canvas.style.height = bHeight + 'px';

      if (L.Browser.retina) {
        ctx!.scale(2, 2);
      }

      // Translate so layer-point coords can be used directly
      ctx!.translate(-boundsMin.x, -boundsMin.y);

      // Compute pixels-per-arcsec at the current zoom by measuring
      // the layer-point distance between two sky positions 1" apart
      const refPx = skyToPixel(wcs, wcs.crval1, wcs.crval2);
      const offPx = skyToPixel(wcs, wcs.crval1, wcs.crval2 + 1 / 3600);
      const refPt = map.latLngToLayerPoint(L.latLng(refPx.y, refPx.x));
      const offPt = map.latLngToLayerPoint(L.latLng(offPx.y, offPx.x));
      const pxPerArcsec = Math.hypot(offPt.x - refPt.x, offPt.y - refPt.y);

      const widthPx = SHUTTER_WIDTH_ARCSEC * pxPerArcsec;
      const heightPx = SHUTTER_HEIGHT_ARCSEC * pxPerArcsec;

      // Skip drawing if shutters are too small to see
      if (widthPx < 1.5) return;

      const items = preparedRef.current;
      const halfW = widthPx / 2;
      const halfH = heightPx / 2;

      // Group by color for efficient batch drawing
      const groups = new Map<string, L.Point[]>();
      const paByPoint = new Map<string, number>();

      for (const item of items) {
        const pt = map.latLngToLayerPoint(item.latLng);

        // Viewport culling (generous margin for rotated rectangles)
        const margin = Math.max(widthPx, heightPx) + 4;
        if (pt.x < boundsMin.x - margin || pt.x > boundsMax.x + margin ||
            pt.y < boundsMin.y - margin || pt.y > boundsMax.y + margin) continue;

        const color = item.color;
        let group = groups.get(color);
        if (!group) {
          group = [];
          groups.set(color, group);
        }
        group.push(pt);
        // Store PA keyed by point identity
        paByPoint.set(`${pt.x},${pt.y}`, item.paRad);
      }

      // Draw each color group
      ctx!.lineWidth = 1;
      for (const [color, points] of groups) {
        // Fill pass
        ctx!.fillStyle = color;
        ctx!.globalAlpha = 0.08;
        for (const pt of points) {
          const pa = paByPoint.get(`${pt.x},${pt.y}`) || 0;
          ctx!.save();
          ctx!.translate(pt.x, pt.y);
          ctx!.rotate(-pa);
          ctx!.fillRect(-halfW, -halfH, widthPx, heightPx);
          ctx!.restore();
        }

        // Stroke pass
        ctx!.strokeStyle = color;
        ctx!.globalAlpha = 0.6;
        for (const pt of points) {
          const pa = paByPoint.get(`${pt.x},${pt.y}`) || 0;
          ctx!.save();
          ctx!.translate(pt.x, pt.y);
          ctx!.rotate(-pa);
          ctx!.strokeRect(-halfW, -halfH, widthPx, heightPx);
          ctx!.restore();
        }
      }
    }

    // ---- Zoom animation (mirrors CanvasMarkerLayer) ----

    function onAnimZoom(ev: L.ZoomAnimEvent) {
      updateTransform(ev.center, ev.zoom);
    }

    function onZoom() {
      updateTransform(map.getCenter(), map.getZoom());
    }

    function updateTransform(center: L.LatLng, zoom: number) {
      const scale = map.getZoomScale(zoom, drawZoom);
      const viewHalf = map.getSize().multiplyBy(0.5 + PADDING);
      const currentCenterPoint = map.project(drawCenter, zoom);
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      const origin = (map as any)._getNewPixelOrigin(center, zoom);
      const topLeftOffset = viewHalf.multiplyBy(-scale)
        .add(currentCenterPoint)
        .subtract(origin);

      if (L.Browser.any3d) {
        L.DomUtil.setTransform(canvas, topLeftOffset, scale);
      } else {
        L.DomUtil.setPosition(canvas, topLeftOffset);
      }
    }

    // ---- Bind events ----

    map.on('moveend', redraw);
    map.on('zoomend', redraw);
    map.on('zoomanim', onAnimZoom as L.LeafletEventHandlerFn);
    map.on('zoom', onZoom);
    map.on('viewreset', redraw);

    // Initial draw
    redraw();

    // ---- Cleanup ----

    return () => {
      map.off('moveend', redraw);
      map.off('zoomend', redraw);
      map.off('zoomanim', onAnimZoom as L.LeafletEventHandlerFn);
      map.off('zoom', onZoom);
      map.off('viewreset', redraw);

      if (canvas.parentNode) {
        canvas.parentNode.removeChild(canvas);
      }
    };
  }, [map, wcs]);

  // Trigger redraw when data or visibility changes
  useEffect(() => {
    map.fire('moveend');
  }, [map, prepared, visible]);

  return null;
}
