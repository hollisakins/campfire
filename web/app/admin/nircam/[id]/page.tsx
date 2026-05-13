'use client';

import React, { useState, useEffect, useCallback, useRef } from 'react';
import { useParams, useRouter } from 'next/navigation';
import Link from 'next/link';
import { Card } from '@/components/ui/Card';
import { Button } from '@/components/ui/Button';
import {
  Loader2, ArrowLeft, Save, Check, ChevronLeft, ChevronRight, Keyboard,
} from 'lucide-react';
import {
  getNircamExposureById,
  updateExposureReview,
  saveExposureMaskRegions,
} from '@/lib/actions/nircam-exposures';
import type { NircamExposure, MaskRegionsPayload } from '@/lib/types';
import { stageBadgeClasses } from '@/lib/nircam-stages';
import MaskEditor from '@/components/nircam/MaskEditor';
import { lookupNircamNav, type NircamNavLookup } from '@/lib/nircam-nav-cache';

// PNGs live in R2 under nircam/exposures/<field>/<filter>/...; the
// /api/nircam-preview proxy handles auth and streams them same-origin.
function previewUrl(r2Key: string | null): string | null {
  if (!r2Key) return null;
  return `/api/nircam-preview?key=${encodeURIComponent(r2Key)}`;
}

export default function ExposureDetailPage() {
  const params = useParams();
  const router = useRouter();
  const id = Number(params.id);

  const [exposure, setExposure] = useState<NircamExposure | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Editable fields
  const [reviewStatus, setReviewStatus] = useState<string>('pending');
  const [masking, setMasking] = useState<string>('none');
  const [correction, setCorrection] = useState<string>('none');
  const [notes, setNotes] = useState<string>('');

  // Sibling-exposure nav (from sessionStorage cache populated by the list).
  // Re-derived on every id change; null when there's no cache (direct entry).
  const [nav, setNav] = useState<NircamNavLookup | null>(null);
  const [showHelp, setShowHelp] = useState(false);
  useEffect(() => { setNav(lookupNircamNav(id)); }, [id]);

  const fetchExposure = useCallback(async () => {
    setLoading(true);
    setError(null);

    const result = await getNircamExposureById(id);
    if (result.error) {
      setError(result.error);
    } else if (result.exposure) {
      setExposure(result.exposure);
      setReviewStatus(result.exposure.review_status);
      setMasking(result.exposure.masking);
      setCorrection(result.exposure.correction);
      setNotes(result.exposure.notes || '');
    }
    setLoading(false);
  }, [id]);

  useEffect(() => {
    fetchExposure();
  }, [fetchExposure]);

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
    if (result.exposure) setExposure(result.exposure);
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
    router.push(`/admin/nircam/${targetId}`);
  }, [handleSave, router]);

  const handleNext = useCallback(() => goTo(nav?.next ?? null), [goTo, nav]);
  const handlePrev = useCallback(() => goTo(nav?.prev ?? null), [goTo, nav]);

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
        <p className="text-text-secondary dark:text-slate-400">Exposure not found.</p>
        <Link href="/admin/nircam" className="text-primary hover:underline mt-2 inline-block">
          Back to NIRCam
        </Link>
      </div>
    );
  }

  const pngUrl = previewUrl(exposure.png_path);
  const fullPngUrl = previewUrl(exposure.full_png_path);
  const editorAvailable = Boolean(
    fullPngUrl && exposure.image_width && exposure.image_height
  );

  const handleSaveMasks = async (regions: MaskRegionsPayload) => {
    const res = await saveExposureMaskRegions(exposure.id, regions);
    if (res.exposure) setExposure(res.exposure);
    return { error: res.error };
  };

  return (
    <div>
      {/* Header */}
      <div className="flex items-center gap-3 mb-6">
        <button onClick={() => router.push('/admin/nircam')} className="text-text-secondary dark:text-slate-400 hover:text-text-primary dark:hover:text-slate-100" title="Back to list">
          <ArrowLeft className="w-5 h-5" />
        </button>
        <div className="flex-1 min-w-0">
          <h1 className="text-xl font-semibold font-mono text-text-primary dark:text-slate-100 truncate">
            {exposure.filename}
          </h1>
          <p className="text-sm text-text-secondary dark:text-slate-400">
            {exposure.field} / {exposure.filter} / {exposure.detector}
          </p>
        </div>
        {nav && (
          <div className="flex items-center gap-1 text-sm text-text-secondary dark:text-slate-400">
            <button
              onClick={handlePrev}
              disabled={nav.prev == null}
              title="Previous (← / P)"
              className="p-1.5 rounded hover:bg-surface-hover dark:hover:bg-slate-800 disabled:opacity-30 disabled:cursor-not-allowed"
            >
              <ChevronLeft className="w-5 h-5" />
            </button>
            <span className="font-mono tabular-nums text-xs px-1">
              {nav.index} / {nav.total}
            </span>
            <button
              onClick={handleNext}
              disabled={nav.next == null}
              title="Next (→ / N)"
              className="p-1.5 rounded hover:bg-surface-hover dark:hover:bg-slate-800 disabled:opacity-30 disabled:cursor-not-allowed"
            >
              <ChevronRight className="w-5 h-5" />
            </button>
          </div>
        )}
        <button
          onClick={() => setShowHelp(prev => !prev)}
          title="Keyboard shortcuts (?)"
          className="p-1.5 rounded text-text-secondary dark:text-slate-400 hover:bg-surface-hover dark:hover:bg-slate-800"
        >
          <Keyboard className="w-5 h-5" />
        </button>
      </div>

      {showHelp && (
        <div className="mb-6 rounded-lg border border-border dark:border-slate-700 bg-card dark:bg-slate-900 p-4">
          <div className="flex items-center justify-between mb-2">
            <h2 className="text-sm font-semibold text-text-primary dark:text-slate-100">Keyboard shortcuts</h2>
            <button onClick={() => setShowHelp(false)} className="text-xs text-text-secondary dark:text-slate-400 hover:underline">close</button>
          </div>
          <dl className="grid grid-cols-2 gap-x-6 gap-y-1 text-sm">
            <div className="flex justify-between"><dt>Next exposure</dt><dd className="font-mono text-text-secondary dark:text-slate-400">→ or N</dd></div>
            <div className="flex justify-between"><dt>Previous</dt><dd className="font-mono text-text-secondary dark:text-slate-400">← or P</dd></div>
            <div className="flex justify-between"><dt>Mark pending</dt><dd className="font-mono text-text-secondary dark:text-slate-400">1</dd></div>
            <div className="flex justify-between"><dt>Mark approved</dt><dd className="font-mono text-text-secondary dark:text-slate-400">2</dd></div>
            <div className="flex justify-between"><dt>Mark excluded</dt><dd className="font-mono text-text-secondary dark:text-slate-400">3</dd></div>
            <div className="flex justify-between"><dt>Save</dt><dd className="font-mono text-text-secondary dark:text-slate-400">S</dd></div>
            <div className="flex justify-between"><dt>Help</dt><dd className="font-mono text-text-secondary dark:text-slate-400">?</dd></div>
          </dl>
          <p className="mt-2 text-xs text-text-secondary dark:text-slate-400">
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
        {/* PNG viewer / mask editor */}
        <div className="flex-1 min-w-0">
          <Card className="overflow-hidden">
            {editorAvailable ? (
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
              <div className="flex items-center justify-center py-24 text-text-secondary dark:text-slate-400">
                No PNG available
              </div>
            )}
          </Card>
        </div>

        {/* Sidebar */}
        <div className="w-80 flex-shrink-0 space-y-4">
          {/* Metadata */}
          <Card className="p-4">
            <h2 className="text-sm font-medium text-text-secondary dark:text-slate-400 uppercase tracking-wider mb-3">
              Metadata
            </h2>
            <dl className="space-y-2 text-sm">
              <div className="flex justify-between">
                <dt className="text-text-secondary dark:text-slate-400">Field</dt>
                <dd className="text-text-primary dark:text-slate-100">{exposure.field}</dd>
              </div>
              <div className="flex justify-between">
                <dt className="text-text-secondary dark:text-slate-400">Filter</dt>
                <dd className="text-text-primary dark:text-slate-100">{exposure.filter}</dd>
              </div>
              <div className="flex justify-between">
                <dt className="text-text-secondary dark:text-slate-400">Detector</dt>
                <dd className="text-text-primary dark:text-slate-100">{exposure.detector}</dd>
              </div>
              <div className="flex justify-between">
                <dt className="text-text-secondary dark:text-slate-400">Visit</dt>
                <dd className="text-text-primary dark:text-slate-100 font-mono text-xs">{exposure.visit || '—'}</dd>
              </div>
              <div className="flex justify-between">
                <dt className="text-text-secondary dark:text-slate-400">Date</dt>
                <dd className="text-text-primary dark:text-slate-100">{exposure.date_obs || '—'}</dd>
              </div>
              <div className="flex justify-between">
                <dt className="text-text-secondary dark:text-slate-400">Stage</dt>
                <dd>
                  <span className={`inline-flex px-2 py-0.5 text-xs font-medium rounded-full font-mono ${stageBadgeClasses(exposure.stage)}`}>
                    {exposure.stage}
                  </span>
                </dd>
              </div>
              {(exposure.ra_center != null && exposure.dec_center != null) && (
                <div className="flex justify-between">
                  <dt className="text-text-secondary dark:text-slate-400">RA, Dec</dt>
                  <dd className="text-text-primary dark:text-slate-100 font-mono text-xs">
                    {exposure.ra_center.toFixed(5)}, {exposure.dec_center.toFixed(5)}
                  </dd>
                </div>
              )}
            </dl>
          </Card>

          {/* Triage controls */}
          <Card className="p-4">
            <h2 className="text-sm font-medium text-text-secondary dark:text-slate-400 uppercase tracking-wider mb-3">
              Triage
            </h2>
            <div className="space-y-3">
              <div>
                <label className="block text-xs font-medium text-text-secondary dark:text-slate-400 mb-1">
                  Review Status
                </label>
                <select
                  value={reviewStatus}
                  onChange={(e) => setReviewStatus(e.target.value)}
                  className="w-full text-sm border border-border dark:border-slate-600 rounded-lg px-3 py-2 bg-white dark:bg-slate-800 text-text-primary dark:text-slate-100"
                >
                  <option value="pending">Pending</option>
                  <option value="approved">Approved</option>
                  <option value="excluded">Excluded</option>
                </select>
              </div>

              <div>
                <label className="block text-xs font-medium text-text-secondary dark:text-slate-400 mb-1">
                  Masking
                </label>
                <select
                  value={masking}
                  onChange={(e) => setMasking(e.target.value)}
                  className="w-full text-sm border border-border dark:border-slate-600 rounded-lg px-3 py-2 bg-white dark:bg-slate-800 text-text-primary dark:text-slate-100"
                >
                  <option value="none">None</option>
                  <option value="needed">Needed</option>
                  <option value="done">Done</option>
                </select>
              </div>

              <div>
                <label className="block text-xs font-medium text-text-secondary dark:text-slate-400 mb-1">
                  Correction
                </label>
                <select
                  value={correction}
                  onChange={(e) => setCorrection(e.target.value)}
                  className="w-full text-sm border border-border dark:border-slate-600 rounded-lg px-3 py-2 bg-white dark:bg-slate-800 text-text-primary dark:text-slate-100"
                >
                  <option value="none">None</option>
                  <option value="needed">Needed</option>
                  <option value="done">Done</option>
                </select>
              </div>

              <div>
                <label className="block text-xs font-medium text-text-secondary dark:text-slate-400 mb-1">
                  Notes
                </label>
                <textarea
                  value={notes}
                  onChange={(e) => setNotes(e.target.value)}
                  placeholder="Describe artifacts, masking needs, etc."
                  rows={4}
                  className="w-full text-sm border border-border dark:border-slate-600 rounded-lg px-3 py-2 bg-white dark:bg-slate-800 text-text-primary dark:text-slate-100 resize-none"
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
