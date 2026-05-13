'use client';

import React, { useState, useEffect, useCallback } from 'react';
import { useParams, useRouter } from 'next/navigation';
import Link from 'next/link';
import { Card } from '@/components/ui/Card';
import { Button } from '@/components/ui/Button';
import { Loader2, ArrowLeft, Save, Check } from 'lucide-react';
import {
  getNircamExposureById,
  updateExposureReview,
} from '@/lib/actions/nircam-exposures';
import type { NircamExposure } from '@/lib/types';
import { stageBadgeClasses } from '@/lib/nircam-stages';

const CDN_BASE = process.env.NEXT_PUBLIC_CDN_BASE_URL || '';

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

  const handleSave = async () => {
    setSaving(true);
    setSaved(false);
    setError(null);

    const result = await updateExposureReview(id, {
      review_status: reviewStatus as NircamExposure['review_status'],
      masking: masking as NircamExposure['masking'],
      correction: correction as NircamExposure['correction'],
      notes: notes || undefined,
    });

    if (result.error) {
      setError(result.error);
    } else if (result.exposure) {
      setExposure(result.exposure);
      setSaved(true);
      setTimeout(() => setSaved(false), 2000);
    }
    setSaving(false);
  };

  const hasChanges = exposure && (
    reviewStatus !== exposure.review_status ||
    masking !== exposure.masking ||
    correction !== exposure.correction ||
    notes !== (exposure.notes || '')
  );

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

  const pngUrl = exposure.png_path ? `${CDN_BASE}/${exposure.png_path}` : null;

  return (
    <div>
      {/* Header */}
      <div className="flex items-center gap-3 mb-6">
        <button onClick={() => router.push('/admin/nircam')} className="text-text-secondary dark:text-slate-400 hover:text-text-primary dark:hover:text-slate-100">
          <ArrowLeft className="w-5 h-5" />
        </button>
        <div>
          <h1 className="text-xl font-semibold font-mono text-text-primary dark:text-slate-100">
            {exposure.filename}
          </h1>
          <p className="text-sm text-text-secondary dark:text-slate-400">
            {exposure.field} / {exposure.filter} / {exposure.detector}
          </p>
        </div>
      </div>

      {error && (
        <div className="bg-red-50 dark:bg-red-950 border border-red-200 dark:border-red-900 rounded-lg p-4 mb-6">
          <p className="text-red-800 dark:text-red-400">{error}</p>
        </div>
      )}

      <div className="flex gap-6">
        {/* PNG viewer */}
        <div className="flex-1 min-w-0">
          <Card className="overflow-hidden">
            {pngUrl ? (
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
