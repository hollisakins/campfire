'use client';

/**
 * Polygon mask editor for a single NIRCam exposure.
 *
 * Coordinate model:
 *   - The canonical storage frame is DS9 ``image`` (FITS 1-indexed). That
 *     means a vertex at the center of pixel (col=0, row=0, numpy index)
 *     stores as (1, 1).
 *   - Internally the SVG viewBox is in raw PNG pixel space (file-y down,
 *     0-indexed, half-pixel = pixel edge). This makes drag math simple
 *     and matches the <img> element it overlays.
 *   - PNGs are written by the pipeline with origin='lower', so PNG file
 *     row 0 corresponds to numpy row H-1 (the TOP of the displayed image
 *     in astronomical convention). The svg↔ds9 transform below handles
 *     the Y flip.
 *
 *   svg → ds9 image:  X = svg_x + 0.5,  Y = H + 0.5 - svg_y
 *   ds9 image → svg:  svg_x = X - 0.5,  svg_y = H + 0.5 - Y
 */

import React, { useCallback, useEffect, useRef, useState } from 'react';
import {
  MousePointer2, PencilLine, Hand, Trash2, Save, Loader2, Check,
} from 'lucide-react';
import { Button } from '@/components/ui/Button';
import type { MaskPolygon, MaskRegionsPayload } from '@/lib/types';

type Mode = 'inspect' | 'draw' | 'edit';

// Storage form uses [number, number] tuples but internal state holds object
// vertices so we can attach drag handles without bookkeeping per-index arrays.
interface SvgVertex { x: number; y: number; }
interface SvgPolygon {
  id: string;
  source: 'imported' | 'web';
  original_frame?: string;
  imported_from?: string;
  imported_at?: string;
  created_at?: string;
  modified_at?: string;
  label?: string;
  vertices: SvgVertex[];
}

interface Props {
  pngUrl: string;
  imageWidth: number;        // PNG width in pixels (= exposure NAXIS1)
  imageHeight: number;       // PNG height in pixels (= exposure NAXIS2)
  initialRegions: MaskRegionsPayload | null;
  onSave: (regions: MaskRegionsPayload) => Promise<{ error?: string }>;
}

function uuid() {
  // Crypto.randomUUID works in all modern browsers; if unavailable fall back.
  if (typeof crypto !== 'undefined' && 'randomUUID' in crypto) {
    return crypto.randomUUID();
  }
  return `${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

function ds9ToSvg(v: [number, number], h: number): SvgVertex {
  return { x: v[0] - 0.5, y: h + 0.5 - v[1] };
}
function svgToDs9(v: SvgVertex, h: number): [number, number] {
  return [v.x + 0.5, h + 0.5 - v.y];
}

function fromPayload(payload: MaskRegionsPayload | null, h: number): SvgPolygon[] {
  if (!payload?.polygons) return [];
  return payload.polygons.map((p) => ({
    id: p.id,
    source: p.source,
    original_frame: p.original_frame,
    imported_from: p.imported_from,
    imported_at: p.imported_at,
    created_at: p.created_at,
    modified_at: p.modified_at,
    label: p.label,
    vertices: p.vertices.map((v) => ds9ToSvg(v, h)),
  }));
}

function toPayload(polys: SvgPolygon[], h: number): MaskRegionsPayload {
  const polygons: MaskPolygon[] = polys.map((p) => ({
    id: p.id,
    source: p.source,
    original_frame: p.original_frame,
    imported_from: p.imported_from,
    imported_at: p.imported_at,
    created_at: p.created_at,
    modified_at: p.modified_at ?? new Date().toISOString(),
    label: p.label,
    vertices: p.vertices.map((v) => svgToDs9(v, h)),
  }));
  return { version: 1, polygons };
}

export default function MaskEditor({
  pngUrl, imageWidth, imageHeight, initialRegions, onSave,
}: Props) {
  const [polygons, setPolygons] = useState<SvgPolygon[]>(
    () => fromPayload(initialRegions, imageHeight)
  );
  const [mode, setMode] = useState<Mode>('inspect');
  const [draftVertices, setDraftVertices] = useState<SvgVertex[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [dragging, setDragging] = useState<
    | { kind: 'pan'; startClient: [number, number]; startTranslate: [number, number] }
    | { kind: 'vertex'; polyId: string; vertexIndex: number }
    | null
  >(null);
  const [dirty, setDirty] = useState(false);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [savedAt, setSavedAt] = useState<number | null>(null);

  // View transform: PNG pixel coords → screen
  const containerRef = useRef<HTMLDivElement>(null);
  const svgRef = useRef<SVGSVGElement>(null);
  const [scale, setScale] = useState(1);
  const [translate, setTranslate] = useState<[number, number]>([0, 0]);

  // Fit-to-container on first mount.
  useEffect(() => {
    if (!containerRef.current) return;
    const cw = containerRef.current.clientWidth;
    const ch = containerRef.current.clientHeight;
    const s = Math.min(cw / imageWidth, ch / imageHeight) * 0.95;
    setScale(s);
    setTranslate([
      (cw - imageWidth * s) / 2,
      (ch - imageHeight * s) / 2,
    ]);
  }, [imageWidth, imageHeight]);

  // Re-sync if parent swaps exposure under us.
  useEffect(() => {
    setPolygons(fromPayload(initialRegions, imageHeight));
    setDirty(false);
    setSelectedId(null);
    setDraftVertices([]);
  }, [initialRegions, imageHeight]);

  const markDirty = useCallback(() => { setDirty(true); setSavedAt(null); }, []);

  // ----- coordinate conversion: client (screen) → svg (PNG pixel) -----
  const clientToSvg = useCallback((clientX: number, clientY: number): SvgVertex | null => {
    if (!containerRef.current) return null;
    const rect = containerRef.current.getBoundingClientRect();
    const cx = clientX - rect.left - translate[0];
    const cy = clientY - rect.top - translate[1];
    return { x: cx / scale, y: cy / scale };
  }, [scale, translate]);

  // ----- wheel zoom (cursor-anchored) -----
  // React's onWheel is registered as a *passive* listener since React 17, so
  // e.preventDefault() is silently ignored and the page scrolls underneath.
  // Attach the listener manually with { passive: false } so the zoom owns
  // the wheel events when the cursor is over the canvas.
  useEffect(() => {
    const node = containerRef.current;
    if (!node) return;
    const handler = (e: WheelEvent) => {
      e.preventDefault();
      const rect = node.getBoundingClientRect();
      const mx = e.clientX - rect.left;
      const my = e.clientY - rect.top;
      const factor = Math.exp(-e.deltaY * 0.0015);
      // Functional updates — wheel events fire faster than React renders, so
      // closing over `scale`/`translate` from the last render would drop
      // intermediate ticks.
      setScale((prevScale) => {
        const newScale = Math.max(0.05, Math.min(20, prevScale * factor));
        setTranslate(([tx, ty]) => {
          // Keep the PNG point under the cursor stationary:
          //   client = translate + svg * scale
          const svgPt = {
            x: (mx - tx) / prevScale,
            y: (my - ty) / prevScale,
          };
          return [mx - svgPt.x * newScale, my - svgPt.y * newScale];
        });
        return newScale;
      });
    };
    node.addEventListener('wheel', handler, { passive: false });
    return () => node.removeEventListener('wheel', handler);
  }, []);

  const finalizeDraft = useCallback(() => {
    if (draftVertices.length < 3) {
      setDraftVertices([]);
      return;
    }
    const now = new Date().toISOString();
    setPolygons((ps) => [...ps, {
      id: uuid(),
      source: 'web',
      vertices: draftVertices,
      created_at: now,
      modified_at: now,
    }]);
    setDraftVertices([]);
    markDirty();
  }, [draftVertices, markDirty]);

  // ----- pointer interactions -----
  const onPointerDown = useCallback((e: React.PointerEvent) => {
    // Shift+drag, middle mouse, or inspect-mode drag → pan.
    const isPanGesture = e.shiftKey || e.button === 1 || mode === 'inspect';

    // Vertex grab in edit mode is handled by the vertex's own handler;
    // here we only handle pan + drawing-mode clicks.
    if (mode === 'draw' && e.button === 0) {
      const pt = clientToSvg(e.clientX, e.clientY);
      if (!pt) return;
      // Close polygon by clicking near the first vertex.
      if (draftVertices.length >= 3) {
        const first = draftVertices[0];
        const dx = (pt.x - first.x) * scale;
        const dy = (pt.y - first.y) * scale;
        if (Math.hypot(dx, dy) < 12) {
          finalizeDraft();
          return;
        }
      }
      setDraftVertices((vs) => [...vs, pt]);
      return;
    }

    if (isPanGesture && e.button === 0) {
      (e.target as Element).setPointerCapture?.(e.pointerId);
      setDragging({
        kind: 'pan',
        startClient: [e.clientX, e.clientY],
        startTranslate: [translate[0], translate[1]],
      });
    }
  }, [mode, draftVertices, clientToSvg, scale, translate, finalizeDraft]);

  const onPointerMove = useCallback((e: React.PointerEvent) => {
    if (!dragging) return;
    if (dragging.kind === 'pan') {
      setTranslate([
        dragging.startTranslate[0] + (e.clientX - dragging.startClient[0]),
        dragging.startTranslate[1] + (e.clientY - dragging.startClient[1]),
      ]);
    } else if (dragging.kind === 'vertex') {
      const pt = clientToSvg(e.clientX, e.clientY);
      if (!pt) return;
      setPolygons((ps) => ps.map((p) =>
        p.id !== dragging.polyId ? p :
          { ...p, vertices: p.vertices.map((v, i) =>
              i === dragging.vertexIndex ? pt : v) }
      ));
      markDirty();
    }
  }, [dragging, clientToSvg, markDirty]);

  const onPointerUp = useCallback(() => setDragging(null), []);

  // ----- keyboard: Enter/Escape (draw), Backspace (vertex undo) -----
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (mode === 'draw') {
        if (e.key === 'Enter') { e.preventDefault(); finalizeDraft(); }
        if (e.key === 'Escape') { e.preventDefault(); setDraftVertices([]); }
        if (e.key === 'Backspace') {
          e.preventDefault();
          setDraftVertices((vs) => vs.slice(0, -1));
        }
      }
      if (mode === 'edit' && (e.key === 'Backspace' || e.key === 'Delete')
          && selectedId) {
        e.preventDefault();
        setPolygons((ps) => ps.filter((p) => p.id !== selectedId));
        setSelectedId(null);
        markDirty();
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [mode, selectedId, markDirty, finalizeDraft]);

  const handleSave = useCallback(async () => {
    setSaving(true);
    setSaveError(null);
    try {
      const result = await onSave(toPayload(polygons, imageHeight));
      if (result.error) {
        setSaveError(result.error);
      } else {
        setDirty(false);
        setSavedAt(Date.now());
      }
    } finally {
      setSaving(false);
    }
  }, [polygons, imageHeight, onSave]);

  const cursorClass = mode === 'inspect' ? 'cursor-grab' :
                      mode === 'draw'    ? 'cursor-crosshair' :
                                           'cursor-default';

  return (
    <div className="flex flex-col h-full">
      {/* Toolbar */}
      <div className="flex items-center justify-between p-2 border-b border-border dark:border-slate-700 bg-surface dark:bg-slate-900 flex-shrink-0">
        <div className="flex items-center gap-1">
          <ToolButton active={mode === 'inspect'} onClick={() => { setMode('inspect'); setDraftVertices([]); }}
            label="Inspect (pan/zoom)"><Hand className="w-4 h-4" /></ToolButton>
          <ToolButton active={mode === 'draw'} onClick={() => { setMode('draw'); setSelectedId(null); }}
            label="Draw polygon"><PencilLine className="w-4 h-4" /></ToolButton>
          <ToolButton active={mode === 'edit'} onClick={() => { setMode('edit'); setDraftVertices([]); }}
            label="Edit / delete"><MousePointer2 className="w-4 h-4" /></ToolButton>
          {mode === 'edit' && selectedId && (
            <ToolButton onClick={() => {
              setPolygons((ps) => ps.filter((p) => p.id !== selectedId));
              setSelectedId(null);
              markDirty();
            }} label="Delete selected polygon">
              <Trash2 className="w-4 h-4 text-red-500" />
            </ToolButton>
          )}
        </div>
        <div className="flex items-center gap-3 text-xs text-text-secondary dark:text-slate-400">
          <span>{polygons.length} polygon{polygons.length === 1 ? '' : 's'}</span>
          <span>{(scale * 100).toFixed(0)}%</span>
          {saveError && <span className="text-red-500">{saveError}</span>}
          <Button onClick={handleSave} disabled={saving || !dirty} size="sm">
            {saving ? (<><Loader2 className="w-4 h-4 mr-1 animate-spin" />Saving</>) :
             savedAt && !dirty ? (<><Check className="w-4 h-4 mr-1" />Saved</>) :
             (<><Save className="w-4 h-4 mr-1" />Save</>)}
          </Button>
        </div>
      </div>

      {/* Canvas */}
      <div
        ref={containerRef}
        className={`flex-1 relative overflow-hidden bg-black select-none ${cursorClass}`}
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerUp}
        onPointerLeave={onPointerUp}
      >
        <div
          className="absolute top-0 left-0 origin-top-left"
          style={{
            transform: `translate(${translate[0]}px, ${translate[1]}px) scale(${scale})`,
            width: imageWidth,
            height: imageHeight,
          }}
        >
          {/* eslint-disable-next-line @next/next/no-img-element */}
          <img
            src={pngUrl}
            width={imageWidth}
            height={imageHeight}
            alt="exposure preview"
            draggable={false}
            className="absolute inset-0 pointer-events-none"
            style={{ imageRendering: 'pixelated' }}
          />
          <svg
            ref={svgRef}
            width={imageWidth}
            height={imageHeight}
            viewBox={`0 0 ${imageWidth} ${imageHeight}`}
            className="absolute inset-0"
            style={{ overflow: 'visible' }}
          >
            {/* Saved polygons */}
            {polygons.map((p) => (
              <PolygonShape
                key={p.id}
                poly={p}
                selected={selectedId === p.id}
                mode={mode}
                scale={scale}
                onSelect={() => mode === 'edit' && setSelectedId(p.id)}
                onVertexDown={(idx, e) => {
                  if (mode !== 'edit') return;
                  e.stopPropagation();
                  (e.target as Element).setPointerCapture?.(e.pointerId);
                  setSelectedId(p.id);
                  setDragging({ kind: 'vertex', polyId: p.id, vertexIndex: idx });
                }}
              />
            ))}

            {/* Draft polygon-in-progress */}
            {draftVertices.length > 0 && (
              <g>
                <polyline
                  points={draftVertices.map((v) => `${v.x},${v.y}`).join(' ')}
                  fill="none"
                  stroke="#22d3ee"
                  strokeWidth={1.5 / scale}
                  strokeDasharray={`${4 / scale} ${4 / scale}`}
                />
                {draftVertices.map((v, i) => (
                  <circle
                    key={i}
                    cx={v.x} cy={v.y}
                    r={4 / scale}
                    fill={i === 0 ? '#0891b2' : '#22d3ee'}
                    stroke="white"
                    strokeWidth={1 / scale}
                  />
                ))}
              </g>
            )}
          </svg>
        </div>

        {/* Help footer */}
        <div className="absolute bottom-2 left-2 right-2 text-[11px] text-white/70 pointer-events-none font-mono">
          {mode === 'inspect' && 'drag = pan • wheel = zoom • shift+drag = pan in any mode'}
          {mode === 'draw'    && 'click = add vertex • click first vertex / Enter = close • Esc = cancel • Backspace = undo vertex'}
          {mode === 'edit'    && 'click polygon = select • drag vertex = move • Delete = remove polygon'}
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Polygon SVG shape (with vertex handles in edit mode)
// ---------------------------------------------------------------------------

function PolygonShape({
  poly, selected, mode, scale, onSelect, onVertexDown,
}: {
  poly: SvgPolygon;
  selected: boolean;
  mode: Mode;
  scale: number;
  onSelect: () => void;
  onVertexDown: (vertexIndex: number, e: React.PointerEvent) => void;
}) {
  const pointsStr = poly.vertices.map((v) => `${v.x},${v.y}`).join(' ');
  const stroke = selected ? '#fbbf24' : (poly.source === 'imported' ? '#a78bfa' : '#22c55e');
  const fill   = selected ? 'rgba(251, 191, 36, 0.18)'
               : poly.source === 'imported' ? 'rgba(167, 139, 250, 0.15)'
               : 'rgba(34, 197, 94, 0.18)';
  const interactive = mode === 'edit';
  return (
    <g style={{ pointerEvents: interactive ? 'auto' : 'none' }}>
      <polygon
        points={pointsStr}
        fill={fill}
        stroke={stroke}
        strokeWidth={1.5 / scale}
        onPointerDown={(e) => { if (interactive) { e.stopPropagation(); onSelect(); } }}
      />
      {interactive && selected && poly.vertices.map((v, i) => (
        <circle
          key={i}
          cx={v.x} cy={v.y}
          r={5 / scale}
          fill="#fbbf24"
          stroke="white"
          strokeWidth={1 / scale}
          style={{ cursor: 'grab' }}
          onPointerDown={(e) => onVertexDown(i, e)}
        />
      ))}
    </g>
  );
}

function ToolButton({
  active = false, onClick, label, children,
}: {
  active?: boolean;
  onClick: () => void;
  label: string;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      title={label}
      aria-label={label}
      className={`p-1.5 rounded text-sm ${
        active
          ? 'bg-primary/15 text-primary'
          : 'text-text-secondary dark:text-slate-400 hover:bg-surface-hover dark:hover:bg-slate-800'
      }`}
    >
      {children}
    </button>
  );
}
