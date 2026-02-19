'use client';

import { useEffect, useRef, useMemo } from 'react';
import L from 'leaflet';
import { useMap } from 'react-leaflet';
import type { MapMarker } from '@/lib/actions/map';
import type { WCSParams } from '@/lib/utils/wcs';
import { skyToPixel } from '@/lib/utils/wcs';

// ============================================
// Quality color mapping (shared with MapViewer)
// ============================================

const QUALITY_COLORS: Record<number, string> = {
  0: '#9ca3af', // Not inspected - gray
  1: '#ef4444', // Impossible - red
  2: '#f59e0b', // Tentative - amber
  3: '#f97316', // Probable - orange
  4: '#22c55e', // Secure - green
};

// ============================================
// Types
// ============================================

interface PreparedMarker {
  marker: MapMarker;
  latLng: L.LatLng;
  color: string;
  isHighlighted: boolean;
}

interface CachedPoint {
  marker: MapMarker;
  latLng: L.LatLng;
  x: number;
  y: number;
  radius: number;
}

export interface CanvasMarkerLayerProps {
  markers: MapMarker[];
  wcs: WCSParams;
  visible: boolean;
  highlightObjectId?: string;
  markerFilter?: (marker: MapMarker) => boolean;
  onMarkerClick: (marker: MapMarker, latLng: L.LatLng) => void;
}

// ============================================
// Canvas padding — fraction of viewport to
// pre-draw beyond edges (prevents pop-in
// during panning)
// ============================================

const PADDING = 0.3;

// ============================================
// Component
// ============================================

export function CanvasMarkerLayer({
  markers,
  wcs,
  visible,
  highlightObjectId,
  markerFilter,
  onMarkerClick,
}: CanvasMarkerLayerProps) {
  const map = useMap();

  // Pre-compute LatLng positions (expensive TAN projection, done once per data change)
  const prepared: PreparedMarker[] = useMemo(() => {
    const filtered = markerFilter ? markers.filter(markerFilter) : markers;
    return filtered.map(m => {
      const { x, y } = skyToPixel(wcs, m.ra, m.dec);
      return {
        marker: m,
        latLng: L.latLng(y, x), // Leaflet: lat=y, lng=x
        color: QUALITY_COLORS[m.redshift_quality] || QUALITY_COLORS[0],
        isHighlighted: m.object_id === highlightObjectId,
      };
    });
  }, [markers, wcs, markerFilter, highlightObjectId]);

  // Stable refs for event handlers (avoid re-binding on every render)
  const preparedRef = useRef(prepared);
  preparedRef.current = prepared;

  const onMarkerClickRef = useRef(onMarkerClick);
  onMarkerClickRef.current = onMarkerClick;

  const visibleRef = useRef(visible);
  visibleRef.current = visible;

  // Cache projected points for hit-testing (updated each redraw)
  const cachedPointsRef = useRef<CachedPoint[]>([]);

  // Main layer lifecycle
  useEffect(() => {
    const canvas = document.createElement('canvas');
    canvas.classList.add('leaflet-zoom-animated');
    canvas.style.pointerEvents = 'auto';

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

    // Hover throttle
    let hoverThrottled = false;

    // ---- Drawing ----

    function redraw() {
      if (!visibleRef.current) {
        canvas.width = 0;
        canvas.height = 0;
        cachedPointsRef.current = [];
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

      const items = preparedRef.current;
      const cached: CachedPoint[] = [];
      let highlightItem: { point: L.Point; prepared: PreparedMarker } | null = null;

      // Group by color for batch drawing
      const groups = new Map<string, L.Point[]>();

      for (const item of items) {
        const pt = map.latLngToLayerPoint(item.latLng);

        // Viewport culling
        if (pt.x < boundsMin.x - 12 || pt.x > boundsMax.x + 12 ||
            pt.y < boundsMin.y - 12 || pt.y > boundsMax.y + 12) continue;

        const radius = item.isHighlighted ? 10 : 6;

        cached.push({
          marker: item.marker,
          latLng: item.latLng,
          x: pt.x,
          y: pt.y,
          radius,
        });

        if (item.isHighlighted) {
          highlightItem = { point: pt, prepared: item };
          continue; // Draw highlighted marker last, on top
        }

        let group = groups.get(item.color);
        if (!group) {
          group = [];
          groups.set(item.color, group);
        }
        group.push(pt);
      }

      // Batch draw per color group
      for (const [color, points] of groups) {
        // Fill pass
        ctx!.beginPath();
        for (const pt of points) {
          ctx!.moveTo(pt.x + 6, pt.y);
          ctx!.arc(pt.x, pt.y, 6, 0, Math.PI * 2);
        }
        ctx!.fillStyle = color;
        ctx!.globalAlpha = 0.1;
        ctx!.fill();

        // Stroke pass
        ctx!.beginPath();
        for (const pt of points) {
          ctx!.moveTo(pt.x + 6, pt.y);
          ctx!.arc(pt.x, pt.y, 6, 0, Math.PI * 2);
        }
        ctx!.strokeStyle = color;
        ctx!.lineWidth = 2;
        ctx!.globalAlpha = 0.9;
        ctx!.stroke();
      }

      // Draw highlighted marker last (on top)
      if (highlightItem) {
        const { point: pt, prepared: item } = highlightItem;

        ctx!.beginPath();
        ctx!.arc(pt.x, pt.y, 10, 0, Math.PI * 2);
        ctx!.fillStyle = item.color;
        ctx!.globalAlpha = 0.5;
        ctx!.fill();

        ctx!.beginPath();
        ctx!.arc(pt.x, pt.y, 10, 0, Math.PI * 2);
        ctx!.strokeStyle = '#ffffff';
        ctx!.lineWidth = 3;
        ctx!.globalAlpha = 0.9;
        ctx!.stroke();
      }

      cachedPointsRef.current = cached;
    }

    // ---- Zoom animation (mirrors Leaflet Renderer._updateTransform) ----

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

    // ---- Hit testing ----

    function hitTest(e: MouseEvent): CachedPoint | null {
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      const layerPoint = map.mouseEventToLayerPoint(e as any);
      const cached = cachedPointsRef.current;

      let closest: CachedPoint | null = null;
      let closestDist = Infinity;

      for (let i = cached.length - 1; i >= 0; i--) {
        const m = cached[i];
        const dx = layerPoint.x - m.x;
        const dy = layerPoint.y - m.y;
        const distSq = dx * dx + dy * dy;
        const threshold = m.radius + 4; // radius + tolerance
        if (distSq < threshold * threshold && distSq < closestDist) {
          closest = m;
          closestDist = distSq;
        }
      }

      return closest;
    }

    function onClick(e: MouseEvent) {
      const hit = hitTest(e);
      if (hit) {
        // Stop propagation so map click doesn't fire (which would close popups)
        e.stopPropagation();
        onMarkerClickRef.current(hit.marker, hit.latLng);
      }
    }

    function onMouseMove(e: MouseEvent) {
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      if ((map as any).dragging?.moving?.() || (map as any)._animatingZoom) return;
      if (hoverThrottled) return;

      const hit = hitTest(e);
      canvas.style.cursor = hit ? 'pointer' : '';

      hoverThrottled = true;
      setTimeout(() => { hoverThrottled = false; }, 32);
    }

    function onMouseOut() {
      canvas.style.cursor = '';
    }

    // ---- Bind events ----

    map.on('moveend', redraw);
    map.on('zoomend', redraw);
    map.on('zoomanim', onAnimZoom as L.LeafletEventHandlerFn);
    map.on('zoom', onZoom);
    map.on('viewreset', redraw);

    canvas.addEventListener('click', onClick);
    canvas.addEventListener('mousemove', onMouseMove);
    canvas.addEventListener('mouseout', onMouseOut);

    // Prevent canvas clicks from propagating to the map container
    L.DomEvent.disableClickPropagation(canvas);

    // Initial draw
    redraw();

    // ---- Cleanup ----

    return () => {
      map.off('moveend', redraw);
      map.off('zoomend', redraw);
      map.off('zoomanim', onAnimZoom as L.LeafletEventHandlerFn);
      map.off('zoom', onZoom);
      map.off('viewreset', redraw);

      canvas.removeEventListener('click', onClick);
      canvas.removeEventListener('mousemove', onMouseMove);
      canvas.removeEventListener('mouseout', onMouseOut);

      if (canvas.parentNode) {
        canvas.parentNode.removeChild(canvas);
      }
      cachedPointsRef.current = [];
    };
  }, [map]); // Only re-create layer when map instance changes

  // Trigger redraw when prepared markers or visibility change
  useEffect(() => {
    // The layer was set up by the effect above; trigger a redraw by
    // firing a synthetic moveend (same as Leaflet's approach)
    map.fire('moveend');
  }, [map, prepared, visible]);

  return null;
}
