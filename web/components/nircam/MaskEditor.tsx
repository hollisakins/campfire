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
  Copy, ClipboardPaste,
} from 'lucide-react';
import { Button } from '@/components/ui/Button';
import type { MaskPolygon, MaskRegionsPayload } from '@/lib/types';
import { getMaskClipboard, setMaskClipboard } from '@/lib/mask-clipboard';
import FitsCanvas, { type FitsCanvasLoad } from './FitsCanvas';
import { STRETCH_MODES, COLORMAP_NAMES, type StretchMode, type ColormapName } from '@/lib/fits';
import { isPngCached } from '@/lib/nircam-exposure-cache';

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
  copied_from?: string;
  copied_at?: string;
  created_at?: string;
  modified_at?: string;
  label?: string;
  vertices: SvgVertex[];
}

interface Props {
  /** Legacy PNG source. Provide this OR `fitsKey`. */
  pngUrl?: string;
  /** Canonical exposure key for the live FITS render (epic #261, N5). */
  fitsKey?: string;
  imageWidth: number;        // image width in pixels (= exposure NAXIS1)
  imageHeight: number;       // image height in pixels (= exposure NAXIS2)
  initialRegions: MaskRegionsPayload | null;
  onSave: (regions: MaskRegionsPayload) => Promise<{ error?: string }>;
  /**
   * Enables the mask clipboard (copy/paste between exposures); the value is
   * the current exposure's filename, stamped onto pasted polygons as
   * provenance. Coordinates are raw detector pixels, so pasting is only
   * offered onto exposures with identical dimensions. Omit to disable.
   */
  clipboardSource?: string;
  /**
   * Returns false while the host page is mid-transition to another exposure —
   * a navigation target set synchronously (keypress) that this editor hasn't
   * re-rendered for yet. Clipboard actions check it at event time and no-op
   * in that gap: a paste keyed there would land in the outgoing exposure's
   * editor state and be wiped by the resync, and a copy would capture (and be
   * attributed to) the exposure being left. Omit to always allow.
   */
  clipboardLive?: () => boolean;
}

function uuid() {
  // Crypto.randomUUID works in all modern browsers; if unavailable fall back.
  if (typeof crypto !== 'undefined' && 'randomUUID' in crypto) {
    return crypto.randomUUID();
  }
  return `${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

function fmtNum(n: number): string {
  if (!Number.isFinite(n)) return '0';
  const a = Math.abs(n);
  if (a !== 0 && (a < 1e-3 || a >= 1e5)) return n.toExponential(3);
  return n.toPrecision(5);
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
    copied_from: p.copied_from,
    copied_at: p.copied_at,
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
    copied_from: p.copied_from,
    copied_at: p.copied_at,
    created_at: p.created_at,
    modified_at: p.modified_at ?? new Date().toISOString(),
    label: p.label,
    vertices: p.vertices.map((v) => svgToDs9(v, h)),
  }));
  return { version: 1, polygons };
}

export default function MaskEditor({
  pngUrl, fitsKey, imageWidth, imageHeight, initialRegions, onSave,
  clipboardSource, clipboardLive,
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
    | { kind: 'polygon'; polyId: string; start: SvgVertex; startVertices: SvgVertex[] }
    | null
  >(null);
  const [dirty, setDirty] = useState(false);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [savedAt, setSavedAt] = useState<number | null>(null);
  // Transient copy/paste feedback ("copied 3", dimension-mismatch refusal).
  const [clipMsg, setClipMsg] = useState<{ text: string; error?: boolean } | null>(null);

  // The PNG actually painted. Managed by the swap effect below; starts as the
  // incoming URL only when its bytes are already decoded (retained cache), so
  // a cold MOUNT shows the loading state too — initializing to `pngUrl`
  // unconditionally meant a fresh mount (every prev/next remounts this page)
  // rendered an invisible, natively-loading <img>: a blank canvas with no
  // indicator until the full-res PNG landed.
  const [shownUrl, setShownUrl] = useState<string | undefined>(
    () => (pngUrl && isPngCached(pngUrl) ? pngUrl : undefined),
  );

  // FITS render controls (only used when `fitsKey` is set). vmin/vmax drive the
  // display interval; 0/0 means "unset" so FitsCanvas keeps its on-load ZScale
  // until the range flows back in from `handleFitsLoad`.
  const [stretch, setStretch] = useState<StretchMode>('linear');
  const [colormap, setColormap] = useState<ColormapName>('gray');
  const [vmin, setVmin] = useState(0);
  const [vmax, setVmax] = useState(0);
  const [rangeText, setRangeText] = useState<{ lo: string; hi: string }>({ lo: '', hi: '' });
  const zbase = useRef<{ lo: number; hi: number } | null>(null); // on-load ZScale, for "Auto"
  const [fitsError, setFitsError] = useState<string | null>(null);

  const handleFitsLoad = useCallback((info: FitsCanvasLoad) => {
    zbase.current = { lo: info.vmin, hi: info.vmax };
    setVmin(info.vmin);
    setVmax(info.vmax);
    setRangeText({ lo: fmtNum(info.vmin), hi: fmtNum(info.vmax) });
    setFitsError(null);
  }, []);

  const autoStretch = useCallback(() => {
    const z = zbase.current;
    if (!z) return;
    setVmin(z.lo);
    setVmax(z.hi);
    setRangeText({ lo: fmtNum(z.lo), hi: fmtNum(z.hi) });
  }, []);

  const commitRange = useCallback((lo: string, hi: string) => {
    const nlo = Number(lo);
    const nhi = Number(hi);
    if (Number.isFinite(nlo) && Number.isFinite(nhi) && nhi > nlo) {
      setVmin(nlo);
      setVmax(nhi);
    }
  }, []);

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
    setClipMsg(null);
  }, [initialRegions, imageHeight]);

  useEffect(() => {
    if (!clipMsg) return;
    const t = setTimeout(() => setClipMsg(null), 4000);
    return () => clearTimeout(t);
  }, [clipMsg]);

  // Image swap, on mount and on prev/next alike:
  //   - Warm (already decoded in the retained cache): keep the current frame
  //     and swap on the ~instant decode, so the step never flashes.
  //   - Cold: blank to a loading state immediately rather than lingering on the
  //     previous exposure's pixels (misleading beside the new metadata + mask)
  //     or an invisible native load, then swap once the image has decoded.
  useEffect(() => {
    if (pngUrl === undefined) { setShownUrl(undefined); return; }
    if (!isPngCached(pngUrl)) setShownUrl(undefined);
    let cancelled = false;
    const swap = () => { if (!cancelled) setShownUrl(pngUrl); };
    const img = new Image();
    img.decoding = 'async';
    img.src = pngUrl;
    // Swap on decode-error too, so a bad frame never wedges us on a blank panel.
    img.decode().then(swap).catch(swap);
    return () => { cancelled = true; };
  }, [pngUrl]);

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

  // ----- mask clipboard (copy/paste between exposures) -----
  // Read during render for button enablement; cheap after the first lazy
  // sessionStorage load, and any copy re-renders via the feedback message.
  const clipboard = clipboardSource ? getMaskClipboard() : null;
  const clipboardFits = !!clipboard &&
    clipboard.imageWidth === imageWidth && clipboard.imageHeight === imageHeight;

  const handleCopy = useCallback(() => {
    if (!clipboardSource || polygons.length === 0) return;
    if (clipboardLive && !clipboardLive()) return;
    setMaskClipboard({
      polygons: toPayload(polygons, imageHeight).polygons,
      imageWidth,
      imageHeight,
      sourceFilename: clipboardSource,
      copiedAt: new Date().toISOString(),
    });
    setClipMsg({ text: `copied ${polygons.length} polygon${polygons.length === 1 ? '' : 's'}` });
  }, [clipboardSource, clipboardLive, polygons, imageWidth, imageHeight]);

  const handlePaste = useCallback(() => {
    if (!clipboardSource) return;
    if (clipboardLive && !clipboardLive()) return;
    const clip = getMaskClipboard();
    if (!clip) {
      setClipMsg({ text: 'mask clipboard is empty', error: true });
      return;
    }
    // Vertices are raw detector pixels: pasting across a size change (NIRCam
    // SW vs LW) would silently land the polygons in the wrong place.
    if (clip.imageWidth !== imageWidth || clip.imageHeight !== imageHeight) {
      setClipMsg({
        text: `clipboard is ${clip.imageWidth}×${clip.imageHeight}; this exposure is ${imageWidth}×${imageHeight} — not pasted`,
        error: true,
      });
      return;
    }
    const now = new Date().toISOString();
    // Pasted polygons are new web-authored polygons for THIS exposure: fresh
    // id and timestamps, source 'web', with copied_from/copied_at recording
    // where they came from. The source polygon's import lineage
    // (original_frame/imported_*) describes a projection through the OTHER
    // exposure's WCS, so it is deliberately not carried over.
    const pasted: SvgPolygon[] = clip.polygons.map((p) => ({
      id: uuid(),
      source: 'web',
      label: p.label,
      copied_from: clip.sourceFilename,
      copied_at: now,
      created_at: now,
      modified_at: now,
      vertices: p.vertices.map((v) => ds9ToSvg(v, imageHeight)),
    }));
    setPolygons((ps) => [...ps, ...pasted]);
    markDirty();
    setClipMsg({ text: `pasted ${pasted.length} from ${clip.sourceFilename}` });
  }, [clipboardSource, clipboardLive, imageWidth, imageHeight, markDirty]);

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
    } else if (dragging.kind === 'polygon') {
      const pt = clientToSvg(e.clientX, e.clientY);
      if (!pt) return;
      // Translate from the drag-start snapshot rather than the previous move,
      // so accumulated float error can't skew the shape mid-drag. A plain
      // click fires no move events, so selection alone never dirties. No
      // zero-delta skip: with absolute offsets, delta (0,0) is how a drag
      // that returns to its start point restores the original position.
      const dx = pt.x - dragging.start.x;
      const dy = pt.y - dragging.start.y;
      setPolygons((ps) => ps.map((p) =>
        p.id !== dragging.polyId ? p :
          { ...p, vertices: dragging.startVertices.map((v) =>
              ({ x: v.x + dx, y: v.y + dy })) }
      ));
      markDirty();
    }
  }, [dragging, clientToSvg, markDirty]);

  const onPointerUp = useCallback(() => setDragging(null), []);

  // ----- keyboard: Enter/Escape (draw), Backspace (vertex undo) -----
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      // Ctrl/⌘ C/V — mask clipboard. Skip when typing in a form field, and
      // yield Ctrl/⌘C to a real text selection anywhere on the page.
      if (clipboardSource && (e.metaKey || e.ctrlKey) && !e.altKey && !e.shiftKey) {
        const t = e.target as HTMLElement;
        const isInput = t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.tagName === 'SELECT';
        const k = e.key.toLowerCase();
        if (k === 'c' && !isInput && !window.getSelection()?.toString()) {
          if (polygons.length > 0) { e.preventDefault(); handleCopy(); }
          return;
        }
        if (k === 'v' && !isInput) {
          e.preventDefault();
          handlePaste();
          return;
        }
      }
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
  }, [mode, selectedId, markDirty, finalizeDraft,
      clipboardSource, polygons.length, handleCopy, handlePaste]);

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
      <div className="flex items-center justify-between p-2 border-b border-border bg-surface-2 flex-shrink-0">
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
          {clipboardSource && (
            <>
              <div className="w-px h-4 bg-border mx-1" />
              <ToolButton
                onClick={handleCopy}
                disabled={polygons.length === 0}
                label={polygons.length === 0
                  ? 'Copy masks — nothing to copy'
                  : `Copy ${polygons.length} mask${polygons.length === 1 ? '' : 's'} (Ctrl/⌘C)`}
              >
                <Copy className="w-4 h-4" />
              </ToolButton>
              <ToolButton
                onClick={handlePaste}
                disabled={!clipboard || !clipboardFits}
                label={!clipboard
                  ? 'Paste masks — clipboard empty'
                  : !clipboardFits
                  ? `Paste masks — clipboard is ${clipboard.imageWidth}×${clipboard.imageHeight}, this exposure is ${imageWidth}×${imageHeight}`
                  : `Paste ${clipboard.polygons.length} mask${clipboard.polygons.length === 1 ? '' : 's'} from ${clipboard.sourceFilename} (Ctrl/⌘V)`}
              >
                <ClipboardPaste className="w-4 h-4" />
              </ToolButton>
            </>
          )}
        </div>
        <div className="flex items-center gap-3 text-xs text-text-secondary">
          {clipMsg && (
            <span className={clipMsg.error ? 'text-red-500' : 'text-primary'}>
              {clipMsg.text}
            </span>
          )}
          <span>{polygons.length} polygon{polygons.length === 1 ? '' : 's'}</span>
          <span>{(scale * 100).toFixed(0)}%</span>
          {saveError && <span className="text-red-500">{saveError}</span>}
          <Button
            onClick={(e) => {
              if (e.detail > 0) e.currentTarget.blur();
              handleSave();
            }}
            disabled={saving || !dirty}
            size="sm"
          >
            {saving ? (<><Loader2 className="w-4 h-4 mr-1 animate-spin" />Saving</>) :
             savedAt && !dirty ? (<><Check className="w-4 h-4 mr-1" />Saved</>) :
             (<><Save className="w-4 h-4 mr-1" />Save</>)}
          </Button>
        </div>
      </div>

      {/* FITS render controls (live SCI render only) */}
      {fitsKey && (
        <div className="flex flex-wrap items-center gap-2 px-2 py-1.5 border-b border-border bg-surface-2 text-xs flex-shrink-0">
          <label className="flex items-center gap-1">
            <span className="text-text-secondary">Stretch</span>
            <select value={stretch} onChange={(e) => setStretch(e.target.value as StretchMode)}
              className="rounded border border-border bg-card px-1.5 py-0.5 text-text-primary">
              {STRETCH_MODES.map((m) => <option key={m} value={m}>{m}</option>)}
            </select>
          </label>
          <label className="flex items-center gap-1">
            <span className="text-text-secondary">Colormap</span>
            <select value={colormap} onChange={(e) => setColormap(e.target.value as ColormapName)}
              className="rounded border border-border bg-card px-1.5 py-0.5 text-text-primary">
              {COLORMAP_NAMES.map((c) => <option key={c} value={c}>{c}</option>)}
            </select>
          </label>
          <label className="flex items-center gap-1">
            <span className="text-text-secondary">Min</span>
            <input type="text" inputMode="decimal" value={rangeText.lo}
              onChange={(e) => setRangeText((r) => ({ ...r, lo: e.target.value }))}
              onBlur={() => commitRange(rangeText.lo, rangeText.hi)}
              onKeyDown={(e) => e.key === 'Enter' && commitRange(rangeText.lo, rangeText.hi)}
              className="w-20 rounded border border-border bg-card px-1.5 py-0.5 font-mono text-text-primary" />
          </label>
          <label className="flex items-center gap-1">
            <span className="text-text-secondary">Max</span>
            <input type="text" inputMode="decimal" value={rangeText.hi}
              onChange={(e) => setRangeText((r) => ({ ...r, hi: e.target.value }))}
              onBlur={() => commitRange(rangeText.lo, rangeText.hi)}
              onKeyDown={(e) => e.key === 'Enter' && commitRange(rangeText.lo, rangeText.hi)}
              className="w-20 rounded border border-border bg-card px-1.5 py-0.5 font-mono text-text-primary" />
          </label>
          <button type="button"
            onClick={(e) => { if (e.detail > 0) e.currentTarget.blur(); autoStretch(); }}
            className="rounded border border-border px-2 py-0.5 text-text-secondary hover:bg-card-hover">
            Auto
          </button>
          {fitsError && <span className="text-red-500">{fitsError}</span>}
        </div>
      )}

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
          {fitsKey ? (
            <FitsCanvas
              fitsKey={fitsKey}
              width={imageWidth}
              height={imageHeight}
              stretch={stretch}
              colormap={colormap}
              vmin={vmin}
              vmax={vmax}
              onLoad={handleFitsLoad}
              onError={setFitsError}
            />
          ) : shownUrl ? (
            /* eslint-disable-next-line @next/next/no-img-element */
            <img
              src={shownUrl}
              width={imageWidth}
              height={imageHeight}
              alt="exposure preview"
              draggable={false}
              className="absolute inset-0 pointer-events-none"
              style={{ imageRendering: 'pixelated' }}
            />
          ) : null}
          {/* Overlay only when an image is present — during a cold-load blank
              there's nothing to draw masks against. */}
          {(shownUrl || fitsKey) && (
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
                onBodyDown={(e) => {
                  if (mode !== 'edit') return;
                  // Leave shift+drag / middle mouse to the container's pan.
                  if (e.shiftKey || e.button !== 0) return;
                  e.stopPropagation();
                  setSelectedId(p.id);
                  const pt = clientToSvg(e.clientX, e.clientY);
                  if (!pt) return;
                  (e.target as Element).setPointerCapture?.(e.pointerId);
                  setDragging({
                    kind: 'polygon',
                    polyId: p.id,
                    start: pt,
                    startVertices: p.vertices,
                  });
                }}
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
          )}
        </div>

        {/* Cold-load indicator: the incoming PNG isn't cached yet, so the panel
            stays blank (rather than showing the previous exposure) until it's
            fetched and decoded. */}
        {!fitsKey && pngUrl && !shownUrl && (
          <div className="absolute inset-0 flex items-center justify-center pointer-events-none">
            <Loader2 className="w-8 h-8 animate-spin text-white/40" />
          </div>
        )}

        {/* Help footer */}
        <div className="absolute bottom-2 left-2 right-2 text-[11px] text-white/70 pointer-events-none font-mono">
          {mode === 'inspect' && 'drag = pan • wheel = zoom • shift+drag = pan in any mode'}
          {mode === 'draw'    && 'click = add vertex • click first vertex / Enter = close • Esc = cancel • Backspace = undo vertex'}
          {mode === 'edit'    && 'click polygon = select • drag polygon = move • drag vertex = reshape • Delete = remove polygon'}
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Polygon SVG shape (with vertex handles in edit mode)
// ---------------------------------------------------------------------------

function PolygonShape({
  poly, selected, mode, scale, onBodyDown, onVertexDown,
}: {
  poly: SvgPolygon;
  selected: boolean;
  mode: Mode;
  scale: number;
  onBodyDown: (e: React.PointerEvent) => void;
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
        style={interactive ? { cursor: 'move' } : undefined}
        onPointerDown={(e) => { if (interactive) onBodyDown(e); }}
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
  active = false, disabled = false, onClick, label, children,
}: {
  active?: boolean;
  disabled?: boolean;
  onClick: () => void;
  label: string;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      disabled={disabled}
      onClick={(e) => {
        // Drop mouse-click focus so a following Space press reaches the page's
        // approve-and-next shortcut instead of re-activating this tool button.
        // Keyboard activation (detail === 0) keeps focus.
        if (e.detail > 0) e.currentTarget.blur();
        onClick();
      }}
      title={label}
      aria-label={label}
      className={`p-1.5 rounded text-sm disabled:opacity-30 disabled:cursor-not-allowed ${
        active
          ? 'bg-primary/15 text-primary'
          : 'text-text-secondary hover:bg-card-hover'
      }`}
    >
      {children}
    </button>
  );
}
