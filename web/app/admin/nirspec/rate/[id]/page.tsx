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
  getNirspecRateExposureById,
  getNirspecRateNeighbors,
  updateNirspecRateReview,
  saveRateMaskRegions,
  type RateNeighbors,
} from '@/lib/actions/nirspec-rate';
import type { NirspecRateExposure, MaskRegionsPayload } from '@/lib/types';
import MaskEditor from '@/components/nircam/MaskEditor';
import { parseRateNavParams } from '@/lib/nirspec-rate-nav';
import { getCachedRate, setCachedRate } from '@/lib/nirspec-rate-cache';

function RateDetailPageInner() {
  const params = useParams();
  const router = useRouter();
  const searchParams = useSearchParams();
  const id = Number(params.id);

  // The list page's filter+sort state, carried in the URL (see
  // lib/nirspec-rate-nav.ts). Defines the ordered set prev/next walks; empty
  // params (direct entry) = the full unfiltered set, so nav always works.
  const navFilters = useMemo(
    () => parseRateNavParams(new URLSearchParams(searchParams.toString())),
    [searchParams],
  );
  const navQuery = searchParams.toString();

  const [exposure, setExposure] = useState<NirspecRateExposure | null>(null);
  const [exposureForId, setExposureForId] = useState<number | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Editable triage fields
  const [reviewStatus, setReviewStatus] = useState<string>('pending');
  const [notes, setNotes] = useState<string>('');

  const [nav, setNav] = useState<RateNeighbors | null>(null);
  const [showHelp, setShowHelp] = useState(false);

  useEffect(() => {
    let cancelled = false;
    getNirspecRateNeighbors(id, { ...navFilters, window: 3 }).then((res) => {
      if (cancelled) return;
      setNav(res.error || res.total === 0 ? null : res);
    });
    return () => { cancelled = true; };
  }, [id, navFilters]);

  // Reset state synchronously from the in-memory cache on route-id change so the
  // new row paints in the same frame (React's "set state during render" pattern,
  // bounded by the exposureForId guard). Server hit in the background revalidates.
  if (exposureForId !== id) {
    const cached = getCachedRate(id);
    setExposureForId(id);
    setExposure(cached);
    setReviewStatus(cached?.review_status || 'pending');
    setNotes(cached?.notes || '');
    setLoading(!cached);
    setError(null);
    setSaved(false);
  }

  // Background revalidation against the DB on every id change.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      const result = await getNirspecRateExposureById(id);
      if (cancelled) return;
      if (result.error) {
        setError(result.error);
        setLoading(false);
        return;
      }
      if (result.exposure) {
        setCachedRate(result.exposure);
        setExposure(result.exposure);
        setReviewStatus(result.exposure.review_status);
        setNotes(result.exposure.notes || '');
      }
      setLoading(false);
    })();
    return () => { cancelled = true; };
  }, [id]);

  // Warm the sibling data cache (prev/next) so navigation paints instantly.
  useEffect(() => {
    if (!nav) return;
    for (const sibId of [nav.nextId, nav.prevId]) {
      if (sibId == null || getCachedRate(sibId)) continue;
      getNirspecRateExposureById(sibId).then((res) => {
        if (res.exposure) setCachedRate(res.exposure);
      });
    }
  }, [nav]);

  const hasChanges = !!(exposure && (
    reviewStatus !== exposure.review_status ||
    notes !== (exposure.notes || '')
  ));

  const stateRef = useRef({ reviewStatus, notes, hasChanges });
  stateRef.current = { reviewStatus, notes, hasChanges };

  const handleSave = useCallback(async (): Promise<{ ok: boolean }> => {
    const s = stateRef.current;
    setSaving(true);
    setSaved(false);
    setError(null);
    const result = await updateNirspecRateReview(id, {
      review_status: s.reviewStatus as NirspecRateExposure['review_status'],
      notes: s.notes || undefined,
    });
    setSaving(false);
    if (result.error) {
      setError(result.error);
      return { ok: false };
    }
    if (result.exposure) {
      setCachedRate(result.exposure);
      setExposure(result.exposure);
    }
    setSaved(true);
    setTimeout(() => setSaved(false), 2000);
    return { ok: true };
  }, [id]);

  // Auto-save on nav: flush dirty triage so the operator can blast through a
  // queue with arrow keys without losing state.
  const goTo = useCallback(async (targetId: number | null) => {
    if (targetId == null) return;
    if (stateRef.current.hasChanges) {
      const result = await handleSave();
      if (!result.ok) return; // don't navigate on a save failure
    }
    router.push(`/admin/nirspec/rate/${targetId}${navQuery ? `?${navQuery}` : ''}`);
  }, [handleSave, router, navQuery]);

  const handleNext = useCallback(() => goTo(nav?.nextId ?? null), [goTo, nav]);
  const handlePrev = useCallback(() => goTo(nav?.prevId ?? null), [goTo, nav]);

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
        <p className="text-text-secondary">Rate exposure not found.</p>
        <Link href="/admin/nirspec/rate" className="text-primary hover:underline mt-2 inline-block">
          Back to NIRSpec Rate
        </Link>
      </div>
    );
  }

  // FITS masking needs the canonical key (carried on the P2 row) plus the pixel
  // dims for the overlay viewBox. Any null → render an explanatory fallback
  // instead of crashing the canvas (e.g. deploy couldn't read the rate header).
  const fitsMaskAvailable = Boolean(
    exposure.storage_key && exposure.image_width && exposure.image_height,
  );

  const handleSaveMasks = async (regions: MaskRegionsPayload) => {
    const res = await saveRateMaskRegions(exposure.id, regions);
    if (res.exposure) {
      setCachedRate(res.exposure);
      setExposure(res.exposure);
    }
    return { error: res.error };
  };

  return (
    <div>
      {/* Header */}
      <div className="flex items-center gap-3 mb-6">
        <button
          onClick={() => router.push(`/admin/nirspec/rate${navQuery ? `?${navQuery}` : ''}`)}
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
            {exposure.observation} / {exposure.detector}
            {exposure.grating ? ` / ${exposure.grating}` : ''}
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
        {/* Image viewer: live FITS render (SCI) + mask editor */}
        <div className="flex-1 min-w-0">
          <Card className="overflow-hidden">
            {fitsMaskAvailable ? (
              <div className="h-[80vh]">
                <MaskEditor
                  fitsKey={exposure.storage_key!}
                  imageWidth={exposure.image_width!}
                  imageHeight={exposure.image_height!}
                  initialRegions={exposure.mask_regions}
                  onSave={handleSaveMasks}
                />
              </div>
            ) : (
              <div className="flex flex-col items-center justify-center py-24 text-center text-text-secondary">
                <p className="mb-1">Rate FITS not available for masking.</p>
                <p className="text-xs">
                  The row is missing its <span className="font-mono">storage_key</span> or image
                  dimensions — re-run <span className="font-mono">campfire deploy</span> for this
                  observation to register the rate file.
                </p>
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
                <dt className="text-text-secondary">Observation</dt>
                <dd className="text-text-primary">{exposure.observation}</dd>
              </div>
              <div className="flex justify-between">
                <dt className="text-text-secondary">Exposure</dt>
                <dd className="text-text-primary font-mono text-xs">{exposure.exposure_root}</dd>
              </div>
              <div className="flex justify-between">
                <dt className="text-text-secondary">Detector</dt>
                <dd className="text-text-primary">{exposure.detector}</dd>
              </div>
              <div className="flex justify-between">
                <dt className="text-text-secondary">Grating</dt>
                <dd className="text-text-primary">{exposure.grating || '—'}</dd>
              </div>
              <div className="flex justify-between">
                <dt className="text-text-secondary">Stage</dt>
                <dd className="text-text-primary font-mono text-xs">{exposure.stage}</dd>
              </div>
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
                  Notes
                </label>
                <textarea
                  value={notes}
                  onChange={(e) => setNotes(e.target.value)}
                  placeholder="Describe persistence trails, MSA shorts, etc."
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

export default function RateDetailPage() {
  return (
    <Suspense
      fallback={
        <div className="flex items-center justify-center py-16">
          <Loader2 className="w-8 h-8 animate-spin text-primary" />
        </div>
      }
    >
      <RateDetailPageInner />
    </Suspense>
  );
}
