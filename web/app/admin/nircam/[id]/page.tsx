'use client';

import React, { Suspense, useState, useEffect, useCallback, useMemo, useRef } from 'react';
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
  type ExposurePngUrls,
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
  prefetchPreviewPng,
} from '@/lib/nircam-exposure-cache';

// Eager PNG prefetch window: previews (~1.3 MB) for a few exposures ahead + one
// back; the heavy full-res mask surface (~5.7 MB) only for the immediate next.
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
  const [masking, setMasking] = useState<string>('none');
  const [correction, setCorrection] = useState<string>('none');
  const [notes, setNotes] = useState<string>('');

  // Sibling-exposure nav: the ±window neighbors + absolute position within
  // the filtered, ordered set, from get_admin_exposure_neighbors. Survives
  // refresh and direct entry because the filter context lives in the URL.
  const [nav, setNav] = useState<ExposureNeighbors | null>(null);
  const [showHelp, setShowHelp] = useState(false);
  // N4 (epic #261): live FITS render vs the legacy pre-generated PNG. Defaults
  // to PNG; the toggle doubles as the pixel-parity check during the rollout.
  const [viewMode, setViewMode] = useState<'png' | 'fits'>('png');
  // Presigned OSN GET URLs (dual-read R2 fallback) for the current + windowed
  // exposures' PNGs (epic #261, N5), keyed by exposure id. Served straight into
  // <img> — no /api/nircam-preview proxy hop. Refreshed on navigation.
  const [pngUrls, setPngUrls] = useState<Record<number, ExposurePngUrls>>({});
  // Read-only mirror so the presign effect can skip ids already presigned:
  // re-presigning mints a fresh signature, which would change the <img src> and
  // force the browser to refetch a PNG the prefetch already cached.
  const pngUrlsRef = useRef(pngUrls);
  pngUrlsRef.current = pngUrls;
  useEffect(() => {
    let cancelled = false;
    getExposureNeighbors(id, { ...navFilters, window: PREFETCH_AHEAD }).then((res) => {
      if (cancelled) return;
      setNav(res.error || res.total === 0 ? null : res);
    });
    return () => { cancelled = true; };
  }, [id, navFilters]);

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
    setMasking(cached?.masking || 'none');
    setCorrection(cached?.correction || 'none');
    setNotes(cached?.notes || '');
    setLoading(!cached);
    setError(null);
    setSaved(false);
  }

  // Background revalidation against the DB on every id change. Auto-save on
  // navigation has already flushed any dirty triage state, so it's safe to
  // overwrite the editable fields with the freshly-fetched values.
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
        setCachedExposure(result.exposure);
        setExposure(result.exposure);
        setReviewStatus(result.exposure.review_status);
        setMasking(result.exposure.masking);
        setCorrection(result.exposure.correction);
        setNotes(result.exposure.notes || '');
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
  // <img> (no proxy hop). Preview (~1.3 MB) is prefetched across the whole
  // window; the heavy full-res mask surface (~5.7 MB) only for the immediate
  // next, so a fast tab-through stays ahead without flooding the network.
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
    // Only presign ids we haven't already (prefetched siblings keep their URL).
    const batch = [id, ...win].filter((x) => pngUrlsRef.current[x] === undefined);
    if (batch.length === 0) return;
    let cancelled = false;
    presignExposurePngs(batch).then((urls) => {
      if (cancelled) return;
      setPngUrls((prev) => ({ ...prev, ...urls }));
      // `urls` holds only the newly-presigned (previously-absent) ids, so each
      // sibling is prefetched exactly once, at the URL that will be reused.
      for (const sib of win) if (urls[sib]) prefetchPreviewPng(urls[sib].preview);
      // Heavy full-res PNG: only the genuine immediate next (null at the end).
      if (nav.nextId != null && urls[nav.nextId]) prefetchPreviewPng(urls[nav.nextId].full);
    });
    return () => { cancelled = true; };
  }, [id, nav]);

  const hasChanges = !!(exposure && (
    reviewStatus !== exposure.review_status ||
    masking !== exposure.masking ||
    correction !== exposure.correction ||
    notes !== (exposure.notes || '')
  ));

  // Latest-state ref so the keyboard handler doesn't capture stale closures.
  const stateRef = useRef({ reviewStatus, masking, correction, notes, hasChanges });
  stateRef.current = { reviewStatus, masking, correction, notes, hasChanges };

  const handleSave = useCallback(async (): Promise<{ ok: boolean }> => {
    const s = stateRef.current;
    setSaving(true);
    setSaved(false);
    setError(null);
    const result = await updateExposureReview(id, {
      review_status: s.reviewStatus as NircamExposure['review_status'],
      masking: s.masking as NircamExposure['masking'],
      correction: s.correction as NircamExposure['correction'],
      notes: s.notes || undefined,
    });
    setSaving(false);
    if (result.error) {
      setError(result.error);
      return { ok: false };
    }
    if (result.exposure) {
      setCachedExposure(result.exposure);
      setExposure(result.exposure);
    }
    setSaved(true);
    setTimeout(() => setSaved(false), 2000);
    return { ok: true };
  }, [id]);

  // Auto-save on nav: mirror inspection mode — flush dirty triage so the
  // operator can blast through a queue with arrow keys without losing state.
  const goTo = useCallback(async (targetId: number | null) => {
    if (targetId == null) return;
    if (stateRef.current.hasChanges) {
      const result = await handleSave();
      if (!result.ok) return; // don't navigate on a save failure
    }
    // Carry the filter context so the next exposure's nav walks the same set.
    router.push(`/admin/nircam/${targetId}${navQuery ? `?${navQuery}` : ''}`);
  }, [handleSave, router, navQuery]);

  const handleNext = useCallback(() => goTo(nav?.nextId ?? null), [goTo, nav]);
  const handlePrev = useCallback(() => goTo(nav?.prevId ?? null), [goTo, nav]);

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
        case '1': e.preventDefault(); setReviewStatus('pending');  break;
        case '2': e.preventDefault(); setReviewStatus('approved'); break;
        case '3': e.preventDefault(); setReviewStatus('excluded'); break;
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
  }, [handleNext, handlePrev, handleSave, showHelp]);

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
  // round-trip lands; null once resolved if the exposure has no PNG).
  const currentUrls = pngUrls[id];
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
    const res = await saveExposureMaskRegions(exposure.id, regions);
    if (res.exposure) {
      setCachedExposure(res.exposure);
      setExposure(res.exposure);
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
            <div className="flex justify-between"><dt>Next exposure</dt><dd className="font-mono text-text-secondary">→ or N</dd></div>
            <div className="flex justify-between"><dt>Previous</dt><dd className="font-mono text-text-secondary">← or P</dd></div>
            <div className="flex justify-between"><dt>Mark pending</dt><dd className="font-mono text-text-secondary">1</dd></div>
            <div className="flex justify-between"><dt>Mark approved</dt><dd className="font-mono text-text-secondary">2</dd></div>
            <div className="flex justify-between"><dt>Mark excluded</dt><dd className="font-mono text-text-secondary">3</dd></div>
            <div className="flex justify-between"><dt>Save</dt><dd className="font-mono text-text-secondary">S</dd></div>
            <div className="flex justify-between"><dt>Help</dt><dd className="font-mono text-text-secondary">?</dd></div>
          </dl>
          <p className="mt-2 text-xs text-text-secondary">
            Navigation auto-saves the triage panel if there are unsaved changes. Mask edits save separately from the editor toolbar.
          </p>
        </div>
      )}

      {error && (
        <div className="bg-red-50 dark:bg-red-950 border border-red-200 dark:border-red-900 rounded-lg p-4 mb-6">
          <p className="text-red-800 dark:text-red-400">{error}</p>
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
                  onChange={(e) => setReviewStatus(e.target.value)}
                  className="w-full text-sm border border-border rounded-lg px-3 py-2 bg-card text-text-primary"
                >
                  <option value="pending">Pending</option>
                  <option value="approved">Approved</option>
                  <option value="excluded">Excluded</option>
                </select>
              </div>

              <div>
                <label className="block text-xs font-medium text-text-secondary mb-1">
                  Masking
                </label>
                <select
                  value={masking}
                  onChange={(e) => setMasking(e.target.value)}
                  className="w-full text-sm border border-border rounded-lg px-3 py-2 bg-card text-text-primary"
                >
                  <option value="none">None</option>
                  <option value="needed">Needed</option>
                  <option value="done">Done</option>
                </select>
              </div>

              <div>
                <label className="block text-xs font-medium text-text-secondary mb-1">
                  Correction
                </label>
                <select
                  value={correction}
                  onChange={(e) => setCorrection(e.target.value)}
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
                  onChange={(e) => setNotes(e.target.value)}
                  placeholder="Describe artifacts, masking needs, etc."
                  rows={4}
                  className="w-full text-sm border border-border rounded-lg px-3 py-2 bg-card text-text-primary placeholder:text-text-tertiary resize-none"
                />
              </div>

              <Button
                onClick={handleSave}
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
