'use client';

import React, { useState, useCallback, useRef } from 'react';
import { ChevronDown, ChevronRight, AlertCircle, Loader2, Download } from 'lucide-react';
import { FilterChip, type FilterOption } from '@/components/ui/FilterChip';
import { SpectrumPlot } from './SpectrumPlot';
import { downloadSingleFile } from './DownloadButtons';
import { formatExposureTime } from './plotting-utils';
import { DQ_FLAGS, decodeBitmask, encodeBitmask } from '@/lib/flags';
import { useAuth } from '@/lib/contexts/AuthContext';
import type { Spectrum } from '@/lib/types';

const DQ_OPTIONS: FilterOption[] = DQ_FLAGS.map(f => ({
  value: f.value,
  label: f.label,
  icon: f.icon,
  color: f.color,
}));

interface SpectrumDetailCardProps {
  spectrum: Spectrum;
  targetId: string;
  expanded: boolean;
  onToggle: () => void;
  /** Color dot keyed to member target (sidebar palette). */
  color?: string;
  /** Object-level redshift used to overlay emission lines on the spectrum. */
  objectRedshift: number | null;
  /** Cycles through inspection-mode shortcuts; safe to leave undefined. */
  cardId?: string;
}

export const SpectrumDetailCard: React.FC<SpectrumDetailCardProps> = ({
  spectrum,
  targetId,
  expanded,
  onToggle,
  color,
  objectRedshift,
  cardId,
}) => {
  const { user, userProfile } = useAuth();
  const canEdit = !!(user && userProfile?.can_comment);

  // Local DQ bitmask: optimistic state mirrors the server, snapped back on failure.
  const [dqBitmask, setDqBitmask] = useState<number>(spectrum.dq_flags ?? 0);
  const [dqSaving, setDqSaving] = useState(false);
  const [dqError, setDqError] = useState<string | null>(null);
  const inflight = useRef<AbortController | null>(null);

  const [downloading, setDownloading] = useState(false);
  const [downloadError, setDownloadError] = useState<string | null>(null);

  const handleDqChange = useCallback(async (next: (string | number)[]) => {
    if (!canEdit) return;
    const nextMask = encodeBitmask(next);
    const prevMask = dqBitmask;
    setDqBitmask(nextMask);
    setDqError(null);
    setDqSaving(true);

    inflight.current?.abort();
    const ctl = new AbortController();
    inflight.current = ctl;

    try {
      const res = await fetch(`/api/spectra/${spectrum.id}/dq`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ dq_flags: nextMask }),
        signal: ctl.signal,
      });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body.error || `Failed to save DQ flags (${res.status})`);
      }
    } catch (err) {
      if ((err as Error).name === 'AbortError') return;
      setDqBitmask(prevMask);
      setDqError(err instanceof Error ? err.message : 'Failed to save DQ flags');
    } finally {
      if (inflight.current === ctl) {
        setDqSaving(false);
        inflight.current = null;
      }
    }
  }, [canEdit, dqBitmask, spectrum.id]);

  const handleDownload = useCallback(async () => {
    setDownloading(true);
    setDownloadError(null);
    try {
      await downloadSingleFile(spectrum.fits_path);
    } catch (err) {
      setDownloadError(err instanceof Error ? err.message : 'Download failed');
    } finally {
      setDownloading(false);
    }
  }, [spectrum.fits_path]);

  const dqSelected = decodeBitmask(dqBitmask, DQ_FLAGS);

  return (
    <div
      id={cardId}
      className="border border-border dark:border-slate-700 rounded-lg overflow-hidden bg-card dark:bg-slate-800"
    >
      {/* Header row — always visible. Compact summary. */}
      <button
        onClick={onToggle}
        className="w-full flex flex-wrap items-center gap-x-4 gap-y-2 px-4 py-3 text-left hover:bg-card-hover dark:hover:bg-slate-700/50 transition-colors"
        aria-expanded={expanded}
      >
        <span className="flex items-center gap-2 text-text-primary dark:text-slate-100">
          {expanded ? (
            <ChevronDown className="w-4 h-4 text-text-secondary dark:text-slate-400" />
          ) : (
            <ChevronRight className="w-4 h-4 text-text-secondary dark:text-slate-400" />
          )}
          <span
            className="inline-block w-2.5 h-2.5 rounded-full flex-shrink-0"
            style={{ backgroundColor: color ?? '#94a3b8' }}
          />
          <span className="text-sm font-mono font-semibold tracking-tight">
            {spectrum.grating}
          </span>
        </span>

        <span className="text-sm font-mono text-text-primary dark:text-slate-200">
          {targetId}
        </span>

        <div className="flex items-center gap-3 text-sm text-text-secondary dark:text-slate-400 ml-auto">
          <span>
            <span className="opacity-70">z=</span>
            <span className="font-mono text-text-primary dark:text-slate-200">
              {spectrum.redshift_auto != null ? spectrum.redshift_auto.toFixed(4) : '—'}
            </span>
          </span>
          <span>
            <span className="opacity-70">S/N=</span>
            <span className="font-mono text-text-primary dark:text-slate-200">
              {spectrum.signal_to_noise != null ? spectrum.signal_to_noise.toFixed(1) : '—'}
            </span>
          </span>
          <span>
            <span className="opacity-70">t=</span>
            <span className="font-mono text-text-primary dark:text-slate-200">
              {formatExposureTime(spectrum.exposure_time)}
            </span>
          </span>
        </div>
      </button>

      {/* Expandable body — single border-t separator, no nested card. */}
      {expanded && (
        <div className="border-t border-border dark:border-slate-700">
          {/* Metadata + actions row (sits above the controls bar). */}
          <div className="flex flex-wrap items-center gap-x-4 gap-y-2 px-4 py-2.5 bg-card dark:bg-slate-800 text-xs text-text-secondary dark:text-slate-400">
            <div className="flex items-center gap-2">
              <FilterChip
                label="DQ"
                options={DQ_OPTIONS}
                selected={dqSelected}
                onChange={handleDqChange}
                disabled={!canEdit || dqSaving}
                dropdownPlacement="bottom"
              />
              {dqSaving && (
                <Loader2 className="w-3.5 h-3.5 animate-spin text-text-secondary dark:text-slate-400" />
              )}
            </div>

            <span className="flex items-center gap-1.5">
              <span className="opacity-70 uppercase tracking-wide">Version:</span>
              <span className="font-mono text-text-primary dark:text-slate-200">
                {spectrum.reduction_version || '—'}
              </span>
            </span>

            <span
              className="flex items-center gap-1.5 min-w-0 max-w-[420px]"
              title={spectrum.fits_path}
            >
              <span className="opacity-70 uppercase tracking-wide">FITS:</span>
              <span className="font-mono text-text-primary dark:text-slate-200 truncate">
                {spectrum.spectrum_id}
              </span>
            </span>

            <button
              onClick={handleDownload}
              disabled={downloading}
              className="ml-auto inline-flex items-center gap-1.5 px-2.5 py-1 text-xs font-medium border border-border dark:border-slate-600 rounded hover:bg-card-hover dark:hover:bg-slate-700 text-text-primary dark:text-slate-200 disabled:opacity-50"
            >
              {downloading ? (
                <Loader2 className="w-3.5 h-3.5 animate-spin" />
              ) : (
                <Download className="w-3.5 h-3.5" />
              )}
              Download FITS
            </button>
          </div>

          {(dqError || downloadError) && (
            <div className="px-4 py-1.5 bg-red-50 dark:bg-red-950/40 border-t border-red-200 dark:border-red-900 flex items-center gap-2 text-xs text-red-800 dark:text-red-300">
              <AlertCircle className="w-3.5 h-3.5 flex-shrink-0" />
              {dqError || downloadError}
            </div>
          )}

          <SpectrumPlot
            bare
            fitsPath={spectrum.fits_path}
            grating={spectrum.grating}
            initialRedshift={objectRedshift}
          />
        </div>
      )}
    </div>
  );
};
