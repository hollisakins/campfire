'use client';

import { useEffect, useRef, useMemo } from 'react';
import L from 'leaflet';
import { useMap } from 'react-leaflet';
import type { SlitRegion, Shutter } from '@/lib/actions/map';
import type { WCSParams } from '@/lib/utils/wcs';
import { skyToPixel } from '@/lib/utils/wcs';
import { getObservationColor } from './observation-colors';

// ============================================
// Types
// ============================================

interface PreparedSlit {
  slit: SlitRegion | Shutter;
  latLng: L.LatLng;
  paRad: number;  // pre-computed rotation in radians
  color: string;
  isStuck?: boolean;
  shutterIdx: number;
  groupKey: string;  // slitlet group: object_id|observation|PA
}

export interface CanvasSlitLayerProps {
  slits: (SlitRegion | Shutter)[];
  wcs: WCSParams;
  visible: boolean;
  highlightObjectId?: string;
  slitFilter?: (slit: SlitRegion | Shutter) => boolean;
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
const SHUTTER_PITCH_ARCSEC = 0.53;

// ============================================
// Component
// ============================================

export function CanvasSlitLayer(props: CanvasSlitLayerProps) {
  const { slits, wcs, visible, slitFilter } = props;
  const map = useMap();

  // Derive unique observation list from FULL (unfiltered) slits for stable color assignment
  const observations = useMemo(() => {
    return [...new Set(slits.map(s => s.observation))].sort();
  }, [slits]);

  // Pre-compute LatLng positions, rotation angles, and slitlet grouping
  const prepared: PreparedSlit[] = useMemo(() => {
    const filtered = slitFilter ? slits.filter(slitFilter) : slits;
    return filtered.map(s => {
      const { x, y } = skyToPixel(wcs, s.center_ra, s.center_dec);
      const isShutter = 'shutter_state' in s;
      const isStuck = isShutter && (s as Shutter).shutter_state === 'stuck_closed';
      const ditherId = isShutter ? (s as Shutter).dither_id : 0;
      return {
        slit: s,
        latLng: L.latLng(y, x),
        paRad: s.position_angle * (Math.PI / 180),
        color: isStuck ? '#ef4444' : getObservationColor(s.observation, observations),
        isStuck,
        shutterIdx: s.shutter_idx,
        groupKey: `${s.object_id}|${s.observation}|${ditherId}`,
      };
    });
  }, [slits, wcs, observations, slitFilter]);

  // Stable refs for event handlers
  const preparedRef = useRef(prepared);
  preparedRef.current = prepared;

  const visibleRef = useRef(visible);
  visibleRef.current = visible;

  // Ref for decoupled data-change redraws (set inside main useEffect)
  const scheduleRedrawRef = useRef<() => void>(() => {});

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

    // rAF batching — coalesce rapid-fire redraws into one per frame
    let rafId = 0;
    function scheduleRedraw() {
      cancelAnimationFrame(rafId);
      rafId = requestAnimationFrame(() => redraw());
    }
    scheduleRedrawRef.current = scheduleRedraw;

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
      const pitchPx = SHUTTER_PITCH_ARCSEC * pxPerArcsec;

      // Group shutters by slitlet so we can compute aligned positions
      // from a single reference point (avoids floating-point jitter at low zoom)
      const slitletMap = new Map<string, PreparedSlit[]>();
      for (const item of items) {
        let group = slitletMap.get(item.groupKey);
        if (!group) {
          group = [];
          slitletMap.set(item.groupKey, group);
        }
        group.push(item);
      }

      // Compute aligned positions per slitlet and collect drawable items
      interface DrawItem {
        pt: L.Point;
        paRad: number;
        color: string;
        isStuck: boolean;
      }
      const drawItems: DrawItem[] = [];
      const margin = Math.max(widthPx, heightPx) + 4;

      for (const slitlet of slitletMap.values()) {
        // Use source shutter (idx=0) as reference, fall back to first
        const ref = slitlet.find(s => s.shutterIdx === 0) || slitlet[0];
        const refPt = map.latLngToLayerPoint(ref.latLng);

        // Compute PA direction unit vector in layer-point space
        // by projecting a sky point offset 1" along PA from the reference
        const refRa = ref.slit.center_ra;
        const refDec = ref.slit.center_dec;
        const pa = ref.paRad;
        const dRa = Math.sin(pa) / Math.cos(refDec * Math.PI / 180) / 3600;
        const dDec = Math.cos(pa) / 3600;
        const offPxSky = skyToPixel(wcs, refRa + dRa, refDec + dDec);
        const offPt = map.latLngToLayerPoint(L.latLng(offPxSky.y, offPxSky.x));
        const dirLen = Math.hypot(offPt.x - refPt.x, offPt.y - refPt.y);
        const ux = dirLen > 0 ? (offPt.x - refPt.x) / dirLen : 0;
        const uy = dirLen > 0 ? (offPt.y - refPt.y) / dirLen : 0;

        for (const item of slitlet) {
          const di = item.shutterIdx - ref.shutterIdx;
          const pt = L.point(
            refPt.x + di * pitchPx * ux,
            refPt.y + di * pitchPx * uy,
          );

          // Viewport culling
          if (pt.x < boundsMin.x - margin || pt.x > boundsMax.x + margin ||
              pt.y < boundsMin.y - margin || pt.y > boundsMax.y + margin) continue;

          drawItems.push({
            pt,
            paRad: item.paRad,
            color: item.color,
            isStuck: item.isStuck || false,
          });
        }
      }

      // Group by color for efficient batch drawing
      const groups = new Map<string, DrawItem[]>();
      for (const item of drawItems) {
        let group = groups.get(item.color);
        if (!group) {
          group = [];
          groups.set(item.color, group);
        }
        group.push(item);
      }

      // Draw each color group
      ctx!.lineWidth = 1;
      for (const [color, points] of groups) {
        // Fill pass
        ctx!.fillStyle = color;
        ctx!.globalAlpha = 0.08;
        for (const item of points) {
          ctx!.save();
          ctx!.translate(item.pt.x, item.pt.y);
          ctx!.rotate(-item.paRad);
          ctx!.fillRect(-halfW, -halfH, widthPx, heightPx);
          ctx!.restore();
        }

        // Stroke pass
        for (const item of points) {
          ctx!.save();
          ctx!.translate(item.pt.x, item.pt.y);
          ctx!.rotate(-item.paRad);
          ctx!.strokeStyle = item.isStuck ? '#ef4444' : color;
          ctx!.globalAlpha = item.isStuck ? 1.0 : 0.6;
          ctx!.lineWidth = item.isStuck ? 1.5 : 1;
          if (item.isStuck) ctx!.setLineDash([3, 2]);
          else ctx!.setLineDash([]);
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

    map.on('moveend', scheduleRedraw);
    map.on('zoomend', scheduleRedraw);
    map.on('zoomanim', onAnimZoom as L.LeafletEventHandlerFn);
    map.on('zoom', onZoom);
    map.on('viewreset', scheduleRedraw);

    // Initial draw
    scheduleRedraw();

    // ---- Cleanup ----

    return () => {
      cancelAnimationFrame(rafId);

      map.off('moveend', scheduleRedraw);
      map.off('zoomend', scheduleRedraw);
      map.off('zoomanim', onAnimZoom as L.LeafletEventHandlerFn);
      map.off('zoom', onZoom);
      map.off('viewreset', scheduleRedraw);

      if (canvas.parentNode) {
        canvas.parentNode.removeChild(canvas);
      }
    };
  }, [map, wcs]);

  // Trigger redraw when data or visibility changes
  useEffect(() => {
    scheduleRedrawRef.current();
  }, [prepared, visible]);

  return null;
}
