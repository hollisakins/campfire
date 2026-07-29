'use client';

import React, { Suspense, useState, useEffect, useCallback, useMemo, useRef, useReducer, useSyncExternalStore } from 'react';
import { useParams, useRouter, useSearchParams } from 'next/navigation';
import Link from 'next/link';
import { Card } from '@/components/ui/Card';
import { Button } from '@/components/ui/Button';
import {
  Loader2, ArrowLeft, Save, Check, ChevronLeft, ChevronRight, Keyboard,
} from 'lucide-react';
import {
  getNircamExposureById,
  getExposureNeighbors,
  updateExposureReview,
  saveExposureMaskRegions,
  presignExposurePngs,
  type ExposureNeighbors,
} from '@/lib/actions/nircam-exposures';
import type { NircamExposure, MaskRegionsPayload } from '@/lib/types';
import { stageBadgeClasses } from '@/lib/nircam-stages';
import MaskEditor from '@/components/nircam/MaskEditor';
import { storageKey } from '@/lib/layout';
import { parseExposureNavParams } from '@/lib/nircam-exposure-nav';
import {
  getCachedExposure,
  setCachedExposure,
  prefetchPng,
  getCachedPngUrls,
  setCachedPngUrls,
  hasPendingSave,
  beginPendingSave,
  endPendingSave,
  getSaveError,
  setSaveError,
  subscribeSaveState,
  getSaveStateVersion,
} from '@/lib/nircam-exposure-cache';

// Eager PNG prefetch window: warm the full-res mask surface (~5.7 MB) the
// editor actually renders for a few exposures ahead + one back, so stepping
// through the queue paints instantly. Falls back to the preview (~1.3 MB) for
// exposures that have no full PNG (the thumbnail-only view).
const PREFETCH_AHEAD = 3;
const PREFETCH_BEHIND = 1;

function ExposureDetailPageInner() {
  const params = useParams();
  const router = useRouter();
  const searchParams = useSearchParams();
  const id = Number(params.id);

  // The list page's filter+sort state, carried in the URL (see
  // lib/nircam-exposure-nav.ts). Defines the ordered set prev/next walks;
  // empty params (direct entry) = the full unfiltered set, so nav always works.
  const navFilters = useMemo(
    () => parseExposureNavParams(new URLSearchParams(searchParams.toString())),
    [searchParams],
  );
  const navQuery = searchParams.toString();

  const [exposure, setExposure] = useState<NircamExposure | null>(null);
  const [exposureForId, setExposureForId] = useState<number | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Editable fields
  const [reviewStatus, setReviewStatus] = useState<string>('pending');
  const [correction, setCorrection] = useState<string>('none');
  const [notes, setNotes] = useState<string>('');

  // Which triage fields the operator has touched, and whether a save has landed,
  // for the exposure currently on screen. The background revalidation below fires
  // on arrival and lands 100-300 ms later — long after a fast operator has
  // already pressed 2 — so it must not write over what they set. Without these
  // guards the status visibly flips back to the DB value, `hasChanges` goes
  // false, and the auto-save on → silently skips: the exposure flashes Approved
  // for a frame and stays Pending.
  //
  // `dirty` is per-field rather than one flag: with a stale cached row, editing
  // a single field would otherwise pin *every* control to its cached value while
  // `exposure` is replaced with the fresh row — so `hasChanges` reads the
  // untouched-but-stale fields as edits and the next save writes them back over
  // a newer server decision.
  //
  // `forId` tags the whole record with the exposure it describes (same pattern
  // as `navState` below), so a save resolving after we've navigated away can
  // tell that it no longer owns this state. Reset on every route-id change
  // (below), so arriving at a fresh exposure still takes the server's values.
  const localEditsRef = useRef<{
    forId: number | null;
    dirty: { reviewStatus: boolean; correction: boolean; notes: boolean };
    saved: boolean;
  }>({
    forId: null,
    dirty: { reviewStatus: false, correction: false, notes: false },
    saved: false,
  });
  const editStatus = useCallback((v: string) => {
    localEditsRef.current.dirty.reviewStatus = true;
    setReviewStatus(v);
  }, []);
  const editCorrection = useCallback((v: string) => {
    localEditsRef.current.dirty.correction = true;
    setCorrection(v);
  }, []);
  const editNotes = useCallback((v: string) => {
    localEditsRef.current.dirty.notes = true;
    setNotes(v);
  }, []);

  // Sibling-exposure nav: the ±window neighbors + absolute position within
  // the filtered, ordered set, from get_admin_exposure_neighbors. Survives
  // refresh and direct entry because the filter context lives in the URL.
  // Tagged with the id it was fetched for: `id` changes the instant we push, but
  // the neighbors RPC for the new exposure takes another round trip to land.
  const [navState, setNavState] = useState<
    { forId: number; data: ExposureNeighbors } | null
  >(null);
  const [showHelp, setShowHelp] = useState(false);
  // N4 (epic #261): live FITS render vs the legacy pre-generated PNG. Defaults
  // to PNG; the toggle doubles as the pixel-parity check during the rollout.
  const [viewMode, setViewMode] = useState<'png' | 'fits'>('png');
  // Presigned OSN GET URLs (epic #261, N5) live in a MODULE-level cache
  // (getCachedPngUrls) — NOT React state — because this page remounts on every
  // prev/next, which would otherwise wipe them and force a fresh presign (new
  // signature) on each step, flashing the spinner and missing the retained PNG.
  // This reducer just forces a re-render when a presign lands so the pending
  // spinner clears and the <img> picks up the freshly-cached URL.
  const [, bumpPngUrls] = useReducer((n: number) => n + 1, 0);
  useEffect(() => {
    let cancelled = false;
    getExposureNeighbors(id, { ...navFilters, window: PREFETCH_AHEAD }).then((res) => {
      if (cancelled) return;
      setNavState(res.error || res.total === 0 ? null : { forId: id, data: res });
    });
    return () => { cancelled = true; };
  }, [id, navFilters]);

  // While that RPC is in flight, `navState` still describes the exposure we just
  // left, and using it verbatim mis-targets both arrows: → points at the current
  // exposure (a no-op push, so hammering → appears to stick) and ← skips one.
  // The stale window almost always already contains the new id, so re-derive
  // prev/next/position from it — instant and correct for the single-step case.
  // Only when the new id falls outside that window do we report "no nav" and let
  // the arrows sit disabled for the ~100 ms until the fresh result lands.
  const nav = useMemo<ExposureNeighbors | null>(() => {
    if (!navState) return null;
    if (navState.forId === id) return navState.data;
    const stale = navState.data;
    const idx = stale.windowIds.indexOf(id);
    const fromIdx = stale.windowIds.indexOf(navState.forId);
    if (idx < 0 || fromIdx < 0) return null;
    return {
      prevId: idx > 0 ? stale.windowIds[idx - 1] : null,
      nextId: idx < stale.windowIds.length - 1 ? stale.windowIds[idx + 1] : null,
      position: stale.position != null ? stale.position + (idx - fromIdx) : null,
      total: stale.total,
      windowIds: stale.windowIds,
    };
  }, [navState, id]);

  // When the route id changes, reset state synchronously from the in-memory
  // cache so the new exposure paints in the same frame as the URL change —
  // no spinner, no flash of the previous exposure. The server is hit in the
  // background to revalidate. (React's "set state during render" pattern,
  // bounded by the exposureForId guard so we don't loop.)
  if (exposureForId !== id) {
    const cached = getCachedExposure(id);
    setExposureForId(id);
    setExposure(cached);
    setReviewStatus(cached?.review_status || 'pending');
    setCorrection(cached?.correction || 'none');
    setNotes(cached?.notes || '');
    setLoading(!cached);
    setError(null);
    setSaved(false);
    localEditsRef.current = {
      forId: id,
      dirty: { reviewStatus: false, correction: false, notes: false },
      saved: false,
    };
  }

  // Background revalidation against the DB on every id change. Auto-save on
  // navigation has already flushed the *previous* exposure's triage state, so
  // arriving clean means the server's values win. But this read races the
  // operator: anything they've typed or keyed since arriving, and any save that
  // has already landed, is newer than this response and must survive it.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      const result = await getNircamExposureById(id);
      if (cancelled) return;
      if (result.error) {
        setError(result.error);
        setLoading(false);
        return;
      }
      if (result.exposure) {
        const edits = localEditsRef.current;
        // A save for this exposure already landed — our write is strictly newer
        // than this read, so don't let it back into state *or* the cache. Same
        // for a save still in flight (fire-and-forget auto-save on nav, then a
        // quick revisit): the optimistic row is newer than this read too.
        const pending = hasPendingSave(id);
        if (!edits.saved && !pending) {
          setCachedExposure(result.exposure);
          setExposure(result.exposure);
        }
        // Per field: an untouched control still takes the fresh server value.
        if (!edits.dirty.reviewStatus && !pending) setReviewStatus(result.exposure.review_status);
        if (!edits.dirty.correction && !pending) setCorrection(result.exposure.correction);
        if (!edits.dirty.notes && !pending) setNotes(result.exposure.notes || '');
      }
      setLoading(false);
    })();
    return () => { cancelled = true; };
  }, [id]);

  // Warm the sibling exposure *data* cache (prev/next) so navigation paints in
  // the same frame — independent of PNG bytes.
  useEffect(() => {
    if (!nav) return;
    for (const sibId of [nav.nextId, nav.prevId]) {
      if (sibId == null || getCachedExposure(sibId)) continue;
      getNircamExposureById(sibId).then((res) => {
        if (res.exposure) setCachedExposure(res.exposure);
      });
    }
  }, [nav]);

  // Presign the current exposure's PNGs + eagerly prefetch a window of upcoming
  // ones (epic #261, N5). Keys are re-derived server-side; URLs go straight into
  // <img> (no proxy hop). We warm the full-res mask surface (~5.7 MB) — the byte
  // the editor actually renders — across the whole window, keyed off the
  // *persistent* URL map so a sibling presigned by an earlier window is still
  // warmed on every step. (Previously the warm keyed off only the freshly-signed
  // ids, so once the next exposure had been presigned by a prior window its full
  // PNG was never prefetched again — every Next reloaded it cold from OSN.)
  useEffect(() => {
    if (!nav) return;
    // Derive the prefetch window from the neighbors RPC result: ahead-heavy
    // slice of the ordered window ids around the current exposure.
    const idx = nav.windowIds.indexOf(id);
    if (idx < 0) return;
    const win = [
      ...nav.windowIds.slice(idx + 1, idx + 1 + PREFETCH_AHEAD),
      ...nav.windowIds.slice(Math.max(0, idx - PREFETCH_BEHIND), idx),
    ];
    // Warm the exact byte the viewer will show for each windowed exposure: the
    // full-res mask surface the editor renders, or the preview when there's no
    // full PNG. Re-warming an already-cached URL is a browser cache hit, so the
    // only new network per step is the frontier exposure entering the window.
    const warm = () => {
      for (const sib of win) {
        const u = getCachedPngUrls(sib);
        if (u) prefetchPng(u.full ?? u.preview);
      }
    };
    // Only presign ids not already in the module cache (a cached sibling keeps
    // its URL); when the whole window is already signed, just re-warm.
    const batch = [id, ...win].filter((x) => getCachedPngUrls(x) === undefined);
    if (batch.length === 0) {
      warm();
      return;
    }
    let cancelled = false;
    presignExposurePngs(batch).then((urls) => {
      // Populate the module cache even if this instance unmounted mid-flight —
      // the URLs are valid for whichever instance renders the exposure next.
      for (const [key, u] of Object.entries(urls)) setCachedPngUrls(Number(key), u);
      if (cancelled) return;
      bumpPngUrls();
      warm();
    });
    return () => { cancelled = true; };
  }, [id, nav]);

  const hasChanges = !!(exposure && (
    reviewStatus !== exposure.review_status ||
    correction !== exposure.correction ||
    notes !== (exposure.notes || '')
  ));

  // Latest-state ref so the keyboard handler doesn't capture stale closures.
  const stateRef = useRef({ reviewStatus, correction, notes, hasChanges, exposure });
  stateRef.current = { reviewStatus, correction, notes, hasChanges, exposure };

  // The last failed fire-and-forget save (module store — survives the remount
  // on navigation, so the failure surfaces even though the operator is several
  // exposures ahead by the time the response lands).
  useSyncExternalStore(subscribeSaveState, getSaveStateVersion, getSaveStateVersion);
  const saveError = getSaveError();

  // A failed save reverts the cached row to the server's truth; if that
  // exposure is the one on screen, re-baseline `exposure` from the cache so
  // hasChanges compares against reality and a retry actually fires (otherwise
  // the optimistic row read at mount claims the decision already stuck).
  useEffect(() => {
    if (saveError?.id !== id) return;
    const cached = getCachedExposure(id);
    if (cached) setExposure(cached);
  }, [saveError, id]);

  const handleSave = useCallback(async (
    statusOverride?: NircamExposure['review_status'],
  ): Promise<{ ok: boolean }> => {
    const s = stateRef.current;
    // The exposure this save is for. The `S` shortcut fires handleSave
    // un-awaited and mask saves aren't gated by navigation at all — so a
    // response can land after the route moved on.
    const savedForId = id;
    setSaving(true);
    setSaved(false);
    setError(null);
    const result = await updateExposureReview(savedForId, {
      review_status: statusOverride ?? (s.reviewStatus as NircamExposure['review_status']),
      correction: s.correction as NircamExposure['correction'],
      // Always sent (even '') so clearing the notes field actually persists —
      // an undefined key is dropped from the PATCH and leaves the old value.
      notes: s.notes,
    });
    setSaving(false);
    if (result.error) {
      // Surfaced even if we've navigated on: a failed save means the operator's
      // decision didn't stick, which they need to know about either way.
      setError(result.error);
      return { ok: false };
    }
    // Keyed by the exposure's own id, so this is correct regardless of route.
    if (result.exposure) setCachedExposure(result.exposure);
    // Everything below writes state belonging to whatever is on screen *now* —
    // only safe while that's still the exposure we saved. Otherwise this would
    // render the old exposure under the new one's URL and set the new one's
    // `saved` guard, permanently suppressing its revalidation.
    if (localEditsRef.current.forId !== savedForId) return { ok: true };
    if (result.exposure) {
      localEditsRef.current.saved = true;
      setExposure(result.exposure);
    }
    setSaved(true);
    setTimeout(() => setSaved(false), 2000);
    return { ok: true };
  }, [id]);

  // Auto-save on nav: flush dirty triage so the operator can blast through a
  // queue with arrow keys without losing state — WITHOUT blocking navigation
  // on the save round trip. The decision goes into the row cache
  // optimistically (that's what the next visit paints from), the server action
  // fires, and the route pushes in the same tick. The pending-save registry
  // keeps a quick revisit's revalidation from resurrecting the pre-save row;
  // a failure reverts the cache to the server's truth and raises the module
  // save-error banner, since this instance is gone by then.
  //
  // `statusOverride` exists for approve-and-next: a status set in the same
  // tick isn't visible in stateRef yet.
  const goTo = useCallback((
    targetId: number | null,
    statusOverride?: NircamExposure['review_status'],
  ) => {
    // targetId === id means the neighbor window is stale in a way the derivation
    // above couldn't repair; pushing would be a no-op that looks like a step.
    if (targetId == null || targetId === id) return;
    const s = stateRef.current;
    const exp = s.exposure;
    const review = statusOverride ?? (s.reviewStatus as NircamExposure['review_status']);
    const changed = !!(exp && (
      review !== exp.review_status ||
      s.correction !== exp.correction ||
      s.notes !== (exp.notes || '')
    ));
    if (changed && exp) {
      const savedForId = exp.id;
      const savedFilename = exp.filename;
      setCachedExposure({
        ...exp,
        review_status: review,
        correction: s.correction as NircamExposure['correction'],
        notes: s.notes || null,
      });
      beginPendingSave(savedForId);
      updateExposureReview(savedForId, {
        review_status: review,
        correction: s.correction as NircamExposure['correction'],
        notes: s.notes, // always sent (even '') so clearing notes persists
      }).then(async (result) => {
        if (result.exposure && !result.error) {
          setCachedExposure(result.exposure);
          // A retry that lands supersedes an earlier failure for this exposure.
          if (getSaveError()?.id === savedForId) setSaveError(null);
          endPendingSave(savedForId);
          return;
        }
        // Failed: put the server's row back over the optimistic one, then tell
        // the operator — they may be several exposures ahead by now.
        const fresh = await getNircamExposureById(savedForId);
        if (fresh.exposure) setCachedExposure(fresh.exposure);
        endPendingSave(savedForId);
        setSaveError({
          id: savedForId,
          filename: savedFilename,
          message: result.error || 'Save failed',
        });
      });
    }
    // Carry the filter context so the next exposure's nav walks the same set.
    router.push(`/admin/nircam/${targetId}${navQuery ? `?${navQuery}` : ''}`);
  }, [router, navQuery, id]);

  const handleNext = useCallback(() => goTo(nav?.nextId ?? null), [goTo, nav]);
  const handlePrev = useCallback(() => goTo(nav?.prevId ?? null), [goTo, nav]);

  // The 90% case as a single keystroke: mark approved and advance. Routed
  // through goTo's status override because the setState here isn't visible in
  // stateRef until the next render. At the end of the queue there's nowhere to
  // advance to, so persist the decision in place instead.
  const approveAndNext = useCallback(() => {
    editStatus('approved');
    const nextId = nav?.nextId ?? null;
    if (nextId == null || nextId === id) {
      handleSave('approved');
      return;
    }
    goTo(nextId, 'approved');
  }, [editStatus, nav, id, goTo, handleSave]);

  // Global keyboard shortcuts (mirrors web/components/spectra/inspection
  // pattern). Skip when an input has focus so users can type in notes etc.
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement;
      const isInput = t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.tagName === 'SELECT';
      if (e.key === 'Escape' && isInput) { t.blur(); return; }
      if (isInput) return;
      if (e.metaKey || e.ctrlKey || e.altKey) return;

      switch (e.key) {
        case '1': e.preventDefault(); editStatus('pending');  break;
        case '2': e.preventDefault(); editStatus('approved'); break;
        case '3': e.preventDefault(); editStatus('excluded'); break;
        case ' ':
        case 'a':
        case 'A': e.preventDefault(); approveAndNext(); break;
        case 'ArrowRight':
        case 'n':
        case 'N': e.preventDefault(); handleNext(); break;
        case 'ArrowLeft':
        case 'p':
        case 'P': e.preventDefault(); handlePrev(); break;
        case 's':
        case 'S': e.preventDefault(); handleSave(); break;
        case '?': e.preventDefault(); setShowHelp(prev => !prev); break;
        case 'Escape': if (showHelp) setShowHelp(false); break;
      }
    };
    document.addEventListener('keydown', handler);
    return () => document.removeEventListener('keydown', handler);
  }, [handleNext, handlePrev, handleSave, approveAndNext, showHelp, editStatus]);

  if (loading) {
    return (
      <div className="flex items-center justify-center py-16">
        <Loader2 className="w-8 h-8 animate-spin text-primary" />
      </div>
    );
  }

  if (!exposure) {
    return (
      <div className="py-8">
        <p className="text-text-secondary">Exposure not found.</p>
        <Link href="/admin/nircam" className="text-primary hover:underline mt-2 inline-block">
          Back to NIRCam
        </Link>
      </div>
    );
  }

  // Presigned OSN URLs for the current exposure (undefined until the presign
  // round-trip lands; null once resolved if the exposure has no PNG). Read from
  // the module cache so a prefetched sibling is already resolved on arrival.
  const currentUrls = getCachedPngUrls(id);
  const pngUrl = currentUrls?.preview ?? null;
  const fullPngUrl = currentUrls?.full ?? null;
  const pngPresignPending = currentUrls === undefined;
  const editorAvailable = Boolean(
    fullPngUrl && exposure.image_width && exposure.image_height
  );

  // Canonical OSN key for the exposure SCI FITS, for the live N4 renderer.
  let fitsKey: string | null = null;
  try {
    if (exposure.field && exposure.filter && exposure.filename) {
      const fname = `${exposure.filename.replace(/\.fits$/, '')}.fits`;
      fitsKey = storageKey(
        'nircam_exposure',
        { field: exposure.field, filt: exposure.filter },
        fname,
        'canonical',
      );
    }
  } catch {
    fitsKey = null;
  }
  // FITS masking needs the key plus the pixel dims (for the overlay viewBox).
  const fitsMaskAvailable = Boolean(
    fitsKey && exposure.image_width && exposure.image_height,
  );

  const handleSaveMasks = async (regions: MaskRegionsPayload) => {
    // Mask saves run from the editor's own toolbar, outside `goTo`'s dirty
    // check — so the operator can navigate away while one is still in flight.
    const savedForId = exposure.id;
    const res = await saveExposureMaskRegions(savedForId, regions);
    if (res.exposure) {
      setCachedExposure(res.exposure);
      // Same newer-than-the-read rule as the triage save: a mask write must not
      // be undone by an in-flight revalidation landing after it — but only for
      // the exposure it was issued for (see handleSave).
      if (localEditsRef.current.forId === savedForId) {
        localEditsRef.current.saved = true;
        setExposure(res.exposure);
      }
    }
    return { error: res.error };
  };

  return (
    <div>
      {/* Header */}
      <div className="flex items-center gap-3 mb-6">
        <button
          onClick={() => router.push(`/admin/nircam${navQuery ? `?${navQuery}` : ''}`)}
          className="text-text-secondary hover:text-text-primary"
          title="Back to list"
        >
          <ArrowLeft className="w-5 h-5" />
        </button>
        <div className="flex-1 min-w-0">
          <h1 className="text-xl font-semibold font-mono text-text-primary truncate">
            {exposure.filename}
          </h1>
          <p className="text-sm text-text-secondary">
            {exposure.field} / {exposure.filter} / {exposure.detector}
          </p>
        </div>
        {nav && (
          <div className="flex items-center gap-1 text-sm text-text-secondary">
            <button
              onClick={handlePrev}
              disabled={nav.prevId == null}
              title="Previous (← / P)"
              className="p-1.5 rounded hover:bg-card-hover disabled:opacity-30 disabled:cursor-not-allowed"
            >
              <ChevronLeft className="w-5 h-5" />
            </button>
            <span className="font-mono tabular-nums text-xs px-1">
              {nav.position} / {nav.total}
            </span>
            <button
              onClick={handleNext}
              disabled={nav.nextId == null}
              title="Next (→ / N)"
              className="p-1.5 rounded hover:bg-card-hover disabled:opacity-30 disabled:cursor-not-allowed"
            >
              <ChevronRight className="w-5 h-5" />
            </button>
          </div>
        )}
        <button
          onClick={() => setShowHelp(prev => !prev)}
          title="Keyboard shortcuts (?)"
          className="p-1.5 rounded text-text-secondary hover:bg-card-hover"
        >
          <Keyboard className="w-5 h-5" />
        </button>
      </div>

      {showHelp && (
        <div className="mb-6 rounded-lg border border-border bg-card p-4">
          <div className="flex items-center justify-between mb-2">
            <h2 className="text-sm font-semibold text-text-primary">Keyboard shortcuts</h2>
            <button onClick={() => setShowHelp(false)} className="text-xs text-text-secondary hover:underline">close</button>
          </div>
          <dl className="grid grid-cols-2 gap-x-6 gap-y-1 text-sm">
            <div className="flex justify-between"><dt>Approve &amp; next</dt><dd className="font-mono text-text-secondary">Space or A</dd></div>
            <div className="flex justify-between"><dt>Next exposure</dt><dd className="font-mono text-text-secondary">→ or N</dd></div>
            <div className="flex justify-between"><dt>Previous</dt><dd className="font-mono text-text-secondary">← or P</dd></div>
            <div className="flex justify-between"><dt>Mark pending</dt><dd className="font-mono text-text-secondary">1</dd></div>
            <div className="flex justify-between"><dt>Mark approved</dt><dd className="font-mono text-text-secondary">2</dd></div>
            <div className="flex justify-between"><dt>Mark excluded</dt><dd className="font-mono text-text-secondary">3</dd></div>
            <div className="flex justify-between"><dt>Save</dt><dd className="font-mono text-text-secondary">S</dd></div>
            <div className="flex justify-between"><dt>Help</dt><dd className="font-mono text-text-secondary">?</dd></div>
          </dl>
          <p className="mt-2 text-xs text-text-secondary">
            Navigation auto-saves the triage panel in the background if there are unsaved changes. Mask edits save separately from the editor toolbar.
          </p>
        </div>
      )}

      {error && (
        <div className="bg-red-50 dark:bg-red-950 border border-red-200 dark:border-red-900 rounded-lg p-4 mb-6">
          <p className="text-red-800 dark:text-red-400">{error}</p>
        </div>
      )}

      {/* A background auto-save (fire-and-forget on nav) failed — possibly for
          an exposure several steps back. Persistent until dismissed or a retry
          for the same exposure lands. */}
      {saveError && (
        <div className="bg-red-50 dark:bg-red-950 border border-red-200 dark:border-red-900 rounded-lg p-4 mb-6 flex items-start justify-between gap-4">
          <p className="text-red-800 dark:text-red-400">
            Failed to save{' '}
            <Link
              href={`/admin/nircam/${saveError.id}${navQuery ? `?${navQuery}` : ''}`}
              className="font-mono underline"
            >
              {saveError.filename}
            </Link>
            : {saveError.message}. Its review status was not changed — revisit it to re-apply your decision.
          </p>
          <button
            onClick={() => setSaveError(null)}
            className="text-xs text-red-800 dark:text-red-400 hover:underline flex-shrink-0"
          >
            Dismiss
          </button>
        </div>
      )}

      <div className="flex gap-6">
        {/* Image viewer: live FITS render (N4) or the legacy PNG / mask editor */}
        <div className="flex-1 min-w-0">
          <div className="mb-2 inline-flex rounded-lg border border-border p-0.5 text-xs">
            <button
              onClick={() => setViewMode('png')}
              className={`rounded-md px-3 py-1 ${viewMode === 'png' ? 'bg-card-hover text-text-primary' : 'text-text-secondary'}`}
            >
              PNG
            </button>
            <button
              onClick={() => setViewMode('fits')}
              disabled={!fitsMaskAvailable}
              title={fitsMaskAvailable ? 'Live FITS render (SCI) + masking' : 'FITS unavailable for this exposure'}
              className={`rounded-md px-3 py-1 disabled:opacity-40 ${viewMode === 'fits' ? 'bg-card-hover text-text-primary' : 'text-text-secondary'}`}
            >
              FITS <span className="text-text-tertiary">beta</span>
            </button>
          </div>
          <Card className="overflow-hidden">
            {viewMode === 'fits' && fitsMaskAvailable ? (
              <div className="h-[80vh]">
                <MaskEditor
                  fitsKey={fitsKey!}
                  imageWidth={exposure.image_width!}
                  imageHeight={exposure.image_height!}
                  initialRegions={exposure.mask_regions}
                  onSave={handleSaveMasks}
                />
              </div>
            ) : pngPresignPending ? (
              // Presign round-trip for the PNG hasn't landed yet — don't flash
              // "No PNG" before we know whether one exists.
              <div className="flex items-center justify-center py-24">
                <Loader2 className="w-8 h-8 animate-spin text-text-secondary" />
              </div>
            ) : editorAvailable ? (
              <div className="h-[80vh]">
                <MaskEditor
                  pngUrl={fullPngUrl!}
                  imageWidth={exposure.image_width!}
                  imageHeight={exposure.image_height!}
                  initialRegions={exposure.mask_regions}
                  onSave={handleSaveMasks}
                />
              </div>
            ) : pngUrl ? (
              // Fallback: thumbnail-only view (full PNG hasn't been deployed yet).
              <img
                src={pngUrl}
                alt={`${exposure.filename} quick-look`}
                className="w-full h-auto"
              />
            ) : (
              <div className="flex items-center justify-center py-24 text-text-secondary">
                No PNG available
              </div>
            )}
          </Card>
        </div>

        {/* Sidebar */}
        <div className="w-80 flex-shrink-0 space-y-4">
          {/* Metadata */}
          <Card className="p-4">
            <h2 className="text-sm font-medium text-text-secondary uppercase tracking-wider mb-3">
              Metadata
            </h2>
            <dl className="space-y-2 text-sm">
              <div className="flex justify-between">
                <dt className="text-text-secondary">Field</dt>
                <dd className="text-text-primary">{exposure.field}</dd>
              </div>
              <div className="flex justify-between">
                <dt className="text-text-secondary">Filter</dt>
                <dd className="text-text-primary">{exposure.filter}</dd>
              </div>
              <div className="flex justify-between">
                <dt className="text-text-secondary">Detector</dt>
                <dd className="text-text-primary">{exposure.detector}</dd>
              </div>
              <div className="flex justify-between">
                <dt className="text-text-secondary">Visit</dt>
                <dd className="text-text-primary font-mono text-xs">{exposure.visit || '—'}</dd>
              </div>
              <div className="flex justify-between">
                <dt className="text-text-secondary">Date</dt>
                <dd className="text-text-primary">{exposure.date_obs || '—'}</dd>
              </div>
              <div className="flex justify-between">
                <dt className="text-text-secondary">Stage</dt>
                <dd>
                  <span className={`inline-flex px-2 py-0.5 text-xs font-medium rounded-full font-mono ${stageBadgeClasses(exposure.stage)}`}>
                    {exposure.stage}
                  </span>
                </dd>
              </div>
              {(exposure.ra_center != null && exposure.dec_center != null) && (
                <div className="flex justify-between">
                  <dt className="text-text-secondary">RA, Dec</dt>
                  <dd className="text-text-primary font-mono text-xs">
                    {exposure.ra_center.toFixed(5)}, {exposure.dec_center.toFixed(5)}
                  </dd>
                </div>
              )}
            </dl>
          </Card>

          {/* Triage controls */}
          <Card className="p-4">
            <h2 className="text-sm font-medium text-text-secondary uppercase tracking-wider mb-3">
              Triage
            </h2>
            <div className="space-y-3">
              <div>
                <label className="block text-xs font-medium text-text-secondary mb-1">
                  Review Status
                </label>
                <select
                  value={reviewStatus}
                  onChange={(e) => editStatus(e.target.value)}
                  className="w-full text-sm border border-border rounded-lg px-3 py-2 bg-card text-text-primary"
                >
                  <option value="pending">Pending</option>
                  <option value="approved">Approved</option>
                  <option value="excluded">Excluded</option>
                </select>
              </div>

              <div>
                <label className="block text-xs font-medium text-text-secondary mb-1">
                  Correction
                </label>
                <select
                  value={correction}
                  onChange={(e) => editCorrection(e.target.value)}
                  className="w-full text-sm border border-border rounded-lg px-3 py-2 bg-card text-text-primary"
                >
                  <option value="none">None</option>
                  <option value="needed">Needed</option>
                  <option value="done">Done</option>
                </select>
              </div>

              <div>
                <label className="block text-xs font-medium text-text-secondary mb-1">
                  Notes
                </label>
                <textarea
                  value={notes}
                  onChange={(e) => editNotes(e.target.value)}
                  placeholder="Describe artifacts, masking needs, etc."
                  rows={4}
                  className="w-full text-sm border border-border rounded-lg px-3 py-2 bg-card text-text-primary placeholder:text-text-tertiary resize-none"
                />
              </div>

              <Button
                onClick={() => handleSave()}
                disabled={saving || !hasChanges}
                className="w-full"
              >
                {saving ? (
                  <><Loader2 className="w-4 h-4 mr-2 animate-spin" /> Saving...</>
                ) : saved ? (
                  <><Check className="w-4 h-4 mr-2" /> Saved</>
                ) : (
                  <><Save className="w-4 h-4 mr-2" /> Save Changes</>
                )}
              </Button>
            </div>
          </Card>
        </div>
      </div>
    </div>
  );
}

export default function ExposureDetailPage() {
  return (
    <Suspense
      fallback={
        <div className="flex items-center justify-center py-16">
          <Loader2 className="w-8 h-8 animate-spin text-primary" />
        </div>
      }
    >
      <ExposureDetailPageInner />
    </Suspense>
  );
}
