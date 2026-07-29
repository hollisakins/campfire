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
  deleteCachedExposure,
  prefetchPng,
  isPngCached,
  getCachedPngUrls,
  setCachedPngUrls,
  hasPendingSave,
  hasAnyPendingSave,
  beginPendingSave,
  endPendingSave,
  getSaveError,
  setSaveError,
  subscribeSaveState,
  getSaveStateVersion,
  enqueueSave,
  nextSaveSeq,
  markSaveCommitted,
  hasNewerCommittedSave,
} from '@/lib/nircam-exposure-cache';

// Eager PNG prefetch window: warm the full-res mask surface (~5.7 MB) the
// editor actually renders for a few exposures ahead + one back, so stepping
// through the queue paints instantly. Falls back to the preview (~1.3 MB) for
// exposures that have no full PNG (the thumbnail-only view).
const PREFETCH_AHEAD = 3;
const PREFETCH_BEHIND = 1;

// A mouse click leaves a button focused, and the shortcut handler yields Space
// to focused buttons (it's their native activation key) — so without this,
// clicking any button once turns every later Space press into "click that
// button again" instead of approve-and-next, which silently skips the approve.
// Keyboard activation (detail === 0) keeps focus for tab-navigation users.
function blurOnMouseClick(e: React.MouseEvent<HTMLElement>) {
  if (e.detail > 0) e.currentTarget.blur();
}

function ExposureDetailPageInner() {
  const params = useParams();
  const router = useRouter();
  const searchParams = useSearchParams();

  // Client-side stepping: prev/next swaps the exposure via local state +
  // history.pushState, NOT router.push. A router.push between dynamic
  // segments paints nothing until the new page's RSC payload has
  // round-tripped from the server — several hundred ms frozen on the old
  // exposure with no loading indicator (none of the new page's spinners exist
  // yet), during which keystrokes still target the OLD exposure, so a fast
  // operator's decisions land on the wrong row or appear to vanish. With
  // local stepping the id swaps in the same frame as the keypress and every
  // loading state below actually engages; the URL stays in sync for
  // refresh/share/back, and Next-driven navigations (direct entry, list
  // links) seed from the route param.
  const routeId = Number(params.id);
  const [id, setId] = useState(routeId);
  // Synchronous mirror of `id` for handlers that can fire faster than React
  // re-renders — a double-tapped arrow in one frame must not double-step.
  const idRef = useRef(id);
  idRef.current = id;
  // A real navigation changed the route param (back/forward or an entry that
  // didn't remount): the route wins over local stepping state.
  const lastRouteIdRef = useRef(routeId);
  if (lastRouteIdRef.current !== routeId) {
    lastRouteIdRef.current = routeId;
    if (id !== routeId) setId(routeId);
  }
  // Back/forward across locally-pushed steps: Next may not remount for URLs
  // written via history.pushState, so follow popstate ourselves as well.
  useEffect(() => {
    const onPop = () => {
      const m = window.location.pathname.match(/\/admin\/nircam\/(\d+)(?:\/|$)/);
      if (m) setId(Number(m[1]));
    };
    window.addEventListener('popstate', onPop);
    return () => window.removeEventListener('popstate', onPop);
  }, []);

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
  // tell that it no longer owns this state. Reset on every id change (below),
  // so arriving at a fresh exposure still takes the server's values.
  //
  // `values` carries the operator's edited values alongside the flags: this
  // record — not React state — is the authoritative "what did the operator
  // decide, for which exposure". It is written synchronously on every edit,
  // so the save flush never depends on a re-render having happened.
  const localEditsRef = useRef<{
    forId: number | null;
    dirty: { reviewStatus: boolean; correction: boolean; notes: boolean };
    values: { reviewStatus: string; correction: string; notes: string };
    saved: boolean;
  }>({
    forId: null,
    dirty: { reviewStatus: false, correction: false, notes: false },
    values: { reviewStatus: 'pending', correction: 'none', notes: '' },
    saved: false,
  });
  // Every edit targets idRef.current — the exposure the operator believes is
  // (or is about to be) on screen — NOT whatever the last committed render
  // showed. The keyboard handler is a native document listener, so its
  // setState calls get default priority and can lag: press `→` then `2`
  // quickly and the `2` fires while the id transition is still uncommitted.
  // Untargeted, that edit lands on the OLD record and the incoming render
  // reset wipes it — the status flashes Approved for a frame, reverts to
  // Pending, and the next flush has nothing dirty to save. Retargeting stages
  // the edit for the incoming exposure; the reset below re-applies it instead
  // of wiping it.
  const targetEdits = useCallback(() => {
    const target = idRef.current;
    if (localEditsRef.current.forId !== target) {
      localEditsRef.current = {
        forId: target,
        dirty: { reviewStatus: false, correction: false, notes: false },
        // Baselines for as-yet-untouched fields; only dirty fields are ever
        // read out of `values`, so stale entries here are never used.
        values: {
          reviewStatus: stateRef.current.reviewStatus,
          correction: stateRef.current.correction,
          notes: stateRef.current.notes,
        },
        saved: false,
      };
    }
    return localEditsRef.current;
  }, []);
  const editStatus = useCallback((v: string) => {
    const edits = targetEdits();
    edits.dirty.reviewStatus = true;
    edits.values.reviewStatus = v;
    stateRef.current.reviewStatus = v;
    setReviewStatus(v);
  }, [targetEdits]);
  const editCorrection = useCallback((v: string) => {
    const edits = targetEdits();
    edits.dirty.correction = true;
    edits.values.correction = v;
    stateRef.current.correction = v;
    setCorrection(v);
  }, [targetEdits]);
  const editNotes = useCallback((v: string) => {
    const edits = targetEdits();
    edits.dirty.notes = true;
    edits.values.notes = v;
    stateRef.current.notes = v;
    setNotes(v);
  }, [targetEdits]);

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
    // Edits keyed to the incoming id were staged by a keypress that raced this
    // transition (see targetEdits) — they are the operator's decision for THIS
    // exposure and must be applied over the cached baseline, not wiped.
    const staged = localEditsRef.current.forId === id ? localEditsRef.current : null;
    setExposureForId(id);
    setExposure(cached);
    setReviewStatus(staged?.dirty.reviewStatus
      ? staged.values.reviewStatus : (cached?.review_status || 'pending'));
    setCorrection(staged?.dirty.correction
      ? staged.values.correction : (cached?.correction || 'none'));
    setNotes(staged?.dirty.notes
      ? staged.values.notes : (cached?.notes || ''));
    setLoading(!cached);
    setError(null);
    setSaved(false);
    if (!staged) {
      localEditsRef.current = {
        forId: id,
        dirty: { reviewStatus: false, correction: false, notes: false },
        values: {
          reviewStatus: cached?.review_status || 'pending',
          correction: cached?.correction || 'none',
          notes: cached?.notes || '',
        },
        saved: false,
      };
    }
  }

  // Background revalidation against the DB on every id change. Auto-save on
  // navigation has already flushed the *previous* exposure's triage state, so
  // arriving clean means the server's values win. But this read races the
  // operator: anything they've typed or keyed since arriving, and any save that
  // has already landed, is newer than this response and must survive it.
  useEffect(() => {
    let cancelled = false;
    // Captured before the read is issued: a save in flight NOW means the
    // response may reflect pre-save state even if the save has finished (and
    // cleared its pending marker) by the time the response resolves.
    const pendingAtStart = hasPendingSave(id);
    (async () => {
      const result = await getNircamExposureById(id);
      if (cancelled) return;
      if (result.error) {
        setError(result.error);
        setLoading(false);
        return;
      }
      if (result.exposure) {
        // The edits record only speaks for this exposure when tagged with its
        // id (a mid-transition keypress can retarget it — see targetEdits).
        const edits = localEditsRef.current;
        const ours = edits.forId === id;
        // A save for this exposure already landed — our write is strictly newer
        // than this read, so don't let it back into state *or* the cache. Same
        // for a save in flight at either end of this read (fire-and-forget
        // auto-save on nav, then a quick revisit): the optimistic or
        // save-confirmed row is newer than what this read may have seen.
        const pending = pendingAtStart || hasPendingSave(id);
        if (!(ours && edits.saved) && !pending) {
          setCachedExposure(result.exposure);
          setExposure(result.exposure);
        }
        // Per field: an untouched control still takes the fresh server value.
        if (!(ours && edits.dirty.reviewStatus) && !pending) setReviewStatus(result.exposure.review_status);
        if (!(ours && edits.dirty.correction) && !pending) setCorrection(result.exposure.correction);
        if (!(ours && edits.dirty.notes) && !pending) setNotes(result.exposure.notes || '');
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
        // Re-checked at RESPONSE time, not just dispatch: the operator can
        // arrive at this sibling and key a decision while this read is in
        // flight (one slow round trip is all it takes). Any cache entry that
        // appeared since dispatch — the optimistic row or a save confirmation
        // — is strictly newer than what this read saw, and a pending save with
        // no cache entry (row never loaded) outranks it too. Overwriting here
        // painted the pre-decision row on the next revisit as if the decision
        // had reverted.
        if (!res.exposure) return;
        if (getCachedExposure(sibId) || hasPendingSave(sibId)) return;
        setCachedExposure(res.exposure);
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

  // Warn before unload while fire-and-forget saves are still in flight — a
  // closed tab takes the in-flight decision (and any failure banner) with it.
  useEffect(() => {
    const handler = (e: BeforeUnloadEvent) => {
      if (hasAnyPendingSave()) e.preventDefault();
    };
    window.addEventListener('beforeunload', handler);
    return () => window.removeEventListener('beforeunload', handler);
  }, []);

  // A failed save reverts the cached row to the server's truth; if that
  // exposure is the one on screen, re-baseline `exposure` from the cache so
  // hasChanges compares against reality and a retry actually fires (otherwise
  // the optimistic row read at mount claims the decision already stuck).
  useEffect(() => {
    if (saveError?.id !== id) return;
    const cached = getCachedExposure(id);
    if (cached) setExposure(cached);
  }, [saveError, id]);

  const handleSave = useCallback(async (): Promise<{ ok: boolean }> => {
    const s = stateRef.current;
    // No baseline row yet (direct entry, still loading): the controls hold
    // defaults, and saving them would overwrite the row's real correction and
    // notes with 'none' / ''.
    if (!s.exposure) return { ok: false };
    // The exposure this save is for. The `S` shortcut fires handleSave
    // un-awaited and mask saves aren't gated by navigation at all — so a
    // response can land after the route moved on.
    const savedForId = id;
    setSaving(true);
    setSaved(false);
    setError(null);
    // Same per-exposure queue as the fire-and-forget nav save, so an explicit
    // save and a background one for this row can't commit out of order. Takes
    // a dispatch generation for the same reason: a success here must be
    // visible to an older failed save's suspended cleanup handler.
    const saveSeq = nextSaveSeq(savedForId);
    const result = await enqueueSave(savedForId, () => updateExposureReview(savedForId, {
      review_status: s.reviewStatus as NircamExposure['review_status'],
      correction: s.correction as NircamExposure['correction'],
      // Always sent (even '') so clearing the notes field actually persists —
      // an undefined key is dropped from the PATCH and leaves the old value.
      notes: s.notes,
    })).catch((err: unknown) => ({
      exposure: null,
      error: err instanceof Error ? err.message : 'Save failed',
    }));
    setSaving(false);
    if (result.error) {
      // Surfaced even if we've navigated on: a failed save means the operator's
      // decision didn't stick, which they need to know about either way.
      setError(result.error);
      return { ok: false };
    }
    // Keyed by the exposure's own id, so this is correct regardless of route.
    if (result.exposure) {
      markSaveCommitted(savedForId, saveSeq);
      // Same guard as the fire-and-forget success path: a newer save queued
      // behind this one (edit + navigate before this resolved) has already
      // written its optimistic row — don't put this older row over it.
      if (!hasPendingSave(savedForId)) setCachedExposure(result.exposure);
      // A retry that lands supersedes an earlier failure for this exposure —
      // same rule as the fire-and-forget path, or the banner outlives the fix.
      if (getSaveError()?.id === savedForId) setSaveError(null);
    }
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

  // Fire-and-forget flush of the operator's dirty triage state — the write
  // half of auto-save-on-nav, also used by the back-to-list button. The
  // decision goes into the row cache optimistically (that's what the next
  // visit paints from) and the server action fires without blocking the
  // caller. The pending-save registry keeps a quick revisit's revalidation
  // from resurrecting the pre-save row; a failure reverts the cache to the
  // server's truth and raises the module save-error banner, since this
  // instance is usually gone by then.
  //
  // The save is built from the edits record ALONE — forId says which exposure,
  // dirty says which fields, values says what the operator chose, all written
  // synchronously at edit time. Nothing here depends on a re-render having
  // happened or on which exposure the last committed render showed, so a
  // decision keyed in during an uncommitted id transition still saves, and it
  // saves to the right row. The update is PARTIAL: only touched fields are
  // sent, so a not-yet-loaded row's untouched values can never be overwritten
  // with defaults.
  const flushDirtySave = useCallback(() => {
    const s = stateRef.current;
    const edits = localEditsRef.current;
    if (edits.forId == null) return;
    const savedForId = edits.forId;
    const dirty = edits.dirty;
    if (!dirty.reviewStatus && !dirty.correction && !dirty.notes) return;

    // Baseline for no-op detection and the optimistic write: the on-screen row
    // when it is this record's row, else the cache. May be null (row never
    // loaded) — the save still goes out, just without a diff to skip on.
    const exp = s.exposure && s.exposure.id === savedForId
      ? s.exposure
      : getCachedExposure(savedForId);

    const updates: Parameters<typeof updateExposureReview>[1] = {};
    if (dirty.reviewStatus && (!exp || edits.values.reviewStatus !== exp.review_status)) {
      updates.review_status = edits.values.reviewStatus as NircamExposure['review_status'];
    }
    if (dirty.correction && (!exp || edits.values.correction !== exp.correction)) {
      updates.correction = edits.values.correction as NircamExposure['correction'];
    }
    if (dirty.notes && (!exp || edits.values.notes !== (exp.notes || ''))) {
      updates.notes = edits.values.notes; // sent even as '' so clearing persists
    }
    if (Object.keys(updates).length === 0) return;

    const savedFilename = exp?.filename ?? `exposure #${savedForId}`;
    if (exp) {
      setCachedExposure({
        ...exp,
        review_status: updates.review_status ?? exp.review_status,
        correction: updates.correction ?? exp.correction,
        notes: updates.notes !== undefined ? (updates.notes || null) : exp.notes,
      });
    }
    {
      beginPendingSave(savedForId);
      // endPendingSave must run exactly once per beginPendingSave on every
      // path — including a transport rejection, which would otherwise leave
      // the id pending for the rest of the tab session (blocking revalidation
      // and the failure banner alike).
      let ended = false;
      const endPending = () => {
        if (ended) return;
        ended = true;
        endPendingSave(savedForId);
      };
      // Queued per exposure id so a second save for the same row (revisit +
      // re-edit before the first lands) can never commit or report out of
      // order with this one. The dispatch generation lets the failure handler
      // below — which awaits, so it can resume after a newer save has fully
      // committed — recognize that it has been superseded.
      const saveSeq = nextSaveSeq(savedForId);
      enqueueSave(savedForId, () => updateExposureReview(savedForId, updates))
        .then(async (result) => {
        if (result.exposure && !result.error) {
          endPending();
          markSaveCommitted(savedForId, saveSeq);
          // A newer save queued behind this one has already written its own
          // optimistic row — don't put this (older) confirmed row over it.
          if (!hasPendingSave(savedForId)) setCachedExposure(result.exposure);
          // A retry that lands supersedes an earlier failure for this exposure.
          if (getSaveError()?.id === savedForId) setSaveError(null);
          return;
        }
        throw new Error(result.error || 'Save failed');
      }).catch(async (err: unknown) => {
        endPending();
        // Put the server's row back over the optimistic one (best effort —
        // the transport may be down), then tell the operator: they may be
        // several exposures ahead by now. Both steps are void if a newer save
        // for this row committed while this handler was suspended: the revert
        // snapshot predates that save, and the banner would report a failure
        // for a decision that did persist.
        const owned = () =>
          !hasPendingSave(savedForId) && !hasNewerCommittedSave(savedForId, saveSeq);
        try {
          const fresh = await getNircamExposureById(savedForId);
          if (fresh.exposure) {
            if (owned()) setCachedExposure(fresh.exposure);
          } else if (owned()) {
            // The revert read failed too. Don't leave the never-persisted
            // optimistic row posing as truth (a revisit would paint it as
            // saved, with hasChanges false and no way to retry) — drop the
            // entry so the next visit misses the cache and refetches.
            deleteCachedExposure(savedForId);
          }
        } catch {
          if (owned()) deleteCachedExposure(savedForId);
        }
        if (!hasNewerCommittedSave(savedForId, saveSeq)) {
          setSaveError({
            id: savedForId,
            filename: savedFilename,
            message: err instanceof Error ? err.message : 'Save failed',
          });
        }
      });
    }
  }, []);

  // Auto-save on nav: flush the dirty triage state (fire-and-forget), then
  // swap the exposure locally in the same tick — the operator can blast
  // through a queue with arrow keys without losing state or waiting on any
  // round trip (save or navigation).
  const goTo = useCallback((targetId: number | null) => {
    // targetId === current id means the neighbor window is stale in a way the
    // derivation above couldn't repair; stepping would be a no-op that looks
    // like a step. Checked against the sync mirror so a double-tap inside one
    // frame can't double-step.
    if (targetId == null || targetId === idRef.current) return;
    flushDirtySave();
    idRef.current = targetId;
    setId(targetId);
    // Keep the URL shareable/refreshable, carrying the filter context so the
    // next exposure's nav walks the same set (see the routeId comment above
    // for why this is history.pushState and not router.push).
    window.history.pushState(null, '', `/admin/nircam/${targetId}${navQuery ? `?${navQuery}` : ''}`);
  }, [flushDirtySave, navQuery]);

  const handleNext = useCallback(() => goTo(nav?.nextId ?? null), [goTo, nav]);
  const handlePrev = useCallback(() => goTo(nav?.prevId ?? null), [goTo, nav]);

  // The 90% case as a single keystroke: mark approved and advance. Routed
  // through goTo's status override because the setState here isn't visible in
  // stateRef until the next render. At the end of the queue there's nowhere to
  // advance to, so persist the decision in place instead.
  const approveAndNext = useCallback(() => {
    // Nothing on screen yet to approve: marking now would just pin 'approved'
    // over whatever the row actually holds once it loads.
    if (!stateRef.current.exposure) return;
    editStatus('approved');
    // "End of queue" only when the neighbors result is FRESH for this
    // exposure. A null or stale nav (RPC still in flight after a fast Space
    // burst, the stale window's edge, or an RPC error) means "unknown": mark
    // approved and wait — mirroring the disabled arrows — rather than firing
    // redundant in-place saves; the next press advances once nav lands, and
    // that navigation auto-saves the marked status.
    const freshNav = navState?.forId === id ? navState.data : null;
    if (freshNav && freshNav.nextId == null) {
      // editStatus above already staged + synced the value, so a plain save
      // picks it up.
      handleSave();
      return;
    }
    const nextId = nav?.nextId ?? null;
    if (nextId != null && nextId !== id) goTo(nextId);
  }, [editStatus, navState, nav, id, goTo, handleSave]);

  // Global keyboard shortcuts (mirrors web/components/spectra/inspection
  // pattern). Skip when an input has focus so users can type in notes etc.
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement;
      const isInput = t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.tagName === 'SELECT';
      if (e.key === 'Escape' && isInput) { t.blur(); return; }
      if (isInput) return;
      if (e.metaKey || e.ctrlKey || e.altKey) return;
      // Space is the browser's native activation key for a focused button or
      // link (and buttons keep focus after a click) — let it press the control
      // instead of firing approve-and-next. Other shortcut keys don't overlap
      // button semantics, so they still work with a button focused.
      if (e.key === ' ' && (t.tagName === 'BUTTON' || t.tagName === 'A')) return;

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
    //
    // Serialized through the SAME per-exposure queue as the triage saves.
    // Unqueued, a mask save and a nav triage save for this row could commit
    // at the database in either order, and each confirmation only reflects
    // the fields the database held at ITS commit — so whichever response was
    // snapshotted first is missing the other's write, and no response-time
    // guard can reassemble the truth from two partial rows. Queued, dispatch
    // order is commit order and each later confirmation contains every
    // earlier write. Marked pending for the same reason as triage saves:
    // revalidation and the sibling prefetch must not resurrect the pre-save
    // row while this write is in flight.
    const savedForId = exposure.id;
    beginPendingSave(savedForId);
    let res: Awaited<ReturnType<typeof saveExposureMaskRegions>>;
    try {
      res = await enqueueSave(savedForId, () =>
        saveExposureMaskRegions(savedForId, regions));
    } catch (err) {
      res = {
        exposure: null,
        error: err instanceof Error ? err.message : 'Save failed',
      };
    } finally {
      endPendingSave(savedForId);
    }
    if (res.exposure) {
      // A save queued behind this one (triage flush during the mask round
      // trip) has already written its optimistic row, and — because the queue
      // serializes commits — its confirmation will carry this mask write too.
      // Only the newest pending state may own the cache.
      if (!hasPendingSave(savedForId)) setCachedExposure(res.exposure);
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
          onClick={(e) => {
            blurOnMouseClick(e);
            // Leaving the triage flow still flushes a pending decision.
            flushDirtySave();
            router.push(`/admin/nircam${navQuery ? `?${navQuery}` : ''}`);
          }}
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
              onClick={(e) => { blurOnMouseClick(e); handlePrev(); }}
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
              onClick={(e) => { blurOnMouseClick(e); handleNext(); }}
              disabled={nav.nextId == null}
              title="Next (→ / N)"
              className="p-1.5 rounded hover:bg-card-hover disabled:opacity-30 disabled:cursor-not-allowed"
            >
              <ChevronRight className="w-5 h-5" />
            </button>
          </div>
        )}
        <button
          onClick={(e) => { blurOnMouseClick(e); setShowHelp(prev => !prev); }}
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
            onClick={(e) => { blurOnMouseClick(e); setSaveError(null); }}
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
              onClick={(e) => { blurOnMouseClick(e); setViewMode('png'); }}
              className={`rounded-md px-3 py-1 ${viewMode === 'png' ? 'bg-card-hover text-text-primary' : 'text-text-secondary'}`}
            >
              PNG
            </button>
            <button
              onClick={(e) => { blurOnMouseClick(e); setViewMode('fits'); }}
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
              <PreviewImage url={pngUrl} alt={`${exposure.filename} quick-look`} />
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
                onClick={(e) => { blurOnMouseClick(e); handleSave(); }}
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

// Thumbnail-only fallback with a cold-load spinner: a plain <img> mounted with
// an unfetched URL is invisible while it downloads, which reads as "nothing is
// happening" when stepping faster than the prefetch window. Warm mounts (URL
// already decoded in the retained cache) skip the spinner entirely.
function PreviewImage({ url, alt }: { url: string; alt: string }) {
  const [loaded, setLoaded] = useState(() => isPngCached(url));
  const imgRef = useRef<HTMLImageElement>(null);
  // Reset per URL, and catch an image that completed before React attached the
  // onLoad handler (cached responses can land between render and commit).
  useEffect(() => {
    setLoaded(isPngCached(url) || !!imgRef.current?.complete);
  }, [url]);
  return (
    <div className="relative">
      {/* eslint-disable-next-line @next/next/no-img-element */}
      <img
        ref={imgRef}
        src={url}
        alt={alt}
        className="w-full h-auto"
        onLoad={() => setLoaded(true)}
      />
      {!loaded && (
        <div className="absolute inset-0 flex items-center justify-center py-24">
          <Loader2 className="w-8 h-8 animate-spin text-text-secondary" />
        </div>
      )}
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
